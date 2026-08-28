"""PostgreSQL memory backend (R1 roadmap item).

Stores Functions as JSONB rows in a single ``memplex_functions`` table,
with a generated ``tsvector`` column for native PostgreSQL full-text
search (replacing the SQLite FTS5 sidecar used by the lite backend).
Edges and observations get their own tables.

Synchronous (psycopg2); the MemoryStore protocol is synchronous, so we
avoid asyncpg's async/sync bridging complexity. Connections are leased from
the service-owned ready pool only while a store operation is in progress.

Backend selection: ``create_store("postgres", path="dbname=memplex ...")``
or via config (``storage.backend = "postgres"``, ``storage.path`` = DSN).

Schema::

    CREATE TABLE IF NOT EXISTS memplex_functions (
        id          TEXT PRIMARY KEY,
        data        JSONB NOT NULL,
        updated_at  TIMESTAMPTZ,
        search_tsv  TSVECTOR GENERATED ALWAYS AS (
            to_tsvector('simple', coalesce(data->>'name','') || ' ' ||
                                 coalesce(data->>'domain','') || ' ' ||
                                 coalesce((data->>'trigger_text'),''))
        ) STORED
    );
    CREATE INDEX IF NOT EXISTS fts_functions_idx
        ON memplex_functions USING GIN (search_tsv);

The tsvector is generated from the JSONB so inserts/updates stay in sync
without triggers. For simplicity, trigger/action text is pre-flattened
into ``data->>'trigger_text'`` at write time by this backend.
"""

from __future__ import annotations

import json
import logging
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
from hashlib import sha256
from typing import Any, List, Optional

from memplex.auth import (
    AuthorizationContext,
    bind_node_identity,
    local_development_context,
)
from memplex.models import (
    BatchResult,
    ChangelogEvent,
    Fact,
    FieldValue,
    Function,
    GraphData,
    GraphEdge,
    MergeResult,
    Observation,
    Preference,
    SearchFilters,
    SearchResult,
    SourceDocument,
    SourceType,
    validate_belongs_to_edges,
    validate_domain,
    validate_func_id,
)
from memplex.storage.inbound import InboundSyncExecutor
from memplex.storage.pool import ReadyPostgresPool, validate_ready_postgres_pool
from memplex.sync_protocol import (
    SyncApplyResult,
    SyncBatch,
    SyncBatchResult,
    SyncCursorClaims,
    SyncDelivery,
    SyncEntityKey,
    SyncPage,
    SyncReceipt,
    SyncSnapshotPage,
    SyncStatus,
    SyncVersion,
)
from memplex.sync_repository import SyncCapturePolicy

logger = logging.getLogger(__name__)


class PostgresWriteRejected(RuntimeError):
    """Opaque failure when an ACL-qualified write affected no durable row."""


class FunctionWriteBusy(RuntimeError):
    """A tenant-scoped Function/edge write lock timed out; retry safely."""


_FUNCTION_WRITE_LOCK_VERSION = "memplex:function-write-lock:v1"
_FUNCTION_WRITE_LOCK_TIMEOUT = "5s"


def _function_write_lock_key(target: object, tenant_id: str) -> int:
    """Derive a stable signed bigint advisory-lock key without Python hashing.

    Length delimiters make the UTF-8 component framing unambiguous, while the
    version component gives future protocol changes a clean namespace.  The
    ready-pool target was verified during resource publication; it prevents
    same-named tenants in different database/schema deployments from sharing
    an advisory lock accidentally.
    """
    database = getattr(target, "database", None)
    schema = getattr(target, "schema", None)
    if (
        type(database) is not str
        or type(schema) is not str
        or type(tenant_id) is not str
    ):
        raise TypeError("function write lock requires a verified PostgreSQL target")
    components = (
        _FUNCTION_WRITE_LOCK_VERSION,
        database,
        schema,
        tenant_id,
    )
    payload = b"".join(
        len(component.encode("utf-8")).to_bytes(4, "big")
        + component.encode("utf-8")
        for component in components
    )
    return int.from_bytes(sha256(payload).digest()[:8], "big", signed=True)


def _acl_scope_sql(alias: str = "") -> str:
    """Return the fail-closed relational visibility predicate.

    This predicate is deliberately embedded in every data statement in
    addition to RLS.  PostgreSQL owners and test superusers may bypass RLS;
    application SQL must still preserve the same tenant and visibility
    boundary in those environments.
    """
    column = f"{alias}." if alias else ""
    tenant = "current_setting('memplex.tenant_id', true)"
    subject = "current_setting('memplex.subject_id', true)"
    workspace = "current_setting('memplex.workspace_id', true)"
    agent = "current_setting('memplex.agent_id', true)"
    session = "current_setting('memplex.session_id', true)"
    return (
        f"{column}tenant_id <> '__memplex_legacy__' "
        f"AND {column}tenant_id = {tenant} "
        "AND ("
        f"({column}visibility = 'user' AND {column}owner_subject = {subject}) "
        f"OR ({column}visibility = 'workspace' AND {column}workspace = {workspace}) "
        f"OR ({column}visibility = 'session' "
        f"AND {column}workspace = {workspace} "
        f"AND {column}owner_subject = {subject} "
        f"AND NULLIF({agent}, '') IS NOT NULL "
        f"AND NULLIF({session}, '') IS NOT NULL "
        f"AND NULLIF({column}source_agent, '') IS NOT NULL "
        f"AND NULLIF({column}source_session, '') IS NOT NULL "
        f"AND {column}source_agent = {agent} "
        f"AND {column}source_session = {session}))"
    )


# The scope is intentionally a ContextVar rather than a store attribute.  A
# PostgreSQL store is shared by services and workers; putting identity on the
# store would let concurrent requests overwrite each other's tenant context.
_POSTGRES_SCOPE: ContextVar[AuthorizationContext | None] = ContextVar(
    "memplex_postgres_scope", default=None
)


class _AuthorizedPostgresStore:
    """Thread-safe, request-scoped facade returned by :meth:`authorized`.

    The facade never mutates the underlying store.  Each method invocation
    installs its immutable authorization context only for that execution
    context, and removes it even if the database operation raises.
    """

    def __init__(self, store: PostgresMemoryStore, context: AuthorizationContext) -> None:
        self._store = store
        self._context = context

    def __getattr__(self, name: str):
        target = getattr(self._store, name)
        if not callable(target):
            return target

        @wraps(target)
        def scoped_call(*args, **kwargs):
            token = _POSTGRES_SCOPE.set(self._context)
            try:
                return target(*args, **kwargs)
            finally:
                _POSTGRES_SCOPE.reset(token)

        return scoped_call



# ── Serialization helpers (mirror LiteMemoryStore shape, JSONB-safe) ──


def _func_to_json(func: Function) -> dict:
    """Flatten a Function into a JSONB-safe dict via the model's standard
    serializer, plus a pre-built search text field for the generated
    tsvector column (``trigger_text`` / ``action_text`` are PG-only keys,
    ignored by :meth:`Function.from_dict`)."""
    d = func.to_dict()
    d["trigger_text"] = " ".join(fv.desc for fv in func.trigger)
    d["action_text"] = " ".join(fv.desc for fv in func.action)
    return d


def _fv_to_json(fv: FieldValue) -> dict:
    """Delegate to the model standard serializer (kept as a thin wrapper:
    tests and this module historically import the private name)."""
    return fv.to_dict()


def _fv_from_json(d: dict) -> FieldValue:
    """Inverse of :func:`_fv_to_json` via the model standard deserializer.

    Note: the historical hand-rolled version defaulted a missing
    ``source_method`` to ``"manual"``; the standard (and the dataclass
    default) is ``"rule_based"``. Rows written by :func:`_fv_to_json`
    always carry the key, so the drift only affected legacy partial rows.
    """
    return FieldValue.from_dict(d)


def _iso(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _func_from_json(d: dict) -> Function:
    """Delegate to the model standard deserializer.

    Note: the historical hand-rolled version defaulted missing
    ``confidence`` to 0.5, ``domain`` to "" and derived
    ``name_normalized`` from ``name``; the standard (and the dataclass
    defaults) are 1.0 / None / "". Rows written by :func:`_func_to_json`
    always carry every key, so the drift only affected legacy partial
    rows.
    """
    return Function.from_dict(d)


def _obs_to_json(obs: Observation) -> dict:
    """Flatten an Observation into a JSONB-safe dict via the model's standard
    serializer (mirrors the lite backend). ``to_dict()`` covers every base
    field -- previously a hand-rolled subset silently dropped ``owner`` (and
    other MemoryNode fields), which broke ``list_observations(owner=...)``
    because the JSONB predicate never matched."""
    return obs.to_dict()


def _merge_field_values(
    existing: List[FieldValue],
    incoming: List[FieldValue],
) -> List[FieldValue]:
    """Merge incoming FieldValues into existing (dedup by desc), mirroring
    the lite backend's merge semantics (including the model-level cap)."""
    seen = {fv.desc for fv in existing}
    merged = list(existing)
    for fv in incoming:
        if fv.desc not in seen:
            merged.append(fv)
            seen.add(fv.desc)
    return merged[: Function.MAX_VALUES_PER_FIELD]


class PostgresMemoryStore:
    """PostgreSQL-backed MemoryStore (JSONB + tsvector + optional pgvector).

    Construction requires a resource-issued ready pool; individual operations
    lease connections from it. Requires the optional ``postgres`` dependency
    (psycopg2).

    Optional semantic search is enabled only when the ready service-owned
    capability gate passes a positive explicit ``vector_dim``.  This store
    never creates extensions or alters tables.  An optional ``embedder``
    (any object with ``.embed(text) -> list[float]``) supplies vectors
    written on ``add``; without it, pgvector columns stay NULL and search
    degrades to tsvector-only.
    """

    def __init__(
        self,
        dsn: str,
        embedder: Any = None,
        inbound_executor: InboundSyncExecutor | None = None,
        *,
        require_authorization: bool = False,
        sync_capture_policy: SyncCapturePolicy = SyncCapturePolicy("off"),
        sync_max_attempts: int = 8,
        sync_snapshot_ttl_seconds: int = 900,
        sync_max_snapshot_items: int = 1000000,
        sync_max_active_snapshots_per_tenant: int = 2,
        sync_max_active_snapshots_per_remote: int = 1,
        sync_snapshot_create_timeout_seconds: int = 30,
        sync_consumer_ttl_seconds: int = 86400,
        sync_retention_min_seconds: int = 86400,
        ready_pool: ReadyPostgresPool | None = None,
        **legacy: Any,
    ) -> None:
        if inbound_executor is not None and type(inbound_executor) is not InboundSyncExecutor:
            raise TypeError(
                "PostgresMemoryStore inbound_executor must be an InboundSyncExecutor"
            )
        if type(sync_capture_policy) is not SyncCapturePolicy:
            raise TypeError(
                "PostgresMemoryStore sync_capture_policy must be an exact SyncCapturePolicy"
            )
        if legacy:
            raise TypeError(
                "PostgresMemoryStore requires a resource-issued ReadyPostgresPool"
            )
        ready_pool = validate_ready_postgres_pool(ready_pool)
        self._dsn = dsn
        self._inbound_executor = inbound_executor
        self._ready_pool = ready_pool
        self._pool_manager = ready_pool.manager
        self._vector_dim = ready_pool.effective_dim
        self._embedder = embedder  # optional: object with .embed(text) -> list[float]
        self._require_authorization = require_authorization
        self._sync_capture_policy = sync_capture_policy
        self._sync_repository = None
        if sync_capture_policy.mode == "required":
            from memplex.storage.postgres_sync import PostgresSyncRepository

            self._sync_repository = PostgresSyncRepository(
                self,
                max_attempts=sync_max_attempts,
                snapshot_ttl_seconds=sync_snapshot_ttl_seconds,
                max_snapshot_items=sync_max_snapshot_items,
                max_active_snapshots_per_tenant=sync_max_active_snapshots_per_tenant,
                max_active_snapshots_per_remote=sync_max_active_snapshots_per_remote,
                snapshot_create_timeout_seconds=sync_snapshot_create_timeout_seconds,
                consumer_ttl_seconds=sync_consumer_ttl_seconds,
                retention_min_seconds=sync_retention_min_seconds,
            )

    def authorized(self, context: AuthorizationContext) -> _AuthorizedPostgresStore:
        """Return a request-scoped facade bound to trusted identity.

        The returned object may be retained or used concurrently: scope is
        installed in a :class:`contextvars.ContextVar` for each call, not on
        the shared store or its connection.
        """
        if not isinstance(context, AuthorizationContext):
            raise TypeError("context must be an AuthorizationContext")
        return _AuthorizedPostgresStore(self, context)

    def _require_sync_repository(self):
        repository = self._sync_repository
        if repository is None:
            raise RuntimeError("PostgreSQL sync repository is not enabled")
        return repository

    def sync_page(
        self,
        remote_id: str,
        consumer_id: str,
        cursor: SyncCursorClaims | None,
        limit: int,
    ) -> SyncPage:
        return self._require_sync_repository().sync_page(
            remote_id, consumer_id, cursor, limit
        )

    def sync_create_snapshot(
        self,
        remote_id: str,
        consumer_id: str,
        request_id: str,
        limit: int,
    ) -> SyncSnapshotPage:
        return self._require_sync_repository().sync_create_snapshot(
            remote_id, consumer_id, request_id, limit
        )

    def sync_snapshot_page(
        self,
        remote_id: str,
        consumer_id: str,
        cursor: SyncCursorClaims,
        limit: int,
    ) -> SyncSnapshotPage:
        return self._require_sync_repository().sync_snapshot_page(
            remote_id, consumer_id, cursor, limit
        )

    def sync_apply_batch(self, batch: SyncBatch) -> SyncBatchResult:
        return self._require_sync_repository().sync_apply_batch(batch)

    def sync_apply_page(self, remote_id: str, page: SyncPage) -> SyncApplyResult:
        return self._require_sync_repository().sync_apply_page(remote_id, page)

    def sync_register_target(
        self, target_id: str, *, bootstrap: str = "future"
    ) -> None:
        self._require_sync_repository().sync_register_target(
            target_id, bootstrap=bootstrap
        )

    def sync_claim(
        self,
        target_id: str,
        *,
        limit: int,
        lease_seconds: int,
    ) -> list[SyncDelivery]:
        return self._require_sync_repository().sync_claim(
            target_id, limit=limit, lease_seconds=lease_seconds
        )

    def sync_ack(self, delivery: SyncDelivery, receipt: SyncReceipt) -> None:
        self._require_sync_repository().sync_ack(delivery, receipt)

    def sync_ack_batch(self, deliveries, receipts) -> None:
        self._require_sync_repository().sync_ack_batch(deliveries, receipts)

    def sync_fail(
        self,
        delivery: SyncDelivery,
        error_code: str,
        now: datetime,
    ) -> None:
        self._require_sync_repository().sync_fail(delivery, error_code, now)

    def sync_dead_letter(
        self,
        delivery: SyncDelivery,
        error_code: str,
        now: datetime,
    ) -> None:
        self._require_sync_repository().sync_dead_letter(
            delivery, error_code, now
        )

    def sync_replay_dead_letter(self, target_id: str, event_id: str) -> bool:
        return self._require_sync_repository().sync_replay_dead_letter(
            target_id, event_id
        )

    def sync_list_dead_letters(self, *, limit: int):
        return self._require_sync_repository().sync_list_dead_letters(
            limit=limit
        )

    def sync_set_target_enabled(self, target_id: str, enabled: bool) -> None:
        self._require_sync_repository().sync_set_target_enabled(target_id, enabled)

    def sync_compact(self, now: datetime, *, limit: int) -> int:
        return self._require_sync_repository().sync_compact(now, limit=limit)

    def sync_status(self) -> SyncStatus:
        return self._require_sync_repository().sync_status()

    def sync_dispatch_status(self) -> SyncStatus:
        return self._require_sync_repository().sync_dispatch_status()

    def _authorization_context(self) -> AuthorizationContext:
        context = _POSTGRES_SCOPE.get()
        if context is not None:
            return context
        if self._require_authorization:
            raise PermissionError(
                "PostgreSQL memory access requires an authorization context; "
                "use store.authorized(context).<operation>(...)"
            )
        # Keep the historic unscoped API usable only as an explicitly named
        # local development principal.  Production construction sets
        # require_authorization=True and never reaches this fallback.
        return local_development_context()

    @staticmethod
    def _context_values(context: AuthorizationContext) -> tuple[str, str, str, str, str]:
        return (
            context.principal.tenant_id,
            context.principal.subject_id,
            context.workspace_id,
            context.agent_id,
            context.session_id,
        )

    def _bind_transaction_scope(self, cur, context: AuthorizationContext) -> None:
        """Bind all RLS settings transaction-locally before application SQL."""
        cur.execute(
            "SELECT "
            "set_config('memplex.tenant_id', %s, true), "
            "set_config('memplex.subject_id', %s, true), "
            "set_config('memplex.workspace_id', %s, true), "
            "set_config('memplex.agent_id', %s, true), "
            "set_config('memplex.session_id', %s, true)",
            self._context_values(context),
        )

    def _bind_local_sync_context(
        self,
        cur,
        node_type: str,
        node_id: str,
        payload: dict,
    ) -> tuple[str, str, str]:
        """Bind the 7 required durable-sync GUC values for local capture.

        Return values are deterministic for this invocation and mirror the
        values that ``memplex_sync_capture_*`` will persist for each DML row.
        """
        if self._sync_capture_policy.mode != "required":
            return ("", "", "")
        if node_type not in {"function", "edge", "observation", "fact", "preference"}:
            raise ValueError("unsupported sync node type")
        if node_type == "edge":
            entity_key = str(SyncEntityKey.parse(node_id))
        else:
            entity_key = str(SyncEntityKey.node(node_id))
        event_id = str(uuid.uuid4())
        version_key = str(
            SyncVersion.create(
                datetime.now(timezone.utc),
                self._sync_capture_policy.local_node_id,
                event_id,
            )
        )
        cur.execute(
            "SELECT "
                "set_config('memplex.sync_capture', %s, true), "
                "set_config('memplex.sync_apply_mode', %s, true), "
                "set_config('memplex.sync_origin_node_id', %s, true), "
                "set_config('memplex.sync_event_id', %s, true), "
                "set_config('memplex.sync_version_key', %s, true), "
                "set_config('memplex.sync_entity_key', %s, true), "
                "set_config('memplex.sync_payload', %s, true)",
            (
                "required",
                "local",
                self._sync_capture_policy.local_node_id,
                event_id,
                version_key,
                entity_key,
                json.dumps(payload, sort_keys=True, separators=(",", ":"))
                if payload
                else "",
            ),
        )
        return (event_id, version_key, entity_key)

    @staticmethod
    def _quoted_sql_literal(value: str) -> str:
        """Quote validated context identifiers for the legacy one-call path."""
        return "'" + value.replace("'", "''") + "'"

    def _inline_transaction_scope(self, sql: str, context: AuthorizationContext) -> str:
        """Prefix legacy development SQL without changing its bind positions.

        Historic mock contracts assert positional query parameters.  For
        unscoped local-development calls we therefore put transaction-local
        settings in the same database execute call as literal-safe values.
        Authenticated production calls use :meth:`_bind_transaction_scope`,
        whose separate first command is easier to audit and test.
        """
        tenant, subject, workspace, agent, session = self._context_values(context)
        return (
            "SELECT "
            f"set_config('memplex.tenant_id', {self._quoted_sql_literal(tenant)}, true), "
            f"set_config('memplex.subject_id', {self._quoted_sql_literal(subject)}, true), "
            f"set_config('memplex.workspace_id', {self._quoted_sql_literal(workspace)}, true), "
            f"set_config('memplex.agent_id', {self._quoted_sql_literal(agent)}, true), "
            f"set_config('memplex.session_id', {self._quoted_sql_literal(session)}, true); "
            + sql
        )

    def _write_identity(self, node) -> AuthorizationContext:
        """Return the active scope and project it only for authenticated calls.

        Legacy unscoped development calls retain their historical payload
        semantics, while the relational columns still receive the auditable
        local-development identity.  Authenticated store writes canonicalize
        every identity and provenance field to the trusted context.  Ingress
        boundaries reject forged payload claims before reaching the store;
        canonicalization here also permits a workspace member to persist an
        update loaded from another member without preserving the prior owner.
        """
        context = self._authorization_context()
        if _POSTGRES_SCOPE.get() is not None:
            visibility = str(getattr(node, "visibility", None) or "workspace").strip().lower()
            # Validate session prerequisites before the first lookup or write;
            # relying on RLS alone is insufficient for bypass-RLS owners.
            self._row_identity_values(context, node, visibility=visibility)
            bind_node_identity(
                node,
                context,
                visibility=visibility,
                reject_conflicts=False,
            )
        return context

    @staticmethod
    def _row_identity_values(
        context: AuthorizationContext,
        node: Any | None = None,
        *,
        visibility: str | None = None,
    ) -> tuple[str, str, str, str, str, str]:
        visibility = str(
            visibility or getattr(node, "visibility", None) or "workspace"
        ).strip().lower()
        if visibility not in {"user", "workspace", "session"}:
            raise ValueError(
                "PostgreSQL visibility must be one of: user, workspace, session"
            )
        if visibility == "session" and (not context.agent_id or not context.session_id):
            raise PermissionError(
                "session visibility requires non-empty agent_id and session_id"
            )
        return (
            context.principal.tenant_id,
            context.principal.subject_id,
            context.workspace_id,
            str(visibility),
            context.agent_id,
            context.session_id,
        )

    def set_embedder(self, embedder: Any) -> None:
        """Inject (or replace) the pgvector embedder after construction.

        ``create_store`` stays embedder-free because the embedding service
        does not exist yet when the store is built; the service layer
        injects its shared EmbeddingService through this setter so the
        hybrid (tsv + vector RRF) search leg lights up when the ready
        service-owned capability gate supplied ``vector_dim``. Harmless
        when ``vector_dim`` is 0 -- the embedder simply goes unused.
        """
        self._embedder = embedder

    @staticmethod
    def _is_lock_unavailable(exc: BaseException) -> bool:
        return getattr(exc, "pgcode", None) == "55P03"

    def _acquire_function_write_lock(self, cur, context: AuthorizationContext) -> None:
        """Serialize Function/edge writers for one tenant in this transaction.

        Scope settings have already been installed by the pool transaction
        before this method runs.  The advisory lock is therefore the first
        business statement, before any Function/edge lookup, DML or row lock.
        ``SET LOCAL`` keeps the bounded wait confined to this transaction.
        """
        key = _function_write_lock_key(
            self._ready_pool.target, context.principal.tenant_id
        )
        cur.execute(f"SET LOCAL lock_timeout = '{_FUNCTION_WRITE_LOCK_TIMEOUT}'")
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (key,))

    @contextmanager
    def _function_write_transaction(
        self,
        context: AuthorizationContext,
        operation: str,
    ):
        """One locked transaction for a public Function/edge write operation."""
        try:
            with self._pool_manager.transaction(
                self._bind_transaction_scope, context
            ) as (_, cur):
                self._acquire_function_write_lock(cur, context)
                yield cur
        except BaseException as exc:
            if self._is_lock_unavailable(exc):
                # Do not mention tenant, DSN, or a competing row: this is a
                # safe retry signal rather than an authorization disclosure.
                raise FunctionWriteBusy(
                    f"Function write is temporarily busy during {operation}; retry"
                ) from exc
            raise


    def _execute(self, sql: str, params: tuple = (), *, commit: bool = True):
        context = self._authorization_context()
        if commit:
            with self._pool_manager.transaction(self._bind_transaction_scope, context) as (_, cur):
                cur.execute(sql, params)
            return None
        cur = self._pool_manager.read_cursor(self._bind_transaction_scope, context)
        try:
            cur.execute(sql, params)
        except BaseException:
            try:
                cur.close()
            except BaseException:
                # Application SQL is the primary failure.  The pool manager
                # records cleanup faults and rejects future leases.
                pass
            raise
        return cur

    # ── Write operations ────────────────────────────────────────────

    def _embed_text(self, func: Function) -> Optional[str]:
        """Return a pgvector-literal string for *func* or None when disabled.

        pgvector accepts the text form ``[1.0, 2.0, ...]``; we pass it as
        a string parameter to avoid adapter complexity.
        """
        if self._vector_dim <= 0 or self._embedder is None:
            return None
        try:
            text = f"{func.name} {func.domain or ''} " + " ".join(
                fv.desc for fv in (func.trigger + func.action)
            )
            vec = self._embedder.embed(text)
            if vec and len(vec) == self._vector_dim:
                return str(list(vec))
        except Exception as exc:
            logger.debug("pgvector embed failed for %s, storing NULL: %s", func.id, exc)
        return None

    def _upsert_function(
        self,
        cur,
        func: Function,
        relational_identity: tuple[str, str, str, str, str, str] | None = None,
    ) -> None:
        """Upsert *func* using the caller-owned transaction cursor.

        Public write methods deliberately own the transaction boundary.  This
        helper must therefore never obtain a lease or commit independently:
        a function row and its audit row are one durable decision.
        """
        if not isinstance(func, Function):
            raise ValueError("PostgreSQL 只接受 Function 节点")
        validate_func_id(func.id)
        validate_domain(func.domain)
        data = _func_to_json(func)
        self._bind_local_sync_context(cur, "function", func.id, data)
        embedding = self._embed_text(func)
        identity = relational_identity or self._row_identity_values(
            self._authorization_context(), func
        )
        conflict_scope = _acl_scope_sql("memplex_functions")
        if embedding is not None:
            cur.execute(
                f"""
                INSERT INTO memplex_functions
                    (id, data, updated_at, embedding, tenant_id, owner_subject,
                     workspace, visibility, source_agent, source_session)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, id) DO UPDATE SET
                    data = EXCLUDED.data,
                    updated_at = EXCLUDED.updated_at,
                    embedding = EXCLUDED.embedding,
                    owner_subject = EXCLUDED.owner_subject,
                    workspace = EXCLUDED.workspace,
                    visibility = EXCLUDED.visibility,
                    source_agent = EXCLUDED.source_agent,
                    source_session = EXCLUDED.source_session
                WHERE {conflict_scope}
                RETURNING id
                """,
                (
                    func.id,
                    json.dumps(data),
                    _iso(func.updated_at) or datetime.now(timezone.utc),
                    embedding,
                    *identity,
                ),
            )
            self._require_returning(cur, (func.id,))
        else:
            cur.execute(
                f"""
                INSERT INTO memplex_functions
                    (id, data, updated_at, tenant_id, owner_subject, workspace,
                     visibility, source_agent, source_session)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, id) DO UPDATE SET
                    data = EXCLUDED.data,
                    updated_at = EXCLUDED.updated_at,
                    owner_subject = EXCLUDED.owner_subject,
                    workspace = EXCLUDED.workspace,
                    visibility = EXCLUDED.visibility,
                    source_agent = EXCLUDED.source_agent,
                    source_session = EXCLUDED.source_session
                WHERE {conflict_scope}
                RETURNING id
                """,
                (
                    func.id,
                    json.dumps(data),
                    _iso(func.updated_at) or datetime.now(timezone.utc),
                    *identity,
                ),
            )
            self._require_returning(cur, (func.id,))

    @staticmethod
    def _require_returning(cur, expected: tuple[object, ...]) -> None:
        """Require an ACL-qualified DML statement to name its durable row.

        PostgreSQL treats ``ON CONFLICT ... DO UPDATE WHERE <ACL>`` that
        fails the ``WHERE`` as a successful command affecting zero rows.  A
        caller must not translate that silence into a changelog, graph count,
        or batch success because it may disclose/act on a foreign record.
        """
        row = cur.fetchone()
        if row is None or tuple(row) != expected:
            raise PostgresWriteRejected(
                "PostgreSQL write did not affect an authorized row"
            )

    def _record_changelog(
        self,
        cur,
        func_id: str,
        event_type: str,
        description: str,
        source: Optional[SourceDocument],
        *,
        node: Any | None = None,
        visibility: str | None = None,
    ) -> None:
        """Append a changelog entry so get_timeline() reflects writes
        (previously the table was read but never written)."""
        src = ""
        if source is not None:
            src = getattr(source, "source_path", None) or getattr(source, "url", "") or ""
        # Changelog rows carry the source memory's visibility.  When a caller
        # has already deleted the node, default to user scope rather than
        # widening an audit event to the workspace.
        identity = self._row_identity_values(
            self._authorization_context(),
            node,
            visibility=visibility or (None if node is not None else "user"),
        )
        cur.execute(
            """
            INSERT INTO memplex_changelog
                (func_id, ts, event_type, description, source, actor, tenant_id,
                 owner_subject, workspace, visibility, source_agent, source_session)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                func_id,
                datetime.now(timezone.utc),
                event_type,
                description,
                src,
                "system",
                *identity,
            ),
        )

    def add(self, func: Function, source: SourceDocument) -> None:
        """Add a Function, merging FieldValues when a Function with the same
        ``name_normalized`` already exists (base contract; lite semantics)."""
        if not isinstance(func, Function):
            raise ValueError("PostgreSQL 只接受 Function 节点")
        validate_func_id(func.id)
        validate_domain(func.domain)
        context = self._write_identity(func)
        with self._function_write_transaction(context, "add") as cur:
            node, created = self._merge_or_insert_function(cur, func, context)
            event_type = "created" if created else "updated"
            description = (
                f"Created function: {node.name}"
                if created
                else "Merged fields from source"
            )
            self._record_changelog(cur, node.id, event_type, description, source, node=node)

    @staticmethod
    def _is_unique_violation(exc: BaseException) -> bool:
        """Recognise psycopg2's portable SQLSTATE without importing it.

        Keeping this SQLSTATE test local lets mock/unit environments exercise
        the public logic without an optional psycopg dependency.
        """
        return getattr(exc, "pgcode", None) == "23505"

    @staticmethod
    def _locked_function_row(
        row: tuple,
        context: AuthorizationContext,
    ) -> tuple[Function, tuple[str, str, str, str, str, str]]:
        data = row[1] if isinstance(row[1], dict) else json.loads(row[1])
        function = _func_from_json(data)
        # Unit doubles predate relational identity fields.  Production queries
        # below always select all six and therefore never use this fallback.
        if len(row) < 8:
            identity = PostgresMemoryStore._row_identity_values(context, function)
        else:
            identity = tuple(row[2:8])
        return function, identity

    def _locked_function_by_id(
        self,
        cur,
        func_id: str,
        context: AuthorizationContext,
    ) -> tuple[Function, tuple[str, str, str, str, str, str]] | None:
        """Lock a visible canonical row by tenant/id before name lookup."""
        cur.execute(
            "SELECT id, data, tenant_id, owner_subject, workspace, visibility, "
            "source_agent, source_session FROM memplex_functions "
            f"WHERE {_acl_scope_sql()} AND id = %s FOR UPDATE",
            (func_id,),
        )
        row = cur.fetchone()
        return None if row is None else self._locked_function_row(row, context)

    def _require_visible_function_endpoint(
        self,
        cur,
        func_id: str,
        context: AuthorizationContext,
    ) -> tuple[Function, tuple[str, str, str, str, str, str]]:
        """Require an edge endpoint to be a visible durable canonical node."""
        locked = self._locked_function_by_id(cur, func_id, context)
        if locked is None:
            raise PostgresWriteRejected(
                "PostgreSQL write did not affect an authorized row"
            )
        return locked

    @staticmethod
    def _is_graph_builder_domain_endpoint(
        edge: GraphEdge,
        source: Function | None,
        target_id: str,
    ) -> bool:
        """Recognise GraphBuilder's non-memory ``BELONGS_TO`` domain node.

        Domain labels deliberately use human text (including non-ASCII),
        whereas ``Function.id`` intentionally rejects such values.  They are
        graph vocabulary, not persisted memory functions.  Only this exact
        source-derived form remains exempt from the canonical-function
        endpoint rule; arbitrary absent endpoints fail closed.
        """
        if edge.edge_type != "BELONGS_TO" or source is None:
            return False
        try:
            # Graph merge may have mapped an incoming normalized-name alias
            # onto an existing canonical Function. Validate the resolved
            # endpoints, not the caller's stale edge source ID.
            resolved_edge = GraphEdge(
                source=source.id,
                target=target_id,
                edge_type=edge.edge_type,
            )
            validate_belongs_to_edges((source,), (resolved_edge,))
        except ValueError:
            return False
        return True

    def _normalized_function(
        self,
        cur,
        normalized_name: str,
        incoming: Function,
        context: AuthorizationContext,
    ) -> tuple[Function, tuple[str, str, str, str, str, str]] | None:
        """Lock the one partial-unique domain selected by *incoming*.

        ACL visibility is deliberately broader than uniqueness.  For example,
        one subject can read both a workspace row and its private user row,
        but those are distinct records.  Match the migrated partial-index
        key exactly before considering a merge.
        """
        if not normalized_name:
            return None
        tenant, subject, workspace, visibility, agent, session = self._row_identity_values(
            context, incoming
        )
        if visibility == "workspace":
            scope_sql = "tenant_id = %s AND visibility = 'workspace' AND workspace = %s"
            scope_params = (tenant, workspace)
        elif visibility == "user":
            scope_sql = "tenant_id = %s AND visibility = 'user' AND owner_subject = %s"
            scope_params = (tenant, subject)
        else:
            scope_sql = (
                "tenant_id = %s AND visibility = 'session' AND workspace = %s "
                "AND owner_subject = %s AND source_agent = %s AND source_session = %s"
            )
            scope_params = (tenant, workspace, subject, agent, session)
        cur.execute(
            "SELECT id, data, tenant_id, owner_subject, workspace, visibility, "
            "source_agent, source_session FROM memplex_functions "
            f"WHERE {scope_sql} "
            "AND lower(btrim(coalesce(data->>'name_normalized', data->>'name', ''))) = %s "
            "LIMIT 1 FOR UPDATE",
            (*scope_params, normalized_name),
        )
        row = cur.fetchone()
        return None if row is None else self._locked_function_row(row, context)

    def _merge_or_insert_function(
        self,
        cur,
        incoming: Function,
        context: AuthorizationContext,
    ) -> tuple[Function, bool]:
        """Return the canonical function and whether it was newly inserted.

        The caller must already have bound ``incoming`` to the authenticated
        identity and own the surrounding transaction.  A normalized-name
        canonical function is immutable in identity, visibility, namespace
        and provenance.  The one existing compatibility exception is an
        explicit same-id workspace update, whose authorized current writer
        becomes canonical while its visibility remains workspace.  Both
        ``add`` and graph ``merge`` use this exact primitive so partial-index
        races converge the same way.
        """
        if not isinstance(incoming, Function):
            raise ValueError("PostgreSQL 图节点必须是 Function")
        validate_func_id(incoming.id)
        validate_domain(incoming.domain)
        locked = self._locked_function_by_id(cur, incoming.id, context)
        matched_by_id = locked is not None
        if locked is None:
            normalized = (incoming.name_normalized or incoming.name or "").strip().lower()
            locked = self._normalized_function(cur, normalized, incoming, context)
        if locked is not None:
            canonical, identity = locked
            # An explicit same-id update to shared workspace memory is the
            # long-standing collaboration contract: the latest authorized
            # workspace writer becomes its canonical writer.  This is
            # intentionally narrower than normalized-name convergence, where
            # a different incoming id must never take over the existing row.
            # Private/session rows retain their locked identity and cannot be
            # widened merely by supplying an incoming workspace visibility.
            if matched_by_id and canonical.visibility == "workspace":
                self._write_identity(canonical)
                identity = self._row_identity_values(context, canonical)
            self._merge_function(canonical, incoming)
            self._upsert_function(cur, canonical, identity)
            return canonical, False

        cur.execute("SAVEPOINT memplex_function_insert")
        try:
            self._upsert_function(cur, incoming)
            cur.execute("RELEASE SAVEPOINT memplex_function_insert")
            return incoming, True
        except BaseException as exc:
            if not self._is_unique_violation(exc):
                raise
            # The unique violation belongs to the exact partial-index domain
            # selected by ``incoming``.  Do not widen ACL scope or replace a
            # competing row's identity.
            cur.execute("ROLLBACK TO SAVEPOINT memplex_function_insert")
            normalized = (incoming.name_normalized or incoming.name or "").strip().lower()
            locked = self._normalized_function(cur, normalized, incoming, context)
            if locked is None:
                raise
            canonical, identity = locked
            self._merge_function(canonical, incoming)
            self._upsert_function(cur, canonical, identity)
            return canonical, False

    @staticmethod
    def _function_merge_sort_key(
        node: Function,
        context: AuthorizationContext,
    ) -> tuple[str, str, str, str, str, str, str, str]:
        """Return the deterministic row-lock order for incoming Functions.

        The partial unique indexes divide names by visibility scope, so both
        the exact scope and normalized name belong in the ordering key.  The
        immutable ID is the final tie breaker.  Keeping this in one helper
        makes reverse graph payloads lock the same sequence across workers.
        """
        tenant, subject, workspace, visibility, agent, session = (
            PostgresMemoryStore._row_identity_values(context, node)
        )
        normalized = (node.name_normalized or node.name or "").strip().lower()
        if visibility == "workspace":
            scope = (tenant, visibility, workspace, "", "", "")
        elif visibility == "user":
            scope = (tenant, visibility, subject, "", "", "")
        else:
            scope = (tenant, visibility, workspace, subject, agent, session)
        return (*scope, normalized, node.id)

    @staticmethod
    def _merge_function(existing: Function, incoming: Function) -> Function:
        merged_trigger = _merge_field_values(existing.trigger, incoming.trigger)
        merged_condition = _merge_field_values(existing.condition, incoming.condition)
        merged_action = _merge_field_values(existing.action, incoming.action)
        merged_benefit = _merge_field_values(existing.benefit, incoming.benefit)
        changed = (
            merged_trigger != existing.trigger
            or merged_condition != existing.condition
            or merged_action != existing.action
            or merged_benefit != existing.benefit
        )
        existing.trigger = merged_trigger
        existing.condition = merged_condition
        existing.action = merged_action
        existing.benefit = merged_benefit
        for paragraph in incoming.source_paragraphs:
            if paragraph not in existing.source_paragraphs:
                existing.source_paragraphs.append(paragraph)
                changed = True
        incoming_version = int(getattr(incoming, "version", 1) or 1)
        if incoming_version > existing.version:
            existing.version = incoming_version
            changed = True
        elif changed:
            existing.version += 1
        if changed:
            existing.updated_at = datetime.now(timezone.utc).isoformat()
        return existing

    def add_batch(
        self,
        funcs: List[Function],
        sources: List[SourceDocument],
    ) -> BatchResult:
        """Batch add.  Per-item failures are isolated and recorded in
        ``BatchResult.failed_items``; each item goes through :meth:`add`
        so embeddings and changelog entries are written too."""
        result = BatchResult(total=len(funcs))
        for func, src in zip(funcs, sources):
            try:
                self.add(func, src)
                result.succeeded += 1
            except Exception as exc:
                result.failed_items.append(
                    {
                        "func_id": func.id,
                        "name": func.name,
                        "error": str(exc),
                    }
                )
        return result

    def add_observation(self, observation: Observation) -> None:
        context = self._write_identity(observation)
        identity = self._row_identity_values(context, observation)
        with self._pool_manager.transaction(self._bind_transaction_scope, context) as (_, cur):
            payload = _obs_to_json(observation)
            self._bind_local_sync_context(cur, "observation", observation.id, payload)
            cur.execute(
                f"""
                INSERT INTO memplex_observations
                    (id, data, created_at, tenant_id, owner_subject, workspace,
                     visibility, source_agent, source_session)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (tenant_id, id) DO UPDATE SET
                    data = EXCLUDED.data,
                    owner_subject = EXCLUDED.owner_subject,
                    workspace = EXCLUDED.workspace,
                    visibility = EXCLUDED.visibility,
                    source_agent = EXCLUDED.source_agent,
                    source_session = EXCLUDED.source_session
                WHERE {_acl_scope_sql('memplex_observations')}
                RETURNING id
                """,
                (
                    observation.id,
                    json.dumps(payload),
                    datetime.now(timezone.utc),
                    *identity,
                ),
            )
            self._require_returning(cur, (observation.id,))
            self._record_changelog(
                cur,
                observation.id,
                "created",
                f"Stored observation: {observation.name}",
                None,
                node=observation,
            )

    # ── Fact / Preference (optional MemoryStore extensions) ─────────

    @staticmethod
    def _stamp_node(node) -> None:
        """Fill created_at/updated_at on a Fact/Preference before upsert."""
        now = datetime.now(timezone.utc).isoformat()
        if not node.created_at:
            node.created_at = now
        node.updated_at = now

    def _upsert_typed_node(
        self,
        cur,
        table: str,
        node,
        context: AuthorizationContext,
        *,
        data: dict | None = None,
    ) -> None:
        """Upsert a typed node through the caller-owned transaction cursor."""
        identity = self._row_identity_values(context, node)
        data = data if data is not None else node.to_dict()
        cur.execute(
            f"""
            INSERT INTO {table}
                (id, data, updated_at, tenant_id, owner_subject, workspace,
                 visibility, source_agent, source_session)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (tenant_id, id) DO UPDATE SET
                data = EXCLUDED.data,
                updated_at = EXCLUDED.updated_at,
                owner_subject = EXCLUDED.owner_subject,
                workspace = EXCLUDED.workspace,
                visibility = EXCLUDED.visibility,
                source_agent = EXCLUDED.source_agent,
                source_session = EXCLUDED.source_session
            WHERE {_acl_scope_sql(table)}
            RETURNING id
            """,
            (node.id, json.dumps(data), _iso(node.updated_at), *identity),
        )
        self._require_returning(cur, (node.id,))

    def add_fact(self, fact: Fact) -> None:
        """Persist a Fact (upsert by id) into ``memplex_facts`` + changelog."""
        self._stamp_node(fact)
        context = self._write_identity(fact)
        with self._pool_manager.transaction(self._bind_transaction_scope, context) as (_, cur):
            data = fact.to_dict()
            self._bind_local_sync_context(cur, "fact", fact.id, data)
            self._upsert_typed_node(cur, "memplex_facts", fact, context, data=data)
            self._record_changelog(
                cur, fact.id, "created", f"Stored fact: {fact.name or fact.subject}",
                None, node=fact,
            )

    def add_preference(self, preference: Preference) -> None:
        """Persist a Preference (upsert by id) into ``memplex_preferences``."""
        self._stamp_node(preference)
        context = self._write_identity(preference)
        with self._pool_manager.transaction(self._bind_transaction_scope, context) as (_, cur):
            data = preference.to_dict()
            self._bind_local_sync_context(cur, "preference", preference.id, data)
            self._upsert_typed_node(
                cur,
                "memplex_preferences",
                preference,
                context,
                data=data,
            )
            self._record_changelog(
                cur, preference.id, "created",
                f"Stored preference: {preference.name or preference.aspect}",
                None, node=preference,
            )

    def get_fact(self, fact_id: str) -> Optional[Fact]:
        cur = self._execute(
            "SELECT data FROM memplex_facts "
            f"WHERE {_acl_scope_sql()} AND id = %s",
            (fact_id,),
            commit=False,
        )
        row = cur.fetchone()
        cur.close()
        if row is None:
            return None
        data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        return Fact.from_dict(data)

    def get_preference(self, preference_id: str) -> Optional[Preference]:
        cur = self._execute(
            "SELECT data FROM memplex_preferences "
            f"WHERE {_acl_scope_sql()} AND id = %s",
            (preference_id,),
            commit=False,
        )
        row = cur.fetchone()
        cur.close()
        if row is None:
            return None
        data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        return Preference.from_dict(data)

    def list_facts(
        self, offset: int = 0, limit: int = 1000, owner: Optional[str] = None
    ) -> List[Fact]:
        if owner:
            cur = self._execute(
                "SELECT data FROM memplex_facts "
                f"WHERE {_acl_scope_sql()} AND data->>'owner' = %s "
                "ORDER BY data->>'updated_at' DESC OFFSET %s LIMIT %s",
                (owner, offset, limit),
                commit=False,
            )
        else:
            cur = self._execute(
                "SELECT data FROM memplex_facts "
                f"WHERE {_acl_scope_sql()} "
                "ORDER BY data->>'updated_at' DESC "
                "OFFSET %s LIMIT %s",
                (offset, limit),
                commit=False,
            )
        facts = []
        for row in cur.fetchall():
            data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
            facts.append(Fact.from_dict(data))
        cur.close()
        return facts

    def list_preferences(
        self, offset: int = 0, limit: int = 1000, owner: Optional[str] = None
    ) -> List[Preference]:
        if owner:
            cur = self._execute(
                "SELECT data FROM memplex_preferences "
                f"WHERE {_acl_scope_sql()} AND data->>'owner' = %s "
                "ORDER BY data->>'updated_at' DESC OFFSET %s LIMIT %s",
                (owner, offset, limit),
                commit=False,
            )
        else:
            cur = self._execute(
                "SELECT data FROM memplex_preferences "
                f"WHERE {_acl_scope_sql()} "
                "ORDER BY data->>'updated_at' DESC "
                "OFFSET %s LIMIT %s",
                (offset, limit),
                commit=False,
            )
        prefs = []
        for row in cur.fetchall():
            data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
            prefs.append(Preference.from_dict(data))
        cur.close()
        return prefs

    def list_observations(
        self,
        offset: int = 0,
        limit: int = 1000,
        category: Optional[str] = None,
        owner: Optional[str] = None,
    ) -> List[Observation]:
        """Paginated Observation listing with optional JSONB filters."""
        clauses = []
        params: list = []
        if category is not None:
            clauses.append("o.data->>'category' = %s")
            params.append(category)
        if owner is not None:
            clauses.append("o.data->>'owner' = %s")
            params.append(owner)
        where = f"WHERE {' AND '.join(clauses)} " if clauses else ""
        cur = self._execute(
            "SELECT o.data FROM memplex_observations o "
            "JOIN (SELECT current_setting('memplex.tenant_id', true) AS tenant_id) scope "
            f"ON {_acl_scope_sql('o')} "
            f"{where}ORDER BY o.created_at DESC OFFSET %s LIMIT %s",
            (*params, offset, limit),
            commit=False,
        )
        observations = []
        for row in cur.fetchall():
            data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
            # Observation.from_dict tolerates rows predating the category key.
            observations.append(Observation.from_dict(data))
        cur.close()
        return observations

    def get_observation(self, observation_id: str) -> Optional[Observation]:
        cur = self._execute(
            "SELECT data FROM memplex_observations "
            f"WHERE {_acl_scope_sql()} AND id = %s",
            (observation_id,),
            commit=False,
        )
        row = cur.fetchone()
        cur.close()
        if row is None:
            return None
        data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        return Observation.from_dict(data)

    def delete_fact(self, fact_id: str) -> None:
        context = self._authorization_context()
        with self._pool_manager.transaction(self._bind_transaction_scope, context) as (_, cur):
            self._bind_local_sync_context(cur, "fact", fact_id, {})
            cur.execute(
                f"DELETE FROM memplex_facts WHERE {_acl_scope_sql()} AND id = %s RETURNING id",
                (fact_id,),
            )
            if cur.fetchone() is None:
                return
            self._record_changelog(
                cur, fact_id, "deleted", f"Deleted fact: {fact_id}", None
            )

    def delete_preference(self, preference_id: str) -> None:
        context = self._authorization_context()
        with self._pool_manager.transaction(self._bind_transaction_scope, context) as (_, cur):
            self._bind_local_sync_context(cur, "preference", preference_id, {})
            cur.execute(
                f"DELETE FROM memplex_preferences WHERE {_acl_scope_sql()} AND id = %s RETURNING id",
                (preference_id,),
            )
            if cur.fetchone() is None:
                return
            self._record_changelog(
                cur, preference_id, "deleted",
                f"Deleted preference: {preference_id}", None,
            )

    def delete_observation(self, observation_id: str) -> None:
        context = self._authorization_context()
        with self._pool_manager.transaction(self._bind_transaction_scope, context) as (_, cur):
            self._bind_local_sync_context(cur, "observation", observation_id, {})
            cur.execute(
                f"DELETE FROM memplex_observations WHERE {_acl_scope_sql()} AND id = %s RETURNING id",
                (observation_id,),
            )
            if cur.fetchone() is None:
                return
            self._record_changelog(
                cur,
                observation_id,
                "deleted",
                f"Deleted observation: {observation_id}",
                None,
            )

    def increment_access(self, func_id: str) -> None:
        context = self._authorization_context()
        with self._function_write_transaction(context, "increment_access") as cur:
            locked = self._locked_function_by_id(cur, func_id, context)
            if locked is None:
                return
            node, _identity = locked
            node.access_count = (int(node.access_count or 0) + 1)
            node.last_accessed_at = datetime.now(timezone.utc).isoformat()
            payload = _func_to_json(node)
            self._bind_local_sync_context(cur, "function", func_id, payload)
            cur.execute(
                f"""
                UPDATE memplex_functions
                SET data = %s, updated_at = %s
                WHERE {_acl_scope_sql()} AND id = %s
                RETURNING id
                """,
                (json.dumps(payload), node.updated_at, func_id),
            )
            self._require_returning(cur, (func_id,))

    def increment_access_batch(self, func_ids) -> None:
        context = self._authorization_context()
        with self._function_write_transaction(context, "increment_access_batch") as cur:
            for fid in func_ids:
                locked = self._locked_function_by_id(cur, fid, context)
                if locked is None:
                    continue
                node, _identity = locked
                node.access_count = (int(node.access_count or 0) + 1)
                node.last_accessed_at = datetime.now(timezone.utc).isoformat()
                payload = _func_to_json(node)
                self._bind_local_sync_context(cur, "function", fid, payload)
                cur.execute(
                    f"""
                    UPDATE memplex_functions
                    SET data = %s, updated_at = %s
                    WHERE {_acl_scope_sql()} AND id = %s
                    RETURNING id
                    """,
                    (json.dumps(payload), node.updated_at, fid),
                )
                self._require_returning(cur, (fid,))

    # ── Retrieval ───────────────────────────────────────────────────

    def vector_search(self, text: str, top_k: int = 5) -> List[SearchResult]:
        """Hybrid search: tsvector full-text + optional pgvector cosine.

        When pgvector is enabled and an embedder is configured, runs both a
        tsvector and a vector-cosine query and merges them with Reciprocal
        Rank Fusion (RRF). Otherwise degrades to tsvector-only.
        """
        # --- tsvector leg (always runs) ---
        cur = self._execute(
            f"""
            SELECT id, data, ts_rank(search_tsv, plainto_tsquery('simple', %s)) AS score
            FROM memplex_functions
            WHERE {_acl_scope_sql()}
              AND search_tsv @@ plainto_tsquery('simple', %s)
            ORDER BY score DESC
            LIMIT %s
            """,
            (text, text, top_k * 2),
            commit=False,
        )
        tsv_rows = cur.fetchall()
        cur.close()

        # --- pgvector leg (only when enabled + embedder available) ---
        vec_rows = []
        if self._vector_dim > 0 and self._embedder is not None:
            try:
                qvec = self._embedder.embed(text)
                if qvec and len(qvec) == self._vector_dim:
                    cur = self._execute(
                        f"""
                        SELECT id, data, 1 - (embedding <=> %s::vector) AS score
                        FROM memplex_functions
                        WHERE {_acl_scope_sql()}
                          AND embedding IS NOT NULL
                        ORDER BY embedding <=> %s::vector
                        LIMIT %s
                        """,
                        (str(list(qvec)), str(list(qvec)), top_k * 2),
                        commit=False,
                    )
                    vec_rows = cur.fetchall()
                    cur.close()
            except Exception as exc:
                logger.debug("pgvector search leg failed, using tsv only: %s", exc)

        # --- RRF merge ---
        return self._rrf_merge(tsv_rows, vec_rows, top_k)

    @staticmethod
    def _rrf_merge(tsv_rows, vec_rows, top_k, k: int = 60) -> List[SearchResult]:
        """Reciprocal Rank Fusion of the two result legs."""
        scores: dict = {}
        meta: dict = {}
        for rank, row in enumerate(tsv_rows):
            fid = row[0]
            scores[fid] = scores.get(fid, 0.0) + 1.0 / (k + rank + 1)
            meta[fid] = row[1]
        for rank, row in enumerate(vec_rows):
            fid = row[0]
            scores[fid] = scores.get(fid, 0.0) + 1.0 / (k + rank + 1)
            if fid not in meta:
                meta[fid] = row[1]
        ordered = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:top_k]
        results = []
        for fid, score in ordered:
            data = meta[fid]
            data = data if isinstance(data, dict) else json.loads(data)
            results.append(
                SearchResult(
                    func_id=fid,
                    name=data.get("name", ""),
                    domain=data.get("domain", ""),
                    relevance_score=score,
                    summary=data.get("trigger_text", "") or data.get("name", ""),
                )
            )
        return results

    def fts_search(self, text: str, top_k: int = 10) -> List[SearchResult]:
        return self.vector_search(text, top_k=top_k)

    def filter(self, filters: SearchFilters) -> List[Function]:
        """Structured filter over stored Functions.

        All SearchFilters fields are pushed into SQL as JSONB predicates
        (previously only ``owner`` was honoured, silently ignoring the
        rest). Mirrors the lite backend's ``_matches_filter``.
        """
        clauses: List[str] = []
        params: List[Any] = []
        if filters.domain:
            clauses.append("data->>'domain' = ANY(%s)")
            params.append(list(filters.domain))
        if filters.source_type:
            clauses.append("data->>'source_type' = ANY(%s)")
            params.append(
                [
                    st.value if isinstance(st, SourceType) else st
                    for st in filters.source_type
                ]
            )
        if filters.confidence_min is not None:
            clauses.append("(data->>'confidence')::float >= %s")
            params.append(filters.confidence_min)
        if filters.updated_after is not None:
            clauses.append("data->>'updated_at' >= %s")
            params.append(_iso(filters.updated_after))
        if filters.updated_before is not None:
            clauses.append("data->>'updated_at' <= %s")
            params.append(_iso(filters.updated_before))
        if filters.needs_review is not None:
            clauses.append("(data->>'needs_review')::boolean = %s")
            params.append(filters.needs_review)
        if filters.owner is not None:
            clauses.append("data->>'owner' = %s")
            params.append(filters.owner)
        sql = (
            "SELECT f.data FROM memplex_functions f "
            "JOIN (SELECT current_setting('memplex.tenant_id', true) AS tenant_id) scope "
            f"ON {_acl_scope_sql('f')}"
        )
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        cur = self._execute(sql, tuple(params), commit=False)
        funcs = []
        for row in cur.fetchall():
            data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
            funcs.append(_func_from_json(data))
        cur.close()
        return funcs

    def get(self, func_id: str) -> Optional[Function]:
        cur = self._execute(
            f"SELECT data FROM memplex_functions WHERE {_acl_scope_sql()} AND id = %s",
            (func_id,),
            commit=False,
        )
        row = cur.fetchone()
        cur.close()
        if row is None:
            return None
        data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        return _func_from_json(data)

    def get_neighbors(
        self,
        func_id: str,
        edge_types: Optional[List[str]] = None,
        max_hops: int = 1,
        limit: Optional[int] = None,
    ) -> List[Function]:
        """Bidirectional BFS over the edge table, matching lite semantics:
        honours *max_hops* depth, optional *edge_types* restriction, and
        traverses edges in both directions. A per-path cycle guard keeps
        the recursive CTE from looping on cyclic graphs."""
        if max_hops < 1 or (limit is not None and limit <= 0):
            return []
        if limit is not None and max_hops == 1:
            return self._get_neighbors_one_hop_limited(
                func_id,
                edge_types=edge_types,
                limit=limit,
            )
        type_clause = "AND e.edge_type = ANY(%s)" if edge_types else ""
        result_limit = "LIMIT %s" if limit is not None else ""
        sql = f"""
            WITH RECURSIVE hop(id, depth, path) AS (
                SELECT CASE WHEN e.source = %s THEN e.target ELSE e.source END,
                       1,
                       ARRAY[e.source::text, e.target::text]
                FROM memplex_edges e
                WHERE {_acl_scope_sql('e')}
                  AND (e.source = %s OR e.target = %s) {type_clause}
                UNION
                SELECT CASE WHEN e.source = h.id THEN e.target ELSE e.source END,
                       h.depth + 1,
                       h.path || (CASE WHEN e.source = h.id THEN e.target ELSE e.source END)::text
                FROM memplex_edges e
                JOIN hop h ON (e.source = h.id OR e.target = h.id)
                WHERE {_acl_scope_sql('e')}
                  AND h.depth < %s
                  AND NOT (CASE WHEN e.source = h.id THEN e.target ELSE e.source END) = ANY(h.path)
                  {type_clause}
            )
            SELECT DISTINCT f.data FROM memplex_functions f
            JOIN hop ON f.id = hop.id
            WHERE {_acl_scope_sql('f')}
            {result_limit}
        """
        params: List[Any] = [func_id, func_id, func_id]
        if edge_types:
            params.append(list(edge_types))
        params.append(max_hops)
        if edge_types:
            params.append(list(edge_types))
        if limit is not None:
            params.append(limit)
        cur = self._execute(sql, tuple(params), commit=False)
        funcs = []
        for row in cur.fetchall():
            data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
            funcs.append(_func_from_json(data))
        cur.close()
        return funcs

    def _get_neighbors_one_hop_limited(
        self,
        func_id: str,
        *,
        edge_types: Optional[List[str]],
        limit: int,
    ) -> List[Function]:
        """Fetch at most ``limit`` one-hop edge candidates before joining nodes."""

        type_clause = "AND edge_type = ANY(%s)" if edge_types else ""
        sql = f"""
            WITH bounded_neighbors AS (
                SELECT neighbor_id
                FROM (
                    SELECT target AS neighbor_id
                    FROM memplex_edges
                    WHERE {_acl_scope_sql()}
                      AND source = %s {type_clause}
                    UNION ALL
                    SELECT source AS neighbor_id
                    FROM memplex_edges
                    WHERE {_acl_scope_sql()}
                      AND target = %s {type_clause}
                ) edge_candidates
                WHERE neighbor_id <> %s
                LIMIT %s
            )
            SELECT f.data
            FROM memplex_functions f
            JOIN bounded_neighbors ON f.id = bounded_neighbors.neighbor_id
            WHERE {_acl_scope_sql('f')}
        """
        params: List[Any] = [func_id]
        if edge_types:
            params.append(list(edge_types))
        params.append(func_id)
        if edge_types:
            params.append(list(edge_types))
        params.extend([func_id, limit])
        cur = self._execute(sql, tuple(params), commit=False)
        funcs: List[Function] = []
        seen: set[str] = set()
        for row in cur.fetchall():
            data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
            func = _func_from_json(data)
            if func.id not in seen:
                funcs.append(func)
                seen.add(func.id)
        cur.close()
        return funcs

    def get_graph(self, func_ids: Optional[List[str]] = None) -> GraphData:
        if func_ids is not None:
            cur = self._execute(
                "SELECT source, target, edge_type, weight, evidence, created_at "
                f"FROM memplex_edges WHERE {_acl_scope_sql()} "
                "AND (source = ANY(%s) OR target = ANY(%s))",
                (list(func_ids), list(func_ids)),
                commit=False,
            )
        else:
            cur = self._execute(
                "SELECT source, target, edge_type, weight, evidence, created_at "
                f"FROM memplex_edges WHERE {_acl_scope_sql()}",
                commit=False,
            )
        edges = []
        for row in cur.fetchall():
            evidence = row[4]
            if isinstance(evidence, str):
                try:
                    evidence = json.loads(evidence)
                except Exception:
                    evidence = []
            edges.append(
                GraphEdge(
                    source=row[0],
                    target=row[1],
                    edge_type=row[2],
                    weight=float(row[3] or 1.0),
                    evidence=evidence or [],
                    created_at=row[5],
                )
            )
        cur.close()
        if func_ids is None:
            cur = self._execute(
                "SELECT data FROM memplex_functions "
                f"WHERE {_acl_scope_sql()} ORDER BY id",
                commit=False,
            )
        else:
            cur = self._execute(
                "SELECT data FROM memplex_functions "
                f"WHERE {_acl_scope_sql()} AND id = ANY(%s) ORDER BY id",
                (list(func_ids),),
                commit=False,
            )
        nodes = []
        for row in cur.fetchall():
            data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
            nodes.append(_func_from_json(data))
        cur.close()
        return GraphData(nodes=nodes, edges=edges)

    def get_timeline(self, func_id: str, limit: int = 20) -> List[ChangelogEvent]:
        cur = self._execute(
            "SELECT func_id, ts, event_type, description, source, actor "
            f"FROM memplex_changelog WHERE {_acl_scope_sql()} AND func_id = %s "
            "ORDER BY ts DESC LIMIT %s",
            (func_id, limit),
            commit=False,
        )
        events = []
        for row in cur.fetchall():
            events.append(
                ChangelogEvent(
                    func_id=row[0],
                    timestamp=row[1],
                    event_type=row[2],
                    description=row[3],
                    source=row[4],
                    actor=row[5],
                )
            )
        cur.close()
        return events

    def count_functions(self) -> int:
        cur = self._execute(
            f"SELECT count(*) FROM memplex_functions WHERE {_acl_scope_sql()}",
            commit=False,
        )
        row = cur.fetchone()
        cur.close()
        return int(row[0]) if row else 0

    def list_functions(
        self, offset: int = 0, limit: int = 1000, owner: Optional[str] = None
    ) -> List[Function]:
        if owner:
            cur = self._execute(
                "SELECT data FROM memplex_functions "
                f"WHERE {_acl_scope_sql()} AND data->>'owner' = %s "
                "ORDER BY data->>'updated_at' DESC OFFSET %s LIMIT %s",
                (owner, offset, limit),
                commit=False,
            )
        else:
            cur = self._execute(
                "SELECT data FROM memplex_functions "
                f"WHERE {_acl_scope_sql()} "
                "ORDER BY data->>'updated_at' DESC "
                "OFFSET %s LIMIT %s",
                (offset, limit),
                commit=False,
            )
        funcs = []
        for row in cur.fetchall():
            data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
            funcs.append(_func_from_json(data))
        cur.close()
        return funcs

    def list_changes_since(
        self, since: Optional[str] = None, limit: int = 100000
    ) -> List[Function]:
        """Incremental query: push the updated_at filter into Postgres.

        Overrides the base default (which loads all then filters in Python)
        so /sync/changes does not scan the entire table on every pull.
        """
        if since is None:
            return self.list_functions(limit=limit)
        cur = self._execute(
            "SELECT data FROM memplex_functions "
            f"WHERE {_acl_scope_sql()} "
            "AND data->>'updated_at' > %s "
            "ORDER BY data->>'updated_at' ASC LIMIT %s",
            (since, limit),
            commit=False,
        )
        funcs = []
        for row in cur.fetchall():
            data = row[0] if isinstance(row[0], dict) else json.loads(row[0])
            funcs.append(_func_from_json(data))
        cur.close()
        return funcs

    # ── Delete / merge / clear ──────────────────────────────────────

    def delete(self, func_id: str) -> None:
        context = self._authorization_context()
        with self._function_write_transaction(context, "delete") as cur:
            locked = self._locked_function_by_id(cur, func_id, context)
            if locked is None:
                return
            node, _identity = locked
            cur.execute(
                "SELECT source, target, edge_type FROM memplex_edges "
                f"WHERE {_acl_scope_sql()} AND (source = %s OR target = %s) "
                "ORDER BY source, target, edge_type FOR UPDATE",
                (func_id, func_id),
            )
            edges = sorted(
                cur.fetchall(),
                key=lambda edge: (str(edge[0]), str(edge[1]), str(edge[2])),
            )
            for source, target, edge_type in edges:
                edge_key = str(SyncEntityKey.edge(source, target, edge_type))
                self._bind_local_sync_context(cur, "edge", edge_key, {})
                cur.execute(
                    "DELETE FROM memplex_edges "
                    f"WHERE {_acl_scope_sql()} AND source = %s AND target = %s "
                    "AND edge_type = %s RETURNING source, target, edge_type",
                    (source, target, edge_type),
                )
                self._require_returning(cur, (source, target, edge_type))
            self._bind_local_sync_context(cur, "function", func_id, {})
            cur.execute(
                f"DELETE FROM memplex_functions WHERE {_acl_scope_sql()} AND id = %s RETURNING id",
                (func_id,),
            )
            if cur.fetchone() is None:
                return
            self._record_changelog(
                cur, func_id, "deleted", f"Deleted function: {func_id}", None, node=node
            )

    def merge(self, sub_graph: GraphData) -> MergeResult:
        # Graph input can bypass Function.__post_init__ by mutating dataclass
        # IDs or supplying duck objects.  Validate *all* nodes before opening
        # a transaction, guaranteeing no edge/index/counter write precedes a
        # rejected virtual-domain ID.
        nodes = list(sub_graph.nodes)
        for node in nodes:
            if not isinstance(node, Function):
                raise ValueError("PostgreSQL 图节点必须是 Function")
            validate_func_id(node.id)
            validate_domain(node.domain)
        context = self._authorization_context()
        result = MergeResult(merged=True)
        with self._function_write_transaction(context, "merge") as cur:
            # Resolve every incoming graph node before writing an edge.  The
            # resulting map makes an edge submitted for a duplicate normalized
            # id point at its immutable canonical row, not a rejected alias.
            canonical_ids: dict[str, str] = {}
            canonical_nodes: dict[str, Function] = {}
            inserted_ids: set[str] = set()
            updated_ids: set[str] = set()
            # Identity binding happens before the deterministic sort so the
            # caller cannot influence lock order with forged scope fields.
            for node in nodes:
                self._write_identity(node)
            for node in sorted(
                nodes,
                key=lambda item: self._function_merge_sort_key(item, context),
            ):
                canonical, created = self._merge_or_insert_function(cur, node, context)
                canonical_ids[node.id] = canonical.id
                canonical_nodes[canonical.id] = canonical
                if created:
                    inserted_ids.add(canonical.id)
                else:
                    updated_ids.add(canonical.id)

            # A canonical row created then encountered again in the same graph
            # remains one new row, not one new plus one update.
            result.new_functions = len(inserted_ids)
            result.updated_functions = len(updated_ids - inserted_ids)
            node_visibilities = {
                str(node.visibility or "workspace").strip().lower()
                for node in canonical_nodes.values()
            }
            if "session" in node_visibilities:
                edge_visibility = "session"
            elif "user" in node_visibilities:
                edge_visibility = "user"
            elif "workspace" in node_visibilities:
                edge_visibility = "workspace"
            else:
                edge_visibility = "user"
            edge_identity = self._row_identity_values(
                context, visibility=edge_visibility
            )
            resolved_edges = [
                (
                    edge,
                    canonical_ids.get(edge.source, edge.source),
                    canonical_ids.get(edge.target, edge.target),
                )
                for edge in sub_graph.edges
            ]

            # Lock every concrete, external endpoint once and in ID order
            # before edge DML.  This is intentionally a *batch* lock: two
            # reverse payloads A→B / B→A must both acquire A then B instead
            # of deadlocking while each holds its own source row.  BELONGS_TO
            # targets are checked below against the loaded source and are
            # never Function rows.
            external_endpoint_ids: set[str] = set()
            for edge, source, target in resolved_edges:
                if source not in canonical_nodes:
                    validate_func_id(source)
                    external_endpoint_ids.add(source)
                if edge.edge_type != "BELONGS_TO" and target not in canonical_nodes:
                    validate_func_id(target)
                    external_endpoint_ids.add(target)
            external_nodes: dict[str, Function] = {}
            for endpoint_id in sorted(external_endpoint_ids):
                locked = self._require_visible_function_endpoint(
                    cur, endpoint_id, context
                )
                external_nodes[endpoint_id] = locked[0]

            for edge, source, target in sorted(
                resolved_edges, key=lambda item: (item[1], item[2], item[0].edge_type)
            ):
                source_node = canonical_nodes.get(source) or external_nodes.get(source)
                if edge.edge_type == "BELONGS_TO":
                    if not self._is_graph_builder_domain_endpoint(
                        edge, source_node, target
                    ):
                        raise PostgresWriteRejected(
                            "PostgreSQL write did not affect an authorized row"
                        )
                elif target not in canonical_nodes and target not in external_nodes:
                    # Defensive invariant: the deterministic prelock set
                    # above must cover every concrete endpoint.
                    raise PostgresWriteRejected(
                        "PostgreSQL write did not affect an authorized row"
                    )
                cur.execute(
                    "SELECT 1 FROM memplex_edges "
                    f"WHERE {_acl_scope_sql()} "
                    "AND source = %s AND target = %s AND edge_type = %s",
                    (source, target, edge.edge_type),
                )
                is_new_edge = cur.fetchone() is None
                edge_created_at = edge.created_at or datetime.now(timezone.utc)
                if edge_created_at.tzinfo is None:
                    edge_created_at = edge_created_at.replace(tzinfo=timezone.utc)
                else:
                    edge_created_at = edge_created_at.astimezone(timezone.utc)
                edge_payload = {
                    "weight": float(edge.weight),
                    "evidence": list(edge.evidence or []),
                    "created_at": edge_created_at.strftime(
                        "%Y-%m-%dT%H:%M:%S.%fZ"
                    ),
                }
                self._bind_local_sync_context(
                    cur,
                    "edge",
                    str(SyncEntityKey.edge(source, target, edge.edge_type)),
                    edge_payload,
                )
                evidence = json.dumps(edge.evidence or [])
                cur.execute(
                    f"""
                    INSERT INTO memplex_edges
                        (source, target, edge_type, weight, evidence, created_at, tenant_id,
                         owner_subject, workspace, visibility, source_agent, source_session)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (tenant_id, source, target, edge_type) DO UPDATE SET
                        weight = EXCLUDED.weight,
                        evidence = EXCLUDED.evidence,
                        created_at = EXCLUDED.created_at,
                        owner_subject = EXCLUDED.owner_subject,
                        workspace = EXCLUDED.workspace,
                        visibility = EXCLUDED.visibility,
                        source_agent = EXCLUDED.source_agent,
                        source_session = EXCLUDED.source_session
                    WHERE {_acl_scope_sql('memplex_edges')}
                    RETURNING source, target, edge_type
                    """,
                    (
                        source,
                        target,
                        edge.edge_type,
                        float(edge.weight),
                        evidence,
                        edge_created_at,
                        *edge_identity,
                    ),
                )
                self._require_returning(cur, (source, target, edge.edge_type))
                if is_new_edge:
                    result.new_edges += 1
            for canonical_id, node in canonical_nodes.items():
                self._record_changelog(
                    cur,
                    canonical_id,
                    "created" if canonical_id in inserted_ids else "updated",
                    "Merged graph node",
                    None,
                    node=node,
                )
        return result

    def clear(self) -> None:
        scope = f" WHERE {_acl_scope_sql()}"
        context = self._authorization_context()
        with self._function_write_transaction(context, "clear") as cur:
            if self._sync_capture_policy.mode != "required":
                # Legacy behavior: bulk clear with no per-row change context.
                cur.execute("DELETE FROM memplex_functions" + scope)
                cur.execute("DELETE FROM memplex_edges" + scope)
                cur.execute("DELETE FROM memplex_observations" + scope)
                cur.execute("DELETE FROM memplex_facts" + scope)
                cur.execute("DELETE FROM memplex_preferences" + scope)
                cur.execute("DELETE FROM memplex_changelog" + scope)
                return

            cur.execute(
                "SELECT source, target, edge_type FROM memplex_edges "
                f"WHERE {_acl_scope_sql()} ORDER BY source, target, edge_type FOR UPDATE"
            )
            for source, target, edge_type in cur.fetchall():
                self._bind_local_sync_context(
                    cur,
                    "edge",
                    str(SyncEntityKey.edge(source, target, edge_type)),
                    {},
                )
                cur.execute(
                    "DELETE FROM memplex_edges "
                    f"WHERE {_acl_scope_sql()} AND source = %s AND target = %s "
                    "AND edge_type = %s RETURNING source, target, edge_type",
                    (source, target, edge_type),
                )
                self._require_returning(cur, (source, target, edge_type))

            cur.execute(
                f"SELECT id FROM memplex_functions WHERE {_acl_scope_sql()} ORDER BY id FOR UPDATE"
            )
            for (func_id,) in cur.fetchall():
                self._bind_local_sync_context(cur, "function", func_id, {})
                cur.execute(
                    f"DELETE FROM memplex_functions WHERE {_acl_scope_sql()} AND id = %s "
                    "RETURNING id",
                    (func_id,),
                )
                self._require_returning(cur, (func_id,))

            for table_name, node_type in (
                ("memplex_observations", "observation"),
                ("memplex_facts", "fact"),
                ("memplex_preferences", "preference"),
            ):
                cur.execute(
                    f"SELECT id FROM {table_name} WHERE {_acl_scope_sql(table_name)} "
                    f"ORDER BY id FOR UPDATE"
                )
                for (node_id,) in cur.fetchall():
                    self._bind_local_sync_context(cur, node_type, node_id, {})
                    cur.execute(
                        f"DELETE FROM {table_name} WHERE {_acl_scope_sql(table_name)} "
                        "AND id = %s RETURNING id",
                        (node_id,),
                    )
                    self._require_returning(cur, (node_id,))

            cur.execute("DELETE FROM memplex_changelog" + scope)
