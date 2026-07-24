"""One-command install and uninstall helpers for agent integrations."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from memplex.adapters._shared import get_plugin_source_dir as _get_plugin_source_dir
from memplex.adapters._shared import marketplace_json as _marketplace_json
from memplex.adapters.agent_runtime import get_agent_manifest

MANAGED_BEGIN = "# >>> memplex managed agent integration >>>"
MANAGED_END = "# <<< memplex managed agent integration <<<"


@dataclass
class AgentInstallResult:
    """Result returned by agent install/uninstall commands."""

    agent: str
    action: str
    status: str
    files: list[str]
    message: str
    next_steps: list[str]


@dataclass
class AgentInstallerSpec:
    """Registry entry describing how to install/uninstall one agent host.

    install / uninstall are the per-agent functions. needs_identity marks
    whether the installer takes ``(target_dir, user_id, project_path, ...)``
    (openclaw, hermes) or just ``(target_dir, ...)`` (codex, claude-code).
    """

    install: Callable[..., AgentInstallResult]
    uninstall: Callable[..., AgentInstallResult]
    needs_identity: bool = False


def install_agent(
    agent: str,
    *,
    target_dir: str | Path | None = None,
    user_id: str | None = None,
    project_path: str | Path | None = None,
    dry_run: bool = False,
) -> list[AgentInstallResult]:
    """Install Memplex into one or all supported agent hosts."""

    names = _expand_agents(agent)
    results: list[AgentInstallResult] = []
    installed: list[str] = []
    for name in names:
        try:
            result = _install_one(
                name,
                target_dir=target_dir,
                user_id=user_id,
                project_path=project_path,
                dry_run=dry_run,
            )
        except Exception as exc:
            if len(names) > 1 and installed and not dry_run:
                rollback_errors = _rollback_installs(installed, target_dir)
                detail = f" Rolled back installed agents: {', '.join(reversed(installed))}."
                if rollback_errors:
                    detail += f" Rollback errors: {'; '.join(rollback_errors)}."
                raise RuntimeError(f"Failed to install {name}: {exc}.{detail}") from exc
            raise
        results.append(result)
        installed.append(name)
    return results


def uninstall_agent(
    agent: str,
    *,
    target_dir: str | Path | None = None,
    dry_run: bool = False,
) -> list[AgentInstallResult]:
    """Remove Memplex integration from one or all supported agent hosts."""

    return [
        _uninstall_one(name, target_dir=target_dir, dry_run=dry_run)
        for name in _expand_agents(agent)
    ]


def _expand_agents(agent: str) -> list[str]:
    requested = (agent or "auto").strip().lower()
    if requested == "all":
        return ["codex", "claude-code", "openclaw", "hermes"]
    if requested == "auto":
        detected = _detect_agents()
        if not detected:
            raise ValueError(
                "No supported local agents detected. Re-run with "
                "--agent codex|claude-code|openclaw|hermes|all."
            )
        return detected
    return [get_agent_manifest(requested)["name"]]


def _detect_agents() -> list[str]:
    detected: list[str] = []
    checks = [
        ("codex", "CODEX_HOME", ".codex", "codex"),
        ("claude-code", "CLAUDE_CONFIG_DIR", ".claude", "claude"),
        ("openclaw", "OPENCLAW_CONFIG_DIR", ".openclaw", "openclaw"),
        ("hermes", "HERMES_CONFIG_DIR", ".hermes", "hermes"),
    ]
    for name, env_name, default_name, command in checks:
        root = Path(os.environ.get(env_name, Path.home() / default_name)).expanduser()
        if root.exists() or shutil.which(command):
            detected.append(name)
    return detected


def _install_one(
    agent: str,
    *,
    target_dir: str | Path | None,
    user_id: str | None,
    project_path: str | Path | None,
    dry_run: bool,
) -> AgentInstallResult:
    spec = _INSTALLERS.get(agent)
    if spec is None:
        raise ValueError(f"Unsupported agent: {agent}")
    if spec.needs_identity:
        return spec.install(target_dir, user_id, project_path, dry_run=dry_run)
    return spec.install(target_dir, dry_run=dry_run)


def _uninstall_one(
    agent: str,
    *,
    target_dir: str | Path | None,
    dry_run: bool,
) -> AgentInstallResult:
    spec = _INSTALLERS.get(agent)
    if spec is None:
        raise ValueError(f"Unsupported agent: {agent}")
    return spec.uninstall(target_dir, dry_run=dry_run)


def _rollback_installs(installed: list[str], target_dir: str | Path | None) -> list[str]:
    errors: list[str] = []
    for name in reversed(installed):
        try:
            _uninstall_one(name, target_dir=target_dir, dry_run=False)
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    return errors


def _install_codex(target_dir: str | Path | None, *, dry_run: bool) -> AgentInstallResult:
    root = _target_dir(target_dir, "CODEX_HOME", ".codex")
    config_path = root / "config.toml"
    block = "\n".join(
        [
            MANAGED_BEGIN,
            "[mcp_servers.memplex]",
            f'command = "{_python_command()}"',
            'args = ["-m", "memplex.adapters.mcp_server"]',
            "startup_timeout_sec = 10",
            "tool_timeout_sec = 60",
            MANAGED_END,
            "",
        ]
    )

    if not dry_run:
        root.mkdir(parents=True, exist_ok=True)
        existing = config_path.read_text() if config_path.exists() else ""
        if _has_unmanaged_codex_memplex_table(existing):
            raise ValueError(
                "Codex config already contains an unmanaged [mcp_servers.memplex] "
                "table. Remove or rename it before running memplex agent install."
            )
        config_path.write_text(_replace_managed_block(existing, block))

    return AgentInstallResult(
        agent="codex",
        action="install",
        status="planned" if dry_run else "installed",
        files=[str(config_path)],
        message="Registered Memplex MCP server in Codex config.toml.",
        next_steps=["Restart Codex or run /mcp to confirm the memplex server is loaded."],
    )


def _uninstall_codex(target_dir: str | Path | None, *, dry_run: bool) -> AgentInstallResult:
    root = _target_dir(target_dir, "CODEX_HOME", ".codex")
    config_path = root / "config.toml"
    if config_path.exists():
        next_text = _remove_managed_block(config_path.read_text())
        if not dry_run:
            config_path.write_text(next_text)
    return AgentInstallResult(
        agent="codex",
        action="uninstall",
        status="planned" if dry_run else "uninstalled",
        files=[str(config_path)],
        message="Removed Memplex managed block from Codex config.toml.",
        next_steps=["Restart Codex or run /mcp to confirm memplex is gone."],
    )


def _install_claude_code(
    target_dir: str | Path | None,
    *,
    dry_run: bool,
) -> AgentInstallResult:
    root = _target_dir(target_dir, "CLAUDE_CONFIG_DIR", ".claude")
    market_dir = root / "plugins" / "marketplaces" / "articultur"
    plugin_target = market_dir / "plugin"
    marketplace_path = market_dir / "marketplace.json"

    if not dry_run:
        source = _get_plugin_source_dir()
        if plugin_target.exists():
            shutil.rmtree(plugin_target)
        shutil.copytree(source, plugin_target, symlinks=False, ignore=_ignore_patterns)
        market_dir.mkdir(parents=True, exist_ok=True)
        marketplace_path.write_text(_marketplace_json().strip() + "\n")
        (market_dir / ".install-version").write_text(
            json.dumps(
                {
                    "version": _package_version(),
                    "installedAt": datetime.now().isoformat(),
                },
                indent=2,
            )
        )

    return AgentInstallResult(
        agent="claude-code",
        action="install",
        status="planned" if dry_run else "installed",
        files=[str(marketplace_path), str(plugin_target)],
        message="Installed Memplex Claude Code plugin marketplace entry.",
        next_steps=["Restart Claude Code to activate hooks, MCP, and skills."],
    )


def _uninstall_claude_code(
    target_dir: str | Path | None,
    *,
    dry_run: bool,
) -> AgentInstallResult:
    root = _target_dir(target_dir, "CLAUDE_CONFIG_DIR", ".claude")
    market_dir = root / "plugins" / "marketplaces" / "articultur"
    if market_dir.exists() and not dry_run:
        shutil.rmtree(market_dir)
    return AgentInstallResult(
        agent="claude-code",
        action="uninstall",
        status="planned" if dry_run else "uninstalled",
        files=[str(market_dir)],
        message="Removed Memplex Claude Code plugin marketplace entry.",
        next_steps=["Restart Claude Code to unload Memplex hooks, MCP, and skills."],
    )


def _install_openclaw(
    target_dir: str | Path | None,
    user_id: str | None,
    project_path: str | Path | None,
    *,
    dry_run: bool,
) -> AgentInstallResult:
    root = _target_dir(target_dir, "OPENCLAW_CONFIG_DIR", ".openclaw")
    config_path = root / "openclaw.json"
    extension_dir = root / "extensions" / "memplex"
    config = _read_json(config_path)
    plugins = config.setdefault("plugins", {})
    slots = plugins.setdefault("slots", {})
    previous_memory_slot = slots.get("memory")
    entries = plugins.setdefault("entries", {})
    existing_entry = entries.get("memplex")
    if existing_entry and not _is_managed_openclaw_entry(existing_entry):
        raise ValueError(
            "OpenClaw config already contains an unmanaged memplex plugin entry. "
            "Remove or rename it before running memplex agent install."
        )
    if extension_dir.exists() and not _is_managed_openclaw_extension(extension_dir):
        raise ValueError(
            "OpenClaw extensions/memplex already exists and is not managed by Memplex. "
            "Remove or rename it before running memplex agent install."
        )
    allow = plugins.setdefault("allow", [])
    existing_allow_entry = "memplex" in allow
    slots["memory"] = "memplex"
    entry = entries.setdefault("memplex", {})
    existing_managed = entry.get("config", {}).get("managed", {})
    managed = {
        "installer": "memplex",
        "previousMemorySlot": existing_managed.get("previousMemorySlot"),
        "addedAllowEntry": existing_managed.get("addedAllowEntry", False),
    }
    if previous_memory_slot and previous_memory_slot != "memplex":
        managed["previousMemorySlot"] = previous_memory_slot
    entry.update(
        {
            "enabled": True,
            "config": {
                "mode": "local",
                "userId": user_id or os.environ.get("USER") or "default",
                "projectPath": str(project_path or Path.cwd()),
                "command": _mcp_command(),
                "skills": {
                    "triage": {"enabled": True},
                    "recall": {"enabled": True, "tokenBudget": 1500, "rerank": True},
                    "dream": {"enabled": True},
                },
                "managed": managed,
            },
        }
    )
    if not existing_allow_entry:
        allow.append("memplex")
        managed["addedAllowEntry"] = True

    if not dry_run:
        root.mkdir(parents=True, exist_ok=True)
        _write_json(config_path, config)
        _write_openclaw_extension(extension_dir)

    return AgentInstallResult(
        agent="openclaw",
        action="install",
        status="planned" if dry_run else "installed",
        files=[
            str(config_path),
            str(extension_dir / "openclaw.plugin.json"),
            str(extension_dir / "plugin.json"),
        ],
        message="Configured OpenClaw memory slot and installed Memplex extension.",
        next_steps=["Restart OpenClaw and inspect plugins to confirm memplex is enabled."],
    )


def _uninstall_openclaw(
    target_dir: str | Path | None,
    *,
    dry_run: bool,
) -> AgentInstallResult:
    root = _target_dir(target_dir, "OPENCLAW_CONFIG_DIR", ".openclaw")
    config_path = root / "openclaw.json"
    extension_dir = root / "extensions" / "memplex"
    if config_path.exists():
        config = _read_json(config_path)
        plugins = config.get("plugins", {})
        slots = plugins.get("slots", {})
        memplex_entry = plugins.get("entries", {}).get("memplex", {})
        is_managed_entry = _is_managed_openclaw_entry(memplex_entry)
        config_changed = False
        previous_memory_slot = (
            memplex_entry.get("config", {}).get("managed", {}).get("previousMemorySlot")
        )
        added_allow_entry = (
            memplex_entry.get("config", {}).get("managed", {}).get("addedAllowEntry", False)
        )
        if is_managed_entry and slots.get("memory") == "memplex":
            if previous_memory_slot:
                slots["memory"] = previous_memory_slot
            else:
                slots.pop("memory", None)
            config_changed = True
        if is_managed_entry:
            plugins.get("entries", {}).pop("memplex", None)
            config_changed = True
        if is_managed_entry and added_allow_entry and "allow" in plugins:
            plugins["allow"] = [item for item in plugins["allow"] if item != "memplex"]
            config_changed = True
        if config_changed and not dry_run:
            _write_json(config_path, config)
    if extension_dir.exists() and _is_managed_openclaw_extension(extension_dir) and not dry_run:
        shutil.rmtree(extension_dir)
    return AgentInstallResult(
        agent="openclaw",
        action="uninstall",
        status="planned" if dry_run else "uninstalled",
        files=[str(config_path), str(extension_dir)],
        message="Removed Memplex OpenClaw memory slot and extension.",
        next_steps=["Restart OpenClaw and inspect plugins to confirm memplex is gone."],
    )


def _install_hermes(
    target_dir: str | Path | None,
    user_id: str | None,
    project_path: str | Path | None,
    *,
    dry_run: bool,
) -> AgentInstallResult:
    root = _target_dir(target_dir, "HERMES_CONFIG_DIR", ".hermes")
    provider_dir = root / "memory-providers"
    provider_path = provider_dir / "memplex.json"
    plugin_dir = root / "plugins" / "memory" / "memplex"
    if provider_path.exists() and not _is_managed_json_file(provider_path):
        raise ValueError(
            "Hermes memory-providers/memplex.json already exists and is not managed "
            "by Memplex. Remove or rename it before running memplex agent install."
        )
    if plugin_dir.exists() and not _is_managed_hermes_plugin(plugin_dir):
        raise ValueError(
            "Hermes plugins/memory/memplex already exists and is not managed by "
            "Memplex. Remove or rename it before running memplex agent install."
        )
    provider_config = {
        "name": "memplex",
        "provider": "memplex",
        "command": _mcp_command(),
        "agent": "hermes",
        "user_id": user_id or "${MEMPLEX_USER_ID:-hermes-user}",
        "project_path": str(project_path or Path.cwd()),
        "prefetch": True,
        "tools": ["memory_agent_manifest", "memory_turn_begin", "memory_turn_end"],
        "managed": {"installer": "memplex"},
    }
    if not dry_run:
        provider_dir.mkdir(parents=True, exist_ok=True)
        _write_json(provider_path, provider_config)
        _write_hermes_provider_plugin(plugin_dir, provider_config)

    return AgentInstallResult(
        agent="hermes",
        action="install",
        status="planned" if dry_run else "installed",
        files=[
            str(provider_path),
            str(plugin_dir / "plugin.yaml"),
            str(plugin_dir / "__init__.py"),
        ],
        message="Installed Memplex Hermes memory provider plugin and descriptor.",
        next_steps=["Restart Hermes and select the memplex memory provider."],
    )


def _uninstall_hermes(
    target_dir: str | Path | None,
    *,
    dry_run: bool,
) -> AgentInstallResult:
    root = _target_dir(target_dir, "HERMES_CONFIG_DIR", ".hermes")
    provider_path = root / "memory-providers" / "memplex.json"
    plugin_dir = root / "plugins" / "memory" / "memplex"
    if provider_path.exists() and _is_managed_json_file(provider_path) and not dry_run:
        provider_path.unlink()
    if plugin_dir.exists() and _is_managed_hermes_plugin(plugin_dir) and not dry_run:
        shutil.rmtree(plugin_dir)
    return AgentInstallResult(
        agent="hermes",
        action="uninstall",
        status="planned" if dry_run else "uninstalled",
        files=[str(provider_path), str(plugin_dir)],
        message="Removed Memplex Hermes memory provider plugin and descriptor.",
        next_steps=["Restart Hermes to unload the memplex memory provider."],
    )


def _target_dir(
    explicit: str | Path | None,
    env_name: str,
    default_name: str,
) -> Path:
    if explicit is not None:
        return Path(explicit).expanduser()
    return Path(os.environ.get(env_name, Path.home() / default_name)).expanduser()


def _python_command() -> str:
    return os.environ.get("MEMPLEX_PYTHON", sys.executable or "python")


def _mcp_command() -> list[str]:
    return [_python_command(), "-m", "memplex.adapters.mcp_server"]


def _replace_managed_block(existing: str, block: str) -> str:
    without = _remove_managed_block(existing).rstrip()
    return f"{without}\n\n{block}" if without else block


def _remove_managed_block(existing: str) -> str:
    lines = existing.splitlines()
    kept: list[str] = []
    skipping = False
    for line in lines:
        if line.strip() == MANAGED_BEGIN:
            skipping = True
            continue
        if line.strip() == MANAGED_END:
            skipping = False
            continue
        if not skipping:
            kept.append(line)
    return "\n".join(kept).rstrip() + ("\n" if kept else "")


def _has_unmanaged_codex_memplex_table(existing: str) -> bool:
    unmanaged = _remove_managed_block(existing)
    return any(line.strip() == "[mcp_servers.memplex]" for line in unmanaged.splitlines())


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = path.read_text()
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return json.loads(_strip_jsonc(raw))


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n")


def _write_openclaw_extension(extension_dir: Path) -> None:
    hooks_dir = extension_dir / "hooks"
    hooks_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": "memplex",
        "name": "Memplex",
        "version": _package_version(),
        "description": "Memplex long-term memory for OpenClaw agents",
        "author": "Memplex",
        "kind": "memory",
        "managed": {"installer": "memplex"},
        "slots": ["memory"],
        "contracts": {
            "tools": [
                "memory_search",
                "memory_add",
                "memory_get",
                "memory_update",
                "memory_delete",
                "memory_turn_end",
            ]
        },
        "hooks": [
            {"event": "recall", "handler": "hooks.recall.load_memories"},
            {"event": "triage", "handler": "hooks.triage.capture_turn"},
            {"event": "dream", "handler": "hooks.dream.compact_memories"},
        ],
        "config": {
            "mode": "local",
            "autoRecall": True,
            "autoCapture": True,
            "command": _mcp_command(),
            "managed": {"installer": "memplex"},
        },
    }
    _write_json(extension_dir / "openclaw.plugin.json", manifest)
    _write_json(extension_dir / "plugin.json", manifest)
    (hooks_dir / "__init__.py").write_text("")
    (hooks_dir / "recall.py").write_text(
        '''"""OpenClaw recall hook for Memplex."""

from memplex.adapters.agent_runtime import AgentMemoryRuntime


def load_memories(context):
    runtime = AgentMemoryRuntime(
        agent="openclaw",
        user_id=context.get("user_id") or context.get("userId"),
        session_id=context.get("session_id") or context.get("sessionId") or "default",
        project_path=context.get("project_path") or context.get("projectPath"),
        top_k=context.get("top_k", 5),
        token_budget=context.get("token_budget", 1500),
    )
    prompt = context.get("prompt") or context.get("task") or context.get("message") or ""
    recalled = runtime.before_prompt(prompt)
    context["memplex_context"] = recalled.context
    context["memplex_memories"] = recalled.total
    return context
'''
    )
    (hooks_dir / "triage.py").write_text(
        '''"""OpenClaw triage hook for Memplex."""

from memplex.adapters.agent_runtime import AgentMemoryRuntime


def capture_turn(context):
    runtime = AgentMemoryRuntime(
        agent="openclaw",
        user_id=context.get("user_id") or context.get("userId"),
        session_id=context.get("session_id") or context.get("sessionId") or "default",
        project_path=context.get("project_path") or context.get("projectPath"),
    )
    user_message = context.get("user_message") or context.get("userMessage") or ""
    assistant_message = context.get("assistant_message") or context.get("assistantMessage") or ""
    if user_message or assistant_message:
        runtime.after_response(user_message=user_message, assistant_message=assistant_message)
    return context
'''
    )
    (hooks_dir / "dream.py").write_text(
        '''"""OpenClaw dream hook for Memplex."""

from memplex.service import MemplexService


def compact_memories(context):
    scope = context.get("scope", "project")
    service = MemplexService()
    try:
        result = service.compact(scope=scope)
        context["memplex_compaction"] = {
            "scope": scope,
            "processed": result.total_processed,
            "merged": result.total_merged,
            "removed": result.total_removed,
            "duration_ms": result.duration_ms,
        }
        return context
    finally:
        service.stop()
'''
    )


def _write_hermes_provider_plugin(plugin_dir: Path, provider_config: dict[str, Any]) -> None:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.yaml").write_text(
        "\n".join(
            [
                "name: memplex",
                f"version: {_package_version()}",
                'description: "Memplex local long-term memory provider"',
                "hooks:",
                "  - sync_turn",
                "  - on_pre_compress",
                "  - on_session_end",
                "",
            ]
        )
    )
    _write_json(plugin_dir / "memplex.json", provider_config)
    (plugin_dir / "README.md").write_text(
        "# Memplex Memory Provider\n\n"
        "Provides Hermes Agent with local Memplex recall, turn sync, and "
        "prefetch through the Hermes MemoryProvider lifecycle.\n"
    )
    (plugin_dir / "__init__.py").write_text(
        '''"""Hermes MemoryProvider plugin for Memplex."""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

logger = logging.getLogger(__name__)


def _load_config(hermes_home: str) -> dict:
    config = {
        "user_id": os.environ.get("MEMPLEX_USER_ID", "hermes-user"),
        "project_path": os.getcwd(),
        "prefetch": True,
    }
    config_path = Path(hermes_home) / "plugins" / "memory" / "memplex" / "memplex.json"
    if config_path.exists():
        try:
            file_config = json.loads(config_path.read_text(encoding="utf-8"))
            config.update({k: v for k, v in file_config.items() if v not in (None, "")})
        except Exception as exc:
            logger.warning("Memplex config load failed: %s", exc)
    return config


class MemplexMemoryProvider(MemoryProvider):
    @property
    def name(self) -> str:
        return "memplex"

    def is_available(self) -> bool:
        try:
            import memplex  # noqa: F401
            return True
        except Exception:
            return False

    def initialize(self, session_id: str, **kwargs) -> None:
        self._session_id = session_id
        self._hermes_home = kwargs.get("hermes_home") or str(Path.home() / ".hermes")
        self._config = _load_config(self._hermes_home)
        self._runtime = None
        self._sync_thread: Optional[threading.Thread] = None

    def _ensure_runtime(self):
        from memplex.adapters.agent_runtime import AgentMemoryRuntime

        if self._runtime is None:
            self._runtime = AgentMemoryRuntime(
                agent="hermes",
                user_id=self._config.get("user_id") or "hermes-user",
                session_id=self._session_id,
                project_path=self._config.get("project_path") or os.getcwd(),
                prefetch=bool(self._config.get("prefetch", True)),
            )
        return self._runtime

    def get_config_schema(self):
        return [
            {
                "key": "user_id",
                "description": "Memplex user id",
                "default": "hermes-user",
            },
            {
                "key": "project_path",
                "description": "Project path used for memory isolation",
                "default": os.getcwd(),
            },
        ]

    def save_config(self, values: dict, hermes_home: str) -> None:
        config_path = Path(hermes_home) / "plugins" / "memory" / "memplex" / "memplex.json"
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps(values, indent=2, ensure_ascii=False) + "\\n")

    def system_prompt_block(self) -> str:
        return "Memplex memory provider is active; relevant memories are injected per turn."

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        if session_id and session_id != self._session_id:
            self._session_id = session_id
            self._runtime = None
        return self._ensure_runtime().before_prompt(query).context

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        if not query:
            return
        if session_id and session_id != self._session_id:
            self._session_id = session_id
            self._runtime = None
        self._ensure_runtime().prefetch(query)

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        if session_id and session_id != self._session_id:
            self._session_id = session_id
            self._runtime = None

        def _sync() -> None:
            try:
                metadata = {"messages": messages} if messages else None
                self._ensure_runtime().after_response(
                    user_message=user_content,
                    assistant_message=assistant_content,
                    metadata=metadata,
                )
            except Exception as exc:
                logger.warning("Memplex sync failed: %s", exc)

        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)
        self._sync_thread = threading.Thread(target=_sync, daemon=True)
        self._sync_thread.start()

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        if not messages:
            return ""
        user = "\\n".join(str(m.get("content", "")) for m in messages if m.get("role") == "user")
        assistant = "\\n".join(
            str(m.get("content", "")) for m in messages if m.get("role") == "assistant"
        )
        if user or assistant:
            self.sync_turn(user, assistant, session_id=self._session_id, messages=messages)
        return ""

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        self.on_pre_compress(messages)

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": "memplex_search",
                "description": "Search local Memplex memories.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string"},
                        "top_k": {"type": "integer", "default": 5},
                    },
                    "required": ["query"],
                },
            },
            {
                "name": "memplex_conclude",
                "description": "Store a durable Memplex memory from text.",
                "parameters": {
                    "type": "object",
                    "properties": {"content": {"type": "string"}},
                    "required": ["content"],
                },
            },
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs) -> str:
        runtime = self._ensure_runtime()
        if tool_name == "memplex_search":
            recalled = runtime.before_prompt(args.get("query", ""))
            return json.dumps({"context": recalled.context, "total": recalled.total}, ensure_ascii=False)
        if tool_name == "memplex_conclude":
            runtime.capture_turn(args.get("content", ""), "Stored by Hermes memplex_conclude.")
            return json.dumps({"status": "stored"}, ensure_ascii=False)
        raise NotImplementedError(f"Unknown Memplex tool: {tool_name}")

    def shutdown(self) -> None:
        if self._sync_thread and self._sync_thread.is_alive():
            self._sync_thread.join(timeout=5.0)


def register(ctx) -> None:
    ctx.register_memory_provider(MemplexMemoryProvider())
'''
    )


def _strip_jsonc(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    lines = []
    for line in text.splitlines():
        in_string = False
        escaped = False
        cutoff = len(line)
        for idx, char in enumerate(line):
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
                continue
            if char == '"':
                in_string = not in_string
                continue
            if not in_string and line[idx : idx + 2] == "//":
                cutoff = idx
                break
        lines.append(line[:cutoff])
    return re.sub(r",(\s*[}\]])", r"\1", "\n".join(lines))


def _is_managed_payload(data: Any) -> bool:
    if not isinstance(data, dict):
        return False
    managed = data.get("managed")
    if isinstance(managed, dict) and managed.get("installer") == "memplex":
        return True
    config = data.get("config")
    if isinstance(config, dict):
        config_managed = config.get("managed")
        return isinstance(config_managed, dict) and config_managed.get("installer") == "memplex"
    return False


def _is_managed_openclaw_entry(entry: Any) -> bool:
    return _is_managed_payload(entry)


def _is_managed_openclaw_extension(extension_dir: Path) -> bool:
    for manifest_name in ("openclaw.plugin.json", "plugin.json"):
        manifest = extension_dir / manifest_name
        if manifest.exists() and _is_managed_json_file(manifest):
            return True
    return False


def _is_managed_hermes_plugin(plugin_dir: Path) -> bool:
    return _is_managed_json_file(plugin_dir / "memplex.json")


def _is_managed_json_file(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        return _is_managed_payload(_read_json(path))
    except Exception:
        return False


def _ignore_patterns(_dir: str, files: Iterable[str]) -> list[str]:
    return [name for name in files if name == "__pycache__" or name.endswith(".pyc")]


def _package_version() -> str:
    pyproject = Path(__file__).resolve().parents[2] / "pyproject.toml"
    if pyproject.exists():
        try:
            import tomllib

            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            project = data.get("project", {})
            if project.get("name") == "memplex" and project.get("version"):
                return str(project["version"])
        except Exception:
            pass

    from importlib.metadata import version as pkg_version

    try:
        return pkg_version("memplex")
    except Exception:
        return "3.2.7"


# ── Agent installer registry ─────────────────────────────────────────
# Populated at module load (after every per-agent installer/uninstaller
# function is defined above). Replaces the previous 4-way if/elif chain
# in ``_install_one`` / ``_uninstall_one``; adding a new agent host is
# now one entry here instead of editing two dispatch functions.
_INSTALLERS: dict[str, AgentInstallerSpec] = {
    "codex": AgentInstallerSpec(
        install=_install_codex,
        uninstall=_uninstall_codex,
        needs_identity=False,
    ),
    "claude-code": AgentInstallerSpec(
        install=_install_claude_code,
        uninstall=_uninstall_claude_code,
        needs_identity=False,
    ),
    "openclaw": AgentInstallerSpec(
        install=_install_openclaw,
        uninstall=_uninstall_openclaw,
        needs_identity=True,
    ),
    "hermes": AgentInstallerSpec(
        install=_install_hermes,
        uninstall=_uninstall_hermes,
        needs_identity=True,
    ),
}
