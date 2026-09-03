"""Shared observation policy: narratives, dedup keys, and rate limiting.

Single source of truth for the tool-event summarisation, deduplication, and
rate-limit logic used by every observation capture path:

- ``memplex.core.hooks.collector.ObservationCollector`` (in-process, attaches
  to a HookRegistry);
- ``plugin/scripts/hook-runner.py`` (standalone Claude Code hook process);
- ``memplex.adapters.agent_runtime.AgentMemoryRuntime`` (turn capture dedup).

Previously the collector and the hook-runner each implemented their own
rate-limit / narrative logic with different parameters and the two drifted
apart.  Keep the policy here so the capture paths cannot diverge again.
"""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime, timezone
from typing import Any, Optional

# Truncation ceiling for a single tool-input field (e.g. a Bash command)
# inside an observation narrative.
FIELD_LIMIT = 200

# Truncation ceiling for a whole narrative line.
NARRATIVE_LIMIT = 300


def hash_event_payload(payload: dict) -> str:
    """Deterministic MD5 of a dict's sorted JSON representation."""
    return hashlib.md5(
        json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()[:12]


def tool_event_key(tool_name: str, tool_input: dict | None) -> str:
    """Deduplication key for a tool-use event.

    Two events with the same key are considered consecutive duplicates; the
    second one is dropped by every capture path.
    """
    return f"{tool_name}:{hash_event_payload(tool_input or {})}"


def summarize_tool_input(tool_input: dict, max_len: int = 80) -> str:
    """Create a one-line ``key=value`` summary of tool input."""
    if not tool_input:
        return ""
    # Pick the most important key
    for key in ("file_path", "url", "query", "command", "name"):
        if key in tool_input:
            val = str(tool_input[key])
            return f"{key}={val[:max_len]}"
    # Fallback: first key
    first_key = next(iter(tool_input), None)
    if first_key:
        return f"{first_key}={str(tool_input[first_key])[:max_len]}"
    return ""


def tool_narrative(
    tool_name: str,
    tool_input: dict | None,
    tool_result: Any = None,
    max_length: int = NARRATIVE_LIMIT,
) -> str:
    """Generate a human-readable one-line narrative from a tool execution.

    Specialised for the high-value tools (Read/Write/Edit/Bash/web/search);
    everything else falls back to a truncated JSON dump of the input.
    Returns ``""`` when there is nothing worth persisting (no specialised
    field present and an empty input payload).
    """
    del tool_result  # reserved for future result-aware narratives
    tool_input = tool_input or {}
    text = ""
    if tool_name in ("Read", "Write", "Edit"):
        path = str(tool_input.get("file_path") or "")
        if path:
            text = f"{tool_name}: {path}"
    elif tool_name == "Bash":
        command = str(tool_input.get("command") or "")
        if command:
            text = f"Bash: {command[:FIELD_LIMIT]}"
    elif tool_name in ("WebFetch", "FetchURL"):
        url = str(tool_input.get("url") or "")
        if url:
            text = f"{tool_name}: {url[:FIELD_LIMIT]}"
    elif tool_name == "Search":
        query = str(tool_input.get("query") or "")
        if query:
            text = f"Search: {query[:FIELD_LIMIT]}"
    if not text and tool_input:
        text = json.dumps(tool_input, ensure_ascii=False, default=str)
    if not text:
        return ""
    return f"[{tool_name}] {text}"[:max_length]


class RateLimiter:
    """Thread-safe fixed-window rate limiter (max events per 60s window).

    In-process counterpart to the hook-runner's file-based cooldown: used
    where the capturer is long-lived (collector attached to a registry).
    """

    def __init__(self, max_per_minute: int = 20) -> None:
        self._max_per_minute = max_per_minute
        self._window_start = datetime.now(UTC)
        self._count_this_window = 0
        self._lock = threading.RLock()

    def allow(self) -> bool:
        """Return True if this event is within the rate limit; otherwise drop."""
        now = datetime.now(UTC)
        with self._lock:
            # Reset window if minute has passed
            if (now - self._window_start).total_seconds() >= 60:
                self._window_start = now
                self._count_this_window = 0

            if self._count_this_window >= self._max_per_minute:
                return False
            self._count_this_window += 1
            return True
