"""Contract tests for real agent hot-path integrations.

These tests intentionally model host behavior rather than only Memplex's
internal installer shape.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

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


def test_codex_hot_path_uses_managed_mcp_block(tmp_path):
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
    assert "[mcp_servers.memplex]" in config
    assert 'args = ["-m", "memplex.adapters.mcp_server"]' in config


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
    config = tomllib.loads((codex_home / "config.toml").read_text())
    server = config["mcp_servers"]["memplex"]

    cfg_path = tmp_path / "memplex.yaml"
    cfg_path.write_text(f"storage:\n  backend: lite\n  path: '{tmp_path / 'memory.json'}'\n")
    init_msg = json.dumps({"jsonrpc": "2.0", "method": "initialize", "params": {}, "id": 1})
    tools_msg = json.dumps({"jsonrpc": "2.0", "method": "tools/list", "params": {}, "id": 2})

    result = subprocess.run(
        [server["command"], *server["args"]],
        input=f"{init_msg}\n{tools_msg}\n",
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
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
    provider_root = hermes_home / "plugins" / "memory" / "memplex"
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
        assert "MEMPLEX_PYTHON" in first_hook["command"]

    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "MEMPLEX_PLUGIN_ROOT": str(PROJECT_ROOT / "memplex" / "_plugin"),
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
