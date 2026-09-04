"""Lite implementation of the G004 durable sync repository.

The repository deliberately owns no files.  It mutates the ``sync`` subtree
of :class:`LiteMemoryStore` while the store's existing flock/journal critical
section owns the business state, changelog, and sync state as one pair.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)
import copy
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, cast

from memplex.sync_protocol import (
    SyncApplyResult,
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
    SyncStreamItem,
    SyncVersion,
)
from memplex.sync_repository import (
    AbstractSyncRepository,
    SyncBackpressureError,
    SyncCapturePolicy,
    SyncCursorExpired,
    SyncDeadLetterEntry,
    SyncDeliveryBusy,
    validate_incoming_page,
)

if TYPE_CHECKING:
    from memplex.models import GraphEdge, MemoryNode
    from memplex.storage.lite.store import LiteMemoryStore


class LiteSyncRepository(AbstractSyncRepository):
    """Operate the frozen v2 sync subtree through one Lite pair commit."""

    def __init__(
        self,
        store: LiteMemoryStore,
        *,
        capture_policy: SyncCapturePolicy,
        max_pending_events: int = 100000,
        max_attempts: int = 8,
        snapshot_ttl_seconds: int = 900,
        max_snapshot_items: int = 1000000,
        max_active_snapshots_per_tenant: int = 2,
        max_active_snapshots_per_remote: int = 1,
        consumer_ttl_seconds: int = 86400,
        retention_min_seconds: int = 86400,
    ) -> None:
        if type(capture_policy) is not SyncCapturePolicy:
            raise TypeError("capture_policy must be an exact SyncCapturePolicy")
        for name, value in (
            ("max_pending_events", max_pending_events),
            ("max_attempts", max_attempts),
            ("snapshot_ttl_seconds", snapshot_ttl_seconds),
            ("max_snapshot_items", max_snapshot_items),
            ("max_active_snapshots_per_tenant", max_active_snapshots_per_tenant),
            ("max_active_snapshots_per_remote", max_active_snapshots_per_remote),
            ("consumer_ttl_seconds", consumer_ttl_seconds),
            ("retention_min_seconds", retention_min_seconds),
        ):
            if type(value) is not int:
                raise TypeError(f"{name} must be an exact int")
            if value < 1:
                raise ValueError(f"{name} must be >= 1")
        self._store = store
        self._capture_policy = capture_policy
        self._local_node_id = capture_policy.local_node_id
        self._max_pending_events = max_pending_events
        self._max_attempts = max_attempts
        self._snapshot_ttl_seconds = snapshot_ttl_seconds
        self._max_snapshot_items = max_snapshot_items
        self._max_active_snapshots_per_tenant = max_active_snapshots_per_tenant
        self._max_active_snapshots_per_remote = max_active_snapshots_per_remote
        self._consumer_ttl_seconds = consumer_ttl_seconds
        self._retention_min_seconds = retention_min_seconds
        if self._capture_policy.mode == "required":
            self._assert_single_tenant_business_state()

    @staticmethod
    def _require_str(value: object, name: str) -> str:
        if type(value) is not str:
            raise TypeError(f"{name} must be an exact str")
        if not value:
            raise ValueError(f"{name} must be non-empty")
        return value

    @staticmethod
    def _require_int(value: object, name: str, *, minimum: int = 0) -> int:
        if type(value) is not int:
            raise TypeError(f"{name} must be an exact int")
        if value < minimum:
            raise ValueError(f"{name} must be >= {minimum}")
        return value

    @staticmethod
    def _require_datetime(value: object, name: str) -> datetime:
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise TypeError(f"{name} must be an aware datetime")
        return value.astimezone(UTC)

    @staticmethod
    def _time(value: datetime) -> str:
        return value.astimezone(UTC).isoformat()

    @staticmethod
    def _parse_time(value: str) -> datetime:
        return datetime.fromisoformat(value).astimezone(UTC)

    @property
    def _state(self) -> dict[str, Any]:
        return self._store._sync_state

    def _business_tenants(self) -> set[str]:
        tenants: set[str] = set()
        nodes = (
            *self._store._functions.values(),
            *self._store._facts.values(),
            *self._store._preferences.values(),
            *self._store._observations,
        )
        for node in nodes:
            tenant_id = node.tenant_id
            if type(tenant_id) is not str or not tenant_id:
                raise ValueError("sync-enabled Lite requires tenant-bound nodes")
            tenants.add(tenant_id)
        return tenants

    def _assert_single_tenant_business_state(self) -> str | None:
        tenants = self._business_tenants()
        binding = self._state["tenant_binding"]
        if len(tenants) > 1 or (tenants and binding not in {None, next(iter(tenants))}):
            raise ValueError("sync-enabled Lite supports a single tenant")
        return next(iter(tenants), binding)

    def _bind_tenant(self, tenant_id: str) -> None:
        tenant_id = self._require_str(tenant_id, "tenant_id")
        current = self._state["tenant_binding"]
        if current is None:
            self._state["tenant_binding"] = tenant_id
        elif current != tenant_id:
            raise ValueError("sync-enabled Lite supports a single tenant")

    @contextmanager
    def _mutation(self) -> Iterator[None]:
        with self._store._durability.writer_lock():
            self._store._reload_for_mutation()
            try:
                yield
                self._store._commit_current_state()
            except BaseException:
                try:
                    self._store._reload_for_mutation(force=True)
                except BaseException as exc:  # noqa: BLE001 - shutdown/cleanup semantics; primary error stays authoritative
                    logger.debug("suppressed BaseException in cleanup/degradation path: %s", exc)
                raise

    @contextmanager
    def _read(self) -> Iterator[None]:
        with self._store._durability.writer_lock():
            self._store._refresh_for_read()
            yield

    @staticmethod
    def _event_to_raw(stream_seq: int, event: SyncEvent) -> dict[str, Any]:
        version = SyncVersion.parse(event.version)
        payload = event.to_dict()["payload"]
        return {
            "stream_seq": stream_seq,
            "event_id": event.event_id,
            "origin_node_id": event.origin_node_id,
            "node_type": event.node_type.value,
            "entity_key": str(event.entity_key),
            "operation": event.operation.value,
            "version_key": event.version,
            "payload": None if payload is None else payload,
            "tenant_id": event.scope.tenant_id,
            "visibility": event.scope.visibility,
            "owner_subject_id": event.scope.owner_subject_id,
            "workspace_id": event.scope.workspace_id,
            "agent_id": event.scope.agent_id,
            "session_id": event.scope.session_id,
            "created_at": version.occurred_at.isoformat(),
        }

    @staticmethod
    def _raw_to_event(raw: dict[str, Any]) -> SyncEvent:
        return SyncEvent(
            1,
            raw["event_id"],
            raw["origin_node_id"],
            SyncNodeType(raw["node_type"]),
            SyncEntityKey.parse(raw["entity_key"]),
            SyncOperation(raw["operation"]),
            raw["version_key"],
            SyncScope(
                raw["tenant_id"],
                raw["owner_subject_id"],
                raw["workspace_id"],
                raw["visibility"],
                raw["agent_id"],
                raw["session_id"],
            ),
            None if raw["payload"] is None else copy.deepcopy(raw["payload"]),
        )

    def _outbox_by_seq(self) -> dict[int, dict[str, Any]]:
        return {item["stream_seq"]: item for item in self._state["outbox"]}

    @staticmethod
    def _result_from_raw(value: dict[str, Any]) -> SyncBatchResult:
        return SyncBatchResult(
            value["batch_id"],
            value["request_digest"],
            value["outcome"],
            tuple(
                SyncReceipt(item["event_id"], item["outcome"])
                for item in value["receipts"]
            ),
        )

    def _assert_quota(self, additional_deliveries: int) -> None:
        active = sum(
            item["state"] in {"pending", "leased", "dead_letter"}
            for item in self._state["deliveries"]
        )
        if active + additional_deliveries > self._max_pending_events:
            raise SyncBackpressureError("sync pending delivery quota exceeded")

    def _append_event(self, event: SyncEvent) -> int:
        if type(event) is not SyncEvent:
            raise TypeError("event must be an exact SyncEvent")
        self._bind_tenant(event.scope.tenant_id)
        if any(item["event_id"] == event.event_id for item in self._state["outbox"]):
            raise ValueError("event_id already exists")
        enabled_targets = [
            target
            for target in self._state["targets"]
            if target["enabled"] and target["remote_node_id"] != event.origin_node_id
        ]
        self._assert_quota(len(enabled_targets))
        stream_seq = self._state["next_stream_seq"]
        self._state["next_stream_seq"] = stream_seq + 1
        self._state["outbox"].append(self._event_to_raw(stream_seq, event))
        version_raw = {
            "node_type": event.node_type.value,
            "entity_key": str(event.entity_key),
            "version_key": event.version,
            "deleted": event.operation is SyncOperation.TOMBSTONE,
            "event_id": event.event_id,
            "last_stream_seq": stream_seq,
        }
        key = (event.node_type.value, str(event.entity_key))
        self._state["entity_versions"] = [
            item
            for item in self._state["entity_versions"]
            if (item["node_type"], item["entity_key"]) != key
        ]
        self._state["entity_versions"].append(version_raw)
        now = SyncVersion.parse(event.version).occurred_at
        for target in enabled_targets:
            self._state["deliveries"].append(
                {
                    "target_id": target["target_id"],
                    "stream_seq": stream_seq,
                    "state": "pending",
                    "attempt_count": 0,
                    "next_attempt_at": self._time(now),
                    "lease_owner": None,
                    "lease_until": None,
                    "last_error_code": None,
                }
            )
        return stream_seq

    def capture_local_event(self, event: SyncEvent) -> int | None:
        """Capture a prevalidated local business event in the current mutation."""
        if self._capture_policy.mode == "off":
            return None
        if event.origin_node_id != self._local_node_id:
            raise ValueError("local capture origin does not match configured node")
        return self._append_event(event)

    @staticmethod
    def _scope_from_node(node: MemoryNode) -> SyncScope:
        tenant_id = node.tenant_id
        owner_subject_id = node.owner_subject_id
        visibility = node.visibility
        if type(tenant_id) is not str or not tenant_id:
            raise ValueError("sync capture requires node tenant_id")
        if type(owner_subject_id) is not str or not owner_subject_id:
            raise ValueError("sync capture requires node owner_subject_id")
        if type(visibility) is not str or not visibility:
            raise ValueError("sync capture requires node visibility")
        return SyncScope(
            tenant_id,
            owner_subject_id,
            node.workspace_id,
            visibility,
            node.provenance.get("agent_id") or None,
            node.provenance.get("session_id") or node.origin_session,
        )

    def capture_node(
        self,
        node: MemoryNode,
        *,
        node_type: SyncNodeType,
        operation: SyncOperation,
    ) -> int | None:
        if self._capture_policy.mode == "off":
            return None
        event_id = str(uuid.uuid4())
        occurred_at = datetime.now(UTC)
        event = SyncEvent(
            1,
            event_id,
            self._local_node_id,
            node_type,
            SyncEntityKey.node(node.id),
            operation,
            str(SyncVersion.create(occurred_at, self._local_node_id, event_id)),
            self._scope_from_node(node),
            None if operation is SyncOperation.TOMBSTONE else node.to_dict(),
        )
        return self.capture_local_event(event)

    def capture_edge(
        self,
        edge: GraphEdge,
        *,
        scope_node: MemoryNode,
        operation: SyncOperation,
    ) -> int | None:
        if self._capture_policy.mode == "off":
            return None
        event_id = str(uuid.uuid4())
        occurred_at = datetime.now(UTC)
        created_at = edge.created_at
        if not isinstance(created_at, datetime):
            created_at = occurred_at
        created_at = created_at.astimezone(UTC)
        payload = None
        if operation is SyncOperation.UPSERT:
            payload = {
                "weight": float(edge.weight),
                "evidence": list(edge.evidence),
                "created_at": created_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            }
        event = SyncEvent(
            1,
            event_id,
            self._local_node_id,
            SyncNodeType.EDGE,
            SyncEntityKey.edge(edge.source, edge.target, edge.edge_type),
            operation,
            str(SyncVersion.create(occurred_at, self._local_node_id, event_id)),
            self._scope_from_node(scope_node),
            payload,
        )
        return self.capture_local_event(event)

    def sync_page(
        self,
        remote_id: str,
        consumer_id: str,
        cursor: SyncCursorClaims | None,
        limit: int,
    ) -> SyncPage:
        remote_id = self._require_str(remote_id, "remote_id")
        consumer_id = self._require_str(consumer_id, "consumer_id")
        limit = self._require_int(limit, "limit", minimum=1)
        if cursor is not None and type(cursor) is not SyncCursorClaims:
            raise TypeError("cursor must be SyncCursorClaims or None")
        with self._mutation():
            floor = self._state["retention_floor"]
            cursor_row = next(
                (
                    item
                    for item in self._state["cursors"]
                    if item["remote_id"] == remote_id
                    and item["consumer_id"] == consumer_id
                ),
                None,
            )
            after = (
                (0 if cursor_row is None else cursor_row["after_seq"])
                if cursor is None
                else cursor.after_seq
            )
            if after < floor:
                raise SyncCursorExpired("cursor_expired")
            snapshot = (
                max((item["stream_seq"] for item in self._state["outbox"]), default=after)
                if cursor is None or cursor.after_seq == cursor.snapshot_seq
                else cursor.snapshot_seq
            )
            if cursor is not None and (
                cursor.tenant_binding != self._state["tenant_binding"]
                or cursor.remote_binding != remote_id
                or cursor.consumer_binding != consumer_id
                or cursor.snapshot_id is not None
                or datetime.now(UTC) >= cursor.expires_at
            ):
                raise SyncCursorExpired("invalid_cursor")
            if cursor is not None:
                outbox_by_seq = self._outbox_by_seq()
                for delivery in self._state["deliveries"]:
                    if (
                        delivery["target_id"] == remote_id
                        and delivery["state"] in {"pending", "leased"}
                        and delivery["stream_seq"] <= after
                    ):
                        raw_event = outbox_by_seq.get(delivery["stream_seq"])
                        if (
                            raw_event is not None
                            and raw_event["origin_node_id"] != remote_id
                        ):
                            delivery.update(
                                state="delivered",
                                lease_owner=None,
                                lease_until=None,
                                last_error_code=None,
                            )
            confirmed = after
            if cursor_row is None:
                self._state["cursors"].append(
                    {
                        "remote_id": remote_id,
                        "consumer_id": consumer_id,
                        "after_seq": confirmed,
                        "updated_at": self._time(datetime.now(UTC)),
                    }
                )
            else:
                cursor_row["after_seq"] = max(cursor_row["after_seq"], confirmed)
                cursor_row["updated_at"] = self._time(datetime.now(UTC))
            rows = [
                item
                for item in self._state["outbox"]
                if after < item["stream_seq"] <= snapshot
                and item["origin_node_id"] != remote_id
            ]
            selected = rows[:limit]
            has_more = len(rows) > limit
            items = tuple(
                SyncStreamItem(item["stream_seq"], self._raw_to_event(item))
                for item in selected
            )
            next_after = items[-1].stream_seq if has_more else snapshot
            return SyncPage(items, snapshot, next_after, has_more)

    def sync_register_target(self, target_id: str, *, bootstrap: str = "future") -> None:
        target_id = self._require_str(target_id, "target_id")
        bootstrap = self._require_str(bootstrap, "bootstrap")
        if bootstrap not in {"future", "retained"}:
            raise ValueError("bootstrap must be 'future' or 'retained'")
        if target_id == self._local_node_id:
            raise ValueError("target_id must not target this node")
        with self._mutation():
            if any(
                item["target_id"] == target_id
                for item in self._state["targets"]
            ):
                return
            bootstrap_seq = (
                self._state["next_stream_seq"] - 1
                if bootstrap == "future"
                else self._state["retention_floor"]
            )
            retained = [
                item
                for item in self._state["outbox"]
                if bootstrap == "retained"
                and item["stream_seq"] >= bootstrap_seq
                and item["origin_node_id"] != target_id
            ]
            self._assert_quota(len(retained))
            self._state["targets"].append(
                {
                    "target_id": target_id,
                    "remote_node_id": target_id,
                    "bootstrap_seq": bootstrap_seq,
                    "enabled": True,
                }
            )
            now = datetime.now(UTC).isoformat()
            for item in retained:
                self._state["deliveries"].append(
                    {
                        "target_id": target_id,
                        "stream_seq": item["stream_seq"],
                        "state": "pending",
                        "attempt_count": 0,
                        "next_attempt_at": now,
                        "lease_owner": None,
                        "lease_until": None,
                        "last_error_code": None,
                    }
                )

    def sync_claim(
        self,
        target_id: str,
        *,
        limit: int,
        lease_seconds: int,
    ) -> list[SyncDelivery]:
        target_id = self._require_str(target_id, "target_id")
        limit = self._require_int(limit, "limit", minimum=1)
        lease_seconds = self._require_int(lease_seconds, "lease_seconds", minimum=1)
        now = datetime.now(UTC)
        with self._mutation():
            target = next(
                (item for item in self._state["targets"] if item["target_id"] == target_id),
                None,
            )
            if target is None or not target["enabled"]:
                return []
            for item in self._state["deliveries"]:
                if (
                    item["target_id"] == target_id
                    and item["state"] == "leased"
                    and self._parse_time(item["lease_until"]) <= now
                ):
                    item.update(
                        state=(
                            "dead_letter"
                            if item["attempt_count"] >= self._max_attempts
                            else "pending"
                        ),
                        lease_owner=None,
                        lease_until=None,
                        last_error_code=(
                            "lease_expired"
                            if item["attempt_count"] >= self._max_attempts
                            else None
                        ),
                    )
            outbox = self._outbox_by_seq()
            candidates = sorted(
                (
                    item
                    for item in self._state["deliveries"]
                    if item["target_id"] == target_id
                    and item["state"] == "pending"
                    and item["attempt_count"] < self._max_attempts
                    and self._parse_time(item["next_attempt_at"]) <= now
                    and outbox[item["stream_seq"]]["origin_node_id"]
                    == self._local_node_id
                ),
                key=lambda item: item["stream_seq"],
            )[:limit]
            result: list[SyncDelivery] = []
            for item in candidates:
                lease_id = str(uuid.uuid4())
                lease_until = now + timedelta(seconds=lease_seconds)
                item["state"] = "leased"
                item["attempt_count"] += 1
                item["lease_owner"] = lease_id
                item["lease_until"] = self._time(lease_until)
                item["last_error_code"] = None
                result.append(
                    SyncDelivery(
                        target_id,
                        self._raw_to_event(outbox[item["stream_seq"]]),
                        item["attempt_count"],
                        lease_id,
                        lease_until,
                    )
                )
            return result

    def _delivery_row(self, delivery: SyncDelivery) -> dict[str, Any] | None:
        outbox = self._outbox_by_seq()
        for item in self._state["deliveries"]:
            raw = outbox.get(item["stream_seq"])
            if (
                item["target_id"] == delivery.target_id
                and raw is not None
                and raw["event_id"] == delivery.event.event_id
            ):
                return item
        return None

    def sync_ack(self, delivery: SyncDelivery, receipt: SyncReceipt) -> None:
        self.sync_ack_batch([delivery], (receipt,))

    def sync_ack_batch(
        self,
        deliveries: list[SyncDelivery],
        receipts: tuple[SyncReceipt, ...],
    ) -> None:
        if type(deliveries) is not list or not all(
            type(item) is SyncDelivery for item in deliveries
        ):
            raise TypeError("deliveries must be a list of exact SyncDelivery")
        if type(receipts) is not tuple or not all(
            type(item) is SyncReceipt for item in receipts
        ):
            raise TypeError("receipts must be a tuple of exact SyncReceipt")
        if len(deliveries) != len(receipts):
            raise ValueError("delivery and receipt cardinality mismatch")
        receipt_ids = {item.event_id for item in receipts}
        delivery_ids = {item.event.event_id for item in deliveries}
        if len(receipt_ids) != len(receipts) or receipt_ids != delivery_ids:
            raise ValueError("delivery and receipt identity mismatch")
        with self._mutation():
            rows: list[dict[str, Any] | None] = []
            for delivery in deliveries:
                row = self._delivery_row(delivery)
                if row is not None and row["state"] != "delivered":
                    self._require_active_lease(row, delivery)
                rows.append(row)
            for row in rows:
                if row is None or row["state"] == "delivered":
                    continue
                row.update(
                    state="delivered",
                    lease_owner=None,
                    lease_until=None,
                    last_error_code=None,
                )

    def _require_active_lease(self, row: dict[str, Any], delivery: SyncDelivery) -> None:
        if row["state"] != "leased" or row["lease_owner"] != delivery.lease_id:
            raise SyncDeliveryBusy("delivery lease is no longer active")
        if self._parse_time(row["lease_until"]) <= datetime.now(UTC):
            raise SyncDeliveryBusy("delivery lease is no longer active")

    def sync_fail(self, delivery: SyncDelivery, error_code: str, now: datetime) -> None:
        if type(delivery) is not SyncDelivery:
            raise TypeError("delivery must be an exact SyncDelivery")
        error_code = self._require_str(error_code, "error_code")
        now = self._require_datetime(now, "now")
        with self._mutation():
            row = self._delivery_row(delivery)
            if row is None:
                return
            self._require_active_lease(row, delivery)
            if row["attempt_count"] >= self._max_attempts:
                row.update(
                    state="dead_letter",
                    lease_owner=None,
                    lease_until=None,
                    next_attempt_at=self._time(now),
                    last_error_code=error_code,
                )
            else:
                delay = min(60, 2 ** max(row["attempt_count"] - 1, 0))
                row.update(
                    state="pending",
                    lease_owner=None,
                    lease_until=None,
                    next_attempt_at=self._time(now + timedelta(seconds=delay)),
                    last_error_code=error_code,
                )

    def sync_dead_letter(
        self, delivery: SyncDelivery, error_code: str, now: datetime
    ) -> None:
        if type(delivery) is not SyncDelivery:
            raise TypeError("delivery must be an exact SyncDelivery")
        error_code = self._require_str(error_code, "error_code")
        now = self._require_datetime(now, "now")
        with self._mutation():
            row = self._delivery_row(delivery)
            if row is None:
                return
            self._require_active_lease(row, delivery)
            row.update(
                state="dead_letter",
                lease_owner=None,
                lease_until=None,
                next_attempt_at=self._time(now),
                last_error_code=error_code,
            )

    def sync_replay_dead_letter(self, target_id: str, event_id: str) -> bool:
        target_id = self._require_str(target_id, "target_id")
        event_id = self._require_str(event_id, "event_id")
        with self._mutation():
            outbox = self._outbox_by_seq()
            for item in self._state["deliveries"]:
                raw = outbox.get(item["stream_seq"])
                if (
                    item["target_id"] == target_id
                    and item["state"] == "dead_letter"
                    and raw is not None
                    and raw["event_id"] == event_id
                ):
                    item.update(
                        state="pending",
                        attempt_count=0,
                        lease_owner=None,
                        lease_until=None,
                        next_attempt_at=self._time(datetime.now(UTC)),
                        last_error_code=None,
                    )
                    return True
            return False

    def sync_list_dead_letters(self, *, limit: int) -> list[SyncDeadLetterEntry]:
        limit = self._require_int(limit, "limit", minimum=1)
        with self._read():
            outbox = self._outbox_by_seq()
            rows = sorted(
                (
                    item
                    for item in self._state["deliveries"]
                    if item["state"] == "dead_letter"
                ),
                key=lambda item: (item["target_id"], item["stream_seq"]),
            )[:limit]
            return [
                SyncDeadLetterEntry(
                    item["target_id"],
                    outbox[item["stream_seq"]]["event_id"],
                    item["attempt_count"],
                    item["last_error_code"] or "delivery_failed",
                )
                for item in rows
            ]

    def sync_set_target_enabled(self, target_id: str, enabled: bool) -> None:
        target_id = self._require_str(target_id, "target_id")
        if type(enabled) is not bool:
            raise TypeError("enabled must be an exact bool")
        with self._mutation():
            target = next(
                (item for item in self._state["targets"] if item["target_id"] == target_id),
                None,
            )
            if target is None:
                raise ValueError("target not found")
            target["enabled"] = enabled

    def sync_status(self) -> SyncStatus:
        with self._read():
            counts = {
                state: sum(item["state"] == state for item in self._state["deliveries"])
                for state in ("pending", "leased", "delivered", "dead_letter")
            }
            return SyncStatus(
                counts["pending"],
                counts["leased"],
                counts["delivered"],
                sum(not item["enabled"] for item in self._state["targets"]),
                counts["dead_letter"],
            )

    def sync_dispatch_status(self) -> SyncStatus:
        with self._read():
            outbox = self._outbox_by_seq()
            rows = [
                item
                for item in self._state["deliveries"]
                if outbox[item["stream_seq"]]["origin_node_id"]
                == self._local_node_id
            ]
            counts = {
                state: sum(item["state"] == state for item in rows)
                for state in ("pending", "leased", "delivered", "dead_letter")
            }
            return SyncStatus(
                counts["pending"],
                counts["leased"],
                counts["delivered"],
                sum(not item["enabled"] for item in self._state["targets"]),
                counts["dead_letter"],
            )

    def _cleanup_expired_snapshots(self, now: datetime) -> None:
        expired = {
            item["snapshot_id"]
            for item in self._state["snapshots"]
            if self._parse_time(item["expires_at"]) <= now
        }
        if not expired:
            return
        self._state["snapshots"] = [
            item for item in self._state["snapshots"] if item["snapshot_id"] not in expired
        ]
        self._state["snapshot_items"] = [
            item
            for item in self._state["snapshot_items"]
            if item["snapshot_id"] not in expired
        ]

    def _snapshot_event_for_node(
        self,
        node: MemoryNode,
        node_type: SyncNodeType,
    ) -> SyncEvent:
        entity_key = SyncEntityKey.node(node.id)
        version_row = next(
            (
                item
                for item in self._state["entity_versions"]
                if item["node_type"] == node_type.value
                and item["entity_key"] == str(entity_key)
            ),
            None,
        )
        if version_row is None:
            event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"memplex:{node_type.value}:{node.id}"))
            occurred_at = datetime.fromisoformat(cast(str, node.updated_at)).astimezone(UTC)
            version = str(SyncVersion.create(occurred_at, self._local_node_id, event_id))
        else:
            event_id = version_row["event_id"]
            version = version_row["version_key"]
        origin_node_id = SyncVersion.parse(version).origin_node_id
        return SyncEvent(
            1,
            event_id,
            origin_node_id,
            node_type,
            entity_key,
            SyncOperation.UPSERT,
            version,
            self._scope_from_node(node),
            node.to_dict(),
        )

    def _current_snapshot_events(self) -> list[SyncEvent]:
        tenant_id = self._assert_single_tenant_business_state()
        if tenant_id is not None:
            self._bind_tenant(tenant_id)
        events: list[SyncEvent] = []
        for node_type, values in (
            (SyncNodeType.FUNCTION, self._store._functions.values()),
            (SyncNodeType.FACT, self._store._facts.values()),
            (SyncNodeType.PREFERENCE, self._store._preferences.values()),
            (SyncNodeType.OBSERVATION, self._store._observations),
        ):
            events.extend(self._snapshot_event_for_node(node, node_type) for node in values)
        for edge in self._store._edges:
            source = self._store._functions.get(edge.source)
            if source is None:
                raise ValueError("snapshot edge source is missing")
            entity_key = SyncEntityKey.edge(edge.source, edge.target, edge.edge_type)
            version_row = next(
                (
                    item
                    for item in self._state["entity_versions"]
                    if item["node_type"] == SyncNodeType.EDGE.value
                    and item["entity_key"] == str(entity_key)
                ),
                None,
            )
            if version_row is None:
                event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"memplex:edge:{entity_key}"))
                occurred_at = edge.created_at or datetime.now(UTC)
                version = str(
                    SyncVersion.create(
                        occurred_at.astimezone(UTC), self._local_node_id, event_id
                    )
                )
            else:
                event_id = version_row["event_id"]
                version = version_row["version_key"]
            origin_node_id = SyncVersion.parse(version).origin_node_id
            created_at = edge.created_at or SyncVersion.parse(version).occurred_at
            events.append(
                SyncEvent(
                    1,
                    event_id,
                    origin_node_id,
                    SyncNodeType.EDGE,
                    entity_key,
                    SyncOperation.UPSERT,
                    version,
                    self._scope_from_node(source),
                    {
                        "weight": float(edge.weight),
                        "evidence": list(edge.evidence),
                        "created_at": created_at.astimezone(UTC).strftime(
                            "%Y-%m-%dT%H:%M:%S.%fZ"
                        ),
                    },
                )
            )
        return sorted(events, key=lambda event: SyncSnapshotAnchor.from_event(event))

    @staticmethod
    def _snapshot_page(
        snapshot_id: str,
        resume_seq: int,
        events: list[SyncEvent],
        *,
        limit: int,
    ) -> SyncSnapshotPage:
        selected = events[:limit]
        has_more = len(events) > limit
        next_anchor = (
            SyncSnapshotAnchor.from_event(selected[-1]) if has_more and selected else None
        )
        return SyncSnapshotPage(
            tuple(selected), snapshot_id, next_anchor, resume_seq, has_more
        )

    def sync_create_snapshot(
        self,
        remote_id: str,
        consumer_id: str,
        request_id: str,
        limit: int,
    ) -> SyncSnapshotPage:
        remote_id = self._require_str(remote_id, "remote_id")
        consumer_id = self._require_str(consumer_id, "consumer_id")
        request_id = self._require_str(request_id, "request_id")
        limit = self._require_int(limit, "limit", minimum=1)
        now = datetime.now(UTC)
        with self._mutation():
            self._cleanup_expired_snapshots(now)
            snapshot = next(
                (
                    item
                    for item in self._state["snapshots"]
                    if item["remote_id"] == remote_id
                    and item["consumer_id"] == consumer_id
                    and item["request_id"] == request_id
                ),
                None,
            )
            if snapshot is None:
                active = sum(
                    item["remote_id"] == remote_id for item in self._state["snapshots"]
                )
                if active >= self._max_active_snapshots_per_remote:
                    raise SyncBackpressureError("snapshot_in_progress")
                if len(self._state["snapshots"]) >= self._max_active_snapshots_per_tenant:
                    raise SyncBackpressureError("snapshot_in_progress")
                events = self._current_snapshot_events()
                if len(events) > self._max_snapshot_items:
                    raise SyncBackpressureError("snapshot_too_large")
                snapshot_id = str(uuid.uuid4())
                resume_seq = self._state["next_stream_seq"] - 1
                snapshot = {
                    "snapshot_id": snapshot_id,
                    "remote_id": remote_id,
                    "consumer_id": consumer_id,
                    "request_id": request_id,
                    "resume_seq": resume_seq,
                    "expires_at": self._time(
                        now + timedelta(seconds=self._snapshot_ttl_seconds)
                    ),
                }
                self._state["snapshots"].append(snapshot)
                self._state["snapshot_items"].extend(
                    {
                        "snapshot_id": snapshot_id,
                        "node_type": event.node_type.value,
                        "entity_key": str(event.entity_key),
                        "event": event.to_dict(),
                    }
                    for event in events
                )
            rows = sorted(
                (
                    item
                    for item in self._state["snapshot_items"]
                    if item["snapshot_id"] == snapshot["snapshot_id"]
                ),
                key=lambda item: (item["node_type"], item["entity_key"]),
            )
            events = [SyncEvent.from_dict(item["event"]) for item in rows]
            return self._snapshot_page(
                snapshot["snapshot_id"], snapshot["resume_seq"], events, limit=limit
            )

    def sync_snapshot_page(
        self,
        remote_id: str,
        consumer_id: str,
        cursor: SyncCursorClaims,
        limit: int,
    ) -> SyncSnapshotPage:
        remote_id = self._require_str(remote_id, "remote_id")
        consumer_id = self._require_str(consumer_id, "consumer_id")
        if type(cursor) is not SyncCursorClaims or cursor.snapshot_id is None:
            raise TypeError("cursor must be a snapshot SyncCursorClaims")
        limit = self._require_int(limit, "limit", minimum=1)
        now = datetime.now(UTC)
        expired_snapshot = False
        page: SyncSnapshotPage | None = None
        with self._mutation():
            self._cleanup_expired_snapshots(now)
            if (
                cursor.tenant_binding != self._state["tenant_binding"]
                or cursor.remote_binding != remote_id
                or cursor.consumer_binding != consumer_id
                or now >= cursor.expires_at
            ):
                raise SyncCursorExpired("invalid_cursor")
            snapshot = next(
                (
                    item
                    for item in self._state["snapshots"]
                    if item["snapshot_id"] == cursor.snapshot_id
                ),
                None,
            )
            if snapshot is None:
                expired_snapshot = True
            else:
                if (
                    snapshot["remote_id"] != remote_id
                    or snapshot["consumer_id"] != consumer_id
                    or snapshot["resume_seq"] != cursor.snapshot_seq
                ):
                    raise SyncCursorExpired("invalid_cursor")
                rows = sorted(
                    (
                        item
                        for item in self._state["snapshot_items"]
                        if item["snapshot_id"] == cursor.snapshot_id
                    ),
                    key=lambda item: (item["node_type"], item["entity_key"]),
                )
                if cursor.snapshot_after is not None:
                    anchor = (
                        cursor.snapshot_after.node_type.value,
                        str(cursor.snapshot_after.entity_key),
                    )
                    if not any(
                        (item["node_type"], item["entity_key"]) == anchor
                        for item in rows
                    ):
                        raise SyncCursorExpired("invalid_cursor")
                    rows = [
                        item
                        for item in rows
                        if (item["node_type"], item["entity_key"]) > anchor
                    ]
                events = [SyncEvent.from_dict(item["event"]) for item in rows]
                page = self._snapshot_page(
                    cursor.snapshot_id, snapshot["resume_seq"], events, limit=limit
                )
        if expired_snapshot:
            raise SyncCursorExpired("snapshot_expired")
        if page is None:  # pragma: no cover - exhaustive state guard
            raise RuntimeError("snapshot page was not produced")
        return page

    def _entity_version(self, event: SyncEvent) -> dict[str, Any] | None:
        return next(
            (
                item
                for item in self._state["entity_versions"]
                if item["node_type"] == event.node_type.value
                and item["entity_key"] == str(event.entity_key)
            ),
            None,
        )

    @staticmethod
    def _edge_payload(event: SyncEvent) -> tuple[float, list[str], datetime]:
        payload = event.to_dict()["payload"]
        if type(payload) is not dict or set(payload) != {
            "weight",
            "evidence",
            "created_at",
        }:
            raise ValueError("edge payload fields are invalid")
        weight = payload["weight"]
        if type(weight) not in {int, float}:
            raise TypeError("edge weight must be numeric")
        evidence = payload["evidence"]
        if type(evidence) is not list or not all(type(item) is str for item in evidence):
            raise TypeError("edge evidence must be string list")
        created_at = payload["created_at"]
        if type(created_at) is not str:
            raise TypeError("edge created_at must be a string")
        parsed = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=UTC
        )
        if parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ") != created_at:
            raise ValueError("edge created_at is not canonical")
        return float(weight), evidence, parsed

    def _apply_business_event(
        self,
        event: SyncEvent,
        *,
        applied_edge_tombstones: set[str],
    ) -> None:
        from memplex.models import Fact, Function, GraphEdge, Observation, Preference

        if event.node_type is SyncNodeType.EDGE:
            parts = event.entity_key.edge_parts
            if parts is None:
                raise ValueError("edge event requires edge entity key")
            source, target, edge_type = parts
            if event.operation is SyncOperation.TOMBSTONE:
                self._store._edges = [
                    edge
                    for edge in self._store._edges
                    if (edge.source, edge.target, edge.edge_type) != parts
                ]
                self._store._rebuild_edge_index()
                applied_edge_tombstones.add(str(event.entity_key))
                return
            weight, evidence, created_at = self._edge_payload(event)
            if source not in self._store._functions:
                raise ValueError("edge source must exist")
            self._store._edges = [
                edge
                for edge in self._store._edges
                if (edge.source, edge.target, edge.edge_type) != parts
            ]
            edge = GraphEdge(source, target, edge_type, weight, evidence, created_at)
            from memplex.models import validate_belongs_to_edges

            if target not in self._store._functions and edge_type != "BELONGS_TO":
                raise ValueError("edge target must exist")
            validate_belongs_to_edges(self._store._functions.values(), [edge])
            self._store._edges.append(edge)
            self._store._rebuild_edge_index()
            return

        node_id = event.entity_key.node_id
        if node_id is None:
            raise ValueError("node event requires node entity key")
        collection: Any
        if event.node_type is SyncNodeType.FUNCTION:
            collection = self._store._functions
        elif event.node_type is SyncNodeType.FACT:
            collection = self._store._facts
        elif event.node_type is SyncNodeType.PREFERENCE:
            collection = self._store._preferences
        else:
            collection = self._store._observations

        if event.operation is SyncOperation.TOMBSTONE:
            if event.node_type is SyncNodeType.FUNCTION:
                for edge in self._store._edges:
                    if edge.source == node_id or edge.target == node_id:
                        key = str(SyncEntityKey.edge(edge.source, edge.target, edge.edge_type))
                        if key not in applied_edge_tombstones:
                            raise ValueError(
                                "function tombstone requires explicit edge tombstones"
                            )
                collection.pop(node_id, None)
                self._store._name_index = {
                    name: identifier
                    for name, identifier in self._store._name_index.items()
                    if identifier != node_id
                }
            elif event.node_type is SyncNodeType.OBSERVATION:
                self._store._observations = [
                    item for item in self._store._observations if item.id != node_id
                ]
            else:
                collection.pop(node_id, None)
            return

        payload = event.to_dict()["payload"]
        if type(payload) is not dict or payload.get("id") != node_id:
            raise ValueError("node payload identity mismatch")
        node_class: type[MemoryNode] = {
            SyncNodeType.FUNCTION: Function,
            SyncNodeType.FACT: Fact,
            SyncNodeType.PREFERENCE: Preference,
            SyncNodeType.OBSERVATION: Observation,
        }[event.node_type]
        node = node_class.from_dict(payload)
        if (
            node.tenant_id not in {None, event.scope.tenant_id}
            or node.owner_subject_id not in {None, event.scope.owner_subject_id}
            or node.owner not in {None, event.scope.owner_subject_id}
            or node.workspace_id not in {None, event.scope.workspace_id}
            or node.visibility not in {None, event.scope.visibility}
            or node.provenance.get("agent_id") not in {None, event.scope.agent_id}
            or node.provenance.get("session_id") not in {None, event.scope.session_id}
        ):
            raise ValueError("node payload scope mismatch")
        node.tenant_id = event.scope.tenant_id
        node.owner_subject_id = event.scope.owner_subject_id
        node.owner = event.scope.owner_subject_id
        node.workspace_id = event.scope.workspace_id
        node.visibility = event.scope.visibility
        node.origin_session = event.scope.session_id
        node.provenance = dict(node.provenance)
        node.provenance.update(
            {
                "agent_id": event.scope.agent_id or "",
                "session_id": event.scope.session_id or "",
            }
        )
        if event.node_type is SyncNodeType.OBSERVATION:
            self._store._observations = [
                item for item in self._store._observations if item.id != node_id
            ]
            # node_class was selected from event.node_type above.
            self._store._observations.append(cast(Observation, node))
        else:
            collection[node_id] = node
            if event.node_type is SyncNodeType.FUNCTION:
                self._store._name_index = {
                    name: identifier
                    for name, identifier in self._store._name_index.items()
                    if identifier != node_id
                }
                function_node = cast(Function, node)
                normalized = function_node.name_normalized or function_node.name
                self._store._name_index[normalized.strip().lower()] = node_id

    def _apply_events(self, events: tuple[SyncEvent, ...]) -> tuple[list[SyncReceipt], int, int, int]:
        receipts: list[SyncReceipt] = []
        applied = duplicate = conflict = 0
        edge_tombstones: set[str] = set()
        for event in events:
            inbox = next(
                (
                    item
                    for item in self._state["inbox"]
                    if item["origin_node_id"] == event.origin_node_id
                    and item["event_id"] == event.event_id
                ),
                None,
            )
            if inbox is not None:
                outcome = (
                    "rejected_conflict"
                    if inbox["outcome"] == "rejected_conflict"
                    else "duplicate"
                )
                receipts.append(SyncReceipt(event.event_id, outcome))
                if outcome == "duplicate":
                    duplicate += 1
                else:
                    conflict += 1
                continue
            current = self._entity_version(event)
            if current is not None and SyncVersion.parse(current["version_key"]) >= SyncVersion.parse(
                event.version
            ):
                self._state["inbox"].append(
                    {
                        "origin_node_id": event.origin_node_id,
                        "event_id": event.event_id,
                        "outcome": "rejected_conflict",
                        "applied_stream_seq": None,
                    }
                )
                receipts.append(SyncReceipt(event.event_id, "rejected_conflict"))
                conflict += 1
                continue
            self._apply_business_event(
                event, applied_edge_tombstones=edge_tombstones
            )
            stream_seq = self._append_event(event)
            self._state["inbox"].append(
                {
                    "origin_node_id": event.origin_node_id,
                    "event_id": event.event_id,
                    "outcome": "accepted",
                    "applied_stream_seq": stream_seq,
                }
            )
            receipts.append(SyncReceipt(event.event_id, "accepted"))
            applied += 1
        return receipts, applied, duplicate, conflict

    def sync_apply_batch(self, batch: SyncBatch) -> SyncBatchResult:
        if type(batch) is not SyncBatch:
            raise TypeError("batch must be an exact SyncBatch")
        with self._mutation():
            self._bind_tenant(batch.events[0].scope.tenant_id)
            existing = next(
                (item for item in self._state["batches"] if item["batch_id"] == batch.batch_id),
                None,
            )
            if existing is not None:
                if existing["request_sha256"] != batch.request_digest:
                    raise ValueError("batch digest conflict")
                return self._result_from_raw(existing["response"])
            receipts, _, _, _ = self._apply_events(batch.events)
            result = SyncBatchResult(
                batch.batch_id, batch.request_digest, "accepted", tuple(receipts)
            )
            self._state["batches"].append(
                {
                    "batch_id": batch.batch_id,
                    "request_sha256": batch.request_digest,
                    "response": result.to_dict(),
                    "created_at": self._time(datetime.now(UTC)),
                }
            )
            return result

    def sync_apply_page(self, remote_id: str, page: SyncPage) -> SyncApplyResult:
        remote_id = self._require_str(remote_id, "remote_id")
        if type(page) is not SyncPage:
            raise TypeError("page must be an exact SyncPage")
        if remote_id == self._local_node_id:
            raise ValueError("remote_id must not identify this node")
        with self._read():
            tenant_id = self._state["tenant_binding"]
            if page.items:
                tenant_id = tenant_id or page.items[0].event.scope.tenant_id
            # Reject a malformed or cross-tenant page before entering the
            # mutation/persistence critical section. Empty pages carry no
            # tenant identity, so the placeholder is structural-only.
            validate_incoming_page(page, tenant_id=tenant_id or "unbound-lite")
        with self._mutation():
            tenant_id = self._state["tenant_binding"]
            if page.items:
                tenant_id = tenant_id or page.items[0].event.scope.tenant_id
            # Revalidate against state reloaded under the writer lock: another
            # process may have established the tenant binding after preflight.
            events = validate_incoming_page(page, tenant_id=tenant_id or "unbound-lite")
            if events:
                self._bind_tenant(events[0].scope.tenant_id)
            cursor = next(
                (
                    item
                    for item in self._state["inbound_cursors"]
                    if item["remote_id"] == remote_id
                    and item["consumer_id"] == self._local_node_id
                ),
                None,
            )
            confirmed = 0 if cursor is None else cursor["after_seq"]
            if confirmed > page.next_after_seq:
                raise ValueError("page cursor regresses confirmed progress")
            if page.items and page.items[0].stream_seq <= confirmed < page.next_after_seq:
                raise ValueError("page partially overlaps confirmed progress")
            if page.next_after_seq <= confirmed:
                return SyncApplyResult(0, 0, 0, confirmed)
            receipts, applied, duplicate, conflict = self._apply_events(events)
            del receipts
            if cursor is None:
                self._state["inbound_cursors"].append(
                    {
                        "remote_id": remote_id,
                        "consumer_id": self._local_node_id,
                        "after_seq": page.next_after_seq,
                        "updated_at": self._time(datetime.now(UTC)),
                    }
                )
            else:
                cursor["after_seq"] = page.next_after_seq
                cursor["updated_at"] = self._time(datetime.now(UTC))
            return SyncApplyResult(
                applied, duplicate, conflict, page.next_after_seq
            )

    def sync_compact(self, now: datetime, *, limit: int) -> int:
        now = self._require_datetime(now, "now")
        limit = self._require_int(limit, "limit", minimum=1)
        retention_before = now - timedelta(seconds=self._retention_min_seconds)
        with self._mutation():
            self._cleanup_expired_snapshots(now)
            consumer_cutoff = now - timedelta(seconds=self._consumer_ttl_seconds)
            self._state["cursors"] = [
                item
                for item in self._state["cursors"]
                if self._parse_time(item["updated_at"]) > consumer_cutoff
            ]
            self._state["inbound_cursors"] = [
                item
                for item in self._state["inbound_cursors"]
                if self._parse_time(item["updated_at"]) > consumer_cutoff
            ]
            pins = [item["after_seq"] for item in self._state["cursors"]]
            pins.extend(item["resume_seq"] for item in self._state["snapshots"])
            upper = min(pins) if pins else self._state["next_stream_seq"] - 1
            deliveries_by_seq: dict[int, list[dict[str, Any]]] = {}
            for delivery in self._state["deliveries"]:
                deliveries_by_seq.setdefault(delivery["stream_seq"], []).append(delivery)
            enabled_targets = [
                target for target in self._state["targets"] if target["enabled"]
            ]
            candidates: list[int] = []
            for item in self._state["outbox"]:
                if item["stream_seq"] <= self._state["retention_floor"]:
                    continue
                if len(candidates) >= limit or item["stream_seq"] > upper:
                    break
                if self._parse_time(item["created_at"]) > retention_before:
                    break
                rows = deliveries_by_seq.get(item["stream_seq"], [])
                if any(
                    item["stream_seq"] > target["bootstrap_seq"]
                    and not any(
                        delivery["target_id"] == target["target_id"]
                        and delivery["state"] == "delivered"
                        for delivery in rows
                    )
                    for target in enabled_targets
                ):
                    break
                candidates.append(item["stream_seq"])
            if not candidates:
                return 0
            selected = set(candidates)
            self._state["outbox"] = [
                item for item in self._state["outbox"] if item["stream_seq"] not in selected
            ]
            self._state["deliveries"] = [
                item
                for item in self._state["deliveries"]
                if item["stream_seq"] not in selected
            ]
            through = candidates[-1]
            self._state["retention_floor"] = through
            self._state["compacted_through"] = through
            return len(candidates)
