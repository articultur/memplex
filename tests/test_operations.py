"""G006 production operations contracts."""

from __future__ import annotations

import base64
import json
import threading
import time

import pytest

from memplex.operations import (
    OperationsEvidenceError,
    OperationsEvidenceReport,
    OperationsMetrics,
    RequestAdmission,
    RuntimeLifecycle,
    load_operations_signing_key,
)


def _key() -> bytes:
    return b"o" * 32


def _report() -> OperationsEvidenceReport:
    return OperationsEvidenceReport.create(
        report_id="018f7f1d-7c9e-7c31-9d34-35f6a91e2bb8",
        window_started_at="2026-08-11T12:00:00.000000Z",
        window_ended_at="2026-08-11T12:05:00.000000Z",
        request_count=1000,
        successful_requests=999,
        availability=0.999,
        error_rate=0.001,
        p95_latency_ms=120.5,
        availability_target=0.999,
        error_rate_target=0.001,
        p95_latency_target_ms=250.0,
        shutdown_drained=True,
        shutdown_deadline_exceeded=False,
        alert_rules_sha256="a" * 64,
        key_id="ops-2026-08",
        signing_key=_key(),
    )


def test_runtime_lifecycle_is_monotonic_and_terminal() -> None:
    lifecycle = RuntimeLifecycle()
    assert lifecycle.state == "starting"
    lifecycle.mark_ready()
    lifecycle.start_draining()
    lifecycle.mark_stopped()
    assert lifecycle.state == "stopped"
    with pytest.raises(RuntimeError, match="operations_lifecycle_transition_invalid"):
        lifecycle.mark_ready()


def test_runtime_lifecycle_fault_never_returns_ready() -> None:
    lifecycle = RuntimeLifecycle()
    lifecycle.mark_faulted()
    assert lifecycle.state == "faulted"
    with pytest.raises(RuntimeError, match="operations_lifecycle_transition_invalid"):
        lifecycle.start_draining()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("schema_version", True),
        ("request_count", True),
        ("availability", float("nan")),
        ("error_rate", float("inf")),
        ("p95_latency_ms", -1.0),
        ("alert_rules_sha256", "A" * 64),
    ),
)
def test_operations_report_rejects_weak_or_noncanonical_fields(field, value) -> None:
    raw = _report().to_dict()
    raw[field] = value
    with pytest.raises(OperationsEvidenceError, match="operations_report_invalid"):
        OperationsEvidenceReport.from_dict(raw)


def test_operations_report_rejects_unknown_and_duplicate_json_keys() -> None:
    raw = _report().to_dict()
    raw["future"] = True
    with pytest.raises(OperationsEvidenceError, match="operations_report_invalid"):
        OperationsEvidenceReport.from_dict(raw)

    payload = b'{"schema_version":1,"schema_version":1}'
    with pytest.raises(OperationsEvidenceError, match="operations_report_invalid"):
        OperationsEvidenceReport.from_json(payload)


def test_operations_report_signature_is_detached_and_tamper_evident() -> None:
    report = _report()
    report.verify(_key())
    raw = report.to_dict()
    raw["alert_rules_sha256"] = "b" * 64
    tampered = OperationsEvidenceReport.from_dict(raw)
    with pytest.raises(OperationsEvidenceError, match="operations_report_signature_invalid"):
        tampered.verify(_key())


@pytest.mark.parametrize("raw", ("bad", base64.b64encode(b"short").decode("ascii")))
def test_operations_signing_key_is_canonical_base64_32_bytes(monkeypatch, raw) -> None:
    monkeypatch.setenv("MEMPLEX_OPERATIONS_HMAC_KEY", raw)
    with pytest.raises(OperationsEvidenceError, match="operations_signing_key_invalid"):
        load_operations_signing_key()


def test_operations_report_roundtrip_has_no_secret_or_path(monkeypatch) -> None:
    encoded = base64.b64encode(_key()).decode("ascii")
    monkeypatch.setenv("MEMPLEX_OPERATIONS_HMAC_KEY", encoded)
    assert load_operations_signing_key() == _key()
    payload = _report().to_json()
    assert encoded.encode("ascii") not in payload
    assert b"/Users/" not in payload
    parsed = OperationsEvidenceReport.from_json(payload)
    parsed.verify(_key())
    assert json.loads(payload)["industrial_gate_closing"] is True


def test_operations_metrics_are_bounded_low_cardinality_and_finite() -> None:
    metrics = OperationsMetrics(max_latency_samples=4)
    for method, status, latency in (
        ("GET", 200, 0.010),
        ("POST", 503, 0.200),
        ("DELETE", 404, 0.050),
        ("CUSTOM-WITH-TENANT-ID", 500, 0.500),
        ("GET", 200, 0.020),
    ):
        metrics.begin_request()
        metrics.finish_request(method, status, latency)

    snapshot = metrics.snapshot()
    assert snapshot["request_count"] == 5
    assert snapshot["successful_requests"] == 3
    assert snapshot["in_flight"] == 0
    assert snapshot["latency_sample_count"] == 4
    rendered = metrics.render_prometheus(
        {
            "runtime_state": "ready",
            "worker_pending": 1,
            "worker_leased": 2,
            "worker_dead_letters": 3,
            "sync_pending": 4,
            "sync_leased": 5,
            "sync_dead_letters": 6,
            "pool_business_leases": 1,
            "pool_high_watermark": 2,
            "pool_max_connections": 4,
            "shutdown_deadline_exceeded_total": 0,
        }
    )
    assert 'method="OTHER"' in rendered
    assert 'status_class="5xx"' in rendered
    assert "tenant" not in rendered.lower()
    assert "CUSTOM-WITH-TENANT-ID" not in rendered
    assert "/Users/" not in rendered
    assert "nan" not in rendered.lower()
    assert "inf" not in rendered.lower().replace('+inf', '')


def test_operations_metrics_10000_requests_keep_fixed_series_and_sample_bound() -> None:
    metrics = OperationsMetrics(max_latency_samples=128)
    for index in range(10_000):
        metrics.begin_request()
        metrics.finish_request(
            "GET" if index % 2 == 0 else f"tenant-{index}",
            200 if index % 100 else 503,
            (index % 25) / 1000,
        )
    snapshot = metrics.snapshot()
    assert snapshot["request_count"] == 10_000
    assert snapshot["latency_sample_count"] == 128
    rendered = metrics.render_prometheus({"runtime_state": "ready"})
    request_series = [
        line
        for line in rendered.splitlines()
        if line.startswith("memplex_http_requests_total{")
    ]
    assert len(request_series) == 6 * 5
    assert "tenant-" not in rendered


def test_request_admission_drains_existing_work_and_rejects_new_work() -> None:
    admission = RequestAdmission()
    entered = threading.Event()
    release = threading.Event()

    def request() -> None:
        assert admission.begin() is True
        entered.set()
        release.wait(timeout=2)
        admission.end()

    thread = threading.Thread(target=request)
    thread.start()
    assert entered.wait(timeout=1)
    admission.start_draining()
    assert admission.begin() is False
    assert admission.wait_for_zero(0.01) is False
    release.set()
    assert admission.wait_for_zero(1.0) is True
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert admission.active == 0
    assert admission.accepting is False


def test_request_admission_deadline_is_bounded_and_balance_is_strict() -> None:
    admission = RequestAdmission()
    assert admission.begin() is True
    started = time.monotonic()
    assert admission.wait_for_zero(0.01) is False
    assert time.monotonic() - started < 0.5
    admission.end()
    with pytest.raises(RuntimeError, match="operations_request_admission_unbalanced"):
        admission.end()
