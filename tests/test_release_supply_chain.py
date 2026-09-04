"""Fail-closed release contract tests."""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from memplex.release import (
    ReleaseArtifact,
    ReleaseIntegrityError,
    ReleaseManifest,
    build_release_manifest,
    validate_release_member_names,
    validate_release_version_set,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def test_release_manifest_is_frozen_canonical_and_digest_stable(tmp_path: Path) -> None:
    artifact = tmp_path / "memplex-3.3.2-py3-none-any.whl"
    artifact.write_bytes(b"wheel-bytes")

    manifest = build_release_manifest(
        PROJECT_ROOT,
        tag="v3.3.2",
        artifacts=(artifact,),
    )

    assert manifest.schema_version == 1
    assert manifest.version == "3.3.2"
    assert manifest.tag == "v3.3.2"
    assert manifest.artifacts == (
        ReleaseArtifact(
            name="memplex-3.3.2-py3-none-any.whl",
            sha256="9ceb18f15662bb87e54af2f5953c0484d2ef76f5444d87913360b9ef87d7296d",
            size=11,
        ),
    )
    assert manifest.canonical_bytes() == (
        b'{"artifacts":[{"name":"memplex-3.3.2-py3-none-any.whl",'
        b'"sha256":"9ceb18f15662bb87e54af2f5953c0484d2ef76f5444d87913360b9ef87d7296d",'
        b'"size":11}],"schema_version":1,"tag":"v3.3.2","version":"3.3.2"}'
    )
    assert len(manifest.digest()) == 64
    with pytest.raises(FrozenInstanceError):
        manifest.version = "9.9.9"  # type: ignore[misc]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"schema_version": 2, "version": "3.3.2", "tag": "v3.3.2", "artifacts": []},
        {"schema_version": True, "version": "3.3.2", "tag": "v3.3.2", "artifacts": []},
        {"schema_version": 1, "version": 3, "tag": "v3.3.2", "artifacts": []},
        {"schema_version": 1, "version": "3.3.2", "tag": "v3.3.2", "artifacts": [], "future": 1},
        {
            "schema_version": 1,
            "version": "3.3.2",
            "tag": "v3.3.2",
            "artifacts": [{"name": "a.whl", "sha256": "0" * 64, "size": 1, "future": 1}],
        },
    ],
)
def test_release_manifest_rejects_weak_future_or_incomplete_schema(payload: dict) -> None:
    with pytest.raises(ReleaseIntegrityError) as exc_info:
        ReleaseManifest.from_dict(payload)
    assert str(exc_info.value) == "release manifest integrity check failed"


@pytest.mark.parametrize("tag", ["3.3.2", "v3.3", "v03.3.2", "v3.3.2-rc1", True, 3])
def test_release_version_set_rejects_non_exact_tag(tag: object) -> None:
    with pytest.raises(ReleaseIntegrityError):
        validate_release_version_set(PROJECT_ROOT, tag=tag)  # type: ignore[arg-type]


def test_release_version_set_rejects_any_metadata_drift(tmp_path: Path) -> None:
    paths = {
        "pyproject.toml": '[project]\nname="memplex"\nversion="3.3.2"\n',
        "npm/memplex/package.json": '{"name":"memplex","version":"3.3.3"}',
        "npm/archive/agent-installer/package.json": '{"version":"0.2.0","dependencies":{"memplex":"3.3.2"}}',
        "npm/archive/hermes-installer/package.json": '{"version":"0.2.0","dependencies":{"memplex":"3.3.2"}}',
        "marketplace.json": '{"plugins":[{"version":"3.3.2"}]}',
        "plugin/.claude-plugin/plugin.json": '{"version":"3.3.2"}',
        "plugin/.codex-plugin/plugin.json": '{"version":"3.3.2"}',
        "memplex/_plugin/.claude-plugin/plugin.json": '{"version":"3.3.2"}',
        "memplex/_plugin/.codex-plugin/plugin.json": '{"version":"3.3.2"}',
    }
    for name, content in paths.items():
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    with pytest.raises(ReleaseIntegrityError):
        validate_release_version_set(tmp_path, tag="v3.3.2")


@pytest.mark.parametrize(
    "name",
    [
        "/Users/example/project/secret.txt",
        "../outside",
        ".superpowers/sdd/review.md",
        ".codex/config.toml",
        ".claude/settings.json",
        ".omx/state/goal.json",
        ".env",
        "secrets/token.txt",
        "tmp/memory.json",
        "postgresql:/app:password@host/db/tombstones.json",
        "memplex/__pycache__/release.cpython-313.pyc",
    ],
)
def test_release_member_allowlist_rejects_private_or_generated_assets(name: str) -> None:
    with pytest.raises(ReleaseIntegrityError) as exc_info:
        validate_release_member_names((name,))
    assert exc_info.value.code == "release_private_asset"
    assert name not in str(exc_info.value)


def test_release_member_allowlist_accepts_public_runtime_assets() -> None:
    validate_release_member_names(
        (
            "memplex/__init__.py",
            "memplex/storage/migrations/0006_background_tasks.sql",
            "memplex/_plugin/.codex-plugin/plugin.json",
            "npm/memplex/install-agent.sh",
            "docs/production-readiness.md",
        )
    )


def test_release_manifest_json_round_trip_is_exact(tmp_path: Path) -> None:
    artifact = tmp_path / "memplex-3.3.2.tgz"
    artifact.write_bytes(b"npm")
    manifest = build_release_manifest(PROJECT_ROOT, tag="v3.3.2", artifacts=(artifact,))

    decoded = json.loads(manifest.canonical_bytes())
    assert ReleaseManifest.from_dict(decoded) == manifest
