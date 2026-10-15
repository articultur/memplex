"""Source validity V1 catalogue contracts; database cases use isolated CI schemas."""

from __future__ import annotations

import pytest

from memplex.storage.migrations import runner

_SOURCE_TABLES = (
    "memplex_source_authorities",
    "memplex_source_heads",
    "memplex_derivation_records",
)


@pytest.mark.parametrize("helper", [
    "_is_source_validity_current_variant", "_is_background_tasks_current_variant",
    "_allowed_adoption_baselines",
])
@pytest.mark.parametrize("with_vector", [False, True])
def test_ingress_guard_variant_preserves_existing_feature_identity(helper, with_vector):
    base = "post_g002_runtime_v1_edge_integrity_current_reliable_sync_v5_background_tasks_v6_source_validity_v1"
    guarded = base + "_source_ingress_guard_v9"
    if with_vector:
        base += "_vector_8"
        guarded += "_vector_8"
    check = getattr(runner, helper)
    assert check(base)
    assert check(guarded) == check(base)


def test_v9_ledger_cannot_admit_unguarded_v8_function_fingerprint():
    migrations = tuple(item for item in runner.discover_migrations() if item.version <= 8) + (
        runner.Migration(9, "source_ingress_guard", b"SELECT 1;", "9" * 64),
    )
    entries = tuple(runner._LedgerEntry(item.version, item.name, item.checksum, "executed", None)
                    for item in migrations)
    fingerprint = runner.SchemaFingerprint(
        "post_g002_current", "fixture",
        "post_g002_runtime_v1_edge_integrity_current_reliable_sync_v5_background_tasks_v6_source_validity_v1",
    )
    with pytest.raises(runner.MigrationIntegrityError, match="migration ledger does not match"):
        runner._plan_from_observed_state(entries, fingerprint, migrations)


def test_migration_catalogue_and_rls_contract() -> None:
    """A packaged V1 migration registers three forced-RLS ledger relations."""
    migrations = runner.discover_migrations()
    source_migration = next(item for item in migrations if item.version == 8)
    assert source_migration.name == "source_validity"
    sql = source_migration.sql_bytes.decode()
    for name in _SOURCE_TABLES:
        assert f"CREATE TABLE {name}" in sql
        assert f"ALTER TABLE {name} ENABLE ROW LEVEL SECURITY" in sql
        assert f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY" in sql
        assert name in runner._MANAGED_TABLES
        assert runner._KNOWN_MEMPLEX_RELATION_KINDS[name] == "r"
    assert "source_validity_v1" in sql
    assert "memplex_derivation_request_key" in sql
    assert "SECURITY DEFINER" not in sql.upper()


def test_unknown_relation_or_missing_constraint_blocks_ready() -> None:
    """A v8 ledger cannot make an older schema appear ready."""
    migrations = (
        *(item for item in runner.discover_migrations() if item.version < 8),
        runner.Migration(8, "source_validity", b"SELECT 1;", "8" * 64),
    )
    entries = tuple(
        runner._LedgerEntry(item.version, item.name, item.checksum, "executed", None)
        for item in migrations
    )
    old = runner.SchemaFingerprint(
        "post_g002_current", "fixture",
        "post_g002_runtime_v1_edge_integrity_current_reliable_sync_v5_background_tasks_v6",
    )
    with pytest.raises(runner.MigrationIntegrityError, match="migration ledger does not match"):
        runner._plan_from_observed_state(entries, old, migrations)


# This is a declaration-level fixture, not a simulated PostgreSQL execution.
# Real catalogue deparsing, constraint enforcement and RLS are tested below.
def _declared_tables():
    import re

    migration = next(item for item in runner.discover_migrations() if item.version == 8).sql_bytes.decode()
    primary = {
        _SOURCE_TABLES[0]: ("authority_id",),
        _SOURCE_TABLES[1]: ("authority_id", "kind", "logical_id"),
        _SOURCE_TABLES[2]: ("authority_id", "record_kind", "record_key"),
    }
    tables = {}
    for name in _SOURCE_TABLES:
        section = migration.split(f"CREATE TABLE {name} (\n", 1)[1].split("\n);", 1)[0]
        cols = re.findall(r"^    (\w+) (\w+) NOT NULL", section, re.MULTILINE)
        constraints = [("p", f"{name}_pkey", "PRIMARY KEY")]
        checks = []
        for key, expression in re.findall(r"^    CONSTRAINT (\w+) CHECK \((.*)\),?$", section, re.MULTILINE):
            constraints.append(("c", key, f"CHECK ({expression})"))
            checks.append((key, expression, True, False, False, False, True, 0))
        indexes, uniques = [], []
        for key, columns in re.findall(r"CONSTRAINT (\w+) UNIQUE \(([^)]+)\)", section):
            keys = tuple(columns.split(", "))
            constraints.append(("u", key, f"UNIQUE ({columns})"))
            uniques.append((key, keys, False, False, True, True, 0, True, True, True))
            indexes.append((key, True, "btree", True, len(keys), len(keys), keys, (),
                            f"CREATE UNIQUE INDEX {key} ON {name} USING btree ({columns})",
                            None, True, True, "p", True))
        foreign = ()
        if name != _SOURCE_TABLES[0]:
            key = f"{name}_authority_fk"
            scope = ("authority_id", "tenant_id", "workspace")
            constraints.append(("f", key, "FOREIGN KEY"))
            foreign = ((key, scope, "test_scope", 9001, _SOURCE_TABLES[0], scope,
                        "a", "a", "s", False, False, True, True),)
        if name == _SOURCE_TABLES[2]:
            indexes.append((
                "memplex_derivation_request_key", True, "btree", True, 2, 2,
                ("authority_id", "(data ->> 'request_id'::text)"), (),
                ("CREATE UNIQUE INDEX memplex_derivation_request_key ON memplex_derivation_records "
                "USING btree (authority_id, (data ->> 'request_id'::text)) "
                "WHERE record_kind = 'publication'::text"),
                "(record_kind = 'publication'::text)", True, True, "p", True,
            ))
        policy = migration.split(f"CREATE POLICY {name}_scope ON {name}\n", 1)[1]
        using = policy.split("    USING (", 1)[1].split(")\n", 1)[0]
        check = policy.split("    WITH CHECK (", 1)[1].split(");\n", 1)[0]
        tables[name] = {
            "owner_is_current_user": True, "acl_is_default": True, "persistence": "p",
            "is_partition": False, "has_no_inheritance": True,
            "column_acls_are_default": dict.fromkeys((col for col, _ in cols), True),
            "column_collations_are_type_default": dict.fromkeys((col for col, _ in cols), True),
            "primary_key": primary[name],
            "primary_index_catalog": ((False, False, True, True, True, True, True, True, True, True, True, True),),
            "columns": tuple((col, i, typ.lower(), True, None, "") for i, (col, typ) in enumerate(cols, 1)),
            "identities": dict.fromkeys((col for col, _ in cols), ""),
            "rules": (), "triggers": (), "rls": True, "force_rls": True,
            "constraints": tuple(sorted(constraints, key=lambda row: row[1])),
            "checks": tuple(sorted(checks)), "foreign_keys": foreign,
            "source_unique_constraints": tuple(sorted(uniques)), "indexes": tuple(sorted(indexes)),
            "policies": ((f"{name}_scope", "*", True, (0,), using, check),),
        }
    return tables


def test_declared_catalogue_and_application_acl_agree() -> None:
    from memplex.storage.migrations.catalogue_checks import _source_validity_catalogue_matches

    assert _source_validity_catalogue_matches(_declared_tables())
    assert tuple(runner._APPLICATION_ACL) == runner._APPLICATION_ACL_TABLES
    for name in _SOURCE_TABLES:
        assert runner._APPLICATION_ACL[name] == {"SELECT", "INSERT", "UPDATE"}
        assert runner._KNOWN_MEMPLEX_RELATION_KINDS[f"{name}_pkey"] == "i"
    assert runner._KNOWN_MEMPLEX_RELATION_KINDS["memplex_source_heads_logical_id_key"] == "i"
    assert runner._KNOWN_MEMPLEX_RELATION_KINDS["memplex_derivation_request_key"] == "i"


@pytest.mark.parametrize("change", [
    "missing", "nullable", "default", "rls", "force_rls", "policy_case", "enum_case",
    "missing_check", "unvalidated_check", "foreign_schema", "foreign_oid", "foreign_columns",
    "foreign_cascade", "foreign_deferred", "missing_unique", "deferred_unique",
    "partial_public_id", "request_id_case", "request_predicate", "extra_index", "trigger", "identity",
])
def test_catalogue_drift_fails_closed(change: str) -> None:
    from memplex.storage.migrations.catalogue_checks import _source_validity_catalogue_matches

    tables = _declared_tables()
    head = tables[_SOURCE_TABLES[1]]
    record = tables[_SOURCE_TABLES[2]]
    if change == "missing":
        del tables[_SOURCE_TABLES[0]]
    elif change in {"nullable", "default"}:
        row = list(head["columns"][4])
        row[3 if change == "nullable" else 4] = False if change == "nullable" else "1"
        head["columns"] = (*head["columns"][:4], tuple(row), *head["columns"][5:])
    elif change in {"rls", "force_rls"}:
        head[change] = False
    elif change == "policy_case":
        row = list(head["policies"][0])
        row[4] = row[4].replace("'user'", "'USER'")
        head["policies"] = (tuple(row),)
    elif change == "enum_case":
        head["checks"] = tuple((key, expression.replace("'live'", "'LIVE'"), *flags)
                               for key, expression, *flags in head["checks"])
    elif change == "missing_check":
        head["checks"] = head["checks"][1:]
    elif change == "unvalidated_check":
        key, expression, *_ = head["checks"][0]
        head["checks"] = ((key, expression, False, False, False, False, True, 0), *head["checks"][1:])
    elif change.startswith("foreign_"):
        row = list(head["foreign_keys"][0])
        index, value = {
            "foreign_schema": (12, False), "foreign_oid": (3, 0),
            "foreign_columns": (1, ("authority_id",)), "foreign_cascade": (7, "c"),
            "foreign_deferred": (9, True),
        }[change]
        row[index] = value
        head["foreign_keys"] = (tuple(row),)
    elif change == "missing_unique":
        head["source_unique_constraints"] = ()
    elif change == "deferred_unique":
        row = list(head["source_unique_constraints"][0])
        row[2] = True
        head["source_unique_constraints"] = (tuple(row),)
    elif change in {"partial_public_id", "request_id_case", "request_predicate"}:
        table = head if change == "partial_public_id" else record
        row = list(table["indexes"][0])
        if change == "request_id_case":
            row[6] = ("authority_id", "(data ->> 'REQUEST_ID'::text)")
        else:
            row[9] = "(state = 'live'::text)" if change == "partial_public_id" else None
        table["indexes"] = (tuple(row),)
    elif change == "extra_index":
        head["indexes"] = (*head["indexes"], ("memplex_unexpected", *head["indexes"][0][1:]))
    elif change == "trigger":
        head["triggers"] = (("unreviewed_trigger",),)
    else:
        head["identities"]["content_revision"] = "a"
    assert not _source_validity_catalogue_matches(tables)


@pytest.mark.parametrize("left,right,equal", [
    ("((a AND b) AND c)", "(a AND b AND c)", True),
    ("(a AND (b OR c))", "((a AND b) OR c)", False),
    ("(state = 'live'::text)", "(state = 'LIVE'::text)", False),
    ("data ->> 'request_id'::text", "data ->> 'REQUEST_ID'::text", False),
    ("f('two  spaces')", "f('two spaces')", False),
    ("(x >= 1)", "(x > 1)", False),
    ("(a", "a", False),
    ("a)", "a", False),
])
def test_catalogue_sql_comparison_preserves_meaning(left: str, right: str, equal: bool) -> None:
    from memplex.storage.migrations.catalogue_checks import _source_sql

    assert (_source_sql(left) == _source_sql(right)) is equal


@pytest.mark.parametrize("with_vector", [False, True])
def test_source_variant_keeps_historical_adoption_and_vector_identity(with_vector: bool) -> None:
    prior = "post_g002_runtime_v1_edge_integrity_current_reliable_sync_v5_background_tasks_v6"
    current = prior + "_source_validity_v1"
    if with_vector:
        prior += "_vector_8"
        current += "_vector_8"
    assert runner._allowed_adoption_baselines(prior) == runner._allowed_adoption_baselines(current)
    assert runner._is_background_tasks_current_variant(current)
    assert runner._is_source_validity_current_variant(current)
    migrations = tuple(item for item in runner.discover_migrations() if item.version <= 8)
    entries = tuple(runner._LedgerEntry(item.version, item.name, item.checksum, "executed", None)
                    for item in migrations)
    fingerprint = runner.SchemaFingerprint("post_g002_current", "fixture", current)
    assert runner._plan_from_observed_state(entries, fingerprint, migrations).state == "ready"
    with pytest.raises(runner.MigrationIntegrityError, match="migration ledger does not match"):
        runner._plan_from_observed_state(entries[:-1], fingerprint, migrations)


@pytest.fixture
def source_pg(pg_function_dsn: str):
    """Native PostgreSQL only; isolated schema and a temporary role with no login."""
    import uuid

    psycopg2 = pytest.importorskip("psycopg2")
    from psycopg2 import sql

    migration = runner.PostgresMigrationRunner(pg_function_dsn)
    assert migration.apply().state == "ready"
    role = "source_v1_" + uuid.uuid4().hex
    connection = psycopg2.connect(pg_function_dsn)
    cursor = connection.cursor()
    cursor.execute("SELECT current_schema()")
    schema = cursor.fetchone()[0]
    cursor.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER NOBYPASSRLS").format(sql.Identifier(role)))
    cursor.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(schema), sql.Identifier(role)))
    connection.commit()
    try:
        yield migration, connection, cursor, role
    finally:
        connection.rollback()
        cursor.execute(sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM {}").format(sql.Identifier(schema), sql.Identifier(role)))
        cursor.execute(sql.SQL("REVOKE ALL ON SCHEMA {} FROM {}").format(sql.Identifier(schema), sql.Identifier(role)))
        cursor.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))
        connection.commit()
        cursor.close()
        connection.close()


_AUTHORITY = "11111111-1111-4111-8111-111111111111"
_INCARNATION = "22222222-2222-4222-8222-222222222222"
_REQUEST = "33333333-3333-4333-8333-333333333333"
_PUBLICATION = "fact_derived_v1_44444444444444448444444444444444"


def _authority(cursor):
    import json

    cursor.execute(
        "INSERT INTO memplex_source_authorities VALUES (%s, 'tenant-source', 'workspace-source', "
        "'live', 1, 1, %s::jsonb, 1)",
        (_AUTHORITY, json.dumps({"revision": 1, "entries": [], "algorithm_versions": []})),
    )


def _head(cursor, *, kind="raw_paragraph", logical_id="source", tenant="tenant-source",
          workspace="workspace-source", revision=1, state="live", data=None):
    import json

    cursor.execute(
        "INSERT INTO memplex_source_heads VALUES (%s,%s,%s,%s,%s,1,%s,'direct_input',%s,1,%s,1,%s::jsonb,"
        "%s,'subject-source',%s,'user','agent-source','session-source')",
        (_AUTHORITY, kind, logical_id, _INCARNATION, revision, state,
         "" if state == "deleted" else "payload", "a" * 64,
         json.dumps(data if data is not None else {"input_context": {}, "root_refs": []}), tenant, workspace),
    )


def _record_data(*, publication=True, request=_REQUEST, generation=1, publication_id=_PUBLICATION):
    token = {"incarnation": _INCARNATION, "generation": generation, "current_publication": publication_id}
    if not publication:
        return {"contract_version": 1, "derivation_key": "b" * 64, "token": token,
                "state": "live", "last_commit_seq": 1}
    ref = {"key": {"authority_id": _AUTHORITY, "tenant_id": "tenant-source", "workspace_id": "workspace-source",
                   "kind": "raw_paragraph", "logical_id": "source"},
           "token": {"incarnation": _INCARNATION, "content_revision": 1, "auth_revision": 1},
           "digest_version": 1, "semantic_digest": "a" * 64}
    return {"contract_version": 1, "publication_id": publication_id, "target_key": "b" * 64,
            "target_token": token, "state": "active", "envelope_digest": "c" * 64,
            "output_digest": "d" * 64, "source_refs": [ref],
            "algorithm_version": "factual_capture/source-validity-v1", "request_id": request,
            "request_digest": "e" * 64, "manifest_digest": "f" * 64,
            "observed_commit_seq": 1, "commit_seq": 1}


def _record(cursor, value, *, publication=True):
    import json

    cursor.execute(
        "INSERT INTO memplex_derivation_records VALUES (%s,%s,%s,%s::jsonb,'tenant-source',"
        "'subject-source','workspace-source','user','agent-source','session-source')",
        (_AUTHORITY, "publication" if publication else "target",
         value["publication_id"] if publication else value["derivation_key"], json.dumps(value)),
    )


def _grant_source_tables(cursor, role):
    from psycopg2 import sql

    for name in _SOURCE_TABLES:
        cursor.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE ON {} TO {}").format(sql.Identifier(name), sql.Identifier(role)))


def _scope(cursor, role, *, tenant="tenant-source", workspace="workspace-source", subject="subject-source"):
    from psycopg2 import sql

    _grant_source_tables(cursor, role)
    cursor.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(role)))
    for key, value in {"tenant_id": tenant, "workspace_id": workspace, "subject_id": subject,
                       "agent_id": "agent-source", "session_id": "session-source"}.items():
        cursor.execute("SELECT set_config(%s,%s,true)", (f"memplex.{key}", value))


@pytest.mark.pg
def test_native_migration_catalogue_and_rls_contract(source_pg) -> None:
    migration, connection, cursor, role = source_pg

    assert migration.plan().current_version == 10
    _grant_source_tables(cursor, role)
    for name in _SOURCE_TABLES:
        cursor.execute(f"SELECT count(*) FROM {name}")
        assert cursor.fetchone() == (0,)
        cursor.execute("SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid=%s::regclass", (name,))
        assert cursor.fetchone() == (True, True)
        cursor.execute("SELECT has_table_privilege(%s,%s,'SELECT,INSERT,UPDATE'), has_table_privilege(%s,%s,'DELETE')",
                       (role, name, role, name))
        assert cursor.fetchone() == (True, False)
    cursor.execute("SELECT capability_name, parameter_digest FROM memplex_schema_capabilities ORDER BY capability_name")
    assert tuple(cursor.fetchall()) == (runner._SOURCE_READ_GRANTS_CAPABILITY, runner._SOURCE_CAPABILITY)
    _authority(cursor)
    connection.commit()
    _scope(cursor, role)
    _head(cursor)
    _record(cursor, _record_data())
    _record(cursor, _record_data(publication=False), publication=False)
    connection.commit()
    _scope(cursor, role)
    cursor.execute("SELECT count(*) FROM memplex_derivation_records")
    assert cursor.fetchone() == (2,)


@pytest.mark.pg
def test_cross_scope_ledger_rows_denied(source_pg) -> None:
    _migration, connection, cursor, role = source_pg
    psycopg2 = pytest.importorskip("psycopg2")
    _authority(cursor)
    _head(cursor)
    _record(cursor, _record_data())
    connection.commit()
    # Even an administrator's bypass of RLS cannot bypass composite identity.
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        _head(cursor, logical_id="forged-scope", workspace="other-workspace")
    connection.rollback()
    for overrides in ({"tenant": "other-tenant"}, {"workspace": "other-workspace"}, {"subject": "other-subject"}):
        _scope(cursor, role, **overrides)
        for name in (_SOURCE_TABLES if "subject" not in overrides else _SOURCE_TABLES[1:]):
            cursor.execute(f"SELECT count(*) FROM {name}")
            assert cursor.fetchone() == (0,)
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            _head(cursor, logical_id="forged-writer")
        connection.rollback()
    _scope(cursor, role)
    with pytest.raises(psycopg2.errors.CheckViolation):
        _head(cursor, logical_id="forged-json", data={"input_context": {}, "root_refs": [], "tenant_id": "other"})
    connection.rollback()


@pytest.mark.pg
@pytest.mark.parametrize("revision", [0, -1, True, 2**63])
def test_native_source_revision_is_positive_bigint(source_pg, revision) -> None:
    _migration, connection, cursor, _role = source_pg
    psycopg2 = pytest.importorskip("psycopg2")
    _authority(cursor)
    connection.commit()
    with pytest.raises(psycopg2.Error):
        _head(cursor, revision=revision)
    connection.rollback()
    _head(cursor, revision=2**63 - 1)


@pytest.mark.pg
@pytest.mark.parametrize("change", ["version", "unknown", "missing_request", "generation_bool", "generation_overflow", "state", "null_state", "null_algorithm"])
def test_native_derivation_json_is_closed_and_versioned(source_pg, change: str) -> None:
    _migration, connection, cursor, _role = source_pg
    psycopg2 = pytest.importorskip("psycopg2")
    _authority(cursor)
    connection.commit()
    value = _record_data()
    if change == "version":
        value["contract_version"] = True
    elif change == "unknown":
        value["body"] = "historical body is not audit metadata"
    elif change == "missing_request":
        del value["request_id"]
    elif change.startswith("generation_"):
        value["target_token"]["generation"] = True if change == "generation_bool" else 2**63
    elif change.startswith("null_"):
        value["state" if change == "null_state" else "algorithm_version"] = None
    else:
        value["state"] = "ACTIVE"
    with pytest.raises(psycopg2.Error):
        _record(cursor, value)
    connection.rollback()


@pytest.mark.pg
def test_native_public_ids_and_request_ids_include_tombstones(source_pg) -> None:
    _migration, connection, cursor, _role = source_pg
    psycopg2 = pytest.importorskip("psycopg2")
    _authority(cursor)
    _head(cursor, state="deleted")
    _record(cursor, {**_record_data(), "state": "deleted"})
    connection.commit()
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _head(cursor, kind="fact")
    connection.rollback()
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _record(cursor, _record_data(publication_id="fact_derived_v1_55555555555545558555555555555555"))
    connection.rollback()
    _record(cursor, _record_data(publication=False), publication=False)


@pytest.mark.pg
@pytest.mark.parametrize("ddl", [
    "CREATE TABLE memplex_unreviewed_source (id text)",
    "ALTER TABLE memplex_source_heads DROP CONSTRAINT memplex_source_heads_revision_check",
    "ALTER TABLE memplex_source_heads DROP CONSTRAINT memplex_source_heads_authority_fk",
    "ALTER TABLE memplex_source_heads DROP CONSTRAINT memplex_source_heads_logical_id_key",
    "ALTER TABLE memplex_source_heads NO FORCE ROW LEVEL SECURITY",
    "DROP INDEX memplex_derivation_request_key",
    "DELETE FROM memplex_schema_capabilities WHERE capability_name='source_validity_v1'",
])
def test_native_unknown_relation_or_missing_constraint_blocks_ready(source_pg, ddl: str) -> None:
    migration, connection, cursor, _role = source_pg
    cursor.execute(ddl)
    connection.commit()
    with pytest.raises(runner.MigrationIntegrityError, match="unrecognised legacy schema"):
        migration.plan()


def test_derivation_rows_decode_as_typed_codec_views() -> None:
    from memplex.source_codec import decode_ledger, encode_ledger

    target = _record_data(publication=False)
    publication = _record_data()
    del target["contract_version"]
    del publication["contract_version"]
    wire = {
        "contract_version": 1,
        "authority": {
            "scope": {"authority_id": _AUTHORITY, "tenant_id": "tenant-source", "workspace_id": "workspace-source"},
            "state": "live", "contract_version": 1,
            "policy": {"revision": 1, "entries": [], "algorithm_versions": []}, "commit_seq": 1,
        },
        "sources": [], "targets": [target], "publications": [publication],
    }
    decoded = decode_ledger(wire)
    assert decoded.targets["b" * 64].token.current_publication == _PUBLICATION
    assert decoded.publications[_PUBLICATION].request_id == _REQUEST
    assert encode_ledger(decoded) == wire


@pytest.mark.pg
@pytest.mark.parametrize("assignment", [
    "logical_id=NULL", "state='LIVE'", "kind='FACT'", "source_class='unknown'",
    "visibility='public'", "auth_revision=0", "digest_version=2",
])
def test_native_heads_reject_null_identity_and_invalid_enums(source_pg, assignment: str) -> None:
    _migration, connection, cursor, _role = source_pg
    psycopg2 = pytest.importorskip("psycopg2")
    _authority(cursor)
    _head(cursor)
    connection.commit()
    with pytest.raises(psycopg2.Error):
        cursor.execute(f"UPDATE memplex_source_heads SET {assignment}")
    connection.rollback()


@pytest.mark.pg
@pytest.mark.parametrize("revision", [True, 1.0, 0, 2])
def test_native_policy_revision_matches_strict_integer(source_pg, revision) -> None:
    import json

    _migration, connection, cursor, _role = source_pg
    psycopg2 = pytest.importorskip("psycopg2")
    _authority(cursor)
    connection.commit()
    policy = {"revision": revision, "entries": [], "algorithm_versions": []}
    with pytest.raises(psycopg2.errors.CheckViolation):
        cursor.execute("UPDATE memplex_source_authorities SET policy=%s::jsonb", (json.dumps(policy),))
    connection.rollback()


@pytest.mark.parametrize("change", ["none", "nullable_boolean", "literal_case", "connective", "cast"])
def test_postgres_boolean_test_deparse_matches_catalogue(change: str) -> None:
    from memplex.storage.migrations.catalogue_checks import _source_validity_catalogue_matches

    tables = _declared_tables()
    table = tables["memplex_derivation_records"]
    key = "memplex_derivation_records_data_check"
    expression = next(value for name, value, *_flags in table["checks"] if name == key)
    case = expression[expression.index("CASE "):expression.rindex(" END") + len(" END")]
    # PostgreSQL 16 ruleutils.c emits BooleanTest around its CASE operand,
    # whose own rendering has no parentheses. Do not mirror the SQL spelling.
    canonical = f"({case} IS TRUE)"
    if change == "nullable_boolean":
        canonical = canonical.removesuffix(" IS TRUE)") + " IS NOT FALSE)"
    elif change == "literal_case":
        canonical = canonical.replace("'active'::text", "'ACTIVE'::text")
    elif change == "connective":
        canonical = canonical.replace(" AND ", " OR ", 1)
    elif change == "cast":
        canonical = canonical.replace("::numeric", "::bigint")
    table["checks"] = tuple((name, canonical if name == key else value, *flags)
                            for name, value, *flags in table["checks"])
    assert _source_validity_catalogue_matches(tables) is (change == "none")


@pytest.mark.parametrize("ledger_version,guarded,expected", [
    (8, False, "upgrade_required"), (9, True, "ready"),
    (8, True, "integrity"), (9, False, "integrity"),
])
def test_guard_body_feature_and_migration_ledger_must_agree(ledger_version, guarded, expected):
    migrations = tuple(item for item in runner.discover_migrations() if item.version <= 9)
    entries = tuple(runner._LedgerEntry(item.version, item.name, item.checksum, "executed", None)
                    for item in migrations if item.version <= ledger_version)
    variant = "post_g002_runtime_v1_edge_integrity_current_reliable_sync_v5_background_tasks_v6_source_validity_v1"
    if guarded:
        variant += "_source_ingress_guard_v9"
    fingerprint = runner.SchemaFingerprint("post_g002_current", "fixture", variant)
    if expected == "integrity":
        with pytest.raises(runner.MigrationIntegrityError, match="ledger does not match"):
            runner._plan_from_observed_state(entries, fingerprint, migrations)
    else:
        assert runner._plan_from_observed_state(entries, fingerprint, migrations).state == expected


@pytest.mark.pg
@pytest.mark.parametrize("tamper", ["apply", "capture", "both", "ledger"])
def test_native_v9_upgrade_preserves_function_identity_and_requires_exact_pair(pg_function_dsn, tamper):
    import psycopg2

    from memplex.storage.migrations.catalogue_checks import (
        _SOURCE_INGRESS_FUNCTION_DEFINITION_DIGESTS,
        _sync_function_digest,
    )

    previous = runner.PostgresMigrationRunner(pg_function_dsn)
    previous._migrations = tuple(item for item in previous._migrations if item.version <= 8)
    assert previous.apply().current_version == 8
    latest = runner.PostgresMigrationRunner(pg_function_dsn)
    assert latest.plan().state == "upgrade_required"
    connection = psycopg2.connect(pg_function_dsn)
    names = ("memplex_sync_capture_before", "memplex_sync_apply_inbound")
    query = "SELECT proname, oid, proowner, proacl::text, proconfig, pg_get_functiondef(oid) FROM pg_proc WHERE pronamespace=current_schema()::regnamespace AND proname IN %s ORDER BY proname"
    try:
        with connection.cursor() as cursor:
            cursor.execute(query, (names,))
            before = cursor.fetchall()
            cursor.execute("SELECT version, checksum FROM memplex_schema_migrations ORDER BY version")
            ledger_before = cursor.fetchall()
        connection.rollback()
        assert latest.apply().current_version == 10
        with connection.cursor() as cursor:
            cursor.execute(query, (names,))
            after = cursor.fetchall()
            assert [row[:5] for row in after] == [row[:5] for row in before]
            for row in after:
                assert _sync_function_digest(row[5]) == _SOURCE_INGRESS_FUNCTION_DEFINITION_DIGESTS[row[0]]
            cursor.execute("SELECT version, checksum FROM memplex_schema_migrations WHERE version<=8 ORDER BY version")
            assert cursor.fetchall() == ledger_before
            if tamper == "ledger":
                cursor.execute("DELETE FROM memplex_schema_migrations WHERE version=9")
            else:
                for row in before:
                    if tamper == "both" or (tamper == "apply") == (row[0] == "memplex_sync_apply_inbound"):
                        cursor.execute(row[5])
        connection.commit()
        with pytest.raises(runner.MigrationIntegrityError):
            latest.plan()
    finally:
        connection.close()
