"""Memplex CLI -- command-line interface using argparse.

Usage::

    memplex query "login function"
    memplex write --text "some observation"
    memplex write --file ./notes.txt
    memplex write --url https://example.com/doc
    memplex get func_abc123
    memplex delete func_abc123
    memplex feedback func_abc123 --role trigger --index 0 --verdict correct
    memplex pending
    memplex compact --scope project
    memplex health
    memplex stats
    memplex setup            # Install into detected local agents
    memplex install --agent codex
    memplex uninstall --agent openclaw
    memplex agent install --agent all
    memplex agent uninstall --agent openclaw
    memplex unsetup          # Uninstall Claude Code plugin
    memplex benchmark list   # List benchmark datasets (source checkout only)
    memplex benchmark run --dataset locomo --synthetic --top-k 10

Global options::

    --config <path>     Path to config YAML file
    --output json|table Output format (default: table)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import asdict
from importlib.metadata import version
from pathlib import Path
from typing import Any, Optional, Sequence

from memplex.adapters._shared import dataclass_to_dict as _dataclass_to_dict

# ── Helpers ─────────────────────────────────────────────────────────


def _make_service(config_path: Optional[str] = None):
    """Create and return a MemplexService instance."""
    from memplex.config import load_config
    from memplex.service import MemplexService

    config = load_config(path=config_path)
    return MemplexService(config=config)


class _AuthorizedService:
    """Request-scoped service facade for a single CLI invocation.

    CLI arguments are intentionally never consulted for identity.  The facade
    injects the context established from the process environment into every
    public service method that accepts ``authorization``.  This also keeps
    higher-level CLI helpers (for example corpus commands) from accidentally
    reintroducing an unscoped service call.
    """

    def __init__(self, service: Any, authorization: Any) -> None:
        self._service = service
        self._authorization = authorization

    @property
    def authorization(self) -> Any:
        """Expose the adapter-established context to the agent runtime only."""
        return self._authorization

    @property
    def store(self) -> Any:
        scoped = getattr(self._service, "_store_for", None)
        store = scoped(self._authorization) if callable(scoped) else self._service.store
        return _AuthorizedStore(store, self._service, self._authorization)

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._service, name)
        if not callable(attribute):
            return attribute
        try:
            import inspect

            accepts_authorization = "authorization" in inspect.signature(attribute).parameters
        except (TypeError, ValueError):
            accepts_authorization = False
        if not accepts_authorization:
            return attribute

        def _bound(*args: Any, **kwargs: Any) -> Any:
            if "authorization" in kwargs:
                raise TypeError("CLI authorization is adapter-established and cannot be overridden")
            return attribute(*args, authorization=self._authorization, **kwargs)

        return _bound


class _AuthorizedStore:
    """Read-only visibility filter for legacy helpers that access ``store``.

    The service already scopes its own public methods.  This small facade
    closes the remaining adapter/product-helper escape hatch on Lite while
    retaining the PostgreSQL authorized facade beneath it.
    """

    def __init__(self, store: Any, service: Any, authorization: Any) -> None:
        self._store = store
        self._service = service
        self._authorization = authorization

    def _visible(self, node: Any) -> Any:
        predicate = getattr(self._service, "_is_node_visible", None)
        if node is None or not callable(predicate) or not predicate(node, self._authorization):
            return None
        return node

    def get(self, node_id: str) -> Any:
        return self._visible(self._store.get(node_id))

    def get_fact(self, node_id: str) -> Any:
        getter = getattr(self._store, "get_fact", None)
        return self._visible(getter(node_id)) if callable(getter) else None

    def get_preference(self, node_id: str) -> Any:
        getter = getattr(self._store, "get_preference", None)
        return self._visible(getter(node_id)) if callable(getter) else None

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._store, name)
        if not callable(attribute) or not name.startswith("list_"):
            return attribute

        def _filtered(*args: Any, **kwargs: Any) -> Any:
            values = attribute(*args, **kwargs)
            return [value for value in values if self._visible(value) is not None]

        return _filtered


def _cli_authorization(config_path: Optional[str] = None, *, agent_id: str = "cli"):
    """Resolve the sole trusted identity source for CLI memory commands.

    A configured principal registry is authoritative.  Production therefore
    requires both that registry and an environment-held opaque credential;
    legacy shared secrets deliberately cannot unlock this boundary.
    """
    from memplex.auth import local_development_context, resolve_environment_authorization
    from memplex.config import load_config

    config = load_config(path=config_path)
    profile = str(getattr(config.deployment, "profile", "development")).strip().lower()
    context = resolve_environment_authorization(
        agent_id=agent_id,
        session_id=os.environ.get("MEMPLEX_SESSION_ID", ""),
        provenance={"transport": "cli"},
        require_registry=profile == "production",
    )
    return context or local_development_context()


def _make_authorized_service(
    config_path: Optional[str] = None, *, agent_id: str = "cli"
) -> tuple[Any, _AuthorizedService]:
    """Authorize before constructing a service, then return its scoped facade."""
    authorization = _cli_authorization(config_path, agent_id=agent_id)
    service = _make_service(config_path)
    return service, _AuthorizedService(service, authorization)


def _fmt(data, output: str) -> str:
    """Format *data* for the chosen output mode."""
    if output == "json":
        return json.dumps(data, indent=2, default=str, ensure_ascii=False)

    # table / plain text
    if isinstance(data, list):
        if not data:
            return "(empty)"
        lines = []
        for item in data:
            if isinstance(item, dict):
                lines.append(_dict_to_table(item))
            else:
                lines.append(str(item))
        return "\n---\n".join(lines)

    if isinstance(data, dict):
        return _dict_to_table(data)

    return str(data)


def _dict_to_table(d: dict, indent: int = 0) -> str:
    """Recursively format a dict as indented key-value lines."""
    prefix = "  " * indent
    lines = []
    for k, v in d.items():
        if isinstance(v, dict):
            lines.append(f"{prefix}{k}:")
            lines.append(_dict_to_table(v, indent + 1))
        elif isinstance(v, list):
            lines.append(f"{prefix}{k}:")
            for item in v:
                if isinstance(item, dict):
                    lines.append(_dict_to_table(item, indent + 1))
                else:
                    lines.append(f"{prefix}  - {item}")
        else:
            lines.append(f"{prefix}{k}: {v}")
    return "\n".join(lines)


def _result_to_dict(result) -> dict:
    """Convert a SearchResult / QueryResult / dataclass to a dict."""
    if hasattr(result, "__dataclass_fields__"):
        return asdict(result)
    if isinstance(result, dict):
        return result
    return {"value": str(result)}


# ── Command implementations ────────────────────────────────────────


def cmd_query(args: argparse.Namespace) -> int:
    """Execute a memory query."""
    raw_service, svc = _make_authorized_service(getattr(args, "config", None))
    try:
        result = svc.query(
            text=args.text,
            top_k=getattr(args, "top_k", 10),
            max_tokens=getattr(args, "max_tokens", 4000),
            explain=getattr(args, "explain", False),
        )

        out = []
        for r in result.results:
            out.append(
                {
                    "id": r.func_id,
                    "name": r.name,
                    "relevance": round(r.relevance_score, 4),
                    "summary": r.summary,
                    "scope": r.domain,
                    # Backfilled per-result by the service when max_tokens > 0;
                    # otherwise fall back to the same summary-length formula.
                    "est_tokens": r.token_estimate or (len(r.summary) // 4 + 1),
                }
            )

        payload = {
            "total": len(out),
            "scope": result.scope.value if hasattr(result.scope, "value") else str(result.scope),
            "latency_ms": result.latency_ms,
            "tokens_used": result.tokens_used,
            "max_tokens": result.max_tokens,
            "truncated": result.truncated,
            "results": out,
        }
        if getattr(args, "explain", False):
            payload["explanation"] = result.explanation
        if not out:
            payload["hint"] = (
                "No memories found. Try 'memplex write --text \"...\"' to add "
                "a memory, or 'memplex stats' to see the total count."
            )
        print(_fmt(payload, args.output))
        return 0
    finally:
        raw_service.stop()


def cmd_write(args: argparse.Namespace) -> int:
    """Write new content into memory."""
    raw_service, svc = _make_authorized_service(getattr(args, "config", None))
    try:
        if args.text:
            content = args.text
            source_type = "text"
        elif args.file:
            with open(args.file, "r", encoding="utf-8") as f:
                content = f.read()
            source_type = "file"
        elif args.url:
            content = args.url
            source_type = "url"
        else:
            print("Error: provide --text, --file, or --url", file=sys.stderr)
            return 1

        result = svc.write_text(text=content, source_type=source_type)

        out = {
            "functions_extracted": len(result.functions),
            "edges": len(result.graph.edges),
            "function_ids": [f.id for f in result.functions],
        }
        print(_fmt(out, args.output))
        return 0
    finally:
        raw_service.stop()


def cmd_observations(args: argparse.Namespace) -> int:
    """List captured observation events with token estimates."""
    raw_service, svc = _make_authorized_service(getattr(args, "config", None))
    try:
        observations = svc.list_observations(
            category=getattr(args, "category", None),
            limit=getattr(args, "limit", 100),
        )
        out = []
        for obs in observations:
            summary = obs.context or obs.event or ""
            out.append(
                {
                    "id": obs.id,
                    "category": obs.category,
                    "event": obs.event,
                    "actor": obs.actor,
                    "observed_at": obs.observed_at,
                    # Same ~4 chars/token estimate as query/search results.
                    "est_tokens": len(summary) // 4 + 1,
                    "summary": summary[:200],
                }
            )
        print(_fmt({"total": len(out), "observations": out}, args.output))
        return 0
    finally:
        raw_service.stop()


def cmd_get(args: argparse.Namespace) -> int:
    """Retrieve a single memory by ID."""
    raw_service, svc = _make_authorized_service(getattr(args, "config", None))
    try:
        func = svc.get(args.memory_id)
        if func is None:
            print(f"Memory not found: {args.memory_id}", file=sys.stderr)
            return 1

        print(_fmt(_dataclass_to_dict(func), args.output))
        return 0
    finally:
        raw_service.stop()


def cmd_delete(args: argparse.Namespace) -> int:
    """Delete a memory by ID."""
    raw_service, svc = _make_authorized_service(getattr(args, "config", None))
    try:
        svc.delete(args.memory_id)
        print(_fmt({"status": "deleted", "id": args.memory_id}, args.output))
        return 0
    finally:
        raw_service.stop()


def cmd_feedback(args: argparse.Namespace) -> int:
    """Submit feedback for a memory field value."""
    raw_service, svc = _make_authorized_service(getattr(args, "config", None))
    try:
        svc.submit_feedback(
            memory_id=args.memory_id,
            field_role=args.role,
            value_index=args.index,
            verdict=args.verdict,
        )
        print(
            _fmt(
                {
                    "status": "recorded",
                    "memory_id": args.memory_id,
                    "role": args.role,
                    "index": args.index,
                    "verdict": args.verdict,
                },
                args.output,
            )
        )
        return 0
    finally:
        raw_service.stop()


def cmd_pending(args: argparse.Namespace) -> int:
    """List pending reviews."""
    raw_service, svc = _make_authorized_service(getattr(args, "config", None))
    try:
        reviews = svc.get_pending_reviews()
        out = [_dataclass_to_dict(r) for r in reviews]
        print(_fmt({"total": len(out), "reviews": out}, args.output))
        return 0
    finally:
        raw_service.stop()


def cmd_compact(args: argparse.Namespace) -> int:
    """Run the compaction pipeline."""
    raw_service, svc = _make_authorized_service(getattr(args, "config", None))
    try:
        is_local = getattr(raw_service, "_is_local_development_context", None)
        if not callable(is_local) or not is_local(svc.authorization):
            raise PermissionError(
                "principal-scoped CLI compaction is unavailable; use an authorized maintenance worker"
            )
        result = svc.compact(scope=getattr(args, "scope", "project"))
        out = _dataclass_to_dict(result)
        print(_fmt(out, args.output))
        return 0
    finally:
        raw_service.stop()


def cmd_health(args: argparse.Namespace) -> int:
    """Health check.

    By default a ``warning`` status still exits 0 (backward compatible).
    With ``--strict`` only ``healthy`` exits 0; anything else exits 1.
    """
    raw_service, svc = _make_authorized_service(getattr(args, "config", None))
    try:
        info = svc.health()
        print(_fmt(info, args.output))
        ok_statuses = {"healthy"} if getattr(args, "strict", False) else {"healthy", "warning"}
        return 0 if info.get("status") in ok_statuses else 1
    finally:
        raw_service.stop()


def cmd_readiness(args: argparse.Namespace) -> int:
    """Print the fail-closed industrial readiness gate report."""

    from memplex.config import load_config
    from memplex.product import industrial_readiness_report

    config = load_config(path=getattr(args, "config", None))
    report = industrial_readiness_report(config)
    print(_fmt(report, args.output))
    if getattr(args, "strict", False) and not report["ready"]:
        return 1
    return 0


def cmd_operations(args: argparse.Namespace) -> int:
    """Run data-only operations/SLO inspection commands."""
    from memplex.config import load_config
    from memplex.operations import (
        OperationsEvidenceError,
        OperationsReadinessBinding,
        alert_rules_bytes,
        alert_rules_sha256,
        load_operations_report,
        load_operations_signing_key,
    )
    from memplex.readiness_evidence import (
        ReadinessEvidenceError,
        load_deployment_evidence_binding_from_environment,
    )

    action = getattr(args, "operations_command", None)
    try:
        if action == "status":
            from memplex.product import industrial_readiness_report

            report = industrial_readiness_report(
                load_config(path=getattr(args, "config", None))
            )
            gate = next(item for item in report["gates"] if item["id"] == "operations_slo")
            print(_fmt(gate, args.output))
            return 0 if gate["status"] == "pass" else 1
        if action == "verify-report":
            report = load_operations_report(Path(args.report))
            config = load_config(path=getattr(args, "config", None))
            deployment = load_deployment_evidence_binding_from_environment(
                memplex_version=version("memplex")
            )
            binding = OperationsReadinessBinding(
                deployment_id=deployment.deployment_id,
                source_sha256=deployment.source_sha256,
                artifact_sha256=deployment.artifact_sha256,
                target_identity_sha256=deployment.target_identity_sha256,
                expected_key_id=config.operations.report_key_id,
            )
            report.verify_readiness(load_operations_signing_key(), binding=binding)
            payload = {
                "schema_version": 1,
                "verified": True,
                "report_id": report.report_id,
                "key_id": report.key_id,
                "request_count": report.request_count,
                "availability": report.availability,
                "error_rate": report.error_rate,
                "p95_latency_ms": report.p95_latency_ms,
                "shutdown_drained": report.shutdown_drained,
                "industrial_gate_closing": report.industrial_gate_closing,
            }
            print(_fmt(payload, args.output))
            return 0
        if action == "alerts-check":
            content = alert_rules_bytes()
            count = content.count(b"      - alert:")
            print(
                _fmt(
                    {
                        "schema_version": 1,
                        "verified": count == 8,
                        "rule_count": count,
                        "sha256": alert_rules_sha256(),
                    },
                    args.output,
                )
            )
            return 0 if count == 8 else 1
    except (OperationsEvidenceError, ReadinessEvidenceError, OSError, ValueError, TypeError):
        print(_fmt({"error": "operations_evidence_invalid"}, args.output))
        return 1
    print(_fmt({"error": "operations_command_invalid"}, args.output))
    return 1


class _MigrationCommandError(RuntimeError):
    """Operator-safe error boundary for PostgreSQL migration commands."""

    def __init__(self, code: str, remediation: str) -> None:
        super().__init__(code)
        self.code = code
        self.remediation = remediation


def _migration_target_key(target: Any) -> tuple[Any, Any, Any, Any]:
    """Compare only the server-resolved identity fields, never DSN text."""
    from memplex.storage.migrations import PostgresTargetIdentity

    if type(target) is not PostgresTargetIdentity:
        raise _MigrationCommandError(
            "postgres_target_invalid",
            "确认迁移连接指向已解析的 PostgreSQL TCP 目标后重试。",
        )

    server_address = getattr(target, "server_address", None)
    server_port = getattr(target, "server_port", None)
    database = getattr(target, "database", None)
    schema = getattr(target, "schema", None)
    if (
        type(server_address) is not str
        or not server_address
        or type(server_port) is not int
        or not 1 <= server_port <= 65_535
        or type(database) is not str
        or not database
        or type(schema) is not str
        or not schema
    ):
        raise _MigrationCommandError(
            "postgres_target_invalid",
            "确认迁移连接指向已解析的 PostgreSQL TCP 目标后重试。",
        )
    return server_address, server_port, database, schema


def _migration_application_principal_key(principal: Any) -> tuple[str, str]:
    """Accept one exact, direct PostgreSQL application login identity."""
    from memplex.storage.migrations import PostgresApplicationPrincipal

    if (
        type(principal) is not PostgresApplicationPrincipal
        or type(principal.role) is not str
        or not principal.role
        or type(principal.session_role) is not str
        or not principal.session_role
        or principal.role != principal.session_role
    ):
        raise _MigrationCommandError(
            "postgres_application_principal_invalid",
            "确认应用连接以单一非特权 PostgreSQL 登录角色连接后重试。",
        )
    return principal.role, principal.session_role


class _MigrationCommandContext:
    """Data-only PostgreSQL migration collaborator for one CLI invocation."""

    def __init__(
        self,
        migration_runner: Any,
        application_runner: Any,
        target: Any,
        principal: Any,
        application_acl: Any,
        profile: str,
    ) -> None:
        self._runner = migration_runner
        self._application_runner = application_runner
        self._target = target
        self._target_key = _migration_target_key(target)
        self._principal_key = _migration_application_principal_key(principal)
        self._application_acl = application_acl
        self._profile = profile

    def _options(self) -> dict[str, Any]:
        return {
            "expected_target": self._target,
            "application_acl": self._application_acl,
            "deployment_profile": self._profile,
        }

    def status(self) -> Any:
        return self._runner.status(**self._options())

    def plan(self) -> Any:
        return self._runner.plan(**self._options())

    def _fresh_strict_readback(self) -> Any:
        """Rebind the application plane before an independent strict status read."""
        fresh_target = self._application_runner.inspect_target()
        if _migration_target_key(fresh_target) != self._target_key:
            raise _MigrationCommandError(
                "postgres_target_mismatch",
                "确认 application 与 migration DSN 指向同一 PostgreSQL database/schema。",
            )
        fresh_principal = self._application_runner.inspect_application_principal(
            expected_target=fresh_target
        )
        if _migration_application_principal_key(fresh_principal) != self._principal_key:
            raise _MigrationCommandError(
                "postgres_application_principal_invalid",
                "确认应用连接以单一非特权 PostgreSQL 登录角色连接后重试。",
            )
        return self.status()

    def apply(self) -> tuple[Any | None, Any]:
        """Confirm the final ledger state even when the mutation call raises."""
        mutation: Any | None = None
        mutation_outcome_unknown = False
        try:
            mutation = self._runner.apply(**self._options())
        except Exception:
            mutation_outcome_unknown = True

        try:
            fresh = self._fresh_strict_readback()
        except Exception:
            if mutation_outcome_unknown:
                raise _MigrationCommandError(
                    "migration_outcome_requires_readback",
                    "确认 PostgreSQL 连通性、应用身份与最小权限 ACL 后重新执行 status；不要重试写入。",
                ) from None
            raise _MigrationCommandError(
                "migration_committed_acl_remediation_required",
                "人工配置已批准的最小权限 ACL 后重新执行 status；命令不会自动 GRANT。",
            ) from None

        state = getattr(fresh, "state", None)
        if state == "ready":
            return mutation, fresh
        if state == "upgrade_required":
            raise _MigrationCommandError(
                "migration_failed",
                "迁移结构仍未收敛；检查 PostgreSQL migration 后重新执行 status 或 plan。",
            )
        if mutation_outcome_unknown:
            raise _MigrationCommandError(
                "migration_outcome_requires_readback",
                "确认 PostgreSQL 连通性、应用身份与最小权限 ACL 后重新执行 status；不要重试写入。",
            )
        raise _MigrationCommandError(
            "migration_committed_acl_remediation_required",
            "人工配置已批准的最小权限 ACL 后重新执行 status；命令不会自动 GRANT。",
        )


def _build_migration_command_context(config_path: Optional[str] = None) -> _MigrationCommandContext:
    """Resolve and validate migration/application connections without a service."""
    from memplex.config import load_config, normalize_deployment_contract
    from memplex.storage.migrations import (
        ApplicationAclContract,
        PostgresMigrationRunner,
    )

    config = load_config(path=config_path)
    profile, backend = normalize_deployment_contract(config)
    application_dsn = getattr(config.storage, "path", None)
    migration_dsn = getattr(config.storage, "migration_dsn", None)
    if backend != "postgres":
        raise _MigrationCommandError(
            "postgres_backend_required",
            "将 storage.backend 配置为 postgres 后重试。",
        )
    if (
        type(application_dsn) is not str
        or not application_dsn.strip()
        or type(migration_dsn) is not str
        or not migration_dsn.strip()
    ):
        raise _MigrationCommandError(
            "split_postgres_dsn_required",
            "配置独立的 storage.path 与 storage.migration_dsn 后重试。",
        )

    try:
        application_runner = PostgresMigrationRunner(application_dsn)
        application_target = application_runner.inspect_target()
        application_target_key = _migration_target_key(application_target)
        principal = application_runner.inspect_application_principal(
            expected_target=application_target
        )
        _migration_application_principal_key(principal)
        migration_runner = PostgresMigrationRunner(migration_dsn)
        migration_target = migration_runner.inspect_target()
        if _migration_target_key(migration_target) != application_target_key:
            raise _MigrationCommandError(
                "postgres_target_mismatch",
                "确认 application 与 migration DSN 指向同一 PostgreSQL database/schema。",
            )
        return _MigrationCommandContext(
            migration_runner,
            application_runner,
            application_target,
            principal,
            ApplicationAclContract(principal.role),
            profile,
        )
    except _MigrationCommandError:
        raise
    except Exception as exc:
        _ = exc
        raise _MigrationCommandError(
            "migration_context_unavailable",
            "确认 PostgreSQL 连通性、应用身份和最小权限 ACL 后重试。",
        ) from None


def _migration_plan_payload(plan: Any) -> dict[str, Any]:
    """Project a runner plan to a small operator-safe public schema."""
    values = plan if isinstance(plan, dict) else {
        "state": getattr(plan, "state", None),
        "current_version": getattr(plan, "current_version", None),
        "known_version": getattr(plan, "known_version", None),
        "pending": getattr(plan, "pending", None),
    }
    state = values.get("state")
    current_version = values.get("current_version")
    known_version = values.get("known_version")
    pending = values.get("pending")
    if (
        state not in {"ready", "upgrade_required", "blocked"}
        or type(current_version) is not int
        or type(known_version) is not int
        or not isinstance(pending, (tuple, list))
    ):
        raise _MigrationCommandError(
            "migration_status_invalid",
            "重新执行 status；若仍失败，请检查 PostgreSQL migration runner。",
        )
    pending_payload: list[dict[str, Any]] = []
    for item in pending:
        version = item.get("version") if isinstance(item, dict) else getattr(item, "version", None)
        name = item.get("name") if isinstance(item, dict) else getattr(item, "name", None)
        if type(version) is not int or type(name) is not str:
            raise _MigrationCommandError(
                "migration_status_invalid",
                "重新执行 status；若仍失败，请检查 PostgreSQL migration runner。",
            )
        pending_payload.append({"version": version, "name": name})
    return {
        "schema_version": 1,
        "state": state,
        "current_version": current_version,
        "known_version": known_version,
        "pending": pending_payload,
    }


def _print_migration_error(args: argparse.Namespace, error: _MigrationCommandError) -> int:
    print(
        _fmt(
            {
                "schema_version": 1,
                "status": "error",
                "code": error.code,
                "remediation": error.remediation,
            },
            args.output,
        ),
        file=sys.stderr,
    )
    return 1


class _BackupCommandError(RuntimeError):
    """Operator-safe error boundary for backup and restore commands."""

    def __init__(self, code: str, remediation: str) -> None:
        super().__init__(code)
        self.code = code
        self.remediation = remediation


class _BackupCommandContext:
    """Data-only backup collaborators for one CLI invocation."""

    def __init__(
        self,
        *,
        backend: str,
        signing_key: bytes,
        key_id: str,
        default_destination: Path,
        postgres_executor: Any | None = None,
        migration_dsn: str | None = None,
        lite_store: Any | None = None,
        rpo_target_seconds: int = 300,
        rto_target_seconds: int = 1800,
        max_artifact_bytes: int = 64 * 1024**3,
    ) -> None:
        self._backend = backend
        self._signing_key = signing_key
        self._key_id = key_id
        self._default_destination = default_destination
        self._postgres_executor = postgres_executor
        self._migration_dsn = migration_dsn
        self._lite_store = lite_store
        self._rpo_target_seconds = rpo_target_seconds
        self._rto_target_seconds = rto_target_seconds
        self._max_artifact_bytes = max_artifact_bytes

    def create(self, destination: str | None) -> Any:
        resolved = self._default_destination if destination is None else Path(destination)
        if self._backend == "postgres":
            return self._postgres_executor.create(
                migration_dsn=self._migration_dsn,
                destination=resolved,
                signing_key=self._signing_key,
                key_id=self._key_id,
                max_bytes=self._max_artifact_bytes,
            )
        return self._lite_store.create_backup(
            resolved,
            self._signing_key,
            self._key_id,
            max_bytes=self._max_artifact_bytes,
        )

    def verify(self, artifact: str) -> Any:
        from memplex.backup import verify_backup_artifact

        return verify_backup_artifact(Path(artifact), self._signing_key)

    def restore(self, artifact: str, target_schema: str) -> Any:
        if self._backend == "postgres":
            return self._postgres_executor.restore(
                migration_dsn=self._migration_dsn,
                artifact=Path(artifact),
                signing_key=self._signing_key,
                target_schema=target_schema,
            )
        if target_schema != "memory":
            raise _BackupCommandError(
                "backup_target_invalid",
                "Lite 开发态恢复的 target schema 必须为 memory。",
            )
        started = __import__("time").monotonic()
        verification = self.verify(artifact)
        self._lite_store.restore_backup(Path(artifact), self._signing_key)
        from memplex.backup import RestoreResult

        return RestoreResult(
            backup_id=verification.backup_id,
            database=verification.database,
            schema=verification.schema,
            restored=True,
            elapsed_seconds=__import__("time").monotonic() - started,
        )

    def pitr_status(self) -> Any:
        if self._backend != "postgres":
            raise _BackupCommandError(
                "pitr_not_ready", "PITR 仅适用于 PostgreSQL 生产存储。"
            )
        from memplex.storage.postgres_backup import inspect_pitr_readiness

        return inspect_pitr_readiness(self._migration_dsn)

    def drill(self, artifact: str, target_schema: str) -> Any:
        from datetime import UTC, datetime

        from memplex.backup import load_verified_backup_manifest, run_restore_drill

        if self._backend != "postgres":
            raise _BackupCommandError(
                "pitr_not_ready", "灾难恢复演练仅适用于 PostgreSQL 生产存储。"
            )
        manifest = load_verified_backup_manifest(Path(artifact), self._signing_key)
        fault_cutoff = datetime.now(UTC)
        restore_started = datetime.now(UTC)
        restored = self.restore(artifact, target_schema)
        if restored.backup_id != manifest.backup_id:
            raise _BackupCommandError(
                "backup_integrity_failed",
                "备份工件在演练期间发生变化；请重新验证后重试。",
            )
        restore_verified = datetime.now(UTC)
        return run_restore_drill(
            backup_id=manifest.backup_id,
            backup_completed_at=manifest.created_at,
            fault_cutoff_at=fault_cutoff.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            restore_started_at=restore_started.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            restore_verified_at=restore_verified.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            rpo_target_seconds=self._rpo_target_seconds,
            rto_target_seconds=self._rto_target_seconds,
            data_digest=manifest.payload_sha256,
            data_verified=True,
            pitr=self.pitr_status(),
            key_id=self._key_id,
            signing_key=self._signing_key,
        )


def _build_backup_command_context(config_path: Optional[str] = None) -> _BackupCommandContext:
    """Build backup collaborators without constructing MemplexService."""
    from memplex.backup import load_backup_signing_key
    from memplex.config import load_config, normalize_deployment_contract

    try:
        config = load_config(path=config_path)
        profile, backend = normalize_deployment_contract(config)
        signing_key = load_backup_signing_key()
        key_id = config.backup.key_id
        destination = Path(config.backup.directory).expanduser()
        if backend == "postgres":
            from memplex.storage.migrations import (
                ApplicationAclContract,
                IngressAclContract,
                PostgresMigrationRunner,
            )
            from memplex.storage.postgres_backup import PostgresBackupExecutor

            migration_dsn = config.storage.migration_dsn
            if type(migration_dsn) is not str or not migration_dsn.strip():
                raise _BackupCommandError(
                    "backup_config_invalid",
                    "配置非空 storage.migration_dsn 后重试。",
                )
            runner = PostgresMigrationRunner(migration_dsn)
            target = runner.inspect_target()
            application_runner = PostgresMigrationRunner(str(config.storage.path))
            application_target = application_runner.inspect_target()
            if application_target != target:
                raise _BackupCommandError(
                    "backup_config_invalid",
                    "确认 application 与 migration DSN 指向同一 PostgreSQL target。",
                )
            application_principal = application_runner.inspect_application_principal(
                expected_target=application_target
            )
            ingress_acl = None
            if config.sync.enabled:
                inbound_dsn = config.storage.inbound_dsn
                if type(inbound_dsn) is not str or not inbound_dsn.strip():
                    raise _BackupCommandError(
                        "backup_config_invalid", "配置非空 storage.inbound_dsn 后重试。"
                    )
                inbound_runner = PostgresMigrationRunner(inbound_dsn)
                inbound_target = inbound_runner.inspect_target()
                if inbound_target != target:
                    raise _BackupCommandError(
                        "backup_config_invalid",
                        "确认 inbound 与 migration DSN 指向同一 PostgreSQL target。",
                    )
                ingress_principal = inbound_runner.inspect_application_principal(
                    expected_target=inbound_target
                )
                ingress_acl = IngressAclContract(ingress_principal.role)
            executor = PostgresBackupExecutor(
                expected_target=target,
                timeout_seconds=config.backup.restore_timeout_seconds,
                application_acl=ApplicationAclContract(application_principal.role),
                ingress_acl=ingress_acl,
                deployment_profile=profile,
            )
            return _BackupCommandContext(
                backend=backend,
                signing_key=signing_key,
                key_id=key_id,
                default_destination=destination,
                postgres_executor=executor,
                migration_dsn=migration_dsn,
                rpo_target_seconds=config.backup.rpo_target_seconds,
                rto_target_seconds=config.backup.rto_target_seconds,
                max_artifact_bytes=config.backup.max_artifact_bytes,
            )
        from memplex.storage.lite.store import LiteMemoryStore

        path = Path(config.storage.path).expanduser() / "memory.json"
        return _BackupCommandContext(
            backend=backend,
            signing_key=signing_key,
            key_id=key_id,
            default_destination=destination,
            lite_store=LiteMemoryStore(path, deployment_profile=profile),
            rpo_target_seconds=config.backup.rpo_target_seconds,
            rto_target_seconds=config.backup.rto_target_seconds,
            max_artifact_bytes=config.backup.max_artifact_bytes,
        )
    except _BackupCommandError:
        raise
    except Exception:
        raise _BackupCommandError(
            "backup_config_invalid",
            "确认备份配置、签名密钥与数据库工具后重试。",
        ) from None


def _build_backup_verification_context(
    config_path: Optional[str] = None,
) -> _BackupCommandContext:
    """Build the offline artifact verifier without database or service access."""
    from memplex.backup import load_backup_signing_key
    from memplex.config import load_config

    try:
        config = load_config(path=config_path)
        return _BackupCommandContext(
            backend=str(config.storage.backend),
            signing_key=load_backup_signing_key(),
            key_id=str(config.backup.key_id),
            default_destination=Path(config.backup.directory).expanduser(),
        )
    except Exception:
        raise _BackupCommandError(
            "backup_config_invalid",
            "确认备份签名密钥与配置后重试。",
        ) from None


def _backup_payload(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else _dataclass_to_dict(value)
    allowed = (
        "backup_id",
        "backend",
        "database",
        "schema",
        "migration_version",
        "payload_size",
        "verified",
        "restored",
        "elapsed_seconds",
        "ready",
        "wal_level",
        "archive_mode",
        "archive_command_configured",
        "full_page_writes",
        "max_wal_senders",
        "backup_completed_at",
        "fault_cutoff_at",
        "restore_started_at",
        "restore_verified_at",
        "observed_rpo_seconds",
        "observed_rto_seconds",
        "rpo_target_seconds",
        "rto_target_seconds",
        "data_digest",
        "data_verified",
        "pitr_ready",
        "industrial_gate_closing",
        "key_id",
        "signature",
    )
    return {key: raw[key] for key in allowed if key in raw}


def _print_backup_error(args: argparse.Namespace, error: _BackupCommandError) -> int:
    print(
        _fmt(
            {
                "schema_version": 1,
                "status": "error",
                "code": error.code,
                "remediation": error.remediation,
            },
            args.output,
        ),
        file=sys.stderr,
    )
    return 1


def _cmd_storage_backup(args: argparse.Namespace) -> int:
    try:
        action = getattr(args, "backup_command", None)
        builder = (
            _build_backup_verification_context
            if action == "verify"
            else _build_backup_command_context
        )
        context = builder(getattr(args, "config", None))
        if action == "create":
            result = context.create(getattr(args, "destination", None))
        elif action == "verify":
            result = context.verify(args.artifact)
        elif action == "restore":
            result = context.restore(args.artifact, args.target_schema)
        elif action == "pitr-status":
            result = context.pitr_status()
        elif action == "drill":
            result = context.drill(args.artifact, args.target_schema)
        else:
            raise _BackupCommandError(
                "backup_command_invalid",
                "使用 storage backup create、verify、restore 或 pitr-status。",
            )
        print(_fmt(_backup_payload(result), args.output))
        return 0
    except _BackupCommandError as error:
        return _print_backup_error(args, error)
    except Exception:
        return _print_backup_error(
            args,
            _BackupCommandError(
                "backup_command_failed",
                "确认备份配置、产物完整性与目标状态后重试。",
            ),
        )


def cmd_storage(args: argparse.Namespace) -> int:
    """Run PostgreSQL migration diagnostics without constructing a service."""
    if getattr(args, "storage_command", None) == "backup":
        return _cmd_storage_backup(args)
    if getattr(args, "storage_command", None) != "migration":
        return _print_migration_error(
            args,
            _MigrationCommandError(
                "storage_command_invalid", "使用 memplex storage migration status、plan 或 apply。"
            ),
        )
    try:
        context = _build_migration_command_context(getattr(args, "config", None))
        action = getattr(args, "migration_command", None)
        if action == "status":
            payload = {"command": "status", **_migration_plan_payload(context.status())}
        elif action == "plan":
            payload = {"command": "plan", **_migration_plan_payload(context.plan())}
        elif action == "apply" and getattr(args, "dry_run", False):
            payload = {
                "command": "apply",
                "dry_run": True,
                **_migration_plan_payload(context.plan()),
            }
        elif action == "apply":
            mutation, fresh = context.apply()
            payload: dict[str, Any] = {
                "command": "apply",
                "dry_run": False,
                "readback": _migration_plan_payload(fresh),
            }
            if mutation is None:
                payload["outcome"] = "readback_confirmed"
            else:
                payload["mutation"] = _migration_plan_payload(mutation)
        else:
            raise _MigrationCommandError(
                "migration_command_invalid", "使用 memplex storage migration status、plan 或 apply。"
            )
        print(_fmt(payload, args.output))
        return 0
    except _MigrationCommandError as error:
        return _print_migration_error(args, error)
    except Exception as exc:
        _ = exc
        return _print_migration_error(
            args,
            _MigrationCommandError(
                "migration_command_failed",
                "确认 PostgreSQL 连通性、应用身份和最小权限 ACL 后重试。",
            ),
        )


def cmd_stats(args: argparse.Namespace) -> int:
    """Display statistics."""
    raw_service, svc = _make_authorized_service(getattr(args, "config", None))
    try:
        info = svc.stats()
        print(_fmt(info, args.output))
        return 0
    finally:
        raw_service.stop()


def cmd_doctor(args: argparse.Namespace) -> int:
    """Run productized readiness checks."""
    from memplex.product import run_doctor

    raw_service, svc = _make_authorized_service(getattr(args, "config", None))
    try:
        report = run_doctor(
            svc,
            agent=getattr(args, "agent", "codex"),
            profile=getattr(args, "profile", None),
            smoke=getattr(args, "smoke", False) or getattr(args, "fix", False),
            target_dir=getattr(args, "target_dir", None),
            user_id=getattr(args, "user_id", None),
            session_id=getattr(args, "session_id", "default"),
            project_path=getattr(args, "project_path", None),
        )
        print(_fmt(report, args.output))
        return 0 if report["status"] == "pass" else 1
    finally:
        raw_service.stop()


def cmd_scope(args: argparse.Namespace) -> int:
    """Visibility-first scope commands."""
    from memplex.product import scope_catalog, scope_explain, scope_preview

    action = getattr(args, "scope_command", None)
    if action == "list":
        print(_fmt(scope_catalog(), args.output))
        return 0

    raw_service, svc = _make_authorized_service(getattr(args, "config", None))
    try:
        explained = scope_explain(
            agent=getattr(args, "agent", "codex"),
            user_id=getattr(args, "user_id", None),
            session_id=getattr(args, "session_id", "default"),
            project_path=getattr(args, "project_path", None),
            storage_namespace=svc.storage_namespace(),
        )
        if action == "explain":
            print(_fmt(explained, args.output))
            return 0
        if action == "preview":
            print(
                _fmt(
                    scope_preview(
                        svc,
                        explained["read_namespace_filters"],
                        limit=getattr(args, "limit", 10),
                    ),
                    args.output,
                )
            )
            return 0
    finally:
        raw_service.stop()

    print("Error: unknown scope command", file=sys.stderr)
    return 1


def cmd_policy(args: argparse.Namespace) -> int:
    """Show recall/capture policy."""
    raw_service, svc = _make_authorized_service(getattr(args, "config", None))
    try:
        print(_fmt(svc.policy(agent=getattr(args, "agent", "codex")), args.output))
        return 0
    finally:
        raw_service.stop()


def cmd_inbox(args: argparse.Namespace) -> int:
    """Review pending memory items through an inbox vocabulary."""
    action = getattr(args, "inbox_command", "list")
    raw_service, svc = _make_authorized_service(getattr(args, "config", None))
    try:
        if action in {None, "list"}:
            reviews = svc.get_pending_reviews(limit=getattr(args, "limit", 100))
            print(
                _fmt({"total": len(reviews), "reviews": _dataclass_to_dict(reviews)}, args.output)
            )
            return 0
        if action == "show":
            reviews = [
                review
                for review in svc.get_pending_reviews(limit=100000)
                if review.memory_id == args.memory_id
            ]
            memory = svc.get(args.memory_id)
            print(
                _fmt(
                    {
                        "memory": _dataclass_to_dict(memory) if memory is not None else None,
                        "reviews": _dataclass_to_dict(reviews),
                    },
                    args.output,
                )
            )
            return 0 if memory is not None or reviews else 1
        if action in {"accept", "reject", "merge"}:
            result = svc.apply_resolution(
                memory_id=args.memory_id,
                field_role=args.field_role,
                action=action,
                new_value=getattr(args, "value", None),
            )
            print(_fmt(result, args.output))
            return 0
    finally:
        raw_service.stop()

    print("Error: unknown inbox command", file=sys.stderr)
    return 1


def cmd_corpus(args: argparse.Namespace) -> int:
    """Manifest-driven canonical corpus commands."""
    from memplex.product import corpus_index, corpus_preview, corpus_recall

    action = getattr(args, "corpus_command", None)
    if action == "preview":
        print(_fmt(corpus_preview(args.manifest, limit=getattr(args, "limit", 100)), args.output))
        return 0

    raw_service, svc = _make_authorized_service(getattr(args, "config", None))
    try:
        if action == "index":
            print(
                _fmt(
                    corpus_index(
                        svc,
                        args.manifest,
                        dry_run=getattr(args, "dry_run", False),
                    ),
                    args.output,
                )
            )
            return 0
        if action == "recall":
            print(
                _fmt(
                    corpus_recall(
                        svc,
                        args.query,
                        top_k=getattr(args, "top_k", 10),
                        max_tokens=getattr(args, "max_tokens", 4000),
                    ),
                    args.output,
                )
            )
            return 0
    finally:
        raw_service.stop()

    print("Error: unknown corpus command", file=sys.stderr)
    return 1


def cmd_report(args: argparse.Namespace) -> int:
    """Generate an operator report."""
    from memplex.product import operator_report

    raw_service, svc = _make_authorized_service(getattr(args, "config", None))
    try:
        print(_fmt(operator_report(svc, agent=getattr(args, "agent", "codex")), args.output))
        return 0
    finally:
        raw_service.stop()


def cmd_benchmark(args: argparse.Namespace) -> int:
    """Benchmark commands (list / run).

    The ``benchmarks`` package is not part of the distribution (pyproject
    only ships ``memplex*``), so imports happen lazily inside this handler
    and a missing package produces a clear error instead of a traceback.
    """
    try:
        from benchmarks.base import BenchmarkRunnerFactory
        from benchmarks.benchmark_cli import run_benchmark_command
    except ImportError:
        print(
            "Error: benchmarks 仅源码可用 -- the 'benchmarks' package is not shipped "
            "in the installed distribution. Run this command from the source "
            "checkout (repository root) instead.",
            file=sys.stderr,
        )
        return 1

    action = getattr(args, "benchmark_command", None)
    if action == "list":
        datasets = sorted(BenchmarkRunnerFactory.available_datasets())
        print(_fmt({"total": len(datasets), "datasets": datasets}, args.output))
        return 0

    if action == "run":
        output_path = getattr(args, "benchmark_output", None) or (
            ".memplex/benchmarks/results.jsonl"
        )
        results = run_benchmark_command(
            dataset=args.dataset,
            path=getattr(args, "path", None),
            output=output_path,
            retrieval_k=getattr(args, "top_k", 10),
            force_synthetic=getattr(args, "synthetic", False),
        )
        # Compact per-dataset summary: best value per metric.
        summary = {}
        for name, dataset_results in sorted(results.items()):
            best: dict = {}
            for r in dataset_results:
                if r.metric not in best or r.value > best[r.metric]:
                    best[r.metric] = r.value
            summary[name] = {m: round(v, 4) for m, v in sorted(best.items())}
        print(_fmt({"status": "ok", "output": output_path, "results": summary}, args.output))
        return 0

    print("Error: unknown benchmark command", file=sys.stderr)
    return 1


def cmd_sync(args: argparse.Namespace) -> int:
    """Multi-node memory sync (pull from / status of remote).

    Requires ``MEMPLEX_REMOTE_URL`` to point at a central Memplex HTTP
    server. ``sync pull`` fetches incremental changes (LWW + tombstones)
    into the local store; ``sync status`` reports the remote config and
    last-pull timestamp without touching the network.
    """
    from memplex.sync import SyncableStore

    action = getattr(args, "sync_command", None)
    raw_service, svc = _make_authorized_service(getattr(args, "config", None))
    read_only = action == "status" or action == "dlq"
    try:
        dispatcher = getattr(raw_service, "_sync_dispatcher", None)
        if dispatcher is not None:
            if action == "status":
                print(
                    _fmt(
                        {"status": "active", **raw_service.sync_status()},
                        args.output,
                    )
                )
                return 0
            if action == "drain":
                result = raw_service.drain_sync(
                    getattr(args, "timeout", None)
                )
                print(_fmt(result.to_dict(), args.output))
                return 0 if result.drained else 1
            if action == "pull":
                target_id = getattr(args, "target", None)
                configured_targets = tuple(raw_service._config.sync.targets)
                if target_id is None:
                    if len(configured_targets) != 1:
                        print(
                            _fmt(
                                {"error": "sync_target_required"}, args.output
                            )
                        )
                        return 1
                    target_id = configured_targets[0]
                result = raw_service.pull_sync(target_id)
                print(_fmt(asdict(result), args.output))
                return 0
            if action == "dlq":
                dlq_action = getattr(args, "dlq_command", None)
                if dlq_action == "list":
                    print(
                        _fmt(
                            {
                                "items": raw_service.list_sync_dead_letters(
                                    limit=args.limit
                                )
                            },
                            args.output,
                        )
                    )
                    return 0
                if dlq_action == "replay":
                    replayed = raw_service.replay_sync_dead_letter(
                        args.target, args.event_id
                    )
                    print(
                        _fmt(
                            {
                                "replayed": replayed,
                                "target_id": args.target,
                                "event_id": args.event_id,
                            },
                            args.output,
                        )
                    )
                    return 0 if replayed else 1
                print(_fmt({"error": "unknown_dlq_command"}, args.output))
                return 1

        store = raw_service.store
        if not isinstance(store, SyncableStore):
            print(
                _fmt(
                    {
                        "status": "disabled",
                        "reason": (
                            "MEMPLEX_REMOTE_URL is not set; the local store is not "
                            "sync-enabled. Set MEMPLEX_REMOTE_URL (and optionally "
                            "MEMPLEX_API_KEY) to enable multi-node sharing."
                        ),
                    },
                    args.output,
                )
            )
            return 0

        if action == "status":
            cfg = store._config
            print(
                _fmt(
                    {
                        "status": "active" if cfg.active else "inactive",
                        "remote_configured": cfg.url is not None,
                        "target_count": len(cfg.all_targets()),
                        "auth": "api_key" if cfg.api_key else ("bearer" if cfg.bearer else "none"),
                        "last_pull_at": store.last_pull_at,
                        "push_failures": store._push_failures,
                    },
                    args.output,
                )
            )
            return 0

        if action == "pull":
            summary = store.authorized(svc.authorization).pull_incremental()
            print(_fmt(summary, args.output))
            return 0

        print(_fmt({"error": f"unknown sync command: {action!r}"}, args.output))
        return 1
    finally:
        raw_service.stop(drain_sync=not read_only)


def cmd_agent(args: argparse.Namespace) -> int:
    """Portable agent integration commands."""
    from memplex.adapters.agent_installer import install_agent, uninstall_agent
    from memplex.adapters.agent_runtime import (
        AgentMemoryRuntime,
        get_agent_manifest,
        list_agent_profiles,
    )

    action = getattr(args, "agent_command", None)
    if action == "list":
        print(_fmt(list_agent_profiles(), args.output))
        return 0
    if action == "manifest":
        if (args.agent or "").strip().lower() == "all":
            manifests = {name: get_agent_manifest(name) for name in list_agent_profiles()}
            print(_fmt(manifests, args.output))
            return 0
        print(_fmt(get_agent_manifest(args.agent), args.output))
        return 0
    if action == "status":
        from memplex.product import run_agent_diagnostics

        requested = (args.agent or "codex").strip().lower()
        target_dir = getattr(args, "target_dir", None)
        if requested == "all" and target_dir is not None:
            print(
                _fmt(
                    {
                        "status": "error",
                        "error": (
                            "--target-dir cannot represent four different host roots with "
                            "--agent all; omit it or inspect each host separately."
                        ),
                    },
                    args.output,
                )
            )
            return 2
        names = list(list_agent_profiles()) if requested == "all" else [requested]
        raw_service, svc = _make_authorized_service(getattr(args, "config", None))
        try:
            reports = {}
            failed = False
            for name in names:
                try:
                    reports[name] = run_agent_diagnostics(
                        svc,
                        agent=name,
                        target_dir=target_dir,
                        user_id=getattr(args, "user_id", None),
                        session_id=getattr(args, "session_id", "default"),
                        project_path=getattr(args, "project_path", None),
                    )
                except Exception as exc:
                    failed = True
                    reports[name] = {
                        "schema_version": 1,
                        "selected_host": name,
                        "status": "error",
                        "error": str(exc),
                    }
        finally:
            raw_service.stop()
        print(_fmt(reports if requested == "all" else reports[names[0]], args.output))
        return 1 if failed else 0
    if action == "install":
        result = install_agent(
            args.agent,
            target_dir=getattr(args, "target_dir", None),
            user_id=getattr(args, "user_id", None),
            project_path=getattr(args, "project_path", None),
            dry_run=getattr(args, "dry_run", False),
        )
        print(_fmt(_dataclass_to_dict(result), args.output))
        return 0
    if action == "uninstall":
        result = uninstall_agent(
            args.agent,
            target_dir=getattr(args, "target_dir", None),
            dry_run=getattr(args, "dry_run", False),
        )
        print(_fmt(_dataclass_to_dict(result), args.output))
        return 0

    runtime_agent = str(getattr(args, "agent", "codex")).strip().lower() or "codex"
    raw_service, svc = _make_authorized_service(
        getattr(args, "config", None), agent_id=runtime_agent
    )
    try:
        runtime = AgentMemoryRuntime(
            service=raw_service,
            agent=runtime_agent,
            user_id=getattr(args, "user_id", None),
            session_id=getattr(args, "session_id", "default"),
            project_path=getattr(args, "project_path", None),
            top_k=getattr(args, "top_k", 5),
            token_budget=getattr(args, "token_budget", 1500),
            authorization=svc.authorization,
        )
        if action == "recall":
            recalled = runtime.before_prompt(args.prompt)
            print(_fmt(recalled.__dict__, args.output))
            return 0
        if action == "capture":
            runtime.after_response(
                user_message=args.user_message,
                assistant_message=args.assistant_message,
                next_prompt_hint=getattr(args, "next_prompt_hint", None),
            )
            print(_fmt({"status": "captured", "agent": runtime.agent}, args.output))
            return 0
    finally:
        raw_service.stop()

    print("Error: unknown agent command", file=sys.stderr)
    return 1


# ── Claude Code Plugin Setup ────────────────────────────────────────

_PLUGIN_AUTHOR = "articultur"
_PLUGIN_NAME = "memplex"


def _get_marketplace_dir() -> Path:
    """Return the Claude Code marketplace target directory."""
    claude_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    return claude_dir / "plugins" / "marketplaces" / _PLUGIN_AUTHOR


def cmd_setup(args: argparse.Namespace) -> int:
    """Install or uninstall Memplex in local agent hosts."""
    from memplex.adapters.agent_installer import install_agent, uninstall_agent
    from memplex.config import load_config
    from memplex.product import apply_profile, setup_profile

    should_uninstall = getattr(args, "uninstall", False) or args.command == "uninstall"
    if should_uninstall:
        result = uninstall_agent(
            args.agent,
            target_dir=getattr(args, "target_dir", None),
            dry_run=getattr(args, "dry_run", False),
        )
    else:
        result = install_agent(
            args.agent,
            target_dir=getattr(args, "target_dir", None),
            user_id=getattr(args, "user_id", None),
            project_path=getattr(args, "project_path", None),
            dry_run=getattr(args, "dry_run", False),
        )
    profile_name = getattr(args, "profile", None)
    profile = setup_profile(profile_name)
    output = _dataclass_to_dict(result)
    if profile is not None:
        # Apply the profile's concrete settings to the loaded config and
        # surface what was applied vs what stays declarative (previously
        # the profile was only displayed, never applied).
        report = apply_profile(load_config(path=getattr(args, "config", None)), profile_name)
        output = {
            "profile": report["profile"],
            "applied": report["applied"],
            "declarative": report["declarative"],
            "result": output,
        }
    print(_fmt(output, args.output))
    return 0


def cmd_unsetup(args: argparse.Namespace) -> int:
    """Uninstall Memplex Claude Code plugin."""
    from memplex.adapters.agent_installer import uninstall_agent

    market_dir = _get_marketplace_dir()

    print("Memplex Plugin Uninstall")
    print("=" * 40)

    if not market_dir.exists():
        print("  Plugin not installed (directory not found).")
        return 0

    uninstall_agent("claude-code", target_dir=market_dir.parents[2])
    print(f"  Removed: {market_dir}")
    print("\nMemplex plugin uninstalled. Restart Claude Code to apply.")
    return 0


# ── Argument parser ────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Build and return the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="memplex",
        description="Memplex -- multi-agent memory system",
    )
    parser.add_argument("--config", default=None, help="Path to config YAML file")
    parser.add_argument(
        "--output",
        choices=["json", "table"],
        default="table",
        help="Output format (default: table)",
    )

    sub = parser.add_subparsers(dest="command", help="Available commands")

    _add_query_parsers(sub)
    _add_write_parsers(sub)
    _add_review_diag_parsers(sub)
    _add_product_parsers(sub)
    _add_agent_parsers(sub)
    _add_setup_parsers(sub)
    _add_sync_parsers(sub)
    _add_storage_parsers(sub)
    _add_operations_parsers(sub)
    _add_benchmark_parsers(sub)

    return parser


# ── Parser builders (split by domain from build_parser) ──────────────
# Each helper registers one cluster of subcommands on the shared ``sub``
# subparsers object. build_parser() just calls them in order. Adding a
# new command means extending the relevant helper (or adding a new one)
# instead of editing a 230-line function.


def _add_query_parsers(sub) -> None:
    """query + recall + observations (the recall-style commands)."""
    p_query = sub.add_parser("query", help="Query memory")
    p_query.add_argument("text", help="Query text")
    p_query.add_argument("--top-k", type=int, default=10, help="Max results")
    p_query.add_argument("--max-tokens", type=int, default=4000, help="Token budget")
    p_query.add_argument(
        "--explain",
        action="store_true",
        help="Explain retrieval stages, scores, filters, and token budget",
    )

    p_recall = sub.add_parser("recall", help="Recall memory (alias for query)")
    p_recall.add_argument("text", help="Recall query")
    p_recall.add_argument("--top-k", type=int, default=10, help="Max results")
    p_recall.add_argument("--max-tokens", type=int, default=4000, help="Token budget")
    p_recall.add_argument("--explain", action="store_true", help="Explain retrieval stages")

    p_obs = sub.add_parser("observations", help="List captured observation events")
    p_obs.add_argument(
        "--category",
        choices=["bugfix", "decision", "change", "discovery", "note"],
        default=None,
        help="Filter by observation category",
    )
    p_obs.add_argument("--limit", type=int, default=100, help="Max results (default 100)")


def _add_write_parsers(sub) -> None:
    """write / get / delete / feedback (memory mutation commands)."""
    p_write = sub.add_parser("write", help="Write content to memory")
    p_write.add_argument("--text", help="Raw text to write")
    p_write.add_argument("--file", help="File path to read and write")
    p_write.add_argument("--url", help="URL to write")

    p_get = sub.add_parser("get", help="Get memory by ID")
    p_get.add_argument("memory_id", help="Memory ID")

    p_del = sub.add_parser("delete", help="Delete memory by ID")
    p_del.add_argument("memory_id", help="Memory ID")

    p_fb = sub.add_parser("feedback", help="Submit feedback on a memory field")
    p_fb.add_argument("memory_id", help="Memory ID")
    p_fb.add_argument("--role", required=True, help="Field role (trigger|action|condition|benefit)")
    p_fb.add_argument("--index", type=int, required=True, help="Value index")
    p_fb.add_argument(
        "--verdict",
        required=True,
        choices=["correct", "wrong"],
        help="Verdict",
    )


def _add_review_diag_parsers(sub) -> None:
    """pending / compact / health / stats / doctor (review + diagnostics)."""
    sub.add_parser("pending", help="List pending reviews")

    p_compact = sub.add_parser("compact", help="Run compaction pipeline")
    p_compact.add_argument(
        "--scope",
        default="project",
        choices=["session", "project", "global"],
        help="Compaction scope (default: project)",
    )

    p_health = sub.add_parser("health", help="Health check")
    p_health.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 unless status is healthy (default: warning also exits 0)",
    )
    p_readiness = sub.add_parser(
        "readiness", help="Report industrial production-readiness gates"
    )
    p_readiness.add_argument(
        "--strict",
        action="store_true",
        help="Exit 1 until every required industrial gate has machine evidence",
    )
    sub.add_parser("stats", help="Show statistics")

    p_doctor = sub.add_parser("doctor", help="Check Memplex product readiness")
    p_doctor.add_argument("--agent", default="codex")
    p_doctor.add_argument("--profile", choices=["local", "privacy", "max-recall", "team"])
    p_doctor.add_argument("--smoke", action="store_true", help="Run capture/recall smoke")
    p_doctor.add_argument("--fix", action="store_true", help="Run safe local smoke checks")
    p_doctor.add_argument("--target-dir", default=None)
    p_doctor.add_argument("--user-id", default=None)
    p_doctor.add_argument("--session-id", default="default")
    p_doctor.add_argument("--project-path", default=None)


def _add_product_parsers(sub) -> None:
    """scope / policy / inbox / corpus / report (operator workflow commands)."""
    # -- scope --
    p_scope = sub.add_parser("scope", help="Explain visibility scopes")
    scope_sub = p_scope.add_subparsers(dest="scope_command", help="Scope command")
    scope_sub.add_parser("list", help="List visibility scopes")
    for name in ("explain", "preview"):
        p_scope_cmd = scope_sub.add_parser(name, help=f"{name.title()} agent namespace")
        p_scope_cmd.add_argument("--agent", default="codex")
        p_scope_cmd.add_argument("--user-id", default=None)
        p_scope_cmd.add_argument("--session-id", default="default")
        p_scope_cmd.add_argument("--project-path", default=None)
        if name == "preview":
            p_scope_cmd.add_argument("--limit", type=int, default=10)

    # -- policy --
    p_policy = sub.add_parser("policy", help="Show recall/capture policy")
    policy_sub = p_policy.add_subparsers(dest="policy_command", help="Policy command")
    p_policy_show = policy_sub.add_parser("show", help="Show current policy")
    p_policy_show.add_argument("--agent", default="codex")

    # -- inbox --
    p_inbox = sub.add_parser("inbox", help="Review pending memory inbox")
    inbox_sub = p_inbox.add_subparsers(dest="inbox_command", help="Inbox command")
    p_inbox_list = inbox_sub.add_parser("list", help="List pending reviews")
    p_inbox_list.add_argument("--limit", type=int, default=100)
    p_inbox_show = inbox_sub.add_parser("show", help="Show pending review and memory")
    p_inbox_show.add_argument("memory_id")
    for name in ("accept", "reject"):
        p_inbox_resolve = inbox_sub.add_parser(name, help=f"{name.title()} pending review")
        p_inbox_resolve.add_argument("memory_id")
        p_inbox_resolve.add_argument("--field-role", required=True)
    p_inbox_merge = inbox_sub.add_parser("merge", help="Merge a replacement value")
    p_inbox_merge.add_argument("memory_id")
    p_inbox_merge.add_argument("--field-role", required=True)
    p_inbox_merge.add_argument("--value", required=True)

    # -- corpus --
    p_corpus = sub.add_parser("corpus", help="Manifest-driven canonical corpus")
    corpus_sub = p_corpus.add_subparsers(dest="corpus_command", help="Corpus command")
    p_corpus_preview = corpus_sub.add_parser("preview", help="Preview manifest files")
    p_corpus_preview.add_argument("--manifest", required=True)
    p_corpus_preview.add_argument("--limit", type=int, default=100)
    p_corpus_index = corpus_sub.add_parser("index", help="Index selected corpus files")
    p_corpus_index.add_argument("--manifest", required=True)
    p_corpus_index.add_argument("--dry-run", action="store_true")
    p_corpus_recall = corpus_sub.add_parser("recall", help="Recall indexed corpus entries")
    p_corpus_recall.add_argument("query")
    p_corpus_recall.add_argument("--top-k", type=int, default=10)
    p_corpus_recall.add_argument("--max-tokens", type=int, default=4000)

    # -- report --
    p_report = sub.add_parser("report", help="Generate an operator report")
    p_report.add_argument("--agent", default="codex")


def _add_agent_parsers(sub) -> None:
    """agent (nested: list / manifest / install / uninstall / recall / capture)."""
    p_agent = sub.add_parser("agent", help="Portable agent integration commands")
    agent_sub = p_agent.add_subparsers(dest="agent_command", help="Agent integration command")
    agent_sub.add_parser("list", help="List supported agent profiles")

    p_agent_manifest = agent_sub.add_parser("manifest", help="Show agent manifest")
    p_agent_manifest.add_argument(
        "--agent",
        default="codex",
        help="Agent id: codex | claude-code | openclaw | hermes | all",
    )

    p_agent_status = agent_sub.add_parser(
        "status", help="Show read-only host, identity, scope, and install diagnostics"
    )
    p_agent_status.add_argument(
        "--agent",
        default="codex",
        help="Agent id: codex | claude-code | openclaw | hermes | all",
    )
    p_agent_status.add_argument("--target-dir", default=None)
    p_agent_status.add_argument("--user-id", default=None)
    p_agent_status.add_argument("--session-id", default="default")
    p_agent_status.add_argument("--project-path", default=None)

    p_agent_install = agent_sub.add_parser("install", help="Install Memplex into an agent host")
    p_agent_install.add_argument(
        "--agent",
        default="all",
        help="Agent id: auto | codex | claude-code | openclaw | hermes | all",
    )
    p_agent_install.add_argument(
        "--target-dir",
        default=None,
        help="Override the agent config root directory for this install",
    )
    p_agent_install.add_argument("--user-id", default=None)
    p_agent_install.add_argument("--project-path", default=None)
    p_agent_install.add_argument(
        "--dry-run", action="store_true", help="Show planned files without writing"
    )

    p_agent_uninstall = agent_sub.add_parser(
        "uninstall", help="Uninstall Memplex from an agent host"
    )
    p_agent_uninstall.add_argument(
        "--agent",
        default="all",
        help="Agent id: auto | codex | claude-code | openclaw | hermes | all",
    )
    p_agent_uninstall.add_argument(
        "--target-dir",
        default=None,
        help="Override the agent config root directory for this uninstall",
    )
    p_agent_uninstall.add_argument(
        "--dry-run", action="store_true", help="Show planned files without writing"
    )

    p_agent_recall = agent_sub.add_parser("recall", help="Recall memories for prompt")
    p_agent_recall.add_argument("prompt", help="Prompt to recall against")
    p_agent_recall.add_argument("--agent", default="codex")
    p_agent_recall.add_argument("--user-id", default=None)
    p_agent_recall.add_argument("--session-id", default="default")
    p_agent_recall.add_argument("--project-path", default=None)
    p_agent_recall.add_argument("--top-k", type=int, default=5)
    p_agent_recall.add_argument("--token-budget", type=int, default=1500)

    p_agent_capture = agent_sub.add_parser("capture", help="Capture a completed agent turn")
    p_agent_capture.add_argument("--agent", default="codex")
    p_agent_capture.add_argument("--user-id", default=None)
    p_agent_capture.add_argument("--session-id", default="default")
    p_agent_capture.add_argument("--project-path", default=None)
    p_agent_capture.add_argument("--user-message", required=True)
    p_agent_capture.add_argument("--assistant-message", required=True)
    p_agent_capture.add_argument("--next-prompt-hint", default=None)


def _add_setup_parsers(sub) -> None:
    """setup / install / stepup / uninstall / unsetup (top-level install aliases)."""
    for name in ("setup", "install", "stepup"):
        _add_one_setup_parser(sub, name, uninstall=False)
    _add_one_setup_parser(sub, "uninstall", uninstall=True)

    sub.add_parser("unsetup", help="Uninstall Memplex Claude Code plugin")


def _add_one_setup_parser(sub, name: str, *, uninstall: bool = False):
    help_text = (
        "Uninstall Memplex from local agent hosts"
        if uninstall
        else "Set up Memplex in detected local agent hosts"
    )
    p_setup = sub.add_parser(name, help=help_text)
    p_setup.add_argument(
        "--agent",
        default="auto",
        help="Agent id: auto | codex | claude-code | openclaw | hermes | all",
    )
    p_setup.add_argument(
        "--target-dir",
        default=None,
        help="Override the selected agent config root directory",
    )
    p_setup.add_argument("--user-id", default=None)
    p_setup.add_argument("--project-path", default=None)
    if not uninstall:
        p_setup.add_argument(
            "--profile",
            choices=["local", "privacy", "max-recall", "team"],
            default=None,
            help="Transparent setup profile",
        )
    p_setup.add_argument(
        "--dry-run", action="store_true", help="Show planned files without writing"
    )
    if not uninstall:
        p_setup.add_argument(
            "--uninstall", action="store_true", help="Uninstall instead of install"
        )
    return p_setup


def _add_sync_parsers(sub) -> None:
    """sync lifecycle and durable-delivery controls."""
    p_sync = sub.add_parser("sync", help="Reliable multi-node sync")
    sync_sub = p_sync.add_subparsers(dest="sync_command", help="Sync command")
    p_pull = sync_sub.add_parser(
        "pull", help="Pull bounded signed pages from one peer"
    )
    p_pull.add_argument("--target", default=None, help="Stable remote node id")
    sync_sub.add_parser("status", help="Show sync config and last-pull timestamp")
    p_drain = sync_sub.add_parser(
        "drain", help="Drain locally-originated durable deliveries"
    )
    p_drain.add_argument("--timeout", type=float, default=None)
    p_dlq = sync_sub.add_parser("dlq", help="Inspect or replay dead letters")
    dlq_sub = p_dlq.add_subparsers(dest="dlq_command", help="DLQ command")
    p_dlq_list = dlq_sub.add_parser("list", help="List dead-letter entries")
    p_dlq_list.add_argument("--limit", type=int, default=100)
    p_replay = dlq_sub.add_parser("replay", help="Replay one dead letter")
    p_replay.add_argument("--target", required=True, help="Stable remote node id")
    p_replay.add_argument("--event-id", required=True, help="Event UUID")


def _add_storage_parsers(sub) -> None:
    """Storage maintenance commands kept separate from service startup."""
    p_storage = sub.add_parser("storage", help="PostgreSQL storage maintenance")
    storage_sub = p_storage.add_subparsers(dest="storage_command", help="Storage command")
    p_migration = storage_sub.add_parser("migration", help="Inspect or apply PostgreSQL migrations")
    migration_sub = p_migration.add_subparsers(
        dest="migration_command", help="Migration command"
    )
    migration_sub.add_parser("status", help="Read current migration status")
    migration_sub.add_parser("plan", help="Read pending migration plan")
    p_apply = migration_sub.add_parser("apply", help="Apply pending migrations")
    p_apply.add_argument(
        "--dry-run", action="store_true", help="Validate and show the plan without writes"
    )
    p_backup = storage_sub.add_parser("backup", help="Create, verify, or restore backups")
    backup_sub = p_backup.add_subparsers(dest="backup_command", help="Backup command")
    p_backup_create = backup_sub.add_parser("create", help="Create a signed backup artifact")
    p_backup_create.add_argument("--destination", default=None)
    p_backup_verify = backup_sub.add_parser("verify", help="Verify a signed backup artifact")
    p_backup_verify.add_argument("artifact")
    p_backup_restore = backup_sub.add_parser("restore", help="Restore a verified backup")
    p_backup_restore.add_argument("artifact")
    p_backup_restore.add_argument("--target-schema", required=True)
    backup_sub.add_parser("pitr-status", help="Inspect PostgreSQL PITR prerequisites")
    p_backup_drill = backup_sub.add_parser("drill", help="Run a measured restore drill")
    p_backup_drill.add_argument("--artifact", required=True)
    p_backup_drill.add_argument("--target-schema", required=True)


def _add_operations_parsers(sub) -> None:
    """Production probes/SLO evidence commands that never construct service."""
    parser = sub.add_parser("operations", help="Production operations and SLO evidence")
    operations_sub = parser.add_subparsers(
        dest="operations_command", help="Operations command"
    )
    operations_sub.add_parser("status", help="Read the operations_slo readiness gate")
    verify = operations_sub.add_parser("verify-report", help="Verify a signed SLO report")
    verify.add_argument("report")
    operations_sub.add_parser("alerts-check", help="Verify packaged Prometheus alert rules")


def _add_benchmark_parsers(sub) -> None:
    """benchmark (nested: list / run) -- evaluation harness, source checkout only."""
    p_bench = sub.add_parser("benchmark", help="Benchmarks (available from the source checkout)")
    bench_sub = p_bench.add_subparsers(dest="benchmark_command", help="Benchmark command")
    bench_sub.add_parser("list", help="List available benchmark datasets")

    p_bench_run = bench_sub.add_parser("run", help="Run a benchmark dataset")
    p_bench_run.add_argument(
        "--dataset",
        required=True,
        help="Dataset name (see 'memplex benchmark list') or 'all'",
    )
    p_bench_run.add_argument(
        "--synthetic",
        action="store_true",
        help="Skip HuggingFace download and generate synthetic data directly",
    )
    p_bench_run.add_argument("--top-k", type=int, default=10, help="Retrieval top-K (default: 10)")
    p_bench_run.add_argument(
        "--output",
        dest="benchmark_output",
        default=".memplex/benchmarks/results.jsonl",
        help="Results JSONL file path (distinct from the global --output format flag)",
    )


# ── Entry point ─────────────────────────────────────────────────────


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point.

    Parameters
    ----------
    argv:
        Argument list.  Defaults to ``sys.argv[1:]``.
    """
    from memplex.logging_config import configure_logging

    configure_logging()

    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    dispatch = {
        "query": cmd_query,
        "recall": cmd_query,
        "observations": cmd_observations,
        "write": cmd_write,
        "get": cmd_get,
        "delete": cmd_delete,
        "feedback": cmd_feedback,
        "pending": cmd_pending,
        "compact": cmd_compact,
        "health": cmd_health,
        "readiness": cmd_readiness,
        "stats": cmd_stats,
        "doctor": cmd_doctor,
        "scope": cmd_scope,
        "policy": cmd_policy,
        "inbox": cmd_inbox,
        "corpus": cmd_corpus,
        "report": cmd_report,
        "agent": cmd_agent,
        "sync": cmd_sync,
        "storage": cmd_storage,
        "operations": cmd_operations,
        "benchmark": cmd_benchmark,
        "setup": cmd_setup,
        "install": cmd_setup,
        "stepup": cmd_setup,
        "uninstall": cmd_setup,
        "unsetup": cmd_unsetup,
    }

    handler = dispatch.get(args.command)
    if handler is None:
        parser.print_help()
        return 1

    try:
        return handler(args)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
