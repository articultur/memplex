"""Test the agent-installer registry and dispatch completeness.

Previously the registry refactor (P3.1) had no test asserting that every
supported agent has a working spec (evaluation: '_install_claude_code is
NOT end-to-end tested'). These tests pin the registry contract:
completeness vs AGENT_PROFILES, signature correctness, and dispatch
behaviour for known/unknown agents.
"""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

import pytest  # noqa: E402

from memplex.adapters.agent_installer import (  # noqa: E402
    _INSTALLERS,
    AgentInstallerSpec,
    _install_one,
    _uninstall_one,
)
from memplex.adapters.agent_runtime import AGENT_PROFILES  # noqa: E402

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
    """codex/claude-code are (target_dir,...) only; openclaw/hermes take identity."""
    assert _INSTALLERS["codex"].needs_identity is False
    assert _INSTALLERS["claude-code"].needs_identity is False
    assert _INSTALLERS["openclaw"].needs_identity is True
    assert _INSTALLERS["hermes"].needs_identity is True


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
