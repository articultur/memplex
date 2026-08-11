"""OpenClaw native plugin contract and cross-host memory integration tests."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from memplex.adapters.agent_installer import _strip_jsonc, install_agent, uninstall_agent
from memplex.adapters.agent_runtime import AgentMemoryRuntime
from memplex.adapters.jsonc_edit import set_jsonc_path
from memplex.config import MemplexConfig
from memplex.service import MemplexService

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _storage_env(storage_path: Path) -> dict[str, str]:
    return {
        **os.environ,
        "MEMPLEX_STORAGE_BACKEND": "lite",
        "MEMPLEX_STORAGE_PATH": str(storage_path),
        "MEMPLEX_EMBEDDING_CONTEXTUAL_RETRIEVAL": "false",
        "MEMPLEX_LLM_QUERY_ENHANCEMENT": "false",
    }


def _run_bridge(action: str, payload: dict, storage_path: Path) -> dict:
    result = subprocess.run(
        [sys.executable, "-m", "memplex.adapters.openclaw_plugin", action],
        cwd=PROJECT_ROOT,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        env=_storage_env(storage_path),
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _service(storage_path: Path) -> MemplexService:
    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(storage_path)
    return MemplexService(config=config)


def test_openclaw_install_writes_a_loadable_native_plugin(tmp_path):
    target = tmp_path / "openclaw"
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    install_agent(
        "openclaw",
        target_dir=target,
        user_id="alice",
        project_path=workspace,
    )

    extension = target / "extensions" / "memplex"
    manifest = json.loads((extension / "openclaw.plugin.json").read_text())
    package = json.loads((extension / "package.json").read_text())
    identity = json.loads((extension / "memplex-agent.json").read_text())

    assert manifest["kind"] == "memory"
    assert manifest["activation"]["onStartup"] is True
    assert set(manifest["contracts"]["tools"]) == {"memory_recall", "memory_store"}
    assert package["type"] == "module"
    assert package["openclaw"]["extensions"] == ["./index.js"]
    assert identity["user_id"] == "alice"
    assert identity["project_path"] == str(workspace.resolve())
    assert identity["source_root"] == str(PROJECT_ROOT)
    assert stat.S_IMODE((extension / ".memplex-install-state.json").stat().st_mode) == 0o600

    node_script = r"""
import { pathToFileURL } from "node:url";
const plugin = (await import(pathToFileURL(process.env.MEMPLEX_PLUGIN_ENTRY).href)).default;
const hooks = [];
const tools = [];
plugin.register({
  pluginConfig: {},
  logger: { debug() {}, info() {}, warn() {}, error() {} },
  on(name, handler) { hooks.push({ name, handler: typeof handler }); },
  registerTool(factory) {
    const tool = typeof factory === "function" ? factory({
      sessionId: "session-a",
      sessionKey: "key-a",
      workspaceDir: process.cwd(),
    }) : factory;
    tools.push(tool.name);
  },
});
process.stdout.write(JSON.stringify({ hooks, tools }));
"""
    registered = subprocess.run(
        ["node", "--input-type=module", "-e", node_script],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "MEMPLEX_PLUGIN_ENTRY": str(extension / "index.js")},
    )
    assert registered.returncode == 0, registered.stderr
    contract = json.loads(registered.stdout)
    assert {item["name"] for item in contract["hooks"]} == {
        "before_prompt_build",
        "agent_end",
        "session_end",
    }
    assert set(contract["tools"]) == {"memory_recall", "memory_store"}


def test_openclaw_bridge_uses_dynamic_workspace_and_shares_with_other_hosts(tmp_path):
    storage_path = tmp_path / "memory.json"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    token = "openclaw-shared-workspace-token-7429"
    payload = {
        "config": {
            "userId": "alice",
            "projectPath": str(tmp_path / "stale-install-workspace"),
        },
        "context": {
            "sessionId": "openclaw-session",
            "sessionKey": "agent:main:openclaw-session",
            "workspaceDir": str(workspace),
            "agentId": "main",
        },
        "event": {
            "success": True,
            "messages": [
                {"role": "user", "content": f"Remember {token} <private>omit-me</private>"},
                {"role": "assistant", "content": "I will remember that workspace fact."},
            ],
        },
    }

    captured = _run_bridge("capture", payload, storage_path)
    assert captured["captured"] is True
    assert captured["identity"]["project_path"] == str(workspace.resolve())

    service = _service(storage_path)
    try:
        for agent in ("codex", "claude-code"):
            recalled = AgentMemoryRuntime(
                service=service,
                agent=agent,
                user_id="alice",
                session_id=f"{agent}-session",
                project_path=workspace,
            ).before_prompt(token)
            assert token in recalled.context
            assert "omit-me" not in recalled.context

        isolated = AgentMemoryRuntime(
            service=service,
            agent="codex",
            user_id="bob",
            session_id="other-user",
            project_path=workspace,
        ).before_prompt(token)
        assert token not in isolated.context
    finally:
        service.stop()

    recalled = _run_bridge(
        "recall",
        {
            "config": payload["config"],
            "context": {
                "sessionId": "openclaw-session-2",
                "workspaceDir": str(workspace),
            },
            "event": {"prompt": token, "messages": []},
        },
        storage_path,
    )
    assert token in recalled["prependContext"]


def test_openclaw_native_hooks_capture_then_recall_through_the_python_bridge(tmp_path):
    target = tmp_path / "openclaw"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    storage_path = tmp_path / "memory.json"
    token = "openclaw-native-hook-roundtrip-9913"
    install_agent(
        "openclaw",
        target_dir=target,
        user_id="alice",
        project_path=workspace,
    )
    extension = target / "extensions" / "memplex"
    entry_config = json.loads((target / "openclaw.json").read_text())["plugins"]["entries"][
        "memplex"
    ]["config"]
    entry_config["userId"] = "forged-plugin-user"
    entry_config["projectPath"] = str(tmp_path / "forged-plugin-workspace")

    node_script = r"""
import { pathToFileURL } from "node:url";
const plugin = (await import(pathToFileURL(process.env.MEMPLEX_PLUGIN_ENTRY).href)).default;
const hooks = new Map();
plugin.register({
  pluginConfig: JSON.parse(process.env.MEMPLEX_PLUGIN_CONFIG),
  logger: { debug() {}, info() {}, warn(message) { throw new Error(message); }, error() {} },
  on(name, handler) { hooks.set(name, handler); },
  registerTool() {},
});
const context = {
  agentId: "main",
  sessionId: "native-session-a",
  sessionKey: "agent:main:native-session-a",
  workspaceDir: process.env.MEMPLEX_WORKSPACE,
};
await hooks.get("agent_end")({
  success: true,
  messages: [
    { role: "user", content: `Remember ${process.env.MEMPLEX_TEST_TOKEN}` },
    { role: "assistant", content: "Captured through the native hook." },
  ],
}, context);
const recalled = await hooks.get("before_prompt_build")({
  prompt: process.env.MEMPLEX_TEST_TOKEN,
  messages: [],
}, { ...context, sessionId: "native-session-b", sessionKey: "agent:main:native-session-b" });
process.stdout.write(JSON.stringify(recalled));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", node_script],
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=45,
        env={
            **_storage_env(storage_path),
            "MEMPLEX_PLUGIN_ENTRY": str(extension / "index.js"),
            "MEMPLEX_PLUGIN_CONFIG": json.dumps(entry_config),
            "MEMPLEX_WORKSPACE": str(workspace),
            "MEMPLEX_TEST_TOKEN": token,
        },
    )

    assert result.returncode == 0, result.stderr
    assert token in json.loads(result.stdout)["prependContext"]
    service = _service(storage_path)
    try:
        stored = service.store.list_functions(limit=10)
        assert stored
        assert {item.owner for item in stored} == {"alice"}
        assert {item.owner_subject_id for item in stored} == {"alice"}
    finally:
        service.stop()


def test_openclaw_jsonc_is_preserved_and_uninstall_restores_exact_config(tmp_path):
    target = tmp_path / "openclaw"
    target.mkdir()
    config_path = target / "openclaw.json"
    original = (
        "{\n"
        "  // keep this operator comment\n"
        '  "plugins": {\n'
        '    "slots": {"memory": "legacy-memory",},\n'
        '    "entries": {"legacy-memory": {"enabled": true,},},\n'
        '    "customFlag": true,\n'
        "  },\n"
        '  "customRoot": {"keep": true,},\n'
        "}\n"
    )
    config_path.write_text(original)
    config_path.chmod(0o640)

    install_agent(
        "openclaw",
        target_dir=target,
        user_id="alice",
        project_path=tmp_path / "workspace",
    )

    installed = config_path.read_text()
    assert "// keep this operator comment" in installed
    assert '"customFlag": true' in installed
    assert '"customRoot": {"keep": true,}' in installed
    assert '"memory": "memplex"' in installed
    assert '"memplex"' in installed
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o640

    uninstall_agent("openclaw", target_dir=target)

    assert config_path.read_text() == original
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o640
    assert not (target / "extensions" / "memplex").exists()


def test_openclaw_uninstall_keeps_operator_edits_made_after_install(tmp_path):
    target = tmp_path / "openclaw"
    target.mkdir()
    config_path = target / "openclaw.json"
    config_path.write_text(
        "{\n"
        "  // retained after managed fallback removal\n"
        '  "plugins": {"slots": {"memory": "legacy-memory"}},\n'
        "}\n"
    )
    install_agent(
        "openclaw",
        target_dir=target,
        user_id="alice",
        project_path=tmp_path / "workspace",
    )
    operator_edited = set_jsonc_path(
        config_path.read_text(),
        ("operatorChange",),
        {"enabled": True},
    )
    config_path.write_text(operator_edited)

    uninstall_agent("openclaw", target_dir=target)

    restored = config_path.read_text()
    parsed = json.loads(_strip_jsonc(restored))
    assert "// retained after managed fallback removal" in restored
    assert parsed["operatorChange"] == {"enabled": True}
    assert parsed["plugins"]["slots"]["memory"] == "legacy-memory"
    assert "memplex" not in parsed["plugins"].get("entries", {})
    assert "memplex" not in parsed["plugins"].get("allow", [])


@pytest.mark.skipif(shutil.which("openclaw") is None, reason="OpenClaw CLI is not installed")
def test_openclaw_cli_loads_memplex_runtime_from_an_isolated_profile(tmp_path):
    target = tmp_path / "openclaw"
    isolated_home = tmp_path / "home"
    isolated_home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    install_agent(
        "openclaw",
        target_dir=target,
        user_id="alice",
        project_path=workspace,
    )

    env = {
        **os.environ,
        "HOME": str(isolated_home),
        "OPENCLAW_HOME": str(isolated_home),
        "OPENCLAW_STATE_DIR": str(target),
        "OPENCLAW_CONFIG_PATH": str(target / "openclaw.json"),
    }
    inspected = subprocess.run(
        ["openclaw", "plugins", "inspect", "memplex", "--runtime", "--json"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )

    assert inspected.returncode == 0, inspected.stderr
    payload = json.loads(inspected.stdout)
    plugin = payload["plugin"]
    assert plugin["id"] == "memplex"
    assert plugin["status"] == "loaded"
    assert plugin["kind"] == "memory"
    assert plugin["memorySlotSelected"] is True
    assert set(plugin["toolNames"]) == {"memory_recall", "memory_store"}
    assert {item["name"] for item in payload["typedHooks"]} == {
        "before_prompt_build",
        "agent_end",
        "session_end",
    }
    assert payload["diagnostics"] == []
