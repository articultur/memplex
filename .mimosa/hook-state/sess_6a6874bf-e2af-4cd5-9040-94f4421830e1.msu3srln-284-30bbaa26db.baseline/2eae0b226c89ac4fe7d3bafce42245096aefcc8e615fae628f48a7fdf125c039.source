"""SBOM, checksum, and local signed evidence tests."""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tomllib
from hashlib import sha256
from pathlib import Path

import pytest

from memplex.release import (
    ReleaseEvidence,
    ReleaseIntegrityError,
    ReleaseManifest,
    build_checksum_document,
    build_cyclonedx_sbom,
    build_release_manifest,
    verify_cyclonedx_sbom,
    verify_release_bundle,
    verify_release_evidence,
    verify_release_readiness_evidence,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BUILD_SCRIPT = PROJECT_ROOT / "scripts/build_release_artifacts.py"
VERIFY_SCRIPT = PROJECT_ROOT / "scripts/verify_g007_supply_chain.py"
SIGNING_KEY = b"r" * 32


@pytest.fixture(scope="module")
def release_bundle(tmp_path_factory: pytest.TempPathFactory) -> Path:
    output = tmp_path_factory.mktemp("release-bundle") / "dist"
    result = subprocess.run(
        [
            sys.executable,
            str(BUILD_SCRIPT),
            "--source",
            str(PROJECT_ROOT),
            "--output",
            str(output),
            "--tag",
            "v3.3.0",
            "--source-date-epoch",
            "1704067200",
            "--allow-dirty",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    return output


def _evidence(bundle: Path) -> ReleaseEvidence:
    manifest_bytes = (bundle / "release-manifest.json").read_bytes()
    manifest = ReleaseManifest.from_dict(json.loads(manifest_bytes))
    return ReleaseEvidence.create(
        manifest=manifest,
        manifest_sha256=sha256(manifest_bytes).hexdigest(),
        sbom_sha256=sha256((bundle / "release-sbom.cdx.json").read_bytes()).hexdigest(),
        checksums_sha256=sha256((bundle / "release-checksums.json").read_bytes()).hexdigest(),
        key_id="local-release-gate-v1",
        signing_key=SIGNING_KEY,
    )


def test_cyclonedx_sbom_is_exact_sorted_and_covers_direct_and_locked_dependencies() -> None:
    payload = build_cyclonedx_sbom(PROJECT_ROOT)
    sbom = json.loads(payload)
    locked = tomllib.loads((PROJECT_ROOT / "uv.lock").read_text())
    locked_identities = {
        (package["name"].replace("_", "-").lower(), package["version"])
        for package in locked["package"]
        if package["name"] != "memplex"
    }
    component_identities = {(item["name"], item["version"]) for item in sbom["components"]}

    assert sbom["bomFormat"] == "CycloneDX"
    assert sbom["specVersion"] == "1.6"
    assert component_identities == locked_identities
    direct = {
        item["name"]
        for item in sbom["components"]
        if item["properties"][0]["value"] == "true"
    }
    assert {"numpy", "pyyaml", "requests"}.issubset(direct)
    assert sbom["components"] == sorted(
        sbom["components"], key=lambda item: (item["name"], item["version"])
    )
    verify_cyclonedx_sbom(PROJECT_ROOT, payload)


def test_sbom_rejects_duplicate_or_tampered_component() -> None:
    sbom = json.loads(build_cyclonedx_sbom(PROJECT_ROOT))
    sbom["components"].append(sbom["components"][0])
    tampered = json.dumps(sbom, sort_keys=True, separators=(",", ":")).encode()

    with pytest.raises(ReleaseIntegrityError):
        verify_cyclonedx_sbom(PROJECT_ROOT, tampered)


def test_release_bundle_verifies_exact_artifacts_sbom_and_checksums(release_bundle: Path) -> None:
    manifest = verify_release_bundle(PROJECT_ROOT, release_bundle)
    assert manifest.version == "3.3.0"
    assert {item.name for item in manifest.artifacts} == {
        "memplex-3.3.0-py3-none-any.whl",
        "memplex-3.3.0.tar.gz",
        "memplex-3.3.0.tgz",
        "release-sbom.cdx.json",
        "release-checksums.json",
    }


@pytest.mark.parametrize("mutation", ["tamper", "unknown", "missing"])
def test_release_bundle_fails_closed_on_artifact_drift(
    tmp_path: Path, release_bundle: Path, mutation: str
) -> None:
    candidate = tmp_path / "candidate"
    shutil.copytree(release_bundle, candidate)
    if mutation == "tamper":
        (candidate / "memplex-3.3.0.tgz").write_bytes(b"tampered")
    elif mutation == "unknown":
        (candidate / "unknown.bin").write_bytes(b"unknown")
    else:
        (candidate / "release-sbom.cdx.json").unlink()

    with pytest.raises(ReleaseIntegrityError):
        verify_release_bundle(PROJECT_ROOT, candidate)


def test_signed_release_evidence_binds_current_bundle(release_bundle: Path) -> None:
    evidence = _evidence(release_bundle)
    verified = verify_release_evidence(
        PROJECT_ROOT,
        release_bundle,
        evidence.canonical_bytes(),
        signing_key=SIGNING_KEY,
    )
    assert verified.status == "passed"
    assert verified.key_id == "local-release-gate-v1"


def test_installed_runtime_readiness_verifies_exact_signed_bundle(release_bundle: Path) -> None:
    evidence = _evidence(release_bundle)
    verified = verify_release_readiness_evidence(
        release_bundle,
        evidence.canonical_bytes(),
        signing_key=SIGNING_KEY,
        expected_version="3.3.0",
    )
    assert verified.status == "passed"


def test_installed_runtime_readiness_rejects_version_or_bundle_drift(
    tmp_path: Path, release_bundle: Path
) -> None:
    evidence = _evidence(release_bundle)
    with pytest.raises(ReleaseIntegrityError):
        verify_release_readiness_evidence(
            release_bundle,
            evidence.canonical_bytes(),
            signing_key=SIGNING_KEY,
            expected_version="3.3.1",
        )
    candidate = tmp_path / "candidate"
    shutil.copytree(release_bundle, candidate)
    (candidate / "memplex-3.3.0.tgz").write_bytes(b"tampered")
    with pytest.raises(ReleaseIntegrityError):
        verify_release_readiness_evidence(
            candidate,
            evidence.canonical_bytes(),
            signing_key=SIGNING_KEY,
            expected_version="3.3.0",
        )


def test_readiness_rejects_self_consistent_archive_with_private_member(
    tmp_path: Path, release_bundle: Path
) -> None:
    candidate = tmp_path / "candidate"
    shutil.copytree(release_bundle, candidate)
    npm_path = candidate / "memplex-3.3.0.tgz"
    with tarfile.open(npm_path, "w:gz") as archive:
        for name, payload in (
            ("package/bin/memplex.js", b"node"),
            ("package/install-agent.sh", b"shell"),
            ("package/package.json", b"{}"),
            ("package/.codex/secret.txt", b"private"),
        ):
            member = tarfile.TarInfo(name)
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    checksums = candidate / "release-checksums.json"
    checksums.write_bytes(
        build_checksum_document(
            (
                candidate / "memplex-3.3.0-py3-none-any.whl",
                candidate / "memplex-3.3.0.tar.gz",
                npm_path,
                candidate / "release-sbom.cdx.json",
            )
        )
        + b"\n"
    )
    manifest = build_release_manifest(
        PROJECT_ROOT,
        tag="v3.3.0",
        artifacts=(
            candidate / "memplex-3.3.0-py3-none-any.whl",
            candidate / "memplex-3.3.0.tar.gz",
            npm_path,
            candidate / "release-sbom.cdx.json",
            checksums,
        ),
    )
    manifest_bytes = manifest.canonical_bytes() + b"\n"
    (candidate / "release-manifest.json").write_bytes(manifest_bytes)
    evidence = ReleaseEvidence.create(
        manifest=manifest,
        manifest_sha256=sha256(manifest_bytes).hexdigest(),
        sbom_sha256=sha256((candidate / "release-sbom.cdx.json").read_bytes()).hexdigest(),
        checksums_sha256=sha256(checksums.read_bytes()).hexdigest(),
        key_id="local-release-gate-v1",
        signing_key=SIGNING_KEY,
    )
    with pytest.raises(ReleaseIntegrityError):
        verify_release_readiness_evidence(
            candidate,
            evidence.canonical_bytes(),
            signing_key=SIGNING_KEY,
            expected_version="3.3.0",
        )


def test_unsigned_or_tampered_release_evidence_never_verifies(release_bundle: Path) -> None:
    evidence = _evidence(release_bundle).to_dict()
    evidence["signature"] = "0" * 64
    with pytest.raises(ReleaseIntegrityError) as exc_info:
        verify_release_evidence(
            PROJECT_ROOT,
            release_bundle,
            json.dumps(evidence).encode(),
            signing_key=SIGNING_KEY,
        )
    assert "local-release-gate-v1" not in str(exc_info.value)


def test_supply_chain_verifier_emits_redacted_signed_evidence(
    tmp_path: Path, release_bundle: Path
) -> None:
    evidence_path = tmp_path / "evidence.json"
    secret = bytes(range(32)).hex()
    env = {**os.environ, "MEMPLEX_RELEASE_EVIDENCE_KEY": secret}
    result = subprocess.run(
        [
            sys.executable,
            str(VERIFY_SCRIPT),
            "--bundle",
            str(release_bundle),
            "--evidence-output",
            str(evidence_path),
        ],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout) == {"schema_version": 1, "status": "passed"}
    assert secret not in result.stdout + result.stderr
    evidence = verify_release_evidence(
        PROJECT_ROOT,
        release_bundle,
        evidence_path.read_bytes(),
        signing_key=bytes.fromhex(secret),
    )
    assert evidence.status == "passed"


def test_evidence_writer_rejects_symlinked_ancestor(tmp_path: Path, release_bundle: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    redirected = tmp_path / "redirected"
    redirected.symlink_to(real, target_is_directory=True)
    secret = bytes(range(32)).hex()
    result = subprocess.run(
        [
            sys.executable,
            str(VERIFY_SCRIPT),
            "--bundle",
            str(release_bundle),
            "--evidence-output",
            str(redirected / "evidence.json"),
        ],
        cwd=PROJECT_ROOT,
        env={**os.environ, "MEMPLEX_RELEASE_EVIDENCE_KEY": secret},
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert result.returncode == 2
    assert not (real / "evidence.json").exists()
