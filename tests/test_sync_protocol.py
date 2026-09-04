"""冻结 G004 同步协议的纯数据契约。"""

from __future__ import annotations

import hashlib
import inspect
import json
import shutil
import subprocess
from datetime import UTC, datetime, timedelta, timezone

import pytest

from memplex.models.memory import Function, sync_node_type_for_memory
from memplex.sync_protocol import (
    _MAX_BATCH_BYTES,
    SyncApplyResult,
    SyncBatch,
    SyncBatchResult,
    SyncCursorClaims,
    SyncCursorCodec,
    SyncDrainResult,
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
    SyncStreamItem,
    SyncVersion,
    _canonical_json_bytes,
)
from memplex.sync_repository import (
    SyncBatchRejected,
    SyncCursorExpired,
    SyncDeliveryBusy,
    SyncRepository,
)

EVENT_A = "018f3e7c-9b52-7f1f-a091-6f5ebf54b1d0"
EVENT_B = "018f3e7c-9b52-7f1f-a091-6f5ebf54b1d1"
NOW = datetime(2026, 8, 11, tzinfo=UTC)
USER_SCOPE = SyncScope("tenant-a", "alice", None, "user", None, None)
WORKSPACE_SCOPE = SyncScope("tenant-a", "alice", "workspace-a", "workspace", None, None)
SESSION_SCOPE = SyncScope(
    "tenant-a", "alice", "workspace-a", "session", "agent-a", "session-a",
)


def _event(
    *,
    event_id: str = EVENT_A,
    entity_key: SyncEntityKey | None = None,
    scope: SyncScope = USER_SCOPE,
) -> SyncEvent:
    return SyncEvent(
        protocol_version=1,
        event_id=event_id,
        origin_node_id="node-a",
        node_type=SyncNodeType.FUNCTION,
        entity_key=entity_key or SyncEntityKey.node("deploy"),
        operation=SyncOperation.UPSERT,
        version=str(SyncVersion.create(NOW, "node-a", event_id)),
        scope=scope,
        payload={"id": "deploy", "memory_type": "function"},
    )


def test_sync_event_rejects_weak_or_future_shapes() -> None:
    valid = {
        "protocol_version": 1,
        "event_id": EVENT_A,
        "origin_node_id": "node-a",
        "node_type": "function",
        "entity_key": "node:v1:ZGVwbG95",
        "operation": "upsert",
        "version": str(SyncVersion.create(NOW, "node-a", EVENT_A)),
        "scope": USER_SCOPE.to_dict(),
        "payload": {"id": "deploy", "memory_type": "function"},
    }
    assert SyncEvent.from_dict(valid).entity_key.node_id == "deploy"
    for bad in (
        {**valid, "protocol_version": True},
        {**valid, "protocol_version": 2},
        {**valid, "node_type": "future"},
        {**valid, "payload": []},
        {key: value for key, value in valid.items() if key != "scope"},
        {**valid, "scope": True},
        {**valid, "scope": {**USER_SCOPE.to_dict(), "future": "field"}},
        {**valid, "future_key": 1},
    ):
        with pytest.raises((TypeError, ValueError)):
            SyncEvent.from_dict(bad)


def test_entity_key_golden_vectors_are_canonical_and_reversible() -> None:
    node = SyncEntityKey.node("deploy")
    edge = SyncEntityKey.edge("deploy", "prod", "DEPENDS_ON")

    assert str(node) == "node:v1:ZGVwbG95"
    assert str(edge) == "edge:v1:WyJkZXBsb3kiLCJwcm9kIiwiREVQRU5EU19PTiJd"
    assert SyncEntityKey.parse(str(node)) == node
    assert SyncEntityKey.parse(str(edge)) == edge
    assert SyncEntityKey.parse(str(edge)).edge_parts == ("deploy", "prod", "DEPENDS_ON")


@pytest.mark.parametrize(
    "value",
    (
        "node:v1:ZGVwbG95=",
        "node:v2:ZGVwbG95",
        "node:v1:ZGVwbG95\n",
        "edge:v1:eyJhIjoxfQ",
        "edge:v1:WyJwcm9kIiwxLCJSRUZFUkVOQ0VTIl0",
    ),
)
def test_entity_key_rejects_noncanonical_or_future_values(value: str) -> None:
    with pytest.raises((TypeError, ValueError)):
        SyncEntityKey.parse(value)


def _unused_bit_base64url_alias(value: str) -> str:
    """Return a distinct base64url spelling for the same decoded bytes."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    remainder = len(value) % 4
    assert remainder in {2, 3}
    unused_bits = 4 if remainder == 2 else 2
    last_value = alphabet.index(value[-1])
    alias_value = (last_value & ~((1 << unused_bits) - 1)) | 1
    return value[:-1] + alphabet[alias_value]


def test_protocol_codecs_reject_base64url_padding_and_unused_bit_aliases() -> None:
    entity = SyncEntityKey.node("a")
    version = SyncVersion.create(NOW, "node-a", EVENT_A)
    codec = SyncCursorCodec("active", "a" * 32)
    claims = SyncCursorClaims(
        1, "active", "tenant-a", "remote-a", "consumer-a", 0, 0,
        None, None, NOW, NOW + timedelta(seconds=60),
    )
    token = codec.encode(claims)
    payload, signature = token.split(".")

    assert SyncEntityKey.parse(str(entity)) == entity
    assert SyncVersion.parse(str(version)) == version
    assert codec.decode(
        token,
        tenant_binding="tenant-a",
        remote_binding="remote-a",
        consumer_binding="consumer-a",
        now=NOW,
    ) == claims

    version_payload = str(version).split(":", maxsplit=1)[1]
    for malformed_entity in (
        f"{entity}=",
        f"node:v1:{_unused_bit_base64url_alias('YQ')}",
    ):
        with pytest.raises((TypeError, ValueError)):
            SyncEntityKey.parse(malformed_entity)
    for malformed_version in (
        f"v1:{version_payload}=",
        f"v1:{_unused_bit_base64url_alias(version_payload)}",
    ):
        with pytest.raises((TypeError, ValueError)):
            SyncVersion.parse(malformed_version)
    for malformed_cursor in (
        f"{payload}=.{signature}",
        f"{payload}.{signature}=",
        f"{payload}.{_unused_bit_base64url_alias(signature)}",
    ):
        with pytest.raises(SyncCursorExpired, match="^invalid_cursor$"):
            codec.decode(
                malformed_cursor,
                tenant_binding="tenant-a",
                remote_binding="remote-a",
                consumer_binding="consumer-a",
                now=NOW,
            )


def test_entity_key_rejects_weak_values_and_size_overflow() -> None:
    with pytest.raises(TypeError):
        SyncEntityKey.node(True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        SyncEntityKey.node("x" * 257)
    with pytest.raises(ValueError):
        SyncEntityKey.edge("x" * 257, "target", "REF")


def test_version_is_canonical_total_order() -> None:
    left = SyncVersion.create(NOW, "a", EVENT_A)
    right = SyncVersion.create(NOW, "b", EVENT_B)
    assert str(left).startswith("v1:")
    assert left < right
    assert left <= right and right > left and right >= left
    assert SyncVersion.parse(str(left)) == left
    with pytest.raises(ValueError):
        SyncVersion.parse("2026-08-11T00:00:00Z|a|" + EVENT_A)


def test_version_round_trips_origins_that_contain_the_old_delimiter() -> None:
    version = SyncVersion.create(NOW, "北京|node|✓", EVENT_A)
    assert SyncVersion.parse(str(version)) == version


def test_memory_mapping_only_admits_frozen_ordinary_node_types() -> None:
    assert sync_node_type_for_memory(Function(id="deploy")) is SyncNodeType.FUNCTION


def test_sync_scope_exact_schema_and_visibility_matrix() -> None:
    assert SyncScope.from_dict(USER_SCOPE.to_dict()) == USER_SCOPE
    assert SyncScope.from_dict(WORKSPACE_SCOPE.to_dict()) == WORKSPACE_SCOPE
    assert SyncScope.from_dict(SESSION_SCOPE.to_dict()) == SESSION_SCOPE
    for bad in (
        {**USER_SCOPE.to_dict(), "tenant_id": ""},
        {**USER_SCOPE.to_dict(), "owner_subject_id": ""},
        {**USER_SCOPE.to_dict(), "visibility": "future"},
        {**USER_SCOPE.to_dict(), "visibility": True},
        {**USER_SCOPE.to_dict(), "workspace_id": True},
        {**WORKSPACE_SCOPE.to_dict(), "workspace_id": None},
        {**SESSION_SCOPE.to_dict(), "workspace_id": None},
        {**SESSION_SCOPE.to_dict(), "agent_id": None},
        {**SESSION_SCOPE.to_dict(), "session_id": None},
        {**USER_SCOPE.to_dict(), "future": "field"},
    ):
        with pytest.raises((TypeError, ValueError)):
            SyncScope.from_dict(bad)

    source = SESSION_SCOPE.to_dict()
    frozen = SyncScope.from_dict(source)
    source["tenant_id"] = "mutated"
    detached = frozen.to_dict()
    detached["owner_subject_id"] = "mutated"
    assert frozen == SESSION_SCOPE


def test_batch_has_exact_schema_limits_stable_digest_and_duplicate_rejection() -> None:
    first = _event()
    second = _event(event_id=EVENT_B, entity_key=SyncEntityKey.node("other"))
    batch = SyncBatch(1, EVENT_A, "node-a", (first, second))
    from_wire = SyncBatch.from_dict(batch.to_dict())
    assert from_wire == batch
    assert batch.request_digest == from_wire.request_digest
    assert len(batch.request_digest) == 64

    with pytest.raises(SyncBatchRejected):
        SyncBatch(1, EVENT_A, "node-a", (first, first))
    with pytest.raises(SyncBatchRejected):
        SyncBatch(1, EVENT_A, "node-a", tuple(first for _ in range(1001)))
    with pytest.raises((TypeError, ValueError)):
        SyncBatch.from_dict({**batch.to_dict(), "future": True})
    with pytest.raises(SyncBatchRejected):
        SyncBatch(
            1, EVENT_A, "node-a",
            (first, _event(event_id=EVENT_B, scope=SyncScope("tenant-b", "bob", None, "user", None, None))),
        )


@pytest.mark.parametrize("bad_number", (float("nan"), float("inf"), float("-inf")))
def test_batch_canonical_json_rejects_nonfinite_numbers(bad_number: float) -> None:
    event = _event()
    object.__setattr__(event, "payload", {"bad": bad_number})
    with pytest.raises((TypeError, ValueError, SyncBatchRejected)):
        SyncBatch(1, EVENT_A, "node-a", (event,))


def test_tombstone_must_not_carry_a_payload() -> None:
    version = str(SyncVersion.create(NOW, "node-a", EVENT_A))
    with pytest.raises((TypeError, ValueError)):
        SyncEvent(1, EVENT_A, "node-a", SyncNodeType.FUNCTION, SyncEntityKey.node("deploy"), SyncOperation.TOMBSTONE, version, USER_SCOPE, {"id": "deploy"})
    event = SyncEvent(1, EVENT_A, "node-a", SyncNodeType.FUNCTION, SyncEntityKey.node("deploy"), SyncOperation.TOMBSTONE, version, USER_SCOPE, None)
    assert event.payload is None
    assert event.scope == USER_SCOPE


def test_event_payload_is_recursively_frozen_and_to_dict_is_detached() -> None:
    original = {"nested": {"items": ["one"]}}
    event = SyncEvent(
        1, EVENT_A, "node-a", SyncNodeType.FUNCTION, SyncEntityKey.node("deploy"),
        SyncOperation.UPSERT, str(SyncVersion.create(NOW, "node-a", EVENT_A)),
        WORKSPACE_SCOPE, original,
    )
    batch = SyncBatch(1, EVENT_A, "node-a", (event,))
    canonical_before, digest_before = batch.canonical_bytes, batch.request_digest
    original["nested"]["items"].append("caller-write")
    with pytest.raises(TypeError):
        event.payload["nested"] = {}  # type: ignore[index]
    with pytest.raises(AttributeError):
        event.payload["nested"]["items"].append("event-write")  # type: ignore[index,union-attr]
    thawed = event.to_dict()
    thawed["payload"]["nested"]["items"].append("wire-write")  # type: ignore[index]
    assert event.to_dict()["payload"] == {"nested": {"items": ["one"]}}
    assert event.to_dict()["scope"] == WORKSPACE_SCOPE.to_dict()
    assert (batch.canonical_bytes, batch.request_digest) == (canonical_before, digest_before)


def test_scope_is_in_jcs_digest_and_batch_4mib_capacity() -> None:
    scope_vector = {"scope": SESSION_SCOPE.to_dict()}
    assert _canonical_json_bytes(scope_vector) == (
        b'{"scope":{"agent_id":"agent-a","owner_subject_id":"alice",'
        b'"session_id":"session-a","tenant_id":"tenant-a","visibility":"session",'
        b'"workspace_id":"workspace-a"}}'
    )
    user_batch = SyncBatch(1, EVENT_A, "node-a", (_event(scope=USER_SCOPE),))
    workspace_batch = SyncBatch(1, EVENT_A, "node-a", (_event(scope=WORKSPACE_SCOPE),))
    assert user_batch.request_digest != workspace_batch.request_digest

    def sized_event(blob: str) -> SyncEvent:
        return SyncEvent(
            1, EVENT_A, "node-a", SyncNodeType.FUNCTION, SyncEntityKey.node("deploy"),
            SyncOperation.UPSERT, str(SyncVersion.create(NOW, "node-a", EVENT_A)),
            SESSION_SCOPE, {"blob": blob},
        )

    base = SyncBatch(1, EVENT_A, "node-a", (sized_event(""),))
    filler_size = _MAX_BATCH_BYTES - len(base.canonical_bytes)
    at_limit = SyncBatch(1, EVENT_A, "node-a", (sized_event("x" * filler_size),))
    assert len(at_limit.canonical_bytes) == _MAX_BATCH_BYTES
    assert at_limit.request_digest == hashlib.sha256(at_limit.canonical_bytes).hexdigest()
    with pytest.raises(SyncBatchRejected):
        SyncBatch(1, EVENT_A, "node-a", (sized_event("x" * (filler_size + 1)),))


def test_scope_jcs_vector_matches_node_json_stringify() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable for the cross-runtime JCS vector")
    expected = _canonical_json_bytes({"scope": SESSION_SCOPE.to_dict()})
    script = (
        'process.stdout.write(JSON.stringify({scope:{agent_id:"agent-a",'
        'owner_subject_id:"alice",session_id:"session-a",tenant_id:"tenant-a",'
        'visibility:"session",workspace_id:"workspace-a"}}))'
    )
    completed = subprocess.run(
        [node, "-e", script], check=True, capture_output=True, timeout=10,
    )
    assert completed.stdout == expected


def test_nul_is_rejected_across_json_payload_and_batch_paths() -> None:
    for value in (
        {"bad\x00key": "value"},
        {"key": "bad\x00value"},
        {"nested": [{"key": "bad\x00value"}]},
    ):
        with pytest.raises(ValueError, match="U\\+0000"):
            _canonical_json_bytes(value)
        with pytest.raises(ValueError, match="U\\+0000"):
            SyncEvent(
                1, EVENT_A, "node-a", SyncNodeType.FUNCTION,
                SyncEntityKey.node("deploy"), SyncOperation.UPSERT,
                str(SyncVersion.create(NOW, "node-a", EVENT_A)), USER_SCOPE, value,
            )

    bypassed = _event()
    object.__setattr__(bypassed, "payload", {"key": "bad\x00value"})
    with pytest.raises(ValueError, match="U\\+0000"):
        SyncBatch(1, EVENT_A, "node-a", (bypassed,))


def test_nul_and_lone_surrogates_are_rejected_in_protocol_identifiers() -> None:
    for field_name in (
        "tenant_id", "owner_subject_id", "workspace_id", "agent_id", "session_id",
    ):
        for invalid in ("bad\x00value", "bad\ud800value"):
            raw = SESSION_SCOPE.to_dict()
            raw[field_name] = invalid
            with pytest.raises(ValueError):
                SyncScope.from_dict(raw)

    for invalid in ("node\x00id", "node\ud800id"):
        with pytest.raises(ValueError):
            SyncEntityKey.node(invalid)
        with pytest.raises(ValueError):
            SyncEntityKey.edge(invalid, "target", "REFERENCES")
        with pytest.raises(ValueError):
            SyncVersion.create(NOW, invalid, EVENT_A)

    version = str(SyncVersion.create(NOW, "node-a", EVENT_A))
    with pytest.raises(ValueError, match="U\\+0000"):
        SyncEvent(
            1, EVENT_A, "node\x00a", SyncNodeType.FUNCTION,
            SyncEntityKey.node("deploy"), SyncOperation.UPSERT,
            version, USER_SCOPE, {"id": "deploy"},
        )
    with pytest.raises(ValueError, match="U\\+0000"):
        SyncBatch(1, EVENT_A, "node\x00a", (_event(),))

    with pytest.raises(ValueError):
        SyncCursorClaims(
            1, "active", "tenant\x00a", "remote-a", "consumer-a", 0, 0,
            None, None, NOW, NOW + timedelta(seconds=60),
        )


def test_non_nul_controls_line_separators_and_astral_unicode_remain_canonical() -> None:
    value = "control:\x01\n\t separators:\u2028\u2029 astral:😀"
    expected = _canonical_json_bytes({"value": value})
    node = shutil.which("node")
    if node is not None:
        script = (
            "process.stdout.write(JSON.stringify({value:"
            + json.dumps(value, ensure_ascii=False)
            + "}))"
        )
        completed = subprocess.run(
            [node, "-e", script], check=True, capture_output=True, timeout=10,
        )
        assert completed.stdout == expected
    entity_key = SyncEntityKey.node(value)
    assert SyncEntityKey.parse(str(entity_key)) == entity_key
    version = SyncVersion.create(NOW, value, EVENT_A)
    assert SyncVersion.parse(str(version)) == version


def test_jcs_subset_golden_vectors_are_cross_runtime_stable() -> None:
    vector = {
        "z": [-0.0, 1.0, 1e-7, 1e-6, 1e20, 1e21],
        "\U00010000": "a",
        "😀": "é",
        "\ue000": "b",
        "nested": {"b": 2, "a": 1},
    }
    assert _canonical_json_bytes(vector) == (
        '{"nested":{"a":1,"b":2},"z":[0,1,1e-7,0.000001,100000000000000000000,1e+21],'
        '"𐀀":"a","😀":"é","":"b"}'
    ).encode()


@pytest.mark.parametrize("value", (2**53, -(2**53)))
def test_jcs_rejects_integer_values_outside_javascript_safe_range(value: int) -> None:
    with pytest.raises(ValueError):
        _canonical_json_bytes({"number": value})
    event = _event()
    object.__setattr__(event, "payload", {"number": value})
    with pytest.raises((ValueError, SyncBatchRejected)):
        SyncBatch(1, EVENT_A, "node-a", (event,))


@pytest.mark.parametrize("value", (-(2**53 - 1), 2**53 - 1))
def test_jcs_keeps_javascript_safe_integer_edges(value: int) -> None:
    assert _canonical_json_bytes({"number": value}) == f'{{"number":{value}}}'.encode("ascii")


def test_data_result_types_have_exact_fields_and_reject_bool_as_int() -> None:
    receipt = SyncReceipt(event_id=EVENT_A, outcome="accepted")
    result = SyncBatchResult(
        batch_id=EVENT_A,
        request_digest="a" * 64,
        outcome="accepted",
        receipts=(receipt,),
    )
    assert result.to_dict()["receipts"] == [receipt.to_dict()]
    assert SyncApplyResult(1, 2, 3, 4).to_dict() == {
        "applied": 1,
        "duplicate": 2,
        "conflict": 3,
        "cursor_advanced": 4,
    }
    assert SyncStatus(1, 2, 3, 4, 5).to_dict()["dead_letters"] == 5
    assert SyncDrainResult(True, 1, 2, 3, 4, False).to_dict()["drained"] is True
    with pytest.raises(TypeError):
        SyncApplyResult(True, 0, 0, 0)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        SyncStatus(True, 0, 0, 0, 0)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        SyncReceipt(event_id=EVENT_A, outcome="applied")
    with pytest.raises(ValueError):
        SyncBatchResult(EVENT_A, "a" * 64, "rejected_conflict", (receipt,))


def test_cursor_codec_rotates_keys_and_hides_all_validation_causes() -> None:
    active = SyncCursorCodec("active", "a" * 32, {"previous": "b" * 32})
    claims = SyncCursorClaims(
        version=1,
        key_id="active",
        tenant_binding="tenant-a",
        remote_binding="remote-a",
        consumer_binding="consumer-a",
        after_seq=1,
        snapshot_seq=2,
        snapshot_id=None,
        snapshot_after=None,
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=60),
    )
    token = active.encode(claims)
    assert active.decode(token, tenant_binding="tenant-a", remote_binding="remote-a", consumer_binding="consumer-a", now=NOW) == claims

    previous = SyncCursorCodec("previous", "b" * 32)
    old_claims = SyncCursorClaims(
        version=1,
        key_id="previous",
        tenant_binding="tenant-a",
        remote_binding="remote-a",
        consumer_binding="consumer-a",
        after_seq=0,
        snapshot_seq=0,
        snapshot_id=None,
        snapshot_after=None,
        issued_at=NOW,
        expires_at=NOW + timedelta(seconds=60),
    )
    assert active.decode(previous.encode(old_claims), tenant_binding="tenant-a", remote_binding="remote-a", consumer_binding="consumer-a", now=NOW) == old_claims

    payload, signature = token.split(".")
    unknown = SyncCursorCodec("unknown", "c" * 32).encode(
        SyncCursorClaims(
            version=1, key_id="unknown", tenant_binding="tenant-a", remote_binding="remote-a",
            consumer_binding="consumer-a", after_seq=0, snapshot_seq=0, snapshot_id=None,
            snapshot_after=None,
            issued_at=NOW, expires_at=NOW + timedelta(seconds=60),
        )
    )
    for broken in (
        payload + "." + ("A" if signature[0] != "A" else "B") + signature[1:],
        unknown,
    ):
        with pytest.raises(SyncCursorExpired, match="^invalid_cursor$"):
            active.decode(broken, tenant_binding="tenant-a", remote_binding="remote-a", consumer_binding="consumer-a", now=NOW)
    with pytest.raises(SyncCursorExpired, match="^invalid_cursor$"):
        active.decode(token, tenant_binding="other", remote_binding="remote-a", consumer_binding="consumer-a", now=NOW)
    with pytest.raises(SyncCursorExpired, match="^invalid_cursor$"):
        active.decode(token, tenant_binding="tenant-a", remote_binding="remote-a", consumer_binding="consumer-a", now=NOW + timedelta(seconds=61))


def test_page_and_snapshot_anchor_close_sequence_gap_and_keyset_contracts() -> None:
    first = _event()
    second = _event(event_id=EVENT_B, entity_key=SyncEntityKey.node("other"))
    page = SyncPage(
        items=(SyncStreamItem(3, first), SyncStreamItem(8, second)),
        snapshot_seq=8,
        next_after_seq=8,
        has_more=False,
    )
    assert [item.stream_seq for item in page.items] == [3, 8]
    assert page.next_after_seq == 8
    anchor = SyncSnapshotAnchor(SyncNodeType.FUNCTION, SyncEntityKey.node("other"))
    snapshot_page = SyncSnapshotPage(
        events=(first, second),
        snapshot_id="snapshot-a",
        next_anchor=None,
        resume_seq=8,
        has_more=False,
    )
    assert snapshot_page.snapshot_id == "snapshot-a"
    assert snapshot_page.next_anchor is None
    with pytest.raises(ValueError):
        SyncPage((SyncStreamItem(8, first), SyncStreamItem(3, second)), 8, 8, False)
    with pytest.raises(ValueError):
        SyncSnapshotPage((second, first), "snapshot-a", anchor, 8, True)
    with pytest.raises((TypeError, ValueError)):
        SyncSnapshotPage((first,), "", None, 8, False)


def test_snapshot_cursor_binds_keyset_anchor_and_rejects_invalid_modes() -> None:
    anchor = SyncSnapshotAnchor(SyncNodeType.FUNCTION, SyncEntityKey.node("deploy"))
    claims = SyncCursorClaims(
        version=1, key_id="active", tenant_binding="tenant-a", remote_binding="remote-a",
        consumer_binding="consumer-a", after_seq=0, snapshot_seq=3, snapshot_id="snapshot-a",
        snapshot_after=anchor, issued_at=NOW, expires_at=NOW + timedelta(seconds=60),
    )
    codec = SyncCursorCodec("active", "a" * 32)
    assert codec.decode(codec.encode(claims), tenant_binding="tenant-a", remote_binding="remote-a", consumer_binding="consumer-a", now=NOW) == claims
    with pytest.raises(ValueError):
        SyncCursorClaims(1, "active", "tenant-a", "remote-a", "consumer-a", 0, 3, "snapshot-a", None, NOW, NOW + timedelta(seconds=60))
    with pytest.raises(ValueError):
        SyncCursorClaims(1, "active", "tenant-a", "remote-a", "consumer-a", 0, 3, None, anchor, NOW, NOW + timedelta(seconds=60))


def test_repository_protocol_is_complete_and_exceptions_are_distinct() -> None:
    expected = {
        "sync_page", "sync_create_snapshot", "sync_snapshot_page", "sync_apply_batch",
        "sync_apply_page", "sync_register_target", "sync_claim", "sync_ack", "sync_fail",
        "sync_replay_dead_letter", "sync_set_target_enabled", "sync_compact", "sync_status",
    }
    assert expected <= set(SyncRepository.__dict__)
    assert str(inspect.signature(SyncRepository.sync_claim)) == "(self, target_id: 'str', *, limit: 'int', lease_seconds: 'int') -> 'list[SyncDelivery]'"
    assert len({SyncBatchRejected, SyncCursorExpired, SyncDeliveryBusy}) == 3
