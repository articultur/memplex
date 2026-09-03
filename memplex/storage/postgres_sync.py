"""PostgreSQL-backed sync repository implementation."""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

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


class PostgresSyncRepository(AbstractSyncRepository):
    """Sync repository implemented on top of PostgreSQL durable tables."""

    def __init__(
        self,
        store: Any,
        *,
        max_attempts: int = 8,
        snapshot_ttl_seconds: int = 900,
        max_snapshot_items: int = 1000000,
        max_active_snapshots_per_tenant: int = 2,
        max_active_snapshots_per_remote: int = 1,
        snapshot_create_timeout_seconds: int = 30,
        consumer_ttl_seconds: int = 86400,
        retention_min_seconds: int = 86400,
    ) -> None:
        if type(max_attempts) is not int:
            raise TypeError("max_attempts must be an int")
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        if type(snapshot_ttl_seconds) is not int:
            raise TypeError("snapshot_ttl_seconds must be an int")
        if snapshot_ttl_seconds < 1:
            raise ValueError("snapshot_ttl_seconds must be >= 1")
        if type(max_snapshot_items) is not int:
            raise TypeError("max_snapshot_items must be an int")
        if max_snapshot_items < 1:
            raise ValueError("max_snapshot_items must be >= 1")
        if type(max_active_snapshots_per_tenant) is not int:
            raise TypeError("max_active_snapshots_per_tenant must be an int")
        if max_active_snapshots_per_tenant < 1:
            raise ValueError("max_active_snapshots_per_tenant must be >= 1")
        if type(max_active_snapshots_per_remote) is not int:
            raise TypeError("max_active_snapshots_per_remote must be an int")
        if max_active_snapshots_per_remote < 1:
            raise ValueError("max_active_snapshots_per_remote must be >= 1")
        if type(snapshot_create_timeout_seconds) is not int:
            raise TypeError("snapshot_create_timeout_seconds must be an int")
        if snapshot_create_timeout_seconds < 1:
            raise ValueError("snapshot_create_timeout_seconds must be >= 1")
        if type(consumer_ttl_seconds) is not int:
            raise TypeError("consumer_ttl_seconds must be an int")
        if consumer_ttl_seconds < 1:
            raise ValueError("consumer_ttl_seconds must be >= 1")
        if type(retention_min_seconds) is not int:
            raise TypeError("retention_min_seconds must be an int")
        if retention_min_seconds < 1:
            raise ValueError("retention_min_seconds must be >= 1")
        if not hasattr(store, "_authorization_context"):
            raise TypeError("store must expose _authorization_context")
        if not hasattr(store, "_pool_manager"):
            raise TypeError("store must expose _pool_manager")
        if not hasattr(store, "_bind_transaction_scope"):
            raise TypeError("store must expose _bind_transaction_scope")
        capture_policy: Any = getattr(store, "_sync_capture_policy", None)
        if type(capture_policy) is not SyncCapturePolicy:
            raise TypeError("store must expose an exact SyncCapturePolicy")
        if capture_policy.mode != "required":
            raise ValueError("PostgreSQL sync repository requires capture mode 'required'")
        self._store = store
        self._max_attempts = max_attempts
        self._snapshot_ttl_seconds = snapshot_ttl_seconds
        self._max_snapshot_items = max_snapshot_items
        self._max_active_snapshots_per_tenant = max_active_snapshots_per_tenant
        self._max_active_snapshots_per_remote = max_active_snapshots_per_remote
        self._snapshot_create_timeout_seconds = snapshot_create_timeout_seconds
        self._consumer_ttl_seconds = consumer_ttl_seconds
        self._retention_min_seconds = retention_min_seconds
        self._local_node_id = capture_policy.local_node_id

    @property
    def _pool(self) -> Any:
        return self._store._pool_manager

    def _authorization_context(self) -> Any:
        return self._store._authorization_context()

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
            raise TypeError(f"{name} must be an int")
        if value < minimum:
            raise ValueError(f"{name} must be >= {minimum}")
        return value

    @staticmethod
    def _require_bool(value: object, name: str) -> bool:
        if type(value) is not bool:
            raise TypeError(f"{name} must be a bool")
        return value

    @staticmethod
    def _require_datetime(value: object, name: str) -> datetime:
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise TypeError(f"{name} must be an aware datetime")
        return value.astimezone(UTC)

    def _tenant_id(self) -> str:
        context = self._authorization_context()
        return context.principal.tenant_id

    def _bind_sync_scope(self, cursor: Any, context: Any, remote_id: str, consumer_id: str) -> None:
        cursor.execute(
            "SELECT set_config('memplex.verified_remote_node_id', %s, true), "
            "set_config('memplex.consumer_id', %s, true)",
            (remote_id, consumer_id),
        )

    @staticmethod
    def _lock_retention(cursor: Any, tenant_id: str) -> None:
        cursor.execute(
            "SELECT pg_catalog.pg_advisory_xact_lock("
            "pg_catalog.hashtextextended('memplex-sync-retention:' || %s, 0))",
            (tenant_id,),
        )

    def _build_event(self, tenant_id: str, row: tuple[Any, ...]) -> SyncEvent:
        (
            _,
            event_id,
            origin_node_id,
            node_type,
            entity_key,
            operation,
            version_key,
            visibility,
            owner_subject_id,
            workspace_id,
            agent_id,
            session_id,
            payload,
        ) = row

        return SyncEvent(
            protocol_version=1,
            event_id=self._require_str(event_id, "event_id"),
            origin_node_id=self._require_str(origin_node_id, "origin_node_id"),
            node_type=SyncNodeType(self._require_str(node_type, "node_type")),
            entity_key=SyncEntityKey.parse(self._require_str(entity_key, "entity_key")),
            operation=SyncOperation(self._require_str(operation, "operation")),
            version=self._require_str(version_key, "version"),
            scope=SyncScope(
                tenant_id=tenant_id,
                owner_subject_id=self._require_str(owner_subject_id, "owner_subject_id"),
                workspace_id=(
                    None if workspace_id is None else self._require_str(workspace_id, "workspace_id")
                ),
                visibility=self._require_str(visibility, "visibility"),
                agent_id=(None if agent_id is None else self._require_str(agent_id, "agent_id")),
                session_id=(None if session_id is None else self._require_str(session_id, "session_id")),
            ),
                payload=(None if payload is None else payload),
            )

    @staticmethod
    def _read_snapshot_items_page(
        tenant_id: str,
        snapshot_id: str,
        resume_seq: int,
        rows: list[tuple[Any, ...]],
        *,
        limit: int,
    ) -> SyncSnapshotPage:
        events: list[SyncEvent] = []
        for row in rows:
            if not row:
                raise ValueError("snapshot row is malformed")
            if type(row) is dict:
                event_data = row
            else:
                event_data = row[0]
            events.append(SyncEvent.from_dict(event_data))
        has_more = len(events) > limit
        if has_more:
            selected = events[:limit]
            next_anchor = SyncSnapshotAnchor.from_event(selected[-1])
        else:
            selected = events
            next_anchor = None
        return SyncSnapshotPage(
            events=tuple(selected),
            snapshot_id=snapshot_id,
            next_anchor=next_anchor,
            resume_seq=resume_seq,
            has_more=has_more,
        )

    def _current_snapshot_events(self, cursor: Any, tenant_id: str) -> list[SyncEvent]:
        """Read visible current state without depending on retained outbox rows."""
        cursor.execute(
            "SELECT node_type, entity_key, version_key, event_id "
            "FROM memplex_sync_entity_versions "
            "WHERE tenant_id=%s AND deleted=FALSE",
            (tenant_id,),
        )
        versions: dict[tuple[str, str], tuple[str, str]] = {}
        for node_type, entity_key, version_key, event_id in cursor.fetchall():
            key = (
                self._require_str(node_type, "node_type"),
                self._require_str(entity_key, "entity_key"),
            )
            if key in versions:
                raise ValueError("snapshot entity version identity is duplicated")
            versions[key] = (
                self._require_str(version_key, "version_key"),
                self._require_str(event_id, "event_id"),
            )

        cursor.execute(
            "SELECT * FROM ("
            "SELECT 'function', id, NULL::text, NULL::text, NULL::text, data, "
            "NULL::real, NULL::jsonb, NULL::timestamptz, owner_subject, workspace, "
            "visibility, source_agent, source_session FROM memplex_functions WHERE tenant_id=%s "
            "UNION ALL SELECT 'fact', id, NULL::text, NULL::text, NULL::text, data, "
            "NULL::real, NULL::jsonb, NULL::timestamptz, owner_subject, workspace, "
            "visibility, source_agent, source_session FROM memplex_facts WHERE tenant_id=%s "
            "UNION ALL SELECT 'preference', id, NULL::text, NULL::text, NULL::text, data, "
            "NULL::real, NULL::jsonb, NULL::timestamptz, owner_subject, workspace, "
            "visibility, source_agent, source_session FROM memplex_preferences WHERE tenant_id=%s "
            "UNION ALL SELECT 'observation', id, NULL::text, NULL::text, NULL::text, data, "
            "NULL::real, NULL::jsonb, NULL::timestamptz, owner_subject, workspace, "
            "visibility, source_agent, source_session FROM memplex_observations WHERE tenant_id=%s "
            "UNION ALL SELECT 'edge', NULL::text, source, target, edge_type, NULL::jsonb, "
            "weight, evidence, created_at, owner_subject, workspace, visibility, "
            "source_agent, source_session FROM memplex_edges WHERE tenant_id=%s"
            ") AS current_state LIMIT %s",
            (
                tenant_id,
                tenant_id,
                tenant_id,
                tenant_id,
                tenant_id,
                self._max_snapshot_items + 1,
            ),
        )
        events: list[SyncEvent] = []
        for row in cursor.fetchall():
            (
                node_type_text,
                node_id,
                source,
                target,
                edge_type,
                data,
                weight,
                evidence,
                created_at,
                owner_subject_id,
                workspace_id,
                visibility,
                agent_id,
                session_id,
            ) = row
            node_type = SyncNodeType(self._require_str(node_type_text, "node_type"))
            if node_type is SyncNodeType.EDGE:
                entity_key = SyncEntityKey.edge(
                    self._require_str(source, "edge source"),
                    self._require_str(target, "edge target"),
                    self._require_str(edge_type, "edge type"),
                )
                edge_created_at = self._require_datetime(
                    created_at, "edge created_at"
                )
                payload: dict[str, object] = {
                    "weight": float(weight),
                    "evidence": list(evidence or []),
                    "created_at": edge_created_at.strftime(
                        "%Y-%m-%dT%H:%M:%S.%fZ"
                    ),
                }
            else:
                entity_key = SyncEntityKey.node(
                    self._require_str(node_id, "node id")
                )
                if type(data) is not dict:
                    raise TypeError("snapshot node payload must be a JSON object")
                payload = data

            metadata = versions.get((node_type.value, str(entity_key)))
            if metadata is None:
                raise ValueError("snapshot business row has no durable entity version")
            version_key, event_id = metadata
            version = SyncVersion.parse(version_key)
            if version.event_id != event_id:
                raise ValueError("snapshot entity version event identity is inconsistent")
            events.append(
                SyncEvent(
                    protocol_version=1,
                    event_id=event_id,
                    origin_node_id=version.origin_node_id,
                    node_type=node_type,
                    entity_key=entity_key,
                    operation=SyncOperation.UPSERT,
                    version=version_key,
                    scope=SyncScope(
                        tenant_id=tenant_id,
                        owner_subject_id=self._require_str(
                            owner_subject_id, "owner_subject_id"
                        ),
                        workspace_id=self._require_str(
                            workspace_id, "workspace_id"
                        ),
                        visibility=self._require_str(visibility, "visibility"),
                        agent_id=self._require_str(agent_id, "agent_id"),
                        session_id=self._require_str(session_id, "session_id"),
                    ),
                    payload=payload,
                )
            )
        events.sort(key=SyncSnapshotAnchor.from_event)
        return events

    def _set_local_snapshot_timeout(self, cursor: Any) -> None:
        cursor.execute(
            "SET LOCAL statement_timeout = %s",
            (f"{self._snapshot_create_timeout_seconds}s",),
        )

    def _bind_snapshot_transaction_scope(self, cursor: Any, context: Any) -> None:
        """Establish snapshot isolation before the first transactional read."""
        connection = getattr(cursor, "connection", None)
        if connection is not None:
            # Per-lease target/principal verification performs read-only SQL
            # before the repository binder runs.  End that verification
            # transaction so PostgreSQL can accept the snapshot isolation
            # level as the first statement of the business transaction.
            connection.rollback()
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
        self._store._bind_transaction_scope(cursor, context)

    def _bind_snapshot_create_transaction_scope(self, cursor: Any, context: Any) -> None:
        """Use SSI so concurrent hidden-consumer admissions cannot both commit."""
        connection = getattr(cursor, "connection", None)
        if connection is not None:
            connection.rollback()
        cursor.execute("SET TRANSACTION ISOLATION LEVEL SERIALIZABLE")
        self._store._bind_transaction_scope(cursor, context)

    def _cleanup_expired_snapshots(self, cursor: Any, tenant_id: str) -> None:
        cursor.execute(
            "DELETE FROM memplex_sync_snapshots "
            "WHERE tenant_id=%s AND expires_at <= clock_timestamp()",
            (tenant_id,),
        )

    @contextmanager
    def _snapshot_transaction(self, context: Any) -> Iterator[tuple[Any, Any]]:
        try:
            with self._pool.transaction(
                self._bind_snapshot_create_transaction_scope, context
            ) as transaction:
                yield transaction
        except BaseException as exc:
            if getattr(exc, "pgcode", None) == "57014":
                raise SyncBackpressureError("snapshot_create_timeout") from None
            if getattr(exc, "pgcode", None) == "40001":
                raise SyncBackpressureError("snapshot_in_progress") from None
            raise

    def _validate_snapshot_cursor(
        self,
        cursor: SyncCursorClaims,
        tenant_id: str,
        remote_id: str,
        consumer_id: str,
    ) -> tuple[str, int]:
        if cursor.tenant_binding != tenant_id:
            raise ValueError("cursor tenant binding mismatch")
        if cursor.remote_binding != remote_id:
            raise ValueError("cursor remote binding mismatch")
        if cursor.consumer_binding != consumer_id:
            raise ValueError("cursor consumer binding mismatch")
        if cursor.snapshot_id is None:
            raise ValueError("snapshot cursor must include snapshot_id")
        if cursor.snapshot_after is None:
            raise ValueError("snapshot cursor must include snapshot_after")
        return cursor.snapshot_id, self._require_int(cursor.snapshot_seq, "snapshot_seq")

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
            raise TypeError("cursor must be a SyncCursorClaims")

        context = self._authorization_context()
        tenant_id = context.principal.tenant_id

        with self._pool.transaction(
            self._bind_snapshot_transaction_scope, context
        ) as (_, cur):
            self._bind_sync_scope(cur, context, remote_id, consumer_id)
            self._lock_retention(cur, tenant_id)

            if cursor is None:
                cur.execute(
                    "SELECT COALESCE(MAX(stream_seq), 0) AS snapshot_seq FROM memplex_sync_outbox WHERE tenant_id = %s",
                    (tenant_id,),
                )
                snapshot_seq_row = cur.fetchone()
                snapshot_seq = int(self._require_int(snapshot_seq_row[0] if snapshot_seq_row else 0, "snapshot_seq"))
                requested_after_seq = 0
            else:
                if cursor.snapshot_id is not None:
                    raise ValueError("sync page cursor must not include snapshot fields")
                if cursor.tenant_binding != tenant_id:
                    raise ValueError("cursor tenant binding mismatch")
                if cursor.remote_binding != remote_id:
                    raise ValueError("cursor remote binding mismatch")
                if cursor.consumer_binding != consumer_id:
                    raise ValueError("cursor consumer binding mismatch")
                requested_after_seq = self._require_int(cursor.after_seq, "after_seq")
                cursor_snapshot_seq = self._require_int(cursor.snapshot_seq, "snapshot_seq")
                if requested_after_seq == cursor_snapshot_seq:
                    cur.execute(
                        "SELECT COALESCE(MAX(stream_seq), 0) AS snapshot_seq "
                        "FROM memplex_sync_outbox WHERE tenant_id = %s",
                        (tenant_id,),
                    )
                    snapshot_seq_row = cur.fetchone()
                    snapshot_seq = max(
                        requested_after_seq,
                        int(
                            self._require_int(
                                snapshot_seq_row[0] if snapshot_seq_row else 0,
                                "snapshot_seq",
                            )
                        ),
                    )
                else:
                    snapshot_seq = cursor_snapshot_seq
                if requested_after_seq > snapshot_seq:
                    raise ValueError("cursor after sequence exceeds snapshot")

            cur.execute(
                "SELECT COALESCE(retention_floor, 0) FROM memplex_sync_stream_state "
                "WHERE tenant_id=%s",
                (tenant_id,),
            )
            row = cur.fetchone()
            retention_floor = self._require_int((row[0] if row else 0), "retention_floor")
            if requested_after_seq < retention_floor:
                raise SyncCursorExpired("cursor_expired")

            if cursor is not None:
                cur.execute(
                    "UPDATE memplex_sync_deliveries AS delivery "
                    "SET state='delivered', lease_owner=NULL, lease_until=NULL, "
                    "last_error_code=NULL "
                    "FROM memplex_sync_targets AS target, memplex_sync_outbox AS outbox "
                    "WHERE delivery.tenant_id=%s "
                    "AND target.tenant_id=delivery.tenant_id "
                    "AND target.target_id=delivery.target_id "
                    "AND target.remote_node_id=%s "
                    "AND outbox.tenant_id=delivery.tenant_id "
                    "AND outbox.stream_seq=delivery.stream_seq "
                    "AND outbox.origin_node_id <> %s "
                    "AND delivery.stream_seq <= %s "
                    "AND delivery.state IN ('pending','leased')",
                    (tenant_id, remote_id, remote_id, requested_after_seq),
                )

            cur.execute(
                "SELECT COALESCE(MAX(after_seq), 0) AS confirmed_after_seq "
                "FROM memplex_sync_cursors "
                "WHERE tenant_id=%s AND remote_id=%s AND consumer_id=%s",
                (tenant_id, remote_id, consumer_id),
            )
            row = cur.fetchone()
            stored_after_seq = self._require_int(row[0] if row else 0, "confirmed_after_seq")
            next_confirmed_after_seq = max(stored_after_seq, requested_after_seq)

            page_sql = (
                "SELECT stream_seq, event_id, origin_node_id, node_type, entity_key, "
                "operation, version_key, visibility, owner_subject_id, workspace_id, "
                "agent_id, session_id, payload "
                "FROM memplex_sync_outbox "
                "WHERE tenant_id = %s AND stream_seq > %s AND stream_seq <= %s "
                "AND origin_node_id <> %s "
                "ORDER BY stream_seq ASC LIMIT %s"
            )
            cur.execute(
                page_sql,
                (
                    tenant_id,
                    next_confirmed_after_seq,
                    snapshot_seq,
                    remote_id,
                    limit + 1,
                ),
            )
            rows = cur.fetchall()
            has_more = len(rows) > limit
            if has_more:
                rows = rows[:limit]

            items: tuple[SyncStreamItem, ...] = tuple(
                SyncStreamItem(
                    stream_seq=self._require_int(row[0], "stream_seq", minimum=1),
                    event=self._build_event(
                        tenant_id,
                        row,
                    ),
                )
                for row in rows
            )
            if has_more:
                next_after_seq = items[-1].stream_seq
            else:
                next_after_seq = snapshot_seq

            # Persist caller-confirmed progress, monotonic against prior cursor
            # state, never allowing rollback.
            cur.execute(
                "INSERT INTO memplex_sync_cursors (tenant_id, remote_id, consumer_id, after_seq) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (tenant_id, remote_id, consumer_id) "
                "DO UPDATE SET after_seq = GREATEST(memplex_sync_cursors.after_seq, EXCLUDED.after_seq), "
                "updated_at = clock_timestamp()",
                (tenant_id, remote_id, consumer_id, next_confirmed_after_seq),
            )

            return SyncPage(
                items=items,
                snapshot_seq=snapshot_seq,
                next_after_seq=next_after_seq,
                has_more=has_more,
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

        context = self._authorization_context()
        tenant_id = context.principal.tenant_id

        with self._snapshot_transaction(context) as (_, cur):
            self._bind_sync_scope(cur, context, remote_id, consumer_id)
            self._lock_retention(cur, tenant_id)
            self._set_local_snapshot_timeout(cur)
            self._cleanup_expired_snapshots(cur, tenant_id)

            cur.execute(
                "SELECT snapshot_id, resume_seq "
                "FROM memplex_sync_snapshots "
                "WHERE tenant_id=%s AND remote_id=%s AND consumer_id=%s "
                "AND request_id=%s AND expires_at > clock_timestamp()",
                (tenant_id, remote_id, consumer_id, request_id),
            )
            reused_snapshot = cur.fetchone()
            if reused_snapshot is not None:
                snapshot_id = self._require_str(reused_snapshot[0], "snapshot_id")
                resume_seq = self._require_int(reused_snapshot[1], "resume_seq")
            else:
                cur.execute(
                    "SELECT remote_count, tenant_count "
                    "FROM memplex_sync_snapshot_admission_counts()"
                )
                admission_counts = cur.fetchone() or (0, 0)
                active_remote = self._require_int(admission_counts[0], "active_remote_snapshots")
                if active_remote >= self._max_active_snapshots_per_remote:
                    raise SyncBackpressureError("snapshot_in_progress")

                active_tenant = self._require_int(admission_counts[1], "active_tenant_snapshots")
                if active_tenant >= self._max_active_snapshots_per_tenant:
                    raise SyncBackpressureError("snapshot_in_progress")

                cur.execute(
                    "SELECT GREATEST("
                    "  (SELECT COALESCE(MAX(stream_seq), 0) "
                    "   FROM memplex_sync_outbox WHERE tenant_id=%s), "
                    "  (SELECT COALESCE(compacted_through, 0) "
                    "   FROM memplex_sync_stream_state WHERE tenant_id=%s)"
                    ")",
                    (tenant_id, tenant_id),
                )
                resume_seq = self._require_int((cur.fetchone() or (0,))[0], "resume_seq")
                events = self._current_snapshot_events(cur, tenant_id)
                if len(events) > self._max_snapshot_items:
                    raise SyncBackpressureError("snapshot_too_large")

                snapshot_id = str(uuid.uuid4())
                cur.execute(
                    "INSERT INTO memplex_sync_snapshots "
                    "(tenant_id, snapshot_id, remote_id, consumer_id, request_id, resume_seq, expires_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, clock_timestamp() + (%s || ' seconds')::interval)",
                    (
                        tenant_id,
                        snapshot_id,
                        remote_id,
                        consumer_id,
                        request_id,
                        resume_seq,
                        self._snapshot_ttl_seconds,
                    ),
                )
                for event in events:
                    cur.execute(
                        "INSERT INTO memplex_sync_snapshot_items "
                        "(tenant_id, snapshot_id, node_type, entity_key, event) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (
                            tenant_id,
                            snapshot_id,
                            event.node_type.value,
                            str(event.entity_key),
                            json.dumps(
                                event.to_dict(),
                                sort_keys=True,
                                separators=(",", ":"),
                            ),
                        ),
                    )

            cur.execute(
                "SELECT event "
                "FROM memplex_sync_snapshot_items "
                "WHERE tenant_id=%s AND snapshot_id=%s "
                "ORDER BY node_type ASC, entity_key ASC LIMIT %s",
                (tenant_id, snapshot_id, limit + 1),
            )
            rows = cur.fetchall()
            return self._read_snapshot_items_page(
                tenant_id,
                snapshot_id,
                resume_seq,
                rows,
                limit=limit,
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
        if type(cursor) is not SyncCursorClaims:
            raise TypeError("cursor must be SyncCursorClaims")
        limit = self._require_int(limit, "limit", minimum=1)

        context = self._authorization_context()
        tenant_id = context.principal.tenant_id
        snapshot_id = self._validate_snapshot_cursor(
            cursor, tenant_id, remote_id, consumer_id
        )[0]

        expired_snapshot = False
        page: SyncSnapshotPage | None = None
        with self._pool.transaction(self._store._bind_transaction_scope, context) as (_, cur):
            self._bind_sync_scope(cur, context, remote_id, consumer_id)
            self._cleanup_expired_snapshots(cur, tenant_id)

            if datetime.now(UTC) >= cursor.expires_at:
                raise SyncCursorExpired("cursor is expired")

            cur.execute(
                "SELECT resume_seq, remote_id, consumer_id, expires_at "
                "FROM memplex_sync_snapshots "
                "WHERE tenant_id=%s AND snapshot_id=%s",
                (tenant_id, snapshot_id),
            )
            snapshot_row = cur.fetchone()
            if snapshot_row is None:
                expired_snapshot = True
            else:
                resume_seq = self._require_int(snapshot_row[0], "resume_seq")
                if resume_seq != cursor.snapshot_seq:
                    raise ValueError("cursor snapshot_seq is stale")
                if snapshot_row[1] != remote_id or snapshot_row[2] != consumer_id:
                    raise ValueError("cursor remote/consumer binding mismatch")
                if self._require_datetime(
                    snapshot_row[3], "expires_at"
                ) <= datetime.now(UTC):
                    cur.execute(
                        "DELETE FROM memplex_sync_snapshots "
                        "WHERE tenant_id=%s AND snapshot_id=%s",
                        (tenant_id, snapshot_id),
                    )
                    expired_snapshot = True
                else:
                    anchor = cursor.snapshot_after
                    if anchor is not None:
                        cur.execute(
                            "SELECT 1 FROM memplex_sync_snapshot_items "
                            "WHERE tenant_id=%s AND snapshot_id=%s "
                            "AND node_type=%s AND entity_key=%s",
                            (
                                tenant_id,
                                snapshot_id,
                                anchor.node_type.value,
                                str(anchor.entity_key),
                            ),
                        )
                        if cur.fetchone() is None:
                            raise ValueError("cursor anchor is not in snapshot")
                        cur.execute(
                            "SELECT event "
                            "FROM memplex_sync_snapshot_items "
                            "WHERE tenant_id=%s AND snapshot_id=%s "
                            "AND (node_type, entity_key) > (%s, %s) "
                            "ORDER BY node_type ASC, entity_key ASC "
                            "LIMIT %s",
                            (
                                tenant_id,
                                snapshot_id,
                                anchor.node_type.value,
                                str(anchor.entity_key),
                                limit + 1,
                            ),
                        )
                    else:
                        cur.execute(
                            "SELECT event "
                            "FROM memplex_sync_snapshot_items "
                            "WHERE tenant_id=%s AND snapshot_id=%s "
                            "ORDER BY node_type ASC, entity_key ASC LIMIT %s",
                            (tenant_id, snapshot_id, limit + 1),
                        )

                    rows = cur.fetchall()
                    page = self._read_snapshot_items_page(
                        tenant_id,
                        snapshot_id,
                        resume_seq,
                        rows,
                        limit=limit,
                    )
        if expired_snapshot:
            raise SyncCursorExpired("snapshot_expired")
        if page is None:  # pragma: no cover - exhaustive state guard
            raise RuntimeError("snapshot page was not produced")
        return page

    def sync_apply_batch(self, batch: SyncBatch) -> SyncBatchResult:
        if type(batch) is not SyncBatch:
            raise TypeError("batch must be an exact SyncBatch")
        executor = getattr(self._store, "_inbound_executor", None)
        if executor is None:
            raise RuntimeError("verified inbound executor is not available")

        from memplex.sync_ingress import validate_ingress_batch

        envelope = validate_ingress_batch(
            batch.canonical_bytes,
            batch.request_digest,
        )
        return executor.apply(envelope)

    @staticmethod
    def _event_payload(event: SyncEvent) -> dict[str, object] | None:
        payload = event.to_dict()["payload"]
        if payload is not None and type(payload) is not dict:
            raise TypeError("event payload must thaw to an exact dict")
        return payload

    def _bind_inbound_event_scope(self, cursor: Any, event: SyncEvent) -> None:
        scope = event.scope
        cursor.execute(
            "SELECT "
            "set_config('memplex.tenant_id', %s, true), "
            "set_config('memplex.subject_id', %s, true), "
            "set_config('memplex.workspace_id', %s, true), "
            "set_config('memplex.agent_id', %s, true), "
            "set_config('memplex.session_id', %s, true), "
            "set_config('memplex.verified_remote_node_id', %s, true), "
            "set_config('memplex.sync_capture', 'off', true), "
            "set_config('memplex.sync_apply_mode', 'off', true)",
            (
                scope.tenant_id,
                scope.owner_subject_id,
                scope.workspace_id or "",
                scope.agent_id or "",
                scope.session_id or "",
                event.origin_node_id,
            ),
        )

    @staticmethod
    def _validate_edge_payload(payload: object) -> tuple[float, list[str], datetime]:
        if type(payload) is not dict or set(payload) != {
            "weight",
            "evidence",
            "created_at",
        }:
            raise ValueError("edge payload fields are invalid")
        weight = payload["weight"]
        if type(weight) not in {int, float}:
            raise TypeError("edge weight must be a number")
        weight_value = float(weight)
        if not math.isfinite(weight_value) or abs(weight_value) > 3.4028235e38:
            raise ValueError("edge weight is outside PostgreSQL REAL range")
        evidence = payload["evidence"]
        if type(evidence) is not list or not all(type(item) is str for item in evidence):
            raise TypeError("edge evidence must be a list of strings")
        created_at = payload["created_at"]
        if type(created_at) is not str:
            raise TypeError("edge created_at must be a string")
        try:
            parsed = datetime.strptime(
                created_at, "%Y-%m-%dT%H:%M:%S.%fZ"
            ).replace(tzinfo=UTC)
        except ValueError as exc:
            raise ValueError("edge created_at must be canonical UTC microseconds") from exc
        if parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ") != created_at:
            raise ValueError("edge created_at must be canonical UTC microseconds")
        return weight_value, evidence, parsed

    def _apply_inbound_business_event(
        self,
        cursor: Any,
        event: SyncEvent,
        *,
        edge_tombstones: frozenset[str],
    ) -> None:
        scope = event.scope
        payload = self._event_payload(event)
        if event.node_type is SyncNodeType.EDGE:
            edge_parts = event.entity_key.edge_parts
            if edge_parts is None:
                raise ValueError("edge event must carry an edge key")
            source, target, edge_type = edge_parts
            if event.operation is SyncOperation.TOMBSTONE:
                cursor.execute(
                    "DELETE FROM memplex_edges WHERE tenant_id=%s "
                    "AND source=%s AND target=%s AND edge_type=%s",
                    (scope.tenant_id, source, target, edge_type),
                )
                return
            weight, evidence, created_at = self._validate_edge_payload(payload)
            cursor.execute(
                "INSERT INTO memplex_edges "
                "(tenant_id, source, target, edge_type, weight, evidence, created_at, "
                "owner_subject, workspace, visibility, source_agent, source_session) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (tenant_id, source, target, edge_type) DO UPDATE SET "
                "weight=EXCLUDED.weight, evidence=EXCLUDED.evidence, "
                "created_at=EXCLUDED.created_at, owner_subject=EXCLUDED.owner_subject, "
                "workspace=EXCLUDED.workspace, visibility=EXCLUDED.visibility, "
                "source_agent=EXCLUDED.source_agent, source_session=EXCLUDED.source_session",
                (
                    scope.tenant_id,
                    source,
                    target,
                    edge_type,
                    weight,
                    json.dumps(evidence, separators=(",", ":")),
                    created_at,
                    scope.owner_subject_id,
                    scope.workspace_id or "",
                    scope.visibility,
                    scope.agent_id or "",
                    scope.session_id or "",
                ),
            )
            return

        node_id = event.entity_key.node_id
        if node_id is None:
            raise ValueError("node event must carry a node key")
        table = {
            SyncNodeType.FUNCTION: "memplex_functions",
            SyncNodeType.FACT: "memplex_facts",
            SyncNodeType.PREFERENCE: "memplex_preferences",
            SyncNodeType.OBSERVATION: "memplex_observations",
        }[event.node_type]
        if event.operation is SyncOperation.TOMBSTONE:
            if event.node_type is SyncNodeType.FUNCTION:
                cursor.execute(
                    "SELECT source, target, edge_type FROM memplex_edges "
                    "WHERE tenant_id=%s AND (source=%s OR target=%s)",
                    (scope.tenant_id, node_id, node_id),
                )
                for source, target, edge_type in cursor.fetchall():
                    key = str(SyncEntityKey.edge(source, target, edge_type))
                    if key not in edge_tombstones:
                        raise ValueError(
                            "function tombstone requires explicit edge tombstones"
                        )
            cursor.execute(
                f"DELETE FROM {table} WHERE tenant_id=%s AND id=%s",
                (scope.tenant_id, node_id),
            )
            return
        if type(payload) is not dict:
            raise TypeError("node upsert payload must be a JSON object")
        cursor.execute(
            f"INSERT INTO {table} "
            "(tenant_id,id,data,owner_subject,workspace,visibility,source_agent,source_session) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (tenant_id,id) DO UPDATE SET "
            "data=EXCLUDED.data, owner_subject=EXCLUDED.owner_subject, "
            "workspace=EXCLUDED.workspace, visibility=EXCLUDED.visibility, "
            "source_agent=EXCLUDED.source_agent, source_session=EXCLUDED.source_session",
            (
                scope.tenant_id,
                node_id,
                json.dumps(payload, sort_keys=True, separators=(",", ":")),
                scope.owner_subject_id,
                scope.workspace_id or "",
                scope.visibility,
                scope.agent_id or "",
                scope.session_id or "",
            ),
        )

    def sync_apply_page(self, remote_id: str, page: SyncPage) -> SyncApplyResult:
        remote_id = self._require_str(remote_id, "remote_id")
        if type(page) is not SyncPage:
            raise TypeError("page must be an exact SyncPage")
        if remote_id == self._local_node_id:
            raise ValueError("remote_id must not identify this node")
        context = self._authorization_context()
        tenant_id = context.principal.tenant_id
        events = validate_incoming_page(page, tenant_id=tenant_id)
        with self._pool.transaction(self._store._bind_transaction_scope, context) as (_, cur):
            self._bind_sync_scope(cur, context, remote_id, self._local_node_id)
            self._lock_retention(cur, tenant_id)
            cur.execute(
                "SELECT after_seq FROM memplex_sync_cursors "
                "WHERE tenant_id=%s AND remote_id=%s AND consumer_id=%s FOR UPDATE",
                (tenant_id, remote_id, self._local_node_id),
            )
            cursor_row = cur.fetchone()
            confirmed_after = self._require_int(
                0 if cursor_row is None else cursor_row[0], "confirmed_after"
            )
            if confirmed_after > page.next_after_seq:
                raise ValueError("page cursor regresses confirmed progress")
            if (
                page.items
                and page.items[0].stream_seq <= confirmed_after < page.next_after_seq
            ):
                raise ValueError("page partially overlaps confirmed progress")

            version_by_entity: dict[tuple[str, str], SyncVersion | None] = {}
            for node_type, entity_key in sorted(
                {(event.node_type.value, str(event.entity_key)) for event in events}
            ):
                cur.execute(
                    "SELECT version_key FROM memplex_sync_entity_versions "
                    "WHERE tenant_id=%s AND node_type=%s AND entity_key=%s FOR UPDATE",
                    (tenant_id, node_type, entity_key),
                )
                row = cur.fetchone()
                version_by_entity[(node_type, entity_key)] = (
                    None if row is None else SyncVersion.parse(row[0])
                )

            decisions: list[str] = []
            accepted_by_origin: dict[str, int] = {}
            for event in events:
                self._bind_inbound_event_scope(cur, event)
                cur.execute(
                    "SELECT outcome FROM memplex_sync_inbox "
                    "WHERE tenant_id=%s AND origin_node_id=%s AND event_id=%s",
                    (tenant_id, event.origin_node_id, event.event_id),
                )
                inbox_row = cur.fetchone()
                if inbox_row is not None:
                    decisions.append(
                        "conflict_replay"
                        if inbox_row[0] == "rejected_conflict"
                        else "duplicate"
                    )
                    continue
                identity = (event.node_type.value, str(event.entity_key))
                current_version = version_by_entity[identity]
                incoming_version = SyncVersion.parse(event.version)
                if current_version is not None and current_version >= incoming_version:
                    decisions.append("conflict")
                    continue
                decisions.append("accepted")
                version_by_entity[identity] = incoming_version
                accepted_by_origin[event.origin_node_id] = (
                    accepted_by_origin.get(event.origin_node_id, 0) + 1
                )

            additional_deliveries = 0
            for origin, accepted_count in accepted_by_origin.items():
                cur.execute(
                    "SELECT COUNT(*) FROM memplex_sync_targets "
                    "WHERE tenant_id=%s AND enabled "
                    "AND remote_node_id IS DISTINCT FROM %s",
                    (tenant_id, origin),
                )
                additional_deliveries += self._require_int(
                    (cur.fetchone() or (0,))[0], "target fanout"
                ) * accepted_count
            cur.execute(
                "SELECT memplex_sync_assert_delivery_quota(%s, %s)",
                (tenant_id, additional_deliveries),
            )

            applied = duplicate = conflict = 0
            applied_edge_tombstones: set[str] = set()
            for event, decision in zip(events, decisions):
                self._bind_inbound_event_scope(cur, event)
                if decision == "duplicate":
                    duplicate += 1
                    continue
                if decision == "conflict_replay":
                    conflict += 1
                    continue
                if decision == "conflict":
                    cur.execute(
                        "INSERT INTO memplex_sync_inbox "
                        "(tenant_id,origin_node_id,event_id,outcome) "
                        "VALUES (%s,%s,%s,'rejected_conflict')",
                        (tenant_id, event.origin_node_id, event.event_id),
                    )
                    conflict += 1
                    continue

                self._apply_inbound_business_event(
                    cur,
                    event,
                    edge_tombstones=frozenset(applied_edge_tombstones),
                )
                payload = self._event_payload(event)
                cur.execute(
                    "INSERT INTO memplex_sync_outbox "
                    "(tenant_id,event_id,origin_node_id,node_type,entity_key,operation,"
                    "version_key,payload,visibility,owner_subject_id,workspace_id,agent_id,session_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "RETURNING stream_seq",
                    (
                        tenant_id,
                        event.event_id,
                        event.origin_node_id,
                        event.node_type.value,
                        str(event.entity_key),
                        event.operation.value,
                        event.version,
                        None
                        if payload is None
                        else json.dumps(
                            payload, sort_keys=True, separators=(",", ":")
                        ),
                        event.scope.visibility,
                        event.scope.owner_subject_id,
                        event.scope.workspace_id,
                        event.scope.agent_id,
                        event.scope.session_id,
                    ),
                )
                stream_seq = self._require_int(
                    (cur.fetchone() or (None,))[0], "stream_seq", minimum=1
                )
                cur.execute(
                    "INSERT INTO memplex_sync_entity_versions "
                    "(tenant_id,node_type,entity_key,version_key,deleted,event_id,last_stream_seq) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (tenant_id,node_type,entity_key) DO UPDATE SET "
                    "version_key=EXCLUDED.version_key, deleted=EXCLUDED.deleted, "
                    "event_id=EXCLUDED.event_id, last_stream_seq=EXCLUDED.last_stream_seq",
                    (
                        tenant_id,
                        event.node_type.value,
                        str(event.entity_key),
                        event.version,
                        event.operation is SyncOperation.TOMBSTONE,
                        event.event_id,
                        stream_seq,
                    ),
                )
                cur.execute(
                    "INSERT INTO memplex_sync_inbox "
                    "(tenant_id,origin_node_id,event_id,outcome,applied_stream_seq) "
                    "VALUES (%s,%s,%s,'accepted',%s)",
                    (tenant_id, event.origin_node_id, event.event_id, stream_seq),
                )
                cur.execute(
                    "INSERT INTO memplex_sync_deliveries "
                    "(tenant_id,target_id,stream_seq,state) "
                    "SELECT %s,target_id,%s,'pending' FROM memplex_sync_targets "
                    "WHERE tenant_id=%s AND enabled "
                    "AND remote_node_id IS DISTINCT FROM %s",
                    (tenant_id, stream_seq, tenant_id, event.origin_node_id),
                )
                if (
                    event.node_type is SyncNodeType.EDGE
                    and event.operation is SyncOperation.TOMBSTONE
                ):
                    applied_edge_tombstones.add(str(event.entity_key))
                applied += 1

            self._bind_sync_scope(cur, context, remote_id, self._local_node_id)
            cur.execute(
                "INSERT INTO memplex_sync_cursors "
                "(tenant_id,remote_id,consumer_id,after_seq) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (tenant_id,remote_id,consumer_id) DO UPDATE SET "
                "after_seq=GREATEST(memplex_sync_cursors.after_seq,EXCLUDED.after_seq), "
                "updated_at=clock_timestamp()",
                (tenant_id, remote_id, self._local_node_id, page.next_after_seq),
            )
            return SyncApplyResult(
                applied=applied,
                duplicate=duplicate,
                conflict=conflict,
                cursor_advanced=max(confirmed_after, page.next_after_seq),
            )

    def sync_register_target(self, target_id: str, *, bootstrap: str = "future") -> None:
        target_id = self._require_str(target_id, "target_id")
        bootstrap = self._require_str(bootstrap, "bootstrap")
        if bootstrap not in {"future", "retained"}:
            raise ValueError("bootstrap must be 'future' or 'retained'")

        context = self._authorization_context()
        tenant_id = context.principal.tenant_id

        with self._pool.transaction(self._store._bind_transaction_scope, context) as (_, cur):
            if self._local_node_id == target_id:
                raise ValueError("target_id must not target this node")
            self._lock_retention(cur, tenant_id)
            cur.execute(
                "SELECT remote_node_id FROM memplex_sync_targets "
                "WHERE tenant_id=%s AND target_id=%s FOR UPDATE",
                (tenant_id, target_id),
            )
            existing = cur.fetchone()
            if existing is not None:
                if self._require_str(existing[0], "remote_node_id") != target_id:
                    raise ValueError("target identity mismatch")
                return

            if bootstrap == "future":
                cur.execute(
                    "SELECT COALESCE(MAX(stream_seq), 0) AS bootstrap_seq "
                    "FROM memplex_sync_outbox WHERE tenant_id = %s",
                    (tenant_id,),
                )
                bootstrap_seq = self._require_int((cur.fetchone() or (0,))[0], "bootstrap_seq")
                additional_deliveries = 0
            else:
                cur.execute(
                    "SELECT COALESCE(retention_floor, 0) FROM memplex_sync_stream_state WHERE tenant_id = %s",
                    (tenant_id,),
                )
                row = cur.fetchone()
                if row is None:
                    raise ValueError("stream state is not initialized")
                bootstrap_seq = self._require_int(row[0], "bootstrap_seq")
                cur.execute(
                    "SELECT COUNT(*) FROM memplex_sync_outbox "
                    "WHERE tenant_id = %s AND stream_seq >= %s",
                    (tenant_id, bootstrap_seq),
                )
                additional_deliveries = self._require_int((cur.fetchone() or (0,))[0], "additional_deliveries")

            cur.execute(
                "SELECT memplex_sync_assert_delivery_quota(%s, %s)",
                (tenant_id, additional_deliveries),
            )

            cur.execute(
                "INSERT INTO memplex_sync_targets "
                "(tenant_id, target_id, remote_node_id, bootstrap_seq) "
                "VALUES (%s, %s, %s, %s)",
                (tenant_id, target_id, target_id, bootstrap_seq),
            )

            if bootstrap == "retained" and additional_deliveries:
                cur.execute(
                    "INSERT INTO memplex_sync_deliveries "
                    "(tenant_id, target_id, stream_seq, state) "
                    "SELECT %s, %s, stream_seq, 'pending' "
                    "FROM memplex_sync_outbox "
                    "WHERE tenant_id = %s AND stream_seq >= %s "
                    "ON CONFLICT DO NOTHING",
                    (tenant_id, target_id, tenant_id, bootstrap_seq),
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

        context = self._authorization_context()
        tenant_id = context.principal.tenant_id
        now = datetime.now(UTC)
        lease_until = now + timedelta(seconds=lease_seconds)

        with self._pool.transaction(self._store._bind_transaction_scope, context) as (_, cur):
            cur.execute(
                "UPDATE memplex_sync_deliveries AS delivery "
                "SET state='pending', lease_owner=NULL, lease_until=NULL, last_error_code=NULL "
                "WHERE delivery.tenant_id=%s AND delivery.target_id=%s "
                "AND delivery.state='leased' AND delivery.lease_until <= %s",
                (tenant_id, target_id, now),
            )

            claim_sql = (
                "SELECT delivery.stream_seq, outbox.event_id, outbox.origin_node_id, "
                "outbox.node_type, outbox.entity_key, outbox.operation, outbox.version_key, "
                "outbox.visibility, outbox.owner_subject_id, outbox.workspace_id, "
                "outbox.agent_id, outbox.session_id, outbox.payload, delivery.attempt_count "
                "FROM memplex_sync_deliveries AS delivery "
                "JOIN memplex_sync_targets AS target "
                "  ON target.tenant_id = delivery.tenant_id AND target.target_id = delivery.target_id "
                "JOIN memplex_sync_outbox AS outbox "
                "  ON outbox.tenant_id = delivery.tenant_id AND outbox.stream_seq = delivery.stream_seq "
                "WHERE delivery.tenant_id=%s AND delivery.target_id=%s "
                "AND target.enabled "
                "AND delivery.state='pending' AND delivery.next_attempt_at <= %s "
                "AND delivery.attempt_count < %s "
                "AND outbox.origin_node_id=%s "
                "ORDER BY delivery.stream_seq ASC "
                "FOR UPDATE OF delivery SKIP LOCKED LIMIT %s"
            )
            cur.execute(
                claim_sql,
                (
                    tenant_id,
                    target_id,
                    now,
                    self._max_attempts,
                    self._local_node_id,
                    limit,
                ),
            )
            rows = cur.fetchall()

            deliveries: list[SyncDelivery] = []
            for row in rows:
                delivery_lease_id = str(uuid.uuid4())
                event = SyncEvent(
                    1,
                    self._require_str(row[1], "event_id"),
                    self._require_str(row[2], "origin_node_id"),
                    SyncNodeType(self._require_str(row[3], "node_type")),
                    SyncEntityKey.parse(self._require_str(row[4], "entity_key")),
                    SyncOperation(self._require_str(row[5], "operation")),
                    self._require_str(row[6], "version_key"),
                    SyncScope(
                        tenant_id=tenant_id,
                        owner_subject_id=self._require_str(row[8], "owner_subject_id"),
                        workspace_id=(None if row[9] is None else self._require_str(row[9], "workspace_id")),
                        visibility=self._require_str(row[7], "visibility"),
                        agent_id=(None if row[10] is None else self._require_str(row[10], "agent_id")),
                        session_id=(None if row[11] is None else self._require_str(row[11], "session_id")),
                    ),
                    payload=(None if row[12] is None else row[12]),
                )
                attempt = self._require_int(row[13], "attempt_count") + 1
                stream_seq = self._require_int(row[0], "stream_seq", minimum=1)
                cur.execute(
                    "UPDATE memplex_sync_deliveries "
                    "SET state='leased', lease_owner=%s, lease_until=%s, attempt_count=%s, "
                    "last_error_code=NULL "
                    "WHERE tenant_id=%s AND target_id=%s AND stream_seq=%s",
                    (delivery_lease_id, lease_until, attempt, tenant_id, target_id, stream_seq),
                )
                deliveries.append(
                    SyncDelivery(
                        target_id=target_id,
                        event=event,
                        attempt=attempt,
                        lease_id=delivery_lease_id,
                        lease_expires_at=lease_until,
                    )
                )

            return deliveries

    def sync_ack(self, delivery: SyncDelivery, receipt: SyncReceipt) -> None:
        if type(delivery) is not SyncDelivery:
            raise TypeError("delivery must be SyncDelivery")
        if type(receipt) is not SyncReceipt:
            raise TypeError("receipt must be SyncReceipt")
        if receipt.event_id != delivery.event.event_id:
            raise ValueError("receipt event_id mismatch")

        context = self._authorization_context()
        tenant_id = context.principal.tenant_id
        with self._pool.transaction(self._store._bind_transaction_scope, context) as (_, cur):
            cur.execute(
                "SELECT delivery.stream_seq, delivery.state, delivery.lease_owner, delivery.lease_until "
                "FROM memplex_sync_deliveries AS delivery "
                "JOIN memplex_sync_outbox AS outbox "
                "  ON outbox.tenant_id=delivery.tenant_id AND outbox.stream_seq=delivery.stream_seq "
                "WHERE delivery.tenant_id=%s AND delivery.target_id=%s AND outbox.event_id=%s "
                "FOR UPDATE OF delivery",
                (tenant_id, delivery.target_id, delivery.event.event_id),
            )
            row = cur.fetchone()
            if row is None:
                return
            stream_seq = self._require_int(row[0], "stream_seq", minimum=1)
            state = self._require_str(row[1], "state")
            lease_owner = row[2]
            if state == "delivered":
                return
            if state != "leased":
                raise SyncDeliveryBusy("delivery lease is no longer active")
            lease_owner = self._require_str(lease_owner, "lease_owner")
            if lease_owner != delivery.lease_id:
                raise SyncDeliveryBusy("delivery lease identity mismatch")
            lease_until = self._require_datetime(row[3], "lease_until")
            if lease_until < datetime.now(UTC):
                raise SyncDeliveryBusy("delivery lease is no longer active")

            cur.execute(
                "UPDATE memplex_sync_deliveries "
                "SET state='delivered', lease_owner=NULL, lease_until=NULL, last_error_code=NULL "
                "WHERE tenant_id=%s AND target_id=%s AND stream_seq=%s "
                "AND lease_owner=%s AND state='leased' AND lease_until > %s",
                (tenant_id, delivery.target_id, stream_seq, delivery.lease_id, datetime.now(UTC)),
            )
            if cur.rowcount < 1:
                raise SyncDeliveryBusy("delivery lease is no longer active")

    def sync_ack_batch(
        self,
        deliveries: list[SyncDelivery],
        receipts: tuple[SyncReceipt, ...],
    ) -> None:
        if type(deliveries) is not list or not all(
            type(item) is SyncDelivery for item in deliveries
        ):
            raise TypeError("deliveries must be a list of SyncDelivery")
        if type(receipts) is not tuple or not all(
            type(item) is SyncReceipt for item in receipts
        ):
            raise TypeError("receipts must be a tuple of SyncReceipt")
        if len(deliveries) != len(receipts):
            raise ValueError("delivery and receipt cardinality mismatch")
        receipt_ids = {item.event_id for item in receipts}
        delivery_ids = {item.event.event_id for item in deliveries}
        if len(receipt_ids) != len(receipts) or receipt_ids != delivery_ids:
            raise ValueError("delivery and receipt identity mismatch")

        context = self._authorization_context()
        tenant_id = context.principal.tenant_id
        with self._pool.transaction(
            self._store._bind_transaction_scope, context
        ) as (_, cur):
            locked: list[tuple[SyncDelivery, int, bool]] = []
            for delivery in deliveries:
                cur.execute(
                    "SELECT delivery.stream_seq, delivery.state, "
                    "delivery.lease_owner, delivery.lease_until "
                    "FROM memplex_sync_deliveries AS delivery "
                    "JOIN memplex_sync_outbox AS outbox "
                    "  ON outbox.tenant_id=delivery.tenant_id "
                    " AND outbox.stream_seq=delivery.stream_seq "
                    "WHERE delivery.tenant_id=%s AND delivery.target_id=%s "
                    "AND outbox.event_id=%s FOR UPDATE OF delivery",
                    (
                        tenant_id,
                        delivery.target_id,
                        delivery.event.event_id,
                    ),
                )
                row = cur.fetchone()
                if row is None:
                    locked.append((delivery, 0, True))
                    continue
                stream_seq = self._require_int(
                    row[0], "stream_seq", minimum=1
                )
                state = self._require_str(row[1], "state")
                if state == "delivered":
                    locked.append((delivery, stream_seq, True))
                    continue
                if state != "leased":
                    raise SyncDeliveryBusy(
                        "delivery lease is no longer active"
                    )
                lease_owner = self._require_str(row[2], "lease_owner")
                if lease_owner != delivery.lease_id:
                    raise SyncDeliveryBusy("delivery lease identity mismatch")
                if self._require_datetime(
                    row[3], "lease_until"
                ) < datetime.now(UTC):
                    raise SyncDeliveryBusy(
                        "delivery lease is no longer active"
                    )
                locked.append((delivery, stream_seq, False))

            for delivery, stream_seq, already_done in locked:
                if already_done:
                    continue
                cur.execute(
                    "UPDATE memplex_sync_deliveries "
                    "SET state='delivered', lease_owner=NULL, "
                    "lease_until=NULL, last_error_code=NULL "
                    "WHERE tenant_id=%s AND target_id=%s "
                    "AND stream_seq=%s AND lease_owner=%s "
                    "AND state='leased'",
                    (
                        tenant_id,
                        delivery.target_id,
                        stream_seq,
                        delivery.lease_id,
                    ),
                )
                if cur.rowcount < 1:
                    raise SyncDeliveryBusy(
                        "delivery lease is no longer active"
                    )

    def sync_fail(self, delivery: SyncDelivery, error_code: str, now: datetime) -> None:
        if type(delivery) is not SyncDelivery:
            raise TypeError("delivery must be SyncDelivery")
        error_code = self._require_str(error_code, "error_code")
        now = self._require_datetime(now, "now")

        context = self._authorization_context()
        tenant_id = context.principal.tenant_id
        with self._pool.transaction(self._store._bind_transaction_scope, context) as (_, cur):
            cur.execute(
                "SELECT delivery.stream_seq, delivery.state, delivery.lease_owner, "
                "delivery.lease_until, delivery.attempt_count "
                "FROM memplex_sync_deliveries AS delivery "
                "JOIN memplex_sync_outbox AS outbox "
                "  ON outbox.tenant_id=delivery.tenant_id AND outbox.stream_seq=delivery.stream_seq "
                "WHERE delivery.tenant_id=%s AND delivery.target_id=%s AND outbox.event_id=%s "
                "FOR UPDATE OF delivery",
                (tenant_id, delivery.target_id, delivery.event.event_id),
            )
            row = cur.fetchone()
            if row is None:
                return
            stream_seq = self._require_int(row[0], "stream_seq", minimum=1)
            state = self._require_str(row[1], "state")
            lease_owner = row[2]
            attempt_count = self._require_int(row[4], "attempt_count")
            if state != "leased":
                if lease_owner is None:
                    return
                raise SyncDeliveryBusy("delivery lease is no longer active")
            lease_owner = self._require_str(lease_owner, "lease_owner")
            if lease_owner != delivery.lease_id:
                raise SyncDeliveryBusy("delivery lease identity mismatch")
            lease_until = self._require_datetime(row[3], "lease_until")
            if lease_until < datetime.now(UTC):
                raise SyncDeliveryBusy("delivery lease is no longer active")

            if attempt_count >= self._max_attempts:
                cur.execute(
                    "UPDATE memplex_sync_deliveries "
                    "SET state='dead_letter', lease_owner=NULL, lease_until=NULL, "
                    "attempt_count=%s, next_attempt_at=%s, last_error_code=%s "
                    "WHERE tenant_id=%s AND target_id=%s AND stream_seq=%s",
                    (attempt_count, now, error_code, tenant_id, delivery.target_id, stream_seq),
                )
                if cur.rowcount < 1:
                    raise SyncDeliveryBusy("delivery lease is no longer active")
                return

            backoff_seconds = min(60, 2 ** max(attempt_count - 1, 0))
            cur.execute(
                "UPDATE memplex_sync_deliveries "
                "SET state='pending', lease_owner=NULL, lease_until=NULL, "
                "next_attempt_at=%s, last_error_code=%s "
                "WHERE tenant_id=%s AND target_id=%s AND stream_seq=%s",
                (now + timedelta(seconds=backoff_seconds), error_code, tenant_id, delivery.target_id, stream_seq),
            )
            if cur.rowcount < 1:
                raise SyncDeliveryBusy("delivery lease is no longer active")

    def sync_dead_letter(
        self, delivery: SyncDelivery, error_code: str, now: datetime
    ) -> None:
        if type(delivery) is not SyncDelivery:
            raise TypeError("delivery must be SyncDelivery")
        error_code = self._require_str(error_code, "error_code")
        now = self._require_datetime(now, "now")

        context = self._authorization_context()
        tenant_id = context.principal.tenant_id
        with self._pool.transaction(
            self._store._bind_transaction_scope, context
        ) as (_, cur):
            cur.execute(
                "SELECT delivery.stream_seq, delivery.state, delivery.lease_owner, "
                "delivery.lease_until "
                "FROM memplex_sync_deliveries AS delivery "
                "JOIN memplex_sync_outbox AS outbox "
                "  ON outbox.tenant_id=delivery.tenant_id "
                " AND outbox.stream_seq=delivery.stream_seq "
                "WHERE delivery.tenant_id=%s AND delivery.target_id=%s "
                "AND outbox.event_id=%s FOR UPDATE OF delivery",
                (tenant_id, delivery.target_id, delivery.event.event_id),
            )
            row = cur.fetchone()
            if row is None:
                return
            stream_seq = self._require_int(row[0], "stream_seq", minimum=1)
            state = self._require_str(row[1], "state")
            if state == "dead_letter":
                return
            if state != "leased":
                raise SyncDeliveryBusy("delivery lease is no longer active")
            lease_owner = self._require_str(row[2], "lease_owner")
            if lease_owner != delivery.lease_id:
                raise SyncDeliveryBusy("delivery lease identity mismatch")
            lease_until = self._require_datetime(row[3], "lease_until")
            if lease_until < datetime.now(UTC):
                raise SyncDeliveryBusy("delivery lease is no longer active")
            cur.execute(
                "UPDATE memplex_sync_deliveries "
                "SET state='dead_letter', lease_owner=NULL, lease_until=NULL, "
                "next_attempt_at=%s, last_error_code=%s "
                "WHERE tenant_id=%s AND target_id=%s AND stream_seq=%s "
                "AND lease_owner=%s AND state='leased'",
                (
                    now,
                    error_code,
                    tenant_id,
                    delivery.target_id,
                    stream_seq,
                    delivery.lease_id,
                ),
            )
            if cur.rowcount < 1:
                raise SyncDeliveryBusy("delivery lease is no longer active")

    def sync_replay_dead_letter(self, target_id: str, event_id: str) -> bool:
        target_id = self._require_str(target_id, "target_id")
        event_id = self._require_str(event_id, "event_id")

        context = self._authorization_context()
        tenant_id = context.principal.tenant_id
        with self._pool.transaction(self._store._bind_transaction_scope, context) as (_, cur):
            cur.execute(
                "SELECT delivery.stream_seq "
                "FROM memplex_sync_deliveries AS delivery "
                "JOIN memplex_sync_outbox AS outbox "
                "  ON outbox.tenant_id=delivery.tenant_id AND outbox.stream_seq=delivery.stream_seq "
                "WHERE delivery.tenant_id=%s AND delivery.target_id=%s "
                "AND outbox.event_id=%s AND delivery.state='dead_letter' "
                "FOR UPDATE OF delivery",
                (tenant_id, target_id, event_id),
            )
            row = cur.fetchone()
            if row is None:
                return False
            stream_seq = self._require_int(row[0], "stream_seq", minimum=1)
            cur.execute(
                "UPDATE memplex_sync_deliveries "
                "SET state='pending', attempt_count=0, lease_owner=NULL, lease_until=NULL, "
                "next_attempt_at=clock_timestamp(), last_error_code=NULL "
                "WHERE tenant_id=%s AND target_id=%s AND stream_seq=%s AND state='dead_letter' "
                "RETURNING stream_seq",
                (tenant_id, target_id, stream_seq),
            )
            return cur.fetchone() is not None

    def sync_list_dead_letters(self, *, limit: int) -> list[SyncDeadLetterEntry]:
        limit = self._require_int(limit, "limit", minimum=1)
        context = self._authorization_context()
        tenant_id = context.principal.tenant_id
        with self._pool.transaction(
            self._store._bind_transaction_scope, context
        ) as (_, cur):
            cur.execute(
                "SELECT delivery.target_id, outbox.event_id, "
                "delivery.attempt_count, delivery.last_error_code "
                "FROM memplex_sync_deliveries AS delivery "
                "JOIN memplex_sync_outbox AS outbox "
                "  ON outbox.tenant_id=delivery.tenant_id "
                " AND outbox.stream_seq=delivery.stream_seq "
                "WHERE delivery.tenant_id=%s "
                "AND delivery.state='dead_letter' "
                "ORDER BY delivery.target_id, delivery.stream_seq LIMIT %s",
                (tenant_id, limit),
            )
            return [
                SyncDeadLetterEntry(
                    self._require_str(row[0], "target_id"),
                    self._require_str(row[1], "event_id"),
                    self._require_int(row[2], "attempt", minimum=1),
                    (
                        "delivery_failed"
                        if row[3] is None
                        else self._require_str(row[3], "error_code")
                    ),
                )
                for row in cur.fetchall()
            ]

    def sync_set_target_enabled(self, target_id: str, enabled: bool) -> None:
        target_id = self._require_str(target_id, "target_id")
        enabled = self._require_bool(enabled, "enabled")

        context = self._authorization_context()
        tenant_id = context.principal.tenant_id
        with self._pool.transaction(self._store._bind_transaction_scope, context) as (_, cur):
            cur.execute(
                "UPDATE memplex_sync_targets SET enabled = %s WHERE tenant_id=%s AND target_id=%s",
                (enabled, tenant_id, target_id),
            )
            if cur.rowcount < 1:
                raise ValueError("target not found")

    def sync_status(self) -> SyncStatus:
        context = self._authorization_context()
        tenant_id = context.principal.tenant_id
        with self._pool.transaction(self._store._bind_transaction_scope, context) as (_, cur):
            cur.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM memplex_sync_deliveries WHERE tenant_id=%s AND state='pending'), "
                "(SELECT COUNT(*) FROM memplex_sync_deliveries WHERE tenant_id=%s AND state='leased'), "
                "(SELECT COUNT(*) FROM memplex_sync_deliveries WHERE tenant_id=%s AND state='delivered'), "
                "(SELECT COUNT(*) FROM memplex_sync_targets WHERE tenant_id=%s AND enabled = FALSE), "
                "(SELECT COUNT(*) FROM memplex_sync_deliveries WHERE tenant_id=%s AND state='dead_letter')",
                (tenant_id, tenant_id, tenant_id, tenant_id, tenant_id),
            )
            row = cur.fetchone() or (0, 0, 0, 0, 0)
            return SyncStatus(
                pending=self._require_int(row[0], "pending"),
                leased=self._require_int(row[1], "leased"),
                delivered=self._require_int(row[2], "delivered"),
                disabled_targets=self._require_int(row[3], "disabled_targets"),
                dead_letters=self._require_int(row[4], "dead_letters"),
            )

    def sync_dispatch_status(self) -> SyncStatus:
        context = self._authorization_context()
        tenant_id = context.principal.tenant_id
        with self._pool.transaction(
            self._store._bind_transaction_scope, context
        ) as (_, cur):
            cur.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM memplex_sync_deliveries AS delivery "
                " JOIN memplex_sync_outbox AS outbox USING (tenant_id, stream_seq) "
                " WHERE delivery.tenant_id=%s AND delivery.state='pending' "
                " AND outbox.origin_node_id=%s), "
                "(SELECT COUNT(*) FROM memplex_sync_deliveries AS delivery "
                " JOIN memplex_sync_outbox AS outbox USING (tenant_id, stream_seq) "
                " WHERE delivery.tenant_id=%s AND delivery.state='leased' "
                " AND outbox.origin_node_id=%s), "
                "(SELECT COUNT(*) FROM memplex_sync_deliveries AS delivery "
                " JOIN memplex_sync_outbox AS outbox USING (tenant_id, stream_seq) "
                " WHERE delivery.tenant_id=%s AND delivery.state='delivered' "
                " AND outbox.origin_node_id=%s), "
                "(SELECT COUNT(*) FROM memplex_sync_targets "
                " WHERE tenant_id=%s AND enabled=FALSE), "
                "(SELECT COUNT(*) FROM memplex_sync_deliveries AS delivery "
                " JOIN memplex_sync_outbox AS outbox USING (tenant_id, stream_seq) "
                " WHERE delivery.tenant_id=%s AND delivery.state='dead_letter' "
                " AND outbox.origin_node_id=%s)",
                (
                    tenant_id,
                    self._local_node_id,
                    tenant_id,
                    self._local_node_id,
                    tenant_id,
                    self._local_node_id,
                    tenant_id,
                    tenant_id,
                    self._local_node_id,
                ),
            )
            row = cur.fetchone() or (0, 0, 0, 0, 0)
            return SyncStatus(
                pending=self._require_int(row[0], "pending"),
                leased=self._require_int(row[1], "leased"),
                delivered=self._require_int(row[2], "delivered"),
                disabled_targets=self._require_int(
                    row[3], "disabled_targets"
                ),
                dead_letters=self._require_int(row[4], "dead_letters"),
            )

    def sync_compact(self, now: datetime, *, limit: int) -> int:
        now = self._require_datetime(now, "now")
        limit = self._require_int(limit, "limit", minimum=1)
        context = self._authorization_context()
        retention_before = now - timedelta(seconds=self._retention_min_seconds)
        consumer_cutoff = now - timedelta(seconds=self._consumer_ttl_seconds)
        with self._pool.transaction(self._store._bind_transaction_scope, context) as (_, cur):
            cur.execute(
                "SELECT memplex_sync_compact(%s, %s, %s)",
                (retention_before, consumer_cutoff, limit),
            )
            return self._require_int(
                (cur.fetchone() or (0,))[0], "compacted_rows"
            )
