"""Isolated install, reinstall, uninstall, and packaged-asset verification."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BUILD_SCRIPT = PROJECT_ROOT / "scripts" / "build_release_artifacts.py"
PYTHON_VERSIONS = tuple(
    item.strip()
    for item in os.environ.get("MEMPLEX_INSTALL_MATRIX_PYTHON", "3.11,3.12,3.13").split(",")
    if item.strip()
)
PACKAGED_ASSETS = (
    "_plugin/.claude-plugin/plugin.json",
    "_plugin/.codex-plugin/plugin.json",
    "_plugin/.mcp.json",
    "_plugin/.codex.mcp.json",
    "_plugin/hooks/hooks.json",
    "_plugin/hooks/hooks-codex.json",
    "_plugin/scripts/hook-runner.py",
    "_plugin/scripts/claude-hook.sh",
    "_plugin/scripts/codex-plugin.sh",
    "_plugin/scripts/mcp-server.sh",
)
UV_PYTHON_INSTALL_DIR = subprocess.run(
    ["uv", "python", "dir"],
    capture_output=True,
    text=True,
    check=True,
).stdout.strip()


def _run(
    command: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int = 120,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    assert result.returncode == 0, (command, result.stdout, result.stderr)
    return result


@pytest.fixture(scope="module")
def release_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("install-matrix-bundle") / "dist"
    _run(
        [
            sys.executable,
            str(BUILD_SCRIPT),
            "--source",
            str(PROJECT_ROOT),
            "--output",
            str(output),
            "--tag",
            "v3.3.2",
            "--source-date-epoch",
            "1704067200",
            "--allow-dirty",
        ],
        cwd=PROJECT_ROOT,
        env=os.environ.copy(),
    )
    return output


def _isolated_env(root: Path) -> dict[str, str]:
    home = root / "home"
    home.mkdir(parents=True, exist_ok=True)
    return {
        **os.environ,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "CODEX_HOME": str(home / ".codex"),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "OPENCLAW_CONFIG_DIR": str(home / ".openclaw"),
        "HERMES_CONFIG_DIR": str(home / ".hermes"),
        "UV_CACHE_DIR": str(root / "uv-cache"),
        "UV_PYTHON_INSTALL_DIR": UV_PYTHON_INSTALL_DIR,
        "npm_config_cache": str(root / "npm-cache"),
        "npm_config_update_notifier": "false",
    }


def _python_path(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


@pytest.mark.parametrize("python_version", PYTHON_VERSIONS)
def test_wheel_fresh_install_reinstall_assets_and_uninstall_are_isolated(
    tmp_path: Path,
    release_bundle: Path,
    python_version: str,
) -> None:
    env = _isolated_env(tmp_path)
    python = _run(
        ["uv", "python", "find", python_version], cwd=tmp_path, env=env
    ).stdout.strip()
    venv = tmp_path / "venv"
    _run(["uv", "venv", "--python", python, str(venv)], cwd=tmp_path, env=env)
    venv_python = _python_path(venv)
    wheel = release_bundle / "memplex-3.3.2-py3-none-any.whl"

    for _ in range(2):
        _run(
            ["uv", "pip", "install", "--python", str(venv_python), str(wheel)],
            cwd=tmp_path,
            env=env,
        )

    smoke = _run(
        [
            str(venv_python),
            "-c",
            (
                "import importlib.metadata as m, json; "
                "from importlib.resources import files; "
                "from memplex.storage.migrations import discover_migrations; "
                "root=files('memplex'); "
                f"assets={json.dumps(PACKAGED_ASSETS)!r}; "
                "print(json.dumps({'version':m.version('memplex'),"
                "'migrations':[x.version for x in discover_migrations()],"
                "'assets':{p:__import__('hashlib').sha256(root.joinpath(p).read_bytes()).hexdigest() "
                "for p in json.loads(assets)}}))"
            ),
        ],
        cwd=tmp_path,
        env=env,
    )
    payload = json.loads(smoke.stdout)
    assert payload["version"] == "3.3.2"
    assert payload["migrations"] == [1, 2, 3, 4, 5, 6]
    assert payload["assets"] == {
        name: hashlib.sha256((PROJECT_ROOT / "memplex" / name).read_bytes()).hexdigest()
        for name in PACKAGED_ASSETS
    }
    _run([str(venv_python), "-m", "memplex", "--help"], cwd=tmp_path, env=env)

    _run(
        ["uv", "pip", "uninstall", "--python", str(venv_python), "memplex"],
        cwd=tmp_path,
        env=env,
    )
    removed = subprocess.run(
        [
            str(venv_python),
            "-c",
            "import importlib.util; raise SystemExit(importlib.util.find_spec('memplex') is not None)",
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert removed.returncode == 0, (removed.stdout, removed.stderr)
    assert not any(
        path.exists()
        for path in (
            Path(env["CODEX_HOME"]),
            Path(env["CLAUDE_CONFIG_DIR"]),
            Path(env["OPENCLAW_CONFIG_DIR"]),
            Path(env["HERMES_CONFIG_DIR"]),
        )
    )


def test_sdist_installs_in_isolated_python_environment(
    tmp_path: Path, release_bundle: Path
) -> None:
    env = _isolated_env(tmp_path)
    venv = tmp_path / "sdist-venv"
    _run(["uv", "venv", "--python", "3.13", str(venv)], cwd=tmp_path, env=env)
    python = _python_path(venv)
    _run(
        [
            "uv",
            "pip",
            "install",
            "--python",
            str(python),
            str(release_bundle / "memplex-3.3.2.tar.gz"),
        ],
        cwd=tmp_path,
        env=env,
    )
    result = _run(
        [str(python), "-c", "import importlib.metadata as m; print(m.version('memplex'))"],
        cwd=tmp_path,
        env=env,
    )
    assert result.stdout.strip() == "3.3.2"


def test_npm_tgz_fresh_install_reinstall_dry_run_and_uninstall_are_isolated(
    tmp_path: Path, release_bundle: Path
) -> None:
    env = _isolated_env(tmp_path)
    node_major = int(_run(["node", "--version"], cwd=tmp_path, env=env).stdout.split(".")[0][1:])
    assert node_major in {22, 24}
    prefix = tmp_path / "npm-prefix"
    package = release_bundle / "memplex-3.3.2.tgz"
    install = [
        "npm",
        "install",
        "--prefix",
        str(prefix),
        "--ignore-scripts",
        "--no-audit",
        "--no-fund",
        str(package),
    ]
    _run(install, cwd=tmp_path, env=env)
    _run(install, cwd=tmp_path, env=env)
    installed = prefix / "node_modules" / "memplex"
    assert (installed / "install-agent.sh").read_bytes() == (
        PROJECT_ROOT / "scripts" / "install-agent.sh"
    ).read_bytes()
    bin_path = prefix / "node_modules" / ".bin" / "memplex"
    _run([str(bin_path), "--help"], cwd=tmp_path, env=env)
    _run(
        [
            str(bin_path),
            "setup",
            "--agent",
            "codex",
            "--dry-run",
            "--venv-dir",
            str(tmp_path / "agent-venv"),
        ],
        cwd=tmp_path,
        env=env,
    )
    _run(
        ["npm", "uninstall", "--prefix", str(prefix), "memplex", "--no-audit", "--no-fund"],
        cwd=tmp_path,
        env=env,
    )
    assert not installed.exists()
    assert not bin_path.exists()


def test_failed_npm_install_does_not_replace_verified_package(
    tmp_path: Path, release_bundle: Path
) -> None:
    env = _isolated_env(tmp_path)
    prefix = tmp_path / "npm-prefix"
    package = release_bundle / "memplex-3.3.2.tgz"
    _run(
        ["npm", "install", "--prefix", str(prefix), "--ignore-scripts", str(package)],
        cwd=tmp_path,
        env=env,
    )
    installed_package = prefix / "node_modules" / "memplex" / "package.json"
    before = hashlib.sha256(installed_package.read_bytes()).hexdigest()
    failed = subprocess.run(
        [
            "npm",
            "install",
            "--prefix",
            str(prefix),
            "--ignore-scripts",
            str(tmp_path / "missing.tgz"),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert failed.returncode != 0
    assert hashlib.sha256(installed_package.read_bytes()).hexdigest() == before
