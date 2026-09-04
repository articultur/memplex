"""Industrial production-contract and readiness acceptance tests."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

import memplex.config as config_module
import memplex.product as product_module
from memplex.backup import BackupArtifactWriter, PitrReadiness, run_restore_drill
from memplex.capacity_chaos import (
    CapacityChaosEvidence,
    WorkloadMetrics,
    write_capacity_chaos_evidence,
)
from memplex.config import MemplexConfig
from memplex.host_lifecycle import (
    HostLifecycleBinding,
    HostLifecycleEvidence,
    HostLifecycleProof,
    current_host_contract_digests,
    required_host_node_results,
    required_node_manifest_sha256,
    write_host_lifecycle_evidence,
)
from memplex.operations import OperationsReadinessBinding, create_operations_evidence
from memplex.readiness_evidence import (
    DeploymentEvidenceBinding,
    IndustrialGateEvidence,
    write_industrial_gate_evidence,
)
from memplex.release import ReleaseEvidence, ReleaseManifest
from memplex.service import MemplexService
from memplex.storage import create_store


def _readiness_report(config: MemplexConfig) -> dict:
    reporter = getattr(product_module, "industrial_readiness_report", None)
    assert callable(reporter), "industrial readiness reporter is missing"
    return reporter(config)


def _set_deployment_binding(
    monkeypatch: pytest.MonkeyPatch,
    *,
    deployment_id: str = "g012-industrial-test",
    source_sha256: str = "1" * 64,
    artifact_sha256: str = "2" * 64,
    target_identity_sha256: str = "3" * 64,
) -> DeploymentEvidenceBinding:
    monkeypatch.setenv("MEMPLEX_DEPLOYMENT_ID", deployment_id)
    monkeypatch.setenv("MEMPLEX_SOURCE_SHA256", source_sha256)
    monkeypatch.setenv("MEMPLEX_ARTIFACT_SHA256", artifact_sha256)
    monkeypatch.setenv("MEMPLEX_TARGET_IDENTITY_SHA256", target_identity_sha256)
    return DeploymentEvidenceBinding.from_values(
        memplex_version="3.3.0",
        source_sha256=source_sha256,
        artifact_sha256=artifact_sha256,
        deployment_id=deployment_id,
        target_identity_sha256=target_identity_sha256,
    )


def test_default_deployment_contract_is_explicitly_development() -> None:
    """Removing the deployment contract must not silently restore an implicit profile."""

    config = MemplexConfig()
    deployment = getattr(config, "deployment", None)

    assert deployment is not None
    assert deployment.profile == "development"


def test_production_profile_rejects_lite_backend() -> None:
    """Changing the production backend rule to permit Lite must fail this test."""

    validator = getattr(config_module, "validate_deployment_contract", None)
    assert callable(validator), "deployment contract validator is missing"
    config = MemplexConfig()
    config.deployment.profile = "production"
    config.storage.backend = "lite"

    with pytest.raises(ValueError, match="production.*postgres",):
        validator(config)


def test_production_profile_accepts_postgres_configuration() -> None:
    """The contract check must not require a live database connection."""

    validator = getattr(config_module, "validate_deployment_contract", None)
    assert callable(validator), "deployment contract validator is missing"
    config = MemplexConfig()
    config.deployment.profile = "production"
    config.storage.backend = "postgres"
    config.storage.path = "postgresql://memplex@example.invalid/memplex"
    config.storage.migration_dsn = "postgresql://migrator@example.invalid/memplex"

    validator(config)


@pytest.mark.parametrize(
    ("application_dsn", "migration_dsn"),
    [
        ("not-a-dsn", "postgresql://migrator@example.invalid/memplex"),
        ("postgresql://memplex@example.invalid/memplex", "not-a-dsn"),
        (
            " postgresql://memplex@example.invalid/memplex",
            "postgresql://migrator@example.invalid/memplex",
        ),
        (
            "postgresql://memplex@example.invalid/memplex",
            "postgresql://migrator@example.invalid/memplex ",
        ),
        (
            "postgresql://shared@example.invalid/memplex",
            "postgresql://shared@example.invalid/memplex",
        ),
        (
            "host=example.invalid dbname=memplex user=shared application_name=app",
            "application_name=migration user=shared dbname=memplex host=example.invalid",
        ),
    ],
)
def test_production_profile_rejects_invalid_or_equivalent_postgres_identities(
    application_dsn: str, migration_dsn: str
) -> None:
    config = MemplexConfig()
    config.deployment.profile = "production"
    config.storage.backend = "postgres"
    config.storage.path = application_dsn
    config.storage.migration_dsn = migration_dsn

    with pytest.raises(ValueError, match="production postgres.*(invalid|distinct)"):
        config_module.validate_deployment_contract(config)

    gate = next(
        item
        for item in _readiness_report(config)["gates"]
        if item["id"] == "production_storage"
    )
    assert gate["status"] == "fail"


@pytest.mark.parametrize("field", ("path", "migration_dsn"))
def test_production_profile_rejects_missing_split_postgres_dsn(field: str) -> None:
    """Production cannot silently reuse a privileged application connection."""

    validator = getattr(config_module, "validate_deployment_contract", None)
    assert callable(validator), "deployment contract validator is missing"
    config = MemplexConfig()
    config.deployment.profile = "production"
    config.storage.backend = "postgres"
    config.storage.path = "postgresql://memplex@example.invalid/memplex"
    config.storage.migration_dsn = "postgresql://migrator@example.invalid/memplex"
    setattr(config.storage, field, "")

    with pytest.raises(ValueError, match="non-empty.*DSN"):
        validator(config)


def test_service_enforces_production_storage_contract_before_startup(tmp_path: Path) -> None:
    """Removing startup validation must allow the forbidden Lite service and fail here."""

    config = MemplexConfig()
    config.deployment = SimpleNamespace(profile="production")
    config.storage.backend = "lite"
    config.storage.path = str(tmp_path)
    config.llm.query_enhancement = False

    with pytest.raises(ValueError, match="production.*postgres"):
        MemplexService(config=config)


def test_public_store_factory_enforces_production_storage_contract(
    tmp_path: Path,
) -> None:
    """Direct factory callers must not bypass the production topology contract."""

    config = MemplexConfig()
    config.deployment.profile = "production"
    config.storage.backend = "lite"
    config.storage.path = str(tmp_path)

    with pytest.raises(ValueError, match="production.*postgres"):
        create_store(config)


def test_readiness_and_startup_share_canonical_deployment_values() -> None:
    """Reporting and startup must agree after trimming and case normalization."""

    validator = getattr(config_module, "validate_deployment_contract", None)
    assert callable(validator), "deployment contract validator is missing"
    config = MemplexConfig()
    config.deployment.profile = " production "
    config.storage.backend = "POSTGRES"
    config.storage.path = "postgresql://memplex@example.invalid/memplex"
    config.storage.migration_dsn = "postgresql://migrator@example.invalid/memplex"

    validator(config)
    report = _readiness_report(config)

    assert config.deployment.profile == "production"
    assert config.storage.backend == "postgres"
    assert report["summary"] == {"passed": 2, "failed": 1, "blocked": 7, "total": 10}
    assert report["gates"][0]["evidence"] == "deployment.profile=production"
    assert report["gates"][1]["evidence"] == (
        "storage.backend=postgres; application and migration DSNs configured"
    )


def test_deployment_profile_environment_override_is_typed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Dropping the env mapping must leave the profile at development and fail here."""

    monkeypatch.setenv("MEMPLEX_DEPLOYMENT_PROFILE", "production")
    config = config_module.load_config(path=str(tmp_path / "missing.yaml"))

    assert config.deployment.profile == "production"


def test_industrial_readiness_report_fails_closed_until_every_gate_passes() -> None:
    """A missing gate or optimistic default must never produce an industrial-ready result."""

    report = _readiness_report(MemplexConfig())

    assert report["schema_version"] == 1
    assert report["status"] == "not_ready"
    assert report["maturity"] == "developer_preview"
    assert report["ready"] is False
    assert set(report["blocking_gate_ids"]) == {
        "production_profile",
        "production_storage",
        "principal_tenant_acl",
        "schema_migrations_atomicity",
        "durable_sync_backpressure",
        "backup_restore_dr",
        "operations_slo",
        "release_supply_chain",
        "four_host_e2e",
        "capacity_chaos",
    }
    assert report["summary"] == {"passed": 0, "failed": 3, "blocked": 7, "total": 10}
    migration_gate = next(
        gate for gate in report["gates"] if gate["id"] == "schema_migrations_atomicity"
    )
    sync_gate = next(
        gate for gate in report["gates"] if gate["id"] == "durable_sync_backpressure"
    )
    assert migration_gate["status"] == "blocked"
    assert sync_gate["status"] == "blocked"


def test_g003_g004_require_current_signed_deployment_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    binding = _set_deployment_binding(monkeypatch)
    key = b"i" * 32
    key_id = "industrial-2026-08"
    monkeypatch.setenv(
        "MEMPLEX_INDUSTRIAL_EVIDENCE_HMAC_KEY",
        base64.b64encode(key).decode("ascii"),
    )
    monkeypatch.setenv("MEMPLEX_INDUSTRIAL_EVIDENCE_KEY_ID", key_id)
    now = datetime.now(UTC)
    for gate_id, env_name in (
        ("schema_migrations_atomicity", "MEMPLEX_G003_STORAGE_REPORT"),
        ("durable_sync_backpressure", "MEMPLEX_G004_SYNC_REPORT"),
    ):
        report_path = tmp_path / f"{gate_id}.json"
        write_industrial_gate_evidence(
            report_path,
            IndustrialGateEvidence.create(
                gate_id=gate_id,
                binding=binding,
                run_result_sha256="4" * 64,
                key_id=key_id,
                signing_key=key,
                generated_at=now,
            ),
        )
        monkeypatch.setenv(env_name, str(report_path))

    report = _readiness_report(MemplexConfig())
    for gate_id in ("schema_migrations_atomicity", "durable_sync_backpressure"):
        gate = next(item for item in report["gates"] if item["id"] == gate_id)
        assert gate["status"] == "pass"
        assert gate["evidence"] == "signed current deployment evidence verified"
    rendered = json.dumps(report, sort_keys=True)
    assert str(tmp_path) not in rendered
    assert key.hex() not in rendered


def test_capacity_chaos_gate_accepts_only_current_signed_passing_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = b"c" * 32
    now = datetime.now(UTC)
    report = CapacityChaosEvidence.create(
        report_id="018f7f1d-7c9e-7c31-9d34-35f6a91e2bb8",
        generated_at=now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        window_started_at=(now - timedelta(seconds=62)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        window_ended_at=(now - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        memplex_version="3.3.0",
        python_version="3.13.5",
        postgres_version="16.4",
        platform="macOS-15.6",
        machine_arch="arm64",
        cpu_count=10,
        memory_bytes=32 * 1024**3,
        function_count=100_000,
        edge_count=1_000_000,
        soak_seconds=61.0,
        operations_count=3_000,
        throughput_ops_per_second=49.18,
        read=WorkloadMetrics(1000, 0, 2.0, 5.0, 10.0),
        write=WorkloadMetrics(1000, 0, 3.0, 8.0, 20.0),
        sync=WorkloadMetrics(1000, 0, 3.0, 9.0, 22.0),
        error_rate=0.0,
        rss_peak_bytes=512 * 1024**2,
        queue_depth_end=0,
        outbox_max_age_seconds=0.0,
        rpo_lost_events=0,
        rto_seconds=1.5,
        data_digest_before="1" * 64,
        data_digest_after="1" * 64,
        chaos={
            "database": "passed",
            "network": "passed",
            "disk": "passed",
            "term": "passed",
            "kill": "passed",
            "duplicate_delivery": "passed",
            "redis": "not_applicable",
        },
        redis_reason="redis_not_in_supported_topology",
        key_id="g009-key",
        signing_key=key,
    )
    path = tmp_path / "capacity.json"
    write_capacity_chaos_evidence(path, report)
    monkeypatch.setenv("MEMPLEX_G009_CAPACITY_CHAOS_REPORT", str(path))
    monkeypatch.setenv("MEMPLEX_CAPACITY_CHAOS_HMAC_KEY", key.hex())

    readiness = _readiness_report(MemplexConfig())
    gate = next(item for item in readiness["gates"] if item["id"] == "capacity_chaos")
    assert gate["status"] == "pass"
    assert gate["evidence"] == "signed production-scale capacity and chaos evidence verified"
    rendered = json.dumps(readiness, sort_keys=True)
    assert str(tmp_path) not in rendered
    assert key.hex() not in rendered


def test_backup_restore_gate_accepts_only_matching_signed_dr_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = bytes(range(32))
    source = tmp_path / "source.dump"
    source.write_bytes(b"verified-postgres-backup")
    artifact = BackupArtifactWriter(
        tmp_path / "backups", key=key, key_id="g005-key", max_bytes=1024
    ).publish(
        manifest_fields={
            "format_version": 1,
            "backup_id": "018f7f1d-7c9e-7c31-9d34-35f6a91e2bb8",
            "created_at": "2026-08-11T12:00:00.000000Z",
            "backend": "postgres",
            "database": "memplex",
            "schema": "tenant_a",
            "migration_version": 5,
            "payload_name": "payload.dump",
            "pg_dump_version": "16.2",
            "server_version": "16.2",
            "consistency": "pg_dump_snapshot",
        },
        payload_source=source,
    )
    manifest = json.loads((artifact / "manifest.json").read_text(encoding="utf-8"))
    report = run_restore_drill(
        backup_id=manifest["backup_id"],
        backup_completed_at=manifest["created_at"],
        fault_cutoff_at="2026-08-11T12:00:05.000000Z",
        restore_started_at="2026-08-11T12:01:00.000000Z",
        restore_verified_at="2026-08-11T12:01:10.000000Z",
        rpo_target_seconds=300,
        rto_target_seconds=1800,
        data_digest=manifest["payload_sha256"],
        data_verified=True,
        pitr=PitrReadiness(
            ready=True,
            wal_level="replica",
            archive_mode="on",
            archive_command_configured=True,
            full_page_writes=True,
            max_wal_senders=10,
        ),
        key_id="g005-key",
        signing_key=key,
    )
    report_path = tmp_path / "drill.json"
    report_path.write_bytes(report.canonical_bytes())
    monkeypatch.setenv(
        "MEMPLEX_BACKUP_HMAC_KEY", base64.b64encode(key).decode("ascii")
    )
    monkeypatch.setenv("MEMPLEX_G005_BACKUP_ARTIFACT", str(artifact))
    monkeypatch.setenv("MEMPLEX_G005_DRILL_REPORT", str(report_path))

    readiness = _readiness_report(MemplexConfig())
    gate = next(item for item in readiness["gates"] if item["id"] == "backup_restore_dr")

    assert gate == {
        "id": "backup_restore_dr",
        "status": "pass",
        "required": True,
        "requirement": "Verified backup, restore, PITR, and measured RPO/RTO drills.",
        "evidence": "signed PostgreSQL restore drill verified",
    }
    serialized = json.dumps(readiness, sort_keys=True)
    assert str(tmp_path) not in serialized
    assert base64.b64encode(key).decode("ascii") not in serialized


def test_backup_restore_gate_rejects_invalid_report_without_exposing_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = base64.b64encode(bytes(range(32))).decode("ascii")
    artifact = tmp_path / "secret-artifact"
    report = tmp_path / "secret-report.json"
    artifact.mkdir()
    report.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("MEMPLEX_BACKUP_HMAC_KEY", secret)
    monkeypatch.setenv("MEMPLEX_G005_BACKUP_ARTIFACT", str(artifact))
    monkeypatch.setenv("MEMPLEX_G005_DRILL_REPORT", str(report))

    readiness = _readiness_report(MemplexConfig())
    gate = next(item for item in readiness["gates"] if item["id"] == "backup_restore_dr")

    assert gate["status"] == "fail"
    assert gate["evidence"] == "signed PostgreSQL restore drill invalid"
    serialized = json.dumps(readiness, sort_keys=True)
    assert str(tmp_path) not in serialized
    assert secret not in serialized


def test_operations_slo_gate_accepts_only_signed_passing_measured_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = b"o" * 32
    config = MemplexConfig()
    config.operations.report_key_id = "ops-key"
    binding = _set_deployment_binding(monkeypatch)
    now = datetime.now(UTC)
    report = create_operations_evidence(
        metrics_snapshot={
            "request_count": 1000,
            "successful_requests": 999,
            "latency_sample_count": 128,
            "p95_latency_ms": 100.0,
        },
        shutdown_result={"request_drained": True, "deadline_exceeded": False},
        config=config,
        report_id="018f7f1d-7c9e-7c31-9d34-35f6a91e2bb8",
        window_started_at=(now - timedelta(seconds=301)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        ),
        window_ended_at=(now - timedelta(seconds=1)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        ),
        generated_at=now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        readiness_binding=OperationsReadinessBinding(
            deployment_id=binding.deployment_id,
            source_sha256=binding.source_sha256,
            artifact_sha256=binding.artifact_sha256,
            target_identity_sha256=binding.target_identity_sha256,
            expected_key_id="ops-key",
        ),
        signing_key=key,
    )
    path = tmp_path / "operations.json"
    path.write_bytes(report.to_json())
    secret = base64.b64encode(key).decode("ascii")
    monkeypatch.setenv("MEMPLEX_OPERATIONS_HMAC_KEY", secret)
    monkeypatch.setenv("MEMPLEX_G006_OPERATIONS_REPORT", str(path))

    readiness = _readiness_report(config)
    gate = next(item for item in readiness["gates"] if item["id"] == "operations_slo")
    assert gate["status"] == "pass"
    assert gate["evidence"] == "signed measured operations SLO report verified"
    rendered = json.dumps(readiness, sort_keys=True)
    assert str(tmp_path) not in rendered
    assert secret not in rendered


def test_operations_slo_gate_rejects_tamper_without_exposing_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = b"o" * 32
    path = tmp_path / "secret-operations.json"
    path.write_text("{}", encoding="utf-8")
    secret = base64.b64encode(key).decode("ascii")
    monkeypatch.setenv("MEMPLEX_OPERATIONS_HMAC_KEY", secret)
    monkeypatch.setenv("MEMPLEX_G006_OPERATIONS_REPORT", str(path))
    readiness = _readiness_report(MemplexConfig())
    gate = next(item for item in readiness["gates"] if item["id"] == "operations_slo")
    assert gate["status"] == "fail"
    assert gate["evidence"] == "signed operations SLO evidence invalid"
    rendered = json.dumps(readiness, sort_keys=True)
    assert str(tmp_path) not in rendered
    assert secret not in rendered


def test_release_supply_chain_gate_accepts_only_current_signed_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = tmp_path / "bundle"
    build = subprocess.run(
        [
            sys.executable,
            "scripts/build_release_artifacts.py",
            "--source",
            ".",
            "--output",
            str(bundle),
            "--tag",
            "v3.3.0",
            "--source-date-epoch",
            "1704067200",
            "--allow-dirty",
        ],
        cwd=Path(__file__).resolve().parent.parent,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    assert build.returncode == 0, (build.stdout, build.stderr)
    manifest_bytes = (bundle / "release-manifest.json").read_bytes()
    manifest = ReleaseManifest.from_dict(json.loads(manifest_bytes))
    key = b"r" * 32
    evidence = ReleaseEvidence.create(
        manifest=manifest,
        manifest_sha256=sha256(manifest_bytes).hexdigest(),
        sbom_sha256=sha256((bundle / "release-sbom.cdx.json").read_bytes()).hexdigest(),
        checksums_sha256=sha256(
            (bundle / "release-checksums.json").read_bytes()
        ).hexdigest(),
        key_id="g007-key",
        signing_key=key,
    )
    evidence_path = tmp_path / "release-evidence.json"
    evidence_path.write_bytes(evidence.canonical_bytes())
    monkeypatch.setenv("MEMPLEX_G007_RELEASE_BUNDLE", str(bundle))
    monkeypatch.setenv("MEMPLEX_G007_RELEASE_EVIDENCE", str(evidence_path))
    monkeypatch.setenv("MEMPLEX_RELEASE_EVIDENCE_KEY", key.hex())

    readiness = _readiness_report(MemplexConfig())
    gate = next(item for item in readiness["gates"] if item["id"] == "release_supply_chain")
    assert gate["status"] == "pass"
    assert gate["evidence"] == "signed immutable release bundle verified"
    rendered = json.dumps(readiness, sort_keys=True)
    assert str(tmp_path) not in rendered
    assert key.hex() not in rendered


def test_four_host_gate_accepts_only_current_signed_matrix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = b"h" * 32
    isolated = tmp_path / "isolated"
    isolated.mkdir()
    marker = isolated / "prestate"
    marker.write_text("isolated", encoding="utf-8")
    producer_cli = tmp_path / "producer-cli"
    producer_cli.write_bytes(Path(sys.executable).resolve().read_bytes())
    producer_cli.chmod(0o700)
    cli_sha256 = sha256(producer_cli.read_bytes()).hexdigest()
    isolated_sha256 = sha256(marker.read_bytes()).hexdigest()
    contracts = current_host_contract_digests()
    proofs = []
    for host in ("claude-code", "codex", "hermes", "openclaw"):
        results = required_host_node_results(host)
        proofs.append(
            HostLifecycleProof(
                host=host,
                cli_path=str(producer_cli),
                cli_sha256=cli_sha256,
                cli_version=sys.version,
                contract_sha256=contracts[host],
                isolated_root_sha256=isolated_sha256,
                required_node_results=results,
                required_node_manifest_sha256=required_node_manifest_sha256(results),
                junit_sha256="4" * 64,
            )
        )
    binding = HostLifecycleBinding(
        deployment_id="g012-industrial-test",
        source_sha256="1" * 64,
        artifact_sha256="2" * 64,
        target_identity_sha256="3" * 64,
        expected_key_id="g008-local",
    )
    evidence = HostLifecycleEvidence.create(
        memplex_version="3.3.0",
        host_proofs=tuple(proofs),
        binding=binding,
        key_id="g008-local",
        signing_key=key,
    )
    path = tmp_path / "hosts.json"
    write_host_lifecycle_evidence(path, evidence)
    producer_cli.unlink()
    monkeypatch.setenv("MEMPLEX_G008_HOST_LIFECYCLE_REPORT", str(path))
    monkeypatch.setenv("MEMPLEX_HOST_LIFECYCLE_HMAC_KEY", key.hex())
    monkeypatch.setenv("MEMPLEX_HOST_LIFECYCLE_KEY_ID", binding.expected_key_id)
    monkeypatch.setenv("MEMPLEX_DEPLOYMENT_ID", binding.deployment_id)
    monkeypatch.setenv("MEMPLEX_SOURCE_SHA256", binding.source_sha256)
    monkeypatch.setenv("MEMPLEX_ARTIFACT_SHA256", binding.artifact_sha256)
    monkeypatch.setenv("MEMPLEX_TARGET_IDENTITY_SHA256", binding.target_identity_sha256)

    readiness = _readiness_report(MemplexConfig())
    gate = next(item for item in readiness["gates"] if item["id"] == "four_host_e2e")
    assert gate["status"] == "pass"
    assert gate["evidence"] == "signed real four-host lifecycle matrix verified"
    rendered = json.dumps(readiness, sort_keys=True)
    assert str(tmp_path) not in rendered
    assert key.hex() not in rendered

    payload = json.loads(path.read_text())
    payload["hosts"][0]["cli_version"] = "tampered"
    path.write_text(json.dumps(payload))
    failed = _readiness_report(MemplexConfig())
    failed_gate = next(item for item in failed["gates"] if item["id"] == "four_host_e2e")
    assert failed_gate["status"] == "fail"
    assert failed_gate["evidence"] == "signed four-host lifecycle evidence invalid"


def test_principal_tenant_acl_gate_requires_production_postgres_and_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A registry alone must not make a non-production topology look authorized."""

    monkeypatch.setenv(
        "MEMPLEX_PRINCIPALS_JSON",
        json.dumps(
            [
                {
                    "credential_id": "ci",
                    "token_sha256": "a" * 64,
                    "tenant_id": "tenant-a",
                    "subject_id": "alice",
                    "workspace_id": "workspace-a",
                }
            ]
        ),
    )

    report = _readiness_report(MemplexConfig())
    gate = next(item for item in report["gates"] if item["id"] == "principal_tenant_acl")

    assert gate["status"] == "fail"
    assert gate["evidence"] == "principal registry configured; production postgres required"


def test_principal_tenant_acl_gate_fails_when_production_registry_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production must not silently fall back to shared or anonymous identity."""

    monkeypatch.delenv("MEMPLEX_PRINCIPALS_JSON", raising=False)
    config = MemplexConfig()
    config.deployment.profile = "production"
    config.storage.backend = "postgres"
    config.storage.path = "postgresql://memplex@example.invalid/memplex"
    config.storage.migration_dsn = "postgresql://migrator@example.invalid/memplex"
    config.storage.path = "postgresql://memplex@example.invalid/memplex"
    config.storage.migration_dsn = "postgresql://migrator@example.invalid/memplex"

    report = _readiness_report(config)
    gate = next(item for item in report["gates"] if item["id"] == "principal_tenant_acl")

    assert gate["status"] == "fail"
    assert gate["evidence"] == "principal registry missing"
    assert report["summary"] == {"passed": 2, "failed": 1, "blocked": 7, "total": 10}


def test_principal_tenant_acl_gate_passes_for_valid_production_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The G002 gate gets a positive machine-readable result only on its contract."""

    monkeypatch.setenv(
        "MEMPLEX_PRINCIPALS_JSON",
        json.dumps(
            [
                {
                    "credential_id": "ci",
                    "token_sha256": "a" * 64,
                    "tenant_id": "tenant-a",
                    "subject_id": "alice",
                    "workspace_id": "workspace-a",
                    "roles": ["memory-user"],
                }
            ]
        ),
    )
    config = MemplexConfig()
    config.deployment.profile = "production"
    config.storage.backend = "postgres"
    config.storage.path = "postgresql://memplex@example.invalid/memplex"
    config.storage.migration_dsn = "postgresql://migrator@example.invalid/memplex"

    report = _readiness_report(config)
    gate = next(item for item in report["gates"] if item["id"] == "principal_tenant_acl")

    assert gate["status"] == "pass"
    assert gate["evidence"] == "principal registry configured"
    assert report["summary"] == {"passed": 3, "failed": 0, "blocked": 7, "total": 10}
    assert report["ready"] is False
    assert report["maturity"] == "developer_preview"


def test_all_independent_machine_gates_produce_industrial_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The final aggregator must become ready only when every verified gate passes."""

    monkeypatch.setenv(
        "MEMPLEX_PRINCIPALS_JSON",
        json.dumps(
            [
                {
                    "credential_id": "ci",
                    "token_sha256": "a" * 64,
                    "tenant_id": "tenant-a",
                    "subject_id": "alice",
                    "workspace_id": "workspace-a",
                }
            ]
        ),
    )

    def passed(gate_id: str, requirement: str, evidence: str) -> dict[str, object]:
        return {
            "id": gate_id,
            "status": "pass",
            "required": True,
            "requirement": requirement,
            "evidence": evidence,
        }

    monkeypatch.setattr(
        product_module,
        "_backup_restore_dr_gate",
        lambda: passed("backup_restore_dr", "backup", "verified"),
    )
    monkeypatch.setattr(
        product_module,
        "_schema_migrations_atomicity_gate",
        lambda: passed("schema_migrations_atomicity", "storage", "verified"),
    )
    monkeypatch.setattr(
        product_module,
        "_durable_sync_backpressure_gate",
        lambda: passed("durable_sync_backpressure", "sync", "verified"),
    )
    monkeypatch.setattr(
        product_module,
        "_operations_slo_gate",
        lambda _config: passed("operations_slo", "operations", "verified"),
    )
    monkeypatch.setattr(
        product_module,
        "_release_supply_chain_gate",
        lambda: passed("release_supply_chain", "release", "verified"),
    )
    monkeypatch.setattr(
        product_module,
        "_four_host_e2e_gate",
        lambda: passed("four_host_e2e", "hosts", "verified"),
    )
    monkeypatch.setattr(
        product_module,
        "_capacity_chaos_gate",
        lambda: passed("capacity_chaos", "capacity", "verified"),
    )
    config = MemplexConfig()
    config.deployment.profile = "production"
    config.storage.backend = "postgres"
    config.storage.path = "postgresql://memplex@example.invalid/memplex"
    config.storage.migration_dsn = "postgresql://migrator@example.invalid/memplex"

    report = _readiness_report(config)

    assert report["ready"] is True
    assert report["status"] == "ready"
    assert report["maturity"] == "industrial"
    assert report["summary"] == {"passed": 10, "failed": 0, "blocked": 0, "total": 10}
    assert report["blocking_gate_ids"] == []


def test_principal_tenant_acl_gate_fails_closed_for_malformed_registry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Readiness must report malformed registry state without exposing its contents."""

    monkeypatch.setenv("MEMPLEX_PRINCIPALS_JSON", '{"not": "a-list"}')
    config = MemplexConfig()
    config.deployment.profile = "production"
    config.storage.backend = "postgres"
    config.storage.path = "postgresql://memplex@example.invalid/memplex"
    config.storage.migration_dsn = "postgresql://migrator@example.invalid/memplex"

    report = _readiness_report(config)
    gate = next(item for item in report["gates"] if item["id"] == "principal_tenant_acl")

    assert gate["status"] == "fail"
    assert gate["evidence"] == "principal registry invalid"
    assert "not" not in gate["evidence"]
    assert report["summary"] == {"passed": 2, "failed": 1, "blocked": 7, "total": 10}


def test_industrial_readiness_contract_never_lists_lite_as_production_supported() -> None:
    """Changing the topology contract to advertise Lite in production must fail here."""

    report = _readiness_report(MemplexConfig())
    topology = report["production_topology"]

    assert topology["storage_backend"] == "postgres"
    assert topology["lite"]["production_supported"] is False
    assert topology["lite"]["max_processes"] == 1


def test_unsigned_local_migration_report_cannot_close_remaining_industrial_gates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Accepting an unsigned local report as industrial evidence would be a false attestation."""

    report_path = tmp_path / "migration-report.json"
    report_path.write_text(
        json.dumps({"schema_version": 1, "passed": True, "signed": False}), encoding="utf-8"
    )
    monkeypatch.setenv("MEMPLEX_STORAGE_INTEGRITY_EVIDENCE", str(report_path))
    config = MemplexConfig()
    config.deployment.profile = "production"
    config.storage.backend = "postgres"
    config.storage.path = "postgresql://memplex@example.invalid/memplex"
    config.storage.migration_dsn = "postgresql://migrator@example.invalid/memplex"

    report = _readiness_report(config)
    gate = next(item for item in report["gates"] if item["id"] == "schema_migrations_atomicity")

    assert gate["status"] == "blocked"
    assert report["ready"] is False


def test_readiness_cli_is_machine_readable_and_strict_is_fail_closed(tmp_path: Path) -> None:
    """Removing the command or returning success from --strict must fail this boundary test."""

    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "MEMPLEX_STORAGE_BACKEND": "lite",
        "MEMPLEX_STORAGE_PATH": str(tmp_path / "store"),
        "MEMPLEX_DEPLOYMENT_PROFILE": "development",
    }
    base = [sys.executable, "-m", "memplex", "--output", "json", "readiness"]

    inspect = subprocess.run(base, capture_output=True, text=True, env=env, timeout=30, check=False)
    strict = subprocess.run(
        [*base, "--strict"], capture_output=True, text=True, env=env, timeout=30, check=False
    
    )

    assert inspect.returncode == 0, inspect.stderr
    assert json.loads(inspect.stdout)["status"] == "not_ready"
    assert strict.returncode == 1, strict.stdout
    assert json.loads(strict.stdout)["ready"] is False


def test_readiness_cli_reports_g002_gate_but_strict_stays_closed(
    tmp_path: Path,
) -> None:
    """A passing G002 prerequisite cannot hide the later industrial blockers."""

    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "MEMPLEX_STORAGE_BACKEND": "postgres",
        "MEMPLEX_STORAGE_PATH": "postgresql://memplex@example.invalid/memplex",
        "MEMPLEX_STORAGE_MIGRATION_DSN": "postgresql://migrator@example.invalid/memplex",
        "MEMPLEX_DEPLOYMENT_PROFILE": "production",
        "MEMPLEX_PRINCIPALS_JSON": json.dumps(
            [
                {
                    "credential_id": "ci",
                    "token_sha256": "a" * 64,
                    "tenant_id": "tenant-a",
                    "subject_id": "alice",
                    "workspace_id": "workspace-a",
                }
            ]
        ),
    }
    command = [sys.executable, "-m", "memplex", "--output", "json", "readiness", "--strict"]

    completed = subprocess.run(command, capture_output=True, text=True, env=env, timeout=30, check=False)
    report = json.loads(completed.stdout)
    gate = next(item for item in report["gates"] if item["id"] == "principal_tenant_acl")

    assert completed.returncode == 1, completed.stderr
    assert gate["status"] == "pass"
    assert report["ready"] is False
    assert report["summary"] == {"passed": 3, "failed": 0, "blocked": 7, "total": 10}
