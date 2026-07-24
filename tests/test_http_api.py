"""End-to-end tests for the FastAPI HTTP adapter.

These guard the request-handling path that previously had ZERO coverage. Two
real runtime bugs slipped through because of that:

1. ``_get_service`` was called with the *handler function* instead of the
   ``Request`` (``AttributeError: 'function' object has no attribute 'app'``).
2. ``_dataclass_to_dict`` used ``dataclasses.asdict`` which does not convert
   ``Enum``/``datetime`` leaves, so every write/query response crashed with
   ``TypeError: Object of type SourceType is not JSON serializable``.

These tests drive the real ASGI app via ``TestClient`` so the path is actually
exercised, not just the auth helper in isolation.
"""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from memplex.adapters.http_api import create_app  # noqa: E402


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMPLEX_STORAGE_PATH", str(tmp_path))
    monkeypatch.setenv("MEMPLEX_STORAGE_BACKEND", "lite")
    monkeypatch.delenv("MEMPLEX_API_KEY", raising=False)
    monkeypatch.delenv("MEMPLEX_BEARER_TOKEN", raising=False)
    with TestClient(create_app()) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["backend"] == "lite"
    assert "functions_total" in body


def test_write_then_query_round_trip(client):
    # Write must succeed and serialize ExtractedData (carries SourceType).
    r = client.post(
        "/memories",
        json={"type": "text", "content": "FastAPI HTTP loop works for recall."},
    )
    assert r.status_code == 200, r.text
    assert len(r.json()["functions"]) >= 1

    # Query must succeed and serialize QueryResult (carries QueryScope).
    q = client.get("/memories", params={"q": "recall", "top_k": 5})
    assert q.status_code == 200, q.text
    names = [x["name"] for x in q.json()["results"]]
    assert any("recall" in n or "FastAPI" in n for n in names), names


def test_get_memory_by_id_serializes(client):
    write = client.post("/memories", json={"type": "text", "content": "Get-by-id target memory."})
    func_id = write.json()["functions"][0]["id"]
    r = client.get(f"/memories/{func_id}")
    assert r.status_code == 200, r.text
    assert r.json()["id"] == func_id


def test_stats_serializes(client):
    r = client.get("/stats")
    assert r.status_code == 200, r.text
    assert r.json()["storage_backend"] == "lite"


def test_auth_required_when_api_key_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMPLEX_STORAGE_PATH", str(tmp_path))
    monkeypatch.setenv("MEMPLEX_STORAGE_BACKEND", "lite")
    monkeypatch.setenv("MEMPLEX_API_KEY", "test-secret-key")
    try:
        with TestClient(create_app()) as c:
            # No credential -> 401.
            assert c.get("/health").status_code == 401
            # Wrong credential -> 401.
            assert c.get("/health", headers={"X-API-Key": "wrong"}).status_code == 401
            # Correct credential -> 200.
            ok = c.get("/health", headers={"X-API-Key": "test-secret-key"})
            assert ok.status_code == 200, ok.text
    finally:
        monkeypatch.delenv("MEMPLEX_API_KEY", raising=False)
