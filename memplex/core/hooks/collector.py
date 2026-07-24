"""ObservationCollector -- auto-captures observations from tool usage events.

Design spec §4.2: Watches POST_TOOL_USE events, extracts structured observations,
and persists them to MemoryStore.  Implements rate-limiting and deduplication.

The collector is enabled by calling ``attach()`` which registers its
``_on_tool_use`` handler with a HookRegistry.  Call ``detach()`` to unregister.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, List, Optional

from memplex.core.hooks.hook_event import HookEvent
from memplex.core.hooks.registry import HookRegistry, get_default_registry
from memplex.models import Observation

if TYPE_CHECKING:
    # Type-hint only: ``core`` is the pure computation layer and must not
    # take a runtime dependency on ``storage``. This import is gated by
    # TYPE_CHECKING (combined with ``from __future__ import annotations``
    # above) so it never executes at runtime; the collector duck-types
    # the store via ``self._store.add_observation(...)``. Kept as an
    # allowed static-only exception for editor/type-checker support.
    from memplex.storage.base import MemoryStore

logger = logging.getLogger(__name__)

# ── Helpers ──────────────────────────────────────────────────────────────


def _hash_dict(d: dict) -> str:
    """Deterministic MD5 of a dict's sorted JSON representation."""
    import json

    return hashlib.md5(
        json.dumps(d, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()[:12]


# ── ObservationCollector ─────────────────────────────────────────────────


class ObservationCollector:
    """Auto-capture observations from POST_TOOL_USE events.

    Parameters
    ----------
    store:
        MemoryStore backend for persisting observations.
    registry:
        HookRegistry to attach to.  When ``None``, uses the global default
        registry (via ``get_default_registry()``).
    max_per_minute:
        Rate-limit ceiling.  When exceeded within a 1-minute window, events
        are silently dropped.  Default: 20.
    session_id:
        Identifier for the current session, used as the Observation ``session_id``.
    """

    MAX_OBS_PER_MINUTE = 20

    def __init__(
        self,
        store: "MemoryStore",
        registry: Optional[HookRegistry] = None,
        max_per_minute: int = MAX_OBS_PER_MINUTE,
        session_id: str = "default",
    ) -> None:
        self._store = store
        self._registry = registry
        self._max_per_minute = max_per_minute
        self._session_id = session_id

        # Rate-limiting window state
        self._window_start = datetime.now(timezone.utc)
        self._count_this_window = 0

        # Deduplication: skip consecutive identical tool+input pairs
        self._last_event_key: Optional[str] = None

        # Lock for all mutable state
        self._lock = threading.RLock()

        # Currently attached registry (for detach)
        self._attached_registry: Optional[HookRegistry] = None

    # ── Attach / Detach ───────────────────────────────────────────────

    def attach(self) -> None:
        """Register this collector's POST_TOOL_USE handler with the registry."""
        registry = self._registry or get_default_registry()
        registry.register(HookEvent.POST_TOOL_USE, self._on_tool_use)
        self._attached_registry = registry
        logger.info(
            "ObservationCollector attached (session=%s, max/min=%d)",
            self._session_id,
            self._max_per_minute,
        )

    def detach(self) -> None:
        """Unregister this collector's handler from the registry."""
        registry = self._attached_registry or self._registry
        if registry is not None:
            registry.unregister(HookEvent.POST_TOOL_USE, self._on_tool_use)
            self._attached_registry = None
            logger.info("ObservationCollector detached")

    # ── Event handler ─────────────────────────────────────────────────

    def _on_tool_use(self, context: dict) -> None:
        """Handle POST_TOOL_USE event: extract and persist an observation.

        Context expected fields:
        - ``tool_name``: str — name of the tool that was called
        - ``tool_input``: dict — input parameters (used for deduplication)
        - ``tool_result``: Any — raw result from the tool (may be inspectable)
        - ``session_id``: str — session identifier (falls back to instance default)
        """
        tool_name = context.get("tool_name", "unknown")
        tool_input = context.get("tool_input", {})
        tool_result = context.get("tool_result")
        session_id = context.get("session_id", self._session_id)

        # 1. Rate-limit check
        if not self._check_rate_limit():
            return

        # 2. Deduplicate consecutive identical events
        event_key = f"{tool_name}:{_hash_dict(tool_input)}"
        if event_key == self._last_event_key:
            return
        self._last_event_key = event_key

        # 3. Extract observation text
        narrative = self._extract_narrative(tool_name, tool_input, tool_result)
        if not narrative:
            return

        # 4. Build Observation and persist
        import json
        import uuid

        obs = Observation(
            id=f"obs_{uuid.uuid4().hex[:12]}",
            memory_type="observation",
            name=f"{tool_name} observation",
            event=narrative,
            context=json.dumps(
                {
                    "tool_name": tool_name,
                    "tool_input_summary": self._summarize_input(tool_input),
                    "facts": [],
                    "concepts": [],
                    "files_read": self._extract_file_paths(
                        tool_input, tool_result, "read"
                    ),
                    "files_modified": self._extract_file_paths(
                        tool_input, tool_result, "modified"
                    ),
                    "functions_mentioned": [],
                },
                ensure_ascii=False,
                default=str,
            ),
            actor="system",
            origin_session=session_id,
            observed_at=datetime.now(timezone.utc).isoformat(),
        )

        try:
            self._store.add_observation(obs)
            logger.debug("Observation persisted: %s — %s", tool_name, narrative[:80])
        except Exception as exc:
            logger.warning("Failed to persist observation: %s", exc)

    # ── Extraction helpers ─────────────────────────────────────────────

    def _check_rate_limit(self) -> bool:
        """Return True if this event is within rate limit; otherwise drop."""
        now = datetime.now(timezone.utc)
        with self._lock:
            # Reset window if minute has passed
            if (now - self._window_start).total_seconds() >= 60:
                self._window_start = now
                self._count_this_window = 0

            if self._count_this_window >= self._max_per_minute:
                return False
            self._count_this_window += 1
            return True

    def _extract_narrative(
        self, tool_name: str, tool_input: dict, tool_result: Any
    ) -> str:
        """Generate a human-readable narrative from a tool execution."""
        # Most useful tool patterns
        if tool_name == "Read":
            path = tool_input.get("file_path", "")
            return f"[Read] {path}"
        if tool_name == "Write" or tool_name == "Edit":
            path = tool_input.get("file_path", "")
            return f"[Write/Edit] {path}"
        if tool_name == "Bash":
            cmd = tool_input.get("command", "")[:60]
            return f"[Bash] {cmd}"
        if tool_name == "WebFetch":
            url = tool_input.get("url", "")[:60]
            return f"[WebFetch] {url}"
        if tool_name == "Search":
            query = tool_input.get("query", "")
            return f"[Search] {query}"
        return f"[{tool_name}]"

    def _summarize_input(self, tool_input: dict) -> str:
        """Create a one-line summary of tool input for Observation.tool_input_summary."""
        if not tool_input:
            return ""
        # Pick the most important key
        for key in ("file_path", "url", "query", "command", "name"):
            if key in tool_input:
                val = str(tool_input[key])
                return f"{key}={val[:80]}"
        # Fallback: first key
        first_key = next(iter(tool_input), None)
        if first_key:
            return f"{first_key}={str(tool_input[first_key])[:80]}"
        return ""

    def _extract_file_paths(
        self,
        tool_input: dict,
        tool_result: Any,
        mode: str,
    ) -> List[str]:
        """Extract file paths touched by a tool (read or modified).

        This is a best-effort heuristic based on tool name and input keys.
        """
        paths: List[str] = []
        # For Read/Write/Edit, check file_path
        if tool_input.get("file_path"):
            paths.append(str(tool_input["file_path"]))
        # For Bash, check for cp/mv/rm/ mkdir paths in command
        command = tool_input.get("command", "") or ""
        if command:
            import re

            if mode == "modified":
                # Approximate: look for redirection or output paths
                for m in re.finditer(r">\s*([^\s]+)", command):
                    paths.append(m.group(1))
        return list(set(paths))
