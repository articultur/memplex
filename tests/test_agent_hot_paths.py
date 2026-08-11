"""Contract tests for real agent hot-path integrations.

These tests intentionally model host behavior rather than only Memplex's
internal installer shape.
"""

from __future__ import annotations

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

import importlib.util
import json
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from memplex.adapters.agent_installer import install_agent

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PACKAGED_HOOK_RUNNER = PROJECT_ROOT / "memplex" / "_plugin" / "scripts" / "hook-runner.py"
PACKAGED_HOOKS = PROJECT_ROOT / "memplex" / "_plugin" / "hooks" / "hooks.json"


def _run_memplex(args: list[str], *, timeout: int = 30, env: dict | None = None):
    return subprocess.run(
        [sys.executable, "-m", "memplex", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
    )


def test_codex_hot_path_uses_managed_native_plugin_block(tmp_path):
    codex_home = tmp_path / "codex"
    result = _run_memplex(
        [
            "--output",
            "json",
            "agent",
            "install",
            "--agent",
            "codex",
            "--target-dir",
            str(codex_home),
        ]
    )

    assert result.returncode == 0, result.stderr
    config = (codex_home / "config.toml").read_text()
    assert "# >>> memplex managed agent integration >>>" in config
    assert "[marketplaces.memplex]" in config
    assert '[plugins."memplex@memplex"]' in config
    assert "[mcp_servers.memplex]" not in config


def test_codex_hot_path_installs_native_plugin_with_identity(tmp_path):
    codex_home = tmp_path / "codex"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    result = _run_memplex(
        [
            "--output",
            "json",
            "agent",
            "install",
            "--agent",
            "codex",
            "--target-dir",
            str(codex_home),
            "--user-id",
            "alice",
            "--project-path",
            str(workspace),
        ]
    )

    assert result.returncode == 0, result.stderr
    config = (codex_home / "config.toml").read_text()
    assert "[marketplaces.memplex]" in config
    assert '[plugins."memplex@memplex"]' in config
    assert "enabled = true" in config

    marketplace_root = codex_home / "plugins" / "marketplaces" / "memplex"
    plugin_root = marketplace_root / "plugin"
    project_version = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())["project"][
        "version"
    ]
    cache_root = codex_home / "plugins" / "cache" / "memplex" / "memplex" / project_version
    marketplace = json.loads(
        (marketplace_root / ".agents" / "plugins" / "marketplace.json").read_text()
    )
    manifest = json.loads((plugin_root / ".codex-plugin" / "plugin.json").read_text())
    identity = json.loads((plugin_root / "memplex-agent.json").read_text())

    assert marketplace["name"] == "memplex"
    assert marketplace["plugins"][0]["source"]["path"] == "./plugin"
    assert manifest["mcpServers"] == "./.codex.mcp.json"
    assert manifest["hooks"] == "./hooks/hooks-codex.json"
    assert manifest["skills"] == "./skills/"
    assert identity["agent"] == "codex"
    assert identity["user_id"] == "alice"
    assert identity["project_path"] == str(workspace.resolve())
    assert identity["source_root"] == str(PROJECT_ROOT)
    assert identity["managed"]["by"] == "memplex"
    assert (cache_root / ".codex-plugin" / "plugin.json").exists()
    assert json.loads((cache_root / "memplex-agent.json").read_text()) == identity


def test_codex_native_plugin_mcp_initializes_over_stdio(tmp_path):
    codex_home = tmp_path / "codex"
    install = _run_memplex(
        [
            "--output",
            "json",
            "agent",
            "install",
            "--agent",
            "codex",
            "--target-dir",
            str(codex_home),
            "--user-id",
            "alice",
            "--project-path",
            str(tmp_path / "workspace"),
        ]
    )
    assert install.returncode == 0, install.stderr

    project_version = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())["project"][
        "version"
    ]
    plugin_root = codex_home / "plugins" / "cache" / "memplex" / "memplex" / project_version
    mcp = json.loads((plugin_root / ".codex.mcp.json").read_text())["mcpServers"]["memplex"]
    init_msg = json.dumps({"jsonrpc": "2.0", "method": "initialize", "params": {}, "id": 1})
    tools_msg = json.dumps({"jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": 2})

    result = subprocess.run(
        [mcp["command"], *mcp["args"]],
        cwd=plugin_root if mcp.get("cwd") == "." else None,
        input=f"{init_msg}\n{tools_msg}\n",
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **{key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
            "MEMPLEX_PYTHON": sys.executable,
            "MEMPLEX_STORAGE_BACKEND": "lite",
            "MEMPLEX_STORAGE_PATH": str(tmp_path / "memory.json"),
        },
    )

    assert result.returncode == 0, result.stderr
    responses = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert responses[0]["result"]["serverInfo"]["name"] == "memplex"
    assert {tool["name"] for tool in responses[1]["result"]["tools"]} >= {
        "memory_turn_begin",
        "memory_turn_end",
    }


def test_codex_native_mcp_inherits_installed_identity(tmp_path):
    from memplex.adapters.agent_runtime import AgentMemoryRuntime
    from memplex.config import MemplexConfig
    from memplex.service import MemplexService

    codex_home = tmp_path / "codex"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    storage_path = tmp_path / "memory.json"
    install = _run_memplex(
        [
            "agent",
            "install",
            "--agent",
            "codex",
            "--target-dir",
            str(codex_home),
            "--user-id",
            "alice",
            "--project-path",
            str(workspace),
        ]
    )
    assert install.returncode == 0, install.stderr

    project_version = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())["project"][
        "version"
    ]
    plugin_root = codex_home / "plugins" / "cache" / "memplex" / "memplex" / project_version
    mcp = json.loads((plugin_root / ".codex.mcp.json").read_text())["mcpServers"]["memplex"]
    capture = json.dumps(
        {
            "jsonrpc": "2.0",
            "method": "tools/call",
            "params": {
                "name": "memory_turn_end",
                "arguments": {
                    "agent": "codex",
                    "user_message": "codex-mcp-identity-token",
                    "assistant_message": "captured",
                },
            },
            "id": 1,
        }
    )
    result = subprocess.run(
        [mcp["command"], *mcp["args"]],
        cwd=plugin_root,
        input=capture + "\n",
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **{
                key: value
                for key, value in os.environ.items()
                if key not in {"PLUGIN_ROOT", "PYTHONPATH"}
            },
            "MEMPLEX_PYTHON": sys.executable,
            "MEMPLEX_STORAGE_BACKEND": "lite",
            "MEMPLEX_STORAGE_PATH": str(storage_path),
        },
    )
    assert result.returncode == 0, result.stderr

    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(storage_path)
    service = MemplexService(config=config)
    recalled = AgentMemoryRuntime(
        service=service,
        agent="claude-code",
        user_id="alice",
        session_id="claude-session",
        project_path=workspace,
    ).before_prompt("codex-mcp-identity-token")
    service.stop()

    assert "codex-mcp-identity-token" in recalled.context


def test_codex_native_plugin_uninstall_restores_user_config(tmp_path):
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    config_path = codex_home / "config.toml"
    original = 'model = "gpt-5.5"\n\n[projects."/repo/custom"]\ntrust_level = "trusted"\n'
    config_path.write_text(original)

    install = _run_memplex(
        [
            "agent",
            "install",
            "--agent",
            "codex",
            "--target-dir",
            str(codex_home),
            "--user-id",
            "alice",
            "--project-path",
            str(tmp_path / "workspace"),
        ]
    )
    assert install.returncode == 0, install.stderr

    uninstall = _run_memplex(
        [
            "agent",
            "uninstall",
            "--agent",
            "codex",
            "--target-dir",
            str(codex_home),
        ]
    )

    assert uninstall.returncode == 0, uninstall.stderr
    assert config_path.read_text() == original
    assert not (codex_home / "plugins" / "marketplaces" / "memplex").exists()
    assert not (codex_home / "plugins" / "cache" / "memplex").exists()


def test_top_level_setup_auto_detects_agent_without_nested_command(tmp_path):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    result = _run_memplex(
        [
            "--output",
            "json",
            "setup",
            "--dry-run",
            "--project-path",
            str(PROJECT_ROOT),
        ],
        env={**os.environ, "HOME": str(home), "PATH": "/usr/bin:/bin"},
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload[0]["agent"] == "codex"
    assert payload[0]["action"] == "install"
    assert payload[0]["status"] == "planned"


def test_top_level_install_and_stepup_aliases(tmp_path):
    for command in ("install", "stepup"):
        result = _run_memplex(
            [
                "--output",
                "json",
                command,
                "--agent",
                "codex",
                "--target-dir",
                str(tmp_path / command),
                "--dry-run",
            ]
        )
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload[0]["agent"] == "codex"
        assert payload[0]["action"] == "install"


def test_top_level_uninstall_alias(tmp_path):
    result = _run_memplex(
        [
            "--output",
            "json",
            "uninstall",
            "--agent",
            "codex",
            "--target-dir",
            str(tmp_path / "codex"),
            "--dry-run",
        ]
    )

    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload[0]["agent"] == "codex"
    assert payload[0]["action"] == "uninstall"


def test_mcp_hot_path_initializes_over_stdio(tmp_path):
    codex_home = tmp_path / "codex"
    install = _run_memplex(
        [
            "--output",
            "json",
            "agent",
            "install",
            "--agent",
            "codex",
            "--target-dir",
            str(codex_home),
        ]
    )
    assert install.returncode == 0, install.stderr
    plugin_root = codex_home / "plugins" / "marketplaces" / "memplex" / "plugin"
    server = json.loads((plugin_root / ".codex.mcp.json").read_text())["mcpServers"]["memplex"]

    cfg_path = tmp_path / "memplex.yaml"
    cfg_path.write_text(f"storage:\n  backend: lite\n  path: '{tmp_path / 'memory.json'}'\n")
    init_msg = json.dumps({"jsonrpc": "2.0", "method": "initialize", "params": {}, "id": 1})
    tools_msg = json.dumps({"jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": 2})

    result = subprocess.run(
        [server["command"], *server["args"]],
        cwd=plugin_root if server.get("cwd") == "." else None,
        input=f"{init_msg}\n{tools_msg}\n",
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            "MEMPLEX_PYTHON": sys.executable,
            "MEMPLEX_STORAGE_BACKEND": "lite",
            "MEMPLEX_CONFIG": str(cfg_path),
        },
    )

    assert result.returncode == 0, result.stderr
    responses = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    assert responses[0]["result"]["serverInfo"]["name"] == "memplex"
    tools = responses[1]["result"]["tools"]
    assert {tool["name"] for tool in tools} >= {
        "memory_agent_manifest",
        "memory_turn_begin",
        "memory_turn_end",
    }


def test_openclaw_hot_path_accepts_jsonc_config(tmp_path):
    openclaw_home = tmp_path / "openclaw"
    openclaw_home.mkdir()
    (openclaw_home / "openclaw.json").write_text(
        '{\n  "plugins": {\n    "slots": {\n      "memory": "memory-lancedb-pro",\n    }\n  }\n}\n'
    )

    result = _run_memplex(
        [
            "--output",
            "json",
            "agent",
            "install",
            "--agent",
            "openclaw",
            "--target-dir",
            str(openclaw_home),
            "--project-path",
            str(PROJECT_ROOT),
        ]
    )

    assert result.returncode == 0, result.stderr
    assert (openclaw_home / "extensions" / "memplex" / "openclaw.plugin.json").exists()


def test_hermes_hot_path_installs_memory_provider_plugin(tmp_path):
    hermes_home = tmp_path / "hermes"
    result = _run_memplex(
        [
            "--output",
            "json",
            "agent",
            "install",
            "--agent",
            "hermes",
            "--target-dir",
            str(hermes_home),
            "--project-path",
            str(PROJECT_ROOT),
        ]
    )

    assert result.returncode == 0, result.stderr
    provider_root = hermes_home / "plugins" / "memplex"
    plugin_yaml = provider_root / "plugin.yaml"
    assert plugin_yaml.exists()
    project_version = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text())["project"][
        "version"
    ]
    assert f"version: {project_version}" in plugin_yaml.read_text()
    assert (provider_root / "__init__.py").exists()

    agent_pkg = tmp_path / "agent"
    agent_pkg.mkdir()
    (agent_pkg / "__init__.py").write_text("")
    (agent_pkg / "memory_provider.py").write_text("class MemoryProvider:\n    pass\n")
    sys.path.insert(0, str(tmp_path))
    try:
        spec = importlib.util.spec_from_file_location(
            "hermes_memplex_provider",
            provider_root / "__init__.py",
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        class Registry:
            def __init__(self):
                self.providers = []

            def register_memory_provider(self, provider):
                self.providers.append(provider)

        registry = Registry()
        module.register(registry)
        assert registry.providers[0].name == "memplex"
    finally:
        sys.path.remove(str(tmp_path))


def test_claude_hook_manifest_commands_are_implemented():
    for command in ("setup", "prompt-submit", "file-context", "summarize"):
        result = subprocess.run(
            [sys.executable, str(PACKAGED_HOOK_RUNNER), command],
            capture_output=True,
            text=True,
            timeout=30,
            env={"MEMPLEX_PROJECT_ROOT": str(PROJECT_ROOT)},
        )
        assert result.returncode == 0, result.stderr


def test_claude_hook_manifest_commands_use_configured_python(tmp_path):
    manifest = json.loads(PACKAGED_HOOKS.read_text())
    command_by_event = {}
    for event, entries in manifest["hooks"].items():
        first_hook = entries[0]["hooks"][0]
        command_by_event[event] = first_hook["command"]
        assert "; python " not in first_hook["command"]
        assert "claude-hook.sh" in first_hook["command"]

    launcher = (PROJECT_ROOT / "memplex" / "_plugin" / "scripts" / "claude-hook.sh").read_text()
    assert "MEMPLEX_PYTHON" in launcher

    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "CLAUDE_PLUGIN_ROOT": str(PROJECT_ROOT / "memplex" / "_plugin"),
        "MEMPLEX_PROJECT_ROOT": str(PROJECT_ROOT),
        "MEMPLEX_PYTHON": sys.executable,
        "MEMPLEX_STORAGE_BACKEND": "lite",
        "MEMPLEX_STORAGE_PATH": str(tmp_path / "manifest-memory"),
        "MEMPLEX_OBS_RATE_FILE": str(tmp_path / "manifest-rate"),
    }
    for event in ("Setup", "UserPromptSubmit", "PreToolUse", "PostToolUse", "Stop"):
        result = subprocess.run(
            ["bash", "-lc", command_by_event[event]],
            input='{"tool_name":"Read","tool_input":{"file_path":"/tmp/manifest.py"}}',
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
        assert result.returncode == 0, f"{event}: {result.stderr}"


# Files that must stay byte-identical between the source plugin tree (plugin/)
# and the packaged copy (memplex/_plugin/). plugin/ is the single source of truth.
SYNCED_PLUGIN_FILES = [
    ".mcp.json",
    ".codex.mcp.json",
    ".claude-plugin/plugin.json",
    ".codex-plugin/plugin.json",
    "hooks/hooks.json",
    "hooks/hooks-codex.json",
    "scripts/codex-plugin.sh",
    "scripts/claude-hook.sh",
    "scripts/hook-runner.py",
    "scripts/mcp-server.sh",
    "skills/mem-explore/SKILL.md",
    "skills/mem-manage/SKILL.md",
    "skills/mem-search/SKILL.md",
    "skills/mem-turn/SKILL.md",
    "skills/mem-write/SKILL.md",
]


def test_packaged_plugin_tree_matches_source():
    source_root = PROJECT_ROOT / "plugin"
    packaged_root = PROJECT_ROOT / "memplex" / "_plugin"
    for rel in SYNCED_PLUGIN_FILES:
        source = source_root / rel
        packaged = packaged_root / rel
        assert source.exists(), f"missing source file: {rel}"
        assert packaged.exists(), f"missing packaged file: {rel}"
        assert packaged.read_bytes() == source.read_bytes(), f"plugin trees drifted: {rel}"


def test_packaged_plugin_manifest_declares_full_capabilities():
    manifest = json.loads(
        (PROJECT_ROOT / "memplex" / "_plugin" / ".claude-plugin" / "plugin.json").read_text()
    )
    assert manifest["skills"] == "./skills/"
    assert manifest["mcpServers"] == "./.mcp.json"
    # Claude loads hooks/hooks.json by convention. Declaring it again causes
    # current Claude Code releases to reject the installed plugin as a
    # duplicate hooks file.
    assert "hooks" not in manifest
    assert "interface" not in manifest
    assert manifest["repository"] == "https://github.com/articultur/memplex"


def test_claude_real_cli_strictly_validates_installed_plugin(tmp_path):
    cli_value = os.environ.get("MEMPLEX_G008_CLAUDE_CLI") or shutil.which("claude")
    if not cli_value:
        pytest.skip("real Claude Code CLI is unavailable")
    config_root = tmp_path / "claude"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    install_agent(
        "claude-code",
        target_dir=config_root,
        user_id="alice",
        project_path=workspace,
    )
    plugin_root = config_root / "plugins" / "marketplaces" / "articultur" / "plugin"

    result = subprocess.run(
        [cli_value, "plugin", "validate", "--strict", str(plugin_root)],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            "HOME": str(tmp_path / "home"),
            "CLAUDE_CONFIG_DIR": str(config_root),
        },
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "Validation passed" in result.stdout

    listed = subprocess.run(
        [cli_value, "plugin", "list", "--available", "--json"],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            "HOME": str(tmp_path / "home"),
            "CLAUDE_CONFIG_DIR": str(config_root),
        },
    )

    assert listed.returncode == 0, (listed.stdout, listed.stderr)
    inventory = json.loads(listed.stdout)
    installed = inventory["installed"]
    memplex = next(
        item for item in installed if item.get("id") == "memplex@articultur"
    )
    assert memplex["enabled"] is True
    assert memplex.get("errors", []) == []


def test_agent_hot_path_dedups_consecutive_identical_captures(tmp_path):
    """PostToolUse + Stop hooks firing for the same turn must not double-write.

    The dedup key comes from the shared policy (memplex.core.hooks.policy)
    and is per-runtime-instance, so distinct sessions/users are unaffected.
    """
    from memplex.adapters.agent_runtime import AgentMemoryRuntime
    from memplex.config import MemplexConfig
    from memplex.service import MemplexService

    cfg = MemplexConfig()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(tmp_path / "memory.json")
    service = MemplexService(config=cfg)
    runtime = AgentMemoryRuntime(
        service=service,
        agent="codex",
        user_id="user-1",
        session_id="s1",
        project_path="/repo/a",
    )

    runtime.after_response(
        user_message="Remember dedup-token-alpha.", assistant_message="Captured."
    )
    assert len(service.store.list_observations(limit=100)) == 1

    # Identical consecutive turn is dropped
    runtime.after_response(
        user_message="Remember dedup-token-alpha.", assistant_message="Captured."
    )
    assert len(service.store.list_observations(limit=100)) == 1

    # A different turn still captures
    runtime.after_response(user_message="Remember dedup-token-beta.", assistant_message="Captured.")
    assert len(service.store.list_observations(limit=100)) == 2
