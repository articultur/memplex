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
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import threading
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from memplex.service import MemplexService

logger = logging.getLogger(__name__)

# FastAPI is an optional dependency -- import lazily so the rest of
# the adapter package remains importable without it.
try:
    from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request
    from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

    _FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover
    _FASTAPI_AVAILABLE = False


# ── Pydantic-like request models (plain dicts for now) ──────────────
# We avoid a hard pydantic dependency by using simple dicts validated
# inside the route handlers.  When pydantic is present (FastAPI pulls
# it in), FastAPI still gets the benefit of automatic doc generation.


# ── Helpers ─────────────────────────────────────────────────────────

from memplex.adapters._shared import dataclass_to_dict as _dataclass_to_dict
from memplex.auth import (
    AuthorizationContext,
    IdentityClaimError,
    MemoryNotFoundError,
    PrincipalRegistry,
    PrincipalRegistryError,
    bind_node_identity,
    local_development_context,
)
from memplex.operations import (
    OperationsEvidenceError,
    OperationsMetrics,
    OperationsReadinessBinding,
    RequestAdmission,
    create_operations_evidence,
    load_operations_signing_key,
    utc_timestamp_now,
    write_operations_report_atomic,
)
from memplex.readiness_evidence import (
    ReadinessEvidenceError,
    load_deployment_evidence_binding_from_environment,
)

_OPERATIONS_CONTROL_PATHS = frozenset({"/health/live", "/health/ready", "/metrics"})
_UNAUTHENTICATED_UDS_ENV = "MEMPLEX_ALLOW_UNAUTHENTICATED_UDS"
_FORWARDED_CLIENT_HEADERS = frozenset({"forwarded", "x-forwarded-for"})


def _current_operations_readiness_binding(config: object) -> OperationsReadinessBinding:
    """Adapt the shared explicit deployment binding to G006's report schema."""
    operations = getattr(config, "operations")
    deployment = load_deployment_evidence_binding_from_environment(
        memplex_version=version("memplex")
    )
    return OperationsReadinessBinding(
        deployment_id=deployment.deployment_id,
        source_sha256=deployment.source_sha256,
        artifact_sha256=deployment.artifact_sha256,
        target_identity_sha256=deployment.target_identity_sha256,
        expected_key_id=getattr(operations, "report_key_id"),
    )


def _get_service(request) -> "MemplexService":
    """Retrieve the shared MemplexService from app state."""
    return request.app.state.memplex_service


def _exception_sqlstate(exc: BaseException) -> str | None:
    """Read psycopg2/psycopg3 SQLSTATE without formatting driver details."""
    for candidate in (exc, getattr(exc, "__cause__", None)):
        if candidate is None:
            continue
        for attribute in ("sqlstate", "pgcode"):
            value = getattr(candidate, attribute, None)
            if type(value) is str and value:
                return value
    return None


def _authorization(request) -> AuthorizationContext:
    """Return the adapter-established request identity.

    The global auth dependency sets this before every route handler.  Route
    code must never derive it from request JSON, query parameters, or a
    caller-supplied owner field.
    """
    context = getattr(request.state, "authorization", None)
    if not isinstance(context, AuthorizationContext):
        raise HTTPException(status_code=401, detail="Authentication required")
    return context


def _typed_changes_since(
    store,
    list_method: str,
    since: Optional[str],
    *,
    is_visible=None,
) -> list:
    """Serialize Fact/Preference/Observation nodes with updated_at > *since*.

    Duck-typed: backends without the list API (or where it raises) simply
    contribute no changes, so the sync endpoints stay functional on stores
    that only persist Functions. Serialization uses the models-standard
    ``to_dict`` (canonical shape shared with the sync client); Fact's
    ``object_`` field is emitted under the external key ``"object"``.
    """
    lister = getattr(store, list_method, None)
    if not callable(lister):
        return []
    try:
        nodes = list(lister(limit=100000))
    except Exception as exc:
        logger.debug("sync changes: %s unavailable: %s", list_method, exc)
        return []
    if since:
        nodes = [n for n in nodes if (n.updated_at or "") > since]
    if is_visible is not None:
        nodes = [node for node in nodes if is_visible(node)]
    return [n.to_dict() for n in nodes]


def _typed_node_from_scoped_store(store, node_id: str):
    """Resolve a Function, Fact, or Preference through one scoped facade.

    HTTP sync must not use ``service._typed_lookup`` because it wraps the
    service's shared store.  In production that skips the request-bound
    PostgreSQL facade entirely.  This small duplicate of the lookup order
    keeps every read on the exact facade selected for the current request.
    """
    node = store.get(node_id)
    if node is not None:
        return node
    for getter_name in ("get_fact", "get_preference"):
        getter = getattr(store, getter_name, None)
        if not callable(getter):
            continue
        node = getter(node_id)
        if node is not None:
            return node
    return None


def _merge_typed_push(
    store,
    raw_nodes: list,
    *,
    cls,
    adder_name: str,
    getter_name: Optional[str] = None,
    lister_name: Optional[str] = None,
) -> tuple:
    """Merge pushed typed nodes into *store* with LWW by updated_at.

    Mirrors the Function LWW in ``/sync/push`` for Fact/Preference/
    Observation. Returns ``(accepted, rejected_older)``. Backends without
    the typed add API contribute nothing (not errors). For Observations
    there is no ``get_observation`` API, so existing state is indexed once
    via *lister_name*.
    """
    accepted = 0
    rejected_older = 0
    if not raw_nodes:
        return accepted, rejected_older
    adder = getattr(store, adder_name, None)
    if not callable(adder):
        return accepted, rejected_older
    getter = getattr(store, getter_name, None) if getter_name else None
    index: Optional[dict] = None
    if not callable(getter) and lister_name:
        lister = getattr(store, lister_name, None)
        if callable(lister):
            try:
                index = {n.id: n for n in lister(limit=100000)}
            except Exception as exc:
                logger.debug("sync_push: %s listing failed: %s", lister_name, exc)
                index = {}
    for raw in raw_nodes:
        try:
            incoming = cls.from_dict(raw)
        except Exception as exc:
            logger.debug("sync_push: skip unparseable %s: %s", cls.__name__, exc)
            continue
        existing = None
        try:
            if callable(getter):
                existing = getter(incoming.id)
            elif index is not None:
                existing = index.get(incoming.id)
        except Exception:
            existing = None
        # LWW: reject if incoming is older than or equal to the stored copy.
        if existing is not None and (incoming.updated_at or "") <= (
            getattr(existing, "updated_at", None) or ""
        ):
            rejected_older += 1
            continue
        try:
            adder(incoming)
        except NotImplementedError:
            logger.debug("sync_push: backend has no %s storage; skipping rest", cls.__name__)
            break
        except Exception as exc:
            logger.debug("sync_push: failed to store %s %s: %s", cls.__name__, incoming.id, exc)
            continue
        accepted += 1
    return accepted, rejected_older


# ── Sync tombstone helpers ───────────────────────────────────────────
# Server-side record of deleted func_ids so other nodes can replicate
# deletions. Kept as a small JSON sidecar (not in the main store) so the
# sync layer does not need to change the MemoryStore contract.


def _tombstone_path(config) -> Path:
    """Return the legacy Lite-only tombstone sidecar path.

    PostgreSQL ``storage.path`` is a credential-bearing DSN, not a filesystem
    root.  Treating it as a path can both create a bogus ``postgresql:/`` tree
    and disclose credentials through filesystem or logging diagnostics.
    """
    if config.storage.backend != "lite":
        raise RuntimeError("legacy tombstone sidecar requires Lite storage")
    return Path(config.storage.path).expanduser() / "tombstones.json"


def _tombstone_storage_key(tenant_id: str, func_id: str) -> str:
    """Return a collision-free v2 sidecar key for a tenant-local memory ID.

    Function IDs are tenant-local in PostgreSQL, so the historic flat
    ``{func_id: tombstone}`` sidecar format loses one deletion when two
    tenants use the same ID. The original ID remains in the value for wire
    compatibility; callers never need to parse this storage-only key.
    """
    return "v2:" + json.dumps([tenant_id, func_id], ensure_ascii=False, separators=(",", ":"))


def _record_tombstone(
    config,
    func_id: str,
    deleted_version: str = "",
    *,
    tenant_id: str = "",
) -> None:
    """Record a deletion tombstone with the deleted record's updated_at.

    The *deleted_version* lets pulling clients detect delete-vs-edit
    conflicts: if the client's local copy has a newer updated_at than the
    tombstone, the edit happened after the delete and must be kept.
    """
    try:
        path = _tombstone_path(config)
        path.parent.mkdir(parents=True, exist_ok=True)
        tombstones: dict = {}
        if path.exists():
            tombstones = json.loads(path.read_text(encoding="utf-8"))
        tombstone = {
            "deleted_at": datetime.now(timezone.utc).isoformat(),
            "deleted_version": deleted_version,
        }
        if tenant_id:
            tombstone["tenant_id"] = tenant_id
            tombstone["func_id"] = func_id
            storage_key = _tombstone_storage_key(tenant_id, func_id)
        else:
            # Keep direct legacy helper callers compatible. Every real HTTP
            # deletion passes an authenticated tenant and therefore writes v2.
            storage_key = func_id
        tombstones[storage_key] = tombstone
        path.write_text(json.dumps(tombstones, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        logger.warning("failed to record tombstone for %s", func_id, exc_info=True)


def _read_tombstones(
    config,
    since: Optional[str] = None,
    *,
    tenant_id: Optional[str] = None,
) -> list:
    """Return tombstones optionally filtered by *since* (iso8601).

    Handles both the new format (``{deleted_at, deleted_version}``) and
    the legacy format (bare iso8601 string) for backward compatibility.
    """
    try:
        path = _tombstone_path(config)
        if not path.exists():
            return []
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        logger.warning("failed to read tombstones", exc_info=True)
        return []
    items = []
    for stored_key, val in raw.items():
        if isinstance(val, dict):
            # New format.
            deleted_at = val.get("deleted_at", "")
            deleted_version = val.get("deleted_version", "")
            func_id = val.get("func_id", stored_key)
        else:
            # Legacy format: bare iso8601 string (no version).
            deleted_at = str(val)
            deleted_version = ""
            func_id = stored_key
        item_tenant = val.get("tenant_id", "") if isinstance(val, dict) else ""
        # A tenant-scoped request must not learn about legacy or foreign
        # tombstones.  Legacy callers without an established tenant retain
        # the historical aggregate view for compatibility.
        if tenant_id is not None and item_tenant != tenant_id:
            continue
        items.append(
            {
                "func_id": func_id,
                "deleted_at": deleted_at,
                "deleted_version": deleted_version,
            }
        )
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

# A subscriber is normally ``(queue, tenant_id)``.  The bare-queue fallback
# remains for older in-process callers/tests, while HTTP-created subscribers
# are always tenant-bound.
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
        logger.debug("Redis auto-probe failed for %s:%s", host, port, exc_info=True)
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
                        logger.debug("skipping malformed Redis SSE message", exc_info=True)
        except Exception as exc:
            logger.debug("Redis subscriber thread stopped: %s", exc)

    _redis_pubsub_thread = threading.Thread(
        target=_sub_loop, name="memplex-sse-redis-sub", daemon=True
    )
    _redis_pubsub_thread.start()


def _fanout_local(event: dict) -> None:
    """Push an event only to subscribers in the event's tenant scope."""
    dead = set()
    event_tenant = event.get("tenant_id")
    for subscriber in _SSE_SUBSCRIBERS:
        if isinstance(subscriber, tuple) and len(subscriber) == 2:
            queue, subscriber_tenant = subscriber
            # HTTP-created subscriptions are fail-closed: an event without
            # an established tenant is not deliverable to any tenant stream.
            if subscriber_tenant != event_tenant:
                continue
        else:
            # Compatibility for direct in-process consumers that predate
            # tenant-aware SSE. HTTP endpoints never create this form.
            queue = subscriber
        try:
            queue.put_nowait(event)
        except Exception:
            dead.add(subscriber)
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
    principal_registry = os.environ.get("MEMPLEX_PRINCIPALS_JSON")

    if non_local and not api_key and not bearer_token and principal_registry is None:
        raise RuntimeError(
            f"Refusing to bind to non-local host {host!r} without "
            "authentication. Set MEMPLEX_PRINCIPALS_JSON, MEMPLEX_API_KEY, or MEMPLEX_BEARER_TOKEN "
            "to enable auth, or bind to 127.0.0.1/localhost/::1."
        )


def _is_remote_peer(host: Optional[str]) -> bool:
    """Return whether a concrete IP peer is outside the loopback range.

    This compatibility helper intentionally does not make a trust decision:
    a missing or malformed peer is not *known* remote, but it is also not
    eligible for unauthenticated development access.  See
    :func:`_is_loopback_peer` for that stricter positive check.
    """
    if not host:
        return False
    try:
        return not ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_loopback_peer(host: Optional[str]) -> bool:
    """Return ``True`` only for a syntactically valid loopback IP address."""
    if not host:
        return False
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _request_deployment_profile(request: Request) -> str:
    """Read the normalized deployment profile stored by the app factory."""
    deployment_profile = getattr(request.app.state, "deployment_profile", "development")
    return str(deployment_profile).strip().lower()


def _has_forwarded_client_headers(request: Request) -> bool:
    """Whether an untrusted proxy/client-origin header was supplied."""
    headers = getattr(request, "headers", {})
    try:
        return any(str(name).lower() in _FORWARDED_CLIENT_HEADERS for name in headers)
    except TypeError:
        return any(
            getattr(headers, "get", lambda _name: None)(name) is not None
            for name in _FORWARDED_CLIENT_HEADERS
        )


def _unauthenticated_uds_is_allowed(request: Request) -> bool:
    """Allow UDS open access only through one explicit development opt-in."""
    return (
        _request_deployment_profile(request) == "development"
        and os.environ.get(_UNAUTHENTICATED_UDS_ENV) == "1"
    )


def _require_maintenance_access(request: Request) -> AuthorizationContext:
    """Restrict HTTP compaction to development maintenance principals."""
    if _request_deployment_profile(request) != "development":
        raise HTTPException(status_code=403, detail="Forbidden")
    context = _authorization(request)
    if "maintenance" not in context.principal.roles:
        raise HTTPException(status_code=403, detail="Forbidden")
    return context


def _safe_extracted_response(service: "MemplexService", extracted: object) -> dict:
    """Serialize an extraction without echoing model-unsafe node content."""
    payload = _dataclass_to_dict(extracted)
    unsafe_ids: set[str] = set()
    for field in ("functions", "facts", "preferences"):
        nodes = list(getattr(extracted, field, []) or [])
        safe_nodes = []
        for node in nodes:
            if service.is_safe_for_model(node):
                safe_nodes.append(node)
            else:
                unsafe_ids.add(str(getattr(node, "id", "") or ""))
        payload[field] = _dataclass_to_dict(safe_nodes)

    graph = getattr(extracted, "graph", None)
    if graph is not None and isinstance(payload.get("graph"), dict):
        safe_graph_nodes = []
        for node in list(getattr(graph, "nodes", []) or []):
            if service.is_safe_for_model(node):
                safe_graph_nodes.append(node)
            else:
                unsafe_ids.add(str(getattr(node, "id", "") or ""))
        safe_edges = [
            edge
            for edge in list(getattr(graph, "edges", []) or [])
            if str(getattr(edge, "source", "") or "") not in unsafe_ids
            and str(getattr(edge, "target", "") or "") not in unsafe_ids
            and service.is_safe_for_model(edge)
        ]
        payload["graph"]["nodes"] = _dataclass_to_dict(safe_graph_nodes)
        payload["graph"]["edges"] = _dataclass_to_dict(safe_edges)
    if unsafe_ids:
        payload["withheld_unsafe"] = len(unsafe_ids)
    return payload


if _FASTAPI_AVAILABLE:

    def _require_auth(
        request: Request,
        x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
        authorization: Optional[str] = Header(None),
    ) -> AuthorizationContext:
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
        if request.url.path in {"/health/live", "/health/ready"}:
            context = local_development_context()
            request.state.authorization = context
            return context
        registry = getattr(request.app.state, "principal_registry", None)
        request_id = request.headers.get("X-Request-ID", "")
        session_id = request.headers.get("X-Memplex-Session-ID", "")

        if registry is not None:
            # A configured registry is authoritative: neither absent nor
            # unknown credentials fall back to the legacy shared secret.
            token = x_api_key
            if token is None and authorization is not None:
                scheme, _, candidate = authorization.partition(" ")
                if scheme.lower() == "bearer" and candidate.strip():
                    token = candidate.strip()
            context = registry.authenticate(
                token or "",
                request_id=request_id,
                session_id=session_id,
                provenance={"transport": "http"},
            )
            if context is None:
                raise HTTPException(
                    status_code=401,
                    detail="Valid X-API-Key or Authorization: Bearer token required",
                    headers={"WWW-Authenticate": 'Bearer realm="memplex"'},
                )
            request.state.authorization = context
            return context

        api_key = os.environ.get("MEMPLEX_API_KEY")
        bearer_token = os.environ.get("MEMPLEX_BEARER_TOKEN")

        # No credentials configured → access is granted only to a concrete
        # loopback IP.  Forwarded client headers are never a substitute for
        # transport authentication.  A missing/non-IP peer (including UDS)
        # fails closed, except for the documented development-only UDS opt-in.
        if not api_key and not bearer_token:
            client = request.client
            peer = client.host if client is not None else None
            if _request_deployment_profile(request) != "development":
                raise HTTPException(status_code=403, detail="Authentication required")
            if _has_forwarded_client_headers(request):
                raise HTTPException(status_code=403, detail="Authentication required")
            if _is_loopback_peer(peer):
                context = local_development_context()
                request.state.authorization = context
                return context
            if client is None and _unauthenticated_uds_is_allowed(request):
                context = local_development_context()
                request.state.authorization = context
                return context
            raise HTTPException(status_code=403, detail="Authentication required")

        # Validate the X-API-Key header against a configured API key.
        if api_key is not None and x_api_key is not None:
            if hmac.compare_digest(x_api_key.encode("utf-8"), api_key.encode("utf-8")):
                context = local_development_context()
                request.state.authorization = context
                return context

        # Validate the Authorization: Bearer header against a configured token.
        if bearer_token is not None and authorization is not None:
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() == "bearer" and token.strip():
                if hmac.compare_digest(token.strip().encode("utf-8"), bearer_token.encode("utf-8")):
                    context = local_development_context()
                    request.state.authorization = context
                    return context

        raise HTTPException(
            status_code=401,
            detail="Valid X-API-Key or Authorization: Bearer token required",
            headers={"WWW-Authenticate": 'Bearer realm="memplex"'},
        )


# ── Rate limiting (simple in-memory token bucket per client IP) ──────

_RATE_LIMIT_WINDOW = 60  # seconds
_RATE_LIMIT_MAX = 120  # requests per window per IP
_RATE_BUCKET_CAPACITY = 4096
_rate_bucket_lock = threading.RLock()
_rate_buckets: dict[str, dict[str, int | float]] = {}


def _check_rate_limit(client_ip: str) -> bool:
    """Return True if the request is allowed, False if rate-limited."""
    now = time.monotonic()
    with _rate_bucket_lock:
        bucket = _rate_buckets.get(client_ip)
        if bucket is not None:
            if now >= float(bucket["reset_at"]):
                bucket["count"] = 1
                bucket["reset_at"] = now + _RATE_LIMIT_WINDOW
                return True
            if int(bucket["count"]) >= _RATE_LIMIT_MAX:
                return False
            bucket["count"] = int(bucket["count"]) + 1
            return True

        if len(_rate_buckets) >= _RATE_BUCKET_CAPACITY:
            expired = [
                key
                for key, candidate in _rate_buckets.items()
                if now >= float(candidate["reset_at"])
            ]
            for key in expired:
                _rate_buckets.pop(key, None)
            if len(_rate_buckets) >= _RATE_BUCKET_CAPACITY:
                return False

        _rate_buckets[client_ip] = {
            "count": 1,
            "reset_at": now + _RATE_LIMIT_WINDOW,
        }
        return True


if _FASTAPI_AVAILABLE:

    def _rate_limit_dependency(request: Request) -> None:
        """Permissive rate limiter: only enforces when auth is configured
        (i.e. a public-facing deployment). Local dev (no auth) is exempt."""
        if request.url.path in {"/health/live", "/health/ready"}:
            return
        api_key = os.environ.get("MEMPLEX_API_KEY")
        bearer = os.environ.get("MEMPLEX_BEARER_TOKEN")
        registry = getattr(request.app.state, "principal_registry", None)
        if not api_key and not bearer and registry is None:
            return  # local dev: no limit
        client_ip = request.client.host if request.client else "unknown"
        if not _check_rate_limit(client_ip):
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded ({_RATE_LIMIT_MAX} req/{_RATE_LIMIT_WINDOW}s). "
                "Retry shortly.",
            )


# ── App factory ─────────────────────────────────────────────────────



def _register_memory_routes(app: "FastAPI", config, profile: str) -> None:
    """Register the /memories* CRUD, feedback, and review routes."""
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
        context = _authorization(request)
        result = svc.write(source, authorization=context)
        # Notify SSE subscribers that new memories are available.
        safe_payload = _safe_extracted_response(svc, result)
        func_ids = [item["id"] for item in safe_payload.get("functions", [])]
        _broadcast_event(
            {
                "type": "write",
                "func_ids": func_ids,
                "tenant_id": context.principal.tenant_id,
            }
        )
        return JSONResponse(safe_payload)

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
        context = _authorization(request)
        result = await svc.query_async(
            text=q,
            top_k=top_k,
            # Query-string owner is caller-controlled metadata; it may not
            # select or widen an ACL scope. Service authorization supplies
            # the tenant/workspace boundary independently.
            owner=None,
            max_tokens=max_tokens,
            authorization=context,
        )
        payload = _dataclass_to_dict(result)
        # SearchResult historically exposed ``func_id`` while every other
        # memory route uses ``id``. Keep the canonical field and add the
        # stable route-level alias so callers can follow an authorized result
        # directly without guessing its identifier shape.
        for item in payload.get("results", []):
            if isinstance(item, dict) and "id" not in item and "func_id" in item:
                item["id"] = item["func_id"]
        return JSONResponse(payload)

    # Concrete paths MUST be registered before the parameterized
    # ``/memories/{memory_id}`` route: FastAPI matches in registration
    # order, so ``/memories/pending_reviews`` placed after it would be
    # shadowed and resolved as memory_id="pending_reviews" (404).
    @app.get("/memories/pending_reviews", summary="List pending reviews")
    async def pending_reviews(
        request: Request,
        owner: Optional[str] = Query(None),
        limit: int = Query(100, ge=1, le=1000),
    ) -> JSONResponse:
        """Retrieve pending feedback reviews."""
        svc = _get_service(request)
        reviews = svc.get_pending_reviews(
            owner=None,
            limit=limit,
            authorization=_authorization(request),
        )
        return JSONResponse(
            {
                "total": len(reviews),
                "reviews": _dataclass_to_dict(reviews),
            }
        )

    @app.get("/memories/{memory_id}", summary="Get memory detail")
    async def get_memory(request: Request, memory_id: str) -> JSONResponse:
        """Retrieve a single memory by ID."""
        svc = _get_service(request)
        func = svc.get(memory_id, authorization=_authorization(request))
        if func is None:
            raise HTTPException(status_code=404, detail="Memory not found")
        return JSONResponse(_dataclass_to_dict(func))

    @app.patch("/memories/{memory_id}", summary="Update a memory field")
    async def update_memory(
        request: Request, memory_id: str, body: Any = Body(...)
    ) -> JSONResponse:
        """Update a visible Function field without exposing foreign IDs."""
        svc = _get_service(request)
        context = _authorization(request)
        if type(body) is not dict:
            raise HTTPException(status_code=422, detail="Invalid update request")
        role = body.get("role")
        new_value = body.get("new_value")
        if type(role) is not str or type(new_value) is not str:
            raise HTTPException(status_code=422, detail="Invalid update request")
        try:
            result = svc.update_memory(
                memory_id,
                role,
                new_value,
                authorization=context,
            )
        except MemoryNotFoundError:
            raise HTTPException(status_code=404, detail="Memory not found") from None
        payload = _dataclass_to_dict(result)
        if svc.get(memory_id, authorization=context) is None:
            payload["old_value"] = None
            payload["new_value"] = None
            payload["withheld_unsafe"] = True
        return JSONResponse(payload)

    @app.get("/memories/{memory_id}/timeline", summary="Get memory timeline")
    async def get_timeline(request: Request, memory_id: str) -> JSONResponse:
        """Get the changelog timeline for a memory."""
        svc = _get_service(request)
        context = _authorization(request)
        if svc.get(memory_id, authorization=context) is None:
            raise HTTPException(status_code=404, detail="Memory not found")
        events = svc.get_timeline(memory_id, authorization=context)
        return JSONResponse(_dataclass_to_dict(events))

    @app.delete("/memories/{memory_id}", summary="Delete memory")
    async def delete_memory(request: Request, memory_id: str) -> JSONResponse:
        """Soft-delete a memory and record a sync tombstone."""
        svc = _get_service(request)
        context = _authorization(request)
        # Snapshot updated_at BEFORE deleting so the tombstone carries the
        # version it deleted. Pull clients use this to decide: if their
        # local copy is NEWER than the tombstone, the edit happened after
        # the delete and must be kept (fixes the delete-vs-edit bug).
        existing = svc.get(memory_id, authorization=context)
        if existing is None:
            raise HTTPException(status_code=404, detail="Memory not found")
        deleted_version = getattr(existing, "updated_at", None) or ""
        try:
            svc.delete(memory_id, authorization=context)
        except MemoryNotFoundError:
            raise HTTPException(status_code=404, detail="Memory not found") from None
        if profile != "production" and not config.sync.enabled:
            _record_tombstone(
                config,
                memory_id,
                deleted_version,
                tenant_id=context.principal.tenant_id,
            )
        _broadcast_event(
            {
                "type": "delete",
                "func_id": memory_id,
                "tenant_id": context.principal.tenant_id,
            }
        )
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
        try:
            svc.submit_feedback(
                memory_id=memory_id,
                field_role=body["role"],
                value_index=body["index"],
                verdict=body["verdict"],
                reason=body.get("reason"),
                authorization=_authorization(request),
            )
        except (KeyError, MemoryNotFoundError):
            raise HTTPException(status_code=404, detail="Memory not found") from None
        return JSONResponse({"status": "recorded"})

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
        try:
            result = svc.apply_resolution(
                memory_id=memory_id,
                field_role=body["field_role"],
                action=body["action"],
                new_value=body.get("new_value"),
                authorization=_authorization(request),
            )
        except (KeyError, MemoryNotFoundError):
            raise HTTPException(status_code=404, detail="Memory not found") from None
        return JSONResponse(result)



def _register_health_routes(app: "FastAPI") -> None:
    """Register health probes, stats, and manual compaction."""
    @app.get("/health", summary="Health check")
    async def health(
        request: Request,
    ) -> JSONResponse:
        """Return service health status."""
        svc = _get_service(request)
        return JSONResponse(svc.health())

    @app.get("/health/live", summary="Liveness probe")
    async def health_live() -> JSONResponse:
        return JSONResponse({"schema_version": 1, "status": "live"})

    @app.get("/health/ready", summary="Readiness probe")
    async def health_ready(request: Request) -> JSONResponse:
        status = _get_service(request).readiness_status()
        return JSONResponse(status, status_code=200 if status["status"] == "ready" else 503)

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
        _require_maintenance_access(request)
        svc = _get_service(request)
        scope = "project"
        if body:
            scope = body.get("scope", "project")
        result = svc.compact(scope=scope)
        return JSONResponse(_dataclass_to_dict(result))

    # ════════════════════════════════════════════════════════════════
    #  Sync endpoints (multi-node sharing)
    # ════════════════════════════════════════════════════════════════





def _sync_bindings(context: AuthorizationContext) -> tuple[str, str, str]:
    tenant_id = context.principal.tenant_id
    remote_id = context.agent_id or context.principal.subject_id
    consumer_id = (
        context.principal.authentication_id or context.principal.subject_id
    )
    return tenant_id, remote_id, consumer_id

def _sync_cursor_codec(config):
    from memplex.sync_protocol import SyncCursorCodec

    return SyncCursorCodec(
        config.sync.cursor_signing_key_id,
        config.sync.cursor_signing_secret,
        config.sync.cursor_previous_signing_keys,
    )

def _encode_stream_cursor(
config,
*,
    tenant_id: str,
    remote_id: str,
    consumer_id: str,
    after_seq: int,
    snapshot_seq: int,
) -> str:
    from memplex.sync_protocol import SyncCursorClaims

    now = datetime.now(timezone.utc)
    claims = SyncCursorClaims(
        1,
        config.sync.cursor_signing_key_id,
        tenant_id,
        remote_id,
        consumer_id,
        after_seq,
        snapshot_seq,
        None,
        None,
        now,
        now + timedelta(seconds=config.sync.cursor_ttl_seconds),
    )
    return _sync_cursor_codec(config).encode(claims)

def _encode_snapshot_cursor(
config,
*,
    tenant_id: str,
    remote_id: str,
    consumer_id: str,
    snapshot_id: str,
    snapshot_seq: int,
    snapshot_after,
) -> str:
    from memplex.sync_protocol import SyncCursorClaims

    now = datetime.now(timezone.utc)
    claims = SyncCursorClaims(
        1,
        config.sync.cursor_signing_key_id,
        tenant_id,
        remote_id,
        consumer_id,
        0,
        snapshot_seq,
        snapshot_id,
        snapshot_after,
        now,
        now + timedelta(seconds=config.sync.cursor_ttl_seconds),
    )
    return _sync_cursor_codec(config).encode(claims)



def _register_sync_v1_routes(app: "FastAPI", config) -> None:  # noqa: C901  documented known debt
    """Register the snapshot-stable /sync/v1 endpoints."""
    @app.post("/sync/v1/batches", summary="Apply one canonical atomic sync batch")
    async def sync_v1_batches(request: Request) -> JSONResponse:
        from memplex.sync_ingress import validate_ingress_batch
        from memplex.sync_repository import SyncBackpressureError, SyncBatchRejected

        if not config.sync.enabled:
            raise HTTPException(status_code=404, detail="sync_not_enabled")
        declared = request.headers.get("content-length")
        if declared is not None:
            try:
                declared_size = int(declared)
            except ValueError:
                raise HTTPException(
                    status_code=400, detail="invalid_content_length"
                ) from None
            if declared_size > config.sync.max_batch_bytes:
                raise HTTPException(
                    status_code=413, detail="sync_batch_too_large"
                )
        body = bytearray()
        async for chunk in request.stream():
            if len(body) + len(chunk) > config.sync.max_batch_bytes:
                raise HTTPException(status_code=413, detail="sync_batch_too_large")
            body.extend(chunk)
        raw = bytes(body)
        # Shared-key payload encryption (opt-in): unwrap envelope bytes BEFORE
        # canonical validation, so the digest is computed over the plaintext.
        from memplex import sync_crypto

        if sync_crypto.looks_encrypted(raw):
            try:
                raw = sync_crypto.decrypt_bytes(raw)
            except sync_crypto.SyncCryptoError as exc:
                raise HTTPException(
                    status_code=400, detail="sync_encryption_invalid"
                ) from exc
            if len(raw) > config.sync.max_batch_bytes:
                raise HTTPException(status_code=413, detail="sync_batch_too_large")
        try:
            envelope = validate_ingress_batch(raw, hashlib.sha256(raw).hexdigest())
            if len(envelope.batch.events) > config.sync.max_batch_events:
                raise SyncBatchRejected("batch exceeds configured event limit")
            context = _authorization(request)
            tenant_id, remote_id, _ = _sync_bindings(context)
            if envelope.batch.origin_node_id != remote_id or any(
                event.scope.tenant_id != tenant_id for event in envelope.batch.events
            ):
                raise SyncBatchRejected("invalid ingress identity")
            svc = _get_service(request)
            svc.scan_sync_events_before_persistence(envelope.batch.events)
            store = svc._store_for(context)
            result = store.sync_apply_batch(envelope.batch)
        except SyncBackpressureError:
            raise HTTPException(status_code=429, detail="sync_backpressure") from None
        except SyncBatchRejected:
            raise HTTPException(status_code=422, detail="invalid_sync_batch") from None
        except (KeyError, TypeError, ValueError) as exc:
            if str(exc) == "batch digest conflict":
                raise HTTPException(status_code=409, detail="batch_conflict") from None
            raise HTTPException(status_code=422, detail="invalid_sync_batch") from None
        except RuntimeError:
            raise HTTPException(status_code=503, detail="sync_apply_unavailable") from None
        except Exception as exc:
            sqlstate = _exception_sqlstate(exc)
            if sqlstate == "23505":
                raise HTTPException(status_code=409, detail="batch_conflict") from None
            if sqlstate == "54000":
                raise HTTPException(status_code=429, detail="sync_backpressure") from None
            if sqlstate in {"22023", "23503", "23514", "42501"}:
                raise HTTPException(status_code=422, detail="invalid_sync_batch") from None
            raise HTTPException(status_code=503, detail="sync_apply_unavailable") from None
        return JSONResponse(result.to_dict())

    @app.get("/sync/v1/changes", summary="Read a snapshot-stable sync event page")
    async def sync_v1_changes(
        request: Request,
        cursor: Optional[str] = Query(None),
        limit: int = Query(500),
    ) -> JSONResponse:
        from memplex.sync_repository import SyncCursorExpired

        if not config.sync.enabled:
            raise HTTPException(status_code=404, detail="sync_not_enabled")
        if type(limit) is not int or not 1 <= limit <= config.sync.max_page_size:
            raise HTTPException(status_code=422, detail="invalid_sync_limit")
        context = _authorization(request)
        tenant_id, remote_id, consumer_id = _sync_bindings(context)
        claims = None
        if cursor is not None:
            try:
                claims = _sync_cursor_codec(config).decode(
                    cursor,
                    tenant_binding=tenant_id,
                    remote_binding=remote_id,
                    consumer_binding=consumer_id,
                )
                if claims.snapshot_id is not None:
                    raise SyncCursorExpired("invalid_cursor")
            except (TypeError, ValueError, SyncCursorExpired):
                raise HTTPException(status_code=400, detail="invalid_cursor") from None
        store = _get_service(request)._store_for(context)
        try:
            page = store.sync_page(remote_id, consumer_id, claims, limit)
        except SyncCursorExpired as exc:
            if str(exc) == "cursor_expired":
                raise HTTPException(status_code=409, detail="cursor_expired") from None
            raise HTTPException(status_code=400, detail="invalid_cursor") from None
        except Exception:
            raise HTTPException(status_code=503, detail="sync_read_unavailable") from None
        next_cursor = _encode_stream_cursor(
            config,
            tenant_id=tenant_id,
            remote_id=remote_id,
            consumer_id=consumer_id,
            after_seq=page.next_after_seq,
            snapshot_seq=page.snapshot_seq,
        )
        return JSONResponse(
            {
                "items": [
                    {
                        "stream_seq": item.stream_seq,
                        "event": item.event.to_dict(),
                    }
                    for item in page.items
                ],
                "snapshot_seq": page.snapshot_seq,
                "next_cursor": next_cursor,
                "has_more": page.has_more,
            }
        )

    @app.get("/sync/v1/snapshot", summary="Read an immutable sync snapshot")
    async def sync_v1_snapshot(
        request: Request,
        request_id: Optional[str] = Query(None),
        cursor: Optional[str] = Query(None),
        limit: int = Query(500),
    ) -> JSONResponse:
        from memplex.sync_repository import SyncBackpressureError, SyncCursorExpired

        if not config.sync.enabled:
            raise HTTPException(status_code=404, detail="sync_not_enabled")
        if type(limit) is not int or not 1 <= limit <= config.sync.max_page_size:
            raise HTTPException(status_code=422, detail="invalid_sync_limit")
        if (request_id is None) == (cursor is None):
            raise HTTPException(status_code=422, detail="invalid_snapshot_request")
        context = _authorization(request)
        tenant_id, remote_id, consumer_id = _sync_bindings(context)
        store = _get_service(request)._store_for(context)
        try:
            if cursor is None:
                if type(request_id) is not str or not request_id:
                    raise ValueError("request_id must be non-empty")
                page = store.sync_create_snapshot(
                    remote_id, consumer_id, request_id, limit
                )
            else:
                claims = _sync_cursor_codec(config).decode(
                    cursor,
                    tenant_binding=tenant_id,
                    remote_binding=remote_id,
                    consumer_binding=consumer_id,
                )
                if claims.snapshot_id is None:
                    raise SyncCursorExpired("invalid_cursor")
                page = store.sync_snapshot_page(
                    remote_id, consumer_id, claims, limit
                )
        except SyncBackpressureError as exc:
            code = str(exc)
            status = 413 if code == "snapshot_too_large" else 409
            raise HTTPException(status_code=status, detail=code) from None
        except SyncCursorExpired as exc:
            if str(exc) == "snapshot_expired":
                raise HTTPException(
                    status_code=409, detail="snapshot_expired"
                ) from None
            raise HTTPException(status_code=400, detail="invalid_cursor") from None
        except (TypeError, ValueError):
            raise HTTPException(status_code=422, detail="invalid_snapshot_request") from None
        except Exception:
            raise HTTPException(
                status_code=503, detail="sync_snapshot_unavailable"
            ) from None

        next_cursor = None
        resume_cursor = None
        if page.has_more:
            next_cursor = _encode_snapshot_cursor(
            config,
                tenant_id=tenant_id,
                remote_id=remote_id,
                consumer_id=consumer_id,
                snapshot_id=page.snapshot_id,
                snapshot_seq=page.resume_seq,
                snapshot_after=page.next_anchor,
            )
        else:
            resume_cursor = _encode_stream_cursor(
            config,
                tenant_id=tenant_id,
                remote_id=remote_id,
                consumer_id=consumer_id,
                after_seq=page.resume_seq,
                snapshot_seq=page.resume_seq,
            )
        return JSONResponse(
            {
                "events": [event.to_dict() for event in page.events],
                "snapshot_id": page.snapshot_id,
                "next_cursor": next_cursor,
                "resume_cursor": resume_cursor,
                "has_more": page.has_more,
            }
        )


def _register_legacy_sync_endpoint_routes(app: "FastAPI", config, profile: str) -> None:  # noqa: C901  documented known debt
    """Register the two legacy /sync endpoints (changes + push)."""
    def _legacy_sync_v1_changes(
        request: Request,
        context: AuthorizationContext,
    ) -> JSONResponse:
        """Serve the development-only legacy shape from the durable stream.

        The timestamp query parameter is intentionally no longer authoritative:
        repository consumer progress is sequence based, so concurrent commits
        cannot fall between a response and a wall-clock cursor.
        """
        from memplex.sync_protocol import (
            SyncCursorClaims,
            SyncNodeType,
            SyncOperation,
            SyncVersion,
        )

        tenant_id, remote_id, consumer_id = _sync_bindings(context)
        svc = _get_service(request)
        store = svc._store_for(context)
        page = store.sync_page(
            remote_id,
            consumer_id,
            None,
            config.sync.max_page_size,
        )
        if page.has_more:
            raise HTTPException(
                status_code=426,
                detail="sync_v1_upgrade_required",
                headers={"Upgrade": "memplex-sync-v1"},
            )

        changes: dict[SyncNodeType, list[dict[str, object]]] = {
            SyncNodeType.FUNCTION: [],
            SyncNodeType.FACT: [],
            SyncNodeType.PREFERENCE: [],
            SyncNodeType.OBSERVATION: [],
        }
        tombstones: list[dict[str, str]] = []
        for item in page.items:
            event = item.event
            if event.operation is SyncOperation.UPSERT:
                if event.node_type in changes and event.payload is not None:
                    changes[event.node_type].append(event.to_dict()["payload"])
                continue
            if (
                event.node_type is SyncNodeType.FUNCTION
                and event.entity_key.node_id is not None
            ):
                tombstones.append(
                    {
                        "func_id": event.entity_key.node_id,
                        "deleted_at": SyncVersion.parse(event.version)
                        .occurred_at.astimezone(timezone.utc)
                        .isoformat(),
                        "deleted_version": "",
                    }
                )

        # Confirm exactly the page just returned. A completed cursor opens a
        # fresh snapshot, but never confirms events committed after this page.
        now = datetime.now(timezone.utc)
        store.sync_page(
            remote_id,
            consumer_id,
            SyncCursorClaims(
                1,
                config.sync.cursor_signing_key_id,
                tenant_id,
                remote_id,
                consumer_id,
                page.next_after_seq,
                page.snapshot_seq,
                None,
                None,
                now,
                now + timedelta(seconds=config.sync.cursor_ttl_seconds),
            ),
            1,
        )
        return JSONResponse(
            {
                "changes": changes[SyncNodeType.FUNCTION],
                "fact_changes": changes[SyncNodeType.FACT],
                "preference_changes": changes[SyncNodeType.PREFERENCE],
                "observation_changes": changes[SyncNodeType.OBSERVATION],
                "tombstones": tombstones,
                "server_time": datetime.now(timezone.utc).isoformat(),
            }
        )

    def _legacy_sync_v1_push(
        request: Request,
        context: AuthorizationContext,
        body: dict,
    ) -> JSONResponse:
        """Translate one bounded legacy development push to one atomic batch."""
        import uuid

        from memplex.models import Fact, Function, Observation, Preference
        from memplex.sync_protocol import (
            SyncBatch,
            SyncEntityKey,
            SyncEvent,
            SyncNodeType,
            SyncOperation,
            SyncScope,
            SyncVersion,
        )
        from memplex.sync_repository import SyncBackpressureError, SyncBatchRejected

        if type(body) is not dict:
            raise HTTPException(status_code=422, detail="Invalid memory payload")
        node_specs = (
            ("functions", Function, SyncNodeType.FUNCTION),
            ("facts", Fact, SyncNodeType.FACT),
            ("preferences", Preference, SyncNodeType.PREFERENCE),
            ("observations", Observation, SyncNodeType.OBSERVATION),
        )
        if set(body) - {item[0] for item in node_specs}:
            raise HTTPException(status_code=422, detail="Invalid memory payload")
        if len(json.dumps(body, separators=(",", ":")).encode("utf-8")) > config.sync.max_batch_bytes:
            raise HTTPException(status_code=413, detail="sync_batch_too_large")
        total = sum(
            len(body.get(key, []))
            for key, _, _ in node_specs
            if isinstance(body.get(key, []), list)
        )
        if total > config.sync.max_batch_events:
            raise HTTPException(status_code=422, detail="invalid_sync_batch")

        svc = _get_service(request)
        store = svc._store_for(context)
        _, remote_id, _ = _sync_bindings(context)
        parsed: list[tuple[str, SyncNodeType, object]] = []
        by_type = {
            key: {"accepted": 0, "rejected_older": 0}
            for key, _, _ in node_specs
        }
        try:
            for key, cls, node_type in node_specs:
                raw_nodes = body.get(key, [])
                if type(raw_nodes) is not list:
                    raise ValueError("sync node collection must be a list")
                for raw in raw_nodes:
                    if type(raw) is not dict:
                        raise ValueError("sync node must be an object")
                    node = cls.from_dict(raw)
                    bind_node_identity(node, context, reject_conflicts=True)
                    parsed.append((key, node_type, node))
        except (IdentityClaimError, KeyError, TypeError, ValueError):
            raise HTTPException(status_code=422, detail="Invalid memory payload") from None

        svc.scan_nodes_before_persistence(node for _, _, node in parsed)

        observations = {
            item.id: item
            for item in getattr(store, "list_observations", lambda **_: [])(
                limit=100000
            )
        }
        events = []
        try:
            for key, node_type, node in parsed:
                if node_type is SyncNodeType.FUNCTION:
                    existing = store.get(node.id)
                elif node_type is SyncNodeType.FACT:
                    existing = store.get_fact(node.id)
                elif node_type is SyncNodeType.PREFERENCE:
                    existing = store.get_preference(node.id)
                else:
                    existing = observations.get(node.id)
                if existing is not None and (node.updated_at or "") <= (
                    getattr(existing, "updated_at", None) or ""
                ):
                    by_type[key]["rejected_older"] += 1
                    continue
                occurred_at = datetime.fromisoformat(
                    (node.updated_at or datetime.now(timezone.utc).isoformat()).replace(
                        "Z", "+00:00"
                    )
                )
                if occurred_at.tzinfo is None:
                    raise ValueError("updated_at must include a timezone")
                event_id = str(uuid.uuid4())
                scope = SyncScope(
                    node.tenant_id,
                    node.owner_subject_id,
                    node.workspace_id,
                    node.visibility,
                    context.agent_id or None,
                    context.session_id or None,
                )
                events.append(
                    (
                        key,
                        SyncEvent(
                            1,
                            event_id,
                            remote_id,
                            node_type,
                            SyncEntityKey.node(node.id),
                            SyncOperation.UPSERT,
                            str(SyncVersion.create(occurred_at, remote_id, event_id)),
                            scope,
                            node.to_dict(),
                        ),
                    )
                )
            if events:
                batch = SyncBatch(
                    1,
                    str(uuid.uuid4()),
                    remote_id,
                    tuple(event for _, event in events),
                )
                result = store.sync_apply_batch(batch)
                for (key, _), receipt in zip(events, result.receipts, strict=True):
                    outcome = (
                        "accepted"
                        if receipt.outcome in {"accepted", "duplicate"}
                        else "rejected_older"
                    )
                    by_type[key][outcome] += 1
        except SyncBackpressureError:
            raise HTTPException(status_code=429, detail="sync_backpressure") from None
        except (SyncBatchRejected, KeyError, TypeError, ValueError):
            raise HTTPException(status_code=422, detail="Invalid memory payload") from None

        accepted = sum(item["accepted"] for item in by_type.values())
        rejected = sum(item["rejected_older"] for item in by_type.values())
        return JSONResponse(
            {
                "accepted": accepted,
                "rejected_older": rejected,
                "by_type": by_type,
            }
        )
    @app.get("/sync/changes", summary="Pull incremental changes since a timestamp")
    async def sync_changes(
        request: Request,
        since: Optional[str] = Query(None, description="ISO-8601 cutoff; omit for all"),
    ) -> JSONResponse:
        """Return nodes with updated_at > since, plus deletion tombstones.

        Covers all four memory node types: Functions under ``changes``,
        Facts under ``fact_changes``, Preferences under
        ``preference_changes``, Observations under ``observation_changes``
        (older clients ignore the keys they do not know). Clients call
        this to pull incremental updates from the central node.
        Tombstones let clients replicate deletions -- they currently cover
        Functions only; Fact/Preference deletions stay local (documented
        limitation). Uses LWW on the client side; this endpoint just
        ships current state.
        """
        if profile == "production":
            raise HTTPException(
                status_code=426,
                detail="sync_v1_upgrade_required",
                headers={"Upgrade": "memplex-sync-v1"},
            )
        if config.sync.enabled:
            return _legacy_sync_v1_changes(request, _authorization(request))
        svc = _get_service(request)
        context = _authorization(request)
        store = svc._store_for(context)

        def visible(node) -> bool:
            return svc._is_node_visible(node, context)

        # Incremental query: use list_changes_since so the backend pushes
        # the updated_at filter into the database (Postgres WHERE) or dict
        # filter (lite), instead of loading 100k functions every pull.
        funcs = store.list_changes_since(since=since, limit=100000)
        # Models-standard serialization (Function.to_dict): the canonical
        # shape shared with the sync client, covering every field.
        changed = [function.to_dict() for function in funcs if visible(function)]
        tombstones = _read_tombstones(
            config,
            since=since,
            tenant_id=context.principal.tenant_id,
        )
        # The server's "now" gives clients a high-water mark for the next
        # pull, so they do not re-process the same window.
        server_now = datetime.now(timezone.utc).isoformat()
        return JSONResponse(
            {
                "changes": changed,
                "fact_changes": _typed_changes_since(
                    store, "list_facts", since, is_visible=visible
                ),
                "preference_changes": _typed_changes_since(
                    store, "list_preferences", since, is_visible=visible
                ),
                "observation_changes": _typed_changes_since(
                    store, "list_observations", since, is_visible=visible
                ),
                "tombstones": tombstones,
                "server_time": server_now,
            }
        )

    @app.post("/sync/push", summary="Push local changes to the central node")
    async def sync_push(request: Request, body: dict) -> JSONResponse:
        """Receive a batch of nodes and merge them with LWW by updated_at.

        Request body (every key optional)::

            {
                "functions": [<serialized Function>, ...],
                "facts": [<serialized Fact>, ...],
                "preferences": [<serialized Preference>, ...],
                "observations": [<serialized Observation>, ...]
            }

        Each node is accepted only if it is newer than the server's
        current copy (or the server has no copy). Older pushes are counted
        as rejected (not errors) so the client can see LWW in action.
        ``accepted`` / ``rejected_older`` are totals across all four
        types; ``by_type`` carries the per-type breakdown.
        """
        if profile == "production":
            raise HTTPException(
                status_code=426,
                detail="sync_v1_upgrade_required",
                headers={"Upgrade": "memplex-sync-v1"},
            )
        # Shared-key payload encryption (opt-in): unwrap the envelope before
        # any validation. Fail-closed — an undecryptable body is a 400.
        from memplex import sync_crypto

        if sync_crypto.is_encrypted_envelope(body):
            try:
                body = sync_crypto.decrypt_json_payload(body)
            except sync_crypto.SyncCryptoError as exc:
                raise HTTPException(
                    status_code=400, detail="sync_encryption_invalid"
                ) from exc
        if config.sync.enabled:
            return _legacy_sync_v1_push(request, _authorization(request), body)
        svc = _get_service(request)
        context = _authorization(request)
        store = svc._store_for(context)
        from memplex.models import (
            Fact,
            Function,
            Observation,
            Preference,
            SourceDocument,
            SourceType,
        )

        node_specs = (
            ("functions", Function),
            ("facts", Fact),
            ("preferences", Preference),
            ("observations", Observation),
        )
        parsed: dict[str, list] = {}
        try:
            for key, cls in node_specs:
                raw_nodes = body.get(key, [])
                if not isinstance(raw_nodes, list):
                    raise ValueError("sync node collection must be a list")
                bound_nodes = []
                for raw in raw_nodes:
                    if not isinstance(raw, dict):
                        raise ValueError("sync node must be an object")
                    node = cls.from_dict(raw)
                    # Validate every supplied claim before any backend write.
                    # bind_node_identity itself performs all conflict checks
                    # prior to mutating the parsed object.
                    bind_node_identity(node, context, reject_conflicts=True)
                    bound_nodes.append(node)
                parsed[key] = bound_nodes
        except (IdentityClaimError, TypeError, ValueError, KeyError):
            # Do not disclose which item, identity field, or tenant failed.
            raise HTTPException(status_code=422, detail="Invalid memory payload") from None

        svc.scan_nodes_before_persistence(
            node for nodes in parsed.values() for node in nodes
        )

        def existing_node(node):
            if getattr(node, "memory_type", "") != "observation":
                return _typed_node_from_scoped_store(store, node.id)
            lister = getattr(store, "list_observations", None)
            if not callable(lister):
                return None
            try:
                return next((item for item in lister(limit=100000) if item.id == node.id), None)
            except Exception:
                return None

        def is_current_tenant_lww_candidate(node) -> tuple[bool, object | None]:
            """Return whether this ID may be written and its visible prior copy.

            Storage migration to a composite tenant key happens in the next
            story. Until then a foreign row with the same ID is treated as an
            opaque collision: it cannot be replaced or inspected through this
            tenant's sync push.
            """
            existing = existing_node(node)
            if existing is None:
                return True, None
            if not svc._is_node_visible(existing, context):
                return False, None
            return True, existing

        accepted = 0
        rejected_older = 0
        by_type: dict = {}

        f_accepted = 0
        f_rejected = 0
        for incoming in parsed["functions"]:
            writable, existing = is_current_tenant_lww_candidate(incoming)
            if not writable or (
                existing is not None
                and (incoming.updated_at or "") <= (getattr(existing, "updated_at", None) or "")
            ):
                f_rejected += 1
                continue
            store.add(incoming, SourceDocument(type="sync_push", source_type=SourceType.WIKI))
            f_accepted += 1
        by_type["functions"] = {"accepted": f_accepted, "rejected_older": f_rejected}
        accepted += f_accepted
        rejected_older += f_rejected

        for key, adder_name in (
            ("facts", "add_fact"),
            ("preferences", "add_preference"),
            ("observations", "add_observation"),
        ):
            type_accepted = 0
            type_rejected = 0
            adder = getattr(store, adder_name, None)
            for incoming in parsed[key]:
                writable, existing = is_current_tenant_lww_candidate(incoming)
                if not writable or (
                    existing is not None
                    and (incoming.updated_at or "") <= (getattr(existing, "updated_at", None) or "")
                ):
                    type_rejected += 1
                    continue
                if callable(adder):
                    adder(incoming)
                    type_accepted += 1
            by_type[key] = {"accepted": type_accepted, "rejected_older": type_rejected}
            accepted += type_accepted
            rejected_older += type_rejected
        return JSONResponse(
            {"accepted": accepted, "rejected_older": rejected_older, "by_type": by_type}
        )



def _register_sync_events_route(app: "FastAPI") -> None:
    """Register the /sync/events SSE stream."""
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
        context = _authorization(request)
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
        subscriber = (queue, context.principal.tenant_id)
        _SSE_SUBSCRIBERS.add(subscriber)

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
                _SSE_SUBSCRIBERS.discard(subscriber)

        return StreamingResponse(
            _event_stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )


def _register_sync_routes(app: "FastAPI", config, profile: str) -> None:
    """Register sync v1 (snapshot-stable) and legacy sync routes."""
    _register_sync_v1_routes(app, config)
    _register_legacy_sync_endpoint_routes(app, config, profile)
    _register_sync_events_route(app)



def _register_metrics_routes(app: "FastAPI", operations_metrics) -> None:
    """Register the Prometheus-format /metrics endpoint."""
    @app.get("/metrics", summary="Prometheus-format metrics")
    async def metrics(request: Request) -> PlainTextResponse:
        """Return bounded low-cardinality Prometheus text metrics."""
        svc = _get_service(request)
        runtime = svc.operations_metrics_status()
        runtime["shutdown_deadline_exceeded_total"] = (
            operations_metrics.shutdown_deadline_exceeded_total
        )
        return PlainTextResponse(operations_metrics.render_prometheus(runtime))

    return app



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
    from memplex.logging_config import configure_logging, install_sensitive_data_filters
    from memplex.service import MemplexService

    # Configure logging once at app construction (the HTTP API is a
    # long-running daemon surface; honour MEMPLEX_LOG_JSON for structured
    # logs the same way the MCP server and CLI do).
    configure_logging()

    if config is None:
        config = load_config()

    operations_admission = RequestAdmission()
    operations_metrics = OperationsMetrics()

    try:
        principal_registry = PrincipalRegistry.from_environment()
    except PrincipalRegistryError as exc:
        raise RuntimeError(f"Invalid principal registry: {exc}") from exc
    deployment = getattr(config, "deployment", None)
    profile = str(getattr(deployment, "profile", "development")).strip().lower()
    if profile == "production" and principal_registry is None:
        raise RuntimeError(
            "production HTTP deployments require a principal registry "
            "(MEMPLEX_PRINCIPALS_JSON); shared secrets are development-only"
        )

    # ── Lifecycle (lifespan replaces deprecated on_event) ──────
    # Startup creates the MemplexService and starts its background
    # worker; shutdown stops it. Keeping this as a closure over
    # ``config`` mirrors the previous on_event behaviour.

    @asynccontextmanager
    async def _lifespan(app: "FastAPI"):
        install_sensitive_data_filters()
        operations_window_started_at = utc_timestamp_now()
        svc = MemplexService(config=config)
        svc.start()
        app.state.memplex_service = svc
        logger.info("Memplex HTTP API started (backend=%s)", config.storage.backend)
        try:
            yield
        finally:
            operations_admission.start_draining()
            svc.begin_draining()
            request_drained = operations_admission.wait_for_zero(
                config.operations.request_drain_timeout_seconds
            )
            service_shutdown = svc.stop()
            sync_shutdown = service_shutdown.get("sync")
            worker_shutdown = service_shutdown.get("worker")
            fully_drained = (
                request_drained
                and (sync_shutdown is None or bool(sync_shutdown.get("drained")))
                and (worker_shutdown is None or bool(worker_shutdown.get("drained")))
            )
            deadline_exceeded = (
                not fully_drained
                or bool(sync_shutdown and sync_shutdown.get("deadline_exceeded"))
                or bool(worker_shutdown and worker_shutdown.get("deadline_exceeded"))
            )
            if deadline_exceeded:
                operations_metrics.record_shutdown_deadline_exceeded()
            app.state.operations_shutdown = {
                "request_drained": request_drained,
                "deadline_exceeded": deadline_exceeded,
            }
            report_output = os.environ.get("MEMPLEX_G006_REPORT_OUTPUT")
            if report_output is not None:
                try:
                    readiness_binding = _current_operations_readiness_binding(config)
                except (
                    AttributeError,
                    OperationsEvidenceError,
                    PackageNotFoundError,
                    ReadinessEvidenceError,
                    TypeError,
                ):
                    logger.warning("operations_report_deployment_binding_invalid")
                else:
                    try:
                        window_ended_at = utc_timestamp_now()
                        report = create_operations_evidence(
                            metrics_snapshot=operations_metrics.snapshot(),
                            shutdown_result={
                                "request_drained": fully_drained,
                                "deadline_exceeded": deadline_exceeded,
                            },
                            config=config,
                            report_id=str(uuid.uuid4()),
                            window_started_at=operations_window_started_at,
                            window_ended_at=window_ended_at,
                            generated_at=utc_timestamp_now(),
                            readiness_binding=readiness_binding,
                            signing_key=load_operations_signing_key(),
                        )
                        write_operations_report_atomic(Path(report_output), report)
                    except Exception:
                        logger.warning("operations_report_write_failed")
            logger.info("Memplex HTTP API stopped")

    app = FastAPI(
        title="Memplex API",
        version="0.1.0",
        description="Multi-agent memory system REST API",
        dependencies=[Depends(_require_auth), Depends(_rate_limit_dependency)],
        lifespan=_lifespan,
    )
    app.state.principal_registry = principal_registry
    app.state.deployment_profile = profile
    app.state.operations_admission = operations_admission
    app.state.operations_metrics = operations_metrics

    @app.middleware("http")
    async def _operations_admission_middleware(request: Request, call_next):
        if request.url.path in _OPERATIONS_CONTROL_PATHS:
            return await call_next(request)
        if not operations_admission.begin():
            return JSONResponse(
                {"schema_version": 1, "status": "draining"},
                status_code=503,
            )
        operations_metrics.begin_request()
        started = time.perf_counter()
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
            return response
        finally:
            try:
                operations_metrics.finish_request(
                    request.method,
                    status_code,
                    max(0.0, time.perf_counter() - started),
                )
            finally:
                operations_admission.end()

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

    _register_memory_routes(app, config, profile)
    _register_health_routes(app)
    _register_sync_routes(app, config, profile)
    _register_metrics_routes(app, operations_metrics)

    return app


def _function_from_dict(data: dict) -> Any:
    """Reconstruct a Function from its serialized dict (sync_push payload).

    Backward-compatible alias for the models-standard
    :meth:`Function.from_dict` (Wave 2a): the hand-rolled reconstruction
    this function used to do dropped ``needs_review_until`` /
    ``priority_from_source`` / ``source_authority`` and FieldValue
    sub-fields (``observation`` / ``created_at`` / ``status``). Kept under
    its old name for existing importers; malformed payloads are caught by
    the caller and skipped.
    """
    from memplex.models import Function

    return Function.from_dict(data)
