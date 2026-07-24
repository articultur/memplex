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

import json
import logging
import sys
import traceback
from dataclasses import asdict
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


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


# ── Tool definitions ────────────────────────────────────────────────

_TOOL_DEFINITIONS = [
    {
        "name": "memory_search",
        "description": "Search Memplex knowledge graph. Returns index with IDs, names, relevance scores. ALWAYS use this before memory_get to filter results (10x token savings).",
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
            },
            "required": ["content"],
        },
    },
    {
        "name": "memory_get",
        "description": "Retrieve full details for a specific memory. Use AFTER memory_search to get details only for filtered IDs (~500-1000 tokens each).",
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
                    "description": "Filter by owner (optional)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Max results (default 100)",
                    "default": 100,
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
                "agent": {"type": "string", "default": "codex"},
                "user_id": {"type": "string", "default": "default"},
                "session_id": {"type": "string", "default": "default"},
                "project_path": {"type": "string"},
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
                "agent": {"type": "string", "default": "codex"},
                "prompt": {"type": "string", "description": "Current user prompt"},
                "user_id": {"type": "string", "default": "default"},
                "session_id": {"type": "string", "default": "default"},
                "project_path": {
                    "type": "string",
                    "description": "Project path used for memory isolation",
                },
                "top_k": {"type": "integer", "default": 5},
                "token_budget": {"type": "integer", "default": 1500},
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
                "agent": {"type": "string", "default": "codex"},
                "user_message": {"type": "string"},
                "assistant_message": {"type": "string"},
                "next_prompt_hint": {"type": "string"},
                "user_id": {"type": "string", "default": "default"},
                "session_id": {"type": "string", "default": "default"},
                "project_path": {
                    "type": "string",
                    "description": "Project path used for memory isolation",
                },
                "metadata": {"type": "object"},
            },
            "required": ["user_message", "assistant_message"],
        },
    },
]


# ── MCPServer ───────────────────────────────────────────────────────


class MCPServer:
    """MCP Server for Memplex, communicating over stdio JSON-RPC.

    Parameters
    ----------
    config:
        Optional :class:`MemplexConfig`.  When ``None``, loaded via
        :func:`load_config`.
    """

    def __init__(self, config=None) -> None:
        self._config = config
        self._service = None

    # ── Lifecycle ────────────────────────────────────────────────

    def _ensure_service(self):
        """Lazy-initialize the MemplexService."""
        if self._service is not None:
            return

        from memplex.config import load_config
        from memplex.service import MemplexService

        cfg = self._config or load_config()
        self._service = MemplexService(config=cfg)
        self._service.start()

    # ── JSON-RPC I/O ─────────────────────────────────────────────

    def _read_request(self) -> Optional[dict]:
        """Read a single JSON-RPC request from stdin."""
        line = sys.stdin.readline()
        if not line:
            return None
        line = line.strip()
        if not line:
            return None
        try:
            return json.loads(line)
        except json.JSONDecodeError as exc:
            logger.warning("Invalid JSON from stdin: %s", exc)
            return None

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
        return {
            "protocolVersion": "2024-11-05",
            "capabilities": {
                "tools": {},
            },
            "serverInfo": {
                "name": "memplex",
                "version": "3.2.7",
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
            raise ValueError(f"Unknown tool: {tool_name!r}")

        result = handler(self, arguments)
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(result, default=str, ensure_ascii=False, indent=2),
                }
            ],
        }

    def _handle_request(self, request: dict) -> Optional[dict]:
        """Route a JSON-RPC request to the correct handler."""
        method = request.get("method", "")
        params = request.get("params", {})
        req_id = request.get("id")

        # Notifications (no id) do not expect a response
        if req_id is None and method.endswith("/notification"):
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

        except Exception as exc:
            logger.error("Error handling %s: %s", method, exc)
            traceback.print_exc(file=sys.stderr)
            return self._make_error(-32603, str(exc), req_id)

    # ── Tool implementations ─────────────────────────────────────

    def _tool_memory_search(self, args: dict) -> dict:
        """Search memories."""
        result = self._service.query(
            text=args["query"],
            top_k=args.get("top_k", 10),
            explain=args.get("explain", False),
        )
        payload = {
            "total": len(result.results),
            "scope": result.scope.value if hasattr(result.scope, "value") else str(result.scope),
            "latency_ms": result.latency_ms,
            "results": [
                {
                    "id": r.func_id,
                    "name": r.name,
                    "relevance": round(r.relevance_score, 4),
                    "summary": r.summary,
                    "domain": r.domain,
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
        result = self._service.write_text(text=content, source_type=source_type)
        return {
            "functions_extracted": len(result.functions),
            "edges": len(result.graph.edges),
            "function_ids": [f.id for f in result.functions],
        }

    def _tool_memory_get(self, args: dict) -> dict:
        """Get a memory by ID."""
        func = self._service.get(args["memory_id"])
        if func is None:
            return {"error": "Memory not found", "memory_id": args["memory_id"]}
        return _dataclass_to_dict(func)

    def _tool_memory_update(self, args: dict) -> dict:
        """Update a memory field."""
        result = self._service.update_memory(
            memory_id=args["memory_id"],
            role=args["role"],
            new_value=args["new_value"],
        )
        return _dataclass_to_dict(result)

    def _tool_memory_delete(self, args: dict) -> dict:
        """Delete a memory."""
        self._service.delete(args["memory_id"])
        return {"status": "deleted", "id": args["memory_id"]}

    def _tool_memory_feedback(self, args: dict) -> dict:
        """Submit feedback."""
        self._service.submit_feedback(
            memory_id=args["memory_id"],
            field_role=args["role"],
            value_index=args["index"],
            verdict=args["verdict"],
            reason=args.get("reason"),
        )
        return {"status": "recorded"}

    def _tool_memory_pending_reviews(self, args: dict) -> dict:
        """List pending reviews."""
        reviews = self._service.get_pending_reviews(
            owner=args.get("owner"),
            limit=args.get("limit", 100),
        )
        return {
            "total": len(reviews),
            "reviews": _dataclass_to_dict(reviews),
        }

    def _tool_memory_resolve(self, args: dict) -> dict:
        """Resolve a pending review."""
        return self._service.apply_resolution(
            memory_id=args["memory_id"],
            field_role=args["field_role"],
            action=args["action"],
            new_value=args.get("new_value"),
        )

    def _tool_memory_health(self, args: dict) -> dict:
        """Health check."""
        self._ensure_service()
        return self._service.health()

    def _tool_memory_doctor(self, args: dict) -> dict:
        """Run productized readiness checks."""
        from memplex.product import run_doctor

        self._ensure_service()
        return run_doctor(
            self._service,
            self._service._config,
            agent=args.get("agent", "codex"),
            profile=args.get("profile"),
            smoke=args.get("smoke", False),
        )

    def _tool_memory_scope_explain(self, args: dict) -> dict:
        """Explain visibility scope metadata."""
        from memplex.product import scope_explain, scope_preview

        self._ensure_service()
        store_path = getattr(getattr(self._service, "store", None), "_path", None)
        storage_namespace = str(store_path) if store_path is not None else f"service:{id(self._service)}"
        explained = scope_explain(
            agent=args.get("agent", "codex"),
            user_id=args.get("user_id"),
            session_id=args.get("session_id", "default"),
            project_path=args.get("project_path"),
            storage_namespace=storage_namespace,
        )
        if args.get("preview", False):
            explained["preview"] = scope_preview(self._service, explained["namespace_filter"])
        return explained

    def _tool_memory_policy_show(self, args: dict) -> dict:
        """Show recall/capture policy."""
        from memplex.product import policy_show

        self._ensure_service()
        return policy_show(self._service._config, agent=args.get("agent", "codex"))

    def _tool_memory_agent_manifest(self, args: dict) -> dict:
        """Return portable agent integration manifest."""
        from memplex.adapters.agent_runtime import get_agent_manifest

        return get_agent_manifest(args.get("agent", "codex"))

    def _agent_runtime(self, args: dict):
        """Build an AgentMemoryRuntime bound to this MCP service."""
        from memplex.adapters.agent_runtime import AgentMemoryRuntime

        return AgentMemoryRuntime(
            service=self._service,
            agent=args.get("agent", "codex"),
            user_id=args.get("user_id"),
            session_id=args.get("session_id", "default"),
            project_path=args.get("project_path"),
            top_k=args.get("top_k", 5),
            token_budget=args.get("token_budget", 1500),
        )

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
    _tool_handlers: Dict[str, Any] = {
        "memory_search": _tool_memory_search,
        "memory_add": _tool_memory_add,
        "memory_get": _tool_memory_get,
        "memory_update": _tool_memory_update,
        "memory_delete": _tool_memory_delete,
        "memory_feedback": _tool_memory_feedback,
        "memory_pending_reviews": _tool_memory_pending_reviews,
        "memory_resolve": _tool_memory_resolve,
        "memory_health": _tool_memory_health,
        "memory_doctor": _tool_memory_doctor,
        "memory_scope_explain": _tool_memory_scope_explain,
        "memory_policy_show": _tool_memory_policy_show,
        "memory_agent_manifest": _tool_memory_agent_manifest,
        "memory_turn_begin": _tool_memory_turn_begin,
        "memory_turn_end": _tool_memory_turn_end,
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
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    MCPServer().run()
