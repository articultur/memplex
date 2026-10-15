"""Named grants are read-only and retain the existing whole-token semantics.

Native assertions in this module require the real isolated PostgreSQL fixture;
local collection or skips are never native execution evidence.
"""
from __future__ import annotations

import re
from types import SimpleNamespace

import pytest

from memplex.auth import AuthorizationContext, Principal
from memplex.authorization import AuthorizationGate


@pytest.mark.parametrize("namespace,expected", [
    ({}, ()), (None, ()), ([], ()), ({"memplex_grants": ""}, ()),
    ({"memplex_grants": 0}, ()), ({"memplex_grants": False}, ()),
    ({"memplex_grants": "bob, bob,,charlie"}, ("bob", "charlie")),
    ({"memplex_grants": "\u2003bob\u2003,\tcharlie\n"}, ("bob", "charlie")),
    ({"memplex_grants": "Bob,bob,bob-team"}, ("Bob", "bob", "bob-team")),
    ({"memplex_grants": 42}, ("42",)),
])
def test_read_grant_projection_matches_existing_token_authorization(namespace, expected):
    from memplex.storage.postgres_sources import _read_grants

    payload = {"namespace": namespace}
    assert _read_grants(payload) == expected
    for agent in ("bob", "Bob", "BOB", "b", "bob-team", "charlie", "42", "unknown"):
        context = AuthorizationContext(Principal("tenant", "subject", frozenset({"member"})),
                                       "workspace", agent_id=agent)
        assert (agent in _read_grants(payload)) is AuthorizationGate._agent_has_grant(
            SimpleNamespace(namespace=namespace), context)
    fallback = AuthorizationContext(Principal("tenant", "bob", frozenset({"member"})), "workspace")
    assert ("bob" in _read_grants(payload)) is AuthorizationGate._agent_has_grant(
        SimpleNamespace(namespace=namespace), fallback)


def test_v10_is_a_new_immutable_select_only_schema_transition():
    from memplex.storage.migrations import runner

    migrations = runner.discover_migrations()
    tenth = next(item for item in migrations if item.version == 10)
    assert tenth.name == "source_read_grants"
    sql = tenth.sql_bytes.decode()
    assert "read_grants TEXT[] NOT NULL DEFAULT '{}'::text[]" in sql
    assert sql.count("FOR SELECT") == 7
    for forbidden in ("DROP POLICY", "ALTER POLICY", "SECURITY DEFINER", "CREATE ROLE"):
        assert forbidden not in sql.upper()
    assert not re.search(r"(?im)^\s*GRANT\b", sql)
    for name in ("memplex_source_heads", "memplex_derivation_records", "memplex_paragraphs",
                 "memplex_functions", "memplex_facts", "memplex_preferences", "memplex_observations"):
        assert f"CREATE POLICY {name}_source_read_grants ON {name}" in sql


def test_v10_feature_keeps_prior_schema_identity_but_requires_matching_ledger():
    from memplex.storage.migrations import runner

    base = ("post_g002_runtime_v1_edge_integrity_current_reliable_sync_v5_background_tasks_v6"
            "_source_validity_v1_source_ingress_guard_v9")
    for suffix in ("", "_vector_8"):
        v10 = base + "_source_read_grants_v10" + suffix
        assert runner._is_source_validity_current_variant(v10)
        assert runner._is_source_ingress_guard_current_variant(v10)
        assert runner._is_background_tasks_current_variant(v10)
        assert runner._allowed_adoption_baselines(v10) == runner._allowed_adoption_baselines(base + suffix)
        migrations = runner.discover_migrations()
        entries = tuple(runner._LedgerEntry(m.version, m.name, m.checksum, "executed", None) for m in migrations)
        fingerprint = runner.SchemaFingerprint("post_g002_current", "fixture", v10)
        assert runner._plan_from_observed_state(entries, fingerprint, migrations).state == "ready"
        with pytest.raises(runner.MigrationIntegrityError, match="migration ledger does not match"):
            runner._plan_from_observed_state(entries[:-1], fingerprint, migrations)


from tests.test_source_v1_compatibility import (
    ordinary_pg_owner as ordinary_pg_owner,  # noqa: PLC0414 - fixture registration
)
from tests.test_source_v1_pg_transactions import NATIVE
from tests.test_source_v1_pg_transactions import (
    source_backend as source_backend,  # noqa: PLC0414 - fixture registration
)
from tests.test_source_v1_publication import (
    publication_backend as publication_backend,  # noqa: PLC0414 - fixture registration
)
from tests.test_source_v1_publication import (
    publication_rls_backend as publication_rls_backend,  # noqa: PLC0414 - fixture registration
)


@pytest.fixture
def occupied_v9(request, monkeypatch, publication_backend):
    """Real v9 SQL with canonical, bound DTO seed; no claim of old-writer execution.

    The existing ordinary_pg_owner fixture uses startup SET ROLE on the test
    administrator connection. We assert the effective role is the table owner
    and is neither superuser nor BYPASSRLS, and retain session_user separately.
    """
    import time
    from contextlib import closing
    from dataclasses import asdict, replace

    import psycopg2

    from memplex.source_codec import _canonical, _wire
    from memplex.source_contract import SourceKey, SourceKind, SourceOperation
    from memplex.storage.migrations import runner
    from memplex.storage.postgres_resources import (
        _source_target_identity,
        _write_source_schema_marker,
    )
    from memplex.storage.postgres_sources import _identity, _locator
    from tests.test_source_v1_lifecycle_matrix import _context, _policy, _publish, _write
    from tests.test_source_v1_pg_transactions import CONTEXT, POLICY, SCOPE
    from tests.test_source_v1_policy import policy_authority

    all_migrations = runner.discover_migrations()
    with monkeypatch.context() as patch:
        patch.setattr(runner, "discover_migrations", lambda: tuple(m for m in all_migrations if m.version <= 9))
        admin_dsn, owner_dsn, target = request.getfixturevalue("ordinary_pg_owner")
    bank = SimpleNamespace(**{**vars(publication_backend), "scope": SCOPE, "context": CONTEXT})
    bob = replace(_context(SCOPE, "bob"), agent_id="reader-agent")
    charlie = replace(_context(SCOPE, "charlie"), agent_id="ungranted-agent")
    policy = replace(POLICY, revision=2, entries=POLICY.entries + (_policy(bob).entries[0], _policy(charlie).entries[0]))
    bank.backend.replace_policy(1, policy, policy_authority(bank))
    namespace = {"domain": "w09", "memplex_subject_id": "alice",
                 "memplex_grants": "\u2003reader-agent\u2003,reader-agent"}
    source = _write(bank, "S", "UPGRADE_SOURCE", visibility="user", namespace=namespace)
    _, derived = _publish(bank, source, "UPGRADE_GRANTED_BODY", private=True, grants="reader-agent")
    revoked = _write(bank, "R", "REVOKED_BODY", visibility="user", namespace=namespace)
    _write(bank, "R", "REVOKED_BODY", receipt=revoked, visibility="user")
    dead = _write(bank, "T", "TOMBSTONE_BODY", visibility="user", namespace=namespace)
    _write(bank, "T", "", receipt=dead, operation=SourceOperation.DELETE)
    _write(bank, "Q", "QUARANTINED_BODY", visibility="user", namespace=namespace)
    _write(bank, "L", "LEGACY_BODY", visibility="user", namespace=namespace)
    names = ("S", "R", "T", "Q", "L", derived.publication_id)
    with bank.backend.read_fence(CONTEXT, deadline_ns=time.monotonic_ns() + 250_000_000) as session:
        authority = session.authority()
        snapshots = [session.source(SourceKey(SCOPE.authority_id, SCOPE.tenant_id, SCOPE.workspace_id,
                                             SourceKind.FACT, name)) for name in names]
        publication = session.publication(derived.publication_id)
        target_head = session.target("d" * 64)
    with closing(psycopg2.connect(admin_dsn)) as connection, connection, connection.cursor() as cur:
        cur.execute("INSERT INTO memplex_source_authorities VALUES (%s,%s,%s,'live',%s,%s,%s::jsonb,1)",
                    (SCOPE.authority_id, SCOPE.tenant_id, SCOPE.workspace_id, policy.revision,
                     authority.commit_seq, _canonical(policy)))
        for snapshot in snapshots:
            head = snapshot.head
            payload = dict(snapshot.payload)
            if payload:
                payload.update(agent_id=CONTEXT.agent_id, session_id=CONTEXT.session_id)
                head = replace(head, payload_locator=_locator(head.ref.key, payload))
                cur.execute("INSERT INTO memplex_facts(id,data,tenant_id,owner_subject,workspace,visibility,source_agent,source_session) "
                            "VALUES (%s,%s::jsonb,%s,%s,%s,%s,%s,%s)",
                            (payload["id"], _canonical(payload), *_identity(payload)))
            if head.ref.key.logical_id == "L":
                head = replace(head, source_class=type(head.source_class).LEGACY_UNVERIFIED)
            if head.ref.key.logical_id == "Q":
                head = replace(head, state=type(head.state).QUARANTINED)
            identity = _identity(payload) if payload else (
                SCOPE.tenant_id, "alice", SCOPE.workspace_id, "user", CONTEXT.agent_id, CONTEXT.session_id)
            cur.execute("INSERT INTO memplex_source_heads VALUES "
                        "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s)",
                        (SCOPE.authority_id, "fact", head.ref.key.logical_id, head.ref.token.incarnation,
                         head.ref.token.content_revision, head.ref.token.auth_revision, head.state.value,
                         head.source_class.value, head.payload_locator, head.ref.digest_version,
                         head.ref.semantic_digest, head.last_commit_seq,
                         _canonical({"input_context": head.input_context, "root_refs": head.root_refs}), *identity))
        derived_identity = (SCOPE.tenant_id, "alice", SCOPE.workspace_id, "user", CONTEXT.agent_id, CONTEXT.session_id)
        for kind, key, record in (("publication", publication.publication_id, publication),
                                  ("target", target_head.derivation_key, target_head)):
            cur.execute("INSERT INTO memplex_derivation_records VALUES (%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s,%s)",
                        (SCOPE.authority_id, kind, key, _canonical({"contract_version": 1, **_wire(record)}), *derived_identity))
        _write_source_schema_marker(cur, target.schema, {"version": 1, "state": "live", "scope": asdict(SCOPE),
                                                        "storage_identity": _source_target_identity(target)})
    with closing(psycopg2.connect(owner_dsn)) as connection, connection.cursor() as cur:
        cur.execute("SELECT current_user,session_user,rolsuper,rolbypassrls, "
                    "(SELECT rolsuper FROM pg_roles WHERE rolname=session_user), "
                    "(SELECT rolbypassrls FROM pg_roles WHERE rolname=session_user) FROM pg_roles WHERE rolname=current_user")
        identity = cur.fetchone()
        assert identity[0] != identity[1] and identity[2:4] == (False, False)
        owner_tables = sorted(("memplex_derivation_records", "memplex_facts", "memplex_functions", "memplex_observations",
                               "memplex_paragraphs", "memplex_preferences", "memplex_source_authorities", "memplex_source_heads"))
        cur.execute("SELECT relname,relowner=current_user::regrole FROM pg_class "
                    "WHERE relnamespace=current_schema()::regnamespace AND relname = ANY(%s) ORDER BY relname", (owner_tables,))
        assert cur.fetchall() == [(name, True) for name in owner_tables]
        cur.execute("SELECT count(*) FROM memplex_source_heads")
        assert cur.fetchone() == (0,), "FORCE RLS must hide other-owner heads before maintenance"
    request.node.user_properties.append(("maintenance_identity", str(identity)))
    return SimpleNamespace(admin_dsn=admin_dsn, owner_dsn=owner_dsn, target=target, source=source,
                           derived=derived, bob=bob, charlie=charlie, scope=SCOPE, context=CONTEXT)


def _business_rows(bank):
    from contextlib import closing

    import psycopg2

    with closing(psycopg2.connect(bank.admin_dsn)) as connection, connection.cursor() as cur:
        result = {}
        for name in ("memplex_source_authorities", "memplex_source_heads", "memplex_derivation_records", "memplex_facts"):
            cur.execute(f"SELECT to_jsonb(t)-'read_grants' FROM {name} t ORDER BY (to_jsonb(t)-'read_grants')::text")
            result[name] = cur.fetchall()
        cur.execute("SELECT version,checksum FROM memplex_schema_migrations WHERE version<=9 ORDER BY version")
        result["old_ledger"] = cur.fetchall()
        return result


def _owner_catalogue(bank):
    """Read the same owner-relative catalogue predicates at both boundaries."""
    from contextlib import closing

    import psycopg2

    from memplex.storage.migrations import runner

    with closing(psycopg2.connect(bank.owner_dsn)) as connection, connection.cursor() as cur:
        catalogue = runner._catalog_snapshot(cur)
    assert connection.closed
    return catalogue


def _assert_v9_rollback(bank, before, *, application_acl=None, catalogue_before=None):
    from contextlib import closing

    import psycopg2

    from memplex.storage.migrations import runner
    from memplex.storage.migrations.source_read_grants import _MAINTENANCE_TABLES

    assert _business_rows(bank) == before
    assert runner.PostgresMigrationRunner(bank.owner_dsn).plan(
        application_acl=application_acl,
        deployment_profile="development" if application_acl is not None else None,
    ).current_version == 9
    if application_acl is not None:
        assert catalogue_before is not None
        assert _owner_catalogue(bank) == catalogue_before
    with closing(psycopg2.connect(bank.admin_dsn)) as connection, connection.cursor() as cur:
        cur.execute("SELECT relname,relrowsecurity,relforcerowsecurity FROM pg_class "
                    "WHERE relnamespace=current_schema()::regnamespace AND relname=ANY(%s) ORDER BY relname",
                    (list(_MAINTENANCE_TABLES),))
        assert cur.fetchall() == [(name, True, True) for name in _MAINTENANCE_TABLES]
        cur.execute("SELECT count(*) FROM information_schema.columns WHERE table_schema=current_schema() "
                    "AND table_name='memplex_source_heads' AND column_name='read_grants'")
        assert cur.fetchone() == (0,)


@NATIVE
@pytest.mark.parametrize("publication_backend", ["lite"], indirect=True)
@pytest.mark.parametrize("isolation", ["READ COMMITTED", "REPEATABLE READ"])
def test_native_v9_owner_upgrade_projects_existing_acl_without_recertifying(occupied_v9, isolation):
    from contextlib import closing

    import psycopg2

    from memplex.storage.migrations import runner

    bank = occupied_v9
    before = _business_rows(bank)
    def connect():
        connection = psycopg2.connect(bank.owner_dsn)
        connection.set_session(isolation_level=isolation)
        return connection
    migration = runner.PostgresMigrationRunner(bank.owner_dsn, connection_factory=connect)
    assert migration.apply().current_version == 10
    assert _business_rows(bank) == before
    with closing(psycopg2.connect(bank.admin_dsn)) as connection, connection.cursor() as cur:
        cur.execute("SELECT logical_id,read_grants FROM memplex_source_heads ORDER BY logical_id")
        values = dict(cur.fetchall())
        assert values == {"S": ["reader-agent"], "R": [], "T": [], "L": [], "Q": ["reader-agent"],
                          bank.derived.publication_id: ["reader-agent"]}
    assert migration.plan().state == "ready"


@NATIVE
@pytest.mark.parametrize("publication_backend", ["lite"], indirect=True)
@pytest.mark.parametrize("boundary", ["no_force", "backfill", "connection_loss", "malformed_locator", "force_restore"])
def test_native_upgrade_failure_restores_exact_v9_and_preserves_first_error(occupied_v9, monkeypatch, boundary):
    from contextlib import closing

    import psycopg2
    from psycopg2.extensions import connection as PgConnection
    from psycopg2.extensions import cursor as PgCursor

    from memplex.storage.migrations import runner
    from memplex.storage.migrations import source_read_grants as implementation

    bank = occupied_v9
    if boundary == "malformed_locator":
        with closing(psycopg2.connect(bank.admin_dsn)) as connection, connection, connection.cursor() as cur:
            cur.execute("UPDATE memplex_source_heads SET payload_locator='malformed' WHERE logical_id='S'")
    before = _business_rows(bank)
    fired = []
    class Cursor(PgCursor):
        def execute(self, sql, parameters=None):
            result = super().execute(sql, parameters)
            match = ((boundary in {"no_force", "connection_loss"} and " NO FORCE ROW LEVEL SECURITY" in str(sql))
                     or (boundary == "force_restore" and " FORCE ROW LEVEL SECURITY" in str(sql)
                         and " NO FORCE ROW LEVEL SECURITY" not in str(sql)))
            if match and not fired:
                fired.append(str(sql))
                if boundary == "connection_loss":
                    self.connection.close()
                raise RuntimeError("original migration boundary failure")
            return result
    class Connection(PgConnection):
        def cursor(self, *args, **kwargs):
            return super().cursor(*args, cursor_factory=Cursor, **kwargs)
    original = implementation._backfill_head
    def fail_after_backfill(*args):
        original(*args)
        raise RuntimeError("original migration boundary failure")
    if boundary == "backfill":
        monkeypatch.setattr(implementation, "_backfill_head", fail_after_backfill)
    migration = runner.PostgresMigrationRunner(bank.owner_dsn,
        connection_factory=lambda: psycopg2.connect(bank.owner_dsn, connection_factory=Connection))
    expected = ValueError if boundary == "malformed_locator" else RuntimeError
    with pytest.raises(expected, match=None if boundary == "malformed_locator" else "original migration boundary failure"):
        migration.apply()
    _assert_v9_rollback(bank, before)


from contextlib import contextmanager


@contextmanager
def _application_role(bank, *, vector=False):
    """Exact existing app ACL on a temporary NOLOGIN role, never a credential."""
    import uuid
    from contextlib import closing

    import psycopg2
    from psycopg2 import sql

    from memplex.storage.migrations._constants import (
        _APPLICATION_ACL,
        _APPLICATION_ACL_FUNCTIONS,
        _SYNC_FUNCTIONS,
    )
    from memplex.storage.migrations.runner import _vector_extension_type

    role = "grant_reader_" + uuid.uuid4().hex
    runtime_path = bank.target.schema
    with closing(psycopg2.connect(bank.admin_dsn)) as connection:
        try:
            with connection, connection.cursor() as cur:
                cur.execute(sql.SQL("CREATE ROLE {} NOLOGIN NOSUPERUSER NOBYPASSRLS").format(sql.Identifier(role)))
                cur.execute(sql.SQL("REVOKE USAGE, CREATE ON SCHEMA {} FROM PUBLIC").format(sql.Identifier(bank.target.schema)))
                cur.execute(sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(sql.Identifier(bank.target.schema), sql.Identifier(role)))
                for table, privileges in _APPLICATION_ACL.items():
                    if privileges:
                        cur.execute(sql.SQL("GRANT {} ON {} TO {}").format(
                            sql.SQL(", ".join(sorted(privileges))), sql.Identifier(table), sql.Identifier(role)))
                for sequence in ("memplex_changelog_id_seq", "memplex_sync_outbox_stream_seq_seq"):
                    cur.execute(sql.SQL("GRANT USAGE ON SEQUENCE {} TO {}").format(sql.Identifier(sequence), sql.Identifier(role)))
                cur.execute("SELECT proname,oid::regprocedure::text FROM pg_proc "
                            "WHERE pronamespace=current_schema()::regnamespace AND proname=ANY(%s)", (list(_SYNC_FUNCTIONS),))
                for name, signature in cur.fetchall():
                    if name in _APPLICATION_ACL_FUNCTIONS:
                        cur.execute(sql.SQL("GRANT EXECUTE ON FUNCTION {} TO {}").format(sql.SQL(signature), sql.Identifier(role)))
                if vector:
                    extension = _vector_extension_type(cur)
                    assert extension is not None, "vector fixture requires the installed extension type"
                    namespace, _type_oid = extension
                    assert re.fullmatch(r"[a-z_][a-z0-9_]*", namespace), "unsupported vector fixture schema identifier"
                    runtime_path += "," + namespace
            dsn = psycopg2.extensions.make_dsn(bank.admin_dsn,
                options=f"-c search_path={runtime_path} -c role={role}")
            with closing(psycopg2.connect(dsn)) as app, app.cursor() as cur:
                cur.execute("SELECT current_user,rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")
                assert cur.fetchone() == (role, False, False)
            yield dsn, role
        finally:
            connection.rollback()
            with connection, connection.cursor() as cur:
                cur.execute(sql.SQL("DROP OWNED BY {}").format(sql.Identifier(role)))
                cur.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


@contextmanager
def _upgraded_runtime(bank, *, vector=False):
    from memplex.config import SourceContractConfig
    from memplex.storage.migrations.runner import VectorCapabilityRequest
    from memplex.storage.postgres import PostgresMemoryStore
    from memplex.storage.postgres_resources import PostgresStorageResources
    from memplex.storage.source_versions import _RESOURCE_ISSUER

    with _application_role(bank, vector=vector) as (dsn, role):
        resources = PostgresStorageResources._for_source_validity(
            dsn, migration_dsn=bank.owner_dsn, issuer=_RESOURCE_ISSUER, capacity=2)
        try:
            request = VectorCapabilityRequest(8, "best_effort") if vector else VectorCapabilityRequest(0, "disabled")
            status = resources.ensure_ready(request, "development")
            assert resources.state == "READY"
            assert status.state == ("ready" if vector else "disabled")
            assert resources.ready_pool.effective_dim == (8 if vector else 0)
            store = PostgresMemoryStore(dsn, ready_pool=resources.ready_pool, require_authorization=True,
                embedder=SimpleNamespace(embed=lambda text: [1.0] + [0.0] * 7) if vector else None)
            resources._source_startup(SourceContractConfig("v1", bank.scope.authority_id,
                bank.scope.tenant_id, bank.scope.workspace_id), store, issuer=_RESOURCE_ISSUER)
            yield SimpleNamespace(**{**vars(bank), "kind": "postgres", "dsn": dsn, "role": role,
                "store": store, "backend": store.authorized(bank.context).source_versions(), "resources": resources})
        finally:
            resources.close()


@NATIVE
@pytest.mark.parametrize("publication_backend", ["lite"], indirect=True)
@pytest.mark.parametrize("entry,vector", [("service", False), ("http", False), ("service", True), ("http", True)])
def test_native_bootstrap_upgrade_discovers_granted_d_through_real_query(occupied_v9, tmp_path, monkeypatch, entry, vector):
    from contextlib import ExitStack, closing
    from dataclasses import replace

    import psycopg2

    from memplex.source_contract import SourceContractError
    from tests.test_source_v1_lifecycle_matrix import QUERY, _read, _service, _write

    before = _business_rows(occupied_v9)
    if vector:
        with closing(psycopg2.connect(occupied_v9.admin_dsn)) as connection, connection, connection.cursor() as cur:
            cur.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
    with _upgraded_runtime(occupied_v9, vector=vector) as bank, ExitStack() as cleanup:
        assert _business_rows(bank) == before
        service = _service(bank, tmp_path / "real-query", cleanup)
        reader = SimpleNamespace(**{**vars(bank), "context": bank.bob,
            "backend": bank.store.authorized(bank.bob).source_versions()})
        _write(reader, "U", "UPGRADE_INDEPENDENT_U")
        assert service.get(bank.derived.publication_id, authorization=bank.bob).object_ == "UPGRADE_GRANTED_BODY"
        assert service.get(bank.derived.publication_id, authorization=bank.charlie) is None
        # No candidate retriever, SQL, result or permission gate is patched.
        ids, body = _read(service, bank.bob, SimpleNamespace(surface="query"), entry, (), None, monkeypatch)
        assert bank.derived.publication_id in ids and "U" in ids
        assert "UPGRADE_GRANTED_BODY" in body and "UPGRADE_INDEPENDENT_U" in body
        assert "REVOKED_BODY" not in body and "TOMBSTONE_BODY" not in body and "QUARANTINED_BODY" not in body and "LEGACY_BODY" not in body
        assert bank.derived.publication_id in {n.id for n in bank.store.authorized(bank.bob).list_facts()}
        assert bank.derived.publication_id in {n.func_id for n in bank.store.authorized(bank.bob).fts_search(QUERY, 20)}
        with pytest.raises(PermissionError, match="only the memory owner"):
            service.share_with("S", "ungranted-agent", authorization=bank.bob)
        from memplex.source_contract import SourceOperation
        from tests.test_source_v1_pg_transactions import _command
        with pytest.raises(SourceContractError, match="missing"):
            reader.backend.mutate_source(bank.bob, replace(_command(name="S"), expected=bank.source.ref.token,
                operation=SourceOperation.REPLACE), deadline_ns=__import__("time").monotonic_ns() + 2_000_000_000)
        with pytest.raises(SourceContractError, match="publication_conflict"):
            reader.backend.delete_target(bank.bob, "d" * 64, bank.derived.target,
                deadline_ns=__import__("time").monotonic_ns() + 2_000_000_000)
        assert service.get(bank.derived.publication_id, authorization=bank.context).object_ == "UPGRADE_GRANTED_BODY"
        revoked = _write(bank, "S", "UPGRADE_SOURCE", receipt=bank.source, visibility="user")
        assert revoked.ref.token.auth_revision == bank.source.ref.token.auth_revision + 1
        assert service.get(bank.derived.publication_id, authorization=bank.bob) is None
        ids, body = _read(service, bank.bob, SimpleNamespace(surface="query"), entry, (), None, monkeypatch)
        assert "U" in ids and bank.derived.publication_id not in ids and "UPGRADE_GRANTED_BODY" not in body
        _write(bank, "S", "UPGRADE_SOURCE", receipt=revoked, visibility="user",
               namespace={"memplex_grants": "reader-agent"})
        assert service.get(bank.derived.publication_id, authorization=bank.bob) is None
        assert service.get("U", authorization=bank.bob).object_ == "UPGRADE_INDEPENDENT_U"


@NATIVE
@pytest.mark.parametrize("publication_backend", ["lite"], indirect=True)
def test_native_private_grant_cannot_update_delete_or_change_projection(occupied_v9):
    from contextlib import closing

    import psycopg2

    from memplex.storage.postgres_sources import _IDENTITY_COLUMNS

    with _upgraded_runtime(occupied_v9) as bank:
        before = _business_rows(bank)
        with closing(psycopg2.connect(bank.dsn)) as connection, connection.cursor() as cur:
            for key, value in {"tenant_id": bank.scope.tenant_id, "workspace_id": bank.scope.workspace_id,
                "subject_id": bank.bob.principal.subject_id, "agent_id": bank.bob.agent_id,
                "session_id": bank.bob.session_id, "grants": "ungranted-agent"}.items():
                cur.execute("SELECT set_config(%s,%s,true)", ("memplex." + key, value))
            cur.execute(f"SELECT {_IDENTITY_COLUMNS},read_grants FROM memplex_source_heads WHERE logical_id='S'")
            assert cur.fetchone() is not None, "the same grantee must first have real SELECT access"
            for sql in ("UPDATE memplex_source_heads SET read_grants=ARRAY['ungranted-agent'] WHERE logical_id='S' RETURNING logical_id",
                        "UPDATE memplex_facts SET data=jsonb_set(data,'{object}','\"FORBIDDEN_WRITE\"') WHERE id='S' RETURNING id",
                        "DELETE FROM memplex_facts WHERE id='S' RETURNING id",
                        "UPDATE memplex_derivation_records SET data=data WHERE record_kind='target' RETURNING record_key"):
                cur.execute(sql)
                assert cur.fetchall() == []
            for field, wrong in (("tenant_id", "foreign-tenant"), ("workspace_id", "foreign-workspace"),
                                  ("agent_id", "ungranted-agent"), ("agent_id", "READER-AGENT")):
                good = {"tenant_id": bank.scope.tenant_id, "workspace_id": bank.scope.workspace_id,
                        "agent_id": bank.bob.agent_id}[field]
                cur.execute("SELECT set_config(%s,%s,true)", ("memplex." + field, wrong))
                cur.execute("SELECT id FROM memplex_facts WHERE id='S'")
                assert cur.fetchall() == []
                cur.execute("SELECT set_config(%s,%s,true)", ("memplex." + field, good))
            connection.commit()
            for key, value in {"tenant_id": bank.scope.tenant_id, "workspace_id": bank.scope.workspace_id,
                "subject_id": bank.charlie.principal.subject_id, "agent_id": bank.charlie.agent_id,
                "session_id": bank.charlie.session_id, "grants": "reader-agent"}.items():
                cur.execute("SELECT set_config(%s,%s,true)", ("memplex." + key, value))
            cur.execute("SELECT id FROM memplex_facts WHERE id='S'")
            assert cur.fetchall() == []
        assert _business_rows(bank) == before
        # A model namespace/GUC on a different ungranted row cannot issue a grant.
        assert bank.store.authorized(bank.charlie).get_fact("S") is None


@NATIVE
@pytest.mark.parametrize("publication_backend", ["lite"], indirect=True)
@pytest.mark.parametrize("vector", [False, True])
def test_native_maintenance_lock_blocks_ordinary_app_and_bootstrap_failure_never_seals(occupied_v9, monkeypatch, vector, request):
    from contextlib import closing

    import psycopg2

    from memplex.storage import pool as pool_module
    from memplex.storage.migrations import runner
    from memplex.storage.migrations import source_read_grants as implementation
    from memplex.storage.migrations.runner import VectorCapabilityRequest
    from memplex.storage.postgres_resources import PostgresStorageResources
    from memplex.storage.source_versions import _RESOURCE_ISSUER

    bank = occupied_v9
    before = _business_rows(bank)
    original = implementation._backfill_head
    observed = []
    opened = []
    readers = []
    original_error = RuntimeError("original blocked-reader boundary")
    new_runner = pool_module._new_migration_runner
    def tracking_runner(dsn):
        migration_runner = new_runner(dsn)
        open_connection = migration_runner._open_connection
        def tracking_open():
            connection = open_connection()
            opened.append(connection)
            return connection
        monkeypatch.setattr(migration_runner, "_open_connection", tracking_open)
        return migration_runner
    monkeypatch.setattr(pool_module, "_new_migration_runner", tracking_runner)
    with _application_role(bank) as (dsn, role):
        application_acl = runner.ApplicationAclContract(role)
        assert runner.PostgresMigrationRunner(bank.owner_dsn).plan(
            application_acl=application_acl, deployment_profile="development",
        ).current_version == 9
        catalogue_before = _owner_catalogue(bank)
        def observe_then_fail(cur, row):
            original(cur, row)
            with closing(psycopg2.connect(dsn)) as reader:
                readers.append(reader)
                with reader.cursor() as check:
                    check.execute("SET LOCAL lock_timeout='100ms'")
                    with pytest.raises(psycopg2.errors.LockNotAvailable):
                        check.execute("SELECT count(*) FROM memplex_source_heads")
                reader.rollback()
                with reader.cursor() as check:
                    check.execute("SET LOCAL lock_timeout='100ms'")
                    with pytest.raises(psycopg2.errors.LockNotAvailable):
                        check.execute("UPDATE memplex_facts SET data=data WHERE id='S'")
                reader.rollback()
            observed.append(True)
            request.node.user_properties.append(("maintenance_stage", "read_and_write_lock_refused"))
            raise original_error
        monkeypatch.setattr(implementation, "_backfill_head", observe_then_fail)
        resource = PostgresStorageResources._for_source_validity(dsn, migration_dsn=bank.owner_dsn,
                                                               issuer=_RESOURCE_ISSUER, capacity=2)
        try:
            with pytest.raises(RuntimeError, match="original blocked-reader boundary") as failed:
                resource.ensure_ready(VectorCapabilityRequest(8, "best_effort") if vector else VectorCapabilityRequest(0, "disabled"), "development")
            assert failed.value is original_error
            assert observed == [True]
            assert resource.state == "FAULTED" and not resource.pool_created
            with pytest.raises(RuntimeError, match="not ready"):
                _ = resource.ready_pool
            with pytest.raises(RuntimeError, match="resources are faulted") as closed:
                resource.close()
            assert closed.value.__cause__ is original_error
            assert opened and readers and all(connection.closed for connection in opened + readers)
            assert resource._staged_manager is None and resource._staged_raw_pool is None
            assert resource._pool_manager is None and resource._ready_pool is None
            request.node.user_properties.append(("owned_connections_closed", str(len(opened) + len(readers))))
            request.node.user_properties.append(("bootstrap_stage", "faulted_without_pool_or_seal"))
        finally:
            if resource.state != "FAULTED":
                resource.close()
        _assert_v9_rollback(bank, before, application_acl=application_acl, catalogue_before=catalogue_before)
    request.node.user_properties.append(("rollback_stage", "exact_v9_restored"))


@NATIVE
@pytest.mark.parametrize("publication_backend", ["lite"], indirect=True)
def test_native_vector_and_lexical_function_legs_use_read_grants(occupied_v9, tmp_path, request):
    import time
    from contextlib import ExitStack, closing

    import psycopg2

    from memplex.models import FieldValue, Function
    from memplex.source_contract import (
        ExpectedAbsent,
        SourceClass,
        SourceContractError,
        SourceKey,
        SourceKind,
        SourceMutation,
        SourceOperation,
    )
    from memplex.storage.migrations.runner import _vector_extension_type
    from memplex.storage.postgres import _acl_scope_sql
    from tests.test_source_v1_lifecycle_matrix import QUERY, _service

    with closing(psycopg2.connect(occupied_v9.admin_dsn)) as connection, connection, connection.cursor() as cur:
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public")
    with _upgraded_runtime(occupied_v9, vector=True) as bank, ExitStack() as cleanup:
        function = Function(id="grant-function", name=QUERY,
            trigger=[FieldValue(desc="lexical-grant-token")], action=[FieldValue(desc="FUNCTION_GRANTED_BODY")],
            visibility="user", namespace={"memplex_grants": "reader-agent"})
        command = SourceMutation(SourceKey(bank.scope.authority_id, bank.scope.tenant_id, bank.scope.workspace_id,
            SourceKind.FUNCTION, function.id), ExpectedAbsent.ABSENT, SourceOperation.CREATE,
            function.to_dict(), SourceClass.DIRECT_INPUT, {}, ())
        bank.backend.mutate_source(bank.context, command, deadline_ns=time.monotonic_ns() + 2_000_000_000)
        update = f"UPDATE memplex_functions SET embedding=%s::vector WHERE id=%s AND {_acl_scope_sql()} RETURNING id"
        parameters = (str([1.0] + [0.0] * 7), function.id)
        with pytest.raises(SourceContractError, match="unsupported_semantics"):
            bank.store.authorized(bank.context)._execute(update, parameters)
        # Fixture preparation uses the ordinary app role and the existing owner
        # mutation predicate; source read cursors retain their SELECT-only guard.
        with closing(psycopg2.connect(bank.dsn)) as connection, connection, connection.cursor() as cur:
            bank.store._bind_transaction_scope(cur, bank.context)
            cur.execute("SELECT current_user,session_user,rolsuper,rolbypassrls FROM pg_roles WHERE rolname=current_user")
            identity = cur.fetchone()
            assert identity[0] == bank.role and identity[0] != identity[1] and identity[2:] == (False, False)
            request.node.user_properties.append(("embedding_identity", str(identity)))
            extension = _vector_extension_type(cur)
            assert extension is not None
            cur.execute("SELECT current_schema(),to_regtype('vector')::oid")
            assert cur.fetchone() == (bank.target.schema, extension[1])
            request.node.user_properties.append(("embedding_vector_type", str(extension)))
            cur.execute(update, parameters)
            assert cur.fetchone() == (function.id,)
        assert connection.closed
        request.node.user_properties.append(("embedding_fixture", "ordinary_owner_connection_closed"))
        reader = bank.store.authorized(bank.bob)
        assert function.id in {r.func_id for r in reader.fts_search("lexical-grant-token", 20)}
        assert function.id in {r.func_id for r in reader.vector_search("no-lexical-match-here", 20)}
        assert function.id not in {r.func_id for r in bank.store.authorized(bank.charlie).vector_search("no-lexical-match-here", 20)}
        service = _service(bank, tmp_path / "vector-query", cleanup)
        assert function.id in {r.func_id for r in service.query("no-lexical-match-here", authorization=bank.bob).results}
        request.node.user_properties.append(("discovery_stage", "bob_fts_vector_service_allow_charlie_deny"))


@NATIVE
@pytest.mark.parametrize("publication_backend", ["lite"], indirect=True)
@pytest.mark.parametrize("drift", ["force", "missing_policy", "or_true", "nullable", "default", "projection"])
def test_native_new_grant_catalogue_or_projection_drift_fails_closed(occupied_v9, tmp_path, drift, request):
    import time
    from contextlib import ExitStack, closing

    import psycopg2

    from memplex.source_contract import SourceContractError
    from memplex.storage.migrations import runner
    from memplex.storage.migrations.source_read_grants import _grant_policy_expressions
    from tests.test_source_v1_lifecycle_matrix import _service, _write

    with _upgraded_runtime(occupied_v9) as bank, ExitStack() as cleanup:
        service = _service(bank, tmp_path / "drift", cleanup)
        _write(bank, "U", "DRIFT_INDEPENDENT_U")
        assert service.get(bank.derived.publication_id, authorization=bank.bob) is not None
        with closing(psycopg2.connect(bank.admin_dsn)) as connection, connection, connection.cursor() as cur:
            sql = {
                "force": "ALTER TABLE memplex_source_heads NO FORCE ROW LEVEL SECURITY",
                "missing_policy": "DROP POLICY memplex_source_heads_source_read_grants ON memplex_source_heads",
                "nullable": "ALTER TABLE memplex_source_heads ALTER COLUMN read_grants DROP NOT NULL",
                "default": "ALTER TABLE memplex_source_heads ALTER COLUMN read_grants SET DEFAULT ARRAY['ungranted-agent']",
                "projection": "UPDATE memplex_source_heads SET read_grants=ARRAY['ungranted-agent'] WHERE logical_id='S'",
            }
            if drift == "or_true":
                cur.execute("ALTER POLICY memplex_source_heads_source_read_grants ON memplex_source_heads USING ("
                            + _grant_policy_expressions()["memplex_source_heads"] + " OR true)")
            else:
                cur.execute(sql[drift])
        if drift == "projection":
            # Catalogue-only readiness does not claim to rescan private bodies.
            with (bank.backend.read_fence(bank.context, deadline_ns=time.monotonic_ns() + 250_000_000) as session,
                  pytest.raises(SourceContractError, match="malformed")):
                session.source(bank.source.ref.key)
            assert service.get(bank.derived.publication_id, authorization=bank.bob) is None
            assert service.get("U", authorization=bank.context).object_ == "DRIFT_INDEPENDENT_U"
        else:
            with pytest.raises(runner.MigrationIntegrityError):
                runner.PostgresMigrationRunner(bank.owner_dsn).plan(
                    application_acl=runner.ApplicationAclContract(bank.role), deployment_profile="development")
        request.node.user_properties.append(("drift_stage", drift + "_rejected"))


@NATIVE
@pytest.mark.parametrize("publication_backend", ["lite"], indirect=True)
def test_native_lost_commit_ack_is_unknown_until_exact_catalogue_readback(occupied_v9):
    import psycopg2
    from psycopg2.extensions import connection as PgConnection

    from memplex.storage.migrations import runner

    bank = occupied_v9
    class LostAckConnection(PgConnection):
        def commit(self):
            super().commit()
            raise RuntimeError("original commit acknowledgement unknown")
    migration = runner.PostgresMigrationRunner(bank.owner_dsn,
        connection_factory=lambda: psycopg2.connect(bank.owner_dsn, connection_factory=LostAckConnection))
    with pytest.raises(RuntimeError, match="original commit acknowledgement unknown"):
        migration.apply()
    # The caller received no ready result. Resolve only through a fresh normal
    # exact target/ledger/catalogue read, never an owner-private body scan.
    readback = runner.PostgresMigrationRunner(bank.owner_dsn)
    assert readback.inspect_target() == bank.target
    assert readback.plan().state == "ready" and readback.plan().current_version == 10


def test_model_namespace_cannot_issue_read_grants():
    from memplex.llm.factual_capture import validate_payload

    payload = {"facts": [{"subject": "A", "predicate": "is", "object": "B", "evidence": [],
                           "namespace": {"memplex_grants": "reader-agent"}}]}
    with pytest.raises(ValueError, match="unknown candidate fields"):
        validate_payload(payload, (), reference_datetime=None, max_facts=1)


@pytest.mark.parametrize("table", ["memplex_source_heads", "memplex_derivation_records", "memplex_paragraphs",
                                   "memplex_functions", "memplex_facts", "memplex_preferences", "memplex_observations"])
def test_restore_accepts_only_exact_new_read_policy_names(table):
    from memplex.storage.postgres_backup import _approved_restore_toc

    base = "1; 0 17 TABLE DATA isolated memplex_facts owner\n"
    policy = f"2; 3256 18 POLICY isolated {table} {table}_source_read_grants owner"
    data, access = _approved_restore_toc(base + policy, "isolated")
    assert policy in data and access == ""
    with pytest.raises(ValueError, match="unknown attached object"):
        _approved_restore_toc(base + policy.replace(f"isolated {table} ", "isolated memplex_edges "), "isolated")
    with pytest.raises(ValueError, match="unknown attached object"):
        _approved_restore_toc(base + policy.replace("_source_read_grants", "_unknown_read_grants"), "isolated")
