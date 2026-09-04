#!/usr/bin/env python3
"""Run the real four-host lifecycle gate and emit deployment-bound signed evidence."""

from __future__ import annotations

import argparse
import json
import os
import stat
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as element_tree
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
try:
    import memplex as _memplex_package
except ModuleNotFoundError:
    sys.path.insert(0, str(PROJECT_ROOT))

from memplex.adapters.agent_installer import inspect_agent_installation
from memplex.host_lifecycle import (
    HostLifecycleBinding,
    HostLifecycleEvidence,
    HostLifecycleIntegrityError,
    HostLifecycleProof,
    current_host_contract_digests,
    required_host_node_results,
    required_node_manifest_sha256,
    write_host_lifecycle_evidence,
)

_HERMES_REVISION = "3c27eb6234bf91b8ceee9e9071591b31e9b148cb"
_HERMES_PROVIDER_SHA256 = "678c9150852f2018182e08622ae25b495360fd5099747f823c35e00cce08d8dd"
_SELECTED = (
    "tests/test_agent_installer_registry.py",
    "tests/test_codex_plugin.py",
    "tests/test_openclaw_plugin.py",
    "tests/test_hermes_memory_provider.py",
    "tests/test_agent_hot_paths.py",
    "tests/test_agent_diagnostics.py",
    "tests/test_hooks.py",
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex-cli", type=Path, required=True)
    parser.add_argument("--claude-cli", type=Path, required=True)
    parser.add_argument("--openclaw-cli", type=Path, required=True)
    parser.add_argument("--hermes-cli", type=Path, required=True)
    parser.add_argument("--hermes-source-root", type=Path, required=True)
    parser.add_argument("--isolated-root", type=Path, required=True)
    parser.add_argument("--hermes-revision", default=_HERMES_REVISION)
    parser.add_argument("--evidence-output", type=Path, required=True)
    parser.add_argument("--source-sha256", required=True)
    parser.add_argument("--artifact-sha256", required=True)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--target-identity-sha256", required=True)
    parser.add_argument("--key-id", required=True)
    parser.add_argument("--key-env", default="MEMPLEX_HOST_LIFECYCLE_HMAC_KEY")
    return parser


def _file_sha256(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise HostLifecycleIntegrityError("host_lifecycle_binding_missing")
    return sha256(path.read_bytes()).hexdigest()


def _tree_sha256(root: Path) -> str:
    if root.is_symlink() or not root.is_dir():
        raise HostLifecycleIntegrityError("host_lifecycle_isolation_invalid")
    digest = sha256()
    files = [path for path in root.rglob("*") if path.is_file()]
    for path in sorted(files, key=lambda item: item.as_posix()):
        if path.is_symlink():
            raise HostLifecycleIntegrityError("host_lifecycle_isolation_invalid")
        relative = path.relative_to(root).as_posix().encode("utf-8")
        payload = path.read_bytes()
        digest.update(relative)
        digest.update(b"\x00")
        digest.update(len(payload).to_bytes(8, "big"))
        digest.update(payload)
    if not files:
        raise HostLifecycleIntegrityError("host_lifecycle_isolation_invalid")
    return digest.hexdigest()


def _run_version(command: list[str], *, expected: str) -> str:
    result = subprocess.run(
        command, cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=30, check=False
    )
    output = (result.stdout + "\n" + result.stderr).strip()
    if result.returncode != 0 or expected not in output:
        raise HostLifecycleIntegrityError("host_cli_version_invalid")
    first_line = next((line.strip() for line in output.splitlines() if line.strip()), "")
    if not first_line or "\x00" in first_line:
        raise HostLifecycleIntegrityError("host_cli_version_invalid")
    return first_line


def _require_regular_executable(path: Path) -> Path:
    try:
        resolved = path.resolve(strict=True)
        info = resolved.stat(follow_symlinks=False)
    except OSError as exc:
        raise HostLifecycleIntegrityError("host_cli_missing") from exc
    if not resolved.is_file() or not stat.S_ISREG(info.st_mode) or not os.access(resolved, os.X_OK):
        raise HostLifecycleIntegrityError("host_cli_missing")
    return resolved


def _host_roots(isolated_root: Path) -> dict[str, Path]:
    if isolated_root.is_symlink():
        raise HostLifecycleIntegrityError("host_lifecycle_isolation_invalid")
    root = isolated_root.expanduser().resolve(strict=False)
    home = Path.home().resolve()
    if not root.is_absolute() or root in {Path(root.anchor), home} or root.parent == home:
        raise HostLifecycleIntegrityError("host_lifecycle_isolation_invalid")
    return {host: root / host for host in ("claude-code", "codex", "hermes", "openclaw")}


def _require_isolated_installations(roots: dict[str, Path]) -> None:
    for host, root in roots.items():
        report = inspect_agent_installation(host, target_dir=root)
        runtime = report.get("runtime_status")
        install_state = report.get("install_state")
        if (
            report.get("selected_host") != host
            or report.get("status") != "healthy"
            or not isinstance(install_state, dict)
            or not all(
                install_state.get(key) is True for key in ("installed", "selected", "managed")
            )
            or not isinstance(runtime, dict)
            or runtime.get("state") != "healthy"
        ):
            raise HostLifecycleIntegrityError("host_lifecycle_isolation_invalid")


def _run_checked(
    command: list[str],
    *,
    env: dict[str, str],
    expected: str,
) -> str:
    result = subprocess.run(
        command,
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    output = (result.stdout + "\n" + result.stderr).strip()
    if result.returncode != 0 or expected not in output:
        raise HostLifecycleIntegrityError("host_cli_start_invalid")
    return result.stdout.strip()


def _start_real_hosts(paths: dict[str, Path], roots: dict[str, Path]) -> None:
    isolated_root = next(iter(roots.values())).parent
    homes = isolated_root / "homes"
    homes.mkdir(mode=0o700, parents=True, exist_ok=True)
    for host in roots:
        (homes / host).mkdir(mode=0o700, exist_ok=True)

    codex_output = _run_checked(
        [str(paths["codex"]), "plugin", "list", "--available", "--json"],
        env={
            **os.environ,
            "HOME": str(homes / "codex"),
            "CODEX_HOME": str(roots["codex"]),
        },
        expected="memplex@memplex",
    )
    try:
        codex_payload = json.loads(codex_output)
        codex_installed = codex_payload["installed"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise HostLifecycleIntegrityError("host_cli_start_invalid") from exc
    if not any(
        item.get("pluginId") == "memplex@memplex" and item.get("enabled") is True
        for item in codex_installed
        if isinstance(item, dict)
    ):
        raise HostLifecycleIntegrityError("host_cli_start_invalid")

    claude_plugin = roots["claude-code"] / "plugins" / "marketplaces" / "articultur" / "plugin"
    _run_checked(
        [str(paths["claude-code"]), "plugin", "validate", "--strict", str(claude_plugin)],
        env={
            **os.environ,
            "HOME": str(homes / "claude-code"),
            "CLAUDE_CONFIG_DIR": str(roots["claude-code"]),
        },
        expected="Validation passed",
    )
    openclaw_output = _run_checked(
        [str(paths["openclaw"]), "plugins", "inspect", "memplex", "--runtime", "--json"],
        env={
            **os.environ,
            "HOME": str(homes / "openclaw"),
            "OPENCLAW_HOME": str(homes / "openclaw"),
            "OPENCLAW_STATE_DIR": str(roots["openclaw"]),
            "OPENCLAW_CONFIG_PATH": str(roots["openclaw"] / "openclaw.json"),
        },
        expected="memplex",
    )
    try:
        openclaw_plugin = json.loads(openclaw_output)["plugin"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise HostLifecycleIntegrityError("host_cli_start_invalid") from exc
    if (
        not isinstance(openclaw_plugin, dict)
        or openclaw_plugin.get("id") != "memplex"
        or openclaw_plugin.get("status") != "loaded"
        or openclaw_plugin.get("memorySlotSelected") is not True
    ):
        raise HostLifecycleIntegrityError("host_cli_start_invalid")
    _run_checked(
        [str(paths["hermes"]), "memory", "status"],
        env={
            **os.environ,
            "HOME": str(homes / "hermes"),
            "HERMES_HOME": str(roots["hermes"]),
            "HERMES_CONFIG_DIR": str(roots["hermes"]),
        },
        expected="Provider:  memplex",
    )


def _node_id(testcase: element_tree.Element) -> str:
    classname = testcase.get("classname", "")
    name = testcase.get("name", "")
    if not classname or not name:
        raise HostLifecycleIntegrityError("host_lifecycle_junit_invalid")
    return f"{classname.replace('.', '/')}.py::{name}"


def _required_junit_results(
    report: Path,
    required_nodes: tuple[str, ...],
    *,
    pytest_output: str = "",
) -> tuple[tuple[str, str], ...]:
    """Return exact passed results; missing, skipped, xfailed, failed all fail closed."""
    try:
        root = element_tree.parse(report).getroot()
    except (OSError, element_tree.ParseError) as exc:
        raise HostLifecycleIntegrityError("host_lifecycle_junit_invalid") from exc
    cases: dict[str, str] = {}
    for testcase in root.iter("testcase"):
        node_id = _node_id(testcase)
        outcome = "passed"
        if testcase.find("skipped") is not None:
            outcome = "skipped"
        elif testcase.find("failure") is not None or testcase.find("error") is not None:
            outcome = "failed"
        if node_id in cases:
            raise HostLifecycleIntegrityError("host_lifecycle_junit_invalid")
        cases[node_id] = outcome
    results: list[tuple[str, str]] = []
    xpassed = {
        line.removeprefix("XPASS ").split(" - ", 1)[0].strip()
        for line in pytest_output.splitlines()
        if line.startswith("XPASS ")
    }
    for expected in required_nodes:
        if expected not in cases or cases[expected] != "passed" or expected in xpassed:
            raise HostLifecycleIntegrityError("host_lifecycle_suite_failed")
        results.append((expected, "passed"))
    return tuple(sorted(results))


def _required_nodes_from_collection(collection: str) -> dict[str, tuple[str, ...]]:
    nodes = tuple(line.strip() for line in collection.splitlines() if line.startswith("tests/"))
    if not nodes or len(set(nodes)) != len(nodes):
        raise HostLifecycleIntegrityError("host_lifecycle_suite_incomplete")
    collected = frozenset(nodes)
    required = {
        host: tuple(node for node, _outcome in required_host_node_results(host))
        for host in ("claude-code", "codex", "hermes", "openclaw")
    }
    if any(not frozenset(expected).issubset(collected) for expected in required.values()):
        raise HostLifecycleIntegrityError("host_lifecycle_suite_incomplete")
    return required


def _pytest_command() -> list[str]:
    return [
        sys.executable,
        "-m",
        "pytest",
        "-c",
        str(PROJECT_ROOT / "pyproject.toml"),
        "--import-mode=importlib",
    ]


def _run_lifecycle_suite(
    env: dict[str, str],
) -> tuple[dict[str, tuple[tuple[str, str], ...]], str]:
    with tempfile.TemporaryDirectory(prefix="memplex-g008-") as temporary:
        run_directory = Path(temporary) / "run"
        run_directory.mkdir()
        base = Path(temporary) / "isolated-pytest"
        report = Path(temporary) / "host-lifecycle.junit.xml"
        selected = tuple(str(PROJECT_ROOT / path) for path in _SELECTED)
        isolated_env = {**env, "PYTHONNOUSERSITE": "1"}
        isolated_env.pop("PYTHONPATH", None)
        collection = subprocess.run(
            [*_pytest_command(), "--collect-only", "-q", *selected],
            cwd=run_directory,
            env=isolated_env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if collection.returncode != 0:
            raise HostLifecycleIntegrityError("host_lifecycle_suite_incomplete")
        required_nodes = _required_nodes_from_collection(collection.stdout)
        result = subprocess.run(
            [
                *_pytest_command(),
                "-q",
                "-rXx",
                f"--junitxml={report}",
                f"--basetemp={base}",
                *selected,
            ],
            cwd=run_directory,
            env=isolated_env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if result.returncode != 0:
            raise HostLifecycleIntegrityError("host_lifecycle_suite_failed")
        junit_sha256 = _file_sha256(report)
        results = {
            host: _required_junit_results(
                report,
                nodes,
                pytest_output=result.stdout + "\n" + result.stderr,
            )
            for host, nodes in required_nodes.items()
        }
        _tree_sha256(base)
        return results, junit_sha256


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
        if _file_sha256(provider_source) != _HERMES_PROVIDER_SHA256:
            raise HostLifecycleIntegrityError("hermes_provider_digest_mismatch")
        binding = HostLifecycleBinding(
            deployment_id=args.deployment_id,
            source_sha256=args.source_sha256,
            artifact_sha256=args.artifact_sha256,
            target_identity_sha256=args.target_identity_sha256,
            expected_key_id=args.key_id,
        )

        paths = {
            "codex": _require_regular_executable(args.codex_cli),
            "claude-code": _require_regular_executable(args.claude_cli),
            "openclaw": _require_regular_executable(args.openclaw_cli),
            "hermes": _require_regular_executable(args.hermes_cli),
        }
        tested_cli_sha256 = {host: _file_sha256(path) for host, path in paths.items()}
        roots = _host_roots(args.isolated_root)
        if not args.isolated_root.is_dir():
            raise HostLifecycleIntegrityError("host_lifecycle_isolation_invalid")
        _require_isolated_installations(roots)
        versions = {
            "codex": _run_version([str(paths["codex"]), "--version"], expected="codex-cli"),
            "claude-code": _run_version(
                [str(paths["claude-code"]), "--version"], expected="Claude Code"
            ),
            "openclaw": _run_version([str(paths["openclaw"]), "--version"], expected="OpenClaw"),
            "hermes": _run_version(
                [str(paths["hermes"]), "--version"], expected="Hermes Agent v0.20.0 (2026.8.3)"
            ),
        }
        _start_real_hosts(paths, roots)
        _require_isolated_installations(roots)
        test_env = {
            **os.environ,
            "MEMPLEX_G008_CODEX_CLI": str(paths["codex"]),
            "MEMPLEX_G008_CLAUDE_CLI": str(paths["claude-code"]),
            "MEMPLEX_G008_OPENCLAW_CLI": str(paths["openclaw"]),
            "MEMPLEX_G008_HERMES_CLI": str(paths["hermes"]),
            "MEMPLEX_G008_HERMES_SOURCE_ROOT": str(args.hermes_source_root),
        }
        results, junit_sha256 = _run_lifecycle_suite(test_env)
        contracts = current_host_contract_digests()
        proofs = tuple(
            HostLifecycleProof(
                host=host,
                cli_path=str(paths[host]),
                cli_sha256=tested_cli_sha256[host],
                cli_version=versions[host],
                contract_sha256=contracts[host],
                isolated_root_sha256=_tree_sha256(roots[host]),
                required_node_results=results[host],
                required_node_manifest_sha256=required_node_manifest_sha256(results[host]),
                junit_sha256=junit_sha256,
            )
            for host in ("claude-code", "codex", "hermes", "openclaw")
        )
        evidence = HostLifecycleEvidence.create(
            memplex_version=version("memplex"),
            host_proofs=proofs,
            binding=binding,
            key_id=args.key_id,
            signing_key=signing_key,
        )
        write_host_lifecycle_evidence(args.evidence_output, evidence)
        evidence.verify(signing_key, expected_version=version("memplex"), expected_binding=binding)
    except (OSError, UnicodeError, ValueError, HostLifecycleIntegrityError) as exc:
        print(getattr(exc, "code", None) or repr(exc), file=sys.stderr)
        print('{"schema_version":1,"status":"failed"}')
        return 2
    print(json.dumps({"schema_version": 1, "status": "passed"}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
