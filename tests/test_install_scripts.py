"""Tests for one-command external installers."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AGENT_INSTALLER = PROJECT_ROOT / "scripts" / "install-agent.sh"
HERMES_INSTALLER = PROJECT_ROOT / "scripts" / "install-hermes.sh"
NPM_AGENT_PACKAGE = PROJECT_ROOT / "npm" / "archive" / "agent-installer" / "package.json"
NPM_AGENT_BIN = (
    PROJECT_ROOT / "npm" / "archive" / "agent-installer" / "bin" / "memplex-install-agent.js"
)
NPM_PACKAGE = PROJECT_ROOT / "npm" / "archive" / "hermes-installer" / "package.json"
NPM_BIN = (
    PROJECT_ROOT / "npm" / "archive" / "hermes-installer" / "bin" / "memplex-install-hermes.js"
)
NPM_MEMPLEX_PACKAGE = PROJECT_ROOT / "npm" / "memplex" / "package.json"
NPM_MEMPLEX_BIN = PROJECT_ROOT / "npm" / "memplex" / "bin" / "memplex.js"
NPM_MEMPLEX_INSTALLER = PROJECT_ROOT / "npm" / "memplex" / "install-agent.sh"
NODE_BIN = shutil.which("node") or "node"
NPM_BIN_EXECUTABLE = shutil.which("npm") or "npm"
RELEASE_INSTALL_SURFACES = (
    AGENT_INSTALLER,
    HERMES_INSTALLER,
    NPM_MEMPLEX_INSTALLER,
    PROJECT_ROOT / "README.md",
    PROJECT_ROOT / "docs" / "getting-started.md",
    PROJECT_ROOT / "docs" / "agent-integration.md",
)


def test_installer_shell_syntax():
    for script in (AGENT_INSTALLER, HERMES_INSTALLER):
        result = subprocess.run(
            ["bash", "-n", str(script)],
            capture_output=True,
            text=True,
            timeout=10,check=False
        
        )
        assert result.returncode == 0, result.stderr


def test_release_install_surfaces_never_execute_mutable_remote_scripts() -> None:
    forbidden = (
        "raw.githubusercontent.com/articultur/memplex/main",
        "curl -fsSL",
        "MEMPLEX_INSTALL_AGENT_SCRIPT_URL",
        "MEMPLEX_INSTALL_SCRIPT_URL",
        "MEMPLEX_PACKAGE",
    )
    for path in RELEASE_INSTALL_SURFACES:
        text = path.read_text(encoding="utf-8")
        assert not any(marker in text for marker in forbidden), path


def test_hermes_compatibility_wrapper_fails_closed_without_packaged_installer(
    tmp_path: Path,
) -> None:
    wrapper = tmp_path / "install-hermes.sh"
    shutil.copyfile(HERMES_INSTALLER, wrapper)
    result = subprocess.run(
        ["bash", str(wrapper), "--dry-run"],
        capture_output=True,
        text=True,
        timeout=10,
        env={"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"},check=False
    
    )
    assert result.returncode != 0
    assert "packaged install-agent.sh is missing" in result.stderr
    assert "http" not in result.stderr


def test_agent_installer_dry_run_uses_persistent_python(tmp_path):
    result = subprocess.run(
        [
            "bash",
            str(AGENT_INSTALLER),
            "--dry-run",
            "--agent",
            "hermes",
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
        env={"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"},check=False
    
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
        env={"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"},check=False
    
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
            "--venv-dir",
            str(tmp_path / "venv"),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},check=False
    
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
            "--venv-dir",
            str(tmp_path / "venv"),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        env={"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"},check=False
    
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
            "--venv-dir",
            str(tmp_path / "venv"),
        ],
        capture_output=True,
        text=True,
        timeout=10,
        env={"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"},check=False
    
    )
    assert result.returncode == 0, result.stderr
    assert "-m memplex agent install --agent all" in result.stdout


def test_npm_hermes_installer_package_shape():
    memplex_package = json.loads(NPM_MEMPLEX_PACKAGE.read_text())
    assert memplex_package["name"] == "memplex"
    assert memplex_package["version"] == "3.3.0"
    assert memplex_package["bin"]["memplex"] == "bin/memplex.js"
    memplex_script = NPM_MEMPLEX_BIN.read_text()
    assert "npx memplex setup" in memplex_script
    assert "memplex==3.3.0" in NPM_MEMPLEX_INSTALLER.read_text()
    assert "MEMPLEX_INSTALL_SCRIPT_URL" not in memplex_script
    assert "curl" not in memplex_script
    assert "bash" in memplex_script
    assert NPM_MEMPLEX_INSTALLER.read_bytes() == AGENT_INSTALLER.read_bytes()

    agent_package = json.loads(NPM_AGENT_PACKAGE.read_text())
    assert agent_package["name"] == "@articultur/memplex-agent-installer"
    assert agent_package["version"] == "0.2.0"
    assert agent_package["bin"]["memplex-install-agent"] == "bin/memplex-install-agent.js"
    assert agent_package["dependencies"] == {"memplex": "3.3.0"}
    agent_script = NPM_AGENT_BIN.read_text()
    assert "MEMPLEX_INSTALL_SCRIPT_URL" not in agent_script
    assert "curl" not in agent_script
    assert 'require.resolve("memplex/bin/memplex.js")' in agent_script

    package = json.loads(NPM_PACKAGE.read_text())
    assert package["name"] == "@articultur/memplex-hermes-installer"
    assert package["version"] == "0.2.0"
    assert package["bin"]["memplex-install-hermes"] == "bin/memplex-install-hermes.js"
    assert package["dependencies"] == {"memplex": "3.3.0"}
    script = NPM_BIN.read_text()
    assert "MEMPLEX_INSTALL_SCRIPT_URL" not in script
    assert "curl" not in script
    assert 'require.resolve("memplex/bin/memplex.js")' in script
    assert '"--agent", "hermes"' in script


def test_npm_memplex_setup_runs_packaged_installer_dry_run(tmp_path):
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
            "MEMPLEX_INSTALL_SCRIPT_URL": "https://attacker.invalid/installer.sh",
            "MEMPLEX_PACKAGE": "attacker-package",
        },check=False
    
    )
    assert result.returncode == 0, result.stderr
    assert "memplex==3.3.0" in result.stdout
    assert "attacker-package" not in result.stdout + result.stderr
    assert "-m memplex agent install --agent codex" in result.stdout
    assert "--project-path /repo/a" in result.stdout


def test_npm_memplex_rejects_python_package_override(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            NODE_BIN,
            str(NPM_MEMPLEX_BIN),
            "setup",
            "--agent",
            "codex",
            "--package",
            "attacker-package",
            "--dry-run",
        ],
        capture_output=True,
        text=True,
        timeout=10,
        env={"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin"},check=False
    
    )
    assert result.returncode != 0
    assert "package override is not allowed" in result.stderr
    assert "attacker-package" not in result.stderr


def test_npm_memplex_uninstall_aliases_packaged_installer(tmp_path):
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
            "MEMPLEX_INSTALL_SCRIPT_URL": "https://attacker.invalid/installer.sh",
        },check=False
    
    )
    assert result.returncode == 0, result.stderr
    assert "-m memplex agent uninstall --agent codex" in result.stdout


def test_npm_memplex_pack_contains_only_version_bound_local_installer() -> None:
    result = subprocess.run(
        [NPM_BIN_EXECUTABLE, "pack", "--dry-run", "--json"],
        cwd=NPM_MEMPLEX_PACKAGE.parent,
        capture_output=True,
        text=True,
        timeout=30,check=False
    
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert len(payload) == 1
    assert {item["path"] for item in payload[0]["files"]} == {
        "bin/memplex.js",
        "install-agent.sh",
        "package.json",
    }
