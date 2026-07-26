"""Memplex HTTP/REST API -- FastAPI application factory.

Usage::

    from memplex.adapters.http_api import create_app

    app = create_app()               # uses default config
    # or
    from memplex.config import load_config
    app = create_app(load_config(path="custom.yaml"))

Run with uvicorn::

    uvicorn memplex.adapters.http_api:app --host 127.0.0.1 --port 8900

Requires optional dependencies: ``fastapi``, ``uvicorn``.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from memplex.service import MemplexService

logger = logging.getLogger(__name__)

# FastAPI is an optional dependency -- import lazily so the rest of
# the adapter package remains importable without it.
try:
    from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
    from fastapi.responses import JSONResponse, StreamingResponse

    _FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover
    _FASTAPI_AVAILABLE = False


# ── Pydantic-like request models (plain dicts for now) ──────────────
# We avoid a hard pydantic dependency by using simple dicts validated
# inside the route handlers.  When pydantic is present (FastAPI pulls
# it in), FastAPI still gets the benefit of automatic doc generation.


# ── Helpers ─────────────────────────────────────────────────────────

from memplex.adapters._shared import dataclass_to_dict as _dataclass_to_dict


def _get_service(request) -> "MemplexService":
    """Retrieve the shared MemplexService from app state."""
    return request.app.state.memplex_service


# ── Sync tombstone helpers ───────────────────────────────────────────
# Server-side record of deleted func_ids so other nodes can replicate
# deletions. Kept as a small JSON sidecar (not in the main store) so the
# sync layer does not need to change the MemoryStore contract.


def _tombstone_path() -> Path:
    return (
        Path(os.environ.get("MEMPLEX_STORAGE_PATH", "~/.memplex")).expanduser() / "tombstones.json"
    )


def _record_tombstone(func_id: str, deleted_version: str = "") -> None:
    """Record a deletion tombstone with the deleted record's updated_at.

    The *deleted_version* lets pulling clients detect delete-vs-edit
    conflicts: if the client's local copy has a newer updated_at than the
    tombstone, the edit happened after the delete and must be kept.
    """
    try:
        path = _tombstone_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        tombstones: dict = {}
        if path.exists():
            tombstones = json.loads(path.read_text(encoding="utf-8"))
        tombstones[func_id] = {
            "deleted_at": datetime.now(timezone.utc).isoformat(),
            "deleted_version": deleted_version,
        }
        path.write_text(json.dumps(tombstones, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logger.debug("failed to record tombstone for %s", func_id)


def _read_tombstones(since: Optional[str] = None) -> list:
    """Return tombstones optionally filtered by *since* (iso8601).

    Handles both the new format (``{deleted_at, deleted_version}``) and
    the legacy format (bare iso8601 string) for backward compatibility.
    """
    try:
        path = _tombstone_path()
        if not path.exists():
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    items = []
    for fid, val in raw.items():
        if isinstance(val, dict):
            # New format.
            deleted_at = val.get("deleted_at", "")
            deleted_version = val.get("deleted_version", "")
        else:
            # Legacy format: bare iso8601 string (no version).
            deleted_at = str(val)
            deleted_version = ""
        items.append({"func_id": fid, "deleted_at": deleted_at, "deleted_version": deleted_version})
    if since:
        items = [i for i in items if i["deleted_at"] > since]
    return items


# ── SSE broadcast helpers (server -> client push notifications) ───────
# In-process pub/sub for /sync/events. Each connected client gets its own
# asyncio.Queue; write/delete routes publish events to all subscribers so
# clients know to pull immediately instead of polling.
#
# Redis pub/sub (plan C): when MEMPLEX_REDIS_URL is set, _broadcast_event
# publishes to a Redis channel so events propagate across uvicorn workers.
# A background thread subscribes to the channel and fans out to local
# SSE subscribers. Without Redis, falls back to the in-process set.

_SSE_SUBSCRIBERS: set = set()
_SSE_MAX_SUBSCRIBERS = 500  # per-worker SSE connection cap for congestion control
_SSE_REDIS_CHANNEL = "memplex:events"
_redis_client: Any = None
_redis_pubsub_thread: Any = None


def _get_redis():
    """Lazily connect to Redis for cross-worker SSE broadcast.

    Resolution order (first wins):
    1. MEMPLEX_REDIS_URL env var (explicit user config).
    2. Auto-probe localhost:6379 (if a Redis is already running locally,
       use it without requiring the user to set any env var).
    3. Give up -> return None (in-process broadcast, single-worker only).

    MEMPLEX_REDIS_URL=disable forces auto-probe off.
    """
    global _redis_client
    if _redis_client is not None:
        return _redis_client if _redis_client is not False else None
    # 1. Explicit env var.
    redis_url = os.environ.get("MEMPLEX_REDIS_URL")
    # 2. Auto-probe localhost (unless explicitly disabled).
    if not redis_url or redis_url == "disable":
        if redis_url == "disable":
            _redis_client = False
            return None
        redis_url = _auto_probe_redis()
    if not redis_url:
        _redis_client = False
        return None
    try:
        import redis  # type: ignore

        _redis_client = redis.from_url(redis_url)
        _redis_client.ping()  # verify connectivity
        logger.info("SSE Redis pub/sub connected: %s", redis_url)
        return _redis_client
    except Exception as exc:
        logger.debug("SSE Redis unavailable, using in-process broadcast: %s", exc)
        _redis_client = False
        return None


def _auto_probe_redis(host: str = "localhost", port: int = 6379) -> Optional[str]:
    """Probe localhost:6379; return the URL if a Redis is reachable.

    This lets a single-machine deployment with a Redis already running
    automatically use it for SSE broadcast without any env configuration.
    """
    try:
        import socket

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(0.5)
        result = sock.connect_ex((host, port))
        sock.close()
        if result == 0:
            logger.debug("Auto-probe: Redis detected at %s:%s", host, port)
            return f"redis://{host}:{port}/0"
    except Exception:
        pass
    return None


def _start_redis_subscriber() -> None:
    """Start a daemon thread that subscribes to the Redis channel and
    fans out received events to local SSE subscribers."""
    global _redis_pubsub_thread
    if _redis_pubsub_thread is not None:
        return
    r = _get_redis()
    if r is None:
        return

    import threading

    def _sub_loop():
        try:
            pubsub = r.pubsub()
            pubsub.subscribe(_SSE_REDIS_CHANNEL)
            for message in pubsub.listen():
                if message["type"] == "message":
                    try:
                        event = json.loads(message["data"])
                        _fanout_local(event)
                    except Exception:
                        pass
        except Exception as exc:
            logger.debug("Redis subscriber thread stopped: %s", exc)

    _redis_pubsub_thread = threading.Thread(
        target=_sub_loop, name="memplex-sse-redis-sub", daemon=True
    )
    _redis_pubsub_thread.start()


def _fanout_local(event: dict) -> None:
    """Push an event to all in-process SSE subscriber queues."""
    dead = set()
    for queue in _SSE_SUBSCRIBERS:
        try:
            queue.put_nowait(event)
        except Exception:
            dead.add(queue)
    if dead:
        _SSE_SUBSCRIBERS.difference_update(dead)


def _broadcast_event(event: dict) -> None:
    """Fan out an event: Redis pub/sub (cross-worker) or in-process set."""
    r = _get_redis()
    if r is not None:
        try:
            r.publish(_SSE_REDIS_CHANNEL, json.dumps(event))
            return
        except Exception as exc:
            logger.debug("Redis publish failed, falling back to local: %s", exc)
    _fanout_local(event)


# ── Security helpers ────────────────────────────────────────────────


def _check_bind_security(app: FastAPI) -> None:
    """Refuse to start when binding to a non-local address without auth.

    Local binds (``127.0.0.1`` / ``localhost`` / ``::1``) keep the
    historical open default.  A non-local bind without any configured
    credential would expose the API unauthenticated, so we raise rather
    than warn.

    Operator contract: the guard keys off the ``MEMPLEX_HOST`` env var,
    which must match the actual host passed to ``uvicorn ... --host``.
    uvicorn's bind argument is not visible to Python at construction
    time, so ``MEMPLEX_HOST`` is the authoritative signal -- if you run
    ``uvicorn ... --host 0.0.0.0`` without also exporting
    ``MEMPLEX_HOST=0.0.0.0`` (plus a credential), this guard will not
    fire. Always set ``MEMPLEX_HOST`` to mirror your bind address.
    """
    host = os.environ.get("MEMPLEX_HOST", "127.0.0.1")
    non_local = host not in ("127.0.0.1", "localhost", "::1")

    api_key = os.environ.get("MEMPLEX_API_KEY")
    bearer_token = os.environ.get("MEMPLEX_BEARER_TOKEN")

    if non_local and not api_key and not bearer_token:
        raise RuntimeError(
            f"Refusing to bind to non-local host {host!r} without "
            "authentication. Set MEMPLEX_API_KEY or MEMPLEX_BEARER_TOKEN "
            "to enable auth, or bind to 127.0.0.1/localhost/::1."
        )


if _FASTAPI_AVAILABLE:

    def _require_auth(
        x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
        authorization: Optional[str] = Header(None),
    ) -> None:
        """Authorize a request against configured credentials.

        Backward compatible: when neither ``MEMPLEX_API_KEY`` nor
        ``MEMPLEX_BEARER_TOKEN`` is set, every request is allowed (the
        local-development default).  As soon as one or both are set, the
        request must present a matching ``X-API-Key`` header (for the API
        key) and/or ``Authorization: Bearer <token>`` header (for the
        bearer token); otherwise HTTP 401.

        Credentials are compared with :func:`hmac.compare_digest` to
        avoid timing oracles.
        """
        api_key = os.environ.get("MEMPLEX_API_KEY")
        bearer_token = os.environ.get("MEMPLEX_BEARER_TOKEN")

        # No credentials configured → open access (local scenario).
        if not api_key and not bearer_token:
            return

        # Validate the X-API-Key header against a configured API key.
        if api_key is not None and x_api_key is not None:
            if hmac.compare_digest(x_api_key.encode("utf-8"), api_key.encode("utf-8")):
                return

        # Validate the Authorization: Bearer header against a configured token.
        if bearer_token is not None and authorization is not None:
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() == "bearer" and token.strip():
                if hmac.compare_digest(token.strip().encode("utf-8"), bearer_token.encode("utf-8")):
                    return

        raise HTTPException(
            status_code=401,
            detail="Valid X-API-Key or Authorization: Bearer token required",
            headers={"WWW-Authenticate": 'Bearer realm="memplex"'},
        )


# ── Rate limiting (simple in-memory token bucket per client IP) ──────

_RATE_LIMIT_WINDOW = 60  # seconds
_RATE_LIMIT_MAX = 120  # requests per window per IP
_rate_buckets: dict = {}


def _check_rate_limit(client_ip: str) -> bool:
    """Return True if the request is allowed, False if rate-limited."""
    import time

    now = time.time()
    bucket = _rate_buckets.get(client_ip)
    if bucket is None:
        _rate_buckets[client_ip] = {"count": 1, "reset_at": now + _RATE_LIMIT_WINDOW}
        return True
    if now > bucket["reset_at"]:
        bucket["count"] = 1
        bucket["reset_at"] = now + _RATE_LIMIT_WINDOW
        return True
    if bucket["count"] >= _RATE_LIMIT_MAX:
        return False
    bucket["count"] += 1
    return True


if _FASTAPI_AVAILABLE:

    def _rate_limit_dependency(request: Request) -> None:
        """Permissive rate limiter: only enforces when auth is configured
        (i.e. a public-facing deployment). Local dev (no auth) is exempt."""
        api_key = os.environ.get("MEMPLEX_API_KEY")
        bearer = os.environ.get("MEMPLEX_BEARER_TOKEN")
        if not api_key and not bearer:
            return  # local dev: no limit
        client_ip = request.client.host if request.client else "unknown"
        if not _check_rate_limit(client_ip):
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded ({_RATE_LIMIT_MAX} req/{_RATE_LIMIT_WINDOW}s). "
                "Retry shortly.",
            )


# ── App factory ─────────────────────────────────────────────────────


def create_app(config=None) -> "FastAPI":
    """Build and return a FastAPI application.

    Parameters
    ----------
    config:
        A :class:`MemplexConfig` instance.  When ``None``, defaults are
        loaded via :func:`load_config`.

    Returns
    -------
    FastAPI
        Configured application with lifecycle hooks.
    """
    if not _FASTAPI_AVAILABLE:
        raise ImportError(
            "FastAPI is required for the HTTP adapter. Install it with: pip install fastapi uvicorn"
        )

    from memplex.config import load_config
    from memplex.logging_config import configure_logging
    from memplex.service import MemplexService

    # Configure logging once at app construction (the HTTP API is a
    # long-running daemon surface; honour MEMPLEX_LOG_JSON for structured
    # logs the same way the MCP server and CLI do).
    configure_logging()

    if config is None:
        config = load_config()

    # ── Lifecycle (lifespan replaces deprecated on_event) ──────
    # Startup creates the MemplexService and starts its background
    # worker; shutdown stops it. Keeping this as a closure over
    # ``config`` mirrors the previous on_event behaviour.

    @asynccontextmanager
    async def _lifespan(app: "FastAPI"):
        svc = MemplexService(config=config)
        svc.start()
        app.state.memplex_service = svc
        logger.info("Memplex HTTP API started (backend=%s)", config.storage.backend)
        try:
            yield
        finally:
            svc.stop()
            logger.info("Memplex HTTP API stopped")

    app = FastAPI(
        title="Memplex API",
        version="0.1.0",
        description="Multi-agent memory system REST API",
        dependencies=[Depends(_require_auth), Depends(_rate_limit_dependency)],
        lifespan=_lifespan,
    )

    # Refuse insecure binds (non-local host without auth) at construction
    # time. Reads MEMPLEX_HOST / credentials from env; the ``app`` argument
    # is kept for signature stability.
    _check_bind_security(app)

    # ── CORS (opt-in via env) ────────────────────────────────────
    cors_origins = os.environ.get("MEMPLEX_CORS_ORIGINS", "")
    if cors_origins:
        try:
            from fastapi.middleware.cors import CORSMiddleware

            origins = [o.strip() for o in cors_origins.split(",") if o.strip()]
            app.add_middleware(
                CORSMiddleware,
                allow_origins=origins,
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )
        except Exception:
            logger.warning("Failed to configure CORS middleware")

    # ══════════════════════════════════════════════════════════════
    #  Routes
    # ══════════════════════════════════════════════════════════════

    @app.post("/memories", summary="Write a new memory")
    async def write_memory(request: Request, body: dict) -> JSONResponse:
        """Write new content into memory.

        Request body::

            {
                "type": "text" | "file" | "url",
                "content": "...",
                "source_type": "wiki"  // optional
            }
        """
        svc = _get_service(request)
        content = body.get("content", "")
        source_type = body.get("type", "text")

        from memplex.models import SourceType as ST

        st_map = {
            "requirement": ST.REQUIREMENT,
            "meeting": ST.MEETING,
            "code": ST.CODE,
            "wiki": ST.WIKI,
        }
        source_type_enum = st_map.get(body.get("source_type", "wiki"), ST.WIKI)

        from memplex.models import SourceDocument

        source = SourceDocument(
            type=source_type,
            content=content,
            source_type=source_type_enum,
        )
        result = svc.write(source)
        # Notify SSE subscribers that new memories are available.
        func_ids = [f.id for f in getattr(result, "functions", [])]
        _broadcast_event({"type": "write", "func_ids": func_ids})
        return JSONResponse(_dataclass_to_dict(result))

    @app.get("/memories", summary="Query memories")
    async def query_memories(
        request: Request,
        q: str = Query(..., description="Query text"),
        top_k: int = Query(10, ge=1, le=100),
        owner: Optional[str] = Query(None),
        max_tokens: int = Query(4000, ge=0),
    ) -> JSONResponse:
        """Search memories with natural language."""
        svc = _get_service(request)
        result = await svc.query_async(
            text=q,
            top_k=top_k,
            owner=owner,
            max_tokens=max_tokens,
        )
        return JSONResponse(_dataclass_to_dict(result))

    @app.get("/memories/{memory_id}", summary="Get memory detail")
    async def get_memory(request: Request, memory_id: str) -> JSONResponse:
        """Retrieve a single memory by ID."""
        svc = _get_service(request)
        func = svc.get(memory_id)
        if func is None:
            raise HTTPException(status_code=404, detail="Memory not found")
        return JSONResponse(_dataclass_to_dict(func))

    @app.get("/memories/{memory_id}/timeline", summary="Get memory timeline")
    async def get_timeline(request: Request, memory_id: str) -> JSONResponse:
        """Get the changelog timeline for a memory."""
        svc = _get_service(request)
        events = svc.store.get_timeline(memory_id)
        return JSONResponse(_dataclass_to_dict(events))

    @app.delete("/memories/{memory_id}", summary="Delete memory")
    async def delete_memory(request: Request, memory_id: str) -> JSONResponse:
        """Soft-delete a memory and record a sync tombstone."""
        svc = _get_service(request)
        # Snapshot updated_at BEFORE deleting so the tombstone carries the
        # version it deleted. Pull clients use this to decide: if their
        # local copy is NEWER than the tombstone, the edit happened after
        # the delete and must be kept (fixes the delete-vs-edit bug).
        existing = svc.get(memory_id)
        deleted_version = (getattr(existing, "updated_at", None) or "") if existing else ""
        svc.delete(memory_id)
        _record_tombstone(memory_id, deleted_version)
        _broadcast_event({"type": "delete", "func_id": memory_id})
        return JSONResponse({"status": "deleted", "id": memory_id})

    @app.post("/memories/{memory_id}/feedback", summary="Submit feedback")
    async def submit_feedback(request: Request, memory_id: str, body: dict) -> JSONResponse:
        """Submit feedback for a memory field value.

        Request body::

            {
                "role": "trigger" | "action" | "condition" | "benefit",
                "index": 0,
                "verdict": "correct" | "wrong",
                "reason": "optional explanation"
            }
        """
        svc = _get_service(request)
        svc.submit_feedback(
            memory_id=memory_id,
            field_role=body["role"],
            value_index=body["index"],
            verdict=body["verdict"],
            reason=body.get("reason"),
        )
        return JSONResponse({"status": "recorded"})

    @app.get("/memories/pending_reviews", summary="List pending reviews")
    async def pending_reviews(
        request: Request,
        owner: Optional[str] = Query(None),
        limit: int = Query(100, ge=1, le=1000),
    ) -> JSONResponse:
        """Retrieve pending feedback reviews."""
        svc = _get_service(request)
        reviews = svc.get_pending_reviews(owner=owner, limit=limit)
        return JSONResponse(
            {
                "total": len(reviews),
                "reviews": _dataclass_to_dict(reviews),
            }
        )

    @app.post("/memories/{memory_id}/resolve", summary="Resolve a review")
    async def resolve_review(request: Request, memory_id: str, body: dict) -> JSONResponse:
        """Apply a resolution to a pending review.

        Request body::

            {
                "field_role": "trigger",
                "action": "accept" | "reject" | "merge",
                "new_value": "optional replacement when action=merge"
            }
        """
        svc = _get_service(request)
        result = svc.apply_resolution(
            memory_id=memory_id,
            field_role=body["field_role"],
            action=body["action"],
            new_value=body.get("new_value"),
        )
        return JSONResponse(result)

    @app.get("/health", summary="Health check")
    async def health(
        request: Request,
    ) -> JSONResponse:
        """Return service health status."""
        svc = _get_service(request)
        return JSONResponse(svc.health())

    @app.get("/stats", summary="Statistics")
    async def stats(
        request: Request,
    ) -> JSONResponse:
        """Return storage and usage statistics."""
        svc = _get_service(request)
        return JSONResponse(svc.stats())

    @app.post("/compact", summary="Trigger compaction")
    async def compact(request: Request, body: Optional[dict] = None) -> JSONResponse:
        """Run the compaction pipeline.

        Request body (optional)::

            {"scope": "project" | "session" | "global"}
        """
        svc = _get_service(request)
        scope = "project"
        if body:
            scope = body.get("scope", "project")
        result = svc.compact(scope=scope)
        return JSONResponse(_dataclass_to_dict(result))

    # ════════════════════════════════════════════════════════════════
    #  Sync endpoints (multi-node sharing)
    # ════════════════════════════════════════════════════════════════

    @app.get("/sync/changes", summary="Pull incremental changes since a timestamp")
    async def sync_changes(
        request: Request,
        since: Optional[str] = Query(None, description="ISO-8601 cutoff; omit for all"),
    ) -> JSONResponse:
        """Return Functions with updated_at > since, plus deletion tombstones.

        Clients call this to pull incremental updates from the central
        node. Tombstones let clients replicate deletions. Uses LWW on the
        client side; this endpoint just ships current state.
        """
        svc = _get_service(request)
        # Incremental query: use list_changes_since so the backend pushes
        # the updated_at filter into the database (Postgres WHERE) or dict
        # filter (lite), instead of loading 100k functions every pull.
        funcs = svc.store.list_changes_since(since=since, limit=100000)
        changed = [_dataclass_to_dict(f) for f in funcs]
        tombstones = _read_tombstones(since=since)
        # The server's "now" gives clients a high-water mark for the next
        # pull, so they do not re-process the same window.
        server_now = datetime.now(timezone.utc).isoformat()
        return JSONResponse(
            {
                "changes": changed,
                "tombstones": tombstones,
                "server_time": server_now,
            }
        )

    @app.post("/sync/push", summary="Push local changes to the central node")
    async def sync_push(request: Request, body: dict) -> JSONResponse:
        """Receive a batch of Functions and merge them with LWW by updated_at.

        Request body::

            {"functions": [<serialized Function>, ...]}

        Each function is accepted only if it is newer than the server's
        current copy (or the server has no copy). Older pushes are counted
        as rejected (not errors) so the client can see LWW in action.
        """
        svc = _get_service(request)
        from memplex.models import FieldValue, Function, SourceDocument, SourceType

        pushed = body.get("functions", [])
        accepted = 0
        rejected_older = 0
        for raw in pushed:
            try:
                incoming = _function_from_dict(raw)
            except Exception as exc:
                logger.debug("sync_push: skip unparseable function: %s", exc)
                continue
            existing = svc.store.get(incoming.id)
            if existing is not None:
                # LWW: reject if incoming is older or equal.
                if (incoming.updated_at or "") <= (existing.updated_at or ""):
                    rejected_older += 1
                    continue
            svc.store.add(
                incoming,
                SourceDocument(type="sync_push", source_type=SourceType.WIKI),
            )
            accepted += 1
        return JSONResponse({"accepted": accepted, "rejected_older": rejected_older})

    @app.get("/sync/events", summary="SSE stream of sync events")
    async def sync_events(request: Request):
        """Server-Sent Events stream: notifies clients of writes/deletes.

        Clients connect here and receive ``data: {"type":"write",...}\\n\\n``
        events. On receiving an event, a client should immediately
        ``pull_incremental()`` to fetch the new state. A ``: ping`` comment
        is sent every 30s to keep the connection alive.

        When the subscriber count exceeds ``_SSE_MAX_SUBSCRIBERS``, new
        connections get HTTP 503 with a hint to fall back to polling
        (``MEMPLEX_SYNC_PULL_INTERVAL``). This prevents connection-exhaustion
        under load and keeps existing connections healthy.
        """
        # Congestion guard: refuse new SSE connections past the limit.
        if len(_SSE_SUBSCRIBERS) >= _SSE_MAX_SUBSCRIBERS:
            raise HTTPException(
                status_code=503,
                detail=(
                    "SSE connection limit reached. Use MEMPLEX_SYNC_PULL_INTERVAL "
                    "for polling-based sync, or deploy a Redis-backed multi-worker "
                    "setup (MEMPLEX_REDIS_URL) to scale SSE."
                ),
            )
        import asyncio as _aio

        # Ensure the Redis subscriber thread is running (cross-worker
        # broadcast). No-op when Redis is not configured.
        _start_redis_subscriber()
        queue: _aio.Queue = _aio.Queue(maxsize=64)
        _SSE_SUBSCRIBERS.add(queue)

        async def _event_stream():
            try:
                # Send an initial hello so the client knows it's connected.
                yield 'data: {"type":"hello"}\n\n'
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=30.0)
                        yield f"data: {json.dumps(event)}\n\n"
                    except asyncio.TimeoutError:
                        yield ": ping\n\n"  # keepalive
            finally:
                _SSE_SUBSCRIBERS.discard(queue)

        return StreamingResponse(
            _event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/metrics", summary="Prometheus-format metrics")
    async def metrics(request: Request) -> JSONResponse:
        """Return basic metrics in Prometheus text exposition format.

        Exposes function count, edge count, queue depth, SSE subscribers,
        and sync push failures. Suitable for scraping by Prometheus or
        compatible monitoring systems.
        """
        svc = _get_service(request)
        h = svc.health()
        lines = [
            "# HELP memplex_functions_total Total stored functions",
            "# TYPE memplex_functions_total gauge",
            f"memplex_functions_total {h.get('functions_total', 0)}",
            "# HELP memplex_edges_total Total graph edges",
            "# TYPE memplex_edges_total gauge",
            f"memplex_edges_total {h.get('edges_total', 0)}",
            "# HELP memplex_queue_depth Background task queue depth",
            "# TYPE memplex_queue_depth gauge",
            f"memplex_queue_depth {h.get('queue_depth', 0)}",
            "# HELP memplex_sse_subscribers Active SSE connections",
            "# TYPE memplex_sse_subscribers gauge",
            f"memplex_sse_subscribers {len(_SSE_SUBSCRIBERS)}",
            "# HELP memplex_dead_letters Pending failed tasks",
            "# TYPE memplex_dead_letters gauge",
            f"memplex_dead_letters {h.get('dead_letters_pending', 0)}",
        ]
        sync = h.get("sync", {})
        if sync.get("enabled"):
            lines.extend(
                [
                    "# HELP memplex_push_failures Sync push failure count",
                    "# TYPE memplex_push_failures gauge",
                    f"memplex_push_failures {sync.get('push_failures', 0)}",
                ]
            )
        from fastapi.responses import PlainTextResponse

        return PlainTextResponse("\n".join(lines) + "\n")

    return app


def _function_from_dict(data: dict) -> Any:
    """Reconstruct a Function from its serialized dict (sync_push payload).

    Only the fields needed for storage + LWW are restored; rich FieldValue
    sub-objects are rebuilt minimally. This is intentionally permissive --
    malformed payloads are caught by the caller and skipped.
    """
    from memplex.models import FieldValue, Function, SourceType

    def _fvs(role):
        return [
            FieldValue(
                desc=fv.get("desc", ""),
                sources=fv.get("sources", []),
                source_method=fv.get("source_method", "manual"),
                weight=fv.get("weight", 1.0),
            )
            for fv in data.get(role, [])
        ]

    source_type_raw = data.get("source_type", "wiki")
    try:
        source_type = (
            SourceType(source_type_raw) if isinstance(source_type_raw, str) else source_type_raw
        )
    except ValueError:
        source_type = SourceType.WIKI

    return Function(
        id=data["id"],
        name=data.get("name", ""),
        name_normalized=data.get("name_normalized", data.get("name", "").lower()),
        domain=data.get("domain", ""),
        memory_type=data.get("memory_type", "function"),
        confidence=data.get("confidence", 0.5),
        source_type=source_type,
        owner=data.get("owner"),
        version=data.get("version", 1),
        created_at=data.get("created_at"),
        updated_at=data.get("updated_at"),
        origin_session=data.get("origin_session"),
        access_count=data.get("access_count", 0),
        last_accessed_at=data.get("last_accessed_at"),
        source_paragraphs=data.get("source_paragraphs", []),
        needs_review=data.get("needs_review", False),
        content_hash=data.get("content_hash"),
        trigger=_fvs("trigger"),
        condition=_fvs("condition"),
        action=_fvs("action"),
        benefit=_fvs("benefit"),
        attributes=data.get("attributes", {}),
        cross_references=data.get("cross_references", []),
    )
