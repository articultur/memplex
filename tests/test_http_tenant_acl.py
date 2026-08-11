"""Authenticated HTTP principals must be isolated across every public data path."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from unittest.mock import ANY

import pytest

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from memplex.adapters.http_api import create_app  # noqa: E402
from memplex.config import MemplexConfig  # noqa: E402


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@pytest.fixture
def tenant_http(tmp_path, monkeypatch):
    principals = [
        {
            "credential_id": "credential-alice",
            "token_sha256": _digest("token-alice"),
            "tenant_id": "tenant-a",
            "subject_id": "alice",
            "workspace_id": "shared-workspace",
            "agent_id": "http",
            "roles": ["member"],
        },
        {
            "credential_id": "credential-bob",
            "token_sha256": _digest("token-bob"),
            "tenant_id": "tenant-b",
            "subject_id": "bob",
            "workspace_id": "shared-workspace",
            "agent_id": "http",
            "roles": ["member"],
        },
    ]
    monkeypatch.setenv("MEMPLEX_PRINCIPALS_JSON", json.dumps(principals))
    monkeypatch.delenv("MEMPLEX_API_KEY", raising=False)
    monkeypatch.delenv("MEMPLEX_BEARER_TOKEN", raising=False)
    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(tmp_path / "store")
    config.llm.query_enhancement = False
    with TestClient(create_app(config)) as client:
        yield client


def _headers(subject: str) -> dict[str, str]:
    return {"Authorization": f"Bearer token-{subject}"}


def _write(client: TestClient, subject: str, token: str) -> dict:
    response = client.post(
        "/memories",
        headers=_headers(subject),
        json={"type": "text", "content": f"Remember {token} for this tenant."},
    )
    assert response.status_code == 200, response.text
    return response.json()["functions"][0]


def test_http_credential_derives_identity_and_owner_query_cannot_override(
    tenant_http: TestClient,
) -> None:
    alice = _write(tenant_http, "alice", "alice-http-boundary-canary")

    assert alice["tenant_id"] == "tenant-a"
    assert alice["owner_subject_id"] == "alice"
    assert alice["workspace_id"] == "shared-workspace"
    assert alice["provenance"]["authentication_id"] == "credential-alice"

    bob_query = tenant_http.get(
        "/memories",
        headers=_headers("bob"),
        params={"q": "alice-http-boundary-canary", "owner": "alice", "top_k": 20},
    )
    assert bob_query.status_code == 200, bob_query.text
    assert bob_query.json()["results"] == []

    alice_query = tenant_http.get(
        "/memories",
        headers=_headers("alice"),
        params={"q": "alice-http-boundary-canary", "top_k": 20},
    )
    assert alice_query.status_code == 200, alice_query.text
    assert alice["id"] in {item["id"] for item in alice_query.json()["results"]}


def test_cross_tenant_id_routes_match_missing_and_cannot_mutate(
    tenant_http: TestClient,
) -> None:
    alice = _write(tenant_http, "alice", "alice-id-route-canary")
    memory_id = alice["id"]
    missing_id = f"missing-{uuid.uuid4().hex}"
    bob = _headers("bob")

    for suffix in ("", "/timeline"):
        denied = tenant_http.get(f"/memories/{memory_id}{suffix}", headers=bob)
        missing = tenant_http.get(f"/memories/{missing_id}{suffix}", headers=bob)
        assert (denied.status_code, denied.json()) == (missing.status_code, missing.json())
        assert denied.status_code == 404

    denied_delete = tenant_http.delete(f"/memories/{memory_id}", headers=bob)
    missing_delete = tenant_http.delete(f"/memories/{missing_id}", headers=bob)
    assert (denied_delete.status_code, denied_delete.json()) == (
        missing_delete.status_code,
        missing_delete.json(),
    )
    assert denied_delete.status_code == 404

    denied_update = tenant_http.patch(
        f"/memories/{memory_id}",
        headers=bob,
        json={"role": "action", "new_value": "tampered"},
    )
    missing_update = tenant_http.patch(
        f"/memories/{missing_id}",
        headers=bob,
        json={"role": "action", "new_value": "tampered"},
    )
    assert (denied_update.status_code, denied_update.json()) == (
        missing_update.status_code,
        missing_update.json(),
    )
    assert denied_update.status_code == 404

    for path, body in (
        (
            f"/memories/{memory_id}/feedback",
            {"role": "action", "index": 0, "verdict": "wrong"},
        ),
        (
            f"/memories/{memory_id}/resolve",
            {"field_role": "action", "action": "reject"},
        ),
    ):
        denied = tenant_http.post(path, headers=bob, json=body)
        missing = tenant_http.post(path.replace(memory_id, missing_id), headers=bob, json=body)
        assert (denied.status_code, denied.json()) == (missing.status_code, missing.json())
        assert denied.status_code == 404

    still_present = tenant_http.get(f"/memories/{memory_id}", headers=_headers("alice"))
    assert still_present.status_code == 200


def test_sync_pull_and_tombstones_are_tenant_scoped(tenant_http: TestClient) -> None:
    alice = _write(tenant_http, "alice", "alice-sync-scope-canary")
    bob = _write(tenant_http, "bob", "bob-sync-scope-canary")

    alice_changes = tenant_http.get("/sync/changes", headers=_headers("alice"))
    bob_changes = tenant_http.get("/sync/changes", headers=_headers("bob"))
    assert {item["id"] for item in alice_changes.json()["changes"]} == {alice["id"]}
    assert {item["id"] for item in bob_changes.json()["changes"]} == {bob["id"]}

    deleted = tenant_http.delete(f"/memories/{alice['id']}", headers=_headers("alice"))
    assert deleted.status_code == 200
    bob_after_delete = tenant_http.get("/sync/changes", headers=_headers("bob")).json()
    assert all(item["func_id"] != alice["id"] for item in bob_after_delete["tombstones"])


def test_tombstone_storage_keeps_same_function_id_for_each_tenant(tmp_path) -> None:
    """A tenant-first database may legitimately delete the same ID twice."""
    from memplex.adapters.http_api import _read_tombstones, _record_tombstone

    config = MemplexConfig()
    config.storage.path = str(tmp_path)
    _record_tombstone(config, "shared-function-id", "a-v1", tenant_id="tenant-a")
    _record_tombstone(config, "shared-function-id", "b-v1", tenant_id="tenant-b")

    assert _read_tombstones(config, tenant_id="tenant-a") == [
        {
            "func_id": "shared-function-id",
            "deleted_at": ANY,
            "deleted_version": "a-v1",
        }
    ]
    assert _read_tombstones(config, tenant_id="tenant-b") == [
        {
            "func_id": "shared-function-id",
            "deleted_at": ANY,
            "deleted_version": "b-v1",
        }
    ]

    # Pre-tenant entries remain readable only to legacy aggregate callers;
    # a tenant-scoped read must fail closed instead of leaking their ID.
    tombstone_path = tmp_path / "tombstones.json"
    raw = json.loads(tombstone_path.read_text(encoding="utf-8"))
    assert len(raw) == 2
    assert "shared-function-id" not in raw
    assert {entry["tenant_id"] for entry in raw.values()} == {"tenant-a", "tenant-b"}
    assert {entry["func_id"] for entry in raw.values()} == {"shared-function-id"}
    raw["legacy-unscoped-id"] = "2026-01-01T00:00:00+00:00"
    raw["legacy-scoped-id"] = {
        "tenant_id": "tenant-a",
        "deleted_at": "2026-01-01T00:00:00+00:00",
        "deleted_version": "legacy-v1",
    }
    tombstone_path.write_text(json.dumps(raw), encoding="utf-8")

    tenant_a = _read_tombstones(config, tenant_id="tenant-a")
    assert {item["func_id"] for item in tenant_a} == {
        "shared-function-id",
        "legacy-scoped-id",
    }
    assert all(item["func_id"] != "legacy-unscoped-id" for item in tenant_a)
    assert {item["func_id"] for item in _read_tombstones(config)} >= {
        "legacy-unscoped-id",
        "legacy-scoped-id",
    }


def test_sync_push_prevalidates_entire_batch_and_rejects_forged_identity(
    tenant_http: TestClient,
) -> None:
    valid_id = "bob-valid-before-forgery"
    payload = {
        "functions": [
            {
                "id": valid_id,
                "name": "valid bob item",
                "name_normalized": "valid bob item",
                "memory_type": "function",
            },
            {
                "id": "forged-victim-item",
                "name": "forged victim item",
                "name_normalized": "forged victim item",
                "memory_type": "function",
                "tenant_id": "tenant-a",
                "owner_subject_id": "alice",
                "owner": "alice",
                "workspace_id": "shared-workspace",
                "namespace": {"memplex_tenant_id": "tenant-a"},
            },
        ]
    }

    response = tenant_http.post(
        "/sync/push",
        headers=_headers("bob"),
        json=payload,
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Invalid memory payload"}
    bob_changes = tenant_http.get("/sync/changes", headers=_headers("bob")).json()
    assert valid_id not in {item["id"] for item in bob_changes["changes"]}


def test_sync_routes_use_request_scoped_storage_facade(
    tenant_http: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HTTP sync must never call a strict production store unscoped.

    The production PostgreSQL store rejects every operation without its
    ``authorized(context)`` facade. This spy makes such a direct call fail,
    while preserving the small read/write contract needed by both sync
    routes.
    """

    class ScopedStore:
        def __init__(self) -> None:
            self.added: list[str] = []

        def list_changes_since(self, *, since=None, limit=100000):
            return []

        def list_facts(self, *, limit=100000):
            return []

        def list_preferences(self, *, limit=100000):
            return []

        def list_observations(self, *, limit=100000):
            return []

        def get(self, memory_id):
            return None

        def get_fact(self, memory_id):
            return None

        def get_preference(self, memory_id):
            return None

        def add(self, node, source) -> None:
            self.added.append(node.id)

        def add_fact(self, node) -> None:
            self.added.append(node.id)

        def add_preference(self, node) -> None:
            self.added.append(node.id)

        def add_observation(self, node) -> None:
            self.added.append(node.id)

    class StrictStore:
        def __init__(self, scoped: ScopedStore) -> None:
            self.scoped = scoped
            self.contexts = []

        def authorized(self, context):
            self.contexts.append(context)
            return self.scoped

        def __getattr__(self, name):
            raise AssertionError(f"unscoped store call: {name}")

    service = tenant_http.app.state.memplex_service
    scoped = ScopedStore()
    strict = StrictStore(scoped)
    monkeypatch.setattr(service, "store", strict)

    pull = tenant_http.get("/sync/changes", headers=_headers("alice"))
    assert pull.status_code == 200, pull.text

    push = tenant_http.post(
        "/sync/push",
        headers=_headers("alice"),
        json={
            "functions": [
                {
                    "id": "scoped-sync-function",
                    "name": "scoped sync function",
                    "name_normalized": "scoped sync function",
                    "memory_type": "function",
                }
            ]
        },
    )
    assert push.status_code == 200, push.text
    assert push.json()["accepted"] == 1
    assert scoped.added == ["scoped-sync-function"]
    assert strict.contexts
    assert all(context.principal.tenant_id == "tenant-a" for context in strict.contexts)


def test_unmapped_credential_is_rejected_when_principal_registry_is_configured(
    tenant_http: TestClient,
) -> None:
    response = tenant_http.get(
        "/health",
        headers={"Authorization": "Bearer unknown-token"},
    )

    assert response.status_code == 401


def test_production_http_refuses_shared_secret_without_principal_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MEMPLEX_PRINCIPALS_JSON", raising=False)
    monkeypatch.setenv("MEMPLEX_API_KEY", "legacy-shared-secret")
    config = MemplexConfig()
    config.deployment.profile = "production"
    config.storage.backend = "postgres"
    config.storage.path = "postgresql://memplex@example.invalid/memplex"

    with pytest.raises(RuntimeError, match="principal registry"):
        create_app(config)
