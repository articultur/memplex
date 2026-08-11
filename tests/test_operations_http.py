"""G006 HTTP liveness, readiness, and admission boundaries."""

from __future__ import annotations

import base64
import os
import threading

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

import memplex.adapters.http_api as http_api  # noqa: E402
from memplex.adapters.http_api import create_app  # noqa: E402
from memplex.config import MemplexConfig  # noqa: E402
from memplex.operations import load_operations_report  # noqa: E402


def _config(tmp_path) -> MemplexConfig:
    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(tmp_path / "store")
    config.llm.query_enhancement = False
    return config


def test_probe_routes_are_fixed_and_do_not_require_business_auth(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MEMPLEX_API_KEY", "super-secret-token")
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        live = client.get("/health/live")
        ready = client.get("/health/ready")
        business = client.get("/stats")

    assert live.status_code == 200
    assert live.json() == {"schema_version": 1, "status": "live"}
    assert ready.status_code == 200
    assert ready.json() == {
        "schema_version": 1,
        "status": "ready",
        "lifecycle": "ready",
        "storage": "ready",
    }
    assert business.status_code == 401
    rendered = live.text + ready.text
    assert "super-secret-token" not in rendered
    assert str(tmp_path) not in rendered


def test_draining_rejects_new_business_and_keeps_liveness(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("MEMPLEX_API_KEY", raising=False)
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        app.state.operations_admission.start_draining()
        app.state.memplex_service.begin_draining()
        assert client.get("/health/live").status_code == 200
        ready = client.get("/health/ready")
        rejected = client.get("/stats")

    assert ready.status_code == 503
    assert ready.json()["lifecycle"] == "draining"
    assert rejected.status_code == 503
    assert rejected.json() == {
        "schema_version": 1,
        "status": "draining",
    }


def test_health_diagnostic_never_exposes_storage_path(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("MEMPLEX_API_KEY", raising=False)
    with TestClient(create_app(_config(tmp_path))) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert "storage_path" not in response.json()
    assert str(tmp_path) not in response.text


def test_metrics_use_fixed_labels_and_do_not_run_health_scan(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("MEMPLEX_API_KEY", raising=False)
    app = create_app(_config(tmp_path))
    with TestClient(app) as client:
        service = app.state.memplex_service
        monkeypatch.setattr(
            service,
            "health",
            lambda: (_ for _ in ()).throw(AssertionError("metrics called health")),
        )
        assert client.get("/stats").status_code == 200
        assert client.get("/missing-route").status_code == 404
        response = client.get("/metrics")

    assert response.status_code == 200
    assert "memplex_http_requests_total" in response.text
    assert "memplex_http_request_duration_seconds_bucket" in response.text
    assert "memplex_runtime_state" in response.text
    assert "tenant" not in response.text.lower()
    assert str(tmp_path) not in response.text


def test_lifespan_writes_signed_measured_report_after_clean_drain(
    tmp_path, monkeypatch
) -> None:
    key = b"o" * 32
    output = tmp_path / "operations-evidence.json"
    monkeypatch.setenv(
        "MEMPLEX_OPERATIONS_HMAC_KEY", base64.b64encode(key).decode("ascii")
    )
    monkeypatch.setenv("MEMPLEX_G006_REPORT_OUTPUT", str(output))
    config = _config(tmp_path)
    config.operations.report_key_id = "ops-key"
    with TestClient(create_app(config)) as client:
        assert client.get("/stats").status_code == 200

    report = load_operations_report(output)
    report.verify(key)
    assert report.request_count == 1
    assert report.successful_requests == 1
    assert report.shutdown_drained is True
    assert report.industrial_gate_closing is True


def test_handler_failure_releases_admission_and_metrics_counters(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("MEMPLEX_API_KEY", raising=False)
    app = create_app(_config(tmp_path))

    @app.get("/_operations_test_boom")
    def _boom():
        raise RuntimeError("private-handler-detail")

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/_operations_test_boom")
        assert response.status_code == 500
        assert app.state.operations_admission.active == 0
        assert app.state.operations_metrics.snapshot()["in_flight"] == 0


def test_active_request_finishes_while_new_business_is_rejected_during_drain(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("MEMPLEX_API_KEY", raising=False)
    app = create_app(_config(tmp_path))
    entered = threading.Event()
    release = threading.Event()

    @app.get("/_operations_test_slow")
    def _slow():
        entered.set()
        release.wait(timeout=2)
        return {"status": "finished"}

    with TestClient(app) as client:
        result: dict[str, object] = {}

        def request() -> None:
            response = client.get("/_operations_test_slow")
            result["status"] = response.status_code
            result["body"] = response.json()

        thread = threading.Thread(target=request)
        thread.start()
        assert entered.wait(timeout=1)
        app.state.operations_admission.start_draining()
        app.state.memplex_service.begin_draining()
        rejected = client.get("/stats")
        assert rejected.status_code == 503
        release.set()
        thread.join(timeout=2)
        assert not thread.is_alive()
        assert result == {"status": 200, "body": {"status": "finished"}}
        assert app.state.operations_admission.wait_for_zero(1.0) is True
        assert app.state.operations_admission.active == 0


def test_rate_limit_registry_is_bounded_and_fails_closed_for_new_clients(
    monkeypatch,
) -> None:
    monkeypatch.setattr(http_api, "_RATE_BUCKET_CAPACITY", 8)
    monkeypatch.setattr(http_api.time, "monotonic", lambda: 100.0)
    with http_api._rate_bucket_lock:
        http_api._rate_buckets.clear()
    try:
        for index in range(8):
            assert http_api._check_rate_limit(f"198.51.100.{index}") is True
        assert http_api._check_rate_limit("203.0.113.200") is False
        with http_api._rate_bucket_lock:
            assert len(http_api._rate_buckets) == 8
    finally:
        with http_api._rate_bucket_lock:
            http_api._rate_buckets.clear()


def test_rate_limit_registry_reclaims_expired_clients(monkeypatch) -> None:
    now = [100.0]
    monkeypatch.setattr(http_api, "_RATE_BUCKET_CAPACITY", 2)
    monkeypatch.setattr(http_api.time, "monotonic", lambda: now[0])
    with http_api._rate_bucket_lock:
        http_api._rate_buckets.clear()
    try:
        assert http_api._check_rate_limit("198.51.100.1") is True
        assert http_api._check_rate_limit("198.51.100.2") is True
        now[0] += http_api._RATE_LIMIT_WINDOW + 1
        assert http_api._check_rate_limit("203.0.113.1") is True
        with http_api._rate_bucket_lock:
            assert set(http_api._rate_buckets) == {"203.0.113.1"}
    finally:
        with http_api._rate_bucket_lock:
            http_api._rate_buckets.clear()
