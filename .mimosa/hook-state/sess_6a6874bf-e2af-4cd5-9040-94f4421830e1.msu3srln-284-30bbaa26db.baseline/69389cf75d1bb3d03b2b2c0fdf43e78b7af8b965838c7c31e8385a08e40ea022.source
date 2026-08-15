"""Data-only G006 operations CLI."""

from __future__ import annotations

import base64
import json
import zipfile
from datetime import datetime, timedelta, timezone

import memplex.adapters.cli as cli
from memplex.config import MemplexConfig
from memplex.operations import OperationsReadinessBinding, create_operations_evidence
from tests.test_storage_migrations import (
    _build_wheel,
    _install_wheel_in_isolated_venv,
    _run,
)

_DEPLOYMENT_ID = "00000000-0000-4000-8000-000000000001"
_SOURCE_SHA256 = "1" * 64
_ARTIFACT_SHA256 = "2" * 64
_TARGET_IDENTITY_SHA256 = "3" * 64


def _set_binding_environment(monkeypatch, *, key_id: str = "ops-key") -> None:
    monkeypatch.setenv("MEMPLEX_DEPLOYMENT_ID", _DEPLOYMENT_ID)
    monkeypatch.setenv("MEMPLEX_SOURCE_SHA256", _SOURCE_SHA256)
    monkeypatch.setenv("MEMPLEX_ARTIFACT_SHA256", _ARTIFACT_SHA256)
    monkeypatch.setenv("MEMPLEX_TARGET_IDENTITY_SHA256", _TARGET_IDENTITY_SHA256)
    monkeypatch.setenv("MEMPLEX_OPERATIONS_REPORT_KEY_ID", key_id)


def _report(
    tmp_path,
    *,
    evidence_age_seconds: int = 0,
    window_seconds: int = 300,
    request_count: int = 1000,
):
    key = b"o" * 32
    config = MemplexConfig()
    config.operations.report_key_id = "ops-key"
    generated_at = datetime.now(timezone.utc) - timedelta(seconds=evidence_age_seconds)
    window_ended_at = generated_at - timedelta(seconds=1)
    window_started_at = window_ended_at - timedelta(seconds=window_seconds)
    report = create_operations_evidence(
        metrics_snapshot={
            "request_count": request_count,
            "successful_requests": request_count - 1,
            "latency_sample_count": 128,
            "p95_latency_ms": 100.0,
        },
        shutdown_result={"request_drained": True, "deadline_exceeded": False},
        config=config,
        report_id="018f7f1d-7c9e-7c31-9d34-35f6a91e2bb8",
        window_started_at=window_started_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        window_ended_at=window_ended_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        generated_at=generated_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        readiness_binding=OperationsReadinessBinding(
            deployment_id=_DEPLOYMENT_ID,
            source_sha256=_SOURCE_SHA256,
            artifact_sha256=_ARTIFACT_SHA256,
            target_identity_sha256=_TARGET_IDENTITY_SHA256,
            expected_key_id="ops-key",
        ),
        signing_key=key,
    )
    path = tmp_path / "secret-report.json"
    path.write_bytes(report.to_json())
    return path, base64.b64encode(key).decode("ascii")


def test_operations_parser_exposes_status_verify_and_alerts() -> None:
    parser = cli.build_parser()
    assert parser.parse_args(["operations", "status"]).operations_command == "status"
    assert (
        parser.parse_args(["operations", "verify-report", "report.json"]).report
        == "report.json"
    )
    assert (
        parser.parse_args(["operations", "alerts-check"]).operations_command
        == "alerts-check"
    )


def test_operations_verify_is_data_only_and_redacted(tmp_path, monkeypatch, capsys) -> None:
    path, secret = _report(tmp_path)
    monkeypatch.setenv("MEMPLEX_OPERATIONS_HMAC_KEY", secret)
    _set_binding_environment(monkeypatch)
    monkeypatch.setattr(
        cli,
        "_make_service",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("service built")),
    )
    assert cli.main(
        [
            "--config",
            str(tmp_path / "missing-config.yaml"),
            "--output",
            "json",
            "operations",
            "verify-report",
            str(path),
        ]
    ) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verified"] is True
    rendered = json.dumps(payload, sort_keys=True)
    assert str(path) not in rendered
    assert secret not in rendered
    assert _DEPLOYMENT_ID not in rendered
    assert _SOURCE_SHA256 not in rendered
    assert _ARTIFACT_SHA256 not in rendered
    assert _TARGET_IDENTITY_SHA256 not in rendered


def test_operations_verify_report_rejects_all_readiness_binding_failures(
    tmp_path, monkeypatch, capsys
) -> None:
    """A signed report alone must not bypass current deployment readiness checks."""
    for mode in (
        "missing_binding",
        "stale",
        "short_window",
        "too_few_requests",
        "cross_deployment",
        "key_id_mismatch",
    ):
        for env_name in (
            "MEMPLEX_DEPLOYMENT_ID",
            "MEMPLEX_SOURCE_SHA256",
            "MEMPLEX_ARTIFACT_SHA256",
            "MEMPLEX_TARGET_IDENTITY_SHA256",
            "MEMPLEX_OPERATIONS_REPORT_KEY_ID",
        ):
            monkeypatch.delenv(env_name, raising=False)
        kwargs = {
            "evidence_age_seconds": 901 if mode == "stale" else 0,
            "window_seconds": 299 if mode == "short_window" else 300,
            "request_count": 999 if mode == "too_few_requests" else 1000,
        }
        path, secret = _report(tmp_path, **kwargs)
        monkeypatch.setenv("MEMPLEX_OPERATIONS_HMAC_KEY", secret)
        if mode != "missing_binding":
            _set_binding_environment(
                monkeypatch, key_id="different-ops-key" if mode == "key_id_mismatch" else "ops-key"
            )
        if mode == "cross_deployment":
            monkeypatch.setenv("MEMPLEX_DEPLOYMENT_ID", "different-production-deployment")

        assert cli.main(
            [
                "--config",
                str(tmp_path / "missing-config.yaml"),
                "--output",
                "json",
                "operations",
                "verify-report",
                str(path),
            ]
        ) == 1
        payload = json.loads(capsys.readouterr().out)
        assert payload == {"error": "operations_evidence_invalid"}
        rendered = json.dumps(payload, sort_keys=True)
        assert str(path) not in rendered
        assert secret not in rendered
        assert _DEPLOYMENT_ID not in rendered
        assert _SOURCE_SHA256 not in rendered
        assert _ARTIFACT_SHA256 not in rendered
        assert _TARGET_IDENTITY_SHA256 not in rendered
        for env_name in (
            "MEMPLEX_DEPLOYMENT_ID",
            "MEMPLEX_SOURCE_SHA256",
            "MEMPLEX_ARTIFACT_SHA256",
            "MEMPLEX_TARGET_IDENTITY_SHA256",
            "MEMPLEX_OPERATIONS_REPORT_KEY_ID",
        ):
            monkeypatch.delenv(env_name, raising=False)


def test_operations_invalid_report_has_fixed_error(tmp_path, monkeypatch, capsys) -> None:
    path = tmp_path / "private-report.json"
    path.write_text("{}", encoding="utf-8")
    secret = base64.b64encode(b"o" * 32).decode("ascii")
    monkeypatch.setenv("MEMPLEX_OPERATIONS_HMAC_KEY", secret)
    assert cli.main(["--output", "json", "operations", "verify-report", str(path)]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"error": "operations_evidence_invalid"}
    assert str(path) not in json.dumps(payload)


def test_operations_alert_rules_check_is_machine_readable(capsys) -> None:
    assert cli.main(["--output", "json", "operations", "alerts-check"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verified"] is True
    assert payload["rule_count"] == 8
    assert len(payload["sha256"]) == 64


def test_operations_alert_rules_ship_and_verify_from_isolated_wheel(tmp_path) -> None:
    wheel = _build_wheel(tmp_path)
    with zipfile.ZipFile(wheel) as archive:
        assert "memplex/operations_assets/memplex-alerts.yml" in archive.namelist()

    isolated = _install_wheel_in_isolated_venv(wheel, tmp_path)
    result = _run(
        isolated,
        "from memplex.adapters.cli import main; "
        "raise SystemExit(main(['--output','json','operations','alerts-check']))",
        tmp_path,
    )
    payload = json.loads(result.stdout)
    assert payload["verified"] is True
    assert payload["rule_count"] == 8
    assert len(payload["sha256"]) == 64
