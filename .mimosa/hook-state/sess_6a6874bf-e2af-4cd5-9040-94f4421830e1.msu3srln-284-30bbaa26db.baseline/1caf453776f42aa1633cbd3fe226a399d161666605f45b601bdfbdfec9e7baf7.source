"""Hermes native MemoryProvider contract and interoperability tests."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from memplex.adapters.agent_installer import install_agent, uninstall_agent
from memplex.adapters.agent_runtime import AgentMemoryRuntime, get_agent_manifest
from memplex.adapters.runtime_status import read_runtime_status, runtime_status_path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Minimal import fixture extracted from the immutable official source pinned by
# ``test_hermes_manifest_pins_immutable_official_memory_provider_source``.
# Updating this shape requires updating the manifest provenance and source hash.
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


def test_hermes_manifest_pins_immutable_official_memory_provider_source():
    contract = get_agent_manifest("hermes")["config"]["host_contract"]

    assert contract == {
        "kind": "bridge-backed-memory-provider",
        "upstream_repository": "https://github.com/NousResearch/hermes-agent",
        "upstream_version": "v2026.8.3",
        "upstream_tag_commit": "7de39e700d2c329e15d32eb0b96e2f7cdd9fbdb2",
        "source_revision": "3c27eb6234bf91b8ceee9e9071591b31e9b148cb",
        "source_path": "agent/memory_provider.py",
        "source_url": (
            "https://github.com/NousResearch/hermes-agent/blob/"
            "3c27eb6234bf91b8ceee9e9071591b31e9b148cb/agent/memory_provider.py"
        ),
        "source_sha256": "678c9150852f2018182e08622ae25b495360fd5099747f823c35e00cce08d8dd",
    }


def test_hermes_official_cli_discovers_installed_provider_in_isolated_home(
    tmp_path,
    monkeypatch,
):
    cli_value = os.environ.get("MEMPLEX_G008_HERMES_CLI")
    source_value = os.environ.get("MEMPLEX_G008_HERMES_SOURCE_ROOT")
    if not cli_value or not source_value:
        pytest.skip("pinned official Hermes CLI/source are unavailable")
    cli = Path(cli_value)
    source = Path(source_value)
    if not cli.is_file() or not source.is_dir():
        pytest.skip("pinned official Hermes CLI/source are unavailable")
    provider_source = source / "agent" / "memory_provider.py"
    assert hashlib.sha256(provider_source.read_bytes()).hexdigest() == (
        "678c9150852f2018182e08622ae25b495360fd5099747f823c35e00cce08d8dd"
    )
    version_result = subprocess.run(
        [str(cli), "--version"], capture_output=True, text=True, timeout=30
    )
    assert version_result.returncode == 0, version_result.stderr
    assert "Hermes Agent v0.20.0 (2026.8.3)" in version_result.stdout

    hermes_home, _, plugin_dir = _install(tmp_path)
    isolated_home = tmp_path / "homes" / "hermes"
    isolated_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(isolated_home))
    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setenv("HERMES_CONFIG_DIR", str(hermes_home))
    status = subprocess.run(
        [str(cli), "memory", "status"],
        capture_output=True,
        text=True,
        timeout=30,
        env=os.environ.copy(),
    )
    assert status.returncode == 0, status.stderr
    assert "Provider:  memplex" in status.stdout
    assert "Plugin:    installed" in status.stdout
    assert "Status:    available" in status.stdout

    module, _ = _load_plugin(plugin_dir, tmp_path, "hermes_g008_lifecycle_test")
    provider = module.MemplexMemoryProvider(
        service_factory=_ServiceProbe,
        runtime_factory=lambda **kwargs: _RuntimeProbe(**kwargs),
    )
    try:
        provider.initialize("g008-hermes-session")
        assert provider.prefetch("g008 lifecycle probe") == "echo:g008 lifecycle probe"
    finally:
        provider.shutdown()


def _install(
    tmp_path: Path,
    *,
    user_id: str = "alice",
    config_text: str | None = None,
) -> tuple[Path, Path, Path]:
    hermes_home = tmp_path / "hermes"
    workspace = tmp_path / "workspace"
    hermes_home.mkdir(parents=True, exist_ok=True)
    workspace.mkdir(parents=True, exist_ok=True)
    if config_text is not None:
        (hermes_home / "config.yaml").write_text(config_text, encoding="utf-8")
    install_agent(
        "hermes",
        target_dir=hermes_home,
        user_id=user_id,
        project_path=workspace,
    )
    return hermes_home, workspace, hermes_home / "plugins" / "memplex"


def _load_plugin(plugin_dir: Path, tmp_path: Path, name: str = "hermes_memplex_test"):
    agent_root = tmp_path / f"{name}-agent-root"
    agent_package = agent_root / "agent"
    agent_package.mkdir(parents=True, exist_ok=True)
    (agent_package / "__init__.py").write_text("", encoding="utf-8")
    (agent_package / "memory_provider.py").write_text(
        _OFFICIAL_MEMORY_PROVIDER_SHAPE,
        encoding="utf-8",
    )
    old_path = list(sys.path)
    saved = {
        key: sys.modules.get(key)
        for key in (
            "agent",
            "agent.memory_provider",
            "memplex.adapters.hermes_memory_provider",
        )
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
        base = sys.modules["agent.memory_provider"].MemoryProvider
        return module, base
    finally:
        sys.path[:] = old_path
        for key, value in saved.items():
            if value is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = value


class _ServiceProbe:
    def __init__(self) -> None:
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


class _RuntimeProbe:
    def __init__(self, **identity: Any) -> None:
        self.identity = identity
        self.after: list[tuple[str, str, dict[str, Any]]] = []
        self.prefetched: list[str] = []
        self.captured: list[tuple[str, str, dict[str, Any]]] = []

    def before_prompt(self, query: str):
        return SimpleNamespace(context=f"echo:{query}", total=1)

    def after_response(self, user_message: str, assistant_message: str, metadata=None):
        self.after.append((user_message, assistant_message, metadata or {}))

    def prefetch(self, query: str):
        self.prefetched.append(query)

    def capture_turn(self, user_message: str, assistant_message: str, metadata=None):
        self.captured.append((user_message, assistant_message, metadata or {}))


def test_hermes_install_selects_official_provider_and_materializes_bootstrap(tmp_path):
    original = "# Hermes profile\nmemory:\n  provider: honcho\n  mode: workspace\n"
    hermes_home, workspace, plugin_dir = _install(
        tmp_path,
        user_id="layout-user",
        config_text=original,
    )

    config = yaml.safe_load((hermes_home / "config.yaml").read_text(encoding="utf-8"))
    assert config["memory"] == {"provider": "memplex", "mode": "workspace"}
    assert "# Hermes profile" in (hermes_home / "config.yaml").read_text(encoding="utf-8")
    provider = json.loads((hermes_home / "memplex.json").read_text(encoding="utf-8"))
    identity = json.loads((plugin_dir / "memplex-agent.json").read_text(encoding="utf-8"))
    assert provider["user_id"] == "layout-user"
    assert provider["project_path"] == str(workspace.resolve())
    assert provider["tools"] == ["memplex_search", "memplex_conclude"]
    assert identity["source_root"] == str(PROJECT_ROOT)
    assert identity["host_root"] == str(hermes_home.resolve())
    assert not (hermes_home / "memory-providers" / "memplex.json").exists()
    bootstrap = (plugin_dir / "__init__.py").read_text(encoding="utf-8")
    assert "memplex.adapters.hermes_memory_provider" in bootstrap
    assert "source_root" in bootstrap

    uninstall_agent("hermes", target_dir=hermes_home)
    assert (hermes_home / "config.yaml").read_text(encoding="utf-8") == original


def test_hermes_config_exact_restore_preserves_mode_and_comments(tmp_path):
    original = "# keep\nmemory:\n  provider: mem0 # previous\nother: true\n"
    hermes_home = tmp_path / "hermes"
    workspace = tmp_path / "workspace"
    hermes_home.mkdir()
    workspace.mkdir()
    config_path = hermes_home / "config.yaml"
    config_path.write_text(original, encoding="utf-8")
    config_path.chmod(0o640)
    install_agent(
        "hermes",
        target_dir=hermes_home,
        user_id="alice",
        project_path=workspace,
    )
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o640
    uninstall_agent("hermes", target_dir=hermes_home)
    assert config_path.read_text(encoding="utf-8") == original
    assert stat.S_IMODE(config_path.stat().st_mode) == 0o640


def test_hermes_config_fallback_preserves_post_install_user_edits(tmp_path):
    original = "memory:\n  provider: hindsight\n  mode: session\n"
    hermes_home, _, _ = _install(tmp_path, config_text=original)
    config_path = hermes_home / "config.yaml"
    installed = config_path.read_text(encoding="utf-8")
    config_path.write_text(
        installed.replace("mode: session", "mode: workspace\n  budget: 42"),
        encoding="utf-8",
    )

    uninstall_agent("hermes", target_dir=hermes_home)

    restored = config_path.read_text(encoding="utf-8")
    assert "provider: hindsight" in restored
    assert "mode: workspace" in restored
    assert "budget: 42" in restored


def test_hermes_config_fallback_respects_user_selected_provider(tmp_path):
    hermes_home, _, _ = _install(tmp_path, config_text="memory:\n  provider: mem0\n")
    config_path = hermes_home / "config.yaml"
    config_path.write_text("memory:\n  provider: external\n# user changed it\n", encoding="utf-8")

    uninstall_agent("hermes", target_dir=hermes_home)

    assert config_path.read_text(encoding="utf-8") == (
        "memory:\n  provider: external\n# user changed it\n"
    )


def test_hermes_install_handles_flow_yaml_and_rejects_non_mapping_memory(tmp_path):
    original = "memory: {provider: mem0, mode: workspace} # keep\nother: true\n"
    hermes_home, _, _ = _install(tmp_path, config_text=original)
    configured = yaml.safe_load((hermes_home / "config.yaml").read_text(encoding="utf-8"))
    assert configured["memory"] == {"provider": "memplex", "mode": "workspace"}
    assert "# keep" in (hermes_home / "config.yaml").read_text(encoding="utf-8")
    uninstall_agent("hermes", target_dir=hermes_home)
    assert (hermes_home / "config.yaml").read_text(encoding="utf-8") == original

    invalid_home = tmp_path / "invalid-hermes"
    invalid_home.mkdir()
    (invalid_home / "config.yaml").write_text("memory: disabled\n", encoding="utf-8")
    with pytest.raises(ValueError, match="not a mapping"):
        install_agent(
            "hermes",
            target_dir=invalid_home,
            user_id="alice",
            project_path=tmp_path / "workspace",
        )
    assert not (invalid_home / "plugins" / "memplex").exists()
    assert not (invalid_home / "memplex.json").exists()


def test_hermes_bootstrap_registers_subclass_of_official_abc(tmp_path):
    hermes_home, _, plugin_dir = _install(tmp_path)
    module, base = _load_plugin(plugin_dir, tmp_path)

    class Registry:
        def __init__(self) -> None:
            self.providers: list[Any] = []

        def register_memory_provider(self, provider: Any) -> None:
            self.providers.append(provider)

    registry = Registry()
    module.register(registry)

    assert len(registry.providers) == 1
    assert isinstance(registry.providers[0], base)
    assert registry.providers[0].name == "memplex"
    uninstall_agent("hermes", target_dir=hermes_home)


def test_hermes_bootstrap_rejects_duplicate_identity_before_touching_sys_path(tmp_path):
    """A damaged identity must not be allowed to inject source_root into sys.path."""

    _, _, plugin_dir = _install(tmp_path)
    identity_path = plugin_dir / "memplex-agent.json"
    raw = identity_path.read_text(encoding="utf-8").replace(
        '"agent": "hermes"',
        '"agent": "hermes", "agent": "hermes"',
        1,
    )
    identity_path.write_text(raw, encoding="utf-8")
    runner = """
import importlib.util
import os
import sys
spec = importlib.util.spec_from_file_location("damaged_hermes", os.environ["MEMPLEX_PLUGIN_ENTRY"])
module = importlib.util.module_from_spec(spec)
try:
    spec.loader.exec_module(module)
except Exception as exc:
    print(int(os.environ["MEMPLEX_SOURCE_ROOT"] in sys.path))
    print(str(exc), file=sys.stderr)
    raise SystemExit(7)
raise SystemExit(0)
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", runner],
        capture_output=True,
        text=True,
        timeout=30,
        env={
            **os.environ,
            "MEMPLEX_PLUGIN_ENTRY": str(plugin_dir / "__init__.py"),
            "MEMPLEX_SOURCE_ROOT": str(PROJECT_ROOT),
        },
    )

    assert result.returncode == 7
    assert result.stdout.strip() == "0"
    assert "reinstall required" in result.stderr


def test_hermes_bootstrap_rejects_identity_for_another_host_before_sys_path(tmp_path):
    """Hermes derives host A from its plugin path before trusting identity source_root."""

    _, _, plugin_dir = _install(tmp_path)
    other_root = tmp_path / "other-hermes"
    other_root.mkdir()
    identity_path = plugin_dir / "memplex-agent.json"
    identity = json.loads(identity_path.read_text(encoding="utf-8"))
    identity["host_root"] = str(other_root.resolve())
    identity_path.write_text(json.dumps(identity), encoding="utf-8")
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            (
                "import importlib.util,os;"
                "p=os.environ['MEMPLEX_PLUGIN_ENTRY'];"
                "s=importlib.util.spec_from_file_location('mismatch_hermes',p);"
                "m=importlib.util.module_from_spec(s);s.loader.exec_module(m)"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        env={**os.environ, "MEMPLEX_PLUGIN_ENTRY": str(plugin_dir / "__init__.py")},
    )

    assert result.returncode != 0
    assert "host_root" in result.stderr
    assert "reinstall required" in result.stderr


def test_hermes_provider_rejects_damaged_managed_identity(tmp_path):
    """Direct provider construction cannot bypass the bootstrap identity gate."""

    hermes_home, _, plugin_dir = _install(tmp_path)
    module, _ = _load_plugin(plugin_dir, tmp_path, "hermes_provider_identity_test")
    identity = json.loads((plugin_dir / "memplex-agent.json").read_text(encoding="utf-8"))
    identity["unexpected"] = True
    provider = module.MemplexMemoryProvider(identity=identity, service_factory=_ServiceProbe)

    with pytest.raises(ValueError, match="reinstall required"):
        provider.initialize("identity-session", hermes_home=str(hermes_home))


def test_hermes_provider_rejects_identity_for_another_requested_home(tmp_path):
    """Direct provider construction cannot redirect lifecycle state into host B."""

    hermes_home, _, plugin_dir = _install(tmp_path)
    other_root = tmp_path / "other-hermes"
    other_root.mkdir()
    module, _ = _load_plugin(plugin_dir, tmp_path, "hermes_provider_host_binding_test")
    identity = json.loads((plugin_dir / "memplex-agent.json").read_text(encoding="utf-8"))
    identity["host_root"] = str(other_root.resolve())
    provider = module.MemplexMemoryProvider(identity=identity, service_factory=_ServiceProbe)

    with pytest.raises(ValueError, match="host_root.*reinstall required|reinstall required.*host_root"):
        provider.initialize("identity-session", hermes_home=str(hermes_home))


@pytest.mark.parametrize(
    "configured_environment",
    [
        ("HERMES_CONFIG_DIR",),
        ("HERMES_HOME",),
        ("HERMES_CONFIG_DIR", "HERMES_HOME"),
    ],
)
def test_hermes_provider_without_kwargs_uses_configured_environment_root(
    tmp_path,
    monkeypatch,
    configured_environment,
):
    """Official initialize(session_id) must bind to the isolated Hermes root."""

    hermes_home, _, plugin_dir = _install(tmp_path)
    isolated_home = tmp_path / "homes" / "hermes"
    isolated_home.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(isolated_home))
    monkeypatch.delenv("HERMES_CONFIG_DIR", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)
    for name in configured_environment:
        monkeypatch.setenv(name, str(hermes_home))
    module, _ = _load_plugin(plugin_dir, tmp_path, "hermes_environment_root_test")
    provider = module.MemplexMemoryProvider(
        service_factory=_ServiceProbe,
        runtime_factory=lambda **kwargs: _RuntimeProbe(**kwargs),
    )

    try:
        provider.initialize("environment-session")
        assert provider.prefetch("environment probe") == "echo:environment probe"
    finally:
        provider.shutdown()


def test_hermes_provider_without_kwargs_rejects_conflicting_environment_roots(
    tmp_path,
    monkeypatch,
):
    """Two different Hermes roots cannot silently select one managed identity."""

    hermes_home, _, plugin_dir = _install(tmp_path)
    other_home = tmp_path / "other-hermes"
    other_home.mkdir()
    monkeypatch.setenv("HERMES_CONFIG_DIR", str(hermes_home))
    monkeypatch.setenv("HERMES_HOME", str(other_home))
    module, _ = _load_plugin(plugin_dir, tmp_path, "hermes_environment_conflict_test")
    provider = module.MemplexMemoryProvider(service_factory=_ServiceProbe)

    with pytest.raises(ValueError, match="HERMES_CONFIG_DIR.*HERMES_HOME.*conflict"):
        provider.initialize("conflict-session")


def test_hermes_provider_explicit_home_precedes_conflicting_environment_roots(
    tmp_path,
    monkeypatch,
):
    """The official explicit lifecycle argument remains the highest-priority root."""

    hermes_home, _, plugin_dir = _install(tmp_path)
    other_home = tmp_path / "other-hermes"
    other_home.mkdir()
    monkeypatch.setenv("HERMES_CONFIG_DIR", str(other_home))
    monkeypatch.setenv("HERMES_HOME", str(other_home))
    module, _ = _load_plugin(plugin_dir, tmp_path, "hermes_explicit_root_test")
    provider = module.MemplexMemoryProvider(
        service_factory=_ServiceProbe,
        runtime_factory=lambda **kwargs: _RuntimeProbe(**kwargs),
    )

    try:
        provider.initialize("explicit-session", hermes_home=str(hermes_home))
        assert provider.prefetch("explicit probe") == "echo:explicit probe"
    finally:
        provider.shutdown()


def test_hermes_lifecycle_orders_sync_prefetch_and_deduplicates_finalizers(tmp_path):
    hermes_home, _, plugin_dir = _install(tmp_path)
    module, _ = _load_plugin(plugin_dir, tmp_path, "hermes_lifecycle_test")
    service = _ServiceProbe()
    runtimes: dict[str, _RuntimeProbe] = {}

    def runtime_factory(**kwargs: Any) -> _RuntimeProbe:
        runtime = _RuntimeProbe(**kwargs)
        runtimes[kwargs["session_id"]] = runtime
        return runtime

    provider = module.MemplexMemoryProvider(
        service_factory=lambda: service,
        runtime_factory=runtime_factory,
    )
    provider.initialize("s1", hermes_home=str(hermes_home), platform="cli")
    assert provider.prefetch("first query", session_id="s1") == "echo:first query"
    assert provider.prefetch("second query", session_id="s2") == "echo:second query"
    provider.sync_turn("u1", "a1", session_id="s1")
    provider.sync_turn("u2", "a2", session_id="s1")
    provider.queue_prefetch("next", session_id="s1")
    assert provider._flush(timeout=2.0)
    assert [(u, a) for u, a, _ in runtimes["s1"].after] == [("u1", "a1"), ("u2", "a2")]
    assert runtimes["s1"].prefetched == ["next"]

    messages = [
        {"role": "user", "content": "u2"},
        {"role": "assistant", "content": "a2"},
    ]
    assert provider.on_pre_compress(messages) == ""
    provider.on_session_end(messages)
    assert len(runtimes["s1"].after) == 2

    provider.shutdown()
    assert service.stopped is True
    uninstall_agent("hermes", target_dir=hermes_home)


def test_hermes_real_prefetch_failure_persists_degraded_host_runtime_state(tmp_path):
    """A provider failure must be persisted even when Hermes surfaces its exception."""
    hermes_home, _, plugin_dir = _install(tmp_path)
    module, _ = _load_plugin(plugin_dir, tmp_path, "hermes_runtime_status_test")

    class BrokenRuntime(_RuntimeProbe):
        def before_prompt(self, _query: str):
            raise RuntimeError("Bearer hermes-secret-must-not-persist")

    provider = module.MemplexMemoryProvider(
        service_factory=_ServiceProbe,
        runtime_factory=lambda **_: BrokenRuntime(),
    )
    provider.initialize("status-session", hermes_home=str(hermes_home))
    with pytest.raises(RuntimeError, match="hermes-secret"):
        provider.prefetch("remember status")

    assert read_runtime_status(runtime_status_path(hermes_home), agent="hermes") == {
        "reason": "runtime_operation_failed",
        "state": "degraded",
    }
    provider.shutdown()
    uninstall_agent("hermes", target_dir=hermes_home)


def test_hermes_managed_identity_cannot_be_overridden_by_runtime_sources(
    tmp_path,
    monkeypatch,
):
    hermes_home, workspace, plugin_dir = _install(tmp_path, user_id="alice")
    module, _ = _load_plugin(plugin_dir, tmp_path, "hermes_managed_identity_test")
    identity = json.loads((plugin_dir / "memplex-agent.json").read_text(encoding="utf-8"))
    attacker_workspace = tmp_path / "attacker-workspace"
    attacker_workspace.mkdir()
    provider_config_path = hermes_home / "memplex.json"
    provider_config = json.loads(provider_config_path.read_text(encoding="utf-8"))
    provider_config.update(
        {
            "user_id": "config-attacker",
            "project_path": str(attacker_workspace),
        }
    )
    provider_config_path.write_text(json.dumps(provider_config), encoding="utf-8")
    monkeypatch.setenv("MEMPLEX_USER_ID", "env-attacker")
    monkeypatch.setenv("MEMPLEX_PROJECT_ROOT", str(attacker_workspace))

    service = _ServiceProbe()
    runtimes: dict[str, _RuntimeProbe] = {}

    def runtime_factory(**kwargs: Any) -> _RuntimeProbe:
        runtime = _RuntimeProbe(**kwargs)
        runtimes[kwargs["session_id"]] = runtime
        return runtime

    provider = module.MemplexMemoryProvider(
        identity=identity,
        service_factory=lambda: service,
        runtime_factory=runtime_factory,
    )
    provider.initialize(
        "dynamic-session",
        hermes_home=str(hermes_home),
        user_id="kwargs-attacker",
        workspace_dir=str(attacker_workspace),
        project_path=str(attacker_workspace),
        platform="telegram",
        agent_identity="host-agent-7",
        agent_workspace="host-workspace-9",
        parent_session_id="parent-session-3",
    )
    provider.prefetch("identity probe")
    provider.sync_turn("managed identity", "kept")
    assert provider._flush(timeout=2.0)

    runtime = runtimes["dynamic-session"]
    assert runtime.identity["user_id"] == "alice"
    assert runtime.identity["project_path"] == str(workspace.resolve())
    assert runtime.identity["session_id"] == "dynamic-session"
    metadata = runtime.after[0][2]
    assert metadata["hermes_platform"] == "telegram"
    assert metadata["hermes_agent_identity"] == "host-agent-7"
    assert metadata["hermes_agent_workspace"] == "host-workspace-9"
    assert metadata["hermes_parent_session_id"] == "parent-session-3"

    provider.shutdown()
    assert service.stopped is True
    uninstall_agent("hermes", target_dir=hermes_home)


def test_hermes_tools_and_private_content_sanitization(tmp_path):
    hermes_home, _, plugin_dir = _install(tmp_path)
    module, _ = _load_plugin(plugin_dir, tmp_path, "hermes_tool_test")
    service = _ServiceProbe()
    runtime = _RuntimeProbe()
    provider = module.MemplexMemoryProvider(
        service_factory=lambda: service,
        runtime_factory=lambda **_: runtime,
    )
    provider.initialize("s1", hermes_home=str(hermes_home))

    assert {schema["name"] for schema in provider.get_tool_schemas()} == {
        "memplex_search",
        "memplex_conclude",
    }
    result = json.loads(provider.handle_tool_call("memplex_search", {"query": "hello"}))
    assert result == {"context": "echo:hello", "total": 1}
    stored = json.loads(
        provider.handle_tool_call(
            "memplex_conclude",
            {"content": "public <private>secret</private> note"},
        )
    )
    assert stored == {"status": "stored"}
    assert runtime.captured[0][0] == "public  note"

    provider.sync_turn(
        "visible <private>hidden-token</private> text",
        "answer",
        messages=[{"role": "user", "content": "<private>hidden-token</private>"}],
    )
    assert provider._flush(timeout=2.0)
    user, _, metadata = runtime.after[0]
    assert user == "visible  text"
    assert "hidden-token" not in json.dumps(metadata)
    with pytest.raises(NotImplementedError):
        provider.handle_tool_call("unknown", {})
    provider.shutdown()
    uninstall_agent("hermes", target_dir=hermes_home)


def test_hermes_non_primary_context_does_not_write(tmp_path):
    hermes_home, _, plugin_dir = _install(tmp_path)
    module, _ = _load_plugin(plugin_dir, tmp_path, "hermes_subagent_test")
    runtime = _RuntimeProbe()
    provider = module.MemplexMemoryProvider(
        service_factory=_ServiceProbe,
        runtime_factory=lambda **_: runtime,
    )
    provider.initialize(
        "child",
        hermes_home=str(hermes_home),
        agent_context="subagent",
    )
    provider.sync_turn("do not store", "ignored")
    provider.on_session_end(
        [
            {"role": "user", "content": "do not store"},
            {"role": "assistant", "content": "ignored"},
        ]
    )
    skipped = json.loads(
        provider.handle_tool_call(
            "memplex_conclude",
            {"content": "subagent conclusion must not store"},
        )
    )
    assert skipped == {"status": "ignored", "reason": "non_primary_context"}
    assert runtime.captured == []
    assert runtime.after == []
    provider.shutdown()
    uninstall_agent("hermes", target_dir=hermes_home)


def test_hermes_capture_is_visible_to_codex_claude_and_openclaw(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("MEMPLEX_STORAGE_BACKEND", "lite")
    monkeypatch.setenv("MEMPLEX_STORAGE_PATH", str(tmp_path / "shared-store"))
    monkeypatch.setenv("MEMPLEX_LLM_QUERY_ENHANCEMENT", "false")
    hermes_home, workspace, plugin_dir = _install(tmp_path, user_id="shared-user")
    module, _ = _load_plugin(plugin_dir, tmp_path, "hermes_interop_test")
    provider = module.MemplexMemoryProvider()
    provider.initialize("hermes-session", hermes_home=str(hermes_home))
    canary = "hermes-cross-host-canary-4d71"
    provider.sync_turn(f"Remember {canary}", "Stored.")
    assert provider._flush(timeout=5.0)

    for agent in ("codex", "claude-code", "openclaw"):
        runtime = AgentMemoryRuntime(
            agent=agent,
            user_id="shared-user",
            session_id=f"{agent}-session",
            project_path=workspace,
        )
        try:
            assert canary in runtime.before_prompt(canary).context
        finally:
            runtime.service.stop()

    provider.shutdown()
    uninstall_agent("hermes", target_dir=hermes_home)
