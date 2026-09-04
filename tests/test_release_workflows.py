"""Static security contract for CI and release workflows."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
CI = ROOT / ".github/workflows/ci.yml"
RELEASE = ROOT / ".github/workflows/release.yml"
G008_REAL_HOST = ROOT / ".github/workflows/g008-real-host-lifecycle.yml"
MUTATION_NIGHTLY = ROOT / ".github/workflows/mutation-nightly.yml"
OLD_NPM_RELEASE = ROOT / ".github/workflows/npm-release.yml"
DEPENDABOT = ROOT / ".github/dependabot.yml"
RELEASE_DOCS = ROOT / "docs/release-automation.md"
FULL_SHA_USE = re.compile(r"^\s*-?\s*uses:\s*[^@\s]+@([0-9a-f]{40})\s*(?:#.*)?$", re.MULTILINE)


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _workflow(path: Path) -> dict:
    return yaml.load(_text(path), Loader=yaml.BaseLoader)


def test_all_actions_are_pinned_to_full_commit_shas() -> None:
    for path in (CI, RELEASE, G008_REAL_HOST, MUTATION_NIGHTLY):
        text = _text(path)
        uses_lines = [
            line for line in text.splitlines() if "uses:" in line and "uses: ./" not in line
        ]
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


def test_release_requires_a_non_skippable_real_host_g008_lifecycle_gate() -> None:
    """Removing the protected macOS gate must block publication, not look green in CI."""
    release = _workflow(RELEASE)
    gate = release["jobs"]["four-host-real-lifecycle"]
    assert gate == {
        "needs": ["attest", "build"],
        "uses": "./.github/workflows/g008-real-host-lifecycle.yml",
        "with": {
            "release-artifact-name": "memplex-release-bundle",
            "release-artifact-sha256": "${{ needs.build.outputs.release-artifact-sha256 }}",
        },
        "secrets": "inherit",
    }
    assert "four-host-real-lifecycle" in release["jobs"]["publish-pypi"]["needs"]

    workflow = _workflow(G008_REAL_HOST)
    job = workflow["jobs"]["verify-four-host-lifecycle"]
    assert job["runs-on"] == ["self-hosted", "macOS", "memplex-g008-real-host"]
    assert job["environment"] == "g008-real-host"
    # Hard cap for the proof; a job queued on an offline runner must not
    # look green, and the rerun path is digest-idempotent.
    assert job["timeout-minutes"] == "30"
    assert "if" not in job
    assert "continue-on-error" not in _text(G008_REAL_HOST)
    assert (
        workflow["on"]["workflow_call"]["secrets"]["MEMPLEX_HOST_LIFECYCLE_HMAC_KEY"]["required"]
        == "true"
    )
    assert workflow["on"]["workflow_call"]["inputs"] == {
        "release-artifact-name": {"required": "true", "type": "string"},
        "release-artifact-sha256": {"required": "true", "type": "string"},
    }

    build = release["jobs"]["build"]
    assert build["outputs"]["release-artifact-sha256"] == (
        "${{ steps.release-bundle.outputs.artifact-digest }}"
    )
    upload = next(
        step
        for step in build["steps"]
        if step.get("uses", "").startswith("actions/upload-artifact@")
        and step.get("with", {}).get("name") == "memplex-release-bundle"
    )
    assert upload["id"] == "release-bundle"


def test_real_host_gate_installs_and_verifies_the_upstream_release_bundle() -> None:
    workflow = _workflow(G008_REAL_HOST)
    job = workflow["jobs"]["verify-four-host-lifecycle"]
    text = _text(G008_REAL_HOST)
    steps = job["steps"]

    download = next(
        step for step in steps if step.get("uses", "").startswith("actions/download-artifact@")
    )
    assert download["with"] == {
        "name": "${{ inputs.release-artifact-name }}",
        "path": "${{ runner.temp }}/g008-release-bundle",
        "merge-multiple": "true",
    }
    assert "MEMPLEX_G008_ARTIFACT_SHA256" not in workflow["on"]["workflow_call"]["secrets"]
    assert "MEMPLEX_G008_ARTIFACT_SHA256: ${{ inputs.release-artifact-sha256 }}" in text
    for required in (
        "release-manifest.json",
        "hashlib.sha256(path.read_bytes()).hexdigest()",
        'path.stat().st_size != item["size"]',
        'name.endswith(".whl")',
        'name.endswith(".tgz")',
        "len(wheels) != 1 or len(npm_packages) != 1",
        "uv export --locked --extra dev --no-emit-project",
        'uv venv "$artifact_venv"',
        'uv pip install --python "$artifact_venv/bin/python" --require-hashes',
        '--force-reinstall "$MEMPLEX_G008_WHEEL"',
        '"$MEMPLEX_G008_ARTIFACT_VENV/bin/memplex"',
        '"$MEMPLEX_G008_ARTIFACT_VENV/bin/python" scripts/verify_g008_host_lifecycle.py',
    ):
        assert required in text
    assert 'valid_sha256 "${{ inputs.release-artifact-sha256 }}"' in text


def test_real_host_bundle_verification_rejects_payload_tampering(tmp_path: Path) -> None:
    workflow = _workflow(G008_REAL_HOST)
    step = next(
        item
        for item in workflow["jobs"]["verify-four-host-lifecycle"]["steps"]
        if item.get("name") == "Verify the downloaded manifest and release payload bytes"
    )
    bundle = tmp_path / "g008-release-bundle"
    bundle.mkdir()
    payloads = {
        "memplex-3.3.2-py3-none-any.whl": b"wheel payload",
        "memplex-3.3.2.tgz": b"npm payload",
    }
    artifacts = []
    for name, payload in sorted(payloads.items()):
        path = bundle / name
        path.write_bytes(payload)
        artifacts.append(
            {"name": name, "sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}
        )
    (bundle / "release-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "version": "3.3.2",
                "tag": "v3.3.2",
                "artifacts": artifacts,
            },
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    github_env = tmp_path / "github-env"
    env = {**os.environ, "RUNNER_TEMP": str(tmp_path), "GITHUB_ENV": str(github_env)}

    accepted = subprocess.run(
        ["bash"], input=step["run"], cwd=ROOT, env=env, capture_output=True, text=True, check=False
    
    )
    assert accepted.returncode == 0, accepted.stderr
    exported = github_env.read_text(encoding="utf-8")
    assert f"MEMPLEX_G008_WHEEL={bundle / 'memplex-3.3.2-py3-none-any.whl'}" in exported
    assert f"MEMPLEX_G008_NPM_TGZ={bundle / 'memplex-3.3.2.tgz'}" in exported

    (bundle / "memplex-3.3.2.tgz").write_bytes(b"Npm payload")
    rejected = subprocess.run(
        ["bash"], input=step["run"], cwd=ROOT, env=env, capture_output=True, text=True, check=False
    
    )
    assert rejected.returncode != 0
    assert "digest mismatch" in rejected.stderr


def test_real_host_gate_fails_closed_for_missing_runner_contract_cli_or_binding_secret() -> None:
    """The G008 proof must be real, deployment-bound, and never be a skipped CI surrogate."""
    text = _text(G008_REAL_HOST)
    assert "pull_request" not in text
    assert "workflow_dispatch" in text
    assert "set -euo pipefail" in text
    for required_value in (
        "command -v codex",
        "command -v claude",
        "command -v openclaw",
        "command -v hermes",
        'test -n "${MEMPLEX_HOST_LIFECYCLE_HMAC_KEY:-}"',
        'test -n "${MEMPLEX_G008_HERMES_SOURCE_ROOT:-}"',
        'test -n "${MEMPLEX_G008_DEPLOYMENT_ID:-}"',
        'test -n "${MEMPLEX_G008_SOURCE_SHA256:-}"',
        'test -n "${MEMPLEX_G008_TARGET_IDENTITY_SHA256:-}"',
        "valid_sha256",
        "scripts/verify_g008_host_lifecycle.py",
        "--source-sha256",
        "--artifact-sha256",
        "--deployment-id",
        "--target-identity-sha256",
        "--evidence-output",
    ):
        assert required_value in text
    assert "actions/upload-artifact" not in text
    assert "g008-host-lifecycle.json" in text


def test_real_host_gate_rejects_degraded_runtime_sidecars_before_and_after_proof() -> None:
    """Persistent host failures must not be hidden by a later verifier-only success."""
    text = _text(G008_REAL_HOST)

    assert "mktemp -d" in text
    assert text.count('"$MEMPLEX_G008_ARTIFACT_VENV/bin/memplex" --output json agent install') == 4
    assert '--agent "$agent" --target-dir "$host_root"' in text
    assert "--agent all" not in text
    assert text.count("check_host_status before") == 4
    assert text.count("check_host_status after") == 4
    assert '--isolated-root "$MEMPLEX_G008_ISOLATED_ROOT"' in text
    assert "Check four host runtime sidecars before lifecycle proof" in text
    assert "Check four host runtime sidecars after lifecycle proof" in text
    assert "selected_host" in text
    assert "runtime_status" in text
    assert "state_unreadable" in text
    assert 'runtime_state != "healthy"' in text
    assert "actions/upload-artifact" not in text


def test_release_docs_do_not_present_unit_host_matrix_as_real_host_proof() -> None:
    text = _text(RELEASE_DOCS)

    assert "test_agent_host_matrix.py" in text
    assert "unit-only" in text
    assert "不能作为真实宿主 proof" in text
    assert "agent status --agent all" not in text
    assert "--target-dir" in text


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
    assert text.count("actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6") == 2
    assert "release/release-sbom.cdx.json" in text
    assert "release/release-checksums.json" in text
    assert "release/release-manifest.json" in text


def test_ci_runs_bench_smoke_and_prometheus_config_gates() -> None:
    """Deterministic retrieval smoke and promtool checks must stay wired into CI."""
    workflow = _workflow(CI)
    jobs = workflow["jobs"]
    bench_steps = json_steps(jobs["bench-smoke"])
    assert "scripts/ci_bench_smoke.py" in bench_steps
    assert jobs["bench-smoke"]["timeout-minutes"] == "15"
    prometheus_steps = json_steps(jobs["prometheus-config"])
    assert "promtool check rules deploy/prometheus/memplex-alerts.yml" in prometheus_steps
    assert "promtool check config deploy/prometheus/prometheus.yml" in prometheus_steps
    assert "deploy/prometheus/alertmanager.yml" in prometheus_steps


def test_mutation_nightly_compares_against_the_cosmic_ray_baseline() -> None:
    """The scheduled pilot must fail on drift from the recorded 77/33 baseline."""
    text = _text(MUTATION_NIGHTLY)
    workflow = _workflow(MUTATION_NIGHTLY)
    assert workflow["on"]["schedule"] == [{"cron": "0 3 * * *"}]
    assert "workflow_dispatch" in workflow["on"]
    assert "pull_request" not in text
    assert workflow["permissions"] == {"contents": "read"}
    job = workflow["jobs"]["mutation-pilot"]
    assert job["runs-on"] == "ubuntu-latest"
    steps = json_steps(job)
    assert "scripts/mutation_pilot.sh" in steps
    assert "MUTATION_PILOT_REPORT" in steps
    assert "BASELINE_KILLED = 77" in text
    assert "BASELINE_SURVIVED = 33" in text


def test_ci_has_dependency_review_lock_drift_and_audit() -> None:
    text = _text(CI)
    assert "actions/dependency-review-action" in text
    assert "uv lock --check" in text
    assert "pip-audit" in text
    assert "pull_request_target" not in text
    workflow = _workflow(CI)
    assert workflow["permissions"] == {"contents": "read"}


def test_ci_type_postgres_and_supply_chain_gates_cover_real_release_boundaries() -> None:
    """CI must type-check a bounded core and exercise pgvector without a skip path."""
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    dev = metadata["project"]["optional-dependencies"]["dev"]
    assert any(requirement.startswith("mypy") for requirement in dev)
    assert any(requirement.startswith("build") for requirement in dev)
    assert metadata["tool"]["mypy"]["files"] == [
        "memplex/storage/postgres_resources.py",
        "memplex/storage/postgres_tasks.py",
        "memplex/task_repository.py",
        "memplex/serialization.py",
        "memplex/authorization.py",
        "memplex/sync_ingress.py",
        "memplex/sync_repository.py",
        "memplex/privacy.py",
        "memplex/query_explainer.py",
        "memplex/intent.py",
        "memplex/backup.py",
        "memplex/compaction.py",
        "memplex/temporal.py",
        "memplex/working_memory.py",
        "memplex/sleep_time.py",
        "memplex/improve.py",
        "memplex/service.py",
        "memplex/retrieval/reranker.py",
        "memplex/retrieval/multi_path.py",
        "memplex/core/engine.py",
        "memplex/sync.py",
        "memplex/operations.py",
        "memplex/adapters/cli.py",
        "memplex/storage/lite/store.py",
        "memplex/storage/pool.py",
        "memplex/adapters/agent_installer.py",
        "memplex/adapters/install_transaction.py",
        "memplex/adapters/agent_assets.py",
        "memplex/adapters/agent_runtime.py",
        "memplex/adapters/managed_identity.py",
        "memplex/adapters/runtime_status.py",
        "memplex/adapters/_shared.py",
        "memplex/models/memory.py",
        "memplex/models/paragraph.py",
        "memplex/storage/base.py",
        "memplex/storage/changelog.py",
        "memplex/storage/inbound.py",
        "memplex/storage/vector.py",
        "memplex/storage/lite/search_index.py",
        "memplex/auth.py",
        "memplex/sync_crypto.py",
        "memplex/__main__.py",
        "memplex/core/hooks/collector.py",
        "memplex/core/hooks/hook_event.py",
        "memplex/core/hooks/policy.py",
        "memplex/core/hooks/registry.py",
        "memplex/llm/enhancer.py",
        "memplex/llm/fallback_chain.py",
        "memplex/llm/sanitizer.py",
        "memplex/llm/injection_guard.py",
        "memplex/llm/provider.py",
        "memplex/wiki/community.py",
        "memplex/wiki/generator.py",
        "memplex/wiki/search.py",
        "memplex/__init__.py",
        "memplex/_plugin/__init__.py",
        "memplex/models/__init__.py",
        "memplex/models/feedback.py",
        "memplex/models/graph.py",
        "memplex/models/misc.py",
        "memplex/models/search.py",
        "memplex/models/source.py",
        "memplex/models/task.py",
        "memplex/query_pipeline.py",
        "memplex/config.py",
        "memplex/host_lifecycle.py",
        "memplex/logging_config.py",
        "memplex/product.py",
        "memplex/readiness_evidence.py",
        "memplex/release.py",
        "memplex/sync_dispatcher.py",
        "memplex/sync_protocol.py",
        "memplex/worker.py",
        "memplex/adapters/__init__.py",
        "memplex/adapters/claude_skill.py",
        "memplex/adapters/codex_plugin.py",
        "memplex/adapters/jsonc_edit.py",
        "memplex/adapters/mcp_server.py",
        "memplex/adapters/openclaw_plugin.py",
        "memplex/adapters/yaml_edit.py",
        "memplex/core/__init__.py",
        "memplex/core/associator/__init__.py",
        "memplex/core/associator/domain_classifier.py",
        "memplex/core/associator/entity_aligner.py",
        "memplex/core/associator/ref_linker.py",
        "memplex/core/dictionaries/__init__.py",
        "memplex/core/extractors/__init__.py",
        "memplex/core/extractors/markdown.py",
        "memplex/core/extractors/vision_mapper.py",
        "memplex/core/handlers/__init__.py",
        "memplex/core/handlers/clipboard.py",
        "memplex/core/handlers/file_handler.py",
        "memplex/core/handlers/url_handler.py",
        "memplex/core/hooks/__init__.py",
        "memplex/llm/__init__.py",
        "memplex/llm/providers/__init__.py",
        "memplex/llm/providers/_common.py",
        "memplex/llm/providers/rule_based.py",
        "memplex/operations_assets/__init__.py",
        "memplex/processing/__init__.py",
        "memplex/processing/function_builder.py",
        "memplex/processing/graph_builder.py",
        "memplex/processing/merger/__init__.py",
        "memplex/processing/merger/confidence_calculator.py",
        "memplex/processing/merger/conflict_resolver.py",
        "memplex/retrieval/__init__.py",
        "memplex/storage/__init__.py",
        "memplex/storage/_messages.py",
        "memplex/storage/feedback.py",
        "memplex/storage/lite/__init__.py",
        "memplex/storage/lite/durability.py",
        "memplex/storage/lite/sync_repository.py",
        "memplex/storage/migrations/__init__.py",
        "memplex/storage/migrations/_constants.py",
        "memplex/storage/migrations/acl_verification.py",
        "memplex/storage/migrations/catalogue_checks.py",
        "memplex/storage/migrations/catalogue_snapshot.py",
        "memplex/storage/migrations/ledger_state.py",
        "memplex/storage/migrations/runner.py",
        "memplex/storage/postgres.py",
        "memplex/storage/postgres_backup.py",
        "memplex/storage/postgres_sync.py",
        "memplex/wiki/__init__.py",
        "memplex/wiki/compiler.py",
    ]

    workflow = _workflow(CI)
    test_steps = json_steps(workflow["jobs"]["test"])
    assert "uv run mypy" in test_steps
    # Coverage floor is enforced in CI (pytest-cov ignores the pyproject
    # fail_under unless the flag is passed) and matches pyproject.
    assert "--cov-fail-under=75" in test_steps
    assert metadata["tool"]["coverage"]["report"]["fail_under"] == 75
    assert metadata["tool"]["pytest"]["ini_options"]["timeout"] == 120
    # Complexity freeze-gate and hexagonal architecture contract.
    assert "uv run ruff check memplex tests" in test_steps
    assert "uv run lint-imports" in test_steps
    for local_g004_test in (
        "tests/test_g004_cli_runner_contract.py",
        "tests/test_g004_lite_real_value.py",
        "tests/test_g004_agent_real_value.py",
        "tests/test_g004_sync_real_loopback.py",
    ):
        assert local_g004_test in test_steps
    assert "--ignore=tests/test_g004_postgres_backup_real_value.py" in test_steps
    assert "--ignore=tests/test_g004_postgres_probe_isolation.py" in test_steps

    postgres_job = workflow["jobs"]["test-postgres"]
    assert postgres_job["services"]["postgres"]["image"] == (
        "pgvector/pgvector:0.8.6-pg16@"
        "sha256:a36250871de0833b8757561c72f2477ef1ddd1101afa4e617fb552e0de514c6b"
    )
    assert postgres_job["env"]["MEMPLEX_TEST_POSTGRES_DSN"].startswith("postgresql://")
    assert postgres_job["env"]["MEMPLEX_REQUIRE_PGVECTOR"] == "1"
    postgres_steps = json_steps(postgres_job)
    for required_test in (
        "tests/test_ci_postgres_contract.py",
        "tests/test_postgres_integration.py",
        "tests/test_postgres_backup_integration.py",
        "tests/test_sync_postgres_integration.py",
        "tests/test_sync_repository_contract.py",
        "tests/test_postgres_store.py",
        "tests/test_g014_postgres_task_repository.py",
        "tests/test_g004_postgres_backup_real_value.py",
        "tests/test_g004_postgres_probe_isolation.py",
    ):
        assert required_test in postgres_steps

    audit_steps = json_steps(workflow["jobs"]["security"])
    for extra in (
        "all",
        "embedding",
        "extractors",
        "vector",
        "local-onnx",
        "graph",
        "http",
        "llm",
        "postgres",
        "pgtest",
        "dev",
    ):
        assert f"--extra {extra}" in audit_steps


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
