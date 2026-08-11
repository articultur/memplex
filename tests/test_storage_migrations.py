"""Contract tests for packaged PostgreSQL schema migrations."""

from __future__ import annotations

import importlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


def _migrations():
    return importlib.import_module("memplex.storage.migrations")


def _migration(version: int):
    return next(item for item in _migrations().discover_migrations() if item.version == version)


def _build_wheel(tmp_path: Path) -> Path:
    dist = tmp_path / "dist"
    repository = Path(__file__).parents[1]
    candidates = (sys.executable, repository / ".venv-pgcheck" / "bin" / "python")
    build_python = next(
        (
            candidate
            for candidate in candidates
            if Path(candidate).exists()
            and subprocess.run(
                [str(candidate), "-c", "import build"],
                check=False,
                cwd=tmp_path,
                capture_output=True,
            ).returncode
            == 0
        ),
        None,
    )
    if build_python is None:
        raise RuntimeError("a Python environment with the build module is required")
    subprocess.run(
        [
            str(build_python),
            "-m",
            "build",
            "--wheel",
            "--no-isolation",
            "--outdir",
            str(dist),
            str(repository),
        ],
        check=True,
        # The dirty shared checkout has an ignored ``build/`` artefact.
        # Invoke the installed build module from a clean temporary CWD while
        # retaining the repository as the explicit source directory.
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    return next(dist.glob("memplex-*.whl"))


def _install_wheel_in_isolated_venv(wheel: Path, tmp_path: Path) -> Path:
    environment = tmp_path / "installed"
    # The pgserver runtime's uv-managed interpreter cannot be relocated into a
    # macOS venv (its libpython rpath is absolute).  The local CPython 3.13 is
    # relocatable; use it only to prove the built wheel installs in isolation.
    bootstrap_python = shutil.which("python3.13")
    if bootstrap_python is None:
        raise RuntimeError("python3.13 is required to create the isolated wheel test environment")
    subprocess.run(
        [bootstrap_python, "-m", "venv", "--without-pip", str(environment)],
        check=True,
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    python = environment / "bin" / "python"
    uv = shutil.which("uv")
    if uv is None:
        raise RuntimeError("uv is required to install the wheel in the isolated test environment")
    try:
        subprocess.run(
            [uv, "pip", "install", "--python", str(python), str(wheel)],
            check=True,
            capture_output=True,
            text=True,
            cwd=tmp_path,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"isolated wheel installation failed: {exc.stderr}") from exc
    return python


def _run(python: Path, code: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            [str(python), "-c", code],
            check=True,
            cwd=cwd,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"isolated wheel invocation failed: {exc.stderr}") from exc


def test_discover_migrations_from_built_wheel_contains_all_sql(tmp_path: Path) -> None:
    """Missing package data must not make wheel installations lose SQL migrations."""
    wheel = _build_wheel(tmp_path)
    isolated = _install_wheel_in_isolated_venv(wheel, tmp_path)
    result = _run(
        isolated,
        "from memplex.storage.migrations import discover_migrations; "
        "print([migration.version for migration in discover_migrations()])",
        tmp_path,
    )
    assert result.stdout.strip() == "[1, 2, 3, 4, 5]"


@pytest.mark.parametrize(
    "variant",
    (
        "post_g002_runtime_v1_reliable_sync_v5_vector_1536",
        "post_g002_runtime_v1_current_reliable_sync_v5_vector_1536",
        "post_g002_runtime_v1_edge_integrity_current_reliable_sync_v5_vector_1536",
    ),
)
def test_allowed_adoption_baselines_normalizes_reliable_sync_vector_variants(variant: str) -> None:
    """A v5 suffix cannot erase the current-vector adoption lineage."""
    runner = importlib.import_module("memplex.storage.migrations.runner")

    assert runner._allowed_adoption_baselines(variant) == frozenset(
        {
            "post_g002_runtime_v1_vector_1536",
            "post_g002_runtime_v1_feedback_v1_vector_1536",
            "post_g002_runtime_v1_current_vector_1536",
        }
    )


def test_allowed_adoption_baselines_keeps_fixed_and_noncurrent_vector_behavior() -> None:
    """The v5-vector normalization must not broaden unrelated adoption paths."""
    runner = importlib.import_module("memplex.storage.migrations.runner")

    assert runner._allowed_adoption_baselines("post_g002_runtime_v1_current_reliable_sync_v5") == frozenset(
        {
            "post_g002_runtime_v1",
            "post_g002_runtime_v1_feedback_v1",
            "post_g002_runtime_v1_current",
        }
    )
    assert runner._allowed_adoption_baselines("post_g002_runtime_v1_reliable_sync_v5") == frozenset()
    assert runner._allowed_adoption_baselines("post_g002_runtime_v1_vector_1536") == frozenset()


def test_non_0002_transaction_control_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """Only the immutable ACL migration may carry a transaction shell."""
    migrations = _migrations()
    runner_module = importlib.import_module("memplex.storage.migrations.runner")
    approved_0002 = _migration(2).sql_bytes
    monkeypatch.setattr(
        runner_module,
        "_migration_resources",
        lambda: {
            "0001_initial.sql": b"SELECT 1;",
            "0002_principal_acl.sql": approved_0002,
            "0003_bad.sql": b"BEGIN; SELECT 3; COMMIT;",
        },
    )

    with pytest.raises(migrations.MigrationIntegrityError, match=r"transaction control.*0003"):
        migrations.discover_migrations()


@pytest.mark.parametrize(
    "statement",
    (
        "END;",
        "ABORT;",
        "START TRANSACTION;",
        "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE;",
        "SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY;",
        "PREPARE TRANSACTION 'g003-review';",
        "COMMIT PREPARED 'g003-review';",
        "ROLLBACK PREPARED 'g003-review';",
    ),
)
def test_postgres_transaction_control_grammar_is_rejected(
    monkeypatch: pytest.MonkeyPatch, statement: str
) -> None:
    """PostgreSQL aliases must not escape the runner-owned transaction boundary."""
    migrations = _migrations()
    runner_module = importlib.import_module("memplex.storage.migrations.runner")
    approved_0002 = _migration(2).sql_bytes
    monkeypatch.setattr(
        runner_module,
        "_migration_resources",
        lambda: {
            "0001_initial.sql": b"SELECT 1;",
            "0002_principal_acl.sql": approved_0002,
            "0003_bad.sql": statement.encode(),
        },
    )

    with pytest.raises(migrations.MigrationIntegrityError, match=r"transaction control.*0003"):
        migrations.discover_migrations()


def test_transaction_words_in_comments_and_quoted_bodies_are_allowed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Masked SQL content cannot be confused with a top-level control statement."""
    migrations = _migrations()
    runner_module = importlib.import_module("memplex.storage.migrations.runner")
    approved_0002 = _migration(2).sql_bytes
    monkeypatch.setattr(
        runner_module,
        "_migration_resources",
        lambda: {
            "0001_initial.sql": b"SELECT 1;",
            "0002_principal_acl.sql": approved_0002,
            "0003_safe.sql": (
                b"-- END; START TRANSACTION;\n"
                b"/* SET TRANSACTION ISOLATION LEVEL SERIALIZABLE; */\n"
                b"SELECT 'ABORT; COMMIT PREPARED';\n"
                b'SELECT "ROLLBACK PREPARED";\n'
                b"DO $$ BEGIN RAISE NOTICE 'PREPARE TRANSACTION'; END $$;\n"
            ),
        },
    )

    assert [migration.version for migration in migrations.discover_migrations()] == [1, 2, 3]


def test_cr_terminated_line_comment_cannot_mask_transaction_control(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PostgreSQL's CR line ending must expose the following COMMIT statement."""
    migrations = _migrations()
    runner_module = importlib.import_module("memplex.storage.migrations.runner")
    approved_0002 = _migration(2).sql_bytes
    monkeypatch.setattr(
        runner_module,
        "_migration_resources",
        lambda: {
            "0001_initial.sql": b"SELECT 1;",
            "0002_principal_acl.sql": approved_0002,
            "0003_cr_comment.sql": b"-- line comment\rCOMMIT;",
        },
    )

    with pytest.raises(migrations.MigrationIntegrityError, match=r"transaction control.*0003"):
        migrations.discover_migrations()


@pytest.mark.parametrize("line_ending", (b"\n", b"\r\n"))
def test_lf_and_crlf_line_comments_do_not_expose_transaction_words(
    monkeypatch: pytest.MonkeyPatch, line_ending: bytes
) -> None:
    """Transaction words remain harmless while they are inside a complete line comment."""
    migrations = _migrations()
    runner_module = importlib.import_module("memplex.storage.migrations.runner")
    approved_0002 = _migration(2).sql_bytes
    monkeypatch.setattr(
        runner_module,
        "_migration_resources",
        lambda: {
            "0001_initial.sql": b"SELECT 1;",
            "0002_principal_acl.sql": approved_0002,
            "0003_line_comment.sql": b"-- COMMIT; END;" + line_ending + b"SELECT 1;",
        },
    )

    assert [migration.version for migration in migrations.discover_migrations()] == [1, 2, 3]


@pytest.mark.parametrize(
    "safe_sql",
    (
        b"/* outer /* inner */ END; */ SELECT 1;",
        b"SELECT E'quoted \\' ; END;';",
    ),
)
def test_valid_nested_comment_and_escape_string_are_discovered(
    monkeypatch: pytest.MonkeyPatch, safe_sql: bytes
) -> None:
    """Valid PostgreSQL lexical forms must not expose a false END statement."""
    migrations = _migrations()
    runner_module = importlib.import_module("memplex.storage.migrations.runner")
    approved_0002 = _migration(2).sql_bytes
    monkeypatch.setattr(
        runner_module,
        "_migration_resources",
        lambda: {
            "0001_initial.sql": b"SELECT 1;",
            "0002_principal_acl.sql": approved_0002,
            "0003_safe.sql": safe_sql,
        },
    )

    assert [migration.version for migration in migrations.discover_migrations()] == [1, 2, 3]


@pytest.mark.parametrize(
    "unsafe_sql",
    (
        b"CREATE TABLE foo$tag$ (id integer); COMMIT; CREATE TABLE bar$tag$ (id integer);",
        "CREATE TABLE 表$tag$ (id integer); COMMIT; CREATE TABLE 终$tag$ (id integer);".encode(),
    ),
)
def test_identifier_adjacent_dollar_tag_cannot_mask_transaction_control(
    monkeypatch: pytest.MonkeyPatch, unsafe_sql: bytes
) -> None:
    """A dollar inside an unquoted identifier cannot begin a dollar-quoted body."""
    migrations = _migrations()
    runner_module = importlib.import_module("memplex.storage.migrations.runner")
    approved_0002 = _migration(2).sql_bytes
    monkeypatch.setattr(
        runner_module,
        "_migration_resources",
        lambda: {
            "0001_initial.sql": b"SELECT 1;",
            "0002_principal_acl.sql": approved_0002,
            "0003_identifier.sql": unsafe_sql,
        },
    )

    with pytest.raises(migrations.MigrationIntegrityError, match=r"transaction control.*0003"):
        migrations.discover_migrations()


@pytest.mark.parametrize(
    "safe_sql",
    (
        b"SELECT $tag$END; COMMIT;$tag$;",
        b"SELECT 1 + $tag$END; COMMIT;$tag$;",
        b"SELECT $$END; COMMIT;$$;",
    ),
)
def test_separated_dollar_quotes_remain_discoverable(
    monkeypatch: pytest.MonkeyPatch, safe_sql: bytes
) -> None:
    """Whitespace and operator-separated dollar bodies retain exact closing tags."""
    migrations = _migrations()
    runner_module = importlib.import_module("memplex.storage.migrations.runner")
    approved_0002 = _migration(2).sql_bytes
    monkeypatch.setattr(
        runner_module,
        "_migration_resources",
        lambda: {
            "0001_initial.sql": b"SELECT 1;",
            "0002_principal_acl.sql": approved_0002,
            "0003_dollar_quote.sql": safe_sql,
        },
    )

    assert [migration.version for migration in migrations.discover_migrations()] == [1, 2, 3]


@pytest.mark.parametrize(
    "invalid_sql",
    (
        b"/* END;",
        b"SELECT 'unterminated; END;",
        b"DO $$ BEGIN RAISE NOTICE 'END';",
    ),
)
def test_unterminated_lexical_forms_fail_closed(
    monkeypatch: pytest.MonkeyPatch, invalid_sql: bytes
) -> None:
    """An incomplete comment or quoted body cannot be accepted as harmless SQL."""
    migrations = _migrations()
    runner_module = importlib.import_module("memplex.storage.migrations.runner")
    approved_0002 = _migration(2).sql_bytes
    monkeypatch.setattr(
        runner_module,
        "_migration_resources",
        lambda: {
            "0001_initial.sql": b"SELECT 1;",
            "0002_principal_acl.sql": approved_0002,
            "0003_invalid.sql": invalid_sql,
        },
    )

    with pytest.raises(migrations.MigrationIntegrityError, match="unterminated"):
        migrations.discover_migrations()


def test_discovery_rejects_non_continuous_versions(monkeypatch: pytest.MonkeyPatch) -> None:
    """A skipped migration version cannot silently become an upgrade path."""
    migrations = _migrations()
    runner_module = importlib.import_module("memplex.storage.migrations.runner")
    monkeypatch.setattr(
        runner_module,
        "_migration_resources",
        lambda: {"0001_initial.sql": b"SELECT 1;", "0003_integrity.sql": b"SELECT 3;"},
    )

    with pytest.raises(migrations.MigrationIntegrityError, match="continuous"):
        migrations.discover_migrations()


def test_0002_checksum_is_approved_and_its_transaction_shell_is_stripped() -> None:
    """ACL bytes remain immutable while the runner supplies the transaction boundary."""
    runner_module = importlib.import_module("memplex.storage.migrations.runner")
    migration = _migration(2)

    assert migration.checksum == "8e932e605e4eb36f6ec410c5a589001133a2db29ba902ff34227ae0a223e9a16"
    body = runner_module._body_for_execution(migration)
    assert b"ALTER TABLE memplex_functions" in body
    assert body.strip().startswith(b"-- The same identity columns are present on every memory-bearing table.")
    assert body.rstrip().endswith(b"));")


def test_0001_creates_complete_pre_acl_core_schema() -> None:
    """The initial migration has every table that immutable ACL upgrades."""
    sql = _migration(1).sql_bytes.decode()
    for table in (
        "memplex_functions",
        "memplex_edges",
        "memplex_observations",
        "memplex_facts",
        "memplex_preferences",
        "memplex_changelog",
    ):
        assert f"CREATE TABLE {table}" in sql
    assert "search_tsv" in sql
    assert "fts_functions_idx" in sql


def test_0003_owns_feedback_and_three_scope_indexes() -> None:
    """Integrity migration centralizes feedback and scope-specific uniqueness."""
    sql = _migration(3).sql_bytes.decode()
    assert "CREATE TABLE IF NOT EXISTS feedback" in sql
    assert "memplex_functions_workspace_normalized_name_key" in sql
    assert "memplex_functions_user_normalized_name_key" in sql
    assert "memplex_functions_session_normalized_name_key" in sql


def test_0003_declares_idempotent_feedback_catalog_upgrade() -> None:
    """Existing feedback catalogs can be upgraded by controlled idempotent DDL."""
    sql = _migration(3).sql_bytes.decode()
    assert "ADD COLUMN IF NOT EXISTS" in sql
    assert "CREATE INDEX IF NOT EXISTS" in sql
    assert "ENABLE ROW LEVEL SECURITY" in sql
    assert "FORCE ROW LEVEL SECURITY" in sql


def test_0004_pins_declarative_edge_function_integrity() -> None:
    """The new immutable resource carries only the reviewed FK/cascade design."""
    migrations = _migrations().discover_migrations()
    checksums = {migration.version: migration.checksum for migration in migrations}
    assert [migration.version for migration in migrations] == [1, 2, 3, 4, 5]
    assert {version: checksums[version] for version in range(1, 5)} == {
        1: "b4ab57dd8545c0ca1573d83fc699c806f2c889bf2d42bea656971171e0fb6373",
        2: "8e932e605e4eb36f6ec410c5a589001133a2db29ba902ff34227ae0a223e9a16",
        3: "11aed197c40c1fc122c550a41e45e7fd40b3379e53bc043d6d743e0a01311434",
        4: "a57bc392735d97c2ab3e3f726f10f86c83de44926c622dc2158faf35e5d44809",
    }
    sql = _migration(4).sql_bytes.decode()
    assert "domain_node_id-v1" in sql
    assert "only by the" in sql
    assert "memplex_functions_reserved_domain_id_check" in sql
    assert "NOT starts_with(id, 'domain_')" in sql
    assert "lower(" not in sql
    assert "data->>" not in sql
    assert "ADD COLUMN IF NOT EXISTS target_function" in sql
    assert "CASE WHEN edge_type = 'BELONGS_TO' THEN NULL::text ELSE target END" in sql
    assert "memplex_edges_source_function_fk" in sql
    assert "memplex_edges_target_function_fk" in sql
    assert sql.count("ON DELETE CASCADE") == 2
    assert "memplex_edges_tenant_target_function_idx" in sql
    assert "WHERE target_function IS NOT NULL" in sql


def test_0005_declares_reliable_sync_catalogue_and_security_boundary() -> None:
    """v5 is an immutable, RLS-forced durable-sync catalogue migration."""
    sql = _migration(5).sql_bytes.decode()
    for table in (
        "memplex_sync_outbox",
        "memplex_sync_entity_versions",
        "memplex_sync_inbox",
        "memplex_sync_batches",
        "memplex_sync_targets",
        "memplex_sync_deliveries",
        "memplex_sync_cursors",
        "memplex_sync_stream_state",
        "memplex_sync_local_identity",
        "memplex_sync_ingress_principals",
        "memplex_sync_snapshots",
        "memplex_sync_snapshot_items",
    ):
        assert f"CREATE TABLE {table}" in sql
        assert f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY" in sql
        assert f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY" in sql
    assert "memplex_sync_capture_local_change" in sql
    assert "SECURITY DEFINER" in sql
    assert "ALTER FUNCTION %I.memplex_sync_capture_before() SET search_path TO pg_catalog, %I" in sql
    assert "REVOKE ALL ON FUNCTION" in sql


def test_0005_does_not_rewrite_prior_immutable_migrations() -> None:
    """Adding v5 must leave the four previously approved resource bytes intact."""
    checksums = {migration.version: migration.checksum for migration in _migrations().discover_migrations()}
    assert {version: checksums[version] for version in range(1, 5)} == {
        1: "b4ab57dd8545c0ca1573d83fc699c806f2c889bf2d42bea656971171e0fb6373",
        2: "8e932e605e4eb36f6ec410c5a589001133a2db29ba902ff34227ae0a223e9a16",
        3: "11aed197c40c1fc122c550a41e45e7fd40b3379e53bc043d6d743e0a01311434",
        4: "a57bc392735d97c2ab3e3f726f10f86c83de44926c622dc2158faf35e5d44809",
    }


def test_edge_fk_catalogue_requires_current_schema_not_relation_name_alone() -> None:
    """The v4 gate consumes parsed pg_catalog facts, not FK definition text."""
    runner = importlib.import_module("memplex.storage.migrations.runner")
    foreign_keys = (
        (
            "memplex_edges_source_function_fk",
            ("tenant_id", "source"),
            "sibling",
            123,
            "memplex_functions",
            ("tenant_id", "id"),
            "a",
            "c",
            "s",
            False,
            False,
            True,
            False,
        ),
        (
            "memplex_edges_target_function_fk",
            ("tenant_id", "target_function"),
            "current",
            456,
            "memplex_functions",
            ("tenant_id", "id"),
            "a",
            "c",
            "s",
            False,
            False,
            True,
            True,
        ),
    )
    edge_table = {
        "columns": (
            ("target_function", 13, "text", False, runner._EDGE_TARGET_FUNCTION_EXPRESSION, "s"),
        ),
        "foreign_keys": foreign_keys,
        "constraints": (
            ("p", "memplex_edges_pkey", "intentionally ignored by this parsed-FK test"),
            ("f", "memplex_edges_source_function_fk", "not parsed"),
            ("f", "memplex_edges_target_function_fk", "not parsed"),
        ),
    }

    assert runner._edge_integrity_matches(edge_table) is False
    edge_table["foreign_keys"] = ((*foreign_keys[0][:-1], True), foreign_keys[1])
    assert runner._edge_integrity_matches(edge_table) is True


def test_reserved_domain_id_check_uses_exact_catalogue_flags() -> None:
    """Renaming, NO INHERIT or validation drift cannot be hidden in DDL text."""
    runner = importlib.import_module("memplex.storage.migrations.runner")
    table = {
        "checks": (
            (
                "memplex_functions_reserved_domain_id_check",
                runner._RESERVED_DOMAIN_ID_CHECK_EXPRESSION,
                True,
                False,
                False,
                False,
                True,
                0,
            ),
        ),
        "constraints": (
            ("p", "memplex_functions_pkey", "ignored"),
            ("c", "memplex_functions_reserved_domain_id_check", "ignored"),
        ),
    }

    assert runner._reserved_domain_id_check_matches(table) is True
    table["checks"] = ((*table["checks"][0][:-3], True, True, 0),)
    assert runner._reserved_domain_id_check_matches(table) is False


def test_disabled_vector_capability_does_not_open_a_connection() -> None:
    """The documented disabled sentinel is zero and must not open or inspect a catalog."""
    runner_module = importlib.import_module("memplex.storage.migrations.runner")
    request = runner_module.VectorCapabilityRequest(dim=0, policy="disabled")

    def _unexpected_connection():
        raise AssertionError("disabled vector capability opened a connection")

    runner = runner_module.PostgresMigrationRunner(
        "postgresql://example.invalid/memplex", connection_factory=_unexpected_connection
    )

    status = runner.ensure_vector_capability(
        request,
        deployment_profile="development",
        expected_target=runner_module.PostgresTargetIdentity(
            database="postgres", schema="ignored", server_address=None, server_port=None
        ),
        application_acl=runner_module.ApplicationAclContract("application"),
        ingress_acl=runner_module.IngressAclContract("ingress"),
    )

    assert status.state == "disabled"
    assert request.dim == 0
    assert status.dim == 0


@pytest.mark.parametrize(
    ("dimension", "policy"),
    (
        (0, "required"),
        (0, "best_effort"),
        (-1, "required"),
        (-1, "best_effort"),
        (True, "required"),
        (True, "best_effort"),
        (16_001, "required"),
        (16_001, "best_effort"),
    ),
)
def test_vector_request_rejects_dimensions_outside_pgvector_contract(
    dimension: int, policy: str
) -> None:
    """Only disabled may use the zero sentinel; active requests need a real pgvector dimension."""
    runner_module = importlib.import_module("memplex.storage.migrations.runner")

    with pytest.raises(ValueError, match="between 1 and 16000"):
        runner_module.VectorCapabilityRequest(dim=dimension, policy=policy)


def test_inspect_then_apply_rechecks_factory_target_before_catalogue_or_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A factory switch after inspection must fail before ledger, lock, or DDL work."""
    runner_module = importlib.import_module("memplex.storage.migrations.runner")

    class _Cursor:
        def __init__(self, target: tuple[str, str, str | None, int | None]) -> None:
            self.target = target
            self.executed: list[str] = []
            self.closed = False

        def execute(self, statement: str, _params=()) -> None:
            self.executed.append(statement)
            if "current_database()" not in statement:
                raise AssertionError("target mismatch reached catalogue or migration SQL")

        def fetchone(self):
            return self.target

        def close(self) -> None:
            self.closed = True

    class _Connection:
        autocommit = True

        def __init__(self, schema: str, user: str, password: str) -> None:
            self.cursor_instance = _Cursor(("postgres", schema, None, None))
            self.user = user
            self.password = password
            self.closed = False
            self.rolled_back = False

        def get_dsn_parameters(self) -> dict[str, str]:
            return {
                "dbname": "postgres",
                "host": "/tmp/g003-target.sock",
                "port": "6543",
                "options": f"-c search_path={self.cursor_instance.target[1]}",
                "user": self.user,
                "password": self.password,
            }

        def set_session(self, *, readonly: bool, autocommit: bool) -> None:
            del readonly
            self.autocommit = autocommit

        def cursor(self) -> _Cursor:
            return self.cursor_instance

        def rollback(self) -> None:
            self.rolled_back = True

        def close(self) -> None:
            self.closed = True

    inspected = _Connection("target_a", "admin", "inspect-secret")
    switched = _Connection("target_b", "application", "apply-secret")
    connections = iter((inspected, switched))
    runner = runner_module.PostgresMigrationRunner(
        "postgresql://credentials-must-not-appear.invalid/memplex",
        connection_factory=lambda: next(connections),
    )
    monkeypatch.setattr(
        runner,
        "_locked_plan_and_apply",
        lambda _cur: (_ for _ in ()).throw(AssertionError("migration lock was reached")),
    )

    expected = runner.inspect_target()

    assert isinstance(expected, _migrations().PostgresTargetIdentity)
    assert expected.database == "postgres"
    assert expected.schema == "target_a"
    assert expected.server_address is None
    assert expected.server_port is None
    assert expected.declared_host == "/tmp/g003-target.sock"
    assert expected.declared_port == 6543
    assert not hasattr(expected, "declared_options")
    assert "secret" not in repr(expected)
    with pytest.raises(runner_module.MigrationIntegrityError, match="target identity"):
        runner.apply(expected_target=expected)
    assert inspected.rolled_back and inspected.closed and inspected.cursor_instance.closed
    assert switched.rolled_back and switched.closed and switched.cursor_instance.closed
    assert len(switched.cursor_instance.executed) == 1


def test_expected_target_uses_resolved_identity_not_credentials_or_dsn_spelling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Equivalent connection declarations must not change the resolved target key."""
    runner_module = importlib.import_module("memplex.storage.migrations.runner")

    class _Cursor:
        def __init__(self) -> None:
            self.executed: list[str] = []

        def execute(self, statement: str, _params=()) -> None:
            self.executed.append(statement)
            if "current_database()" not in statement:
                raise AssertionError("resolved target check was bypassed")

        @staticmethod
        def fetchone():
            return ("postgres", "target_a", None, None)

        def close(self) -> None:
            pass

    class _Connection:
        autocommit = True

        def __init__(self, host: str, options: str, user: str, password: str) -> None:
            self.cursor_instance = _Cursor()
            self.host = host
            self.options = options
            self.user = user
            self.password = password
            self.committed = False

        def get_dsn_parameters(self) -> dict[str, str]:
            return {
                "dbname": "postgres",
                "host": self.host,
                "port": "6543",
                "options": self.options,
                "user": self.user,
                "password": self.password,
            }

        def set_session(self, *, readonly: bool, autocommit: bool) -> None:
            del readonly
            self.autocommit = autocommit

        def cursor(self) -> _Cursor:
            return self.cursor_instance

        def rollback(self) -> None:
            pass

        def commit(self) -> None:
            self.committed = True

        def close(self) -> None:
            pass

    inspected = _Connection(
        "/private/tmp/g003-target.sock",
        "-c search_path=target_a",
        "admin",
        "inspect-secret",
    )
    equivalent = _Connection(
        "/tmp/g003-target.sock",
        "-c search_path='target_a'",
        "application",
        "apply-secret",
    )
    connections = iter((inspected, equivalent))
    runner = runner_module.PostgresMigrationRunner(
        "postgresql://credentials-must-not-appear.invalid/memplex",
        connection_factory=lambda: next(connections),
    )
    final = runner_module.MigrationPlan(
        current_version=4,
        known_version=5,
        pending=(),
        state="ready",
    )
    monkeypatch.setattr(runner, "_locked_plan_and_apply", lambda _cur, **_kwargs: final)

    expected = runner.inspect_target()

    assert runner.apply(expected_target=expected) is final
    assert equivalent.committed
    assert len(equivalent.cursor_instance.executed) == 1


@pytest.mark.parametrize(
    "operation",
    (
        "plan",
        "status",
        "dry_run",
        "apply",
        "vector_required",
        "vector_best_effort",
        "vector_disabled",
    ),
)
@pytest.mark.parametrize("expected_kind", ("lookalike", "subclass"))
def test_every_target_aware_entry_point_rejects_forged_expected_identity(
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    expected_kind: str,
) -> None:
    """Foreign comparison methods must never influence the actual target gate."""
    runner_module = importlib.import_module("memplex.storage.migrations.runner")

    class _Cursor:
        executed: list[str]
        closed: bool

        def __init__(self) -> None:
            self.executed = []
            self.closed = False

        def execute(self, statement: str, _params=()) -> None:
            self.executed.append(statement)
            if "current_database()" not in statement:
                raise AssertionError("forged expected target reached catalogue or DDL")

        @staticmethod
        def fetchone():
            return ("postgres", "target_a", None, None)

        def close(self) -> None:
            self.closed = True

    class _Connection:
        autocommit = True

        def __init__(self) -> None:
            self.cursor_instance = _Cursor()
            self.rolled_back = False
            self.closed = False

        @staticmethod
        def get_dsn_parameters() -> dict[str, str]:
            return {"dbname": "postgres", "host": "/tmp", "port": "5432"}

        def set_session(self, *, readonly: bool, autocommit: bool) -> None:
            del readonly
            self.autocommit = autocommit

        def cursor(self) -> _Cursor:
            return self.cursor_instance

        def rollback(self) -> None:
            self.rolled_back = True

        def close(self) -> None:
            self.closed = True

    class _ForgedExpected:
        @staticmethod
        def __ne__(_other) -> bool:
            return False

    class _TargetSubclass(runner_module.PostgresTargetIdentity):
        @staticmethod
        def __ne__(_other) -> bool:
            return False

    if expected_kind == "lookalike":
        expected_target = _ForgedExpected()
    else:
        expected_target = _TargetSubclass(
            database="other",
            schema="target_b",
            server_address=None,
            server_port=None,
        )
    connection = _Connection()
    runner = runner_module.PostgresMigrationRunner(
        "postgresql://credentials-must-not-appear.invalid/memplex",
        connection_factory=lambda: connection,
    )
    monkeypatch.setattr(
        runner,
        "_locked_plan_and_apply",
        lambda _cur: (_ for _ in ()).throw(AssertionError("forged target reached lock")),
    )

    with pytest.raises(runner_module.MigrationIntegrityError, match="target identity"):
        if operation == "plan":
            runner.plan(expected_target=expected_target)
        elif operation == "status":
            runner.status(expected_target=expected_target)
        elif operation == "dry_run":
            runner.apply(dry_run=True, expected_target=expected_target)
        elif operation == "apply":
            runner.apply(expected_target=expected_target)
        elif operation == "vector_required":
            runner.ensure_vector_capability(
                runner_module.VectorCapabilityRequest(dim=8, policy="required"),
                "production",
                expected_target=expected_target,
            )
        elif operation == "vector_best_effort":
            runner.ensure_vector_capability(
                runner_module.VectorCapabilityRequest(dim=8, policy="best_effort"),
                "development",
                expected_target=expected_target,
            )
        else:
            runner.ensure_vector_capability(
                runner_module.VectorCapabilityRequest(dim=0, policy="disabled"),
                "development",
                expected_target=expected_target,
            )

    assert len(connection.cursor_instance.executed) == (
        0 if operation == "vector_disabled" else 1
    )
    if operation == "vector_disabled":
        assert not connection.rolled_back
        assert not connection.closed
        assert not connection.cursor_instance.closed
    else:
        assert connection.rolled_back
        assert connection.closed
        assert connection.cursor_instance.closed


def test_declared_dsn_metadata_cannot_reject_the_same_actual_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Proxy aliases and ports are diagnostics, never part of target acceptance."""
    runner_module = importlib.import_module("memplex.storage.migrations.runner")

    class _Cursor:
        def __init__(self) -> None:
            self.executed: list[str] = []

        def execute(self, statement: str, _params=()) -> None:
            self.executed.append(statement)
            if "current_database()" not in statement:
                raise AssertionError("same actual target unexpectedly reached migration SQL")

        @staticmethod
        def fetchone():
            return ("actual_database", "target_a", "127.0.0.1", 5432)

        def close(self) -> None:
            pass

    class _Connection:
        autocommit = True

        def __init__(self, database: str, host: str, hostaddr: str, port: str) -> None:
            self.cursor_instance = _Cursor()
            self.database = database
            self.host = host
            self.hostaddr = hostaddr
            self.port = port
            self.committed = False

        def get_dsn_parameters(self) -> dict[str, str]:
            return {
                "dbname": self.database,
                "host": self.host,
                "hostaddr": self.hostaddr,
                "port": self.port,
                "options": "-c memplex.secret=must-not-appear",
                "user": "application",
                "password": "credential-secret",
            }

        def set_session(self, *, readonly: bool, autocommit: bool) -> None:
            del readonly
            self.autocommit = autocommit

        def cursor(self) -> _Cursor:
            return self.cursor_instance

        def rollback(self) -> None:
            pass

        def commit(self) -> None:
            self.committed = True

        def close(self) -> None:
            pass

    inspected = _Connection("pool_alias", "proxy-a", "10.0.0.1", "6432")
    equivalent = _Connection("another_alias", "proxy-b", "10.0.0.2", "proxy-port")
    connections = iter((inspected, equivalent))
    runner = runner_module.PostgresMigrationRunner(
        "postgresql://credentials-must-not-appear.invalid/memplex",
        connection_factory=lambda: next(connections),
    )
    final = runner_module.MigrationPlan(3, 3, (), "ready")
    monkeypatch.setattr(runner, "_locked_plan_and_apply", lambda _cur, **_kwargs: final)

    expected = runner.inspect_target()

    assert expected.database == "actual_database"
    assert expected.server_port == 5432
    assert expected.declared_database == "pool_alias"
    assert expected.declared_host == "proxy-a"
    assert expected.declared_hostaddr == "10.0.0.1"
    assert expected.declared_port == 6432
    assert not hasattr(expected, "declared_options")
    assert "secret" not in repr(expected)
    assert runner.apply(expected_target=expected) is final
    assert equivalent.committed


def test_public_connection_target_inspector_does_not_own_external_resources() -> None:
    """Pool probes reuse the authoritative parser without surrendering their lease."""

    class _Cursor:
        closed = False

        def __init__(self) -> None:
            self.executed: list[str] = []

        def execute(self, statement: str) -> None:
            self.executed.append(statement)

        @staticmethod
        def fetchone():
            return ("postgres", "target_a", None, None)

        def close(self) -> None:
            self.closed = True

    class _Connection:
        closed = False
        rolled_back = False

        @staticmethod
        def get_dsn_parameters():
            raise RuntimeError("diagnostic-secret")

        def rollback(self) -> None:
            self.rolled_back = True

        def close(self) -> None:
            self.closed = True

    connection = _Connection()
    cursor = _Cursor()

    identity = _migrations().inspect_postgres_connection_target(connection, cursor)

    assert type(identity) is _migrations().PostgresTargetIdentity
    assert identity.database == "postgres"
    assert identity.schema == "target_a"
    assert identity.declared_database is None
    assert len(cursor.executed) == 1
    assert not cursor.closed
    assert not connection.rolled_back
    assert not connection.closed
    assert "secret" not in repr(identity)


def test_target_inspection_cursor_cleanup_does_not_mask_the_primary_failure() -> None:
    """A failing cursor close must not replace the target-inspection diagnostic."""
    runner_module = importlib.import_module("memplex.storage.migrations.runner")

    class _Cursor:
        @staticmethod
        def execute(_statement: str) -> None:
            raise runner_module.MigrationIntegrityError("target identity primary failure")

        @staticmethod
        def close() -> None:
            raise RuntimeError("cursor close failure")

    class _Connection:
        autocommit = True
        rolled_back = False
        closed = False

        def set_session(self, *, readonly: bool, autocommit: bool) -> None:
            del readonly
            self.autocommit = autocommit

        @staticmethod
        def cursor() -> _Cursor:
            return _Cursor()

        def rollback(self) -> None:
            self.rolled_back = True

        def close(self) -> None:
            self.closed = True

    connection = _Connection()
    runner = runner_module.PostgresMigrationRunner(
        "postgresql://credentials-must-not-appear.invalid/memplex",
        connection_factory=lambda: connection,
    )

    with pytest.raises(
        runner_module.MigrationIntegrityError,
        match="target identity primary failure",
    ):
        runner.inspect_target()

    assert connection.rolled_back
    assert connection.closed


def test_legacy_belongs_cleanup_does_not_mask_invalid_endpoint() -> None:
    """FORCE-RLS cleanup is best effort when legacy validation has already failed."""
    runner_module = importlib.import_module("memplex.storage.migrations.runner")

    class _Cursor:
        def __init__(self) -> None:
            self.executed: list[str] = []
            self._last_statement = ""

        def execute(self, statement: str) -> None:
            self.executed.append(statement)
            self._last_statement = statement
            if statement == "ALTER TABLE memplex_edges FORCE ROW LEVEL SECURITY":
                raise RuntimeError("edges FORCE cleanup failure")

        def fetchall(self):
            if "FROM pg_catalog.pg_class AS relation" in self._last_statement:
                return (
                    ("memplex_edges", True, True),
                    ("memplex_functions", True, True),
                )
            if "FROM memplex_edges AS edge" in self._last_statement:
                return (("source", "domain_wrong", "source", {"domain": "payments"}),)
            raise AssertionError(f"unexpected fetchall after: {self._last_statement}")

    cursor = _Cursor()

    with pytest.raises(runner_module.MigrationIntegrityError, match="invalid legacy BELONGS_TO"):
        runner_module._validate_legacy_belongs_to_edges(cursor)

    assert "ALTER TABLE memplex_edges FORCE ROW LEVEL SECURITY" in cursor.executed
    assert "ALTER TABLE memplex_functions FORCE ROW LEVEL SECURITY" in cursor.executed


def test_legacy_belongs_cleanup_failure_surfaces_after_successful_validation() -> None:
    """A cleanup failure is observable only when no primary validation error occurred."""
    runner_module = importlib.import_module("memplex.storage.migrations.runner")

    class _Cursor:
        def __init__(self) -> None:
            self.executed: list[str] = []
            self._last_statement = ""

        def execute(self, statement: str) -> None:
            self.executed.append(statement)
            self._last_statement = statement
            if statement == "ALTER TABLE memplex_edges FORCE ROW LEVEL SECURITY":
                raise RuntimeError("edges FORCE cleanup failure")

        def fetchall(self):
            if "FROM pg_catalog.pg_class AS relation" in self._last_statement:
                return (
                    ("memplex_edges", True, True),
                    ("memplex_functions", True, True),
                )
            if "FROM memplex_edges AS edge" in self._last_statement:
                return ()
            raise AssertionError(f"unexpected fetchall after: {self._last_statement}")

    cursor = _Cursor()

    with pytest.raises(RuntimeError, match="edges FORCE cleanup failure"):
        runner_module._validate_legacy_belongs_to_edges(cursor)

    assert "ALTER TABLE memplex_edges FORCE ROW LEVEL SECURITY" in cursor.executed
    assert "ALTER TABLE memplex_functions FORCE ROW LEVEL SECURITY" in cursor.executed


def test_legacy_belongs_rejects_non_string_domain_before_domain_node_mapping() -> None:
    """JSONB values outside MemoryNode.domain's Optional[str] contract fail closed."""
    runner_module = importlib.import_module("memplex.storage.migrations.runner")

    class _Cursor:
        def __init__(self) -> None:
            self.executed: list[str] = []
            self._last_statement = ""

        def execute(self, statement: str) -> None:
            self.executed.append(statement)
            self._last_statement = statement

        def fetchall(self):
            if "FROM pg_catalog.pg_class AS relation" in self._last_statement:
                return (
                    ("memplex_edges", True, True),
                    ("memplex_functions", True, True),
                )
            if "FROM memplex_edges AS edge" in self._last_statement:
                return (("source", "domain_1", "source", {"domain": 1}),)
            raise AssertionError(f"unexpected fetchall after: {self._last_statement}")

    cursor = _Cursor()

    with pytest.raises(runner_module.MigrationIntegrityError, match="invalid legacy BELONGS_TO"):
        runner_module._validate_legacy_belongs_to_edges(cursor)

    assert "ALTER TABLE memplex_edges FORCE ROW LEVEL SECURITY" in cursor.executed
    assert "ALTER TABLE memplex_functions FORCE ROW LEVEL SECURITY" in cursor.executed


def test_readonly_plan_closes_a_connection_factory_transaction(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read-only planning rolls back and closes its one short-lived connection."""
    runner_module = importlib.import_module("memplex.storage.migrations.runner")

    class _Cursor:
        closed = False

        def close(self) -> None:
            self.closed = True

    class _Connection:
        autocommit = True
        rolled_back = False
        closed = False
        readonly = False

        def __init__(self) -> None:
            self.cursor_instance = _Cursor()

        def set_session(self, *, readonly: bool, autocommit: bool) -> None:
            self.readonly = readonly
            self.autocommit = autocommit

        def cursor(self) -> _Cursor:
            return self.cursor_instance

        def rollback(self) -> None:
            self.rolled_back = True

        def close(self) -> None:
            self.closed = True

    connection = _Connection()
    monkeypatch.setattr(runner_module, "_read_ledger_if_present", lambda _cur: ())
    monkeypatch.setattr(runner_module, "_confine_to_current_schema", lambda _cur: "fixture")
    monkeypatch.setattr(
        runner_module,
        "schema_fingerprint",
        lambda _cur, **_kwargs: runner_module.SchemaFingerprint("empty", "fixture"),
    )

    plan = runner_module.PostgresMigrationRunner(
        "postgresql://example.invalid/memplex", connection_factory=lambda: connection
    ).plan()

    assert plan.current_version == 0
    assert connection.readonly
    assert connection.rolled_back
    assert connection.cursor_instance.closed
    assert connection.closed


def test_readonly_connection_preserves_primary_error_when_rollback_fails() -> None:
    """Cleanup failures must not conceal the catalogue failure that triggered cleanup."""
    runner_module = importlib.import_module("memplex.storage.migrations.runner")

    class _Connection:
        autocommit = True
        closed = False

        def set_session(self, *, readonly: bool, autocommit: bool) -> None:
            del readonly, autocommit

        def rollback(self) -> None:
            raise RuntimeError("rollback failed")

        def close(self) -> None:
            self.closed = True

    connection = _Connection()
    runner = runner_module.PostgresMigrationRunner(
        "postgresql://example.invalid/memplex", connection_factory=lambda: connection
    )

    with pytest.raises(ValueError, match="primary catalogue failure"):
        with runner._short_connection(readonly=True):
            raise ValueError("primary catalogue failure")

    assert connection.closed


def test_readonly_connection_closes_when_normal_rollback_fails() -> None:
    """The no-error readonly path still closes a short-lived connection after rollback failure."""
    runner_module = importlib.import_module("memplex.storage.migrations.runner")

    class _Connection:
        autocommit = True
        closed = False

        def set_session(self, *, readonly: bool, autocommit: bool) -> None:
            del readonly, autocommit

        def rollback(self) -> None:
            raise RuntimeError("rollback failed")

        def close(self) -> None:
            self.closed = True

    connection = _Connection()
    runner = runner_module.PostgresMigrationRunner(
        "postgresql://example.invalid/memplex", connection_factory=lambda: connection
    )

    with pytest.raises(RuntimeError, match="rollback failed"):
        with runner._short_connection(readonly=True):
            pass

    assert connection.closed


def test_fake_unavailable_vector_catalogue_never_writes_capability_or_embedding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A controlled unavailable-extension catalogue exercises both negotiated failure modes."""
    runner_module = importlib.import_module("memplex.storage.migrations.runner")

    class _Cursor:
        def __init__(self) -> None:
            self.executed: list[str] = []
            self._answers = iter(((False,), (False,)))
            self.closed = False

        def execute(self, statement: str, _params=()) -> None:
            self.executed.append(statement)

        def fetchone(self):
            return next(self._answers)

        def close(self) -> None:
            self.closed = True

    class _Connection:
        autocommit = True

        def __init__(self) -> None:
            self.cursor_instance = _Cursor()
            self.committed = False
            self.rolled_back = False
            self.rollback_calls = 0
            self.closed = False

        def cursor(self) -> _Cursor:
            return self.cursor_instance

        def commit(self) -> None:
            self.committed = True

        def rollback(self) -> None:
            self.rolled_back = True
            self.rollback_calls += 1

        def close(self) -> None:
            self.closed = True

    def _runner_for(policy: str):
        connection = _Connection()
        runner = runner_module.PostgresMigrationRunner(
            "postgresql://example.invalid/memplex", connection_factory=lambda: connection
        )
        monkeypatch.setattr(
            runner,
            "_locked_plan_and_apply",
                lambda _cur, **_kwargs: runner_module.MigrationPlan(3, 3, (), "ready"),
        )
        monkeypatch.setattr(runner_module, "_vector_extension_type", lambda _cur: None)
        return runner, connection, runner_module.VectorCapabilityRequest(dim=8, policy=policy)

    required_runner, required_connection, required_request = _runner_for("required")
    with pytest.raises(runner_module.MigrationIntegrityError, match="unavailable"):
        required_runner.ensure_vector_capability(required_request, deployment_profile="production")
    assert required_connection.rolled_back and not required_connection.committed
    assert required_connection.rollback_calls == 1
    assert required_connection.closed
    assert not any(
        "ALTER TABLE memplex_functions" in statement
        or "memplex_schema_capabilities" in statement
        for statement in required_connection.cursor_instance.executed
    )

    best_effort_runner, best_effort_connection, best_effort_request = _runner_for("best_effort")
    status = best_effort_runner.ensure_vector_capability(
        best_effort_request, deployment_profile="development"
    )
    assert status.state == "degraded"
    assert best_effort_connection.committed and best_effort_connection.closed
    assert not any(
        "ALTER TABLE memplex_functions" in statement
        or "memplex_schema_capabilities" in statement
        for statement in best_effort_connection.cursor_instance.executed
    )


def test_apply_uses_the_fixed_signed_bigint_advisory_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lock query uses the reviewed bigint parameter, never a runtime hash."""
    runner_module = importlib.import_module("memplex.storage.migrations.runner")

    class _Cursor:
        def __init__(self) -> None:
            self.executed: list[tuple[str, tuple[object, ...]]] = []
            self.closed = False

        def execute(self, statement: str, params=()) -> None:
            self.executed.append((statement, params))

        def close(self) -> None:
            self.closed = True

    class _Connection:
        autocommit = True
        committed = False
        rolled_back = False
        closed = False

        def __init__(self) -> None:
            self.cursor_instance = _Cursor()

        def cursor(self) -> _Cursor:
            return self.cursor_instance

        def commit(self) -> None:
            self.committed = True

        def rollback(self) -> None:
            self.rolled_back = True

        def close(self) -> None:
            self.closed = True

    connection = _Connection()
    runner = runner_module.PostgresMigrationRunner(
        "postgresql://example.invalid/memplex", connection_factory=lambda: connection
    )
    entries = tuple(
        runner_module._LedgerEntry(
            migration.version, migration.name, migration.checksum, "executed", None
        )
        for migration in runner._migrations
    )
    monkeypatch.setattr(runner_module, "_read_ledger_if_present", lambda _cur: entries)
    monkeypatch.setattr(runner_module, "_confine_to_current_schema", lambda _cur: "fixture")
    monkeypatch.setattr(
        runner_module,
        "schema_fingerprint",
            lambda _cur, **_kwargs: runner_module.SchemaFingerprint(
            "post_g002_current", "fixture", "post_g002_runtime_v1_edge_integrity_current_reliable_sync_v5"
        ),
    )
    monkeypatch.setattr(runner, "_apply_plan_in_current_transaction", lambda *_args: None)

    assert runner.apply().state == "ready"
    assert (
        "SELECT pg_advisory_xact_lock(%s::bigint)",
        (runner_module._MIGRATION_LOCK_KEY,),
    ) in connection.cursor_instance.executed
    assert isinstance(runner_module._MIGRATION_LOCK_KEY, int)
    assert -(2**63) <= runner_module._MIGRATION_LOCK_KEY < 2**63
    assert connection.committed and connection.closed and connection.cursor_instance.closed


def test_apply_rolls_back_when_cursor_close_fails_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A cursor cleanup fault must happen while migration ledger work is still rollbackable."""
    runner_module = importlib.import_module("memplex.storage.migrations.runner")
    calls: list[str] = []

    class _Cursor:
        def close(self) -> None:
            calls.append("cursor.close")
            raise RuntimeError("cursor close failed")

    class _Connection:
        autocommit = True

        def cursor(self) -> _Cursor:
            return _Cursor()

        def commit(self) -> None:
            calls.append("commit")

        def rollback(self) -> None:
            calls.append("rollback")

        def close(self) -> None:
            calls.append("connection.close")

    runner = runner_module.PostgresMigrationRunner(
        "postgresql://example.invalid/memplex", connection_factory=_Connection
    )
    monkeypatch.setattr(
        runner,
        "_locked_plan_and_apply",
        lambda _cur, **_kwargs: runner_module.MigrationPlan(4, 4, (), "ready"),
    )

    with pytest.raises(RuntimeError, match="cursor close failed"):
        runner.apply()

    assert calls == ["cursor.close", "rollback", "connection.close"]


def test_apply_preserves_committed_plan_when_connection_close_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Post-commit close is best effort and cannot turn a committed plan into an error."""
    runner_module = importlib.import_module("memplex.storage.migrations.runner")
    calls: list[str] = []
    final = runner_module.MigrationPlan(4, 4, (), "ready")

    class _Cursor:
        def close(self) -> None:
            calls.append("cursor.close")

    class _Connection:
        autocommit = True

        def cursor(self) -> _Cursor:
            return _Cursor()

        def commit(self) -> None:
            calls.append("commit")

        def rollback(self) -> None:
            calls.append("rollback")

        def close(self) -> None:
            calls.append("connection.close")
            raise RuntimeError("connection close failed")

    runner = runner_module.PostgresMigrationRunner(
        "postgresql://example.invalid/memplex", connection_factory=_Connection
    )
    monkeypatch.setattr(runner, "_locked_plan_and_apply", lambda _cur, **_kwargs: final)

    assert runner.apply() is final
    assert calls == ["cursor.close", "commit", "connection.close"]


def test_apply_preserves_commit_error_when_rollback_and_close_also_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An uncertain commit outcome must expose the commit failure, never cleanup noise."""
    runner_module = importlib.import_module("memplex.storage.migrations.runner")
    calls: list[str] = []

    class _Cursor:
        def close(self) -> None:
            calls.append("cursor.close")

    class _Connection:
        autocommit = True

        def cursor(self) -> _Cursor:
            return _Cursor()

        def commit(self) -> None:
            calls.append("commit")
            raise RuntimeError("commit failed")

        def rollback(self) -> None:
            calls.append("rollback")
            raise RuntimeError("rollback failed")

        def close(self) -> None:
            calls.append("connection.close")
            raise RuntimeError("connection close failed")

    runner = runner_module.PostgresMigrationRunner(
        "postgresql://example.invalid/memplex", connection_factory=_Connection
    )
    monkeypatch.setattr(
        runner,
        "_locked_plan_and_apply",
        lambda _cur, **_kwargs: runner_module.MigrationPlan(4, 4, (), "ready"),
    )

    with pytest.raises(RuntimeError, match="commit failed"):
        runner.apply()

    assert calls == ["cursor.close", "commit", "rollback", "connection.close"]


def test_apply_preserves_business_error_when_cursor_rollback_and_close_fail(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed migration keeps its diagnostic even when every cleanup step also fails."""
    runner_module = importlib.import_module("memplex.storage.migrations.runner")
    calls: list[str] = []

    class _Cursor:
        def close(self) -> None:
            calls.append("cursor.close")
            raise RuntimeError("cursor close failed")

    class _Connection:
        autocommit = True

        def cursor(self) -> _Cursor:
            return _Cursor()

        def rollback(self) -> None:
            calls.append("rollback")
            raise RuntimeError("rollback failed")

        def close(self) -> None:
            calls.append("connection.close")
            raise RuntimeError("connection close failed")

    runner = runner_module.PostgresMigrationRunner(
        "postgresql://example.invalid/memplex", connection_factory=_Connection
    )
    monkeypatch.setattr(
        runner,
        "_locked_plan_and_apply",
        lambda _cur, **_kwargs: (_ for _ in ()).throw(ValueError("business failure")),
    )

    with pytest.raises(ValueError, match="business failure"):
        runner.apply()

    assert calls == ["cursor.close", "rollback", "connection.close"]
