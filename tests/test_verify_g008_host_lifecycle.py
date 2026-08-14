from __future__ import annotations

import importlib.util
import sys
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from memplex.host_lifecycle import HostLifecycleIntegrityError, required_host_node_results


def _script_module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "verify_g008_host_lifecycle.py"
    spec = importlib.util.spec_from_file_location("verify_g008_host_lifecycle", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_junit_parser_rejects_required_skip_and_accepts_only_passes(tmp_path):
    verifier = _script_module()
    report = tmp_path / "report.xml"
    report.write_text(
        """<?xml version="1.0"?><testsuites><testsuite><testcase classname="tests.test_codex_plugin" name="test_real"/><testcase classname="tests.test_openclaw_plugin" name="test_real"><skipped/></testcase></testsuite></testsuites>""",
        encoding="utf-8",
    )

    with pytest.raises(HostLifecycleIntegrityError):
        verifier._required_junit_results(
            report,
            ("tests/test_codex_plugin.py::test_real", "tests/test_openclaw_plugin.py::test_real"),
        )

    report.write_text(
        """<?xml version="1.0"?><testsuites><testsuite><testcase classname="tests.test_codex_plugin" name="test_real"/><testcase classname="tests.test_openclaw_plugin" name="test_real"/></testsuite></testsuites>""",
        encoding="utf-8",
    )
    results = verifier._required_junit_results(
        report,
        ("tests/test_codex_plugin.py::test_real", "tests/test_openclaw_plugin.py::test_real"),
    )
    assert results == (
        ("tests/test_codex_plugin.py::test_real", "passed"),
        ("tests/test_openclaw_plugin.py::test_real", "passed"),
    )
    assert verifier._file_sha256(report) == sha256(report.read_bytes()).hexdigest()

    with pytest.raises(HostLifecycleIntegrityError):
        verifier._required_junit_results(
            report,
            ("tests/test_codex_plugin.py::test_real",),
            pytest_output=("XPASS tests/test_codex_plugin.py::test_real - expected failure\n"),
        )


def test_verifier_requires_explicit_deployment_binding_arguments() -> None:
    verifier = _script_module()
    with pytest.raises(SystemExit):
        verifier._parser().parse_args(
            [
                "--codex-cli",
                "/bin/true",
                "--claude-cli",
                "/bin/true",
                "--openclaw-cli",
                "/bin/true",
                "--hermes-cli",
                "/bin/true",
                "--hermes-source-root",
                "/tmp/hermes",
                "--evidence-output",
                "/tmp/hosts.json",
                "--key-id",
                "g008-key",
            ]
        )


def test_verifier_maps_every_host_to_one_explicit_nondefault_isolated_root(tmp_path: Path) -> None:
    verifier = _script_module()
    isolated = tmp_path / "g008-root"
    roots = verifier._host_roots(isolated)

    assert roots == {
        "claude-code": isolated.resolve() / "claude-code",
        "codex": isolated.resolve() / "codex",
        "hermes": isolated.resolve() / "hermes",
        "openclaw": isolated.resolve() / "openclaw",
    }
    assert len(set(roots.values())) == 4

    with pytest.raises(HostLifecycleIntegrityError):
        verifier._host_roots(Path.home())

    real_root = tmp_path / "real-root"
    real_root.mkdir()
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(real_root, target_is_directory=True)
    with pytest.raises(HostLifecycleIntegrityError):
        verifier._host_roots(linked_root)


def test_collection_must_contain_the_fixed_required_node_contract() -> None:
    verifier = _script_module()
    complete = sorted(
        {
            node
            for host in ("claude-code", "codex", "hermes", "openclaw")
            for node, _outcome in required_host_node_results(host)
        }
    )
    expected = verifier._required_nodes_from_collection("\n".join(complete))
    assert expected["codex"] == tuple(
        node for node, _outcome in required_host_node_results("codex")
    )

    forged = [node for node in complete if "codex_real_cli" not in node]
    forged.append("tests/test_codex_plugin.py::test_unrelated")
    with pytest.raises(HostLifecycleIntegrityError):
        verifier._required_nodes_from_collection("\n".join(forged))


def test_signed_required_nodes_exclude_unit_only_host_matrix() -> None:
    verifier = _script_module()

    for host in ("claude-code", "codex", "hermes", "openclaw"):
        required = tuple(node for node, _outcome in required_host_node_results(host))
        assert not any("test_agent_host_matrix.py" in node for node in required)
    assert "tests/test_agent_host_matrix.py" not in verifier._SELECTED


def test_lifecycle_pytest_command_uses_current_artifact_interpreter_and_importlib_mode() -> None:
    verifier = _script_module()

    command = verifier._pytest_command()

    assert command[:3] == [sys.executable, "-m", "pytest"]
    assert command[3:] == [
        "-c",
        str(verifier.PROJECT_ROOT / "pyproject.toml"),
        "--import-mode=importlib",
    ]


def test_verifier_rejects_cli_replacement_after_the_lifecycle_suite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The signed digest must name the binary that the lifecycle suite exercised."""
    verifier = _script_module()
    cli_paths: dict[str, Path] = {}
    for host in ("codex", "claude-code", "openclaw", "hermes"):
        path = tmp_path / f"{host}-cli"
        path.write_bytes(f"tested-{host}".encode())
        path.chmod(0o700)
        cli_paths[host] = path

    hermes_source = tmp_path / "hermes-source"
    provider = hermes_source / "agent" / "memory_provider.py"
    provider.parent.mkdir(parents=True)
    provider.write_text("provider", encoding="utf-8")
    isolated_root = tmp_path / "isolated"
    for host in ("claude-code", "codex", "hermes", "openclaw"):
        root = isolated_root / host
        root.mkdir(parents=True)
        (root / "installed").write_text(host, encoding="utf-8")

    args = SimpleNamespace(
        codex_cli=cli_paths["codex"],
        claude_cli=cli_paths["claude-code"],
        openclaw_cli=cli_paths["openclaw"],
        hermes_cli=cli_paths["hermes"],
        hermes_source_root=hermes_source,
        isolated_root=isolated_root,
        hermes_revision=verifier._HERMES_REVISION,
        evidence_output=tmp_path / "evidence.json",
        source_sha256="1" * 64,
        artifact_sha256="2" * 64,
        deployment_id="g016-cli-toctou",
        target_identity_sha256="3" * 64,
        key_id="g008-key",
        key_env="MEMPLEX_HOST_LIFECYCLE_HMAC_KEY",
    )

    class _Parser:
        @staticmethod
        def parse_args():
            return args

    def replace_after_suite(_environment):
        for host, path in cli_paths.items():
            path.write_bytes(f"untested-replacement-{host}".encode())
        return (
            {
                host: verifier.required_host_node_results(host)
                for host in ("claude-code", "codex", "hermes", "openclaw")
            },
            "4" * 64,
        )

    monkeypatch.setenv(args.key_env, (b"h" * 32).hex())
    monkeypatch.setattr(verifier, "_parser", lambda: _Parser())
    monkeypatch.setattr(
        verifier, "_HERMES_PROVIDER_SHA256", sha256(provider.read_bytes()).hexdigest()
    )
    monkeypatch.setattr(verifier, "_require_isolated_installations", lambda _roots: None)
    monkeypatch.setattr(verifier, "_run_version", lambda _command, expected: expected)
    monkeypatch.setattr(verifier, "_start_real_hosts", lambda _paths, _roots: None)
    monkeypatch.setattr(verifier, "_run_lifecycle_suite", replace_after_suite)

    assert verifier.main() == 2
    assert not args.evidence_output.exists()
