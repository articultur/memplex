"""Strict signed deployment evidence shared by the G003/G004 closure gates."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from memplex.readiness_evidence import (
    DeploymentEvidenceBinding,
    IndustrialGateEvidence,
    ReadinessEvidenceError,
    load_deployment_evidence_binding_from_environment,
    load_expected_key_id_from_environment,
    load_signing_key_from_environment,
    read_industrial_gate_evidence,
    write_industrial_gate_evidence,
)

KEY = b"k" * 32
KEY_ID = "g012-deployment-evidence-v1"
NOW = datetime(2026, 8, 12, 8, 30, tzinfo=UTC)


def _binding() -> DeploymentEvidenceBinding:
    return DeploymentEvidenceBinding.from_values(
        memplex_version="3.3.0",
        source_sha256="1" * 64,
        artifact_sha256="2" * 64,
        deployment_id="018f7f1d-7c9e-7c31-9d34-35f6a91e2bb8",
        target_identity_sha256="3" * 64,
    )


def _evidence(*, generated_at: datetime = NOW) -> IndustrialGateEvidence:
    return IndustrialGateEvidence.create(
        gate_id="schema_migrations_atomicity",
        binding=_binding(),
        run_result_sha256="4" * 64,
        key_id=KEY_ID,
        signing_key=KEY,
        generated_at=generated_at,
    )


def test_signed_gate_evidence_binds_all_explicit_deployment_values(tmp_path: Path) -> None:
    evidence = _evidence()
    path = tmp_path / "g003.json"
    write_industrial_gate_evidence(path, evidence)

    loaded = read_industrial_gate_evidence(path)
    loaded.verify(
        expected_gate_id="schema_migrations_atomicity",
        expected_binding=_binding(),
        expected_key_id=KEY_ID,
        signing_key=KEY,
        now=NOW + timedelta(seconds=1),
        max_age=timedelta(minutes=10),
    )
    assert loaded.status == "passed"
    assert loaded.gate_id == "schema_migrations_atomicity"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("memplex_version", "3.3.1"),
        ("source_sha256", "5" * 64),
        ("artifact_sha256", "5" * 64),
        ("deployment_id", "production-us-east"),
        ("target_identity_sha256", "5" * 64),
    ],
)
def test_evidence_verification_rejects_each_binding_mismatch(field: str, value: str) -> None:
    evidence = _evidence()
    values = {
        "memplex_version": "3.3.0",
        "source_sha256": "1" * 64,
        "artifact_sha256": "2" * 64,
        "deployment_id": "018f7f1d-7c9e-7c31-9d34-35f6a91e2bb8",
        "target_identity_sha256": "3" * 64,
    }
    values[field] = value
    changed = DeploymentEvidenceBinding.from_values(**values)

    with pytest.raises(ReadinessEvidenceError, match="industrial_gate_evidence_invalid"):
        evidence.verify(
            expected_gate_id="schema_migrations_atomicity",
            expected_binding=changed,
            expected_key_id=KEY_ID,
            signing_key=KEY,
            now=NOW,
            max_age=timedelta(minutes=10),
        )


def test_evidence_verification_rejects_unexpected_key_id() -> None:
    with pytest.raises(ReadinessEvidenceError, match="industrial_gate_evidence_invalid"):
        _evidence().verify(
            expected_gate_id="schema_migrations_atomicity",
            expected_binding=_binding(),
            expected_key_id="another-key",
            signing_key=KEY,
            now=NOW,
            max_age=timedelta(minutes=10),
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("gate_id", "capacity_chaos"),
        ("schema_version", 2),
        ("status", "failed"),
        ("source_sha256", "A" * 64),
        ("deployment_id", 4),
        ("run_result_sha256", 4),
        ("generated_at", 4),
    ],
)
def test_schema_rejects_unknown_future_and_weak_values(field: str, value: object) -> None:
    raw = _evidence().to_dict()
    raw[field] = value

    with pytest.raises(ReadinessEvidenceError, match="industrial_gate_evidence_invalid"):
        IndustrialGateEvidence.from_dict(raw)


def test_schema_rejects_unknown_or_missing_keys() -> None:
    raw = _evidence().to_dict()
    raw["unexpected"] = True
    with pytest.raises(ReadinessEvidenceError, match="industrial_gate_evidence_invalid"):
        IndustrialGateEvidence.from_dict(raw)

    raw = _evidence().to_dict()
    raw.pop("signature")
    with pytest.raises(ReadinessEvidenceError, match="industrial_gate_evidence_invalid"):
        IndustrialGateEvidence.from_dict(raw)


def test_json_duplicate_keys_and_tamper_fail_closed() -> None:
    evidence = _evidence()
    duplicate = (
        b'{"schema_version":1,"schema_version":1,'
        + evidence.canonical_bytes()[1:]
    )
    with pytest.raises(ReadinessEvidenceError, match="industrial_gate_evidence_invalid"):
        IndustrialGateEvidence.from_json(duplicate)

    raw = evidence.to_dict()
    raw["run_result_sha256"] = "5" * 64
    parsed = IndustrialGateEvidence.from_dict(raw)
    with pytest.raises(ReadinessEvidenceError, match="industrial_gate_evidence_invalid"):
        parsed.verify(
            expected_gate_id="schema_migrations_atomicity",
            expected_binding=_binding(),
            expected_key_id=KEY_ID,
            signing_key=KEY,
            now=NOW,
            max_age=timedelta(minutes=10),
        )


@pytest.mark.parametrize(
    "generated_at",
    [NOW - timedelta(minutes=11), NOW + timedelta(microseconds=1)],
)
def test_expired_and_future_evidence_fail_closed(generated_at: datetime) -> None:
    evidence = _evidence(generated_at=generated_at)
    with pytest.raises(ReadinessEvidenceError, match="industrial_gate_evidence_invalid"):
        evidence.verify(
            expected_gate_id="schema_migrations_atomicity",
            expected_binding=_binding(),
            expected_key_id=KEY_ID,
            signing_key=KEY,
            now=NOW,
            max_age=timedelta(minutes=10),
        )


def test_secure_file_io_rejects_symlink_and_never_echoes_path_or_key(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    redirected = tmp_path / "redirected"
    redirected.symlink_to(actual, target_is_directory=True)
    target = redirected / "g003.json"

    with pytest.raises(ReadinessEvidenceError) as exc_info:
        write_industrial_gate_evidence(target, _evidence())
    assert str(target) not in str(exc_info.value)
    assert KEY.hex() not in str(exc_info.value)
    assert not (actual / "g003.json").exists()

    path = tmp_path / "g003.json"
    write_industrial_gate_evidence(path, _evidence())
    linked = tmp_path / "linked.json"
    linked.symlink_to(path)
    with pytest.raises(ReadinessEvidenceError, match="industrial_gate_evidence_invalid"):
        read_industrial_gate_evidence(linked)


def test_serialized_evidence_is_a_canonical_signed_json_object() -> None:
    evidence = _evidence()
    rendered = evidence.canonical_bytes()
    assert json.loads(rendered) == evidence.to_dict()
    assert b" " not in rendered


def test_binding_accepts_non_uuid_deployment_identity_and_verification_rejects_gate_swap() -> None:
    binding = DeploymentEvidenceBinding.from_values(
        memplex_version="3.3.0",
        source_sha256="1" * 64,
        artifact_sha256="2" * 64,
        deployment_id="production-us-east",
        target_identity_sha256="3" * 64,
    )
    evidence = IndustrialGateEvidence.create(
        gate_id="durable_sync_backpressure",
        binding=binding,
        run_result_sha256="4" * 64,
        key_id=KEY_ID,
        signing_key=KEY,
        generated_at=NOW,
    )
    with pytest.raises(ReadinessEvidenceError, match="industrial_gate_evidence_invalid"):
        evidence.verify(
            expected_gate_id="schema_migrations_atomicity",
            expected_binding=binding,
            expected_key_id=KEY_ID,
            signing_key=KEY,
            now=NOW,
            max_age=timedelta(minutes=10),
        )


def test_signing_key_requires_exactly_32_bytes() -> None:
    for key in (b"k" * 31, b"k" * 33):
        with pytest.raises(ReadinessEvidenceError, match="industrial_gate_evidence_invalid"):
            IndustrialGateEvidence.create(
                gate_id="schema_migrations_atomicity",
                binding=_binding(),
                run_result_sha256="4" * 64,
                key_id=KEY_ID,
                signing_key=key,
                generated_at=NOW,
            )


def test_environment_loaders_require_exact_deployment_binding_key_and_key_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MEMPLEX_DEPLOYMENT_ID", "018f7f1d-7c9e-7c31-9d34-35f6a91e2bb8")
    monkeypatch.setenv("MEMPLEX_SOURCE_SHA256", "1" * 64)
    monkeypatch.setenv("MEMPLEX_ARTIFACT_SHA256", "2" * 64)
    monkeypatch.setenv("MEMPLEX_TARGET_IDENTITY_SHA256", "3" * 64)
    monkeypatch.setenv("G012_KEY", "a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s=")
    monkeypatch.setenv("G012_KEY_ID", KEY_ID)

    assert load_deployment_evidence_binding_from_environment(
        memplex_version="3.3.0"
    ) == _binding()
    assert load_signing_key_from_environment("G012_KEY") == KEY
    assert load_expected_key_id_from_environment("G012_KEY_ID") == KEY_ID


@pytest.mark.parametrize(
    ("env_name", "value"),
    [
        ("MEMPLEX_DEPLOYMENT_ID", ""),
        ("MEMPLEX_SOURCE_SHA256", "A" * 64),
        ("MEMPLEX_ARTIFACT_SHA256", "2" * 63),
        ("MEMPLEX_TARGET_IDENTITY_SHA256", ""),
    ],
)
def test_binding_environment_loader_rejects_missing_and_noncanonical_values(
    monkeypatch: pytest.MonkeyPatch, env_name: str, value: str
) -> None:
    monkeypatch.setenv("MEMPLEX_DEPLOYMENT_ID", "018f7f1d-7c9e-7c31-9d34-35f6a91e2bb8")
    monkeypatch.setenv("MEMPLEX_SOURCE_SHA256", "1" * 64)
    monkeypatch.setenv("MEMPLEX_ARTIFACT_SHA256", "2" * 64)
    monkeypatch.setenv("MEMPLEX_TARGET_IDENTITY_SHA256", "3" * 64)
    monkeypatch.setenv(env_name, value)

    with pytest.raises(ReadinessEvidenceError, match="industrial_gate_evidence_invalid"):
        load_deployment_evidence_binding_from_environment(memplex_version="3.3.0")

    monkeypatch.delenv(env_name)
    with pytest.raises(ReadinessEvidenceError, match="industrial_gate_evidence_invalid"):
        load_deployment_evidence_binding_from_environment(memplex_version="3.3.0")


@pytest.mark.parametrize(
    ("value", "env_name"),
    [
        ("a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s", "KEY"),
        ("a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s==", "KEY"),
        ("a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s=\n", "KEY"),
        ("a2tra2tra2tra2tra2tra2tra2tra2tra2tra2tra2s=", "KEY_ID"),
        (" ", "KEY_ID"),
    ],
)
def test_key_loaders_reject_noncanonical_or_missing_values(
    monkeypatch: pytest.MonkeyPatch, value: str, env_name: str
) -> None:
    monkeypatch.setenv(env_name, value)
    loader = load_signing_key_from_environment if env_name == "KEY" else load_expected_key_id_from_environment
    with pytest.raises(ReadinessEvidenceError, match="industrial_gate_evidence_invalid"):
        loader(env_name)

    monkeypatch.delenv(env_name)
    with pytest.raises(ReadinessEvidenceError, match="industrial_gate_evidence_invalid"):
        loader(env_name)
