"""Static security contract for CI and release workflows."""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CI = ROOT / ".github/workflows/ci.yml"
RELEASE = ROOT / ".github/workflows/release.yml"
OLD_NPM_RELEASE = ROOT / ".github/workflows/npm-release.yml"
DEPENDABOT = ROOT / ".github/dependabot.yml"
RELEASE_DOCS = ROOT / "docs/release-automation.md"
FULL_SHA_USE = re.compile(r"^\s*-?\s*uses:\s*[^@\s]+@([0-9a-f]{40})\s*(?:#.*)?$", re.MULTILINE)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _workflow(path: Path) -> dict:
    return yaml.load(_text(path), Loader=yaml.BaseLoader)


def test_all_actions_are_pinned_to_full_commit_shas() -> None:
    for path in (CI, RELEASE):
        text = _text(path)
        uses_lines = [line for line in text.splitlines() if "uses:" in line]
        assert uses_lines
        assert len(FULL_SHA_USE.findall(text)) == len(uses_lines), (path, uses_lines)
        assert "@v" not in text
        assert "@main" not in text


def test_release_workflow_uses_minimum_permissions_and_oidc_publishers() -> None:
    workflow = _workflow(RELEASE)
    assert workflow["permissions"] == {"contents": "read"}
    jobs = workflow["jobs"]
    assert jobs["build"]["permissions"] == {"contents": "read"}
    assert jobs["attest"]["permissions"] == {
        "contents": "read",
        "id-token": "write",
        "attestations": "write",
        "artifact-metadata": "write",
    }
    assert jobs["publish-pypi"]["permissions"] == {"contents": "read", "id-token": "write"}
    assert jobs["publish-npm"]["permissions"] == {"contents": "read", "id-token": "write"}
    assert jobs["publish-pypi"]["environment"] == "pypi"
    assert jobs["publish-npm"]["environment"] == "npm"
    assert "publish-pypi" in jobs["publish-npm"]["needs"]


def test_release_builds_once_offline_and_publish_jobs_only_consume_artifacts() -> None:
    workflow = _workflow(RELEASE)
    jobs = workflow["jobs"]
    assert jobs["build"]["needs"] == "prepare-build-tools"
    build_text = json_steps(jobs["build"])
    assert "scripts/build_release_artifacts.py" in build_text
    assert "MEMPLEX_RELEASE_NO_BUILD_ISOLATION" in _text(RELEASE)
    assert "cmp " in build_text
    assert "npm pack" not in json_steps(jobs["publish-npm"])
    assert "build_release_artifacts" not in json_steps(jobs["publish-pypi"])
    assert "build_release_artifacts" not in json_steps(jobs["publish-npm"])
    assert "actions/cache" not in _text(RELEASE)
    assert "self-hosted" not in _text(RELEASE)
    assert "--require-hashes" in _text(RELEASE)
    assert "release/build-tools.txt" in _text(RELEASE)


def json_steps(job: dict) -> str:
    return "\n".join(str(step.get("run", "")) + str(step.get("uses", "")) for step in job["steps"])


def test_release_has_no_long_lived_registry_secret_or_mutable_skip() -> None:
    text = _text(RELEASE)
    forbidden = (
        "NPM_TOKEN",
        "PYPI_TOKEN",
        "NODE_AUTH_TOKEN",
        "secrets.",
        "skip-existing",
        "already exists on npm; skipping",
        "pull_request_target",
    )
    assert not any(value in text for value in forbidden)
    assert "dist.integrity" in text
    assert "digest conflict" in text
    assert "pypa/gh-action-pypi-publish" in text
    assert "npm publish" in text
    assert "npm view" in text
    assert "|| true" not in text
    assert "E404" in text
    assert "npm registry preflight failed" in text


def test_release_attests_artifacts_and_sbom() -> None:
    text = _text(RELEASE)
    assert text.count("actions/attest@59d89421af93a897026c735860bf21b6eb4f7b26") == 2
    assert "release/release-sbom.cdx.json" in text
    assert "release/release-checksums.json" in text
    assert "release/release-manifest.json" in text


def test_ci_has_dependency_review_lock_drift_and_audit() -> None:
    text = _text(CI)
    assert "actions/dependency-review-action" in text
    assert "uv lock --check" in text
    assert "pip-audit" in text
    assert "pull_request_target" not in text
    workflow = _workflow(CI)
    assert workflow["permissions"] == {"contents": "read"}


def test_ci_runs_release_install_matrix_on_supported_hosts_and_runtimes() -> None:
    workflow = _workflow(CI)
    job = workflow["jobs"]["release-install-matrix"]
    matrix = job["strategy"]["matrix"]
    assert matrix == {
        "os": ["ubuntu-latest", "macos-latest"],
        "python-version": ["3.11", "3.12", "3.13"],
        "node-version": ["22.14.0", "24"],
    }
    text = json_steps(job)
    assert "tests/test_release_install_matrix.py" in text
    assert "MEMPLEX_INSTALL_MATRIX_PYTHON" in _text(CI)


def test_dependabot_covers_github_actions_python_and_npm() -> None:
    config = yaml.safe_load(_text(DEPENDABOT))
    ecosystems = {item["package-ecosystem"] for item in config["updates"]}
    assert ecosystems == {"github-actions", "pip", "npm"}


def test_legacy_npm_token_workflow_is_removed() -> None:
    assert not OLD_NPM_RELEASE.exists()


def test_release_documentation_uses_a_zip_compatible_source_date_epoch() -> None:
    text = _text(RELEASE_DOCS)
    assert 'epoch="$(git show -s --format=%ct HEAD)"' in text
    assert "--source-date-epoch 0" not in text
