"""Observed-state ledger validation, split from ``runner.py``.

``_read_ledger_if_present`` / ``_validate_ledger`` / ``_plan_from_observed_state``
/ ``_validate_legacy_belongs_to_edges`` read the on-disk migration ledger,
validate it against discovered migrations, and plan the work needed to reach
the target schema. They depend only on the migration data classes and are
re-exported from ``memplex.storage.migrations.runner`` (where
``PostgresMigrationRunner`` calls them; the test suite monkeypatches
``runner._read_ledger_if_present``, which keeps working because the class
resolves the bare name against the runner module global at call time).
"""

from __future__ import annotations

from typing import Any

from memplex.models import domain_node_id

# Data classes and the ledger-table constant live in ``_constants`` (the
# dependency-free shared module for this migration cluster).
from memplex.storage.migrations._constants import (
    _LEDGER_TABLE,
    Migration,
    MigrationIntegrityError,
    MigrationPlan,
    SchemaFingerprint,
    _LedgerEntry,
)

# Catalogue-check helpers live in ``catalogue_checks`` (one-directional).
from memplex.storage.migrations.catalogue_checks import (
    _allowed_adoption_baselines,
    _is_background_tasks_current_variant,
    _is_edge_integrity_current_variant,
    _is_reliable_sync_current_variant,
    _variant_digest,
)


def _read_ledger_if_present(cur: Any) -> tuple[_LedgerEntry, ...]:
    cur.execute("SELECT to_regclass(%s)", (_LEDGER_TABLE,))
    if cur.fetchone()[0] is None:
        return ()
    cur.execute(
        """
        SELECT c.relkind, c.relrowsecurity, c.relforcerowsecurity,
               c.relowner = (
                   SELECT role.oid
                   FROM pg_catalog.pg_roles AS role
                   WHERE role.rolname = current_user
               ),
               c.relacl IS NULL,
               c.relpersistence,
               c.relispartition,
               NOT EXISTS (
                   SELECT 1
                   FROM pg_catalog.pg_inherits AS inheritance
                   WHERE inheritance.inhrelid = c.oid OR inheritance.inhparent = c.oid
               )
        FROM pg_catalog.pg_class AS c
        WHERE c.oid = %s::regclass
        """,
        (_LEDGER_TABLE,),
    )
    if cur.fetchone() != ("r", False, False, True, True, "p", False, True):
        raise MigrationIntegrityError("migration ledger has an unrecognised shape")
    cur.execute(
        """
        SELECT column_name, data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_schema = current_schema() AND table_name = %s
        ORDER BY ordinal_position
        """,
        (_LEDGER_TABLE,),
    )
    expected_columns = (
        ("version", "integer", "NO", None),
        ("name", "text", "NO", None),
        ("checksum", "text", "NO", None),
        ("applied_at", "timestamp with time zone", "NO", None),
        ("execution_mode", "text", "NO", None),
        ("baseline_fingerprint", "text", "YES", None),
    )
    if tuple(cur.fetchall()) != expected_columns:
        raise MigrationIntegrityError("migration ledger has an unrecognised shape")
    cur.execute(
        """
        SELECT bool_and(attribute.attacl IS NULL),
               bool_and(attribute.attcollation = typ.typcollation)
        FROM pg_catalog.pg_attribute AS attribute
        JOIN pg_catalog.pg_type AS typ ON typ.oid = attribute.atttypid
        WHERE attribute.attrelid = %s::regclass
          AND attribute.attnum > 0
          AND NOT attribute.attisdropped
        """,
        (_LEDGER_TABLE,),
    )
    if cur.fetchone() != (True, True):
        raise MigrationIntegrityError("migration ledger has an unrecognised shape")
    cur.execute(
        """
        SELECT con.contype, con.conname, pg_catalog.pg_get_constraintdef(con.oid),
               con.condeferrable, con.condeferred, con.convalidated,
               ARRAY_AGG(attribute.attname ORDER BY key.ordinality)
        FROM pg_catalog.pg_constraint AS con
        CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS key(attnum, ordinality)
        JOIN pg_catalog.pg_attribute AS attribute
          ON attribute.attrelid = con.conrelid AND attribute.attnum = key.attnum
        WHERE con.conrelid = %s::regclass
        GROUP BY con.oid, con.contype
        ORDER BY con.contype
        """,
        (_LEDGER_TABLE,),
    )
    if tuple(
        (kind, name, definition, deferrable, deferred, validated, tuple(columns))
        for kind, name, definition, deferrable, deferred, validated, columns in cur.fetchall()
    ) != (
        (
            "p",
            "memplex_schema_migrations_pkey",
            "PRIMARY KEY (version)",
            False,
            False,
            True,
            ("version",),
        ),
    ):
        raise MigrationIntegrityError("migration ledger has an unrecognised shape")
    cur.execute(
        """
        SELECT index_class.relname, pk_constraint.conname, pk_constraint.contype,
               idx.indisprimary, idx.indisunique, idx.indisvalid, idx.indisready, idx.indimmediate,
               idx.indnkeyatts, idx.indnatts,
               ARRAY(
                 SELECT attribute.attname
                 FROM unnest(idx.indkey) WITH ORDINALITY AS key(attnum, ordinality)
                 JOIN pg_catalog.pg_attribute AS attribute
                   ON attribute.attrelid = idx.indrelid AND attribute.attnum = key.attnum
                   WHERE key.ordinality <= idx.indnkeyatts
                   ORDER BY key.ordinality
               ),
               pg_catalog.pg_get_indexdef(idx.indexrelid, 0, true),
               index_class.relowner = (
                   SELECT role.oid
                   FROM pg_catalog.pg_roles AS role
                   WHERE role.rolname = current_user
               ),
               index_class.relacl IS NULL,
               index_class.relpersistence,
               NOT index_class.relispartition
        FROM pg_catalog.pg_index AS idx
        JOIN pg_catalog.pg_class AS index_class ON index_class.oid = idx.indexrelid
        LEFT JOIN pg_catalog.pg_constraint AS pk_constraint
          ON pk_constraint.conindid = idx.indexrelid AND pk_constraint.conrelid = idx.indrelid
        WHERE idx.indrelid = %s::regclass
        ORDER BY idx.indexrelid
        """,
        (_LEDGER_TABLE,),
    )
    if tuple(cur.fetchall()) != (
        (
            "memplex_schema_migrations_pkey",
            "memplex_schema_migrations_pkey",
            "p",
            True,
            True,
            True,
            True,
            True,
            1,
            1,
            ["version"],
            "CREATE UNIQUE INDEX memplex_schema_migrations_pkey ON memplex_schema_migrations USING btree (version)",
            True,
            True,
            "p",
            True,
        ),
    ):
        raise MigrationIntegrityError("migration ledger has an unrecognised shape")
    cur.execute(
        "SELECT COUNT(*) FROM pg_catalog.pg_policy WHERE polrelid = %s::regclass",
        (_LEDGER_TABLE,),
    )
    if cur.fetchone() != (0,):
        raise MigrationIntegrityError("migration ledger has an unrecognised shape")
    cur.execute(
        """
        SELECT COUNT(*)
        FROM pg_catalog.pg_rewrite
        WHERE ev_class = %s::regclass
        """,
        (_LEDGER_TABLE,),
    )
    if cur.fetchone() != (0,):
        raise MigrationIntegrityError("migration ledger has an unrecognised shape")
    cur.execute(
        """
        SELECT COUNT(*)
        FROM pg_catalog.pg_trigger
        WHERE tgrelid = %s::regclass AND NOT tgisinternal
        """,
        (_LEDGER_TABLE,),
    )
    if cur.fetchone() != (0,):
        raise MigrationIntegrityError("migration ledger has an unrecognised shape")
    cur.execute(
        """
        SELECT version, name, checksum, execution_mode, baseline_fingerprint
        FROM memplex_schema_migrations
        ORDER BY version
        """
    )
    return tuple(_LedgerEntry(*row) for row in cur.fetchall())


def _validate_ledger(
    entries: tuple[_LedgerEntry, ...],
    migrations: tuple[Migration, ...],
    fingerprint: SchemaFingerprint,
) -> None:
    versions = tuple(entry.version for entry in entries)
    if versions != tuple(range(1, len(entries) + 1)) or len(entries) > len(migrations):
        raise MigrationIntegrityError("migration ledger is not continuous")
    for entry, migration in zip(entries, migrations):
        if (
            entry.name != migration.name
            or entry.checksum != migration.checksum
            or entry.execution_mode not in {"executed", "adopted"}
            or (entry.execution_mode == "executed" and entry.baseline_fingerprint is not None)
        ):
            raise MigrationIntegrityError("migration ledger integrity check failed")
    adopted = tuple(entry for entry in entries if entry.execution_mode == "adopted")
    if not adopted:
        return
    if (
        len(entries) < 2
        or tuple(entry.execution_mode for entry in entries[:2]) != ("adopted", "adopted")
        or any(entry.execution_mode != "executed" for entry in entries[2:])
        or any(entry.baseline_fingerprint is not None for entry in entries[2:])
    ):
        raise MigrationIntegrityError("migration ledger integrity check failed")
    baseline = entries[0].baseline_fingerprint
    if (
        baseline is None
        or entries[1].baseline_fingerprint != baseline
        or baseline not in {_variant_digest(variant) for variant in _allowed_adoption_baselines(fingerprint.variant)}
    ):
        raise MigrationIntegrityError("migration ledger integrity check failed")


def _plan_from_observed_state(
    entries: tuple[_LedgerEntry, ...], fingerprint: SchemaFingerprint, migrations: tuple[Migration, ...]
) -> MigrationPlan:
    if fingerprint.kind == "unknown":
        raise MigrationIntegrityError("unrecognised legacy schema")
    _validate_ledger(entries, migrations, fingerprint)
    current = len(entries)
    if entries:
        if current <= 1 and fingerprint.kind != "pre_g002_3_2_7":
            raise MigrationIntegrityError("migration ledger does not match the schema")
        if current >= 2 and fingerprint.kind != "post_g002_current":
            raise MigrationIntegrityError("migration ledger does not match the schema")
        if current >= 4 and not _is_edge_integrity_current_variant(fingerprint.variant):
            raise MigrationIntegrityError("migration ledger does not match the schema")
        if current >= 5 and not _is_reliable_sync_current_variant(fingerprint.variant):
            raise MigrationIntegrityError("migration ledger does not match the schema")
        if current >= 6 and not _is_background_tasks_current_variant(fingerprint.variant):
            raise MigrationIntegrityError("migration ledger does not match the schema")
        if 2 <= current < 4 and _is_edge_integrity_current_variant(fingerprint.variant):
            raise MigrationIntegrityError("migration ledger does not match the schema")
        if 4 <= current < 5 and _is_reliable_sync_current_variant(fingerprint.variant):
            raise MigrationIntegrityError("migration ledger does not match the schema")
        if 5 <= current < 6 and _is_background_tasks_current_variant(fingerprint.variant):
            raise MigrationIntegrityError("migration ledger does not match the schema")
    elif fingerprint.kind not in {"empty", "pre_g002_3_2_7", "post_g002_current"}:
        raise MigrationIntegrityError("unrecognised legacy schema")
    elif fingerprint.kind == "post_g002_current":
        if _is_edge_integrity_current_variant(fingerprint.variant):
            raise MigrationIntegrityError("unrecognised legacy schema")
        # The catalogue itself proves the first two migrations were already
        # applied.  Reporting 2 keeps a read-only plan honest without first
        # materialising the adopted ledger rows.
        current = 2
    pending = tuple(migration for migration in migrations if migration.version > current)
    return MigrationPlan(
        current_version=current,
        known_version=migrations[-1].version,
        pending=pending,
        state="ready" if not pending else "upgrade_required",
    )


def _validate_legacy_belongs_to_edges(cur: Any) -> None:
    """Run domain_node_id-v1 over every legacy virtual edge before 0004 DDL.

    The migration transaction already owns the fixed advisory lock.  We take
    ACCESS EXCLUSIVE locks before temporarily removing FORCE RLS, so a
    non-superuser table owner can inspect every historical JSONB row without a
    concurrently observable policy-bypass window.  The cleanup attempts to
    restore every disabled table: a cleanup failure cannot replace the primary
    validation error, while a successful validation surfaces one for rollback.
    """
    cur.execute(
        """
        SELECT relation.relname,
               relation.relowner = (
                   SELECT role.oid
                   FROM pg_catalog.pg_roles AS role
                   WHERE role.rolname = current_user
               ),
               relation.relforcerowsecurity
        FROM pg_catalog.pg_class AS relation
        WHERE relation.oid IN ('memplex_edges'::regclass, 'memplex_functions'::regclass)
        ORDER BY relation.relname
        """
    )
    if tuple(cur.fetchall()) != (
        ("memplex_edges", True, True),
        ("memplex_functions", True, True),
    ):
        raise MigrationIntegrityError("legacy BELONGS_TO validation requires current table owner FORCE RLS")

    cur.execute("LOCK TABLE memplex_functions, memplex_edges IN ACCESS EXCLUSIVE MODE")
    force_disabled: list[str] = []
    primary_error: BaseException | None = None
    try:
        for table_name in ("memplex_functions", "memplex_edges"):
            cur.execute(f"ALTER TABLE {table_name} NO FORCE ROW LEVEL SECURITY")
            force_disabled.append(table_name)
        cur.execute(
            """
            SELECT edge.source, edge.target, source_function.id, source_function.data
            FROM memplex_edges AS edge
            LEFT JOIN memplex_functions AS source_function
              ON source_function.tenant_id = edge.tenant_id
             AND source_function.id = edge.source
            WHERE edge.edge_type = 'BELONGS_TO'
            ORDER BY edge.tenant_id, edge.source, edge.target
            """
        )
        for source, target, source_id, data in cur.fetchall():
            domain = data.get("domain") if source_id is not None and type(data) is dict else None
            if type(domain) is not str or not domain or target != domain_node_id(domain):
                raise MigrationIntegrityError("invalid legacy BELONGS_TO edge endpoint")
    except BaseException as error:
        primary_error = error
        raise
    finally:
        cleanup_error: BaseException | None = None
        for table_name in reversed(force_disabled):
            try:
                cur.execute(f"ALTER TABLE {table_name} FORCE ROW LEVEL SECURITY")
            except BaseException as error:  # noqa: BLE001 - shutdown/cleanup semantics; primary error stays authoritative
                if cleanup_error is None:
                    cleanup_error = error
        if primary_error is None and cleanup_error is not None:
            raise cleanup_error
