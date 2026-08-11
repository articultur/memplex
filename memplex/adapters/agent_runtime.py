"""Portable agent memory runtime.

This module gives Codex, Claude Code, OpenClaw, Hermes, and similar agents a
shared production-to-consumption loop:

1. recall relevant memories before a prompt;
2. capture a completed turn in the background path;
3. optionally prefetch context for the next turn.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from memplex.adapters._shared import (
    MAX_MODEL_SEARCH_CANDIDATES,
    MAX_MODEL_SEARCH_RESULTS,
    MAX_MODEL_TOKEN_BUDGET,
)
from memplex.auth import (
    AuthorizationContext,
    Principal,
    resolve_environment_authorization,
)
from memplex.core.hooks.policy import hash_event_payload
from memplex.intent import classify_observation
from memplex.models import Observation, QueryResult
from memplex.service import MemplexService

logger = logging.getLogger(__name__)

MEMORY_VISIBILITIES = frozenset({"session", "workspace", "user"})
AGENT_MANIFEST_SCHEMA_VERSION = 1
DEFAULT_MEMORY_VISIBILITY = "workspace"


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
    est_tokens: int = 0


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
            "memory_observations",
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
            "memory_observations",
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
        integration_modes=["native-plugin", "plugin-slot", "cli", "mcp"],
        hook_events=["before_prompt_build", "agent_end", "session_end"],
        tools=["memory_recall", "memory_store"],
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
                        "hooks": {"allowConversationAccess": True},
                        "config": {
                            "autoRecall": True,
                            "autoCapture": True,
                            "tokenBudget": 1500,
                            "visibility": "workspace",
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
        hook_events=[
            "prefetch",
            "queue_prefetch",
            "sync_turn",
            "on_pre_compress",
            "on_session_end",
            "on_session_switch",
        ],
        tools=["memplex_search", "memplex_conclude"],
        capabilities={
            "auto_capture": True,
            "auto_recall": True,
            "background_consolidation": True,
            "zero_latency_prefetch": True,
        },
        config={
            "memory": {"provider": "memplex"},
            "host_contract": {
                "kind": "bridge-backed-memory-provider",
                "upstream_repository": "https://github.com/NousResearch/hermes-agent",
                "upstream_version": "v2026.8.3",
                "upstream_tag_commit": "7de39e700d2c329e15d32eb0b96e2f7cdd9fbdb2",
                "source_revision": "3c27eb6234bf91b8ceee9e9071591b31e9b148cb",
                "source_path": "agent/memory_provider.py",
                "source_url": (
                    "https://github.com/NousResearch/hermes-agent/blob/"
                    "3c27eb6234bf91b8ceee9e9071591b31e9b148cb/agent/memory_provider.py"
                ),
                "source_sha256": (
                    "678c9150852f2018182e08622ae25b495"
                    "360fd5099747f823c35e00cce08d8dd"
                ),
            },
            "memplex": {
                "user_id": "${MEMPLEX_USER_ID}",
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

    return {name: _manifest_from_profile(profile) for name, profile in AGENT_PROFILES.items()}


def _manifest_from_profile(profile: AgentProfile) -> Dict[str, Any]:
    manifest = asdict(profile)
    manifest.update(
        {
            "schema_version": AGENT_MANIFEST_SCHEMA_VERSION,
            "memory_contract": {
                "default_visibility": DEFAULT_MEMORY_VISIBILITY,
                "supported_visibilities": sorted(MEMORY_VISIBILITIES),
                "workspace_identity": "canonical_project_path",
                "shared_store": True,
                "cross_host_recall": True,
                "provenance_fields": [
                    "memplex_source_agent",
                    "memplex_source_session_id",
                ],
            },
        }
    )
    return manifest


def get_agent_manifest(agent: str) -> Dict[str, Any]:
    """Return install/runtime hints for an agent family."""

    key = _normalise_agent(agent)
    try:
        profile = AGENT_PROFILES[key]
    except KeyError as exc:
        supported = ", ".join(sorted(AGENT_PROFILES))
        raise ValueError(f"Unsupported agent {agent!r}. Supported: {supported}") from exc
    return _manifest_from_profile(profile)


def describe_memory_scope(
    *,
    agent: str,
    user_id: Optional[str],
    session_id: str,
    project_path: Optional[str | Path],
    storage_namespace: str,
    visibility: str = DEFAULT_MEMORY_VISIBILITY,
    workspace_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Describe the exact namespace contract used by agent runtimes.

    The returned read filters are OR branches.  The final two branches are
    compatibility reads for memories captured before explicit visibility and
    workspace provenance were introduced.
    """

    selected_agent = _normalise_agent(agent)
    get_agent_manifest(selected_agent)
    selected_visibility = str(visibility or DEFAULT_MEMORY_VISIBILITY).lower()
    if selected_visibility not in MEMORY_VISIBILITIES:
        supported = ", ".join(sorted(MEMORY_VISIBILITIES))
        raise ValueError(
            f"Unsupported memory visibility {selected_visibility!r}. Supported: {supported}"
        )
    user = user_id or "default"
    session = session_id or "default"
    if workspace_id is None:
        project = str(Path(project_path or Path.cwd()).expanduser().resolve(strict=False))
        workspace = project
    else:
        project = str(project_path or workspace_id)
        workspace = str(workspace_id)
    namespace = str(storage_namespace)
    # ``memplex_storage_namespace`` identifies the physical local cache, not
    # the authenticated principal.  Explicit visibility records are allowed
    # to cross cache boundaries through remote sync; tenant / subject /
    # workspace authorization is enforced by the service.  Keep the storage
    # namespace only on the legacy branch, whose older records have no
    # principal-scoped visibility metadata to authorize against.
    common = {"memplex_user_id": user}
    read_filters: list[Dict[str, Optional[str]]] = [
        {
            **common,
            "memplex_visibility": "session",
            "memplex_workspace_id": workspace,
            "memplex_source_agent": selected_agent,
            "memplex_source_session_id": session,
        },
        {
            **common,
            "memplex_visibility": "workspace",
            "memplex_workspace_id": workspace,
        },
        {
            **common,
            "memplex_visibility": "user",
        },
        # Remote sync re-binds the payload to the server-authenticated
        # principal and is allowed to retain only this canonical projection.
        # The service ACL has already enforced tenant + base visibility before
        # this metadata filter runs; the subject check preserves the runtime's
        # per-user workspace semantics without coupling it to one local cache.
        {"memplex_subject_id": user},
        {
            **common,
            "memplex_storage_namespace": namespace,
            "memplex_visibility": None,
            "memplex_agent": selected_agent,
            "memplex_session_id": session,
            "memplex_project_path": project,
        },
        {
            "memplex_user_id": user,
            "memplex_session_id": session,
            "memplex_visibility": None,
            "memplex_legacy_typed": "true",
        },
    ]
    write_namespace = {
        "memplex_agent": selected_agent,
        "memplex_user_id": user,
        "memplex_session_id": session,
        "memplex_project_path": project,
        "memplex_storage_namespace": namespace,
        "memplex_visibility": selected_visibility,
        "memplex_workspace_id": workspace,
        "memplex_source_agent": selected_agent,
        "memplex_source_session_id": session,
    }
    return {
        "schema_version": AGENT_MANIFEST_SCHEMA_VERSION,
        "agent": selected_agent,
        "identity": {
            "user_id": user,
            "session_id": session,
            "project_path": project,
            "workspace_id": workspace,
            "storage_namespace": namespace,
        },
        "visibility": {
            "default": DEFAULT_MEMORY_VISIBILITY,
            "write": selected_visibility,
            "supported": sorted(MEMORY_VISIBILITIES),
            "read_order": [
                "session",
                "workspace",
                "user",
                "canonical-principal",
                "legacy",
            ],
        },
        "write_namespace": write_namespace,
        "read_namespace_filters": read_filters,
        "provenance": {
            "source_agent": selected_agent,
            "source_session_id": session,
        },
    }


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
        authorization: Optional[AuthorizationContext] = None,
    ) -> None:
        if service is None:
            # Self-created services must be started so the background
            # worker (consolidation, dead-letter retry) actually runs;
            # caller-provided services are owned -- and started -- by
            # the caller.
            service = MemplexService()
            service.start()
        self.service = service
        if authorization is not None and not isinstance(authorization, AuthorizationContext):
            raise TypeError("authorization must be an AuthorizationContext")
        requested_agent = _normalise_agent(agent)
        profile = str(
            getattr(
                getattr(getattr(service, "_config", None), "deployment", None),
                "profile",
                "development",
            )
        ).strip().lower()
        sync_config = getattr(getattr(service, "store", None), "_config", None)
        remote_active = bool(getattr(sync_config, "active", False))

        # ``local_development_context`` is the service's compatibility
        # principal, not a host-authenticated adapter identity.  In a pure
        # local process it must not erase the CLI/hook user, project, session,
        # or selected host.  Production and remote-sync runtimes never accept
        # that compatibility context as a fallback.
        if authorization is not None:
            principal = authorization.principal
            compatibility_context = (
                principal.tenant_id == "local"
                and principal.subject_id == "local-development"
                and "local-development" in principal.roles
                and authorization.workspace_id == "local-development"
                and authorization.provenance.get("trust_boundary") == "local-development"
            )
            if compatibility_context:
                if profile == "production" or remote_active:
                    raise PermissionError(
                        "local-development authorization is not valid for production or remote sync"
                    )
                authorization = None

        # A trusted host-bound context wins over the adapter argument.  A
        # transport-generic context (for example CLI) carries the principal
        # but is projected onto the selected host so session provenance stays
        # meaningful.
        context_agent = ""
        if authorization is not None and authorization.agent_id:
            candidate = _normalise_agent(authorization.agent_id)
            if candidate in AGENT_PROFILES:
                context_agent = candidate
        self.agent = context_agent or requested_agent
        if self.agent not in AGENT_PROFILES:
            get_agent_manifest(self.agent)
        requested_user = str(user_id or "default").strip() or "default"
        requested_session = str(session_id or "default").strip() or "default"
        requested_project = str(
            Path(project_path or Path.cwd()).expanduser().resolve(strict=False)
        )
        if authorization is None:
            authorization = resolve_environment_authorization(
                agent_id=self.agent,
                session_id=requested_session,
                provenance={"transport": "agent-runtime"},
                require_registry=profile == "production" or remote_active,
            )
        if authorization is not None:
            if authorization.agent_id != self.agent:
                authorization = AuthorizationContext(
                    principal=authorization.principal,
                    workspace_id=authorization.workspace_id,
                    agent_id=self.agent,
                    session_id=authorization.session_id,
                    request_id=authorization.request_id,
                    provenance=authorization.provenance,
                )
            self.authorization_context = authorization
            self.user_id = authorization.principal.subject_id
            # A trusted context with no session deliberately does not fall
            # back to a caller-supplied session identifier.
            self.session_id = authorization.session_id or "default"
            self.project_path = authorization.workspace_id
        else:
            self.user_id = requested_user
            self.session_id = requested_session
            self.project_path = requested_project
            self.authorization_context = self._local_process_authorization()
        self.workspace_id = self.project_path
        self.top_k = top_k
        self.token_budget = token_budget
        self.auto_capture = auto_capture
        self.auto_recall = auto_recall
        capabilities = AGENT_PROFILES[self.agent].capabilities
        self.prefetch_enabled = (
            capabilities.get("zero_latency_prefetch", False) if prefetch is None else prefetch
        )
        self._prefetch_cache = _PREFETCH_CACHE
        # Deduplication key of the most recent captured turn; consecutive
        # identical captures (e.g. PostToolUse + Stop hooks both firing for
        # the same turn) are dropped. Shared policy: core.hooks.policy.
        self._last_capture_key: Optional[str] = None

    def _local_process_authorization(self) -> AuthorizationContext:
        """Project local adapter parameters into an explicit trust boundary.

        This is intentionally not an operating-system adversary proof.  It
        makes local CLI/MCP adapter identity auditable while keeping a user's
        tenant stable across agent hosts on the same machine.
        """

        tenant_digest = hashlib.sha256(self.user_id.encode("utf-8")).hexdigest()[:24]
        return AuthorizationContext(
            principal=Principal(
                tenant_id=f"local-process-{tenant_digest}",
                subject_id=self.user_id,
                roles=frozenset({"local-process"}),
                authentication_id="local-process",
            ),
            workspace_id=self.project_path,
            agent_id=self.agent,
            session_id=self.session_id,
            provenance={
                "trust_boundary": "local-process",
                "identity_source": "adapter-parameters",
                "security_note": "not-os-adversary-safe",
            },
        )

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
        visibility = str(payload["metadata"].get("memplex_visibility") or "workspace").lower()
        self._validate_visibility(visibility)
        capture_key = hash_event_payload(
            {
                "user": payload["user"],
                "assistant": payload["assistant"],
                "metadata": payload["metadata"],
            }
        )
        if capture_key == self._last_capture_key:
            logger.debug("capture_turn: consecutive identical turn dropped")
            return
        self._last_capture_key = capture_key
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
        self.write_text(body, source_type="observation", visibility=visibility)
        self._capture_observation(payload)

    def write_text(
        self,
        text: str,
        *,
        source_type: str = "text",
        visibility: str = DEFAULT_MEMORY_VISIBILITY,
    ):
        """Write text through the runtime's identity and visibility boundary."""

        selected_visibility = str(visibility or DEFAULT_MEMORY_VISIBILITY).lower()
        self._validate_visibility(selected_visibility)
        result = self.service.write_text(
            text=text,
            source_type=source_type,
            visibility=selected_visibility,
            authorization=self.authorization_context,
        )
        self._stamp_captured_memories(
            result.functions + result.facts + result.preferences,
            visibility=selected_visibility,
        )
        return result

    def _capture_observation(self, payload: Dict[str, Any]) -> None:
        """Persist one structured Observation node for the captured turn.

        Complements the extraction pipeline (which never emits Observation
        nodes) with a claude-mem-style event record: a compressed context
        plus a structured ``category`` (see ``OBSERVATION_CATEGORIES``).

        Best-effort: any failure (store without ``add_observation``,
        compression error, classification hiccup) is debug-logged and
        never interrupts the ``after_response`` hook contract.
        """
        try:
            metadata = payload.get("metadata") or {}
            visibility = str(metadata.get("memplex_visibility") or "workspace").lower()
            tool_name = str(metadata.get("tool_name") or "")
            category = classify_observation(
                f"{payload['user']}\n{payload['assistant']}", tool_name=tool_name
            )
            context = self._compress_observation_context(
                f"User: {payload['user']}\nAssistant: {payload['assistant']}"
            )
            observation = Observation(
                id=f"obs_{uuid.uuid4().hex[:12]}",
                name=f"{self.agent} turn",
                event="agent_turn",
                context=context,
                observed_at=payload["observed_at"],
                actor=self.agent,
                category=category,
                owner=self.user_id,
                origin_session=self.session_id,
                namespace=self._namespace_metadata(visibility=visibility),
            )
            self.service.add_observation(
                observation,
                visibility=visibility,
                authorization=self.authorization_context,
            )
        except Exception as exc:
            logger.warning("observation capture skipped: %s", exc)

    def _compress_observation_context(self, content: str, max_length: int = 500) -> str:
        """Compress long observation text for storage; never raises.

        Uses the service's LLM enhancer when available, with the same
        event-loop juggling as ``MemplexService._detect_scope`` (thread
        pool inside a running loop, plain ``asyncio.run`` otherwise).
        Without an enhancer -- or when it fails -- falls back to the
        enhancer's rule-based head/tail truncation so capture never
        blocks on the LLM path.
        """
        if len(content) <= max_length:
            return content
        from memplex.llm.enhancer import LLMEnhancer

        llm = getattr(self.service, "_llm", None)
        if llm is None:
            return LLMEnhancer._rule_truncate(content, max_length)
        try:
            coro = llm.compress_observation(content, max_length=max_length)
            try:
                asyncio.get_running_loop()
                # Inside an existing event loop -- use a thread to avoid
                # nested loop issues.
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                    return pool.submit(asyncio.run, coro).result(timeout=5.0)
            except RuntimeError:
                # No running loop (CLI / sync hook path)
                return asyncio.run(coro)
        except Exception as exc:
            logger.warning("observation compression failed, using rule truncation: %s", exc)
            return LLMEnhancer._rule_truncate(content, max_length)

    def prefetch(self, prompt: str) -> RecalledContext:
        """Populate and return the cache entry for a likely next prompt."""

        query = self._query_from_prompt(prompt)
        recalled = self._recall(query, source="prefetch")
        self._prefetch_cache[self._cache_key(query)] = recalled
        return recalled

    def _recall(self, query: str, source: str) -> RecalledContext:
        result = self.search_memories(
            query,
            top_k=self.top_k,
            max_tokens=self.token_budget,
        )
        context = self._format_context(result)
        return RecalledContext(
            agent=self.agent,
            context=context,
            source=source,
            query=query,
            total=len(result.results),
            tokens_used=result.tokens_used,
            # Same ~4 chars/token estimate as the service/MCP token
            # exposure, applied to the injected context string.
            est_tokens=len(context) // 4 + 1 if context else 0,
        )

    def search_memories(
        self,
        query: str,
        *,
        top_k: Optional[int] = None,
        max_tokens: Optional[int] = None,
        explain: bool = False,
    ) -> QueryResult:
        """Search only memories visible to this runtime identity."""

        requested_top_k = int(self.top_k if top_k is None else top_k)
        requested_max_tokens = int(self.token_budget if max_tokens is None else max_tokens)
        selected_top_k = min(MAX_MODEL_SEARCH_RESULTS, max(1, requested_top_k))
        # ``MemplexService.query(max_tokens=0)`` means unlimited.  That is a
        # useful internal API, but an agent-controlled runtime must never be
        # able to opt out of its token budget.
        selected_max_tokens = min(MAX_MODEL_TOKEN_BUDGET, max(1, requested_max_tokens))
        query_top_k = min(
            MAX_MODEL_SEARCH_CANDIDATES,
            max(selected_top_k * 5, selected_top_k + 20),
        )
        result = self.service.query(
            text=query,
            top_k=query_top_k,
            max_tokens=selected_max_tokens,
            namespace_filter=self._read_namespace_filters(),
            explain=explain,
            authorization=self.authorization_context,
        )
        visible = [item for item in result.results if self._result_in_namespace(item.func_id)]
        public_top_k_truncated = len(visible) > selected_top_k
        result.results = visible[:selected_top_k]
        result.tokens_used = sum(
            max(item.token_estimate, len(item.summary) // 4 + 1) for item in result.results
        )
        result.truncated = bool(result.truncated or public_top_k_truncated)
        self._redact_explanation_to_visible_results(result, selected_top_k=selected_top_k)
        return result

    @staticmethod
    def _redact_explanation_to_visible_results(
        result: QueryResult,
        *,
        selected_top_k: int,
    ) -> None:
        """Align a public trace with the runtime-authorized result set.

        ``MemplexService`` intentionally has no host identity and therefore
        builds its trace before this runtime performs the final authorization
        and legacy-namespace migration check. Rebuild the record projection
        here so a denied record can never survive only in ``explanation``.
        Aggregate retrieval counts remain diagnostic and contain no record
        identity or content.
        """

        explanation = result.explanation
        if not isinstance(explanation, dict):
            return
        explanation["results"] = [
            {
                "id": item.func_id,
                "name": item.name,
                "score": item.relevance_score,
                "domain": item.domain,
                "token_estimate": item.token_estimate,
                "source_type": getattr(item.source_type, "value", str(item.source_type)),
            }
            for item in result.results
        ]
        budget = explanation.get("budget")
        if isinstance(budget, dict):
            budget["tokens_used"] = result.tokens_used
            budget["truncated"] = result.truncated
        selection = explanation.get("selection")
        if isinstance(selection, dict):
            selection["public_top_k_limit"] = selected_top_k
            selection["after_runtime_authorization"] = len(result.results)
            selection["after_token_budget"] = len(result.results)
        boundaries = explanation.get("boundaries")
        if isinstance(boundaries, dict):
            boundaries["runtime_authorization"] = (
                "Record metadata is projected only after host identity authorization."
            )

    def _format_context(self, result: QueryResult) -> str:
        if not result.results:
            return ""
        wrapped = self.service.filter_and_wrap_for_context(
            result.results,
            authorization=self.authorization_context,
        )
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

    def _namespace_metadata(self, *, visibility: str = "workspace") -> Dict[str, str]:
        return describe_memory_scope(
            agent=self.agent,
            user_id=self.user_id,
            session_id=self.session_id,
            project_path=self.project_path,
            storage_namespace=self._storage_namespace(),
            visibility=visibility,
            workspace_id=self.workspace_id,
        )["write_namespace"]

    @staticmethod
    def _validate_visibility(visibility: str) -> None:
        if visibility not in MEMORY_VISIBILITIES:
            supported = ", ".join(sorted(MEMORY_VISIBILITIES))
            raise ValueError(
                f"Unsupported memory visibility {visibility!r}. Supported: {supported}"
            )

    def _read_namespace_filters(self) -> list[Dict[str, Optional[str]]]:
        """Return OR-ed visibility boundaries for the current runtime."""

        return describe_memory_scope(
            agent=self.agent,
            user_id=self.user_id,
            session_id=self.session_id,
            project_path=self.project_path,
            storage_namespace=self._storage_namespace(),
            workspace_id=self.workspace_id,
        )["read_namespace_filters"]

    def read_namespace_filters(self) -> list[Dict[str, Optional[str]]]:
        """Expose a copy of the runtime's effective read boundaries."""

        return [dict(branch) for branch in self._read_namespace_filters()]

    def _stamp_captured_memories(self, nodes: list, *, visibility: str) -> None:
        metadata = self._namespace_metadata(visibility=visibility)
        # Stamp owner/origin_session + namespace attributes through the
        # public service boundary so we never reach into store internals
        # (store.get / store._save). For backends that return live objects,
        # mutating the returned node and then calling annotate_memories
        # persists the full record (including owner/origin_session/namespace)
        # via the backend's native save hook. Typed nodes (Fact/Preference)
        # carry no attributes map, so their namespace lives on MemoryNode.
        ids: list[str] = []
        for node in nodes:
            stored = self.service.get(node.id, authorization=self.authorization_context)
            if stored is None:
                continue
            ids.append(node.id)
        if ids:
            self.service.annotate_memories(
                ids,
                attributes=metadata,
                authorization=self.authorization_context,
            )

    def _result_in_namespace(self, func_id: str) -> bool:
        node = self.service.get(func_id, authorization=self.authorization_context)
        if node is None:
            return False
        return self._node_in_namespace(node)

    def get_accessible_memory(self, memory_id: str):
        """Return a memory only when it is visible to this runtime."""

        node = self.service.get(memory_id, authorization=self.authorization_context)
        if node is None or not self._node_in_namespace(node):
            return None
        return node

    def can_access_node(self, node: Any) -> bool:
        """Check a loaded memory-like node against this runtime's boundary."""

        return self._node_in_namespace(node)

    def _node_in_namespace(self, node: Any) -> bool:
        attrs = getattr(node, "attributes", None)
        namespace = dict(getattr(node, "namespace", {}) or {})
        if attrs is None and not namespace:
            # Legacy typed nodes predate the base namespace projection.
            accepted = (
                getattr(node, "memory_type", None) in {"fact", "preference"}
                and bool(getattr(node, "id", None))
                and getattr(node, "owner", None) == self.user_id
                and getattr(node, "origin_session", None) == self.session_id
            )
            if accepted:
                metadata = self._namespace_metadata(visibility="workspace")
                previous_namespace = dict(getattr(node, "namespace", {}) or {})
                node.namespace = dict(metadata)
                try:
                    migrated = self.service.annotate_memories(
                        [node.id],
                        attributes=metadata,
                        authorization=self.authorization_context,
                    )
                except Exception:
                    node.namespace = previous_namespace
                    logger.exception(
                        "legacy typed memory migration failed closed: id=%s user=%s session=%s",
                        node.id,
                        self.user_id,
                        self.session_id,
                    )
                    return False
                if not migrated:
                    node.namespace = previous_namespace
                    logger.error(
                        "legacy typed memory migration returned no record; denying recall: id=%s",
                        node.id,
                    )
                    return False
                logger.warning(
                    "legacy typed memory accepted without workspace provenance and migrated "
                    "to workspace: id=%s user=%s session=%s workspace=%s",
                    node.id,
                    self.user_id,
                    self.session_id,
                    self.workspace_id,
                )
            return accepted
        attrs = {**namespace, **(attrs or {})}
        tenant_id = getattr(node, "tenant_id", None) or attrs.get("memplex_tenant_id")
        subject_id = (
            getattr(node, "owner_subject_id", None)
            or attrs.get("memplex_subject_id")
            or attrs.get("memplex_user_id")
            or getattr(node, "owner", None)
        )
        workspace_id = getattr(node, "workspace_id", None) or attrs.get(
            "memplex_workspace_id"
        )
        visibility = getattr(node, "visibility", None) or attrs.get("memplex_visibility")

        # Principal-bound records use canonical base fields.  In particular,
        # an HTTP sync server may discard adapter-local attributes while
        # preserving this authenticated projection.  Re-check every identity
        # component here because ``can_access_node`` can receive a preloaded
        # node without first going through ``service.get``.
        if tenant_id is not None:
            if tenant_id != self.authorization_context.principal.tenant_id:
                return False
            if subject_id != self.user_id:
                return False
            if visibility == "user":
                return True
            if workspace_id != self.workspace_id:
                return False
            if visibility == "workspace":
                return True
            if visibility == "session":
                provenance = getattr(node, "provenance", {}) or {}
                if not isinstance(provenance, dict):
                    provenance = {}
                source_agent = (
                    provenance.get("agent_id")
                    or attrs.get("memplex_source_agent")
                    or attrs.get("memplex_agent")
                )
                source_session = (
                    getattr(node, "origin_session", None)
                    or provenance.get("session_id")
                    or attrs.get("memplex_source_session_id")
                    or attrs.get("memplex_session_id")
                )
                return source_agent == self.agent and source_session == self.session_id
            return False

        if attrs.get("memplex_user_id") != self.user_id:
            return False
        if visibility is None:
            return (
                attrs.get("memplex_storage_namespace") == self._storage_namespace()
                and attrs.get("memplex_agent") == self.agent
                and attrs.get("memplex_session_id") == self.session_id
                and attrs.get("memplex_project_path") == self.project_path
            )
        if visibility == "user":
            return True
        stored_workspace = attrs.get("memplex_workspace_id") or attrs.get("memplex_project_path")
        if stored_workspace != self.workspace_id:
            return False
        if visibility == "workspace":
            return True
        if visibility == "session":
            source_agent = attrs.get("memplex_source_agent") or attrs.get("memplex_agent")
            source_session = attrs.get("memplex_source_session_id") or attrs.get(
                "memplex_session_id"
            )
            return source_agent == self.agent and source_session == self.session_id
        return False
