"""Memplex MCP Server -- Model Context Protocol over stdio JSON-RPC.

Implements a lightweight MCP server that communicates via stdin/stdout
using JSON-RPC 2.0.  No external MCP SDK dependency -- pure Python.

Protocol::

    Request:  {"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "...", "arguments": {...}}, "id": 1}
    Response: {"jsonrpc": "2.0", "result": {...}, "id": 1}

Usage::

    from memplex.adapters.mcp_server import MCPServer

    server = MCPServer()
    server.run()   # reads from stdin, writes to stdout

Or as a module::

    python -m memplex.adapters.mcp_server
"""

from __future__ import annotations

import getpass
import inspect
import json
import logging
import os
import sys
import traceback
from typing import Any, ClassVar, Optional

from memplex.adapters._shared import (
    MAX_MODEL_COLLECTION_RESULTS,
    MAX_MODEL_SCAN_ITEMS,
    MAX_MODEL_SEARCH_RESULTS,
    MAX_MODEL_TOKEN_BUDGET,
)
from memplex.adapters._shared import (
    dataclass_to_dict as _dataclass_to_dict,
)
from memplex.auth import MemoryNotFoundError

logger = logging.getLogger(__name__)


def _estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token), same formula as the service
    budget truncation fallback (``len(summary) // 4 + 1``)."""
    return len(text) // 4 + 1


# ── Tool definitions ────────────────────────────────────────────────

_TOOL_DEFINITIONS = [
    {
        "name": "memory_search",
        "description": "Search Memplex knowledge graph. Returns an index with IDs, names, relevance scores, and an est_tokens annotation per result (~50-100 tokens each); the payload also reports tokens_used / max_tokens / truncated. ALWAYS use this before memory_get to filter results (10x token savings).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language search query",
                },
                "top_k": {
                    "type": "integer",
                    "description": "Max results (default 10, max 100)",
                    "default": 10,
                    "minimum": 1,
                    "maximum": MAX_MODEL_SEARCH_RESULTS,
                },
                "max_tokens": {
                    "type": "integer",
                    "description": "Result token budget (default 4000, max 32000)",
                    "default": 4000,
                    "minimum": 1,
                    "maximum": MAX_MODEL_TOKEN_BUDGET,
                },
                "explain": {
                    "type": "boolean",
                    "description": "Include retrieval-stage explanation and safety/filter trace.",
                    "default": False,
                },
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_add",
        "description": "Add a new memory from text content.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "Text content to store"},
                "source_type": {
                    "type": "string",
                    "description": "Source type: text | file | url (default: text)",
                    "default": "text",
                },
                "visibility": {
                    "type": "string",
                    "enum": ["session", "workspace", "user"],
                    "description": "Memory visibility (default: workspace)",
                    "default": "workspace",
                },
            },
            "required": ["content"],
        },
    },
    {
        "name": "memory_get",
        "description": "Retrieve full details for a specific memory (~500-1000 tokens; response includes an est_tokens field). Use AFTER memory_search to get details only for filtered IDs.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {
                    "type": "string",
                    "description": "Memory ID from search results",
                },
            },
            "required": ["memory_id"],
        },
    },
    {
        "name": "memory_update",
        "description": "Update a field value of an existing memory (self-editing).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "Memory ID"},
                "role": {
                    "type": "string",
                    "description": "Field role: trigger | condition | action | benefit",
                },
                "new_value": {"type": "string", "description": "New field value text"},
            },
            "required": ["memory_id", "role", "new_value"],
        },
    },
    {
        "name": "memory_promote",
        "description": "Promote a memory to a curated knowledge tier (personal/domain/team). Team tier makes it workspace-visible to all member agents.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "Memory ID to promote"},
                "tier": {
                    "type": "string",
                    "enum": ["personal", "domain", "team"],
                    "description": "Target knowledge tier",
                },
            },
            "required": ["memory_id", "tier"],
        },
    },
    {
        "name": "memory_share",
        "description": "Share a user-private memory with a named peer agent (read-only grant; the peer cannot promote or re-share).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "Memory ID to share"},
                "agent_id": {"type": "string", "description": "Target agent ID"},
            },
            "required": ["memory_id", "agent_id"],
        },
    },
    {
        "name": "memory_facts",
        "description": "List facts with optional point-in-time filtering (bi-temporal history). Without --as_of, returns currently-valid facts only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "as_of": {
                    "type": "string",
                    "description": "ISO datetime for point-in-time query (optional)",
                },
                "include_invalidated": {
                    "type": "boolean",
                    "description": "Include superseded facts (default false)",
                },
                "limit": {"type": "integer", "description": "Max results (default 50)"},
            },
        },
    },
    {
        "name": "memory_delete",
        "description": "Delete a memory by ID.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "Memory ID"},
            },
            "required": ["memory_id"],
        },
    },
    {
        "name": "memory_feedback",
        "description": "Submit feedback on a memory field value.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "Memory ID"},
                "role": {
                    "type": "string",
                    "description": "Field role: trigger | action | condition | benefit",
                },
                "index": {
                    "type": "integer",
                    "description": "Value index within the field",
                },
                "verdict": {
                    "type": "string",
                    "description": "Verdict: correct | wrong",
                },
                "reason": {"type": "string", "description": "Optional explanation"},
            },
            "required": ["memory_id", "role", "index", "verdict"],
        },
    },
    {
        "name": "memory_pending_reviews",
        "description": "List pending feedback reviews that need resolution.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "owner": {
                    "type": "string",
                    "description": "Deprecated compatibility input; the trusted MCP identity is always enforced.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 100, max 1000)",
                    "default": 100,
                    "minimum": 0,
                    "maximum": MAX_MODEL_COLLECTION_RESULTS,
                },
            },
        },
    },
    {
        "name": "memory_resolve",
        "description": "Apply a resolution to a pending review.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "memory_id": {"type": "string", "description": "Memory ID"},
                "field_role": {
                    "type": "string",
                    "description": "Field role under review",
                },
                "action": {
                    "type": "string",
                    "description": "Resolution action: accept | reject | merge",
                },
                "new_value": {
                    "type": "string",
                    "description": "Replacement value (required when action=merge)",
                },
            },
            "required": ["memory_id", "field_role", "action"],
        },
    },
    {
        "name": "memory_health",
        "description": "Check Memplex service health status.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "memory_observations",
        "description": "List captured observation events (agent turns, tool uses) with structured categories and a per-item est_tokens annotation. Filter by category (bugfix | decision | change | discovery | note) or by a substring query over the event/context text.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Optional substring filter over event/context text",
                },
                "category": {
                    "type": "string",
                    "description": "Filter by category: bugfix | decision | change | discovery | note",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 100, max 1000)",
                    "default": 100,
                    "minimum": 0,
                    "maximum": MAX_MODEL_COLLECTION_RESULTS,
                },
            },
        },
    },
    {
        "name": "memory_doctor",
        "description": "Run productized readiness checks for Memplex and an agent integration.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "default": "codex"},
                "profile": {
                    "type": "string",
                    "description": "Optional setup profile: local | privacy | max-recall | team",
                },
                "smoke": {
                    "type": "boolean",
                    "description": "Run a safe capture/recall smoke in the configured store.",
                    "default": False,
                },
            },
        },
    },
    {
        "name": "memory_scope_explain",
        "description": "Explain Memplex visibility scope metadata for an agent. This is not an ACL mutation tool.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "preview": {"type": "boolean", "default": False},
            },
        },
    },
    {
        "name": "memory_policy_show",
        "description": "Show recall/capture policy, token budgets, embedding mode, and safety boundaries.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {"type": "string", "default": "codex"},
            },
        },
    },
    {
        "name": "memory_agent_manifest",
        "description": "Return integration manifest for Codex, Claude Code, OpenClaw, Hermes, or another supported agent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "agent": {
                    "type": "string",
                    "description": "Agent id: codex | claude-code | openclaw | hermes",
                    "default": "codex",
                },
            },
        },
    },
    {
        "name": "memory_turn_begin",
        "description": "Auto-recall prompt-ready memories before an agent turn. Uses prefetched context when available.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Current user prompt"},
                "top_k": {
                    "type": "integer",
                    "default": 5,
                    "minimum": 1,
                    "maximum": MAX_MODEL_SEARCH_RESULTS,
                },
                "token_budget": {
                    "type": "integer",
                    "default": 1500,
                    "minimum": 1,
                    "maximum": MAX_MODEL_TOKEN_BUDGET,
                },
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "memory_turn_end",
        "description": "Auto-capture a completed user/assistant turn and optionally prefetch context for the next turn.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "user_message": {"type": "string"},
                "assistant_message": {"type": "string"},
                "next_prompt_hint": {"type": "string"},
                "metadata": {"type": "object"},
            },
            "required": ["user_message", "assistant_message"],
        },
    },
]


# ── MCPServer ───────────────────────────────────────────────────────


class _UnknownToolError(ValueError):
    """Raised when ``tools/call`` names a tool that is not registered."""


class MCPServer:
    """MCP Server for Memplex, communicating over stdio JSON-RPC.

    Parameters
    ----------
    config:
        Optional :class:`MemplexConfig`.  When ``None``, loaded via
        :func:`load_config`.
    """

    def __init__(self, config: Any = None) -> None:
        self._config = config
        self._service: Any = None

    # ── Lifecycle ────────────────────────────────────────────────

    def _ensure_service(self) -> None:
        """Lazy-initialize the MemplexService."""
        if self._service is not None:
            return

        from memplex.config import load_config
        from memplex.service import MemplexService

        cfg = self._config or load_config()
        self._service = MemplexService(config=cfg)
        self._service.start()

    # ── JSON-RPC I/O ─────────────────────────────────────────────

    def _read_request(self) -> dict | None:
        """Read a single JSON-RPC request from stdin.

        Returns ``None`` only on EOF. Blank lines are skipped, and a
        malformed line is answered with a JSON-RPC ``-32700`` (Parse
        error) response with a null id -- the server keeps reading
        instead of treating the bad line as EOF and dying.
        """
        while True:
            line = sys.stdin.readline()
            if not line:
                return None  # EOF
            line = line.strip()
            if not line:
                continue  # skip blank lines
            try:
                return json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning("Invalid JSON from stdin: %s", exc)
                self._write_response(self._make_error(-32700, f"Parse error: {exc}", None))

    def _write_response(self, response: dict) -> None:
        """Write a JSON-RPC response to stdout."""
        sys.stdout.write(json.dumps(response, default=str, ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def _make_result(self, result: Any, req_id: Any) -> dict:
        """Build a JSON-RPC success response."""
        return {"jsonrpc": "2.0", "result": result, "id": req_id}

    def _make_error(self, code: int, message: str, req_id: Any = None) -> dict:
        """Build a JSON-RPC error response."""
        return {
            "jsonrpc": "2.0",
            "error": {"code": code, "message": message},
            "id": req_id,
        }

    # ── Method dispatch ──────────────────────────────────────────

    def _handle_initialize(self, params: dict) -> dict:
        """Handle ``initialize`` request."""
        from memplex.adapters.agent_installer import _package_version

        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {},
            },
            "serverInfo": {
                "name": "memplex",
                "version": _package_version(),
            },
        }

    def _handle_tools_list(self, params: dict) -> dict:
        """Handle ``tools/list`` request."""
        return {"tools": _TOOL_DEFINITIONS}

    def _handle_tools_call(self, params: dict) -> dict:
        """Handle ``tools/call`` request."""
        tool_name = params.get("name", "")
        arguments = params.get("arguments", {})

        handler = self._tool_handlers.get(tool_name)
        if handler is None:
            raise _UnknownToolError(f"Unknown tool: {tool_name!r}")

        result = handler(self, arguments)
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, default=str, ensure_ascii=False, indent=2),
                }
            ],
        }

    def _handle_request(self, request: dict) -> dict | None:
        """Route a JSON-RPC request to the correct handler."""
        method = request.get("method", "")
        params = request.get("params", {})
        req_id = request.get("id")

        # Notifications (no id) do not expect a response. Real MCP
        # notification methods look like "notifications/initialized".
        if req_id is None and method.startswith("notifications/"):
            return None

        try:
            if method == "initialize":
                result = self._handle_initialize(params)
            elif method == "tools/list":
                result = self._handle_tools_list(params)
            elif method == "tools/call":
                self._ensure_service()
                result = self._handle_tools_call(params)
            elif method == "ping":
                result = {}
            else:
                return self._make_error(-32601, f"Method not found: {method}", req_id)

            return self._make_result(result, req_id)

        except _UnknownToolError as exc:
            # Unknown tool name -> Invalid params (JSON-RPC -32602).
            logger.error("Error handling %s: %s", method, exc)
            return self._make_error(-32602, str(exc), req_id)
        except Exception as exc:  # noqa: BLE001 - logged degradation path
            logger.error("Error handling %s: %s", method, exc)
            traceback.print_exc(file=sys.stderr)
            return self._make_error(-32603, str(exc), req_id)

    # ── Tool implementations ─────────────────────────────────────

    def _tool_memory_search(self, args: dict) -> dict:
        """Search memories."""
        result = self._agent_runtime(args).search_memories(
            args["query"],
            top_k=args.get("top_k", 10),
            max_tokens=args.get("max_tokens", 4000),
            explain=args.get("explain", False),
        )
        payload = {
            "total": len(result.results),
            "scope": result.scope.value if hasattr(result.scope, "value") else str(result.scope),
            "latency_ms": result.latency_ms,
            "tokens_used": result.tokens_used,
            "max_tokens": result.max_tokens,
            "truncated": result.truncated,
            "results": [
                {
                    "id": r.func_id,
                    "name": r.name,
                    "relevance": round(r.relevance_score, 4),
                    "summary": r.summary,
                    "domain": r.domain,
                    # Backfilled per-result by the service when max_tokens > 0;
                    # otherwise fall back to the same summary-length formula.
                    "est_tokens": r.token_estimate or _estimate_tokens(r.summary),
                }
                for r in result.results
            ],
        }
        if args.get("explain", False):
            payload["explanation"] = result.explanation
        return payload

    def _tool_memory_add(self, args: dict) -> dict:
        """Add a new memory."""
        content = args["content"]
        source_type = args.get("source_type", "text")
        result = self._agent_runtime(args).write_text(
            content,
            source_type=source_type,
            visibility=args.get("visibility", "workspace"),
        )
        return {
            "functions_extracted": len(result.functions),
            "edges": len(result.graph.edges),
            "function_ids": [f.id for f in result.functions],
        }

    def _tool_memory_get(self, args: dict) -> dict:
        """Get a memory by ID."""
        func = self._agent_runtime(args).get_accessible_memory(args["memory_id"])
        if func is None:
            return {"error": "Memory not found", "memory_id": args["memory_id"]}
        payload = _dataclass_to_dict(func)
        # Full-detail reads are the expensive layer (~500-1000 tokens);
        # annotate the cost so callers can budget progressively.
        payload["est_tokens"] = _estimate_tokens(
            json.dumps(payload, default=str, ensure_ascii=False)
        )
        return payload

    def _tool_memory_update(self, args: dict) -> dict:
        """Update a memory field."""
        runtime = self._agent_runtime(args)
        try:
            result = self._service.update_memory(
                memory_id=args["memory_id"],
                role=args["role"],
                new_value=args["new_value"],
                authorization=runtime.authorization_context,
            )
        except MemoryNotFoundError as exc:
            raise PermissionError("Memory not found or inaccessible") from exc
        payload = _dataclass_to_dict(result)
        if runtime.get_accessible_memory(args["memory_id"]) is None:
            payload["old_value"] = None
            payload["new_value"] = None
            payload["withheld_unsafe"] = True
        return payload

    def _tool_memory_promote(self, args: dict) -> dict:
        """Promote a memory to a curated knowledge tier."""
        runtime = self._agent_runtime(args)
        result = self._service.promote(
            args["memory_id"], args["tier"],
            authorization=runtime.authorization_context,
        )
        return result

    def _tool_memory_share(self, args: dict) -> dict:
        """Share a private memory with a named peer agent."""
        runtime = self._agent_runtime(args)
        result = self._service.share_with(
            args["memory_id"], args["agent_id"],
            authorization=runtime.authorization_context,
        )
        return result

    def _tool_memory_facts(self, args: dict) -> dict:
        """List facts with optional point-in-time filtering."""
        runtime = self._agent_runtime(args)
        facts = self._service.list_facts(
            as_of=args.get("as_of"),
            limit=int(args.get("limit", 50)),
            include_invalidated=bool(args.get("include_invalidated", False)),
            authorization=runtime.authorization_context,
        )
        return {
            "count": len(facts),
            "facts": [
                {
                    "id": f.id,
                    "subject": f.subject,
                    "predicate": f.predicate,
                    "object": f.object_,
                    "knowledge_tier": f.knowledge_tier,
                    "valid_from": f.valid_from,
                    "invalid_at": f.invalid_at,
                }
                for f in facts
            ],
        }

    def _tool_memory_delete(self, args: dict) -> dict:
        """Delete a memory."""
        runtime = self._agent_runtime(args)
        try:
            self._service.delete(
                args["memory_id"], authorization=runtime.authorization_context
            )
        except MemoryNotFoundError as exc:
            raise PermissionError("Memory not found or inaccessible") from exc
        return {"status": "deleted", "id": args["memory_id"]}

    def _tool_memory_feedback(self, args: dict) -> dict:
        """Submit feedback."""
        runtime = self._agent_runtime(args)
        try:
            self._service.submit_feedback(
                memory_id=args["memory_id"],
                field_role=args["role"],
                value_index=args["index"],
                verdict=args["verdict"],
                reason=args.get("reason"),
                authorization=runtime.authorization_context,
            )
        except MemoryNotFoundError as exc:
            raise PermissionError("Memory not found or inaccessible") from exc
        return {"status": "recorded"}

    def _tool_memory_pending_reviews(self, args: dict) -> dict:
        """List pending reviews."""
        runtime = self._agent_runtime(args)
        limit = min(MAX_MODEL_COLLECTION_RESULTS, max(0, int(args.get("limit", 100))))
        if limit == 0:
            return {"total": 0, "reviews": []}
        get_pending_reviews = self._service.get_pending_reviews
        if "authorization" in inspect.signature(get_pending_reviews).parameters:
            reviews = get_pending_reviews(
                limit=MAX_MODEL_SCAN_ITEMS,
                authorization=runtime.authorization_context,
            )
        else:
            # Narrow compatibility for test doubles and third-party service
            # facades predating the authorization keyword. The production
            # service path above is always authorization-bound.
            reviews = get_pending_reviews(limit=MAX_MODEL_SCAN_ITEMS)
        reviews = [
            review
            for review in reviews
            if runtime.get_accessible_memory(review.memory_id) is not None
        ][:limit]
        return {
            "total": len(reviews),
            "reviews": _dataclass_to_dict(reviews),
        }

    def _tool_memory_resolve(self, args: dict) -> dict:
        """Resolve a pending review."""
        runtime = self._agent_runtime(args)
        try:
            return self._service.apply_resolution(
                memory_id=args["memory_id"],
                field_role=args["field_role"],
                action=args["action"],
                new_value=args.get("new_value"),
                authorization=runtime.authorization_context,
            )
        except MemoryNotFoundError as exc:
            raise PermissionError("Memory not found or inaccessible") from exc

    def _tool_memory_health(self, args: dict) -> dict:
        """Health check."""
        self._ensure_service()
        return self._service.health()

    def _tool_memory_observations(self, args: dict) -> dict:
        """List captured observation events with token estimates."""
        self._ensure_service()
        runtime = self._agent_runtime(args)
        limit = min(MAX_MODEL_COLLECTION_RESULTS, max(0, int(args.get("limit", 100))))
        observations = self._list_observations_filtered(
            category=args.get("category"),
            query=str(args.get("query") or "").strip().lower(),
            limit=limit,
            runtime=runtime,
        )
        items = []
        for obs in observations:
            summary = obs.context or obs.event or ""
            items.append(
                {
                    "id": obs.id,
                    "category": obs.category,
                    "event": obs.event,
                    "actor": obs.actor,
                    "observed_at": obs.observed_at,
                    # Same ~4 chars/token estimate as memory_search results.
                    "est_tokens": _estimate_tokens(summary),
                    "summary": summary[:200],
                }
            )
        return {"total": len(items), "observations": items}

    def _list_observations_filtered(self, *, category: Any, query: Any, limit: Any, runtime: Any) -> list:
        """List observations with the substring filter applied BEFORE *limit*.

        The store applies its own limit first, so when a *query* is present
        we paginate through the store in pages, collect matches, and only
        then truncate -- otherwise matches beyond the first ``limit`` rows
        would be silently dropped.
        """
        # PostgreSQL production stores reject every unscoped operation.  Bind
        # this bounded scan to the same trusted runtime context used by the
        # rest of the MCP tool surface; the final runtime check below retains
        # the host visibility contract for non-PG backends as well.
        scoped_store = self._service._store_for(runtime.authorization_context)
        store_list = getattr(scoped_store, "list_observations", None)
        if not callable(store_list):
            return []
        if limit <= 0:
            return []
        matched: list = []
        offset = 0
        scanned = 0
        page_size = min(MAX_MODEL_SCAN_ITEMS, max(limit, 100))
        while len(matched) < limit and scanned < MAX_MODEL_SCAN_ITEMS:
            request_size = min(page_size, MAX_MODEL_SCAN_ITEMS - scanned)
            batch = list(
                store_list(
                    offset=offset,
                    limit=request_size,
                    category=category,
                    owner=runtime.user_id,
                )
            )[:request_size]
            if not batch:
                break
            scanned += len(batch)
            for obs in batch:
                if not runtime.can_access_node(obs):
                    continue
                # Observation payloads are model-visible here just like
                # query and memory_get results.  Run the service-owned typed
                # injection decision before constructing any serialized text.
                if not self._service.is_safe_for_model(obs):
                    continue
                summary = obs.context or obs.event or ""
                if not query or query in f"{obs.event}\n{summary}".lower():
                    matched.append(obs)
                    if len(matched) >= limit:
                        break
            if len(batch) < request_size:
                break
            offset += len(batch)
        return matched

    def _tool_memory_doctor(self, args: dict) -> dict:
        """Run productized readiness checks."""
        from memplex.product import run_doctor

        self._ensure_service()
        return run_doctor(
            self._service,
            agent=args.get("agent", "codex"),
            profile=args.get("profile"),
            smoke=args.get("smoke", False),
        )

    def _tool_memory_scope_explain(self, args: dict) -> dict:
        """Explain visibility scope metadata."""
        from memplex.product import scope_explain, scope_preview

        self._ensure_service()
        runtime = self._agent_runtime(args)
        explained = scope_explain(
            agent=runtime.agent,
            user_id=runtime.user_id,
            session_id=runtime.session_id,
            project_path=runtime.project_path,
            storage_namespace=self._service.storage_namespace(),
        )
        if args.get("preview", False):
            preview = scope_preview(
                self._service,
                runtime.read_namespace_filters(),
                scan_limit=MAX_MODEL_SCAN_ITEMS,
            )
            explained["preview"] = preview
        return explained

    def _tool_memory_policy_show(self, args: dict) -> dict:
        """Show recall/capture policy."""
        self._ensure_service()
        return self._service.policy(agent=args.get("agent", "codex"))

    def _tool_memory_agent_manifest(self, args: dict) -> dict:
        """Return portable agent integration manifest."""
        from memplex.adapters.agent_runtime import get_agent_manifest

        return get_agent_manifest(args.get("agent", "codex"))

    def _agent_runtime(self, args: dict) -> Any:
        """Build an AgentMemoryRuntime bound to this MCP service."""
        from memplex.adapters.agent_runtime import AgentMemoryRuntime

        self._ensure_service()
        return AgentMemoryRuntime(
            service=self._service,
            agent=self._trusted_identity("MEMPLEX_AGENT_ID", "codex"),
            user_id=self._trusted_identity("MEMPLEX_USER_ID", getpass.getuser()),
            session_id=self._trusted_identity(
                "MEMPLEX_SESSION_ID",
                f"mcp-{os.getpid()}",
            ),
            project_path=self._trusted_identity("MEMPLEX_PROJECT_ROOT", os.getcwd()),
            top_k=args.get("top_k", 5),
            token_budget=args.get("token_budget", 1500),
        )

    @staticmethod
    def _trusted_identity(env_name: str, default: Any) -> Any:
        """Resolve identity from process state, never model-controlled arguments."""

        installed_value = os.environ.get(env_name)
        if installed_value:
            return installed_value
        return default

    def _require_memory_access(self, memory_id: str, args: dict) -> Any:
        runtime = self._agent_runtime(args)
        memory = runtime.get_accessible_memory(memory_id)
        if memory is None:
            raise PermissionError("Memory not found or inaccessible")
        return memory

    def _tool_memory_turn_begin(self, args: dict) -> dict:
        """Recall memories before an agent turn."""
        runtime = self._agent_runtime(args)
        recalled = runtime.before_prompt(args["prompt"])
        return _dataclass_to_dict(recalled)

    def _tool_memory_turn_end(self, args: dict) -> dict:
        """Capture a completed turn."""
        runtime = self._agent_runtime(args)
        runtime.after_response(
            user_message=args["user_message"],
            assistant_message=args["assistant_message"],
            next_prompt_hint=args.get("next_prompt_hint"),
            metadata=args.get("metadata"),
        )
        return {"status": "captured", "agent": runtime.agent}

    # Map tool names to handler methods
    _tool_handlers: ClassVar[dict[str, Any]] = {        "memory_search": _tool_memory_search,
        "memory_add": _tool_memory_add,
        "memory_get": _tool_memory_get,
        "memory_update": _tool_memory_update,
        "memory_delete": _tool_memory_delete,
        "memory_feedback": _tool_memory_feedback,
        "memory_pending_reviews": _tool_memory_pending_reviews,
        "memory_resolve": _tool_memory_resolve,
        "memory_health": _tool_memory_health,
        "memory_observations": _tool_memory_observations,
        "memory_doctor": _tool_memory_doctor,
        "memory_scope_explain": _tool_memory_scope_explain,
        "memory_policy_show": _tool_memory_policy_show,
        "memory_agent_manifest": _tool_memory_agent_manifest,
        "memory_turn_begin": _tool_memory_turn_begin,
        "memory_turn_end": _tool_memory_turn_end,
        "memory_promote": _tool_memory_promote,
        "memory_share": _tool_memory_share,
        "memory_facts": _tool_memory_facts,
    }

    # ── Main loop ────────────────────────────────────────────────

    def run(self) -> None:
        """Run the MCP server, reading requests from stdin.

        Blocks until stdin is closed (EOF) or a fatal error occurs.
        """
        logger.info("Memplex MCP Server starting (stdio JSON-RPC)")

        try:
            while True:
                request = self._read_request()
                if request is None:
                    break  # EOF

                response = self._handle_request(request)
                if response is not None:
                    self._write_response(response)
        except KeyboardInterrupt:
            pass
        finally:
            if self._service is not None:
                self._service.stop()
            logger.info("Memplex MCP Server stopped")


# ── CLI entry point ─────────────────────────────────────────────────

if __name__ == "__main__":
    from memplex.config import load_config
    from memplex.logging_config import configure_logging

    config = load_config()
    configure_logging(level=config.logging.level)
    MCPServer(config=config).run()
