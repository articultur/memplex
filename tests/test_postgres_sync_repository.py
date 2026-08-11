"""Unit tests for PostgreSQL sync repository behavior.

These tests stay in-memory and validate SQL shapes, cursor semantics, lease
handling, and exact validation required for durable sync operations.
"""

from __future__ import annotations

import sys
import types
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

# Lightweight shim to make collection work in environments without optional YAML
# dependency installed. The repository surface under test does not touch YAML at
# import time; this only prevents bootstrap import chain failures.
if "yaml" not in sys.modules:
    yaml_shim = types.ModuleType("yaml")
    yaml_shim.safe_load = lambda *args, **kwargs: {}
    yaml_shim.safe_dump = lambda *args, **kwargs: ""
    yaml_shim.FullLoader = object
    yaml_shim.CSafeLoader = object
    sys.modules["yaml"] = yaml_shim

from memplex.auth import AuthorizationContext, Principal
from memplex.storage.postgres_sync import PostgresSyncRepository
from memplex.sync_protocol import (
    SyncBatch,
    SyncBatchResult,
    SyncCursorClaims,
    SyncDelivery,
    SyncEntityKey,
    SyncEvent,
    SyncNodeType,
    SyncOperation,
    SyncPage,
    SyncReceipt,
    SyncScope,
    SyncSnapshotAnchor,
    SyncSnapshotPage,
    SyncStatus,
    SyncVersion,
)
from memplex.sync_repository import (
    SyncBackpressureError,
    SyncCapturePolicy,
    SyncCursorExpired,
    SyncDeliveryBusy,
)


class _FakeCursor:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []
        self.fetchone_queue: list[object] = []
        self.fetchall_queue: list[list[tuple]] = []
        self.rowcount_queue: list[int] = []
        self.rowcount = -1
        self.closed = False

    def execute(self, sql: str, params: tuple = ()) -> None:
        self.executed.append((sql, params))
        if self.rowcount_queue:
            self.rowcount = self.rowcount_queue.pop(0)
        else:
            head = sql.lstrip().split(" ", maxsplit=1)[0].upper()
            self.rowcount = -1 if head == "SELECT" else 1

    def fetchone(self):
        if self.fetchone_queue:
            return self.fetchone_queue.pop(0)
        return None

    def fetchall(self):
        if self.fetchall_queue:
            return self.fetchall_queue.pop(0)
        return []

    def close(self) -> None:
        self.closed = True


class _FakeReadCursor(_FakeCursor):
    """Read-cursor shaped object where fetch auto-closes."""

    def fetchone(self):
        row = super().fetchone()
        self.close()
        return row

    def fetchall(self):
        rows = super().fetchall()
        self.close()
        return rows


class _FakeTx:
    def __init__(self, cursor: _FakeCursor):
        self.cursor = cursor

    def __enter__(self):
        return None, self.cursor

    def __exit__(self, exc_type: object, exc: object, tb: object) -> bool:
        return False


class _FakePoolManager:
    def __init__(self, cursor: _FakeCursor):
        self._cursor = cursor
        self.transaction_calls: list[tuple[object, object]] = []
        self.read_cursor_calls: list[tuple[object, object]] = []

    def transaction(self, bind_scope, context):
        self.transaction_calls.append((bind_scope, context))
        bind_scope(self._cursor, context)
        return _FakeTx(self._cursor)

    def read_cursor(self, bind_scope, context):
        self.read_cursor_calls.append((bind_scope, context))
        read_cursor = _FakeReadCursor()
        # read_cursor is intentionally not a context manager in real API.
        read_cursor.rowcount_queue = self._cursor.rowcount_queue
        bind_scope(read_cursor, context)
        return read_cursor


@dataclass
class _FakeStore:
    cursor: _FakeCursor

    def __post_init__(self) -> None:
        self._pool_manager = _FakePoolManager(self.cursor)
        self._inbound_executor = None
        self._sync_capture_policy = SyncCapturePolicy(
            "required", local_node_id="node-local"
        )
        self._bind_calls: list[tuple[object, object]] = []

    def _authorization_context(self) -> AuthorizationContext:
        return AuthorizationContext(
            principal=Principal(
                tenant_id="tenant-sync",
                subject_id="alice",
                roles=frozenset({"member"}),
                authentication_id="cred",
            ),
            workspace_id="workspace-sync",
            agent_id="agent-sync",
            session_id="session-sync",
            request_id="req-sync",
        )

    def _bind_transaction_scope(self, cursor: _FakeCursor, context: AuthorizationContext) -> None:
        self._bind_calls.append((cursor, context))


def _repo(
    cursor: _FakeCursor,
    *,
    max_attempts: int = 8,
    **kwargs: object,
) -> PostgresSyncRepository:
    return PostgresSyncRepository(_FakeStore(cursor), max_attempts=max_attempts, **kwargs)  # type: ignore[arg-type]


def _version(event_id: str) -> str:
    return str(SyncVersion.create(datetime(2026, 8, 11, tzinfo=timezone.utc), "origin-a", event_id))


def _event_id(index: int) -> str:
    return str(uuid.UUID(int=index))


def _outbox_row(*, stream_seq: int, event_id: str) -> tuple:
    return (
        stream_seq,
        event_id,
        "origin-a",
        "function",
        str(SyncEntityKey.node("fn")),
        "upsert",
        _version(event_id),
        "user",
        "owner-a",
        None,
        None,
        None,
        {"id": event_id},
    )


def _outbox_row_with_attempt(*, stream_seq: int, event_id: str, attempt_count: int = 0) -> tuple:
    return _outbox_row(stream_seq=stream_seq, event_id=event_id) + (attempt_count,)


def _snapshot_source_row(
    *,
    node_type: str = "function",
    entity_key: str = "fn",
    stream_seq: int = 1,
    event_id: str = "event-id",
) -> tuple[Any, ...]:
    entity_key_text = str(SyncEntityKey.node(entity_key))
    return (
        node_type,
        entity_key_text,
        _version(event_id),
        event_id,
    )


def _snapshot_business_row(
    *, entity_key: str, event_id: str
) -> tuple[Any, ...]:
    return (
        "function",
        entity_key,
        None,
        None,
        None,
        {"id": event_id},
        None,
        None,
        None,
        "owner-a",
        "workspace-sync",
        "user",
        "agent-sync",
        "session-sync",
    )


def _snapshot_event_dict(*, event_id: str, entity_key: str = "fn") -> dict[str, Any]:
    return SyncEvent(
        1,
        event_id,
        "origin-a",
        SyncNodeType.FUNCTION,
        SyncEntityKey.node(entity_key),
        SyncOperation.UPSERT,
        _version(event_id),
        SyncScope("tenant-sync", "owner-a", "workspace-sync", "user", None, None),
        {"id": event_id},
    ).to_dict()


def _event_anchor(entity_key: str) -> SyncSnapshotAnchor:
    return SyncSnapshotAnchor(
        SyncNodeType.FUNCTION,
        SyncEntityKey.node(entity_key),
    )


def test_postgres_sync_repository_snapshot_constructor_validates_limits() -> None:
    cursor = _FakeCursor()
    store = _FakeStore(cursor)
    PostgresSyncRepository(store)
    with pytest.raises(TypeError):
        PostgresSyncRepository(store, snapshot_ttl_seconds="10")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PostgresSyncRepository(store, snapshot_ttl_seconds=0)
    with pytest.raises(TypeError):
        PostgresSyncRepository(store, max_snapshot_items=1.5)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PostgresSyncRepository(store, max_snapshot_items=0)
    with pytest.raises(TypeError):
        PostgresSyncRepository(store, max_active_snapshots_per_tenant="1")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PostgresSyncRepository(store, max_active_snapshots_per_tenant=0)
    with pytest.raises(TypeError):
        PostgresSyncRepository(store, max_active_snapshots_per_remote="1")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PostgresSyncRepository(store, max_active_snapshots_per_remote=0)
    with pytest.raises(TypeError):
        PostgresSyncRepository(store, snapshot_create_timeout_seconds="1")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PostgresSyncRepository(store, snapshot_create_timeout_seconds=0)
    with pytest.raises(TypeError):
        PostgresSyncRepository(store, consumer_ttl_seconds=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PostgresSyncRepository(store, consumer_ttl_seconds=0)
    with pytest.raises(TypeError):
        PostgresSyncRepository(store, retention_min_seconds="1")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        PostgresSyncRepository(store, retention_min_seconds=0)


def _delivery_from_row(stream_seq: int, event_id: str, lease_id: str | None = None, attempt: int = 1) -> SyncDelivery:
    return SyncDelivery(
        target_id="target-a",
        event=SyncEvent(
            1,
            event_id,
            "origin-a",
            SyncNodeType.FUNCTION,
            SyncEntityKey.node("fn"),
            SyncOperation.UPSERT,
            _version(event_id),
            SyncScope("tenant-sync", "owner-a", "workspace-sync", "user", None, None),
            {"id": event_id},
        ),
        attempt=attempt,
        lease_id=lease_id or "550e8400-e29b-41d4-a716-446655440000",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )


def _batch() -> SyncBatch:
    event_id = _event_id(700)
    return SyncBatch(
        1,
        _event_id(701),
        "origin-a",
        (
            SyncEvent(
                1,
                event_id,
                "origin-a",
                SyncNodeType.FUNCTION,
                SyncEntityKey.node("fn"),
                SyncOperation.UPSERT,
                _version(event_id),
                SyncScope(
                    "tenant-sync",
                    "owner-a",
                    "workspace-sync",
                    "user",
                    None,
                    None,
                ),
                {"id": "fn"},
            ),
        ),
    )


def test_sync_page_requires_exact_types_and_bounds() -> None:
    repo = _repo(_FakeCursor())

    with pytest.raises(TypeError):
        repo.sync_page(remote_id=123, consumer_id="consumer", cursor=None, limit=10)
    with pytest.raises(TypeError):
        repo.sync_page(remote_id="remote", consumer_id=object(), cursor=None, limit=10)
    with pytest.raises(ValueError):
        repo.sync_page(remote_id="remote", consumer_id="consumer", cursor=None, limit=0)
    with pytest.raises(TypeError):
        repo.sync_page(
            remote_id="remote",
            consumer_id="consumer",
            cursor="not-cursor",  # type: ignore[arg-type]
            limit=1,
        )


def test_sync_page_persists_confirmed_after_seq_without_advancing() -> None:
    cursor = _FakeCursor()
    cursor.fetchone_queue = [
        (4,),  # snapshot_seq
        (0,),  # retention_floor
        (0,),  # stored cursor after_seq
    ]
    cursor.fetchall_queue = [
        [
            _outbox_row(stream_seq=1, event_id=_event_id(1)),
            _outbox_row(stream_seq=2, event_id=_event_id(2)),
            _outbox_row(stream_seq=3, event_id=_event_id(3)),
        ],
    ]
    repo = _repo(cursor)

    first = repo.sync_page("remote-a", "consumer-a", None, 2)
    assert isinstance(first, SyncPage)
    assert first.snapshot_seq == 4
    assert first.next_after_seq == 2
    assert first.has_more is True
    assert tuple(item.stream_seq for item in first.items) == (1, 2)

    insert_sqls = [sql for sql, _ in cursor.executed if sql.startswith("INSERT INTO memplex_sync_cursors")]
    assert insert_sqls
    assert [params for sql, params in cursor.executed if sql.startswith("INSERT INTO memplex_sync_cursors")][0][3] == 0

    cursor.fetchone_queue = [
        (0,),  # retention_floor
        (0,),  # previous cursor
    ]
    cursor.fetchall_queue = [[_outbox_row(stream_seq=3, event_id=_event_id(3))]]

    claims = SyncCursorClaims(
        1,
        "kid",
        "tenant-sync",
        "remote-a",
        "consumer-a",
        2,
        4,
        None,
        None,
        datetime(2026, 8, 11, tzinfo=timezone.utc),
        datetime(2026, 8, 11, tzinfo=timezone.utc) + timedelta(minutes=1),
    )

    second = repo.sync_page("remote-a", "consumer-a", claims, 10)
    assert second.snapshot_seq == 4
    assert second.next_after_seq == 4
    assert tuple(item.stream_seq for item in second.items) == (3,)
    assert second.has_more is False

    assert cursor.executed[-1][0].startswith("INSERT INTO memplex_sync_cursors")
    assert cursor.executed[-1][1][3] == 2
    assert any(
        "SET state='delivered'" in sql
        and params[-2:] == ("remote-a", 2)
        for sql, params in cursor.executed
    )
    page_sql = next(
        sql
        for sql, _ in cursor.executed
        if "FROM memplex_sync_outbox" in sql and "ORDER BY stream_seq" in sql
    )
    assert "origin_node_id <> %s" in page_sql


def test_sync_page_rejects_before_retention_floor() -> None:
    cursor = _FakeCursor()
    cursor.fetchone_queue = [
        (20,),  # snapshot_seq
        (5,),   # retention_floor
        (0,),   # stored cursor
    ]
    repo = _repo(cursor)
    claims = SyncCursorClaims(
        1,
        "kid",
        "tenant-sync",
        "remote-a",
        "consumer-a",
        0,
        20,
        None,
        None,
        datetime(2026, 8, 11, tzinfo=timezone.utc),
        datetime(2026, 8, 11, tzinfo=timezone.utc) + timedelta(minutes=1),
    )

    with pytest.raises(SyncCursorExpired, match="cursor_expired"):
        repo.sync_page("remote-a", "consumer-a", claims, 10)


def test_completed_stream_cursor_opens_new_snapshot_without_replaying_confirmed_rows() -> None:
    cursor = _FakeCursor()
    cursor.fetchone_queue = [
        (3,),  # new snapshot_seq after the prior cursor completed at 2
        (0,),  # retention_floor
        (2,),  # stored confirmed cursor
    ]
    cursor.fetchall_queue = [[_outbox_row(stream_seq=3, event_id=_event_id(3))]]
    repo = _repo(cursor)
    now = datetime(2026, 8, 11, tzinfo=timezone.utc)
    completed = SyncCursorClaims(
        1,
        "kid",
        "tenant-sync",
        "remote-a",
        "consumer-a",
        2,
        2,
        None,
        None,
        now,
        now + timedelta(minutes=1),
    )

    page = repo.sync_page("remote-a", "consumer-a", completed, 10)

    assert page.snapshot_seq == 3
    assert page.next_after_seq == 3
    assert tuple(item.stream_seq for item in page.items) == (3,)
    assert cursor.executed[-1][1][3] == 2


def test_sync_create_snapshot_is_idempotent_for_request_id() -> None:
    cursor = _FakeCursor()
    existing_snapshot = ("snapshot-id", 7)
    cursor.fetchone_queue = [existing_snapshot]
    cursor.fetchall_queue = [[
        _snapshot_event_dict(event_id=_event_id(1), entity_key="fn-a"),
        _snapshot_event_dict(event_id=_event_id(2), entity_key="fn-b"),
    ]]
    repo = _repo(cursor)

    page = repo.sync_create_snapshot("remote-a", "consumer-a", "request-id", 1)

    assert isinstance(page, SyncSnapshotPage)
    assert page.snapshot_id == "snapshot-id"
    assert page.resume_seq == 7
    assert page.has_more is True
    assert page.next_anchor == _event_anchor("fn-a")
    assert tuple(event.event_id for event in page.events) == (_event_id(1),)
    assert not any("INSERT INTO memplex_sync_snapshots" in sql for sql, _ in cursor.executed)
    assert any("DELETE FROM memplex_sync_snapshots" in sql for sql, _ in cursor.executed)


def test_sync_create_snapshot_materializes_items_and_sets_timeout() -> None:
    cursor = _FakeCursor()
    cursor.fetchone_queue = [
        None,
        (0, 0),  # remote and tenant active snapshots
        (12,),  # outbox resume_seq
    ]
    cursor.fetchall_queue = [
        [
            _snapshot_source_row(stream_seq=3, event_id=_event_id(1), entity_key="a"),
            _snapshot_source_row(stream_seq=4, event_id=_event_id(2), entity_key="b"),
        ],
        [
            _snapshot_business_row(event_id=_event_id(1), entity_key="a"),
            _snapshot_business_row(event_id=_event_id(2), entity_key="b"),
        ],
        [
            _snapshot_event_dict(event_id=_event_id(1), entity_key="a"),
            _snapshot_event_dict(event_id=_event_id(2), entity_key="b"),
        ],
    ]
    repo = _repo(
        cursor,
        snapshot_ttl_seconds=15,
        max_snapshot_items=2,
        max_active_snapshots_per_tenant=1,
        max_active_snapshots_per_remote=1,
    )
    page = repo.sync_create_snapshot("remote-a", "consumer-a", "request-id", 1)

    assert page.resume_seq == 12
    assert page.snapshot_id
    assert page.has_more is True
    assert page.next_anchor == _event_anchor("a")
    assert cursor.executed[0][0] == "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE"
    assert any("SET LOCAL statement_timeout" in sql for sql, _ in cursor.executed)
    assert any(
        "memplex_sync_snapshot_admission_counts" in sql and params == ()
        for sql, params in cursor.executed
    )
    assert any("INSERT INTO memplex_sync_snapshots" in sql for sql, _ in cursor.executed)
    assert any("INSERT INTO memplex_sync_snapshot_items" in sql for sql, _ in cursor.executed)


def test_sync_create_snapshot_rejects_item_overflow_and_snapshot_caps() -> None:
    cursor = _FakeCursor()
    cursor.fetchone_queue = [
        None,
        (0, 0),  # active remote and tenant cap check
        (9,),  # resume_seq
    ]
    cursor.fetchall_queue = [
        [
            _snapshot_source_row(stream_seq=3, event_id=_event_id(1), entity_key="a"),
            _snapshot_source_row(stream_seq=4, event_id=_event_id(2), entity_key="b"),
        ],
        [
            _snapshot_business_row(event_id=_event_id(1), entity_key="a"),
            _snapshot_business_row(event_id=_event_id(2), entity_key="b"),
        ],
    ]
    repo = _repo(cursor, max_snapshot_items=1)
    with pytest.raises(SyncBackpressureError):
        repo.sync_create_snapshot("remote-a", "consumer-a", "request-id", 10)


def test_sync_create_snapshot_maps_statement_timeout_to_stable_error() -> None:
    class _QueryTimeout(RuntimeError):
        pgcode = "57014"

    class _TimeoutTx:
        def __enter__(self):
            raise _QueryTimeout("driver-specific timeout details")

        def __exit__(self, exc_type, exc, tb):
            return False

    cursor = _FakeCursor()
    repo = _repo(cursor)
    repo._store._pool_manager.transaction = lambda *_: _TimeoutTx()

    with pytest.raises(SyncBackpressureError, match="^snapshot_create_timeout$"):
        repo.sync_create_snapshot("remote-a", "consumer-a", "request-id", 10)


def test_sync_create_snapshot_maps_serialization_race_to_in_progress() -> None:
    class _SerializationRace(RuntimeError):
        pgcode = "40001"

    class _SerializationTx:
        def __enter__(self):
            raise _SerializationRace("driver-specific serialization details")

        def __exit__(self, exc_type, exc, tb):
            return False

    cursor = _FakeCursor()
    repo = _repo(cursor)
    repo._store._pool_manager.transaction = lambda *_: _SerializationTx()

    with pytest.raises(SyncBackpressureError, match="^snapshot_in_progress$"):
        repo.sync_create_snapshot("remote-a", "consumer-a", "request-id", 10)


def test_sync_create_snapshot_rejects_active_snapshot_caps() -> None:
    cursor = _FakeCursor()
    cursor.fetchone_queue = [
        None,
        (0, 5),  # active tenant limit hit
    ]
    repo = _repo(
        cursor,
        max_active_snapshots_per_tenant=3,
        max_active_snapshots_per_remote=10,
    )
    with pytest.raises(SyncBackpressureError):
        repo.sync_create_snapshot("remote-a", "consumer-a", "request-id", 10)

    cursor = _FakeCursor()
    cursor.fetchone_queue = [
        None,
        (5, 5),  # active remote cap hit
    ]
    repo = _repo(
        cursor,
        max_active_snapshots_per_remote=2,
        max_active_snapshots_per_tenant=10,
    )
    with pytest.raises(SyncBackpressureError):
        repo.sync_create_snapshot("remote-a", "consumer-a", "request-id", 10)


def test_sync_snapshot_page_validates_binding_and_anchor() -> None:
    anchor = _event_anchor("fn")
    cursor = _FakeCursor()
    cursor.fetchone_queue = [
        (42, "remote-a", "consumer-a", datetime.now(timezone.utc) + timedelta(minutes=5)),
        (1,),
        (1,),
    ]
    cursor.fetchall_queue = [[
        _snapshot_event_dict(event_id=_event_id(1)),
    ]]
    repo = _repo(cursor)
    claims = SyncCursorClaims(
        1,
        "kid",
        "tenant-sync",
        "remote-a",
        "consumer-a",
        0,
        42,
        "snapshot-id",
        anchor,
        datetime.now(timezone.utc),
        datetime.now(timezone.utc) + timedelta(minutes=1),
    )
    page = repo.sync_snapshot_page("remote-a", "consumer-a", claims, 10)

    assert isinstance(page, SyncSnapshotPage)
    assert page.resume_seq == 42
    assert tuple(event.event_id for event in page.events) == (_event_id(1),)

    bad_claim = SyncCursorClaims(
        1,
        "kid",
        "tenant-sync",
        "remote-other",
        "consumer-a",
        0,
        42,
        "snapshot-id",
        anchor,
        datetime.now(timezone.utc),
        datetime.now(timezone.utc) + timedelta(minutes=1),
    )
    with pytest.raises(ValueError):
        _repo(_FakeCursor()).sync_snapshot_page("remote-a", "consumer-a", bad_claim, 1)

    now = datetime.now(timezone.utc)
    bad_expired = SyncCursorClaims(
        1,
        "kid",
        "tenant-sync",
        "remote-a",
        "consumer-a",
        0,
        42,
        "snapshot-id",
        anchor,
        now - timedelta(minutes=1),
        now,
    )
    with pytest.raises(SyncCursorExpired):
        _repo(_FakeCursor()).sync_snapshot_page("remote-a", "consumer-a", bad_expired, 1)


def test_sync_snapshot_page_reports_verified_missing_snapshot_as_expired() -> None:
    cursor = _FakeCursor()
    cursor.fetchone_queue = [None]
    repo = _repo(cursor)
    now = datetime.now(timezone.utc)
    claims = SyncCursorClaims(
        1,
        "kid",
        "tenant-sync",
        "remote-a",
        "consumer-a",
        0,
        42,
        "expired-snapshot",
        _event_anchor("fn"),
        now,
        now + timedelta(minutes=1),
    )

    with pytest.raises(SyncCursorExpired, match="^snapshot_expired$"):
        repo.sync_snapshot_page("remote-a", "consumer-a", claims, 10)


def test_sync_register_target_disallows_self_target_and_uses_remote_identity() -> None:
    cursor = _FakeCursor()
    repo = _repo(cursor)

    with pytest.raises(ValueError):
        repo.sync_register_target("node-local")

    cursor.fetchone_queue = [
        None,
        (10,),
        (0,),
    ]
    repo.sync_register_target("remote-target")
    assert not any(
        "memplex_sync_local_identity" in sql for sql, _ in cursor.executed
    )
    insert_sqls = [item for item in cursor.executed if item[0].startswith("INSERT INTO memplex_sync_targets")]
    assert len(insert_sqls) == 1
    assert insert_sqls[0][1][2] == "remote-target"


def test_sync_claim_generates_unique_lease_per_delivery() -> None:
    cursor = _FakeCursor()
    cursor.fetchall_queue = [[
        _outbox_row_with_attempt(stream_seq=10, event_id=_event_id(10)),
        _outbox_row_with_attempt(stream_seq=12, event_id=_event_id(12)),
    ]]
    repo = _repo(cursor)

    deliveries = repo.sync_claim("target-a", limit=2, lease_seconds=30)
    assert len(deliveries) == 2
    assert len(set(item.lease_id for item in deliveries)) == 2
    assert any(
        "FOR UPDATE OF delivery SKIP LOCKED" in sql
        for sql, _ in cursor.executed
    )
    assert all(item.attempt == 1 for item in deliveries)
    claim_sql, claim_params = next(
        (sql, params)
        for sql, params in cursor.executed
        if "FOR UPDATE OF delivery SKIP LOCKED" in sql
    )
    assert "outbox.origin_node_id=%s" in claim_sql
    assert "node-local" in claim_params


def test_sync_ack_validates_lease_and_prevents_stale_or_mismatch_rows() -> None:
    delivery = _delivery_from_row(1, _event_id(1), attempt=1)

    cursor = _FakeCursor()
    cursor.fetchone_queue = [None]
    repo = _repo(cursor)
    repo.sync_ack(delivery, SyncReceipt(_event_id(1), "accepted"))

    cursor.fetchone_queue = [
        (1, "leased", delivery.lease_id, datetime.now(timezone.utc) + timedelta(minutes=1)),
    ]
    cursor.rowcount_queue = [1]
    repo = _repo(cursor)
    repo.sync_ack(delivery, SyncReceipt(_event_id(1), "accepted"))

    assert any(sql.startswith("UPDATE memplex_sync_deliveries") for sql, _ in cursor.executed)

    cursor.fetchone_queue = [
        (1, "delivered", None, datetime.now(timezone.utc) + timedelta(minutes=1)),
    ]
    repo = _repo(cursor)
    repo.sync_ack(delivery, SyncReceipt(_event_id(1), "accepted"))

    cursor.fetchone_queue = [
        (
            1,
            "leased",
            "f5a4dce1-7e6f-4bf5-b3a7-88f7e9eb9f11",
            datetime.now(timezone.utc) + timedelta(minutes=1),
        ),
    ]
    with pytest.raises(SyncDeliveryBusy):
        _repo(cursor).sync_ack(delivery, SyncReceipt(_event_id(1), "accepted"))

    cursor.fetchone_queue = [
        (
            1,
            "leased",
            delivery.lease_id,
            datetime.now(timezone.utc) - timedelta(minutes=1),
        ),
    ]
    with pytest.raises(SyncDeliveryBusy):
        _repo(cursor).sync_ack(delivery, SyncReceipt(_event_id(1), "accepted"))


def test_sync_ack_batch_locks_all_rows_before_updating_any() -> None:
    first = _delivery_from_row(1, _event_id(1), attempt=1)
    second = _delivery_from_row(2, _event_id(2), attempt=1)
    cursor = _FakeCursor()
    cursor.fetchone_queue = [
        (
            1,
            "leased",
            first.lease_id,
            datetime.now(timezone.utc) + timedelta(minutes=1),
        ),
        (
            2,
            "leased",
            "123e4567-e89b-42d3-a456-426614174999",
            datetime.now(timezone.utc) + timedelta(minutes=1),
        ),
    ]

    with pytest.raises(SyncDeliveryBusy, match="identity"):
        _repo(cursor).sync_ack_batch(
            [first, second],
            (
                SyncReceipt(first.event.event_id, "accepted"),
                SyncReceipt(second.event.event_id, "accepted"),
            ),
        )

    assert not any(
        sql.lstrip().startswith("UPDATE memplex_sync_deliveries")
        for sql, _ in cursor.executed
    )

def test_sync_fail_uses_current_attempt_for_backoff_and_dead_letter() -> None:
    now = datetime(2026, 8, 11, tzinfo=timezone.utc)
    delivery = _delivery_from_row(5, _event_id(5), attempt=2)

    cursor = _FakeCursor()
    cursor.fetchone_queue = [
        (5, "leased", delivery.lease_id, datetime.now(timezone.utc) + timedelta(minutes=1), 1),
    ]
    repo = _repo(cursor, max_attempts=2)
    cursor.rowcount_queue = [1]
    repo.sync_fail(delivery, "temporary", now)
    assert any(
        "state='pending'" in sql and "attempt_count=" not in sql
        for sql, _ in cursor.executed if sql.strip().upper().startswith("UPDATE MEMPLEX_SYNC_DELIVERIES")
    )

    cursor = _FakeCursor()
    cursor.fetchone_queue = [
        (5, "leased", delivery.lease_id, datetime.now(timezone.utc) + timedelta(minutes=1), 2),
    ]
    cursor.rowcount_queue = [1]
    repo = _repo(cursor, max_attempts=2)
    repo.sync_fail(delivery, "final", now)
    assert any("state='dead_letter'" in sql for sql, _ in cursor.executed)


def test_sync_dead_letter_is_terminal_on_first_attempt() -> None:
    now = datetime(2026, 8, 11, tzinfo=timezone.utc)
    delivery = _delivery_from_row(5, _event_id(5), attempt=1)
    cursor = _FakeCursor()
    cursor.fetchone_queue = [
        (
            5,
            "leased",
            delivery.lease_id,
            datetime.now(timezone.utc) + timedelta(minutes=1),
        )
    ]
    cursor.rowcount_queue = [1]

    _repo(cursor).sync_dead_letter(delivery, "remote_batch_rejected", now)

    assert any(
        "state='dead_letter'" in sql and params[-1] == delivery.lease_id
        for sql, params in cursor.executed
        if sql.lstrip().startswith("UPDATE memplex_sync_deliveries")
    )


def test_sync_replay_dead_letter_uses_for_update_and_returns_true() -> None:
    cursor = _FakeCursor()
    cursor.fetchone_queue = [(11,), (11,)]
    repo = _repo(cursor)
    cursor.rowcount_queue = [1]
    assert repo.sync_replay_dead_letter("target-a", _event_id(111)) is True

    cursor.fetchone_queue = [None]
    assert _repo(cursor).sync_replay_dead_letter("target-a", _event_id(112)) is False

    assert any("FOR UPDATE" in sql for sql, _ in cursor.executed)


def test_sync_list_dead_letters_returns_only_fixed_safe_fields() -> None:
    cursor = _FakeCursor()
    cursor.fetchall_queue = [[("target-a", _event_id(1), 3, "remote_rejected")]]

    rows = _repo(cursor).sync_list_dead_letters(limit=5)

    assert [item.to_dict() for item in rows] == [
        {
            "target_id": "target-a",
            "event_id": _event_id(1),
            "attempt": 3,
            "error_code": "remote_rejected",
        }
    ]
    sql, params = next(
        (sql, params)
        for sql, params in cursor.executed
        if "state='dead_letter'" in sql and "ORDER BY" in sql
    )
    assert "last_error_code" in sql
    assert params[-1] == 5


def test_only_claim_uses_skip_locked_for_delivery_state_transitions() -> None:
    cursor = _FakeCursor()
    delivery = _delivery_from_row(1, _event_id(1), attempt=1)
    lease_until = datetime.now(timezone.utc) + timedelta(minutes=1)
    cursor.fetchone_queue = [
        (1, "leased", delivery.lease_id, lease_until),
        (1, "leased", delivery.lease_id, lease_until, 1),
        None,
    ]
    cursor.rowcount_queue = [1, 1]
    repo = _repo(cursor)

    repo.sync_ack(delivery, SyncReceipt(delivery.event.event_id, "accepted"))
    repo.sync_fail(delivery, "retry", datetime.now(timezone.utc))
    assert repo.sync_replay_dead_letter("target-a", delivery.event.event_id) is False

    locking_sql = [sql for sql, _ in cursor.executed if "FOR UPDATE" in sql]
    assert locking_sql
    assert all("SKIP LOCKED" not in sql for sql in locking_sql)


def test_sync_set_target_enabled_fails_closed_on_unknown_target() -> None:
    cursor = _FakeCursor()
    cursor.rowcount_queue = [0]
    with pytest.raises(ValueError):
        _repo(cursor).sync_set_target_enabled("missing-target", True)

    cursor.rowcount_queue = [1]
    _repo(cursor).sync_set_target_enabled("target-a", False)


def test_sync_status_uses_transaction_and_reads_counts() -> None:
    cursor = _FakeCursor()
    cursor.fetchone_queue = [(1, 2, 3, 4, 5)]
    repo = _repo(cursor)

    status = repo.sync_status()

    assert isinstance(status, SyncStatus)
    assert status == SyncStatus(1, 2, 3, 4, 5)
    assert len(repo._store._pool_manager.transaction_calls) >= 1
    assert len(repo._store._pool_manager.read_cursor_calls) == 0


def test_sync_dispatch_status_filters_to_local_origin() -> None:
    cursor = _FakeCursor()
    cursor.fetchone_queue = [(1, 2, 3, 4, 5)]
    repo = _repo(cursor)

    status = repo.sync_dispatch_status()

    assert status == SyncStatus(1, 2, 3, 4, 5)
    status_sql, status_params = next(
        (sql, params)
        for sql, params in cursor.executed
        if "state='pending'" in sql and "memplex_sync_outbox" in sql
    )
    assert "outbox.origin_node_id=%s" in status_sql
    assert status_params.count("node-local") == 4


def test_sync_compact_uses_policy_cutoffs_and_exact_inputs() -> None:
    cursor = _FakeCursor()
    cursor.fetchone_queue = [(3,)]
    repo = _repo(cursor, consumer_ttl_seconds=20, retention_min_seconds=10)
    now = datetime(2026, 8, 11, 12, 0, tzinfo=timezone.utc)

    assert repo.sync_compact(now, limit=4) == 3
    compact_calls = [
        (sql, params)
        for sql, params in cursor.executed
        if "memplex_sync_compact" in sql
    ]
    assert compact_calls == [
        (
            "SELECT memplex_sync_compact(%s, %s, %s)",
            (now - timedelta(seconds=10), now - timedelta(seconds=20), 4),
        )
    ]
    with pytest.raises(TypeError):
        repo.sync_compact(now.replace(tzinfo=None), limit=1)
    with pytest.raises(TypeError):
        repo.sync_compact(now, limit=True)
    with pytest.raises(ValueError):
        repo.sync_compact(now, limit=0)


def test_sync_apply_batch_validates_and_delegates_only_opaque_envelope() -> None:
    batch = _batch()
    expected = SyncBatchResult(
        batch.batch_id,
        batch.request_digest,
        "accepted",
        (SyncReceipt(batch.events[0].event_id, "accepted"),),
    )
    seen = []

    class _Executor:
        def apply(self, envelope):
            seen.append(envelope)
            return expected

    repo = _repo(_FakeCursor())
    repo._store._inbound_executor = _Executor()

    assert repo.sync_apply_batch(batch) is expected
    assert len(seen) == 1
    assert seen[0].batch == batch
    assert seen[0].canonical_bytes == batch.canonical_bytes
    assert seen[0].request_digest == batch.request_digest
    with pytest.raises(TypeError):
        repo.sync_apply_batch(object())


def test_sync_apply_batch_requires_verified_inbound_executor() -> None:
    repo = _repo(_FakeCursor())

    with pytest.raises(RuntimeError, match="inbound executor is not available"):
        repo.sync_apply_batch(_batch())


def test_sync_methods_do_not_use_read_cursor() -> None:
    cursor = _FakeCursor()
    cursor.fetchone_queue = [
        (4,),
        (0,),
        (0,),
    ]
    cursor.fetchall_queue = [[_outbox_row(stream_seq=1, event_id=_event_id(1))]]

    repo = _repo(cursor)
    repo.sync_page("remote-a", "consumer-a", None, 1)
    repo.sync_claim("target-a", limit=1, lease_seconds=10)
    repo.sync_status()
    repo.sync_set_target_enabled("target-a", True)
    repo.sync_replay_dead_letter("target-a", _event_id(1))

    assert not repo._store._pool_manager.read_cursor_calls
    assert len(repo._store._pool_manager.transaction_calls) >= 4


def test_sync_snapshot_methods_do_not_use_read_cursor() -> None:
    cursor = _FakeCursor()
    cursor.fetchone_queue = [
        ("snapshot-id", 1),
        (1, "remote-a", "consumer-a", datetime.now(timezone.utc) + timedelta(minutes=5)),
        (1,),
    ]
    cursor.fetchall_queue = [[_snapshot_event_dict(event_id=_event_id(2), entity_key="fn")]]

    repo = _repo(cursor)
    first_page = repo.sync_create_snapshot("remote-a", "consumer-a", "request-id", 1)
    assert first_page.snapshot_id == "snapshot-id"
    repo.sync_snapshot_page(
        "remote-a",
        "consumer-a",
        SyncCursorClaims(
            1,
            "kid",
            "tenant-sync",
            "remote-a",
            "consumer-a",
            0,
            1,
            "snapshot-id",
            _event_anchor("fn"),
            datetime.now(timezone.utc),
            datetime.now(timezone.utc) + timedelta(minutes=1),
        ),
        1,
    )

    assert not repo._store._pool_manager.read_cursor_calls
    assert len(repo._store._pool_manager.transaction_calls) >= 2
