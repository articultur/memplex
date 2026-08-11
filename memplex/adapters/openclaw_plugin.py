"""Process bridge used by the native OpenClaw JavaScript plugin.

OpenClaw loads JavaScript/TypeScript plugins in-process.  Memplex keeps its
storage/runtime implementation in Python, so the installed native entry sends
one JSON payload per lifecycle hook or tool call to this module over stdio.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from memplex.adapters.agent_runtime import AgentMemoryRuntime
from memplex.config import load_config
from memplex.service import MemplexService

_PRIVATE_TAG_RE = re.compile(r"<private>.*?</private>", re.DOTALL | re.IGNORECASE)
_RECALLED_BLOCK_RE = re.compile(
    r"<relevant-memories>.*?</relevant-memories>",
    re.DOTALL | re.IGNORECASE,
)


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _mapping(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _clean_text(value: str) -> str:
    without_private = _PRIVATE_TAG_RE.sub("", value)
    return _RECALLED_BLOCK_RE.sub("", without_private).strip()


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return _clean_text(content)
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
            continue
        if not isinstance(item, dict):
            continue
        item_type = _text(item.get("type"))
        if item_type and item_type not in {"text", "input_text", "output_text"}:
            continue
        candidate = _text(item.get("text")) or _text(item.get("content"))
        if candidate:
            parts.append(candidate)
    return _clean_text("\n".join(parts))


def _identity(payload: dict[str, Any]) -> dict[str, str]:
    config = _mapping(payload.get("config"))
    context = _mapping(payload.get("context"))
    event = _mapping(payload.get("event"))
    user_id = (
        _text(os.environ.get("MEMPLEX_USER_ID"))
        or _text(config.get("userId"))
        or _text(config.get("user_id"))
        or getpass.getuser()
    )
    session_id = (
        _text(context.get("sessionId"))
        or _text(context.get("sessionKey"))
        or _text(event.get("sessionId"))
        or _text(event.get("sessionKey"))
        or _text(os.environ.get("MEMPLEX_SESSION_ID"))
        or f"openclaw-{os.getpid()}"
    )
    project_path = (
        _text(context.get("workspaceDir"))
        or _text(context.get("cwd"))
        or _text(event.get("cwd"))
        or _text(os.environ.get("MEMPLEX_PROJECT_ROOT"))
        or _text(config.get("projectPath"))
        or _text(config.get("project_path"))
        or os.getcwd()
    )
    return {
        "agent": "openclaw",
        "user_id": user_id,
        "session_id": session_id,
        "project_path": str(Path(project_path).expanduser().resolve(strict=False)),
    }


def _runtime(
    payload: dict[str, Any],
) -> tuple[AgentMemoryRuntime, MemplexService, dict[str, str]]:
    config = _mapping(payload.get("config"))
    identity = _identity(payload)
    service = MemplexService(config=load_config())
    service.start()
    runtime = AgentMemoryRuntime(
        service=service,
        agent="openclaw",
        user_id=identity["user_id"],
        session_id=identity["session_id"],
        project_path=identity["project_path"],
        top_k=int(config.get("topK") or config.get("top_k") or 5),
        token_budget=int(config.get("tokenBudget") or config.get("token_budget") or 1500),
    )
    return runtime, service, identity


def _last_turn(messages: Any) -> tuple[str, str]:
    if not isinstance(messages, list):
        return "", ""
    current_user = ""
    completed: tuple[str, str] = ("", "")
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = _text(message.get("role")).lower()
        content = _content_text(message.get("content"))
        if not content:
            continue
        if role == "user":
            current_user = content
        elif role == "assistant" and current_user:
            completed = (current_user, content)
    return completed


def recall(payload: dict[str, Any]) -> dict[str, Any]:
    event = _mapping(payload.get("event"))
    prompt = _clean_text(
        _text(event.get("prompt")) or _text(event.get("query")) or _text(payload.get("query"))
    )
    identity = _identity(payload)
    if not prompt:
        return {"prependContext": "", "total": 0, "identity": identity}
    runtime, service, identity = _runtime(payload)
    try:
        recalled = runtime.before_prompt(prompt)
        return {
            "prependContext": recalled.context,
            "total": recalled.total,
            "source": recalled.source,
            "tokensUsed": recalled.tokens_used,
            "identity": identity,
        }
    finally:
        service.stop()


def capture(payload: dict[str, Any]) -> dict[str, Any]:
    event = _mapping(payload.get("event"))
    identity = _identity(payload)
    if event.get("success") is False:
        return {"captured": False, "reason": "unsuccessful_run", "identity": identity}
    user_message, assistant_message = _last_turn(event.get("messages"))
    if not user_message or not assistant_message:
        return {"captured": False, "reason": "no_completed_turn", "identity": identity}
    context = _mapping(payload.get("context"))
    config = _mapping(payload.get("config"))
    metadata = {
        "hook_event_name": "agent_end",
        "openclaw_agent_id": _text(context.get("agentId")),
        "openclaw_run_id": _text(context.get("runId")) or _text(event.get("runId")),
        "memplex_visibility": _text(config.get("visibility")) or "workspace",
    }
    runtime, service, identity = _runtime(payload)
    try:
        runtime.after_response(
            user_message=user_message,
            assistant_message=assistant_message,
            metadata=metadata,
        )
        return {"captured": True, "identity": identity}
    finally:
        service.stop()


def store(payload: dict[str, Any]) -> dict[str, Any]:
    event = _mapping(payload.get("event"))
    content = _clean_text(_text(event.get("content")) or _text(payload.get("content")))
    identity = _identity(payload)
    if not content:
        return {"stored": False, "reason": "empty_content", "identity": identity}
    config = _mapping(payload.get("config"))
    runtime, service, identity = _runtime(payload)
    try:
        runtime.capture_turn(
            content,
            "Stored explicitly by the OpenClaw memory_store tool.",
            metadata={
                "tool_name": "memory_store",
                "memplex_visibility": _text(config.get("visibility")) or "workspace",
            },
        )
        return {"stored": True, "identity": identity}
    finally:
        service.stop()


def session_end(payload: dict[str, Any]) -> dict[str, Any]:
    event = _mapping(payload.get("event"))
    return {
        "closed": True,
        "reason": _text(event.get("reason")) or "unknown",
        "identity": _identity(payload),
    }


_ACTIONS = {
    "capture": capture,
    "recall": recall,
    "search": recall,
    "session-end": session_end,
    "store": store,
}


def _read_payload() -> dict[str, Any]:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        raise ValueError("OpenClaw bridge expects one JSON object on stdin") from exc
    if not isinstance(payload, dict):
        raise ValueError("OpenClaw bridge payload must be an object")
    return payload


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if len(arguments) != 1 or arguments[0] not in _ACTIONS:
        print(
            "usage: python -m memplex.adapters.openclaw_plugin "
            "capture|recall|search|session-end|store",
            file=sys.stderr,
        )
        return 2
    try:
        result = _ACTIONS[arguments[0]](_read_payload())
    except Exception as exc:
        print(f"memplex openclaw bridge failed: {exc}", file=sys.stderr)
        return 1
    sys.stdout.write(json.dumps(result, ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
