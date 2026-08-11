"""G006 signed SLO evidence and shipped alert rules."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from memplex.config import MemplexConfig
from memplex.operations import (
    OperationsEvidenceError,
    alert_rules_bytes,
    alert_rules_sha256,
    create_operations_evidence,
    load_operations_report,
    write_operations_report_atomic,
)


def _config() -> MemplexConfig:
    config = MemplexConfig()
    config.operations.report_key_id = "ops-key"
    return config


def _metrics(*, successful: int = 999, p95: float = 100.0) -> dict[str, object]:
    return {
        "request_count": 1000,
        "successful_requests": successful,
        "p95_latency_ms": p95,
    }


def _create(*, successful: int = 999, p95: float = 100.0, drained: bool = True):
    return create_operations_evidence(
        metrics_snapshot=_metrics(successful=successful, p95=p95),
        shutdown_result={
            "request_drained": drained,
            "deadline_exceeded": not drained,
        },
        config=_config(),
        report_id="018f7f1d-7c9e-7c31-9d34-35f6a91e2bb8",
        window_started_at="2026-08-11T12:00:00.000000Z",
        window_ended_at="2026-08-11T12:05:00.000000Z",
        signing_key=b"o" * 32,
    )


def test_operations_evidence_closes_only_when_all_targets_and_drain_pass() -> None:
    passing = _create()
    passing.verify(b"o" * 32)
    assert passing.industrial_gate_closing is True
    assert _create(successful=998).industrial_gate_closing is False
    assert _create(p95=251.0).industrial_gate_closing is False
    assert _create(drained=False).industrial_gate_closing is False


def test_packaged_and_operator_alert_rules_are_exact_and_low_cardinality() -> None:
    packaged = alert_rules_bytes()
    operator = Path("deploy/prometheus/memplex-alerts.yml").read_bytes()
    assert packaged == operator
    assert len(alert_rules_sha256()) == 64
    text = packaged.decode("utf-8")
    for name in (
        "MemplexNotReady",
        "MemplexErrorBudgetBurn",
        "MemplexP95LatencyHigh",
        "MemplexWorkerBacklog",
        "MemplexSyncBacklog",
        "MemplexDeadLettersPresent",
        "MemplexPoolSaturated",
        "MemplexShutdownDeadlineExceeded",
    ):
        assert f"alert: {name}" in text
    for forbidden in ("tenant_id", "subject_id", "workspace_id", "memory_id", "exception"):
        assert forbidden not in text


def test_signed_report_file_roundtrip_does_not_expose_key(tmp_path, monkeypatch) -> None:
    key = b"o" * 32
    encoded = base64.b64encode(key).decode("ascii")
    monkeypatch.setenv("MEMPLEX_OPERATIONS_HMAC_KEY", encoded)
    path = tmp_path / "operations-report.json"
    path.write_bytes(_create().to_json())
    loaded = load_operations_report(path)
    loaded.verify(key)
    assert encoded.encode("ascii") not in path.read_bytes()


def test_atomic_report_writer_rejects_a_symlink_in_any_ancestor(tmp_path) -> None:
    real = tmp_path / "real"
    nested = real / "nested"
    nested.mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    redirected = nested / "report.json"

    with pytest.raises(
        OperationsEvidenceError, match="operations_report_output_invalid"
    ):
        write_operations_report_atomic(link / "nested" / "report.json", _create())

    assert not redirected.exists()
