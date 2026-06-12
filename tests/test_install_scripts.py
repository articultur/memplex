"""Tests for one-command external installers."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AGENT_INSTALLER = PROJECT_ROOT / "scripts" / "install-agent.sh"
HERMES_INSTALLER = PROJECT_ROOT / "scripts" / "install-hermes.sh"
NPM_AGENT_PACKAGE = PROJECT_ROOT / "npm" / "agent-installer" / "package.json"
NPM_AGENT_BIN = PROJECT_ROOT / "npm" / "agent-installer" / "bin" / "memplex-install-agent.js"
NPM_PACKAGE = PROJECT_ROOT / "npm" / "hermes-installer" / "package.json"
NPM_BIN = PROJECT_ROOT / "npm" / "hermes-installer" / "bin" / "memplex-install-hermes.js"
NPM_MEMPLEX_PACKAGE = PROJECT_ROOT / "npm" / "memplex" / "package.json"
NPM_MEMPLEX_BIN = PROJECT_ROOT / "npm" / "memplex" / "bin" / "memplex.js"
NODE_BIN = shutil.which("node") or "node"


def test_installer_shell_syntax():
    for script in (AGENT_INSTALLER, HERMES_INSTALLER):
        result = subprocess.run(
            ["bash", "-n", str(script)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, result.stderr


def test_agent_installer_dry_run_uses_persistent_python(tmp_path):
    result = subprocess.run(
        [
            "bash",
            str(AGENT_INSTALLER),
            "--dry-run",
            "--agent",
            "hermes",
            "--package",
            "memplex",
            "--project-path",
            "/repo/a",
            "--user-id",
            "alice",
            "--hermes-config-dir",
            str(tmp_path / "hermes"),
            "--venv-dir",
            str(tmp_path / "venv"),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        env={"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    assert "uv venv" not in result.stdout
    assert str(tmp_path / "venv" / "bin" / "python") in result.stdout
    assert "-m memplex agent install --agent hermes" in result.stdout
    assert "--project-path /repo/a" in result.stdout
    assert "--user-id alice" in result.stdout


def test_hermes_installer_wrapper_delegates_to_agent_installer(tmp_path):
    result = subprocess.run(
        [
            "bash",
            str(HERMES_INSTALLER),
            "--dry-run",
            "--package",
            "memplex",
            "--project-path",
            "/repo/a",
            "--user-id",
            "alice",
            "--venv-dir",
            str(tmp_path / "venv"),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        env={"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    assert "-m memplex agent install --agent hermes" in result.stdout
    assert "--project-path /repo/a" in result.stdout
    assert "--user-id alice" in result.stdout


def test_agent_installer_auto_detects_local_agents(tmp_path):
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".hermes").mkdir()
    result = subprocess.run(
        [
            "bash",
            str(AGENT_INSTALLER),
            "--dry-run",
            "--package",
            "memplex",
            "--venv-dir",
            str(tmp_path / "venv"),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    assert "detected agents: codex hermes" in result.stdout
    assert "-m memplex agent install --agent codex" in result.stdout
    assert "-m memplex agent install --agent hermes" in result.stdout
    assert "-m memplex agent install --agent all" not in result.stdout


def test_agent_installer_auto_without_detected_agent_stops_before_install(tmp_path):
    result = subprocess.run(
        [
            "bash",
            str(AGENT_INSTALLER),
            "--dry-run",
            "--package",
            "memplex",
            "--venv-dir",
            str(tmp_path / "venv"),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        env={"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode != 0
    assert "no supported local agents detected" in result.stderr
    assert "venv" not in result.stdout
    assert "-m memplex" not in result.stdout


def test_agent_installer_all_uses_transactional_cli_path(tmp_path):
    result = subprocess.run(
        [
            "bash",
            str(AGENT_INSTALLER),
            "--dry-run",
            "--agent",
            "all",
            "--package",
            "memplex",
            "--venv-dir",
            str(tmp_path / "venv"),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        env={"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    assert "-m memplex agent install --agent all" in result.stdout


def test_npm_hermes_installer_package_shape():
    memplex_package = json.loads(NPM_MEMPLEX_PACKAGE.read_text())
    assert memplex_package["name"] == "memplex"
    assert memplex_package["version"] == "3.2.7"
    assert memplex_package["bin"]["memplex"] == "bin/memplex.js"
    memplex_script = NPM_MEMPLEX_BIN.read_text()
    assert "npx memplex setup" in memplex_script
    assert "memplex==3.2.7" in memplex_script

    agent_package = json.loads(NPM_AGENT_PACKAGE.read_text())
    assert agent_package["name"] == "@articultur/memplex-agent-installer"
    assert agent_package["version"] == "0.2.0"
    assert agent_package["bin"]["memplex-install-agent"] == "bin/memplex-install-agent.js"
    agent_script = NPM_AGENT_BIN.read_text()
    assert "MEMPLEX_INSTALL_SCRIPT_URL" in agent_script
    assert "install-agent.sh" in agent_script

    package = json.loads(NPM_PACKAGE.read_text())
    assert package["name"] == "@articultur/memplex-hermes-installer"
    assert package["version"] == "0.2.0"
    assert package["bin"]["memplex-install-hermes"] == "bin/memplex-install-hermes.js"
    script = NPM_BIN.read_text()
    assert "MEMPLEX_INSTALL_SCRIPT_URL" in script
    assert "install-agent.sh" in script
    assert '"--agent", "hermes"' in script


def test_npm_memplex_setup_runs_hosted_installer_dry_run(tmp_path):
    result = subprocess.run(
        [
            NODE_BIN,
            str(NPM_MEMPLEX_BIN),
            "setup",
            "--agent",
            "codex",
            "--dry-run",
            "--venv-dir",
            str(tmp_path / "venv"),
            "--project-path",
            "/repo/a",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        env={
            "HOME": str(tmp_path / "home"),
            "PATH": "/usr/bin:/bin",
            "MEMPLEX_INSTALL_SCRIPT_URL": f"file://{AGENT_INSTALLER}",
        },
    )
    assert result.returncode == 0, result.stderr
    assert "memplex==3.2.7" in result.stdout
    assert "-m memplex agent install --agent codex" in result.stdout
    assert "--project-path /repo/a" in result.stdout


def test_npm_memplex_uninstall_aliases_hosted_installer(tmp_path):
    result = subprocess.run(
        [
            NODE_BIN,
            str(NPM_MEMPLEX_BIN),
            "uninstall",
            "--agent",
            "codex",
            "--dry-run",
            "--venv-dir",
            str(tmp_path / "venv"),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        env={
            "HOME": str(tmp_path / "home"),
            "PATH": "/usr/bin:/bin",
            "MEMPLEX_INSTALL_SCRIPT_URL": f"file://{AGENT_INSTALLER}",
        },
    )
    assert result.returncode == 0, result.stderr
    assert "-m memplex agent uninstall --agent codex" in result.stdout
