"""Whole-catalogue snapshot reader, split from ``runner.py``.

``_catalog_snapshot`` reads every managed catalogue fact (relations, columns,
constraints, indexes, policies, triggers, extensions, sequences, capabilities,
sync functions) in one pass without issuing DDL or taking table locks. The
result feeds ``schema_fingerprint`` and the adoption-baseline checks. It is
re-exported from ``memplex.storage.migrations.runner`` (its only caller) for
import-path stability.
"""

from __future__ import annotations

from typing import Any

from memplex.storage.migrations.catalogue_checks import _normalise_sql
from memplex.storage.migrations.runner import (
    _CAPABILITIES_TABLE,
    _KNOWN_MEMPLEX_RELATION_KINDS,
    _MANAGED_TABLES,
    _SYNC_FUNCTIONS,
)


def _read_schema_and_relations(cur: Any) -> tuple[Any, Any, tuple]:
    """Read the current schema name and every managed/related relation row."""
    cur.execute("SELECT current_schema(), pg_catalog.quote_ident(current_schema())")
    schema_name, schema_path = cur.fetchone()
    cur.execute(
        """
        SELECT c.relname, c.relkind, c.relrowsecurity, c.relforcerowsecurity,
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
        JOIN pg_catalog.pg_namespace AS n ON n.oid = c.relnamespace
        WHERE n.nspname = current_schema()
          AND (c.relname = ANY(%s) OR c.relname LIKE 'memplex%%')
        ORDER BY c.relname
        """,
        (list(_MANAGED_TABLES),),
    )
    relations = tuple(cur.fetchall())
    return schema_name, schema_path, relations

def _snapshot_table(
    cur: Any,
    name: Any,
    rls: Any,
    force_rls: Any,
    owner_is_current_user: Any,
    acl_is_default: Any,
    persistence: Any,
    is_partition: Any,
    has_no_inheritance: Any,
) -> dict[str, Any]:
    """Read one managed table's full catalogue entry in one pass."""
    cur.execute(
        """
        SELECT a.attname, a.attnum,
               pg_catalog.format_type(a.atttypid, a.atttypmod),
               a.attnotnull,
               pg_catalog.pg_get_expr(ad.adbin, ad.adrelid),
               a.attgenerated,
               a.atttypid,
               a.atttypmod,
               default_dependency.refobjid,
               a.attacl IS NULL,
               a.attcollation,
               typ.typcollation
        FROM pg_catalog.pg_attribute AS a
        JOIN pg_catalog.pg_type AS typ ON typ.oid = a.atttypid
        LEFT JOIN pg_catalog.pg_attrdef AS ad
          ON ad.adrelid = a.attrelid AND ad.adnum = a.attnum
        LEFT JOIN LATERAL (
            SELECT dependency.refobjid
            FROM pg_catalog.pg_depend AS dependency
            JOIN pg_catalog.pg_class AS dependency_relation
              ON dependency_relation.oid = dependency.refobjid
             AND dependency_relation.relkind = 'S'
            WHERE dependency.classid = 'pg_attrdef'::pg_catalog.regclass
              AND dependency.objid = ad.oid
              AND dependency.refclassid = 'pg_class'::pg_catalog.regclass
              AND dependency.deptype IN ('n', 'a')
            ORDER BY dependency.refobjid
            LIMIT 1
        ) AS default_dependency ON true
        WHERE a.attrelid = %s::regclass
          AND a.attnum > 0 AND NOT a.attisdropped
        ORDER BY a.attnum
        """,
        (name,),
    )
    column_rows = tuple(cur.fetchall())
    cur.execute(
        """
        SELECT attname, attidentity
        FROM pg_catalog.pg_attribute
        WHERE attrelid=%s::regclass AND attnum>0 AND NOT attisdropped
        ORDER BY attnum
        """,
        (name,),
    )
    identities = {str(column): str(identity) for column, identity in cur.fetchall()}
    columns = tuple(
        (column, number, data_type, not_null, default, generated)
        for (
            column,
            number,
            data_type,
            not_null,
            default,
            generated,
            _type_oid,
            _type_mod,
            _dependency_oid,
            _acl_is_default,
            _attcollation,
            _typecollation,
        ) in column_rows
    )
    cur.execute(
        """
        SELECT a.attname
        FROM pg_catalog.pg_constraint AS con
        CROSS JOIN LATERAL unnest(con.conkey) WITH ORDINALITY AS key(attnum, ordinality)
        JOIN pg_catalog.pg_attribute AS a
          ON a.attrelid = con.conrelid AND a.attnum = key.attnum
        WHERE con.conrelid = %s::regclass AND con.contype = 'p'
        ORDER BY key.ordinality
        """,
        (name,),
    )
    primary_key = tuple(row[0] for row in cur.fetchall())
    cur.execute(
        """
        SELECT con.condeferrable,
               con.condeferred,
               con.convalidated,
               idx.indisprimary,
               idx.indisunique,
               idx.indisvalid,
               idx.indisready,
               idx.indimmediate,
               index_class.relowner = (
                   SELECT role.oid
                   FROM pg_catalog.pg_roles AS role
                   WHERE role.rolname = current_user
               ),
               index_class.relacl IS NULL,
               index_class.relpersistence = 'p',
               NOT index_class.relispartition
        FROM pg_catalog.pg_index AS idx
        JOIN pg_catalog.pg_class AS index_class ON index_class.oid = idx.indexrelid
        JOIN pg_catalog.pg_constraint AS con ON con.conindid = idx.indexrelid
        WHERE idx.indrelid = %s::regclass AND con.contype = 'p'
        ORDER BY con.oid
        """,
        (name,),
    )
    primary_index_catalog = tuple(
        tuple(bool(value) for value in row) for row in cur.fetchall()
    )
    cur.execute(
        """
        SELECT con.contype, con.conname, pg_catalog.pg_get_constraintdef(con.oid)
        FROM pg_catalog.pg_constraint AS con
        WHERE con.conrelid = %s::regclass
        ORDER BY con.conname
        """,
        (name,),
    )
    constraints = tuple((kind, constraint, definition) for kind, constraint, definition in cur.fetchall())
    cur.execute(
        """
        SELECT con.conname,
               pg_catalog.pg_get_expr(con.conbin, con.conrelid),
               con.convalidated,
               con.condeferrable,
               con.condeferred,
               con.connoinherit,
               con.conislocal,
               con.coninhcount
        FROM pg_catalog.pg_constraint AS con
        WHERE con.conrelid = %s::regclass AND con.contype = 'c'
        ORDER BY con.conname
        """,
        (name,),
    )
    checks = tuple(
        (
            str(constraint),
            str(expression),
            bool(validated),
            bool(deferrable),
            bool(deferred),
            bool(no_inherit),
            bool(is_local),
            int(inherit_count),
        )
        for (
            constraint,
            expression,
            validated,
            deferrable,
            deferred,
            no_inherit,
            is_local,
            inherit_count,
        ) in cur.fetchall()
    )
    cur.execute(
        """
        SELECT con.conname,
               ARRAY(
                   SELECT source_attribute.attname
                   FROM unnest(con.conkey) WITH ORDINALITY AS source_key(attnum, ordinality)
                   JOIN pg_catalog.pg_attribute AS source_attribute
                     ON source_attribute.attrelid = con.conrelid
                    AND source_attribute.attnum = source_key.attnum
                   ORDER BY source_key.ordinality
               ),
               target_namespace.nspname,
               target_relation.oid,
               target_relation.relname,
               ARRAY(
                   SELECT target_attribute.attname
                   FROM unnest(con.confkey) WITH ORDINALITY AS target_key(attnum, ordinality)
                   JOIN pg_catalog.pg_attribute AS target_attribute
                     ON target_attribute.attrelid = con.confrelid
                    AND target_attribute.attnum = target_key.attnum
                   ORDER BY target_key.ordinality
               ),
               con.confupdtype, con.confdeltype, con.confmatchtype,
               con.condeferrable, con.condeferred, con.convalidated,
               target_namespace.oid = (
                   SELECT current_namespace.oid
                   FROM pg_catalog.pg_namespace AS current_namespace
                   WHERE current_namespace.nspname = current_schema()
               )
        FROM pg_catalog.pg_constraint AS con
        JOIN pg_catalog.pg_class AS target_relation ON target_relation.oid = con.confrelid
        JOIN pg_catalog.pg_namespace AS target_namespace
          ON target_namespace.oid = target_relation.relnamespace
        WHERE con.conrelid = %s::regclass AND con.contype = 'f'
        ORDER BY con.conname
        """,
        (name,),
    )
    foreign_keys = tuple(
        (
            str(constraint),
            tuple(str(column) for column in source_columns),
            str(target_schema),
            int(target_relation_oid),
            str(target_relation),
            tuple(str(column) for column in target_columns),
            str(update_action),
            str(delete_action),
            str(match_type),
            bool(deferrable),
            bool(deferred),
            bool(validated),
            bool(target_is_current_schema),
        )
        for (
            constraint,
            source_columns,
            target_schema,
            target_relation_oid,
            target_relation,
            target_columns,
            update_action,
            delete_action,
            match_type,
            deferrable,
            deferred,
            validated,
            target_is_current_schema,
        ) in cur.fetchall()
    )
    cur.execute(
        """
        SELECT index_class.relname, idx.indisunique, access_method.amname,
               idx.indisvalid, idx.indnkeyatts, idx.indnatts,
               ARRAY(
                 SELECT pg_catalog.pg_get_indexdef(idx.indexrelid, ordinality, true)
                 FROM generate_series(1, idx.indnkeyatts) AS ordinality
               ),
               ARRAY(
                 SELECT pg_catalog.pg_get_indexdef(idx.indexrelid, ordinality, true)
                 FROM generate_series(idx.indnkeyatts + 1, idx.indnatts) AS ordinality
               ),
               pg_catalog.pg_get_indexdef(idx.indexrelid, 0, true),
               pg_catalog.pg_get_expr(idx.indpred, idx.indrelid),
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
        JOIN pg_catalog.pg_am AS access_method ON access_method.oid = index_class.relam
        WHERE idx.indrelid = %s::regclass AND NOT idx.indisprimary
        ORDER BY index_class.relname
        """,
        (name,),
    )
    indexes = tuple(
        (
            index_name,
            bool(unique),
            method,
            bool(valid),
            int(key_count),
            int(total_count),
            tuple(keys),
            tuple(included),
            definition,
            predicate,
            index_owner_is_current_user,
            index_acl_is_default,
            index_persistence,
            index_is_not_partition,
        )
        for (
            index_name,
            unique,
            method,
            valid,
            key_count,
            total_count,
            keys,
            included,
            definition,
            predicate,
            index_owner_is_current_user,
            index_acl_is_default,
            index_persistence,
            index_is_not_partition,
        ) in cur.fetchall()
    )
    cur.execute(
        """
        SELECT pol.polname, pol.polcmd, pol.polpermissive, pol.polroles,
               pg_catalog.pg_get_expr(pol.polqual, pol.polrelid),
               pg_catalog.pg_get_expr(pol.polwithcheck, pol.polrelid)
        FROM pg_catalog.pg_policy AS pol
        WHERE pol.polrelid = %s::regclass
        ORDER BY pol.polname
        """,
        (name,),
    )
    policies = tuple(
        (policy_name, command, bool(permissive), tuple(int(role) for role in roles), qualify, check)
        for policy_name, command, permissive, roles, qualify, check in cur.fetchall()
    )
    cur.execute(
        """
        SELECT rule.rulename, rule.ev_type, rule.is_instead,
               pg_catalog.pg_get_ruledef(rule.oid, true)
        FROM pg_catalog.pg_rewrite AS rule
        WHERE rule.ev_class = %s::regclass
        ORDER BY rule.rulename
        """,
        (name,),
    )
    rules = tuple(
        (str(rule_name), str(event_type), bool(is_instead), str(definition))
        for rule_name, event_type, is_instead, definition in cur.fetchall()
    )
    cur.execute(
        """
        SELECT trigger.tgname, trigger.tgtype, trigger.tgenabled, trigger.tgfoid,
               pg_catalog.encode(trigger.tgargs, 'hex')
        FROM pg_catalog.pg_trigger AS trigger
        WHERE trigger.tgrelid = %s::regclass AND NOT trigger.tgisinternal
        ORDER BY trigger.tgname
        """,
        (name,),
    )
    triggers = tuple(
        (str(trigger_name), int(trigger_type), str(enabled), int(function_oid), str(arguments))
        for trigger_name, trigger_type, enabled, function_oid, arguments in cur.fetchall()
    )
    return {
        "rls": bool(rls),
        "force_rls": bool(force_rls),
        "owner_is_current_user": bool(owner_is_current_user),
        "acl_is_default": bool(acl_is_default),
        "persistence": str(persistence),
        "is_partition": bool(is_partition),
        "has_no_inheritance": bool(has_no_inheritance),
        "columns": columns,
        "primary_key": primary_key,
        "primary_index_catalog": primary_index_catalog,
        "constraints": constraints,
        "checks": checks,
        "foreign_keys": foreign_keys,
        "indexes": indexes,
        "policies": policies,
        "rules": rules,
        "triggers": triggers,
        "type_oids": {
            str(column): int(type_oid)
            for (
                column,
                _number,
                _data_type,
                _not_null,
                _default,
                _generated,
                type_oid,
                _type_mod,
                _dependency_oid,
                _acl_is_default,
                _attcollation,
                _typecollation,
            ) in column_rows
        },
        "type_mods": {
            str(column): int(type_mod)
            for (
                column,
                _number,
                _data_type,
                _not_null,
                _default,
                _generated,
                _type_oid,
                type_mod,
                _dependency_oid,
                _acl_is_default,
                _attcollation,
                _typecollation,
            ) in column_rows
        },
        "default_dependencies": {
            str(column): int(dependency_oid)
            for (
                column,
                _number,
                _data_type,
                _not_null,
                _default,
                _generated,
                _type_oid,
                _type_mod,
                dependency_oid,
                _acl_is_default,
                _attcollation,
                _typecollation,
            ) in column_rows
            if dependency_oid is not None
        },
        "column_acls_are_default": {
            str(column): bool(acl_is_default)
            for (
                column,
                _number,
                _data_type,
                _not_null,
                _default,
                _generated,
                _type_oid,
                _type_mod,
                _dependency_oid,
                acl_is_default,
                _attcollation,
                _typecollation,
            ) in column_rows
        },
        "column_collations_are_type_default": {
            str(column): int(attcollation) == int(typecollation)
            for (
                column,
                _number,
                _data_type,
                _not_null,
                _default,
                _generated,
                _type_oid,
                _type_mod,
                _dependency_oid,
                _acl_is_default,
                attcollation,
                typecollation,
            ) in column_rows
        },
        "identities": identities,
    }

def _snapshot_tables(cur: Any, relations: tuple) -> tuple[dict[str, Any], list[str]]:
    """Classify relation rows and snapshot every managed table."""
    tables: dict[str, Any] = {}
    unexpected: list[str] = []
    for (
        name,
        relkind,
        rls,
        force_rls,
        owner_is_current_user,
        acl_is_default,
        persistence,
        is_partition,
        has_no_inheritance,
    ) in relations:
        expected_kind = _KNOWN_MEMPLEX_RELATION_KINDS.get(str(name))
        if str(name).startswith("memplex"):
            if expected_kind != relkind:
                unexpected.append(str(name))
        elif name in _MANAGED_TABLES and relkind != "r":
            unexpected.append(str(name))
        if name not in _MANAGED_TABLES or relkind != "r":
            continue
        tables[name] = _snapshot_table(
            cur, name, rls, force_rls, owner_is_current_user,
            acl_is_default, persistence, is_partition, has_no_inheritance,
        )
    return tables, unexpected

def _read_capabilities(cur: Any, tables: dict[str, Any]) -> tuple[tuple[str, str], ...] | None:
    """Read applied capability digests when the capabilities table exists."""
    capabilities: tuple[tuple[str, str], ...] | None = None
    capability_table = tables.get(_CAPABILITIES_TABLE)
    if capability_table is not None and tuple(column[0] for column in capability_table["columns"]) == (
        "capability_name",
        "parameter_digest",
        "applied_at",
    ):
        cur.execute(
            """
            SELECT capability_name, parameter_digest
            FROM memplex_schema_capabilities
            ORDER BY capability_name
            """
        )
        capabilities = tuple((str(name), str(digest)) for name, digest in cur.fetchall())
    return capabilities

def _read_extensions(cur: Any) -> tuple[tuple, Any]:
    """Read installed extensions and the pgvector extension row if present."""
    cur.execute("SELECT extname FROM pg_catalog.pg_extension ORDER BY extname")
    extensions = tuple(row[0] for row in cur.fetchall())
    cur.execute(
        """
        SELECT namespace.nspname, typ.oid
        FROM pg_catalog.pg_extension AS extension
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = extension.extnamespace
        JOIN pg_catalog.pg_type AS typ
          ON typ.typnamespace = extension.extnamespace AND typ.typname = 'vector'
        WHERE extension.extname = 'vector'
        """
    )
    vector_extension = cur.fetchone()
    return extensions, vector_extension

def _read_changelog_sequence(cur: Any) -> tuple[Any, tuple]:
    """Read the changelog BIGSERIAL sequence identity and its dependencies."""
    cur.execute(
        """
        SELECT relation.oid,
               relation.relkind,
               relation.relowner = (
                   SELECT role.oid
                   FROM pg_catalog.pg_roles AS role
                   WHERE role.rolname = current_user
               ),
               relation.relacl IS NULL,
               relation.relpersistence,
               relation.relispartition,
               pg_catalog.format_type(sequence.seqtypid, NULL),
               sequence.seqstart,
               sequence.seqincrement,
               sequence.seqmin,
               sequence.seqmax,
               sequence.seqcache,
               sequence.seqcycle
        FROM pg_catalog.pg_class AS relation
        JOIN pg_catalog.pg_namespace AS namespace ON namespace.oid = relation.relnamespace
        JOIN pg_catalog.pg_sequence AS sequence ON sequence.seqrelid = relation.oid
        WHERE namespace.nspname = current_schema()
          AND relation.relname = 'memplex_changelog_id_seq'
          AND relation.relkind = 'S'
        """
    )
    sequence = cur.fetchone()
    sequence_dependencies: tuple[tuple[str, bool, bool], ...] = ()
    if sequence is not None:
        cur.execute(
            """
            SELECT dependency.deptype,
                   dependency.refobjid = 'memplex_changelog'::regclass,
                   dependency.refobjsubid = changelog_column.attnum
            FROM pg_catalog.pg_depend AS dependency
            JOIN pg_catalog.pg_attribute AS changelog_column
              ON changelog_column.attrelid = 'memplex_changelog'::regclass
             AND changelog_column.attname = 'id'
             AND NOT changelog_column.attisdropped
            WHERE dependency.classid = 'pg_class'::regclass
              AND dependency.objid = %s
              AND dependency.refclassid = 'pg_class'::regclass
            ORDER BY dependency.deptype, dependency.refobjid, dependency.refobjsubid
            """,
            (sequence[0],),
        )
        sequence_dependencies = tuple(
            (str(dependency_type), bool(is_changelog), bool(is_id))
            for dependency_type, is_changelog, is_id in cur.fetchall()
        )
    return sequence, sequence_dependencies

def _read_sync_functions(cur: Any) -> tuple:
    """Read every managed sync function definition and its ACL posture."""
    cur.execute(
        """
        SELECT procedure.proname, procedure.oid,
               procedure.prosecdef,
               procedure.proowner = (
                   SELECT role.oid FROM pg_catalog.pg_roles role WHERE role.rolname = current_user
               ),
               procedure.proconfig,
               NOT EXISTS (
                   SELECT 1
                   FROM pg_catalog.aclexplode(
                       COALESCE(procedure.proacl, pg_catalog.acldefault('f', procedure.proowner))
                   ) privilege
                   WHERE privilege.grantee = 0 AND privilege.privilege_type = 'EXECUTE'
               ),
               pg_catalog.pg_get_functiondef(procedure.oid)
        FROM pg_catalog.pg_proc procedure
        JOIN pg_catalog.pg_namespace namespace ON namespace.oid = procedure.pronamespace
        WHERE namespace.nspname = current_schema()
          AND procedure.proname = ANY(%s)
        ORDER BY procedure.proname
        """,
        (list(_SYNC_FUNCTIONS),),
    )
    sync_functions = tuple(
        (str(name), int(oid), bool(security_definer), bool(owner_is_current), tuple(config or ()), bool(public_denied), str(definition))
        for name, oid, security_definer, owner_is_current, config, public_denied, definition in cur.fetchall()
    )
    return sync_functions

def _catalog_snapshot(cur: Any) -> dict[str, Any]:
    """Read all managed catalogue facts without issuing DDL or table locks."""
    schema_name, schema_path, relations = _read_schema_and_relations(cur)
    tables, unexpected = _snapshot_tables(cur, relations)
    capabilities = _read_capabilities(cur, tables)
    extensions, vector_extension = _read_extensions(cur)
    sequence, sequence_dependencies = _read_changelog_sequence(cur)
    sync_functions = _read_sync_functions(cur)
    return {
        "schema": str(schema_name),
        "schema_path": str(schema_path),
        "tables": tables,
        "relations": relations,
        "unexpected": tuple(unexpected),
        "extensions": extensions,
        "vector_extension": None
        if vector_extension is None
        else (str(vector_extension[0]), int(vector_extension[1])),
        "changelog_sequence_oid": None if sequence is None else int(sequence[0]),
        "changelog_sequence": None
        if sequence is None
        else {
            "kind": str(sequence[1]),
            "owner_is_current_user": bool(sequence[2]),
            "acl_is_default": bool(sequence[3]),
            "persistence": str(sequence[4]),
            "is_partition": bool(sequence[5]),
            "type": str(sequence[6]),
            "start": int(sequence[7]),
            "increment": int(sequence[8]),
            "minimum": int(sequence[9]),
            "maximum": int(sequence[10]),
            "cache": int(sequence[11]),
            "cycle": bool(sequence[12]),
            "dependencies": sequence_dependencies,
        },
        "capabilities": capabilities,
        "sync_functions": sync_functions,
    }

