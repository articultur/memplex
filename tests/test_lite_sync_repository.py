from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memplex.models import (
    Fact,
    Function,
    GraphData,
    GraphEdge,
    Observation,
    Preference,
    SourceDocument,
    SourceType,
)
from memplex.storage.lite.store import LiteMemoryStore
from memplex.sync_protocol import (
    SyncBatch,
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
    SyncStreamItem,
    SyncVersion,
)
from memplex.sync_repository import (
    SyncBackpressureError,
    SyncCapturePolicy,
    SyncCursorExpired,
)


def _store(
    tmp_path: Path,
    *,
    max_pending: int = 100,
    retention_min_seconds: int = 86400,
) -> LiteMemoryStore:
    return LiteMemoryStore(
        path=tmp_path / "memory.json",
        sync_capture_policy=SyncCapturePolicy("required", "lite-local"),
        sync_max_pending_events=max_pending,
        sync_retention_min_seconds=retention_min_seconds,
    )


def _function(identifier: str) -> Function:
    return Function(
        id=identifier,
        name=identifier,
        name_normalized=identifier,
        tenant_id="tenant-a",
        owner_subject_id="subject-a",
        owner="subject-a",
        workspace_id="workspace-a",
        visibility="workspace",
        provenance={"agent_id": "agent-a", "session_id": "session-a"},
    )


def _function_for_tenant(identifier: str, tenant_id: str) -> Function:
    node = _function(identifier)
    node.tenant_id = tenant_id
    return node


def _source() -> SourceDocument:
    return SourceDocument(type="text", source_type=SourceType.WIKI)


def _identity(node):
    node.tenant_id = "tenant-a"
    node.owner_subject_id = "subject-a"
    node.owner = "subject-a"
    node.workspace_id = "workspace-a"
    node.visibility = "workspace"
    node.provenance = {"agent_id": "agent-a", "session_id": "session-a"}
    return node


def _remote_event(identifier: str, *, occurred_at: datetime) -> SyncEvent:
    event_id = str(uuid.uuid4())
    node = _function(identifier)
    return SyncEvent(
        1,
        event_id,
        "remote-a",
        SyncNodeType.FUNCTION,
        SyncEntityKey.node(identifier),
        SyncOperation.UPSERT,
        str(SyncVersion.create(occurred_at, "remote-a", event_id)),
        SyncScope(
            "tenant-a",
            "subject-a",
            "workspace-a",
            "workspace",
            "agent-a",
            "session-a",
        ),
        node.to_dict(),
    )


def test_local_function_capture_delivery_and_ack_survive_reopen(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert store.schema_version == 2
    store.sync_register_target("remote-b")

    store.add(_function("function-a"), _source())

    assert store.sync_status().pending == 1
    reopened = _store(tmp_path)
    assert reopened.get("function-a") is not None
    assert reopened.sync_status().pending == 1

    deliveries = reopened.sync_claim("remote-b", limit=10, lease_seconds=30)
    assert len(deliveries) == 1
    delivery = deliveries[0]
    assert delivery.event.entity_key.node_id == "function-a"
    assert delivery.event.scope.agent_id == "agent-a"
    assert delivery.event.scope.session_id == "session-a"

    reopened.sync_ack(
        delivery,
        SyncReceipt(delivery.event.event_id, "accepted"),
    )
    assert _store(tmp_path).sync_status().delivered == 1


def test_clear_rejects_before_partial_delete_when_delivery_quota_is_full(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, max_pending=2)
    store.sync_register_target("remote-b")
    store.add(_function("function-a"), _source())
    store.add(_function("function-b"), _source())
    before = (tmp_path / "memory.json").read_bytes()

    with pytest.raises(SyncBackpressureError):
        store.clear()

    assert {item.id for item in store.list_functions()} == {
        "function-a",
        "function-b",
    }
    assert (tmp_path / "memory.json").read_bytes() == before
    assert store.sync_status().pending == 2


def test_dispatch_status_and_terminal_rejection_only_cover_local_origin(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.sync_register_target("remote-b")
    store.add(_function("local-delivery"), _source())
    remote = _remote_event(
        "remote-delivery", occurred_at=datetime(2026, 8, 11, tzinfo=timezone.utc)
    )
    store.sync_apply_batch(
        SyncBatch(1, str(uuid.uuid4()), "remote-a", (remote,))
    )

    assert store.sync_status().pending == 2
    assert store.sync_dispatch_status().pending == 1
    delivery = store.sync_claim("remote-b", limit=10, lease_seconds=30)[0]
    store.sync_dead_letter(
        delivery, "remote_batch_rejected", datetime.now(timezone.utc)
    )

    assert store.sync_dispatch_status().dead_letters == 1
    assert [item.to_dict() for item in store.sync_list_dead_letters(limit=10)] == [
        {
            "target_id": "remote-b",
            "event_id": delivery.event.event_id,
            "attempt": 1,
            "error_code": "remote_batch_rejected",
        }
    ]
    assert store.sync_status().pending == 1


def test_batch_ack_rejects_one_stale_lease_without_partial_delivery(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.sync_register_target("remote-b")
    store.add_batch(
        [_function("batch-ack-a"), _function("batch-ack-b")],
        [_source(), _source()],
    )
    deliveries = store.sync_claim("remote-b", limit=10, lease_seconds=30)
    stale = SyncDelivery(
        deliveries[1].target_id,
        deliveries[1].event,
        deliveries[1].attempt,
        str(uuid.uuid4()),
        deliveries[1].lease_expires_at,
    )
    receipts = tuple(
        SyncReceipt(item.event.event_id, "accepted")
        for item in deliveries
    )

    with pytest.raises(Exception, match="lease"):
        store.sync_ack_batch([deliveries[0], stale], receipts)

    assert _store(tmp_path).sync_dispatch_status().leased == 2


def test_local_capture_quota_rejects_business_and_sync_in_one_pair(tmp_path: Path) -> None:
    store = _store(tmp_path, max_pending=1)
    store.sync_register_target("remote-b")
    store.add(_function("first"), _source())
    before_memory = (tmp_path / "memory.json").read_bytes()
    before_changelog = (tmp_path / "changelog.json").read_bytes()

    with pytest.raises(SyncBackpressureError):
        store.add(_function("second"), _source())

    assert (tmp_path / "memory.json").read_bytes() == before_memory
    assert (tmp_path / "changelog.json").read_bytes() == before_changelog
    reopened = _store(tmp_path, max_pending=1)
    assert reopened.get("first") is not None
    assert reopened.get("second") is None
    assert reopened.sync_status().pending == 1


def test_sync_enabled_lite_rejects_second_tenant_without_partial_commit(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.add(_function("tenant-a-function"), _source())
    before_memory = (tmp_path / "memory.json").read_bytes()
    before_changelog = (tmp_path / "changelog.json").read_bytes()

    with pytest.raises(ValueError, match="single tenant"):
        store.add(_function_for_tenant("tenant-b-function", "tenant-b"), _source())

    assert (tmp_path / "memory.json").read_bytes() == before_memory
    assert (tmp_path / "changelog.json").read_bytes() == before_changelog
    reopened = _store(tmp_path)
    assert reopened.get("tenant-a-function") is not None
    assert reopened.get("tenant-b-function") is None


def test_sync_enabled_lite_rejects_preexisting_multi_tenant_store(
    tmp_path: Path,
) -> None:
    path = tmp_path / "memory.json"
    legacy_local = LiteMemoryStore(path=path)
    legacy_local.add(_function("tenant-a-function"), _source())
    legacy_local.add(
        _function_for_tenant("tenant-b-function", "tenant-b"), _source()
    )
    before_memory = path.read_bytes()
    before_changelog = (tmp_path / "changelog.json").read_bytes()

    with pytest.raises(ValueError, match="single tenant"):
        _store(tmp_path)

    assert path.read_bytes() == before_memory
    assert (tmp_path / "changelog.json").read_bytes() == before_changelog


def test_stream_and_snapshot_cursors_must_match_lite_tenant_binding(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.add(_function("bound-function"), _source())
    page = store.sync_page("remote-b", "consumer-b", None, 10)
    now = datetime.now(timezone.utc)
    wrong_stream_cursor = SyncCursorClaims(
        1,
        "key-1",
        "tenant-b",
        "remote-b",
        "consumer-b",
        page.next_after_seq,
        page.snapshot_seq,
        None,
        None,
        now,
        now + timedelta(minutes=5),
    )
    with pytest.raises(ValueError, match="invalid_cursor"):
        store.sync_page("remote-b", "consumer-b", wrong_stream_cursor, 10)

    snapshot = store.sync_create_snapshot("remote-b", "consumer-b", "request-b", 1)
    assert snapshot.next_anchor is None
    wrong_snapshot_cursor = SyncCursorClaims(
        1,
        "key-1",
        "tenant-b",
        "remote-b",
        "consumer-b",
        0,
        snapshot.resume_seq,
        snapshot.snapshot_id,
        SyncSnapshotAnchor.from_event(snapshot.events[0]),
        now,
        now + timedelta(minutes=5),
    )
    with pytest.raises(ValueError, match="invalid_cursor"):
        store.sync_snapshot_page(
            "remote-b", "consumer-b", wrong_snapshot_cursor, 10
        )


def test_snapshot_pages_are_stable_and_survive_reopen(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(_function("one"), _source())
    store.add(_function("two"), _source())

    first = store.sync_create_snapshot("remote-b", "consumer-b", "request-1", 1)
    assert len(first.events) == 1
    assert first.has_more is True
    assert first.next_anchor is not None

    now = datetime.now(timezone.utc)
    cursor = SyncCursorClaims(
        1,
        "key-1",
        "tenant-a",
        "remote-b",
        "consumer-b",
        0,
        first.resume_seq,
        first.snapshot_id,
        first.next_anchor,
        now,
        now + timedelta(minutes=5),
    )
    second = _store(tmp_path).sync_snapshot_page(
        "remote-b", "consumer-b", cursor, 10
    )
    assert len(second.events) == 1
    assert second.events[0].entity_key != first.events[0].entity_key
    assert second.has_more is False


def test_missing_verified_snapshot_is_reported_as_expired(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.add(_function("expired"), _source())
    snapshot = store.sync_create_snapshot("remote-b", "consumer-b", "request-1", 1)
    now = datetime.now(timezone.utc)
    claims = SyncCursorClaims(
        1,
        "key-1",
        "tenant-a",
        "remote-b",
        "consumer-b",
        0,
        snapshot.resume_seq,
        "missing-snapshot",
        SyncSnapshotAnchor.from_event(snapshot.events[0]),
        now,
        now + timedelta(minutes=5),
    )

    with pytest.raises(SyncCursorExpired, match="^snapshot_expired$"):
        store.sync_snapshot_page("remote-b", "consumer-b", claims, 10)


def test_stream_cursor_pins_retention_until_confirmed_progress(tmp_path: Path) -> None:
    store = _store(tmp_path, retention_min_seconds=1)
    store.add(_function("cursor-item"), _source())
    page = store.sync_page("remote-b", "consumer-b", None, 10)
    assert len(page.items) == 1
    compact_at = datetime.now(timezone.utc) + timedelta(seconds=2)
    assert store.sync_compact(compact_at, limit=10) == 0

    now = datetime.now(timezone.utc)
    cursor = SyncCursorClaims(
        1,
        "key-1",
        "tenant-a",
        "remote-b",
        "consumer-b",
        page.next_after_seq,
        page.snapshot_seq,
        None,
        None,
        now,
        now + timedelta(minutes=5),
    )
    complete = store.sync_page("remote-b", "consumer-b", cursor, 10)
    assert complete.items == ()
    assert store.sync_compact(compact_at, limit=10) == 1


def test_apply_batch_is_atomic_idempotent_lww_and_no_echo(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.sync_register_target("remote-a")
    store.sync_register_target("remote-b")
    occurred_at = datetime(2026, 1, 1, tzinfo=timezone.utc)
    event = _remote_event("remote-function", occurred_at=occurred_at)
    batch = SyncBatch(1, str(uuid.uuid4()), "remote-a", (event,))

    result = store.sync_apply_batch(batch)
    assert [receipt.outcome for receipt in result.receipts] == ["accepted"]
    assert store.get("remote-function") is not None
    assert store.sync_status().pending == 1

    duplicate = _store(tmp_path).sync_apply_batch(batch)
    assert duplicate.to_dict() == result.to_dict()
    assert _store(tmp_path).sync_status().pending == 1
    snapshot = store.sync_create_snapshot("remote-c", "consumer-c", "request-c", 10)
    assert snapshot.events[0].origin_node_id == "remote-a"

    older = _remote_event(
        "remote-function", occurred_at=occurred_at - timedelta(seconds=1)
    )
    older_batch = SyncBatch(1, str(uuid.uuid4()), "remote-a", (older,))
    conflict = store.sync_apply_batch(older_batch)
    assert conflict.receipts[0].outcome == "rejected_conflict"
    assert store.sync_status().pending == 1


def test_mixed_origin_delivery_is_pull_only_and_cursor_confirmation_acks_target(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.sync_register_target("remote-a")
    store.sync_register_target("remote-b")
    event = _remote_event(
        "remote-function",
        occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    store.sync_apply_batch(
        SyncBatch(1, str(uuid.uuid4()), "remote-a", (event,))
    )

    # The local dispatcher must not impersonate remote-a when talking to
    # remote-b.  The durable delivery remains as a retention pin until
    # remote-b confirms it through its own signed pull cursor.
    assert store.sync_claim("remote-b", limit=10, lease_seconds=30) == []
    assert store.sync_status().pending == 1

    page = store.sync_page("remote-b", "consumer-b", None, 10)
    assert [item.event.event_id for item in page.items] == [event.event_id]
    now = datetime.now(timezone.utc)
    cursor = SyncCursorClaims(
        1,
        "key-1",
        "tenant-a",
        "remote-b",
        "consumer-b",
        page.next_after_seq,
        page.snapshot_seq,
        None,
        None,
        now,
        now + timedelta(minutes=5),
    )
    store.sync_page("remote-b", "consumer-b", cursor, 10)

    assert store.sync_status().pending == 0
    assert store.sync_status().delivered == 1


def test_apply_page_advances_cursor_atomically_and_compaction_honours_delivery(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.sync_register_target("remote-b")
    event = _remote_event("from-page", occurred_at=datetime(2025, 1, 1, tzinfo=timezone.utc))
    page = SyncPage((SyncStreamItem(1, event),), 1, 1, False)

    applied = store.sync_apply_page("remote-a", page)
    assert applied.applied == 1
    assert applied.cursor_advanced == 1
    assert _store(tmp_path).get("from-page") is not None
    assert store.sync_apply_page("remote-a", page).applied == 0

    assert store.sync_compact(datetime(2030, 1, 1, tzinfo=timezone.utc), limit=10) == 0
    pulled = store.sync_page("remote-b", "consumer-b", None, 10)
    now = datetime.now(timezone.utc)
    store.sync_page(
        "remote-b",
        "consumer-b",
        SyncCursorClaims(
            1,
            "key-1",
            "tenant-a",
            "remote-b",
            "consumer-b",
            pulled.next_after_seq,
            pulled.snapshot_seq,
            None,
            None,
            now,
            now + timedelta(minutes=5),
        ),
        10,
    )
    assert store.sync_compact(datetime(2030, 1, 1, tzinfo=timezone.utc), limit=10) == 1


def test_sparse_remote_page_applies_atomically_replays_after_reopen_and_does_not_pin_compaction(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, retention_min_seconds=1)
    first = _remote_event(
        "sparse-first", occurred_at=datetime(2025, 1, 1, tzinfo=timezone.utc)
    )
    second = _remote_event(
        "sparse-second", occurred_at=datetime(2025, 1, 2, tzinfo=timezone.utc)
    )
    page = SyncPage(
        (SyncStreamItem(3, first), SyncStreamItem(8, second)), 8, 8, False
    )

    applied = store.sync_apply_page("remote-a", page)

    assert (applied.applied, applied.cursor_advanced) == (2, 8)
    reopened = _store(tmp_path, retention_min_seconds=1)
    assert {node.id for node in reopened.list_functions()} >= {
        "sparse-first",
        "sparse-second",
    }
    persisted_sync = reopened._durability.load_authoritative().memory["sync"]
    assert persisted_sync["cursors"] == []
    assert persisted_sync["inbound_cursors"][0]["after_seq"] == 8
    assert reopened.sync_apply_page("remote-a", page).to_dict() == {
        "applied": 0,
        "duplicate": 0,
        "conflict": 0,
        "cursor_advanced": 8,
    }
    assert reopened.sync_compact(datetime(2030, 1, 1, tzinfo=timezone.utc), limit=10) == 2


def test_outbound_cursor_remote_id_cannot_impersonate_inbound_namespace(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path, retention_min_seconds=1)
    reserved_looking_remote = "\x00memplex-inbound:peer-target"
    store.sync_page(reserved_looking_remote, "consumer-a", None, 10)
    store.add(_function("locally-unconfirmed"), _source())

    # The outbound consumer is still at sequence zero, so the local outbox
    # event must remain pinned regardless of the remote identifier text.
    assert store.sync_compact(
        datetime.now(timezone.utc) + timedelta(seconds=2), limit=10
    ) == 0
    state = store._durability.load_authoritative().memory["sync"]
    assert state["cursors"][0]["remote_id"] == reserved_looking_remote
    assert state["inbound_cursors"] == []


@pytest.mark.parametrize("invalid", ("cross_tenant", "duplicate_identity"))
def test_invalid_inbound_page_is_rejected_before_any_lite_pair_write(
    tmp_path: Path, invalid: str
) -> None:
    store = _store(tmp_path)
    store.add(_function("existing"), _source())
    before_memory = (tmp_path / "memory.json").read_bytes()
    before_changelog = (tmp_path / "changelog.json").read_bytes()
    first = _remote_event(
        "incoming-first", occurred_at=datetime(2025, 1, 1, tzinfo=timezone.utc)
    )
    if invalid == "cross_tenant":
        second = _remote_event(
            "incoming-second", occurred_at=datetime(2025, 1, 2, tzinfo=timezone.utc)
        )
        second = SyncEvent(
            1,
            second.event_id,
            second.origin_node_id,
            second.node_type,
            second.entity_key,
            second.operation,
            second.version,
            SyncScope(
                "tenant-b",
                second.scope.owner_subject_id,
                second.scope.workspace_id,
                second.scope.visibility,
                second.scope.agent_id,
                second.scope.session_id,
            ),
            second.to_dict()["payload"],
        )
    else:
        second = first
    page = SyncPage(
        (SyncStreamItem(3, first), SyncStreamItem(8, second)), 8, 8, False
    )

    with pytest.raises(
        ValueError,
        match="page (event tenant does not match repository tenant|contains duplicate event identities)",
    ):
        store.sync_apply_page("remote-a", page)

    assert (tmp_path / "memory.json").read_bytes() == before_memory
    assert (tmp_path / "changelog.json").read_bytes() == before_changelog
    assert _store(tmp_path).get("incoming-first") is None


def test_all_local_node_types_edges_and_tombstones_share_capture_commit(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.sync_register_target("remote-b")
    left = _function("left")
    right = _function("right")
    edge = GraphEdge(
        "left",
        "right",
        "REFERENCES",
        weight=0.5,
        evidence=["unit"],
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    store.merge(GraphData(nodes=[left, right], edges=[edge]))
    store.add_fact(_identity(Fact(id="fact-a", subject="s", predicate="p", object_="o")))
    store.add_preference(
        _identity(Preference(id="pref-a", aspect="editor", preference="vim"))
    )
    store.add_observation(
        _identity(
            Observation(
                id="obs-a",
                event="deploy",
                context="prod",
                observed_at="2026-01-01T00:00:00+00:00",
                category="discovery",
            )
        )
    )

    assert store.sync_status().pending == 6
    store.delete("left")
    store.delete_fact("fact-a")
    store.delete_preference("pref-a")
    reopened = _store(tmp_path)
    assert reopened.get("left") is None
    assert reopened.get_fact("fact-a") is None
    assert reopened.get_preference("pref-a") is None
    assert reopened.sync_status().pending == 10


def test_merge_sync_page_orders_functions_before_edge_for_empty_destination(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "source"
    destination_path = tmp_path / "destination"
    source_path.mkdir()
    destination_path.mkdir()
    source = _store(source_path)
    destination = _store(destination_path)
    left = _function("merge-left")
    right = _function("merge-right")
    edge = GraphEdge("merge-left", "merge-right", "REFERENCES")

    source.merge(GraphData(nodes=[left, right], edges=[edge]))
    page = source.sync_page("destination", "destination-consumer", None, 10)

    applied = destination.sync_apply_page("source", page)
    assert [item.event.node_type for item in page.items] == [
        SyncNodeType.FUNCTION,
        SyncNodeType.FUNCTION,
        SyncNodeType.EDGE,
    ]
    assert applied.applied == 3
    assert {node.id for node in destination.list_functions()} == {
        "merge-left",
        "merge-right",
    }
    destination_edges = destination.get_graph().edges
    assert len(destination_edges) == 1
    assert (
        destination_edges[0].source,
        destination_edges[0].target,
        destination_edges[0].edge_type,
    ) == ("merge-left", "merge-right", "REFERENCES")


def test_clear_sync_page_orders_edge_tombstone_before_function_tombstones(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "source"
    destination_path = tmp_path / "destination"
    source_path.mkdir()
    destination_path.mkdir()
    source = _store(source_path)
    destination = _store(destination_path)
    source.merge(
        GraphData(
            nodes=[_function("clear-left"), _function("clear-right")],
            edges=[GraphEdge("clear-left", "clear-right", "REFERENCES")],
        )
    )
    initial_page = source.sync_page(
        "destination", "destination-consumer", None, 10
    )
    destination.sync_apply_page("source", initial_page)
    now = datetime.now(timezone.utc)
    cursor = SyncCursorClaims(
        1,
        "key-1",
        "tenant-a",
        "destination",
        "destination-consumer",
        initial_page.next_after_seq,
        initial_page.snapshot_seq,
        None,
        None,
        now,
        now + timedelta(minutes=5),
    )

    source.clear()
    deletion_page = source.sync_page(
        "destination", "destination-consumer", cursor, 10
    )

    applied = destination.sync_apply_page("source", deletion_page)
    assert [item.event.node_type for item in deletion_page.items] == [
        SyncNodeType.EDGE,
        SyncNodeType.FUNCTION,
        SyncNodeType.FUNCTION,
    ]
    assert all(
        item.event.operation is SyncOperation.TOMBSTONE
        for item in deletion_page.items
    )
    assert applied.applied == 3
    assert destination.list_functions() == []
    assert destination.get_graph().edges == []


def test_clear_captures_every_node_and_edge_tombstone_in_one_pair(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.sync_register_target("remote-b")
    left = _function("clear-left")
    right = _function("clear-right")
    store.merge(
        GraphData(
            nodes=[left, right],
            edges=[GraphEdge("clear-left", "clear-right", "REFERENCES")],
        )
    )
    store.add_fact(
        _identity(Fact(id="clear-fact", subject="s", predicate="p", object_="o"))
    )
    store.add_preference(
        _identity(
            Preference(id="clear-pref", aspect="editor", preference="vim")
        )
    )
    store.add_observation(
        _identity(
            Observation(
                id="clear-obs",
                event="deploy",
                context="prod",
                observed_at="2026-01-01T00:00:00+00:00",
                category="discovery",
            )
        )
    )
    assert store.sync_status().pending == 6

    store.clear()

    reopened = _store(tmp_path)
    assert reopened.list_functions() == []
    assert reopened.list_facts() == []
    assert reopened.list_preferences() == []
    assert reopened.list_observations() == []
    assert reopened.get_graph().edges == []
    assert reopened.sync_status().pending == 12
    page = reopened.sync_page("remote-b", "clear-consumer", None, 100)
    tombstones = [
        item.event
        for item in page.items
        if item.event.operation is SyncOperation.TOMBSTONE
    ]
    assert len(tombstones) == 6
    assert {event.node_type for event in tombstones} == {
        SyncNodeType.FUNCTION,
        SyncNodeType.FACT,
        SyncNodeType.PREFERENCE,
        SyncNodeType.OBSERVATION,
        SyncNodeType.EDGE,
    }


def test_apply_batch_mid_event_fault_rolls_back_business_sync_and_pair_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _store(tmp_path)
    store.sync_register_target("remote-b")
    first = _remote_event("first-remote", occurred_at=datetime(2026, 1, 1, tzinfo=timezone.utc))
    second = _remote_event("second-remote", occurred_at=datetime(2026, 1, 2, tzinfo=timezone.utc))
    batch = SyncBatch(1, str(uuid.uuid4()), "remote-a", (first, second))
    memory_before = (tmp_path / "memory.json").read_bytes()
    changelog_before = (tmp_path / "changelog.json").read_bytes()
    original = store._sync_repository._append_event
    calls = 0

    def fail_second(event):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected apply fault")
        return original(event)

    monkeypatch.setattr(store._sync_repository, "_append_event", fail_second)
    with pytest.raises(RuntimeError, match="injected apply fault"):
        store.sync_apply_batch(batch)

    assert (tmp_path / "memory.json").read_bytes() == memory_before
    assert (tmp_path / "changelog.json").read_bytes() == changelog_before
    reopened = _store(tmp_path)
    assert reopened.get("first-remote") is None
    assert reopened.get("second-remote") is None


def test_delivery_append_fault_restores_old_business_and_sync_pair(
    tmp_path: Path,
) -> None:
    store = _store(tmp_path)
    store.sync_register_target("remote-b")
    memory_before = (tmp_path / "memory.json").read_bytes()
    changelog_before = (tmp_path / "changelog.json").read_bytes()

    class FailingDeliveries(list):
        def append(self, _item) -> None:
            raise RuntimeError("injected delivery append fault")

    original = store._sync_repository._append_event

    def fail_delivery(event):
        store._sync_state["deliveries"] = FailingDeliveries(
            store._sync_state["deliveries"]
        )
        return original(event)

    store._sync_repository._append_event = fail_delivery
    with pytest.raises(RuntimeError, match="injected delivery append fault"):
        store.add(_function("not-committed"), _source())

    assert (tmp_path / "memory.json").read_bytes() == memory_before
    assert (tmp_path / "changelog.json").read_bytes() == changelog_before
    assert _store(tmp_path).get("not-committed") is None


def test_preopened_peer_observes_committed_sync_generation(tmp_path: Path) -> None:
    writer = _store(tmp_path)
    reader = _store(tmp_path)
    writer.sync_register_target("remote-b")
    writer.add(_function("peer-visible"), _source())

    assert reader.sync_status().pending == 1
    page = reader.sync_page("remote-c", "consumer-c", None, 10)
    assert [item.event.entity_key.node_id for item in page.items] == ["peer-visible"]
