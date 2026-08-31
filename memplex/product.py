"""Productized operator workflows for Memplex.

These helpers keep CLI/MCP surfaces thin while preserving Memplex's core
local-first architecture. They do not add a storage backend, remote embedding
default, hidden ACL layer, or repo-wide indexer.
"""

from __future__ import annotations

import fnmatch
import logging
import os
import tomllib
from datetime import datetime, timedelta, timezone
from importlib.metadata import version
from pathlib import Path
from typing import Any, Iterable, Optional

from memplex.auth import PrincipalRegistry, PrincipalRegistryError
from memplex.config import MemplexConfig, normalize_deployment_contract

logger = logging.getLogger(__name__)


SETUP_PROFILES: dict[str, dict[str, Any]] = {
    "local": {
        "description": "Offline-friendly local defaults: lite storage, local retrieval, no remote embedding default.",
        "auto_recall": True,
        "auto_capture": "auto",
        "review_required": False,
        "remote_embedding_default": False,
    },
    "privacy": {
        "description": "Privacy-first defaults: local retrieval, review-gated capture, no remote providers.",
        "auto_recall": True,
        "auto_capture": "review",
        "review_required": True,
        "remote_embedding_default": False,
    },
    "max-recall": {
        "description": "Higher recall budget with explicit cost/safety visibility.",
        "auto_recall": True,
        "auto_capture": "auto",
        "review_required": False,
        "remote_embedding_default": False,
        "recommended_top_k": 10,
        "recommended_token_budget": 4000,
    },
    "team": {
        "description": "Project memory is shared deliberately; user and agent-local memory stay separated.",
        "auto_recall": True,
        "auto_capture": "review",
        "review_required": True,
        "remote_embedding_default": False,
    },
}

SCOPE_DESCRIPTIONS: dict[str, str] = {
    "session": "Only this agent session/conversation.",
    "project": "The current project path.",
    "user": "User-wide memory for this operator.",
    "agent": "Agent-specific memory such as codex, claude-code, openclaw, or hermes.",
    "global": "Explicitly shared memory. Memplex does not promote data here implicitly.",
}

_INDUSTRIAL_BLOCKED_GATES: tuple[tuple[str, str, str], ...] = (
    (
        "release_supply_chain",
        "Reproducible signed artifacts and clean registry installation gates.",
        "G007",
    ),
    (
        "four_host_e2e",
        "Real Codex, Claude Code, OpenClaw, and Hermes lifecycle matrix.",
        "G008",
    ),
    (
        "capacity_chaos",
        "Production-scale load, soak, chaos, and independent final review.",
        "G009",
    ),
)


def _blocked_industrial_gate(
    gate_id: str, requirement: str, next_goal: str
) -> dict[str, Any]:
    gate: dict[str, Any] = {
        "id": gate_id,
        "status": "blocked",
        "required": True,
        "requirement": requirement,
        "next_goal": next_goal,
    }
    return gate


def _completed_industrial_gate(
    gate_id: str, requirement: str, evidence: str
) -> dict[str, Any]:
    return {
        "id": gate_id,
        "status": "pass",
        "required": True,
        "requirement": requirement,
        "evidence": evidence,
    }


def _backup_restore_dr_gate() -> dict[str, Any]:
    requirement = "Verified backup, restore, PITR, and measured RPO/RTO drills."
    artifact_value = os.environ.get("MEMPLEX_G005_BACKUP_ARTIFACT")
    report_value = os.environ.get("MEMPLEX_G005_DRILL_REPORT")
    if artifact_value is None and report_value is None:
        return _blocked_industrial_gate("backup_restore_dr", requirement, "G005")
    invalid = {
        "id": "backup_restore_dr",
        "status": "fail",
        "required": True,
        "requirement": requirement,
        "next_goal": "G005",
        "evidence": "signed PostgreSQL restore drill invalid",
    }
    if (
        type(artifact_value) is not str
        or not artifact_value.strip()
        or type(report_value) is not str
        or not report_value.strip()
    ):
        return invalid
    try:
        from memplex.backup import (
            drill_result_from_json,
            load_backup_signing_key,
            load_verified_backup_manifest,
        )

        key = load_backup_signing_key()
        artifact = Path(artifact_value)
        manifest = load_verified_backup_manifest(artifact, key)
        report = drill_result_from_json(Path(report_value).read_bytes())
        report.verify(key)
        if (
            manifest.backend != "postgres"
            or report.backup_id != manifest.backup_id
            or report.key_id != manifest.key_id
            or report.data_digest != manifest.payload_sha256
            or not report.industrial_gate_closing
        ):
            return invalid
    except Exception:
        return invalid
    return _completed_industrial_gate(
        "backup_restore_dr",
        requirement,
        "signed PostgreSQL restore drill verified",
    )


def _principal_tenant_acl_gate(profile: str, backend: str) -> dict[str, Any]:
    """Report the G002 contract without disclosing registry configuration.

    Readiness is an operator-facing diagnostic, so an invalid registry must
    fail closed but must not echo JSON, credential identifiers, subjects, or
    parser detail.  The HTTP/CLI/MCP runtime remains responsible for its
    stricter startup-time validation and authentication behavior.
    """

    try:
        registry = PrincipalRegistry.from_environment()
    except PrincipalRegistryError:
        return {
            "id": "principal_tenant_acl",
            "status": "fail",
            "required": True,
            "requirement": "Authenticated principal and tenant authorization across every entry point.",
            "next_goal": "G002-principal-acl",
            "evidence": "principal registry invalid",
        }

    if registry is None:
        evidence = "principal registry missing"
    elif profile != "production" or backend != "postgres":
        evidence = "principal registry configured; production postgres required"
    else:
        return {
            "id": "principal_tenant_acl",
            "status": "pass",
            "required": True,
            "requirement": "Authenticated principal and tenant authorization across every entry point.",
            "next_goal": "G002-principal-acl",
            "evidence": "principal registry configured",
        }

    return {
        "id": "principal_tenant_acl",
        "status": "fail",
        "required": True,
        "requirement": "Authenticated principal and tenant authorization across every entry point.",
        "next_goal": "G002-principal-acl",
        "evidence": evidence,
    }


def _deployment_evidence_binding() -> Any:
    from memplex.readiness_evidence import (
        load_deployment_evidence_binding_from_environment,
    )

    return load_deployment_evidence_binding_from_environment(
        memplex_version=version("memplex")
    )


def _signed_deployment_gate(
    *,
    gate_id: str,
    requirement: str,
    next_goal: str,
    report_env: str,
) -> dict[str, Any]:
    report_value = os.environ.get(report_env)
    key_value = os.environ.get("MEMPLEX_INDUSTRIAL_EVIDENCE_HMAC_KEY")
    key_id_value = os.environ.get("MEMPLEX_INDUSTRIAL_EVIDENCE_KEY_ID")
    if report_value is None and key_value is None and key_id_value is None:
        return _blocked_industrial_gate(gate_id, requirement, next_goal)
    invalid = {
        "id": gate_id,
        "status": "fail",
        "required": True,
        "requirement": requirement,
        "next_goal": next_goal,
        "evidence": "signed current deployment evidence invalid",
    }
    if (
        type(report_value) is not str
        or type(key_value) is not str
        or type(key_id_value) is not str
        or not report_value.strip()
        or not key_value.strip()
        or not key_id_value.strip()
    ):
        return invalid
    try:
        from memplex.readiness_evidence import (
            load_expected_key_id_from_environment,
            load_signing_key_from_environment,
            read_industrial_gate_evidence,
        )

        evidence = read_industrial_gate_evidence(Path(report_value))
        evidence.verify(
            expected_gate_id=gate_id,
            expected_binding=_deployment_evidence_binding(),
            expected_key_id=load_expected_key_id_from_environment(
                "MEMPLEX_INDUSTRIAL_EVIDENCE_KEY_ID"
            ),
            signing_key=load_signing_key_from_environment(
                "MEMPLEX_INDUSTRIAL_EVIDENCE_HMAC_KEY"
            ),
            now=datetime.now(timezone.utc),
            max_age=timedelta(minutes=15),
        )
    except Exception:
        return invalid
    return _completed_industrial_gate(
        gate_id,
        requirement,
        "signed current deployment evidence verified",
    )


def _schema_migrations_atomicity_gate() -> dict[str, Any]:
    return _signed_deployment_gate(
        gate_id="schema_migrations_atomicity",
        requirement="Versioned migrations, atomic storage operations, and concurrency proof.",
        next_goal="G003",
        report_env="MEMPLEX_G003_STORAGE_REPORT",
    )


def _durable_sync_backpressure_gate() -> dict[str, Any]:
    return _signed_deployment_gate(
        gate_id="durable_sync_backpressure",
        requirement="Durable outbox/inbox, gap-free cursors, idempotency, and bounded work.",
        next_goal="G004",
        report_env="MEMPLEX_G004_SYNC_REPORT",
    )


def _operations_slo_gate(config: MemplexConfig) -> dict[str, Any]:
    requirement = "Fail-fast production entry, probes, telemetry, alerts, and SLO evidence."
    report_value = os.environ.get("MEMPLEX_G006_OPERATIONS_REPORT")
    if report_value is None:
        return _blocked_industrial_gate("operations_slo", requirement, "G006-slo")
    invalid = {
        "id": "operations_slo",
        "status": "fail",
        "required": True,
        "requirement": requirement,
        "next_goal": "G006-slo",
        "evidence": "signed operations SLO evidence invalid",
    }
    if type(report_value) is not str or not report_value.strip():
        return invalid
    try:
        from memplex.operations import (
            OperationsReadinessBinding,
            load_operations_report,
            load_operations_signing_key,
        )

        report = load_operations_report(Path(report_value))
        deployment = _deployment_evidence_binding()
        report.verify_readiness(
            load_operations_signing_key(),
            binding=OperationsReadinessBinding(
                deployment_id=deployment.deployment_id,
                source_sha256=deployment.source_sha256,
                artifact_sha256=deployment.artifact_sha256,
                target_identity_sha256=deployment.target_identity_sha256,
                expected_key_id=config.operations.report_key_id,
            ),
        )
    except Exception:
        return invalid
    return _completed_industrial_gate(
        "operations_slo",
        requirement,
        "signed measured operations SLO report verified",
    )


def _release_supply_chain_gate() -> dict[str, Any]:
    requirement = "Reproducible signed artifacts and clean registry installation gates."
    bundle_value = os.environ.get("MEMPLEX_G007_RELEASE_BUNDLE")
    evidence_value = os.environ.get("MEMPLEX_G007_RELEASE_EVIDENCE")
    key_value = os.environ.get("MEMPLEX_RELEASE_EVIDENCE_KEY")
    if bundle_value is None and evidence_value is None and key_value is None:
        return _blocked_industrial_gate("release_supply_chain", requirement, "G007")
    invalid = {
        "id": "release_supply_chain",
        "status": "fail",
        "required": True,
        "requirement": requirement,
        "next_goal": "G007",
        "evidence": "signed release supply-chain evidence invalid",
    }
    if (
        type(bundle_value) is not str
        or type(evidence_value) is not str
        or type(key_value) is not str
        or not bundle_value.strip()
        or not evidence_value.strip()
        or not key_value.strip()
    ):
        return invalid
    try:
        from importlib.metadata import version

        from memplex.release import read_release_evidence_file, verify_release_readiness_evidence

        if len(key_value) != 64:
            return invalid
        signing_key = bytes.fromhex(key_value)
        verify_release_readiness_evidence(
            Path(bundle_value),
            read_release_evidence_file(Path(evidence_value)),
            signing_key=signing_key,
            expected_version=version("memplex"),
        )
    except Exception:
        return invalid
    return _completed_industrial_gate(
        "release_supply_chain",
        requirement,
        "signed immutable release bundle verified",
    )


def _four_host_e2e_gate() -> dict[str, Any]:
    requirement = "Real Codex, Claude Code, OpenClaw, and Hermes lifecycle matrix."
    report_value = os.environ.get("MEMPLEX_G008_HOST_LIFECYCLE_REPORT")
    key_value = os.environ.get("MEMPLEX_HOST_LIFECYCLE_HMAC_KEY")
    key_id_value = os.environ.get("MEMPLEX_HOST_LIFECYCLE_KEY_ID")
    if report_value is None and key_value is None and key_id_value is None:
        return _blocked_industrial_gate("four_host_e2e", requirement, "G008")
    invalid = {
        "id": "four_host_e2e",
        "status": "fail",
        "required": True,
        "requirement": requirement,
        "next_goal": "G008",
        "evidence": "signed four-host lifecycle evidence invalid",
    }
    if (
        type(report_value) is not str
        or type(key_value) is not str
        or type(key_id_value) is not str
        or not report_value.strip()
        or not key_value.strip()
        or not key_id_value.strip()
    ):
        return invalid
    try:
        from memplex.host_lifecycle import (
            HostLifecycleBinding,
            read_host_lifecycle_evidence,
        )
        from memplex.readiness_evidence import (
            load_expected_key_id_from_environment,
        )

        if len(key_value) != 64:
            return invalid
        signing_key = bytes.fromhex(key_value)
        deployment = _deployment_evidence_binding()
        evidence = read_host_lifecycle_evidence(Path(report_value))
        evidence.verify(
            signing_key,
            expected_version=deployment.memplex_version,
            expected_binding=HostLifecycleBinding(
                deployment_id=deployment.deployment_id,
                source_sha256=deployment.source_sha256,
                artifact_sha256=deployment.artifact_sha256,
                target_identity_sha256=deployment.target_identity_sha256,
                expected_key_id=load_expected_key_id_from_environment(
                    "MEMPLEX_HOST_LIFECYCLE_KEY_ID"
                ),
            ),
        )
    except Exception:
        return invalid
    return _completed_industrial_gate(
        "four_host_e2e",
        requirement,
        "signed real four-host lifecycle matrix verified",
    )


def _capacity_chaos_gate() -> dict[str, Any]:
    requirement = "Production-scale load, soak, chaos, and independent final review."
    report_value = os.environ.get("MEMPLEX_G009_CAPACITY_CHAOS_REPORT")
    key_value = os.environ.get("MEMPLEX_CAPACITY_CHAOS_HMAC_KEY")
    if report_value is None and key_value is None:
        return _blocked_industrial_gate("capacity_chaos", requirement, "G009")
    invalid = {
        "id": "capacity_chaos",
        "status": "fail",
        "required": True,
        "requirement": requirement,
        "next_goal": "G009",
        "evidence": "signed capacity and chaos evidence invalid",
    }
    if (
        type(report_value) is not str
        or type(key_value) is not str
        or not report_value.strip()
        or not key_value.strip()
    ):
        return invalid
    try:
        from importlib.metadata import version

        from memplex.capacity_chaos import (
            load_capacity_chaos_signing_key,
            read_capacity_chaos_evidence,
        )

        report = read_capacity_chaos_evidence(Path(report_value))
        report.verify(
            load_capacity_chaos_signing_key(),
            expected_version=version("memplex"),
        )
    except Exception:
        return invalid
    return _completed_industrial_gate(
        "capacity_chaos",
        requirement,
        "signed production-scale capacity and chaos evidence verified",
    )


def industrial_readiness_report(config: MemplexConfig) -> dict[str, Any]:
    """Return the fail-closed industrial readiness contract and gate state."""

    profile, backend = normalize_deployment_contract(config)
    split_postgres_dsn_configured = False
    if profile == "production" and backend == "postgres":
        try:
            from memplex.config import postgres_dsn_identity

            application_identity = postgres_dsn_identity(config.storage.path)
            migration_identity = postgres_dsn_identity(config.storage.migration_dsn)
            split_postgres_dsn_configured = application_identity != migration_identity
        except (ImportError, TypeError, ValueError):
            split_postgres_dsn_configured = False
    storage_evidence = (
        "storage.backend=postgres; application and migration DSNs configured"
        if split_postgres_dsn_configured
        else "storage.backend=%s; production requires postgres plus application and migration DSNs"
        % backend
    )
    gates: list[dict[str, Any]] = [
        {
            "id": "production_profile",
            "status": "pass" if profile == "production" else "fail",
            "required": True,
            "evidence": f"deployment.profile={profile}",
        },
        {
            "id": "production_storage",
            "status": "pass" if split_postgres_dsn_configured else "fail",
            "required": True,
            "evidence": storage_evidence,
        },
        _principal_tenant_acl_gate(profile, backend),
        _schema_migrations_atomicity_gate(),
        _durable_sync_backpressure_gate(),
    ]
    gates.append(_backup_restore_dr_gate())
    gates.append(_operations_slo_gate(config))
    gates.append(_release_supply_chain_gate())
    gates.append(_four_host_e2e_gate())
    gates.append(_capacity_chaos_gate())
    gates.extend(
        _blocked_industrial_gate(gate_id, requirement, next_goal)
        for gate_id, requirement, next_goal in _INDUSTRIAL_BLOCKED_GATES
        if gate_id not in {"release_supply_chain", "four_host_e2e", "capacity_chaos"}
    )

    counts = {
        "passed": sum(1 for gate in gates if gate["status"] == "pass"),
        "failed": sum(1 for gate in gates if gate["status"] == "fail"),
        "blocked": sum(1 for gate in gates if gate["status"] == "blocked"),
    }
    ready = all(gate["status"] == "pass" for gate in gates)
    return {
        "schema_version": 1,
        "status": "ready" if ready else "not_ready",
        "ready": ready,
        "maturity": "industrial" if ready else "developer_preview",
        "deployment_profile": profile,
        "production_topology": {
            "application": "one_or_more_stateless_memplex_services",
            "storage_backend": "postgres",
            "lite": {
                "production_supported": False,
                "max_processes": 1,
                "purpose": "single-process local development and tests",
            },
        },
        "summary": {**counts, "total": len(gates)},
        "blocking_gate_ids": [gate["id"] for gate in gates if gate["status"] != "pass"],
        "gates": gates,
        "boundary": (
            "Unit and integration test counts do not establish industrial readiness; "
            "every required gate needs machine evidence."
        ),
    }


def migration_verification_report(store: Any) -> dict[str, Any]:
    """Describe an already-ready local PostgreSQL store without attesting it.

    The report is deliberately an unsigned convenience diagnostic.  It does
    not open a service, issue synchronization calls, or substitute for the
    independent industrial-readiness evidence required by later goals.
    """
    from memplex.storage import _unwrap_postgres_for_migration
    from memplex.storage.migrations import discover_migrations
    from memplex.storage.pool import validate_ready_postgres_pool

    local = _unwrap_postgres_for_migration(store)
    try:
        ready_pool = validate_ready_postgres_pool(getattr(local, "_ready_pool", None))
    except Exception as exc:
        _ = exc
        raise ValueError("PostgreSQL store has no verified storage readiness seal") from None
    target = getattr(ready_pool, "target", None)
    request = getattr(ready_pool, "request", None)
    capability = getattr(ready_pool, "status", None)
    database = getattr(target, "database", None)
    schema = getattr(target, "schema", None)
    capability_state = getattr(capability, "state", None)
    capability_dim = getattr(capability, "dim", None)
    request_policy = getattr(request, "policy", None)
    if (
        type(database) is not str
        or not database
        or type(schema) is not str
        or not schema
        or capability_state not in {"ready", "degraded", "disabled"}
        or type(capability_dim) is not int
        or request_policy not in {"required", "best_effort", "disabled"}
    ):
        raise ValueError("PostgreSQL store has no verified storage readiness seal")
    known_version = len(discover_migrations())
    return {
        "schema_version": 1,
        "signed": False,
        "local_diagnostic_only": True,
        "industrial_gate_closing": False,
        "status": "diagnostic_only",
        "schema": {
            "status": "verified_by_ready_pool",
            "database": database,
            "schema": schema,
        },
        "ledger": {
            "status": "verified_by_ready_pool",
            "current_version": known_version,
            "known_version": known_version,
        },
        "capability": {
            "state": capability_state,
            "dim": capability_dim,
        },
        "command": {
            "schema_version": 1,
            "surface": "memplex storage migration",
            "version": "v1",
        },
        "test_references": [
            "tests/test_storage_migrations.py",
            "tests/test_cli_migrations.py",
            "tests/test_industrial_readiness.py",
        ],
        "limitations": [
            "本地 unsigned 诊断不能关闭 industrial readiness gate。",
            "报告不包含 DSN、token、SQL、绑定参数或业务 payload。",
        ],
    }

PRIVATE_CORPUS_PATTERNS = (
    ".codex",
    ".agents",
    ".claude",
    ".git",
    ".gitnexus",
    ".ssh",
    ".aws",
    ".kube",
    ".gnupg",
    ".env",
    ".env.*",
    "*secret*",
    "*credential*",
    "*token*",
    "id_rsa",
    "id_ed25519",
    "id_ecdsa",
    "*.pem",
    "*.key",
    ".netrc",
    ".npmrc",
)


def setup_profile(name: Optional[str]) -> Optional[dict[str, Any]]:
    """Return a named setup profile, or ``None`` when no profile was requested."""

    if name is None:
        return None
    if name not in SETUP_PROFILES:
        known = ", ".join(sorted(SETUP_PROFILES))
        raise ValueError(f"Unknown setup profile {name!r}. Known profiles: {known}")
    return {"name": name, **SETUP_PROFILES[name]}


_REMOTE_EMBEDDING_PREFIXES = ("hf:", "openai:", "anthropic:")


def apply_profile(config: MemplexConfig, name: str) -> dict[str, Any]:
    """Apply a setup profile's concrete settings to *config* in place.

    Profile keys that map onto a real :class:`MemplexConfig` field are
    written into *config*:

    - ``remote_embedding_default=False`` resets a remote embedding model
      (``hf:``/``openai:``/``anthropic:`` prefix) back to the local
      ``"default"`` so the profile's offline boundary actually holds.
    - ``recommended_token_budget`` sets
      ``config.retrieval.default_max_tokens``.

    Keys with no config counterpart today (``auto_recall``,
    ``auto_capture``, ``review_required``, ``recommended_top_k``) are
    returned under ``declarative`` for the caller (CLI setup / agent
    runtime) to honour; they are not silently dropped.

    Returns a report dict::

        {
            "profile": <profile dict>,
            "applied": {"<dotted config path>": <new value>, ...},
            "declarative": {"<key>": <value>, ...},
        }

    Raises ``ValueError`` for an unknown profile name.
    """

    profile = setup_profile(name)
    if profile is None:
        raise ValueError("apply_profile requires a profile name.")

    applied: dict[str, Any] = {}
    declarative: dict[str, Any] = {}

    if profile.get("remote_embedding_default") is False:
        model = config.embedding.model
        if model.startswith(_REMOTE_EMBEDDING_PREFIXES):
            config.embedding.model = "default"
            applied["embedding.model"] = "default"

    token_budget = profile.get("recommended_token_budget")
    if token_budget is not None:
        config.retrieval.default_max_tokens = int(token_budget)
        applied["retrieval.default_max_tokens"] = config.retrieval.default_max_tokens

    for key in ("auto_recall", "auto_capture", "review_required", "recommended_top_k"):
        if key in profile:
            declarative[key] = profile[key]

    return {"profile": profile, "applied": applied, "declarative": declarative}


def scope_catalog() -> dict[str, Any]:
    """Return the visibility-first scope vocabulary."""

    return {
        "boundary": "Visibility map only; not an ACL engine.",
        "scopes": SCOPE_DESCRIPTIONS,
    }


def scope_explain(
    *,
    agent: str,
    user_id: Optional[str],
    session_id: str,
    project_path: Optional[str],
    storage_namespace: str,
) -> dict[str, Any]:
    """Explain the namespace metadata a runtime will use."""

    from memplex.adapters.agent_runtime import describe_memory_scope

    contract = describe_memory_scope(
        agent=agent,
        user_id=user_id,
        session_id=session_id,
        project_path=project_path,
        storage_namespace=storage_namespace,
    )
    return {
        **contract,
        "scope_boundary": "Visibility-first metadata projection; not an ACL engine; enforcement remains in runtime/store filters.",
        "read_visibility": contract["visibility"]["read_order"],
        "write_visibility": contract["visibility"]["supported"],
        # Compatibility alias retained for callers that predate the OR-ed
        # read_namespace_filters contract.
        "namespace_filter": contract["write_namespace"],
        "catalog": SCOPE_DESCRIPTIONS,
    }


def run_agent_diagnostics(
    service: Any,
    *,
    agent: str = "codex",
    target_dir: Optional[str | Path] = None,
    user_id: Optional[str] = None,
    session_id: str = "default",
    project_path: Optional[str | Path] = None,
) -> dict[str, Any]:
    """Return one read-only integration snapshot for an agent host."""

    from memplex.adapters.agent_installer import inspect_agent_installation
    from memplex.adapters.agent_runtime import (
        DEFAULT_MEMORY_VISIBILITY,
        MEMORY_VISIBILITIES,
        get_agent_manifest,
    )

    manifest = get_agent_manifest(agent)
    installation = inspect_agent_installation(manifest["name"], target_dir=target_dir)
    installed_identity = installation["identity"]
    resolved_user = user_id or installed_identity.get("user_id")
    resolved_project = project_path or installed_identity.get("project_path")
    if user_id is not None or project_path is not None:
        identity_source = "arguments"
    elif installed_identity.get("user_id") or installed_identity.get("project_path"):
        identity_source = "installed"
    else:
        identity_source = "runtime_default"

    configured_visibility = str(
        installation.get("configured_visibility") or DEFAULT_MEMORY_VISIBILITY
    ).lower()
    visibility_fallback = configured_visibility not in MEMORY_VISIBILITIES
    effective_visibility = (
        DEFAULT_MEMORY_VISIBILITY if visibility_fallback else configured_visibility
    )
    scope = scope_explain(
        agent=manifest["name"],
        user_id=resolved_user,
        session_id=session_id,
        project_path=str(resolved_project) if resolved_project is not None else None,
        storage_namespace=service.storage_namespace(),
    )
    identity = {**scope["identity"], "source": identity_source}
    return {
        "schema_version": 1,
        "selected_host": manifest["name"],
        "manifest": manifest,
        "identity": identity,
        "workspace": {
            "workspace_id": identity["workspace_id"],
            "project_path": identity["project_path"],
            "storage_namespace": identity["storage_namespace"],
        },
        "visibility": {
            "configured": configured_visibility,
            "effective": effective_visibility,
            "default": DEFAULT_MEMORY_VISIBILITY,
            "supported": sorted(MEMORY_VISIBILITIES),
            "fallback_applied": visibility_fallback,
        },
        "scope": scope,
        "paths": installation["paths"],
        "managed_state": installation["install_state"],
        "install_state": installation,
    }


def scope_preview(
    service: Any,
    namespace_filter: dict[str, Any] | list[dict[str, Any]],
    *,
    limit: int = 10,
    scan_limit: int = 1_000,
) -> dict[str, Any]:
    """Count and sample a bounded window without computing a corpus total."""

    selected_limit = min(100, max(0, int(limit)))
    selected_scan_limit = min(1_000, max(1, int(scan_limit)))

    try:
        funcs = service.store.list_functions(limit=selected_scan_limit)
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc),
            "namespace_filter": namespace_filter,
        }

    filters = namespace_filter if isinstance(namespace_filter, list) else [namespace_filter]
    matches = []
    for func in funcs:
        attrs = getattr(func, "attributes", {}) or {}
        if any(all(attrs.get(key) == value for key, value in branch.items()) for branch in filters):
            matches.append(
                {
                    "id": func.id,
                    "name": func.name,
                    "memory_type": getattr(func, "memory_type", "function"),
                    "domain": func.domain,
                }
            )

    return {
        "status": "ok",
        "boundary": "Preview only; does not grant or change access.",
        "count_boundary": "Counts describe only the bounded scan window, not the corpus total.",
        "namespace_filter": namespace_filter,
        "filter_mode": "or" if len(filters) > 1 else "single",
        "scan_limit": selected_scan_limit,
        "scanned_functions": len(funcs),
        "scan_limit_reached": len(funcs) >= selected_scan_limit,
        "matched_in_scan": len(matches),
        "sample_limit": selected_limit,
        "sample": matches[:selected_limit],
    }


def policy_show(config: MemplexConfig, *, agent: str = "codex") -> dict[str, Any]:
    """Return the recall/capture policy Memplex will use by default."""

    embedding_model = config.embedding.model
    remote_embedding = embedding_model.startswith(("hf:", "openai:", "anthropic:"))
    return {
        "agent": agent,
        "auto_recall": True,
        "auto_capture": "auto",
        "max_injected_tokens": config.retrieval.default_max_tokens,
        "skill_token_budget": config.retrieval.skill_max_tokens,
        "injection_scan_enabled": config.retrieval.injection_scan_enabled,
        "embedding": {
            "model": embedding_model,
            "remote_default": remote_embedding,
            "boundary": "Remote embeddings are opt-in; Memplex does not require them.",
        },
        "reranker": {
            "weights": dict(config.reranker.weights),
            "cross_encoder_enabled": config.reranker.cross_encoder_enabled,
            "cross_encoder_model": config.reranker.cross_encoder_model,
        },
        "scope_boundary": "Policy display does not mutate scope; not an ACL engine.",
    }


def _normalise_manifest(raw: dict[str, Any], manifest_path: Path) -> dict[str, Any]:
    corpus = raw.get("corpus", raw)
    include = corpus.get("include", [])
    deny = corpus.get("deny", corpus.get("exclude", []))
    if isinstance(include, str):
        include = [include]
    if isinstance(deny, str):
        deny = [deny]
    root_value = corpus.get("root", ".")
    root = (manifest_path.parent / root_value).resolve()
    return {
        "name": corpus.get("name", manifest_path.stem),
        "scope": corpus.get("scope", "project"),
        "root": root,
        "include": list(include),
        "deny": list(PRIVATE_CORPUS_PATTERNS) + list(deny),
        "read_only": bool(corpus.get("read_only", True)),
    }


def load_corpus_manifest(path: str | Path) -> dict[str, Any]:
    """Load a TOML corpus manifest."""

    manifest_path = Path(path).expanduser().resolve()
    raw = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = _normalise_manifest(raw, manifest_path)
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def _is_denied(path: Path, root: Path, patterns: Iterable[str]) -> bool:
    rel_path = path.relative_to(root)
    rel = rel_path.as_posix()
    parts = rel_path.parts
    name = rel_path.name.lower()

    private_dirs = {".codex", ".agents", ".claude", ".git", ".gitnexus", ".ssh", ".aws", ".kube", ".gnupg"}
    if any(part.lower() in private_dirs for part in parts):
        return True

    if name == ".env" or name.startswith(".env."):
        return True

    sensitive_name_fragments = ("secret", "credential", "token")
    if any(fragment in name for fragment in sensitive_name_fragments):
        return True

    # SSH/cloud key material and credential files (2026-08 review: these
    # carry no secret-ish name fragment yet must never enter the corpus).
    if name in {"id_rsa", "id_ed25519", "id_ecdsa", ".netrc", ".npmrc"}:
        return True
    if name.endswith((".pem", ".key")):
        return True

    return any(
        fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(rel_path.name, pattern)
        for pattern in patterns
    )


def corpus_preview(path: str | Path, *, limit: int = 100) -> dict[str, Any]:
    """Preview files selected by a canonical corpus manifest."""

    manifest = load_corpus_manifest(path)
    root: Path = manifest["root"]
    included: list[dict[str, Any]] = []
    denied: list[dict[str, Any]] = []
    seen: set[Path] = set()

    for pattern in manifest["include"]:
        for match in root.glob(pattern):
            if not match.is_file():
                continue
            resolved = match.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            if not resolved.is_relative_to(root):
                denied.append({"path": str(resolved), "reason": "outside_root"})
                continue
            if _is_denied(resolved, root, manifest["deny"]):
                denied.append(
                    {
                        "path": str(resolved.relative_to(root)),
                        "reason": "private_or_denied",
                    }
                )
                continue
            included.append(
                {
                    "path": str(resolved.relative_to(root)),
                    "bytes": resolved.stat().st_size,
                }
            )

    return {
        "status": "ok",
        "boundary": "Opt-in manifest preview; no repo-wide implicit indexing.",
        "manifest": {
            "name": manifest["name"],
            "scope": manifest["scope"],
            "root": str(root),
            "include": manifest["include"],
            "deny": manifest["deny"],
            "read_only": manifest["read_only"],
        },
        "included_count": len(included),
        "denied_count": len(denied),
        "included": included[:limit],
        "denied": denied[:limit],
    }


def corpus_index(service: Any, path: str | Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Index manifest-selected files as reviewable canonical corpus memories."""

    preview = corpus_preview(path, limit=100000)
    if dry_run:
        # status must come AFTER **preview so it is not overwritten by
        # preview's own "status": "ok" field.
        return {**preview, "status": "dry_run"}

    manifest = load_corpus_manifest(path)
    root: Path = manifest["root"]
    indexed: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []
    for item in preview["included"]:
        source_path = root / item["path"]
        try:
            text = source_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            # A single unreadable/non-UTF-8 file must not abort the whole index.
            logger.warning("corpus_index: skipping unreadable file %s: %s", item["path"], exc)
            skipped.append({"path": item["path"], "reason": str(exc)})
            continue
        payload = (
            "Canonical Memplex corpus source.\n"
            f"Corpus: {manifest['name']}\n"
            f"Scope: {manifest['scope']}\n"
            f"Source Path: {item['path']}\n\n"
            f"{text}"
        )
        result = service.write_text(payload, source_type="file")
        attrs = {
            "memplex_corpus": "true",
            "memplex_corpus_name": manifest["name"],
            "memplex_corpus_scope": manifest["scope"],
            "memplex_source_path": item["path"],
            "memplex_manifest_path": manifest["manifest_path"],
            "memplex_canonical_read_only": str(manifest["read_only"]).lower(),
        }
        annotated = service.annotate_memories(
            [func.id for func in result.functions],
            attributes=attrs,
            needs_review=True,
        )
        for stored in annotated:
            indexed.append({"id": stored.id, "name": stored.name, "source_path": item["path"]})
    return {
        "status": "indexed",
        "boundary": "Canonical sources were indexed as read-only, reviewable memory; source files were not mutated.",
        "indexed_count": len(indexed),
        "indexed": indexed,
        "skipped_count": len(skipped),
        "skipped": skipped,
        "denied_count": preview["denied_count"],
        "denied": preview["denied"],
    }


def corpus_recall(
    service: Any, query: str, *, top_k: int = 10, max_tokens: int = 4000
) -> dict[str, Any]:
    """Recall only memories stamped as canonical corpus entries."""

    result = service.query(
        query,
        top_k=max(top_k * 3, top_k),
        max_tokens=max_tokens,
        namespace_filter={"memplex_corpus": "true"},
        explain=True,
    )
    entries = []
    for item in result.results:
        func = service.store.get(item.func_id)
        attrs = getattr(func, "attributes", {}) if func is not None else {}
        if attrs.get("memplex_corpus") != "true":
            continue
        entries.append(
            {
                "id": item.func_id,
                "name": item.name,
                "relevance": item.relevance_score,
                "summary": item.summary,
                "source_path": attrs.get("memplex_source_path"),
                "corpus": attrs.get("memplex_corpus_name"),
            }
        )
        if len(entries) >= top_k:
            break
    return {
        "total": len(entries),
        "results": entries,
        "explanation": result.explanation,
    }


def run_doctor(
    service: Any,
    *,
    agent: str = "codex",
    profile: Optional[str] = None,
    smoke: bool = False,
    target_dir: Optional[str | Path] = None,
    user_id: Optional[str] = None,
    session_id: str = "default",
    project_path: Optional[str | Path] = None,
) -> dict[str, Any]:
    """Run productized readiness checks."""

    from memplex.adapters.agent_runtime import get_agent_manifest

    checks: list[dict[str, Any]] = []
    health = service.health()
    checks.append(
        {
            "name": "service_health",
            "status": "pass" if health.get("status") in {"healthy", "warning"} else "fail",
            "details": health,
        }
    )

    diagnostics: Optional[dict[str, Any]] = None
    try:
        manifest = get_agent_manifest(agent)
        checks.append(
            {
                "name": "agent_manifest",
                "status": "pass",
                "details": {
                    "agent": manifest["name"],
                    "schema_version": manifest["schema_version"],
                    "hook_events": manifest["hook_events"],
                    "integration_modes": manifest["integration_modes"],
                    "tools": manifest["tools"],
                    "memory_contract": manifest["memory_contract"],
                },
            }
        )
    except Exception as exc:
        checks.append({"name": "agent_manifest", "status": "fail", "error": str(exc)})
    else:
        try:
            diagnostics = run_agent_diagnostics(
                service,
                agent=agent,
                target_dir=target_dir,
                user_id=user_id,
                session_id=session_id,
                project_path=project_path,
            )
        except Exception as exc:
            checks.append(
                {
                    "name": "agent_installation",
                    "status": "fail",
                    "error": str(exc),
                }
            )
        else:
            installation = diagnostics["install_state"]
            checks.append(
                {
                    "name": "agent_installation",
                    "status": "pass" if installation["status"] == "healthy" else "warning",
                    "details": installation,
                }
            )
            checks.append(
                {
                    "name": "memory_scope_contract",
                    "status": "warning"
                    if diagnostics["identity"]["user_id"] == "default"
                    else "pass",
                    "details": diagnostics["scope"],
                }
            )

    if profile is not None:
        checks.append(
            {
                "name": "setup_profile",
                "status": "pass",
                "details": setup_profile(profile),
            }
        )

    policy = service.policy(agent=agent)
    checks.append(
        {
            "name": "offline_first_boundary",
            "status": "pass",
            "details": {
                "embedding_model": policy["embedding"]["model"],
                "remote_default": policy["embedding"]["remote_default"],
                "boundary": "Remote embeddings are optional and never required by doctor.",
            },
        }
    )

    if smoke:
        canary = "memplex-doctor-smoke-token"
        captured_ids: list[str] = []
        try:
            result = service.write_text(f"{canary}: doctor smoke capture and recall.")
            captured_ids = [func.id for func in result.functions]
            query = service.query(canary, top_k=3, explain=True)
            found = any(canary in item.summary for item in query.results)
            details = {
                "captured": len(result.functions),
                "recalled": found,
                "explanation": query.explanation,
            }
            status = "pass" if found else "fail"
        except Exception as exc:
            details = {"error": str(exc), "captured_ids": captured_ids}
            status = "fail"
        finally:
            for memory_id in captured_ids:
                try:
                    service.delete(memory_id)
                except Exception:
                    logger.debug("Failed to clean doctor smoke memory %s", memory_id)
        checks.append(
            {
                "name": "capture_recall_smoke",
                "status": status,
                "details": details,
            }
        )

    # Congestion / scalability check: examine sync health indicators and
    # advise when the system is approaching scaling limits.
    sync_info = health.get("sync", {})
    if sync_info.get("enabled"):
        sse_subs = sync_info.get("sse_subscribers", 0)
        push_fails = sync_info.get("push_failures", 0)
        pending = sync_info.get("pending_push_tasks", 0)
        advice: list = []
        if sse_subs > 400:
            advice.append(
                "SSE connections near limit (" + str(sse_subs) + "/500). "
                "Deploy Redis (MEMPLEX_REDIS_URL) for multi-worker SSE scaling."
            )
        if push_fails > 10:
            advice.append(
                "High push failure count (" + str(push_fails) + "). "
                "Check server connectivity or increase MEMPLEX_SYNC_PULL_INTERVAL."
            )
        if pending > 20:
            advice.append(
                "Push queue backlog (" + str(pending) + " pending). "
                "Consider a read-replica (MEMPLEX_READ_URL) to reduce write contention."
            )
        checks.append(
            {
                "name": "congestion_check",
                "status": "warning" if advice else "pass",
                "details": {
                    "sse_subscribers": sse_subs,
                    "push_failures": push_fails,
                    "pending_push_tasks": pending,
                    "advice": advice,
                },
            }
        )

    failed = [check for check in checks if check["status"] == "fail"]
    status = "pass" if not failed else "fail"
    return {
        "status": status,
        "agent": agent,
        "profile": setup_profile(profile),
        "agent_diagnostics": diagnostics,
        "checks": checks,
        "next_steps": []
        if not failed
        else ["Run memplex doctor --agent <agent> --smoke after fixing failed checks."],
    }


def lifecycle_counts(service: Any) -> dict[str, int]:
    """Return derived lifecycle labels without changing storage schema."""

    counts = {"working": 0, "trusted": 0, "project": 0, "archived": 0, "blocked": 0}
    try:
        funcs = service.store.list_functions(limit=100000)
    except Exception:
        return counts
    for func in funcs:
        attrs = getattr(func, "attributes", {}) or {}
        if getattr(func, "needs_review", False):
            counts["blocked"] += 1
        elif attrs.get("memplex_corpus") == "true":
            counts["project"] += 1
        elif getattr(func, "access_count", 0) > 0:
            counts["trusted"] += 1
        else:
            counts["working"] += 1
    return counts


def operator_report(service: Any, *, agent: str = "codex") -> dict[str, Any]:
    """Generate a local operator report."""

    pending = service.get_pending_reviews(limit=100)
    return {
        "health": service.health(),
        "stats": service.stats(),
        "policy": service.policy(agent=agent),
        "scope_catalog": scope_catalog(),
        "pending_reviews": len(pending),
        "lifecycle": {
            "boundary": "Derived labels only; not a canonical storage schema.",
            "counts": lifecycle_counts(service),
        },
    }
