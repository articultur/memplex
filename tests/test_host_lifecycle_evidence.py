from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from importlib.metadata import version
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

from memplex.host_lifecycle import (
    HostLifecycleBinding,
    HostLifecycleEvidence,
    HostLifecycleIntegrityError,
    HostLifecycleProof,
    _contract_files,
    current_host_contract_digests,
    read_host_lifecycle_evidence,
    required_host_node_results,
    required_node_manifest_sha256,
    write_host_lifecycle_evidence,
)

# Hardcoded manifest of the G008 contract cluster: every file hashed into a
# host readiness proof, mapped to the hosts whose digest it feeds. Adding or
# removing a contract file without updating this map must fail
# test_contract_files_match_coverage_map below.
_CONTRACT_COVERAGE = {
    "memplex/adapters/runtime_status.py": {"claude-code", "codex", "hermes", "openclaw"},
    "memplex/adapters/managed_identity.py": {
        "claude-code",
        "codex",
        "hermes",
        "openclaw",
    },
    "memplex/adapters/agent_installer.py": {
        "claude-code",
        "codex",
        "hermes",
        "openclaw",
    },
    "memplex/adapters/install_transaction.py": {
        "claude-code",
        "codex",
        "hermes",
        "openclaw",
    },
    "memplex/adapters/agent_assets.py": {
        "claude-code",
        "codex",
        "hermes",
        "openclaw",
    },
    "memplex/adapters/agent_runtime.py": {
        "claude-code",
        "codex",
        "hermes",
        "openclaw",
    },
    "memplex/adapters/_shared.py": {
        "claude-code",
        "codex",
        "hermes",
        "openclaw",
    },
    "memplex/_plugin/.claude-plugin/plugin.json": {"claude-code"},
    "memplex/_plugin/.mcp.json": {"claude-code"},
    "memplex/_plugin/hooks/hooks.json": {"claude-code"},
    "memplex/_plugin/scripts/claude-hook.sh": {"claude-code"},
    "memplex/_plugin/scripts/hook-runner.py": {"claude-code"},
    "memplex/_plugin/scripts/mcp-server.sh": {"claude-code", "codex"},
    "memplex/_plugin/.codex-plugin/plugin.json": {"codex"},
    "memplex/_plugin/.codex.mcp.json": {"codex"},
    "memplex/_plugin/hooks/hooks-codex.json": {"codex"},
    "memplex/_plugin/scripts/codex-plugin.sh": {"codex"},
    "memplex/adapters/codex_plugin.py": {"codex"},
    "memplex/adapters/mcp_server.py": {"claude-code", "codex"},
    "memplex/adapters/openclaw_plugin.py": {"openclaw"},
    "memplex/adapters/hermes_memory_provider.py": {"hermes"},
}


def _proofs(root: Path) -> tuple[HostLifecycleProof, ...]:
    root.mkdir(exist_ok=True)
    (root / "isolated-prestate.txt").write_text("isolated host proof", encoding="utf-8")
    executable = Path(sys.executable).resolve()
    executable_digest = sha256(executable.read_bytes()).hexdigest()
    root_digest = sha256((root / "isolated-prestate.txt").read_bytes()).hexdigest()
    contracts = current_host_contract_digests()

    def proof(host: str) -> HostLifecycleProof:
        results = required_host_node_results(host)
        return HostLifecycleProof(
            host=host,
            cli_path=str(executable),
            cli_sha256=executable_digest,
            cli_version=sys.version,
            contract_sha256=contracts[host],
            isolated_root_sha256=root_digest,
            required_node_results=results,
            required_node_manifest_sha256=required_node_manifest_sha256(results),
            junit_sha256="2" * 64,
        )

    return tuple(proof(host) for host in ("claude-code", "codex", "hermes", "openclaw"))


def _binding() -> HostLifecycleBinding:
    return HostLifecycleBinding(
        deployment_id="g012-host-lifecycle-test",
        source_sha256="a" * 64,
        artifact_sha256="b" * 64,
        target_identity_sha256="c" * 64,
        expected_key_id="g008-local",
    )


def test_host_lifecycle_proof_rejects_uncontracted_required_nodes(tmp_path: Path) -> None:
    """Changing the verifier's required-node contract must invalidate the proof."""
    root = tmp_path / "forged"
    root.mkdir()
    executable = Path(sys.executable).resolve()
    forged_results = (("forged/codex::anything", "passed"),)

    with pytest.raises(HostLifecycleIntegrityError, match="integrity"):
        HostLifecycleProof(
            host="codex",
            cli_path=str(executable),
            cli_sha256=sha256(executable.read_bytes()).hexdigest(),
            cli_version=sys.version,
            contract_sha256=current_host_contract_digests()["codex"],
            isolated_root_sha256="1" * 64,
            required_node_results=forged_results,
            required_node_manifest_sha256=required_node_manifest_sha256(forged_results),
            junit_sha256="2" * 64,
        )


def test_host_lifecycle_evidence_creation_validates_the_producer_cli_digest(
    tmp_path: Path,
) -> None:
    """Changing the executable after host verification must block evidence issuance."""
    key = b"h" * 32
    proofs = list(_proofs(tmp_path / "isolated"))
    proofs[0] = HostLifecycleProof(
        host=proofs[0].host,
        cli_path=proofs[0].cli_path,
        cli_sha256="0" * 64,
        cli_version=proofs[0].cli_version,
        contract_sha256=proofs[0].contract_sha256,
        isolated_root_sha256=proofs[0].isolated_root_sha256,
        required_node_results=proofs[0].required_node_results,
        required_node_manifest_sha256=proofs[0].required_node_manifest_sha256,
        junit_sha256=proofs[0].junit_sha256,
    )

    with pytest.raises(HostLifecycleIntegrityError, match="integrity"):
        HostLifecycleEvidence.create(
            memplex_version=version("memplex"),
            host_proofs=tuple(proofs),
            binding=_binding(),
            key_id="g008-local",
            signing_key=key,
        )


def test_contract_files_match_coverage_map() -> None:
    """_contract_files() is the source of truth for the G008 contract
    cluster; every hashed file must be pinned by _CONTRACT_COVERAGE.
    Adding/removing a contract file without updating the map fails here."""
    actual = {
        path.relative_to(PROJECT_ROOT).as_posix()
        for paths in _contract_files(PROJECT_ROOT).values()
        for path in paths
    }
    assert actual == set(_CONTRACT_COVERAGE)


def test_current_host_contract_digests_cover_each_real_launcher_adapter_and_shared_status(
    tmp_path: Path,
) -> None:
    """Any byte drift in a real host boundary must invalidate that host's proof."""
    isolated_project = tmp_path / "project"
    shutil.copytree(PROJECT_ROOT / "memplex", isolated_project / "memplex")
    baseline = current_host_contract_digests(project_root=isolated_project)
    coverage = _CONTRACT_COVERAGE

    for relative, affected_hosts in coverage.items():
        target = isolated_project / relative
        original = target.read_bytes()
        target.write_bytes(original + b"\n# g008 contract mutation\n")
        changed = current_host_contract_digests(project_root=isolated_project)
        assert {host for host in baseline if changed[host] != baseline[host]} == affected_hosts
        target.write_bytes(original)


def test_host_lifecycle_evidence_round_trip_and_tamper_rejection(tmp_path):
    key = b"h" * 32
    evidence = HostLifecycleEvidence.create(
        memplex_version=version("memplex"),
        host_proofs=_proofs(tmp_path / "isolated"),
        binding=_binding(),
        key_id="g008-local",
        signing_key=key,
    )
    path = tmp_path / "hosts.json"
    write_host_lifecycle_evidence(path, evidence)
    loaded = read_host_lifecycle_evidence(path)
    loaded.verify(key, expected_version=version("memplex"), expected_binding=_binding())
    assert [item.host for item in loaded.hosts] == [
        "claude-code",
        "codex",
        "hermes",
        "openclaw",
    ]

    payload = json.loads(path.read_text())
    payload["hosts"][0]["cli_version"] = "stale"
    path.write_text(json.dumps(payload))
    with pytest.raises(HostLifecycleIntegrityError):
        read_host_lifecycle_evidence(path).verify(
            key, expected_version=version("memplex"), expected_binding=_binding()
        )


def test_host_lifecycle_evidence_rejects_missing_host_and_symlink(tmp_path):
    key = b"h" * 32
    with pytest.raises(HostLifecycleIntegrityError, match="integrity"):
        HostLifecycleEvidence.create(
            memplex_version=version("memplex"),
            host_proofs=_proofs(tmp_path / "missing")[:-1],
            binding=_binding(),
            key_id="g008-local",
            signing_key=key,
        )

    evidence = HostLifecycleEvidence.create(
        memplex_version=version("memplex"),
        host_proofs=_proofs(tmp_path / "isolated"),
        binding=_binding(),
        key_id="g008-local",
        signing_key=key,
    )
    target = tmp_path / "target.json"
    target.write_bytes(evidence.canonical_bytes())
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(HostLifecycleIntegrityError):
        read_host_lifecycle_evidence(link)


def test_host_lifecycle_evidence_writer_rejects_symlink_ancestor(tmp_path):
    key = b"h" * 32
    evidence = HostLifecycleEvidence.create(
        memplex_version=version("memplex"),
        host_proofs=_proofs(tmp_path / "isolated"),
        binding=_binding(),
        key_id="g008-local",
        signing_key=key,
    )
    real = tmp_path / "real"
    real.mkdir()
    linked = tmp_path / "linked"
    linked.symlink_to(real, target_is_directory=True)

    with pytest.raises(HostLifecycleIntegrityError):
        write_host_lifecycle_evidence(linked / "hosts.json", evidence)

    assert not (real / "hosts.json").exists()


def test_host_lifecycle_evidence_rejects_expired_and_future_reports(tmp_path):
    key = b"h" * 32
    now = datetime.now(timezone.utc)
    for generated_at in (
        now - timedelta(hours=25),
        now + timedelta(minutes=6),
    ):
        evidence = HostLifecycleEvidence.create(
            memplex_version=version("memplex"),
            host_proofs=_proofs(tmp_path / "isolated"),
            binding=_binding(),
            key_id="g008-local",
            signing_key=key,
            generated_at=generated_at,
        )
        path = tmp_path / f"hosts-{generated_at.timestamp()}.json"
        write_host_lifecycle_evidence(path, evidence)

        with pytest.raises(HostLifecycleIntegrityError):
            read_host_lifecycle_evidence(path).verify(
                key,
                expected_version=version("memplex"),
                expected_binding=_binding(),
                now=now,
            )


def test_host_lifecycle_evidence_rejects_placeholder_cli_and_stale_source(tmp_path):
    key = b"h" * 32
    proofs = list(_proofs(tmp_path / "isolated"))
    proofs[0] = HostLifecycleProof(
        host=proofs[0].host,
        cli_path="/not-installed/claude",
        cli_sha256=proofs[0].cli_sha256,
        cli_version="not-installed",
        contract_sha256=proofs[0].contract_sha256,
        isolated_root_sha256=proofs[0].isolated_root_sha256,
        required_node_results=proofs[0].required_node_results,
        required_node_manifest_sha256=proofs[0].required_node_manifest_sha256,
        junit_sha256=proofs[0].junit_sha256,
    )
    with pytest.raises(HostLifecycleIntegrityError):
        HostLifecycleEvidence.create(
            memplex_version=version("memplex"),
            host_proofs=tuple(proofs),
            binding=_binding(),
            key_id="g008-local",
            signing_key=key,
        )

    evidence = HostLifecycleEvidence.create(
        memplex_version=version("memplex"),
        host_proofs=_proofs(tmp_path / "current"),
        binding=_binding(),
        key_id="g008-local",
        signing_key=key,
    )
    payload = evidence.to_dict()
    payload["source_sha256"] = "3" * 64
    unsigned = dict(payload)
    unsigned.pop("signature")
    import hmac

    payload["signature"] = hmac.new(
        key,
        json.dumps(unsigned, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(),
        sha256,
    ).hexdigest()
    with pytest.raises(HostLifecycleIntegrityError):
        HostLifecycleEvidence.from_dict(payload).verify(
            key, expected_version=version("memplex"), expected_binding=_binding()
        )

    uppercase = evidence.to_dict()
    uppercase["source_sha256"] = "A" * 64
    with pytest.raises(HostLifecycleIntegrityError):
        HostLifecycleEvidence.from_dict(uppercase)


def test_host_lifecycle_evidence_rejects_duplicate_json_keys_and_binding_mismatch(tmp_path):
    key = b"h" * 32
    evidence = HostLifecycleEvidence.create(
        memplex_version=version("memplex"),
        host_proofs=_proofs(tmp_path / "isolated"),
        binding=_binding(),
        key_id="g008-local",
        signing_key=key,
    )
    path = tmp_path / "hosts.json"
    write_host_lifecycle_evidence(path, evidence)
    raw = path.read_text(encoding="utf-8")
    path.write_text(raw.replace('"key_id":"g008-local"', '"key_id":"g008-local","key_id":"other"'))
    with pytest.raises(HostLifecycleIntegrityError):
        read_host_lifecycle_evidence(path)

    for changed_binding in (
        HostLifecycleBinding(
            deployment_id="other-deployment",
            source_sha256="a" * 64,
            artifact_sha256="b" * 64,
            target_identity_sha256="c" * 64,
            expected_key_id="g008-local",
        ),
        HostLifecycleBinding(
            deployment_id="g012-host-lifecycle-test",
            source_sha256="a" * 64,
            artifact_sha256="b" * 64,
            target_identity_sha256="c" * 64,
            expected_key_id="previous-g008-key",
        ),
    ):
        with pytest.raises(HostLifecycleIntegrityError):
            evidence.verify(
                key, expected_version=version("memplex"), expected_binding=changed_binding
            )
