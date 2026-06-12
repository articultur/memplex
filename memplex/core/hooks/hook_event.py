"""Hook event types defined in design spec §4.2."""

from __future__ import annotations

from enum import Enum


class HookEvent(str, Enum):
    """Lifecycle hook events that agents can register handlers for.

    These events are emitted by adapters (MCP, CLI, HTTP) at specific
    points in the agent lifecycle.  HookRegistry.listeners are called
    synchronously (blocking the emitter) but must be thread-safe since
    multiple adapters may trigger events concurrently.

    Design reference: §4.2 Hook lifecycle events
    """

    SESSION_START = "session_start"
    """Fired when a new agent session begins (after config/identity is loaded)."""

    USER_PROMPT_SUBMIT = "user_prompt_submit"
    """Fired when the user submits a prompt (before tool dispatch)."""

    PRE_TOOL_USE = "pre_tool_use"
    """Fired before a tool is executed (can inspect/modify inputs)."""

    POST_TOOL_USE = "post_tool_use"
    """Fired after a tool completes (can inspect results, emit observations)."""

    POST_TOOL_RESULT = "post_tool_result"
    """Fired after a tool result is formatted (final chance to observe)."""

    SESSION_END = "session_end"
    """Fired when the agent session terminates (last chance to persist state)."""

    AGENT_STOP = "agent_stop"
    """Fired when the agent is being stopped (can flush pending observations)."""
