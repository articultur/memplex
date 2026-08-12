"""One-command install and uninstall helpers for agent integrations."""

from __future__ import annotations

import getpass
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable

from memplex.adapters._shared import get_plugin_source_dir as _get_plugin_source_dir
from memplex.adapters._shared import marketplace_json as _marketplace_json
from memplex.adapters.agent_runtime import get_agent_manifest
from memplex.adapters.jsonc_edit import remove_jsonc_path, set_jsonc_path
from memplex.adapters.managed_identity import (
    ManagedIdentityError,
    load_managed_identity,
    validate_managed_identity,
)
from memplex.adapters.runtime_status import read_runtime_status, runtime_status_path
from memplex.adapters.yaml_edit import (
    remove_yaml_path,
    set_yaml_scalar_path,
    yaml_path_value,
)

logger = logging.getLogger(__name__)

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
    whether the installer takes ``(target_dir, user_id, project_path, ...)``.
    All supported hosts persist or generate a stable identity.
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
    transaction: tuple[
        dict[Path, _InstallSnapshot],
        Path,
        list[Path],
    ] | None = None
    if len(names) > 1 and not dry_run:
        mutation_paths: list[Path] = []
        created_directories: set[Path] = set()
        for name in names:
            root, host_paths = _agent_install_mutation_paths(name, target_dir)
            mutation_paths.extend(host_paths)
            created_directories.update(_missing_install_directories(root, host_paths))
        unique_paths = list(dict.fromkeys(mutation_paths))
        snapshots, snapshot_root = _snapshot_install_paths(unique_paths)
        transaction = (
            snapshots,
            snapshot_root,
            sorted(created_directories, key=lambda path: len(path.parts), reverse=True),
        )
    try:
        for name in names:
            result = _install_one(
                name,
                target_dir=target_dir,
                user_id=user_id,
                project_path=project_path,
                dry_run=dry_run,
            )
            results.append(result)
            installed.append(name)
    except Exception as exc:
        if transaction is not None:
            snapshots, snapshot_root, created_directories = transaction
            rollback_errors = _restore_install_snapshot(snapshots, snapshot_root)
            rollback_errors.extend(_remove_created_install_directories(created_directories))
            transaction = None
            restored = ", ".join(reversed(installed)) or "none completed"
            detail = f" Rolled back installed agents to exact preinstall state: {restored}."
            if rollback_errors:
                detail += f" Rollback errors: {'; '.join(rollback_errors)}."
            raise RuntimeError(f"Failed to install {name}: {exc}.{detail}") from exc
        raise
    finally:
        if transaction is not None:
            _cleanup_snapshot_root(transaction[1])
    return results


def uninstall_agent(
    agent: str,
    *,
    target_dir: str | Path | None = None,
    dry_run: bool = False,
) -> list[AgentInstallResult]:
    """Remove Memplex integration from one or all supported agent hosts."""

    names = _expand_agents(agent)
    results: list[AgentInstallResult] = []
    removed: list[str] = []
    transaction: tuple[dict[Path, _InstallSnapshot], Path] | None = None
    if len(names) > 1 and not dry_run:
        mutation_paths: list[Path] = []
        for name in names:
            _, host_paths = _agent_install_mutation_paths(name, target_dir)
            mutation_paths.extend(host_paths)
        snapshots, snapshot_root = _snapshot_install_paths(dict.fromkeys(mutation_paths))
        transaction = snapshots, snapshot_root
    try:
        for name in names:
            result = _uninstall_one(name, target_dir=target_dir, dry_run=dry_run)
            results.append(result)
            removed.append(name)
    except Exception as exc:
        if transaction is not None:
            snapshots, snapshot_root = transaction
            rollback_errors = _restore_install_snapshot(snapshots, snapshot_root)
            transaction = None
            restored = ", ".join(removed) or "none completed"
            detail = f" Rolled back uninstalled agents to exact preuninstall state: {restored}."
            if rollback_errors:
                detail += f" Rollback errors: {'; '.join(rollback_errors)}."
            raise RuntimeError(f"Failed to uninstall {name}: {exc}.{detail}") from exc
        raise
    finally:
        if transaction is not None:
            _cleanup_snapshot_root(transaction[1])
    return results


def inspect_agent_installation(
    agent: str,
    *,
    target_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Inspect one host integration without changing files or configuration."""

    selected_host = get_agent_manifest(agent)["name"]
    root, paths = _agent_installation_paths(selected_host, target_dir)
    missing_paths: list[str] = []
    drift_reasons: list[str] = []
    identity: dict[str, Any] = {
        "user_id": None,
        "project_path": None,
        "source": "runtime",
    }
    configured_visibility = "workspace"
    installed = False
    selected = False
    managed = False
    identity_valid = False
    footprint = False

    if selected_host == "codex":
        config_path = paths["config"]
        marketplace_root = paths["marketplace_root"]
        marker_path = paths["managed_marker"]
        identity_path = paths["identity"]
        required = {
            "marketplace_manifest": paths["marketplace_manifest"],
            "plugin": paths["plugin"],
            "plugin_cache": paths["plugin_cache"],
            "managed_marker": marker_path,
            "identity": identity_path,
        }
        config_text = _safe_read_text(config_path, drift_reasons)
        selected = bool(
            MANAGED_BEGIN in config_text
            and MANAGED_END in config_text
            and '[plugins."memplex@memplex"]' in config_text
        )
        managed = _is_managed_json_file(marker_path) and _is_managed_json_file(identity_path)
        installed = _required_paths_exist(required, missing_paths)
        footprint = (
            selected
            or marketplace_root.exists()
            or any(path.exists() for path in required.values())
        )
        identity, identity_valid = _installation_identity(
            identity_path,
            expected_agent=selected_host,
            expected_host_root=root,
            errors=drift_reasons,
        )
        managed = managed and identity_valid
    elif selected_host == "claude-code":
        market_dir = paths["marketplace_root"]
        marker_path = paths["managed_marker"]
        identity_path = paths["identity"]
        required = {
            "marketplace_manifest": paths["marketplace_manifest"],
            "plugin": paths["plugin"],
            "plugin_cache": paths["plugin_cache"],
            "managed_marker": marker_path,
            "identity": identity_path,
        }
        installed = _required_paths_exist(required, missing_paths)
        settings = (
            _safe_read_json(paths["config"], drift_reasons)
            if paths["config"].exists()
            else {}
        )
        enabled = settings.get("enabledPlugins", {}) if isinstance(settings, dict) else {}
        selected = isinstance(enabled, dict) and enabled.get("memplex@articultur") is True
        marker = _safe_read_json(marker_path, drift_reasons)
        managed = _is_managed_payload(marker) and _is_managed_json_file(identity_path)
        footprint = market_dir.exists() or any(path.exists() for path in required.values())
        identity, identity_valid = _installation_identity(
            identity_path,
            expected_agent=selected_host,
            expected_host_root=root,
            errors=drift_reasons,
        )
        managed = managed and identity_valid
    elif selected_host == "openclaw":
        config_path = paths["config"]
        extension_dir = paths["plugin"]
        identity_path = paths["identity"]
        required = {
            "plugin_manifest": paths["plugin_manifest"],
            "plugin_entry": paths["plugin_entry"],
            "plugin_runtime": paths["plugin_runtime"],
            "managed_marker": paths["managed_marker"],
            "identity": identity_path,
        }
        config = _safe_read_json(config_path, drift_reasons) if config_path.exists() else {}
        plugins = config.get("plugins", {}) if isinstance(config, dict) else {}
        slots = plugins.get("slots", {}) if isinstance(plugins, dict) else {}
        entries = plugins.get("entries", {}) if isinstance(plugins, dict) else {}
        entry = entries.get("memplex", {}) if isinstance(entries, dict) else {}
        selected = isinstance(slots, dict) and slots.get("memory") == "memplex"
        managed = _is_managed_openclaw_extension(extension_dir) and _is_managed_openclaw_entry(
            entry
        )
        configured_visibility = str(
            entry.get("config", {}).get("visibility", "workspace")
            if isinstance(entry, dict)
            else "workspace"
        )
        installed = _required_paths_exist(required, missing_paths)
        footprint = (
            extension_dir.exists()
            or bool(entry)
            or (isinstance(slots, dict) and slots.get("memory") == "memplex")
        )
        identity, identity_valid = _installation_identity(
            identity_path,
            expected_agent=selected_host,
            expected_host_root=root,
            errors=drift_reasons,
        )
        managed = managed and identity_valid
    else:
        config_path = paths["config"]
        provider_path = paths["provider_config"]
        plugin_dir = paths["plugin"]
        identity_path = paths["identity"]
        required = {
            "provider_config": provider_path,
            "plugin_manifest": paths["plugin_manifest"],
            "plugin_bootstrap": paths["plugin_bootstrap"],
            "managed_marker": paths["managed_marker"],
            "identity": identity_path,
        }
        config_text = _safe_read_text(config_path, drift_reasons)
        try:
            provider_present, provider_name = yaml_path_value(
                config_text,
                ("memory", "provider"),
            )
            selected = provider_present and provider_name == "memplex"
        except ValueError as exc:
            drift_reasons.append(f"cannot parse provider selector: {exc}")
        managed = _is_managed_json_file(provider_path) and _is_managed_hermes_plugin(plugin_dir)
        provider = _safe_read_json(provider_path, drift_reasons) if provider_path.exists() else {}
        configured_visibility = str(provider.get("visibility", "workspace"))
        installed = _required_paths_exist(required, missing_paths)
        footprint = plugin_dir.exists() or provider_path.exists() or selected
        identity, identity_valid = _installation_identity(
            identity_path,
            expected_agent=selected_host,
            expected_host_root=root,
            errors=drift_reasons,
        )
        managed = managed and identity_valid

    if footprint and not selected:
        drift_reasons.append("memory provider is not selected")
    if footprint and not managed:
        drift_reasons.append("Memplex ownership markers are missing or invalid")
    if footprint and missing_paths:
        drift_reasons.append("required integration files are missing")

    if installed and selected and managed:
        status = "healthy"
    elif not footprint:
        status = "not_installed"
        missing_paths = []
        drift_reasons = []
    elif not managed:
        status = "unmanaged"
    else:
        status = "drifted"

    runtime_status = read_runtime_status(runtime_status_path(root), agent=selected_host)
    if status == "healthy" and runtime_status["state"] == "degraded":
        status = "degraded"

    serialised_paths = {name: str(path) for name, path in paths.items()}
    return {
        "schema_version": 1,
        "selected_host": selected_host,
        "status": status,
        "configured_visibility": configured_visibility,
        "identity": identity,
        "paths": serialised_paths,
        "install_state": {
            "installed": installed,
            "selected": selected,
            "managed": managed,
            "reinstall_needed": status in {"drifted", "unmanaged"},
        },
        "missing_paths": missing_paths,
        "drift_reasons": list(dict.fromkeys(drift_reasons)),
        "runtime_status": runtime_status,
    }


def _agent_installation_paths(
    agent: str,
    target_dir: str | Path | None,
) -> tuple[Path, dict[str, Path]]:
    specs = {
        "codex": ("CODEX_HOME", ".codex"),
        "claude-code": ("CLAUDE_CONFIG_DIR", ".claude"),
        "openclaw": ("OPENCLAW_CONFIG_DIR", ".openclaw"),
        "hermes": ("HERMES_CONFIG_DIR", ".hermes"),
    }
    env_name, default_name = specs[agent]
    root = _target_dir(target_dir, env_name, default_name)
    if agent == "codex":
        marketplace_root = root / "plugins" / "marketplaces" / "memplex"
        plugin = marketplace_root / "plugin"
        return root, {
            "root": root,
            "config": root / "config.toml",
            "marketplace_root": marketplace_root,
            "marketplace_manifest": marketplace_root / ".agents" / "plugins" / "marketplace.json",
            "plugin": plugin,
            "plugin_cache": root / "plugins" / "cache" / "memplex" / "memplex" / _package_version(),
            "managed_marker": marketplace_root / ".memplex-managed.json",
            "identity": plugin / "memplex-agent.json",
        }
    if agent == "claude-code":
        marketplace_root = root / "plugins" / "marketplaces" / "articultur"
        plugin_cache_root = root / "plugins" / "cache" / "articultur" / "memplex"
        return root, {
            "root": root,
            "config": root / "settings.json",
            "known_marketplaces": root / "plugins" / "known_marketplaces.json",
            "installed_plugins": root / "plugins" / "installed_plugins.json",
            "marketplace_root": marketplace_root,
            "marketplace_manifest": marketplace_root / ".claude-plugin" / "marketplace.json",
            "plugin": marketplace_root / "plugin",
            "plugin_cache_root": plugin_cache_root,
            "plugin_cache": plugin_cache_root / _package_version(),
            "managed_marker": marketplace_root / ".memplex-install-state.json",
            "identity": marketplace_root / "plugin" / "memplex-agent.json",
        }
    if agent == "openclaw":
        plugin = root / "extensions" / "memplex"
        return root, {
            "root": root,
            "config": root / "openclaw.json",
            "plugin": plugin,
            "plugin_manifest": plugin / "openclaw.plugin.json",
            "plugin_entry": plugin / "plugin.json",
            "plugin_runtime": plugin / "index.js",
            "managed_marker": plugin / ".memplex-install-state.json",
            "identity": plugin / "memplex-agent.json",
        }
    plugin = root / "plugins" / "memplex"
    return root, {
        "root": root,
        "config": root / "config.yaml",
        "provider_config": root / "memplex.json",
        "plugin": plugin,
        "plugin_manifest": plugin / "plugin.yaml",
        "plugin_bootstrap": plugin / "__init__.py",
        "managed_marker": plugin / ".memplex-install-state.json",
        "identity": plugin / "memplex-agent.json",
    }


def _agent_install_mutation_paths(
    agent: str,
    target_dir: str | Path | None,
) -> tuple[Path, list[Path]]:
    """Return the exact top-level paths a host installer may mutate."""

    root, paths = _agent_installation_paths(agent, target_dir)
    if agent == "codex":
        return root, [
            paths["config"],
            paths["marketplace_root"],
            root / "plugins" / "cache" / "memplex",
        ]
    if agent == "claude-code":
        return root, [
            paths["config"],
            paths["known_marketplaces"],
            paths["installed_plugins"],
            paths["marketplace_root"],
            paths["plugin_cache_root"],
        ]
    if agent == "openclaw":
        return root, [paths["config"], paths["plugin"]]
    return root, [
        paths["config"],
        paths["provider_config"],
        root / "memory-providers" / "memplex.json",
        root / "plugins" / "memory" / "memplex",
        paths["plugin"],
    ]


def _missing_install_directories(root: Path, paths: Iterable[Path]) -> set[Path]:
    """Record absent parent directories so rollback can remove empty residue."""

    missing: set[Path] = set()
    for path in paths:
        parent = path.parent
        while True:
            if not parent.exists():
                missing.add(parent)
            if parent == root:
                break
            if root not in parent.parents:
                break
            parent = parent.parent
    return missing


def _remove_created_install_directories(paths: Iterable[Path]) -> list[str]:
    """Remove only transaction-created directories that are now empty."""

    errors: list[str] = []
    for path in paths:
        try:
            path.rmdir()
        except FileNotFoundError:
            continue
        except OSError as exc:
            errors.append(f"{path}: {exc}")
    return errors


def _required_paths_exist(required: dict[str, Path], missing: list[str]) -> bool:
    for name, path in required.items():
        if not path.exists():
            missing.append(name)
    return not missing


@dataclass
class _InstallSnapshot:
    target: Path
    existed: bool
    is_dir: bool
    backup: Path | None
    mode: int | None
    is_symlink: bool = False
    link_target: str | None = None
    link_mode: int | None = None
    referent: Path | None = None
    referent_existed: bool = False


def _path_lexists(path: Path) -> bool:
    """Return true for regular paths and dangling symbolic links."""

    return os.path.lexists(os.fspath(path))


def _remove_install_path(path: Path) -> None:
    """Remove one managed path without following a symbolic link."""

    if not _path_lexists(path):
        return
    if path.is_symlink() or not path.is_dir():
        path.unlink()
    else:
        shutil.rmtree(path)


def _copy_install_snapshot(source: Path, backup: Path) -> tuple[bool, int]:
    """Copy one concrete file/directory and return its type and full mode."""

    mode = source.stat().st_mode & 0o7777
    if source.is_dir():
        shutil.copytree(source, backup, symlinks=True)
        return True, mode
    shutil.copy2(source, backup)
    return False, mode


def _snapshot_install_paths(paths: Iterable[Path]) -> tuple[dict[Path, _InstallSnapshot], Path]:
    """Capture preinstall state for a list of candidate target paths."""

    snapshot_root = Path(tempfile.mkdtemp(prefix="memplex-installer-backup-"))
    snapshots: dict[Path, _InstallSnapshot] = {}
    try:
        for index, path in enumerate(paths):
            if not _path_lexists(path):
                snapshots[path] = _InstallSnapshot(
                    target=path,
                    existed=False,
                    is_dir=False,
                    backup=None,
                    mode=None,
                )
                continue
            backup = snapshot_root / f"{index}-{path.name}"
            if path.is_symlink():
                link_target = os.readlink(path)
                raw_referent = Path(link_target)
                referent = (
                    raw_referent
                    if raw_referent.is_absolute()
                    else path.parent / raw_referent
                ).absolute()
                if referent == path.absolute() or referent.is_symlink():
                    raise ValueError(
                        f"Managed install path uses an unsupported cyclic or chained symlink: {path}"
                    )
                referent_existed = _path_lexists(referent)
                if referent_existed:
                    is_dir, mode = _copy_install_snapshot(referent, backup)
                else:
                    is_dir, mode = False, None
                snapshots[path] = _InstallSnapshot(
                    target=path,
                    existed=True,
                    is_dir=is_dir,
                    backup=backup if referent_existed else None,
                    mode=mode,
                    is_symlink=True,
                    link_target=link_target,
                    link_mode=path.lstat().st_mode & 0o7777,
                    referent=referent,
                    referent_existed=referent_existed,
                )
                continue
            is_dir, mode = _copy_install_snapshot(path, backup)
            snapshots[path] = _InstallSnapshot(
                target=path,
                existed=True,
                is_dir=is_dir,
                backup=backup,
                mode=mode,
            )
    except Exception:
        _cleanup_snapshot_root(snapshot_root)
        raise
    return snapshots, snapshot_root


def _restore_concrete_install_path(path: Path, state: _InstallSnapshot) -> None:
    """Restore the concrete file/directory represented by a snapshot."""

    if state.backup is None:
        raise RuntimeError("missing backup for managed install path")
    _remove_install_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if state.is_dir:
        shutil.copytree(state.backup, path, symlinks=True)
    else:
        shutil.copy2(state.backup, path)
    if state.mode is not None:
        path.chmod(state.mode)


def _restore_symlink_install_path(path: Path, state: _InstallSnapshot) -> None:
    """Restore both a managed symlink and the concrete referent it exposed."""

    if state.link_target is None or state.referent is None:
        raise RuntimeError("missing symbolic-link snapshot metadata")
    if state.referent_existed:
        _restore_concrete_install_path(state.referent, state)
    else:
        _remove_install_path(state.referent)
    _remove_install_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.symlink_to(state.link_target, target_is_directory=state.is_dir)
    if state.link_mode is not None and hasattr(os, "lchmod"):
        os.lchmod(path, state.link_mode)


def _restore_install_snapshot(
    snapshots: dict[Path, _InstallSnapshot],
    snapshot_root: Path,
) -> list[str]:
    errors: list[str] = []
    for path, state in snapshots.items():
        try:
            if state.existed:
                if state.is_symlink:
                    _restore_symlink_install_path(path, state)
                else:
                    _restore_concrete_install_path(path, state)
            else:
                _remove_install_path(path)
        except Exception as exc:
            errors.append(f"{path}: {exc}")
    try:
        shutil.rmtree(snapshot_root)
    except Exception as exc:
        errors.append(f"{snapshot_root}: {exc}")
    return errors


def _cleanup_snapshot_root(snapshot_root: Path) -> None:
    try:
        shutil.rmtree(snapshot_root)
    except FileNotFoundError:
        return
    except OSError as exc:
        logger.warning("installer snapshot cleanup failed for %s: %s", snapshot_root, exc)


def _safe_read_text(path: Path, errors: list[str]) -> str:
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        errors.append(f"cannot read {path.name}: {exc}")
        return ""


def _safe_read_json(path: Path, errors: list[str]) -> dict[str, Any]:
    try:
        return _read_json(path)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        errors.append(f"cannot parse {path.name}: {exc}")
        return {}


def _installation_identity(
    path: Path,
    *,
    expected_agent: str,
    expected_host_root: Path,
    errors: list[str],
) -> tuple[dict[str, Any], bool]:
    try:
        payload = load_managed_identity(
            path,
            expected_agent=expected_agent,
            expected_host_root=expected_host_root,
        )
    except ManagedIdentityError as exc:
        errors.append(f"managed identity invalid: {exc}")
        return {
            "user_id": None,
            "project_path": None,
            "source": str(path) if path.exists() else "runtime",
        }, False
    return {
        "user_id": payload["user_id"],
        "project_path": payload["project_path"],
        "source": str(path),
    }, True


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


def _install_codex(
    target_dir: str | Path | None,
    user_id: str | None,
    project_path: str | Path | None,
    *,
    dry_run: bool,
) -> AgentInstallResult:
    root = _target_dir(target_dir, "CODEX_HOME", ".codex")
    config_path = root / "config.toml"
    marketplace_root = root / "plugins" / "marketplaces" / "memplex"
    plugin_target = marketplace_root / "plugin"
    marketplace_path = marketplace_root / ".agents" / "plugins" / "marketplace.json"
    marker_path = marketplace_root / ".memplex-managed.json"
    identity_path = plugin_target / "memplex-agent.json"
    cache_marketplace_root = root / "plugins" / "cache" / "memplex"
    cache_plugin_target = cache_marketplace_root / "memplex" / _package_version()
    resolved_user_id = _resolved_user_id(user_id)
    resolved_project_path = _resolved_project_path(project_path)
    source_root = str(Path(__file__).resolve().parents[2])
    block = "\n".join(
        [
            MANAGED_BEGIN,
            "[marketplaces.memplex]",
            'source_type = "local"',
            f"source = {json.dumps(str(marketplace_root))}",
            "",
            '[plugins."memplex@memplex"]',
            "enabled = true",
            MANAGED_END,
            "",
        ]
    )

    if not dry_run:
        snapshots, snapshot_root = _snapshot_install_paths(
            [config_path, marketplace_root, cache_marketplace_root]
        )
        root.mkdir(parents=True, exist_ok=True)
        try:
            existing = config_path.read_text() if config_path.exists() else ""
            if _has_unmanaged_codex_memplex_table(existing):
                raise ValueError(
                    "Codex config already contains an unmanaged [mcp_servers.memplex], "
                    '[marketplaces.memplex], or [plugins."memplex@memplex"] table. '
                    "Remove or rename it before running memplex agent install."
                )
            if marketplace_root.exists() and not _is_managed_json_file(marker_path):
                raise ValueError(
                    f"Codex marketplace path already exists and is not managed by Memplex: "
                    f"{marketplace_root}"
                )
            if cache_marketplace_root.exists() and not _is_managed_json_file(marker_path):
                raise ValueError(
                    f"Codex plugin cache already exists and is not managed by Memplex: "
                    f"{cache_marketplace_root}"
                )

            source = _get_plugin_source_dir()
            if plugin_target.exists():
                shutil.rmtree(plugin_target)
            marketplace_root.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, plugin_target, symlinks=False, ignore=_ignore_patterns)
            marketplace_path.parent.mkdir(parents=True, exist_ok=True)
            marketplace_path.write_text(_marketplace_json().strip() + "\n")
            managed = {
                "by": "memplex",
                "installer": "memplex",
                "schema_version": 1,
            }
            _write_json(
                identity_path,
                _managed_identity_payload(
                    agent="codex",
                    user_id=resolved_user_id,
                    project_path=resolved_project_path,
                    source_root=source_root,
                    host_root=str(root.resolve()),
                    managed=managed,
                ),
            )
            _write_json(
                marker_path,
                {
                    "version": _package_version(),
                    "installed_at": datetime.now().isoformat(),
                    "managed": managed,
                },
            )
            if cache_marketplace_root.exists():
                shutil.rmtree(cache_marketplace_root)
            cache_plugin_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(plugin_target, cache_plugin_target, symlinks=False)
            config_path.write_text(_replace_managed_block(existing, block))
        except Exception:
            restore_errors = _restore_install_snapshot(snapshots, snapshot_root)
            if restore_errors:
                msg = "; ".join(restore_errors)
                raise RuntimeError(
                    "Memplex Codex install failed and rollback encountered errors: " + msg
                )
            raise
        finally:
            _cleanup_snapshot_root(snapshot_root)

    return AgentInstallResult(
        agent="codex",
        action="install",
        status="planned" if dry_run else "installed",
        files=[
            str(config_path),
            str(marketplace_path),
            str(plugin_target),
            str(cache_plugin_target),
            str(identity_path),
        ],
        message="Installed the native Memplex Codex plugin with MCP, hooks, and skills.",
        next_steps=["Restart Codex, then confirm the Memplex plugin and MCP tools are loaded."],
    )


def _uninstall_codex(target_dir: str | Path | None, *, dry_run: bool) -> AgentInstallResult:
    root = _target_dir(target_dir, "CODEX_HOME", ".codex")
    config_path = root / "config.toml"
    marketplace_root = root / "plugins" / "marketplaces" / "memplex"
    marker_path = marketplace_root / ".memplex-managed.json"
    cache_marketplace_root = root / "plugins" / "cache" / "memplex"
    if config_path.exists():
        next_text = _remove_managed_block(config_path.read_text())
        if not dry_run:
            config_path.write_text(next_text)
    managed_install = _is_managed_json_file(marker_path)
    if managed_install and not dry_run:
        if marketplace_root.exists():
            shutil.rmtree(marketplace_root)
        if cache_marketplace_root.exists():
            shutil.rmtree(cache_marketplace_root)
    return AgentInstallResult(
        agent="codex",
        action="uninstall",
        status="planned" if dry_run else "uninstalled",
        files=[str(config_path), str(marketplace_root), str(cache_marketplace_root)],
        message="Removed the managed Memplex Codex plugin and config block.",
        next_steps=["Restart Codex to confirm the plugin and MCP tools are gone."],
    )


def _install_claude_code(
    target_dir: str | Path | None,
    user_id: str | None,
    project_path: str | Path | None,
    *,
    dry_run: bool,
) -> AgentInstallResult:
    root = _target_dir(target_dir, "CLAUDE_CONFIG_DIR", ".claude")
    market_dir = root / "plugins" / "marketplaces" / "articultur"
    plugin_target = market_dir / "plugin"
    marketplace_path = market_dir / ".claude-plugin" / "marketplace.json"
    marker_path = market_dir / ".memplex-install-state.json"
    settings_path = root / "settings.json"
    known_marketplaces_path = root / "plugins" / "known_marketplaces.json"
    installed_plugins_path = root / "plugins" / "installed_plugins.json"
    cache_root = root / "plugins" / "cache" / "articultur" / "memplex"
    cache_target = cache_root / _package_version()
    cache_marker = cache_root / ".memplex-managed.json"
    identity_path = plugin_target / "memplex-agent.json"
    resolved_user_id = _resolved_user_id(user_id)
    resolved_project_path = _resolved_project_path(project_path)
    source_root = str(Path(__file__).resolve().parents[2])

    if market_dir.exists() and not _is_managed_claude_marketplace(market_dir):
        raise ValueError(
            "Claude Code marketplace path already exists and is not managed by "
            f"Memplex: {market_dir}"
        )
    if cache_root.exists() and not _is_managed_json_file(cache_marker):
        raise ValueError(
            "Claude Code plugin cache path already exists and is not managed by "
            f"Memplex: {cache_root}"
        )

    prior_state = _read_json(marker_path) if marker_path.exists() else {}
    prior_files = prior_state.get("files", {}) if isinstance(prior_state, dict) else {}
    if not isinstance(prior_files, dict):
        prior_files = {}

    current_texts: dict[str, str] = {}
    original_states: dict[str, dict[str, Any]] = {}
    external_paths = {
        "settings": settings_path,
        "knownMarketplaces": known_marketplaces_path,
        "installedPlugins": installed_plugins_path,
    }
    for key, path in external_paths.items():
        existed = path.exists()
        text = path.read_text(encoding="utf-8") if existed else "{}\n"
        current_texts[key] = text
        previous = prior_files.get(key, {})
        previous = previous if isinstance(previous, dict) else {}
        current_hash = hashlib.sha256(text.encode()).hexdigest()
        previous_matches = bool(
            previous.get("installedSha256") == current_hash
            and isinstance(previous.get("originalText"), str)
        )
        if previous_matches:
            original_text = str(previous["originalText"])
            original_existed = bool(previous.get("originalExisted"))
            original_mode = int(previous.get("originalMode", 0o600))
            restore_exact = bool(previous.get("restoreExact", True))
        elif previous:
            original_text = str(previous.get("originalText", text))
            original_existed = bool(previous.get("originalExisted", existed))
            original_mode = int(previous.get("originalMode", 0o600))
            restore_exact = False
        else:
            original_text = text
            original_existed = existed
            original_mode = path.stat().st_mode & 0o777 if existed else 0o600
            restore_exact = True
        original_states[key] = {
            "originalExisted": original_existed,
            "originalMode": original_mode,
            "originalText": original_text,
            "restoreExact": restore_exact,
        }

    market_source = str(market_dir)
    now = datetime.now().isoformat()
    installed_settings = set_jsonc_path(
        current_texts["settings"],
        ("extraKnownMarketplaces", "articultur", "source"),
        {"source": "directory", "path": market_source},
    )
    installed_settings = set_jsonc_path(
        installed_settings,
        ("enabledPlugins", "memplex@articultur"),
        True,
    )

    known_marketplaces = _read_json(known_marketplaces_path)
    known_marketplaces["articultur"] = {
        "source": {"source": "directory", "path": market_source},
        "installLocation": market_source,
        "lastUpdated": now,
    }
    installed_known = json.dumps(
        known_marketplaces, indent=2, ensure_ascii=False, sort_keys=True
    ) + "\n"

    installed_plugins = _read_json(installed_plugins_path)
    installed_plugins["version"] = 2
    plugin_records = installed_plugins.setdefault("plugins", {})
    if not isinstance(plugin_records, dict):
        raise ValueError("Claude Code installed_plugins.json plugins must be an object.")
    plugin_records["memplex@articultur"] = [
        {
            "scope": "user",
            "installPath": str(cache_target),
            "version": _package_version(),
            "installedAt": now,
            "lastUpdated": now,
        }
    ]
    installed_registry = json.dumps(
        installed_plugins, indent=2, ensure_ascii=False, sort_keys=True
    ) + "\n"
    installed_texts = {
        "settings": installed_settings,
        "knownMarketplaces": installed_known,
        "installedPlugins": installed_registry,
    }
    for key, installed_text in installed_texts.items():
        original_states[key]["installedSha256"] = hashlib.sha256(
            installed_text.encode()
        ).hexdigest()

    managed = {
        "by": "memplex",
        "installer": "memplex",
        "schema_version": 1,
    }
    marketplace = {
        "name": "articultur",
        "owner": {"name": "articultur"},
        "plugins": [
            {
                "name": "memplex",
                "source": "./plugin",
                "description": "Memplex persistent memory for Claude Code",
                "category": "productivity",
            }
        ],
    }

    if not dry_run:
        snapshots, snapshot_root = _snapshot_install_paths(
            [
                settings_path,
                known_marketplaces_path,
                installed_plugins_path,
                market_dir,
                cache_root,
            ]
        )
        try:
            source = _get_plugin_source_dir()
            if plugin_target.exists():
                shutil.rmtree(plugin_target)
            if cache_root.exists():
                shutil.rmtree(cache_root)
            shutil.copytree(source, plugin_target, symlinks=False, ignore=_ignore_patterns)
            shutil.copytree(source, cache_target, symlinks=False, ignore=_ignore_patterns)
            marketplace_path.parent.mkdir(parents=True, exist_ok=True)
            _write_json(marketplace_path, marketplace)
            _write_json(
                identity_path,
                _managed_identity_payload(
                    agent="claude-code",
                    user_id=resolved_user_id,
                    project_path=resolved_project_path,
                    source_root=source_root,
                    host_root=str(root.resolve()),
                    managed=managed,
                ),
            )
            identity_path.chmod(0o600)
            cache_identity = cache_target / "memplex-agent.json"
            _write_json(
                cache_identity,
                _managed_identity_payload(
                    agent="claude-code",
                    user_id=resolved_user_id,
                    project_path=resolved_project_path,
                    source_root=source_root,
                    host_root=str(root.resolve()),
                    managed=managed,
                ),
            )
            cache_identity.chmod(0o600)
            _write_json(cache_marker, {"managed": managed, "version": _package_version()})
            _write_json(
                marker_path,
                {
                    "version": _package_version(),
                    "installedAt": now,
                    "managed": managed,
                    "files": original_states,
                },
            )
            _write_text_atomic(settings_path, installed_settings)
            _write_text_atomic(known_marketplaces_path, installed_known)
            _write_text_atomic(installed_plugins_path, installed_registry)
        except Exception:
            restore_errors = _restore_install_snapshot(snapshots, snapshot_root)
            if restore_errors:
                msg = "; ".join(restore_errors)
                raise RuntimeError(
                    "Memplex Claude Code install failed and rollback encountered errors: " + msg
                )
            raise
        finally:
            _cleanup_snapshot_root(snapshot_root)

    return AgentInstallResult(
        agent="claude-code",
        action="install",
        status="planned" if dry_run else "installed",
        files=[
            str(settings_path),
            str(known_marketplaces_path),
            str(installed_plugins_path),
            str(marketplace_path),
            str(plugin_target),
            str(cache_target),
            str(identity_path),
        ],
        message="Installed and enabled the Memplex Claude Code plugin.",
        next_steps=["Restart Claude Code to activate hooks, MCP, and skills."],
    )


def _uninstall_claude_code(
    target_dir: str | Path | None,
    *,
    dry_run: bool,
) -> AgentInstallResult:
    root = _target_dir(target_dir, "CLAUDE_CONFIG_DIR", ".claude")
    market_dir = root / "plugins" / "marketplaces" / "articultur"
    marker_path = market_dir / ".memplex-install-state.json"
    cache_root = root / "plugins" / "cache" / "articultur" / "memplex"
    state = _read_json(marker_path) if marker_path.exists() else {}
    managed = market_dir.exists() and _is_managed_claude_marketplace(market_dir)
    if managed and not dry_run:
        files = state.get("files", {}) if isinstance(state, dict) else {}
        files = files if isinstance(files, dict) else {}
        external_paths = {
            "settings": root / "settings.json",
            "knownMarketplaces": root / "plugins" / "known_marketplaces.json",
            "installedPlugins": root / "plugins" / "installed_plugins.json",
        }
        for key, path in external_paths.items():
            file_state = files.get(key, {})
            file_state = file_state if isinstance(file_state, dict) else {}
            current_text = path.read_text(encoding="utf-8") if path.exists() else ""
            current_hash = hashlib.sha256(current_text.encode()).hexdigest()
            can_restore_exact = bool(
                file_state.get("restoreExact")
                and file_state.get("installedSha256") == current_hash
                and isinstance(file_state.get("originalText"), str)
            )
            if can_restore_exact:
                if file_state.get("originalExisted"):
                    _write_text_atomic(path, str(file_state["originalText"]))
                    path.chmod(int(file_state.get("originalMode", 0o600)))
                elif path.exists():
                    path.unlink()
                continue
            if not path.exists():
                continue
            if key == "settings":
                updated = remove_jsonc_path(
                    current_text, ("enabledPlugins", "memplex@articultur")
                )
                updated = remove_jsonc_path(
                    updated, ("extraKnownMarketplaces", "articultur")
                )
                _write_text_atomic(path, updated)
            else:
                payload = _read_json(path)
                if key == "knownMarketplaces":
                    entry = payload.get("articultur")
                    if isinstance(entry, dict) and entry.get("installLocation") == str(
                        market_dir
                    ):
                        payload.pop("articultur", None)
                else:
                    plugins = payload.get("plugins", {})
                    if isinstance(plugins, dict):
                        plugins.pop("memplex@articultur", None)
                _write_json(path, payload)
        if cache_root.exists() and _is_managed_json_file(
            cache_root / ".memplex-managed.json"
        ):
            shutil.rmtree(cache_root)
        shutil.rmtree(market_dir)
    return AgentInstallResult(
        agent="claude-code",
        action="uninstall",
        status="planned" if dry_run else "uninstalled",
        files=[
            str(root / "settings.json"),
            str(root / "plugins" / "known_marketplaces.json"),
            str(root / "plugins" / "installed_plugins.json"),
            str(market_dir),
            str(cache_root),
        ],
        message="Removed the managed Memplex Claude Code plugin and registry entries.",
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
    install_state_path = extension_dir / ".memplex-install-state.json"
    config_existed = config_path.exists()
    existing_text = config_path.read_text() if config_existed else "{}\n"
    config = _read_json(config_path)
    plugins = config.get("plugins", {})
    if not isinstance(plugins, dict):
        raise ValueError("OpenClaw plugins config must be an object.")
    slots = plugins.get("slots", {})
    entries = plugins.get("entries", {})
    if not isinstance(slots, dict) or not isinstance(entries, dict):
        raise ValueError("OpenClaw plugins.slots and plugins.entries must be objects.")
    previous_memory_slot = slots.get("memory")
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
    existing_managed = (
        existing_entry.get("config", {}).get("managed", {})
        if isinstance(existing_entry, dict)
        else {}
    )
    managed = {
        "installer": "memplex",
        "previousMemorySlot": existing_managed.get("previousMemorySlot"),
        "addedAllowEntry": existing_managed.get("addedAllowEntry", False),
        "addedBundledDiscovery": existing_managed.get("addedBundledDiscovery", False),
    }
    allow = plugins.get("allow", [])
    if not isinstance(allow, list):
        raise ValueError("OpenClaw plugins.allow must be an array when present.")
    existing_allow_entry = "memplex" in allow
    if previous_memory_slot and previous_memory_slot != "memplex":
        managed["previousMemorySlot"] = previous_memory_slot
    resolved_user_id = _resolved_user_id(user_id)
    resolved_project_path = _resolved_project_path(project_path)
    source_root = str(Path(__file__).resolve().parents[2])
    entry = {
        "enabled": True,
        "hooks": {"allowConversationAccess": True},
        "config": {
            "userId": resolved_user_id,
            "projectPath": resolved_project_path,
            "python": _python_command(),
            "sourceRoot": source_root,
            "autoRecall": True,
            "autoCapture": True,
            "topK": 5,
            "tokenBudget": 1500,
            "timeoutMs": 10000,
            "visibility": "workspace",
            "managed": managed,
        },
    }
    installed_text = set_jsonc_path(existing_text, ("plugins", "slots", "memory"), "memplex")
    installed_text = set_jsonc_path(
        installed_text,
        ("plugins", "entries", "memplex"),
        entry,
    )
    if not existing_allow_entry:
        installed_text = set_jsonc_path(
            installed_text,
            ("plugins", "allow"),
            [*allow, "memplex"],
        )
        managed["addedAllowEntry"] = True
        installed_text = set_jsonc_path(
            installed_text,
            ("plugins", "entries", "memplex"),
            entry,
        )
    bundled_discovery = plugins.get("bundledDiscovery")
    if bundled_discovery is None:
        installed_text = set_jsonc_path(
            installed_text,
            ("plugins", "bundledDiscovery"),
            "allowlist",
        )
        managed["addedBundledDiscovery"] = True
        installed_text = set_jsonc_path(
            installed_text,
            ("plugins", "entries", "memplex"),
            entry,
        )
    parsed_installed = json.loads(_strip_jsonc(installed_text))
    if parsed_installed["plugins"]["slots"]["memory"] != "memplex":
        raise ValueError("Failed to prepare the OpenClaw memory slot configuration.")

    prior_state = _read_json(install_state_path) if install_state_path.exists() else {}
    prior_hash_matches = bool(
        prior_state.get("installedSha256")
        and prior_state.get("installedSha256") == hashlib.sha256(existing_text.encode()).hexdigest()
    )
    restore_exact = bool(prior_state.get("restoreExact", True)) and (
        not prior_state or prior_hash_matches
    )
    original_text = (
        prior_state.get("originalText")
        if prior_state and isinstance(prior_state.get("originalText"), str)
        else existing_text
    )

    if not dry_run:
        snapshots, snapshot_root = _snapshot_install_paths([config_path, extension_dir])
        root.mkdir(parents=True, exist_ok=True)
        try:
            _write_openclaw_extension(
                extension_dir,
                user_id=resolved_user_id,
                project_path=resolved_project_path,
                source_root=source_root,
                host_root=str(root.resolve()),
                install_state={
                    "managed": {"installer": "memplex"},
                    "originalText": original_text,
                    "originalExisted": bool(prior_state.get("originalExisted", config_existed)),
                    "installedSha256": hashlib.sha256(installed_text.encode()).hexdigest(),
                    "restoreExact": restore_exact,
                },
            )
            _write_text_atomic(config_path, installed_text)
        except Exception:
            restore_errors = _restore_install_snapshot(snapshots, snapshot_root)
            if restore_errors:
                msg = "; ".join(restore_errors)
                raise RuntimeError(
                    "Memplex OpenClaw install failed and rollback encountered errors: " + msg
                )
            raise
        finally:
            _cleanup_snapshot_root(snapshot_root)

    return AgentInstallResult(
        agent="openclaw",
        action="install",
        status="planned" if dry_run else "installed",
        files=[
            str(config_path),
            str(extension_dir / "openclaw.plugin.json"),
            str(extension_dir / "plugin.json"),
            str(extension_dir / "index.js"),
        ],
        message="Configured OpenClaw memory slot and installed the native Memplex plugin.",
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
    install_state_path = extension_dir / ".memplex-install-state.json"
    if config_path.exists():
        current_text = config_path.read_text()
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
        added_bundled_discovery = (
            memplex_entry.get("config", {})
            .get("managed", {})
            .get("addedBundledDiscovery", False)
        )
        state = _read_json(install_state_path) if install_state_path.exists() else {}
        exact_hash = hashlib.sha256(current_text.encode()).hexdigest()
        can_restore_exact = bool(
            is_managed_entry
            and state.get("restoreExact")
            and state.get("installedSha256") == exact_hash
            and isinstance(state.get("originalText"), str)
        )
        updated_text = current_text
        if can_restore_exact:
            updated_text = state["originalText"]
            config_changed = updated_text != current_text
        elif is_managed_entry:
            if slots.get("memory") == "memplex":
                if previous_memory_slot:
                    updated_text = set_jsonc_path(
                        updated_text,
                        ("plugins", "slots", "memory"),
                        previous_memory_slot,
                    )
                else:
                    updated_text = remove_jsonc_path(updated_text, ("plugins", "slots", "memory"))
            updated_text = remove_jsonc_path(updated_text, ("plugins", "entries", "memplex"))
            if added_allow_entry and isinstance(plugins.get("allow"), list):
                preserved_allow = [item for item in plugins["allow"] if item != "memplex"]
                updated_text = set_jsonc_path(updated_text, ("plugins", "allow"), preserved_allow)
            if added_bundled_discovery and plugins.get("bundledDiscovery") == "allowlist":
                updated_text = remove_jsonc_path(
                    updated_text,
                    ("plugins", "bundledDiscovery"),
                )
            config_changed = updated_text != current_text
        if config_changed and not dry_run:
            _write_text_atomic(config_path, updated_text)
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
    config_path = root / "config.yaml"
    provider_path = root / "memplex.json"
    legacy_provider_path = root / "memory-providers" / "memplex.json"
    plugin_dir = root / "plugins" / "memplex"
    legacy_plugin_dir = root / "plugins" / "memory" / "memplex"
    install_state_path = plugin_dir / ".memplex-install-state.json"
    if provider_path.exists() and not _is_managed_json_file(provider_path):
        raise ValueError(
            "Hermes memplex.json already exists and is not managed "
            "by Memplex. Remove or rename it before running memplex agent install."
        )
    if plugin_dir.exists() and not _is_managed_hermes_plugin(plugin_dir):
        raise ValueError(
            "Hermes plugins/memplex already exists and is not managed by "
            "Memplex. Remove or rename it before running memplex agent install."
        )
    if legacy_plugin_dir.exists() and not _is_managed_hermes_plugin(legacy_plugin_dir):
        raise ValueError(
            "Legacy Hermes plugins/memory/memplex exists and is not managed by "
            "Memplex. Remove or rename it before running memplex agent install."
        )
    config_existed = config_path.exists()
    existing_text = config_path.read_text(encoding="utf-8") if config_existed else ""
    provider_present, previous_provider = yaml_path_value(
        existing_text,
        ("memory", "provider"),
    )
    if (
        provider_present
        and previous_provider is not None
        and not isinstance(previous_provider, str)
    ):
        raise ValueError("Hermes memory.provider must be a string when configured.")
    installed_text = set_yaml_scalar_path(
        existing_text,
        ("memory", "provider"),
        "memplex",
    )
    selected, selected_provider = yaml_path_value(
        installed_text,
        ("memory", "provider"),
    )
    if not selected or selected_provider != "memplex":
        raise ValueError("Failed to prepare Hermes memory.provider configuration.")

    prior_state = _read_json(install_state_path) if install_state_path.exists() else {}
    existing_hash = hashlib.sha256(existing_text.encode()).hexdigest()
    prior_hash_matches = bool(
        prior_state.get("installedSha256") and prior_state.get("installedSha256") == existing_hash
    )
    if prior_state and prior_hash_matches:
        original_text = prior_state.get("originalText", existing_text)
        original_existed = bool(prior_state.get("originalExisted", config_existed))
        original_mode = int(prior_state.get("originalMode", 0o600))
        previous_provider = prior_state.get("previousProvider")
        provider_present = bool(prior_state.get("previousProviderPresent", False))
        restore_exact = bool(prior_state.get("restoreExact", True))
    else:
        original_text = existing_text
        original_existed = config_existed
        original_mode = config_path.stat().st_mode & 0o777 if config_existed else 0o600
        restore_exact = not bool(prior_state)
        if prior_state and previous_provider == "memplex":
            previous_provider = prior_state.get("previousProvider")
            provider_present = bool(prior_state.get("previousProviderPresent", False))

    resolved_user_id = _resolved_user_id(user_id)
    resolved_project_path = _resolved_project_path(project_path)
    source_root = str(Path(__file__).resolve().parents[2])
    managed = {
        "by": "memplex",
        "installer": "memplex",
        "schema_version": 1,
    }
    provider_config = {
        "name": "memplex",
        "provider": "memplex",
        "command": _mcp_command(),
        "agent": "hermes",
        "user_id": resolved_user_id,
        "project_path": resolved_project_path,
        "python": _python_command(),
        "source_root": source_root,
        "host_root": str(root.resolve()),
        "prefetch": True,
        "top_k": 5,
        "token_budget": 1500,
        "visibility": "workspace",
        "tools": ["memplex_search", "memplex_conclude"],
        "managed": managed,
    }
    if not dry_run:
        snapshots, snapshot_root = _snapshot_install_paths(
            [
                config_path,
                provider_path,
                legacy_provider_path,
                plugin_dir,
                legacy_plugin_dir,
            ]
        )
        root.mkdir(parents=True, exist_ok=True)
        try:
            _write_json(provider_path, provider_config)
            provider_path.chmod(0o600)
            _write_hermes_provider_plugin(
                plugin_dir,
                provider_config,
                install_state={
                    "managed": managed,
                    "originalText": original_text,
                    "originalExisted": original_existed,
                    "originalMode": original_mode,
                    "installedSha256": hashlib.sha256(installed_text.encode()).hexdigest(),
                    "restoreExact": restore_exact,
                    "previousProvider": previous_provider,
                    "previousProviderPresent": provider_present,
                },
            )
            _write_text_atomic(config_path, installed_text)
            if legacy_provider_path.exists() and _is_managed_json_file(legacy_provider_path):
                legacy_provider_path.unlink()
            if legacy_plugin_dir.exists() and _is_managed_hermes_plugin(legacy_plugin_dir):
                shutil.rmtree(legacy_plugin_dir)
        except Exception:
            restore_errors = _restore_install_snapshot(snapshots, snapshot_root)
            if restore_errors:
                msg = "; ".join(restore_errors)
                raise RuntimeError(
                    "Memplex Hermes install failed and rollback encountered errors: " + msg
                )
            raise
        finally:
            _cleanup_snapshot_root(snapshot_root)

    return AgentInstallResult(
        agent="hermes",
        action="install",
        status="planned" if dry_run else "installed",
        files=[
            str(config_path),
            str(provider_path),
            str(plugin_dir / "plugin.yaml"),
            str(plugin_dir / "__init__.py"),
            str(plugin_dir / "memplex-agent.json"),
        ],
        message="Installed and selected the native Memplex Hermes memory provider.",
        next_steps=["Restart Hermes and run `hermes memory status`."],
    )


def _uninstall_hermes(
    target_dir: str | Path | None,
    *,
    dry_run: bool,
) -> AgentInstallResult:
    root = _target_dir(target_dir, "HERMES_CONFIG_DIR", ".hermes")
    config_path = root / "config.yaml"
    provider_path = root / "memplex.json"
    legacy_provider_path = root / "memory-providers" / "memplex.json"
    plugin_dir = root / "plugins" / "memplex"
    legacy_plugin_dir = root / "plugins" / "memory" / "memplex"
    install_state_path = plugin_dir / ".memplex-install-state.json"
    if config_path.exists():
        current_text = config_path.read_text(encoding="utf-8")
        state = _read_json(install_state_path) if install_state_path.exists() else {}
        current_hash = hashlib.sha256(current_text.encode()).hexdigest()
        can_restore_exact = bool(
            _is_managed_hermes_plugin(plugin_dir)
            and state.get("restoreExact")
            and state.get("installedSha256") == current_hash
            and isinstance(state.get("originalText"), str)
        )
        if can_restore_exact:
            if not dry_run:
                if state.get("originalExisted"):
                    _write_text_atomic(config_path, state["originalText"])
                    config_path.chmod(int(state.get("originalMode", 0o600)))
                else:
                    config_path.unlink()
        else:
            provider_present, current_provider = yaml_path_value(
                current_text,
                ("memory", "provider"),
            )
            if provider_present and current_provider == "memplex":
                if state.get("previousProviderPresent"):
                    updated_text = set_yaml_scalar_path(
                        current_text,
                        ("memory", "provider"),
                        state.get("previousProvider"),
                    )
                else:
                    updated_text = remove_yaml_path(
                        current_text,
                        ("memory", "provider"),
                    )
                if updated_text != current_text and not dry_run:
                    _write_text_atomic(config_path, updated_text)
    if provider_path.exists() and _is_managed_json_file(provider_path) and not dry_run:
        provider_path.unlink()
    if (
        legacy_provider_path.exists()
        and _is_managed_json_file(legacy_provider_path)
        and not dry_run
    ):
        legacy_provider_path.unlink()
    if plugin_dir.exists() and _is_managed_hermes_plugin(plugin_dir) and not dry_run:
        shutil.rmtree(plugin_dir)
    if (
        legacy_plugin_dir.exists()
        and _is_managed_hermes_plugin(legacy_plugin_dir)
        and not dry_run
    ):
        shutil.rmtree(legacy_plugin_dir)
    return AgentInstallResult(
        agent="hermes",
        action="uninstall",
        status="planned" if dry_run else "uninstalled",
        files=[str(config_path), str(provider_path), str(plugin_dir)],
        message="Removed the managed Memplex Hermes provider and restored its selector.",
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


def _resolved_user_id(user_id: str | None) -> str:
    resolved = (user_id or os.environ.get("MEMPLEX_USER_ID") or getpass.getuser()).strip()
    if not resolved or resolved == "default":
        raise ValueError("A non-default user_id is required for agent integrations.")
    return resolved


def _resolved_project_path(project_path: str | Path | None) -> str:
    candidate = project_path or os.environ.get("MEMPLEX_PROJECT_ROOT") or Path.cwd()
    return str(Path(candidate).expanduser().resolve(strict=False))


def _managed_identity_payload(
    *,
    agent: str,
    user_id: str,
    project_path: str,
    source_root: str,
    host_root: str,
    managed: dict[str, Any],
) -> dict[str, Any]:
    return validate_managed_identity(
        {
            "agent": agent,
            "user_id": user_id,
            "project_path": project_path,
            "python": _python_command(),
            "source_root": source_root,
            "host_root": host_root,
            "managed": managed,
        },
        expected_agent=agent,
        expected_host_root=host_root,
    )


def _python_command() -> str:
    """Return the absolute interpreter recorded in managed host integrations.

    A launcher must not later resolve a different interpreter from PATH: its
    dependencies and Memplex version belong to the persistent environment
    chosen at installation time.
    """

    configured = os.environ.get("MEMPLEX_PYTHON") or sys.executable
    if not configured:
        raise RuntimeError("Memplex installer could not determine a Python interpreter")
    interpreter = Path(configured).expanduser()
    if not interpreter.is_absolute():
        raise ValueError(
            "MEMPLEX_PYTHON must be an absolute path so managed launchers do not use PATH"
        )
    if not interpreter.is_file() or not os.access(interpreter, os.X_OK):
        raise ValueError(
            "MEMPLEX_PYTHON must identify an existing executable file for managed launchers"
        )
    return str(interpreter)


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
    reserved_tables = {
        "[mcp_servers.memplex]",
        "[marketplaces.memplex]",
        '[plugins."memplex@memplex"]',
    }
    return any(line.strip() in reserved_tables for line in unmanaged.splitlines())


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


def _write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    temporary = path.with_name(f".{path.name}.memplex.tmp")
    temporary.write_text(text)
    temporary.chmod(mode)
    temporary.replace(path)


def _write_openclaw_extension(
    extension_dir: Path,
    *,
    user_id: str,
    project_path: str,
    source_root: str,
    host_root: str,
    install_state: dict[str, Any],
) -> None:
    if extension_dir.exists():
        shutil.rmtree(extension_dir)
    extension_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "id": "memplex",
        "name": "Memplex",
        "version": _package_version(),
        "description": "Memplex long-term memory for OpenClaw agents",
        "kind": "memory",
        "activation": {"onStartup": True},
        "contracts": {
            "tools": ["memory_recall", "memory_store"],
        },
        "configSchema": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "userId": {"type": "string", "minLength": 1},
                "projectPath": {"type": "string", "minLength": 1},
                "python": {"type": "string", "minLength": 1},
                "sourceRoot": {"type": "string", "minLength": 1},
                "autoRecall": {"type": "boolean", "default": True},
                "autoCapture": {"type": "boolean", "default": True},
                "topK": {"type": "integer", "minimum": 1, "maximum": 50},
                "tokenBudget": {
                    "type": "integer",
                    "minimum": 64,
                    "maximum": 32000,
                },
                "timeoutMs": {
                    "type": "integer",
                    "minimum": 500,
                    "maximum": 60000,
                },
                "visibility": {
                    "type": "string",
                    "enum": ["session", "workspace", "user"],
                    "default": "workspace",
                },
                "managed": {
                    "type": "object",
                    "additionalProperties": True,
                },
            },
        },
    }
    _write_json(extension_dir / "openclaw.plugin.json", manifest)
    _write_json(extension_dir / "plugin.json", manifest)
    _write_json(
        extension_dir / "package.json",
        {
            "name": "@memplex/openclaw-plugin",
            "version": _package_version(),
            "description": "Native OpenClaw lifecycle bridge for Memplex",
            "type": "module",
            "private": True,
            "main": "./index.js",
            "peerDependencies": {"openclaw": ">=2026.5.17"},
            "openclaw": {"extensions": ["./index.js"]},
        },
    )
    managed = {
        "by": "memplex",
        "installer": "memplex",
        "schema_version": 1,
    }
    _write_json(
        extension_dir / "memplex-agent.json",
        _managed_identity_payload(
            agent="openclaw",
            user_id=user_id,
            project_path=project_path,
            source_root=source_root,
            host_root=host_root,
            managed=managed,
        ),
    )
    install_state_path = extension_dir / ".memplex-install-state.json"
    _write_json(install_state_path, install_state)
    install_state_path.chmod(0o600)
    (extension_dir / "index.js").write_text(_openclaw_plugin_javascript())


def _openclaw_plugin_javascript() -> str:
    return r"""import { accessSync, constants, readFileSync, realpathSync, statSync } from "node:fs";
import { spawn } from "node:child_process";
import { delimiter, isAbsolute, resolve } from "node:path";
import { fileURLToPath } from "node:url";

function identityError(detail) {
  return new Error(`Memplex managed identity invalid; reinstall required: ${detail}`);
}

function parseJsonWithoutDuplicateKeys(text) {
  let index = 0;
  const skipWhitespace = () => {
    while (/\s/u.test(text[index] || "")) index += 1;
  };
  const parseString = () => {
    const start = index;
    if (text[index] !== '"') throw new SyntaxError("expected string");
    index += 1;
    while (index < text.length) {
      if (text[index] === "\\") {
        index += 2;
        continue;
      }
      if (text[index] === '"') {
        index += 1;
        return JSON.parse(text.slice(start, index));
      }
      index += 1;
    }
    throw new SyntaxError("unterminated string");
  };
  const parseValue = () => {
    skipWhitespace();
    if (text[index] === "{") return parseObject();
    if (text[index] === "[") {
      const values = [];
      index += 1;
      skipWhitespace();
      if (text[index] === "]") { index += 1; return values; }
      while (true) {
        values.push(parseValue());
        skipWhitespace();
        if (text[index] === "]") { index += 1; return values; }
        if (text[index] !== ",") throw new SyntaxError("expected array separator");
        index += 1;
      }
    }
    if (text[index] === '"') return parseString();
    const start = index;
    while (index < text.length && !/[\s,}\]]/u.test(text[index])) index += 1;
    if (start === index) throw new SyntaxError("expected value");
    return JSON.parse(text.slice(start, index));
  };
  const parseObject = () => {
    const value = {};
    const keys = new Set();
    index += 1;
    skipWhitespace();
    if (text[index] === "}") { index += 1; return value; }
    while (true) {
      skipWhitespace();
      const key = parseString();
      if (keys.has(key)) throw identityError(`duplicate key ${JSON.stringify(key)}`);
      keys.add(key);
      skipWhitespace();
      if (text[index] !== ":") throw new SyntaxError("expected object colon");
      index += 1;
      value[key] = parseValue();
      skipWhitespace();
      if (text[index] === "}") { index += 1; return value; }
      if (text[index] !== ",") throw new SyntaxError("expected object separator");
      index += 1;
    }
  };
  const value = parseValue();
  skipWhitespace();
  if (index !== text.length) throw new SyntaxError("trailing JSON content");
  return value;
}

function exactKeys(value, expected) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const actual = Object.keys(value).sort();
  return actual.length === expected.length && actual.every((key, i) => key === expected[i]);
}

function validateIdentity(value, expectedHostRoot) {
  const keys = ["agent", "host_root", "managed", "project_path", "python", "source_root", "user_id"];
  if (!exactKeys(value, keys)) throw identityError("identity must contain exact keys");
  for (const field of ["agent", "user_id", "project_path", "python", "source_root", "host_root"]) {
    if (typeof value[field] !== "string" || !value[field] || value[field] !== value[field].trim() ||
        /[\u0000\r\n]/u.test(value[field])) {
      throw identityError(`${field} must be canonical non-empty text`);
    }
  }
  if (value.agent !== "openclaw") throw identityError("agent must be openclaw");
  if (!exactKeys(value.managed, ["by", "installer", "schema_version"])) {
    throw identityError("managed must contain exact ownership keys");
  }
  if (value.managed.by !== "memplex" || value.managed.installer !== "memplex" ||
      value.managed.schema_version !== 1) {
    throw identityError("managed ownership is invalid");
  }
  for (const field of ["project_path", "python", "source_root", "host_root"]) {
    if (!isAbsolute(value[field])) throw identityError(`${field} must be absolute`);
  }
  try {
    if (!statSync(value.python).isFile()) throw new Error("not a file");
    accessSync(value.python, constants.X_OK);
  } catch {
    throw identityError("recorded Python interpreter is unavailable or not executable");
  }
  for (const field of ["source_root", "host_root"]) {
    try {
      if (!statSync(value[field]).isDirectory()) throw new Error("not a directory");
    } catch {
      throw identityError(`${field} directory is unavailable`);
    }
  }
  let canonicalHostRoot;
  let canonicalExpectedRoot;
  try {
    canonicalHostRoot = realpathSync(value.host_root);
    canonicalExpectedRoot = realpathSync(expectedHostRoot);
  } catch {
    throw identityError("host_root binding cannot be resolved");
  }
  if (value.host_root !== canonicalHostRoot) {
    throw identityError("host_root must be a canonical path");
  }
  const hostStat = statSync(canonicalHostRoot);
  const expectedStat = statSync(canonicalExpectedRoot);
  if (canonicalHostRoot !== canonicalExpectedRoot || hostStat.dev !== expectedStat.dev ||
      hostStat.ino !== expectedStat.ino) {
    throw identityError("host_root does not match the actual installation root");
  }
  return value;
}

function loadIdentity() {
  try {
    const raw = readFileSync(new URL("./memplex-agent.json", import.meta.url), "utf8");
    const expectedHostRoot = realpathSync(resolve(pluginRoot, "../.."));
    return validateIdentity(parseJsonWithoutDuplicateKeys(raw), expectedHostRoot);
  } catch (error) {
    if (String(error).includes("reinstall required")) throw error;
    throw identityError("identity file is missing, unreadable, or invalid JSON");
  }
}

const pluginRoot = realpathSync(fileURLToPath(new URL(".", import.meta.url)));
const identity = loadIdentity();

function effectiveConfig(pluginConfig) {
  return {
    autoRecall: true,
    autoCapture: true,
    topK: 5,
    tokenBudget: 1500,
    timeoutMs: 10000,
    visibility: "workspace",
    ...(pluginConfig || {}),
    // Identity fields come from the installer-managed file.  Keep this
    // assignment after host configuration so an OpenClaw config can tune
    // behavior but cannot move memories into another principal/workspace.
    userId: identity.user_id,
    projectPath: identity.project_path,
    python: identity.python,
    sourceRoot: identity.source_root,
    hostRoot: identity.host_root,
    pluginRoot,
  };
}

function bridgeEnv(config) {
  const pythonPath = [config.sourceRoot, process.env.PYTHONPATH].filter(Boolean).join(delimiter);
  return {
    ...process.env,
    PYTHONPATH: pythonPath,
    MEMPLEX_USER_ID: config.userId,
    MEMPLEX_PROJECT_ROOT: config.projectPath,
    OPENCLAW_CONFIG_DIR: config.hostRoot,
    MEMPLEX_PLUGIN_ROOT: config.pluginRoot,
  };
}

function callBridge(action, event, context, config) {
  return new Promise((resolve, reject) => {
    const child = spawn(config.python, ["-m", "memplex.adapters.openclaw_plugin", action], {
      env: bridgeEnv(config),
      stdio: ["pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    const timeout = setTimeout(() => {
      child.kill("SIGTERM");
      reject(new Error(`Memplex ${action} timed out after ${config.timeoutMs}ms`));
    }, config.timeoutMs || 10000);
    child.stdout.setEncoding("utf8");
    child.stderr.setEncoding("utf8");
    child.stdout.on("data", (chunk) => { stdout += chunk; });
    child.stderr.on("data", (chunk) => { stderr += chunk; });
    child.on("error", (error) => {
      clearTimeout(timeout);
      reject(error);
    });
    child.on("close", (code) => {
      clearTimeout(timeout);
      if (code !== 0) {
        reject(new Error(stderr.trim() || `Memplex ${action} exited with code ${code}`));
        return;
      }
      try {
        resolve(JSON.parse(stdout || "{}"));
      } catch (error) {
        reject(new Error(`Memplex ${action} returned invalid JSON: ${String(error)}`));
      }
    });
    child.stdin.end(JSON.stringify({ config, event: event || {}, context: context || {} }));
  });
}

function textResult(result) {
  return {
    content: [{ type: "text", text: JSON.stringify(result) }],
    details: result,
  };
}

export default {
  id: "memplex",
  name: "Memplex",
  description: "Shared long-term memory for OpenClaw",
  kind: "memory",
  register(api) {
    const config = effectiveConfig(api.pluginConfig);
    const log = api.logger || console;

    api.on("before_prompt_build", async (event, context) => {
      if (config.autoRecall === false || !event?.prompt?.trim()) return;
      try {
        const result = await callBridge("recall", event, context, config);
        if (result.prependContext) return { prependContext: result.prependContext };
      } catch (error) {
        log.warn?.(`memplex: recall skipped: ${String(error)}`);
      }
    });

    api.on("agent_end", async (event, context) => {
      if (config.autoCapture === false || event?.success === false) return;
      try {
        await callBridge("capture", event, context, config);
      } catch (error) {
        log.warn?.(`memplex: capture skipped: ${String(error)}`);
      }
    });

    api.on("session_end", async (event, context) => {
      try {
        await callBridge("session-end", event, context, config);
      } catch (error) {
        log.debug?.(`memplex: session cleanup skipped: ${String(error)}`);
      }
    });

    api.registerTool((toolContext) => ({
      name: "memory_recall",
      label: "Memplex Recall",
      description: "Recall shared Memplex memories for the active workspace.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: { query: { type: "string", minLength: 1 } },
        required: ["query"],
      },
      async execute(_toolCallId, params) {
        const result = await callBridge(
          "search",
          { query: params.query },
          toolContext || {},
          config,
        );
        return textResult(result);
      },
    }), { name: "memory_recall" });

    api.registerTool((toolContext) => ({
      name: "memory_store",
      label: "Memplex Store",
      description: "Store a durable memory in the active Memplex workspace.",
      parameters: {
        type: "object",
        additionalProperties: false,
        properties: { content: { type: "string", minLength: 1 } },
        required: ["content"],
      },
      async execute(_toolCallId, params) {
        const result = await callBridge(
          "store",
          { content: params.content },
          toolContext || {},
          config,
        );
        return textResult(result);
      },
    }), { name: "memory_store" });
  },
};
"""


def _write_hermes_provider_plugin(
    plugin_dir: Path,
    provider_config: dict[str, Any],
    *,
    install_state: dict[str, Any],
) -> None:
    if plugin_dir.exists():
        shutil.rmtree(plugin_dir)
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
        ),
        encoding="utf-8",
    )
    managed = provider_config.get("managed", {"installer": "memplex"})
    _write_json(
        plugin_dir / "memplex-agent.json",
        _managed_identity_payload(
            agent="hermes",
            user_id=provider_config["user_id"],
            project_path=provider_config["project_path"],
            source_root=provider_config["source_root"],
            host_root=provider_config["host_root"],
            managed=managed,
        ),
    )
    (plugin_dir / "memplex-agent.json").chmod(0o600)
    _write_json(plugin_dir / ".memplex-install-state.json", install_state)
    (plugin_dir / ".memplex-install-state.json").chmod(0o600)
    (plugin_dir / "README.md").write_text(
        "# Memplex Memory Provider\n\n"
        "Hermes Agent 原生 MemoryProvider：共享 Codex、Claude Code 与 OpenClaw "
        "的本地 Memplex 记忆，并在压缩、会话结束和退出前冲刷已接收写入。\n",
        encoding="utf-8",
    )
    (plugin_dir / "__init__.py").write_text(
        '''"""Hermes MemoryProvider bootstrap for Memplex."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).resolve().parent
_EXPECTED_HOST_ROOT = _PLUGIN_DIR.parents[1].resolve(strict=True)


def _identity_error(detail: str) -> ValueError:
    return ValueError(f"Memplex managed identity invalid; reinstall required: {detail}")


def _object_without_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise _identity_error(f"duplicate key {key!r}")
        value[key] = item
    return value


def _load_identity():
    try:
        raw = (_PLUGIN_DIR / "memplex-agent.json").read_text(encoding="utf-8")
        value = json.loads(raw, object_pairs_hook=_object_without_duplicates)
    except ValueError as exc:
        if "reinstall required" in str(exc):
            raise
        raise _identity_error("identity is not valid JSON") from exc
    except (OSError, UnicodeError) as exc:
        raise _identity_error("identity file is missing or unreadable") from exc
    expected = {
        "agent", "user_id", "project_path", "python", "source_root", "host_root", "managed"
    }
    if type(value) is not dict or set(value) != expected:
        raise _identity_error("identity must contain exact keys")
    for field in ("agent", "user_id", "project_path", "python", "source_root", "host_root"):
        item = value[field]
        if (
            type(item) is not str
            or not item
            or item != item.strip()
            or any(control in item for control in ("\\x00", "\\n", "\\r"))
        ):
            raise _identity_error(f"{field} must be canonical non-empty text")
    if value["agent"] != "hermes":
        raise _identity_error("agent must be hermes")
    managed = value["managed"]
    if type(managed) is not dict or set(managed) != {"by", "installer", "schema_version"}:
        raise _identity_error("managed must contain exact ownership keys")
    if managed["by"] != "memplex" or managed["installer"] != "memplex":
        raise _identity_error("managed ownership is invalid")
    if type(managed["schema_version"]) is not int or managed["schema_version"] != 1:
        raise _identity_error("managed schema_version must be integer 1")
    for field in ("project_path", "python", "source_root", "host_root"):
        if not Path(value[field]).is_absolute():
            raise _identity_error(f"{field} must be absolute")
    python = Path(value["python"])
    if not python.is_file() or not os.access(python, os.X_OK):
        raise _identity_error("recorded Python interpreter is unavailable or not executable")
    for field in ("source_root", "host_root"):
        if not Path(value[field]).is_dir():
            raise _identity_error(f"{field} directory is unavailable")
    recorded_host_root = Path(value["host_root"])
    try:
        canonical_host_root = recorded_host_root.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise _identity_error("host_root binding cannot be resolved") from exc
    if str(recorded_host_root) != str(canonical_host_root):
        raise _identity_error("host_root must be a canonical path")
    try:
        same_host = os.path.samefile(canonical_host_root, _EXPECTED_HOST_ROOT)
    except OSError as exc:
        raise _identity_error("host_root binding cannot be compared") from exc
    if not same_host:
        raise _identity_error("host_root does not match the actual installation root")
    return value


_IDENTITY = _load_identity()
_SOURCE_ROOT = _IDENTITY["source_root"]
if _SOURCE_ROOT not in sys.path:
    sys.path.insert(0, _SOURCE_ROOT)

from memplex.adapters.hermes_memory_provider import MemplexMemoryProvider
from memplex.adapters.hermes_memory_provider import register as _register


def register(ctx) -> None:
    _register(ctx, identity=_IDENTITY)
''',
        encoding="utf-8",
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
    identity = extension_dir / "memplex-agent.json"
    if identity.exists() and _is_managed_json_file(identity):
        return True
    for manifest_name in ("openclaw.plugin.json", "plugin.json"):
        manifest = extension_dir / manifest_name
        if manifest.exists() and _is_managed_json_file(manifest):
            return True
    return False


def _is_managed_hermes_plugin(plugin_dir: Path) -> bool:
    return _is_managed_json_file(plugin_dir / "memplex-agent.json") or _is_managed_json_file(
        plugin_dir / "memplex.json"
    )


def _is_managed_claude_marketplace(market_dir: Path) -> bool:
    state_path = market_dir / ".memplex-install-state.json"
    if state_path.exists():
        try:
            return _is_managed_payload(_read_json(state_path))
        except Exception:
            return False
    legacy_marker = market_dir / ".install-version"
    if not legacy_marker.exists():
        return False
    try:
        marker = _read_json(legacy_marker)
    except Exception:
        return False
    # Older Memplex installers wrote a version-only marker.
    return bool(marker.get("version"))


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
            logger.debug("pyproject version read failed", exc_info=True)

    from importlib.metadata import version as pkg_version

    try:
        return pkg_version("memplex")
    except Exception:
        return "unknown"


# ── Agent installer registry ─────────────────────────────────────────
# Populated at module load (after every per-agent installer/uninstaller
# function is defined above). Replaces the previous 4-way if/elif chain
# in ``_install_one`` / ``_uninstall_one``; adding a new agent host is
# now one entry here instead of editing two dispatch functions.
_INSTALLERS: dict[str, AgentInstallerSpec] = {
    "codex": AgentInstallerSpec(
        install=_install_codex,
        uninstall=_uninstall_codex,
        needs_identity=True,
    ),
    "claude-code": AgentInstallerSpec(
        install=_install_claude_code,
        uninstall=_uninstall_claude_code,
        needs_identity=True,
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
