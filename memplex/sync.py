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
        self.api_key = os.environ.get("MEMPLEX_REMOTE_API_KEY") or os.environ.get("MEMPLEX_API_KEY")
        self.bearer = os.environ.get("MEMPLEX_REMOTE_BEARER_TOKEN") or os.environ.get(
            "MEMPLEX_BEARER_TOKEN"
        )
        self.enabled = self.url is not None and (
            os.environ.get("MEMPLEX_SYNC_ENABLED", "1").lower() not in ("0", "false", "no", "off")
        )

    @property
    def active(self) -> bool:
        """True when sync is configured and enabled."""
        return bool(self.enabled and self.url)


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
        """Best-effort push of Functions to the central server's /sync/push."""
        if not self._config.active or not funcs:
            return
        try:
            from memplex.adapters.http_api import _dataclass_to_dict

            payload = {"functions": [_dataclass_to_dict(f) for f in funcs]}
            resp = self._requests().post(
                f"{self._config.url}/sync/push",
                json=payload,
                headers=self._auth_headers(),
                timeout=10,
            )
            if resp.status_code >= 400:
                logger.debug(
                    "sync push rejected (HTTP %s) for %d functions",
                    resp.status_code,
                    len(funcs),
                )
                self._push_failures += 1
        except Exception as exc:
            # Offline / unreachable remote -- local write already succeeded.
            logger.debug("sync push failed (offline?): %s", exc)
            self._push_failures += 1

    def _push_delete(self, func_id: str) -> None:
        """Best-effort: delete on the server so the tombstone propagates."""
        if not self._config.active:
            return
        try:
            self._requests().delete(
                f"{self._config.url}/memories/{func_id}",
                headers=self._auth_headers(),
                timeout=10,
            )
        except Exception as exc:
            logger.debug("sync delete push failed (offline?): %s", exc)

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
        resp = self._requests().get(
            f"{self._config.url}/sync/changes",
            params={"since": cutoff} if cutoff else {},
            headers=self._auth_headers(),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        changes = data.get("changes", [])
        tombstones = data.get("tombstones", [])
        server_time = data.get("server_time")

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
        for t in tombstones:
            fid = t.get("func_id")
            if fid and self._local.get(fid) is not None:
                self._local.delete(fid)
                deleted += 1

        if server_time:
            self._last_pull_at = server_time

        return {
            "pulled": len(changes),
            "applied": applied,
            "rejected_older": rejected_older,
            "deleted": deleted,
            "server_time": server_time,
        }


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
