"""Install-path enumeration and snapshot/rollback transaction machinery.

Extracted from ``agent_installer.py``: the functions that enumerate per-host
installation paths and snapshot / restore / roll back every mutated path so a
failed multi-host install restores the exact pre-install state. They are
re-exported from ``memplex.adapters.agent_installer`` for import-path
stability and participate in the G008 host-contract digests via
``host_lifecycle._contract_files``.

``_target_dir`` / ``_package_version`` live in ``agent_installer`` (which
imports this module at its end); they are imported lazily inside the
functions so module loading stays one-directional.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

def _agent_installation_paths(
    agent: str,
    target_dir: str | Path | None,
) -> tuple[Path, dict[str, Path]]:
    from memplex.adapters.agent_installer import (  # lazy: avoid circular import
        _package_version,
        _target_dir,
    )
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
