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

import logging
import os
from dataclasses import asdict
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:
    from memplex.service import MemplexService

logger = logging.getLogger(__name__)

# FastAPI is an optional dependency -- import lazily so the rest of
# the adapter package remains importable without it.
try:
    from fastapi import FastAPI, HTTPException, Query
    from fastapi.responses import JSONResponse

    _FASTAPI_AVAILABLE = True
except ImportError:  # pragma: no cover
    _FASTAPI_AVAILABLE = False


# ── Pydantic-like request models (plain dicts for now) ──────────────
# We avoid a hard pydantic dependency by using simple dicts validated
# inside the route handlers.  When pydantic is present (FastAPI pulls
# it in), FastAPI still gets the benefit of automatic doc generation.


# ── Helpers ─────────────────────────────────────────────────────────


def _dataclass_to_dict(obj) -> Any:
    """Recursively convert dataclasses to plain dicts."""
    if hasattr(obj, "__dataclass_fields__"):
        return asdict(obj)
    if isinstance(obj, list):
        return [_dataclass_to_dict(item) for item in obj]
    if isinstance(obj, dict):
        return {k: _dataclass_to_dict(v) for k, v in obj.items()}
    return obj


def _get_service(request) -> "MemplexService":
    """Retrieve the shared MemplexService from app state."""
    return request.app.state.memplex_service


# ── Security helpers ────────────────────────────────────────────────


def _check_bind_security(app: FastAPI) -> None:
    """Warn or refuse if binding to non-local address without auth."""
    host = os.environ.get("MEMPLEX_HOST", "127.0.0.1")
    non_local = host not in ("127.0.0.1", "localhost", "::1")

    api_key = os.environ.get("MEMPLEX_API_KEY")
    bearer_token = os.environ.get("MEMPLEX_BEARER_TOKEN")

    if non_local and not api_key and not bearer_token:
        logger.warning(
            "SECURITY: Binding to %s without authentication. "
            "Set MEMPLEX_API_KEY or MEMPLEX_BEARER_TOKEN to enable auth.",
            host,
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
            "FastAPI is required for the HTTP adapter. "
            "Install it with: pip install fastapi uvicorn"
        )

    from memplex.config import load_config
    from memplex.service import MemplexService

    if config is None:
        config = load_config()

    app = FastAPI(
        title="Memplex API",
        version="0.1.0",
        description="Multi-agent memory system REST API",
    )

    # ── Lifecycle ────────────────────────────────────────────────

    @app.on_event("startup")
    async def _on_startup() -> None:
        svc = MemplexService(config=config)
        svc.start()
        app.state.memplex_service = svc
        logger.info("Memplex HTTP API started (backend=%s)", config.storage.backend)

    @app.on_event("shutdown")
    async def _on_shutdown() -> None:
        svc: Optional[MemplexService] = getattr(app.state, "memplex_service", None)
        if svc is not None:
            svc.stop()
            logger.info("Memplex HTTP API stopped")

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
    async def write_memory(body: dict) -> JSONResponse:
        """Write new content into memory.

        Request body::

            {
                "type": "text" | "file" | "url",
                "content": "...",
                "source_type": "wiki"  // optional
            }
        """
        svc = _get_service(write_memory)
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
        return JSONResponse(_dataclass_to_dict(result))

    @app.get("/memories", summary="Query memories")
    async def query_memories(
        q: str = Query(..., description="Query text"),
        top_k: int = Query(10, ge=1, le=100),
        owner: Optional[str] = Query(None),
        max_tokens: int = Query(4000, ge=0),
    ) -> JSONResponse:
        """Search memories with natural language."""
        svc = _get_service(query_memories)
        result = await svc.query_async(
            text=q,
            top_k=top_k,
            owner=owner,
            max_tokens=max_tokens,
        )
        return JSONResponse(_dataclass_to_dict(result))

    @app.get("/memories/{memory_id}", summary="Get memory detail")
    async def get_memory(memory_id: str) -> JSONResponse:
        """Retrieve a single memory by ID."""
        svc = _get_service(get_memory)
        func = svc.get(memory_id)
        if func is None:
            raise HTTPException(status_code=404, detail="Memory not found")
        return JSONResponse(_dataclass_to_dict(func))

    @app.get("/memories/{memory_id}/timeline", summary="Get memory timeline")
    async def get_timeline(memory_id: str) -> JSONResponse:
        """Get the changelog timeline for a memory."""
        svc = _get_service(get_timeline)
        events = svc.store.get_timeline(memory_id)
        return JSONResponse(_dataclass_to_dict(events))

    @app.delete("/memories/{memory_id}", summary="Delete memory")
    async def delete_memory(memory_id: str) -> JSONResponse:
        """Soft-delete a memory."""
        svc = _get_service(delete_memory)
        svc.delete(memory_id)
        return JSONResponse({"status": "deleted", "id": memory_id})

    @app.post("/memories/{memory_id}/feedback", summary="Submit feedback")
    async def submit_feedback(memory_id: str, body: dict) -> JSONResponse:
        """Submit feedback for a memory field value.

        Request body::

            {
                "role": "trigger" | "action" | "condition" | "benefit",
                "index": 0,
                "verdict": "correct" | "wrong",
                "reason": "optional explanation"
            }
        """
        svc = _get_service(submit_feedback)
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
        owner: Optional[str] = Query(None),
        limit: int = Query(100, ge=1, le=1000),
    ) -> JSONResponse:
        """Retrieve pending feedback reviews."""
        svc = _get_service(pending_reviews)
        reviews = svc.get_pending_reviews(owner=owner, limit=limit)
        return JSONResponse(
            {
                "total": len(reviews),
                "reviews": _dataclass_to_dict(reviews),
            }
        )

    @app.post("/memories/{memory_id}/resolve", summary="Resolve a review")
    async def resolve_review(memory_id: str, body: dict) -> JSONResponse:
        """Apply a resolution to a pending review.

        Request body::

            {
                "field_role": "trigger",
                "action": "accept" | "reject" | "merge",
                "new_value": "optional replacement when action=merge"
            }
        """
        svc = _get_service(resolve_review)
        result = svc.apply_resolution(
            memory_id=memory_id,
            field_role=body["field_role"],
            action=body["action"],
            new_value=body.get("new_value"),
        )
        return JSONResponse(result)

    @app.get("/health", summary="Health check")
    async def health() -> JSONResponse:
        """Return service health status."""
        svc = _get_service(health)
        return JSONResponse(svc.health())

    @app.get("/stats", summary="Statistics")
    async def stats() -> JSONResponse:
        """Return storage and usage statistics."""
        svc = _get_service(stats)
        return JSONResponse(svc.stats())

    @app.post("/compact", summary="Trigger compaction")
    async def compact(body: Optional[dict] = None) -> JSONResponse:
        """Run the compaction pipeline.

        Request body (optional)::

            {"scope": "project" | "session" | "global"}
        """
        svc = _get_service(compact)
        scope = "project"
        if body:
            scope = body.get("scope", "project")
        result = svc.compact(scope=scope)
        return JSONResponse(_dataclass_to_dict(result))

    return app
