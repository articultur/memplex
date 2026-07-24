"""Portable agent memory runtime.

This module gives Codex, Claude Code, OpenClaw, Hermes, and similar agents a
shared production-to-consumption loop:

1. recall relevant memories before a prompt;
2. capture a completed turn in the background path;
3. optionally prefetch context for the next turn.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from memplex.models import QueryResult
from memplex.service import MemplexService


@dataclass(frozen=True)
class AgentProfile:
    """Installation and behavior contract for one agent family."""

    name: str
    display_name: str
    integration_modes: list[str]
    hook_events: list[str]
    tools: list[str]
    capabilities: Dict[str, bool]
    config: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RecalledContext:
    """Context returned to an adapter before a model turn."""

    agent: str
    context: str
    source: str
    query: str
    total: int
    tokens_used: int = 0


AGENT_PROFILES: Dict[str, AgentProfile] = {
    "codex": AgentProfile(
        name="codex",
        display_name="Codex",
        integration_modes=["mcp", "cli", "hooks"],
        hook_events=["session_start", "user_prompt_submit", "post_tool_use", "stop"],
        tools=[
            "memory_search",
            "memory_add",
            "memory_get",
            "memory_update",
            "memory_delete",
            "memory_turn_end",
        ],
        capabilities={
            "auto_capture": True,
            "auto_recall": True,
            "background_consolidation": True,
            "zero_latency_prefetch": False,
        },
        config={
            "mcp": {"command": "python -m memplex.adapters.mcp_server"},
            "hooks": {"user_prompt_submit": "memplex agent recall --agent codex"},
        },
    ),
    "claude-code": AgentProfile(
        name="claude-code",
        display_name="Claude Code",
        integration_modes=["plugin", "mcp", "lifecycle-hooks", "cli"],
        hook_events=[
            "SessionStart",
            "UserPromptSubmit",
            "PreToolUse",
            "PostToolUse",
            "PreCompact",
            "Stop",
        ],
        tools=[
            "memory_search",
            "memory_add",
            "memory_get",
            "memory_update",
            "memory_delete",
            "memory_feedback",
            "memory_pending_reviews",
            "memory_resolve",
            "memory_health",
        ],
        capabilities={
            "auto_capture": True,
            "auto_recall": True,
            "background_consolidation": True,
            "zero_latency_prefetch": False,
        },
        config={
            "plugin": "plugin/hooks/hooks.json",
            "mcp": {"command": "python -m memplex.adapters.mcp_server"},
        },
    ),
    "openclaw": AgentProfile(
        name="openclaw",
        display_name="OpenClaw",
        integration_modes=["plugin-slot", "cli", "mcp"],
        hook_events=["triage", "recall", "dream"],
        tools=[
            "memory_search",
            "memory_add",
            "memory_get",
            "memory_list",
            "memory_update",
            "memory_delete",
            "memory_turn_end",
            "memory_status",
        ],
        capabilities={
            "auto_capture": True,
            "auto_recall": True,
            "background_consolidation": True,
            "zero_latency_prefetch": False,
        },
        config={
            "plugins": {
                "slots": {"memory": "memplex"},
                "entries": {
                    "memplex": {
                        "enabled": True,
                        "config": {
                            "mode": "local",
                            "skills": {
                                "triage": {"enabled": True},
                                "recall": {
                                    "enabled": True,
                                    "tokenBudget": 1500,
                                    "rerank": True,
                                },
                                "dream": {"enabled": True},
                            },
                        },
                    }
                },
            }
        },
    ),
    "hermes": AgentProfile(
        name="hermes",
        display_name="Hermes Agent",
        integration_modes=["memory-provider", "cli", "mcp"],
        hook_events=["prefetch_before_response", "sync_after_response", "prefetch_next"],
        tools=["memplex_profile", "memplex_search", "memplex_conclude"],
        capabilities={
            "auto_capture": True,
            "auto_recall": True,
            "background_consolidation": True,
            "zero_latency_prefetch": True,
        },
        config={
            "memory": {"provider": "memplex"},
            "memplex": {
                "user_id": "${MEMPLEX_USER_ID:-hermes-user}",
                "agent_id": "hermes",
                "prefetch": True,
                "rerank": True,
            },
        },
    ),
}

_PREFETCH_CACHE: Dict[str, RecalledContext] = {}


def _normalise_agent(agent: str) -> str:
    key = (agent or "codex").strip().lower().replace("_", "-")
    aliases = {
        "claude": "claude-code",
        "claudecode": "claude-code",
        "claude_code": "claude-code",
        "open-claw": "openclaw",
    }
    return aliases.get(key, key)


def list_agent_profiles() -> Dict[str, Dict[str, Any]]:
    """Return all supported agent profiles as JSON-serialisable dicts."""

    return {name: asdict(profile) for name, profile in AGENT_PROFILES.items()}


def get_agent_manifest(agent: str) -> Dict[str, Any]:
    """Return install/runtime hints for an agent family."""

    key = _normalise_agent(agent)
    try:
        profile = AGENT_PROFILES[key]
    except KeyError as exc:
        supported = ", ".join(sorted(AGENT_PROFILES))
        raise ValueError(f"Unsupported agent {agent!r}. Supported: {supported}") from exc
    return asdict(profile)


class AgentMemoryRuntime:
    """Shared auto-recall / auto-capture / prefetch loop for agent adapters."""

    def __init__(
        self,
        service: Optional[MemplexService] = None,
        agent: str = "codex",
        user_id: Optional[str] = None,
        session_id: str = "default",
        project_path: Optional[str | Path] = None,
        top_k: int = 5,
        token_budget: int = 1500,
        auto_capture: bool = True,
        auto_recall: bool = True,
        prefetch: Optional[bool] = None,
    ) -> None:
        self.service = service or MemplexService()
        self.agent = _normalise_agent(agent)
        if self.agent not in AGENT_PROFILES:
            get_agent_manifest(self.agent)
        self.user_id = user_id or "default"
        self.session_id = session_id or "default"
        self.project_path = str(project_path or Path.cwd())
        self.top_k = top_k
        self.token_budget = token_budget
        self.auto_capture = auto_capture
        self.auto_recall = auto_recall
        capabilities = AGENT_PROFILES[self.agent].capabilities
        self.prefetch_enabled = (
            capabilities.get("zero_latency_prefetch", False) if prefetch is None else prefetch
        )
        self._prefetch_cache = _PREFETCH_CACHE

    def before_prompt(self, prompt: str) -> RecalledContext:
        """Return prompt-ready memory context for the next model call."""

        query = self._query_from_prompt(prompt)
        cached = self._prefetch_cache.pop(self._cache_key(query), None)
        if cached is not None:
            return cached
        if not self.auto_recall:
            return RecalledContext(
                agent=self.agent,
                context="",
                source="disabled",
                query=query,
                total=0,
            )
        return self._recall(query, source="live")

    def after_response(
        self,
        user_message: str,
        assistant_message: str,
        next_prompt_hint: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Capture a completed turn and prefetch next-turn context."""

        if self.auto_capture:
            self.capture_turn(user_message, assistant_message, metadata=metadata)
        if self.prefetch_enabled and next_prompt_hint:
            recalled = self._recall(self._query_from_prompt(next_prompt_hint), source="prefetch")
            self._prefetch_cache[self._cache_key(recalled.query)] = recalled

    def capture_turn(
        self,
        user_message: str,
        assistant_message: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Persist a turn as observation input without requiring manual writes."""

        payload = {
            "agent": self.agent,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "project_path": self.project_path,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "user": user_message.strip(),
            "assistant": assistant_message.strip(),
            "metadata": metadata or {},
        }
        body = (
            "Observation from agent conversation.\n"
            f"Agent: {payload['agent']}\n"
            f"User ID: {payload['user_id']}\n"
            f"Session ID: {payload['session_id']}\n"
            f"Project Path: {payload['project_path']}\n"
            f"Storage Namespace: {self._storage_namespace()}\n"
            f"User: {payload['user']}\n"
            f"Assistant: {payload['assistant']}\n"
            f"Metadata: {json.dumps(payload['metadata'], ensure_ascii=False, sort_keys=True)}"
        )
        result = self.service.write_text(body, source_type="observation")
        self._stamp_captured_memories(result.functions)

    def prefetch(self, prompt: str) -> RecalledContext:
        """Populate and return the cache entry for a likely next prompt."""

        query = self._query_from_prompt(prompt)
        recalled = self._recall(query, source="prefetch")
        self._prefetch_cache[self._cache_key(query)] = recalled
        return recalled

    def _recall(self, query: str, source: str) -> RecalledContext:
        result = self.service.query(
            text=query,
            top_k=max(self.top_k * 5, self.top_k + 20),
            max_tokens=self.token_budget,
            namespace_filter=self._namespace_metadata(),
        )
        result.results = [
            item for item in result.results if self._result_in_namespace(item.func_id)
        ][: self.top_k]
        return RecalledContext(
            agent=self.agent,
            context=self._format_context(result),
            source=source,
            query=query,
            total=len(result.results),
            tokens_used=result.tokens_used,
        )

    def _format_context(self, result: QueryResult) -> str:
        if not result.results:
            return ""
        wrapped = self.service.filter_and_wrap_for_context(result.results)
        if wrapped:
            return wrapped
        return "[MEMORY FILTERED | reason=indirect_injection]\nUnsafe recalled memory was omitted."

    @staticmethod
    def _query_from_prompt(prompt: str) -> str:
        return " ".join(str(prompt or "").strip().split())

    def _cache_key(self, query: str) -> str:
        raw = (
            f"{self._storage_namespace()}:{self.project_path}:"
            f"{self.agent}:{self.user_id}:{self.session_id}:{query}"
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    def _storage_namespace(self) -> str:
        return self.service.storage_namespace()

    def _namespace_metadata(self) -> Dict[str, str]:
        return {
            "memplex_agent": self.agent,
            "memplex_user_id": self.user_id,
            "memplex_session_id": self.session_id,
            "memplex_project_path": self.project_path,
            "memplex_storage_namespace": self._storage_namespace(),
        }

    def _stamp_captured_memories(self, functions: list) -> None:
        metadata = self._namespace_metadata()
        # Stamp owner/origin_session + namespace attributes through the
        # public service boundary so we never reach into store internals
        # (store.get / store._save). For backends that return live objects,
        # mutating the returned Function and then calling annotate_memories
        # persists the full record (including owner/origin_session) via the
        # backend's native save hook.
        ids: list[str] = []
        for func in functions:
            stored = self.service.get(func.id)
            if stored is None:
                continue
            stored.owner = self.user_id
            stored.origin_session = self.session_id
            ids.append(func.id)
        if ids:
            self.service.annotate_memories(ids, attributes=metadata)

    def _result_in_namespace(self, func_id: str) -> bool:
        func = self.service.get(func_id)
        if func is None:
            return False
        attrs = getattr(func, "attributes", {}) or {}
        if attrs.get("memplex_user_id") != self.user_id:
            return False
        if attrs.get("memplex_session_id") != self.session_id:
            return False
        if attrs.get("memplex_project_path") != self.project_path:
            return False
        if attrs.get("memplex_storage_namespace") != self._storage_namespace():
            return False
        return True
