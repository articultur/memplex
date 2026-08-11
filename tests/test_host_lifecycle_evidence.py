from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from importlib.metadata import version

import pytest

from memplex.host_lifecycle import (
    HostLifecycleEvidence,
    HostLifecycleIntegrityError,
    read_host_lifecycle_evidence,
    write_host_lifecycle_evidence,
)


def _versions() -> dict[str, str]:
    return {
        "claude-code": "2.1.224",
        "codex": "0.147.0-alpha.6.5",
        "hermes": "0.20.0 (2026.8.3)",
        "openclaw": "2026.7.1 (2d2ddc4)",
    }


def test_host_lifecycle_evidence_round_trip_and_tamper_rejection(tmp_path):
    key = b"h" * 32
    evidence = HostLifecycleEvidence.create(
        memplex_version=version("memplex"),
        cli_versions=_versions(),
        key_id="g008-local",
        signing_key=key,
    )
    path = tmp_path / "hosts.json"
    write_host_lifecycle_evidence(path, evidence)
    loaded = read_host_lifecycle_evidence(path)
    loaded.verify(key, expected_version=version("memplex"))
    assert [item.host for item in loaded.hosts] == [
        "claude-code",
        "codex",
        "hermes",
        "openclaw",
    ]

    payload = json.loads(path.read_text())
    payload["hosts"][0]["cli_version"] = "stale"
    path.write_text(json.dumps(payload))
    with pytest.raises(HostLifecycleIntegrityError):
        read_host_lifecycle_evidence(path).verify(key, expected_version=version("memplex"))


def test_host_lifecycle_evidence_rejects_missing_host_and_symlink(tmp_path):
    key = b"h" * 32
    with pytest.raises(HostLifecycleIntegrityError, match="integrity"):
        HostLifecycleEvidence.create(
            memplex_version=version("memplex"),
            cli_versions={key: value for key, value in _versions().items() if key != "hermes"},
            key_id="g008-local",
            signing_key=key,
        )

    evidence = HostLifecycleEvidence.create(
        memplex_version=version("memplex"),
        cli_versions=_versions(),
        key_id="g008-local",
        signing_key=key,
    )
    target = tmp_path / "target.json"
    target.write_bytes(evidence.canonical_bytes())
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(HostLifecycleIntegrityError):
        read_host_lifecycle_evidence(link)


def test_host_lifecycle_evidence_writer_rejects_symlink_ancestor(tmp_path):
    key = b"h" * 32
    evidence = HostLifecycleEvidence.create(
        memplex_version=version("memplex"),
        cli_versions=_versions(),
        key_id="g008-local",
        signing_key=key,
    )
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises(HostLifecycleIntegrityError):
        write_host_lifecycle_evidence(linked / "hosts.json", evidence)

    assert not (real / "hosts.json").exists()


def test_host_lifecycle_evidence_rejects_expired_and_future_reports(tmp_path):
    key = b"h" * 32
    now = datetime.now(timezone.utc)
    for generated_at in (
        now - timedelta(hours=25),
        now + timedelta(minutes=6),
    ):
        evidence = HostLifecycleEvidence.create(
            memplex_version=version("memplex"),
            cli_versions=_versions(),
            key_id="g008-local",
            signing_key=key,
            generated_at=generated_at,
        )
        path = tmp_path / f"hosts-{generated_at.timestamp()}.json"
        write_host_lifecycle_evidence(path, evidence)

        with pytest.raises(HostLifecycleIntegrityError):
            read_host_lifecycle_evidence(path).verify(
                key,
                expected_version=version("memplex"),
                now=now,
            )
