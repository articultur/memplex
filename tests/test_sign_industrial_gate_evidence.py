"""G012 独立 verifier 结果签名工具的 fail-closed 契约。"""

from __future__ import annotations

import base64
import hashlib
import importlib.metadata
import importlib.util
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from memplex.readiness_evidence import read_industrial_gate_evidence


def _signer_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "sign_industrial_gate_evidence.py"
    spec = importlib.util.spec_from_file_location("sign_industrial_gate_evidence", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _set_environment(monkeypatch) -> str:
    key = base64.b64encode(b"k" * 32).decode("ascii")
    monkeypatch.setenv("MEMPLEX_SOURCE_SHA256", "1" * 64)
    monkeypatch.setenv("MEMPLEX_ARTIFACT_SHA256", "2" * 64)
    monkeypatch.setenv("MEMPLEX_DEPLOYMENT_ID", "production-us-east-1")
    monkeypatch.setenv("MEMPLEX_TARGET_IDENTITY_SHA256", "3" * 64)
    monkeypatch.setenv("MEMPLEX_INDUSTRIAL_EVIDENCE_HMAC_KEY", key)
    monkeypatch.setenv("MEMPLEX_INDUSTRIAL_EVIDENCE_KEY_ID", "g012-operator-v1")
    return key


def _invoke(monkeypatch, args: list[str]) -> int:
    signer = _signer_module()
    monkeypatch.setattr("sys.argv", ["sign_industrial_gate_evidence.py", *args])
    return signer.main()


def _valid_verifier_result(gate_id: str) -> dict[str, object]:
    signer = _signer_module()
    verifier_id, required_checks = signer._VERIFIER_CONTRACTS[gate_id]
    now = datetime.now(timezone.utc)
    return {
        "schema_version": 1,
        "gate_id": gate_id,
        "verifier_id": verifier_id,
        "verifier_contract_sha256": signer._verifier_contract_sha256(gate_id),
        "status": "passed",
        "memplex_version": importlib.metadata.version("memplex"),
        "source_sha256": "1" * 64,
        "artifact_sha256": "2" * 64,
        "deployment_id": "production-us-east-1",
        "target_identity_sha256": "3" * 64,
        "started_at": (now - timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "completed_at": (now - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "checks": [
            {
                "id": check_id,
                "status": "passed",
                "evidence_sha256": hashlib.sha256(check_id.encode()).hexdigest(),
            }
            for check_id in required_checks
        ],
    }


def _write_valid_verifier_result(path: Path, gate_id: str) -> bytes:
    raw = (
        json.dumps(
            _valid_verifier_result(gate_id),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n"
    ).encode("utf-8")
    path.write_bytes(raw)
    return raw


def test_signs_only_complete_g003_and_g004_passed_verifier_results(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    key = _set_environment(monkeypatch)
    for gate_id in ("schema_migrations_atomicity", "durable_sync_backpressure"):
        run_result = tmp_path / f"{gate_id}-result.json"
        raw = _write_valid_verifier_result(run_result, gate_id)
        output = tmp_path / f"{gate_id}-evidence.json"

        assert _invoke(
            monkeypatch,
            [
                "--gate-id",
                gate_id,
                "--run-result",
                str(run_result),
                "--output",
                str(output),
                "--key-id",
                "g012-operator-v1",
            ],
        ) == 0
        assert json.loads(capsys.readouterr().out) == {
            "schema_version": 1,
            "status": "signed",
            "gate": gate_id,
        }
        evidence = read_industrial_gate_evidence(output)
        assert evidence.gate_id == gate_id
        assert evidence.status == "passed"
        assert evidence.memplex_version == importlib.metadata.version("memplex")
        assert evidence.run_result_sha256 == hashlib.sha256(raw).hexdigest()
        evidence.verify(
            expected_gate_id=gate_id,
            expected_binding=evidence.binding(),
            expected_key_id="g012-operator-v1",
            signing_key=base64.b64decode(key),
            now=datetime.now(timezone.utc),
            max_age=timedelta(minutes=1),
        )


def test_help_records_attestation_boundary(monkeypatch, capsys) -> None:
    signer = _signer_module()
    monkeypatch.setattr("sys.argv", ["sign_industrial_gate_evidence.py", "--help"])

    try:
        signer.main()
    except SystemExit as exc:
        assert exc.code == 0
    rendered = capsys.readouterr().out
    normalized = " ".join(rendered.split())
    assert "固定verifier合同" in normalized
    assert "所有必需检查均passed" in normalized
    assert "key holder承担attestation" in normalized


def test_rejects_arbitrary_failed_cross_binding_or_incomplete_verifier_results(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _set_environment(monkeypatch)
    gate_id = "schema_migrations_atomicity"
    valid = _valid_verifier_result(gate_id)

    def clone() -> dict[str, object]:
        return json.loads(json.dumps(valid))

    failed = clone()
    failed["status"] = "failed"
    wrong_gate = clone()
    wrong_gate["gate_id"] = "durable_sync_backpressure"
    wrong_binding = clone()
    wrong_binding["source_sha256"] = "9" * 64
    future_schema = clone()
    future_schema["schema_version"] = 2
    incomplete = clone()
    incomplete["checks"] = incomplete["checks"][:-1]
    failed_check = clone()
    failed_check["checks"][0]["status"] = "failed"
    extra = clone()
    extra["future_control"] = True
    stale = clone()
    stale["started_at"] = "2026-01-01T00:00:00.000000Z"
    stale["completed_at"] = "2026-01-01T00:05:00.000000Z"

    cases = (
        {"arbitrary_independent_verifier_payload": False},
        failed,
        wrong_gate,
        wrong_binding,
        future_schema,
        incomplete,
        failed_check,
        extra,
        stale,
    )
    for index, payload in enumerate(cases):
        run_result = tmp_path / f"invalid-{index}.json"
        run_result.write_text(json.dumps(payload), encoding="utf-8")
        output = tmp_path / f"invalid-{index}-evidence.json"
        assert _invoke(
            monkeypatch,
            [
                "--gate-id",
                gate_id,
                "--run-result",
                str(run_result),
                "--output",
                str(output),
                "--key-id",
                "g012-operator-v1",
            ],
        ) == 2
        assert json.loads(capsys.readouterr().out)["error"] == (
            "industrial_gate_evidence_invalid"
        )
        assert not output.exists()


def test_rejects_duplicate_json_keys_and_unexpected_key_id(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    _set_environment(monkeypatch)
    gate_id = "schema_migrations_atomicity"
    valid = json.dumps(_valid_verifier_result(gate_id), separators=(",", ":"))
    duplicate = tmp_path / "duplicate.json"
    duplicate.write_text(valid[:-1] + ',"status":"passed"}', encoding="utf-8")
    output = tmp_path / "evidence.json"

    for result, key_id in (
        (duplicate, "g012-operator-v1"),
        (tmp_path / "valid.json", "different-key"),
    ):
        if not result.exists():
            _write_valid_verifier_result(result, gate_id)
        assert _invoke(
            monkeypatch,
            [
                "--gate-id",
                gate_id,
                "--run-result",
                str(result),
                "--output",
                str(output),
                "--key-id",
                key_id,
            ],
        ) == 2
        assert json.loads(capsys.readouterr().out)["status"] == "failed"
        assert not output.exists()


def test_missing_environment_fails_closed_without_disclosing_secret_or_paths(
    tmp_path: Path, monkeypatch, capsys
) -> None:
    run_result = tmp_path / "private-result.json"
    run_result.write_text("private-verifier-content", encoding="utf-8")
    output = tmp_path / "private-evidence.json"
    secret = "private-secret-never-rendered"
    monkeypatch.setenv("MEMPLEX_INDUSTRIAL_EVIDENCE_HMAC_KEY", secret)

    assert _invoke(
        monkeypatch,
        [
            "--gate-id",
            "schema_migrations_atomicity",
            "--run-result",
            str(run_result),
            "--output",
            str(output),
            "--key-id",
            "g012-operator-v1",
        ],
    ) == 2
    rendered = capsys.readouterr().out
    assert json.loads(rendered) == {
        "schema_version": 1,
        "status": "failed",
        "error": "industrial_gate_evidence_invalid",
    }
    assert str(run_result) not in rendered
    assert str(output) not in rendered
    assert secret not in rendered
    assert not output.exists()


def test_rejects_symlinked_run_result_ancestor_and_output(tmp_path: Path, monkeypatch, capsys) -> None:
    _set_environment(monkeypatch)
    actual = tmp_path / "actual"
    actual.mkdir()
    result = actual / "result.json"
    _write_valid_verifier_result(result, "schema_migrations_atomicity")
    linked_dir = tmp_path / "linked"
    linked_dir.symlink_to(actual, target_is_directory=True)
    output = tmp_path / "output.json"

    assert _invoke(
        monkeypatch,
        [
            "--gate-id",
            "schema_migrations_atomicity",
            "--run-result",
            str(linked_dir / result.name),
            "--output",
            str(output),
            "--key-id",
            "g012-operator-v1",
        ],
    ) == 2
    assert not output.exists()
    assert json.loads(capsys.readouterr().out)["status"] == "failed"

    linked_result = tmp_path / "linked-result.json"
    linked_result.symlink_to(result)
    assert _invoke(
        monkeypatch,
        [
            "--gate-id",
            "schema_migrations_atomicity",
            "--run-result",
            str(linked_result),
            "--output",
            str(output),
            "--key-id",
            "g012-operator-v1",
        ],
    ) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "failed"

    linked_output_dir = tmp_path / "linked-output"
    linked_output_dir.symlink_to(actual, target_is_directory=True)
    assert _invoke(
        monkeypatch,
        [
            "--gate-id",
            "schema_migrations_atomicity",
            "--run-result",
            str(result),
            "--output",
            str(linked_output_dir / "evidence.json"),
            "--key-id",
            "g012-operator-v1",
        ],
    ) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "failed"
    assert not (actual / "evidence.json").exists()

    output.symlink_to(result)
    assert _invoke(
        monkeypatch,
        [
            "--gate-id",
            "schema_migrations_atomicity",
            "--run-result",
            str(result),
            "--output",
            str(output),
            "--key-id",
            "g012-operator-v1",
        ],
    ) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "failed"


def test_rejects_invalid_gate_without_echoing_input(tmp_path: Path, monkeypatch, capsys) -> None:
    _set_environment(monkeypatch)
    run_result = tmp_path / "result.json"
    run_result.write_text("result", encoding="utf-8")
    forbidden = "invalid-gate-with-private-context"

    assert _invoke(
        monkeypatch,
        [
            "--gate-id",
            forbidden,
            "--run-result",
            str(run_result),
            "--output",
            str(tmp_path / "output.json"),
            "--key-id",
            "g012-operator-v1",
        ],
    ) == 2
    rendered = capsys.readouterr().out
    assert json.loads(rendered)["error"] == "industrial_gate_evidence_invalid"
    assert forbidden not in rendered


def test_rejects_non_regular_and_oversized_run_results(tmp_path: Path, monkeypatch, capsys) -> None:
    _set_environment(monkeypatch)
    directory = tmp_path / "not-a-file"
    directory.mkdir()
    output = tmp_path / "output.json"
    for run_result in (directory,):
        assert _invoke(
            monkeypatch,
            [
                "--gate-id",
                "schema_migrations_atomicity",
                "--run-result",
                str(run_result),
                "--output",
                str(output),
                "--key-id",
                "g012-operator-v1",
            ],
        ) == 2
        assert json.loads(capsys.readouterr().out)["status"] == "failed"

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * (16 * 1024 * 1024 + 1))
    assert _invoke(
        monkeypatch,
        [
            "--gate-id",
            "schema_migrations_atomicity",
            "--run-result",
            str(oversized),
            "--output",
            str(output),
            "--key-id",
            "g012-operator-v1",
        ],
    ) == 2
    assert json.loads(capsys.readouterr().out)["status"] == "failed"
