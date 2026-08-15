"""Integration tests for the PostgreSQL backends against a REAL PostgreSQL.

These tests start an actual PostgreSQL 16 instance via ``pgserver`` (unix
socket, no system install required) and exercise the full
``PostgresMemoryStore`` / ``PostgresFeedbackStore`` contract: JSONB
round-trips, recursive-CTE neighbour traversal, tsvector full-text search,
pgvector RRF hybrid search, changelog/timeline, and the
facts/preferences/observations tables.

The whole module is skipped when ``pgserver`` or ``psycopg2`` is not
importable (e.g. the main cp313 test environment), so the primary suite
stays green without a database.

Run manually with the project-pinned environment::

    PYTHONPATH=<repo root> .venv-pgcheck/bin/python -m pytest \
        tests/test_postgres_integration.py
"""

import base64
import concurrent.futures
import hashlib
import json
import os
import threading
import uuid

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier, BrokenBarrierError

import pytest

psycopg2 = pytest.importorskip("psycopg2", reason="psycopg2 not installed (use .venv-pgcheck)")
from psycopg2 import sql as pg_sql

from memplex.adapters.agent_runtime import AgentMemoryRuntime
from memplex.adapters.mcp_server import MCPServer
from memplex.config import MemplexConfig
from memplex.models import (
    Fact,
    FeedbackVerdict,
    FieldValue,
    Function,
    GraphData,
    GraphEdge,
    MemoryFeedback,
    Observation,
    Preference,
    SearchFilters,
    SourceDocument,
    SourceType,
    domain_node_id,
)
from memplex.service import MemplexService
from memplex.storage.feedback import PostgresFeedbackStore
from memplex.storage.migrations import (
    ApplicationAclContract,
    IngressAclContract,
    MigrationIntegrityError,
    PostgresMigrationRunner,
    discover_migrations,
)
from memplex.storage.migrations.runner import (
    VectorCapabilityRequest,
    _body_for_execution,
)
from memplex.storage.pool import PostgresStorageResources, PostgresSyncStorageResources
from memplex.storage.postgres import (
    FunctionWriteBusy,
    PostgresMemoryStore,
    _function_write_lock_key,
)
from memplex.sync import SyncableStore
from memplex.sync_dispatcher import SyncDispatcher
from memplex.sync_ingress import validate_ingress_batch
from memplex.sync_protocol import (
    SyncBatch,
    SyncCursorClaims,
    SyncEntityKey,
    SyncEvent,
    SyncNodeType,
    SyncOperation,
    SyncPage,
    SyncReceipt,
    SyncScope,
    SyncStreamItem,
    SyncVersion,
    _canonical_json_bytes,
)
from memplex.sync_repository import (
    SyncBackpressureError,
    SyncBatchRejected,
    SyncCapturePolicy,
    SyncCursorExpired,
)


def _ready_resources(dsn: str, dim: int = 0) -> PostgresStorageResources:
    resources = PostgresStorageResources(dsn=dsn)
    resources.ensure_ready(
        VectorCapabilityRequest(
            dim=dim,
            policy="disabled" if dim == 0 else "best_effort",
        ),
        "development",
    )
    return resources


def _admin_query(dsn: str, statement: str, params=()):
    """Run a test-only catalogue assertion on a distinct admin connection."""
    conn = psycopg2.connect(dsn)
    try:
        cur = conn.cursor()
        try:
            cur.execute(statement, params)
            return cur.fetchall()
        finally:
            cur.close()
    finally:
        conn.close()


def _admin_execute(dsn: str, statement: str, params=()) -> None:
    conn = psycopg2.connect(dsn)
    try:
        cur = conn.cursor()
        try:
            cur.execute(statement, params)
            conn.commit()
        finally:
            cur.close()
    finally:
        conn.close()


def _required_capture_store(dsn: str, *, local_node_id: str = "typed-local-node"):
    """Build an application store that writes durable local sync outbox rows."""
    PostgresMigrationRunner(dsn).apply()
    _admin_execute(dsn, "SELECT memplex_configure_sync_local_identity(%s)", (local_node_id,))
    resources = PostgresStorageResources(dsn)
    resources.ensure_ready(VectorCapabilityRequest(dim=0, policy="disabled"), "development")
    store = PostgresMemoryStore(
        dsn=dsn,
        ready_pool=resources.ready_pool,
        sync_capture_policy=SyncCapturePolicy(
            "required",
            local_node_id=local_node_id,
        ),
    )
    return resources, store


def _build_typed_node(method: str, node_id: str):
    if method in {"add_preference", "delete_preference"}:
        return Preference(
            id=node_id,
            name="theme",
            aspect="ui",
            preference="dark",
        )
    if method in {"add_observation", "delete_observation"}:
        return Observation(
            id=node_id,
            name="obs",
            domain="ops",
            event="deploy",
            actor="ci",
            context="g4-pg",
        )
    return Fact(id=node_id, name="temperature", subject="sensor", predicate="is", object_="20")


def _application_feedback_store(pg_dsn: str, role: str):
    """Create a least-privileged feedback store through its explicit pool."""
    application_dsn = psycopg2.extensions.make_dsn(pg_dsn, user=role)
    resources = PostgresStorageResources(
        dsn=application_dsn, migration_dsn=pg_dsn
    )
    resources.ensure_ready(
        VectorCapabilityRequest(dim=0, policy="disabled"),
        "development",
    )
    return (
        PostgresFeedbackStore(
            dsn=application_dsn,
            require_authorization=True,
            ready_pool=resources.ready_pool,
        ),
        resources,
    )


def _provision_application_role(pg_dsn: str, role: str) -> None:
    """Provision the complete shared-pool application ACL contract."""
    conn = psycopg2.connect(pg_dsn)
    try:
        cur = conn.cursor()
        try:
            cur.execute("SELECT current_schema()")
            schema = cur.fetchone()[0]
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,))
            if cur.fetchone() is None:
                cur.execute(pg_sql.SQL("CREATE ROLE {} LOGIN").format(pg_sql.Identifier(role)))
            # Production exact ACL deliberately has no schema-level PUBLIC
            # escape hatch; retain only the database-owner implicit entry.
            cur.execute(
                pg_sql.SQL("REVOKE USAGE, CREATE ON SCHEMA {} FROM PUBLIC").format(
                    pg_sql.Identifier(schema)
                )
            )
            cur.execute(
                pg_sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                    pg_sql.Identifier(schema), pg_sql.Identifier(role)
                )
            )
            for table in (
                "memplex_functions",
                "memplex_edges",
                "memplex_observations",
                "memplex_facts",
                "memplex_preferences",
            ):
                cur.execute(
                    pg_sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO {}").format(
                        pg_sql.Identifier(table), pg_sql.Identifier(role)
                    )
                )
            cur.execute(
                pg_sql.SQL("GRANT SELECT, INSERT, DELETE ON memplex_changelog TO {}").format(
                    pg_sql.Identifier(role)
                )
            )
            cur.execute(
                pg_sql.SQL("GRANT USAGE ON SEQUENCE memplex_changelog_id_seq TO {}").format(
                    pg_sql.Identifier(role)
                )
            )
            cur.execute(
                pg_sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON feedback TO {}").format(
                    pg_sql.Identifier(role)
                )
            )
            sync_acl = {
                "memplex_sync_outbox": "SELECT, INSERT, DELETE",
                "memplex_sync_entity_versions": "SELECT, INSERT, UPDATE",
                "memplex_sync_inbox": "SELECT, INSERT",
                "memplex_sync_batches": "SELECT, INSERT",
                "memplex_sync_targets": "SELECT, INSERT, UPDATE",
                "memplex_sync_deliveries": "SELECT, INSERT, UPDATE, DELETE",
                "memplex_sync_cursors": "SELECT, INSERT, UPDATE, DELETE",
                "memplex_sync_stream_state": "SELECT, INSERT, UPDATE",
                "memplex_sync_snapshots": "SELECT, INSERT, UPDATE, DELETE",
                "memplex_sync_snapshot_items": "SELECT, INSERT, DELETE",
                "memplex_background_tasks": "SELECT, INSERT, UPDATE, DELETE",
            }
            for table, privileges in sync_acl.items():
                cur.execute(
                    pg_sql.SQL("GRANT {} ON {} TO {}").format(
                        pg_sql.SQL(privileges), pg_sql.Identifier(table), pg_sql.Identifier(role)
                    )
                )
            cur.execute(
                pg_sql.SQL("GRANT USAGE ON SEQUENCE memplex_sync_outbox_stream_seq_seq TO {}").format(
                    pg_sql.Identifier(role)
                )
            )
            for function, arguments in (
                ("memplex_sync_capture_before", ""),
                ("memplex_sync_capture_local_change", ""),
                ("memplex_sync_assert_delivery_quota", "TEXT, BIGINT"),
                ("memplex_sync_snapshot_admission_counts", ""),
                ("memplex_sync_compact", "TIMESTAMPTZ, TIMESTAMPTZ, INTEGER"),
            ):
                cur.execute(
                    pg_sql.SQL("GRANT EXECUTE ON FUNCTION {}({}) TO {}").format(
                        pg_sql.Identifier(function), pg_sql.SQL(arguments), pg_sql.Identifier(role)
                    )
                )
            conn.commit()
        finally:
            cur.close()
    finally:
        conn.close()


def _create_ungranted_application_role(pg_dsn: str, role: str) -> None:
    """Create an application login without the readiness ACL contract."""
    conn = psycopg2.connect(pg_dsn)
    try:
        cur = conn.cursor()
        try:
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,))
            if cur.fetchone() is None:
                cur.execute(pg_sql.SQL("CREATE ROLE {} LOGIN").format(pg_sql.Identifier(role)))
            conn.commit()
        finally:
            cur.close()
    finally:
        conn.close()


def _provision_ingress_role(pg_dsn: str, role: str, remote_node_id: str) -> None:
    """Provision only the documented inbound-function LOGIN authority."""
    conn = psycopg2.connect(pg_dsn)
    try:
        cur = conn.cursor()
        try:
            cur.execute("SELECT current_schema()")
            schema = cur.fetchone()[0]
            cur.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,))
            if cur.fetchone() is None:
                cur.execute(pg_sql.SQL("CREATE ROLE {} LOGIN").format(pg_sql.Identifier(role)))
            cur.execute(
                pg_sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                    pg_sql.Identifier(schema), pg_sql.Identifier(role)
                )
            )
            cur.execute(
                pg_sql.SQL(
                    "GRANT EXECUTE ON FUNCTION memplex_sync_apply_inbound(bytea,text) TO {}"
                ).format(pg_sql.Identifier(role))
            )
            cur.execute(
                "SELECT memplex_configure_sync_ingress_principal(%s::name, %s)",
                (role, remote_node_id),
            )
            conn.commit()
        finally:
            cur.close()
    finally:
        conn.close()


def _production_service_config(migration_dsn: str) -> tuple[MemplexConfig, str]:
    """Build a production config with separate exact app/admin identities."""
    role = f"memplex_service_app_{uuid.uuid4().hex[:8]}"
    _ready_resources(migration_dsn).close()
    _provision_application_role(migration_dsn, role)
    config = MemplexConfig()
    config.deployment.profile = "production"
    config.storage.backend = "postgres"
    config.storage.path = psycopg2.extensions.make_dsn(migration_dsn, user=role)
    config.storage.migration_dsn = migration_dsn
    config.llm.query_enhancement = False
    return config, role


def _grant_vector_type_usage(migration_dsn: str, role: str) -> None:
    """Grant only the ready vector extension type required by a vector pool."""
    conn = psycopg2.connect(migration_dsn)
    try:
        cur = conn.cursor()
        try:
            cur.execute(
                """
                SELECT namespace.nspname
                FROM pg_catalog.pg_extension extension
                JOIN pg_catalog.pg_namespace namespace ON namespace.oid=extension.extnamespace
                WHERE extension.extname='vector'
                """
            )
            row = cur.fetchone()
            if row is None:
                raise AssertionError("vector extension missing from vector fixture")
            schema = str(row[0])
            cur.execute(
                pg_sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                    pg_sql.Identifier(schema), pg_sql.Identifier(role)
                )
            )
            cur.execute(
                pg_sql.SQL("GRANT USAGE ON TYPE {}.vector TO {}").format(
                    pg_sql.Identifier(schema), pg_sql.Identifier(role)
                )
            )
            conn.commit()
        finally:
            cur.close()
    finally:
        conn.close()


def _drop_unprivileged_role(pg_dsn: str, role: str) -> None:
    """Drop a target-inspection role and its direct database grants."""
    conn = psycopg2.connect(pg_dsn)
    try:
        cur = conn.cursor()
        try:
            cur.execute(pg_sql.SQL("REASSIGN OWNED BY {} TO CURRENT_USER").format(pg_sql.Identifier(role)))
            cur.execute(pg_sql.SQL("DROP OWNED BY {}").format(pg_sql.Identifier(role)))
            cur.execute(pg_sql.SQL("DROP ROLE IF EXISTS {}").format(pg_sql.Identifier(role)))
            conn.commit()
        finally:
            cur.close()
    finally:
        conn.close()


def _drop_feedback_role(pg_dsn: str, role: str) -> None:
    conn = psycopg2.connect(pg_dsn)
    try:
        cur = conn.cursor()
        try:
            # DROP OWNED revokes the complete shared-pool contract, including
            # core tables and the changelog sequence, without materializing
            # an empty owner ACL that would violate the generic fingerprint.
            cur.execute(pg_sql.SQL("REASSIGN OWNED BY {} TO CURRENT_USER").format(pg_sql.Identifier(role)))
            cur.execute(pg_sql.SQL("DROP OWNED BY {}").format(pg_sql.Identifier(role)))
            cur.execute(pg_sql.SQL("DROP ROLE IF EXISTS {}").format(pg_sql.Identifier(role)))
            conn.commit()
        finally:
            cur.close()
    finally:
        conn.close()


def _install_legacy_feedback_fixture(dsn: str, memory_id: str) -> None:
    """Exact runtime-v1 feedback catalogue from the pre-Task3 backend."""
    _migration_execute(dsn, f"""
        CREATE TABLE feedback (
            memory_id TEXT NOT NULL, field_role TEXT NOT NULL, value_index INTEGER DEFAULT 0,
            verdict TEXT NOT NULL, reason TEXT, source TEXT DEFAULT 'user', timestamp TIMESTAMPTZ,
            owner TEXT, feedback_type TEXT DEFAULT 'field_value', old_value TEXT, new_value TEXT,
            needs_review BOOLEAN DEFAULT TRUE, needs_review_until TIMESTAMPTZ,
            resolved_at TIMESTAMPTZ, resolution TEXT, tenant_id TEXT NOT NULL,
            owner_subject_id TEXT NOT NULL, workspace_id TEXT NOT NULL,
            visibility TEXT NOT NULL DEFAULT 'workspace', provenance JSONB NOT NULL DEFAULT '{{}}'::jsonb
        );
        CREATE INDEX feedback_tenant_memory_idx ON feedback(tenant_id, memory_id, timestamp DESC);
        ALTER TABLE feedback ENABLE ROW LEVEL SECURITY;
        ALTER TABLE feedback FORCE ROW LEVEL SECURITY;
        CREATE POLICY feedback_tenant_scope ON feedback
        USING (tenant_id = current_setting('memplex.tenant_id', true) AND
          ((visibility = 'user' AND owner_subject_id = current_setting('memplex.subject_id', true)) OR
           (visibility = 'workspace' AND workspace_id = current_setting('memplex.workspace_id', true)) OR
           (visibility = 'session' AND workspace_id = current_setting('memplex.workspace_id', true)
             AND owner_subject_id = current_setting('memplex.subject_id', true)
             AND NULLIF(current_setting('memplex.agent_id', true), '') IS NOT NULL
             AND NULLIF(current_setting('memplex.session_id', true), '') IS NOT NULL
             AND NULLIF(provenance->>'agent_id', '') IS NOT NULL
             AND NULLIF(provenance->>'session_id', '') IS NOT NULL
             AND provenance->>'agent_id' = current_setting('memplex.agent_id', true)
             AND provenance->>'session_id' = current_setting('memplex.session_id', true))))
        WITH CHECK (tenant_id = current_setting('memplex.tenant_id', true)
          AND owner_subject_id = current_setting('memplex.subject_id', true)
          AND workspace_id = current_setting('memplex.workspace_id', true)
          AND visibility IN ('user', 'workspace', 'session')
          AND (visibility <> 'session' OR (NULLIF(current_setting('memplex.agent_id', true), '') IS NOT NULL
             AND NULLIF(current_setting('memplex.session_id', true), '') IS NOT NULL
             AND provenance->>'agent_id' = current_setting('memplex.agent_id', true)
             AND provenance->>'session_id' = current_setting('memplex.session_id', true))));
        INSERT INTO feedback (memory_id, field_role, verdict, tenant_id, owner_subject_id, workspace_id)
        VALUES ('{memory_id}', 'name', 'correct', 'vector-tenant', 'subject', 'workspace');
    """)


def _install_runtime_v1_core_fixture(dsn: str, dim: int) -> None:
    """Exact statement extraction of the pre-Task3 ``_ensure_schema`` DDL.

    This deliberately preserves the old statement boundaries, identifiers,
    defaults, generated expression and policy replacement order.  It is a
    migration adoption fixture, not a second schema implementation.
    """
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    try:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS memplex_functions (
                tenant_id       TEXT NOT NULL,
                id              TEXT NOT NULL,
                data            JSONB NOT NULL,
                updated_at      TIMESTAMPTZ,
                owner_subject   TEXT NOT NULL,
                workspace       TEXT NOT NULL,
                visibility      TEXT NOT NULL DEFAULT 'workspace',
                source_agent    TEXT NOT NULL DEFAULT '',
                source_session  TEXT NOT NULL DEFAULT '',
                search_tsv  TSVECTOR GENERATED ALWAYS AS (
                    to_tsvector('simple',
                        coalesce(data->>'name','') || ' ' ||
                        coalesce(data->>'domain','') || ' ' ||
                        coalesce(data->>'trigger_text','') || ' ' ||
                        coalesce(data->>'action_text','')
                    )
                ) STORED,
                PRIMARY KEY (tenant_id, id)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS fts_functions_idx "
            "ON memplex_functions USING GIN (search_tsv)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS memplex_functions_tenant_updated_idx "
            "ON memplex_functions (tenant_id, updated_at DESC)"
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS memplex_edges (
                tenant_id       TEXT NOT NULL,
                source          TEXT NOT NULL,
                target          TEXT NOT NULL,
                edge_type       TEXT NOT NULL,
                weight          REAL,
                evidence        JSONB,
                created_at      TIMESTAMPTZ,
                owner_subject   TEXT NOT NULL,
                workspace       TEXT NOT NULL,
                visibility      TEXT NOT NULL DEFAULT 'workspace',
                source_agent    TEXT NOT NULL DEFAULT '',
                source_session  TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (tenant_id, source, target, edge_type)
            )
            """
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS memplex_edges_tenant_source_type_target_idx "
            "ON memplex_edges (tenant_id, source, edge_type, target)"
        )
        cur.execute(
            "CREATE INDEX IF NOT EXISTS memplex_edges_tenant_target_type_source_idx "
            "ON memplex_edges (tenant_id, target, edge_type, source)"
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS memplex_observations (
                tenant_id       TEXT NOT NULL,
                id              TEXT NOT NULL,
                data            JSONB NOT NULL,
                created_at      TIMESTAMPTZ,
                owner_subject   TEXT NOT NULL,
                workspace       TEXT NOT NULL,
                visibility      TEXT NOT NULL DEFAULT 'workspace',
                source_agent    TEXT NOT NULL DEFAULT '',
                source_session  TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (tenant_id, id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS memplex_facts (
                tenant_id       TEXT NOT NULL,
                id              TEXT NOT NULL,
                data            JSONB NOT NULL,
                updated_at      TIMESTAMPTZ,
                owner_subject   TEXT NOT NULL,
                workspace       TEXT NOT NULL,
                visibility      TEXT NOT NULL DEFAULT 'workspace',
                source_agent    TEXT NOT NULL DEFAULT '',
                source_session  TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (tenant_id, id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS memplex_preferences (
                tenant_id       TEXT NOT NULL,
                id              TEXT NOT NULL,
                data            JSONB NOT NULL,
                updated_at      TIMESTAMPTZ,
                owner_subject   TEXT NOT NULL,
                workspace       TEXT NOT NULL,
                visibility      TEXT NOT NULL DEFAULT 'workspace',
                source_agent    TEXT NOT NULL DEFAULT '',
                source_session  TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (tenant_id, id)
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS memplex_changelog (
                tenant_id       TEXT NOT NULL,
                id              BIGSERIAL NOT NULL,
                func_id         TEXT,
                ts              TIMESTAMPTZ,
                event_type      TEXT,
                description     TEXT,
                source          TEXT,
                actor           TEXT,
                owner_subject   TEXT NOT NULL,
                workspace       TEXT NOT NULL,
                visibility      TEXT NOT NULL DEFAULT 'workspace',
                source_agent    TEXT NOT NULL DEFAULT '',
                source_session  TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (tenant_id, id)
            )
            """
        )
        for table in (
            "memplex_functions",
            "memplex_edges",
            "memplex_observations",
            "memplex_facts",
            "memplex_preferences",
            "memplex_changelog",
        ):
            cur.execute(
                f"CREATE INDEX IF NOT EXISTS {table}_tenant_idx "
                f"ON {table} (tenant_id)"
            )
            cur.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
            cur.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
            cur.execute(f"DROP POLICY IF EXISTS {table}_scope ON {table}")
            scope = (
                "tenant_id <> '__memplex_legacy__' "
                "AND tenant_id = current_setting('memplex.tenant_id', true) "
                "AND ((visibility = 'user' "
                "AND owner_subject = current_setting('memplex.subject_id', true)) "
                "OR (visibility = 'workspace' "
                "AND workspace = current_setting('memplex.workspace_id', true)) "
                "OR (visibility = 'session' "
                "AND workspace = current_setting('memplex.workspace_id', true) "
                "AND owner_subject = current_setting('memplex.subject_id', true) "
                "AND NULLIF(current_setting('memplex.agent_id', true), '') IS NOT NULL "
                "AND NULLIF(current_setting('memplex.session_id', true), '') IS NOT NULL "
                "AND NULLIF(source_agent, '') IS NOT NULL "
                "AND NULLIF(source_session, '') IS NOT NULL "
                "AND source_agent = current_setting('memplex.agent_id', true) "
                "AND source_session = current_setting('memplex.session_id', true)))"
            )
            cur.execute(
                f"CREATE POLICY {table}_scope ON {table} "
                f"USING ({scope}) "
                f"WITH CHECK ({scope} "
                "AND owner_subject = current_setting('memplex.subject_id', true) "
                "AND workspace = current_setting('memplex.workspace_id', true) "
                "AND source_agent = current_setting('memplex.agent_id', true) "
                "AND source_session = current_setting('memplex.session_id', true))"
            )
        if dim > 0:
            try:
                cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                cur.execute(
                    "ALTER TABLE memplex_functions "
                    f"ADD COLUMN IF NOT EXISTS embedding vector({dim})"
                )
            except Exception:
                pass
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def pg_dsn(pg_function_dsn):
    """Backward-compatible alias for the function-scoped real PG schema."""
    return pg_function_dsn


@pytest.fixture
def store(pg_dsn):
    """Function-scoped PostgresMemoryStore on a clean slate."""
    resources = PostgresStorageResources(dsn=pg_dsn)
    resources.ensure_ready(VectorCapabilityRequest(dim=0, policy="disabled"), "development")
    s = PostgresMemoryStore(dsn=pg_dsn, ready_pool=resources.ready_pool)
    s.clear()
    yield s
    resources.close()


@pytest.fixture
def feedback_store(pg_dsn):
    resources = PostgresStorageResources(dsn=pg_dsn)
    resources.ensure_ready(VectorCapabilityRequest(dim=0, policy="disabled"), "development")
    fs = PostgresFeedbackStore(dsn=pg_dsn, ready_pool=resources.ready_pool)
    fs.clear()
    yield fs
    resources.close()


def test_function_scoped_schema_is_empty_for_each_real_pg_case(pg_function_dsn):
    """A real migration case begins in its own empty, non-public schema."""
    conn = psycopg2.connect(pg_function_dsn)
    try:
        cur = conn.cursor()
        try:
            cur.execute("SELECT current_schema()")
            schema = cur.fetchone()[0]
            cur.execute(
                "SELECT count(*) FROM information_schema.tables "
                "WHERE table_schema = current_schema()"
            )
            scoped_table_count = cur.fetchone()[0]
            cur.execute(
                "SELECT count(*) FROM pg_catalog.pg_tables "
                "WHERE schemaname = 'public' AND tablename LIKE 'memplex_%'"
            )
            public_memplex_table_count = cur.fetchone()[0]
        finally:
            cur.close()
    finally:
        conn.close()

    assert schema.startswith("g003_function_")
    assert scoped_table_count == 0
    assert public_memplex_table_count == 0

    result = PostgresMigrationRunner(pg_function_dsn).apply()
    assert result.state == "ready"
    expected_versions = [migration.version for migration in discover_migrations()]

    conn = psycopg2.connect(pg_function_dsn)
    try:
        cur = conn.cursor()
        try:
            cur.execute("SELECT version FROM memplex_schema_migrations ORDER BY version")
            actual_versions = [row[0] for row in cur.fetchall()]
            cur.execute(
                "SELECT count(*) FROM pg_catalog.pg_tables "
                "WHERE schemaname = 'public' AND tablename LIKE 'memplex_%'"
            )
            public_memplex_table_count = cur.fetchone()[0]
        finally:
            cur.close()
    finally:
        conn.close()

    assert actual_versions == expected_versions
    assert public_memplex_table_count == 0


def test_pool_max_connections_and_rls_parallelism(store):
    """Sixteen scoped tenants converge through one bounded real PG pool.

    The two barriers ensure cross-tenant reads occur only after every write
    has committed, so ``None`` proves the RLS visibility boundary rather than
    merely a read racing an uncommitted insert.
    """
    workers = 16
    start = Barrier(workers, timeout=20)
    writes_complete = Barrier(workers, timeout=20)

    def write_then_cross_read(index: int) -> tuple[str | None, object | None]:
        context = _authorization(
            tenant=f"task7-parallel-{index}",
            subject=f"subject-{index}",
            workspace=f"workspace-{index}",
            agent=f"agent-{index}",
            session=f"session-{index}",
        )
        scoped = store.authorized(context)
        function_id = f"task7-parallel-function-{index}"
        start.wait()
        scoped.add(_func(function_id, "Shared parallel name"), SRC)
        writes_complete.wait()
        own = scoped.get(function_id)
        other = scoped.get(f"task7-parallel-function-{(index + 1) % workers}")
        return (None if own is None else own.id, other)

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(write_then_cross_read, range(workers)))

    assert [own_id for own_id, _other in results] == [
        f"task7-parallel-function-{index}" for index in range(workers)
    ]
    assert all(other is None for _own_id, other in results)
    manager = store._pool_manager
    assert manager.business_lease_count == 0
    assert 1 <= manager.business_lease_high_watermark <= manager.max_connections


def test_resources_publish_a_seal_for_the_verified_application_target(pg_dsn):
    """The seal carries the exact target inspected before migration/pool use."""
    resources = _ready_resources(pg_dsn)
    try:
        assert resources.ready_pool.target == PostgresMigrationRunner(pg_dsn).inspect_target()
        assert resources.business_lease_count == 0
    finally:
        resources.close()


def test_resources_allow_least_privileged_application_role_in_production(migration_dsn):
    """An exactly granted application role can seal against the admin migrator."""
    role = f"memplex_target_role_{uuid.uuid4().hex[:8]}"
    _ready_resources(migration_dsn).close()
    _provision_application_role(migration_dsn, role)
    application_dsn = psycopg2.extensions.make_dsn(migration_dsn, user=role)
    resources = PostgresStorageResources(
        dsn=application_dsn, migration_dsn=migration_dsn
    )
    try:
        resources.ensure_ready(
            VectorCapabilityRequest(dim=0, policy="disabled"),
            "production",
        )
        assert resources.ready_pool.target == PostgresMigrationRunner(
            application_dsn
        ).inspect_target()
        assert resources.business_lease_count == 0
    finally:
        if resources.state == "READY":
            resources.close()
        _drop_unprivileged_role(migration_dsn, role)


def test_production_resources_reject_same_principal_hidden_by_distinct_dsns(
    migration_dsn,
):
    """Different connection options cannot disguise one shared database role."""
    application_dsn = psycopg2.extensions.make_dsn(
        migration_dsn, application_name="memplex-application"
    )
    admin_dsn = psycopg2.extensions.make_dsn(
        migration_dsn, application_name="memplex-migration"
    )
    resources = PostgresStorageResources(
        dsn=application_dsn,
        migration_dsn=admin_dsn,
    )

    with pytest.raises(MigrationIntegrityError, match="principals must be distinct"):
        resources.ensure_ready(
            VectorCapabilityRequest(dim=0, policy="disabled"),
            "production",
        )

    assert resources.state == "FAULTED"
    assert resources.business_lease_count == 0
    assert not _migration_table_exists(migration_dsn, "memplex_schema_migrations")


def test_v5_application_acl_rejects_extra_inbox_write_privilege(migration_dsn):
    """The v5 application matrix is exact; an unnecessary inbox UPDATE is unsafe."""
    role = f"memplex_sync_acl_{uuid.uuid4().hex[:8]}"
    _ready_resources(migration_dsn).close()
    _provision_application_role(migration_dsn, role)
    conn = psycopg2.connect(migration_dsn)
    try:
        cursor = conn.cursor()
        cursor.execute(
            pg_sql.SQL("GRANT UPDATE ON memplex_sync_inbox TO {}").format(pg_sql.Identifier(role))
        )
        conn.commit()
        cursor.close()
        with pytest.raises(MigrationIntegrityError, match="table ACL"):
            PostgresMigrationRunner(migration_dsn).plan(
                application_acl=ApplicationAclContract(role), deployment_profile="production"
            )
    finally:
        conn.close()
        _drop_unprivileged_role(migration_dsn, role)


def test_v5_application_acl_rejects_local_identity_access(migration_dsn):
    """The operator-only identity table and configurator are never application APIs."""
    role = f"memplex_sync_identity_acl_{uuid.uuid4().hex[:8]}"
    _ready_resources(migration_dsn).close()
    _provision_application_role(migration_dsn, role)
    conn = psycopg2.connect(migration_dsn)
    try:
        cursor = conn.cursor()
        cursor.execute(
            pg_sql.SQL("GRANT SELECT ON memplex_sync_local_identity TO {}").format(
                pg_sql.Identifier(role)
            )
        )
        conn.commit()
        cursor.close()
        with pytest.raises(MigrationIntegrityError, match="table ACL"):
            PostgresMigrationRunner(migration_dsn).plan(
                application_acl=ApplicationAclContract(role), deployment_profile="production"
            )
    finally:
        conn.close()
        _drop_unprivileged_role(migration_dsn, role)


def test_v5_candidate_pool_probe_rolls_back_durable_sync_rows(migration_dsn):
    """Candidate-pool sync DML is a rollback-only readiness proof, never seed data."""
    role = f"memplex_sync_probe_{uuid.uuid4().hex[:8]}"
    _ready_resources(migration_dsn).close()
    _provision_application_role(migration_dsn, role)
    resources = PostgresStorageResources(
        dsn=psycopg2.extensions.make_dsn(migration_dsn, user=role), migration_dsn=migration_dsn
    )
    try:
        resources.ensure_ready(VectorCapabilityRequest(dim=0, policy="disabled"), "production")
        assert _admin_query(migration_dsn, "SELECT count(*) FROM memplex_sync_outbox") == [(0,)]
        assert _admin_query(migration_dsn, "SELECT count(*) FROM memplex_sync_inbox") == [(0,)]
        assert _admin_query(migration_dsn, "SELECT count(*) FROM memplex_sync_snapshots") == [(0,)]
    finally:
        if resources.state == "READY":
            resources.close()
        _drop_unprivileged_role(migration_dsn, role)


def test_resources_reject_ungranted_application_role_in_production(migration_dsn):
    """A migration-capable admin DSN cannot hide a missing application ACL."""
    role = f"memplex_no_grant_role_{uuid.uuid4().hex[:8]}"
    _ready_resources(migration_dsn).close()
    _create_ungranted_application_role(migration_dsn, role)
    application_dsn = psycopg2.extensions.make_dsn(migration_dsn, user=role)
    resources = PostgresStorageResources(
        dsn=application_dsn, migration_dsn=migration_dsn
    )
    try:
        with pytest.raises(MigrationIntegrityError):
            resources.ensure_ready(
                VectorCapabilityRequest(dim=0, policy="disabled"),
                "production",
            )
        assert resources.state == "FAULTED"
        assert resources.pool_created is False
        assert resources.business_lease_count == 0
        with pytest.raises(RuntimeError, match="not ready"):
            _ = resources.ready_pool
    finally:
        _drop_unprivileged_role(migration_dsn, role)


def test_admin_plan_status_and_dry_run_accept_only_exact_application_acl_contract(migration_dsn):
    """Operator planning remains strict unless it names the verified app role."""
    role = f"memplex_plan_role_{uuid.uuid4().hex[:8]}"
    _ready_resources(migration_dsn).close()
    _provision_application_role(migration_dsn, role)
    runner = PostgresMigrationRunner(migration_dsn)
    contract = ApplicationAclContract(role)
    try:
        with pytest.raises(MigrationIntegrityError, match="unrecognised legacy schema"):
            runner.plan()
        plan = runner.plan(application_acl=contract, deployment_profile="production")
        assert plan.state == "ready"
        assert runner.status(
            application_acl=contract, deployment_profile="production"
        ) == plan
        assert runner.apply(
            dry_run=True,
            application_acl=contract,
            deployment_profile="production",
        ) == plan
    finally:
        _drop_unprivileged_role(migration_dsn, role)


def test_admin_acl_contract_accepts_only_the_exact_joint_app_and_ingress_principals(
    migration_dsn,
):
    """A deployed app+ingress split is ready through every runner readiness API."""
    app_role = f"memplex_joint_app_{uuid.uuid4().hex[:8]}"
    ingress_role = f"memplex_joint_ingress_{uuid.uuid4().hex[:8]}"
    _ready_resources(migration_dsn).close()
    _provision_application_role(migration_dsn, app_role)
    _provision_ingress_role(migration_dsn, ingress_role, "remote-joint")
    runner = PostgresMigrationRunner(migration_dsn)
    app = ApplicationAclContract(app_role)
    ingress = IngressAclContract(ingress_role)
    try:
        plan = runner.plan(
            application_acl=app,
            ingress_acl=ingress,
            deployment_profile="production",
        )
        assert plan.state == "ready"
        assert runner.status(
            application_acl=app,
            ingress_acl=ingress,
            deployment_profile="production",
        ) == plan
        assert runner.apply(
            dry_run=True,
            application_acl=app,
            ingress_acl=ingress,
            deployment_profile="production",
        ) == plan
        assert runner.verify_storage_readiness(
            VectorCapabilityRequest(dim=0, policy="disabled"),
            "production",
            expected_target=runner.inspect_target(),
            application_acl=app,
            ingress_acl=ingress,
        ).state == "disabled"
        assert runner.ensure_vector_capability(
            VectorCapabilityRequest(dim=0, policy="disabled"),
            "production",
            expected_target=runner.inspect_target(),
            application_acl=app,
            ingress_acl=ingress,
        ).state == "disabled"
    finally:
        _drop_unprivileged_role(migration_dsn, ingress_role)
        _drop_unprivileged_role(migration_dsn, app_role)


def test_ingress_acl_contract_rejects_an_extra_inbound_function_grantee(migration_dsn):
    """Ingress-only readiness enumerates function ACL grantees, not just itself."""
    ingress_role = f"memplex_ingress_exact_{uuid.uuid4().hex[:8]}"
    extra_role = f"memplex_ingress_extra_{uuid.uuid4().hex[:8]}"
    _ready_resources(migration_dsn).close()
    _provision_ingress_role(migration_dsn, ingress_role, "remote-exact")
    _create_ungranted_application_role(migration_dsn, extra_role)
    conn = psycopg2.connect(migration_dsn)
    try:
        cur = conn.cursor()
        cur.execute(
            pg_sql.SQL(
                "GRANT EXECUTE ON FUNCTION memplex_sync_apply_inbound(bytea,text) TO {}"
            ).format(pg_sql.Identifier(extra_role))
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()
    try:
        with pytest.raises(MigrationIntegrityError, match="function ACL"):
            PostgresMigrationRunner(migration_dsn).plan(
                ingress_acl=IngressAclContract(ingress_role),
                deployment_profile="production",
            )
    finally:
        _drop_unprivileged_role(migration_dsn, extra_role)
        _drop_unprivileged_role(migration_dsn, ingress_role)


@pytest.mark.parametrize(
    "mutation",
    (
        "public_execute",
        "extra_execute_grant_option",
        "extra_relation",
        "extra_sequence",
        "unsafe_membership",
    ),
)
def test_joint_acl_contract_rejects_public_extra_and_unsafe_grants(migration_dsn, mutation):
    """Every direct or inherited authority outside the two declared roles is drift."""
    app_role = f"memplex_joint_grant_app_{uuid.uuid4().hex[:8]}"
    ingress_role = f"memplex_joint_grant_ingress_{uuid.uuid4().hex[:8]}"
    extra_role = f"memplex_joint_grant_extra_{uuid.uuid4().hex[:8]}"
    _ready_resources(migration_dsn).close()
    _provision_application_role(migration_dsn, app_role)
    _provision_ingress_role(migration_dsn, ingress_role, "remote-joint-grant")
    _create_ungranted_application_role(migration_dsn, extra_role)
    conn = psycopg2.connect(migration_dsn)
    try:
        cur = conn.cursor()
        if mutation == "public_execute":
            cur.execute("GRANT EXECUTE ON FUNCTION memplex_sync_apply_inbound(bytea,text) TO PUBLIC")
        elif mutation == "extra_execute_grant_option":
            cur.execute(
                pg_sql.SQL(
                    "GRANT EXECUTE ON FUNCTION memplex_sync_apply_inbound(bytea,text) TO {} WITH GRANT OPTION"
                ).format(pg_sql.Identifier(extra_role))
            )
        elif mutation == "extra_relation":
            cur.execute(
                pg_sql.SQL("GRANT SELECT ON memplex_sync_outbox TO {}").format(
                    pg_sql.Identifier(extra_role)
                )
            )
        elif mutation == "extra_sequence":
            cur.execute(
                pg_sql.SQL("GRANT USAGE ON SEQUENCE memplex_sync_outbox_stream_seq_seq TO {}").format(
                    pg_sql.Identifier(extra_role)
                )
            )
        else:
            cur.execute(pg_sql.SQL("GRANT pg_read_all_data TO {}").format(pg_sql.Identifier(ingress_role)))
        conn.commit()
        cur.close()
    finally:
        conn.close()
    try:
        with pytest.raises(MigrationIntegrityError):
            PostgresMigrationRunner(migration_dsn).plan(
                application_acl=ApplicationAclContract(app_role),
                ingress_acl=IngressAclContract(ingress_role),
                deployment_profile="production",
            )
    finally:
        _drop_unprivileged_role(migration_dsn, extra_role)
        _drop_unprivileged_role(migration_dsn, ingress_role)
        _drop_unprivileged_role(migration_dsn, app_role)


def test_joint_acl_contract_rejects_disabled_ingress_binding(migration_dsn):
    app_role = f"memplex_joint_disabled_app_{uuid.uuid4().hex[:8]}"
    ingress_role = f"memplex_joint_disabled_ingress_{uuid.uuid4().hex[:8]}"
    _ready_resources(migration_dsn).close()
    _provision_application_role(migration_dsn, app_role)
    _provision_ingress_role(migration_dsn, ingress_role, "remote-joint-disabled")
    _admin_execute(
        migration_dsn,
        "UPDATE memplex_sync_ingress_principals SET enabled=false WHERE role_name=%s::name",
        (ingress_role,),
    )
    try:
        with pytest.raises(MigrationIntegrityError, match="owner-bound"):
            PostgresMigrationRunner(migration_dsn).plan(
                application_acl=ApplicationAclContract(app_role),
                ingress_acl=IngressAclContract(ingress_role),
                deployment_profile="production",
            )
    finally:
        _drop_unprivileged_role(migration_dsn, ingress_role)
        _drop_unprivileged_role(migration_dsn, app_role)


@pytest.mark.parametrize(
    ("grantee", "privilege"),
    (
        ("PUBLIC", "USAGE"),
        ("PUBLIC", "CREATE"),
        ("pg_database_owner", "USAGE"),
        ("pg_database_owner", "CREATE"),
    ),
)
def test_production_application_contract_rejects_extra_schema_acl(
    migration_dsn, grantee, privilege
):
    """PUBLIC and non-owner pg_database_owner schema authority invalidate readiness."""
    role = f"memplex_schema_public_{uuid.uuid4().hex[:8]}"
    _ready_resources(migration_dsn).close()
    _provision_application_role(migration_dsn, role)
    conn = psycopg2.connect(migration_dsn)
    try:
        cur = conn.cursor()
        try:
            cur.execute("SELECT current_schema()")
            schema = cur.fetchone()[0]
            grantee_sql = pg_sql.SQL("PUBLIC") if grantee == "PUBLIC" else pg_sql.Identifier(grantee)
            cur.execute(
                pg_sql.SQL("GRANT {} ON SCHEMA {} TO {}").format(
                    pg_sql.SQL(privilege), pg_sql.Identifier(schema), grantee_sql
                )
            )
            conn.commit()
        finally:
            cur.close()
    finally:
        conn.close()
    contract = ApplicationAclContract(role)
    runner = PostgresMigrationRunner(migration_dsn)
    application_dsn = psycopg2.extensions.make_dsn(migration_dsn, user=role)
    resources = PostgresStorageResources(
        dsn=application_dsn, migration_dsn=migration_dsn
    )
    try:
        with pytest.raises(MigrationIntegrityError):
            runner.plan(application_acl=contract, deployment_profile="production")
        with pytest.raises(MigrationIntegrityError):
            runner.status(application_acl=contract, deployment_profile="production")
        with pytest.raises(MigrationIntegrityError):
            runner.apply(
                dry_run=True,
                application_acl=contract,
                deployment_profile="production",
            )
        with pytest.raises(MigrationIntegrityError):
            runner.verify_storage_readiness(
                VectorCapabilityRequest(dim=0, policy="disabled"),
                "production",
                expected_target=runner.inspect_target(),
                application_acl=contract,
            )
        with pytest.raises(MigrationIntegrityError):
            resources.ensure_ready(
                VectorCapabilityRequest(dim=0, policy="disabled"), "production"
            )
        assert resources.state == "FAULTED"
        assert resources.pool_created is False
        assert resources.business_lease_count == 0
    finally:
        _drop_unprivileged_role(migration_dsn, role)


def test_resources_reject_different_admin_schema_before_migration_or_pool(pg_dsn):
    """Application schema A and migration schema B fail closed before DDL/pool creation."""
    schema_a = f"memplex_target_a_{uuid.uuid4().hex[:8]}"
    schema_b = f"memplex_target_b_{uuid.uuid4().hex[:8]}"
    conn = psycopg2.connect(pg_dsn)
    try:
        cur = conn.cursor()
        cur.execute(pg_sql.SQL("CREATE SCHEMA {}").format(pg_sql.Identifier(schema_a)))
        cur.execute(pg_sql.SQL("CREATE SCHEMA {}").format(pg_sql.Identifier(schema_b)))
        conn.commit()
        cur.close()
        app_dsn = psycopg2.extensions.make_dsn(
            pg_dsn, options=f"-c search_path={schema_a},public"
        )
        admin_dsn = psycopg2.extensions.make_dsn(
            pg_dsn, options=f"-c search_path={schema_b},public"
        )
        resources = PostgresStorageResources(dsn=app_dsn, migration_dsn=admin_dsn)
        with pytest.raises(MigrationIntegrityError, match="migration target"):
            resources.ensure_ready(
                VectorCapabilityRequest(dim=0, policy="disabled"),
                "development",
            )
        assert resources.pool_created is False
        assert resources.business_lease_count == 0
        assert resources.state == "FAULTED"
        assert not _migration_table_exists(app_dsn, "memplex_schema_migrations")
        assert not _migration_table_exists(admin_dsn, "memplex_schema_migrations")
    finally:
        cur = conn.cursor()
        cur.execute(pg_sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(pg_sql.Identifier(schema_a)))
        cur.execute(pg_sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(pg_sql.Identifier(schema_b)))
        conn.commit()
        cur.close()
        conn.close()


def test_resources_close_pool_factory_target_b_without_publishing_a_seal(pg_dsn):
    """A pool factory resolving to B is physically closed before any lease escapes."""
    from psycopg2.pool import ThreadedConnectionPool

    schema_a = f"memplex_pool_a_{uuid.uuid4().hex[:8]}"
    schema_b = f"memplex_pool_b_{uuid.uuid4().hex[:8]}"
    conn = psycopg2.connect(pg_dsn)
    pools = []
    try:
        cur = conn.cursor()
        cur.execute(pg_sql.SQL("CREATE SCHEMA {}").format(pg_sql.Identifier(schema_a)))
        cur.execute(pg_sql.SQL("CREATE SCHEMA {}").format(pg_sql.Identifier(schema_b)))
        conn.commit()
        cur.close()
        app_dsn = psycopg2.extensions.make_dsn(
            pg_dsn, options=f"-c search_path={schema_a},public"
        )
        pool_b_dsn = psycopg2.extensions.make_dsn(
            pg_dsn, options=f"-c search_path={schema_b},public"
        )

        class _TrackingPool:
            def __init__(self):
                self._pool = ThreadedConnectionPool(1, 8, pool_b_dsn)
                self.closeall_calls = 0

            def getconn(self):
                return self._pool.getconn()

            def putconn(self, connection):
                self._pool.putconn(connection)

            def closeall(self):
                self.closeall_calls += 1
                self._pool.closeall()

        def pool_factory(*_args):
            pool = _TrackingPool()
            pools.append(pool)
            return pool

        resources = PostgresStorageResources(dsn=app_dsn, pool_factory=pool_factory)
        with pytest.raises(MigrationIntegrityError, match="pool target"):
            resources.ensure_ready(
                VectorCapabilityRequest(dim=0, policy="disabled"),
                "development",
            )
        assert len(pools) == 1
        assert pools[0].closeall_calls == 1
        assert resources.pool_created is False
        assert resources.business_lease_count == 0
        assert resources.state == "FAULTED"
    finally:
        cur = conn.cursor()
        cur.execute(pg_sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(pg_sql.Identifier(schema_a)))
        cur.execute(pg_sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(pg_sql.Identifier(schema_b)))
        conn.commit()
        cur.close()
        conn.close()


@pytest.fixture
def pgvector_available(pg_dsn):
    """Whether this function-scoped PostgreSQL case can use pgvector."""
    conn = psycopg2.connect(pg_dsn)
    try:
        cur = conn.cursor()
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.commit()
        cur.close()
        return True
    except Exception as exc:
        conn.rollback()
        if os.environ.get("MEMPLEX_REQUIRE_PGVECTOR") == "1":
            pytest.fail(f"pgvector is required by this CI gate: {exc}")
        return False
    finally:
        conn.close()


@pytest.fixture
def migration_dsn(pg_dsn):
    """A dedicated schema prevents migration checks from touching ``public``."""
    schema = f"g003_migrations_{uuid.uuid4().hex}"
    conn = psycopg2.connect(pg_dsn)
    try:
        cur = conn.cursor()
        cur.execute(f'CREATE SCHEMA "{schema}"')
        conn.commit()
        cur.close()
    finally:
        conn.close()
    isolated_dsn = psycopg2.extensions.make_dsn(
        pg_dsn, options=f"-c search_path={schema}"
    )
    try:
        yield isolated_dsn
    finally:
        conn = psycopg2.connect(pg_dsn)
        try:
            cur = conn.cursor()
            cur.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            conn.commit()
            cur.close()
        finally:
            conn.close()


def _migration_table_exists(dsn: str, table: str) -> bool:
    conn = psycopg2.connect(dsn)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM pg_catalog.pg_class AS relation
                JOIN pg_catalog.pg_namespace AS namespace
                  ON namespace.oid = relation.relnamespace
                WHERE namespace.nspname = pg_catalog.current_schema()
                  AND relation.relname = %s
            )
            """,
            (table,),
        )
        present = bool(cur.fetchone()[0])
        cur.close()
        return present
    finally:
        conn.close()


def test_readonly_readiness_verifier_requires_catalogue_and_never_materialises_it(migration_dsn):
    """Verifier readback cannot replace the migration transaction it validates."""
    runner = PostgresMigrationRunner(migration_dsn)
    target = runner.inspect_target()

    with pytest.raises(MigrationIntegrityError, match="catalogue is not ready"):
        runner.verify_storage_readiness(
            VectorCapabilityRequest(dim=0, policy="disabled"),
            "development",
            expected_target=target,
        )
    assert not _migration_table_exists(migration_dsn, "memplex_schema_migrations")

    runner.apply(expected_target=target)
    status = PostgresMigrationRunner(migration_dsn).verify_storage_readiness(
        VectorCapabilityRequest(dim=0, policy="disabled"),
        "development",
        expected_target=target,
    )
    assert status.state == "disabled"
    assert status.dim == 0
    assert status.parameter_digest is None


def test_readiness_verifier_rejects_catalogue_capability_digest_mismatch(migration_dsn):
    """A capability row without the matching vector catalogue cannot become disabled-ready."""
    runner = PostgresMigrationRunner(migration_dsn)
    target = runner.inspect_target()
    runner.apply(expected_target=target)
    _migration_execute(
        migration_dsn,
        """
        INSERT INTO memplex_schema_capabilities (capability_name, parameter_digest, applied_at)
        VALUES ('pgvector_embedding', 'forged-digest', CURRENT_TIMESTAMP)
        """,
    )

    with pytest.raises(MigrationIntegrityError):
        PostgresMigrationRunner(migration_dsn).verify_storage_readiness(
            VectorCapabilityRequest(dim=0, policy="disabled"),
            "development",
            expected_target=target,
        )


def _migration_execute(dsn: str, statement: str) -> None:
    conn = psycopg2.connect(dsn)
    try:
        cur = conn.cursor()
        cur.execute(statement)
        conn.commit()
        cur.close()
    finally:
        conn.close()


def _install_pre_g002_3_2_7(dsn: str) -> None:
    """Pin the pre-ACL 3.2.7 catalogue instead of using store startup DDL."""
    _migration_execute(
        dsn,
        """
        CREATE TABLE memplex_functions (
            id TEXT PRIMARY KEY,
            data JSONB NOT NULL,
            updated_at TIMESTAMPTZ,
            search_tsv TSVECTOR GENERATED ALWAYS AS (
                to_tsvector('simple', coalesce(data->>'name','') || ' ' ||
                    coalesce(data->>'domain','') || ' ' ||
                    coalesce(data->>'trigger_text','') || ' ' ||
                    coalesce(data->>'action_text',''))
            ) STORED
        );
        CREATE INDEX fts_functions_idx ON memplex_functions USING GIN (search_tsv);
        CREATE TABLE memplex_edges (
            source TEXT,
            target TEXT,
            edge_type TEXT,
            weight REAL,
            evidence JSONB,
            created_at TIMESTAMPTZ,
            PRIMARY KEY (source, target, edge_type)
        );
        CREATE TABLE memplex_observations (
            id TEXT PRIMARY KEY,
            data JSONB NOT NULL,
            created_at TIMESTAMPTZ
        );
        CREATE TABLE memplex_changelog (
            id SERIAL PRIMARY KEY,
            func_id TEXT,
            ts TIMESTAMPTZ,
            event_type TEXT,
            description TEXT,
            source TEXT,
            actor TEXT
        );
        """,
    )


def _install_post_g002_core(dsn: str) -> None:
    migrations = discover_migrations()
    _migration_execute(dsn, migrations[0].sql_bytes.decode("utf-8"))
    _migration_execute(dsn, _body_for_execution(migrations[1]).decode("utf-8"))


def _install_v3_catalogue_with_executed_ledger(dsn: str) -> None:
    """Build the exact pre-0004 catalogue without treating it as a baseline."""
    migrations = discover_migrations()
    for migration in migrations[:3]:
        _migration_execute(dsn, _body_for_execution(migration).decode("utf-8"))
    values = ",\n".join(
        "(" + ", ".join(
            (
                str(migration.version),
                repr(migration.name),
                repr(migration.checksum),
                "CURRENT_TIMESTAMP",
                "'executed'",
                "NULL",
            )
        ) + ")"
        for migration in migrations[:3]
    )
    _migration_execute(
        dsn,
        f"""
        CREATE TABLE memplex_schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            checksum TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL,
            execution_mode TEXT NOT NULL,
            baseline_fingerprint TEXT
        );
        INSERT INTO memplex_schema_migrations
            (version, name, checksum, applied_at, execution_mode, baseline_fingerprint)
        VALUES {values}
        """,
    )


_V3_MANAGED_TABLES = (
    "memplex_functions",
    "memplex_edges",
    "memplex_observations",
    "memplex_facts",
    "memplex_preferences",
    "memplex_changelog",
    "feedback",
    "memplex_schema_capabilities",
    "memplex_schema_migrations",
)
_V5_SYNC_TABLES = (
    "memplex_sync_outbox", "memplex_sync_entity_versions", "memplex_sync_inbox",
    "memplex_sync_batches", "memplex_sync_targets", "memplex_sync_deliveries",
    "memplex_sync_cursors", "memplex_sync_stream_state", "memplex_sync_local_identity", "memplex_sync_ingress_principals", "memplex_sync_snapshots",
    "memplex_sync_snapshot_items",
    "memplex_background_tasks",
)


def _handoff_v3_catalogue_to_non_superuser_owner(dsn: str, role: str) -> str:
    """Make the runner's verified owner a FORCE-RLS-constrained login role."""
    conn = psycopg2.connect(dsn)
    try:
        cur = conn.cursor()
        cur.execute("SELECT current_schema(), current_user")
        schema, original_owner = cur.fetchone()
        cur.execute(pg_sql.SQL("CREATE ROLE {} LOGIN").format(pg_sql.Identifier(role)))
        cur.execute(
            pg_sql.SQL("GRANT USAGE, CREATE ON SCHEMA {} TO {}").format(
                pg_sql.Identifier(schema), pg_sql.Identifier(role)
            )
        )
        for table in _V3_MANAGED_TABLES:
            cur.execute(
                pg_sql.SQL("ALTER TABLE {} OWNER TO {}").format(
                    pg_sql.Identifier(table), pg_sql.Identifier(role)
                )
            )
        cur.execute(
            pg_sql.SQL("ALTER SEQUENCE memplex_changelog_id_seq OWNER TO {}").format(
                pg_sql.Identifier(role)
            )
        )
        conn.commit()
        return str(original_owner)
    finally:
        conn.close()


def _restore_v3_catalogue_owner_and_drop_role(dsn: str, role: str, original_owner: str) -> None:
    conn = psycopg2.connect(dsn)
    try:
        cur = conn.cursor()
        cur.execute("SELECT current_schema()")
        schema = cur.fetchone()[0]
        for table in _V3_MANAGED_TABLES:
            cur.execute(
                pg_sql.SQL("ALTER TABLE {} OWNER TO {}").format(
                    pg_sql.Identifier(table), pg_sql.Identifier(original_owner)
                )
            )
        cur.execute(
            pg_sql.SQL("ALTER SEQUENCE memplex_changelog_id_seq OWNER TO {}").format(
                pg_sql.Identifier(original_owner)
            )
        )
        for table in _V5_SYNC_TABLES:
            cur.execute("SELECT to_regclass(%s)", (table,))
            if cur.fetchone()[0] is None:
                continue
            cur.execute(
                pg_sql.SQL("ALTER TABLE {} OWNER TO {}").format(
                    pg_sql.Identifier(table), pg_sql.Identifier(original_owner)
                )
            )
        cur.execute("SELECT to_regclass('memplex_sync_outbox_stream_seq_seq')")
        if cur.fetchone()[0] is not None:
            cur.execute(
                pg_sql.SQL("ALTER SEQUENCE memplex_sync_outbox_stream_seq_seq OWNER TO {}").format(
                    pg_sql.Identifier(original_owner)
                )
            )
        for function, arguments in (
            ("memplex_configure_sync_local_identity", "text"),
            ("memplex_configure_sync_ingress_principal", "name,text"),
            ("memplex_sync_assert_delivery_quota", "text,bigint"),
            ("memplex_sync_snapshot_admission_counts", ""),
            ("memplex_sync_compact", "timestamptz,timestamptz,integer"),
            ("memplex_sync_capture_before", ""),
            ("memplex_sync_capture_local_change", ""),
            ("memplex_sync_apply_inbound", "bytea,text"),
            ("memplex_sync_require_canonical_entity_key", "text,text"),
            ("memplex_sync_require_canonical_version", "text,text,text"),
            ("memplex_sync_encode_string_array", "jsonb"),
        ):
            cur.execute("SELECT to_regprocedure(%s)", (f"{function}({arguments})",))
            if cur.fetchone()[0] is None:
                continue
            cur.execute(
                pg_sql.SQL("ALTER FUNCTION {}({}) OWNER TO {}").format(
                    pg_sql.Identifier(function), pg_sql.SQL(arguments), pg_sql.Identifier(original_owner)
                )
            )
        cur.execute(
            pg_sql.SQL("REVOKE ALL ON SCHEMA {} FROM {}").format(
                pg_sql.Identifier(schema), pg_sql.Identifier(role)
            )
        )
        cur.execute(pg_sql.SQL("DROP ROLE {}").format(pg_sql.Identifier(role)))
        conn.commit()
        cur.close()
    finally:
        conn.close()


def _ledger_rows(dsn: str):
    conn = psycopg2.connect(dsn)
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT version, execution_mode FROM memplex_schema_migrations ORDER BY version"
        )
        rows = cur.fetchall()
        cur.close()
        return rows
    finally:
        conn.close()


def _ledger_rows_with_timestamps(dsn: str):
    conn = psycopg2.connect(dsn)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT version, execution_mode, applied_at
            FROM memplex_schema_migrations
            ORDER BY version
            """
        )
        rows = cur.fetchall()
        cur.close()
        return rows
    finally:
        conn.close()


def _migration_column_exists(dsn: str, table: str, column: str) -> bool:
    conn = psycopg2.connect(dsn)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = current_schema() AND table_name = %s AND column_name = %s
            )
            """,
            (table, column),
        )
        present = bool(cur.fetchone()[0])
        cur.close()
        return present
    finally:
        conn.close()


def _vector_extension_is_available(dsn: str) -> bool:
    conn = psycopg2.connect(dsn)
    try:
        cur = conn.cursor()
        cur.execute("SELECT EXISTS (SELECT 1 FROM pg_available_extensions WHERE name = 'vector')")
        available = bool(cur.fetchone()[0])
        cur.close()
        return available
    finally:
        conn.close()


def _schema_connection_factory(pg_dsn: str, *schemas: str):
    """Return a connection factory with safely quoted schema identifiers."""

    def factory():
        conn = psycopg2.connect(pg_dsn)
        cur = conn.cursor()
        try:
            cur.execute(
                pg_sql.SQL("SET search_path TO {}").format(
                    pg_sql.SQL(", ").join(pg_sql.Identifier(schema) for schema in schemas)
                )
            )
            conn.commit()
        finally:
            cur.close()
        return conn

    return factory


def _migration_execute_in_schema(pg_dsn: str, schema: str, sql: str) -> None:
    """Execute fixture SQL where libpq's option grammar cannot hold the identifier."""
    conn = _schema_connection_factory(pg_dsn, schema)()
    try:
        cur = conn.cursor()
        try:
            cur.execute(sql)
            conn.commit()
        finally:
            cur.close()
    finally:
        conn.close()


def _install_post_g002_core_in_schema(pg_dsn: str, schema: str) -> None:
    migrations = discover_migrations()
    _migration_execute_in_schema(pg_dsn, schema, migrations[0].sql_bytes.decode("utf-8"))
    _migration_execute_in_schema(
        pg_dsn, schema, _body_for_execution(migrations[1]).decode("utf-8")
    )


def _create_schema(pg_dsn: str, schema: str) -> None:
    conn = psycopg2.connect(pg_dsn)
    try:
        cur = conn.cursor()
        cur.execute(
            pg_sql.SQL("CREATE SCHEMA {}").format(pg_sql.Identifier(schema))
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()


def _drop_schema(pg_dsn: str, schema: str) -> None:
    conn = psycopg2.connect(pg_dsn)
    try:
        cur = conn.cursor()
        cur.execute(
            pg_sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(pg_sql.Identifier(schema))
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()


def _managed_relation_count(pg_dsn: str, schema: str) -> int:
    """Count runner-managed relations without relying on the target search path."""
    rows = _admin_query(
        pg_dsn,
        """
        SELECT count(*)
        FROM pg_class AS relation
        JOIN pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = %s
          AND relation.relname LIKE 'memplex%%'
        """,
        (schema,),
    )
    return int(rows[0][0])


def _migration_ledger_digest(dsn: str, connection_factory=None) -> str:
    conn = connection_factory() if connection_factory is not None else psycopg2.connect(dsn)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT md5(COALESCE(string_agg(
                version::text || ':' || name || ':' || checksum || ':' || execution_mode || ':' ||
                COALESCE(baseline_fingerprint, ''), '|' ORDER BY version), ''))
            FROM memplex_schema_migrations
            """
        )
        digest = cur.fetchone()[0]
        cur.close()
        return digest
    finally:
        conn.close()


def test_migration_plan_and_dry_run_on_empty_schema_are_read_only(migration_dsn):
    """A status check must not create a ledger or issue migration DDL."""
    runner = PostgresMigrationRunner(migration_dsn)

    plan = runner.plan()
    dry_run = runner.apply(dry_run=True)

    assert [item.version for item in plan.pending] == [1, 2, 3, 4, 5, 6]
    assert dry_run == plan
    assert not _migration_table_exists(migration_dsn, "memplex_schema_migrations")


def test_recognised_pre_g002_catalogue_upgrades_atomically(migration_dsn):
    """The pinned legacy catalogue must receive a contiguous executed ledger."""
    _install_pre_g002_3_2_7(migration_dsn)
    _migration_execute(
        migration_dsn,
        "INSERT INTO memplex_functions (id, data) VALUES ('legacy', '{\"name\": \"Legacy\"}')",
    )
    _migration_execute(
        migration_dsn,
        """
        CREATE TABLE feedback (
            memory_id TEXT NOT NULL,
            field_role TEXT NOT NULL,
            value_index INTEGER DEFAULT 0,
            verdict TEXT NOT NULL,
            reason TEXT,
            source TEXT DEFAULT 'user',
            timestamp TIMESTAMPTZ,
            owner TEXT,
            feedback_type TEXT DEFAULT 'field_value',
            old_value TEXT,
            new_value TEXT,
            needs_review BOOLEAN DEFAULT TRUE,
            needs_review_until TIMESTAMPTZ,
            resolved_at TIMESTAMPTZ,
            resolution TEXT
        );
        INSERT INTO feedback (memory_id, field_role, verdict, reason)
        VALUES ('legacy', 'name', 'incorrect', 'kept through upgrade');
        """,
    )

    result = PostgresMigrationRunner(migration_dsn).apply()

    assert result.state == "ready"
    assert _ledger_rows(migration_dsn) == [
        (1, "executed"),
        (2, "executed"),
        (3, "executed"),
        (4, "executed"),
        (5, "executed"),
        (6, "executed"),
    ]
    conn = psycopg2.connect(migration_dsn)
    try:
        cur = conn.cursor()
        cur.execute("SELECT data->>'name' FROM memplex_functions WHERE id = 'legacy'")
        assert cur.fetchone() == ("Legacy",)
        cur.execute("SELECT reason, tenant_id FROM feedback WHERE memory_id = 'legacy'")
        assert cur.fetchone() == ("kept through upgrade", "__memplex_legacy__")
        cur.close()
    finally:
        conn.close()


def test_recognised_post_g002_catalogue_adopts_then_applies_integrity(migration_dsn):
    """Already-authorised core is adopted; only migration 3 executes."""
    _install_post_g002_core(migration_dsn)
    _migration_execute(
        migration_dsn,
        """
        INSERT INTO memplex_functions
        (id, data, tenant_id, owner_subject, workspace, visibility, source_agent, source_session)
        VALUES ('current', '{"name": "Current"}', 'tenant', 'subject', 'workspace', 'workspace', 'agent', 'session')
        """,
    )

    runner = PostgresMigrationRunner(migration_dsn)
    plan = runner.plan()
    assert plan.current_version == 2
    assert [migration.version for migration in plan.pending] == [3, 4, 5, 6]
    assert not _migration_table_exists(migration_dsn, "memplex_schema_migrations")

    result = runner.apply()

    assert result.state == "ready"
    assert _ledger_rows(migration_dsn) == [
        (1, "adopted"),
        (2, "adopted"),
        (3, "executed"),
        (4, "executed"),
        (5, "executed"),
        (6, "executed"),
    ]
    assert _migration_table_exists(migration_dsn, "feedback")


def test_v3_catalogue_upgrades_to_v4_with_function_and_virtual_edge_targets(migration_dsn):
    """0004 validates ordinary endpoints while retaining virtual BELONGS_TO nodes."""
    _install_v3_catalogue_with_executed_ledger(migration_dsn)
    _migration_execute(
        migration_dsn,
        """
        INSERT INTO memplex_functions
            (id, data, tenant_id, owner_subject, workspace, visibility, source_agent, source_session)
        VALUES
            ('source', '{"domain": "virtual namespace"}', 'tenant', 'subject', 'workspace', 'workspace', 'agent', 'session'),
            ('target', '{}', 'tenant', 'subject', 'workspace', 'workspace', 'agent', 'session');
        INSERT INTO memplex_edges
            (source, target, edge_type, tenant_id, owner_subject, workspace, visibility, source_agent, source_session)
        VALUES
            ('source', 'target', 'RELATED_TO', 'tenant', 'subject', 'workspace', 'workspace', 'agent', 'session'),
            ('source', 'domain_virtual_namespace', 'BELONGS_TO', 'tenant', 'subject', 'workspace', 'workspace', 'agent', 'session');
        """,
    )

    plan = PostgresMigrationRunner(migration_dsn).plan()
    assert plan.current_version == 3
    assert [migration.version for migration in plan.pending] == [4, 5, 6]
    assert PostgresMigrationRunner(migration_dsn).apply().state == "ready"
    assert _ledger_rows(migration_dsn)[-1] == (6, "executed")
    assert _admin_query(
        migration_dsn,
        "SELECT edge_type, target_function FROM memplex_edges ORDER BY edge_type",
    ) == [("BELONGS_TO", None), ("RELATED_TO", "target")]


def test_v4_upgrades_atomically_to_reliable_sync_v5(migration_dsn):
    """A contiguous v4 ledger is the only historical baseline accepted by v5."""
    _install_v3_catalogue_with_executed_ledger(migration_dsn)
    PostgresMigrationRunner(migration_dsn).apply()
    assert _ledger_rows(migration_dsn) == [
        (1, "executed"),
        (2, "executed"),
        (3, "executed"),
        (4, "executed"),
        (5, "executed"),
        (6, "executed"),
    ]
    assert _admin_query(
        migration_dsn,
        "SELECT relrowsecurity, relforcerowsecurity FROM pg_class WHERE oid='memplex_sync_outbox'::regclass",
    ) == [(True, True)]
    assert PostgresMigrationRunner(migration_dsn).plan().state == "ready"


def test_v5_capture_uses_frozen_context_in_a_quoted_schema(pg_dsn):
    """SECURITY DEFINER hooks resolve their own quoted schema, not the caller path."""
    schema = f'g004 sync "{uuid.uuid4().hex[:12]}'
    _create_schema(pg_dsn, schema)
    factory = _schema_connection_factory(pg_dsn, schema)
    runner = PostgresMigrationRunner(pg_dsn, connection_factory=factory)
    try:
        assert runner.apply().state == "ready"
        conn = factory()
        try:
            cursor = conn.cursor()
            try:
                cursor.execute("SELECT memplex_configure_sync_local_identity('verified-local')")
                context = {
                    "memplex.sync_capture": "required",
                    "memplex.sync_apply_mode": "local",
                    "memplex.tenant_id": "tenant-capture",
                    "memplex.subject_id": "subject-capture",
                    "memplex.sync_origin_node_id": "verified-local",
                    "memplex.sync_event_id": "123e4567-e89b-42d3-a456-426614174000",
                    "memplex.sync_version_key": "v1:dmVyc2lvbg",
                    "memplex.sync_entity_key": "node:v1:ZnVuY3Rpb24",
                    "memplex.sync_payload": '{"protocol":"frozen"}',
                }
                for key, value in context.items():
                    cursor.execute("SELECT set_config(%s, %s, true)", (key, value))
                cursor.execute(
                    """
                    INSERT INTO memplex_functions
                        (id, data, tenant_id, owner_subject, workspace, visibility, source_agent, source_session)
                    VALUES ('capture-function', '{}', 'tenant-capture', 'subject-capture',
                            'workspace-capture', 'user', 'agent-capture', 'session-capture')
                    """
                )
                cursor.execute(
                    """
                    SELECT event_id, origin_node_id, entity_key, version_key, payload
                    FROM memplex_sync_outbox
                    """
                )
                assert cursor.fetchall() == [
                    (
                        "123e4567-e89b-42d3-a456-426614174000",
                        "verified-local",
                        "node:v1:ZnVuY3Rpb24",
                        "v1:dmVyc2lvbg",
                        {"protocol": "frozen"},
                    )
                ]
                conn.commit()
            finally:
                cursor.close()
        finally:
            conn.close()
        assert runner.plan().state == "ready"
    finally:
        _drop_schema(pg_dsn, schema)


def test_v5_local_identity_is_owner_only_and_rejects_forged_origin(migration_dsn):
    """Only the migration owner can bind the local origin trusted by capture."""
    role = f"memplex_sync_identity_{uuid.uuid4().hex[:8]}"
    _ready_resources(migration_dsn).close()
    _provision_application_role(migration_dsn, role)
    _migration_execute(
        migration_dsn,
        "SELECT memplex_configure_sync_local_identity('trusted-local-node')",
    )
    application_dsn = psycopg2.extensions.make_dsn(migration_dsn, user=role)
    conn = psycopg2.connect(application_dsn)
    try:
        cursor = conn.cursor()
        try:
            with pytest.raises(psycopg2.Error):
                cursor.execute("SELECT node_id FROM memplex_sync_local_identity")
            conn.rollback()
            context = {
                "memplex.sync_capture": "required",
                "memplex.sync_apply_mode": "local",
                "memplex.tenant_id": "tenant-identity",
                "memplex.subject_id": "subject-identity",
                "memplex.workspace_id": "workspace-identity",
                "memplex.sync_origin_node_id": "forged-local-node",
                "memplex.verified_local_node_id": "forged-local-node",
                "memplex.sync_event_id": "123e4567-e89b-42d3-a456-426614174000",
                "memplex.sync_version_key": "v1:dmVyc2lvbg",
                "memplex.sync_entity_key": "node:v1:ZnVuY3Rpb24",
                "memplex.sync_payload": "{}",
            }
            for key, value in context.items():
                cursor.execute("SELECT set_config(%s, %s, true)", (key, value))
            with pytest.raises(psycopg2.Error, match="memplex sync context is incomplete"):
                cursor.execute(
                    """
                    INSERT INTO memplex_functions
                        (id, data, tenant_id, owner_subject, workspace, visibility, source_agent, source_session)
                    VALUES ('forged-origin', '{}', 'tenant-identity', 'subject-identity',
                            'workspace-identity', 'user', 'agent-identity', 'session-identity')
                    """
                )
            conn.rollback()
        finally:
            cursor.close()
    finally:
        conn.close()
    assert _admin_query(migration_dsn, "SELECT count(*) FROM memplex_functions") == [(0,)]
    assert _admin_query(migration_dsn, "SELECT count(*) FROM memplex_sync_outbox") == [(0,)]
    assert _admin_query(migration_dsn, "SELECT count(*) FROM memplex_sync_deliveries") == [(0,)]
    assert _admin_query(
        migration_dsn, "SELECT last_value, is_called FROM memplex_sync_outbox_stream_seq_seq"
    ) == [(1, False)]
    _drop_unprivileged_role(migration_dsn, role)


@pytest.mark.parametrize("statement", (
    "INSERT INTO memplex_functions (id,data,tenant_id,owner_subject,workspace,visibility,source_agent,source_session) VALUES ('inbound-bypass','{}','tenant-bypass','subject-bypass','workspace-bypass','user','agent','session')",
    "UPDATE memplex_functions SET data='{}' WHERE id='inbound-bypass' AND tenant_id='tenant-bypass'",
    "DELETE FROM memplex_functions WHERE id='inbound-bypass' AND tenant_id='tenant-bypass'",
))
def test_v5_application_role_cannot_forge_inbound_capture_mode(migration_dsn, statement):
    """Inbound is never an application-selected trigger mode."""
    role = f"memplex_inbound_bypass_{uuid.uuid4().hex[:8]}"
    _ready_resources(migration_dsn).close()
    _provision_application_role(migration_dsn, role)
    _migration_execute(
        migration_dsn,
        "INSERT INTO memplex_functions (id,data,tenant_id,owner_subject,workspace,visibility,source_agent,source_session) VALUES ('inbound-bypass','{}','tenant-bypass','subject-bypass','workspace-bypass','user','agent','session')",
    )
    connection = psycopg2.connect(psycopg2.extensions.make_dsn(migration_dsn, user=role))
    try:
        cursor = connection.cursor()
        try:
            for key, value in {
                "memplex.sync_capture": "required",
                "memplex.sync_apply_mode": "inbound",
                "memplex.tenant_id": "tenant-bypass",
                "memplex.subject_id": "subject-bypass",
                "memplex.workspace_id": "workspace-bypass",
                "memplex.agent_id": "agent",
                "memplex.session_id": "session",
            }.items():
                cursor.execute("SELECT set_config(%s, %s, true)", (key, value))
            with pytest.raises(psycopg2.Error, match="memplex sync inbound is ingress-only"):
                cursor.execute(statement)
        finally:
            connection.rollback()
            cursor.close()
    finally:
        connection.close()
        _drop_unprivileged_role(migration_dsn, role)
    assert _admin_query(migration_dsn, "SELECT count(*) FROM memplex_functions") == [(1,)]
    assert _admin_query(migration_dsn, "SELECT count(*) FROM memplex_sync_outbox") == [(0,)]
    assert _admin_query(migration_dsn, "SELECT count(*) FROM memplex_sync_deliveries") == [(0,)]
    assert _admin_query(
        migration_dsn, "SELECT last_value, is_called FROM memplex_sync_outbox_stream_seq_seq"
    ) == [(1, False)]


def test_v5_capture_requires_configured_owner_local_identity(migration_dsn):
    """An unset deployment singleton rejects active capture before identity allocation."""
    PostgresMigrationRunner(migration_dsn).apply()
    conn = psycopg2.connect(migration_dsn)
    try:
        cursor = conn.cursor()
        try:
            context = {
                "memplex.sync_capture": "required",
                "memplex.sync_apply_mode": "local",
                "memplex.tenant_id": "tenant-unconfigured",
                "memplex.subject_id": "subject-unconfigured",
                "memplex.sync_origin_node_id": "claimed-local-node",
                "memplex.sync_event_id": "123e4567-e89b-42d3-a456-426614174000",
                "memplex.sync_version_key": "v1:dmVyc2lvbg",
                "memplex.sync_entity_key": "node:v1:ZnVub3V0",
                "memplex.sync_payload": "{}",
            }
            for key, value in context.items():
                cursor.execute("SELECT set_config(%s, %s, true)", (key, value))
            with pytest.raises(psycopg2.Error, match="memplex sync context is incomplete"):
                cursor.execute(
                    """
                    INSERT INTO memplex_functions
                        (id, data, tenant_id, owner_subject, workspace, visibility, source_agent, source_session)
                    VALUES ('unconfigured-origin', '{}', 'tenant-unconfigured', 'subject-unconfigured',
                            'workspace-unconfigured', 'user', 'agent-unconfigured', 'session-unconfigured')
                    """
                )
        finally:
            conn.rollback()
            cursor.close()
    finally:
        conn.close()
    assert _admin_query(migration_dsn, "SELECT count(*) FROM memplex_functions") == [(0,)]
    assert _admin_query(migration_dsn, "SELECT count(*) FROM memplex_sync_outbox") == [(0,)]
    assert _admin_query(
        migration_dsn, "SELECT last_value, is_called FROM memplex_sync_outbox_stream_seq_seq"
    ) == [(1, False)]


@pytest.mark.parametrize(
    ("method", "table"),
    (
        ("add_fact", "memplex_facts"),
        ("add_preference", "memplex_preferences"),
        ("add_observation", "memplex_observations"),
    ),
)
def test_v5_required_capture_upsert_creates_outbox_atomically_in_real_postgres(
    migration_dsn, method, table
):
    resources, store = _required_capture_store(
        migration_dsn,
        local_node_id="typed-upsert-local",
    )
    try:
        node_id = f"{method}-real-{uuid.uuid4().hex}"
        node = _build_typed_node(method, node_id)
        pre_row = _admin_query(
            migration_dsn,
            f"SELECT count(*) FROM {table} WHERE id=%s",
            (node_id,),
        )[0][0]
        pre_outbox = _admin_query(migration_dsn, "SELECT count(*) FROM memplex_sync_outbox")[0][0]

        getattr(store, method)(node)

        assert _admin_query(
            migration_dsn,
            f"SELECT count(*) FROM {table} WHERE id=%s",
            (node_id,),
        ) == [(pre_row + 1,)]
        assert _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_sync_outbox",
        ) == [(pre_outbox + 1,)]
        assert _admin_query(
            migration_dsn,
            "SELECT operation, payload FROM memplex_sync_outbox WHERE entity_key = %s ORDER BY stream_seq DESC LIMIT 1",
            (str(SyncEntityKey.node(node_id)),),
        ) == [("upsert", node.to_dict())]
    finally:
        resources.close()


@pytest.mark.parametrize(
    ("delete_method", "seed_method", "table"),
    (
        ("delete_fact", "add_fact", "memplex_facts"),
        ("delete_preference", "add_preference", "memplex_preferences"),
        ("delete_observation", "add_observation", "memplex_observations"),
    ),
)
def test_v5_required_capture_tombstone_and_outbox_cooccur_atomically_real_postgres(
    migration_dsn, delete_method, seed_method, table
):
    resources, store = _required_capture_store(
        migration_dsn,
        local_node_id="typed-delete-local",
    )
    try:
        node_id = f"{delete_method}-real-{uuid.uuid4().hex}"
        node = _build_typed_node(seed_method, node_id)

        getattr(store, seed_method)(node)
        pre_outbox = _admin_query(
            migration_dsn, "SELECT count(*) FROM memplex_sync_outbox"
        )[0][0]

        getattr(store, delete_method)(node.id)

        assert _admin_query(
            migration_dsn,
            f"SELECT count(*) FROM {table} WHERE id=%s",
            (node_id,),
        ) == [(0,)]
        assert _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_sync_outbox",
        ) == [(pre_outbox + 1,)]
        assert _admin_query(
            migration_dsn,
            "SELECT operation, payload FROM memplex_sync_outbox WHERE entity_key = %s ORDER BY stream_seq DESC LIMIT 1",
            (str(SyncEntityKey.node(node_id)),),
        ) == [("tombstone", None)]
    finally:
        resources.close()


@pytest.mark.parametrize(
    ("method", "seed_method", "table"),
    (
        ("add_fact", None, "memplex_facts"),
        ("add_preference", None, "memplex_preferences"),
        ("add_observation", None, "memplex_observations"),
        ("delete_fact", "add_fact", "memplex_facts"),
        ("delete_preference", "add_preference", "memplex_preferences"),
        ("delete_observation", "add_observation", "memplex_observations"),
    ),
)
def test_v5_required_capture_failure_in_changelog_rolls_back_real_postgres(
    migration_dsn, method, seed_method, table
):
    resources, store = _required_capture_store(
        migration_dsn,
        local_node_id="typed-failure-local",
    )
    fail_trigger = f"task4_capture_fail_{uuid.uuid4().hex[:8]}"
    fail_fn = f"{fail_trigger}_fn"
    try:
        node_id = f"{method}-real-{uuid.uuid4().hex}"
        node = _build_typed_node(seed_method or method, node_id)

        if seed_method is not None:
            getattr(store, seed_method)(node)

        pre_row = _admin_query(
            migration_dsn,
            f"SELECT count(*) FROM {table} WHERE id=%s",
            (node_id,),
        )[0][0]
        pre_outbox = _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_sync_outbox",
        )[0][0]

        _admin_execute(
            migration_dsn,
            f"""
            CREATE OR REPLACE FUNCTION {fail_fn}() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'typed capture rollback check';
            END;
            $$;
            CREATE TRIGGER {fail_trigger}
            BEFORE INSERT ON memplex_changelog
            FOR EACH ROW EXECUTE FUNCTION {fail_fn}();
            """,
        )

        with pytest.raises(psycopg2.Error, match="typed capture rollback check"):
            if method.startswith("add_"):
                getattr(store, method)(node)
            else:
                getattr(store, method)(node.id)

        assert _admin_query(
            migration_dsn,
            f"SELECT count(*) FROM {table} WHERE id=%s",
            (node_id,),
        ) == [(pre_row,)]
        assert _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_sync_outbox",
        ) == [(pre_outbox,)]
    finally:
        _admin_execute(
            migration_dsn,
            f"DROP TRIGGER IF EXISTS {fail_trigger} ON memplex_changelog;"
            f" DROP FUNCTION IF EXISTS {fail_fn}();",
        )
        resources.close()


def test_v5_required_capture_function_create_atomically_real_postgres(migration_dsn):
    resources, store = _required_capture_store(
        migration_dsn,
        local_node_id="function-create-local",
    )
    try:
        function_id = f"function-real-create-{uuid.uuid4().hex}"
        function = _func(function_id, "Capture")

        pre_rows = _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_functions WHERE id=%s",
            (function_id,),
        )[0][0]
        pre_outbox = _admin_query(migration_dsn, "SELECT count(*) FROM memplex_sync_outbox")[0][0]

        store.add(function, SRC)

        assert _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_functions WHERE id=%s",
            (function_id,),
        ) == [(pre_rows + 1,)]
        assert _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_sync_outbox",
        ) == [(pre_outbox + 1,)]
        expected_payload = _admin_query(
            migration_dsn,
            "SELECT data FROM memplex_functions WHERE id=%s",
            (function_id,),
        )[0][0]
        outbox_payload, = _admin_query(
            migration_dsn,
            "SELECT payload FROM memplex_sync_outbox WHERE entity_key = %s ORDER BY stream_seq DESC LIMIT 1",
            (str(SyncEntityKey.node(function_id)),),
        )[0]
        assert (
            "upsert",
            expected_payload,
        ) == _admin_query(
            migration_dsn,
            "SELECT operation, payload FROM memplex_sync_outbox WHERE entity_key = %s ORDER BY stream_seq DESC LIMIT 1",
            (str(SyncEntityKey.node(function_id)),),
        )[0]
        assert outbox_payload == expected_payload
    finally:
        resources.close()


def test_v5_required_capture_same_name_merge_updates_function_canonical_real_postgres(migration_dsn):
    resources, store = _required_capture_store(
        migration_dsn,
        local_node_id="function-merge-local",
    )
    try:
        canonical = _func("function-real-canonical", "Shared Name")
        store.add(canonical, SRC)
        base_payload = _admin_query(
            migration_dsn,
            "SELECT data FROM memplex_functions WHERE id=%s",
            (canonical.id,),
        )[0][0]

        incoming = _func("function-real-merge", "Shared Name", trigger=[_fv("incoming")])
        pre_outbox = _admin_query(migration_dsn, "SELECT count(*) FROM memplex_sync_outbox")[0][0]
        pre_entity_outbox = _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_sync_outbox WHERE entity_key=%s",
            (str(SyncEntityKey.node(canonical.id)),),
        )[0][0]

        result = store.merge(GraphData(nodes=[incoming], edges=[]))

        merged_payload = _admin_query(
            migration_dsn,
            "SELECT data FROM memplex_functions WHERE id=%s",
            (canonical.id,),
        )[0][0]
        assert result.new_functions == 0
        assert result.updated_functions == 1
        assert merged_payload != base_payload
        assert _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_sync_outbox WHERE entity_key=%s",
            (str(SyncEntityKey.node(canonical.id)),),
        )[0][0] == pre_entity_outbox + 1
        assert _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_sync_outbox",
        )[0][0] == pre_outbox + 1
        assert _admin_query(
            migration_dsn,
            "SELECT operation, payload FROM memplex_sync_outbox WHERE entity_key=%s ORDER BY stream_seq DESC LIMIT 1",
            (str(SyncEntityKey.node(canonical.id)),),
        ) == [("upsert", merged_payload)]
        assert _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_functions WHERE id=%s",
            (incoming.id,),
        ) == [(0,)]
    finally:
        resources.close()


def test_v5_required_capture_function_failure_in_changelog_rolls_back_real_postgres(migration_dsn):
    resources, store = _required_capture_store(
        migration_dsn,
        local_node_id="function-failure-local",
    )
    function_id = f"function-real-fail-{uuid.uuid4().hex}"
    function = _func(function_id, "Failure Function")
    incoming = _func(f"incoming-{function_id}", "Failure Function", trigger=[_fv("incoming")])
    fail_trigger = f"task4_capture_fail_{uuid.uuid4().hex[:8]}"
    fail_fn = f"{fail_trigger}_fn"
    try:
        pre_add_outbox = _admin_query(migration_dsn, "SELECT count(*) FROM memplex_sync_outbox")[0][0]
        pre_add_rows = _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_functions",
        )[0][0]
        store.add(function, SRC)
        before_merge_payload = _admin_query(
            migration_dsn,
            "SELECT data FROM memplex_functions WHERE id=%s",
            (function_id,),
        )[0][0]

        _admin_execute(
            migration_dsn,
            f"""
            CREATE OR REPLACE FUNCTION {fail_fn}() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'typed capture rollback check';
            END;
            $$;
            CREATE TRIGGER {fail_trigger}
            BEFORE INSERT ON memplex_changelog
            FOR EACH ROW EXECUTE FUNCTION {fail_fn}();
            """,
        )

        with pytest.raises(psycopg2.Error, match="typed capture rollback check"):
            store.merge(GraphData(nodes=[incoming], edges=[]))

        assert _admin_query(
            migration_dsn,
            "SELECT data FROM memplex_functions WHERE id=%s",
            (function_id,),
        ) == [(before_merge_payload,)]
        assert _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_functions",
        ) == [(pre_add_rows + 1,)]
        assert _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_sync_outbox",
        ) == [(pre_add_outbox + 1,)]

        with pytest.raises(psycopg2.Error, match="typed capture rollback check"):
            store.add(_func(
                f"failed-create-{uuid.uuid4().hex}", "Failed Add", trigger=[_fv("add")]
            ), SRC)

        assert _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_functions",
        ) == [(pre_add_rows + 1,)]
    finally:
        _admin_execute(
            migration_dsn,
            f"DROP TRIGGER IF EXISTS {fail_trigger} ON memplex_changelog;"
            f" DROP FUNCTION IF EXISTS {fail_fn}();",
        )
        resources.close()


def test_v5_required_capture_function_delete_tombstones_edges_only_real_postgres(
    migration_dsn,
):
    resources, store = _required_capture_store(
        migration_dsn,
        local_node_id="function-delete-local",
    )
    function_id = f"function-real-delete-{uuid.uuid4().hex}"
    function = _func(function_id, "Capture Delete")
    linked = _func(f"function-real-linked-{uuid.uuid4().hex}", "Capture Linked")
    fact = _build_typed_node("add_fact", function_id)
    pref = _build_typed_node("add_preference", function_id)
    obs = _build_typed_node("add_observation", function_id)
    edge_one_key = str(SyncEntityKey.edge(function.id, linked.id, "REFERENCES"))
    edge_two_key = str(SyncEntityKey.edge(linked.id, function.id, "SUPPORTED_BY"))
    function_key = str(SyncEntityKey.node(function_id))
    try:
        store.add(function, SRC)
        store.add(linked, SRC)
        store.merge(
            GraphData(
                nodes=[],
                edges=[
                    GraphEdge(function.id, linked.id, "REFERENCES"),
                    GraphEdge(linked.id, function.id, "SUPPORTED_BY"),
                ],
            )
        )
        store.add_fact(fact)
        store.add_preference(pref)
        store.add_observation(obs)

        pre_outbox = _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_sync_outbox",
        )[0][0]
        pre_stream_seq = _admin_query(
            migration_dsn,
            "SELECT COALESCE(MAX(stream_seq), 0) FROM memplex_sync_outbox",
        )[0][0]

        store.delete(function.id)

        assert _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_functions WHERE id=%s",
            (function.id,),
        ) == [(0,)]
        assert _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_edges WHERE source=%s OR target=%s",
            (function.id, function.id),
        )[0][0] == 0
        assert _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_facts WHERE id=%s",
            (function.id,),
        ) == [(1,)]
        assert _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_preferences WHERE id=%s",
            (function.id,),
        ) == [(1,)]
        assert _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_observations WHERE id=%s",
            (function.id,),
        ) == [(1,)]
        assert _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_sync_outbox",
        )[0][0] == (pre_outbox + 3)
        assert _admin_query(
            migration_dsn,
            "SELECT operation, payload, stream_seq FROM memplex_sync_outbox "
            "WHERE stream_seq > %s AND entity_key = ANY(%s) "
            "ORDER BY stream_seq",
            (
                pre_stream_seq,
                [edge_one_key, edge_two_key, function_key],
            ),
        ) == [
            ("tombstone", None, pre_stream_seq + 1),
            ("tombstone", None, pre_stream_seq + 2),
            ("tombstone", None, pre_stream_seq + 3),
        ]
        function_key_events = _admin_query(
            migration_dsn,
            "SELECT stream_seq FROM memplex_sync_outbox WHERE entity_key = %s AND stream_seq > %s ORDER BY stream_seq",
            (
                function_key,
                pre_stream_seq,
            ),
        )
        assert function_key_events == [(pre_stream_seq + 3,)]
        assert _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_sync_outbox "
            "WHERE stream_seq > %s AND node_type = ANY(%s) AND operation = 'tombstone'",
            (pre_stream_seq, ["fact", "preference", "observation"]),
        ) == [(0,)]
        assert _admin_query(
            migration_dsn,
            "SELECT stream_seq FROM memplex_sync_outbox WHERE entity_key = %s ORDER BY stream_seq DESC LIMIT 1",
            (edge_one_key,),
        )[0][0] < _admin_query(
            migration_dsn,
            "SELECT stream_seq FROM memplex_sync_outbox WHERE entity_key = %s ORDER BY stream_seq DESC LIMIT 1",
            (function_key,),
        )[0][0]
        assert _admin_query(
            migration_dsn,
            "SELECT stream_seq FROM memplex_sync_outbox WHERE entity_key = %s ORDER BY stream_seq DESC LIMIT 1",
            (edge_two_key,),
        )[0][0] < _admin_query(
            migration_dsn,
            "SELECT stream_seq FROM memplex_sync_outbox WHERE entity_key = %s ORDER BY stream_seq DESC LIMIT 1",
            (function_key,),
        )[0][0]
    finally:
        resources.close()


def test_v5_required_capture_function_delete_failure_in_changelog_rolls_back_real_postgres(migration_dsn):
    resources, store = _required_capture_store(
        migration_dsn,
        local_node_id="function-delete-failure-local",
    )
    function = _func(f"function-real-delete-fail-{uuid.uuid4().hex}", "Failure Delete")
    function_id = function.id
    linked = _func(f"function-real-delete-fail-linked-{uuid.uuid4().hex}", "Linked")
    fact = _build_typed_node("add_fact", function_id)
    fail_trigger = f"task4_capture_delete_fail_{uuid.uuid4().hex[:8]}"
    fail_fn = f"{fail_trigger}_fn"
    try:
        store.add(function, SRC)
        store.add(linked, SRC)
        store.merge(
            GraphData(
                nodes=[],
                edges=[GraphEdge(function.id, linked.id, "REFERENCES")],
            )
        )
        store.add_fact(fact)

        pre_outbox = _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_sync_outbox",
        )[0][0]
        pre_function_rows = _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_functions WHERE id=%s",
            (function.id,),
        )[0][0]
        pre_fact_rows = _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_facts WHERE id=%s",
            (function.id,),
        )[0][0]
        pre_edge_rows = _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_edges WHERE source=%s OR target=%s",
            (function.id, function.id),
        )[0][0]

        _admin_execute(
            migration_dsn,
            f"""
            CREATE OR REPLACE FUNCTION {fail_fn}() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'typed capture delete rollback check';
            END;
            $$;
            CREATE TRIGGER {fail_trigger}
            BEFORE INSERT ON memplex_changelog
            FOR EACH ROW EXECUTE FUNCTION {fail_fn}();
            """
        )

        with pytest.raises(psycopg2.Error, match="typed capture delete rollback check"):
            store.delete(function_id)

        assert _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_functions WHERE id=%s",
            (function.id,),
        ) == [(pre_function_rows,)]
        assert _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_facts WHERE id=%s",
            (function.id,),
        ) == [(pre_fact_rows,)]
        assert _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_edges WHERE source=%s OR target=%s",
            (function.id, function.id),
        )[0][0] == pre_edge_rows
        assert _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_sync_outbox",
        )[0][0] == pre_outbox
    finally:
        _admin_execute(
            migration_dsn,
            f"DROP TRIGGER IF EXISTS {fail_trigger} ON memplex_changelog;"
            f" DROP FUNCTION IF EXISTS {fail_fn}();",
        )
        resources.close()


def test_v5_required_capture_increment_access_events_full_payload_real_postgres(migration_dsn):
    resources, store = _required_capture_store(
        migration_dsn,
        local_node_id="func-access-local",
    )
    function = _func(f"function-real-access-{uuid.uuid4().hex}", "Capture Access")
    try:
        store.add(function, SRC)
        pre_outbox = _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_sync_outbox",
        )[0][0]
        function_key = str(SyncEntityKey.node(function.id))

        store.increment_access(function.id)
        store.increment_access(function.id)

        assert _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_sync_outbox",
        )[0][0] == (pre_outbox + 2)
        outbox_payloads = _admin_query(
            migration_dsn,
            "SELECT payload, stream_seq FROM memplex_sync_outbox "
            "WHERE entity_key = %s ORDER BY stream_seq DESC LIMIT 2",
            (function_key,),
        )
        assert len(outbox_payloads) == 2
        payload_latest, _ = outbox_payloads[0]
        payload_prev, _ = outbox_payloads[1]
        assert payload_latest["access_count"] == 2
        assert payload_prev["access_count"] == 1
        assert payload_latest["last_accessed_at"]
        stored_payload = _admin_query(
            migration_dsn,
            "SELECT data FROM memplex_functions WHERE id=%s",
            (function.id,),
        )[0][0]
        assert stored_payload["access_count"] == 2
    finally:
        resources.close()


def test_v5_required_capture_increment_access_batch_repeat_fid_produces_incrementing_payload_real_postgres(migration_dsn):
    resources, store = _required_capture_store(
        migration_dsn,
        local_node_id="func-batch-access-local",
    )
    first = _func(f"function-real-batch-1-{uuid.uuid4().hex}", "Batch Access 1")
    second = _func(f"function-real-batch-2-{uuid.uuid4().hex}", "Batch Access 2")
    missing_id = f"missing-{uuid.uuid4().hex}"
    try:
        store.add(first, SRC)
        store.add(second, SRC)
        pre_outbox = _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_sync_outbox",
        )[0][0]
        pre_first_stream = _admin_query(
            migration_dsn,
            "SELECT COALESCE(MAX(stream_seq), 0) FROM memplex_sync_outbox WHERE entity_key = %s",
            (str(SyncEntityKey.node(first.id)),),
        )[0][0]
        pre_second_stream = _admin_query(
            migration_dsn,
            "SELECT COALESCE(MAX(stream_seq), 0) FROM memplex_sync_outbox WHERE entity_key = %s",
            (str(SyncEntityKey.node(second.id)),),
        )[0][0]

        store.increment_access_batch([first.id, missing_id, first.id, second.id])

        first_key = str(SyncEntityKey.node(first.id))
        second_key = str(SyncEntityKey.node(second.id))
        assert _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_sync_outbox",
        )[0][0] == pre_outbox + 3
        first_payloads = _admin_query(
            migration_dsn,
            "SELECT payload, stream_seq FROM memplex_sync_outbox "
            "WHERE entity_key = %s AND stream_seq > %s ORDER BY stream_seq DESC",
            (first_key, pre_first_stream),
        )
        assert len(first_payloads) == 2
        assert first_payloads[0][0]["access_count"] == 2
        assert first_payloads[1][0]["access_count"] == 1
        second_payloads = _admin_query(
            migration_dsn,
            "SELECT payload, stream_seq FROM memplex_sync_outbox "
            "WHERE entity_key = %s AND stream_seq > %s ORDER BY stream_seq DESC",
            (second_key, pre_second_stream),
        )
        assert len(second_payloads) == 1
        assert second_payloads[0][0]["access_count"] == 1

        first_payload = _admin_query(
            migration_dsn,
            "SELECT data FROM memplex_functions WHERE id=%s",
            (first.id,),
        )[0][0]
        second_payload = _admin_query(
            migration_dsn,
            "SELECT data FROM memplex_functions WHERE id=%s",
            (second.id,),
        )[0][0]
        assert first_payload["access_count"] == 2
        assert second_payload["access_count"] == 1
    finally:
        resources.close()


def test_v5_required_capture_clear_rolls_back_all_entities_on_changelog_fault_real_postgres(migration_dsn):
    resources, store = _required_capture_store(
        migration_dsn,
        local_node_id="clear-failure-local",
    )
    function = _func(f"function-real-clear-fail-{uuid.uuid4().hex}", "Clear Rollback")
    linked = _func(f"function-real-clear-linked-{uuid.uuid4().hex}", "Clear Linked")
    fact = _build_typed_node("add_fact", function.id)
    pref = _build_typed_node("add_preference", function.id)
    obs = _build_typed_node("add_observation", function.id)
    fail_trigger = f"task4_capture_clear_fail_{uuid.uuid4().hex[:8]}"
    fail_fn = f"{fail_trigger}_fn"

    try:
        store.add(function, SRC)
        store.add(linked, SRC)
        store.merge(
            GraphData(
                nodes=[],
                edges=[
                    GraphEdge(function.id, linked.id, "REFERENCES"),
                    GraphEdge(linked.id, function.id, "SUPPORTED_BY"),
                ],
            )
        )
        store.add_fact(fact)
        store.add_preference(pref)
        store.add_observation(obs)

        pre_outbox = _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_sync_outbox",
        )[0][0]
        pre_function_rows = _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_functions WHERE tenant_id = %s",
            (store._authorization_context().principal.tenant_id,),
        )[0][0]
        pre_edge_rows = _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_edges",
        )[0][0]
        pre_observations = _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_observations WHERE tenant_id = %s",
            (store._authorization_context().principal.tenant_id,),
        )[0][0]

        _admin_execute(
            migration_dsn,
            f"""
            CREATE OR REPLACE FUNCTION {fail_fn}() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'clear required capture rollback check';
            END;
            $$;
            CREATE TRIGGER {fail_trigger}
            BEFORE DELETE ON memplex_functions
            FOR EACH ROW EXECUTE FUNCTION {fail_fn}();
            """,
        )

        with pytest.raises(psycopg2.Error, match="clear required capture rollback check"):
            store.clear()

        assert _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_functions WHERE tenant_id = %s",
            (store._authorization_context().principal.tenant_id,),
        )[0][0] == pre_function_rows
        assert _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_edges",
        )[0][0] == pre_edge_rows
        assert _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_observations WHERE tenant_id = %s",
            (store._authorization_context().principal.tenant_id,),
        )[0][0] == pre_observations
        assert _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_sync_outbox",
        )[0][0] == pre_outbox
    finally:
        _admin_execute(
            migration_dsn,
            f"DROP TRIGGER IF EXISTS {fail_trigger} ON memplex_functions;"
            f" DROP FUNCTION IF EXISTS {fail_fn}();",
        )
        resources.close()


def test_v5_required_capture_clear_tombstones_every_entity_real_postgres(
    migration_dsn,
):
    resources, store = _required_capture_store(
        migration_dsn,
        local_node_id="clear-success-local",
    )
    left = _func(f"clear-success-left-{uuid.uuid4().hex}", "Clear Left")
    right = _func(f"clear-success-right-{uuid.uuid4().hex}", "Clear Right")
    fact = _build_typed_node("add_fact", f"clear-fact-{uuid.uuid4().hex}")
    pref = _build_typed_node("add_preference", f"clear-pref-{uuid.uuid4().hex}")
    obs = _build_typed_node("add_observation", f"clear-obs-{uuid.uuid4().hex}")
    tenant_id = store._authorization_context().principal.tenant_id
    try:
        store.add(left, SRC)
        store.add(right, SRC)
        store.merge(
            GraphData(
                nodes=[],
                edges=[GraphEdge(left.id, right.id, "REFERENCES")],
            )
        )
        store.add_fact(fact)
        store.add_preference(pref)
        store.add_observation(obs)
        pre_stream_seq = _admin_query(
            migration_dsn,
            "SELECT COALESCE(MAX(stream_seq), 0) FROM memplex_sync_outbox",
        )[0][0]

        store.clear()

        for table in (
            "memplex_functions",
            "memplex_edges",
            "memplex_facts",
            "memplex_preferences",
            "memplex_observations",
        ):
            assert _admin_query(
                migration_dsn,
                f"SELECT count(*) FROM {table} WHERE tenant_id = %s",
                (tenant_id,),
            ) == [(0,)]
        new_events = _admin_query(
            migration_dsn,
            "SELECT node_type, operation FROM memplex_sync_outbox "
            "WHERE tenant_id = %s AND stream_seq > %s ORDER BY stream_seq",
            (tenant_id, pre_stream_seq),
        )
        assert sorted(node_type for node_type, _operation in new_events) == [
            "edge",
            "fact",
            "function",
            "function",
            "observation",
            "preference",
        ]
        assert {operation for _node_type, operation in new_events} == {
            "tombstone"
        }
    finally:
        resources.close()


def test_v5_sync_repository_page_delivery_and_lease_lifecycle_real_postgres(
    migration_dsn,
):
    """Least-privileged application role can use the durable repository."""
    app_role = f"memplex_sync_repo_{uuid.uuid4().hex[:8]}"
    local_node_id = f"repo-local-{uuid.uuid4().hex[:8]}"
    target_id = f"repo-remote-{uuid.uuid4().hex[:8]}"
    tenant_id = f"tenant-repo-{uuid.uuid4().hex[:8]}"
    PostgresMigrationRunner(migration_dsn).apply()
    _admin_execute(
        migration_dsn,
        "SELECT memplex_configure_sync_local_identity(%s)",
        (local_node_id,),
    )
    _provision_application_role(migration_dsn, app_role)
    app_dsn = psycopg2.extensions.make_dsn(migration_dsn, user=app_role)
    resources = PostgresStorageResources(
        dsn=app_dsn,
        migration_dsn=migration_dsn,
    )
    try:
        resources.ensure_ready(
            VectorCapabilityRequest(dim=0, policy="disabled"),
            "production",
        )
        store = PostgresMemoryStore(
            dsn=app_dsn,
            ready_pool=resources.ready_pool,
            require_authorization=True,
            sync_capture_policy=SyncCapturePolicy(
                "required", local_node_id=local_node_id
            ),
            sync_max_attempts=1,
        )
        scoped = store.authorized(
            _authorization(
                tenant=tenant_id,
                subject="repo-owner",
                workspace="repo-workspace",
            )
        )
        scoped.sync_register_target(target_id, bootstrap="future")
        function = _func(
            f"repo-function-{uuid.uuid4().hex}",
            "Repository Function",
        )
        scoped.add(function, SRC)

        with ThreadPoolExecutor(max_workers=2) as executor:
            claimed = list(
                executor.map(
                    lambda _: scoped.sync_claim(
                        target_id, limit=1, lease_seconds=30
                    ),
                    range(2),
                )
            )
        deliveries = [item for group in claimed for item in group]
        assert len(deliveries) == 1
        delivery = deliveries[0]
        assert scoped.sync_status().leased == 1

        scoped.sync_fail(delivery, "remote_unavailable", datetime.now(timezone.utc))
        status = scoped.sync_status()
        assert status.dead_letters == 1
        assert status.pending == 0
        assert scoped.sync_replay_dead_letter(
            target_id, delivery.event.event_id
        )
        replayed = scoped.sync_claim(target_id, limit=1, lease_seconds=30)
        assert len(replayed) == 1
        scoped.sync_ack(
            replayed[0],
            SyncReceipt(replayed[0].event.event_id, "accepted"),
        )
        assert scoped.sync_status().delivered == 1

        consumer_id = local_node_id
        remote_id = "page-source"
        first_page = scoped.sync_page(remote_id, consumer_id, None, 1)
        assert first_page.items
        assert _admin_query(
            migration_dsn,
            "SELECT after_seq FROM memplex_sync_cursors "
            "WHERE tenant_id=%s AND remote_id=%s AND consumer_id=%s",
            (tenant_id, remote_id, consumer_id),
        ) == [(0,)]
        now = datetime.now(timezone.utc)
        confirmed = SyncCursorClaims(
            1,
            "test-key",
            tenant_id,
            remote_id,
            consumer_id,
            first_page.next_after_seq,
            first_page.snapshot_seq,
            None,
            None,
            now,
            now + timedelta(minutes=5),
        )
        scoped.sync_page(remote_id, consumer_id, confirmed, 1)
        assert _admin_query(
            migration_dsn,
            "SELECT after_seq FROM memplex_sync_cursors "
            "WHERE tenant_id=%s AND remote_id=%s AND consumer_id=%s",
            (tenant_id, remote_id, consumer_id),
        ) == [(first_page.next_after_seq,)]

        second_function = _func(
            f"repo-snapshot-b-{uuid.uuid4().hex}",
            "Repository Snapshot B",
        )
        scoped.add(second_function, SRC)
        snapshot_page = scoped.sync_create_snapshot(
            remote_id,
            consumer_id,
            "snapshot-request-1",
            1,
        )
        assert snapshot_page.snapshot_id
        assert snapshot_page.has_more is True
        assert snapshot_page.next_anchor is not None
        same_snapshot = scoped.sync_create_snapshot(
            remote_id,
            consumer_id,
            "snapshot-request-1",
            1,
        )
        assert same_snapshot.snapshot_id == snapshot_page.snapshot_id
        with pytest.raises(SyncBackpressureError, match="snapshot_in_progress"):
            scoped.sync_create_snapshot(
                remote_id,
                f"other-consumer-{uuid.uuid4().hex}",
                "snapshot-request-other-consumer",
                1,
            )

        post_snapshot_function = _func(
            f"repo-snapshot-after-{uuid.uuid4().hex}",
            "Repository Snapshot After",
        )
        scoped.add(post_snapshot_function, SRC)
        snapshot_now = datetime.now(timezone.utc)
        snapshot_cursor = SyncCursorClaims(
            1,
            "test-key",
            tenant_id,
            remote_id,
            consumer_id,
            0,
            snapshot_page.resume_seq,
            snapshot_page.snapshot_id,
            snapshot_page.next_anchor,
            snapshot_now,
            snapshot_now + timedelta(minutes=5),
        )
        snapshot_tail = scoped.sync_snapshot_page(
            remote_id,
            consumer_id,
            snapshot_cursor,
            10,
        )
        assert snapshot_tail.snapshot_id == snapshot_page.snapshot_id
        snapshotted_ids = {
            event.payload["id"]
            for event in snapshot_page.events + snapshot_tail.events
            if event.payload is not None
        }
        assert function.id in snapshotted_ids
        assert second_function.id in snapshotted_ids
        assert post_snapshot_function.id not in snapshotted_ids

        compacted_through = _admin_query(
            migration_dsn,
            "SELECT max(stream_seq) FROM memplex_sync_outbox WHERE tenant_id=%s",
            (tenant_id,),
        )[0][0]
        _admin_execute(
            migration_dsn,
            "DELETE FROM memplex_sync_snapshots WHERE tenant_id=%s; "
            "INSERT INTO memplex_sync_stream_state "
            "(tenant_id, retention_floor, compacted_through) VALUES (%s, %s, %s) "
            "ON CONFLICT (tenant_id) DO UPDATE SET "
            "retention_floor=EXCLUDED.retention_floor, "
            "compacted_through=EXCLUDED.compacted_through; "
            "DELETE FROM memplex_sync_outbox "
            "WHERE tenant_id=%s AND stream_seq <= %s",
            (
                tenant_id,
                tenant_id,
                compacted_through,
                compacted_through,
                tenant_id,
                compacted_through,
            ),
        )
        post_compaction_snapshot = scoped.sync_create_snapshot(
            remote_id,
            consumer_id,
            "snapshot-request-after-compaction",
            10,
        )
        assert post_compaction_snapshot.resume_seq == compacted_through
        assert {
            event.payload["id"]
            for event in post_compaction_snapshot.events
            if event.payload is not None
        } == {function.id, second_function.id, post_snapshot_function.id}

        _admin_execute(
            migration_dsn,
            "DELETE FROM memplex_sync_snapshots WHERE tenant_id=%s",
            (tenant_id,),
        )
        admission_barrier = Barrier(2)

        def create_competing_snapshot(index: int) -> str:
            admission_barrier.wait(timeout=10)
            try:
                scoped.sync_create_snapshot(
                    remote_id,
                    f"competing-consumer-{index}",
                    f"competing-request-{index}",
                    10,
                )
            except SyncBackpressureError as exc:
                return str(exc)
            return "created"

        with ThreadPoolExecutor(max_workers=2) as executor:
            admission_results = list(executor.map(create_competing_snapshot, range(2)))
        assert sorted(admission_results) == ["created", "snapshot_in_progress"]
        assert _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_sync_snapshots WHERE tenant_id=%s AND remote_id=%s",
            (tenant_id, remote_id),
        ) == [(1,)]
    finally:
        resources.close()
        _drop_unprivileged_role(migration_dsn, app_role)


def test_v5_dispatcher_ack_loss_restart_retries_identical_batch_real_postgres(
    migration_dsn,
):
    app_role = f"memplex_dispatch_{uuid.uuid4().hex[:8]}"
    local_node_id = f"dispatch-local-{uuid.uuid4().hex[:8]}"
    target_id = f"dispatch-remote-{uuid.uuid4().hex[:8]}"
    tenant_id = f"tenant-dispatch-{uuid.uuid4().hex[:8]}"
    PostgresMigrationRunner(migration_dsn).apply()
    _admin_execute(
        migration_dsn,
        "SELECT memplex_configure_sync_local_identity(%s)",
        (local_node_id,),
    )
    _provision_application_role(migration_dsn, app_role)
    app_dsn = psycopg2.extensions.make_dsn(migration_dsn, user=app_role)
    resources = PostgresStorageResources(
        dsn=app_dsn,
        migration_dsn=migration_dsn,
    )

    class Response:
        status_code = 200

        def __init__(self, body):
            self._raw = json.dumps(
                body, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")

        def iter_content(self, chunk_size):
            del chunk_size
            yield self._raw

        def close(self):
            return None

    class AckLossHttp:
        def __init__(self):
            self.requests = []

        def post(self, url, *, data, headers, timeout, stream):
            del url, headers, timeout
            assert stream is True
            self.requests.append(data)
            batch = SyncBatch.from_dict(json.loads(data))
            if len(self.requests) == 1:
                raise TimeoutError("remote committed before response loss")
            return Response(
                {
                    "batch_id": batch.batch_id,
                    "request_digest": batch.request_digest,
                    "outcome": "duplicate",
                    "receipts": [
                        {
                            "event_id": event.event_id,
                            "outcome": "duplicate",
                        }
                        for event in batch.events
                    ],
                }
            )

    try:
        resources.ensure_ready(
            VectorCapabilityRequest(dim=0, policy="disabled"),
            "production",
        )
        store = PostgresMemoryStore(
            dsn=app_dsn,
            ready_pool=resources.ready_pool,
            require_authorization=True,
            sync_capture_policy=SyncCapturePolicy(
                "required", local_node_id=local_node_id
            ),
        )
        scoped = store.authorized(
            _authorization(
                tenant=tenant_id,
                subject="dispatch-owner",
                workspace="dispatch-workspace",
            )
        )
        scoped.sync_register_target(target_id, bootstrap="future")
        function = _func(
            f"dispatch-function-{uuid.uuid4().hex}",
            "Dispatcher ACK Loss",
        )
        scoped.add(function, SRC)
        http = AckLossHttp()

        first = SyncDispatcher(
            scoped,
            targets={target_id: "https://remote.example"},
            local_node_id=local_node_id,
            http=http,
            max_in_flight=1,
        ).dispatch_once()
        assert first.failed == 1
        assert scoped.sync_dispatch_status().pending == 1
        _admin_execute(
            migration_dsn,
            "UPDATE memplex_sync_deliveries SET next_attempt_at=now() "
            "WHERE tenant_id=%s AND target_id=%s",
            (tenant_id, target_id),
        )

        second = SyncDispatcher(
            scoped,
            targets={target_id: "https://remote.example"},
            local_node_id=local_node_id,
            http=http,
            max_in_flight=1,
        ).dispatch_once()

        assert second.delivered == 1
        assert http.requests[0] == http.requests[1]
        assert scoped.sync_dispatch_status().delivered == 1
        assert _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_sync_outbox "
            "WHERE tenant_id=%s AND entity_key=%s",
            (tenant_id, str(SyncEntityKey.node(function.id))),
        ) == [(1,)]
    finally:
        resources.close()
        _drop_unprivileged_role(migration_dsn, app_role)


def test_v5_sync_apply_page_is_atomic_mixed_origin_and_relay_safe_real_postgres(
    migration_dsn,
):
    """One application transaction applies, relays and advances a mixed page."""
    app_role = f"memplex_sync_page_{uuid.uuid4().hex[:8]}"
    local_node_id = f"page-local-{uuid.uuid4().hex[:8]}"
    tenant_id = f"tenant-page-{uuid.uuid4().hex[:8]}"
    remote_id = "relay-source"
    PostgresMigrationRunner(migration_dsn).apply()
    _admin_execute(
        migration_dsn,
        "SELECT memplex_configure_sync_local_identity(%s)",
        (local_node_id,),
    )
    _provision_application_role(migration_dsn, app_role)
    app_dsn = psycopg2.extensions.make_dsn(migration_dsn, user=app_role)
    resources = PostgresStorageResources(dsn=app_dsn, migration_dsn=migration_dsn)

    def event(index: int, origin: str, node_id: str) -> SyncEvent:
        event_id = str(uuid.UUID(int=1000 + index))
        return SyncEvent(
            1,
            event_id,
            origin,
            SyncNodeType.FUNCTION,
            SyncEntityKey.node(node_id),
            SyncOperation.UPSERT,
            str(
                SyncVersion.create(
                    datetime(2026, 8, 11, 0, 0, index, tzinfo=timezone.utc),
                    origin,
                    event_id,
                )
            ),
            SyncScope(
                tenant_id,
                "page-owner",
                "page-workspace",
                "workspace",
                None,
                None,
            ),
            {"id": node_id},
        )

    try:
        resources.ensure_ready(
            VectorCapabilityRequest(dim=0, policy="disabled"),
            "production",
        )
        store = PostgresMemoryStore(
            dsn=app_dsn,
            ready_pool=resources.ready_pool,
            require_authorization=True,
            sync_capture_policy=SyncCapturePolicy(
                "required", local_node_id=local_node_id
            ),
        )
        scoped = store.authorized(
            _authorization(
                tenant=tenant_id,
                subject="page-owner",
                workspace="page-workspace",
            )
        )
        scoped.sync_register_target("origin-a", bootstrap="future")
        scoped.sync_register_target("downstream", bootstrap="future")
        first = event(1, "origin-a", "mixed-a")
        second = event(2, "origin-b", "mixed-b")
        page = SyncPage(
            (SyncStreamItem(10, first), SyncStreamItem(12, second)),
            12,
            12,
            False,
        )

        result = scoped.sync_apply_page(remote_id, page)
        assert result.to_dict() == {
            "applied": 2,
            "duplicate": 0,
            "conflict": 0,
            "cursor_advanced": 12,
        }
        assert _admin_query(
            migration_dsn,
            "SELECT id FROM memplex_functions WHERE tenant_id=%s ORDER BY id",
            (tenant_id,),
        ) == [("mixed-a",), ("mixed-b",)]
        assert _admin_query(
            migration_dsn,
            "SELECT origin_node_id FROM memplex_sync_outbox "
            "WHERE tenant_id=%s ORDER BY stream_seq",
            (tenant_id,),
        ) == [("origin-a",), ("origin-b",)]
        assert _admin_query(
            migration_dsn,
            "SELECT target_id, outbox.origin_node_id "
            "FROM memplex_sync_deliveries delivery "
            "JOIN memplex_sync_outbox outbox USING (tenant_id, stream_seq) "
            "WHERE delivery.tenant_id=%s ORDER BY outbox.stream_seq, target_id",
            (tenant_id,),
        ) == [
            ("downstream", "origin-a"),
            ("downstream", "origin-b"),
            ("origin-a", "origin-b"),
        ]
        assert _admin_query(
            migration_dsn,
            "SELECT after_seq FROM memplex_sync_cursors "
            "WHERE tenant_id=%s AND remote_id=%s AND consumer_id=%s",
            (tenant_id, remote_id, local_node_id),
        ) == [(12,)]

        # Inbound events remain retained for downstream pull, but the local
        # dispatcher must never impersonate their remote origins.
        assert scoped.sync_claim("downstream", limit=10, lease_seconds=30) == []
        downstream_consumer = "downstream-consumer"
        downstream_page = scoped.sync_page(
            "downstream", downstream_consumer, None, 10
        )
        assert [
            item.event.origin_node_id for item in downstream_page.items
        ] == ["origin-a", "origin-b"]
        now = datetime.now(timezone.utc)
        downstream_confirmed = SyncCursorClaims(
            1,
            "test-key",
            tenant_id,
            "downstream",
            downstream_consumer,
            downstream_page.next_after_seq,
            downstream_page.snapshot_seq,
            None,
            None,
            now,
            now + timedelta(minutes=5),
        )
        scoped.sync_page(
            "downstream", downstream_consumer, downstream_confirmed, 10
        )
        assert _admin_query(
            migration_dsn,
            "SELECT delivery.target_id, delivery.state "
            "FROM memplex_sync_deliveries AS delivery "
            "WHERE delivery.tenant_id=%s ORDER BY delivery.target_id, delivery.stream_seq",
            (tenant_id,),
        ) == [
            ("downstream", "delivered"),
            ("downstream", "delivered"),
            ("origin-a", "pending"),
        ]

        # A remote never receives its own origin back; confirming its pull
        # releases only the remaining cross-origin retention pin.
        origin_consumer = "origin-a-consumer"
        origin_page = scoped.sync_page("origin-a", origin_consumer, None, 10)
        assert [item.event.origin_node_id for item in origin_page.items] == [
            "origin-b"
        ]
        origin_confirmed = SyncCursorClaims(
            1,
            "test-key",
            tenant_id,
            "origin-a",
            origin_consumer,
            origin_page.next_after_seq,
            origin_page.snapshot_seq,
            None,
            None,
            now,
            now + timedelta(minutes=5),
        )
        scoped.sync_page("origin-a", origin_consumer, origin_confirmed, 10)
        assert _admin_query(
            migration_dsn,
            "SELECT state FROM memplex_sync_deliveries "
            "WHERE tenant_id=%s AND target_id='origin-a'",
            (tenant_id,),
        ) == [("delivered",)]

        replay = scoped.sync_apply_page(remote_id, page)
        assert replay.applied == 0
        assert replay.duplicate == 2
        assert replay.cursor_advanced == 12
        assert _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_sync_outbox WHERE tenant_id=%s",
            (tenant_id,),
        ) == [(2,)]

        overlap = SyncPage(
            (
                SyncStreamItem(12, event(3, "origin-a", "overlap-a")),
                SyncStreamItem(14, event(4, "origin-b", "overlap-b")),
            ),
            14,
            14,
            False,
        )
        with pytest.raises(ValueError, match="partially overlaps"):
            scoped.sync_apply_page(remote_id, overlap)
        assert _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_functions "
            "WHERE tenant_id=%s AND id LIKE 'overlap-%%'",
            (tenant_id,),
        ) == [(0,)]

        concurrent_remote = "concurrent-relay-source"
        concurrent_pages = (
            SyncPage(
                (
                    SyncStreamItem(10, event(5, "origin-a", "concurrent-a-1")),
                    SyncStreamItem(12, event(6, "origin-b", "concurrent-a-2")),
                ),
                12,
                12,
                False,
            ),
            SyncPage(
                (
                    SyncStreamItem(11, event(7, "origin-a", "concurrent-b-1")),
                    SyncStreamItem(13, event(8, "origin-b", "concurrent-b-2")),
                ),
                13,
                13,
                False,
            ),
        )
        apply_barrier = Barrier(2)

        def apply_competing_page(candidate: SyncPage) -> str:
            apply_barrier.wait(timeout=10)
            try:
                scoped.sync_apply_page(concurrent_remote, candidate)
            except ValueError:
                return "rejected"
            return "applied"

        with ThreadPoolExecutor(max_workers=2) as executor:
            concurrent_results = list(
                executor.map(apply_competing_page, concurrent_pages)
            )
        assert sorted(concurrent_results) == ["applied", "rejected"]
        assert _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_functions "
            "WHERE tenant_id=%s AND id LIKE 'concurrent-%%'",
            (tenant_id,),
        ) == [(2,)]
        assert _admin_query(
            migration_dsn,
            "SELECT after_seq FROM memplex_sync_cursors "
            "WHERE tenant_id=%s AND remote_id=%s AND consumer_id=%s",
            (tenant_id, concurrent_remote, local_node_id),
        )[0][0] in {12, 13}

        fault_tenant = f"tenant-page-fault-{uuid.uuid4().hex[:8]}"
        fault_scoped = store.authorized(
            _authorization(
                tenant=fault_tenant,
                subject="page-owner",
                workspace="page-workspace",
            )
        )
        fault_fn = f"memplex_page_fault_{uuid.uuid4().hex[:8]}"
        fault_trigger = f"memplex_page_fault_trigger_{uuid.uuid4().hex[:8]}"
        _admin_execute(
            migration_dsn,
            f"""
            CREATE FUNCTION {fault_fn}() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                IF NEW.id = 'fault-b' THEN
                    RAISE EXCEPTION 'apply page injected failure';
                END IF;
                RETURN NEW;
            END;
            $$;
            CREATE TRIGGER {fault_trigger}
            BEFORE INSERT ON memplex_functions
            FOR EACH ROW EXECUTE FUNCTION {fault_fn}();
            """,
        )
        try:
            fault_first = SyncEvent(
                1,
                str(uuid.UUID(int=2001)),
                "origin-a",
                SyncNodeType.FUNCTION,
                SyncEntityKey.node("fault-a"),
                SyncOperation.UPSERT,
                str(
                    SyncVersion.create(
                        datetime(2026, 8, 11, 0, 1, 1, tzinfo=timezone.utc),
                        "origin-a",
                        str(uuid.UUID(int=2001)),
                    )
                ),
                SyncScope(
                    fault_tenant,
                    "page-owner",
                    "page-workspace",
                    "workspace",
                    None,
                    None,
                ),
                {"id": "fault-a"},
            )
            fault_second = SyncEvent(
                1,
                str(uuid.UUID(int=2002)),
                "origin-b",
                SyncNodeType.FUNCTION,
                SyncEntityKey.node("fault-b"),
                SyncOperation.UPSERT,
                str(
                    SyncVersion.create(
                        datetime(2026, 8, 11, 0, 1, 2, tzinfo=timezone.utc),
                        "origin-b",
                        str(uuid.UUID(int=2002)),
                    )
                ),
                SyncScope(
                    fault_tenant,
                    "page-owner",
                    "page-workspace",
                    "workspace",
                    None,
                    None,
                ),
                {"id": "fault-b"},
            )
            fault_page = SyncPage(
                (
                    SyncStreamItem(1, fault_first),
                    SyncStreamItem(2, fault_second),
                ),
                2,
                2,
                False,
            )
            with pytest.raises(psycopg2.Error, match="apply page injected failure"):
                fault_scoped.sync_apply_page(remote_id, fault_page)
        finally:
            _admin_execute(
                migration_dsn,
                f"DROP TRIGGER IF EXISTS {fault_trigger} ON memplex_functions; "
                f"DROP FUNCTION IF EXISTS {fault_fn}();",
            )
        for relation in (
            "memplex_functions",
            "memplex_sync_inbox",
            "memplex_sync_entity_versions",
            "memplex_sync_outbox",
            "memplex_sync_deliveries",
            "memplex_sync_cursors",
        ):
            assert _admin_query(
                migration_dsn,
                f"SELECT count(*) FROM {relation} WHERE tenant_id=%s",
                (fault_tenant,),
            ) == [(0,)]
    finally:
        resources.close()
        _drop_unprivileged_role(migration_dsn, app_role)


def test_v5_sync_compact_preserves_cursor_snapshot_and_dead_letter_pins_real_postgres(
    migration_dsn,
):
    """Compaction advances only one old, delivered, continuously safe prefix."""
    app_role = f"memplex_sync_compact_{uuid.uuid4().hex[:8]}"
    local_node_id = f"compact-local-{uuid.uuid4().hex[:8]}"
    target_id = f"compact-target-{uuid.uuid4().hex[:8]}"
    tenant_id = f"tenant-compact-{uuid.uuid4().hex[:8]}"
    PostgresMigrationRunner(migration_dsn).apply()
    _admin_execute(
        migration_dsn,
        "SELECT memplex_configure_sync_local_identity(%s)",
        (local_node_id,),
    )
    _provision_application_role(migration_dsn, app_role)
    app_dsn = psycopg2.extensions.make_dsn(migration_dsn, user=app_role)
    resources = PostgresStorageResources(dsn=app_dsn, migration_dsn=migration_dsn)
    try:
        resources.ensure_ready(
            VectorCapabilityRequest(dim=0, policy="disabled"),
            "production",
        )
        store = PostgresMemoryStore(
            dsn=app_dsn,
            ready_pool=resources.ready_pool,
            require_authorization=True,
            sync_capture_policy=SyncCapturePolicy(
                "required", local_node_id=local_node_id
            ),
            sync_max_attempts=1,
            sync_consumer_ttl_seconds=1,
            sync_retention_min_seconds=1,
        )
        scoped = store.authorized(
            _authorization(
                tenant=tenant_id,
                subject="compact-owner",
                workspace="compact-workspace",
            )
        )
        scoped.sync_register_target(target_id, bootstrap="future")
        for index in range(3):
            scoped.add(
                _func(
                    f"compact-function-{index}-{uuid.uuid4().hex}",
                    f"Compact Function {index}",
                ),
                SRC,
            )
        deliveries = scoped.sync_claim(target_id, limit=10, lease_seconds=30)
        assert len(deliveries) == 3
        for delivery in deliveries:
            scoped.sync_ack(delivery, SyncReceipt(delivery.event.event_id, "accepted"))
        stream_seqs = [
            row[0]
            for row in _admin_query(
                migration_dsn,
                "SELECT stream_seq FROM memplex_sync_outbox "
                "WHERE tenant_id=%s ORDER BY stream_seq",
                (tenant_id,),
            )
        ]
        _admin_execute(
            migration_dsn,
            "UPDATE memplex_sync_outbox SET created_at=clock_timestamp()-interval '2 seconds' "
            "WHERE tenant_id=%s",
            (tenant_id,),
        )
        now = datetime.now(timezone.utc)
        cursor = SyncCursorClaims(
            1,
            "test-key",
            tenant_id,
            "compact-reader",
            "compact-consumer",
            stream_seqs[1],
            stream_seqs[2],
            None,
            None,
            now,
            now + timedelta(minutes=5),
        )
        scoped.sync_page("compact-reader", "compact-consumer", cursor, 10)
        assert scoped.sync_compact(datetime.now(timezone.utc), limit=10) == 2
        assert _admin_query(
            migration_dsn,
            "SELECT stream_seq FROM memplex_sync_outbox WHERE tenant_id=%s ORDER BY stream_seq",
            (tenant_id,),
        ) == [(stream_seqs[2],)]

        _admin_execute(
            migration_dsn,
            "UPDATE memplex_sync_cursors SET updated_at=clock_timestamp()-interval '2 seconds' "
            "WHERE tenant_id=%s",
            (tenant_id,),
        )
        snapshot = scoped.sync_create_snapshot(
            "compact-snapshot-remote",
            "compact-snapshot-consumer",
            "compact-snapshot-request",
            10,
        )
        assert snapshot.resume_seq == stream_seqs[2]
        fourth = _func(
            f"compact-function-3-{uuid.uuid4().hex}",
            "Compact Function 3",
        )
        scoped.add(fourth, SRC)
        fourth_delivery = scoped.sync_claim(target_id, limit=1, lease_seconds=30)[0]
        scoped.sync_ack(
            fourth_delivery,
            SyncReceipt(fourth_delivery.event.event_id, "accepted"),
        )
        fourth_stream_seq = _admin_query(
            migration_dsn,
            "SELECT stream_seq FROM memplex_sync_outbox "
            "WHERE tenant_id=%s AND event_id=%s",
            (tenant_id, fourth_delivery.event.event_id),
        )[0][0]
        _admin_execute(
            migration_dsn,
            "UPDATE memplex_sync_outbox SET created_at=clock_timestamp()-interval '2 seconds' "
            "WHERE tenant_id=%s",
            (tenant_id,),
        )
        assert scoped.sync_compact(datetime.now(timezone.utc), limit=10) == 1
        assert _admin_query(
            migration_dsn,
            "SELECT stream_seq FROM memplex_sync_outbox WHERE tenant_id=%s ORDER BY stream_seq",
            (tenant_id,),
        ) == [(fourth_stream_seq,)]
        _admin_execute(
            migration_dsn,
            "UPDATE memplex_sync_snapshots SET expires_at=clock_timestamp()-interval '1 second' "
            "WHERE tenant_id=%s",
            (tenant_id,),
        )
        assert scoped.sync_compact(datetime.now(timezone.utc), limit=10) == 1

        fifth = _func(
            f"compact-function-4-{uuid.uuid4().hex}",
            "Compact Function 4",
        )
        scoped.add(fifth, SRC)
        fifth_delivery = scoped.sync_claim(target_id, limit=1, lease_seconds=30)[0]
        scoped.sync_fail(fifth_delivery, "permanent", datetime.now(timezone.utc))
        fifth_stream_seq = _admin_query(
            migration_dsn,
            "SELECT stream_seq FROM memplex_sync_outbox "
            "WHERE tenant_id=%s AND event_id=%s",
            (tenant_id, fifth_delivery.event.event_id),
        )[0][0]
        _admin_execute(
            migration_dsn,
            "UPDATE memplex_sync_outbox SET created_at=clock_timestamp()-interval '2 seconds' "
            "WHERE tenant_id=%s",
            (tenant_id,),
        )
        assert scoped.sync_compact(datetime.now(timezone.utc), limit=10) == 0
        scoped.sync_set_target_enabled(target_id, False)
        assert scoped.sync_compact(datetime.now(timezone.utc), limit=10) == 1
        assert _admin_query(
            migration_dsn,
            "SELECT retention_floor, compacted_through FROM memplex_sync_stream_state "
            "WHERE tenant_id=%s",
            (tenant_id,),
        ) == [(fifth_stream_seq, fifth_stream_seq)]
        assert _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_sync_outbox WHERE tenant_id=%s",
            (tenant_id,),
        ) == [(0,)]
        expired_now = datetime.now(timezone.utc)
        expired_cursor = SyncCursorClaims(
            1,
            "test-key",
            tenant_id,
            "expired-reader",
            "expired-consumer",
            fifth_stream_seq - 1,
            fifth_stream_seq,
            None,
            None,
            expired_now,
            expired_now + timedelta(minutes=5),
        )
        with pytest.raises(SyncCursorExpired):
            scoped.sync_page(
                "expired-reader",
                "expired-consumer",
                expired_cursor,
                10,
            )
    finally:
        resources.close()
        _drop_unprivileged_role(migration_dsn, app_role)


def test_v5_quota_rejects_enabled_target_fanout_before_any_write(migration_dsn):
    """A single local event cannot create more than the pending-delivery quota."""
    PostgresMigrationRunner(migration_dsn).apply()
    _migration_execute(
        migration_dsn, "SELECT memplex_configure_sync_local_identity('quota-local-node')"
    )
    _migration_execute(
        migration_dsn,
        """
        INSERT INTO memplex_sync_targets (tenant_id, target_id, remote_node_id, bootstrap_seq)
        SELECT 'tenant-quota', 'target-' || value, 'remote-' || value, 0
        FROM generate_series(1, 100001) AS value
        """,
    )
    conn = psycopg2.connect(migration_dsn)
    try:
        cursor = conn.cursor()
        try:
            context = {
                "memplex.sync_capture": "required",
                "memplex.sync_apply_mode": "local",
                "memplex.tenant_id": "tenant-quota",
                "memplex.subject_id": "subject-quota",
                "memplex.sync_origin_node_id": "quota-local-node",
                "memplex.sync_event_id": "123e4567-e89b-42d3-a456-426614174000",
                "memplex.sync_version_key": "v1:dmVyc2lvbg",
                "memplex.sync_entity_key": "node:v1:ZnVub3V0",
                "memplex.sync_payload": "{}",
            }
            for key, value in context.items():
                cursor.execute("SELECT set_config(%s, %s, true)", (key, value))
            with pytest.raises(psycopg2.Error, match="pending delivery quota exceeded"):
                cursor.execute(
                    """
                    INSERT INTO memplex_functions
                        (id, data, tenant_id, owner_subject, workspace, visibility, source_agent, source_session)
                    VALUES ('quota-rejected', '{}', 'tenant-quota', 'subject-quota',
                            'workspace-quota', 'user', 'agent-quota', 'session-quota')
                    """
                )
        finally:
            conn.rollback()
            cursor.close()
    finally:
        conn.close()
    assert _admin_query(migration_dsn, "SELECT count(*) FROM memplex_functions") == [(0,)]
    assert _admin_query(migration_dsn, "SELECT count(*) FROM memplex_edges") == [(0,)]
    assert _admin_query(migration_dsn, "SELECT count(*) FROM memplex_changelog") == [(0,)]
    assert _admin_query(migration_dsn, "SELECT count(*) FROM memplex_sync_outbox") == [(0,)]
    assert _admin_query(migration_dsn, "SELECT count(*) FROM memplex_sync_deliveries") == [(0,)]
    assert _admin_query(
        migration_dsn, "SELECT last_value, is_called FROM memplex_sync_outbox_stream_seq_seq"
    ) == [(1, False)]


def test_v5_quota_gate_serializes_concurrent_local_writes(migration_dsn):
    """The advisory gate prevents two individually-valid writes from crossing 100000."""
    PostgresMigrationRunner(migration_dsn).apply()
    _migration_execute(
        migration_dsn, "SELECT memplex_configure_sync_local_identity('quota-local-node')"
    )
    _migration_execute(
        migration_dsn,
        """
        INSERT INTO memplex_sync_targets (tenant_id, target_id, remote_node_id, bootstrap_seq)
        VALUES ('tenant-concurrent-quota', 'target-a', 'remote-a', 0),
               ('tenant-concurrent-quota', 'target-b', 'remote-b', 0);
        INSERT INTO memplex_sync_outbox
            (tenant_id, stream_seq, event_id, origin_node_id, node_type, entity_key,
             operation, version_key, payload, visibility, owner_subject_id, workspace_id)
        OVERRIDING SYSTEM VALUE
        SELECT 'tenant-concurrent-quota', 1000000 + value, 'seed-event-' || value,
               'seed-origin', 'function', 'seed-entity-' || value, 'upsert',
               'seed-version-' || value, '{}'::jsonb, 'user', 'seed-subject', 'seed-workspace'
        FROM generate_series(1, 99997) AS value;
        INSERT INTO memplex_sync_deliveries (tenant_id, target_id, stream_seq, state)
        SELECT 'tenant-concurrent-quota',
               CASE WHEN value % 2 = 0 THEN 'target-a' ELSE 'target-b' END,
               1000000 + value, 'pending'
        FROM generate_series(1, 99997) AS value
        """,
    )
    start = Barrier(3, timeout=10)

    def write(event_id: str, function_id: str) -> str:
        conn = psycopg2.connect(migration_dsn)
        try:
            cursor = conn.cursor()
            try:
                context = {
                    "memplex.sync_capture": "required",
                    "memplex.sync_apply_mode": "local",
                    "memplex.tenant_id": "tenant-concurrent-quota",
                    "memplex.subject_id": "subject-concurrent-quota",
                    "memplex.sync_origin_node_id": "quota-local-node",
                    "memplex.sync_event_id": event_id,
                    "memplex.sync_version_key": "v1:dmVyc2lvbg",
                    "memplex.sync_entity_key": "node:v1:ZnVuY3Rpb24",
                    "memplex.sync_payload": "{}",
                }
                for key, value in context.items():
                    cursor.execute("SELECT set_config(%s, %s, true)", (key, value))
                start.wait()
                try:
                    cursor.execute(
                        """
                        INSERT INTO memplex_functions
                            (id, data, tenant_id, owner_subject, workspace, visibility, source_agent, source_session)
                        VALUES (%s, '{}', 'tenant-concurrent-quota', 'subject-concurrent-quota',
                                'workspace-concurrent-quota', 'user', 'agent-quota', 'session-quota')
                        """,
                        (function_id,),
                    )
                    conn.commit()
                    return "committed"
                except psycopg2.Error as error:
                    conn.rollback()
                    assert "pending delivery quota exceeded" in str(error)
                    return "quota"
            finally:
                cursor.close()
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(write, "123e4567-e89b-42d3-a456-426614174000", "quota-first")
        second = executor.submit(write, "123e4567-e89b-42d3-a456-426614174001", "quota-second")
        start.wait()
        outcomes = {first.result(timeout=20), second.result(timeout=20)}
    assert outcomes == {"committed", "quota"}
    assert _admin_query(migration_dsn, "SELECT count(*) FROM memplex_functions") == [(1,)]
    assert _admin_query(migration_dsn, "SELECT count(*) FROM memplex_sync_outbox") == [(99998,)]
    assert _admin_query(migration_dsn, "SELECT count(*) FROM memplex_sync_deliveries") == [(99999,)]


@pytest.mark.parametrize(
    ("context_override", "expected_message"),
    (
        ({"memplex.sync_apply_mode": None}, "memplex sync context is incomplete"),
        ({"memplex.sync_origin_node_id": "forged-local"}, "memplex sync context is incomplete"),
        ({"memplex.sync_entity_key": "source:RELATED_TO:target"}, "memplex sync context is incomplete"),
        ({"memplex.sync_payload": "[]"}, "memplex sync context is incomplete"),
    ),
)
def test_v5_capture_required_rejects_missing_or_forged_local_context(
    migration_dsn, context_override, expected_message
):
    """Required capture uses NULL-safe validation and never invents protocol identity."""
    PostgresMigrationRunner(migration_dsn).apply()
    _migration_execute(
        migration_dsn, "SELECT memplex_configure_sync_local_identity('verified-local')"
    )
    context = {
        "memplex.sync_capture": "required",
        "memplex.sync_apply_mode": "local",
        "memplex.tenant_id": "tenant-capture",
        "memplex.subject_id": "subject-capture",
        "memplex.sync_origin_node_id": "verified-local",
        "memplex.sync_event_id": "123e4567-e89b-42d3-a456-426614174000",
        "memplex.sync_version_key": "v1:dmVyc2lvbg",
        "memplex.sync_entity_key": "node:v1:ZnVuY3Rpb24",
        "memplex.sync_payload": "{}",
    }
    context.update(context_override)
    conn = psycopg2.connect(migration_dsn)
    try:
        cursor = conn.cursor()
        try:
            for key, value in context.items():
                if value is not None:
                    cursor.execute("SELECT set_config(%s, %s, true)", (key, value))
            with pytest.raises(psycopg2.Error, match=expected_message):
                cursor.execute(
                    """
                    INSERT INTO memplex_functions
                        (id, data, tenant_id, owner_subject, workspace, visibility, source_agent, source_session)
                    VALUES ('rejected-context', '{}', 'tenant-capture', 'subject-capture',
                            'workspace-capture', 'user', 'agent-capture', 'session-capture')
                    """
                )
        finally:
            conn.rollback()
            cursor.close()
    finally:
        conn.close()


@pytest.mark.parametrize(
    "tamper_sql",
    (
        """
        CREATE OR REPLACE FUNCTION memplex_sync_capture_before() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER AS $$ BEGIN RETURN COALESCE(NEW, OLD); END; $$
        """,
        """
        CREATE OR REPLACE FUNCTION memplex_sync_assert_delivery_quota(
            quota_tenant_id TEXT, additional_deliveries BIGINT
        ) RETURNS void LANGUAGE plpgsql SECURITY DEFINER AS $$ BEGIN RETURN; END; $$
        """,
        "ALTER TABLE memplex_functions DISABLE TRIGGER memplex_sync_functions_before",
        """
        DROP TRIGGER memplex_sync_functions_before ON memplex_functions;
        CREATE TRIGGER memplex_sync_functions_before
        AFTER INSERT OR UPDATE OR DELETE ON memplex_functions
        FOR EACH ROW EXECUTE FUNCTION memplex_sync_capture_before()
        """,
        "ALTER TABLE memplex_sync_outbox DROP CONSTRAINT memplex_sync_outbox_operation_check",
        "ALTER TABLE memplex_sync_outbox ADD CONSTRAINT forged_sync_check CHECK (tenant_id <> 'forged')",
        """
        DROP POLICY memplex_sync_outbox_scope ON memplex_sync_outbox;
        CREATE POLICY memplex_sync_outbox_scope ON memplex_sync_outbox
        USING (true) WITH CHECK (true)
        """,
    ),
)
def test_v5_function_trigger_and_catalogue_definition_tampering_fails_closed(
    migration_dsn, tamper_sql
):
    """Readiness fingerprints exact hook semantics and every v5 table definition."""
    PostgresMigrationRunner(migration_dsn).apply()
    _migration_execute(migration_dsn, tamper_sql)
    with pytest.raises(MigrationIntegrityError, match="unrecognised legacy schema"):
        PostgresMigrationRunner(migration_dsn).plan()


@pytest.mark.parametrize(
    "tamper_sql",
    (
        "ALTER TABLE memplex_sync_outbox NO FORCE ROW LEVEL SECURITY",
        "DROP INDEX memplex_sync_deliveries_claim_idx",
        "ALTER TABLE memplex_sync_inbox DROP CONSTRAINT memplex_sync_inbox_pkey",
    ),
)
def test_v5_sync_catalogue_tamper_fails_closed(migration_dsn, tamper_sql):
    """Durable queue security and claim structures are readiness-critical."""
    PostgresMigrationRunner(migration_dsn).apply()
    _migration_execute(migration_dsn, tamper_sql)
    with pytest.raises(MigrationIntegrityError, match="unrecognised legacy schema"):
        PostgresMigrationRunner(migration_dsn).plan()


@pytest.mark.parametrize(
    "edge_sql",
    (
        """
        INSERT INTO memplex_edges
            (source, target, edge_type, tenant_id, owner_subject, workspace, visibility, source_agent, source_session)
        VALUES ('missing-source', 'virtual', 'BELONGS_TO', 'tenant', 'subject', 'workspace', 'workspace', 'agent', 'session')
        """,
        """
        INSERT INTO memplex_edges
            (source, target, edge_type, tenant_id, owner_subject, workspace, visibility, source_agent, source_session)
        VALUES ('source', 'missing-target', 'RELATED_TO', 'tenant', 'subject', 'workspace', 'workspace', 'agent', 'session')
        """,
    ),
)
def test_0004_rejects_dangling_legacy_edges_atomically(migration_dsn, edge_sql):
    """Failed validation cannot leave a half-added generated column or ledger row."""
    _install_v3_catalogue_with_executed_ledger(migration_dsn)
    _migration_execute(
        migration_dsn,
        """
        INSERT INTO memplex_functions
            (id, data, tenant_id, owner_subject, workspace, visibility, source_agent, source_session)
        VALUES ('source', '{}', 'tenant', 'subject', 'workspace', 'workspace', 'agent', 'session')
        """,
    )
    _migration_execute(migration_dsn, edge_sql)

    with pytest.raises((psycopg2.Error, MigrationIntegrityError)):
        PostgresMigrationRunner(migration_dsn).apply()

    assert _ledger_rows(migration_dsn) == [(1, "executed"), (2, "executed"), (3, "executed")]
    assert not _migration_column_exists(migration_dsn, "memplex_edges", "target_function")


@pytest.mark.parametrize(
    ("domain", "target"),
    (
        (None, "domain_missing"),
        ("", "domain_"),
        ("payments", "domain_wrong"),
    ),
)
def test_0004_rejects_invalid_legacy_belongs_to_targets_atomically(migration_dsn, domain, target):
    """A virtual edge must already obey the historical GraphBuilder node mapping."""
    _install_v3_catalogue_with_executed_ledger(migration_dsn)
    domain_json = "null" if domain is None else json.dumps(domain)
    _migration_execute(
        migration_dsn,
        f"""
        INSERT INTO memplex_functions
            (id, data, tenant_id, owner_subject, workspace, visibility, source_agent, source_session)
        VALUES
            ('source', '{{"domain": {domain_json}}}', 'tenant', 'subject', 'workspace', 'workspace', 'agent', 'session');
        INSERT INTO memplex_edges
            (source, target, edge_type, tenant_id, owner_subject, workspace, visibility, source_agent, source_session)
        VALUES
            ('source', '{target}', 'BELONGS_TO', 'tenant', 'subject', 'workspace', 'workspace', 'agent', 'session');
        """,
    )

    with pytest.raises(MigrationIntegrityError, match="BELONGS_TO"):
        PostgresMigrationRunner(migration_dsn).apply()

    assert _ledger_rows(migration_dsn) == [(1, "executed"), (2, "executed"), (3, "executed")]
    assert not _migration_column_exists(migration_dsn, "memplex_edges", "target_function")


@pytest.mark.parametrize(
    ("domain", "target"),
    (
        ("Payments  Core", "domain_payments__core"),
        ("中文 领域", "domain_中文_领域"),
    ),
)
def test_0004_accepts_legacy_belongs_to_graphbuilder_mapping(migration_dsn, domain, target):
    """Valid virtual endpoints preserve ASCII-space replacement and lowercase semantics."""
    _install_v3_catalogue_with_executed_ledger(migration_dsn)
    _migration_execute(
        migration_dsn,
        f"""
        INSERT INTO memplex_functions
            (id, data, tenant_id, owner_subject, workspace, visibility, source_agent, source_session)
        VALUES
            ('source', '{{"domain": {json.dumps(domain)}}}', 'tenant', 'subject', 'workspace', 'workspace', 'agent', 'session');
        INSERT INTO memplex_edges
            (source, target, edge_type, tenant_id, owner_subject, workspace, visibility, source_agent, source_session)
        VALUES
            ('source', '{target}', 'BELONGS_TO', 'tenant', 'subject', 'workspace', 'workspace', 'agent', 'session');
        """,
    )

    assert PostgresMigrationRunner(migration_dsn).apply().state == "ready"
    assert _admin_query(
        migration_dsn,
        "SELECT target_function FROM memplex_edges WHERE source = 'source'",
    ) == [(None,)]


def test_0004_rejects_legacy_reserved_domain_function_ids_atomically(migration_dsn):
    """The v4 function namespace check must validate every old row before ledger 4."""
    _install_v3_catalogue_with_executed_ledger(migration_dsn)
    _migration_execute(
        migration_dsn,
        """
        INSERT INTO memplex_functions
            (id, data, tenant_id, owner_subject, workspace, visibility, source_agent, source_session)
        VALUES ('domain_hidden_legacy', '{}', 'tenant', 'subject', 'workspace', 'workspace', 'agent', 'session')
        """,
    )

    with pytest.raises(psycopg2.errors.CheckViolation):
        PostgresMigrationRunner(migration_dsn).apply()

    assert _ledger_rows(migration_dsn) == [(1, "executed"), (2, "executed"), (3, "executed")]
    assert not _migration_column_exists(migration_dsn, "memplex_edges", "target_function")


def test_0004_rejects_force_rls_hidden_legacy_reserved_domain_function_id(migration_dsn):
    """The SQL CHECK validates a legacy virtual-node collision even for a blank-GUC owner."""
    _install_v3_catalogue_with_executed_ledger(migration_dsn)
    _migration_execute(
        migration_dsn,
        """
        INSERT INTO memplex_functions
            (id, data, tenant_id, owner_subject, workspace, visibility, source_agent, source_session)
        VALUES ('domain_hidden_legacy', '{}', 'tenant', 'subject', 'workspace', 'workspace', 'agent', 'session')
        """,
    )
    role = f"g003_domain_owner_{uuid.uuid4().hex}"
    original_owner = _handoff_v3_catalogue_to_non_superuser_owner(migration_dsn, role)
    owner_dsn = psycopg2.extensions.make_dsn(migration_dsn, user=role)
    try:
        assert _admin_query(owner_dsn, "SELECT count(*) FROM memplex_functions") == [(0,)]
        with pytest.raises(psycopg2.errors.CheckViolation):
            PostgresMigrationRunner(owner_dsn).apply()
        assert _force_rls_state(migration_dsn) == [
            ("memplex_edges", True),
            ("memplex_functions", True),
        ]
        assert _ledger_rows(migration_dsn) == [(1, "executed"), (2, "executed"), (3, "executed")]
        assert not _migration_column_exists(migration_dsn, "memplex_edges", "target_function")
    finally:
        _restore_v3_catalogue_owner_and_drop_role(migration_dsn, role, original_owner)


def test_v4_reserves_only_the_exact_lowercase_domain_function_namespace(migration_dsn):
    """The database rejects the virtual-node namespace on both insert and update."""
    PostgresMigrationRunner(migration_dsn).apply()
    _migration_execute(
        migration_dsn,
        """
        INSERT INTO memplex_functions
            (id, data, tenant_id, owner_subject, workspace, visibility, source_agent, source_session)
        VALUES ('DOMAIN_still_a_function', '{}', 'tenant', 'subject', 'workspace', 'workspace', 'agent', 'session');
        INSERT INTO memplex_functions
            (id, data, tenant_id, owner_subject, workspace, visibility, source_agent, source_session)
        VALUES ('ordinary', '{}', 'tenant', 'subject', 'workspace', 'workspace', 'agent', 'session')
        """,
    )
    with pytest.raises(psycopg2.errors.CheckViolation):
        _migration_execute(
            migration_dsn,
            """
            INSERT INTO memplex_functions
                (id, data, tenant_id, owner_subject, workspace, visibility, source_agent, source_session)
            VALUES ('domain_virtual', '{}', 'tenant', 'subject', 'workspace', 'workspace', 'agent', 'session')
            """,
        )
    with pytest.raises(psycopg2.errors.CheckViolation):
        _migration_execute(
            migration_dsn,
            "UPDATE memplex_functions SET id = 'domain_renamed' WHERE id = 'ordinary'",
        )


@pytest.mark.parametrize(
    "tamper_sql",
    (
        "ALTER TABLE memplex_functions DROP CONSTRAINT memplex_functions_reserved_domain_id_check",
        """
        ALTER TABLE memplex_functions RENAME CONSTRAINT memplex_functions_reserved_domain_id_check
        TO renamed_domain_id_check
        """,
        """
        ALTER TABLE memplex_functions DROP CONSTRAINT memplex_functions_reserved_domain_id_check;
        ALTER TABLE memplex_functions ADD CONSTRAINT memplex_functions_reserved_domain_id_check
        CHECK (NOT starts_with(id, 'domain_')) NOT VALID
        """,
        """
        ALTER TABLE memplex_functions DROP CONSTRAINT memplex_functions_reserved_domain_id_check;
        ALTER TABLE memplex_functions ADD CONSTRAINT memplex_functions_reserved_domain_id_check
        CHECK (NOT starts_with(id, 'domain_')) NO INHERIT
        """,
        """
        ALTER TABLE memplex_functions DROP CONSTRAINT memplex_functions_reserved_domain_id_check;
        ALTER TABLE memplex_functions ADD CONSTRAINT memplex_functions_reserved_domain_id_check
        CHECK (id <> '')
        """,
        "ALTER TABLE memplex_functions ADD CONSTRAINT extra_domain_id_check CHECK (id <> '')",
    ),
)
def test_v4_reserved_domain_check_catalogue_tampering_fails_closed(migration_dsn, tamper_sql):
    """The function namespace authority is exact, not merely a best-effort check."""
    PostgresMigrationRunner(migration_dsn).apply()
    _migration_execute(migration_dsn, tamper_sql)

    with pytest.raises(MigrationIntegrityError, match="unrecognised legacy schema"):
        PostgresMigrationRunner(migration_dsn).plan()


_VALID_HIDDEN_DOMAIN_VALUES = ("İ", "ΟΣ", "中文 领域", "\u00a0", "\t", "0")
_INVALID_HIDDEN_DOMAIN_VALUES = (
    None,
    "",
    False,
    0,
    1,
    True,
    1e20,
    {"z": ["nested", {"b": 2, "a": 1}], "a": "key-order"},
    ["nested", ["list", 1]],
)


def _insert_v3_belongs_to_rows(dsn: str, values, *, mismatch: bool = False) -> None:
    conn = psycopg2.connect(dsn)
    try:
        cur = conn.cursor()
        for index, domain in enumerate(values):
            source = f"source_{index}"
            cur.execute(
                """
                INSERT INTO memplex_functions
                    (id, data, tenant_id, owner_subject, workspace, visibility, source_agent, source_session)
                VALUES (%s, %s::jsonb, 'tenant', 'subject', 'workspace', 'workspace', 'agent', 'session')
                """,
                (source, json.dumps({"domain": domain})),
            )
            cur.execute("SELECT data FROM memplex_functions WHERE id = %s", (source,))
            raw_domain = cur.fetchone()[0].get("domain")
            # This helper deliberately creates legacy-invalid JSONB rows for
            # the migration verifier.  Production domain_node_id only accepts
            # exact strings, so non-string fixtures use an opaque placeholder
            # rather than reintroducing arbitrary-object stringification.
            target = (
                "domain_mismatch"
                if mismatch
                else domain_node_id(raw_domain)
                if type(raw_domain) is str
                else f"domain_legacy_invalid_{index}"
            )
            cur.execute(
                """
                INSERT INTO memplex_edges
                    (source, target, edge_type, tenant_id, owner_subject, workspace, visibility, source_agent, source_session)
                VALUES (%s, %s, 'BELONGS_TO', 'tenant', 'subject', 'workspace', 'workspace', 'agent', 'session')
                """,
                (source, target),
            )
        conn.commit()
        cur.close()
    finally:
        conn.close()


def _force_rls_state(dsn: str):
    return _admin_query(
        dsn,
        """
        SELECT relname, relforcerowsecurity
        FROM pg_catalog.pg_class
        WHERE oid IN ('memplex_functions'::regclass, 'memplex_edges'::regclass)
        ORDER BY relname
        """,
    )


def test_0004_runner_reads_hidden_typed_domains_with_python_runtime_semantics(migration_dsn):
    """The verified non-superuser owner sees FORCE-RLS-hidden JSONB rows under the lock."""
    _install_v3_catalogue_with_executed_ledger(migration_dsn)
    _insert_v3_belongs_to_rows(migration_dsn, _VALID_HIDDEN_DOMAIN_VALUES)
    role = f"g003_force_rls_owner_{uuid.uuid4().hex}"
    original_owner = _handoff_v3_catalogue_to_non_superuser_owner(migration_dsn, role)
    owner_dsn = psycopg2.extensions.make_dsn(migration_dsn, user=role)
    try:
        assert _admin_query(owner_dsn, "SELECT count(*) FROM memplex_edges") == [(0,)]
        assert PostgresMigrationRunner(owner_dsn).apply().state == "ready"
        assert _force_rls_state(migration_dsn) == [
            ("memplex_edges", True),
            ("memplex_functions", True),
        ]
        assert _ledger_rows(migration_dsn)[-1] == (6, "executed")
    finally:
        _restore_v3_catalogue_owner_and_drop_role(migration_dsn, role, original_owner)


@pytest.mark.parametrize("domain", _INVALID_HIDDEN_DOMAIN_VALUES)
def test_0004_runner_rejects_hidden_invalid_domain_types_without_partial_ddl(migration_dsn, domain):
    """Only nonempty strings match MemoryNode.domain; JSONB type drift stays blocked."""
    _install_v3_catalogue_with_executed_ledger(migration_dsn)
    _insert_v3_belongs_to_rows(migration_dsn, (domain,))
    role = f"g003_force_rls_owner_{uuid.uuid4().hex}"
    original_owner = _handoff_v3_catalogue_to_non_superuser_owner(migration_dsn, role)
    owner_dsn = psycopg2.extensions.make_dsn(migration_dsn, user=role)
    try:
        assert _admin_query(owner_dsn, "SELECT count(*) FROM memplex_edges") == [(0,)]
        with pytest.raises(MigrationIntegrityError, match="BELONGS_TO"):
            PostgresMigrationRunner(owner_dsn).apply()
        assert _force_rls_state(migration_dsn) == [
            ("memplex_edges", True),
            ("memplex_functions", True),
        ]
        assert _ledger_rows(migration_dsn) == [(1, "executed"), (2, "executed"), (3, "executed")]
        assert not _migration_column_exists(migration_dsn, "memplex_edges", "target_function")
    finally:
        _restore_v3_catalogue_owner_and_drop_role(migration_dsn, role, original_owner)


def test_0004_runner_rejects_hidden_domain_target_mismatch_without_partial_ddl(migration_dsn):
    """The shared helper, rather than SQL lower/text coercion, decides target identity."""
    _install_v3_catalogue_with_executed_ledger(migration_dsn)
    _insert_v3_belongs_to_rows(migration_dsn, ("payments",), mismatch=True)
    role = f"g003_force_rls_owner_{uuid.uuid4().hex}"
    original_owner = _handoff_v3_catalogue_to_non_superuser_owner(migration_dsn, role)
    owner_dsn = psycopg2.extensions.make_dsn(migration_dsn, user=role)
    try:
        with pytest.raises(MigrationIntegrityError, match="BELONGS_TO"):
            PostgresMigrationRunner(owner_dsn).apply()
        assert _force_rls_state(migration_dsn) == [
            ("memplex_edges", True),
            ("memplex_functions", True),
        ]
        assert _ledger_rows(migration_dsn) == [(1, "executed"), (2, "executed"), (3, "executed")]
        assert not _migration_column_exists(migration_dsn, "memplex_edges", "target_function")
    finally:
        _restore_v3_catalogue_owner_and_drop_role(migration_dsn, role, original_owner)


def test_0004_foreign_keys_reject_cross_tenant_edges_and_cascade_function_deletes(migration_dsn):
    """The database, not RLS visibility, enforces endpoint tenancy and cleanup."""
    PostgresMigrationRunner(migration_dsn).apply()
    _migration_execute(
        migration_dsn,
        """
        INSERT INTO memplex_functions
            (id, data, tenant_id, owner_subject, workspace, visibility, source_agent, source_session)
        VALUES
            ('source', '{}', 'tenant-a', 'subject-a', 'workspace-a', 'workspace', 'agent', 'session'),
            ('target', '{}', 'tenant-a', 'subject-b', 'workspace-b', 'workspace', 'agent', 'session');
        INSERT INTO memplex_edges
            (source, target, edge_type, tenant_id, owner_subject, workspace, visibility, source_agent, source_session)
        VALUES ('source', 'target', 'RELATED_TO', 'tenant-a', 'subject-a', 'workspace-a', 'workspace', 'agent', 'session');
        """,
    )
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        _migration_execute(
            migration_dsn,
            """
            INSERT INTO memplex_edges
                (source, target, edge_type, tenant_id, owner_subject, workspace, visibility, source_agent, source_session)
            VALUES ('source', 'target', 'RELATED_TO', 'tenant-b', 'subject', 'workspace', 'workspace', 'agent', 'session')
            """,
        )

    _migration_execute(
        migration_dsn,
        "DELETE FROM memplex_functions WHERE tenant_id = 'tenant-a' AND id = 'target'",
    )
    assert _admin_query(migration_dsn, "SELECT count(*) FROM memplex_edges") == [(0,)]


def test_v4_catalogue_without_ledger_is_never_adopted(migration_dsn):
    """A v4-shaped catalogue is evidence of 0004, not an adoption baseline."""
    PostgresMigrationRunner(migration_dsn).apply()
    _migration_execute(migration_dsn, "DROP TABLE memplex_schema_migrations")

    with pytest.raises(MigrationIntegrityError, match="unrecognised legacy schema"):
        PostgresMigrationRunner(migration_dsn).plan()


@pytest.mark.parametrize(
    ("constraint_name", "local_column", "endpoint"),
    (
        ("memplex_edges_source_function_fk", "source", "source"),
        ("memplex_edges_target_function_fk", "target_function", "target"),
    ),
)
def test_0004_foreign_key_target_must_belong_to_current_schema(
    migration_dsn, constraint_name, local_column, endpoint
):
    """A same-named sibling table must not be accepted as the endpoint authority."""
    PostgresMigrationRunner(migration_dsn).apply()
    sibling = f"g003_fk_sibling_{uuid.uuid4().hex}"
    try:
        _migration_execute(
            migration_dsn,
            f"""
            INSERT INTO memplex_functions
                (id, data, tenant_id, owner_subject, workspace, visibility, source_agent, source_session)
            VALUES
                ('source', '{{}}', 'tenant', 'subject', 'workspace', 'workspace', 'agent', 'session'),
                ('target', '{{}}', 'tenant', 'subject', 'workspace', 'workspace', 'agent', 'session');
            INSERT INTO memplex_edges
                (source, target, edge_type, tenant_id, owner_subject, workspace, visibility, source_agent, source_session)
            VALUES ('source', 'target', 'RELATED_TO', 'tenant', 'subject', 'workspace', 'workspace', 'agent', 'session');
            CREATE SCHEMA {sibling};
            CREATE TABLE {sibling}.memplex_functions (
                tenant_id TEXT NOT NULL,
                id TEXT NOT NULL,
                PRIMARY KEY (tenant_id, id)
            );
            INSERT INTO {sibling}.memplex_functions (tenant_id, id) VALUES ('tenant', '{endpoint}');
            ALTER TABLE memplex_edges DROP CONSTRAINT {constraint_name};
            ALTER TABLE memplex_edges ADD CONSTRAINT {constraint_name}
            FOREIGN KEY (tenant_id, {local_column})
            REFERENCES {sibling}.memplex_functions (tenant_id, id)
            ON DELETE CASCADE;
            DELETE FROM memplex_functions WHERE tenant_id = 'tenant' AND id = '{endpoint}';
            """,
        )
        # The current-schema endpoint is now gone, yet the edge survives:
        # this is the exact false-ready condition the catalogue gate prevents.
        assert _admin_query(migration_dsn, "SELECT count(*) FROM memplex_edges") == [(1,)]

        runner = PostgresMigrationRunner(migration_dsn)
        expected_target = runner.inspect_target()
        with pytest.raises(MigrationIntegrityError, match="unrecognised legacy schema"):
            runner.plan()
        with pytest.raises(MigrationIntegrityError, match="unrecognised legacy schema"):
            runner.status()
        with pytest.raises(MigrationIntegrityError, match="unrecognised legacy schema"):
            runner.verify_storage_readiness(
                VectorCapabilityRequest(dim=0, policy="disabled"),
                "development",
                expected_target=expected_target,
            )
    finally:
        _migration_execute(migration_dsn, f"DROP SCHEMA IF EXISTS {sibling} CASCADE")


@pytest.mark.parametrize(
    "tamper_sql",
    (
        "ALTER TABLE memplex_edges RENAME CONSTRAINT memplex_edges_source_function_fk TO forged_fk",
        "DROP INDEX memplex_edges_tenant_target_function_idx",
        """
        ALTER TABLE memplex_edges DROP CONSTRAINT memplex_edges_target_function_fk;
        ALTER TABLE memplex_edges ADD CONSTRAINT memplex_edges_target_function_fk
        FOREIGN KEY (tenant_id, target_function) REFERENCES memplex_functions (tenant_id, id)
        ON DELETE NO ACTION
        """,
        """
        ALTER TABLE memplex_edges DROP CONSTRAINT memplex_edges_target_function_fk;
        ALTER TABLE memplex_edges ADD CONSTRAINT memplex_edges_target_function_fk
        FOREIGN KEY (tenant_id, target_function) REFERENCES memplex_functions (tenant_id, id)
        DEFERRABLE
        """,
        """
        ALTER TABLE memplex_edges DROP CONSTRAINT memplex_edges_target_function_fk;
        ALTER TABLE memplex_edges ADD CONSTRAINT memplex_edges_target_function_fk
        FOREIGN KEY (tenant_id, target_function) REFERENCES memplex_functions (tenant_id, id) NOT VALID
        """,
        """
        ALTER TABLE memplex_edges DROP CONSTRAINT memplex_edges_target_function_fk;
        ALTER TABLE memplex_edges ADD CONSTRAINT memplex_edges_target_function_fk
        FOREIGN KEY (tenant_id, target) REFERENCES memplex_functions (tenant_id, id)
        ON DELETE CASCADE
        """,
        """
        ALTER TABLE memplex_edges DROP CONSTRAINT memplex_edges_target_function_fk;
        DROP INDEX memplex_edges_tenant_target_function_idx;
        ALTER TABLE memplex_edges DROP COLUMN target_function;
        ALTER TABLE memplex_edges ADD COLUMN target_function TEXT GENERATED ALWAYS AS (
            CASE WHEN edge_type = 'BELONGS_TO' THEN NULL::text ELSE source END
        ) STORED;
        ALTER TABLE memplex_edges ADD CONSTRAINT memplex_edges_target_function_fk
        FOREIGN KEY (tenant_id, target_function) REFERENCES memplex_functions (tenant_id, id)
        ON DELETE CASCADE;
        CREATE INDEX memplex_edges_tenant_target_function_idx
        ON memplex_edges (tenant_id, target_function) WHERE target_function IS NOT NULL
        """,
    ),
)
def test_0004_catalogue_tampering_fails_closed(migration_dsn, tamper_sql):
    """Names, columns, action, validation, deferral and supporting index are exact."""
    PostgresMigrationRunner(migration_dsn).apply()
    _migration_execute(migration_dsn, tamper_sql)

    with pytest.raises(MigrationIntegrityError, match="unrecognised legacy schema"):
        PostgresMigrationRunner(migration_dsn).plan()


def test_unknown_memplex_object_fails_before_a_ledger_is_created(migration_dsn):
    """A typoed or foreign Memplex table is never silently adopted."""
    _migration_execute(migration_dsn, "CREATE TABLE memplex_unexpected (id TEXT PRIMARY KEY)")

    with pytest.raises(MigrationIntegrityError, match="unrecognised legacy schema"):
        PostgresMigrationRunner(migration_dsn).plan()

    assert not _migration_table_exists(migration_dsn, "memplex_schema_migrations")


def test_0002_body_and_ledger_insert_rollback_together(migration_dsn, monkeypatch):
    """A ledger failure after ACL DDL must roll back every preceding change."""
    _install_pre_g002_3_2_7(migration_dsn)
    runner = PostgresMigrationRunner(migration_dsn)
    original_insert = runner._insert_ledger_row

    def _fault_after_acl(cur, migration, execution_mode, baseline_fingerprint=None):
        if migration.version == 2:
            raise RuntimeError("ledger fault")
        return original_insert(cur, migration, execution_mode, baseline_fingerprint)

    monkeypatch.setattr(runner, "_insert_ledger_row", _fault_after_acl)

    with pytest.raises(RuntimeError, match="ledger fault"):
        runner.apply()

    assert not _migration_table_exists(migration_dsn, "memplex_schema_migrations")
    assert not _migration_column_exists(migration_dsn, "memplex_functions", "tenant_id")
    assert not _migration_table_exists(migration_dsn, "memplex_facts")


@pytest.mark.parametrize(
    ("tamper_sql", "expected"),
    (
        ("DELETE FROM memplex_schema_migrations WHERE version = 2", "not continuous"),
        ("UPDATE memplex_schema_migrations SET name = 'forged' WHERE version = 2", "integrity check failed"),
        ("UPDATE memplex_schema_migrations SET checksum = 'forged' WHERE version = 2", "integrity check failed"),
        (
            """
            INSERT INTO memplex_schema_migrations
                (version, name, checksum, applied_at, execution_mode, baseline_fingerprint)
            VALUES ({future_version}, 'future', 'forged', CURRENT_TIMESTAMP, 'executed', NULL)
            """,
            "not continuous",
        ),
    ),
)
def test_ledger_gap_drift_and_future_versions_fail_closed(migration_dsn, tamper_sql, expected):
    """A syntactically valid ledger is still rejected when its history is not exact."""
    PostgresMigrationRunner(migration_dsn).apply()
    future_version = discover_migrations()[-1].version + 1
    _migration_execute(migration_dsn, tamper_sql.format(future_version=future_version))

    with pytest.raises(MigrationIntegrityError, match=expected):
        PostgresMigrationRunner(migration_dsn).plan()


def test_illegal_vector_column_is_not_adopted(migration_dsn):
    """Only an installed pgvector extension may justify an embedding column."""
    _install_post_g002_core(migration_dsn)
    _migration_execute(migration_dsn, "ALTER TABLE memplex_functions ADD COLUMN embedding INTEGER")

    with pytest.raises(MigrationIntegrityError, match="unrecognised legacy schema"):
        PostgresMigrationRunner(migration_dsn).plan()

    assert not _migration_table_exists(migration_dsn, "memplex_schema_migrations")


def test_unknown_core_column_is_not_silently_adopted(migration_dsn):
    """Fingerprint validation covers columns as well as table names."""
    _install_post_g002_core(migration_dsn)
    _migration_execute(migration_dsn, "ALTER TABLE memplex_facts ADD COLUMN foreign_drift TEXT")

    with pytest.raises(MigrationIntegrityError, match="unrecognised legacy schema"):
        PostgresMigrationRunner(migration_dsn).plan()


def test_feedback_policy_drift_is_not_masked_by_a_matching_table_name(migration_dsn):
    """Fingerprinting must reject a weaker RLS policy with the expected name."""
    PostgresMigrationRunner(migration_dsn).apply()
    _migration_execute(
        migration_dsn,
        """
        DROP POLICY feedback_tenant_scope ON feedback;
        CREATE POLICY feedback_tenant_scope ON feedback
        USING (
            tenant_id = current_setting('memplex.tenant_id', true)
            AND (visibility = 'user' OR visibility = 'workspace' OR visibility = 'session')
        )
        WITH CHECK (
            tenant_id = current_setting('memplex.tenant_id', true)
            AND owner_subject_id IS NOT NULL
            AND workspace_id IS NOT NULL
            AND visibility IS NOT NULL
        )
        """,
    )

    with pytest.raises(MigrationIntegrityError, match="unrecognised legacy schema"):
        PostgresMigrationRunner(migration_dsn).plan()


def test_feedback_index_drift_is_not_masked_by_a_matching_index_name(migration_dsn):
    """Fingerprinting must bind the index definition, not merely its name."""
    PostgresMigrationRunner(migration_dsn).apply()
    _migration_execute(
        migration_dsn,
        """
        DROP INDEX feedback_tenant_memory_idx;
        CREATE INDEX feedback_tenant_memory_idx ON feedback (memory_id);
        """,
    )

    with pytest.raises(MigrationIntegrityError, match="unrecognised legacy schema"):
        PostgresMigrationRunner(migration_dsn).plan()


def test_feedback_default_drift_is_not_silently_adopted(migration_dsn):
    """The canonical fingerprint includes column defaults, not only names and types."""
    PostgresMigrationRunner(migration_dsn).apply()
    _migration_execute(migration_dsn, "ALTER TABLE feedback ALTER COLUMN source SET DEFAULT 'foreign'")

    with pytest.raises(MigrationIntegrityError, match="unrecognised legacy schema"):
        PostgresMigrationRunner(migration_dsn).plan()


def test_core_index_drift_is_not_masked_by_a_matching_index_name(migration_dsn):
    """Core catalogue recognition binds every required index to its key shape."""
    PostgresMigrationRunner(migration_dsn).apply()
    _migration_execute(
        migration_dsn,
        """
        DROP INDEX memplex_edges_tenant_idx;
        CREATE INDEX memplex_edges_tenant_idx ON memplex_edges (source);
        """,
    )

    with pytest.raises(MigrationIntegrityError, match="unrecognised legacy schema"):
        PostgresMigrationRunner(migration_dsn).plan()


def test_two_runners_converge_to_one_contiguous_ledger(migration_dsn):
    """The fixed transaction advisory lock serialises concurrent first applies."""
    start = Barrier(2)

    def _apply_once():
        start.wait(timeout=10)
        return PostgresMigrationRunner(migration_dsn).apply().state

    with ThreadPoolExecutor(max_workers=2) as executor:
        outcomes = list(executor.map(lambda _ignored: _apply_once(), range(2)))

    assert outcomes == ["ready", "ready"]
    assert _ledger_rows(migration_dsn) == [
        (1, "executed"),
        (2, "executed"),
        (3, "executed"),
        (4, "executed"),
        (5, "executed"),
        (6, "executed"),
    ]


def test_second_apply_preserves_catalogue_ledger_modes_and_applied_at(migration_dsn):
    """Reapplying an already converged legacy schema cannot rewrite audit history."""
    _install_pre_g002_3_2_7(migration_dsn)
    runner = PostgresMigrationRunner(migration_dsn)
    assert runner.apply().state == "ready"
    first_rows = _ledger_rows_with_timestamps(migration_dsn)

    assert runner.apply().state == "ready"

    assert _ledger_rows_with_timestamps(migration_dsn) == first_rows
    assert _ledger_rows(migration_dsn) == [
        (1, "executed"),
        (2, "executed"),
        (3, "executed"),
        (4, "executed"),
        (5, "executed"),
        (6, "executed"),
    ]


def test_required_vector_unavailable_rolls_back_migrations(migration_dsn):
    """Required capability failure cannot leave an adopted ledger or DDL behind."""
    if _vector_extension_is_available(migration_dsn):
        pytest.skip("pgvector is available in this PostgreSQL build")
    from memplex.storage.migrations.runner import VectorCapabilityRequest

    with pytest.raises(MigrationIntegrityError, match="required vector capability is unavailable"):
        PostgresMigrationRunner(migration_dsn).ensure_vector_capability(
            VectorCapabilityRequest(dim=8, policy="required"), deployment_profile="production"
        )

    assert not _migration_table_exists(migration_dsn, "memplex_schema_migrations")
    assert not _migration_table_exists(migration_dsn, "memplex_functions")


def test_best_effort_vector_unavailable_degrades_without_capability_row(migration_dsn):
    """Best effort may commit ordinary migrations but never pretend vector succeeded."""
    if _vector_extension_is_available(migration_dsn):
        pytest.skip("pgvector is available in this PostgreSQL build")
    from memplex.storage.migrations.runner import VectorCapabilityRequest

    status = PostgresMigrationRunner(migration_dsn).ensure_vector_capability(
        VectorCapabilityRequest(dim=8, policy="best_effort"), deployment_profile="development"
    )

    assert status.state == "degraded"
    assert _ledger_rows(migration_dsn) == [(1, "executed"), (2, "executed"), (3, "executed")]
    assert not _migration_column_exists(migration_dsn, "memplex_functions", "embedding")
    conn = psycopg2.connect(migration_dsn)
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM memplex_schema_capabilities")
        assert cur.fetchone() == (0,)
        cur.close()
    finally:
        conn.close()


def test_required_vector_capability_records_the_real_extension_and_dimension(migration_dsn):
    """When pgvector is installed, the runner owns the exact typed column and audit row."""
    if not _vector_extension_is_available(migration_dsn):
        pytest.skip("pgvector is unavailable in this PostgreSQL build")
    from memplex.storage.migrations.runner import VectorCapabilityRequest

    status = PostgresMigrationRunner(migration_dsn).ensure_vector_capability(
        VectorCapabilityRequest(dim=8, policy="required"), deployment_profile="production"
    )

    assert status.state == "ready"
    assert _migration_column_exists(migration_dsn, "memplex_functions", "embedding")
    conn = psycopg2.connect(migration_dsn)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT pg_catalog.format_type(a.atttypid, a.atttypmod)
            FROM pg_catalog.pg_attribute AS a
            WHERE a.attrelid = 'memplex_functions'::regclass AND a.attname = 'embedding'
            """
        )
        assert cur.fetchone() == ("vector(8)",)
        cur.execute(
            """
            SELECT parameter_digest FROM memplex_schema_capabilities
            WHERE capability_name = 'pgvector_embedding'
            """
        )
        assert cur.fetchone() == (status.parameter_digest,)
        cur.close()
    finally:
        conn.close()


# ── G003 Task 2 Fix-1 adversarial catalogue contracts ───────────────


def _replace_policy_with_true_disjunct(dsn: str, table: str, policy: str) -> None:
    """Preserve the visible policy text while making its predicate permissive."""
    conn = psycopg2.connect(dsn)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT pg_catalog.pg_get_expr(polqual, polrelid),
                   pg_catalog.pg_get_expr(polwithcheck, polrelid)
            FROM pg_catalog.pg_policy
            WHERE polrelid = %s::regclass AND polname = %s
            """,
            (table, policy),
        )
        qualify, check = cur.fetchone()
        cur.execute(f"DROP POLICY {policy} ON {table}")
        cur.execute(
            f"CREATE POLICY {policy} ON {table} TO PUBLIC "
            f"USING (({qualify}) OR TRUE) WITH CHECK (({check}) OR TRUE)"
        )
        conn.commit()
        cur.close()
    finally:
        conn.close()


def test_core_policy_role_and_or_true_drift_fail_closed(migration_dsn):
    """Policy names and contained ACL tokens cannot authenticate an unsafe policy."""
    PostgresMigrationRunner(migration_dsn).apply()
    _migration_execute(
        migration_dsn,
        "ALTER POLICY memplex_functions_scope ON memplex_functions TO CURRENT_USER",
    )

    with pytest.raises(MigrationIntegrityError, match="unrecognised legacy schema"):
        PostgresMigrationRunner(migration_dsn).plan()

    _migration_execute(
        migration_dsn,
        "ALTER POLICY memplex_functions_scope ON memplex_functions TO PUBLIC",
    )
    _replace_policy_with_true_disjunct(
        migration_dsn, "memplex_functions", "memplex_functions_scope"
    )

    with pytest.raises(MigrationIntegrityError, match="unrecognised legacy schema"):
        PostgresMigrationRunner(migration_dsn).plan()


def test_feedback_policy_or_true_drift_fail_closed(migration_dsn):
    """Feedback RLS must compare canonical USING and WITH CHECK expressions exactly."""
    PostgresMigrationRunner(migration_dsn).apply()
    _replace_policy_with_true_disjunct(migration_dsn, "feedback", "feedback_tenant_scope")

    with pytest.raises(MigrationIntegrityError, match="unrecognised legacy schema"):
        PostgresMigrationRunner(migration_dsn).plan()


def test_integrity_index_expression_drift_fails_closed(migration_dsn):
    """A same-named unique index must retain its exact expression and predicate."""
    PostgresMigrationRunner(migration_dsn).apply()
    _migration_execute(
        migration_dsn,
        """
        DROP INDEX memplex_functions_workspace_normalized_name_key;
        CREATE UNIQUE INDEX memplex_functions_workspace_normalized_name_key
        ON memplex_functions (tenant_id, workspace, lower(btrim(data->>'name_normalized')))
        WHERE visibility = 'workspace' AND btrim(data->>'name_normalized') <> '';
        """,
    )

    with pytest.raises(MigrationIntegrityError, match="unrecognised legacy schema"):
        PostgresMigrationRunner(migration_dsn).plan()


def test_core_index_include_drift_fails_closed(migration_dsn):
    """INCLUDE is part of an index definition, not ignorable decoration."""
    PostgresMigrationRunner(migration_dsn).apply()
    _migration_execute(
        migration_dsn,
        """
        DROP INDEX memplex_functions_tenant_idx;
        CREATE INDEX memplex_functions_tenant_idx
        ON memplex_functions (tenant_id) INCLUDE (id);
        """,
    )

    with pytest.raises(MigrationIntegrityError, match="unrecognised legacy schema"):
        PostgresMigrationRunner(migration_dsn).plan()


def test_post_g002_workspace_default_is_not_a_compatible_baseline(migration_dsn):
    """A migration-v2 baseline has its exact defaults; it is not fieldwise permissive."""
    _install_post_g002_core(migration_dsn)
    _migration_execute(
        migration_dsn,
        "ALTER TABLE memplex_functions ALTER COLUMN visibility SET DEFAULT 'workspace'",
    )

    with pytest.raises(MigrationIntegrityError, match="unrecognised legacy schema"):
        PostgresMigrationRunner(migration_dsn).plan()


@pytest.mark.parametrize("with_feedback", (False, True))
def test_exact_runtime_v1_catalogue_is_a_known_upgrade_baseline(migration_dsn, with_feedback):
    """Current store startup DDL is a fixed v1 variant, never an arbitrary mixed schema."""
    _install_runtime_v1_core_fixture(migration_dsn, dim=0)
    if with_feedback:
        _install_legacy_feedback_fixture(migration_dsn, "runtime-v1-feedback")

    runner = PostgresMigrationRunner(migration_dsn)
    plan = runner.plan()
    assert plan.current_version == 2
    assert [migration.version for migration in plan.pending] == [3, 4, 5, 6]
    assert runner.status() == plan
    assert runner.apply(dry_run=True) == plan
    assert runner.apply().state == "ready"
    assert PostgresMigrationRunner(migration_dsn).status().state == "ready"
    if with_feedback:
        # The dynamic feedback store's old policy is a recognised pre-0003
        # baseline only. Reintroducing it after 0003 must not weaken current.
        _admin_execute(migration_dsn, "DROP TABLE feedback")
        _install_legacy_feedback_fixture(migration_dsn, "post-v3-feedback")
        with pytest.raises(MigrationIntegrityError, match="unrecognised legacy schema"):
            PostgresMigrationRunner(migration_dsn).plan()


def test_vector_capability_column_after_acl_remains_a_current_catalogue(migration_dsn):
    """The controlled vector ALTER appends after ACL fields and survives a new runner."""
    if not _vector_extension_is_available(migration_dsn):
        pytest.skip("pgvector is unavailable in this PostgreSQL build")
    from memplex.storage.migrations.runner import VectorCapabilityRequest

    runner = PostgresMigrationRunner(migration_dsn)
    status = runner.ensure_vector_capability(
        VectorCapabilityRequest(dim=8, policy="required"), deployment_profile="production"
    )

    assert status.state == "ready"
    assert PostgresMigrationRunner(migration_dsn).plan().state == "ready"
    conn = psycopg2.connect(migration_dsn)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT a.attnum, pg_catalog.format_type(a.atttypid, a.atttypmod)
            FROM pg_catalog.pg_attribute AS a
            WHERE a.attrelid = 'memplex_functions'::regclass AND a.attname = 'embedding'
            """
        )
        assert cur.fetchone() == (11, "vector(8)")
        cur.close()
    finally:
        conn.close()


def test_unknown_memplex_sequence_fails_closed(migration_dsn):
    """The managed-object scan includes sequences rather than tables alone."""
    PostgresMigrationRunner(migration_dsn).apply()
    _migration_execute(migration_dsn, "CREATE SEQUENCE memplex_unrecognised_sequence")

    with pytest.raises(MigrationIntegrityError, match="unrecognised legacy schema"):
        PostgresMigrationRunner(migration_dsn).plan()


@pytest.mark.parametrize(
    "tamper_sql",
    (
        "ALTER TABLE memplex_schema_migrations DROP CONSTRAINT memplex_schema_migrations_pkey",
        "CREATE INDEX memplex_schema_migrations_unexpected_idx ON memplex_schema_migrations (name)",
    ),
)
def test_ledger_requires_exact_primary_key_and_no_extra_indexes(migration_dsn, tamper_sql):
    """Ledger table authenticity includes relation kind, PK and all physical indexes."""
    PostgresMigrationRunner(migration_dsn).apply()
    _migration_execute(migration_dsn, tamper_sql)

    with pytest.raises(MigrationIntegrityError, match="migration ledger has an unrecognised shape"):
        PostgresMigrationRunner(migration_dsn).plan()


def test_ledger_rejects_forged_or_late_adoption_modes(migration_dsn):
    """Only audited 1/2 adoption pairs may exist; all later rows are executed."""
    _install_post_g002_core(migration_dsn)
    migrations = discover_migrations()
    first, second = migrations[:2]
    _migration_execute(
        migration_dsn,
        f"""
        CREATE TABLE memplex_schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            checksum TEXT NOT NULL,
            applied_at TIMESTAMPTZ NOT NULL,
            execution_mode TEXT NOT NULL,
            baseline_fingerprint TEXT
        );
        INSERT INTO memplex_schema_migrations
            (version, name, checksum, applied_at, execution_mode, baseline_fingerprint)
        VALUES
            (1, '{first.name}', '{first.checksum}',
             CURRENT_TIMESTAMP, 'executed', NULL),
            (2, '{second.name}', '{second.checksum}',
             CURRENT_TIMESTAMP, 'adopted', 'forged');
        """,
    )

    with pytest.raises(MigrationIntegrityError, match="migration ledger integrity check failed"):
        PostgresMigrationRunner(migration_dsn).plan()


def test_ledger_rejects_adoption_of_migration_three(migration_dsn):
    """A current ledger may never rewrite migration 3 as a baseline adoption."""
    PostgresMigrationRunner(migration_dsn).apply()
    _migration_execute(
        migration_dsn,
        """
        UPDATE memplex_schema_migrations
        SET execution_mode = 'adopted', baseline_fingerprint = 'forged'
        WHERE version = 3
        """,
    )
    with pytest.raises(MigrationIntegrityError, match="migration ledger integrity check failed"):
        PostgresMigrationRunner(migration_dsn).plan()


def test_runner_confines_a_quoted_target_schema_before_reading_ledger(pg_dsn):
    """A later search-path schema's ready ledger cannot change target migration state."""
    target = f'g003 target "{uuid.uuid4().hex[:12]}'
    source = f'g003 source "{uuid.uuid4().hex[:12]}'
    _create_schema(pg_dsn, target)
    _create_schema(pg_dsn, source)
    try:
        _install_post_g002_core_in_schema(pg_dsn, target)
        source_factory = _schema_connection_factory(pg_dsn, source)
        target_then_source_factory = _schema_connection_factory(pg_dsn, target, source)
        PostgresMigrationRunner(pg_dsn, connection_factory=source_factory).apply()
        source_before = _migration_ledger_digest(pg_dsn, source_factory)

        runner = PostgresMigrationRunner(pg_dsn, connection_factory=target_then_source_factory)
        plan = runner.plan()
        assert plan.current_version == 2
        assert [migration.version for migration in plan.pending] == [3, 4, 5, 6]
        assert runner.apply().state == "ready"
        assert _migration_ledger_digest(pg_dsn, source_factory) == source_before
        assert PostgresMigrationRunner(pg_dsn, connection_factory=source_factory).status().state == "ready"
    finally:
        _drop_schema(pg_dsn, target)
        _drop_schema(pg_dsn, source)


@pytest.mark.parametrize(
    "operation",
    (
        "plan",
        "status",
        "dry_run",
        "apply",
        "vector_required",
        "vector_best_effort",
    ),
)
def test_expected_target_rejects_factory_schema_switch_before_catalogue(
    pg_dsn,
    operation,
):
    """Every target-aware entry point must recheck a newly opened connection."""
    inspected_schema = f"g003_target_a_{uuid.uuid4().hex}"
    switched_schema = f"g003_target_b_{uuid.uuid4().hex}"
    _create_schema(pg_dsn, inspected_schema)
    _create_schema(pg_dsn, switched_schema)
    try:
        factories = iter(
            (
                _schema_connection_factory(pg_dsn, inspected_schema),
                _schema_connection_factory(pg_dsn, switched_schema),
            )
        )
        runner = PostgresMigrationRunner(
            pg_dsn,
            connection_factory=lambda: next(factories)(),
        )
        expected = runner.inspect_target()

        with pytest.raises(MigrationIntegrityError, match="target identity"):
            if operation == "plan":
                runner.plan(expected_target=expected)
            elif operation == "status":
                runner.status(expected_target=expected)
            elif operation == "dry_run":
                runner.apply(dry_run=True, expected_target=expected)
            elif operation == "apply":
                runner.apply(expected_target=expected)
            elif operation == "vector_required":
                runner.ensure_vector_capability(
                    VectorCapabilityRequest(dim=8, policy="required"),
                    "production",
                    expected_target=expected,
                )
            elif operation == "vector_best_effort":
                runner.ensure_vector_capability(
                    VectorCapabilityRequest(dim=8, policy="best_effort"),
                    "development",
                    expected_target=expected,
                )
        assert _managed_relation_count(pg_dsn, inspected_schema) == 0
        assert _managed_relation_count(pg_dsn, switched_schema) == 0
    finally:
        _drop_schema(pg_dsn, inspected_schema)
        _drop_schema(pg_dsn, switched_schema)


def test_expected_target_allows_a_different_role_on_the_same_resolved_target(pg_dsn):
    """Target identity excludes credentials while binding server, database, and schema."""
    schema = f"g003_target_role_{uuid.uuid4().hex}"
    role = f"g003_application_{uuid.uuid4().hex}"
    _create_schema(pg_dsn, schema)
    _admin_execute(
        pg_dsn,
        pg_sql.SQL("CREATE ROLE {} LOGIN").format(pg_sql.Identifier(role)),
    )
    try:
        _admin_execute(
            pg_dsn,
            pg_sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                pg_sql.Identifier(schema),
                pg_sql.Identifier(role),
            ),
        )
        expected = PostgresMigrationRunner(
            pg_dsn,
            connection_factory=_schema_connection_factory(pg_dsn, schema),
        ).inspect_target()
        application_dsn = psycopg2.extensions.make_dsn(
            pg_dsn,
            user=role,
            options=f"-c search_path={schema}",
        )
        application_runner = PostgresMigrationRunner(application_dsn)

        plan = application_runner.plan(expected_target=expected)

        assert plan.state == "upgrade_required"
        assert plan.current_version == 0
        assert _managed_relation_count(pg_dsn, schema) == 0
    finally:
        _drop_schema(pg_dsn, schema)
        _admin_execute(
            pg_dsn,
            pg_sql.SQL("DROP ROLE IF EXISTS {}").format(pg_sql.Identifier(role)),
        )


@pytest.mark.parametrize("with_feedback", (False, True))
def test_runtime_vector_v1_is_adopted_with_its_verified_dimension(migration_dsn, with_feedback):
    """A real pre-G003 runtime vector table becomes current without changing its data."""
    if not _vector_extension_is_available(migration_dsn):
        pytest.skip("pgvector is unavailable in this PostgreSQL build")
    conn = psycopg2.connect(migration_dsn)
    try:
        cur = conn.cursor()
        cur.execute("SELECT current_schema()")
        target = cur.fetchone()[0]
        cur.close()
    finally:
        conn.close()
    runtime_dsn = psycopg2.extensions.make_dsn(
        migration_dsn, options=f"-c search_path={target},public"
    )
    _install_runtime_v1_core_fixture(migration_dsn, 8)
    _migration_execute(
        migration_dsn,
        """
        INSERT INTO memplex_functions (tenant_id, id, data, owner_subject, workspace, visibility, source_agent, source_session)
        VALUES ('vector-tenant', 'vector-runtime', '{"name": "Vector Runtime"}', 'subject', 'workspace', 'workspace', 'agent', 'session')
        """,
    )
    if with_feedback:
        _install_legacy_feedback_fixture(migration_dsn, "vector-runtime")

    runner = PostgresMigrationRunner(migration_dsn)
    plan = runner.plan()
    assert plan.current_version == 2
    assert runner.status() == plan
    assert runner.apply(dry_run=True) == plan
    assert runner.apply().state == "ready"
    assert PostgresMigrationRunner(migration_dsn).status().state == "ready"
    role = f"memplex_vector_app_{uuid.uuid4().hex[:8]}"
    _provision_application_role(migration_dsn, role)
    _grant_vector_type_usage(migration_dsn, role)
    application_dsn = psycopg2.extensions.make_dsn(runtime_dsn, user=role)
    resources = PostgresStorageResources(
        dsn=application_dsn, migration_dsn=migration_dsn
    )
    try:
        resources.ensure_ready(VectorCapabilityRequest(dim=8, policy="required"), "production")
        store = PostgresMemoryStore(dsn=application_dsn, ready_pool=resources.ready_pool)
        assert store._vector_dim == 8
        conn = psycopg2.connect(migration_dsn)
        try:
            cur = conn.cursor()
            cur.execute("SELECT data->>'name' FROM memplex_functions WHERE id = 'vector-runtime'")
            assert cur.fetchone() == ("Vector Runtime",)
            cur.execute(
                """
                SELECT parameter_digest FROM memplex_schema_capabilities
                WHERE capability_name = 'pgvector_embedding'
                """
            )
            assert cur.fetchone() == (hashlib.sha256(b"pgvector:8").hexdigest(),)
            cur.close()
        finally:
            conn.close()
    finally:
        if resources.state == "READY":
            resources.close()
        _drop_feedback_role(migration_dsn, role)


def test_sibling_vector_extension_is_bound_by_type_oid_not_search_path(migration_dsn, pg_dsn):
    """A target-local fake vector type cannot replace pgvector in a sibling schema."""
    if not _vector_extension_is_available(migration_dsn):
        pytest.skip("pgvector is unavailable in this PostgreSQL build")
    sibling = f"g003_vector_extension_{uuid.uuid4().hex}"
    target_conn = psycopg2.connect(migration_dsn)
    try:
        target_cur = target_conn.cursor()
        target_cur.execute("SELECT current_schema()")
        target = target_cur.fetchone()[0]
        target_cur.close()
    finally:
        target_conn.close()
    conn = psycopg2.connect(pg_dsn)
    try:
        cur = conn.cursor()
        cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
        conn.commit()
        cur.execute(
            """
            SELECT namespace.nspname, extension.extrelocatable
            FROM pg_catalog.pg_extension AS extension
            JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = extension.extnamespace
            WHERE extension.extname = 'vector'
            """
        )
        original_schema, relocatable = cur.fetchone()
        if not relocatable:
            pytest.skip("this pgvector build cannot move its extension schema")
        cur.execute(pg_sql.SQL("CREATE SCHEMA {}").format(pg_sql.Identifier(sibling)))
        cur.execute(pg_sql.SQL("ALTER EXTENSION vector SET SCHEMA {}").format(pg_sql.Identifier(sibling)))
        conn.commit()
        cur.close()
    finally:
        conn.close()
    runtime_dsn = psycopg2.extensions.make_dsn(
        pg_dsn, options=f"-c search_path={target},{sibling}"
    )
    try:
        _install_runtime_v1_core_fixture(runtime_dsn, 8)
        _migration_execute(migration_dsn, "CREATE DOMAIN vector AS integer")
        runner = PostgresMigrationRunner(migration_dsn)
        assert runner.plan().current_version == 2
        assert runner.apply().state == "ready"
        role = f"memplex_vector_app_{uuid.uuid4().hex[:8]}"
        _provision_application_role(migration_dsn, role)
        _grant_vector_type_usage(migration_dsn, role)
        application_dsn = psycopg2.extensions.make_dsn(runtime_dsn, user=role)
        resources = PostgresStorageResources(
            dsn=application_dsn, migration_dsn=migration_dsn
        )
        try:
            assert resources.ensure_ready(
                VectorCapabilityRequest(dim=8, policy="required"), "production"
            ).state == "ready"
        finally:
            if resources.state == "READY":
                resources.close()
            _drop_feedback_role(migration_dsn, role)
    finally:
        conn = psycopg2.connect(pg_dsn)
        try:
            cur = conn.cursor()
            cur.execute(pg_sql.SQL("ALTER EXTENSION vector SET SCHEMA {}").format(pg_sql.Identifier(original_schema)))
            cur.execute(pg_sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(pg_sql.Identifier(sibling)))
            conn.commit()
            cur.close()
        finally:
            conn.close()


def test_migration_v2_legacy327_adopts_and_restarts_without_data_loss(migration_dsn):
    """The explicitly recognised integer-changelog G002 baseline must be apply-compatible."""
    _install_post_g002_core(migration_dsn)
    _migration_execute(
        migration_dsn,
        """
        ALTER TABLE memplex_changelog ALTER COLUMN id TYPE INTEGER USING id::integer;
        INSERT INTO memplex_functions
            (id, data, tenant_id, owner_subject, workspace, visibility, source_agent, source_session)
        VALUES ('legacy327', '{"name": "Legacy 327"}', 'tenant', 'subject', 'workspace',
                'workspace', 'agent', 'session');
        """,
    )

    runner = PostgresMigrationRunner(migration_dsn)
    plan = runner.plan()
    assert plan.current_version == 2
    assert runner.status() == plan
    assert runner.apply(dry_run=True) == plan
    assert runner.apply().state == "ready"
    assert PostgresMigrationRunner(migration_dsn).status().state == "ready"
    conn = psycopg2.connect(migration_dsn)
    try:
        cur = conn.cursor()
        cur.execute("SELECT data->>'name' FROM memplex_functions WHERE id = 'legacy327'")
        assert cur.fetchone() == ("Legacy 327",)
        cur.close()
    finally:
        conn.close()


@pytest.mark.parametrize("fixture", ("pre", "post"))
def test_foreign_changelog_sequence_default_is_not_a_recognised_catalogue(migration_dsn, fixture):
    """A foreign nextval default cannot impersonate the audited changelog serial dependency."""
    if fixture == "pre":
        _install_pre_g002_3_2_7(migration_dsn)
    else:
        _install_post_g002_core(migration_dsn)
    _migration_execute(
        migration_dsn,
        """
        CREATE SEQUENCE foreign_changelog_seq;
        ALTER TABLE memplex_changelog
        ALTER COLUMN id SET DEFAULT nextval('foreign_changelog_seq');
        """,
    )

    with pytest.raises(MigrationIntegrityError, match="unrecognised legacy schema"):
        PostgresMigrationRunner(migration_dsn).plan()


def test_ledger_adoption_digest_must_match_the_current_layout(migration_dsn):
    """A real migration-v2 current schema cannot claim a runtime-v1 adoption baseline."""
    _install_post_g002_core(migration_dsn)
    runner = PostgresMigrationRunner(migration_dsn)
    assert runner.apply().state == "ready"
    from memplex.storage.migrations.runner import _variant_digest

    runtime_baseline = _variant_digest("post_g002_runtime_v1")
    _migration_execute(
        migration_dsn,
        f"""
        UPDATE memplex_schema_migrations
        SET execution_mode = 'adopted', baseline_fingerprint = '{runtime_baseline}'
        WHERE version IN (1, 2)
        """,
    )

    with pytest.raises(MigrationIntegrityError, match="migration ledger integrity check failed"):
        PostgresMigrationRunner(migration_dsn).plan()


@pytest.mark.parametrize(
    "tamper_sql",
    (
        "ALTER TABLE memplex_schema_migrations ENABLE ROW LEVEL SECURITY",
        "ALTER TABLE memplex_schema_migrations FORCE ROW LEVEL SECURITY",
        """
        CREATE POLICY ledger_drift_policy ON memplex_schema_migrations
        TO PUBLIC USING (true) WITH CHECK (true)
        """,
        "ALTER TABLE memplex_schema_migrations RENAME CONSTRAINT memplex_schema_migrations_pkey TO ledger_drift_pkey",
        "ALTER INDEX memplex_schema_migrations_pkey RENAME TO ledger_drift_pkey",
        """
        CREATE FUNCTION ledger_drift_trigger() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RETURN NEW; END $$;
        CREATE TRIGGER ledger_drift_trigger BEFORE INSERT ON memplex_schema_migrations
        FOR EACH ROW EXECUTE FUNCTION ledger_drift_trigger();
        """,
    ),
)
def test_ledger_security_and_identity_drift_fail_closed(migration_dsn, tamper_sql):
    """Ledger RLS, policies, user triggers and canonical PK identities are all authenticated."""
    PostgresMigrationRunner(migration_dsn).apply()
    _migration_execute(migration_dsn, tamper_sql)

    with pytest.raises(MigrationIntegrityError, match="migration ledger has an unrecognised shape"):
        PostgresMigrationRunner(migration_dsn).plan()


@pytest.mark.parametrize(
    "table_name",
    (
        "memplex_schema_migrations",
        "memplex_functions",
        "feedback",
        "memplex_schema_capabilities",
    ),
)
def test_managed_relation_rewrite_rules_fail_closed_in_every_runner_mode(migration_dsn, table_name):
    """Any user rule can rewrite managed writes, so all planning paths must reject it."""
    PostgresMigrationRunner(migration_dsn).apply()
    rule_name = f"g003_rewrite_{table_name.removeprefix('memplex_')}"
    _migration_execute(
        migration_dsn,
        f"CREATE RULE {rule_name} AS ON INSERT TO {table_name} DO INSTEAD NOTHING",
    )

    runner = PostgresMigrationRunner(migration_dsn)
    for operation in (runner.plan, runner.status, lambda: runner.apply(dry_run=True), runner.apply):
        with pytest.raises(MigrationIntegrityError):
            operation()


@pytest.mark.parametrize("table_name", ("memplex_schema_migrations", "memplex_functions"))
def test_managed_table_and_backing_index_owner_must_remain_current_user(
    migration_dsn, table_name
):
    """A whole-owner transfer cannot leave an otherwise exact managed catalogue ready."""
    PostgresMigrationRunner(migration_dsn).apply()
    role_name = f"g003_owner_{uuid.uuid4().hex}"
    conn = psycopg2.connect(migration_dsn)
    try:
        cur = conn.cursor()
        cur.execute(pg_sql.SQL("CREATE ROLE {}").format(pg_sql.Identifier(role_name)))
        cur.execute(
            pg_sql.SQL("ALTER TABLE {} OWNER TO {}").format(
                pg_sql.Identifier(table_name), pg_sql.Identifier(role_name)
            )
        )
        conn.commit()
        cur.execute(
            """
            SELECT table_class.relowner = role.oid,
                   bool_and(index_class.relowner = role.oid)
            FROM pg_catalog.pg_class AS table_class
            JOIN pg_catalog.pg_index AS index_data ON index_data.indrelid = table_class.oid
            JOIN pg_catalog.pg_class AS index_class ON index_class.oid = index_data.indexrelid
            JOIN pg_catalog.pg_roles AS role ON role.rolname = %s
            WHERE table_class.oid = %s::regclass
            GROUP BY table_class.relowner, role.oid
            """,
            (role_name, table_name),
        )
        assert cur.fetchone() == (True, True)
        with pytest.raises(MigrationIntegrityError):
            PostgresMigrationRunner(migration_dsn).plan()
        cur.execute("SELECT current_user")
        current_role = cur.fetchone()[0]
        cur.execute(
            pg_sql.SQL("ALTER TABLE {} OWNER TO {}").format(
                pg_sql.Identifier(table_name), pg_sql.Identifier(current_role)
            )
        )
        cur.execute(pg_sql.SQL("DROP ROLE {}").format(pg_sql.Identifier(role_name)))
        conn.commit()
        cur.close()
    finally:
        conn.close()


def test_capability_insert_rule_rolls_back_vector_schema_and_never_returns_ready(migration_dsn):
    """A rule that swallows the capability row cannot produce a false-ready vector result."""
    if not _vector_extension_is_available(migration_dsn):
        pytest.skip("pgvector is unavailable in this PostgreSQL build")
    PostgresMigrationRunner(migration_dsn).apply()
    _migration_execute(
        migration_dsn,
        """
        CREATE RULE g003_capability_discard AS
        ON INSERT TO memplex_schema_capabilities DO INSTEAD NOTHING
        """,
    )

    with pytest.raises(MigrationIntegrityError):
        PostgresMigrationRunner(migration_dsn).ensure_vector_capability(
            VectorCapabilityRequest(dim=8, policy="required"), "production"
        )
    assert not _migration_column_exists(migration_dsn, "memplex_functions", "embedding")
    conn = psycopg2.connect(migration_dsn)
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM memplex_schema_capabilities")
        assert cur.fetchone() == (0,)
        cur.close()
    finally:
        conn.close()


def test_vector_ready_requires_final_catalogue_convergence(migration_dsn, monkeypatch):
    """A faulty row recorder that only returns a digest must roll back instead of faking ready."""
    if not _vector_extension_is_available(migration_dsn):
        pytest.skip("pgvector is unavailable in this PostgreSQL build")
    runner = PostgresMigrationRunner(migration_dsn)
    monkeypatch.setattr(
        runner,
        "_record_vector_capability",
        lambda _cur, dimension: hashlib.sha256(f"pgvector:{dimension}".encode()).hexdigest(),
    )

    with pytest.raises(MigrationIntegrityError):
        runner.ensure_vector_capability(VectorCapabilityRequest(dim=8, policy="required"), "production")
    assert not _migration_table_exists(migration_dsn, "memplex_schema_migrations")
    assert not _migration_column_exists(migration_dsn, "memplex_functions", "embedding")


def _assert_all_runner_modes_fail_closed(runner: PostgresMigrationRunner) -> None:
    """Every public migration entry point must reject catalog tampering before writes."""
    for operation in (runner.plan, runner.status, lambda: runner.apply(dry_run=True), runner.apply):
        with pytest.raises(MigrationIntegrityError):
            operation()


def _alter_text_column_to_nondefault_collation(migration_dsn, table_name: str, column_name: str) -> None:
    """Apply one real, supported non-default text collation and prove the OID changed."""
    conn = psycopg2.connect(migration_dsn)
    try:
        cur = conn.cursor()
        policy = None
        if (table_name, column_name) == ("memplex_functions", "tenant_id"):
            cur.execute(
                """
                SELECT pol.polname,
                       pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
                       pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid)
                FROM pg_catalog.pg_policy AS pol
                WHERE pol.polrelid = 'memplex_functions'::regclass
                  AND pol.polname = 'memplex_functions_scope'
                """
            )
            policy = cur.fetchone()
            assert policy is not None
            cur.execute("DROP POLICY memplex_functions_scope ON memplex_functions")
        cur.execute(
            """
            SELECT namespace.nspname, coll.collname
            FROM pg_catalog.pg_collation AS coll
            JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = coll.collnamespace
            CROSS JOIN pg_catalog.pg_type AS text_type
            WHERE text_type.oid = 'text'::regtype
              AND coll.oid <> text_type.typcollation
              AND coll.collencoding IN (-1, pg_catalog.pg_char_to_encoding(current_setting('server_encoding')))
            ORDER BY (coll.collname = 'C') DESC, namespace.nspname, coll.collname
            LIMIT 1
            """
        )
        collation_schema, collation_name = cur.fetchone()
        cur.execute(
            pg_sql.SQL(
                "ALTER TABLE {} ALTER COLUMN {} TYPE text COLLATE {} USING {}"
            ).format(
                pg_sql.Identifier(table_name),
                pg_sql.Identifier(column_name),
                pg_sql.Identifier(collation_schema, collation_name),
                pg_sql.Identifier(column_name),
            )
        )
        if policy is not None:
            policy_name, qualify, check = policy
            cur.execute(
                pg_sql.SQL("CREATE POLICY {} ON memplex_functions USING ({}) WITH CHECK ({})").format(
                    pg_sql.Identifier(policy_name), pg_sql.SQL(qualify), pg_sql.SQL(check)
                )
            )
        conn.commit()
        if policy is not None:
            cur.execute(
                """
                SELECT pol.polname,
                       pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
                       pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid)
                FROM pg_catalog.pg_policy AS pol
                WHERE pol.polrelid = 'memplex_functions'::regclass
                  AND pol.polname = 'memplex_functions_scope'
                """
            )
            assert cur.fetchone() == policy
        cur.execute(
            """
            SELECT attribute.attcollation <> typ.typcollation
            FROM pg_catalog.pg_attribute AS attribute
            JOIN pg_catalog.pg_type AS typ ON typ.oid = attribute.atttypid
            WHERE attribute.attrelid = %s::regclass
              AND attribute.attname = %s
              AND NOT attribute.attisdropped
            """,
            (table_name, column_name),
        )
        assert cur.fetchone() == (True,)
        cur.close()
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("table_name", "column_name"),
    (
        ("memplex_functions", "id"),
        ("memplex_functions", "tenant_id"),
        ("feedback", "memory_id"),
        ("memplex_schema_capabilities", "capability_name"),
        ("memplex_schema_migrations", "name"),
    ),
)
def test_nondefault_managed_text_collation_fails_closed_for_every_runner_mode(
    migration_dsn, table_name, column_name
):
    """A managed text column must retain the canonical collation of its declared type."""
    PostgresMigrationRunner(migration_dsn).apply()
    _alter_text_column_to_nondefault_collation(migration_dsn, table_name, column_name)

    _assert_all_runner_modes_fail_closed(PostgresMigrationRunner(migration_dsn))


def test_vector_capability_rejects_collation_drift_before_embedding(migration_dsn):
    """Required vector negotiation cannot write through a non-default primary-key collation."""
    if not _vector_extension_is_available(migration_dsn):
        pytest.skip("pgvector is unavailable in this PostgreSQL build")
    PostgresMigrationRunner(migration_dsn).apply()
    _alter_text_column_to_nondefault_collation(migration_dsn, "memplex_functions", "id")

    with pytest.raises(MigrationIntegrityError):
        PostgresMigrationRunner(migration_dsn).ensure_vector_capability(
            VectorCapabilityRequest(dim=8, policy="required"), "production"
        )
    assert not _migration_column_exists(migration_dsn, "memplex_functions", "embedding")
    conn = psycopg2.connect(migration_dsn)
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM memplex_schema_capabilities")
        assert cur.fetchone() == (0,)
        cur.close()
    finally:
        conn.close()


@pytest.mark.parametrize(
    ("table_name", "event", "action"),
    (
        ("memplex_functions", "INSERT", "DO INSTEAD NOTHING"),
        ("memplex_functions", "UPDATE", "DO ALSO NOTIFY g003_rule"),
        ("feedback", "DELETE", "DO INSTEAD NOTHING"),
        ("feedback", "DELETE", "DO ALSO NOTIFY g003_rule"),
        ("memplex_schema_capabilities", "INSERT", "DO ALSO NOTIFY g003_rule"),
        ("memplex_schema_migrations", "UPDATE", "DO INSTEAD NOTHING"),
    ),
)
def test_renamed_user_rewrite_rule_fails_closed_for_every_runner_mode(
    migration_dsn, table_name, event, action
):
    """A user rule renamed _RETURN remains a row in pg_rewrite on a regular table."""
    PostgresMigrationRunner(migration_dsn).apply()
    _migration_execute(
        migration_dsn,
        f"CREATE RULE g003_return_rule AS ON {event} TO {table_name} {action}; "
        f'ALTER RULE g003_return_rule ON {table_name} RENAME TO "_RETURN"',
    )

    _assert_all_runner_modes_fail_closed(PostgresMigrationRunner(migration_dsn))


@pytest.mark.parametrize(
    "table_name",
    (
        "memplex_functions",
        "memplex_edges",
        "memplex_observations",
        "memplex_facts",
        "memplex_preferences",
        "memplex_changelog",
        "feedback",
        "memplex_schema_capabilities",
        "memplex_schema_migrations",
    ),
)
def test_managed_user_trigger_fails_closed_for_every_runner_mode(migration_dsn, table_name):
    """A user trigger can change a managed write, regardless of its event type."""
    PostgresMigrationRunner(migration_dsn).apply()
    _migration_execute(
        migration_dsn,
        f"""
        CREATE FUNCTION g003_drop_trigger_{table_name.removeprefix('memplex_')}()
        RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NULL; END $$;
        CREATE TRIGGER g003_drop_trigger
        BEFORE INSERT OR UPDATE OR DELETE ON {table_name}
        FOR EACH ROW EXECUTE FUNCTION g003_drop_trigger_{table_name.removeprefix('memplex_')}();
        """,
    )

    _assert_all_runner_modes_fail_closed(PostgresMigrationRunner(migration_dsn))


def test_internal_foreign_key_trigger_does_not_drift_managed_catalogue(migration_dsn):
    """Only user triggers count; PostgreSQL's internal referential triggers remain valid."""
    PostgresMigrationRunner(migration_dsn).apply()
    _migration_execute(
        migration_dsn,
        """
        CREATE TABLE g003_internal_trigger_child (
            tenant_id TEXT NOT NULL,
            function_id TEXT NOT NULL,
            FOREIGN KEY (tenant_id, function_id)
                REFERENCES memplex_functions (tenant_id, id)
        )
        """,
    )

    assert PostgresMigrationRunner(migration_dsn).plan().state == "ready"


@pytest.mark.parametrize(
    "table_name",
    (
        "memplex_functions",
        "memplex_edges",
        "memplex_observations",
        "memplex_facts",
        "memplex_preferences",
        "memplex_changelog",
        "feedback",
        "memplex_schema_capabilities",
        "memplex_schema_migrations",
    ),
)
def test_managed_table_public_acl_fails_closed_for_every_runner_mode(migration_dsn, table_name):
    """Any relation ACL grant expands the managed database authority surface."""
    PostgresMigrationRunner(migration_dsn).apply()
    _migration_execute(migration_dsn, f"GRANT SELECT ON TABLE {table_name} TO PUBLIC")

    _assert_all_runner_modes_fail_closed(PostgresMigrationRunner(migration_dsn))


@pytest.mark.parametrize(
    ("table_name", "column_name"),
    (
        ("memplex_functions", "data"),
        ("feedback", "provenance"),
        ("memplex_schema_capabilities", "parameter_digest"),
        ("memplex_schema_migrations", "checksum"),
    ),
)
def test_managed_column_public_acl_fails_closed_for_every_runner_mode(
    migration_dsn, table_name, column_name
):
    """A column grant is as much a catalog ACL drift as a table grant."""
    PostgresMigrationRunner(migration_dsn).apply()
    _migration_execute(
        migration_dsn, f"GRANT UPDATE ({column_name}) ON TABLE {table_name} TO PUBLIC"
    )

    _assert_all_runner_modes_fail_closed(PostgresMigrationRunner(migration_dsn))


def test_changelog_sequence_public_acl_fails_closed_for_every_runner_mode(migration_dsn):
    """The serial sequence must not gain an independently writable ACL."""
    PostgresMigrationRunner(migration_dsn).apply()
    _migration_execute(
        migration_dsn, "GRANT USAGE, SELECT, UPDATE ON SEQUENCE memplex_changelog_id_seq TO PUBLIC"
    )

    _assert_all_runner_modes_fail_closed(PostgresMigrationRunner(migration_dsn))


def test_temp_role_relation_column_and_sequence_acls_fail_closed(migration_dsn):
    """A named grantee is no safer than PUBLIC on managed relation, column and sequence ACLs."""
    PostgresMigrationRunner(migration_dsn).apply()
    role_name = f"g003_acl_{uuid.uuid4().hex}"
    conn = psycopg2.connect(migration_dsn)
    try:
        cur = conn.cursor()
        cur.execute(pg_sql.SQL("CREATE ROLE {}").format(pg_sql.Identifier(role_name)))
        cur.execute(
            pg_sql.SQL("GRANT SELECT ON TABLE memplex_functions TO {}").format(
                pg_sql.Identifier(role_name)
            )
        )
        cur.execute(
            pg_sql.SQL("GRANT UPDATE (data) ON TABLE memplex_functions TO {}").format(
                pg_sql.Identifier(role_name)
            )
        )
        cur.execute(
            pg_sql.SQL("GRANT USAGE ON SEQUENCE memplex_changelog_id_seq TO {}").format(
                pg_sql.Identifier(role_name)
            )
        )
        conn.commit()
        _assert_all_runner_modes_fail_closed(PostgresMigrationRunner(migration_dsn))
        cur.execute(
            pg_sql.SQL("REVOKE ALL ON TABLE memplex_functions FROM {}").format(
                pg_sql.Identifier(role_name)
            )
        )
        cur.execute(
            pg_sql.SQL("REVOKE ALL ON SEQUENCE memplex_changelog_id_seq FROM {}").format(
                pg_sql.Identifier(role_name)
            )
        )
        cur.execute(pg_sql.SQL("DROP ROLE {}").format(pg_sql.Identifier(role_name)))
        conn.commit()
        cur.close()
    finally:
        conn.close()


@pytest.mark.parametrize(
    "table_name",
    (
        "memplex_functions",
        "feedback",
        "memplex_schema_capabilities",
        "memplex_schema_migrations",
        "memplex_changelog",
    ),
)
def test_unlogged_managed_table_fails_closed_for_every_runner_mode(migration_dsn, table_name):
    """A crash-unsafe managed relation cannot be reported ready."""
    PostgresMigrationRunner(migration_dsn).apply()
    if table_name == "memplex_functions":
        # 0004 correctly makes the function PK a referenced key.  Remove the
        # two edge FKs solely to reach the unrelated persistence-drift probe.
        _migration_execute(
            migration_dsn,
            """
            ALTER TABLE memplex_edges DROP CONSTRAINT memplex_edges_source_function_fk;
            ALTER TABLE memplex_edges DROP CONSTRAINT memplex_edges_target_function_fk;
            ALTER TABLE memplex_functions SET UNLOGGED
            """,
        )
    else:
        _migration_execute(migration_dsn, f"ALTER TABLE {table_name} SET UNLOGGED")

    _assert_all_runner_modes_fail_closed(PostgresMigrationRunner(migration_dsn))


@pytest.mark.parametrize(
    "tamper_sql",
    (
        "ALTER SEQUENCE memplex_changelog_id_seq OWNED BY NONE",
        "ALTER SEQUENCE memplex_changelog_id_seq INCREMENT BY 17",
        "ALTER SEQUENCE memplex_changelog_id_seq CACHE 23",
        "ALTER SEQUENCE memplex_changelog_id_seq CYCLE",
        "ALTER SEQUENCE memplex_changelog_id_seq AS integer START WITH 7 MAXVALUE 123456 CACHE 29 CYCLE",
        "ALTER SEQUENCE memplex_changelog_id_seq SET UNLOGGED",
    ),
)
def test_changelog_sequence_descriptor_drift_fails_closed_for_every_runner_mode(
    migration_dsn, tamper_sql
):
    """The serial sequence descriptor and OWNED BY edge are part of the schema contract."""
    PostgresMigrationRunner(migration_dsn).apply()
    _migration_execute(migration_dsn, tamper_sql)

    _assert_all_runner_modes_fail_closed(PostgresMigrationRunner(migration_dsn))


def test_detached_changelog_sequence_owner_and_role_acl_fail_closed(migration_dsn):
    """A detached sequence with owner/ACL drift cannot impersonate the audited serial object."""
    PostgresMigrationRunner(migration_dsn).apply()
    role_name = f"g003_sequence_{uuid.uuid4().hex}"
    conn = psycopg2.connect(migration_dsn)
    try:
        cur = conn.cursor()
        cur.execute(pg_sql.SQL("CREATE ROLE {}").format(pg_sql.Identifier(role_name)))
        cur.execute("ALTER SEQUENCE memplex_changelog_id_seq OWNED BY NONE")
        cur.execute(
            pg_sql.SQL("ALTER SEQUENCE memplex_changelog_id_seq OWNER TO {}").format(
                pg_sql.Identifier(role_name)
            )
        )
        cur.execute(
            pg_sql.SQL("GRANT USAGE ON SEQUENCE memplex_changelog_id_seq TO {}").format(
                pg_sql.Identifier(role_name)
            )
        )
        conn.commit()
        _assert_all_runner_modes_fail_closed(PostgresMigrationRunner(migration_dsn))
        cur.execute("SELECT current_user")
        cur.execute(
            pg_sql.SQL("ALTER SEQUENCE memplex_changelog_id_seq OWNER TO {}").format(
                pg_sql.Identifier(cur.fetchone()[0])
            )
        )
        cur.execute("ALTER SEQUENCE memplex_changelog_id_seq OWNED BY memplex_changelog.id")
        cur.execute(
            pg_sql.SQL("REVOKE ALL ON SEQUENCE memplex_changelog_id_seq FROM {}").format(
                pg_sql.Identifier(role_name)
            )
        )
        cur.execute(pg_sql.SQL("DROP ROLE {}").format(pg_sql.Identifier(role_name)))
        conn.commit()
        cur.close()
    finally:
        conn.close()


def test_managed_partition_child_fails_closed_for_every_runner_mode(migration_dsn):
    """A managed table attached to an unmanaged partition parent is not standalone catalog state."""
    PostgresMigrationRunner(migration_dsn).apply()
    _migration_execute(
        migration_dsn,
        """
        CREATE TABLE g003_partition_parent (
            id TEXT NOT NULL,
            data JSONB NOT NULL,
            updated_at TIMESTAMPTZ,
            tenant_id TEXT NOT NULL,
            owner_subject TEXT NOT NULL,
            workspace TEXT NOT NULL,
            visibility TEXT NOT NULL,
            source_agent TEXT NOT NULL,
            source_session TEXT NOT NULL,
            PRIMARY KEY (tenant_id, id)
        ) PARTITION BY LIST (id);
        ALTER TABLE g003_partition_parent
            ATTACH PARTITION memplex_facts FOR VALUES IN ('g003-partition-probe')
        """,
    )

    _assert_all_runner_modes_fail_closed(PostgresMigrationRunner(migration_dsn))


_POST_PRIMARY_KEYS = {
    "memplex_functions": ("tenant_id", "id"),
    "memplex_edges": ("tenant_id", "source", "edge_type", "target"),
    "memplex_observations": ("tenant_id", "id"),
    "memplex_facts": ("tenant_id", "id"),
    "memplex_preferences": ("tenant_id", "id"),
    "memplex_changelog": ("tenant_id", "id"),
    "memplex_schema_capabilities": ("capability_name",),
    "memplex_schema_migrations": ("version",),
}


@pytest.mark.parametrize("table_name", tuple(_POST_PRIMARY_KEYS))
def test_deferrable_managed_primary_key_fails_closed_for_every_runner_mode(migration_dsn, table_name):
    """A deferrable PK breaks ON CONFLICT and cannot satisfy the current catalog contract."""
    PostgresMigrationRunner(migration_dsn).apply()
    columns = ", ".join(_POST_PRIMARY_KEYS[table_name])
    dependent_edge_foreign_keys = (
        "ALTER TABLE memplex_edges DROP CONSTRAINT memplex_edges_source_function_fk;\n"
        "ALTER TABLE memplex_edges DROP CONSTRAINT memplex_edges_target_function_fk;"
        if table_name == "memplex_functions"
        else ""
    )
    _migration_execute(
        migration_dsn,
        f"""
        {dependent_edge_foreign_keys}
        ALTER TABLE {table_name} DROP CONSTRAINT {table_name}_pkey;
        ALTER TABLE {table_name} ADD CONSTRAINT {table_name}_pkey
        PRIMARY KEY ({columns}) DEFERRABLE INITIALLY DEFERRED
        """,
    )

    _assert_all_runner_modes_fail_closed(PostgresMigrationRunner(migration_dsn))


def test_deferrable_functions_primary_key_breaks_real_on_conflict(migration_dsn):
    """PostgreSQL rejects the store-facing ON CONFLICT arbiter when its PK is deferred."""
    PostgresMigrationRunner(migration_dsn).apply()
    _migration_execute(
        migration_dsn,
        """
        ALTER TABLE memplex_edges DROP CONSTRAINT memplex_edges_source_function_fk;
        ALTER TABLE memplex_edges DROP CONSTRAINT memplex_edges_target_function_fk;
        ALTER TABLE memplex_functions DROP CONSTRAINT memplex_functions_pkey;
        ALTER TABLE memplex_functions ADD CONSTRAINT memplex_functions_pkey
        PRIMARY KEY (tenant_id, id) DEFERRABLE INITIALLY DEFERRED
        """,
    )
    conn = psycopg2.connect(migration_dsn)
    try:
        cur = conn.cursor()
        with pytest.raises(psycopg2.errors.ObjectNotInPrerequisiteState, match="deferrable"):
            cur.execute(
                """
                INSERT INTO memplex_functions
                    (id, data, tenant_id, owner_subject, workspace, visibility, source_agent, source_session)
                VALUES
                    ('g003-deferrable', '{}', 'tenant', 'subject', 'workspace', 'workspace', 'agent', 'session')
                ON CONFLICT (tenant_id, id) DO NOTHING
                """
            )
        conn.rollback()
        cur.close()
    finally:
        conn.close()


@pytest.mark.parametrize(
    "tamper_sql",
    (
        "GRANT SELECT ON TABLE memplex_schema_capabilities TO PUBLIC",
        "CREATE RULE g003_vector_rule AS ON INSERT TO memplex_schema_capabilities DO INSTEAD NOTHING",
        """
        CREATE RULE g003_vector_return_rule AS
        ON INSERT TO memplex_schema_capabilities DO INSTEAD NOTHING;
        ALTER RULE g003_vector_return_rule ON memplex_schema_capabilities RENAME TO "_RETURN"
        """,
        """
        CREATE FUNCTION g003_vector_trigger() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RETURN NULL; END $$;
        CREATE TRIGGER g003_vector_trigger BEFORE INSERT ON memplex_schema_capabilities
        FOR EACH ROW EXECUTE FUNCTION g003_vector_trigger()
        """,
        "ALTER TABLE memplex_schema_capabilities SET UNLOGGED",
        "ALTER SEQUENCE memplex_changelog_id_seq CACHE 2",
        """
        ALTER TABLE memplex_schema_capabilities DROP CONSTRAINT memplex_schema_capabilities_pkey;
        ALTER TABLE memplex_schema_capabilities ADD CONSTRAINT memplex_schema_capabilities_pkey
        PRIMARY KEY (capability_name) DEFERRABLE INITIALLY DEFERRED
        """,
    ),
)
def test_vector_capability_catalogue_drift_rolls_back_before_embedding(migration_dsn, tamper_sql):
    """Capability negotiation must fail before vector DDL when any managed catalog gate drifts."""
    if not _vector_extension_is_available(migration_dsn):
        pytest.skip("pgvector is unavailable in this PostgreSQL build")
    PostgresMigrationRunner(migration_dsn).apply()
    _migration_execute(migration_dsn, tamper_sql)

    with pytest.raises(MigrationIntegrityError):
        PostgresMigrationRunner(migration_dsn).ensure_vector_capability(
            VectorCapabilityRequest(dim=8, policy="required"), "production"
        )
    assert not _migration_column_exists(migration_dsn, "memplex_functions", "embedding")


# ── Helpers ──────────────────────────────────────────────────────────

SRC = SourceDocument(type="file", source_path="wiki/auth.md", source_type=SourceType.WIKI)


def _fv(desc, **kw):
    return FieldValue(desc=desc, sources=["s1"], source_method="manual", **kw)


def _func(fid, name, **kw):
    defaults = dict(
        name_normalized=name.strip().lower(),
        domain="auth",
        confidence=0.9,
        source_type=SourceType.CODE,
        trigger=[_fv(f"{name} trigger")],
        action=[_fv(f"{name} action")],
    )
    defaults.update(kw)
    return Function(id=fid, name=name, **defaults)


def _authorization(
    *,
    tenant,
    subject,
    workspace="shared-workspace",
    agent="http",
    session=None,
):
    from memplex.auth import AuthorizationContext, Principal

    return AuthorizationContext(
        principal=Principal(
            tenant_id=tenant,
            subject_id=subject,
            roles=frozenset({"member"}),
            authentication_id=f"credential-{subject}",
        ),
        workspace_id=workspace,
        agent_id=agent,
        session_id=session or f"session-{subject}",
        request_id=f"request-{subject}",
    )


class _BagOfWordsEmbedder:
    """Deterministic stand-in embedder: hashes whitespace tokens into a
    fixed-dimension bag-of-words vector, so texts sharing tokens get
    small cosine distance. Only used to exercise the pgvector code path."""

    def __init__(self, dim):
        self.dim = dim

    def embed(self, text):
        vec = [0.0] * self.dim
        for tok in str(text).lower().split():
            vec[hash(tok) % self.dim] += 1.0
        return vec


# ── Tenant authorization / RLS ─────────────────────────────────────


class TestTenantAuthorization:
    def test_same_external_id_and_name_are_independent_per_tenant(self, store, pg_dsn):
        alice = store.authorized(_authorization(tenant="tenant-a", subject="alice"))
        bob = store.authorized(_authorization(tenant="tenant-b", subject="bob"))

        alice.add(_func("shared-id", "Shared Canonical", domain="alice-domain"), SRC)
        bob.add(_func("shared-id", "Shared Canonical", domain="bob-domain"), SRC)

        assert alice.get("shared-id").domain == "alice-domain"
        assert bob.get("shared-id").domain == "bob-domain"
        assert [item.domain for item in alice.list_functions(limit=10)] == ["alice-domain"]
        assert [item.domain for item in bob.list_functions(limit=10)] == ["bob-domain"]
        assert [item.func_id for item in alice.fts_search("shared", top_k=10)] == [
            "shared-id"
        ]
        assert [item.func_id for item in bob.fts_search("shared", top_k=10)] == [
            "shared-id"
        ]

        assert _admin_query(
            pg_dsn,
            "SELECT count(*) FROM memplex_functions WHERE id = %s",
            ("shared-id",),
        ) == [(2,)]

    def test_schema_enables_and_forces_rls_on_every_memory_table(self, store, pg_dsn):
        expected = {
            "memplex_functions",
            "memplex_edges",
            "memplex_observations",
            "memplex_facts",
            "memplex_preferences",
            "memplex_changelog",
        }
        rows = {
            name: (enabled, forced)
            for name, enabled, forced in _admin_query(
                pg_dsn,
                """
                SELECT relname, relrowsecurity, relforcerowsecurity
                FROM pg_class
                WHERE relname = ANY(%s)
                """,
                (list(expected),),
            )
        }

        assert set(rows) == expected
        assert all(enabled and forced for enabled, forced in rows.values())

    def test_production_service_executes_through_authorized_postgres_scope(self, migration_dsn):
        config, role = _production_service_config(migration_dsn)
        service = MemplexService(config=config)
        alice_context = _authorization(tenant="tenant-service-a", subject="alice")
        bob_context = _authorization(tenant="tenant-service-b", subject="bob")
        try:
            assert service.store._require_authorization is True
            with pytest.raises(PermissionError, match="authorization context"):
                service.store.get("unscoped-production-read")

            written = service.write_text(
                "Remember production service tenant scope canary.",
                authorization=alice_context,
            )
            memory_id = written.functions[0].id

            assert service.get(memory_id, authorization=alice_context) is not None
            assert service.get(memory_id, authorization=bob_context) is None
            assert service.query(
                "production service tenant scope canary",
                top_k=10,
                authorization=bob_context,
            ).results == []
            assert memory_id in {
                result.func_id
                for result in service.query(
                    "production service tenant scope canary",
                    top_k=10,
                    authorization=alice_context,
                ).results
            }
        finally:
            service.stop()
            _drop_feedback_role(migration_dsn, role)

    def test_production_sync_wrapper_keeps_authorized_pg_push_and_pull_closed(
        self, migration_dsn, monkeypatch
    ):
        """A scoped production sync call must retain both PG ACL and remote I/O.

        This is intentionally a real PostgreSQL regression: before the
        SyncableStore facade, ``service._store_for(context)`` returned the
        inner authorized PostgreSQL store directly.  The write succeeded but
        never queued a remote push; pull then used the unscoped strict store
        and silently skipped every remote node.
        """

        token = "pg-sync-principal-token"
        tenant = "tenant-sync-production"
        workspace = "sync-workspace"
        monkeypatch.setenv("MEMPLEX_REMOTE_URL", "https://sync.invalid")
        monkeypatch.setenv("MEMPLEX_DEPLOYMENT_PROFILE", "production")
        monkeypatch.setenv(
            "MEMPLEX_PRINCIPALS_JSON",
            json.dumps(
                [
                    {
                        "credential_id": "postgres-sync-principal",
                        "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                        "tenant_id": tenant,
                        "subject_id": "alice",
                        "workspace_id": workspace,
                        "roles": ["host"],
                    }
                ]
            ),
        )
        monkeypatch.setenv("MEMPLEX_PRINCIPAL_TOKEN", token)

        class _RemoteStub:
            def __init__(self):
                self.posted = []
                self.get_called = threading.Event()
                self.background_function = _func(
                    "background-sync-node",
                    "Background Sync Node",
                    updated_at="2029-01-01T00:00:00+00:00",
                )
                self.remote_function = _func(
                    "remote-sync-node",
                    "Remote Sync Node",
                    updated_at="2030-01-01T00:00:00+00:00",
                    tenant_id="forged-remote-tenant",
                    owner_subject_id="mallory",
                    workspace_id="forged-remote-workspace",
                )
                self._pulls = 0

            def post(self, url, json=None, headers=None, timeout=None):
                self.posted.append((url, json, headers))
                return type("Response", (), {"status_code": 200})()

            def get(self, url, params=None, headers=None, timeout=None):
                self._pulls += 1
                self.get_called.set()
                remote_function = (
                    self.background_function if self._pulls == 1 else self.remote_function
                )
                payload = {
                    "changes": [remote_function.to_dict()],
                    "tombstones": [],
                    "server_time": "2030-01-01T00:00:01+00:00",
                }
                return type(
                    "Response",
                    (),
                    {"status_code": 200, "raise_for_status": lambda self: None, "json": lambda self: payload},
                )()

            def delete(self, url, headers=None, timeout=None):
                return type("Response", (), {"status_code": 200})()

        config, role = _production_service_config(migration_dsn)
        service = MemplexService(config=config)
        alice = _authorization(tenant=tenant, subject="alice", workspace=workspace)
        bob = _authorization(tenant="other-tenant", subject="bob", workspace=workspace)
        remote = _RemoteStub()
        try:
            assert isinstance(service.store, SyncableStore)
            service.store._http = remote

            written = service.write_text("Remember scoped PostgreSQL remote push.", authorization=alice)
            local_id = written.functions[0].id
            service.store.flush_push()

            assert any(
                local_id in {item["id"] for item in body.get("functions", [])}
                for _url, body, _headers in remote.posted
            )
            # Managed remote identity is restricted to the worker pull path;
            # arbitrary raw store reads and writes still fail closed.
            with pytest.raises(PermissionError, match="authorization context"):
                service.store.get(local_id)
            with pytest.raises(PermissionError, match="authorization context"):
                service.store.add(_func("raw-sync-write", "Raw Sync Write"), SRC)
            # Delegated reads resolve after the facade has installed its
            # context; this used to capture a raw strict-PG method first.
            assert service.store.authorized(alice).get(local_id) is not None
            assert service.store.authorized(bob).get(local_id) is None
            assert service.get(local_id, authorization=alice) is not None
            assert service.get(local_id, authorization=bob) is None
            assert local_id in {
                item.func_id
                for item in service.query(
                    "scoped PostgreSQL remote push", top_k=10, authorization=alice
                ).results
            }
            assert service.query(
                "scoped PostgreSQL remote push", top_k=10, authorization=bob
            ).results == []

            # Background workers do not carry a request ContextVar.  They
            # may use only RemoteSyncConfig's registry-validated managed
            # identity, never an unscoped strict PG operation.
            service.store.start_auto_pull(interval=1)
            assert remote.get_called.wait(timeout=3)
            service.store.stop_auto_pull()
            assert service.get("background-sync-node", authorization=alice) is not None
            assert service.get("background-sync-node", authorization=bob) is None

            pulled = service.store.authorized(alice).pull_incremental()
            assert pulled["applied"] == 1
            pulled_node = service.get("remote-sync-node", authorization=alice)
            assert pulled_node is not None
            # Pull applies only under the trusted request scope: forged wire
            # claims cannot create a readable node in another tenant.
            assert pulled_node.tenant_id == tenant
            assert service.get("remote-sync-node", authorization=bob) is None

            # A configured URL is not itself an authorization grant.  If a
            # managed worker identity is unavailable, even raw pull fails
            # before making a permissive local PG call.
            service.store._config.authorization = None
            with pytest.raises(PermissionError, match="production sync pull"):
                service.store.pull_incremental()
        finally:
            service.stop()
            _drop_feedback_role(migration_dsn, role)

    def test_production_registry_runtime_wraps_recall_through_authorized_postgres_scope(
        self,
        migration_dsn,
        monkeypatch,
    ):
        """Runtime context wrapping must never fall back to an unscoped PG lookup."""

        token = "postgres-runtime-principal-token"
        monkeypatch.setenv(
            "MEMPLEX_PRINCIPALS_JSON",
            json.dumps(
                [
                    {
                        "credential_id": "postgres-runtime-principal",
                        "token_sha256": hashlib.sha256(token.encode("utf-8")).hexdigest(),
                        "tenant_id": "tenant-runtime-production",
                        "subject_id": "runtime-alice",
                        "workspace_id": "runtime-workspace",
                        "agent_id": "",
                        "roles": ["host"],
                    }
                ]
            ),
        )
        monkeypatch.setenv("MEMPLEX_PRINCIPAL_TOKEN", token)
        monkeypatch.delenv("MEMPLEX_REMOTE_URL", raising=False)
        monkeypatch.delenv("MEMPLEX_PEERS", raising=False)

        config, role = _production_service_config(migration_dsn)
        service = MemplexService(config=config)
        runtime = AgentMemoryRuntime(
            service=service,
            agent="codex",
            user_id="forged-local-user",
            session_id="runtime-production-session",
            project_path="/forged/local/workspace",
        )
        canary = "pg-runtime-recall-71bd"
        try:
            runtime.write_text(f"Remember {canary} for authorized runtime recall.")

            assert canary in runtime.before_prompt(canary).context
            assert canary in runtime.prefetch(canary).context
            runtime.after_response(
                user_message=f"Remember structured observation {canary}.",
                assistant_message="Captured through the production runtime.",
            )
            assert any(
                canary in observation.context
                for observation in service.list_observations(
                    authorization=runtime.authorization_context
                )
            )
        finally:
            service.stop()
            _drop_feedback_role(migration_dsn, role)

    def test_production_registry_mcp_observations_use_authorized_postgres_scope(
        self,
        migration_dsn,
        monkeypatch,
    ):
        """MCP observation listing must neither use raw PG nor leak tenants."""

        alice_token = "postgres-mcp-alice-token"
        bob_token = "postgres-mcp-bob-token"
        monkeypatch.setenv(
            "MEMPLEX_PRINCIPALS_JSON",
            json.dumps(
                [
                    {
                        "credential_id": "postgres-mcp-alice",
                        "token_sha256": hashlib.sha256(
                            alice_token.encode("utf-8")
                        ).hexdigest(),
                        "tenant_id": "tenant-mcp-alice",
                        "subject_id": "alice",
                        "workspace_id": "workspace-mcp-alice",
                        "agent_id": "",
                        "roles": ["host"],
                    },
                    {
                        "credential_id": "postgres-mcp-bob",
                        "token_sha256": hashlib.sha256(
                            bob_token.encode("utf-8")
                        ).hexdigest(),
                        "tenant_id": "tenant-mcp-bob",
                        "subject_id": "bob",
                        "workspace_id": "workspace-mcp-bob",
                        "agent_id": "",
                        "roles": ["host"],
                    },
                ]
            ),
        )
        monkeypatch.setenv("MEMPLEX_AGENT_ID", "codex")
        monkeypatch.setenv("MEMPLEX_SESSION_ID", "mcp-production-session")
        monkeypatch.delenv("MEMPLEX_REMOTE_URL", raising=False)
        monkeypatch.delenv("MEMPLEX_PEERS", raising=False)

        config, role = _production_service_config(migration_dsn)
        server = MCPServer(config=config)
        server._ensure_service()
        canary = "pg-mcp-observation-alice-42ce"
        try:
            monkeypatch.setenv("MEMPLEX_PRINCIPAL_TOKEN", alice_token)
            server._tool_memory_turn_end(
                {
                    "user_message": f"Remember {canary}.",
                    "assistant_message": "Captured for Alice.",
                }
            )
            alice_result = server._tool_memory_observations(
                {"query": canary, "limit": 10}
            )
            assert alice_result["total"] == 1
            assert canary in alice_result["observations"][0]["summary"]

            monkeypatch.setenv("MEMPLEX_PRINCIPAL_TOKEN", bob_token)
            bob_result = server._tool_memory_observations(
                {"query": canary, "limit": 10}
            )
            assert bob_result == {"total": 0, "observations": []}
        finally:
            server._service.stop()
            _drop_feedback_role(migration_dsn, role)

    def test_session_visibility_is_preserved_in_relational_acl_and_service(self, migration_dsn):
        config, role = _production_service_config(migration_dsn)
        service = MemplexService(config=config)
        alice_context = _authorization(tenant="tenant-session", subject="alice")
        bob_context = _authorization(tenant="tenant-session", subject="bob")
        try:
            written = service.write_text(
                "Remember this session-only PostgreSQL ACL canary.",
                visibility="session",
                authorization=alice_context,
            )
            memory_id = written.functions[0].id

            relational_visibility = _admin_query(
                migration_dsn,
                "SELECT visibility FROM memplex_functions WHERE tenant_id = %s AND id = %s",
                (alice_context.principal.tenant_id, memory_id),
            )[0][0]

            assert service.get(memory_id, authorization=alice_context) is not None
            bob_direct = service.store.authorized(bob_context).get(memory_id)
            bob_service = service.get(memory_id, authorization=bob_context)
            assert (relational_visibility, bob_direct, bob_service) == (
                "session",
                None,
                None,
            )
        finally:
            service.stop()
            _drop_feedback_role(migration_dsn, role)

    def test_workspace_memory_can_be_updated_and_deleted_by_another_workspace_member(
        self, migration_dsn
    ):
        from memplex.auth import MemoryNotFoundError

        config, role = _production_service_config(migration_dsn)
        service = MemplexService(config=config)
        alice_context = _authorization(
            tenant="tenant-workspace-mutation",
            subject="alice",
            session="session-alice",
        )
        bob_context = _authorization(
            tenant="tenant-workspace-mutation",
            subject="bob",
            session="session-bob",
        )
        try:
            workspace_write = service.write_text(
                "Remember the shared workspace mutation contract.",
                visibility="workspace",
                authorization=alice_context,
            )
            workspace_id = workspace_write.functions[0].id
            assert service.get(workspace_id, authorization=bob_context) is not None

            result = service.update_memory(
                workspace_id,
                "trigger",
                "Bob updated the shared workspace memory.",
                authorization=bob_context,
            )
            assert result.success is True

            updated = service.get(workspace_id, authorization=alice_context)
            assert updated is not None
            assert updated.trigger[0].desc == "Bob updated the shared workspace memory."
            assert updated.owner == "bob"
            assert updated.owner_subject_id == "bob"
            assert updated.provenance["authentication_id"] == "credential-bob"
            assert updated.provenance["session_id"] == "session-bob"

            assert _admin_query(
                migration_dsn,
                "SELECT owner_subject, workspace, source_agent, source_session "
                "FROM memplex_functions WHERE tenant_id = %s AND id = %s",
                (bob_context.principal.tenant_id, workspace_id),
            ) == [(
                "bob",
                bob_context.workspace_id,
                bob_context.agent_id,
                bob_context.session_id,
            )]

            user_write = service.write_text(
                "Remember the user-private mutation boundary.",
                visibility="user",
                authorization=alice_context,
            )
            session_write = service.write_text(
                "Remember the session-private mutation boundary.",
                visibility="session",
                authorization=alice_context,
            )
            for private_id in (
                user_write.functions[0].id,
                session_write.functions[0].id,
            ):
                assert service.get(private_id, authorization=bob_context) is None
                with pytest.raises(MemoryNotFoundError, match="Memory not found"):
                    service.update_memory(
                        private_id,
                        "trigger",
                        "Bob must not update private memory.",
                        authorization=bob_context,
                    )
                with pytest.raises(MemoryNotFoundError, match="Memory not found"):
                    service.delete(private_id, authorization=bob_context)

            service.delete(workspace_id, authorization=bob_context)
            assert service.get(workspace_id, authorization=alice_context) is None
            assert service.get(workspace_id, authorization=bob_context) is None
        finally:
            service.stop()
            _drop_feedback_role(migration_dsn, role)

    def test_concurrent_scoped_reads_never_share_transaction_identity(
        self, store, monkeypatch
    ):
        alice_context = _authorization(tenant="tenant-concurrent-a", subject="alice")
        bob_context = _authorization(tenant="tenant-concurrent-b", subject="bob")
        alice = store.authorized(alice_context)
        bob = store.authorized(bob_context)
        alice.add(_func("concurrent-shared", "Concurrent", domain="alice-domain"), SRC)
        bob.add(_func("concurrent-shared", "Concurrent", domain="bob-domain"), SRC)

        original_bind = store._bind_transaction_scope
        round_state = {
            "alice_bound": threading.Event(),
            "bob_started": threading.Event(),
        }

        def delayed_alice_scope(cur, context):
            original_bind(cur, context)
            if context.principal.tenant_id == "tenant-concurrent-a":
                round_state["alice_bound"].set()
                # Without a store-wide transaction critical section this lets
                # Bob overwrite SET LOCAL on the shared connection before
                # Alice executes her SELECT.
                assert round_state["bob_started"].wait(timeout=5)

        monkeypatch.setattr(store, "_bind_transaction_scope", delayed_alice_scope)

        for _ in range(10):
            start = threading.Barrier(2)
            round_state["alice_bound"] = threading.Event()
            round_state["bob_started"] = threading.Event()

            alice_bound = round_state["alice_bound"]
            bob_started = round_state["bob_started"]

            def read_alice(start_gate=start):
                start_gate.wait(timeout=5)
                node = alice.get("concurrent-shared")
                return node.domain if node is not None else None

            def read_bob(
                start_gate=start,
                alice_bound_gate=alice_bound,
                bob_started_gate=bob_started,
            ):
                start_gate.wait(timeout=5)
                assert alice_bound_gate.wait(timeout=5)
                bob_started_gate.set()
                node = bob.get("concurrent-shared")
                return node.domain if node is not None else None

            with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
                alice_result = pool.submit(read_alice)
                bob_result = pool.submit(read_bob)
                assert alice_result.result(timeout=5) == "alice-domain"
                assert bob_result.result(timeout=5) == "bob-domain"

    def test_session_visibility_covers_typed_graph_and_changelog_tables(self, store, pg_dsn):
        alice_context = _authorization(tenant="tenant-session-matrix", subject="alice")
        bob_context = _authorization(tenant="tenant-session-matrix", subject="bob")
        alice = store.authorized(alice_context)
        bob = store.authorized(bob_context)

        function = _func("session-function", "Session Function")
        function.visibility = "session"
        fact = Fact(id="session-fact", name="Session Fact", subject="alice")
        fact.visibility = "session"
        preference = Preference(
            id="session-preference",
            name="Session Preference",
            aspect="editor",
            preference="compact",
        )
        preference.visibility = "session"
        observation = Observation(id="session-observation", name="Session Observation")
        observation.visibility = "session"
        graph_left = _func("session-graph-left", "Session Graph Left")
        graph_left.visibility = "session"
        graph_right = _func("session-graph-right", "Session Graph Right")
        graph_right.visibility = "session"

        alice.add(function, SRC)
        alice.add_fact(fact)
        alice.add_preference(preference)
        alice.add_observation(observation)
        alice.merge(
            GraphData(
                nodes=[graph_left, graph_right],
                edges=[
                    GraphEdge(
                        source=graph_left.id,
                        target=graph_right.id,
                        edge_type="SESSION_ONLY",
                    )
                ],
            )
        )

        assert alice.get(function.id) is not None
        assert alice.get_fact(fact.id) is not None
        assert alice.get_preference(preference.id) is not None
        assert [item.id for item in alice.list_observations()] == [observation.id]
        assert alice.get_graph().edges
        assert alice.get_timeline(function.id)

        assert bob.get(function.id) is None
        assert bob.get_fact(fact.id) is None
        assert bob.get_preference(preference.id) is None
        assert bob.list_observations() == []
        assert bob.get_graph().edges == []
        assert bob.get_timeline(function.id) == []

        for table in (
            "memplex_functions",
            "memplex_edges",
            "memplex_observations",
            "memplex_facts",
            "memplex_preferences",
            "memplex_changelog",
        ):
            assert _admin_query(
                pg_dsn,
                f"SELECT DISTINCT visibility FROM {table} WHERE tenant_id = %s",
                (alice_context.principal.tenant_id,),
            ) == [("session",)], table

    def test_user_workspace_and_session_visibility_match_exact_contract(self, store):
        alice_context = _authorization(
            tenant="tenant-visibility-contract",
            subject="alice",
            agent="agent-a",
            session="session-a",
        )
        bob_context = _authorization(
            tenant="tenant-visibility-contract",
            subject="bob",
            agent="agent-a",
            session="session-b",
        )
        wrong_session = _authorization(
            tenant="tenant-visibility-contract",
            subject="alice",
            agent="agent-a",
            session="session-other",
        )
        wrong_agent = _authorization(
            tenant="tenant-visibility-contract",
            subject="alice",
            agent="agent-other",
            session="session-a",
        )
        alice = store.authorized(alice_context)

        session_node = _func("visibility-session", "Visibility Session")
        session_node.visibility = "session"
        user_node = _func("visibility-user", "Visibility User")
        user_node.visibility = "user"
        workspace_node = _func("visibility-workspace", "Visibility Workspace")
        workspace_node.visibility = "workspace"
        alice.add(session_node, SRC)
        alice.add(user_node, SRC)
        alice.add(workspace_node, SRC)

        assert store.authorized(alice_context).get(session_node.id) is not None
        assert store.authorized(wrong_session).get(session_node.id) is None
        assert store.authorized(wrong_agent).get(session_node.id) is None
        assert store.authorized(bob_context).get(session_node.id) is None
        assert store.authorized(bob_context).get(user_node.id) is None
        assert store.authorized(bob_context).get(workspace_node.id) is not None


# ── add / get / serialization round-trip ─────────────────────────────


class TestAddGetRoundtrip:
    def test_full_field_roundtrip(self, store):
        f = _func(
            "rt-1",
            "Login Flow",
            owner="alice",
            version=3,
            needs_review=True,
            needs_review_until="2030-01-02T03:04:05+00:00",
            priority_from_source="high",
            source_authority="official-docs",
            content_hash="abc123",
            attributes={"ns": "test", "n": 7},
            cross_references=[{"target": "other"}],
            condition=[_fv("cond", status="disputed", observation=0.5)],
            benefit=[_fv("benefit")],
        )
        store.add(f, SRC)
        got = store.get("rt-1")
        assert got is not None
        assert got.id == "rt-1"
        assert got.name == "Login Flow"
        assert got.name_normalized == "login flow"
        assert got.domain == "auth"
        assert got.confidence == pytest.approx(0.9)
        assert got.source_type == SourceType.CODE
        assert got.owner == "alice"
        assert got.version == 3
        assert got.needs_review is True
        assert got.needs_review_until == "2030-01-02T03:04:05+00:00"
        assert got.priority_from_source == "high"
        assert got.source_authority == "official-docs"
        assert got.content_hash == "abc123"
        assert got.attributes == {"ns": "test", "n": 7}
        assert got.cross_references == [{"target": "other"}]
        assert [fv.desc for fv in got.trigger] == ["Login Flow trigger"]
        assert got.condition[0].status == "disputed"
        assert got.condition[0].observation == pytest.approx(0.5)
        assert [fv.desc for fv in got.benefit] == ["benefit"]
        assert got.created_at and got.updated_at

    def test_fieldvalue_created_at_roundtrip(self, store):
        ts = datetime(2025, 5, 6, 7, 8, 9, tzinfo=timezone.utc)
        f = _func("rt-2", "Stamped", trigger=[_fv("t", created_at=ts)])
        store.add(f, SRC)
        got = store.get("rt-2")
        assert got.trigger[0].created_at == ts

    def test_get_missing_returns_none(self, store):
        assert store.get("nope") is None


# ── name_normalized merge / update semantics ─────────────────────────


class TestAddMerge:
    def test_add_merges_by_name_normalized(self, store):
        store.add(_func("m-1", "Login", trigger=[_fv("t1")]), SRC)
        # Different id, same normalized name -> merged into the first row.
        store.add(
            _func("m-2", "  login ", name_normalized="login", trigger=[_fv("t2")]),
            SRC,
        )
        funcs = store.list_functions()
        assert len(funcs) == 1
        merged = funcs[0]
        assert merged.id == "m-1"
        assert sorted(fv.desc for fv in merged.trigger) == ["t1", "t2"]
        assert merged.version == 2
        assert store.get("m-2") is None

    def test_add_dedups_duplicate_field_values(self, store):
        store.add(_func("m-3", "Logout", trigger=[_fv("same")]), SRC)
        store.add(
            _func("m-4", "logout", name_normalized="logout", trigger=[_fv("same")]),
            SRC,
        )
        merged = store.get("m-3")
        assert [fv.desc for fv in merged.trigger] == ["same"]

    def test_merge_appends_source_paragraphs(self, store):
        f1 = _func("m-5", "Signup", source_paragraphs=["p1"])
        store.add(f1, SRC)
        f2 = _func("m-6", "signup", name_normalized="signup", source_paragraphs=["p1", "p2"])
        store.add(f2, SRC)
        merged = store.get("m-5")
        assert merged.source_paragraphs == ["p1", "p2"]


@pytest.mark.parametrize("visibility", ("workspace", "user", "session"))
def test_same_scope_normalized_name_converges_under_concurrency(store, pg_dsn, visibility):
    """The partial unique index, not broad ACL visibility, chooses a merge.

    Twelve writers race with distinct ids but one normalized name.  The
    operation must converge to exactly one row in the selected index domain.
    ``Barrier`` makes this deterministic enough to exercise the savepoint
    path without timing sleeps.
    """
    context = _authorization(
        tenant=f"task4-concurrent-{visibility}",
        subject="alice",
        workspace="task4-workspace",
        agent="task4-agent",
        session="task4-session",
    )
    barrier = Barrier(12)
    # A production pool is intentionally capped at eight leases.  Two
    # independently ready application pools represent two service instances,
    # allowing this test to exercise twelve real concurrent writers without
    # weakening the resource pool bound just for tests.
    sibling_resources = _ready_resources(pg_dsn)
    sibling_store = PostgresMemoryStore(
        dsn=pg_dsn, ready_pool=sibling_resources.ready_pool
    )

    def add_one(item: tuple[int, PostgresMemoryStore]) -> None:
        index, writer = item
        function = _func(f"task4-{visibility}-{index}", "Deploy")
        function.visibility = visibility
        barrier.wait(timeout=15)
        writer.authorized(context).add(function, SRC)

    try:
        with ThreadPoolExecutor(max_workers=12) as executor:
            list(executor.map(add_one, ((index, store if index < 6 else sibling_store) for index in range(12))))
    finally:
        sibling_resources.close()

    assert _admin_query(
        pg_dsn,
        "SELECT count(*) FROM memplex_functions "
        "WHERE tenant_id=%s AND visibility=%s "
        "AND lower(btrim(coalesce(data->>'name_normalized', data->>'name', '')))='deploy'",
        (context.principal.tenant_id, visibility),
    ) == [(1,)]


def test_normalized_name_does_not_merge_across_visibility_scopes(store, pg_dsn):
    """One reader may see all scopes; that does not make them one key."""
    context = _authorization(
        tenant="task4-cross-scope",
        subject="alice",
        workspace="task4-workspace",
        agent="task4-agent",
        session="task4-session",
    )
    scoped = store.authorized(context)
    for visibility in ("workspace", "user", "session"):
        function = _func(f"task4-cross-{visibility}", "Deploy")
        function.visibility = visibility
        scoped.add(function, SRC)

    assert _admin_query(
        pg_dsn,
        "SELECT visibility, count(*) FROM memplex_functions "
        "WHERE tenant_id=%s AND lower(btrim(coalesce(data->>'name_normalized', data->>'name', '')))='deploy' "
        "GROUP BY visibility ORDER BY visibility",
        (context.principal.tenant_id,),
    ) == [("session", 1), ("user", 1), ("workspace", 1)]


@pytest.mark.parametrize(
    "operation",
    ("add", "delete", "merge", "add_fact", "add_preference", "delete_fact", "delete_preference"),
)
def test_task4_memory_public_write_rolls_back_when_audit_fails(store, monkeypatch, operation):
    """No public write can leave data behind when its last SQL boundary fails."""
    if operation == "delete":
        store.add(_func("task4-delete", "Task4 Delete"), SRC)
    elif operation == "delete_fact":
        store.add_fact(Fact(id="task4-delete-fact", subject="s", predicate="p", object_="o"))
    elif operation == "delete_preference":
        store.add_preference(
            Preference(id="task4-delete-preference", aspect="a", preference="p")
        )

    def fail_audit(*_args, **_kwargs):
        raise RuntimeError("task4 audit fault")

    monkeypatch.setattr(store, "_record_changelog", fail_audit)
    with pytest.raises(RuntimeError, match="task4 audit fault"):
        if operation == "add":
            store.add(_func("task4-add", "Task4 Add"), SRC)
        elif operation == "delete":
            store.delete("task4-delete")
        elif operation == "merge":
            store.merge(GraphData(nodes=[_func("task4-merge", "Task4 Merge")], edges=[]))
        elif operation == "add_fact":
            store.add_fact(Fact(id="task4-fact", subject="s", predicate="p", object_="o"))
        elif operation == "add_preference":
            store.add_preference(
                Preference(id="task4-preference", aspect="a", preference="p")
            )
        elif operation == "delete_fact":
            store.delete_fact("task4-delete-fact")
        else:
            store.delete_preference("task4-delete-preference")

    if operation == "delete":
        assert store.get("task4-delete") is not None
    elif operation == "delete_fact":
        assert store.get_fact("task4-delete-fact") is not None
    elif operation == "delete_preference":
        assert store.get_preference("task4-delete-preference") is not None
    elif operation == "add":
        assert store.get("task4-add") is None
    elif operation == "merge":
        assert store.get("task4-merge") is None
    elif operation == "add_fact":
        assert store.get_fact("task4-fact") is None
    else:
        assert store.get_preference("task4-preference") is None


def test_task4_clear_rolls_back_prior_tables_when_later_delete_fails(store, pg_dsn):
    """A real PostgreSQL trigger faults late in ``clear`` without sleeps."""
    store.add(_func("task4-clear-function", "Task4 Clear"), SRC)
    store.add_fact(Fact(id="task4-clear-fact", subject="s", predicate="p", object_="o"))
    _admin_execute(
        pg_dsn,
        """
        CREATE FUNCTION task4_clear_fault() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'task4 clear fault'; END;
        $$;
        CREATE TRIGGER task4_clear_fault_trigger BEFORE DELETE ON memplex_facts
        FOR EACH ROW EXECUTE FUNCTION task4_clear_fault();
        """,
    )
    try:
        with pytest.raises(Exception, match="task4 clear fault"):
            store.clear()
    finally:
        _admin_execute(pg_dsn, "DROP TRIGGER IF EXISTS task4_clear_fault_trigger ON memplex_facts")
        _admin_execute(pg_dsn, "DROP FUNCTION IF EXISTS task4_clear_fault()")

    assert store.get("task4-clear-function") is not None
    assert store.get_fact("task4-clear-fact") is not None


def test_task4_fix1_add_preserves_canonical_identity_on_same_normalized_write(store, pg_dsn):
    """A later workspace writer may merge fields but cannot take ownership."""
    alice_context = _authorization(
        tenant="task4-fix1-owner", subject="alice", workspace="task4-workspace"
    )
    bob_context = _authorization(
        tenant="task4-fix1-owner", subject="bob", workspace="task4-workspace"
    )
    original = _func(
        "task4-fix1-alice", "Deploy", trigger=[_fv("alice trigger")],
        namespace={"owner": "alice"}, provenance={"source": "alice"},
    )
    alice = store.authorized(alice_context)
    alice.add(original, SRC)
    before_identity = _admin_query(
        pg_dsn,
        "SELECT id, owner_subject, visibility, data->>'owner', data->'namespace', data->'provenance' "
        "FROM memplex_functions WHERE tenant_id=%s",
        ("task4-fix1-owner",),
    )
    incoming = _func(
        "task4-fix1-bob", "Deploy", trigger=[_fv("bob trigger")],
        namespace={"owner": "bob"}, provenance={"source": "bob"},
    )
    store.authorized(bob_context).add(incoming, SRC)

    rows = _admin_query(
        pg_dsn,
        "SELECT id, owner_subject, visibility, data->>'owner', data->'namespace', data->'provenance' "
        "FROM memplex_functions WHERE tenant_id=%s",
        ("task4-fix1-owner",),
    )
    assert rows == before_identity
    got = store.authorized(bob_context).get("task4-fix1-alice")
    assert {field.desc for field in got.trigger} == {"alice trigger", "bob trigger"}


def test_task4_fix1_merge_converges_nodes_and_remaps_edges(store):
    context = _authorization(tenant="task4-fix1-merge", subject="alice")
    scoped = store.authorized(context)
    canonical = _func("task4-fix1-canonical", "Deploy", trigger=[_fv("old")])
    scoped.add(canonical, SRC)
    incoming = _func("task4-fix1-incoming", "Deploy", trigger=[_fv("new")])
    target = _func("task4-fix1-target", "Target")
    result = scoped.merge(
        GraphData(
            nodes=[incoming, target],
            edges=[GraphEdge(incoming.id, target.id, "REFERENCES")],
        )
    )
    assert result.new_functions == 1
    assert result.updated_functions == 1
    assert scoped.get(incoming.id) is None
    got = scoped.get(canonical.id)
    assert {field.desc for field in got.trigger} == {"old", "new"}
    assert [(edge.source, edge.target) for edge in scoped.get_graph().edges] == [
        (canonical.id, target.id)
    ]


def test_task4_fix1_merge_same_id_preserves_private_canonical_row(store, pg_dsn):
    context = _authorization(tenant="task4-fix1-same-id", subject="alice")
    scoped = store.authorized(context)
    original = _func(
        "task4-fix1-private", "Private Deploy", trigger=[_fv("old")],
        namespace={"canonical": "private"}, provenance={"source": "old"},
    )
    original.visibility = "user"
    scoped.add(original, SRC)
    before_identity = _admin_query(
        pg_dsn,
        "SELECT visibility, owner_subject, data->>'name', data->'namespace' "
        "FROM memplex_functions WHERE tenant_id=%s AND id=%s",
        ("task4-fix1-same-id", original.id),
    )
    replacement = _func(
        original.id, "Workspace Deploy", trigger=[_fv("new")],
        namespace={"incoming": "workspace"}, provenance={"source": "new"},
    )
    replacement.visibility = "workspace"
    scoped.merge(GraphData(nodes=[replacement], edges=[]))

    assert _admin_query(
        pg_dsn,
        "SELECT visibility, owner_subject, data->>'name', data->'namespace' "
        "FROM memplex_functions WHERE tenant_id=%s AND id=%s",
        ("task4-fix1-same-id", original.id),
    ) == before_identity
    got = scoped.get(original.id)
    assert {field.desc for field in got.trigger} == {"old", "new"}


@pytest.mark.parametrize("visibility", ("workspace", "user", "session"))
def test_task4_fix1_merge_normalized_conflict_converges_in_each_scope(store, visibility):
    context = _authorization(
        tenant=f"task4-fix1-normalized-{visibility}",
        subject="alice",
        agent="task4-agent",
        session="task4-session",
    )
    scoped = store.authorized(context)
    canonical = _func("task4-fix1-canonical", "Deploy", trigger=[_fv("old")])
    canonical.visibility = visibility
    scoped.add(canonical, SRC)
    incoming = _func("task4-fix1-incoming", "Deploy", trigger=[_fv("new")])
    incoming.visibility = visibility
    result = scoped.merge(GraphData(nodes=[incoming], edges=[]))
    assert (result.new_functions, result.updated_functions) == (0, 1)
    assert scoped.get(incoming.id) is None
    got = scoped.get(canonical.id)
    assert {field.desc for field in got.trigger} == {"old", "new"}


def test_task4_fix1_merge_fault_after_node_mapping_rolls_back_nodes_and_edges(
    store, monkeypatch
):
    context = _authorization(tenant="task4-fix1-fault", subject="alice")
    scoped = store.authorized(context)
    left = _func("task4-fix1-fault-left", "Left")
    right = _func("task4-fix1-fault-right", "Right")

    def fail_changelog(*_args, **_kwargs):
        raise RuntimeError("task4 fix1 merge audit fault")

    monkeypatch.setattr(store, "_record_changelog", fail_changelog)
    with pytest.raises(RuntimeError, match="task4 fix1 merge audit fault"):
        scoped.merge(
            GraphData(
                nodes=[left, right],
                edges=[GraphEdge(left.id, right.id, "REFERENCES")],
            )
        )
    assert scoped.get(left.id) is None
    assert scoped.get(right.id) is None
    assert scoped.get_graph().edges == []


def test_task4_fix2_invisible_same_id_cannot_create_phantom_success(store):
    """A tenant-wide primary-key conflict outside ACL must fail closed."""
    alice = store.authorized(
        _authorization(tenant="task4-fix2-hidden", subject="alice", session="alice")
    )
    bob = store.authorized(
        _authorization(tenant="task4-fix2-hidden", subject="bob", session="bob")
    )
    victim = _func("task4-fix2-hidden-id", "Private Deploy", trigger=[_fv("alice")])
    victim.visibility = "user"
    alice.add(victim, SRC)
    collision = _func("task4-fix2-hidden-id", "Bob Deploy", trigger=[_fv("bob")])
    collision.visibility = "user"

    with pytest.raises(RuntimeError, match="authorized row"):
        bob.add(collision, SRC)
    batch = bob.add_batch([collision], [SRC])
    assert (batch.succeeded, len(batch.failed_items)) == (0, 1)
    with pytest.raises(RuntimeError, match="authorized row"):
        bob.merge(GraphData(nodes=[collision], edges=[]))

    assert alice.get(victim.id).trigger[0].desc == "alice"
    assert bob.get(victim.id) is None
    assert bob.get_timeline(victim.id) == []


def test_task4_fix2_typed_observation_edge_and_delete_are_acl_verified(store):
    alice = store.authorized(
        _authorization(tenant="task4-fix2-all", subject="alice", session="alice")
    )
    bob = store.authorized(
        _authorization(tenant="task4-fix2-all", subject="bob", session="bob")
    )
    fact = Fact(id="task4-fix2-fact", subject="alice", predicate="is", object_="private")
    fact.visibility = "user"
    preference = Preference(id="task4-fix2-preference", aspect="theme", preference="dark")
    preference.visibility = "user"
    observation = Observation(id="task4-fix2-observation", name="private", event="note")
    observation.visibility = "user"
    alice.add_fact(fact)
    alice.add_preference(preference)
    alice.add_observation(observation)

    for operation in (
        lambda: bob.add_fact(
            Fact(id=fact.id, subject="bob", predicate="is", object_="collision", visibility="user")
        ),
        lambda: bob.add_preference(
            Preference(id=preference.id, aspect="theme", preference="collision", visibility="user")
        ),
        lambda: bob.add_observation(
            Observation(id=observation.id, name="collision", event="note", visibility="user")
        ),
    ):
        with pytest.raises(RuntimeError, match="authorized row"):
            operation()

    # A user-scoped edge can collide on an otherwise visible workspace pair.
    left = _func("task4-fix2-left", "Visible Left")
    right = _func("task4-fix2-right", "Visible Right")
    alice.add(left, SRC)
    alice.add(right, SRC)
    edge = GraphEdge(left.id, right.id, "REFERENCES")
    alice.merge(GraphData(nodes=[], edges=[edge]))
    with pytest.raises(RuntimeError, match="authorized row"):
        bob.merge(GraphData(nodes=[], edges=[edge]))

    # Missing/invisible deletes are no-op and must not manufacture an audit
    # event or remove a caller-owned associated row with the same id.
    bob.delete(fact.id)
    bob.delete_fact(fact.id)
    bob.delete_preference(preference.id)
    assert alice.get_fact(fact.id).object_ == "private"
    assert alice.get_preference(preference.id).preference == "dark"
    assert bob.get_timeline(fact.id) == []


def test_task7_function_delete_preserves_same_id_typed_nodes(store):
    scoped = store.authorized(
        _authorization(tenant="task7-delete-kind", subject="alice", session="alice")
    )
    shared_id = f"task7-shared-{uuid.uuid4().hex}"
    function = _func(shared_id, "Shared Function")
    fact = Fact(id=shared_id, subject="alice", predicate="keeps", object_="fact")
    preference = Preference(id=shared_id, aspect="editor", preference="vim")
    observation = Observation(id=shared_id, name="note", event="deploy")
    scoped.add(function, SRC)
    scoped.add_fact(fact)
    scoped.add_preference(preference)
    scoped.add_observation(observation)

    scoped.delete(shared_id)

    assert scoped.get(shared_id) is None
    assert scoped.get_fact(shared_id) is not None
    assert scoped.get_preference(shared_id) is not None
    assert scoped.get_observation(shared_id) is not None


def test_task4_fix2_merge_rejects_absent_or_invisible_edge_endpoints(store):
    alice = store.authorized(_authorization(tenant="task4-fix2-edge", subject="alice"))
    bob = store.authorized(_authorization(tenant="task4-fix2-edge", subject="bob"))
    private = _func("task4-fix2-private-endpoint", "Private Endpoint")
    private.visibility = "user"
    alice.add(private, SRC)
    visible = _func("task4-fix2-visible-endpoint", "Visible Endpoint")
    alice.add(visible, SRC)
    with pytest.raises(RuntimeError, match="authorized row"):
        bob.merge(
            GraphData(
                nodes=[],
                edges=[GraphEdge(private.id, visible.id, "REFERENCES")],
            )
        )
    with pytest.raises(RuntimeError, match="authorized row"):
        alice.merge(
            GraphData(
                nodes=[],
                edges=[GraphEdge("task4-fix2-absent", visible.id, "REFERENCES")],
            )
        )
    assert alice.get_graph().edges == []


def test_task4_fix3b2_reverse_graph_merges_lock_endpoints_in_global_order(
    store, pg_dsn, monkeypatch
):
    """A→B and B→A graph merges cannot deadlock on reverse endpoint locks.

    The barrier sits immediately before each transaction's tenant advisory
    lock. Both transactions therefore contend at the same point; one runs
    and commits before the other touches endpoint rows. There is no timing
    sleep or retry in this proof.
    """
    context = _authorization(
        tenant="task4-fix3b2-reverse", subject="alice", workspace="task4-workspace"
    )
    scoped = store.authorized(context)
    left = _func("task4-fix3b2-a", "Alpha")
    right = _func("task4-fix3b2-b", "Beta")
    scoped.add(left, SRC)
    scoped.add(right, SRC)

    first_lock = Barrier(2, timeout=15)
    per_thread = threading.local()
    original_lock = store._acquire_function_write_lock

    def synchronized_first_lock(cur, auth_context):
        if not getattr(per_thread, "first_lock_seen", False):
            per_thread.first_lock_seen = True
            first_lock.wait()
        return original_lock(cur, auth_context)

    monkeypatch.setattr(store, "_acquire_function_write_lock", synchronized_first_lock)

    def merge_one(edge: GraphEdge) -> None:
        node = left if edge.source == left.id else right
        store.authorized(context).merge(GraphData(nodes=[node], edges=[edge]))

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(merge_one, GraphEdge(left.id, right.id, "REFERENCES")),
            executor.submit(merge_one, GraphEdge(right.id, left.id, "REFERENCES")),
        ]
        for future in futures:
            future.result(timeout=30)

    assert {
        (edge.source, edge.target, edge.edge_type) for edge in scoped.get_graph().edges
    } >= {
        (left.id, right.id, "REFERENCES"),
        (right.id, left.id, "REFERENCES"),
    }
    assert _admin_query(
        pg_dsn,
        """
        SELECT count(*)
        FROM memplex_edges edge
        LEFT JOIN memplex_functions source
          ON source.tenant_id = edge.tenant_id AND source.id = edge.source
        LEFT JOIN memplex_functions target
          ON target.tenant_id = edge.tenant_id AND target.id = edge.target
        WHERE edge.tenant_id = %s
          AND (source.id IS NULL OR target.id IS NULL)
        """,
        (context.principal.tenant_id,),
    ) == [(0,)]


def test_task4_fix3b5_normalized_alias_belongs_to_remaps_before_validation(store, pg_dsn):
    """BELONGS_TO validates the canonical endpoint, never the merged alias."""
    context = _authorization(tenant="task4-fix3b5-alias", subject="alice")
    scoped = store.authorized(context)
    canonical = _func("task4-fix3b5-canonical", "Deploy", domain="  A  B ")
    scoped.add(canonical, SRC)
    alias = _func("task4-fix3b5-alias", "Deploy", domain="  A  B ")
    target = domain_node_id(alias.domain)

    result = scoped.merge(
        GraphData(
            nodes=[alias],
            edges=[GraphEdge(alias.id, target, "BELONGS_TO")],
        )
    )
    assert result.updated_functions == 1
    assert scoped.get(alias.id) is None
    assert scoped.get(canonical.id) is not None
    assert {
        (edge.source, edge.target, edge.edge_type) for edge in scoped.get_graph().edges
    } >= {(canonical.id, target, "BELONGS_TO")}
    assert _admin_query(
        pg_dsn,
        """
        SELECT count(*)
        FROM memplex_edges edge
        LEFT JOIN memplex_functions source
          ON source.tenant_id = edge.tenant_id AND source.id = edge.source
        WHERE edge.tenant_id = %s AND source.id IS NULL
        """,
        (context.principal.tenant_id,),
    ) == [(0,)]

    with pytest.raises(RuntimeError, match="authorized row"):
        scoped.merge(
            GraphData(
                nodes=[_func("task4-fix3b5-alias-mismatch", "Deploy", domain="  A  B ")],
                edges=[GraphEdge("task4-fix3b5-alias-mismatch", "domain_bad", "BELONGS_TO")],
            )
        )

    other = _func("task4-fix3b5-other", "Other")
    alias_for_edge = _func("task4-fix3b5-alias-edge", "Deploy", domain="  A  B ")
    scoped.merge(
        GraphData(
            nodes=[alias_for_edge, other],
            edges=[GraphEdge(alias_for_edge.id, other.id, "REFERENCES")],
        )
    )
    assert (canonical.id, other.id, "REFERENCES") in {
        (edge.source, edge.target, edge.edge_type) for edge in scoped.get_graph().edges
    }


def test_task4_fix3b2_reserved_domain_id_never_reaches_real_pg(store, pg_dsn):
    """Post-construction ID mutation cannot create a Function or edge row."""
    context = _authorization(tenant="task4-fix3b2-domain", subject="alice")
    scoped = store.authorized(context)
    mutated_add = _func("task4-fix3b2-valid-add", "Valid Add")
    mutated_add.id = "domain_auth"
    mutated_merge = _func("task4-fix3b2-valid-merge", "Valid Merge")
    mutated_merge.id = "domain_deploy"

    with pytest.raises(ValueError, match="保留"):
        scoped.add(mutated_add, SRC)
    with pytest.raises(ValueError, match="保留"):
        scoped.merge(
            GraphData(
                nodes=[mutated_merge],
                edges=[GraphEdge(mutated_merge.id, "missing", "REFERENCES")],
            )
        )

    assert _admin_query(
        pg_dsn,
        "SELECT count(*) FROM memplex_functions WHERE tenant_id=%s",
        (context.principal.tenant_id,),
    ) == [(0,)]
    assert _admin_query(
        pg_dsn,
        "SELECT count(*) FROM memplex_edges WHERE tenant_id=%s",
        (context.principal.tenant_id,),
    ) == [(0,)]


def test_task4_fix3b3_mutated_non_string_domain_never_reaches_real_pg(store, pg_dsn):
    context = _authorization(tenant="task4-fix3b3-domain", subject="alice")
    scoped = store.authorized(context)
    mutated_add = _func("task4-fix3b3-domain-add", "Domain", domain="auth")
    mutated_add.domain = {"forged": "domain"}
    mutated_merge = _func("task4-fix3b3-domain-merge", "Domain", domain="auth")
    mutated_merge.domain = 0

    with pytest.raises(ValueError, match="domain"):
        scoped.add(mutated_add, SRC)
    with pytest.raises(ValueError, match="domain"):
        scoped.merge(
            GraphData(
                nodes=[mutated_merge],
                edges=[GraphEdge(mutated_merge.id, "missing", "REFERENCES")],
            )
        )

    for table in ("memplex_functions", "memplex_edges"):
        assert _admin_query(
            pg_dsn,
            f"SELECT count(*) FROM {table} WHERE tenant_id=%s",
            (context.principal.tenant_id,),
        ) == [(0,)]


def test_task4_fix3b3_advisory_lock_timeout_rolls_back_and_releases(
    store, pg_dsn, monkeypatch
):
    """A real 55P03 becomes retryable and cannot leave a partial Function."""
    import memplex.storage.postgres as postgres_module

    context = _authorization(tenant="task4-fix3b3-busy", subject="alice")
    scoped = store.authorized(context)
    monkeypatch.setattr(postgres_module, "_FUNCTION_WRITE_LOCK_TIMEOUT", "100ms")
    key = _function_write_lock_key(store._ready_pool.target, context.principal.tenant_id)
    blocker = psycopg2.connect(pg_dsn)
    try:
        cur = blocker.cursor()
        cur.execute("SELECT pg_advisory_xact_lock(%s)", (key,))
        with pytest.raises(FunctionWriteBusy, match="retry"):
            scoped.add(_func("task4-fix3b3-busy", "Busy"), SRC)
        assert scoped.get("task4-fix3b3-busy") is None
        blocker.rollback()
        scoped.add(_func("task4-fix3b3-after-busy", "After Busy"), SRC)
        assert scoped.get("task4-fix3b3-after-busy") is not None
    finally:
        blocker.close()


def test_task4_fix3b3_tenant_lock_rejects_all_function_writers_without_partial_state(
    store, pg_dsn, monkeypatch
):
    """add/delete/clear/access-batch share the same tenant write barrier."""
    import memplex.storage.postgres as postgres_module

    context = _authorization(tenant="task4-fix3b3-writers", subject="alice")
    scoped = store.authorized(context)
    delete_target = _func("task4-fix3b3-delete", "Delete Target")
    access_target = _func("task4-fix3b3-access", "Access Target")
    clear_target = _func("task4-fix3b3-clear", "Clear Target")
    for node in (delete_target, access_target, clear_target):
        scoped.add(node, SRC)
    monkeypatch.setattr(postgres_module, "_FUNCTION_WRITE_LOCK_TIMEOUT", "100ms")
    key = _function_write_lock_key(store._ready_pool.target, context.principal.tenant_id)

    def while_blocked(operation):
        blocker = psycopg2.connect(pg_dsn)
        try:
            cur = blocker.cursor()
            cur.execute("SELECT pg_advisory_xact_lock(%s)", (key,))
            with pytest.raises(FunctionWriteBusy, match="retry"):
                operation()
        finally:
            blocker.rollback()
            blocker.close()

    while_blocked(lambda: scoped.add(_func("task4-fix3b3-add", "Blocked Add"), SRC))
    while_blocked(lambda: scoped.delete(delete_target.id))
    while_blocked(scoped.clear)
    before_access = scoped.get(access_target.id).access_count
    while_blocked(lambda: scoped.increment_access_batch([access_target.id]))

    assert scoped.get("task4-fix3b3-add") is None
    assert scoped.get(delete_target.id) is not None
    assert scoped.get(clear_target.id) is not None
    assert scoped.get(access_target.id).access_count == before_access


def test_task4_fix3b3_cross_tenant_function_locks_do_not_serialize(store, monkeypatch):
    """Different tenant keys may acquire their write locks concurrently."""
    alice_context = _authorization(tenant="task4-fix3b3-cross-a", subject="alice")
    bob_context = _authorization(tenant="task4-fix3b3-cross-b", subject="bob")
    first_acquired = threading.Event()
    second_acquired = threading.Event()
    release_first = threading.Event()
    guard = threading.Lock()
    acquired_count = 0
    original_lock = store._acquire_function_write_lock

    def observe_lock(cur, context):
        nonlocal acquired_count
        original_lock(cur, context)
        with guard:
            acquired_count += 1
            position = acquired_count
        if position == 1:
            first_acquired.set()
            assert release_first.wait(timeout=5)
        else:
            second_acquired.set()

    monkeypatch.setattr(store, "_acquire_function_write_lock", observe_lock)
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(
            store.authorized(alice_context).add,
            _func("task4-fix3b3-cross-a", "Cross A"),
            SRC,
        )
        assert first_acquired.wait(timeout=5)
        second = executor.submit(
            store.authorized(bob_context).add,
            _func("task4-fix3b3-cross-b", "Cross B"),
            SRC,
        )
        assert second_acquired.wait(timeout=5)
        release_first.set()
        first.result(timeout=15)
        second.result(timeout=15)

    assert store.authorized(alice_context).get("task4-fix3b3-cross-a") is not None
    assert store.authorized(bob_context).get("task4-fix3b3-cross-b") is not None


def test_task4_fix3b3_merge_holds_tenant_lock_before_increment_access(store, monkeypatch):
    """A merge in progress blocks a same-tenant access write without drift."""
    context = _authorization(tenant="task4-fix3b3-merge-hold", subject="alice")
    scoped = store.authorized(context)
    access = _func("task4-fix3b3-merge-access", "Access")
    scoped.add(access, SRC)
    entered_audit = threading.Event()
    release_merge = threading.Event()
    original_audit = store._record_changelog

    def block_merge_audit(cur, func_id, event_type, description, source, **kwargs):
        if func_id == "task4-fix3b3-merge-holder":
            entered_audit.set()
            assert release_merge.wait(timeout=5)
        return original_audit(cur, func_id, event_type, description, source, **kwargs)

    monkeypatch.setattr(store, "_record_changelog", block_merge_audit)
    holder = _func("task4-fix3b3-merge-holder", "Holder")
    with ThreadPoolExecutor(max_workers=2) as executor:
        merge_future = executor.submit(
            scoped.merge, GraphData(nodes=[holder], edges=[])
        )
        assert entered_audit.wait(timeout=5)
        access_future = executor.submit(scoped.increment_access, access.id)
        assert scoped.get(access.id).access_count == 0
        assert not access_future.done()
        release_merge.set()
        merge_future.result(timeout=15)
        access_future.result(timeout=15)
    assert scoped.get(access.id).access_count == 1


def test_task4_fix3b3_late_sql_failure_releases_tenant_lock(store, pg_dsn):
    """A post-write SQL error rolls back and the next tenant write can lock."""
    context = _authorization(tenant="task4-fix3b3-late-fault", subject="alice")
    scoped = store.authorized(context)
    _admin_execute(
        pg_dsn,
        """
        CREATE FUNCTION task4_fix3b3_late_fault() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'task4 fix3b3 late fault'; END;
        $$;
        CREATE TRIGGER task4_fix3b3_late_fault_trigger
        BEFORE INSERT ON memplex_changelog
        FOR EACH ROW EXECUTE FUNCTION task4_fix3b3_late_fault();
        """,
    )
    try:
        with pytest.raises(Exception, match="task4 fix3b3 late fault"):
            scoped.add(_func("task4-fix3b3-late-failed", "Late Failed"), SRC)
    finally:
        _admin_execute(
            pg_dsn,
            "DROP TRIGGER IF EXISTS task4_fix3b3_late_fault_trigger ON memplex_changelog; "
            "DROP FUNCTION IF EXISTS task4_fix3b3_late_fault()",
        )
    assert scoped.get("task4-fix3b3-late-failed") is None
    scoped.add(_func("task4-fix3b3-late-after", "Late After"), SRC)
    assert scoped.get("task4-fix3b3-late-after") is not None


# ── add_batch ────────────────────────────────────────────────────────


class TestAddBatch:
    def test_batch_all_succeed(self, store):
        funcs = [_func("b-1", "Alpha"), _func("b-2", "Beta")]
        result = store.add_batch(funcs, [SRC, SRC])
        assert result.total == 2
        assert result.succeeded == 2
        assert result.failed_items == []
        assert len(store.list_functions()) == 2

    def test_batch_failure_isolation(self, store):
        good = _func("b-3", "Good")
        bad = _func("b-4", "Bad", attributes={"unserializable": object()})
        result = store.add_batch([good, bad], [SRC, SRC])
        assert result.total == 2
        assert result.succeeded == 1
        assert len(result.failed_items) == 1
        assert result.failed_items[0]["func_id"] == "b-4"
        # The good item survived the bad one's failure.
        assert store.get("b-3") is not None
        # The failed item left no partial row behind.
        assert store.get("b-4") is None


# ── delete / list / clear ────────────────────────────────────────────


class TestDeleteListClear:
    def test_delete_removes_function_and_edges(self, store):
        store.add(_func("d-1", "One"), SRC)
        store.add(_func("d-2", "Two"), SRC)
        store.merge(
            GraphData(nodes=[], edges=[GraphEdge("d-1", "d-2", "REFERENCES")])
        )
        store.delete("d-1")
        assert store.get("d-1") is None
        assert store.get("d-2") is not None
        graph = store.get_graph()
        assert graph.edges == []
        # The deletion is recorded in the changelog.
        events = store.get_timeline("d-1")
        assert any(e.event_type == "deleted" for e in events)

    def test_list_functions_pagination_and_owner(self, store):
        for i in range(5):
            owner = "alice" if i % 2 == 0 else "bob"
            store.add(_func(f"l-{i}", f"Func{i}", owner=owner), SRC)
        assert len(store.list_functions()) == 5
        page = store.list_functions(offset=0, limit=2)
        assert len(page) == 2
        rest = store.list_functions(offset=2, limit=100)
        assert len(rest) == 3
        assert {f.id for f in page}.isdisjoint({f.id for f in rest})
        alice = store.list_functions(owner="alice")
        assert {f.id for f in alice} == {"l-0", "l-2", "l-4"}

    def test_clear_empties_everything(self, store):
        store.add(_func("c-1", "Wipe"), SRC)
        store.add_fact(Fact(id="c-f1", name="fact", subject="s"))
        store.add_preference(Preference(id="c-p1", name="pref", aspect="a"))
        store.add_observation(Observation(id="c-o1", name="obs"))
        store.clear()
        assert store.list_functions() == []
        assert store.list_facts() == []
        assert store.list_preferences() == []
        assert store.list_observations() == []
        assert store.get_timeline("c-1") == []

    def test_increment_access(self, store):
        store.add(_func("a-1", "Access"), SRC)
        store.increment_access("a-1")
        store.increment_access_batch(["a-1", "a-1"])
        got = store.get("a-1")
        assert got.access_count == 3
        assert got.last_accessed_at

    def test_list_changes_since(self, store):
        store.add(_func("s-1", "Old", updated_at="2020-01-01T00:00:00+00:00"), SRC)
        store.add(_func("s-2", "New", updated_at="2025-06-01T00:00:00+00:00"), SRC)
        changed = store.list_changes_since(since="2021-01-01T00:00:00+00:00")
        assert [f.id for f in changed] == ["s-2"]


# ── Structured filter (all 7 SearchFilters fields) ───────────────────


class TestFilter:
    @pytest.fixture
    def filtered_store(self, store):
        store.add(
            _func(
                "f-1",
                "AuthLogin",
                domain="auth",
                confidence=0.9,
                owner="alice",
                needs_review=True,
                updated_at="2025-01-10T00:00:00+00:00",
            ),
            SRC,
        )
        store.add(
            _func(
                "f-2",
                "WikiSync",
                domain="wiki",
                confidence=0.4,
                owner="bob",
                source_type=SourceType.WIKI,
                updated_at="2025-03-01T00:00:00+00:00",
            ),
            SRC,
        )
        return store

    def test_filter_domain(self, filtered_store):
        got = filtered_store.filter(SearchFilters(domain=["auth"]))
        assert [f.id for f in got] == ["f-1"]

    def test_filter_source_type(self, filtered_store):
        got = filtered_store.filter(SearchFilters(source_type=[SourceType.WIKI]))
        assert [f.id for f in got] == ["f-2"]

    def test_filter_confidence_min(self, filtered_store):
        got = filtered_store.filter(SearchFilters(confidence_min=0.5))
        assert [f.id for f in got] == ["f-1"]

    def test_filter_updated_after(self, filtered_store):
        got = filtered_store.filter(
            SearchFilters(updated_after=datetime(2025, 2, 1, tzinfo=timezone.utc))
        )
        assert [f.id for f in got] == ["f-2"]

    def test_filter_updated_before(self, filtered_store):
        got = filtered_store.filter(
            SearchFilters(updated_before=datetime(2025, 2, 1, tzinfo=timezone.utc))
        )
        assert [f.id for f in got] == ["f-1"]

    def test_filter_needs_review(self, filtered_store):
        got = filtered_store.filter(SearchFilters(needs_review=True))
        assert [f.id for f in got] == ["f-1"]
        got_false = filtered_store.filter(SearchFilters(needs_review=False))
        assert [f.id for f in got_false] == ["f-2"]

    def test_filter_owner(self, filtered_store):
        got = filtered_store.filter(SearchFilters(owner="bob"))
        assert [f.id for f in got] == ["f-2"]

    def test_filter_combined(self, filtered_store):
        got = filtered_store.filter(
            SearchFilters(
                domain=["auth", "wiki"],
                confidence_min=0.3,
                owner="alice",
                needs_review=True,
            )
        )
        assert [f.id for f in got] == ["f-1"]

    def test_filter_empty_returns_all(self, filtered_store):
        assert len(filtered_store.filter(SearchFilters())) == 2


# ── Graph: merge / get_neighbors / get_graph ─────────────────────────


class TestGraph:
    def _chain(self, store):
        """A -> B -> C -> D chain plus a C -> A cycle edge."""
        for fid, name in [("g-a", "A"), ("g-b", "B"), ("g-c", "C"), ("g-d", "D")]:
            store.add(_func(fid, name), SRC)
        store.merge(
            GraphData(
                nodes=[],
                edges=[
                    GraphEdge("g-a", "g-b", "REFERENCES"),
                    GraphEdge("g-b", "g-c", "DEPENDS_ON"),
                    GraphEdge("g-c", "g-d", "REFERENCES"),
                    GraphEdge("g-c", "g-a", "REFERENCES"),  # cycle
                ],
            )
        )

    def test_get_neighbors_one_hop_bidirectional(self, store):
        self._chain(store)
        # Outgoing and incoming edges are both traversed (lite semantics).
        from_a = {f.id for f in store.get_neighbors("g-a", max_hops=1)}
        assert from_a == {"g-b", "g-c"}
        from_d = {f.id for f in store.get_neighbors("g-d", max_hops=1)}
        assert from_d == {"g-c"}

    def test_get_neighbors_max_hops(self, store):
        self._chain(store)
        # max_hops=2 from D: C is 1 hop; B and A are 2 hops (C->B reverse,
        # C->A reverse).
        two = {f.id for f in store.get_neighbors("g-d", max_hops=2)}
        assert two == {"g-c", "g-b", "g-a"}
        three = {f.id for f in store.get_neighbors("g-d", max_hops=3)}
        assert three == {"g-c", "g-b", "g-a"}  # cycle guard: no D re-visit

    def test_get_neighbors_edge_type_filter(self, store):
        self._chain(store)
        got = {f.id for f in store.get_neighbors("g-a", edge_types=["DEPENDS_ON"], max_hops=3)}
        # Only DEPENDS_ON edges: A has none directly; A->B is REFERENCES so
        # traversal cannot start. Nothing reachable.
        assert got == set()
        got_b = {f.id for f in store.get_neighbors("g-b", edge_types=["DEPENDS_ON"], max_hops=1)}
        assert got_b == {"g-c"}

    def test_get_neighbors_max_hops_zero_returns_empty(self, store):
        self._chain(store)
        assert store.get_neighbors("g-a", max_hops=0) == []

    def test_get_neighbors_limit_is_enforced(self, store):
        self._chain(store)
        assert len(store.get_neighbors("g-a", max_hops=1, limit=1)) <= 1

    def test_merge_counts_and_idempotency(self, store):
        store.add(_func("mg-1", "Existing"), SRC)
        graph = GraphData(
            nodes=[_func("mg-1", "Existing", version=9), _func("mg-2", "NewNode")],
            edges=[GraphEdge("mg-1", "mg-2", "REFERENCES", weight=0.7)],
        )
        result = store.merge(graph)
        assert result.merged is True
        assert result.new_functions == 1
        assert result.updated_functions == 1
        assert result.new_edges == 1
        # Re-merging the same graph creates no new edges/functions.
        result2 = store.merge(graph)
        assert result2.new_edges == 0
        assert result2.new_functions == 0
        assert result2.updated_functions == 2
        # The upserted node kept its version.
        assert store.get("mg-1").version == 9

    def test_get_graph_roundtrip(self, store):
        self._chain(store)
        graph = store.get_graph()
        assert {f.id for f in graph.nodes} == {"g-a", "g-b", "g-c", "g-d"}
        edges = {(e.source, e.target, e.edge_type) for e in graph.edges}
        assert ("g-a", "g-b", "REFERENCES") in edges
        assert ("g-b", "g-c", "DEPENDS_ON") in edges
        # Scoped graph matches Lite: requested nodes plus every incident edge.
        scoped = store.get_graph(func_ids=["g-a"])
        assert [node.id for node in scoped.nodes] == ["g-a"]
        assert {(edge.source, edge.target) for edge in scoped.edges} == {
            ("g-a", "g-b"),
            ("g-c", "g-a"),
        }


# ── Full-text search (real tsvector) ─────────────────────────────────


class TestFullTextSearch:
    def test_fts_english_match_and_rank(self, store):
        store.add(
            _func("fts-1", "User Login", trigger=[_fv("user logs into the system")]),
            SRC,
        )
        store.add(
            _func("fts-2", "Data Export", trigger=[_fv("export rows to csv")]),
            SRC,
        )
        results = store.fts_search("login")
        assert [r.func_id for r in results] == ["fts-1"]
        assert results[0].relevance_score > 0
        assert results[0].name == "User Login"

    def test_fts_matches_action_text(self, store):
        store.add(
            _func("fts-3", "Cleanup", action=[_fv("purge expired sessions")]),
            SRC,
        )
        results = store.fts_search("purge")
        assert [r.func_id for r in results] == ["fts-3"]

    def test_fts_no_match_returns_empty(self, store):
        store.add(_func("fts-4", "Alpha"), SRC)
        assert store.fts_search("zzz-unrelated-token") == []

    def test_fts_top_k(self, store):
        for i in range(5):
            store.add(_func(f"fts-k{i}", f"Login Variant{i}"), SRC)
        results = store.fts_search("login", top_k=3)
        assert len(results) == 3

    def test_fts_chinese_whole_token_only(self, store):
        """Documented real behaviour of to_tsvector('simple') on Chinese:
        an unsegmented run of CJK characters is indexed as ONE lexeme, so
        only the exact full string (or space-separated tokens) match.
        This is a real-engine limitation, recorded as-is (not a bug fix)."""
        store.add(_func("fts-zh1", "用户登录系统", domain="认证"), SRC)
        store.add(_func("fts-zh2", "数据 导出 功能"), SRC)
        # Exact full-string query matches (single lexeme equality).
        assert [r.func_id for r in store.fts_search("用户登录系统")] == ["fts-zh1"]
        # A substring of the lexeme does NOT match (no segmentation).
        assert store.fts_search("用户登录") == []
        # Space-separated tokens are independent lexemes and DO match.
        assert [r.func_id for r in store.fts_search("导出")] == ["fts-zh2"]


# ── Changelog / timeline ─────────────────────────────────────────────


class TestChangelog:
    def test_add_merge_delete_writes_timeline(self, store):
        store.add(_func("t-1", "Tracked"), SRC)
        store.add(
            _func("t-2", "tracked", name_normalized="tracked", trigger=[_fv("more")]),
            SRC,
        )
        store.delete("t-1")
        events = store.get_timeline("t-1")
        types = [e.event_type for e in events]
        assert types == ["deleted", "updated", "created"]  # DESC by ts
        assert all(e.actor == "system" for e in events)
        assert events[0].description

    def test_timeline_limit(self, store):
        store.add(_func("t-3", "Limited"), SRC)
        for _ in range(5):
            store.add(
                _func("t-x", "limited", name_normalized="limited", trigger=[_fv("x")]),
                SRC,
            )
        events = store.get_timeline("t-3", limit=3)
        assert len(events) == 3

    def test_changelog_records_source_path(self, store):
        store.add(_func("t-4", "Sourced"), SRC)
        events = store.get_timeline("t-4")
        assert events[0].source == "wiki/auth.md"


# ── Fact / Preference / Observation tables ───────────────────────────


class TestFactsPreferencesObservations:
    def test_fact_crud(self, store):
        fact = Fact(
            id="fact-1",
            name="User timezone",
            subject="alice",
            predicate="has_timezone",
            object_="Asia/Shanghai",
            owner="alice",
        )
        store.add_fact(fact)
        got = store.get_fact("fact-1")
        assert got is not None
        assert got.subject == "alice"
        assert got.object_ == "Asia/Shanghai"
        assert got.owner == "alice"
        assert got.created_at and got.updated_at  # stamped by the store
        # Upsert by id.
        fact.object_ = "UTC"
        store.add_fact(fact)
        assert store.get_fact("fact-1").object_ == "UTC"
        assert len(store.list_facts()) == 1
        assert [f.id for f in store.list_facts(owner="alice")] == ["fact-1"]
        assert store.list_facts(owner="bob") == []
        store.delete_fact("fact-1")
        assert store.get_fact("fact-1") is None

    def test_preference_crud(self, store):
        pref = Preference(
            id="pref-1",
            name="Editor",
            aspect="editor",
            preference="vim",
            owner="bob",
        )
        store.add_preference(pref)
        got = store.get_preference("pref-1")
        assert got is not None
        assert got.aspect == "editor"
        assert got.preference == "vim"
        pref.preference = "emacs"
        store.add_preference(pref)
        assert store.get_preference("pref-1").preference == "emacs"
        assert [p.id for p in store.list_preferences(owner="bob")] == ["pref-1"]
        store.delete_preference("pref-1")
        assert store.get_preference("pref-1") is None

    def test_fact_delete_records_changelog(self, store):
        store.add_fact(Fact(id="fact-9", name="F", subject="s"))
        store.delete_fact("fact-9")
        events = store.get_timeline("fact-9")
        assert [e.event_type for e in events] == ["deleted", "created"]

    def test_observation_crud(self, store):
        store.add_observation(
            Observation(
                id="obs-1",
                name="Deploy failed",
                event="deploy",
                context="prod",
                actor="ci-bot",
                category="bugfix",
                owner="ops",
            )
        )
        store.add_observation(Observation(id="obs-2", name="Note", category="note"))
        all_obs = store.list_observations()
        assert {o.id for o in all_obs} == {"obs-1", "obs-2"}
        by_cat = store.list_observations(category="bugfix")
        assert [o.id for o in by_cat] == ["obs-1"]
        assert by_cat[0].actor == "ci-bot"
        assert by_cat[0].context == "prod"
        by_owner = store.list_observations(owner="ops")
        assert [o.id for o in by_owner] == ["obs-1"]
        # Upsert by id.
        store.add_observation(Observation(id="obs-1", name="Deploy failed v2", category="bugfix"))
        assert len(store.list_observations()) == 2
        assert store.list_observations(category="bugfix")[0].name == "Deploy failed v2"


# ── pgvector hybrid (RRF) search ─────────────────────────────────────


class TestPgvectorHybrid:
    DIM = 32

    @pytest.fixture
    def vec_store(self, pg_dsn, pgvector_available, store):
        if not pgvector_available:
            pytest.skip("pgvector extension not available in this PostgreSQL build")
        embedder = _BagOfWordsEmbedder(self.DIM)
        resources = _ready_resources(pg_dsn, self.DIM)
        s = PostgresMemoryStore(
            dsn=pg_dsn,
            embedder=embedder,
            ready_pool=resources.ready_pool,
        )
        assert s._vector_dim == self.DIM  # extension really enabled, not degraded
        yield s
        resources.close()

    def test_extension_enabled_and_embedding_stored(self, vec_store):
        vec_store.add(
            _func("v-1", "User Login", trigger=[_fv("user authentication flow")]),
            SRC,
        )
        cur = vec_store._execute(
            "SELECT embedding IS NOT NULL FROM memplex_functions WHERE id = 'v-1'",
            commit=False,
        )
        row = cur.fetchone()
        cur.close()
        assert row == (True,)

    def test_vector_leg_orders_by_cosine(self, vec_store):
        """The pgvector cosine leg ranks the doc sharing more query tokens
        first, in agreement with the tsvector leg."""
        vec_store.add(
            _func("v-2", "User Login", trigger=[_fv("user login authentication")]),
            SRC,
        )
        vec_store.add(
            _func("v-3", "Banana Bread", trigger=[_fv("banana bread recipe")]),
            SRC,
        )
        results = vec_store.vector_search("user login", top_k=2)
        ids = [r.func_id for r in results]
        # The tsv leg already ranks v-2 first; the vector leg must agree.
        assert ids[0] == "v-2"

    def test_rrf_surfaces_vector_only_match(self, vec_store):
        """RRF merge: a doc that the tsvector leg CANNOT match still shows
        up via the pgvector leg. ``plainto_tsquery`` is AND semantics, so
        the two-token query "gamma delta" matches only v-4 in the tsv leg,
        while the vector leg also reaches v-5 (shared token "gamma")."""
        vec_store.add(
            _func("v-4", "Gamma Delta", trigger=[_fv("gamma delta epsilon")]),
            SRC,
        )
        vec_store.add(
            _func("v-5", "Gamma Zeta", trigger=[_fv("gamma zeta eta")]),
            SRC,
        )
        results = vec_store.vector_search("gamma delta", top_k=2)
        assert [r.func_id for r in results] == ["v-4", "v-5"]
        # v-4 was found by BOTH legs, so its fused score must exceed the
        # single-leg score of v-5.
        assert results[0].relevance_score > results[1].relevance_score

    def test_graceful_degradation_without_extension(self, pg_dsn, store, monkeypatch):
        """If CREATE EXTENSION vector fails the store degrades to
        tsvector-only instead of raising."""
        real_connect = psycopg2.connect

        class _NoVectorCursor:
            def __init__(self, cur):
                self._cur = cur

            def execute(self, sql, params=None):
                if "CREATE EXTENSION" in sql:
                    raise psycopg2.Error("extension not available (simulated)")
                return self._cur.execute(sql, params)

            def __getattr__(self, name):
                return getattr(self._cur, name)

        class _NoVectorConn:
            def __init__(self, conn):
                self._conn = conn

            def cursor(self):
                return _NoVectorCursor(self._conn.cursor())

            def __getattr__(self, name):
                return getattr(self._conn, name)

        monkeypatch.setattr(
            "psycopg2.connect", lambda dsn: _NoVectorConn(real_connect(dsn))
        )
        resources = _ready_resources(pg_dsn, self.DIM)
        try:
            s = PostgresMemoryStore(
                dsn=pg_dsn,
                ready_pool=resources.ready_pool,
            )
            s.add(_func("v-6", "Degraded Search"), SRC)
            # A best-effort capability failure is represented by effective
            # dim=0; a pre-existing extension may instead yield ready.
            expected_dim = (
                self.DIM
                if resources.ready_pool.status.state == "ready"
                else 0
            )
            assert s._vector_dim == expected_dim
            results = s.fts_search("degraded")
            assert [r.func_id for r in results] == ["v-6"]
        finally:
            resources.close()


# ── PostgresFeedbackStore ────────────────────────────────────────────


class TestPostgresFeedbackStore:
    def _fb(self, memory_id="fb-m1", role="trigger", **kw):
        defaults = dict(
            memory_id=memory_id,
            field_role=role,
            value_index=0,
            verdict=FeedbackVerdict.WRONG,
            reason="wrong value",
            source="user",
            timestamp=datetime(2025, 4, 1, 12, 0, 0),
            owner="alice",
            feedback_type="field_value",
            old_value="old",
            new_value="new",
            needs_review=True,
            needs_review_until=datetime(2025, 5, 1, tzinfo=timezone.utc),
        )
        defaults.update(kw)
        return MemoryFeedback(**defaults)

    def test_record_and_get_history(self, feedback_store):
        feedback_store.record(self._fb())
        feedback_store.record(
            self._fb(role="action", verdict=FeedbackVerdict.CORRECT, reason=None)
        )
        history = feedback_store.get_history("fb-m1")
        assert len(history) == 2
        fb = history[0]
        assert fb.memory_id == "fb-m1"
        assert fb.verdict in (FeedbackVerdict.WRONG, FeedbackVerdict.CORRECT)
        assert fb.owner == "alice"
        assert fb.old_value == "old"
        assert fb.new_value == "new"
        assert fb.needs_review is True
        # needs_review_until survived the TIMESTAMPTZ round-trip.
        assert fb.needs_review_until is not None

    def test_get_pending_groups_unresolved(self, feedback_store):
        feedback_store.record(self._fb())
        feedback_store.record(self._fb(role="action"))
        feedback_store.record(self._fb(memory_id="fb-m2"))
        # A resolved record must not show up as pending.
        feedback_store.record(
            self._fb(
                memory_id="fb-m3",
                needs_review=False,
                resolved_at=datetime(2025, 4, 2),
                resolution="accepted",
            )
        )
        pending = feedback_store.get_pending()
        keys = {(p.memory_id, p.field_role) for p in pending}
        assert keys == {("fb-m1", "trigger"), ("fb-m1", "action"), ("fb-m2", "trigger")}
        assert all(p.source == "user" for p in pending)

    def test_resolve_marks_records(self, feedback_store):
        feedback_store.record(self._fb())
        feedback_store.resolve("fb-m1", "trigger", "accepted")
        assert feedback_store.get_pending() == []
        history = feedback_store.get_history("fb-m1")
        assert history[0].needs_review is False
        assert history[0].resolution == "accepted"
        assert history[0].resolved_at is not None

    def test_history_limit_and_clear(self, feedback_store):
        for i in range(5):
            feedback_store.record(
                self._fb(timestamp=datetime(2025, 4, 1, 12, i, 0))
            )
        assert len(feedback_store.get_history("fb-m1", limit=3)) == 3
        feedback_store.clear()
        assert feedback_store.get_history("fb-m1") == []
        assert feedback_store.get_pending() == []

    def test_history_isolated_per_memory(self, feedback_store):
        feedback_store.record(self._fb(memory_id="fb-a"))
        feedback_store.record(self._fb(memory_id="fb-b"))
        assert len(feedback_store.get_history("fb-a")) == 1
        assert len(feedback_store.get_history("fb-b")) == 1

    @pytest.mark.parametrize("operation", ("record", "resolve", "clear"))
    def test_task4_feedback_writes_roll_back_after_sql_fault(
        self, feedback_store, monkeypatch, operation
    ):
        """A statement that succeeds before the fault is still rolled back."""
        if operation in {"resolve", "clear"}:
            feedback_store.record(self._fb(memory_id="task4-feedback"))
        before = [
            (item.memory_id, item.field_role, item.needs_review, item.resolution)
            for item in feedback_store.get_history("task4-feedback")
        ]
        original = feedback_store._execute_in_transaction

        def fail_after_sql(cur, sql, params=()):
            original(cur, sql, params)
            raise RuntimeError("task4 feedback fault")

        monkeypatch.setattr(feedback_store, "_execute_in_transaction", fail_after_sql)
        with pytest.raises(RuntimeError, match="task4 feedback fault"):
            if operation == "record":
                feedback_store.record(self._fb(memory_id="task4-feedback"))
            elif operation == "resolve":
                feedback_store.resolve("task4-feedback", "trigger", "accepted")
            else:
                feedback_store.clear()
        after = [
            (item.memory_id, item.field_role, item.needs_review, item.resolution)
            for item in feedback_store.get_history("task4-feedback")
        ]
        assert after == before

    def test_feedback_history_and_resolution_are_tenant_scoped(self, feedback_store):
        alice = feedback_store.authorized(
            _authorization(tenant="tenant-a", subject="alice")
        )
        bob = feedback_store.authorized(_authorization(tenant="tenant-b", subject="bob"))
        alice.record(self._fb(memory_id="shared-feedback", reason="alice-only"))
        bob.record(
            self._fb(
                memory_id="shared-feedback",
                owner="bob",
                source="bob",
                reason="bob-only",
            )
        )

        assert [item.reason for item in alice.get_history("shared-feedback")] == [
            "alice-only"
        ]
        assert [item.reason for item in bob.get_history("shared-feedback")] == ["bob-only"]

        bob.resolve("shared-feedback", "trigger", "bob-resolved")
        assert alice.get_pending()
        assert bob.get_pending() == []

    def test_feedback_visibility_acl_matches_memory_visibility(self, migration_dsn):
        role = "memplex_feedback_visibility"
        readiness = _ready_resources(migration_dsn)
        readiness.close()
        _provision_application_role(migration_dsn, role)
        application_store, resources = _application_feedback_store(migration_dsn, role)

        alice = application_store.authorized(
            _authorization(
                tenant="tenant-visibility",
                subject="alice",
                session="session-alice",
            )
        )
        bob = application_store.authorized(
            _authorization(
                tenant="tenant-visibility",
                subject="bob",
                session="session-bob",
            )
        )

        try:
            alice.record(
                self._fb(
                    memory_id="feedback-user-private",
                    reason="user-private",
                    visibility="user",
                )
            )
            alice.record(
                self._fb(
                    memory_id="feedback-workspace-shared",
                    reason="workspace-shared",
                    visibility="workspace",
                )
            )
            alice.record(
                self._fb(
                    memory_id="feedback-session-private",
                    reason="session-private",
                    visibility="session",
                )
            )

            assert [item.reason for item in alice.get_history("feedback-user-private")] == [
                "user-private"
            ]
            assert bob.get_history("feedback-user-private") == []
            assert [
                item.reason for item in bob.get_history("feedback-workspace-shared")
            ] == ["workspace-shared"]
            assert [
                item.reason for item in alice.get_history("feedback-session-private")
            ] == ["session-private"]
            assert bob.get_history("feedback-session-private") == []

            bob_pending = {item.memory_id for item in bob.get_pending()}
            assert "feedback-workspace-shared" in bob_pending
            assert "feedback-user-private" not in bob_pending
            assert "feedback-session-private" not in bob_pending
        finally:
            resources.close()
            _drop_feedback_role(migration_dsn, role)

    def test_feedback_scopes_remain_isolated_under_concurrency(
        self,
        migration_dsn,
        monkeypatch,
    ):
        """SET LOCAL and application SQL must be one connection-critical section."""
        # The pgserver owner is a superuser and therefore bypasses RLS.  Run
        # the contended application calls through a least-privileged role so
        # a leaked SET LOCAL value has the same effect it has in production.
        role = "memplex_feedback_concurrency"
        readiness = _ready_resources(migration_dsn)
        readiness.close()
        _provision_application_role(migration_dsn, role)
        application_store, resources = _application_feedback_store(migration_dsn, role)

        alice = application_store.authorized(
            _authorization(tenant="tenant-a", subject="alice")
        )
        bob = application_store.authorized(
            _authorization(tenant="tenant-b", subject="bob")
        )
        alice.record(self._fb(memory_id="concurrent-feedback", reason="alice-only"))
        bob.record(
            self._fb(
                memory_id="concurrent-feedback",
                owner="bob",
                source="bob",
                reason="bob-only",
            )
        )

        original_bind = application_store._bind_transaction_scope

        def run_contended(operation):
            # Without a connection-wide critical section, both threads reach
            # this barrier after SET LOCAL and the second tenant overwrites the
            # first before its query.  With a lock, the first waiter times out,
            # completes, and only then can the second request bind its scope.
            bound = Barrier(2, timeout=0.4)
            start = Barrier(3, timeout=5)

            def bind_then_pause(cur, context):
                original_bind(cur, context)
                try:
                    bound.wait()
                except BrokenBarrierError:
                    pass

            monkeypatch.setattr(
                application_store,
                "_bind_transaction_scope",
                bind_then_pause,
            )

            def invoke(scoped):
                start.wait()
                return operation(scoped)

            with ThreadPoolExecutor(max_workers=2) as executor:
                alice_result = executor.submit(invoke, alice)
                bob_result = executor.submit(invoke, bob)
                start.wait()
                return alice_result.result(timeout=5), bob_result.result(timeout=5)

        histories = run_contended(
            lambda scoped: [
                item.reason for item in scoped.get_history("concurrent-feedback")
            ]
        )
        assert histories == (["alice-only"], ["bob-only"])

        pending = run_contended(
            lambda scoped: [
                item.memory_id
                for item in scoped.get_pending()
                if item.memory_id == "concurrent-feedback"
            ]
        )
        assert pending == (["concurrent-feedback"], ["concurrent-feedback"])

        run_contended(
            lambda scoped: scoped.resolve(
                "concurrent-feedback",
                "trigger",
                f"resolved-{scoped._context.principal.subject_id}",
            )
        )
        monkeypatch.setattr(
            application_store,
            "_bind_transaction_scope",
            original_bind,
        )
        assert alice.get_history("concurrent-feedback")[0].resolution == "resolved-alice"
        assert bob.get_history("concurrent-feedback")[0].resolution == "resolved-bob"
        resources.close()
        _drop_feedback_role(migration_dsn, role)

    def test_feedback_table_forces_rls(self, migration_dsn):
        resources = _ready_resources(migration_dsn)
        resources.close()
        assert _admin_query(
            migration_dsn,
            """
            SELECT relrowsecurity, relforcerowsecurity
            FROM pg_class
            WHERE relname = 'feedback'
              AND relnamespace = current_schema()::regnamespace
            """
        ) == [(True, True)]


def test_v5_ingress_apply_inbound_accepts_verified_canonical_batch(migration_dsn):
    role = f"memplex_ingress_{uuid.uuid4().hex[:8]}"
    PostgresMigrationRunner(migration_dsn).apply()
    _migration_execute(migration_dsn, f"CREATE ROLE {role} LOGIN")
    _migration_execute(migration_dsn, f"GRANT USAGE ON SCHEMA { _admin_query(migration_dsn, 'SELECT current_schema()')[0][0] } TO {role}")
    _migration_execute(migration_dsn, f"GRANT EXECUTE ON FUNCTION memplex_sync_apply_inbound(bytea,text) TO {role}")
    _migration_execute(migration_dsn, f"SELECT memplex_configure_sync_ingress_principal('{role}', 'remote-node')")
    owner = psycopg2.connect(migration_dsn)
    try:
        owner_cursor = owner.cursor()
        owner_cursor.execute("SELECT set_config('memplex.tenant_id', 'tenant-inbound', true)")
        owner_cursor.execute("SELECT set_config('memplex.subject_id', 'owner-inbound', true)")
        owner_cursor.execute("SELECT set_config('memplex.workspace_id', 'workspace-inbound', true)")
        owner_cursor.execute("INSERT INTO memplex_sync_targets (tenant_id,target_id,remote_node_id,bootstrap_seq) VALUES ('tenant-inbound','same-remote','remote-node',0)")
        owner.commit()
        owner_cursor.close()
    finally:
        owner.close()
    event_id = "123e4567-e89b-42d3-a456-426614174001"
    event = SyncEvent(1, event_id, "remote-node", SyncNodeType.FUNCTION, SyncEntityKey.node("fn"), SyncOperation.UPSERT, str(SyncVersion.create(datetime(2026, 8, 11, tzinfo=timezone.utc), "remote-node", event_id)), SyncScope("tenant-inbound", "owner-inbound", "workspace-inbound", "user", None, None), {"id": "fn"})
    batch = SyncBatch(1, "123e4567-e89b-42d3-a456-426614174002", "remote-node", (event,))
    pretty = json.dumps(batch.to_dict(), indent=2).encode("utf-8")
    forged_event = SyncEvent(1, event_id, "other-remote", SyncNodeType.FUNCTION, SyncEntityKey.node("fn"), SyncOperation.UPSERT, str(SyncVersion.create(datetime(2026, 8, 11, tzinfo=timezone.utc), "other-remote", event_id)), SyncScope("tenant-inbound", "owner-inbound", "workspace-inbound", "user", None, None), {"id": "fn"})
    forged_origin = SyncBatch(1, "123e4567-e89b-42d3-a456-426614174003", "other-remote", (forged_event,))
    conn = psycopg2.connect(psycopg2.extensions.make_dsn(migration_dsn, user=role))
    try:
        cur = conn.cursor()
        with pytest.raises(SyncBatchRejected):
            validate_ingress_batch(pretty, hashlib.sha256(pretty).hexdigest())
        with pytest.raises(psycopg2.Error, match="origin is not authorised"):
            cur.execute("SELECT memplex_sync_apply_inbound(%s,%s)", (forged_origin.canonical_bytes, forged_origin.request_digest))
        conn.rollback()
        assert _admin_query(migration_dsn, "SELECT count(*) FROM memplex_sync_outbox") == [(0,)]
        cur.execute(
            "SELECT memplex_sync_apply_inbound(%s,%s)",
            (batch.canonical_bytes, batch.request_digest),
        )
        result = cur.fetchone()[0]
        conn.commit()
        cur.close()
    finally:
        conn.close()
    assert result["accepted"] == 1
    assert _admin_query(migration_dsn, "SELECT count(*) FROM memplex_sync_outbox") == [(1,)]
    assert _admin_query(migration_dsn, "SELECT id, data FROM memplex_functions") == [("fn", {"id": "fn"})]
    assert _admin_query(migration_dsn, "SELECT count(*) FROM memplex_sync_deliveries") == [(0,)]
    conn = psycopg2.connect(psycopg2.extensions.make_dsn(migration_dsn, user=role))
    try:
        cur = conn.cursor()
        cur.execute("SELECT memplex_sync_apply_inbound(%s,%s)", (batch.canonical_bytes, batch.request_digest))
        assert cur.fetchone()[0] == result
        changed_event = SyncEvent(
            1, event_id, "remote-node", SyncNodeType.FUNCTION, SyncEntityKey.node("fn"),
            SyncOperation.UPSERT,
            str(SyncVersion.create(datetime(2026, 8, 11, tzinfo=timezone.utc), "remote-node", event_id)),
            SyncScope("tenant-inbound", "owner-inbound", "workspace-inbound", "user", None, None),
            {"id": "changed"},
        )
        changed = SyncBatch(1, batch.batch_id, "remote-node", (changed_event,))
        with pytest.raises(psycopg2.Error, match="batch digest conflict"):
            cur.execute("SELECT memplex_sync_apply_inbound(%s,%s)", (changed.canonical_bytes, changed.request_digest))
        conn.rollback()
        cur.close()
    finally:
        conn.close()
    assert _admin_query(migration_dsn, "SELECT count(*) FROM memplex_sync_outbox") == [(1,)]
    _drop_unprivileged_role(migration_dsn, role)


def test_sync_resources_publish_verified_inbound_executor_on_real_postgres(migration_dsn):
    """The coordinated app/inbound boundary publishes only after joint readiness."""
    app_role = f"memplex_sync_app_{uuid.uuid4().hex[:8]}"
    ingress_role = f"memplex_sync_ingress_{uuid.uuid4().hex[:8]}"
    PostgresMigrationRunner(migration_dsn).apply()
    _provision_application_role(migration_dsn, app_role)
    _provision_ingress_role(migration_dsn, ingress_role, "remote-resource")
    app_dsn = psycopg2.extensions.make_dsn(migration_dsn, user=app_role)
    inbound_dsn = psycopg2.extensions.make_dsn(migration_dsn, user=ingress_role)
    resources = PostgresSyncStorageResources(
        app_dsn=app_dsn,
        migration_dsn=migration_dsn,
        inbound_dsn=inbound_dsn,
    )
    event_id = "123e4567-e89b-42d3-a456-426614174501"
    event = SyncEvent(
        1,
        event_id,
        "remote-resource",
        SyncNodeType.FUNCTION,
        SyncEntityKey.node("fn-resource"),
        SyncOperation.UPSERT,
        str(
            SyncVersion.create(
                datetime(2026, 8, 11, tzinfo=timezone.utc),
                "remote-resource",
                event_id,
            )
        ),
        SyncScope(
            "tenant-resource",
            "owner-resource",
            "workspace-resource",
            "user",
            None,
            None,
        ),
        {"id": "fn-resource"},
    )
    batch = SyncBatch(
        1,
        "123e4567-e89b-42d3-a456-426614174502",
        "remote-resource",
        (event,),
    )
    validated = validate_ingress_batch(batch.canonical_bytes, batch.request_digest)
    try:
        status = resources.ensure_ready(
            VectorCapabilityRequest(dim=0, policy="disabled"),
            "production",
        )
        assert status.state == "disabled"
        assert resources.state == "READY"
        assert resources.ready_pool.target == PostgresMigrationRunner(migration_dsn).inspect_target()

        store = PostgresMemoryStore(
            dsn=app_dsn,
            ready_pool=resources.ready_pool,
            require_authorization=True,
            inbound_executor=resources.executor,
            sync_capture_policy=SyncCapturePolicy(
                "required", local_node_id="local-resource"
            ),
        )
        result = store.authorized(
            _authorization(
                tenant="tenant-resource",
                subject="owner-resource",
                workspace="workspace-resource",
            )
        ).sync_apply_batch(batch)
        assert result.batch_id == batch.batch_id
        assert result.request_digest == batch.request_digest
        assert result.receipts[0].outcome == "accepted"
        assert _admin_query(
            migration_dsn,
            "SELECT id, data FROM memplex_functions WHERE id='fn-resource'",
        ) == [("fn-resource", {"id": "fn-resource"})]
        assert _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_sync_outbox",
        ) == [(1,)]

        replay = resources.executor.apply(validated)
        assert replay == result
        assert _admin_query(
            migration_dsn,
            "SELECT count(*) FROM memplex_sync_outbox",
        ) == [(1,)]
    finally:
        try:
            resources.close()
        finally:
            _drop_unprivileged_role(migration_dsn, ingress_role)
            _drop_unprivileged_role(migration_dsn, app_role)


def _noncanonical_b64url_tail(encoded: str) -> str:
    """Return a same-byte, noncanonical base64url spelling when tail bits exist."""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
    index = alphabet.index(encoded[-1])
    if len(encoded) % 4 == 2:
        replacement = (index & 0b110000) | 1
    elif len(encoded) % 4 == 3:
        replacement = (index & 0b111100) | 1
    else:
        raise AssertionError("test vector needs a base64url tail")
    return encoded[:-1] + alphabet[replacement]


def _codec_test_batch(
    *, entity_key: str | None = None, version: str | None = None
) -> tuple[bytes, str]:
    """Build an otherwise Task1-canonical batch for SQL preflight negatives."""
    origin = "remote-codec"
    event_id = "123e4567-e89b-42d3-a456-426614174201"
    event = SyncEvent(
        1,
        event_id,
        origin,
        SyncNodeType.EDGE if entity_key is not None else SyncNodeType.FUNCTION,
        SyncEntityKey.edge("fn-codec", "fn-codec", "supports")
        if entity_key is not None
        else SyncEntityKey.node("fn-codec"),
        SyncOperation.UPSERT,
        str(SyncVersion.create(datetime(2026, 8, 11, tzinfo=timezone.utc), origin, event_id)),
        SyncScope("tenant-codec", "owner-codec", "workspace-codec", "user", None, None),
        (
            {
                "weight": 1.0,
                "evidence": [],
                "created_at": "2026-08-11T00:00:00.000000Z",
            }
            if entity_key is not None
            else {"id": "fn-codec"}
        ),
    )
    document = SyncBatch(1, "123e4567-e89b-42d3-a456-426614174202", origin, (event,)).to_dict()
    if entity_key is not None:
        document["events"][0]["entity_key"] = entity_key
    if version is not None:
        document["events"][0]["version"] = version
    raw = _canonical_json_bytes(document)
    return raw, hashlib.sha256(raw).hexdigest()


def _assert_codec_preflight_has_no_side_effects(dsn: str) -> None:
    for table in (
        "memplex_functions",
        "memplex_edges",
        "memplex_sync_outbox",
        "memplex_sync_inbox",
        "memplex_sync_batches",
        "memplex_sync_entity_versions",
        "memplex_sync_deliveries",
    ):
        assert _admin_query(dsn, f"SELECT count(*) FROM {table}") == [(0,)]
    assert _admin_query(
        dsn, "SELECT last_value, is_called FROM memplex_sync_outbox_stream_seq_seq"
    ) == [(1, False)]


@pytest.mark.parametrize(
    ("codec", "decoded", "mutation"),
    [
        ("edge", '["fn-codec", "fn-codec", "supports"]', "identity"),
        ("edge", '["fn-codec","fn-codec","\\u0073upports"]', "identity"),
        ("edge", '["fn-codec","fn-codec","supports"]', "padding"),
        ("edge", '["fn-codec","fn-codec","supports"]', "tail"),
        (
            "version",
            '["2026-08-11T00:00:00.000000Z", "remote-codec", "123e4567-e89b-42d3-a456-426614174201"]',
            "identity",
        ),
        (
            "version",
            '["2026-08-11T00:00:00.000000Z","remote-\\u0063odec","123e4567-e89b-42d3-a456-426614174201"]',
            "identity",
        ),
        (
            "version",
            '["2026-13-41T00:00:00.000000Z","remote-codec","123e4567-e89b-42d3-a456-426614174201"]',
            "identity",
        ),
        (
            "version",
            '["2026-08-11T00:00:00.000000Z","remote-codec","123e4567-e89b-42d3-a456-426614174201"]',
            "padding",
        ),
        (
            "version",
            '["2026-08-11T00:00:00.000000Z","remote-codec","123e4567-e89b-42d3-a456-426614174201"]',
            "tail",
        ),
    ],
)
def test_v5_ingress_preflight_rejects_noncanonical_inner_codecs_before_side_effects(
    migration_dsn, codec, decoded, mutation
):
    """Task1 and PostgreSQL reject the same fixed-array codec alternatives."""
    PostgresMigrationRunner(migration_dsn).apply()
    role = f"memplex_ingress_codec_{uuid.uuid4().hex[:8]}"
    schema = _admin_query(migration_dsn, "SELECT current_schema()")[0][0]
    _migration_execute(migration_dsn, f"CREATE ROLE {role} LOGIN")
    _migration_execute(migration_dsn, f"GRANT USAGE ON SCHEMA {schema} TO {role}")
    _migration_execute(
        migration_dsn,
        f"GRANT EXECUTE ON FUNCTION memplex_sync_apply_inbound(bytea,text) TO {role}",
    )
    _migration_execute(
        migration_dsn,
        f"SELECT memplex_configure_sync_ingress_principal('{role}', 'remote-codec')",
    )
    encoded = base64.urlsafe_b64encode(decoded.encode()).decode().rstrip("=")
    if mutation == "padding":
        encoded += "="
    elif mutation == "tail":
        encoded = _noncanonical_b64url_tail(encoded)
    value = f"{'edge' if codec == 'edge' else 'v1'}:v1:{encoded}" if codec == "edge" else f"v1:{encoded}"
    raw, digest = _codec_test_batch(
        entity_key=value if codec == "edge" else None,
        version=value if codec == "version" else None,
    )
    with pytest.raises(SyncBatchRejected):
        validate_ingress_batch(raw, digest)
    conn = psycopg2.connect(psycopg2.extensions.make_dsn(migration_dsn, user=role))
    try:
        cur = conn.cursor()
        with pytest.raises(psycopg2.Error) as raised:
            cur.execute("SELECT memplex_sync_apply_inbound(%s,%s)", (raw, digest))
        assert raised.value.pgcode == "22023"
        conn.rollback()
        cur.close()
    finally:
        conn.close()
    _assert_codec_preflight_has_no_side_effects(migration_dsn)
    _drop_unprivileged_role(migration_dsn, role)


def test_v5_codec_helper_accepts_task1_control_and_unicode_fixed_arrays(migration_dsn):
    """The narrow SQL encoder preserves Task1's canonical string escaping."""
    PostgresMigrationRunner(migration_dsn).apply()
    edge = SyncEntityKey.edge("control\\x01", "\u2028\u2029", "astral-😀")
    version = str(
        SyncVersion.create(
            datetime(2026, 8, 11, tzinfo=timezone.utc),
            "origin-\u2028😀",
            "123e4567-e89b-42d3-a456-426614174203",
        )
    )
    _admin_execute(
        migration_dsn,
        "SELECT memplex_sync_require_canonical_entity_key(%s, 'edge')",
        (str(edge),),
    )
    _admin_execute(
        migration_dsn,
        "SELECT memplex_sync_require_canonical_version(%s, %s, %s)",
        (version, "origin-\u2028😀", "123e4567-e89b-42d3-a456-426614174203"),
    )


@pytest.mark.parametrize(
    "edge_parts",
    (
        ("", "fn-codec", "supports"),
        ("fn-codec", "", "supports"),
        ("fn-codec", "fn-codec", ""),
        ("fn-codec", "fn-codec", "x" * 257),
        ("fn-codec", "fn-codec", "é" * 128 + "a"),
        ("\x01" * 256, "\x01" * 256, "\x01" * 256),
    ),
)
def test_v5_ingress_preflight_rejects_task1_edge_codec_bounds_before_side_effects(
    migration_dsn, edge_parts
):
    """SQL defense-in-depth keeps Task1's nonempty, byte and wire-key bounds."""
    PostgresMigrationRunner(migration_dsn).apply()
    role = f"memplex_ingress_edge_bound_{uuid.uuid4().hex[:8]}"
    schema = _admin_query(migration_dsn, "SELECT current_schema()")[0][0]
    _migration_execute(migration_dsn, f"CREATE ROLE {role} LOGIN")
    _migration_execute(migration_dsn, f"GRANT USAGE ON SCHEMA {schema} TO {role}")
    _migration_execute(
        migration_dsn,
        f"GRANT EXECUTE ON FUNCTION memplex_sync_apply_inbound(bytea,text) TO {role}",
    )
    _migration_execute(
        migration_dsn,
        f"SELECT memplex_configure_sync_ingress_principal('{role}', 'remote-codec')",
    )
    decoded = _canonical_json_bytes(list(edge_parts))
    entity_key = "edge:v1:" + base64.urlsafe_b64encode(decoded).decode().rstrip("=")
    raw, digest = _codec_test_batch(entity_key=entity_key)
    with pytest.raises(SyncBatchRejected):
        validate_ingress_batch(raw, digest)
    conn = psycopg2.connect(psycopg2.extensions.make_dsn(migration_dsn, user=role))
    try:
        cur = conn.cursor()
        with pytest.raises(psycopg2.Error) as raised:
            cur.execute("SELECT memplex_sync_apply_inbound(%s,%s)", (raw, digest))
        assert raised.value.pgcode == "22023"
        conn.rollback()
        cur.close()
    finally:
        conn.close()
    _assert_codec_preflight_has_no_side_effects(migration_dsn)
    _drop_unprivileged_role(migration_dsn, role)


def test_v5_codec_helper_accepts_task1_256_byte_edge_components(migration_dsn):
    PostgresMigrationRunner(migration_dsn).apply()
    edge = SyncEntityKey.edge("é" * 128, "x" * 256, "y" * 256)
    _admin_execute(
        migration_dsn,
        "SELECT memplex_sync_require_canonical_entity_key(%s, 'edge')",
        (str(edge),),
    )


@pytest.mark.parametrize(
    "timestamp",
    (
        "2026-12-31T24:00:00.000000Z",
        "2026-12-31T23:59:60.000000Z",
        "2026-02-29T00:00:00.000000Z",
    ),
)
def test_v5_ingress_preflight_rejects_task1_version_calendar_spellings_before_side_effects(
    migration_dsn, timestamp
):
    PostgresMigrationRunner(migration_dsn).apply()
    role = f"memplex_ingress_time_bound_{uuid.uuid4().hex[:8]}"
    schema = _admin_query(migration_dsn, "SELECT current_schema()")[0][0]
    _migration_execute(migration_dsn, f"CREATE ROLE {role} LOGIN")
    _migration_execute(migration_dsn, f"GRANT USAGE ON SCHEMA {schema} TO {role}")
    _migration_execute(
        migration_dsn,
        f"GRANT EXECUTE ON FUNCTION memplex_sync_apply_inbound(bytea,text) TO {role}",
    )
    _migration_execute(
        migration_dsn,
        f"SELECT memplex_configure_sync_ingress_principal('{role}', 'remote-codec')",
    )
    event_id = "123e4567-e89b-42d3-a456-426614174201"
    version = "v1:" + base64.urlsafe_b64encode(
        _canonical_json_bytes([timestamp, "remote-codec", event_id])
    ).decode().rstrip("=")
    raw, digest = _codec_test_batch(version=version)
    with pytest.raises(SyncBatchRejected):
        validate_ingress_batch(raw, digest)
    conn = psycopg2.connect(psycopg2.extensions.make_dsn(migration_dsn, user=role))
    try:
        cur = conn.cursor()
        with pytest.raises(psycopg2.Error) as raised:
            cur.execute("SELECT memplex_sync_apply_inbound(%s,%s)", (raw, digest))
        assert raised.value.pgcode == "22023"
        conn.rollback()
        cur.close()
    finally:
        conn.close()
    _assert_codec_preflight_has_no_side_effects(migration_dsn)
    _drop_unprivileged_role(migration_dsn, role)


@pytest.mark.parametrize("timestamp", ("2024-02-29T12:34:56.123456Z", "2026-08-11T00:00:00.000001Z"))
def test_v5_codec_helper_accepts_task1_valid_calendar_microseconds(migration_dsn, timestamp):
    PostgresMigrationRunner(migration_dsn).apply()
    event_id = "123e4567-e89b-42d3-a456-426614174203"
    version = "v1:" + base64.urlsafe_b64encode(
        _canonical_json_bytes([timestamp, "remote-codec", event_id])
    ).decode().rstrip("=")
    _admin_execute(
        migration_dsn,
        "SELECT memplex_sync_require_canonical_version(%s, %s, %s)",
        (version, "remote-codec", event_id),
    )


def _edge_payload_test_context(migration_dsn):
    """Seed visible endpoints for one inbound edge-payload projection test."""
    role = f"memplex_ingress_edge_payload_{uuid.uuid4().hex[:8]}"
    origin = "remote-edge-payload"
    tenant = "tenant-edge-payload"
    scope = SyncScope(tenant, "owner-edge-payload", "workspace-edge-payload", "user", None, None)
    PostgresMigrationRunner(migration_dsn).apply()
    schema = _admin_query(migration_dsn, "SELECT current_schema()")[0][0]
    _migration_execute(migration_dsn, f"CREATE ROLE {role} LOGIN")
    _migration_execute(migration_dsn, f"GRANT USAGE ON SCHEMA {schema} TO {role}")
    _migration_execute(
        migration_dsn,
        f"GRANT EXECUTE ON FUNCTION memplex_sync_apply_inbound(bytea,text) TO {role}",
    )
    _migration_execute(
        migration_dsn,
        f"SELECT memplex_configure_sync_ingress_principal('{role}', '{origin}')",
    )
    base = datetime(2026, 8, 11, tzinfo=timezone.utc)

    def event(number, kind, key, payload, second=0):
        event_id = f"123e4567-e89b-42d3-a456-426614174{number:03d}"
        return SyncEvent(
            1,
            event_id,
            origin,
            kind,
            key,
            SyncOperation.UPSERT,
            str(SyncVersion.create(base + timedelta(seconds=second), origin, event_id)),
            scope,
            payload,
        )

    def apply(batch):
        conn = psycopg2.connect(psycopg2.extensions.make_dsn(migration_dsn, user=role))
        try:
            cur = conn.cursor()
            cur.execute("SELECT memplex_sync_apply_inbound(%s,%s)", (batch.canonical_bytes, batch.request_digest))
            result = cur.fetchone()[0]
            conn.commit()
            cur.close()
            return result
        finally:
            conn.close()

    seed = SyncBatch(
        1,
        "123e4567-e89b-42d3-a456-426614174240",
        origin,
        (
            event(241, SyncNodeType.FUNCTION, SyncEntityKey.node("edge-left"), {"id": "edge-left"}),
            event(242, SyncNodeType.FUNCTION, SyncEntityKey.node("edge-right"), {"id": "edge-right"}),
        ),
    )
    assert apply(seed)["accepted"] == 2
    return role, origin, event, apply


def test_v5_ingress_edge_payload_projects_weight_evidence_and_created_at(migration_dsn):
    role, origin, event, apply = _edge_payload_test_context(migration_dsn)
    key = SyncEntityKey.edge("edge-left", "edge-right", "LINKS")
    try:
        first = SyncBatch(
            1,
            "123e4567-e89b-42d3-a456-426614174243",
            origin,
            (
                event(
                    243,
                    SyncNodeType.EDGE,
                    key,
                    {
                        "weight": 2.5,
                        "evidence": ["first", "U+2028\u2029😀"],
                        "created_at": "2024-02-29T12:34:56.123456Z",
                    },
                    1,
                ),
            ),
        )
        assert apply(first)["accepted"] == 1
        assert _admin_query(
            migration_dsn,
            "SELECT weight, evidence, created_at FROM memplex_edges WHERE source='edge-left'",
        ) == [(2.5, ["first", "U+2028\u2029😀"], datetime(2024, 2, 29, 12, 34, 56, 123456, tzinfo=timezone.utc))]
        second = SyncBatch(
            1,
            "123e4567-e89b-42d3-a456-426614174244",
            origin,
            (
                event(
                    244,
                    SyncNodeType.EDGE,
                    key,
                    {
                        "weight": -0.25,
                        "evidence": [],
                        "created_at": "2026-08-11T00:00:00.000001Z",
                    },
                    2,
                ),
            ),
        )
        assert apply(second)["accepted"] == 1
        assert _admin_query(
            migration_dsn,
            "SELECT weight, evidence, created_at FROM memplex_edges WHERE source='edge-left'",
        ) == [(-0.25, [], datetime(2026, 8, 11, 0, 0, 0, 1, tzinfo=timezone.utc))]
    finally:
        _drop_unprivileged_role(migration_dsn, role)


@pytest.mark.parametrize(
    "payload",
    (
        {"weight": 1.0, "evidence": [], "created_at": "2026-08-11T00:00:00.000000Z", "extra": True},
        {"weight": 1.0, "evidence": []},
        {"weight": "1", "evidence": [], "created_at": "2026-08-11T00:00:00.000000Z"},
        {"weight": True, "evidence": [], "created_at": "2026-08-11T00:00:00.000000Z"},
        {"weight": 1e100, "evidence": [], "created_at": "2026-08-11T00:00:00.000000Z"},
        {"weight": 1.0, "evidence": ["ok", 1], "created_at": "2026-08-11T00:00:00.000000Z"},
        {"weight": 1.0, "evidence": {}, "created_at": "2026-08-11T00:00:00.000000Z"},
        {"weight": 1.0, "evidence": [], "created_at": "2026-12-31T24:00:00.000000Z"},
    ),
)
def test_v5_ingress_edge_payload_preflight_rejects_invalid_semantics_without_side_effects(
    migration_dsn, payload
):
    role, origin, event, _apply = _edge_payload_test_context(migration_dsn)
    key = SyncEntityKey.edge("edge-left", "edge-right", "LINKS")
    batch = SyncBatch(
        1,
        "123e4567-e89b-42d3-a456-426614174245",
        origin,
        (event(245, SyncNodeType.EDGE, key, payload, 1),),
    )
    baseline = {
        table: _admin_query(migration_dsn, f"SELECT count(*) FROM {table}")
        for table in (
            "memplex_functions", "memplex_edges", "memplex_sync_outbox", "memplex_sync_inbox",
            "memplex_sync_batches", "memplex_sync_entity_versions", "memplex_sync_deliveries",
        )
    }
    sequence = _admin_query(
        migration_dsn, "SELECT last_value, is_called FROM memplex_sync_outbox_stream_seq_seq"
    )
    conn = psycopg2.connect(psycopg2.extensions.make_dsn(migration_dsn, user=role))
    try:
        cur = conn.cursor()
        with pytest.raises(psycopg2.Error) as raised:
            cur.execute("SELECT memplex_sync_apply_inbound(%s,%s)", (batch.canonical_bytes, batch.request_digest))
        assert raised.value.pgcode == "22023"
        conn.rollback()
        cur.close()
    finally:
        conn.close()
    for table, expected in baseline.items():
        assert _admin_query(migration_dsn, f"SELECT count(*) FROM {table}") == expected
    assert _admin_query(
        migration_dsn, "SELECT last_value, is_called FROM memplex_sync_outbox_stream_seq_seq"
    ) == sequence
    _drop_unprivileged_role(migration_dsn, role)


def test_v5_ingress_apply_inbound_preserves_all_node_kinds_lww_and_explicit_edge_deletes(migration_dsn):
    """The ingress boundary, rather than application GUCs, owns remote mutation.

    This intentionally exercises the SQL projection directly.  Task 3 may wrap
    it in a repository, but cannot replace its atomic/LWW/no-cascade contract.
    """
    role = f"memplex_ingress_matrix_{uuid.uuid4().hex[:8]}"
    PostgresMigrationRunner(migration_dsn).apply()
    schema = _admin_query(migration_dsn, "SELECT current_schema()")[0][0]
    _migration_execute(migration_dsn, f"CREATE ROLE {role} LOGIN")
    _migration_execute(migration_dsn, f"GRANT USAGE ON SCHEMA {schema} TO {role}")
    _migration_execute(migration_dsn, f"GRANT EXECUTE ON FUNCTION memplex_sync_apply_inbound(bytea,text) TO {role}")
    _migration_execute(migration_dsn, f"SELECT memplex_configure_sync_ingress_principal('{role}', 'remote-matrix')")

    scope = SyncScope("tenant-matrix", "owner-matrix", "workspace-matrix", "user", None, None)
    origin = "remote-matrix"
    base = datetime(2026, 8, 11, tzinfo=timezone.utc)

    def event(number: int, kind: SyncNodeType, key: SyncEntityKey, operation: SyncOperation, payload: dict[str, object] | None, second: int = 0) -> SyncEvent:
        event_id = f"123e4567-e89b-42d3-a456-426614174{number:03d}"
        return SyncEvent(
            1, event_id, origin, kind, key, operation,
            str(SyncVersion.create(base + timedelta(seconds=second), origin, event_id)),
            scope, payload,
        )

    function = event(101, SyncNodeType.FUNCTION, SyncEntityKey.node("fn-matrix"), SyncOperation.UPSERT, {"id": "fn-matrix"})
    fact = event(102, SyncNodeType.FACT, SyncEntityKey.node("fact-matrix"), SyncOperation.UPSERT, {"id": "fact-matrix"})
    preference = event(103, SyncNodeType.PREFERENCE, SyncEntityKey.node("preference-matrix"), SyncOperation.UPSERT, {"id": "preference-matrix"})
    observation = event(104, SyncNodeType.OBSERVATION, SyncEntityKey.node("observation-matrix"), SyncOperation.UPSERT, {"id": "observation-matrix"})
    # The legacy core schema constrains an edge target to a Function; keep the
    # edge valid while still exercising its independent durable identity.
    edge_key = SyncEntityKey.edge("fn-matrix", "fn-matrix", "supports")
    edge = event(
        105,
        SyncNodeType.EDGE,
        edge_key,
        SyncOperation.UPSERT,
        {"weight": 1.0, "evidence": ["matrix"], "created_at": "2026-08-11T00:00:00.000000Z"},
    )
    initial = SyncBatch(1, "123e4567-e89b-42d3-a456-426614174110", origin, (function, fact, preference, observation, edge))

    def apply(batch: SyncBatch) -> dict[str, object]:
        conn = psycopg2.connect(psycopg2.extensions.make_dsn(migration_dsn, user=role))
        try:
            cur = conn.cursor()
            cur.execute("SELECT memplex_sync_apply_inbound(%s,%s)", (batch.canonical_bytes, batch.request_digest))
            result = cur.fetchone()[0]
            conn.commit()
            cur.close()
            return result
        finally:
            conn.close()

    assert apply(initial)["accepted"] == 5
    assert _admin_query(migration_dsn, "SELECT count(*) FROM memplex_functions") == [(1,)]
    assert _admin_query(migration_dsn, "SELECT count(*) FROM memplex_facts") == [(1,)]
    assert _admin_query(migration_dsn, "SELECT count(*) FROM memplex_preferences") == [(1,)]
    assert _admin_query(migration_dsn, "SELECT count(*) FROM memplex_observations") == [(1,)]
    assert _admin_query(migration_dsn, "SELECT count(*) FROM memplex_edges") == [(1,)]

    # A Function deletion cannot silently cascade an un-signed edge deletion.
    blocked = SyncBatch(1, "123e4567-e89b-42d3-a456-426614174111", origin, (
        event(106, SyncNodeType.FUNCTION, SyncEntityKey.node("fn-matrix"), SyncOperation.TOMBSTONE, None, 10),
    ))
    with pytest.raises(psycopg2.Error, match="requires explicit edge tombstones"):
        apply(blocked)
    assert _admin_query(migration_dsn, "SELECT count(*) FROM memplex_functions") == [(1,)]

    # The signed edge tombstone before the Function tombstone is accepted.
    tombstones = SyncBatch(1, "123e4567-e89b-42d3-a456-426614174112", origin, (
        event(107, SyncNodeType.EDGE, edge_key, SyncOperation.TOMBSTONE, None, 11),
        event(108, SyncNodeType.FUNCTION, SyncEntityKey.node("fn-matrix"), SyncOperation.TOMBSTONE, None, 12),
        event(109, SyncNodeType.FACT, SyncEntityKey.node("fact-matrix"), SyncOperation.TOMBSTONE, None, 13),
        event(110, SyncNodeType.PREFERENCE, SyncEntityKey.node("preference-matrix"), SyncOperation.TOMBSTONE, None, 14),
        event(111, SyncNodeType.OBSERVATION, SyncEntityKey.node("observation-matrix"), SyncOperation.TOMBSTONE, None, 15),
    ))
    assert apply(tombstones)["accepted"] == 5
    for table in ("memplex_functions", "memplex_facts", "memplex_preferences", "memplex_observations", "memplex_edges"):
        assert _admin_query(migration_dsn, f"SELECT count(*) FROM {table}") == [(0,)]

    newer = event(112, SyncNodeType.FACT, SyncEntityKey.node("lww-matrix"), SyncOperation.UPSERT, {"v": "new"}, 30)
    assert apply(SyncBatch(1, "123e4567-e89b-42d3-a456-426614174113", origin, (newer,)))["accepted"] == 1
    older = event(113, SyncNodeType.FACT, SyncEntityKey.node("lww-matrix"), SyncOperation.UPSERT, {"v": "old"}, 20)
    conflict = apply(SyncBatch(1, "123e4567-e89b-42d3-a456-426614174114", origin, (older,)))
    assert conflict["conflict"] == 1
    duplicate = apply(SyncBatch(1, "123e4567-e89b-42d3-a456-426614174115", origin, (newer,)))
    assert duplicate["duplicate"] == 1
    assert _admin_query(migration_dsn, "SELECT data FROM memplex_facts WHERE id='lww-matrix'") == [({"v": "new"},)]
    _drop_unprivileged_role(migration_dsn, role)


def test_v5_ingress_preflight_rejects_later_bad_event_without_outbox_sequence_leak(migration_dsn):
    role = f"memplex_ingress_fault_{uuid.uuid4().hex[:8]}"
    PostgresMigrationRunner(migration_dsn).apply()
    schema = _admin_query(migration_dsn, "SELECT current_schema()")[0][0]
    _migration_execute(migration_dsn, f"CREATE ROLE {role} LOGIN")
    _migration_execute(migration_dsn, f"GRANT USAGE ON SCHEMA {schema} TO {role}")
    _migration_execute(migration_dsn, f"GRANT EXECUTE ON FUNCTION memplex_sync_apply_inbound(bytea,text) TO {role}")
    _migration_execute(migration_dsn, f"SELECT memplex_configure_sync_ingress_principal('{role}', 'remote-fault')")
    scope = SyncScope("tenant-fault", "owner-fault", "workspace-fault", "user", None, None)
    origin = "remote-fault"
    events = []
    for number, node_id in ((121, "first-fault"), (122, "later-fault")):
        event_id = f"123e4567-e89b-42d3-a456-426614174{number:03d}"
        events.append(SyncEvent(
            1, event_id, origin, SyncNodeType.FUNCTION, SyncEntityKey.node(node_id),
            SyncOperation.UPSERT,
            str(SyncVersion.create(datetime(2026, 8, 11, tzinfo=timezone.utc), origin, event_id)),
            scope, {"id": node_id},
        ))
    batch = SyncBatch(1, "123e4567-e89b-42d3-a456-426614174123", origin, tuple(events))
    malformed = batch.to_dict()
    malformed["events"][1].pop("scope")
    raw = json.dumps(malformed, sort_keys=True, separators=(",", ":")).encode("utf-8")
    conn = psycopg2.connect(psycopg2.extensions.make_dsn(migration_dsn, user=role))
    try:
        cur = conn.cursor()
        with pytest.raises(psycopg2.Error, match="inbound event is invalid"):
            cur.execute("SELECT memplex_sync_apply_inbound(%s,%s)", (raw, hashlib.sha256(raw).hexdigest()))
        conn.rollback()
        cur.close()
    finally:
        conn.close()
    assert _admin_query(migration_dsn, "SELECT count(*) FROM memplex_functions") == [(0,)]
    assert _admin_query(migration_dsn, "SELECT count(*) FROM memplex_sync_outbox") == [(0,)]
    assert _admin_query(migration_dsn, "SELECT count(*) FROM memplex_sync_deliveries") == [(0,)]
    assert _admin_query(migration_dsn, "SELECT last_value, is_called FROM memplex_sync_outbox_stream_seq_seq") == [(1, False)]
    _drop_unprivileged_role(migration_dsn, role)
