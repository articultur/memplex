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


@pytest.fixture
def sync_v1_client(tmp_path, monkeypatch):
    from memplex.config import MemplexConfig

    monkeypatch.delenv("MEMPLEX_PRINCIPALS_JSON", raising=False)
    monkeypatch.delenv("MEMPLEX_API_KEY", raising=False)
    monkeypatch.delenv("MEMPLEX_BEARER_TOKEN", raising=False)
    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(tmp_path / "sync-store")
    config.llm.query_enhancement = False
    config.sync.enabled = True
    config.sync.node_id = "server-node"
    config.sync.cursor_signing_key_id = "active"
    config.sync.cursor_signing_secret = "s" * 32
    config.sync.cursor_previous_signing_keys = {"old": "o" * 32}
    with TestClient(create_app(config)) as c:
        yield c


@pytest.fixture
def sync_v1_tenant_client(tmp_path, monkeypatch):
    import hashlib
    import json

    from memplex.config import MemplexConfig

    principals = [
        {
            "credential_id": "credential-a",
            "token_sha256": hashlib.sha256(b"token-a").hexdigest(),
            "tenant_id": "tenant-a",
            "subject_id": "alice",
            "workspace_id": "workspace-a",
            "agent_id": "remote-a",
            "roles": ["member"],
        },
        {
            "credential_id": "credential-b",
            "token_sha256": hashlib.sha256(b"token-b").hexdigest(),
            "tenant_id": "tenant-b",
            "subject_id": "bob",
            "workspace_id": "workspace-b",
            "agent_id": "remote-b",
            "roles": ["member"],
        },
    ]
    monkeypatch.setenv("MEMPLEX_PRINCIPALS_JSON", json.dumps(principals))
    monkeypatch.delenv("MEMPLEX_API_KEY", raising=False)
    monkeypatch.delenv("MEMPLEX_BEARER_TOKEN", raising=False)
    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(tmp_path / "sync-tenant-store")
    config.llm.query_enhancement = False
    config.sync.enabled = True
    config.sync.node_id = "server-node"
    config.sync.cursor_signing_key_id = "active"
    config.sync.cursor_signing_secret = "s" * 32
    with TestClient(create_app(config)) as c:
        yield c


def _sync_v1_function(identifier: str):
    from memplex.models import Function

    return Function(
        id=identifier,
        name=identifier,
        name_normalized=identifier,
        tenant_id="local",
        owner="local-development",
        owner_subject_id="local-development",
        workspace_id="local-development",
        visibility="workspace",
        provenance={
            "agent_id": "memplex",
            "session_id": "local-development",
        },
    )


def _sync_v1_batch(*identifiers: str, invalid_last: bool = False):
    import uuid
    from datetime import datetime, timezone

    from memplex.sync_protocol import (
        SyncBatch,
        SyncEntityKey,
        SyncEvent,
        SyncNodeType,
        SyncOperation,
        SyncScope,
        SyncVersion,
    )

    events = []
    for index, identifier in enumerate(identifiers):
        event_id = str(uuid.uuid4())
        payload = _sync_v1_function(identifier).to_dict()
        if invalid_last and index == len(identifiers) - 1:
            payload["id"] = f"{identifier}-mismatch"
        events.append(
            SyncEvent(
                1,
                event_id,
                "memplex",
                SyncNodeType.FUNCTION,
                SyncEntityKey.node(identifier),
                SyncOperation.UPSERT,
                str(
                    SyncVersion.create(
                        datetime.now(timezone.utc), "memplex", event_id
                    )
                ),
                SyncScope(
                    "local",
                    "local-development",
                    "local-development",
                    "workspace",
                    "memplex",
                    "local-development",
                ),
                payload,
            )
        )
    return SyncBatch(1, str(uuid.uuid4()), "memplex", tuple(events))


def _tenant_sync_v1_batch(identifier: str, *, origin: str, tenant_id: str):
    import uuid
    from datetime import datetime, timezone

    from memplex.models import Function
    from memplex.sync_protocol import (
        SyncBatch,
        SyncEntityKey,
        SyncEvent,
        SyncNodeType,
        SyncOperation,
        SyncScope,
        SyncVersion,
    )

    subject = "alice" if tenant_id == "tenant-a" else "bob"
    workspace = "workspace-a" if tenant_id == "tenant-a" else "workspace-b"
    node = Function(
        id=identifier,
        name=identifier,
        name_normalized=identifier,
        tenant_id=tenant_id,
        owner=subject,
        owner_subject_id=subject,
        workspace_id=workspace,
        visibility="workspace",
        provenance={"agent_id": origin},
    )
    event_id = str(uuid.uuid4())
    event = SyncEvent(
        1,
        event_id,
        origin,
        SyncNodeType.FUNCTION,
        SyncEntityKey.node(identifier),
        SyncOperation.UPSERT,
        str(SyncVersion.create(datetime.now(timezone.utc), origin, event_id)),
        SyncScope(tenant_id, subject, workspace, "workspace", origin, None),
        node.to_dict(),
    )
    return SyncBatch(1, str(uuid.uuid4()), origin, (event,))


def test_sync_v1_batch_is_atomic_and_idempotent(sync_v1_client: TestClient) -> None:
    accepted = _sync_v1_batch("v1-accepted")
    first = sync_v1_client.post(
        "/sync/v1/batches",
        content=accepted.canonical_bytes,
        headers={"Content-Type": "application/json"},
    )
    assert first.status_code == 200, first.text
    assert first.json()["receipts"] == [
        {"event_id": accepted.events[0].event_id, "outcome": "accepted"}
    ]
    retry = sync_v1_client.post(
        "/sync/v1/batches",
        content=accepted.canonical_bytes,
        headers={"Content-Type": "application/json"},
    )
    assert retry.status_code == 200
    assert retry.json() == first.json()

    from memplex.sync_protocol import SyncBatch

    changed = _sync_v1_batch("v1-conflicting-digest")
    conflict = SyncBatch(
        1,
        accepted.batch_id,
        accepted.origin_node_id,
        changed.events,
    )
    conflict_response = sync_v1_client.post(
        "/sync/v1/batches",
        content=conflict.canonical_bytes,
        headers={"Content-Type": "application/json"},
    )
    assert conflict_response.status_code == 409
    assert conflict_response.json() == {"detail": "batch_conflict"}

    rejected = _sync_v1_batch("v1-rolled-back", "v1-invalid", invalid_last=True)
    response = sync_v1_client.post(
        "/sync/v1/batches",
        content=rejected.canonical_bytes,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    service = sync_v1_client.app.state.memplex_service
    assert service.get("v1-rolled-back") is None
    assert service.get("v1-invalid") is None


def test_sync_v1_batch_second_event_failure_returns_503_and_rolls_back(
    sync_v1_client: TestClient,
    monkeypatch,
) -> None:
    store = sync_v1_client.app.state.memplex_service.store
    original = store._sync_repository._append_event
    calls = 0

    def fail_second(event):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected repository failure")
        return original(event)

    monkeypatch.setattr(store._sync_repository, "_append_event", fail_second)
    batch = _sync_v1_batch("http-rollback-first", "http-rollback-second")
    response = sync_v1_client.post(
        "/sync/v1/batches",
        content=batch.canonical_bytes,
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "sync_apply_unavailable"}
    assert store.get("http-rollback-first") is None
    assert store.get("http-rollback-second") is None
    assert store.sync_status().pending == 0


def test_sync_v1_batch_maps_repository_backpressure_without_writes(
    sync_v1_client: TestClient,
    monkeypatch,
) -> None:
    from memplex.sync_repository import SyncBackpressureError

    store = sync_v1_client.app.state.memplex_service.store

    def reject(_batch):
        raise SyncBackpressureError("private queue detail")

    monkeypatch.setattr(store, "sync_apply_batch", reject)
    batch = _sync_v1_batch("http-backpressure")
    response = sync_v1_client.post(
        "/sync/v1/batches",
        content=batch.canonical_bytes,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 429
    assert response.json() == {"detail": "sync_backpressure"}
    assert store.get("http-backpressure") is None
    assert store._sync_state["outbox"] == []


@pytest.mark.parametrize(
    ("sqlstate", "status", "detail"),
    [
        ("23505", 409, "batch_conflict"),
        ("54000", 429, "sync_backpressure"),
        ("22023", 422, "invalid_sync_batch"),
        ("XX000", 503, "sync_apply_unavailable"),
    ],
)
def test_sync_v1_batch_maps_database_faults_without_leaking_driver_details(
    sync_v1_client: TestClient,
    monkeypatch,
    sqlstate: str,
    status: int,
    detail: str,
) -> None:
    class DatabaseFault(Exception):
        pgcode = sqlstate

    store = sync_v1_client.app.state.memplex_service.store

    def reject(_batch):
        raise DatabaseFault("postgresql://app:secret@db/private driver payload")

    monkeypatch.setattr(store, "sync_apply_batch", reject)
    batch = _sync_v1_batch(f"db-fault-{sqlstate.lower()}")
    response = sync_v1_client.post(
        "/sync/v1/batches",
        content=batch.canonical_bytes,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == status
    assert response.json() == {"detail": detail}
    assert "secret" not in response.text
    assert store._sync_state["outbox"] == []


def test_sync_v1_changes_uses_signed_snapshot_cursor(
    sync_v1_client: TestClient,
) -> None:
    from memplex.models import SourceDocument

    store = sync_v1_client.app.state.memplex_service.store
    source = SourceDocument(type="text", content="cursor")
    store.add(_sync_v1_function("cursor-first"), source)
    store.add(_sync_v1_function("cursor-second"), source)

    first = sync_v1_client.get("/sync/v1/changes", params={"limit": 1})
    assert first.status_code == 200, first.text
    first_body = first.json()
    assert len(first_body["items"]) == 1
    assert first_body["has_more"] is True
    assert isinstance(first_body["next_cursor"], str)

    store.add(_sync_v1_function("cursor-later"), source)

    second = sync_v1_client.get(
        "/sync/v1/changes",
        params={"limit": 10, "cursor": first_body["next_cursor"]},
    )
    assert second.status_code == 200, second.text
    seen = {
        item["event"]["entity_key"]
        for item in first_body["items"] + second.json()["items"]
    }
    assert len(seen) == 2
    assert all("cursor-later" not in item for item in seen)

    third = sync_v1_client.get(
        "/sync/v1/changes",
        params={"limit": 10, "cursor": second.json()["next_cursor"]},
    )
    assert third.status_code == 200, third.text
    assert len(third.json()["items"]) == 1
    from memplex.sync_protocol import SyncEntityKey

    assert third.json()["items"][0]["event"]["entity_key"] == str(
        SyncEntityKey.node("cursor-later")
    )

    bad = first_body["next_cursor"][:-1] + (
        "A" if first_body["next_cursor"][-1] != "A" else "B"
    )
    invalid = sync_v1_client.get(
        "/sync/v1/changes", params={"limit": 10, "cursor": bad}
    )
    assert invalid.status_code == 400
    assert invalid.json() == {"detail": "invalid_cursor"}


def test_sync_v1_changes_accepts_previous_cursor_key_and_rotates_to_active(
    sync_v1_client: TestClient,
) -> None:
    from datetime import datetime, timedelta, timezone

    from memplex.sync_protocol import SyncCursorClaims, SyncCursorCodec

    batch = _sync_v1_batch("previous-key")
    assert sync_v1_client.post(
        "/sync/v1/batches",
        content=batch.canonical_bytes,
        headers={"Content-Type": "application/json"},
    ).status_code == 200
    now = datetime.now(timezone.utc)
    old = SyncCursorCodec("old", "o" * 32).encode(
        SyncCursorClaims(
            1,
            "old",
            "local",
            "memplex",
            "local-development",
            0,
            1,
            None,
            None,
            now,
            now + timedelta(minutes=5),
        )
    )
    response = sync_v1_client.get(
        "/sync/v1/changes", params={"cursor": old, "limit": 10}
    )
    assert response.status_code == 200, response.text
    rotated = SyncCursorCodec(
        "active", "s" * 32, {"old": "o" * 32}
    ).decode(
        response.json()["next_cursor"],
        tenant_binding="local",
        remote_binding="memplex",
        consumer_binding="local-development",
    )
    assert rotated.key_id == "active"


@pytest.mark.parametrize(
    ("path", "method_name", "detail"),
    [
        ("/sync/v1/changes", "sync_page", "sync_read_unavailable"),
        (
            "/sync/v1/snapshot?request_id=unavailable",
            "sync_create_snapshot",
            "sync_snapshot_unavailable",
        ),
    ],
)
def test_sync_v1_reads_map_backend_faults_to_fixed_errors(
    sync_v1_client: TestClient,
    monkeypatch,
    path: str,
    method_name: str,
    detail: str,
) -> None:
    store = sync_v1_client.app.state.memplex_service.store

    def fail(*_args, **_kwargs):
        raise OSError("postgresql://app:secret@db/private driver payload")

    monkeypatch.setattr(store, method_name, fail)
    response = sync_v1_client.get(path)
    assert response.status_code == 503
    assert response.json() == {"detail": detail}
    assert "secret" not in response.text


def test_sync_v1_batch_rejects_oversized_body_before_repository(
    sync_v1_client: TestClient,
) -> None:
    response = sync_v1_client.post(
        "/sync/v1/batches",
        content=b"{" + b"x" * (4 * 1024 * 1024 + 1),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json() == {"detail": "sync_batch_too_large"}


def test_sync_v1_batch_rejects_chunked_actual_body_over_limit(
    sync_v1_client: TestClient,
) -> None:
    chunks = iter((b"{", b"x" * (2 * 1024 * 1024), b"y" * (2 * 1024 * 1024 + 1)))
    response = sync_v1_client.post(
        "/sync/v1/batches",
        content=chunks,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json() == {"detail": "sync_batch_too_large"}


def test_sync_v1_batch_rejects_1001_events_before_repository(
    sync_v1_client: TestClient,
) -> None:
    import json

    template = _sync_v1_batch("event-limit").to_dict()
    template["events"] = [template["events"][0]] * 1001
    raw = json.dumps(template, sort_keys=True, separators=(",", ":")).encode()
    response = sync_v1_client.post(
        "/sync/v1/batches",
        content=raw,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_sync_batch"}
    store = sync_v1_client.app.state.memplex_service.store
    assert store.sync_status().pending == 0
    assert store._sync_state["batches"] == []
    assert store._sync_state["outbox"] == []


@pytest.mark.parametrize("path", ["/sync/v1/changes", "/sync/v1/snapshot?request_id=limits"])
@pytest.mark.parametrize("limit", [0, 1001])
def test_sync_v1_page_limits_are_hard_bounded(
    sync_v1_client: TestClient,
    path: str,
    limit: int,
) -> None:
    separator = "&" if "?" in path else "?"
    response = sync_v1_client.get(f"{path}{separator}limit={limit}")
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_sync_limit"}


def test_sync_v1_changes_collapses_all_untrusted_cursor_faults(
    sync_v1_client: TestClient,
) -> None:
    import base64
    import hashlib
    import hmac
    import json
    from datetime import datetime, timedelta, timezone

    from memplex.sync_protocol import SyncCursorClaims, SyncCursorCodec

    now = datetime.now(timezone.utc)
    claims = SyncCursorClaims(
        1,
        "active",
        "local",
        "memplex",
        "local-development",
        0,
        0,
        None,
        None,
        now,
        now + timedelta(minutes=5),
    )
    valid = SyncCursorCodec("active", "s" * 32).encode(claims)

    def signed_payload(payload: dict, *, secret: str = "s" * 32) -> str:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        signature = hmac.new(secret.encode(), raw, hashlib.sha256).digest()

        def encode(value: bytes) -> str:
            return base64.urlsafe_b64encode(value).rstrip(b"=").decode()

        return f"{encode(raw)}.{encode(signature)}"

    future = claims.to_dict()
    future["version"] = 2
    wrong_tenant = SyncCursorCodec("active", "s" * 32).encode(
        SyncCursorClaims(
            1,
            "active",
            "foreign",
            "memplex",
            "local-development",
            0,
            0,
            None,
            None,
            now,
            now + timedelta(minutes=5),
        )
    )
    wrong_remote = SyncCursorCodec("active", "s" * 32).encode(
        SyncCursorClaims(
            1,
            "active",
            "local",
            "foreign",
            "local-development",
            0,
            0,
            None,
            None,
            now,
            now + timedelta(minutes=5),
        )
    )
    wrong_consumer = SyncCursorCodec("active", "s" * 32).encode(
        SyncCursorClaims(
            1,
            "active",
            "local",
            "memplex",
            "foreign",
            0,
            0,
            None,
            None,
            now,
            now + timedelta(minutes=5),
        )
    )
    expired = SyncCursorCodec("active", "s" * 32).encode(
        SyncCursorClaims(
            1,
            "active",
            "local",
            "memplex",
            "local-development",
            0,
            0,
            None,
            None,
            now - timedelta(minutes=2),
            now - timedelta(minutes=1),
        )
    )
    unknown_key = SyncCursorCodec("unknown", "u" * 32).encode(
        SyncCursorClaims(
            1,
            "unknown",
            "local",
            "memplex",
            "local-development",
            0,
            0,
            None,
            None,
            now,
            now + timedelta(minutes=5),
        )
    )
    invalid = {
        "malformed": "%%%",
        "unsigned": valid.split(".", 1)[0],
        "bad_mac": f"{valid.rsplit('.', 1)[0]}.{'A' * 43}",
        "future": signed_payload(future),
        "expired": expired,
        "unknown_key": unknown_key,
        "foreign_tenant": wrong_tenant,
        "foreign_remote": wrong_remote,
        "foreign_consumer": wrong_consumer,
    }
    for token in invalid.values():
        response = sync_v1_client.get(
            "/sync/v1/changes", params={"cursor": token, "limit": 10}
        )
        assert response.status_code == 400
        assert response.json() == {"detail": "invalid_cursor"}


def test_sync_v1_snapshot_expiry_is_distinct_and_releases_durable_state(
    sync_v1_client: TestClient,
) -> None:
    from datetime import datetime, timedelta, timezone

    batch = _sync_v1_batch("snapshot-expiry-a", "snapshot-expiry-b")
    assert sync_v1_client.post(
        "/sync/v1/batches",
        content=batch.canonical_bytes,
        headers={"Content-Type": "application/json"},
    ).status_code == 200
    first = sync_v1_client.get(
        "/sync/v1/snapshot", params={"request_id": "expires", "limit": 1}
    )
    assert first.status_code == 200
    store = sync_v1_client.app.state.memplex_service.store
    with store._sync_repository._mutation():
        store._sync_state["snapshots"][0]["expires_at"] = (
            datetime.now(timezone.utc) - timedelta(seconds=1)
        ).isoformat()

    response = sync_v1_client.get(
        "/sync/v1/snapshot", params={"cursor": first.json()["next_cursor"], "limit": 1}
    )
    assert response.status_code == 409
    assert response.json() == {"detail": "snapshot_expired"}
    assert store._sync_state["snapshots"] == []
    assert store._sync_state["snapshot_items"] == []
    recreated = sync_v1_client.get(
        "/sync/v1/snapshot", params={"request_id": "after-expiry", "limit": 10}
    )
    assert recreated.status_code == 200, recreated.text


@pytest.mark.parametrize("forgery", ["origin", "tenant"])
def test_sync_v1_batch_rejects_authenticated_binding_forgery_before_write(
    sync_v1_client: TestClient,
    forgery: str,
) -> None:
    from memplex.sync_protocol import SyncBatch, SyncEvent, SyncScope, SyncVersion

    original = _sync_v1_batch(f"forged-{forgery}")
    event = original.events[0]
    if forgery == "origin":
        forged_event = SyncEvent(
            event.protocol_version,
            event.event_id,
            "untrusted-peer",
            event.node_type,
            event.entity_key,
            event.operation,
            str(
                SyncVersion.create(
                    SyncVersion.parse(event.version).occurred_at,
                    "untrusted-peer",
                    event.event_id,
                )
            ),
            event.scope,
            event.to_dict()["payload"],
        )
        forged = SyncBatch(
            1, original.batch_id, "untrusted-peer", (forged_event,)
        )
    else:
        scope = event.scope
        forged_event = SyncEvent(
            event.protocol_version,
            event.event_id,
            event.origin_node_id,
            event.node_type,
            event.entity_key,
            event.operation,
            event.version,
            SyncScope(
                "tenant-b",
                scope.owner_subject_id,
                scope.workspace_id,
                scope.visibility,
                scope.agent_id,
                scope.session_id,
            ),
            event.to_dict()["payload"],
        )
        forged = SyncBatch(1, original.batch_id, original.origin_node_id, (forged_event,))
    response = sync_v1_client.post(
        "/sync/v1/batches",
        content=forged.canonical_bytes,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 422
    assert response.json() == {"detail": "invalid_sync_batch"}
    assert sync_v1_client.app.state.memplex_service.get(f"forged-{forgery}") is None


def test_sync_v1_principal_binds_peer_tenant_and_cursor_opaquely(
    sync_v1_tenant_client: TestClient,
) -> None:
    alice_headers = {"Authorization": "Bearer token-a", "Content-Type": "application/json"}
    bob_headers = {"Authorization": "Bearer token-b"}
    accepted = _tenant_sync_v1_batch(
        "tenant-bound", origin="remote-a", tenant_id="tenant-a"
    )
    response = sync_v1_tenant_client.post(
        "/sync/v1/batches",
        content=accepted.canonical_bytes,
        headers=alice_headers,
    )
    assert response.status_code == 200, response.text
    page = sync_v1_tenant_client.get(
        "/sync/v1/changes", headers=alice_headers, params={"limit": 10}
    )
    assert page.status_code == 200, page.text

    foreign_cursor = sync_v1_tenant_client.get(
        "/sync/v1/changes",
        headers=bob_headers,
        params={"limit": 10, "cursor": page.json()["next_cursor"]},
    )
    assert foreign_cursor.status_code == 400
    assert foreign_cursor.json() == {"detail": "invalid_cursor"}

    forged_origin = _tenant_sync_v1_batch(
        "forged-origin", origin="remote-b", tenant_id="tenant-a"
    )
    forged = sync_v1_tenant_client.post(
        "/sync/v1/batches",
        content=forged_origin.canonical_bytes,
        headers=alice_headers,
    )
    assert forged.status_code == 422
    assert forged.json() == {"detail": "invalid_sync_batch"}


def test_sync_v1_snapshot_is_immutable_and_resumes_event_stream(
    sync_v1_client: TestClient,
) -> None:
    from memplex.sync_protocol import SyncEntityKey

    initial = _sync_v1_batch("snapshot-first", "snapshot-second")
    assert sync_v1_client.post(
        "/sync/v1/batches",
        content=initial.canonical_bytes,
        headers={"Content-Type": "application/json"},
    ).status_code == 200
    first = sync_v1_client.get(
        "/sync/v1/snapshot", params={"request_id": "request-a", "limit": 1}
    )
    assert first.status_code == 200, first.text
    assert first.json()["has_more"] is True
    assert first.json()["next_cursor"]
    retry = sync_v1_client.get(
        "/sync/v1/snapshot", params={"request_id": "request-a", "limit": 1}
    )
    assert retry.status_code == 200
    assert retry.json()["snapshot_id"] == first.json()["snapshot_id"]
    conflicting = sync_v1_client.get(
        "/sync/v1/snapshot", params={"request_id": "request-b", "limit": 1}
    )
    assert conflicting.status_code == 409
    assert conflicting.json() == {"detail": "snapshot_in_progress"}
    store = sync_v1_client.app.state.memplex_service.store
    assert len(store._sync_state["snapshots"]) == 1
    assert len(store._sync_state["snapshot_items"]) == 2

    from memplex.models import SourceDocument

    store.add(
        _sync_v1_function("snapshot-later"),
        SourceDocument(type="text", content="snapshot-later"),
    )
    second = sync_v1_client.get(
        "/sync/v1/snapshot",
        params={"cursor": first.json()["next_cursor"], "limit": 10},
    )
    assert second.status_code == 200, second.text
    assert second.json()["has_more"] is False
    assert second.json()["resume_cursor"]
    snapshot_keys = {
        event["entity_key"]
        for event in first.json()["events"] + second.json()["events"]
    }
    assert snapshot_keys == {
        str(SyncEntityKey.node("snapshot-first")),
        str(SyncEntityKey.node("snapshot-second")),
    }

    resumed = sync_v1_client.get(
        "/sync/v1/changes",
        params={"cursor": second.json()["resume_cursor"], "limit": 10},
    )
    assert resumed.status_code == 200, resumed.text
    assert [item["event"]["entity_key"] for item in resumed.json()["items"]] == [
        str(SyncEntityKey.node("snapshot-later"))
    ]


def test_sync_v1_snapshot_item_limit_and_timeout_leave_no_partial_state(
    sync_v1_client: TestClient,
    monkeypatch,
) -> None:
    from memplex.sync_repository import SyncBackpressureError

    store = sync_v1_client.app.state.memplex_service.store
    store._sync_repository._max_snapshot_items = 1
    batch = _sync_v1_batch("snapshot-limit-a", "snapshot-limit-b")
    assert sync_v1_client.post(
        "/sync/v1/batches",
        content=batch.canonical_bytes,
        headers={"Content-Type": "application/json"},
    ).status_code == 200
    oversized = sync_v1_client.get(
        "/sync/v1/snapshot", params={"request_id": "too-large", "limit": 10}
    )
    assert oversized.status_code == 413
    assert oversized.json() == {"detail": "snapshot_too_large"}
    assert store._sync_state["snapshots"] == []
    assert store._sync_state["snapshot_items"] == []

    def timeout(*_args, **_kwargs):
        raise SyncBackpressureError("snapshot_create_timeout")

    monkeypatch.setattr(store, "sync_create_snapshot", timeout)
    timed_out = sync_v1_client.get(
        "/sync/v1/snapshot", params={"request_id": "timeout", "limit": 10}
    )
    assert timed_out.status_code == 409
    assert timed_out.json() == {"detail": "snapshot_create_timeout"}
    assert store._sync_state["snapshots"] == []
    assert store._sync_state["snapshot_items"] == []


def test_sync_v1_snapshot_requires_exactly_one_pagination_input(
    sync_v1_client: TestClient,
) -> None:
    neither = sync_v1_client.get("/sync/v1/snapshot")
    assert neither.status_code == 422
    assert neither.json() == {"detail": "invalid_snapshot_request"}
    both = sync_v1_client.get(
        "/sync/v1/snapshot",
        params={"request_id": "both", "cursor": "unsigned"},
    )
    assert both.status_code == 422
    assert both.json() == {"detail": "invalid_snapshot_request"}


def test_sync_enabled_delete_uses_atomic_outbox_without_tombstone_sidecar(
    sync_v1_client: TestClient,
) -> None:
    batch = _sync_v1_batch("delete-through-outbox")
    assert sync_v1_client.post(
        "/sync/v1/batches",
        content=batch.canonical_bytes,
        headers={"Content-Type": "application/json"},
    ).status_code == 200
    deleted = sync_v1_client.delete("/memories/delete-through-outbox")
    assert deleted.status_code == 200, deleted.text
    store = sync_v1_client.app.state.memplex_service.store
    page = store.sync_page("audit-remote", "audit-consumer", None, 10)
    assert [item.event.operation.value for item in page.items] == [
        "upsert",
        "tombstone",
    ]
    assert not (store._path.parent / "tombstones.json").exists()


def test_production_legacy_sync_routes_require_v1_without_starting_service(
    tmp_path,
    monkeypatch,
) -> None:
    import asyncio
    import hashlib
    import json

    from fastapi import HTTPException

    from memplex.config import MemplexConfig

    monkeypatch.setenv(
        "MEMPLEX_PRINCIPALS_JSON",
        json.dumps(
            [
                {
                    "credential_id": "credential-a",
                    "token_sha256": hashlib.sha256(b"token-a").hexdigest(),
                    "tenant_id": "tenant-a",
                    "subject_id": "alice",
                    "workspace_id": "workspace-a",
                    "agent_id": "remote-a",
                    "roles": ["member"],
                }
            ]
        ),
    )
    config = MemplexConfig()
    config.deployment.profile = "production"
    config.storage.backend = "postgres"
    config.storage.path = "postgresql://application.invalid/memplex"
    config.storage.migration_dsn = "postgresql://migration.invalid/memplex"
    config.storage.inbound_dsn = "postgresql://inbound.invalid/memplex"
    config.sync.enabled = True
    config.sync.node_id = "server-node"
    config.sync.cursor_signing_key_id = "active"
    config.sync.cursor_signing_secret = "s" * 32
    app = create_app(config)
    endpoints = {
        route.path: route.endpoint
        for route in app.routes
        if getattr(route, "path", "") in {"/sync/changes", "/sync/push"}
    }

    with pytest.raises(HTTPException) as changes_error:
        asyncio.run(endpoints["/sync/changes"](object(), since=None))
    assert changes_error.value.status_code == 426
    assert changes_error.value.detail == "sync_v1_upgrade_required"
    with pytest.raises(HTTPException) as push_error:
        asyncio.run(endpoints["/sync/push"](object(), {}))
    assert push_error.value.status_code == 426
    assert push_error.value.detail == "sync_v1_upgrade_required"


def test_development_legacy_sync_adapter_does_not_echo_and_uses_durable_cursor(
    sync_v1_client: TestClient,
) -> None:
    first = _sync_v1_function("legacy-v1-first")
    second = _sync_v1_function("legacy-v1-second")
    pushed = sync_v1_client.post(
        "/sync/push",
        json={"functions": [first.to_dict(), second.to_dict()]},
    )
    assert pushed.status_code == 200, pushed.text
    assert pushed.json()["accepted"] == 2

    store = sync_v1_client.app.state.memplex_service.store
    assert len(store._sync_state["batches"]) == 1
    assert len(store._sync_state["outbox"]) == 2
    from memplex.models import SourceDocument

    store.add(
        _sync_v1_function("legacy-local-only"),
        SourceDocument(type="text", content="legacy-local-only"),
    )
    pulled = sync_v1_client.get("/sync/changes")
    assert pulled.status_code == 200, pulled.text
    assert {item["id"] for item in pulled.json()["changes"]} == {
        "legacy-local-only"
    }
    assert sync_v1_client.get("/sync/changes").json()["changes"] == []


def test_development_legacy_sync_adapter_rejects_whole_invalid_batch(
    sync_v1_client: TestClient,
) -> None:
    valid = _sync_v1_function("legacy-atomic-valid").to_dict()
    invalid = _sync_v1_function("legacy-atomic-invalid").to_dict()
    invalid["id"] = ["weak-id"]
    response = sync_v1_client.post(
        "/sync/push", json={"functions": [valid, invalid]}
    )
    assert response.status_code == 422
    store = sync_v1_client.app.state.memplex_service.store
    assert store.get("legacy-atomic-valid") is None
    assert store._sync_state["batches"] == []
    assert store._sync_state["outbox"] == []


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
    assert "storage_path" not in r.json()


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


def test_pending_reviews_route_not_shadowed_by_memory_id(client):
    """Regression: /memories/pending_reviews was registered AFTER
    /memories/{memory_id}, so FastAPI matched it as memory_id=
    "pending_reviews" and returned 404 'Memory not found'."""
    r = client.get("/memories/pending_reviews")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "total" in body
    assert "reviews" in body


def test_metrics_returns_prometheus_text(client):
    r = client.get("/metrics")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/plain")
    assert "memplex_http_requests_total" in r.text
    assert "memplex_runtime_state" in r.text


def test_tombstones_follow_config_storage_path(tmp_path, monkeypatch):
    """Tombstone sidecar must live under config.storage.path (an explicit
    config wins), not the MEMPLEX_STORAGE_PATH env var."""
    from memplex.config import MemplexConfig

    env_dir = tmp_path / "env-store"
    cfg_dir = tmp_path / "cfg-store"
    monkeypatch.setenv("MEMPLEX_STORAGE_PATH", str(env_dir))
    monkeypatch.setenv("MEMPLEX_STORAGE_BACKEND", "lite")
    monkeypatch.delenv("MEMPLEX_API_KEY", raising=False)
    monkeypatch.delenv("MEMPLEX_BEARER_TOKEN", raising=False)
    cfg = MemplexConfig()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(cfg_dir)
    with TestClient(create_app(cfg)) as c:
        write = c.post("/memories", json={"type": "text", "content": "tombstone cfg canary"})
        assert write.status_code == 200, write.text
        fid = write.json()["functions"][0]["id"]
        assert c.delete(f"/memories/{fid}").status_code == 200
        r = c.get("/sync/changes")
        assert any(t["func_id"] == fid for t in r.json()["tombstones"])
    assert (cfg_dir / "tombstones.json").exists()
    assert not (env_dir / "tombstones.json").exists()


def test_record_tombstone_failure_logs_warning(tmp_path, caplog):
    """Tombstone write failures must surface as warnings, not debug."""
    import logging

    from memplex.adapters.http_api import _record_tombstone
    from memplex.config import MemplexConfig

    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    cfg = MemplexConfig()
    cfg.storage.path = str(blocker / "sub")  # mkdir under a file -> fails
    with caplog.at_level(logging.WARNING, logger="memplex.adapters.http_api"):
        _record_tombstone(cfg, "f1")
    assert any(
        r.levelno >= logging.WARNING and "failed to record tombstone" in r.message
        for r in caplog.records
    )


def test_read_tombstones_corrupt_file_logs_warning(tmp_path, caplog):
    """Unreadable tombstone files must surface as warnings, not debug."""
    import logging

    from memplex.adapters.http_api import _read_tombstones
    from memplex.config import MemplexConfig

    cfg = MemplexConfig()
    cfg.storage.path = str(tmp_path)
    (tmp_path / "tombstones.json").write_text("{not json", encoding="utf-8")
    with caplog.at_level(logging.WARNING, logger="memplex.adapters.http_api"):
        assert _read_tombstones(cfg) == []
    assert any(
        r.levelno >= logging.WARNING and "failed to read tombstones" in r.message
        for r in caplog.records
    )


def test_legacy_tombstone_sidecar_rejects_postgres_dsn_without_path_or_secret_leak(
    tmp_path, monkeypatch, caplog
):
    import logging

    from memplex.adapters.http_api import _read_tombstones, _record_tombstone
    from memplex.config import MemplexConfig

    monkeypatch.chdir(tmp_path)
    cfg = MemplexConfig()
    cfg.storage.backend = "postgres"
    cfg.storage.path = "postgresql://app:topsecret@example.invalid/memplex"

    with caplog.at_level(logging.WARNING, logger="memplex.adapters.http_api"):
        _record_tombstone(cfg, "f1", tenant_id="tenant-a")
        assert _read_tombstones(cfg, tenant_id="tenant-a") == []

    assert not (tmp_path / "postgresql:").exists()
    rendered = "\n".join(record.getMessage() for record in caplog.records)
    assert "topsecret" not in rendered
    assert "postgresql://" not in rendered


# ── Sync payload serialization: models-standard to_dict/from_dict ────


def test_sync_push_function_roundtrips_drift_prone_fields(client):
    """Regression: the hand-rolled _function_from_dict used to drop
    needs_review_until / priority_from_source / source_authority and
    FieldValue sub-fields. The /sync/push path now uses
    Function.from_dict, so they survive the wire."""
    from memplex.models import FieldValue, Function, SourceType

    func = Function(
        id="ser-1",
        name="serialization canary",
        updated_at="2026-02-01T00:00:00+00:00",
        needs_review=True,
        needs_review_until="2026-03-01T00:00:00+00:00",
        priority_from_source="high",
        source_authority="authoritative",
        source_type=SourceType.CODE,
        trigger=[FieldValue(desc="t", source_method="manual", weight=0.4, observation=0.8)],
    )
    r = client.post("/sync/push", json={"functions": [func.to_dict()]})
    assert r.status_code == 200, r.text
    assert r.json()["accepted"] == 1

    got = client.get("/memories/ser-1").json()
    assert got["needs_review_until"] == "2026-03-01T00:00:00+00:00"
    assert got["priority_from_source"] == "high"
    assert got["source_authority"] == "authoritative"
    trigger_fv = got["trigger"][0]
    assert trigger_fv["source_method"] == "manual"
    assert trigger_fv["weight"] == 0.4
    assert trigger_fv["observation"] == 0.8


def test_sync_changes_uses_function_to_dict(client):
    """The /sync/changes feed serializes via Function.to_dict (canonical
    shape covering drift-prone fields), not the dataclass walker."""
    from memplex.models import Function, SourceType

    func = Function(
        id="ser-2",
        name="changes canary",
        updated_at="2026-02-01T00:00:00+00:00",
        needs_review_until="2026-03-01T00:00:00+00:00",
        source_authority="authoritative",
        source_type=SourceType.CODE,
    )
    r = client.post("/sync/push", json={"functions": [func.to_dict()]})
    assert r.status_code == 200, r.text
    change = next(c for c in client.get("/sync/changes").json()["changes"] if c["id"] == "ser-2")
    assert change["needs_review_until"] == "2026-03-01T00:00:00+00:00"
    assert change["source_authority"] == "authoritative"
    # to_dict shape: memory_type + FieldValue lists present.
    assert change["memory_type"] == "function"
    assert change["trigger"] == []
