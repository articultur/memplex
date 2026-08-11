"""Operator-facing PostgreSQL migration command contracts.

These tests are intentionally independent from service construction: a
migration inspection must not initialise MemplexService, because startup may
perform migration work itself.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import memplex.config as config_module
import memplex.product as product_module
import memplex.storage.migrations as migrations_module
import memplex.storage.pool as pool_module
from memplex.adapters import cli
from memplex.storage import postgres as postgres_module
from memplex.storage.migrations import PostgresApplicationPrincipal, PostgresTargetIdentity
from memplex.storage.migrations.runner import VectorCapabilityRequest, VectorCapabilityStatus


def test_storage_migration_parser_exposes_only_operator_actions() -> None:
    """Removing the storage parser would make maintenance invocations impossible."""

    args = cli.build_parser().parse_args(["storage", "migration", "apply", "--dry-run"])

    assert args.command == "storage"
    assert args.storage_command == "migration"
    assert args.migration_command == "apply"
    assert args.dry_run is True


def test_status_uses_migration_context_without_constructing_service(monkeypatch, capsys) -> None:
    """Routing status through MemplexService would permit startup-time writes."""

    class Context:
        def status(self):
            return {
                "state": "ready",
                "current_version": 4,
                "known_version": 4,
                "pending": [],
            }

    monkeypatch.setattr(cli, "_build_migration_command_context", lambda _path: Context(), raising=False)
    monkeypatch.setattr(
        cli,
        "_make_service",
        lambda _path=None: (_ for _ in ()).throw(AssertionError("service startup is forbidden")),
    )

    assert cli.main(["--output", "json", "storage", "migration", "status"]) == 0

    assert json.loads(capsys.readouterr().out)["state"] == "ready"


def test_migration_verification_report_is_unsigned_local_diagnostic(monkeypatch) -> None:
    """Marking local evidence signed or gate-closing would overstate its authority."""

    reporter = getattr(product_module, "migration_verification_report", None)
    assert callable(reporter), "migration verification report is missing"
    store = object.__new__(postgres_module.PostgresMemoryStore)
    store._ready_pool = SimpleNamespace(
        target=PostgresTargetIdentity("memplex", "tenant_a", "127.0.0.1", 5432),
        request=VectorCapabilityRequest(dim=0, policy="disabled"),
        status=VectorCapabilityStatus(state="disabled", dim=0),
    )
    monkeypatch.setattr(pool_module, "validate_ready_postgres_pool", lambda value: value)

    report = reporter(store)

    assert report["schema_version"] == 1
    assert report["signed"] is False
    assert report["local_diagnostic_only"] is True
    assert report["industrial_gate_closing"] is False
    assert report["ledger"]["known_version"] >= 1
    assert report["capability"] == {"state": "disabled", "dim": 0}
    assert "127.0.0.1" not in json.dumps(report)


def test_migration_verification_report_rejects_an_unsealed_ready_pool() -> None:
    """Trusting a lookalike pool would turn unverified state into an operator report."""

    store = object.__new__(postgres_module.PostgresMemoryStore)
    store._ready_pool = SimpleNamespace(
        target=PostgresTargetIdentity("memplex", "tenant_a", "127.0.0.1", 5432),
        request=VectorCapabilityRequest(dim=0, policy="disabled"),
        status=VectorCapabilityStatus(state="disabled", dim=0),
    )

    with pytest.raises(ValueError, match="verified storage readiness seal"):
        product_module.migration_verification_report(store)


def test_plan_and_dry_run_do_not_construct_service_or_apply(monkeypatch, capsys) -> None:
    """Replacing read-only migration actions with apply would create ledger writes."""

    calls: list[str] = []

    class Context:
        def plan(self):
            calls.append("plan")
            return {
                "state": "upgrade_required",
                "current_version": 3,
                "known_version": 4,
                "pending": [{"version": 4, "name": "edge_integrity"}],
            }

        def apply(self):  # pragma: no cover - the test proves this stays unreachable
            raise AssertionError("read-only command invoked apply")

    monkeypatch.setattr(cli, "_build_migration_command_context", lambda _path: Context())
    monkeypatch.setattr(
        cli,
        "_make_service",
        lambda _path=None: (_ for _ in ()).throw(AssertionError("service startup is forbidden")),
    )

    assert cli.main(["--output", "json", "storage", "migration", "plan"]) == 0
    plan_output = json.loads(capsys.readouterr().out)
    assert cli.main(["--output", "json", "storage", "migration", "apply", "--dry-run"]) == 0
    dry_run_output = json.loads(capsys.readouterr().out)

    assert plan_output["command"] == "plan"
    assert dry_run_output["dry_run"] is True
    assert calls == ["plan", "plan"]


def test_apply_defers_strict_acl_check_to_independent_ready_readback() -> None:
    """A strict ACL plan before first DDL would reject the approved bootstrap path."""

    target = PostgresTargetIdentity("app", "tenant_a", "127.0.0.1", 5432)
    principal = PostgresApplicationPrincipal("memplex_app", "memplex_app")
    mutation = SimpleNamespace(state="ready")
    ready = SimpleNamespace(state="ready")
    calls: list[str] = []

    class MigrationRunner:
        def plan(self, **_kwargs):
            raise AssertionError("apply must not run a strict external plan before first DDL")

        def apply(self, **_kwargs):
            calls.append("apply")
            return mutation

        def status(self, **_kwargs):
            calls.append("status")
            return ready

    class ApplicationRunner:
        def inspect_target(self):
            calls.append("inspect_target")
            return target

        def inspect_application_principal(self, *, expected_target):
            calls.append("inspect_principal")
            assert expected_target == target
            return principal

    context = cli._MigrationCommandContext(
        MigrationRunner(),
        ApplicationRunner(),
        target,
        principal,
        object(),
        "production",
    )

    assert context.apply() == (mutation, ready)
    assert calls == ["apply", "inspect_target", "inspect_principal", "status"]


def test_storage_command_error_does_not_echo_dsn_or_driver_error(monkeypatch, capsys) -> None:
    """Returning a raw migration error would leak connection credentials."""

    monkeypatch.setattr(
        cli,
        "_build_migration_command_context",
        lambda _path: (_ for _ in ()).throw(
            RuntimeError("postgresql://operator:secret@example.invalid/db SQL=SELECT payload=private")
        ),
    )

    assert cli.main(["--output", "json", "storage", "migration", "status"]) == 1

    result = json.loads(capsys.readouterr().err)
    assert result["code"] == "migration_command_failed"
    assert "postgresql://" not in json.dumps(result)
    assert "secret" not in json.dumps(result)


def test_apply_reports_committed_acl_remediation_after_nonready_readback(monkeypatch, capsys) -> None:
    """Committed bootstrap DDL needs an operator ACL fix, not a false success or auto-GRANT."""

    target = PostgresTargetIdentity("app", "tenant_a", "127.0.0.1", 5432)
    principal = PostgresApplicationPrincipal("memplex_app", "memplex_app")
    calls: list[str] = []

    class MigrationRunner:
        def apply(self, **_kwargs):
            calls.append("apply")
            return SimpleNamespace(state="ready")

        def status(self, **_kwargs):
            calls.append("status")
            return SimpleNamespace(state="blocked")

    class ApplicationRunner:
        def inspect_target(self):
            calls.append("inspect_target")
            return target

        def inspect_application_principal(self, *, expected_target):
            calls.append("inspect_principal")
            assert expected_target == target
            return principal

    context = cli._MigrationCommandContext(
        MigrationRunner(),
        ApplicationRunner(),
        target,
        principal,
        object(),
        "production",
    )
    monkeypatch.setattr(cli, "_build_migration_command_context", lambda _path: context)

    assert cli.main(["--output", "json", "storage", "migration", "apply"]) == 1

    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == "migration_committed_acl_remediation_required"
    assert "postgresql://" not in json.dumps(payload)
    assert "memplex_app" not in json.dumps(payload)
    assert calls == ["apply", "inspect_target", "inspect_principal", "status"]


def test_apply_exception_returns_fresh_ready_readback_without_driver_error(
    monkeypatch, capsys
) -> None:
    """A close-time apply exception cannot hide a newly confirmed ready ledger."""

    target = PostgresTargetIdentity("app", "tenant_a", "127.0.0.1", 5432)
    principal = PostgresApplicationPrincipal("memplex_app", "memplex_app")
    calls: list[str] = []

    class MigrationRunner:
        def apply(self, **_kwargs):
            calls.append("apply")
            raise RuntimeError("postgresql://migration:secret@example.invalid/app SQL=COMMIT")

        def status(self, **_kwargs):
            calls.append("status")
            return SimpleNamespace(state="ready", current_version=4, known_version=4, pending=())

    class ApplicationRunner:
        def inspect_target(self):
            calls.append("inspect_target")
            return target

        def inspect_application_principal(self, *, expected_target):
            calls.append("inspect_principal")
            assert expected_target == target
            return principal

    context = cli._MigrationCommandContext(
        MigrationRunner(),
        ApplicationRunner(),
        target,
        principal,
        object(),
        "production",
    )
    monkeypatch.setattr(cli, "_build_migration_command_context", lambda _path: context)

    assert cli.main(["--output", "json", "storage", "migration", "apply"]) == 0

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["outcome"] == "readback_confirmed"
    assert payload["readback"]["state"] == "ready"
    assert "mutation" not in payload
    assert captured.err == ""
    assert "postgresql://" not in captured.out
    assert "secret" not in captured.out
    assert calls == ["apply", "inspect_target", "inspect_principal", "status"]


@pytest.mark.parametrize(
    ("output", "readback", "expected_code"),
    (
        ("json", "upgrade_required", "migration_failed"),
        ("table", "blocked", "migration_outcome_requires_readback"),
        ("json", "acl_error", "migration_outcome_requires_readback"),
    ),
)
def test_apply_exception_requires_sanitized_readback_outcome(
    monkeypatch, capsys, output, readback, expected_code
) -> None:
    """An uncertain mutation remains nonzero unless independent strict readback proves ready."""

    target = PostgresTargetIdentity("app", "tenant_a", "127.0.0.1", 5432)
    principal = PostgresApplicationPrincipal("memplex_app", "memplex_app")

    class MigrationRunner:
        def apply(self, **_kwargs):
            raise RuntimeError("postgresql://migration:secret@example.invalid/app SQL=COMMIT")

        def status(self, **_kwargs):
            if readback == "acl_error":
                raise RuntimeError(
                    "postgresql://application:secret@example.invalid/app SQL=SELECT payload=private"
                )
            return SimpleNamespace(state=readback)

    class ApplicationRunner:
        def inspect_target(self):
            return target

        def inspect_application_principal(self, *, expected_target):
            assert expected_target == target
            return principal

    context = cli._MigrationCommandContext(
        MigrationRunner(),
        ApplicationRunner(),
        target,
        principal,
        object(),
        "production",
    )
    monkeypatch.setattr(cli, "_build_migration_command_context", lambda _path: context)

    assert cli.main(["--output", output, "storage", "migration", "apply"]) == 1

    error = capsys.readouterr().err
    assert expected_code in error
    assert "postgresql://" not in error
    assert "secret" not in error
    assert "SQL=" not in error
    assert "payload" not in error


@pytest.mark.parametrize("drift", ("target", "principal"))
def test_apply_rejects_committed_application_binding_drift_before_readback(drift) -> None:
    """A post-commit target or login change cannot borrow the initial ACL binding."""

    target = PostgresTargetIdentity("app", "tenant_a", "127.0.0.1", 5432)
    principal = PostgresApplicationPrincipal("memplex_app", "memplex_app")
    calls: list[str] = []

    class MigrationRunner:
        def apply(self, **_kwargs):
            calls.append("apply")
            return SimpleNamespace(state="ready")

        def status(self, **_kwargs):
            raise AssertionError("strict readback must not run after binding drift")

    class ApplicationRunner:
        def inspect_target(self):
            calls.append("inspect_target")
            if drift == "target":
                return PostgresTargetIdentity("app", "tenant_a", "127.0.0.2", 5432)
            return target

        def inspect_application_principal(self, *, expected_target):
            calls.append("inspect_principal")
            assert expected_target == target
            if drift == "principal":
                return PostgresApplicationPrincipal("other_role", "other_role")
            return principal

    context = cli._MigrationCommandContext(
        MigrationRunner(),
        ApplicationRunner(),
        target,
        principal,
        object(),
        "production",
    )

    with pytest.raises(
        cli._MigrationCommandError, match="migration_committed_acl_remediation_required"
    ):
        context.apply()

    expected_calls = ["apply", "inspect_target"]
    if drift == "principal":
        expected_calls.append("inspect_principal")
    assert calls == expected_calls


def test_migration_context_rejects_nonpostgres_without_constructing_service(monkeypatch, capsys) -> None:
    """Accepting Lite here would make a production migration command select the wrong backend."""

    config = config_module.MemplexConfig()
    config.storage.backend = "lite"
    monkeypatch.setattr(config_module, "load_config", lambda path=None: config)
    monkeypatch.setattr(
        cli,
        "_make_service",
        lambda _path=None: (_ for _ in ()).throw(AssertionError("service startup is forbidden")),
    )

    assert cli.main(["--output", "json", "storage", "migration", "status"]) == 1

    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == "postgres_backend_required"


@pytest.mark.parametrize("profile", ("production", "development"))
def test_migration_context_binds_exact_application_target_principal_and_acl(monkeypatch, profile) -> None:
    """Skipping target/ACL binding could apply migration credentials to a different data plane."""

    config = config_module.MemplexConfig()
    config.deployment.profile = profile
    config.storage.backend = "postgres"
    config.storage.path = "postgresql://application:secret@host/app"
    config.storage.migration_dsn = "postgresql://migration:secret@host/app"
    application_target = PostgresTargetIdentity("app", "tenant_a", "127.0.0.1", 5432)
    calls: list[tuple[str, dict]] = []

    class Runner:
        def __init__(self, dsn):
            self.is_application = dsn == config.storage.path

        def inspect_target(self):
            return application_target

        def inspect_application_principal(self, *, expected_target):
            assert self.is_application is True
            assert expected_target == application_target
            return PostgresApplicationPrincipal("memplex_app", "memplex_app")

        def plan(self, **kwargs):
            calls.append(("plan", kwargs))
            return SimpleNamespace(state="ready")

    monkeypatch.setattr(config_module, "load_config", lambda path=None: config)
    monkeypatch.setattr(migrations_module, "PostgresMigrationRunner", Runner)

    context = cli._build_migration_command_context()
    assert context.plan().state == "ready"
    _, options = calls[0]
    assert options["expected_target"] == application_target
    assert options["application_acl"].role == "memplex_app"
    assert options["deployment_profile"] == profile


def test_migration_context_rejects_uninspectable_resolved_target_before_mutation(
    monkeypatch, capsys
) -> None:
    """Treating two malformed targets as equal could direct maintenance at an unknown database."""

    config = config_module.MemplexConfig()
    config.deployment.profile = "production"
    config.storage.backend = "postgres"
    config.storage.path = "postgresql://application:secret@host/app"
    config.storage.migration_dsn = "postgresql://migration:secret@host/app"
    apply_calls: list[str] = []

    class Runner:
        def __init__(self, _dsn):
            pass

        def inspect_target(self):
            return object()

        def inspect_application_principal(self, *, expected_target):
            del expected_target
            return SimpleNamespace(role="memplex_app")

        def apply(self, **_kwargs):
            apply_calls.append("apply")
            raise AssertionError("invalid target must fail before mutation")

    monkeypatch.setattr(config_module, "load_config", lambda path=None: config)
    monkeypatch.setattr(migrations_module, "PostgresMigrationRunner", Runner)

    assert cli.main(["--output", "json", "storage", "migration", "apply"]) == 1

    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == "postgres_target_invalid"
    assert apply_calls == []


def test_migration_target_key_rejects_ambiguous_or_nonexact_target() -> None:
    """Equating Unix-socket or lookalike targets could route DDL to another cluster."""

    TargetSubclass = type("TargetSubclass", (PostgresTargetIdentity,), {})
    candidates = (
        PostgresTargetIdentity("app", "tenant_a", None, None),
        PostgresTargetIdentity("app", "tenant_a", None, 5432),
        PostgresTargetIdentity("app", "tenant_a", "127.0.0.1", None),
        SimpleNamespace(
            database="app", schema="tenant_a", server_address="127.0.0.1", server_port=5432
        ),
        TargetSubclass("app", "tenant_a", "127.0.0.1", 5432),
    )

    for candidate in candidates:
        with pytest.raises(cli._MigrationCommandError, match="postgres_target_invalid"):
            cli._migration_target_key(candidate)


def test_migration_target_key_never_equates_distinct_unix_socket_clusters() -> None:
    """Identical database/schema fields cannot identify two different socket endpoints."""

    application_socket_cluster = PostgresTargetIdentity("app", "tenant_a", None, None)
    migration_socket_cluster = PostgresTargetIdentity("app", "tenant_a", None, None)

    for target in (application_socket_cluster, migration_socket_cluster):
        with pytest.raises(cli._MigrationCommandError, match="postgres_target_invalid"):
            cli._migration_target_key(target)


def test_migration_context_rejects_indistinguishable_unix_socket_clusters(monkeypatch) -> None:
    """Missing host and port must fail before two Unix-socket clusters can be equated."""

    config = config_module.MemplexConfig()
    config.deployment.profile = "production"
    config.storage.backend = "postgres"
    config.storage.path = "postgresql://application:secret@host/app"
    config.storage.migration_dsn = "postgresql://migration:secret@host/app"
    unresolved_socket_target = PostgresTargetIdentity("app", "tenant_a", None, None)

    class Runner:
        def __init__(self, _dsn):
            pass

        def inspect_target(self):
            return unresolved_socket_target

        def inspect_application_principal(self, *, expected_target):
            assert expected_target == unresolved_socket_target
            return PostgresApplicationPrincipal("memplex_app", "memplex_app")

    monkeypatch.setattr(config_module, "load_config", lambda path=None: config)
    monkeypatch.setattr(migrations_module, "PostgresMigrationRunner", Runner)

    with pytest.raises(cli._MigrationCommandError, match="postgres_target_invalid"):
        cli._build_migration_command_context()


@pytest.mark.parametrize("profile", ("production", "development"))
def test_cli_rejects_set_role_application_principal_without_leaking_dsn(
    monkeypatch, capsys, profile
) -> None:
    """Accepting current_user=app with a privileged session_user would permit SET ROLE escalation."""

    config = config_module.MemplexConfig()
    config.deployment.profile = profile
    config.storage.backend = "postgres"
    config.storage.path = "postgresql://application:secret@host/app"
    config.storage.migration_dsn = "postgresql://migration:secret@host/app"
    target = PostgresTargetIdentity("app", "tenant_a", "127.0.0.1", 5432)
    apply_calls: list[str] = []

    class Runner:
        def __init__(self, _dsn):
            pass

        def inspect_target(self):
            return target

        def inspect_application_principal(self, *, expected_target):
            assert expected_target == target
            return PostgresApplicationPrincipal("memplex_app", "migration_owner")

        def apply(self, **_kwargs):
            apply_calls.append("apply")
            raise AssertionError("invalid application principal must fail before mutation")

    monkeypatch.setattr(config_module, "load_config", lambda path=None: config)
    monkeypatch.setattr(migrations_module, "PostgresMigrationRunner", Runner)

    assert cli.main(["--output", "json", "storage", "migration", "apply"]) == 1

    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == "postgres_application_principal_invalid"
    assert "postgresql://" not in json.dumps(payload)
    assert "secret" not in json.dumps(payload)
    assert apply_calls == []


def test_migration_context_rejects_nonexact_or_weak_application_principal(monkeypatch) -> None:
    """Duck-typed or weak principal fields must not become an ACL contract."""

    PrincipalSubclass = type("PrincipalSubclass", (PostgresApplicationPrincipal,), {})
    target = PostgresTargetIdentity("app", "tenant_a", "127.0.0.1", 5432)
    config = config_module.MemplexConfig()
    config.deployment.profile = "production"
    config.storage.backend = "postgres"
    config.storage.path = "postgresql://application:secret@host/app"
    config.storage.migration_dsn = "postgresql://migration:secret@host/app"

    class Runner:
        principal = None

        def __init__(self, _dsn):
            pass

        def inspect_target(self):
            return target

        def inspect_application_principal(self, *, expected_target):
            assert expected_target == target
            return self.principal

    monkeypatch.setattr(config_module, "load_config", lambda path=None: config)
    monkeypatch.setattr(migrations_module, "PostgresMigrationRunner", Runner)

    for principal in (
        SimpleNamespace(role="memplex_app", session_role="memplex_app"),
        PrincipalSubclass("memplex_app", "memplex_app"),
        SimpleNamespace(role="memplex_app", session_role=True),
        SimpleNamespace(role="", session_role=""),
    ):
        Runner.principal = principal
        with pytest.raises(cli._MigrationCommandError, match="postgres_application_principal_invalid"):
            cli._build_migration_command_context()
