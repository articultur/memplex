"""FeedbackStore -- three-tier feedback persistence.

Tiers:
    Lite     -- in-memory dict + JSON file
    SQLite   -- SQLite database (connection lazily created)
    Postgres -- PostgreSQL backend (synchronous psycopg2, service-owned pool)

Usage::

    store = create_feedback_store("lite")
    store.record(MemoryFeedback(...))
"""

from __future__ import annotations

import json
import logging
import tempfile
from collections.abc import Mapping
from contextvars import ContextVar
from datetime import UTC, datetime, timezone
from functools import wraps
from pathlib import Path
from threading import RLock
from typing import Any, Protocol, runtime_checkable

from memplex.auth import (
    AuthorizationContext,
    IdentityClaimError,
    local_development_context,
)
from memplex.models import (
    FeedbackVerdict,
    MemoryFeedback,
    PendingReview,
)
from memplex.storage.pool import ReadyPostgresPool, validate_ready_postgres_pool

logger = logging.getLogger(__name__)


# Store instances can be shared by concurrent service requests.  A ContextVar
# keeps each authorized facade's immutable request identity in the executing
# thread/task instead of storing it on the shared backend instance.
_FEEDBACK_SCOPE: ContextVar[AuthorizationContext | None] = ContextVar(
    "memplex_feedback_scope", default=None
)


class _AuthorizedFeedbackStore:
    """A request-scoped facade that never mutates the underlying store."""

    def __init__(self, store: Any, context: AuthorizationContext) -> None:
        self._store = store
        self._context = context

    def __getattr__(self, name: str) -> Any:
        target = getattr(self._store, name)
        if not callable(target):
            return target

        @wraps(target)
        def scoped_call(*args: Any, **kwargs: Any) -> Any:
            token = _FEEDBACK_SCOPE.set(self._context)
            try:
                return target(*args, **kwargs)
            finally:
                _FEEDBACK_SCOPE.reset(token)

        return scoped_call


def _connection_locked(method: Any) -> Any:
    """Serialize one complete database operation on a shared connection.

    PostgreSQL transaction-local identity and SQLite transactions belong to
    the connection, not the calling thread.  The critical section therefore
    spans connection setup, scope binding, application SQL, fetches, and the
    final commit/rollback.
    """

    @wraps(method)
    def locked(self: Any, *args: Any, **kwargs: Any) -> Any:
        with self._connection_lock:
            return method(self, *args, **kwargs)

    return locked


def _feedback_context(require_authorization: bool) -> AuthorizationContext | None:
    """Return the active request context, preserving legacy local APIs."""
    context = _FEEDBACK_SCOPE.get()
    if context is not None:
        return context
    if require_authorization:
        raise PermissionError(
            "Feedback access requires an authorization context; "
            "use store.authorized(context).<operation>(...)"
        )
    return None


def _payload_claim_conflicts(value: object, expected: str) -> bool:
    if value is None:
        return False
    supplied = str(value).strip()
    return bool(supplied) and supplied != expected


_FEEDBACK_VISIBILITIES = frozenset({"user", "workspace", "session"})


def _feedback_visibility(value: object) -> str:
    visibility = str(value or "workspace").strip().lower()
    if visibility not in _FEEDBACK_VISIBILITIES:
        raise ValueError("feedback visibility must be one of: user, workspace, session")
    return visibility


def _feedback_is_visible(
    feedback: MemoryFeedback,
    context: AuthorizationContext,
) -> bool:
    """Apply the same fail-closed user/workspace/session ACL as memories."""
    if feedback.tenant_id != context.principal.tenant_id:
        return False
    try:
        visibility = _feedback_visibility(feedback.visibility)
    except ValueError:
        return False
    if visibility == "user":
        return feedback.owner_subject_id == context.principal.subject_id
    if visibility == "workspace":
        return feedback.workspace_id == context.workspace_id

    provenance = feedback.provenance or {}
    if not isinstance(provenance, Mapping):
        return False
    return (
        feedback.workspace_id == context.workspace_id
        and feedback.owner_subject_id == context.principal.subject_id
        and bool(context.agent_id)
        and bool(context.session_id)
        and bool(provenance.get("agent_id"))
        and bool(provenance.get("session_id"))
        and provenance.get("agent_id") == context.agent_id
        and provenance.get("session_id") == context.session_id
    )


def _sqlite_scope(context: AuthorizationContext) -> tuple[str, tuple[str, ...]]:
    """Return the parameterized SQLite equivalent of the memory ACL."""
    return (
        "tenant_id=? AND (" +
        "(visibility='user' AND owner_subject_id=?) " +
        "OR (visibility='workspace' AND workspace_id=?) " +
        "OR (visibility='session' AND workspace_id=? AND owner_subject_id=? " +
        "AND ?<>'' AND ?<>'' " +
        "AND COALESCE(json_extract(provenance, '$.agent_id'), '')<>'' " +
        "AND COALESCE(json_extract(provenance, '$.session_id'), '')<>'' " +
        "AND json_extract(provenance, '$.agent_id')=? "
        "AND json_extract(provenance, '$.session_id')=?))",
        (
            context.principal.tenant_id,
            context.principal.subject_id,
            context.workspace_id,
            context.workspace_id,
            context.principal.subject_id,
            context.agent_id,
            context.session_id,
            context.agent_id,
            context.session_id,
        ),
    )


def _bind_feedback_identity(
    feedback: MemoryFeedback,
    context: AuthorizationContext,
) -> MemoryFeedback:
    """Validate all caller claims before assigning trusted identity.

    The checks intentionally finish before mutating ``feedback``.  This is
    the feedback equivalent of ``bind_node_identity`` and prevents a rejected
    forged record from becoming partially scoped in memory.
    """
    if not isinstance(context, AuthorizationContext):
        raise TypeError("context must be an AuthorizationContext")
    if not isinstance(feedback.provenance, Mapping):
        raise TypeError("feedback provenance must be a mapping")

    principal = context.principal
    visibility = _feedback_visibility(feedback.visibility)
    if visibility == "session" and (not context.agent_id or not context.session_id):
        raise PermissionError(
            "session feedback requires non-empty agent_id and session_id"
        )
    expected_claims = {
        "tenant_id": principal.tenant_id,
        "owner_subject_id": principal.subject_id,
        "owner": principal.subject_id,
        "workspace_id": context.workspace_id,
    }
    conflicts = [
        field_name
        for field_name, expected in expected_claims.items()
        if _payload_claim_conflicts(getattr(feedback, field_name), expected)
    ]
    trusted_provenance = dict(context.provenance)
    trusted_provenance.update(
        {
            "agent_id": context.agent_id,
            "authentication_id": principal.authentication_id or "",
            "request_id": context.request_id,
            "session_id": context.session_id,
        }
    )
    conflicts.extend(
        f"provenance.{key}"
        for key, expected in trusted_provenance.items()
        if _payload_claim_conflicts(feedback.provenance.get(key), str(expected))
    )
    if conflicts:
        raise IdentityClaimError(
            "Invalid feedback identity claims: " + ", ".join(sorted(conflicts))
        )

    next_provenance = dict(feedback.provenance)
    next_provenance.update(trusted_provenance)
    feedback.tenant_id = principal.tenant_id
    feedback.owner_subject_id = principal.subject_id
    feedback.owner = principal.subject_id
    feedback.workspace_id = context.workspace_id
    feedback.visibility = visibility
    feedback.provenance = next_provenance
    return feedback


def _prepare_postgres_feedback(
    feedback: MemoryFeedback,
    *,
    require_authorization: bool,
) -> AuthorizationContext:
    """Scope a PostgreSQL write, including the explicit local-dev fallback."""
    context = _feedback_context(require_authorization)
    if context is not None:
        return_context = context
        _bind_feedback_identity(feedback, context)
        return return_context

    # PostgreSQL RLS needs a tenant even for the long-supported local API.
    # This is intentionally not a trusted remote identity binding: legacy
    # caller-facing ``owner`` remains unchanged while authoritative columns
    # become the explicit local-development principal.
    context = local_development_context()
    feedback.visibility = _feedback_visibility(feedback.visibility)
    feedback.tenant_id = feedback.tenant_id or context.principal.tenant_id
    feedback.owner_subject_id = feedback.owner_subject_id or context.principal.subject_id
    feedback.workspace_id = feedback.workspace_id or context.workspace_id
    return context


# ── Protocol ─────────────────────────────────────────────────────────


@runtime_checkable
class FeedbackStore(Protocol):
    """Feedback persistence interface."""

    def authorized(self, context: AuthorizationContext) -> FeedbackStore: ...

    def record(self, feedback: MemoryFeedback) -> None: ...

    def get_pending(self) -> list[PendingReview]: ...

    def resolve(self, memory_id: str, field_role: str, resolution: str) -> None: ...

    def get_history(self, memory_id: str, limit: int = 50) -> list[MemoryFeedback]: ...

    def clear(self) -> None: ...


# ── Serialization helpers ───────────────────────────────────────────


def _serialize_feedback(fb: MemoryFeedback) -> dict:
    ts: Any = fb.timestamp
    if isinstance(ts, datetime):
        ts = ts.isoformat()
    reviewed: Any = fb.needs_review_until
    if isinstance(reviewed, datetime):
        reviewed = reviewed.isoformat()
    resolved: Any = fb.resolved_at
    if isinstance(resolved, datetime):
        resolved = resolved.isoformat()
    return {
        "memory_id": fb.memory_id,
        "field_role": fb.field_role,
        "value_index": fb.value_index,
        "verdict": fb.verdict.value if isinstance(fb.verdict, FeedbackVerdict) else fb.verdict,
        "reason": fb.reason,
        "source": fb.source,
        "timestamp": ts,
        "owner": fb.owner,
        "feedback_type": fb.feedback_type,
        "old_value": fb.old_value,
        "new_value": fb.new_value,
        "needs_review": fb.needs_review,
        "needs_review_until": reviewed,
        "resolved_at": resolved,
        "resolution": fb.resolution,
        "tenant_id": fb.tenant_id,
        "owner_subject_id": fb.owner_subject_id,
        "workspace_id": fb.workspace_id,
        "visibility": fb.visibility,
        "provenance": fb.provenance,
    }


def _deserialize_feedback(d: dict) -> MemoryFeedback:
    verdict = d.get("verdict", "correct")
    if isinstance(verdict, str):
        try:
            verdict = FeedbackVerdict(verdict)
        except ValueError:
            verdict = FeedbackVerdict.CORRECT

    ts = d.get("timestamp")
    if isinstance(ts, str):
        ts = datetime.fromisoformat(ts)
    elif ts is None:
        ts = datetime.now(UTC)

    reviewed = d.get("needs_review_until")
    if isinstance(reviewed, str):
        reviewed = datetime.fromisoformat(reviewed)

    resolved = d.get("resolved_at")
    if isinstance(resolved, str):
        resolved = datetime.fromisoformat(resolved)

    return MemoryFeedback(
        memory_id=d["memory_id"],
        field_role=d.get("field_role", ""),
        value_index=d.get("value_index", 0),
        verdict=verdict,
        reason=d.get("reason"),
        source=d.get("source", "user"),
        timestamp=ts,
        owner=d.get("owner"),
        feedback_type=d.get("feedback_type", "field_value"),
        old_value=d.get("old_value"),
        new_value=d.get("new_value"),
        needs_review=d.get("needs_review", True),
        needs_review_until=reviewed,
        resolved_at=resolved,
        resolution=d.get("resolution"),
        tenant_id=d.get("tenant_id"),
        owner_subject_id=d.get("owner_subject_id"),
        workspace_id=d.get("workspace_id"),
        visibility=d.get("visibility", "workspace"),
        provenance=d.get("provenance") or {},
    )


# ── LiteFeedbackStore ────────────────────────────────────────────────


class LiteFeedbackStore:
    """In-memory dict + JSON persistence."""

    def __init__(
        self,
        path: Path | None = None,
        *,
        require_authorization: bool = False,
    ) -> None:
        self._path = path or Path("~/.memplex/feedback.json").expanduser()
        self._require_authorization = require_authorization
        self._records: list[MemoryFeedback] = []
        self._load()

    def authorized(self, context: AuthorizationContext) -> _AuthorizedFeedbackStore:
        if not isinstance(context, AuthorizationContext):
            raise TypeError("context must be an AuthorizationContext")
        return _AuthorizedFeedbackStore(self, context)

    def _context(self) -> AuthorizationContext | None:
        return _feedback_context(self._require_authorization)

    def _visible(self, feedback: MemoryFeedback, context: AuthorizationContext | None) -> bool:
        if context is None:
            return True
        # Historic Lite JSON has no tenant metadata.  Keep that development
        # compatibility path for callers that still enforce visibility via the
        # associated memory, but never expose such records in strict mode.
        if feedback.tenant_id is None:
            return not self._require_authorization
        return _feedback_is_visible(feedback, context)

    def record(self, feedback: MemoryFeedback) -> None:
        context = self._context()
        if context is not None:
            _bind_feedback_identity(feedback, context)
        self._records.append(feedback)
        self._save()

    def get_pending(self) -> list[PendingReview]:
        context = self._context()
        groups: dict[str, list[MemoryFeedback]] = {}
        for fb in self._records:
            if (
                not self._visible(fb, context)
                or not fb.needs_review
                or fb.resolved_at is not None
            ):
                continue
            key = f"{fb.memory_id}:{fb.field_role}"
            groups.setdefault(key, []).append(fb)

        pending: list[PendingReview] = []
        for key, fbs in groups.items():
            mem_id, role = key.split(":", 1)
            pending.append(
                PendingReview(
                    memory_id=mem_id,
                    field_role=role,
                    conflicting_values=[],  # Populated by caller with actual FieldValues
                    detected_at=fbs[0].timestamp if fbs else None,
                    source=fbs[0].source if fbs else "",
                )
            )
        return pending

    def resolve(self, memory_id: str, field_role: str, resolution: str) -> None:
        context = self._context()
        for fb in self._records:
            if (
                fb.memory_id == memory_id
                and fb.field_role == field_role
                and self._visible(fb, context)
                and fb.needs_review
                and fb.resolved_at is None
            ):
                fb.needs_review = False
                fb.resolved_at = datetime.now(UTC)
                fb.resolution = resolution
        self._save()

    def get_history(self, memory_id: str, limit: int = 50) -> list[MemoryFeedback]:
        context = self._context()
        matching = [
            fb
            for fb in self._records
            if fb.memory_id == memory_id and self._visible(fb, context)
        ]
        matching.sort(
            key=lambda fb: fb.timestamp if isinstance(fb.timestamp, datetime) else datetime.min,  # noqa: DTZ901 - naive sentinel only orders non-datetime fallbacks
            reverse=True,
        )
        return matching[:limit]

    def clear(self) -> None:
        context = self._context()
        if context is None:
            self._records.clear()
        else:
            self._records = [
                fb for fb in self._records if not self._visible(fb, context)
            ]
        self._save()

    # ── Persistence ──────────────────────────────────────────────

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._records = [_deserialize_feedback(d) for d in raw]
        except Exception:  # noqa: BLE001 - logged degradation path
            logger.warning("Failed to load feedback from %s", self._path)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = [_serialize_feedback(fb) for fb in self._records]
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with open(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            Path(tmp_path).replace(self._path)
        except Exception:
            Path(tmp_path).unlink(missing_ok=True)
            raise


# ── SQLiteFeedbackStore ──────────────────────────────────────────────


class SQLiteFeedbackStore:
    """SQLite-backed feedback store.  Connection is created lazily."""

    _COLUMNS = (
        "memory_id, field_role, value_index, verdict, reason, source, timestamp, "
        "owner, feedback_type, old_value, new_value, needs_review, "
        "needs_review_until, resolved_at, resolution, tenant_id, "
        "owner_subject_id, workspace_id, visibility, provenance"
    )

    def __init__(
        self,
        db_path: str | None = None,
        *,
        require_authorization: bool = False,
    ) -> None:
        self._db_path = db_path or str(Path("~/.memplex/feedback.db").expanduser())
        self._require_authorization = require_authorization
        self._connection_lock = RLock()
        self._conn: Any = None

    def authorized(self, context: AuthorizationContext) -> _AuthorizedFeedbackStore:
        if not isinstance(context, AuthorizationContext):
            raise TypeError("context must be an AuthorizationContext")
        return _AuthorizedFeedbackStore(self, context)

    def _context(self) -> AuthorizationContext | None:
        return _feedback_context(self._require_authorization)

    @_connection_locked
    def _ensure_conn(self) -> None:
        if self._conn is not None:
            return
        import sqlite3

        self._conn = sqlite3.connect(self._db_path, check_same_thread=False)
        self._conn.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                memory_id   TEXT NOT NULL,
                field_role  TEXT NOT NULL,
                value_index INTEGER DEFAULT 0,
                verdict     TEXT NOT NULL,
                reason      TEXT,
                source      TEXT DEFAULT 'user',
                timestamp   TEXT,
                owner       TEXT,
                feedback_type TEXT DEFAULT 'field_value',
                old_value   TEXT,
                new_value   TEXT,
                needs_review INTEGER DEFAULT 1,
                needs_review_until TEXT,
                resolved_at TEXT,
                resolution  TEXT,
                tenant_id TEXT,
                owner_subject_id TEXT,
                workspace_id TEXT,
                visibility TEXT DEFAULT 'workspace',
                provenance TEXT DEFAULT '{}'
            )
        """)
        existing_columns = {
            row[1] for row in self._conn.execute("PRAGMA table_info(feedback)").fetchall()
        }
        # SQLite has no ADD COLUMN IF NOT EXISTS.  Preserve historic rows
        # (whose new fields remain NULL and are invisible to authorized calls).
        additions = {
            "tenant_id": "TEXT",
            "owner_subject_id": "TEXT",
            "workspace_id": "TEXT",
            "visibility": "TEXT DEFAULT 'workspace'",
            "provenance": "TEXT DEFAULT '{}'",
        }
        for name, definition in additions.items():
            if name not in existing_columns:
                self._conn.execute(f"ALTER TABLE feedback ADD COLUMN {name} {definition}")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS feedback_tenant_memory_idx "
            "ON feedback(tenant_id, memory_id, timestamp DESC)"
        )
        self._conn.commit()

    @_connection_locked
    def record(self, feedback: MemoryFeedback) -> None:
        self._ensure_conn()
        context = self._context()
        if context is not None:
            _bind_feedback_identity(feedback, context)
        self._conn.execute(
            f"INSERT INTO feedback ({self._COLUMNS}) VALUES ({','.join('?' for _ in range(20))})",
            (
                feedback.memory_id,
                feedback.field_role,
                feedback.value_index,
                feedback.verdict.value
                if isinstance(feedback.verdict, FeedbackVerdict)
                else feedback.verdict,
                feedback.reason,
                feedback.source,
                feedback.timestamp.isoformat()
                if isinstance(feedback.timestamp, datetime)
                else feedback.timestamp,
                feedback.owner,
                feedback.feedback_type,
                feedback.old_value,
                feedback.new_value,
                1 if feedback.needs_review else 0,
                feedback.needs_review_until.isoformat()
                if isinstance(feedback.needs_review_until, datetime)
                else feedback.needs_review_until,
                feedback.resolved_at.isoformat()
                if isinstance(feedback.resolved_at, datetime)
                else feedback.resolved_at,
                feedback.resolution,
                feedback.tenant_id,
                feedback.owner_subject_id,
                feedback.workspace_id,
                feedback.visibility,
                json.dumps(feedback.provenance, ensure_ascii=False, sort_keys=True),
            ),
        )
        self._conn.commit()

    @_connection_locked
    def get_pending(self) -> list[PendingReview]:
        self._ensure_conn()
        context = self._context()
        clauses = ["needs_review=1", "resolved_at IS NULL"]
        params: list[Any] = []
        if context is not None:
            scope, scope_params = _sqlite_scope(context)
            clauses.append(scope)
            params.extend(scope_params)
        rows = self._conn.execute(
            "SELECT DISTINCT memory_id, field_role, source, MIN(timestamp) "
            f"FROM feedback WHERE {' AND '.join(clauses)} "
            "GROUP BY memory_id, field_role, source",
            params,
        ).fetchall()
        return [
            PendingReview(
                memory_id=r[0],
                field_role=r[1],
                detected_at=r[3],
                source=r[2] or "",
            )
            for r in rows
        ]

    @_connection_locked
    def resolve(self, memory_id: str, field_role: str, resolution: str) -> None:
        self._ensure_conn()
        context = self._context()
        now = datetime.now(UTC).isoformat()
        clauses = [
            "memory_id=?",
            "field_role=?",
            "needs_review=1",
            "resolved_at IS NULL",
        ]
        params: list[Any] = [now, resolution, memory_id, field_role]
        if context is not None:
            scope, scope_params = _sqlite_scope(context)
            clauses.append(scope)
            params.extend(scope_params)
        self._conn.execute(
            "UPDATE feedback SET needs_review=0, resolved_at=?, resolution=? "
            f"WHERE {' AND '.join(clauses)}",
            params,
        )
        self._conn.commit()

    @_connection_locked
    def get_history(self, memory_id: str, limit: int = 50) -> list[MemoryFeedback]:
        self._ensure_conn()
        context = self._context()
        clauses = ["memory_id=?"]
        params: list[Any] = [memory_id]
        if context is not None:
            scope, scope_params = _sqlite_scope(context)
            clauses.append(scope)
            params.extend(scope_params)
        params.append(limit)
        rows = self._conn.execute(
            f"SELECT {self._COLUMNS} FROM feedback WHERE {' AND '.join(clauses)} "
            "ORDER BY timestamp DESC LIMIT ?",
            params,
        ).fetchall()
        return [self._row_to_feedback(r) for r in rows]

    @_connection_locked
    def clear(self) -> None:
        self._ensure_conn()
        context = self._context()
        if context is None:
            self._conn.execute("DELETE FROM feedback")
        else:
            scope, scope_params = _sqlite_scope(context)
            self._conn.execute(
                f"DELETE FROM feedback WHERE {scope}",
                scope_params,
            )
        self._conn.commit()

    @staticmethod
    def _row_to_feedback(r: tuple) -> MemoryFeedback:
        return MemoryFeedback(
            memory_id=r[0],
            field_role=r[1],
            value_index=r[2],
            verdict=FeedbackVerdict(r[3]),
            reason=r[4],
            source=r[5] or "user",
            timestamp=datetime.fromisoformat(r[6]) if r[6] else datetime.now(UTC),
            owner=r[7],
            feedback_type=r[8] or "field_value",
            old_value=r[9],
            new_value=r[10],
            needs_review=bool(r[11]),
            needs_review_until=datetime.fromisoformat(r[12]) if r[12] else None,
            resolved_at=datetime.fromisoformat(r[13]) if r[13] else None,
            resolution=r[14],
            tenant_id=r[15],
            owner_subject_id=r[16],
            workspace_id=r[17],
            visibility=r[18] or "workspace",
            provenance=json.loads(r[19]) if r[19] else {},
        )


# ── PostgresFeedbackStore ────────────────────────────────────────────


class PostgresFeedbackStore:
    """PostgreSQL-backed feedback store (synchronous, psycopg2).

    The ``FeedbackStore`` protocol is synchronous (see the service call
    sites), so this backend uses psycopg2 -- matching
    ``PostgresMemoryStore``. The connection is opened lazily on first
    use, so importing or constructing never requires a database.
    """

    _COLUMNS = (
        "memory_id, field_role, value_index, verdict, reason, source, timestamp, "
        "owner, feedback_type, old_value, new_value, needs_review, "
        "needs_review_until, resolved_at, resolution, tenant_id, "
        "owner_subject_id, workspace_id, visibility, provenance"
    )

    def __init__(
        self,
        dsn: str = "",
        *,
        require_authorization: bool = False,
        ready_pool: ReadyPostgresPool | None = None,
        **legacy: Any,
    ) -> None:
        if legacy:
            raise TypeError(
                "PostgresFeedbackStore requires a resource-issued ReadyPostgresPool"
            )
        ready_pool = validate_ready_postgres_pool(ready_pool)
        self._dsn = dsn
        self._require_authorization = require_authorization
        self._ready_pool = ready_pool
        self._pool_manager = ready_pool.manager

    def authorized(self, context: AuthorizationContext) -> _AuthorizedFeedbackStore:
        if not isinstance(context, AuthorizationContext):
            raise TypeError("context must be an AuthorizationContext")
        return _AuthorizedFeedbackStore(self, context)

    def _context(self) -> AuthorizationContext:
        context = _feedback_context(self._require_authorization)
        return context or local_development_context()

    @staticmethod
    def _context_values(context: AuthorizationContext) -> tuple[str, str, str, str, str]:
        return (
            context.principal.tenant_id,
            context.principal.subject_id,
            context.workspace_id,
            context.agent_id,
            context.session_id,
        )

    def _bind_transaction_scope(self, cur: Any, context: AuthorizationContext) -> None:
        """Set RLS values transaction-locally before feedback application SQL."""
        cur.execute(
            "SELECT "
            "set_config('memplex.tenant_id', %s, true), "
            "set_config('memplex.subject_id', %s, true), "
            "set_config('memplex.workspace_id', %s, true), "
            "set_config('memplex.agent_id', %s, true), "
            "set_config('memplex.session_id', %s, true)",
            self._context_values(context),
        )

    @staticmethod
    def _scope_predicate(
        context: AuthorizationContext,
    ) -> tuple[str, tuple[str, ...]]:
        """Return the explicit ACL predicate for a feedback operation.

        The legacy unscoped interface is fixed to the known local-development
        identity, so it can retain its historic application bind positions
        without interpolating caller data.  Authenticated calls always use
        regular SQL parameters from the immutable request context.
        """
        if _FEEDBACK_SCOPE.get() is None:
            return (
                "tenant_id='local' AND (" +
                "(visibility='user' AND owner_subject_id='local-development') " +
                "OR (visibility='workspace' AND workspace_id='local-development') " +
                "OR (visibility='session' " +
                "AND workspace_id='local-development' " +
                "AND owner_subject_id='local-development' " +
                "AND provenance->>'agent_id'='memplex' "
                "AND provenance->>'session_id'='local-development'))",
                (),
            )
        return (
            "tenant_id=%s AND (" +
            "(visibility='user' AND owner_subject_id=%s) " +
            "OR (visibility='workspace' AND workspace_id=%s) " +
            "OR (visibility='session' AND workspace_id=%s AND owner_subject_id=%s " +
            "AND %s<>'' AND %s<>'' " +
            "AND COALESCE(provenance->>'agent_id', '')<>'' " +
            "AND COALESCE(provenance->>'session_id', '')<>'' " +
            "AND provenance->>'agent_id'=%s "
            "AND provenance->>'session_id'=%s))",
            (
                context.principal.tenant_id,
                context.principal.subject_id,
                context.workspace_id,
                context.workspace_id,
                context.principal.subject_id,
                context.agent_id,
                context.session_id,
                context.agent_id,
                context.session_id,
            ),
        )

    @staticmethod
    def _execute_in_transaction(cur: Any, sql: str, params: tuple = ()) -> None:
        """Execute one feedback statement on the public operation's cursor.

        Keeping this narrow seam makes the transaction boundary testable and
        prevents a future helper from silently leasing or committing halfway
        through ``record``, ``resolve`` or ``clear``.
        """
        cur.execute(sql, params)

    def record(self, feedback: MemoryFeedback) -> None:
        context = _prepare_postgres_feedback(
            feedback,
            require_authorization=self._require_authorization,
        )
        with self._pool_manager.transaction(self._bind_transaction_scope, context) as (_, cur):
            self._execute_in_transaction(
                cur,
                f"INSERT INTO feedback ({self._COLUMNS}) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)",
                (
                    feedback.memory_id,
                    feedback.field_role,
                    feedback.value_index,
                    feedback.verdict.value
                    if isinstance(feedback.verdict, FeedbackVerdict)
                    else feedback.verdict,
                    feedback.reason,
                    feedback.source,
                    feedback.timestamp,
                    feedback.owner,
                    feedback.feedback_type,
                    feedback.old_value,
                    feedback.new_value,
                    feedback.needs_review,
                    feedback.needs_review_until,
                    feedback.resolved_at,
                    feedback.resolution,
                    feedback.tenant_id,
                    feedback.owner_subject_id,
                    feedback.workspace_id,
                    feedback.visibility,
                    json.dumps(feedback.provenance, ensure_ascii=False, sort_keys=True),
                ),
            )

    def get_pending(self) -> list[PendingReview]:
        context = self._context()
        scope_predicate, scope_params = self._scope_predicate(context)
        cur = self._pool_manager.read_cursor(self._bind_transaction_scope, context)
        try:
            cur.execute(
                "SELECT memory_id, field_role, source, MIN(timestamp) "
                f"FROM feedback WHERE {scope_predicate} AND needs_review=TRUE "
                "AND resolved_at IS NULL "
                "GROUP BY memory_id, field_role, source",
                scope_params,
            )
            rows = cur.fetchall()
        except BaseException:
            try:
                cur.close()
            except BaseException as exc:  # noqa: BLE001 - shutdown/cleanup semantics; primary error stays authoritative
                logger.debug("suppressed BaseException in cleanup/degradation path: %s", exc)
            raise
        else:
            cur.close()
        return [
            PendingReview(
                memory_id=r[0],
                field_role=r[1],
                detected_at=r[3],
                source=r[2] or "",
            )
            for r in rows
        ]

    def resolve(self, memory_id: str, field_role: str, resolution: str) -> None:
        context = self._context()
        scope_predicate, scope_params = self._scope_predicate(context)
        with self._pool_manager.transaction(self._bind_transaction_scope, context) as (_, cur):
            self._execute_in_transaction(
                cur,
                "UPDATE feedback SET needs_review=FALSE, resolved_at=now(), resolution=%s "
                f"WHERE {scope_predicate} AND memory_id=%s AND field_role=%s "
                "AND needs_review=TRUE AND resolved_at IS NULL",
                (
                    resolution,
                    *scope_params,
                    memory_id,
                    field_role,
                ),
            )

    def get_history(self, memory_id: str, limit: int = 50) -> list[MemoryFeedback]:
        context = self._context()
        scope_predicate, scope_params = self._scope_predicate(context)
        cur = self._pool_manager.read_cursor(self._bind_transaction_scope, context)
        try:
            cur.execute(
                f"SELECT {self._COLUMNS} FROM feedback "
                f"WHERE {scope_predicate} AND memory_id=%s ORDER BY timestamp DESC LIMIT %s",
                (
                    *scope_params,
                    memory_id,
                    limit,
                ),
            )
            rows = cur.fetchall()
        except BaseException:
            try:
                cur.close()
            except BaseException as exc:  # noqa: BLE001 - shutdown/cleanup semantics; primary error stays authoritative
                logger.debug("suppressed BaseException in cleanup/degradation path: %s", exc)
            raise
        else:
            cur.close()
        return [self._row_to_feedback(r) for r in rows]

    def clear(self) -> None:
        context = self._context()
        scope_predicate, scope_params = self._scope_predicate(context)
        with self._pool_manager.transaction(self._bind_transaction_scope, context) as (_, cur):
            self._execute_in_transaction(
                cur,
                f"DELETE FROM feedback WHERE {scope_predicate}",
                scope_params,
            )

    @staticmethod
    def _row_to_feedback(r: tuple) -> MemoryFeedback:
        return MemoryFeedback(
            memory_id=r[0],
            field_role=r[1],
            value_index=r[2],
            verdict=FeedbackVerdict(r[3]),
            reason=r[4],
            source=r[5] or "user",
            timestamp=r[6] or datetime.now(UTC),
            owner=r[7],
            feedback_type=r[8] or "field_value",
            old_value=r[9],
            new_value=r[10],
            needs_review=bool(r[11]),
            needs_review_until=r[12],
            resolved_at=r[13],
            resolution=r[14],
            # A 15-column tuple is the pre-ACL row shape used by old callers
            # and test doubles.  Real scoped reads select all authority
            # columns, while this fallback keeps historic deserialization
            # usable without making those rows visible in strict mode.
            tenant_id=r[15] if len(r) > 15 else None,
            owner_subject_id=r[16] if len(r) > 16 else None,
            workspace_id=r[17] if len(r) > 17 else None,
            visibility=(r[18] if len(r) > 18 else None) or "workspace",
            provenance=(r[19] if len(r) > 19 else None) or {},
        )


# ── Factory ──────────────────────────────────────────────────────────


def create_feedback_store(
    backend: str = "lite",
    **kwargs: Any,
) -> FeedbackStore:
    """Create a feedback store by backend name.

    Parameters
    ----------
    backend:
        ``"lite"`` | ``"sqlite"`` | ``"postgres"``
    """
    if backend == "lite":
        return LiteFeedbackStore(
            path=kwargs.get("path"),
            require_authorization=kwargs.get("require_authorization", False),
        )
    if backend == "sqlite":
        return SQLiteFeedbackStore(
            db_path=kwargs.get("db_path"),
            require_authorization=kwargs.get("require_authorization", False),
        )
    if backend == "postgres":
        ready_pool = kwargs.get("ready_pool")
        ready_pool = validate_ready_postgres_pool(ready_pool)
        return PostgresFeedbackStore(
            dsn=kwargs.get("dsn", ""),
            require_authorization=kwargs.get("require_authorization", False),
            ready_pool=ready_pool,
        )
    raise ValueError(f"Unknown feedback store backend: {backend!r}")
