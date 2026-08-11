"""Tests for release metadata: packaging extras, licenses, and marketplace descriptors."""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = PROJECT_ROOT / "pyproject.toml"
NPM_MEMPLEX_PACKAGE = PROJECT_ROOT / "npm" / "memplex" / "package.json"
ROOT_MARKETPLACE = PROJECT_ROOT / "marketplace.json"
CHANGELOG = PROJECT_ROOT / "CHANGELOG.md"


def _pyproject() -> dict:
    return tomllib.loads(PYPROJECT.read_text())


def _extras() -> dict:
    return _pyproject()["project"]["optional-dependencies"]


def test_project_metadata_declares_readme_and_mit_license():
    project = _pyproject()["project"]
    assert project["readme"] == "README.md"
    assert project["license"]["text"] == "MIT"


def test_postgres_extra_uses_psycopg2_not_asyncpg():
    postgres = _extras()["postgres"]
    assert any(dep.startswith("psycopg2-binary") for dep in postgres)
    assert not any("asyncpg" in dep for dep in postgres)


def test_neo4j_extra_removed():
    assert "neo4j" not in _extras()


def test_all_extra_includes_postgres():
    all_deps = _extras()["all"]
    assert any("postgres" in dep for dep in all_deps)


def test_ruff_ignore_lists_have_no_duplicates():
    ruff = _pyproject()["tool"]["ruff"]
    ignore = ruff["lint"]["ignore"]
    assert len(ignore) == len(set(ignore))
    per_file = ruff["lint"]["per-file-ignores"]
    assert not any(path.startswith("memplex/benchmarks/") for path in per_file)


def test_npm_memplex_license_matches_repo_license():
    package = json.loads(NPM_MEMPLEX_PACKAGE.read_text())
    assert package["license"] == "MIT"


def test_marketplace_descriptors_divide_publishing_and_local_install():
    version = _pyproject()["project"]["version"]

    root = json.loads(ROOT_MARKETPLACE.read_text())
    assert root["plugins"][0]["source"]["source"] == "git"
    assert root["plugins"][0]["version"] == version

    from memplex.adapters._shared import marketplace_json

    local = json.loads(marketplace_json())
    assert local["plugins"][0]["source"]["source"] == "local"
    # Shared fields stay aligned between the two descriptors.
    assert local["name"] == root["name"]
    assert local["plugins"][0]["name"] == root["plugins"][0]["name"]
    assert local["plugins"][0]["policy"] == root["plugins"][0]["policy"]
    assert local["plugins"][0]["category"] == root["plugins"][0]["category"]


def test_changelog_has_dated_section_for_current_version():
    version = _pyproject()["project"]["version"]
    changelog = CHANGELOG.read_text()
    assert f"## [{version}] - 20" in changelog
    assert "memplex backup` / `memplex restore" not in changelog
