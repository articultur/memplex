"""Multi-node memory sharing: local cache + remote push/pull.

When ``MEMPLEX_REMOTE_URL`` is set, ``create_store`` wraps the local
``LiteMemoryStore`` in a :class:`SyncableStore`. Writes go to the local
store first (so the host keeps working offline) and are then pushed to the
central HTTP server. Other nodes pull those changes on demand via
:meth:`pull_incremental` (exposed as ``memplex sync pull``).

Conflict policy is last-write-wins by ``updated_at`` for all four memory
node types (functions, facts, preferences, observations). The legacy
best-effort transport only has durable deletion semantics for Functions.
Typed deletes therefore fail closed while that transport is active: a local
delete is never presented as a remotely replicated one.

Architecture::

    node A (SyncableStore) --push-->  central server (HTTP /sync/push)
    node B (SyncableStore) --pull-->  central server (HTTP /sync/changes)

Reads stay local-first (fast, offline-capable); callers that need the
latest remote state call ``pull_incremental`` explicitly before reading.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from collections.abc import Callable, Iterable
from contextvars import ContextVar
from functools import wraps
from typing import TYPE_CHECKING, Any, Optional

from memplex.auth import AuthorizationContext, resolve_environment_authorization
from memplex.config import validate_sync_remote_url

if TYPE_CHECKING:
    from memplex.models import (
        Fact,
        Function,
        GraphData,
        MemoryNode,
        Observation,
        Preference,
        SourceDocument,
    )

logger = logging.getLogger(__name__)


# A sync wrapper can sit in front of a production PostgreSQL store.  Keep
# request identity in the execution context rather than on the wrapper: one
# SyncableStore is shared by concurrent requests, and a mutable attribute
# would let one request's principal leak into another.  The wrapper resolves
# this scope immediately before every local operation; async push workers only
# consume already-serialized payloads and therefore never need this context.
_SYNC_SCOPE: ContextVar[AuthorizationContext | None] = ContextVar(
    "memplex_sync_scope", default=None
)


class _AuthorizedSyncableStore:
    """Request-scoped facade preserving SyncableStore write-through behavior.

    Delegating ``authorized()`` straight to the wrapped local store bypasses
    SyncableStore's push/pull methods.  This facade installs the trusted
    context for the duration of every call while keeping the outer wrapper as
    the call target.
    """

    def __init__(self, store: SyncableStore, context: AuthorizationContext) -> None:
        self._store = store
        self._context = context

    @property
    def local(self) -> Any:
        """Expose the same context-scoped local backend for diagnostics."""
        return self._store._local_for_context(self._context)

    def __getattr__(self, name: str) -> Any:
        # Resolve delegated reads only after installing the scope.  Resolving
        # ``store.get`` first would make SyncableStore.__getattr__ capture a
        # raw strict-PostgreSQL bound method, which cannot be repaired by
        # setting the ContextVar afterwards.
        token = _SYNC_SCOPE.set(self._context)
        try:
            target = getattr(self._store, name)
        finally:
            _SYNC_SCOPE.reset(token)
        if not callable(target):
            return target

        @wraps(target)
        def scoped_call(*args: Any, **kwargs: Any) -> Any:
            token = _SYNC_SCOPE.set(self._context)
            try:
                return getattr(self._store, name)(*args, **kwargs)
            finally:
                _SYNC_SCOPE.reset(token)

        return scoped_call


def _node_to_payload(node: Any) -> dict:
    """Serialize a memory node for the sync wire format.

    Uses the models-standard ``to_dict`` (canonical shape covering every
    field; Fact's ``object_`` becomes ``"object"``). Falls back to the
    adapter dataclass serializer for duck-typed nodes without ``to_dict``.
    """
    to_dict = getattr(node, "to_dict", None)
    if callable(to_dict):
        return to_dict()
    # Layer-neutral serializer: the domain must not import adapters
    # (import-linter contract).
    from memplex.serialization import dataclass_to_dict

    return dataclass_to_dict(node)


# ── Remote sync configuration ────────────────────────────────────────


class RemoteSyncConfig:
    """Resolved remote-sync configuration (from env).

    Attributes
    ----------
    url:
        Base URL of the central Memplex HTTP server, e.g.
        ``https://memplex.example.org``. ``None`` disables sync entirely.
    api_key:
        Shared secret sent as ``X-API-Key``. ``None`` when the server
        has no auth configured.
    bearer:
        Alternative bearer token sent as ``Authorization: Bearer <t>``.
    enabled:
        Master switch (``MEMPLEX_SYNC_ENABLED=0`` disables even when a
        URL is set).
    """

    def __init__(self) -> None:
        profile = os.environ.get(
            "MEMPLEX_DEPLOYMENT_PROFILE", "development"
        ).strip().lower()
        raw_url = os.environ.get("MEMPLEX_REMOTE_URL") or ""
        self.url = (
            validate_sync_remote_url(raw_url, profile=profile) if raw_url else None
        )
        # Read-replica URL (plan D): pull reads from here when set, push
        # still goes to the primary url (write authority). Enables read
        # scaling via Postgres streaming replication or multiple read-only
        # server instances. MEMPLEX_READ_URL=http://replica:8900
        raw_read_url = os.environ.get("MEMPLEX_READ_URL") or ""
        self.read_url = (
            validate_sync_remote_url(raw_read_url, profile=profile)
            if raw_read_url
            else None
        )
        self.principal_token = os.environ.get("MEMPLEX_PRINCIPAL_TOKEN")
        self.api_key = (
            self.principal_token
            or os.environ.get("MEMPLEX_REMOTE_API_KEY")
            or os.environ.get("MEMPLEX_API_KEY")
        )
        self.bearer = os.environ.get("MEMPLEX_REMOTE_BEARER_TOKEN") or os.environ.get(
            "MEMPLEX_BEARER_TOKEN"
        )
        # P2P peers: comma-separated list of additional node URLs. Each is
        # treated the same as the primary url for pull/push. Enables mesh
        # sync without a single central server. MEMPLEX_PEERS=url1,url2,...
        peers_raw = os.environ.get("MEMPLEX_PEERS", "")
        self.peers: list[str] = [
            validate_sync_remote_url(item.strip(), profile=profile)
            for item in peers_raw.split(",")
            if item.strip()
        ]
        self.enabled = (self.url is not None or self.peers) and (
            os.environ.get("MEMPLEX_SYNC_ENABLED", "1").lower() not in ("0", "false", "no", "off")
        )
        self.agent_id = os.environ.get("MEMPLEX_AGENT_ID", "")
        self.session_id = os.environ.get("MEMPLEX_SESSION_ID", "")
        self.authorization: AuthorizationContext | None = None
        registry_configured = os.environ.get("MEMPLEX_PRINCIPALS_JSON") is not None
        if self.active and (registry_configured or profile == "production"):
            self.authorization = resolve_environment_authorization(
                agent_id=None,
                session_id=self.session_id,
                provenance={"transport": "sync"},
                require_registry=True,
            )
            # require_registry=True fails closed (raises) instead of returning
            # None, so the resolved context is always present here.
            assert self.authorization is not None
            self.agent_id = self.authorization.agent_id or self.agent_id
            self.session_id = self.authorization.session_id
        # Auto-pull interval in seconds. 0 (default) = disabled; pull stays
        # on-demand (memplex sync pull). A positive value starts a daemon
        # thread that calls pull_incremental on that cadence.
        try:
            self.auto_pull_interval = int(os.environ.get("MEMPLEX_SYNC_PULL_INTERVAL", "0"))
        except ValueError:
            self.auto_pull_interval = 0
        # SSE push notifications: when active, the client connects to the
        # server's /sync/events stream and pulls immediately on each event.
        # MEMPLEX_SSE_ENABLED=0 disables (fall back to polling/manual pull).
        self.sse_enabled = os.environ.get("MEMPLEX_SSE_ENABLED", "1").lower() not in (
            "0",
            "false",
            "no",
            "off",
        )

    def all_targets(self) -> list:
        """Return every remote URL to sync with (primary + peers), deduped."""
        seen, out = set(), []
        for u in [self.url] + self.peers if self.url else self.peers:
            if u and u not in seen:
                seen.add(u)
                out.append(u)
        return out

    @property
    def active(self) -> bool:
        """True when sync is configured and enabled.

        Active when there is at least one sync target (primary url OR any
        P2P peer) and the master switch is on.
        """
        return bool(self.enabled and (self.url or self.peers))


# ── SyncableStore wrapper ────────────────────────────────────────────


class SyncableStore:
    """Wrap a local MemoryStore, pushing writes to and pulling from a remote.

    Read methods are transparently delegated to the local store via
    ``__getattr__``. Write methods (``add``/``add_batch``/``merge``/
    ``delete``/``increment_access``/``increment_access_batch``) write
    locally first, then best-effort push to the remote. A remote failure
    is logged at debug level and never raised -- the local write has
    already succeeded and the host stays functional offline.

    Use :meth:`pull_incremental` to fetch the latest remote state into
    the local store (``memplex sync pull``).
    """

    def __init__(self, local: Any, config: RemoteSyncConfig | None = None) -> None:
        self._local = local
        self._config = config or RemoteSyncConfig()
        self._last_pull_at: str | None = None
        self._push_failures = 0
        # Injectable HTTP layer (defaults to the real `requests` module,
        # lazily imported so sync stays optional). Tests replace this with
        # a stub to exercise push/pull without a live server.
        self._http: Any = None
        # Development-only best-effort push is deliberately bounded. Production
        # ``sync_capture=required`` bypasses SyncableStore and uses the durable
        # outbox dispatcher; this legacy path must still avoid an unbounded
        # in-memory future list when a remote is slow or unavailable.
        self._push_queue_capacity = 256
        self._push_queue: queue.Queue[
            tuple[Callable[..., None], tuple[Any, ...]]
        ] = queue.Queue(maxsize=self._push_queue_capacity)
        self._push_condition = threading.Condition()
        self._push_pending = 0
        self._push_workers: list[threading.Thread] = []
        # Auto-pull worker state (started by start_auto_pull).
        self._auto_pull_thread: threading.Thread | None = None
        self._auto_pull_stop = threading.Event()
        # SSE push-notification listener state. `_sse_thread` points at the
        # first listener thread (backward-compatible single-target view);
        # `_sse_threads` tracks all of them (one per sync target).
        self._sse_thread: threading.Thread | None = None
        self._sse_threads: list = []
        self._sse_stop = threading.Event()

    def _requests(self) -> Any:
        if self._http is None:
            import requests

            self._http = requests
        return self._http

    def authorized(self, context: AuthorizationContext) -> _AuthorizedSyncableStore:
        """Return a request-scoped sync facade for a trusted principal.

        The facade deliberately wraps *this* object rather than returning
        ``self._local.authorized(context)``.  The latter correctly scopes
        PostgreSQL but silently bypasses write-through push and pull/apply
        behavior, leaving a production remote node out of sync.
        """
        if not isinstance(context, AuthorizationContext):
            raise TypeError("context must be an AuthorizationContext")
        return _AuthorizedSyncableStore(self, context)

    def _local_for_context(self, context: AuthorizationContext | None = None) -> Any:
        """Return local storage constrained by the explicit active identity.

        This helper intentionally does *not* use the managed remote identity
        as a general fallback.  Doing so would make raw ``store.get()`` or
        ``store.add()`` calls authenticated in production.  Background pull
        has a narrow, separately-audited re-entry path in
        :meth:`pull_incremental`.
        """
        context = context or _SYNC_SCOPE.get()
        if context is None:
            return self._local
        authorize = getattr(self._local, "authorized", None)
        return authorize(context) if callable(authorize) else self._local

    def _scoped_local(self) -> Any:
        """Resolve local storage for the current facade call."""
        return self._local_for_context()

    # ── Transparent read delegation ────────────────────────────────

    def __getattr__(self, name: str) -> Any:
        # Only called when the attribute is not found on SyncableStore
        # itself -> delegate every read/query/get/list to the local store.
        try:
            return getattr(self._scoped_local(), name)
        except AttributeError:
            raise AttributeError(
                f"{type(self).__name__!s}(wrapping {type(self._local).__name__!s}) "
                f"has no attribute {name!r}"
            ) from None

    @property
    def local(self) -> Any:
        """Expose the underlying local store (for sync internals + tests)."""
        return self._local

    @property
    def last_pull_at(self) -> str | None:
        return self._last_pull_at

    # ── Write methods: local first, then best-effort push ──────────

    def add(self, func: Function, source: SourceDocument) -> None:
        self._scoped_local().add(func, source)
        self._push_functions([func])

    def add_batch(self, funcs: list[Function], sources: list[SourceDocument]) -> None:
        # Store contract (base/lite/postgres): add_batch takes a list of
        # SourceDocuments parallel to funcs, not a single source.
        self._scoped_local().add_batch(funcs, sources)
        self._push_functions(list(funcs))

    def merge(self, sub_graph: GraphData) -> None:
        self._scoped_local().merge(sub_graph)
        # sub_graph.nodes carry the merged Functions; push them.
        nodes = getattr(sub_graph, "nodes", None) or []
        self._push_functions(list(nodes))

    def delete(self, func_id: str) -> None:
        self._scoped_local().delete(func_id)
        # Deletion propagation: tell the server to delete + tombstone.
        self._push_delete(func_id)

    def increment_access(self, func_id: str) -> None:
        # Access-count churn is local-only; pushing it would flood the
        # server with per-query writes (the exact anti-pattern we just
        # fixed). Pull merges server-side access_count via LWW.
        self._scoped_local().increment_access(func_id)

    def increment_access_batch(self, func_ids: list[str]) -> None:
        self._scoped_local().increment_access_batch(func_ids)

    # ── Typed-node writes (Fact / Preference / Observation) ────────
    # Same local-first + best-effort-push contract as add(). Unlike upserts,
    # a typed delete cannot use this legacy transport: it has no durable
    # tombstone/retry protocol. Refuse before the local mutation so callers
    # never mistake a local-only delete for replicated state.

    def add_fact(self, fact: Fact) -> None:
        self._scoped_local().add_fact(fact)
        self._push_typed_nodes(facts=[fact])

    def add_preference(self, preference: Preference) -> None:
        self._scoped_local().add_preference(preference)
        self._push_typed_nodes(preferences=[preference])

    def add_observation(self, observation: Observation) -> None:
        self._scoped_local().add_observation(observation)
        self._push_typed_nodes(observations=[observation])

    def delete_fact(self, fact_id: str) -> None:
        self._reject_legacy_typed_tombstone()
        self._scoped_local().delete_fact(fact_id)

    def delete_preference(self, preference_id: str) -> None:
        self._reject_legacy_typed_tombstone()
        self._scoped_local().delete_preference(preference_id)

    def delete_observation(self, observation_id: str) -> None:
        self._reject_legacy_typed_tombstone()
        self._scoped_local().delete_observation(observation_id)

    def _reject_legacy_typed_tombstone(self) -> None:
        """Reject typed deletes when the lossy legacy remote is active."""
        if self._config.active:
            raise RuntimeError("legacy_typed_tombstone_unsupported")

    # ── Push helpers (best-effort) ─────────────────────────────────

    def _auth_headers(self) -> dict:
        headers = {}
        if self._config.api_key:
            headers["X-API-Key"] = self._config.api_key
        if self._config.bearer:
            headers["Authorization"] = f"Bearer {self._config.bearer}"
        if self._config.agent_id:
            headers["X-Memplex-Agent-ID"] = self._config.agent_id
        if self._config.session_id:
            headers["X-Memplex-Session-ID"] = self._config.session_id
        return headers

    def _push_functions(self, funcs: list[Function]) -> None:
        """Schedule an async push of Functions to every target.

        Returns immediately; the actual HTTP POST runs on the bounded daemon
        worker queue. Previously this blocked the caller (add/merge) for up
        to 10s per target when a server was slow or unreachable.
        """
        if not self._config.active or not funcs:
            return
        try:
            payload = {"functions": [_node_to_payload(f) for f in funcs]}
        except Exception:  # noqa: BLE001 - logged degradation path
            logger.debug("sync push serialisation failed")
            return
        for target in self._config.all_targets():
            self._enqueue_push(self._do_push_functions, target, payload)

    def _push_typed_nodes(
        self,
        facts: Iterable[Fact] = (),
        preferences: Iterable[Preference] = (),
        observations: Iterable[Observation] = (),
    ) -> None:
        """Schedule an async push of Fact/Preference/Observation nodes.

        Same best-effort async contract as :meth:`_push_functions`; the
        payload lands on the same ``/sync/push`` endpoint under the
        ``facts`` / ``preferences`` / ``observations`` keys (older servers
        ignore unknown keys).
        """
        if not self._config.active or not (facts or preferences or observations):
            return
        try:
            payload = {
                "facts": [_node_to_payload(f) for f in facts],
                "preferences": [_node_to_payload(p) for p in preferences],
                "observations": [_node_to_payload(o) for o in observations],
            }
        except Exception:  # noqa: BLE001 - logged degradation path
            logger.debug("sync typed-node push serialisation failed")
            return
        for target in self._config.all_targets():
            self._enqueue_push(self._do_push_functions, target, payload)

    def _safe_list(self, method_name: str) -> list:
        """Call a local ``list_*`` API, tolerating backends without it."""
        lister = getattr(self._scoped_local(), method_name, None)
        if not callable(lister):
            return []
        try:
            return list(lister(limit=100000))
        except Exception:  # noqa: BLE001 - logged degradation path
            logger.debug("local %s unavailable", method_name)
            return []

    def push_local_typed_nodes(self) -> dict:
        """Sweep local Fact/Preference/Observation state and push it.

        Functions push write-through on ``add()``; typed nodes written
        before sync was configured (or through paths that bypass
        SyncableStore) never reach the remote. This sweep reads the local
        ``list_facts`` / ``list_preferences`` / ``list_observations`` APIs
        (duck-typed; backends without support return nothing) and pushes
        everything. The remote applies LWW, so re-pushing is idempotent.

        Returns the counts scheduled for push per type.
        """
        facts = self._safe_list("list_facts")
        preferences = self._safe_list("list_preferences")
        observations = self._safe_list("list_observations")
        self._push_typed_nodes(facts=facts, preferences=preferences, observations=observations)
        return {
            "facts": len(facts),
            "preferences": len(preferences),
            "observations": len(observations),
        }

    @property
    def pending_push_tasks(self) -> int:
        """Return queued plus active legacy push tasks."""
        with self._push_condition:
            return self._push_pending

    def _ensure_push_workers_locked(self) -> None:
        if self._push_workers:
            return
        for index in range(2):
            worker = threading.Thread(
                target=self._push_worker_loop,
                name=f"memplex-sync-push-{index}",
                daemon=True,
            )
            worker.start()
            self._push_workers.append(worker)

    def _enqueue_push(self, operation: Callable[..., None], *args: Any) -> bool:
        """Enqueue one best-effort legacy push without exceeding the hard cap."""
        with self._push_condition:
            if self._push_pending >= self._push_queue_capacity:
                self._push_failures += 1
                logger.debug("sync legacy push queue full; dropping newest task")
                return False
            self._push_pending += 1
            self._ensure_push_workers_locked()
        try:
            self._push_queue.put_nowait((operation, args))
        except queue.Full:
            with self._push_condition:
                self._push_pending -= 1
                self._push_condition.notify_all()
            self._push_failures += 1
            logger.debug("sync legacy push queue full; dropping newest task")
            return False
        return True

    def _push_worker_loop(self) -> None:
        while True:
            operation, args = self._push_queue.get()
            try:
                operation(*args)
            except BaseException:  # noqa: BLE001 - shutdown/cleanup semantics; primary error stays authoritative
                self._push_failures += 1
                logger.debug("sync legacy push worker failed")
            finally:
                self._push_queue.task_done()
                with self._push_condition:
                    self._push_pending -= 1
                    self._push_condition.notify_all()

    def _do_push_functions(self, target: str, payload: dict) -> None:
        """Worker: POST a /sync/push payload to one target.

        Generic over the payload shape: Function batches carry
        ``{"functions": [...]}``, typed-node batches carry
        ``facts`` / ``preferences`` / ``observations`` keys.
        """
        try:
            from memplex import sync_crypto

            body = sync_crypto.encrypt_json_payload(payload)
            resp = self._requests().post(
                f"{target}/sync/push",
                json=body,
                headers=self._auth_headers(),
                timeout=10,
            )
            if resp.status_code >= 400:
                logger.debug("sync legacy push rejected (HTTP %s)", resp.status_code)
                self._push_failures += 1
        except Exception:  # noqa: BLE001 - logged degradation path
            logger.debug("sync legacy push transport failed")
            self._push_failures += 1

    def _push_delete(self, func_id: str) -> None:
        """Schedule an async delete push to every target."""
        if not self._config.active:
            return
        for target in self._config.all_targets():
            self._enqueue_push(self._do_push_delete, target, func_id)

    def _do_push_delete(self, target: str, func_id: str) -> None:
        """Worker: DELETE on one target.

        Mirrors ``_do_push_functions``: HTTP rejections and transport
        errors both count as push failures (previously neither was
        checked, so delete pushes failed silently).
        """
        try:
            resp = self._requests().delete(
                f"{target}/memories/{func_id}",
                headers=self._auth_headers(),
                timeout=10,
            )
            if resp.status_code >= 400:
                logger.debug("sync legacy delete rejected (HTTP %s)", resp.status_code)
                self._push_failures += 1
        except Exception:  # noqa: BLE001 - logged degradation path
            logger.debug("sync legacy delete transport failed")
            self._push_failures += 1

    def flush_push(self, timeout: float = 5.0) -> None:
        """Wait for queued push tasks to finish (best-effort).

        Useful in tests and on shutdown to avoid asserting before the
        async push has reached the server. The pending counter includes both
        queued and active work, so a zero value proves the bounded queue is
        fully drained even with multiple workers.
        """
        deadline = time.monotonic() + max(0.0, timeout)
        with self._push_condition:
            while self._push_pending:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return
                self._push_condition.wait(timeout=remaining)

    # ── Pull ───────────────────────────────────────────────────────

    def pull_incremental(self, since: str | None = None) -> dict:
        """Pull changes newer than *since* (ISO-8601) from the remote.

        Applies Functions, Facts, Preferences and Observations with LWW
        (an incoming node wins only if newer than the local copy) and
        replicates tombstones (Functions only). Typed deletes are rejected
        before local mutation while the legacy transport is active, so this
        pull path never has to reconcile a local-only typed deletion. Updates
        ``last_pull_at`` to the server's reported time for the next call.

        Returns a summary dict: ``{pulled, applied, rejected_older,
        deleted, tombstones_skipped_edit, facts_applied,
        facts_rejected_older, preferences_applied,
        preferences_rejected_older, observations_applied,
        observations_rejected_older, server_time}``. ``pulled`` counts
        Function changes only (the historical meaning); typed-node counts
        live in their own keys.

        When sync is inactive (no ``MEMPLEX_REMOTE_URL``), returns a
        no-op summary without touching the network.
        """
        if not self._config.active:
            return {
                "pulled": 0,
                "applied": 0,
                "rejected_older": 0,
                "deleted": 0,
                "tombstones_skipped_edit": 0,
                "facts_applied": 0,
                "facts_rejected_older": 0,
                "preferences_applied": 0,
                "preferences_rejected_older": 0,
                "observations_applied": 0,
                "observations_rejected_older": 0,
                "server_time": None,
                "skipped": "sync not active (no MEMPLEX_REMOTE_URL)",
            }

        # Auto-pull and SSE worker threads are not request-scoped.  They may
        # enter the pull path only through the RemoteSyncConfig identity that
        # was resolved from the principal registry at configuration time.
        # Re-enter through the public facade so the *entire* fetch/apply path
        # receives one ContextVar scope.  Deliberately do not make that
        # identity a generic local-store fallback: raw get/add/delete remain
        # fail-closed in production.
        if _SYNC_SCOPE.get() is None:
            managed_context = self._config.authorization
            if managed_context is not None:
                return self.authorized(managed_context).pull_incremental(since)
            if bool(getattr(self._local, "_require_authorization", False)):
                raise PermissionError(
                    "production sync pull requires an authorized facade or "
                    "a registry-validated remote principal"
                )
        from memplex.models import Function, SourceDocument, SourceType

        cutoff = since or self._last_pull_at
        # Pull targets: prefer read-replica (MEMPLEX_READ_URL) when set
        # (plan D: read scaling); otherwise fall back to all push targets
        # (primary + P2P peers). The write authority stays at the primary.
        if self._config.read_url:
            pull_targets = [self._config.read_url]
        else:
            pull_targets = self._config.all_targets()
        changes = []
        fact_changes = []
        preference_changes = []
        observation_changes = []
        tombstones = []
        server_time = None
        for target in pull_targets:
            try:
                resp = self._requests().get(
                    f"{target}/sync/changes",
                    params={"since": cutoff} if cutoff else {},
                    headers=self._auth_headers(),
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()
                changes.extend(data.get("changes", []))
                # Typed-node keys are absent on older servers -> [].
                fact_changes.extend(data.get("fact_changes", []))
                preference_changes.extend(data.get("preference_changes", []))
                observation_changes.extend(data.get("observation_changes", []))
                tombstones.extend(data.get("tombstones", []))
                if data.get("server_time"):
                    server_time = data["server_time"]
            except Exception:  # noqa: BLE001 - logged degradation path
                logger.debug("sync legacy pull transport failed")

        applied = 0
        rejected_older = 0
        local_store = self._scoped_local()
        for raw in changes:
            func_id = raw.get("id")
            if not func_id:
                continue
            existing = local_store.get(func_id)
            if existing is not None and (raw.get("updated_at") or "") <= (
                existing.updated_at or ""
            ):
                rejected_older += 1
                continue
            try:
                incoming = Function.from_dict(raw)
                local_store.add(
                    incoming,
                    SourceDocument(type="sync_pull", source_type=SourceType.WIKI),
                )
                applied += 1
            except Exception as exc:  # noqa: BLE001 - logged degradation path
                logger.debug("sync pull: skip unparseable change %s: %s", func_id, exc)

        from memplex.models import Fact, Observation, Preference

        facts_applied, facts_rejected = self._apply_typed_pull(
            fact_changes, cls=Fact, getter_name="get_fact", adder_name="add_fact"
        )
        prefs_applied, prefs_rejected = self._apply_typed_pull(
            preference_changes,
            cls=Preference,
            getter_name="get_preference",
            adder_name="add_preference",
        )
        obs_applied, obs_rejected = self._apply_typed_pull(
            observation_changes,
            cls=Observation,
            getter_name=None,
            adder_name="add_observation",
        )

        deleted = 0
        tombstones_skipped_edit = 0
        for t in tombstones:
            fid = t.get("func_id")
            if not fid:
                continue
            local = local_store.get(fid)
            if local is None:
                continue  # already absent, nothing to delete
            # Delete-vs-edit fix: if the tombstone carries a deleted_version
            # and the local copy is NEWER, the edit happened after the
            # delete -- keep the edit, skip the tombstone.
            tomb_deleted_version = t.get("deleted_version", "")
            local_updated = getattr(local, "updated_at", None) or ""
            if tomb_deleted_version and local_updated > tomb_deleted_version:
                tombstones_skipped_edit += 1
                continue
            local_store.delete(fid)
            deleted += 1

        if server_time:
            self._last_pull_at = server_time

        return {
            "pulled": len(changes),
            "applied": applied,
            "rejected_older": rejected_older,
            "deleted": deleted,
            "tombstones_skipped_edit": tombstones_skipped_edit,
            "facts_applied": facts_applied,
            "facts_rejected_older": facts_rejected,
            "preferences_applied": prefs_applied,
            "preferences_rejected_older": prefs_rejected,
            "observations_applied": obs_applied,
            "observations_rejected_older": obs_rejected,
            "server_time": server_time,
        }

    def _apply_typed_pull(
        self,
        changes: list[dict[str, Any]],
        *,
        cls: type[MemoryNode],
        getter_name: str | None,
        adder_name: str,
    ) -> tuple[int, int]:
        """Apply pulled Fact/Preference/Observation changes locally with LWW.

        Returns ``(applied, rejected_older)``. Duck-typed: a local backend
        without the typed add API contributes nothing. There is no
        ``get_observation`` store API, so for Observations the existing
        state is indexed once via ``list_observations``.
        """
        applied = 0
        rejected_older = 0
        if not changes:
            return applied, rejected_older
        local_store = self._scoped_local()
        adder = getattr(local_store, adder_name, None)
        if not callable(adder):
            return applied, rejected_older
        getter = getattr(local_store, getter_name, None) if getter_name else None
        index: dict | None = None
        if not callable(getter):
            index = {n.id: n for n in self._safe_list("list_observations")}
        for raw in changes:
            node_id = raw.get("id")
            if not node_id:
                continue
            try:
                existing = getter(node_id) if callable(getter) else (index or {}).get(node_id)
            except Exception:  # noqa: BLE001 - broad catch with explicit fallback handling
                existing = None
            # LWW: reject if incoming is older than or equal to local.
            if existing is not None and (raw.get("updated_at") or "") <= (
                getattr(existing, "updated_at", None) or ""
            ):
                rejected_older += 1
                continue
            try:
                adder(cls.from_dict(raw))
                applied += 1
            except NotImplementedError:
                logger.debug("sync pull: local store has no %s storage", cls.__name__)
                break
            except Exception as exc:  # noqa: BLE001 - logged degradation path
                logger.debug("sync pull: skip unparseable %s %s: %s", cls.__name__, node_id, exc)
        return applied, rejected_older

    # ── Auto-pull worker (periodic background sync) ────────────────

    def start_auto_pull(self, interval: int | None = None) -> None:
        """Start a daemon thread that pulls from the remote on a cadence.

        Parameters
        ----------
        interval:
            Seconds between pulls. When ``None``, reads
            ``config.auto_pull_interval``; when that is ``<= 0`` this is a
            no-op (auto-pull stays disabled, pull remains on-demand).

        The thread stops on :meth:`stop_auto_pull` or process exit. Pull
        failures are logged at debug and never crash the thread -- the
        next tick retries.
        """
        if interval is None:
            interval = self._config.auto_pull_interval
        if interval <= 0 or not self._config.active:
            return
        if self._auto_pull_thread is not None and self._auto_pull_thread.is_alive():
            return  # already running
        self._auto_pull_stop.clear()

        def _loop() -> None:
            while not self._auto_pull_stop.wait(interval):
                try:
                    self.pull_incremental()
                except Exception:  # noqa: BLE001 - logged degradation path
                    logger.debug("auto-pull tick failed; will retry")

        self._auto_pull_thread = threading.Thread(
            target=_loop, name="memplex-auto-pull", daemon=True
        )
        self._auto_pull_thread.start()
        logger.debug("auto-pull worker started (interval=%ss)", interval)

    def stop_auto_pull(self) -> None:
        """Signal the auto-pull thread to stop and wait briefly."""
        if self._auto_pull_thread is None:
            return
        self._auto_pull_stop.set()
        self._auto_pull_thread.join(timeout=5.0)
        self._auto_pull_thread = None

    # ── SSE push-notification listener (near-real-time sync) ───────

    def start_sse_listener(self) -> None:
        """Connect to every sync target's /sync/events SSE stream.

        On each received event (write/delete), immediately calls
        pull_incremental so the local store reflects the remote change
        within seconds instead of waiting for the next manual/auto pull.

        One listener thread is started per target (primary + P2P peers);
        previously only the first target was listened to, so writes
        landing on peers never triggered a pull. Each listener reconnects
        with exponential backoff (1s, 2s, 4s, ... up to 60s) on
        disconnect or error. No-op when SSE is disabled or no target.
        """
        if not self._config.active or not self._config.sse_enabled:
            return
        if self._sse_thread is not None and self._sse_thread.is_alive():
            return
        targets = self._config.all_targets()
        if not targets:
            return
        self._sse_stop.clear()
        self._sse_threads = []
        for idx, target in enumerate(targets):
            thread = threading.Thread(
                target=self._sse_loop,
                args=(target,),
                name=f"memplex-sse-listener-{idx}",
                daemon=True,
            )
            thread.start()
            self._sse_threads.append(thread)
        self._sse_thread = self._sse_threads[0]
        logger.debug("SSE listener started (%d target(s))", len(self._sse_threads))

    def _sse_loop(self, target: str) -> None:
        """Listen on one target's /sync/events stream until stopped."""
        backoff = 1
        while not self._sse_stop.is_set():
            try:
                resp = self._requests().get(
                    f"{target}/sync/events",
                    headers={**self._auth_headers(), "Accept": "text/event-stream"},
                    stream=True,
                    timeout=(10, None),  # connect timeout, no read timeout
                )
                if resp.status_code != 200:
                    resp.close()
                    raise RuntimeError(f"SSE HTTP {resp.status_code}")
                backoff = 1  # reset on successful connect
                for line in resp.iter_lines(decode_unicode=True):
                    if self._sse_stop.is_set():
                        break
                    if line and line.startswith("data:"):
                        payload = line[len("data:") :].strip()
                        if payload and payload != '{"type":"hello"}':
                            logger.debug("SSE event received; triggering pull")
                            try:
                                self.pull_incremental()
                            except Exception:  # noqa: BLE001 - logged degradation path
                                logger.debug("SSE-triggered pull failed")
            except Exception:  # noqa: BLE001 - logged degradation path
                if not self._sse_stop.is_set():
                    logger.debug(
                        "SSE listener disconnected; reconnecting in %ss",
                        backoff,
                    )
                # Exponential backoff with cap.
                for _ in range(backoff):
                    if self._sse_stop.wait(1.0):
                        break
                backoff = min(backoff * 2, 60)

    def stop_sse_listener(self) -> None:
        """Signal the SSE listener threads to stop and wait briefly."""
        if not self._sse_threads and self._sse_thread is None:
            return
        self._sse_stop.set()
        for thread in self._sse_threads or ([self._sse_thread] if self._sse_thread else []):
            thread.join(timeout=3.0)
        self._sse_threads = []
        self._sse_thread = None


# ── Factory helper ───────────────────────────────────────────────────


def maybe_wrap_sync(local_store: Any) -> Any:
    """Return a SyncableStore wrapping *local_store* when sync is active.

    Used by ``create_store`` so that setting ``MEMPLEX_REMOTE_URL`` is the
    only configuration needed to enable multi-node sharing. When sync is
    inactive, returns *local_store* unchanged (zero behaviour change).
    """
    config = RemoteSyncConfig()
    if not config.active:
        return local_store
    return SyncableStore(local_store, config=config)
