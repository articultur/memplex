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
from memplex.adapters.runtime_status import read_runtime_status, runtime_status_path
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


def _openclaw_cli_path() -> str:
    """Resolve the exact CLI selected by the G008 verifier, when provided."""
    configured = os.environ.get("MEMPLEX_G008_OPENCLAW_CLI")
    if configured:
        path = Path(configured)
        if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
            pytest.fail("configured OpenClaw CLI is not an executable absolute file")
        return str(path)
    discovered = shutil.which("openclaw")
    if discovered is None:
        pytest.skip("OpenClaw CLI is not installed")
    return discovered


def test_g008_openclaw_cli_path_cannot_be_replaced_by_path_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = tmp_path / "configured" / "openclaw"
    configured.parent.mkdir()
    configured.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    configured.chmod(0o755)
    lookalike = tmp_path / "lookalike" / "openclaw"
    lookalike.parent.mkdir()
    lookalike.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    lookalike.chmod(0o755)
    monkeypatch.setenv("PATH", str(lookalike.parent))
    monkeypatch.setenv("MEMPLEX_G008_OPENCLAW_CLI", str(configured))

    assert _openclaw_cli_path() == str(configured)


def test_openclaw_real_recall_failure_persists_degraded_host_runtime_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bridge failure remains operator-visible until the same operation succeeds."""
    from memplex.adapters import openclaw_plugin

    class BrokenRuntime:
        def before_prompt(self, _query: str):
            raise RuntimeError("Bearer openclaw-secret-must-not-persist")

    class HealthyRuntime:
        def before_prompt(self, _query: str):
            return type(
                "Recalled",
                (),
                {"context": "", "source": "none", "tokens_used": 0, "total": 0},
            )()

    class Service:
        def stop(self) -> None:
            return None

    root = tmp_path / "openclaw"
    monkeypatch.setenv("OPENCLAW_CONFIG_DIR", str(root))
    monkeypatch.setattr(
        openclaw_plugin,
        "_runtime",
        lambda _payload: (BrokenRuntime(), Service(), openclaw_plugin._identity(_payload)),
    )
    payload = {"event": {"prompt": "remember status"}}
    with pytest.raises(RuntimeError, match="openclaw-secret"):
        openclaw_plugin.recall(payload)
    assert read_runtime_status(runtime_status_path(root), agent="openclaw") == {
        "reason": "runtime_operation_failed",
        "state": "degraded",
    }

    monkeypatch.setattr(
        openclaw_plugin,
        "_runtime",
        lambda _payload: (HealthyRuntime(), Service(), openclaw_plugin._identity(_payload)),
    )
    assert openclaw_plugin.recall(payload)["total"] == 0
    assert read_runtime_status(runtime_status_path(root), agent="openclaw") == {
        "reason": None,
        "state": "healthy",
    }


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
    assert identity["host_root"] == str(target.resolve())
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


def test_openclaw_generated_launcher_exports_identity_host_root(tmp_path):
    """The bridge process must use the installed host root, not ambient OpenClaw state."""

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    target = tmp_path / "openclaw"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    install_agent("openclaw", target_dir=target, user_id="alice", project_path=workspace)
    extension = target / "extensions" / "memplex"
    identity_path = extension / "memplex-agent.json"
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    probe = tmp_path / "probe-python"
    probe.write_text(
        '#!/bin/sh\nprintf "%s" "$OPENCLAW_CONFIG_DIR" > "$MEMPLEX_TEST_HOST_ROOT"\nprintf "{}"\n',
        encoding="utf-8",
    )
    probe.chmod(0o755)
    identity["python"] = str(probe)
    identity_path.write_text(json.dumps(identity), encoding="utf-8")
    observed = tmp_path / "observed-host-root"
    runner = r"""
import { pathToFileURL } from "node:url";
const plugin = (await import(pathToFileURL(process.env.MEMPLEX_PLUGIN_ENTRY).href)).default;
let recall;
plugin.register({
  pluginConfig: {},
  logger: { debug() {}, info() {}, warn(message) { throw new Error(message); }, error() {} },
  on(name, handler) { if (name === "before_prompt_build") recall = handler; },
  registerTool() {},
});
await recall({ prompt: "identity root probe" }, {});
"""
    result = subprocess.run(
        [node, "--input-type=module", "-e", runner],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            "OPENCLAW_CONFIG_DIR": str(tmp_path / "ambient-openclaw"),
            "MEMPLEX_PLUGIN_ENTRY": str(extension / "index.js"),
            "MEMPLEX_TEST_HOST_ROOT": str(observed),
        },
    )

    assert result.returncode == 0, result.stderr
    assert observed.read_text(encoding="utf-8") == str(target.resolve())


def test_openclaw_generated_launcher_rejects_identity_for_another_host_root(tmp_path):
    """A copied identity cannot redirect runtime health and config into host B."""

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    target = tmp_path / "host-a"
    other_target = tmp_path / "host-b"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    other_target.mkdir()
    install_agent("openclaw", target_dir=target, user_id="alice", project_path=workspace)
    extension = target / "extensions" / "memplex"
    identity_path = extension / "memplex-agent.json"
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    identity["host_root"] = str(other_target.resolve())
    identity_path.write_text(json.dumps(identity), encoding="utf-8")

    result = subprocess.run(
        [
            node,
            "--input-type=module",
            "-e",
            'await import(new URL("file://" + process.env.MEMPLEX_PLUGIN_ENTRY));',
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "MEMPLEX_PLUGIN_ENTRY": str(extension / "index.js")},
    )

    assert result.returncode != 0
    assert "host_root" in result.stderr
    assert "reinstall required" in result.stderr


def test_openclaw_python_adapter_rechecks_managed_plugin_root(tmp_path, monkeypatch):
    """The Python bridge cannot be invoked with a copied host-B identity either."""

    from memplex.adapters import openclaw_plugin

    target = tmp_path / "host-a"
    other_target = tmp_path / "host-b"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    other_target.mkdir()
    install_agent("openclaw", target_dir=target, user_id="alice", project_path=workspace)
    extension = target / "extensions" / "memplex"
    identity_path = extension / "memplex-agent.json"
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    identity["host_root"] = str(other_target.resolve())
    identity_path.write_text(json.dumps(identity), encoding="utf-8")
    monkeypatch.setenv("MEMPLEX_PLUGIN_ROOT", str(extension))
    monkeypatch.setenv("OPENCLAW_CONFIG_DIR", str(target.resolve()))

    with pytest.raises(ValueError, match="host_root.*reinstall required|reinstall required.*host_root"):
        openclaw_plugin._identity(
            {
                "config": {
                    "hostRoot": str(target.resolve()),
                    "pluginRoot": str(extension.resolve()),
                }
            }
        )


@pytest.mark.parametrize(
    "damage",
    [
        "missing",
        "duplicate",
        "extra",
        "wrong-agent",
        "weak-type",
        "weak-string",
        "missing-python",
    ],
)
def test_openclaw_generated_launcher_rejects_damaged_identity(tmp_path, damage):
    """The native JS entry must fail before registering hooks for a damaged identity."""

    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is not installed")
    target = tmp_path / "openclaw"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    install_agent("openclaw", target_dir=target, user_id="alice", project_path=workspace)
    extension = target / "extensions" / "memplex"
    identity_path = extension / "memplex-agent.json"
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    if damage == "missing":
        identity_path.unlink()
    elif damage == "duplicate":
        raw = json.dumps(identity).replace(
            '"agent": "openclaw"',
            '"agent": "openclaw", "agent": "openclaw"',
            1,
        )
        identity_path.write_text(raw, encoding="utf-8")
    else:
        if damage == "extra":
            identity["unexpected"] = True
        elif damage == "wrong-agent":
            identity["agent"] = "hermes"
        elif damage == "weak-type":
            identity["user_id"] = 7
        elif damage == "weak-string":
            identity["user_id"] = " alice "
        else:
            identity["python"] = str(tmp_path / "missing-python")
        identity_path.write_text(json.dumps(identity), encoding="utf-8")

    result = subprocess.run(
        [
            node,
            "--input-type=module",
            "-e",
            'await import(new URL("file://" + process.env.MEMPLEX_PLUGIN_ENTRY));',
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "MEMPLEX_PLUGIN_ENTRY": str(extension / "index.js")},
    )

    assert result.returncode != 0
    assert "reinstall required" in result.stderr


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


def test_openclaw_cli_loads_memplex_runtime_from_an_isolated_profile(tmp_path):
    openclaw_cli = _openclaw_cli_path()
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
        [openclaw_cli, "plugins", "inspect", "memplex", "--runtime", "--json"],
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
