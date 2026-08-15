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


def _set_deployment_binding(monkeypatch) -> dict[str, str]:
    binding = {
        "MEMPLEX_DEPLOYMENT_ID": "00000000-0000-4000-8000-000000000001",
        "MEMPLEX_SOURCE_SHA256": "1" * 64,
        "MEMPLEX_ARTIFACT_SHA256": "2" * 64,
        "MEMPLEX_TARGET_IDENTITY_SHA256": "3" * 64,
    }
    for name, value in binding.items():
        monkeypatch.setenv(name, value)
    return binding


def test_probe_routes_are_fixed_and_do_not_require_business_auth(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MEMPLEX_API_KEY", "super-secret-token")
    app = create_app(_config(tmp_path))
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
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
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
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
    with TestClient(create_app(_config(tmp_path)), client=("127.0.0.1", 50000)) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert "storage_path" not in response.json()
    assert str(tmp_path) not in response.text


def test_metrics_use_fixed_labels_and_do_not_run_health_scan(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("MEMPLEX_API_KEY", raising=False)
    app = create_app(_config(tmp_path))
    with TestClient(app, client=("127.0.0.1", 50000)) as client:
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


def test_lifespan_writes_report_with_explicit_deployment_binding(
    tmp_path, monkeypatch
) -> None:
    key = b"o" * 32
    output = tmp_path / "operations-evidence.json"
    monkeypatch.setenv(
        "MEMPLEX_OPERATIONS_HMAC_KEY", base64.b64encode(key).decode("ascii")
    )
    monkeypatch.setenv("MEMPLEX_G006_REPORT_OUTPUT", str(output))
    binding = _set_deployment_binding(monkeypatch)
    config = _config(tmp_path)
    config.operations.report_key_id = "ops-key"
    with TestClient(create_app(config), client=("127.0.0.1", 50000)) as client:
        assert client.get("/stats").status_code == 200

    report = load_operations_report(output)
    report.verify(key)
    assert report.request_count == 1
    assert report.successful_requests == 1
    assert report.shutdown_drained is True
    assert report.generated_at >= report.window_ended_at
    assert report.deployment_id == binding["MEMPLEX_DEPLOYMENT_ID"]
    assert report.source_sha256 == binding["MEMPLEX_SOURCE_SHA256"]
    assert report.artifact_sha256 == binding["MEMPLEX_ARTIFACT_SHA256"]
    assert report.target_identity_sha256 == binding["MEMPLEX_TARGET_IDENTITY_SHA256"]
    assert report.industrial_gate_closing is False


@pytest.mark.parametrize("source_sha256", [None, "not-a-digest"])
def test_lifespan_refuses_missing_or_invalid_deployment_binding_without_leakage(
    tmp_path, monkeypatch, source_sha256
) -> None:
    key = b"o" * 32
    output = tmp_path / "operations-evidence.json"
    private_deployment = "private-production-deployment"
    monkeypatch.setenv(
        "MEMPLEX_OPERATIONS_HMAC_KEY", base64.b64encode(key).decode("ascii")
    )
    monkeypatch.setenv("MEMPLEX_G006_REPORT_OUTPUT", str(output))
    monkeypatch.setenv("MEMPLEX_DEPLOYMENT_ID", private_deployment)
    if source_sha256 is not None:
        monkeypatch.setenv("MEMPLEX_SOURCE_SHA256", source_sha256)
    monkeypatch.setenv("MEMPLEX_ARTIFACT_SHA256", "2" * 64)
    monkeypatch.setenv("MEMPLEX_TARGET_IDENTITY_SHA256", "3" * 64)
    warnings: list[str] = []
    monkeypatch.setattr(http_api.logger, "warning", warnings.append)
    config = _config(tmp_path)
    config.operations.report_key_id = "ops-key"

    with TestClient(create_app(config), client=("127.0.0.1", 50000)) as client:
        assert client.get("/stats").status_code == 200

    assert not output.exists()
    assert warnings == ["operations_report_deployment_binding_invalid"]
    assert private_deployment not in warnings
    assert source_sha256 not in warnings


def test_handler_failure_releases_admission_and_metrics_counters(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.delenv("MEMPLEX_API_KEY", raising=False)
    app = create_app(_config(tmp_path))

    @app.get("/_operations_test_boom")
    def _boom():
        raise RuntimeError("private-handler-detail")

    with TestClient(
        app,
        raise_server_exceptions=False,
        client=("127.0.0.1", 50000),
    ) as client:
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

    with TestClient(app, client=("127.0.0.1", 50000)) as client:
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
