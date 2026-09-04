"""G006 standalone verifier behavior."""

from __future__ import annotations

import base64
import importlib.util
import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

from memplex.config import MemplexConfig
from memplex.operations import OperationsReadinessBinding, create_operations_evidence


def _script_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "verify_g006_operations_slo.py"
    spec = importlib.util.spec_from_file_location("verify_g006_operations_slo", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_report(tmp_path: Path) -> tuple[Path, str]:
    key = b"o" * 32
    config = MemplexConfig()
    config.operations.report_key_id = "ops-key"
    now = datetime.now(UTC)
    report = create_operations_evidence(
        metrics_snapshot={
            "request_count": 1000,
            "successful_requests": 999,
            "latency_sample_count": 128,
            "p95_latency_ms": 100.0,
        },
        shutdown_result={"request_drained": True, "deadline_exceeded": False},
        config=config,
        report_id="018f7f1d-7c9e-7c31-9d34-35f6a91e2bb8",
        window_started_at=(now - timedelta(seconds=301)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        window_ended_at=(now - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        generated_at=now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        readiness_binding=OperationsReadinessBinding(
            deployment_id="production-us-east-1",
            source_sha256="1" * 64,
            artifact_sha256="2" * 64,
            target_identity_sha256="3" * 64,
            expected_key_id="ops-key",
        ),
        signing_key=key,
    )
    path = tmp_path / "private-g006-report.json"
    path.write_bytes(report.to_json())
    return path, base64.b64encode(key).decode("ascii")


def _set_binding_environment(monkeypatch) -> None:
    monkeypatch.setenv("MEMPLEX_DEPLOYMENT_ID", "production-us-east-1")
    monkeypatch.setenv("MEMPLEX_SOURCE_SHA256", "1" * 64)
    monkeypatch.setenv("MEMPLEX_ARTIFACT_SHA256", "2" * 64)
    monkeypatch.setenv("MEMPLEX_TARGET_IDENTITY_SHA256", "3" * 64)
    monkeypatch.setenv("MEMPLEX_OPERATIONS_REPORT_KEY_ID", "ops-key")


def test_standalone_verifier_accepts_current_deployment_evidence(
    tmp_path, monkeypatch, capsys
) -> None:
    verifier = _script_module()
    path, secret = _write_report(tmp_path)
    monkeypatch.setenv("MEMPLEX_OPERATIONS_HMAC_KEY", secret)
    _set_binding_environment(monkeypatch)
    monkeypatch.setattr(
        "sys.argv",
        ["verify_g006_operations_slo.py", "--report", str(path), "--config", str(tmp_path / "missing.yaml")],
    )

    assert verifier.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verified"] is True
    rendered = json.dumps(payload, sort_keys=True)
    assert str(path) not in rendered
    assert secret not in rendered
    assert "production-us-east-1" not in rendered
    assert "1" * 64 not in rendered
    assert "2" * 64 not in rendered
    assert "3" * 64 not in rendered


def test_standalone_verifier_rejects_cross_deployment_without_leaking_binding(
    tmp_path, monkeypatch, capsys
) -> None:
    """A valid signature bound to another deployment must fail closed."""
    verifier = _script_module()
    path, secret = _write_report(tmp_path)
    monkeypatch.setenv("MEMPLEX_OPERATIONS_HMAC_KEY", secret)
    _set_binding_environment(monkeypatch)
    monkeypatch.setenv("MEMPLEX_DEPLOYMENT_ID", "other-production-deployment")
    monkeypatch.setattr(
        "sys.argv",
        ["verify_g006_operations_slo.py", "--report", str(path), "--config", str(tmp_path / "missing.yaml")],
    )

    assert verifier.main() == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"error": "operations_evidence_invalid", "verified": False}
    rendered = json.dumps(payload, sort_keys=True)
    assert str(path) not in rendered
    assert secret not in rendered
    assert "production-us-east-1" not in rendered
    assert "1" * 64 not in rendered
    assert "2" * 64 not in rendered
    assert "3" * 64 not in rendered
