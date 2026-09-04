"""End-to-end deterministic release artifact tests."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

from memplex.release import ReleaseIntegrityError

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BUILD_SCRIPT = PROJECT_ROOT / "scripts" / "build_release_artifacts.py"


def _build_module():
    spec = importlib.util.spec_from_file_location("memplex_release_builder_test", BUILD_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _build(output: Path, *, locale: str, umask: str) -> dict[str, str]:
    output.parent.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    env.update({"LC_ALL": locale, "LANG": locale, "MEMPLEX_TEST_UMASK": umask})
    result = subprocess.run(
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
            "--umask",
            umask,
        ],
        cwd=output.parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=90,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(output.iterdir())
        if path.is_file()
    }


def test_release_artifacts_are_reproducible_across_paths_locale_and_umask(tmp_path: Path) -> None:
    first = _build(tmp_path / "first-root" / "dist", locale="C", umask="022")
    second = _build(tmp_path / "second-much-longer-root" / "dist", locale="C.UTF-8", umask="077")

    assert first == second
    assert set(first) == {
        "memplex-3.3.2-py3-none-any.whl",
        "memplex-3.3.2.tar.gz",
        "memplex-3.3.2.tgz",
        "release-checksums.json",
        "release-manifest.json",
        "release-sbom.cdx.json",
    }


def test_release_archives_are_sorted_normalized_and_private_asset_free(tmp_path: Path) -> None:
    output = tmp_path / "dist"
    _build(output, locale="C", umask="022")

    wheel = output / "memplex-3.3.2-py3-none-any.whl"
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert names == sorted(names)
        assert all(info.date_time == (2024, 1, 1, 0, 0, 0) for info in archive.infolist())
        assert all(not name.startswith("/") for name in names)
        assert not any("tests/" in name or ".superpowers" in name for name in names)
        assert not any(b"/Users/nonon/" in archive.read(name) for name in names if not name.endswith("/"))

    for name in ("memplex-3.3.2.tar.gz", "memplex-3.3.2.tgz"):
        with tarfile.open(output / name, "r:gz") as archive:
            members = archive.getmembers()
            assert [member.name for member in members] == sorted(member.name for member in members)
            assert all(member.mtime == 1704067200 for member in members)
            assert all(member.uid == 0 and member.gid == 0 for member in members)
            assert not any("tests/" in member.name or ".superpowers" in member.name for member in members)

    manifest = json.loads((output / "release-manifest.json").read_text())
    assert manifest["schema_version"] == 1
    assert manifest["tag"] == "v3.3.2"
    assert [item["name"] for item in manifest["artifacts"]] == [
        "memplex-3.3.2-py3-none-any.whl",
        "memplex-3.3.2.tar.gz",
        "memplex-3.3.2.tgz",
        "release-checksums.json",
        "release-sbom.cdx.json",
    ]


@pytest.mark.parametrize(
    "relative_path",
    ("memplex/external.py", "npm/memplex/bin/external.js"),
)
def test_release_builder_rejects_source_tree_symlinks(
    tmp_path: Path, relative_path: str
) -> None:
    source = tmp_path / "source"
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name, encoding="utf-8")
    (source / "memplex").mkdir()
    (source / "memplex/__init__.py").write_text("", encoding="utf-8")
    (source / "npm/memplex/bin").mkdir(parents=True)
    (source / "npm/memplex/package.json").write_text("{}", encoding="utf-8")
    (source / "npm/memplex/install-agent.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (source / "npm/memplex/bin/memplex.js").write_text("", encoding="utf-8")
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("TOP-SECRET-123", encoding="utf-8")
    (source / relative_path).symlink_to(outside)

    with pytest.raises(ReleaseIntegrityError) as exc_info:
        _build_module()._copy_release_sources(source, tmp_path / "workspace")

    assert exc_info.value.code == "release_source_invalid"
    assert "outside-secret" not in str(exc_info.value)
