"""Package-data contract: shipped assets must survive into the built wheel."""

from __future__ import annotations

import fnmatch
import tomllib
import zipfile
from pathlib import Path

from tests.test_storage_migrations import _build_wheel

ROOT = Path(__file__).resolve().parent.parent

# package-data key -> source directory whose data files must reach the wheel.
PACKAGED_DATA = {
    "memplex.storage.migrations": ROOT / "memplex" / "storage" / "migrations",
    "memplex.operations_assets": ROOT / "memplex" / "operations_assets",
}


def _data_files(package: str, directory: Path) -> set[str]:
    patterns = _pyproject_package_data()[package]
    matched = {
        path.name
        for path in directory.iterdir()
        if path.is_file() and any(fnmatch.fnmatch(path.name, pattern) for pattern in patterns)
    }
    # Every pattern must match at least one real file, and vice versa below.
    assert matched, f"package-data patterns {patterns} match nothing in {directory}"
    return matched


def _pyproject_package_data() -> dict:
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return metadata["tool"]["setuptools"]["package-data"]


def test_package_data_patterns_cover_every_shipped_asset() -> None:
    """pyproject package-data must match each on-disk asset (no silent drift)."""
    package_data = _pyproject_package_data()
    for package, directory in PACKAGED_DATA.items():
        patterns = package_data[package]
        data_files = {
            path.name
            for path in directory.iterdir()
            if path.is_file() and path.suffix not in {".py", ".pyc"}
        }
        covered = {
            name
            for name in data_files
            if any(fnmatch.fnmatch(name, pattern) for pattern in patterns)
        }
        assert covered == data_files


def test_built_wheel_contains_migrations_and_operations_assets(tmp_path: Path) -> None:
    """The wheel must carry every migration SQL and the operations console assets."""
    wheel = _build_wheel(tmp_path)
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())

    expected = {
        f"{package.replace('.', '/')}/{name}"
        for package, directory in PACKAGED_DATA.items()
        for name in _data_files(package, directory)
    }
    assert "memplex/storage/migrations/0006_background_tasks.sql" in expected
    assert "memplex/operations_assets/admin.html" in expected
    assert expected <= names
