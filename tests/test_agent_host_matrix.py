"""Interoperability matrix for four hosted adapters using one shared store."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from memplex.adapters.agent_installer import install_agent, uninstall_agent
from memplex.adapters.agent_runtime import AgentMemoryRuntime
from memplex.config import MemplexConfig
from memplex.service import MemplexService

_HOSTS = ("claude-code", "codex", "openclaw", "hermes")
_REPO_ROOT = Path(__file__).resolve().parents[1]

_OFFICIAL_MEMORY_PROVIDER_SHAPE = """from abc import ABC, abstractmethod

class MemoryProvider(ABC):
    @property
    @abstractmethod
    def name(self) -> str: ...
    @abstractmethod
    def is_available(self) -> bool: ...
    @abstractmethod
    def initialize(self, session_id: str, **kwargs) -> None: ...
    @abstractmethod
    def get_tool_schemas(self): ...
    def system_prompt_block(self) -> str: return ""
    def prefetch(self, query: str, *, session_id: str = "") -> str: return ""
    def queue_prefetch(self, query: str, *, session_id: str = "") -> None: pass
    def sync_turn(self, user_content, assistant_content, *, session_id="", messages=None): pass
    def handle_tool_call(self, tool_name, args, **kwargs): raise NotImplementedError
    def shutdown(self) -> None: pass
    def on_session_end(self, messages) -> None: pass
    def on_session_switch(self, new_session_id, *, parent_session_id="", reset=False,
                          rewound=False, **kwargs) -> None: pass
    def on_pre_compress(self, messages) -> str: return ""
    def on_memory_write(self, action, target, content, metadata=None) -> None: pass
    def on_delegation(self, task, result, *, child_session_id="", **kwargs) -> None: pass
    def get_config_schema(self): return []
    def save_config(self, values, hermes_home: str) -> None: pass
    def backup_paths(self): return []
"""


def _service(storage_path: Path) -> MemplexService:
    cfg = MemplexConfig()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(storage_path)
    return MemplexService(config=cfg)


def _bridge_env(
    storage_path: Path,
    *,
    claude_rate_file: Path | None = None,
    claude_home: Path | None = None,
    claude_plugin_root: Path | None = None,
) -> dict[str, str]:
    env = {
        **os.environ,
        "MEMPLEX_STORAGE_BACKEND": "lite",
        "MEMPLEX_STORAGE_PATH": str(storage_path),
        "MEMPLEX_EMBEDDING_CONTEXTUAL_RETRIEVAL": "false",
        "MEMPLEX_LLM_QUERY_ENHANCEMENT": "false",
    }
    if claude_rate_file:
        env["MEMPLEX_OBS_RATE_FILE"] = str(claude_rate_file)
    if claude_home:
        env["HOME"] = str(claude_home)
        env["CLAUDE_CONFIG_DIR"] = str(claude_home / ".claude")
    if claude_plugin_root:
        env["CLAUDE_PLUGIN_ROOT"] = str(claude_plugin_root)
    return env


def _runtime_has_token(
    *,
    host: str,
    user: str,
    session: str,
    project: Path,
    token: str,
    storage_path: Path,
    claude_home: Path | None = None,
    claude_plugin_root: Path | None = None,
    override_claude_user: bool = False,
) -> bool:
    if host == "claude-code":
        assert claude_plugin_root is not None
        assert claude_home is not None
        return _recall_with_claude_code(
            workspace=project,
            session=session,
            user=user,
            token=token,
            storage_path=storage_path,
            claude_home=claude_home,
            claude_plugin_root=claude_plugin_root,
            override_user=override_claude_user,
        )

    service = _service(storage_path)
    try:
        runtime = AgentMemoryRuntime(
            service=service,
            agent=host,
            user_id=user,
            session_id=session,
            project_path=project,
        )
        return token in runtime.before_prompt(token).context
    finally:
        service.stop()


def _run_claude_hook(
    *,
    args: list[str],
    payload: dict[str, Any],
    token: str,
    storage_path: Path,
    claude_home: Path,
    project_root: Path,
    claude_plugin_root: Path,
) -> subprocess.CompletedProcess[str]:
    claude_home.mkdir(parents=True, exist_ok=True)
    env = _bridge_env(
        storage_path,
        claude_rate_file=claude_home / f"memplex-obs-{token}.json",
        claude_home=claude_home,
        claude_plugin_root=claude_plugin_root,
    )
    current_pythonpath = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(_REPO_ROOT) + (
        os.pathsep + current_pythonpath if current_pythonpath else ""
    )
    return subprocess.run(
        [sys.executable, str(claude_plugin_root / "scripts" / "hook-runner.py"), *args],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        cwd=str(project_root),
        env=env,
    )


def _recall_with_claude_code(
    *,
    workspace: Path,
    session: str,
    user: str,
    token: str,
    storage_path: Path,
    claude_home: Path,
    claude_plugin_root: Path,
    override_user: bool = False,
) -> bool:
    payload = {
        "prompt": token,
        "cwd": str(workspace),
        "session_id": session,
    }
    if override_user:
        payload["user_id"] = user
    result = _run_claude_hook(
        args=["prompt-submit"],
        payload=payload,
        token=token,
        storage_path=storage_path,
        claude_home=claude_home,
        project_root=workspace,
        claude_plugin_root=claude_plugin_root,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout is not None and token in result.stdout


def _capture_with_claude_code(
    *,
    workspace: Path,
    session: str,
    token: str,
    storage_path: Path,
    claude_home: Path,
    claude_plugin_root: Path,
) -> None:
    result = _run_claude_hook(
        args=["observation", "Write", session],
        payload={
            "tool_input": {"file_path": f"memory-token:{token}"},
            "cwd": str(workspace),
            "session_id": session,
        },
        token=token,
        storage_path=storage_path,
        claude_home=claude_home,
        project_root=workspace,
        claude_plugin_root=claude_plugin_root,
    )
    assert result.returncode == 0, result.stderr


def _recall_with_codex(
    *,
    plugin_root: Path,
    plugin_data: Path,
    workspace: Path,
    session: str,
    user: str,
    token: str,
    storage_path: Path,
) -> bool:
    payload = {
        "hook_event_name": "UserPromptSubmit",
        "session_id": session,
        "cwd": str(workspace),
        "prompt": token,
    }
    result = subprocess.run(
        [sys.executable, "-m", "memplex.adapters.codex_plugin", "hook"],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        env=_bridge_env(storage_path)
        | {
            "PLUGIN_ROOT": str(plugin_root),
            "PLUGIN_DATA": str(plugin_data),
        },
    )
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout or "{}")
    hook_output = data.get("hookSpecificOutput", {})
    return token in hook_output.get("additionalContext", "")


def _capture_with_codex(
    *,
    plugin_root: Path,
    plugin_data: Path,
    workspace: Path,
    session: str,
    user: str,
    token: str,
    storage_path: Path,
) -> None:
    submit = subprocess.run(
        [sys.executable, "-m", "memplex.adapters.codex_plugin", "hook"],
        input=json.dumps(
            {
                "hook_event_name": "UserPromptSubmit",
                "session_id": session,
                "cwd": str(workspace),
                "prompt": f"Remember {token}",
            }
        ),
        capture_output=True,
        text=True,
        timeout=30,
        env=_bridge_env(storage_path)
        | {"PLUGIN_ROOT": str(plugin_root), "PLUGIN_DATA": str(plugin_data)},
    )
    assert submit.returncode == 0, submit.stderr

    stop = subprocess.run(
        [sys.executable, "-m", "memplex.adapters.codex_plugin", "hook"],
        input=json.dumps(
            {
                "hook_event_name": "Stop",
                "session_id": session,
                "cwd": str(workspace),
                "last_assistant_message": "Captured by matrix writer.",
            }
        ),
        capture_output=True,
        text=True,
        timeout=30,
        env=_bridge_env(storage_path)
        | {"PLUGIN_ROOT": str(plugin_root), "PLUGIN_DATA": str(plugin_data)},
    )
    assert stop.returncode == 0, stop.stderr
    assert json.loads(stop.stdout or "{}") == {}


def _run_openclaw_bridge(action: str, payload: dict[str, Any], storage_path: Path) -> dict:
    result = subprocess.run(
        [sys.executable, "-m", "memplex.adapters.openclaw_plugin", action],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=30,
        env=_bridge_env(storage_path),
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def _capture_with_openclaw(
    *,
    user: str,
    workspace: Path,
    session: str,
    token: str,
    storage_path: Path,
) -> None:
    payload = {
        "config": {"userId": user, "projectPath": str(workspace)},
        "context": {
            "sessionId": session,
            "sessionKey": f"agent:main:{session}",
            "workspaceDir": str(workspace),
            "agentId": "main",
        },
        "event": {
            "success": True,
            "messages": [
                {"role": "user", "content": f"Remember {token}"},
                {"role": "assistant", "content": "Captured by matrix writer."},
            ],
        },
    }
    captured = _run_openclaw_bridge("capture", payload, storage_path)
    assert captured["captured"] is True


def _recall_with_openclaw(
    *,
    user: str,
    workspace: Path,
    session: str,
    token: str,
    storage_path: Path,
) -> bool:
    payload = {
        "config": {"userId": user, "projectPath": str(workspace)},
        "context": {
            "sessionId": session,
            "sessionKey": f"agent:main:{session}",
            "workspaceDir": str(workspace),
        },
        "event": {"prompt": token, "query": token, "messages": []},
    }
    recalled = _run_openclaw_bridge("recall", payload, storage_path)
    return token in str(recalled.get("prependContext", ""))


def _install_hermes_bridge(
    tmp_path: Path, *, user_id: str, project_path: Path
) -> tuple[Path, Path]:
    hermes_home = tmp_path / "hermes"
    install_agent(
        "hermes",
        target_dir=hermes_home,
        user_id=user_id,
        project_path=project_path,
    )
    plugin_dir = hermes_home / "plugins" / "memplex"
    return hermes_home, plugin_dir


def _install_claude_bridge(
    tmp_path: Path, *, user_id: str, project_path: Path
) -> tuple[Path, Path]:
    claude_home = tmp_path
    install_agent("claude-code", target_dir=claude_home, user_id=user_id, project_path=project_path)
    plugin_root = claude_home / "plugins" / "marketplaces" / "articultur" / "plugin"
    return claude_home, plugin_root


def _load_hermes_plugin(plugin_dir: Path, tmp_path: Path, name: str):
    agent_root = tmp_path / f"{name}-agent-root"
    agent_package = agent_root / "agent"
    agent_package.mkdir(parents=True, exist_ok=True)
    (agent_package / "__init__.py").write_text("", encoding="utf-8")
    (agent_package / "memory_provider.py").write_text(
        _OFFICIAL_MEMORY_PROVIDER_SHAPE, encoding="utf-8"
    )

    old_path = list(sys.path)
    saved = {
        key: sys.modules.get(key)
        for key in ("agent", "agent.memory_provider", "memplex.adapters.hermes_memory_provider")
    }
    for key in saved:
        sys.modules.pop(key, None)
    sys.path.insert(0, str(agent_root))
    try:
        spec = importlib.util.spec_from_file_location(name, plugin_dir / "__init__.py")
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path[:] = old_path
        for key, value in saved.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value


def _capture_with_hermes(
    *,
    provider_module: Any,
    hermes_home: Path,
    workspace: Path,
    session: str,
    token: str,
    storage_path: Path,
) -> None:
    provider = provider_module.MemplexMemoryProvider(
        service_factory=lambda: _service(storage_path),
    )
    try:
        provider.initialize(session, hermes_home=str(hermes_home), workspace_dir=str(workspace))
        provider.sync_turn(f"Remember {token}", "Captured by matrix writer.")
        assert provider._flush(timeout=5.0)
    finally:
        provider.shutdown()


def _recall_with_hermes(
    *,
    provider_module: Any,
    hermes_home: Path,
    workspace: Path,
    session: str,
    user: str,
    token: str,
    storage_path: Path,
) -> bool:
    provider = provider_module.MemplexMemoryProvider(
        service_factory=lambda: _service(storage_path),
        runtime_factory=lambda **kwargs: AgentMemoryRuntime(
            service=kwargs["service"],
            agent="hermes",
            user_id=user,
            session_id=kwargs["session_id"],
            project_path=kwargs["project_path"],
        ),
    )
    try:
        provider.initialize(session, hermes_home=str(hermes_home), workspace_dir=str(workspace))
        recalled = provider.prefetch(token)
        return token in recalled
    finally:
        provider.shutdown()


def _prepare_codex_root(tmp_path: Path, workspace: Path, *, user: str) -> tuple[Path, Path]:
    host_root = tmp_path / "codex-home"
    root = host_root / "plugins" / "marketplaces" / "memplex" / "plugin"
    root.mkdir(parents=True, exist_ok=True)
    (root / "memplex-agent.json").write_text(
        json.dumps(
            {
                "agent": "codex",
                "user_id": user,
                "project_path": str(workspace),
                "python": sys.executable,
                "source_root": str(_REPO_ROOT),
                "host_root": str(host_root),
                "managed": {
                    "by": "memplex",
                    "installer": "memplex",
                    "schema_version": 1,
                },
            }
        ),
        encoding="utf-8",
    )
    return root, tmp_path / "codex-plugin-data"


def _write_by_host(
    *,
    host: str,
    workspace: Path,
    session: str,
    token: str,
    user: str,
    storage_path: Path,
    codex_root: Path,
    codex_data: Path,
    hermes_home: Path,
    hermes_module: Any,
    claude_home: Path,
    claude_plugin_root: Path,
) -> None:
    if host == "claude-code":
        _capture_with_claude_code(
            workspace=workspace,
            session=session,
            token=token,
            storage_path=storage_path,
            claude_home=claude_home,
            claude_plugin_root=claude_plugin_root,
        )
        return

    if host == "codex":
        _capture_with_codex(
            plugin_root=codex_root,
            plugin_data=codex_data,
            workspace=workspace,
            session=session,
            user=user,
            token=token,
            storage_path=storage_path,
        )
        return

    if host == "openclaw":
        _capture_with_openclaw(
            user=user,
            workspace=workspace,
            session=session,
            token=token,
            storage_path=storage_path,
        )
        return

    if host == "hermes":
        _capture_with_hermes(
            provider_module=hermes_module,
            hermes_home=hermes_home,
            workspace=workspace,
            session=session,
            token=token,
            storage_path=storage_path,
        )
        return

    raise ValueError(f"Unknown host {host}")


def _recall_by_host(
    *,
    host: str,
    workspace: Path,
    session: str,
    token: str,
    user: str,
    storage_path: Path,
    codex_root: Path,
    codex_data: Path,
    hermes_home: Path,
    hermes_module: Any,
    claude_home: Path,
    claude_plugin_root: Path,
) -> bool:
    if host == "claude-code":
        return _runtime_has_token(
            host="claude-code",
            user=user,
            session=session,
            project=workspace,
            token=token,
            storage_path=storage_path,
            claude_home=claude_home,
            claude_plugin_root=claude_plugin_root,
        )
    if host == "codex":
        return _recall_with_codex(
            plugin_root=codex_root,
            plugin_data=codex_data,
            workspace=workspace,
            session=session,
            user=user,
            token=token,
            storage_path=storage_path,
        )
    if host == "openclaw":
        return _recall_with_openclaw(
            user=user,
            workspace=workspace,
            session=session,
            token=token,
            storage_path=storage_path,
        )
    if host == "hermes":
        return _recall_with_hermes(
            provider_module=hermes_module,
            hermes_home=hermes_home,
            workspace=workspace,
            session=session,
            user=user,
            token=token,
            storage_path=storage_path,
        )
    raise ValueError(f"Unknown host {host}")


def test_four_host_workspace_matrix_is_deterministic(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    storage_path = tmp_path / "matrix-store.json"
    codex_root, codex_data = _prepare_codex_root(tmp_path / "codex", workspace, user="alice")
    claude_home, claude_plugin_root = _install_claude_bridge(
        tmp_path / "claude", user_id="alice", project_path=workspace
    )
    hermes_home, plugin_dir = _install_hermes_bridge(
        tmp_path / "hermes", user_id="alice", project_path=workspace
    )
    hermes_module = _load_hermes_plugin(plugin_dir, tmp_path / "hermes-bootstrap", "hermes_matrix")

    try:
        for writer in _HOSTS:
            token = f"matrix-{writer}-workspace-token"
            writer_session = f"{writer}-writer"
            _write_by_host(
                host=writer,
                workspace=workspace,
                session=writer_session,
                token=token,
                user="alice",
                storage_path=storage_path,
                codex_root=codex_root,
                codex_data=codex_data,
                hermes_home=hermes_home,
                hermes_module=hermes_module,
                claude_home=claude_home,
                claude_plugin_root=claude_plugin_root,
            )

            for reader in _HOSTS:
                if reader == writer:
                    continue
                reader_session = f"{reader}-reader"
                assert _recall_by_host(
                    host=reader,
                    workspace=workspace,
                    session=reader_session,
                    token=token,
                    user="alice",
                    storage_path=storage_path,
                    codex_root=codex_root,
                    codex_data=codex_data,
                    hermes_home=hermes_home,
                    hermes_module=hermes_module,
                    claude_home=claude_home,
                    claude_plugin_root=claude_plugin_root,
                )
    finally:
        uninstall_agent("hermes", target_dir=hermes_home)
        uninstall_agent("claude-code", target_dir=claude_home)


def test_user_isolation_breaks_cross_user_recall_across_hosts(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    storage_path = tmp_path / "matrix-store.json"
    alice_claude_home, alice_claude_plugin_root = _install_claude_bridge(
        tmp_path / "claude-alice", user_id="alice", project_path=workspace
    )
    bob_claude_home, bob_claude_plugin_root = _install_claude_bridge(
        tmp_path / "claude-bob", user_id="bob", project_path=workspace
    )

    service = _service(storage_path)
    runtime = AgentMemoryRuntime(
        service=service,
        agent="codex",
        user_id="alice",
        session_id="alice-session",
        project_path=workspace,
    )
    runtime.after_response(
        user_message="Remember user-isolation-token",
        assistant_message="Captured by matrix writer.",
    )
    service.stop()

    try:
        assert _runtime_has_token(
            host="claude-code",
            user="alice",
            session="alice-reader",
            project=workspace,
            token="user-isolation-token",
            storage_path=storage_path,
            claude_home=alice_claude_home,
            claude_plugin_root=alice_claude_plugin_root,
        )
        assert not _runtime_has_token(
            host="claude-code",
            user="bob",
            session="bob-reader",
            project=workspace,
            token="user-isolation-token",
            storage_path=storage_path,
            claude_home=bob_claude_home,
            claude_plugin_root=bob_claude_plugin_root,
        )
    finally:
        uninstall_agent("claude-code", target_dir=alice_claude_home)
        uninstall_agent("claude-code", target_dir=bob_claude_home)

    assert not _runtime_has_token(
        host="codex",
        user="bob",
        session="bob-reader",
        project=workspace,
        token="user-isolation-token",
        storage_path=storage_path,
    )
    assert not _runtime_has_token(
        host="openclaw",
        user="bob",
        session="bob-reader",
        project=workspace,
        token="user-isolation-token",
        storage_path=storage_path,
    )
    assert not _runtime_has_token(
        host="hermes",
        user="bob",
        session="bob-reader",
        project=workspace,
        token="user-isolation-token",
        storage_path=storage_path,
    )


def test_workspace_isolation_blocks_recall_in_other_workspace(tmp_path):
    workspace_a = tmp_path / "workspace-a"
    workspace_b = tmp_path / "workspace-b"
    workspace_a.mkdir()
    workspace_b.mkdir()
    storage_path = tmp_path / "matrix-store.json"
    claude_home_a, claude_plugin_root_a = _install_claude_bridge(
        tmp_path / "claude-a", user_id="alice", project_path=workspace_a
    )
    claude_home_b, claude_plugin_root_b = _install_claude_bridge(
        tmp_path / "claude-b", user_id="alice", project_path=workspace_b
    )

    service = _service(storage_path)
    runtime = AgentMemoryRuntime(
        service=service,
        agent="openclaw",
        user_id="alice",
        session_id="openclaw-session",
        project_path=workspace_a,
    )
    runtime.after_response(
        user_message="Remember workspace-isolation-token",
        assistant_message="Captured by matrix writer.",
    )
    service.stop()

    try:
        assert _runtime_has_token(
            host="claude-code",
            user="alice",
            session="alice-a",
            project=workspace_a,
            token="workspace-isolation-token",
            storage_path=storage_path,
            claude_home=claude_home_a,
            claude_plugin_root=claude_plugin_root_a,
        )
        assert not _runtime_has_token(
            host="claude-code",
            user="alice",
            session="alice-b",
            project=workspace_b,
            token="workspace-isolation-token",
            storage_path=storage_path,
            claude_home=claude_home_b,
            claude_plugin_root=claude_plugin_root_b,
        )
    finally:
        uninstall_agent("claude-code", target_dir=claude_home_a)
        uninstall_agent("claude-code", target_dir=claude_home_b)

    assert not _runtime_has_token(
        host="codex",
        user="alice",
        session="codex-b",
        project=workspace_b,
        token="workspace-isolation-token",
        storage_path=storage_path,
    )
    assert not _runtime_has_token(
        host="openclaw",
        user="alice",
        session="openclaw-b",
        project=workspace_b,
        token="workspace-isolation-token",
        storage_path=storage_path,
    )
    assert not _runtime_has_token(
        host="hermes",
        user="alice",
        session="hermes-b",
        project=workspace_b,
        token="workspace-isolation-token",
        storage_path=storage_path,
    )


def test_session_source_visibility_isolated_from_other_hosts_and_sessions(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    storage_path = tmp_path / "matrix-store.json"
    token = "session-source-token-matrix"
    claude_home, claude_plugin_root = _install_claude_bridge(
        tmp_path / "claude", user_id="alice", project_path=workspace
    )

    service = _service(storage_path)
    runtime = AgentMemoryRuntime(
        service=service,
        agent="codex",
        user_id="alice",
        session_id="source-session",
        project_path=workspace,
    )
    runtime.after_response(
        user_message=f"Remember {token}",
        assistant_message="Captured by matrix writer.",
        metadata={"memplex_visibility": "session"},
    )
    service.stop()

    assert _runtime_has_token(
        host="codex",
        user="alice",
        session="source-session",
        project=workspace,
        token=token,
        storage_path=storage_path,
    )
    assert not _runtime_has_token(
        host="codex",
        user="alice",
        session="other-session",
        project=workspace,
        token=token,
        storage_path=storage_path,
    )
    try:
        assert not _runtime_has_token(
            host="claude-code",
            user="alice",
            session="other-session",
            project=workspace,
            token=token,
            storage_path=storage_path,
            claude_home=claude_home,
            claude_plugin_root=claude_plugin_root,
        )
        assert not _runtime_has_token(
            host="openclaw",
            user="alice",
            session="other-session",
            project=workspace,
            token=token,
            storage_path=storage_path,
        )
        assert not _runtime_has_token(
            host="hermes",
            user="alice",
            session="other-session",
            project=workspace,
            token=token,
            storage_path=storage_path,
        )
    finally:
        uninstall_agent("claude-code", target_dir=claude_home)
