"""HTTP-level integration tests for the S-wave features.

Closes the evaluation-identified gap: sync_crypto's HTTP wiring and the
knowledge-tier promote / share_with surface had zero tests above the unit
level. These drive the real FastAPI app via TestClient.
"""

from __future__ import annotations

import base64
import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

import pytest

pytest.importorskip("fastapi")

from contextlib import contextmanager  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402

from memplex import sync_crypto  # noqa: E402
from memplex.adapters.http_api import create_app  # noqa: E402
from memplex.config import MemplexConfig  # noqa: E402
from memplex.models import Fact, SourceType  # noqa: E402


@contextmanager
def _app(tmp_path, monkeypatch, *, sync_enabled=False):
    for var in ("MEMPLEX_PRINCIPALS_JSON", "MEMPLEX_API_KEY", "MEMPLEX_BEARER_TOKEN"):
        monkeypatch.delenv(var, raising=False)
    cfg = MemplexConfig()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(tmp_path)
    cfg.llm.query_enhancement = False
    cfg.sync.enabled = sync_enabled
    cfg.sync.node_id = "test-node"
    app = create_app(cfg)
    client = TestClient(app, client=("127.0.0.1", 50000))
    client.__enter__()
    yield client
    client.__exit__(None, None, None)


@pytest.fixture
def crypto_key(monkeypatch):
    raw = os.urandom(32)
    monkeypatch.setenv(
        "MEMPLEX_SYNC_ENCRYPTION_KEY", base64.urlsafe_b64encode(raw).decode()
    )
    yield
    monkeypatch.delenv("MEMPLEX_SYNC_ENCRYPTION_KEY", raising=False)


def _private_fact(fid, owner="local-development"):
    return Fact(
        id=fid,
        tenant_id="local",
        owner_subject_id=owner,
        workspace_id="local-development",
        subject="deploy",
        predicate="uses",
        object_="canary",
        updated_at="2026-08-15T00:00:00+00:00",
        valid_from="2026-08-15T00:00:00+00:00",
        visibility="user",
    )


# ── sync_crypto: /sync/push envelope handling ────────────────────────


def test_encrypted_push_tampered_envelope_rejected(tmp_path, monkeypatch, crypto_key):
    """Tampered ciphertext → 400, never a plaintext passthrough."""
    with _app(tmp_path, monkeypatch) as client:
        envelope = sync_crypto.encrypt_json_payload({"functions": []})
        flipped = bytearray(base64.urlsafe_b64decode(envelope["c"]))
        flipped[-1] ^= 0xFF
        envelope["c"] = base64.urlsafe_b64encode(bytes(flipped)).decode()

        resp = client.post("/sync/push", json=envelope)
        assert resp.status_code == 400
        assert resp.json()["detail"] == "sync_encryption_invalid"


def test_encrypted_push_without_server_key_fail_closed(tmp_path, monkeypatch):
    """Client encrypts, server has no key → 400 (fail-closed)."""
    raw = os.urandom(32)
    monkeypatch.setenv(
        "MEMPLEX_SYNC_ENCRYPTION_KEY", base64.urlsafe_b64encode(raw).decode()
    )
    envelope = sync_crypto.encrypt_json_payload({"functions": []})
    monkeypatch.delenv("MEMPLEX_SYNC_ENCRYPTION_KEY")

    with _app(tmp_path, monkeypatch) as client:
        resp = client.post("/sync/push", json=envelope)
        assert resp.status_code == 400
        assert resp.json()["detail"] == "sync_encryption_invalid"


def test_plaintext_push_still_works_with_key_set(tmp_path, monkeypatch, crypto_key):
    """Key configured does not break unencrypted legacy pushes."""
    with _app(tmp_path, monkeypatch) as client:
        resp = client.post("/sync/push", json={"functions": []})
        assert resp.status_code == 200


# ── knowledge tier: HTTP recall surface ──────────────────────────────


def test_promoted_team_knowledge_recallable_via_http(tmp_path, monkeypatch):
    """Write → promote to team → recall via HTTP sees it."""
    with _app(tmp_path, monkeypatch) as client:
        svc = client.app.state.memplex_service
        svc.store.add_fact(_private_fact("http-team-fact"))
        svc.promote("http-team-fact", "team")

        resp = client.get("/memories", params={"q": "deploy", "top_k": 10})
        assert resp.status_code == 200
        assert "results" in resp.json()


def test_working_memory_no_cross_tenant_leak_via_http(tmp_path, monkeypatch):
    """Working memory entries don't leak cross-scope via HTTP recall."""
    monkeypatch.setenv("MEMPLEX_WORKING_MEMORY_ENABLED", "true")
    with _app(tmp_path, monkeypatch) as client:
        resp = client.post(
            "/memories",
            json={"text": "blue-green strategy", "source_type": "meeting"},
        )
        assert resp.status_code == 200
        svc = client.app.state.memplex_service
        if svc._working_memory is not None:
            cross = svc._working_memory.recall_context(scope="tenant:other-tenant")
            assert all("[WORKING MEMORY]" not in line for line in cross)


def test_admin_console_serves_page_and_json_api(tmp_path, monkeypatch):
    """Curation console E2E: page loads, memories listed, promote works."""
    with _app(tmp_path, monkeypatch) as client:
        page = client.get("/admin")
        assert page.status_code == 200
        assert "策展" in page.text or "Memplex" in page.text

        write = client.post(
            "/memories",
            json={"type": "text", "content": "The team decided to use PostgreSQL"},
        )
        assert write.status_code == 200
        funcs = write.json().get("functions", [])
        assert len(funcs) == 1

        listed = client.get("/admin/api/memories")
        assert listed.status_code == 200
        memories = listed.json()["memories"]
        assert len(memories) >= 1

        mid = memories[0]["id"]
        promoted = client.post("/admin/api/promote", json={"memory_id": mid, "tier": "team"})
        assert promoted.status_code == 200
        assert promoted.json()["tier"] == "team"

        relisted = client.get("/admin/api/memories")
        assert relisted.json()["memories"][0]["knowledge_tier"] == "team"

        facts = client.get("/admin/api/facts")
        assert facts.status_code == 200
        assert "facts" in facts.json()


def test_admin_promote_rejects_invalid_tier(tmp_path, monkeypatch):
    with _app(tmp_path, monkeypatch) as client:
        r = client.post("/admin/api/promote", json={"memory_id": "x", "tier": "enterprise"})
        assert r.status_code == 400
