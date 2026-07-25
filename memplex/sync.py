"""Multi-node memory sharing: local cache + remote push/pull.

When ``MEMPLEX_REMOTE_URL`` is set, ``create_store`` wraps the local
``LiteMemoryStore`` in a :class:`SyncableStore`. Writes go to the local
store first (so the host keeps working offline) and are then pushed to the
central HTTP server. Other nodes pull those changes on demand via
:meth:`pull_incremental` (exposed as ``memplex sync pull``).

Conflict policy is last-write-wins by ``updated_at`` (Function field).
Deletions propagate via tombstones the server records on ``DELETE``.

Architecture::

    node A (SyncableStore) --push-->  central server (HTTP /sync/push)
    node B (SyncableStore) --pull-->  central server (HTTP /sync/changes)

Reads stay local-first (fast, offline-capable); callers that need the
latest remote state call ``pull_incremental`` explicitly before reading.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)


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
        import os

        self.url = (os.environ.get("MEMPLEX_REMOTE_URL") or "").rstrip("/") or None
        # Read-replica URL (plan D): pull reads from here when set, push
        # still goes to the primary url (write authority). Enables read
        # scaling via Postgres streaming replication or multiple read-only
        # server instances. MEMPLEX_READ_URL=http://replica:8900
        self.read_url = (os.environ.get("MEMPLEX_READ_URL") or "").rstrip("/") or None
        self.api_key = os.environ.get("MEMPLEX_REMOTE_API_KEY") or os.environ.get("MEMPLEX_API_KEY")
        self.bearer = os.environ.get("MEMPLEX_REMOTE_BEARER_TOKEN") or os.environ.get(
            "MEMPLEX_BEARER_TOKEN"
        )
        # P2P peers: comma-separated list of additional node URLs. Each is
        # treated the same as the primary url for pull/push. Enables mesh
        # sync without a single central server. MEMPLEX_PEERS=url1,url2,...
        peers_raw = os.environ.get("MEMPLEX_PEERS", "")
        self.peers: list = [u.strip().rstrip("/") for u in peers_raw.split(",") if u.strip()]
        self.enabled = (self.url is not None or self.peers) and (
            os.environ.get("MEMPLEX_SYNC_ENABLED", "1").lower() not in ("0", "false", "no", "off")
        )
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

    def __init__(self, local: Any, config: Optional[RemoteSyncConfig] = None) -> None:
        self._local = local
        self._config = config or RemoteSyncConfig()
        self._last_pull_at: Optional[str] = None
        self._push_failures = 0
        # Injectable HTTP layer (defaults to the real `requests` module,
        # lazily imported so sync stays optional). Tests replace this with
        # a stub to exercise push/pull without a live server.
        self._http: Any = None
        # Async push: writes return immediately; the actual HTTP POST to
        # each target happens on this daemon pool. Previously push was
        # synchronous inside add(), blocking writes up to 10s per target.
        self._push_executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="memplex-sync-push"
        )
        # Futures for in-flight push tasks, drained by flush_push. With
        # max_workers > 1 a sentinel is NOT sufficient to prove all earlier
        # tasks are done (they may run in parallel), so we track futures
        # explicitly and wait on them.
        self._push_futures: list = []
        # Auto-pull worker state (started by start_auto_pull).
        self._auto_pull_thread: Optional[threading.Thread] = None
        self._auto_pull_stop = threading.Event()
        # SSE push-notification listener state.
        self._sse_thread: Optional[threading.Thread] = None
        self._sse_stop = threading.Event()

    def _requests(self):
        if self._http is None:
            import requests

            self._http = requests
        return self._http

    # ── Transparent read delegation ────────────────────────────────

    def __getattr__(self, name: str) -> Any:
        # Only called when the attribute is not found on SyncableStore
        # itself -> delegate every read/query/get/list to the local store.
        return getattr(self._local, name)

    @property
    def local(self) -> Any:
        """Expose the underlying local store (for sync internals + tests)."""
        return self._local

    @property
    def last_pull_at(self) -> Optional[str]:
        return self._last_pull_at

    # ── Write methods: local first, then best-effort push ──────────

    def add(self, func, source) -> None:
        self._local.add(func, source)
        self._push_functions([func])

    def add_batch(self, funcs, source) -> None:
        self._local.add_batch(funcs, source)
        self._push_functions(list(funcs))

    def merge(self, sub_graph) -> None:
        self._local.merge(sub_graph)
        # sub_graph.nodes carry the merged Functions; push them.
        nodes = getattr(sub_graph, "nodes", None) or []
        self._push_functions(list(nodes))

    def delete(self, func_id) -> None:
        self._local.delete(func_id)
        # Deletion propagation: tell the server to delete + tombstone.
        self._push_delete(func_id)

    def increment_access(self, func_id) -> None:
        # Access-count churn is local-only; pushing it would flood the
        # server with per-query writes (the exact anti-pattern we just
        # fixed). Pull merges server-side access_count via LWW.
        self._local.increment_access(func_id)

    def increment_access_batch(self, func_ids) -> None:
        self._local.increment_access_batch(func_ids)

    # ── Push helpers (best-effort) ─────────────────────────────────

    def _auth_headers(self) -> dict:
        headers = {}
        if self._config.api_key:
            headers["X-API-Key"] = self._config.api_key
        if self._config.bearer:
            headers["Authorization"] = f"Bearer {self._config.bearer}"
        return headers

    def _push_functions(self, funcs) -> None:
        """Schedule an async push of Functions to every target.

        Returns immediately; the actual HTTP POST runs on the daemon
        push pool. Previously this blocked the caller (add/merge) for up
        to 10s per target when a server was slow or unreachable.
        """
        if not self._config.active or not funcs:
            return
        try:
            from memplex.adapters.http_api import _dataclass_to_dict

            payload = {"functions": [_dataclass_to_dict(f) for f in funcs]}
        except Exception as exc:
            logger.debug("sync push serialisation failed: %s", exc)
            return
        for target in self._config.all_targets():
            fut = self._push_executor.submit(self._do_push_functions, target, payload)
            self._push_futures.append(fut)

    def _do_push_functions(self, target: str, payload: dict) -> None:
        """Worker: POST functions to one target (runs on push pool)."""
        try:
            resp = self._requests().post(
                f"{target}/sync/push",
                json=payload,
                headers=self._auth_headers(),
                timeout=10,
            )
            if resp.status_code >= 400:
                logger.debug(
                    "sync push to %s rejected (HTTP %s)",
                    target,
                    resp.status_code,
                )
                self._push_failures += 1
        except Exception as exc:
            logger.debug("sync push to %s failed (offline?): %s", target, exc)
            self._push_failures += 1

    def _push_delete(self, func_id: str) -> None:
        """Schedule an async delete push to every target."""
        if not self._config.active:
            return
        for target in self._config.all_targets():
            fut = self._push_executor.submit(self._do_push_delete, target, func_id)
            self._push_futures.append(fut)

    def _do_push_delete(self, target: str, func_id: str) -> None:
        """Worker: DELETE on one target (runs on push pool)."""
        try:
            self._requests().delete(
                f"{target}/memories/{func_id}",
                headers=self._auth_headers(),
                timeout=10,
            )
        except Exception as exc:
            logger.debug("sync delete push to %s failed (offline?): %s", target, exc)

    def flush_push(self, timeout: float = 5.0) -> None:
        """Wait for queued push tasks to finish (best-effort).

        Useful in tests and on shutdown to avoid asserting before the
        async push has reached the server. Waits on the actual push
        futures (not a sentinel) so it is correct even with
        max_workers > 1.
        """
        import concurrent.futures as cf

        futures = list(self._push_futures)
        if not futures:
            return
        done, _not_done = cf.wait(futures, timeout=timeout)
        # Drain completed futures from the tracked list.
        self._push_futures = [f for f in self._push_futures if not f.done()]

    # ── Pull ───────────────────────────────────────────────────────

    def pull_incremental(self, since: Optional[str] = None) -> dict:
        """Pull changes newer than *since* (ISO-8601) from the remote.

        Applies Functions with LWW (incoming wins only if newer than local)
        and replicates tombstones (deletes locally). Updates
        ``last_pull_at`` to the server's reported time for the next call.

        Returns a summary dict: ``{pulled, applied, rejected_older,
        deleted, server_time}``.

        When sync is inactive (no ``MEMPLEX_REMOTE_URL``), returns a
        no-op summary without touching the network.
        """
        if not self._config.active:
            return {
                "pulled": 0,
                "applied": 0,
                "rejected_older": 0,
                "deleted": 0,
                "server_time": None,
                "skipped": "sync not active (no MEMPLEX_REMOTE_URL)",
            }
        from memplex.models import SourceDocument, SourceType

        cutoff = since or self._last_pull_at
        # Pull targets: prefer read-replica (MEMPLEX_READ_URL) when set
        # (plan D: read scaling); otherwise fall back to all push targets
        # (primary + P2P peers). The write authority stays at the primary.
        if self._config.read_url:
            pull_targets = [self._config.read_url]
        else:
            pull_targets = self._config.all_targets()
        changes = []
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
                tombstones.extend(data.get("tombstones", []))
                if data.get("server_time"):
                    server_time = data["server_time"]
            except Exception as exc:
                logger.debug("sync pull from %s failed: %s", target, exc)

        applied = 0
        rejected_older = 0
        for raw in changes:
            func_id = raw.get("id")
            if not func_id:
                continue
            existing = self._local.get(func_id)
            if existing is not None and (raw.get("updated_at") or "") <= (
                existing.updated_at or ""
            ):
                rejected_older += 1
                continue
            try:
                from memplex.adapters.http_api import _function_from_dict

                incoming = _function_from_dict(raw)
                self._local.add(
                    incoming,
                    SourceDocument(type="sync_pull", source_type=SourceType.WIKI),
                )
                applied += 1
            except Exception as exc:
                logger.debug("sync pull: skip unparseable change %s: %s", func_id, exc)

        deleted = 0
        tombstones_skipped_edit = 0
        for t in tombstones:
            fid = t.get("func_id")
            if not fid:
                continue
            local = self._local.get(fid)
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
            self._local.delete(fid)
            deleted += 1

        if server_time:
            self._last_pull_at = server_time

        return {
            "pulled": len(changes),
            "applied": applied,
            "rejected_older": rejected_older,
            "deleted": deleted,
            "tombstones_skipped_edit": tombstones_skipped_edit,
            "server_time": server_time,
        }

    # ── Auto-pull worker (periodic background sync) ────────────────

    def start_auto_pull(self, interval: Optional[int] = None) -> None:
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

        def _loop():
            while not self._auto_pull_stop.wait(interval):
                try:
                    self.pull_incremental()
                except Exception as exc:
                    logger.debug("auto-pull tick failed (will retry): %s", exc)

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
        """Connect to the primary target's /sync/events SSE stream.

        On each received event (write/delete), immediately calls
        pull_incremental so the local store reflects the remote change
        within seconds instead of waiting for the next manual/auto pull.

        Reconnects with exponential backoff (1s, 2s, 4s, ... up to 60s)
        on disconnect or error. No-op when SSE is disabled or no target.
        """
        if not self._config.active or not self._config.sse_enabled:
            return
        if self._sse_thread is not None and self._sse_thread.is_alive():
            return
        self._sse_stop.clear()

        def _sse_loop():
            import time

            backoff = 1
            while not self._sse_stop.is_set():
                target = self._config.url or (self._config.peers[0] if self._config.peers else None)
                if not target:
                    break
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
                                logger.debug("SSE event received, triggering pull: %s", payload)
                                try:
                                    self.pull_incremental()
                                except Exception as exc:
                                    logger.debug("SSE-triggered pull failed: %s", exc)
                except Exception as exc:
                    if not self._sse_stop.is_set():
                        logger.debug(
                            "SSE listener disconnected, reconnecting in %ss: %s", backoff, exc
                        )
                    # Exponential backoff with cap.
                    for _ in range(backoff):
                        if self._sse_stop.wait(1.0):
                            break
                    backoff = min(backoff * 2, 60)

        self._sse_thread = threading.Thread(
            target=_sse_loop, name="memplex-sse-listener", daemon=True
        )
        self._sse_thread.start()
        logger.debug("SSE listener started")

    def stop_sse_listener(self) -> None:
        """Signal the SSE listener to stop and wait briefly."""
        if self._sse_thread is None:
            return
        self._sse_stop.set()
        self._sse_thread.join(timeout=3.0)
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
