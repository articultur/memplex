"""Codex 原生插件入口：MCP stdio 与生命周期 Hook。

Codex 插件通过 ``PLUGIN_ROOT`` 找到安装器写入的托管身份文件，并以
Hook payload 中的 ``cwd``/``session_id`` 作为动态工作区和会话来源。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)
import getpass
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

from memplex.adapters.agent_runtime import AgentMemoryRuntime
from memplex.adapters.managed_identity import (
    derive_managed_host_root,
    load_managed_identity,
)
from memplex.adapters.runtime_status import (
    clear_runtime_status_on_success,
    record_runtime_failure,
    runtime_status_path,
)
from memplex.config import load_config
from memplex.service import MemplexService

_PRIVATE_TAG_RE = re.compile(r"<private>.*?</private>", re.DOTALL)
_IDENTITY_FILE = "memplex-agent.json"


def _strip_private(value: str) -> str:
    return _PRIVATE_TAG_RE.sub("", value)


def _sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return _strip_private(value)
    if isinstance(value, dict):
        return {str(key): _sanitize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize(item) for item in value]
    return value


def _safe_text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _plugin_root() -> Path | None:
    raw = _safe_text(os.environ.get("PLUGIN_ROOT")) or _safe_text(
        os.environ.get("MEMPLEX_PLUGIN_ROOT")
    )
    return Path(raw).expanduser().resolve(strict=False) if raw else None


def _identity_config() -> dict[str, Any]:
    root = _plugin_root()
    if root is None:
        return {}
    path = root / _IDENTITY_FILE
    expected_host_root = derive_managed_host_root(root, expected_agent="codex")
    payload = load_managed_identity(
        path,
        expected_agent="codex",
        expected_host_root=expected_host_root,
    )
    return {
        "user_id": payload["user_id"],
        "project_path": payload["project_path"],
        "host_root": payload["host_root"],
    }


def _identity(payload: dict[str, Any]) -> dict[str, str]:
    configured = _identity_config()
    user_id = (
        _safe_text(configured.get("user_id"))
        or _safe_text(os.environ.get("MEMPLEX_USER_ID"))
        or getpass.getuser()
    )
    session_id = (
        _safe_text(payload.get("session_id"))
        or _safe_text(payload.get("sessionId"))
        or _safe_text(os.environ.get("MEMPLEX_SESSION_ID"))
        or _safe_text(os.environ.get("CODEX_SESSION_ID"))
        or f"codex-{os.getpid()}"
    )
    project_path = (
        _safe_text(configured.get("project_path"))
        or _safe_text(payload.get("cwd"))
        or _safe_text(payload.get("project_path"))
        or _safe_text(os.environ.get("MEMPLEX_PROJECT_ROOT"))
        or os.getcwd()
    )
    return {
        "agent": "codex",
        "user_id": user_id,
        "session_id": session_id,
        "project_path": str(Path(project_path).expanduser().resolve(strict=False)),
    }


def _state_path(identity: dict[str, str]) -> Path:
    configured = _safe_text(os.environ.get("PLUGIN_DATA"))
    if configured:
        root = Path(configured).expanduser().resolve(strict=False)
    else:
        plugin_root = _plugin_root()
        root = (plugin_root or Path.cwd()) / ".memplex-data"
    key = f"{identity['user_id']}\u0000{identity['project_path']}\u0000{identity['session_id']}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return root / "turns" / f"{digest}.json"


def _runtime_status_path() -> Path:
    """Keep hook health with the stable Codex host root, not per-turn data."""

    configured = _identity_config()
    root = Path(configured.get("host_root") or os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    return runtime_status_path(root)


def _record_runtime_failure(operation: str, error: BaseException) -> None:
    try:
        record_runtime_failure(
            _runtime_status_path(), agent="codex", operation=operation, error=error
        )
    except Exception as exc:  # noqa: BLE001 - logged degradation path
        # The original hook error remains authoritative and the hook must stay
        # non-blocking even when the local status volume is unavailable.
        logger.debug("suppressed Exception: %s", exc)


def _clear_runtime_status(operation: str) -> None:
    try:
        clear_runtime_status_on_success(
            _runtime_status_path(), agent="codex", operation=operation, completed=True
        )
    except Exception as exc:  # noqa: BLE001 - logged degradation path
        logger.debug("suppressed Exception in cleanup/degradation path: %s", exc)


def _write_turn_state(identity: dict[str, str], prompt: str) -> None:
    path = _state_path(identity)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "user_id": identity["user_id"],
        "session_id": identity["session_id"],
        "project_path": identity["project_path"],
        "prompt": _strip_private(prompt),
    }
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(payload, handle, ensure_ascii=False)
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.chmod(0o600)
        os.replace(temporary_path, path)
    except Exception:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
        raise


def _read_turn_state(identity: dict[str, str]) -> tuple[Path, dict[str, Any]]:
    path = _state_path(identity)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return path, {}
    if not isinstance(payload, dict):
        return path, {}
    if (
        _safe_text(payload.get("user_id")) != identity["user_id"]
        or _safe_text(payload.get("session_id")) != identity["session_id"]
        or _safe_text(payload.get("project_path")) != identity["project_path"]
    ):
        return path, {}
    return path, payload


def _runtime(identity: dict[str, str]) -> tuple[AgentMemoryRuntime, MemplexService]:
    service = MemplexService(config=load_config())
    service.start()
    return (
        AgentMemoryRuntime(
            service=service,
            agent="codex",
            user_id=identity["user_id"],
            session_id=identity["session_id"],
            project_path=identity["project_path"],
        ),
        service,
    )


def _context_output(event: str, context: str) -> dict[str, Any]:
    if not context:
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": f"[Memplex] 相关记忆：\n{context}",
        }
    }


def _prompt(payload: dict[str, Any]) -> str:
    for key in ("prompt", "user_prompt", "userPrompt", "text"):
        text = _safe_text(payload.get(key))
        if text:
            return text
    return ""


def _session_start_query(identity: dict[str, str]) -> str:
    override = _safe_text(os.environ.get("MEMPLEX_SESSION_QUERY"))
    if override:
        return override
    project_name = Path(identity["project_path"]).name or identity["project_path"]
    return f"{project_name} project context conventions decisions"


def _handle_recall(event: str, payload: dict[str, Any]) -> dict[str, Any]:
    identity = _identity(payload)
    query = _prompt(payload) if event == "UserPromptSubmit" else _session_start_query(identity)
    if not query:
        return {}
    if event == "UserPromptSubmit":
        _write_turn_state(identity, query)
    try:
        runtime, service = _runtime(identity)
        try:
            output = _context_output(event, runtime.before_prompt(query).context)
        finally:
            service.stop()
    except Exception as exc:
        _record_runtime_failure("recall", exc)
        raise
    _clear_runtime_status("recall")
    return output


def _handle_post_tool(payload: dict[str, Any]) -> dict[str, Any]:
    from memplex.core.hooks.policy import tool_narrative

    tool_name = _safe_text(payload.get("tool_name")) or _safe_text(payload.get("toolName"))
    tool_input = payload.get("tool_input", {})
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    narrative = _strip_private(tool_narrative(tool_name or "unknown", tool_input))
    if not narrative:
        return {}
    identity = _identity(payload)
    try:
        runtime, service = _runtime(identity)
        try:
            runtime.after_response(
                user_message=narrative,
                assistant_message="Observed Codex tool use.",
                metadata={"tool_name": tool_name or "unknown", "tool_input": _sanitize(tool_input)},
            )
        finally:
            service.stop()
    except Exception as exc:
        _record_runtime_failure("capture", exc)
        raise
    _clear_runtime_status("capture")
    return {}


def _handle_stop(payload: dict[str, Any]) -> dict[str, Any]:
    identity = _identity(payload)
    path, turn = _read_turn_state(identity)
    prompt = _safe_text(turn.get("prompt"))
    assistant = _safe_text(payload.get("last_assistant_message")) or _safe_text(
        payload.get("lastAssistantMessage")
    )
    if not prompt or not assistant:
        return {}

    capture_identity = {
        "agent": "codex",
        "user_id": _safe_text(turn.get("user_id")) or identity["user_id"],
        "session_id": _safe_text(turn.get("session_id")) or identity["session_id"],
        "project_path": _safe_text(turn.get("project_path")) or identity["project_path"],
    }
    try:
        runtime, service = _runtime(capture_identity)
        try:
            runtime.after_response(
                user_message=_strip_private(prompt),
                assistant_message=_strip_private(assistant),
                metadata={"hook_event_name": "Stop"},
            )
        finally:
            service.stop()
    except Exception as exc:
        _record_runtime_failure("capture", exc)
        raise
    _clear_runtime_status("capture")
    try:
        path.unlink()
    except OSError as exc:
        logger.debug("suppressed OSError in cleanup/degradation path: %s", exc)
    return {}


def dispatch_hook(payload: dict[str, Any]) -> dict[str, Any]:
    event = _safe_text(payload.get("hook_event_name")) or _safe_text(payload.get("hookEventName"))
    if event in {"SessionStart", "UserPromptSubmit"}:
        return _handle_recall(event, payload)
    if event == "PostToolUse":
        return _handle_post_tool(payload)
    if event == "Stop":
        return _handle_stop(payload)
    return {}


def _read_stdin_payload() -> dict[str, Any]:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _run_hook() -> int:
    try:
        output = dispatch_hook(_read_stdin_payload())
    except Exception as exc:  # Hook failures must not block Codex.  # noqa: BLE001 - broad catch with explicit fallback handling
        print(f"memplex codex hook skipped: {exc}", file=sys.stderr)
        output = {}
    sys.stdout.write(json.dumps(output, ensure_ascii=False) + "\n")
    return 0


def _run_mcp() -> int:
    identity = _identity({})
    # This process boundary is the trust anchor for MCPServer.  A managed
    # installation must win over inherited environment variables; only the
    # host session stays dynamic (and _identity creates a process-stable one
    # when the host did not supply it).
    os.environ["MEMPLEX_AGENT_ID"] = identity["agent"]
    os.environ["MEMPLEX_USER_ID"] = identity["user_id"]
    os.environ["MEMPLEX_SESSION_ID"] = identity["session_id"]
    os.environ["MEMPLEX_PROJECT_ROOT"] = identity["project_path"]
    from memplex.adapters.mcp_server import MCPServer

    MCPServer().run()
    return 0


def main(argv: list[str] | None = None) -> int:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments or arguments[0] not in {"hook", "mcp"}:
        print("usage: python -m memplex.adapters.codex_plugin hook|mcp", file=sys.stderr)
        return 2
    return _run_hook() if arguments[0] == "hook" else _run_mcp()


if __name__ == "__main__":
    raise SystemExit(main())
