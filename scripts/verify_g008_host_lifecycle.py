#!/usr/bin/env python3
"""Run the real four-host lifecycle gate and emit signed redacted evidence."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memplex.host_lifecycle import (  # noqa: E402
    HostLifecycleEvidence,
    HostLifecycleIntegrityError,
    write_host_lifecycle_evidence,
)

_HERMES_REVISION = "3c27eb6234bf91b8ceee9e9071591b31e9b148cb"
_HERMES_PROVIDER_SHA256 = "678c9150852f2018182e08622ae25b495360fd5099747f823c35e00cce08d8dd"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-cli", type=Path, required=True)
    parser.add_argument("--claude-cli", type=Path, required=True)
    parser.add_argument("--openclaw-cli", type=Path, required=True)
    parser.add_argument("--hermes-cli", type=Path, required=True)
    parser.add_argument("--hermes-source-root", type=Path, required=True)
    parser.add_argument("--hermes-revision", default=_HERMES_REVISION)
    parser.add_argument("--evidence-output", type=Path, required=True)
    parser.add_argument("--key-id", default="g008-local-host-matrix-v1")
    parser.add_argument("--key-env", default="MEMPLEX_HOST_LIFECYCLE_HMAC_KEY")
    return parser


def _run_version(command: list[str], *, expected: str) -> str:
    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    output = (result.stdout + "\n" + result.stderr).strip()
    if result.returncode != 0 or expected not in output:
        raise HostLifecycleIntegrityError("host_cli_version_invalid")
    first_line = next((line.strip() for line in output.splitlines() if line.strip()), "")
    if not first_line or "\x00" in first_line:
        raise HostLifecycleIntegrityError("host_cli_version_invalid")
    return first_line


def _require_regular_executable(path: Path) -> Path:
    if path.is_symlink():
        path = path.resolve(strict=True)
    if not path.is_file() or not os.access(path, os.X_OK):
        raise HostLifecycleIntegrityError("host_cli_missing")
    return path


def _run_lifecycle_suite(env: dict[str, str]) -> None:
    selected = [
        "tests/test_agent_installer_registry.py",
        "tests/test_agent_host_matrix.py",
        "tests/test_codex_plugin.py",
        "tests/test_openclaw_plugin.py",
        "tests/test_hermes_memory_provider.py",
        "tests/test_agent_hot_paths.py",
        "tests/test_agent_diagnostics.py",
    ]
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", *selected],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if result.returncode != 0:
        raise HostLifecycleIntegrityError("host_lifecycle_suite_failed")
    required_nodes = (
        "test_codex_real_cli_discovers_plugin_in_isolated_home",
        "test_claude_real_cli_strictly_validates_installed_plugin",
        "test_openclaw_cli_loads_memplex_runtime_from_an_isolated_profile",
        "test_hermes_official_cli_discovers_installed_provider_in_isolated_home",
        "test_four_host_workspace_matrix_is_deterministic",
        "test_user_isolation_breaks_cross_user_recall_across_hosts",
        "test_workspace_isolation_blocks_recall_in_other_workspace",
        "test_session_source_visibility_isolated_from_other_hosts_and_sessions",
        "test_installation_diagnostics_detect_config_drift",
        "test_codex_single_host_failure_restores_preinstall_state",
        "test_claude_single_host_failure_restores_preinstall_state",
        "test_configured_host_failure_restores_preinstall_state[openclaw",
        "test_configured_host_failure_restores_preinstall_state[hermes",
        "test_four_host_reinstall_upgrade_preserves_healthy_state_and_prestate[codex]",
        "test_four_host_reinstall_upgrade_preserves_healthy_state_and_prestate[claude-code]",
        "test_four_host_reinstall_upgrade_preserves_healthy_state_and_prestate[openclaw]",
        "test_four_host_reinstall_upgrade_preserves_healthy_state_and_prestate[hermes]",
    )
    collection = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *selected],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if collection.returncode != 0 or any(name not in collection.stdout for name in required_nodes):
        raise HostLifecycleIntegrityError("host_lifecycle_suite_incomplete")


def main() -> int:
    args = _parser().parse_args()
    try:
        key_text = os.environ.get(args.key_env, "")
        if len(key_text) != 64:
            raise HostLifecycleIntegrityError("host_lifecycle_key_invalid")
        signing_key = bytes.fromhex(key_text)
        if len(signing_key) != 32:
            raise HostLifecycleIntegrityError("host_lifecycle_key_invalid")
        if args.hermes_revision != _HERMES_REVISION:
            raise HostLifecycleIntegrityError("hermes_source_revision_mismatch")
        provider_source = args.hermes_source_root / "agent" / "memory_provider.py"
        if (
            provider_source.is_symlink()
            or not provider_source.is_file()
            or sha256(provider_source.read_bytes()).hexdigest() != _HERMES_PROVIDER_SHA256
        ):
            raise HostLifecycleIntegrityError("hermes_provider_digest_mismatch")

        codex = _require_regular_executable(args.codex_cli)
        claude = _require_regular_executable(args.claude_cli)
        openclaw = _require_regular_executable(args.openclaw_cli)
        hermes = _require_regular_executable(args.hermes_cli)
        versions = {
            "codex": _run_version([str(codex), "--version"], expected="codex-cli"),
            "claude-code": _run_version([str(claude), "--version"], expected="Claude Code"),
            "openclaw": _run_version([str(openclaw), "--version"], expected="OpenClaw"),
            "hermes": _run_version(
                [str(hermes), "--version"],
                expected="Hermes Agent v0.20.0 (2026.8.3)",
            ),
        }
        test_env = {
            **os.environ,
            "MEMPLEX_G008_CODEX_CLI": str(codex),
            "MEMPLEX_G008_CLAUDE_CLI": str(claude),
            "MEMPLEX_G008_OPENCLAW_CLI": str(openclaw),
            "MEMPLEX_G008_HERMES_CLI": str(hermes),
            "MEMPLEX_G008_HERMES_SOURCE_ROOT": str(args.hermes_source_root),
        }
        _run_lifecycle_suite(test_env)
        evidence = HostLifecycleEvidence.create(
            memplex_version=version("memplex"),
            cli_versions=versions,
            key_id=args.key_id,
            signing_key=signing_key,
        )
        write_host_lifecycle_evidence(args.evidence_output, evidence)
        evidence.verify(signing_key, expected_version=version("memplex"))
    except (OSError, UnicodeError, ValueError, HostLifecycleIntegrityError):
        print('{"schema_version":1,"status":"failed"}')
        return 2
    print(json.dumps({"schema_version": 1, "status": "passed"}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
