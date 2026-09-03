"""Test the agent-installer registry and dispatch completeness.

Previously the registry refactor (P3.1) had no test asserting that every
supported agent has a working spec (evaluation: '_install_claude_code is
NOT end-to-end tested'). These tests pin the registry contract:
completeness vs AGENT_PROFILES, signature correctness, and dispatch
behaviour for known/unknown agents.
"""

import json
import os
import sys

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from pathlib import Path

import pytest

from memplex.adapters import agent_installer
from memplex.adapters.agent_installer import (
    _INSTALLERS,
    AgentInstallerSpec,
    _install_one,
    _uninstall_one,
    inspect_agent_installation,
    install_agent,
    uninstall_agent,
)
from memplex.adapters.agent_runtime import AGENT_PROFILES

# ── Registry completeness ────────────────────────────────────────────


def test_registry_covers_every_supported_agent_profile():
    """Every agent in AGENT_PROFILES must have an installer entry, so
    'setup --agent all' cannot silently skip a host."""
    missing = sorted(set(AGENT_PROFILES) - set(_INSTALLERS))
    assert missing == [], f"agents without installer: {missing}"


@pytest.mark.parametrize("agent", sorted(_INSTALLERS))
def test_every_registry_entry_has_install_and_uninstall(agent):
    spec = _INSTALLERS[agent]
    assert isinstance(spec, AgentInstallerSpec)
    assert callable(spec.install)
    assert callable(spec.uninstall)


def test_registry_needs_identity_flag_matches_signatures():
    """Native hosts persist identity; Claude Code delegates to its plugin bundle."""
    assert _INSTALLERS["codex"].needs_identity is True
    assert _INSTALLERS["claude-code"].needs_identity is True
    assert _INSTALLERS["openclaw"].needs_identity is True
    assert _INSTALLERS["hermes"].needs_identity is True


def test_managed_identity_loader_enforces_the_exact_lifecycle_contract(tmp_path):
    """Any schema widening or weak coercion would let a launcher change principal."""

    host_root = tmp_path / "host"
    source_root = tmp_path / "source"
    host_root.mkdir()
    source_root.mkdir()
    identity_path = tmp_path / "memplex-agent.json"
    valid = {
        "agent": "openclaw",
        "user_id": "alice",
        "project_path": str((tmp_path / "workspace").resolve()),
        "python": sys.executable,
        "source_root": str(source_root.resolve()),
        "host_root": str(host_root.resolve()),
        "managed": {
            "by": "memplex",
            "installer": "memplex",
            "schema_version": 1,
        },
    }
    loader = getattr(agent_installer, "load_managed_identity", None)
    assert callable(loader), "agent installer must expose the shared identity loader"

    identity_path.write_text(json.dumps(valid), encoding="utf-8")
    assert loader(
        identity_path,
        expected_agent="openclaw",
        expected_host_root=host_root,
    ) == valid

    other_host = tmp_path / "other-host"
    other_host.mkdir()
    with pytest.raises(ValueError, match="host_root.*reinstall required|reinstall required.*host_root"):
        loader(
            identity_path,
            expected_agent="openclaw",
            expected_host_root=other_host,
        )

    host_alias = tmp_path / "host-alias"
    host_alias.symlink_to(host_root, target_is_directory=True)
    assert loader(
        identity_path,
        expected_agent="openclaw",
        expected_host_root=host_alias,
    ) == valid

    invalid_payloads = []
    for field in valid:
        missing = dict(valid)
        missing.pop(field)
        invalid_payloads.append(json.dumps(missing))
    invalid_payloads.extend(
        [
            json.dumps({**valid, "unexpected": True}),
            json.dumps({**valid, "agent": "hermes"}),
            json.dumps({**valid, "user_id": 7}),
            json.dumps({**valid, "user_id": " alice "}),
            json.dumps({**valid, "project_path": "relative/workspace"}),
            json.dumps({**valid, "project_path": "/workspace\u0000suffix"}),
            json.dumps({**valid, "python": str(tmp_path / "missing-python")}),
            json.dumps({**valid, "source_root": str(tmp_path / "missing-source")}),
            json.dumps({**valid, "host_root": str(tmp_path / "missing-host")}),
            json.dumps({**valid, "managed": {**valid["managed"], "unexpected": True}}),
            json.dumps({**valid, "managed": {"installer": "memplex"}}),
            json.dumps({**valid, "managed": {**valid["managed"], "schema_version": True}}),
            json.dumps(valid).replace(
                '"agent": "openclaw"',
                '"agent": "openclaw", "agent": "openclaw"',
                1,
            ),
            json.dumps(valid).replace(
                '"by": "memplex"',
                '"by": "memplex", "by": "memplex"',
                1,
            ),
        ]
    )

    for raw in invalid_payloads:
        identity_path.write_text(raw, encoding="utf-8")
        with pytest.raises(ValueError, match="reinstall required"):
            loader(
                identity_path,
                expected_agent="openclaw",
                expected_host_root=host_root,
            )


# ── Dispatch behaviour ───────────────────────────────────────────────


def test_install_one_raises_for_unknown_agent():
    with pytest.raises(ValueError, match="Unsupported agent"):
        _install_one(
            "not-a-real-agent", target_dir=None, user_id=None, project_path=None, dry_run=True
        )


def test_uninstall_one_raises_for_unknown_agent():
    with pytest.raises(ValueError, match="Unsupported agent"):
        _uninstall_one("not-a-real-agent", target_dir=None, dry_run=True)


def test_install_one_dry_run_dispatches_to_codex_installer(tmp_path):
    """A known agent in dry-run must reach the per-agent installer and
    return an AgentInstallResult rather than raising."""
    result = _install_one(
        "codex", target_dir=tmp_path, user_id=None, project_path=None, dry_run=True
    )
    assert result.agent == "codex"
    assert result.action == "install"


def test_uninstall_one_dry_run_dispatches_to_codex(tmp_path):
    result = _uninstall_one("codex", target_dir=tmp_path, dry_run=True)
    assert result.agent == "codex"
    assert result.action == "uninstall"


def test_install_one_claude_code_reachable(tmp_path):
    """Claude-Code install path was previously untested end-to-end."""
    result = _install_one(
        "claude-code", target_dir=tmp_path, user_id=None, project_path=None, dry_run=True
    )
    assert result.agent == "claude-code"


# ── Registry is a stable reference (not rebuilt per call) ────────────


def test_registry_is_module_level_singleton():
    """Importing twice yields the same dict object (no per-import rebuild)."""
    from memplex.adapters.agent_installer import _INSTALLERS as again

    assert again is _INSTALLERS


def _assert_failed_install_restored(root: Path, config_name: str, original: str) -> None:
    config_path = root / config_name
    assert config_path.read_text() == original


def _snapshot_tree(root: Path):
    """Return an exact small-tree snapshot suitable for rollback assertions."""

    if not root.exists():
        return None
    snapshot = {}
    paths = [root, *sorted(root.rglob("*"))]
    for path in paths:
        relative = "." if path == root else str(path.relative_to(root))
        mode = path.lstat().st_mode & 0o7777
        if path.is_symlink():
            snapshot[relative] = ("symlink", mode, os.readlink(path))
        elif path.is_dir():
            snapshot[relative] = ("dir", mode)
        else:
            snapshot[relative] = ("file", mode, path.read_bytes())
    return snapshot


def test_codex_single_host_failure_restores_preinstall_state(tmp_path, monkeypatch):
    original = 'model = "gpt-5.5"\n'
    (tmp_path / "config.toml").write_text(original)

    def fail_final_config(*_args, **_kwargs):
        raise OSError("injected codex config failure")

    monkeypatch.setattr(agent_installer, "_replace_managed_block", fail_final_config)
    with pytest.raises(OSError, match="injected codex"):
        install_agent("codex", target_dir=tmp_path, user_id="alice", project_path=tmp_path)

    _assert_failed_install_restored(tmp_path, "config.toml", original)
    assert not (tmp_path / "plugins" / "marketplaces" / "memplex").exists()
    assert not (tmp_path / "plugins" / "cache" / "memplex").exists()


def test_all_host_failure_restores_exact_preexisting_managed_state(tmp_path):
    """A later host failure must not uninstall an earlier preexisting host."""

    target = tmp_path / "agents"
    target.mkdir()
    install_agent(
        "codex",
        target_dir=target,
        user_id="alice",
        project_path=tmp_path / "workspace-a",
    )
    config_path = target / "config.toml"
    config_path.write_text(config_path.read_text() + "\n# caller-owned sentinel\n")
    config_path.chmod(0o3640)
    marketplace_path = target / "plugins" / "marketplaces" / "memplex"
    marketplace_referent = tmp_path / "codex-marketplace-referent"
    marketplace_path.rename(marketplace_referent)
    marketplace_path.symlink_to(marketplace_referent, target_is_directory=True)
    provider_path = target / "memplex.json"
    provider_path.write_text('{"name":"memplex","provider":"custom"}\n')
    (target / "caller-owned.txt").write_text("preserve me exactly\n")
    before = _snapshot_tree(target)
    referent_before = _snapshot_tree(marketplace_referent)

    with pytest.raises(RuntimeError, match="Failed to install hermes"):
        install_agent(
            "all",
            target_dir=target,
            user_id="alice",
            project_path=tmp_path / "workspace-b",
        )

    assert _snapshot_tree(target) == before
    assert _snapshot_tree(marketplace_referent) == referent_before


def test_all_host_second_uninstall_failure_restores_exact_preuninstall_state(tmp_path, monkeypatch):
    """A second-host uninstall failure restores the first host and untouched hosts exactly."""

    target = tmp_path / "agents"
    target.mkdir()
    install_agent(
        "all",
        target_dir=target,
        user_id="alice",
        project_path=tmp_path / "workspace",
    )
    config_path = target / "config.toml"
    config_referent = tmp_path / "codex-config-referent.toml"
    config_path.rename(config_referent)
    config_path.symlink_to(config_referent)
    config_referent.chmod(0o640)
    before = _snapshot_tree(target)
    referent_before = _snapshot_tree(config_referent)

    def fail_claude_uninstall(*_args, **_kwargs):
        raise OSError("injected claude uninstall failure")

    monkeypatch.setitem(
        agent_installer._INSTALLERS,
        "claude-code",
        AgentInstallerSpec(
            install=agent_installer._INSTALLERS["claude-code"].install,
            uninstall=fail_claude_uninstall,
            needs_identity=True,
        ),
    )
    with pytest.raises(RuntimeError, match="Failed to uninstall claude-code"):
        uninstall_agent("all", target_dir=target)

    assert _snapshot_tree(target) == before
    assert _snapshot_tree(config_referent) == referent_before


def test_install_snapshot_restores_a_dangling_symbolic_link(tmp_path):
    """A dangling managed link is preexisting state, not an absent path."""

    referent = tmp_path / "future-managed-target"
    link = tmp_path / "managed-link"
    link.symlink_to(referent)
    original_target = os.readlink(link)
    snapshots, snapshot_root = agent_installer._snapshot_install_paths([link])
    referent.write_text("created during failed install\n")

    errors = agent_installer._restore_install_snapshot(snapshots, snapshot_root)

    assert errors == []
    assert link.is_symlink()
    assert os.readlink(link) == original_target
    assert not referent.exists()


def test_claude_single_host_failure_restores_preinstall_state(tmp_path, monkeypatch):
    original_write_text = Path.write_text

    def fail_marker(path, *args, **kwargs):
        if path.name == ".memplex-install-state.json":
            raise OSError("injected claude marker failure")
        return original_write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", fail_marker)
    with pytest.raises(OSError, match="injected claude"):
        install_agent("claude-code", target_dir=tmp_path, user_id="alice", project_path=tmp_path)

    assert not (tmp_path / "plugins" / "marketplaces" / "articultur").exists()
    assert not (tmp_path / "plugins" / "cache" / "articultur" / "memplex").exists()
    assert not (tmp_path / "settings.json").exists()
    assert not (tmp_path / "plugins" / "known_marketplaces.json").exists()
    assert not (tmp_path / "plugins" / "installed_plugins.json").exists()


def test_claude_install_uninstall_restores_exact_registry_prestate(tmp_path):
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    settings = tmp_path / "settings.json"
    known = plugins / "known_marketplaces.json"
    installed = plugins / "installed_plugins.json"
    settings.write_text('{\n  // caller comment\n  "theme": "dark"\n}\n')
    known.write_text('{"other":{"installLocation":"/caller"}}\n')
    installed.write_text('{"version":2,"plugins":{"other@caller":[]}}\n')
    settings.chmod(0o640)
    before = {
        path: (path.read_bytes(), path.stat().st_mode & 0o777)
        for path in (settings, known, installed)
    }

    install_agent(
        "claude-code",
        target_dir=tmp_path,
        user_id="alice",
        project_path=tmp_path,
    )

    assert (
        tmp_path
        / "plugins"
        / "cache"
        / "articultur"
        / "memplex"
        / agent_installer._package_version()
    ).is_dir()
    uninstall_agent("claude-code", target_dir=tmp_path)

    for path, (content, mode) in before.items():
        assert path.read_bytes() == content
        assert path.stat().st_mode & 0o777 == mode
    assert not (tmp_path / "plugins" / "marketplaces" / "articultur").exists()
    assert not (tmp_path / "plugins" / "cache" / "articultur" / "memplex").exists()


def test_claude_uninstall_preserves_registry_drift_and_removes_only_memplex(tmp_path):
    install_agent(
        "claude-code",
        target_dir=tmp_path,
        user_id="alice",
        project_path=tmp_path,
    )
    settings_path = tmp_path / "settings.json"
    known_path = tmp_path / "plugins" / "known_marketplaces.json"
    installed_path = tmp_path / "plugins" / "installed_plugins.json"

    settings = json.loads(settings_path.read_text())
    settings["callerAfterInstall"] = True
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    known = json.loads(known_path.read_text())
    known["caller"] = {"installLocation": "/caller"}
    known_path.write_text(json.dumps(known, indent=2) + "\n")
    installed = json.loads(installed_path.read_text())
    installed["plugins"]["caller@local"] = []
    installed_path.write_text(json.dumps(installed, indent=2) + "\n")

    uninstall_agent("claude-code", target_dir=tmp_path)

    settings = json.loads(settings_path.read_text())
    assert settings["callerAfterInstall"] is True
    assert "memplex@articultur" not in settings.get("enabledPlugins", {})
    assert "articultur" not in settings.get("extraKnownMarketplaces", {})
    assert "articultur" not in json.loads(known_path.read_text())
    assert json.loads(known_path.read_text())["caller"]["installLocation"] == "/caller"
    assert "memplex@articultur" not in json.loads(installed_path.read_text())["plugins"]
    assert "caller@local" in json.loads(installed_path.read_text())["plugins"]


@pytest.mark.parametrize("host", ["codex", "claude-code", "openclaw", "hermes"])
def test_four_host_reinstall_upgrade_preserves_healthy_state_and_prestate(tmp_path, host):
    root = tmp_path / host
    root.mkdir()
    originals = {
        "codex": (root / "config.toml", 'model = "caller"\n'),
        "claude-code": (root / "settings.json", '{"theme":"dark"}\n'),
        "openclaw": (
            root / "openclaw.json",
            json.dumps(
                {
                    "plugins": {
                        "slots": {"memory": "other"},
                        "entries": {},
                        "allow": ["other"],
                        "bundledDiscovery": "allowlist",
                    }
                },
                separators=(",", ":"),
            )
            + "\n",
        ),
        "hermes": (root / "config.yaml", "memory:\n  provider: caller\n"),
    }
    config_path, original = originals[host]
    config_path.write_text(original)

    install_agent(host, target_dir=root, user_id="alice", project_path=tmp_path)
    install_agent(host, target_dir=root, user_id="alice", project_path=tmp_path)

    status = inspect_agent_installation(host, target_dir=root)
    assert status["status"] == "healthy"
    assert status["install_state"] == {
        "installed": True,
        "selected": True,
        "managed": True,
        "reinstall_needed": False,
    }

    uninstall_agent(host, target_dir=root)
    assert config_path.read_text() == original


def test_snapshot_cleanup_failure_is_visible_in_logs(tmp_path, monkeypatch, caplog):
    def fail_cleanup(_path):
        raise OSError("snapshot cleanup unavailable")

    monkeypatch.setattr(agent_installer.shutil, "rmtree", fail_cleanup)
    agent_installer._cleanup_snapshot_root(tmp_path / "snapshot")

    assert "snapshot cleanup unavailable" in caplog.text


@pytest.mark.parametrize(
    ("agent", "config_name", "managed_paths"),
    [
        ("openclaw", "openclaw.json", ("extensions/memplex",)),
        ("hermes", "config.yaml", ("memplex.json", "plugins/memplex")),
    ],
)
def test_configured_host_failure_restores_preinstall_state(
    tmp_path, monkeypatch, agent, config_name, managed_paths
):
    original = "{}\n" if agent == "openclaw" else "model: test\n"
    (tmp_path / config_name).write_text(original)
    original_atomic_write = agent_installer._write_text_atomic

    def fail_config(path, text):
        if path.name == config_name:
            raise OSError(f"injected {agent} config failure")
        return original_atomic_write(path, text)

    monkeypatch.setattr(agent_installer, "_write_text_atomic", fail_config)
    with pytest.raises(OSError, match=f"injected {agent}"):
        install_agent(agent, target_dir=tmp_path, user_id="alice", project_path=tmp_path)

    _assert_failed_install_restored(tmp_path, config_name, original)
    for relative in managed_paths:
        assert not (tmp_path / relative).exists()
