"""Data-only G006 operations CLI."""

from __future__ import annotations

import base64
import json
import zipfile

import memplex.adapters.cli as cli
from memplex.config import MemplexConfig
from memplex.operations import create_operations_evidence
from tests.test_storage_migrations import (
    _build_wheel,
    _install_wheel_in_isolated_venv,
    _run,
)


def _report(tmp_path):
    key = b"o" * 32
    config = MemplexConfig()
    config.operations.report_key_id = "ops-key"
    report = create_operations_evidence(
        metrics_snapshot={
            "request_count": 1000,
            "successful_requests": 999,
            "p95_latency_ms": 100.0,
        },
        shutdown_result={"request_drained": True, "deadline_exceeded": False},
        config=config,
        report_id="018f7f1d-7c9e-7c31-9d34-35f6a91e2bb8",
        window_started_at="2026-08-11T12:00:00.000000Z",
        window_ended_at="2026-08-11T12:05:00.000000Z",
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
    monkeypatch.setattr(
        cli,
        "_make_service",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("service built")),
    )
    assert cli.main(["--output", "json", "operations", "verify-report", str(path)]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["verified"] is True
    rendered = json.dumps(payload, sort_keys=True)
    assert str(path) not in rendered
    assert secret not in rendered


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
