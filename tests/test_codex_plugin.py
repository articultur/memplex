"""Codex 原生插件生命周期与跨宿主互通契约。"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from memplex.adapters.agent_installer import install_agent
from memplex.adapters.agent_runtime import AgentMemoryRuntime
from memplex.adapters.runtime_status import read_runtime_status, runtime_status_path
from memplex.config import MemplexConfig
from memplex.service import MemplexService


def _service(storage_path):
    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(storage_path)
    return MemplexService(config=config)


def _codex_cli() -> Path | None:
    configured = os.environ.get("MEMPLEX_G008_CODEX_CLI")
    candidates = [
        Path(configured) if configured else None,
        Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
    ]
    return next((path for path in candidates if path is not None and path.is_file()), None)


def test_codex_real_cli_discovers_plugin_in_isolated_home(tmp_path):
    cli = _codex_cli()
    if cli is None:
        pytest.skip("real Codex CLI is unavailable")
    config_root = tmp_path / "codex"
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    (config_root / "config.toml").parent.mkdir(parents=True)
    (config_root / "config.toml").write_text("# isolated pre-state\n")
    install_agent(
        "codex",
        target_dir=config_root,
        user_id="alice",
        project_path=workspace,
    )

    result = subprocess.run(
        [str(cli), "plugin", "list", "--available", "--json"],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "HOME": str(home), "CODEX_HOME": str(config_root)},check=False
    
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    installed = {item["pluginId"]: item for item in payload["installed"]}
    assert installed["memplex@memplex"]["enabled"] is True
    assert installed["memplex@memplex"]["version"] == "3.3.1"


def _plugin_root(tmp_path, *, user_id="alice", project_path=None):
    host_root = tmp_path / "codex-home"
    host_root.mkdir(parents=True, exist_ok=True)
    root = host_root / "plugins" / "marketplaces" / "memplex" / "plugin"
    root.mkdir(parents=True)
    (root / "memplex-agent.json").write_text(
        json.dumps(
            {
                "agent": "codex",
                "user_id": user_id,
                "project_path": str(project_path or tmp_path),
                "python": sys.executable,
                "source_root": str(Path(__file__).resolve().parent.parent),
                "host_root": str(host_root),
                "managed": {
                    "by": "memplex",
                    "installer": "memplex",
                    "schema_version": 1,
                },
            }
        )
    )
    return root


def _run_hook(plugin_root, plugin_data, storage_path, payload):
    return subprocess.run(
        [sys.executable, "-m", "memplex.adapters.codex_plugin", "hook"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            "PLUGIN_ROOT": str(plugin_root),
            "PLUGIN_DATA": str(plugin_data),
            "MEMPLEX_STORAGE_BACKEND": "lite",
            "MEMPLEX_STORAGE_PATH": str(storage_path),
        },check=False
    
    )


def test_codex_managed_identity_cannot_be_overridden_by_hook_payload(
    tmp_path,
    monkeypatch,
):
    from memplex.adapters import codex_plugin

    workspace = tmp_path / "managed-workspace"
    attacker_workspace = tmp_path / "attacker-workspace"
    workspace.mkdir()
    attacker_workspace.mkdir()
    plugin_root = _plugin_root(tmp_path, user_id="managed-alice", project_path=workspace)
    monkeypatch.setenv("PLUGIN_ROOT", str(plugin_root))
    monkeypatch.setenv("MEMPLEX_USER_ID", "polluted-env-user")
    monkeypatch.setenv("MEMPLEX_PROJECT_ROOT", str(attacker_workspace))

    identity = codex_plugin._identity(
        {
            "user_id": "payload-user",
            "cwd": str(attacker_workspace),
            "project_path": str(attacker_workspace),
            "session_id": "host-session",
        }
    )

    assert identity["user_id"] == "managed-alice"
    assert identity["project_path"] == str(workspace.resolve())
    assert identity["session_id"] == "host-session"


def test_codex_real_recall_failure_persists_degraded_host_runtime_state(tmp_path, monkeypatch):
    """A swallowed Codex hook failure must remain visible to agent status."""
    from memplex.adapters import codex_plugin

    class BrokenRuntime:
        def before_prompt(self, _query):
            raise RuntimeError("Bearer codex-secret-must-not-persist")

    class Service:
        def stop(self):
            return None

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr(codex_plugin, "_runtime", lambda _identity: (BrokenRuntime(), Service()))
    with pytest.raises(RuntimeError, match="codex-secret"):
        codex_plugin._handle_recall(
            "UserPromptSubmit",
            {"session_id": "status-session", "prompt": "remember status"},
        )

    assert read_runtime_status(runtime_status_path(tmp_path), agent="codex") == {
        "reason": "runtime_operation_failed",
        "state": "degraded",
    }

    class HealthyRuntime:
        def before_prompt(self, _query):
            return type("Recalled", (), {"context": ""})()

    monkeypatch.setattr(codex_plugin, "_runtime", lambda _identity: (HealthyRuntime(), Service()))
    assert codex_plugin._handle_recall(
        "UserPromptSubmit",
        {"session_id": "status-session", "prompt": "remember status"},
    ) == {}
    assert read_runtime_status(runtime_status_path(tmp_path), agent="codex") == {
        "reason": None,
        "state": "healthy",
    }


def test_codex_runtime_status_uses_managed_identity_host_root(tmp_path, monkeypatch):
    """A non-default managed root wins over a polluted process CODEX_HOME."""
    from memplex.adapters import codex_plugin

    plugin_root = _plugin_root(tmp_path)
    managed_root = tmp_path / "codex-home"
    polluted_root = tmp_path / "polluted-codex-home"
    monkeypatch.setenv("PLUGIN_ROOT", str(plugin_root))
    monkeypatch.setenv("CODEX_HOME", str(polluted_root))

    assert codex_plugin._runtime_status_path() == runtime_status_path(managed_root)


def test_codex_adapter_rejects_identity_for_another_install_root(tmp_path, monkeypatch):
    """The Python adapter independently binds identity to the plugin install path."""

    from memplex.adapters import codex_plugin

    host_root = tmp_path / "host-a"
    other_root = tmp_path / "host-b"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    other_root.mkdir()
    install_agent("codex", target_dir=host_root, user_id="alice", project_path=workspace)
    plugin_root = host_root / "plugins" / "marketplaces" / "memplex" / "plugin"
    identity_path = plugin_root / "memplex-agent.json"
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    identity["host_root"] = str(other_root.resolve())
    identity_path.write_text(json.dumps(identity), encoding="utf-8")
    monkeypatch.setenv("PLUGIN_ROOT", str(plugin_root))

    with pytest.raises(ValueError, match="host_root.*reinstall required|reinstall required.*host_root"):
        codex_plugin._identity_config()


def test_codex_mcp_launcher_forces_managed_scope_and_preserves_host_session(
    tmp_path,
    monkeypatch,
):
    """MCP launcher must not let inherited process env widen a managed scope."""
    from memplex.adapters import codex_plugin
    from memplex.adapters.mcp_server import MCPServer

    workspace = tmp_path / "managed-workspace"
    attacker_workspace = tmp_path / "attacker-workspace"
    workspace.mkdir()
    attacker_workspace.mkdir()
    plugin_root = _plugin_root(tmp_path, user_id="managed-alice", project_path=workspace)
    monkeypatch.setenv("PLUGIN_ROOT", str(plugin_root))
    monkeypatch.setenv("MEMPLEX_AGENT_ID", "attacker-agent")
    monkeypatch.setenv("MEMPLEX_USER_ID", "attacker-user")
    monkeypatch.setenv("MEMPLEX_PROJECT_ROOT", str(attacker_workspace))
    monkeypatch.setenv("MEMPLEX_SESSION_ID", "host-trusted-session")
    captured: list[dict[str, str]] = []

    def capture_run(_self):
        captured.append(
            {
                key: os.environ[key]
                for key in (
                    "MEMPLEX_AGENT_ID",
                    "MEMPLEX_USER_ID",
                    "MEMPLEX_PROJECT_ROOT",
                    "MEMPLEX_SESSION_ID",
                )
            }
        )

    monkeypatch.setattr(MCPServer, "run", capture_run)
    assert codex_plugin._run_mcp() == 0

    assert captured == [
        {
            "MEMPLEX_AGENT_ID": "codex",
            "MEMPLEX_USER_ID": "managed-alice",
            "MEMPLEX_PROJECT_ROOT": str(workspace.resolve()),
            "MEMPLEX_SESSION_ID": "host-trusted-session",
        }
    ]


def test_codex_mcp_launcher_generates_stable_session_when_host_omits_one(
    tmp_path,
    monkeypatch,
):
    from memplex.adapters import codex_plugin
    from memplex.adapters.mcp_server import MCPServer

    workspace = tmp_path / "managed-workspace"
    workspace.mkdir()
    monkeypatch.setenv("PLUGIN_ROOT", str(_plugin_root(tmp_path, project_path=workspace)))
    # Register restoration points before the launcher installs its managed
    # identity and fallback session into the process environment.
    monkeypatch.setenv("MEMPLEX_AGENT_ID", "")
    monkeypatch.setenv("MEMPLEX_USER_ID", "")
    monkeypatch.setenv("MEMPLEX_PROJECT_ROOT", "")
    monkeypatch.setenv("MEMPLEX_SESSION_ID", "")
    monkeypatch.delenv("CODEX_SESSION_ID", raising=False)
    sessions: list[str] = []
    monkeypatch.setattr(MCPServer, "run", lambda _self: sessions.append(os.environ["MEMPLEX_SESSION_ID"]))

    codex_plugin._run_mcp()
    codex_plugin._run_mcp()

    assert sessions == [f"codex-{os.getpid()}", f"codex-{os.getpid()}"]


def test_codex_user_prompt_submit_recalls_claude_workspace_memory(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    storage_path = tmp_path / "memory.json"
    plugin_root = _plugin_root(tmp_path, project_path=workspace)
    plugin_data = tmp_path / "plugin-data"

    service = _service(storage_path)
    AgentMemoryRuntime(
        service=service,
        agent="claude-code",
        user_id="alice",
        session_id="claude-session",
        project_path=workspace,
    ).after_response("codex-recall-token", "Claude captured it")
    service.stop()

    result = _run_hook(
        plugin_root,
        plugin_data,
        storage_path,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "codex-session",
            "cwd": str(workspace),
            "prompt": "Where is codex-recall-token?",
        },
    )

    assert result.returncode == 0, result.stderr
    output = json.loads(result.stdout)
    hook_output = output["hookSpecificOutput"]
    assert hook_output["hookEventName"] == "UserPromptSubmit"
    assert "codex-recall-token" in hook_output["additionalContext"]


def test_codex_stop_capture_is_visible_to_claude_in_same_workspace(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    storage_path = tmp_path / "memory.json"
    plugin_root = _plugin_root(tmp_path, project_path=workspace)
    plugin_data = tmp_path / "plugin-data"
    session_id = "codex-session"

    prompt = _run_hook(
        plugin_root,
        plugin_data,
        storage_path,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": session_id,
            "cwd": str(workspace),
            "prompt": "Remember codex-capture-token for the next agent.",
        },
    )
    assert prompt.returncode == 0, prompt.stderr

    stop = _run_hook(
        plugin_root,
        plugin_data,
        storage_path,
        {
            "hook_event_name": "Stop",
            "session_id": session_id,
            "cwd": str(workspace),
            "last_assistant_message": "Stored codex-capture-token.",
        },
    )
    assert stop.returncode == 0, stop.stderr
    assert json.loads(stop.stdout) == {}

    service = _service(storage_path)
    recalled = AgentMemoryRuntime(
        service=service,
        agent="claude-code",
        user_id="alice",
        session_id="new-claude-session",
        project_path=workspace,
    ).before_prompt("codex-capture-token")
    service.stop()

    assert "codex-capture-token" in recalled.context


def test_codex_post_tool_use_captures_sanitized_observation(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    storage_path = tmp_path / "memory.json"
    plugin_root = _plugin_root(tmp_path, project_path=workspace)
    plugin_data = tmp_path / "plugin-data"

    result = _run_hook(
        plugin_root,
        plugin_data,
        storage_path,
        {
            "hook_event_name": "PostToolUse",
            "session_id": "codex-session",
            "cwd": str(workspace),
            "tool_name": "Bash",
            "tool_input": {"command": "printf codex-tool-token <private>secret-token</private>"},
            "tool_response": {"exit_code": 0},
        },
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {}

    service = _service(storage_path)
    recalled = AgentMemoryRuntime(
        service=service,
        agent="hermes",
        user_id="alice",
        session_id="hermes-session",
        project_path=workspace,
    ).before_prompt("codex-tool-token")
    leaked = AgentMemoryRuntime(
        service=service,
        agent="hermes",
        user_id="alice",
        session_id="hermes-session",
        project_path=workspace,
    ).before_prompt("secret-token")
    service.stop()

    assert "codex-tool-token" in recalled.context
    assert "secret-token" not in leaked.context


def test_codex_turn_state_is_partitioned_by_user_workspace_and_session(tmp_path):
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    storage_path = tmp_path / "memory.json"
    plugin_data = tmp_path / "plugin-data"
    plugin_root_a = _plugin_root(tmp_path / "alice", user_id="alice", project_path=workspace_a)
    plugin_root_b = _plugin_root(tmp_path / "bob", user_id="bob", project_path=workspace_b)
    shared_session = "reused-session-id"

    for plugin_root, workspace, token in (
        (plugin_root_a, workspace_a, "alice-partition-token"),
        (plugin_root_b, workspace_b, "bob-partition-token"),
    ):
        result = _run_hook(
            plugin_root,
            plugin_data,
            storage_path,
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": shared_session,
                "cwd": str(workspace),
                "prompt": f"Remember {token}.",
            },
        )
        assert result.returncode == 0, result.stderr

    stop = _run_hook(
        plugin_root_a,
        plugin_data,
        storage_path,
        {
            "hook_event_name": "Stop",
            "session_id": shared_session,
            "cwd": str(workspace_a),
            "last_assistant_message": "Captured Alice's turn.",
        },
    )
    assert stop.returncode == 0, stop.stderr

    service = _service(storage_path)
    recalled = AgentMemoryRuntime(
        service=service,
        agent="claude-code",
        user_id="alice",
        session_id="reader-session",
        project_path=workspace_a,
    ).before_prompt("alice-partition-token")
    service.stop()
    assert "alice-partition-token" in recalled.context
    assert "bob-partition-token" not in recalled.context


def test_codex_turn_state_is_written_private(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    storage_path = tmp_path / "memory.json"
    plugin_root = _plugin_root(tmp_path, project_path=workspace)
    plugin_data = tmp_path / "plugin-data"

    result = _run_hook(
        plugin_root,
        plugin_data,
        storage_path,
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "private-state-session",
            "cwd": str(workspace),
            "prompt": "private-turn-state-token",
        },
    )

    assert result.returncode == 0, result.stderr
    state_files = list((plugin_data / "turns").glob("*.json"))
    assert len(state_files) == 1
    assert stat.S_IMODE(state_files[0].stat().st_mode) == 0o600
