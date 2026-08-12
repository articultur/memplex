"""PostgreSQL ACL contract verification, split from ``runner.py``.

``_verify_application_acl`` / ``_verify_ingress_acl`` / ``_verify_acl_contracts``
check that the independently deployed service roles grant exactly the least
privilege the application and ingress paths need. They operate purely on a
cursor and the ACL-contract data classes, and are re-exported from
``memplex.storage.migrations.runner`` for import-path stability.
"""

from __future__ import annotations

from typing import Any

from memplex.storage.migrations.runner import (
    _APPLICATION_ACL,
    _APPLICATION_ACL_FUNCTIONS,
    _APPLICATION_ACL_TABLES,
    _CAPABILITIES_TABLE,
    _LEDGER_TABLE,
    _MANAGED_TABLES,
    _SYNC_FUNCTIONS,
    ApplicationAclContract,
    IngressAclContract,
    MigrationIntegrityError,
)


def _verify_application_acl(
    cur: Any,
    contract: ApplicationAclContract,
    profile: str,
    *,
    ingress_role: str | None = None,
) -> bool:
    """Accept only the one documented least-privilege business ACL.

    This is intentionally separate from the immutable schema fingerprint: a
    migration run without an application principal still treats any ACL as a
    drift signal.  Readiness supplies one exact role and therefore may allow
    only its direct grants, never PUBLIC, column grants, grant options or
    access to runner-owned catalogues.
    """
    if type(contract) is not ApplicationAclContract:
        raise TypeError("application ACL contract must be exact ApplicationAclContract")
    application_role = contract.role
    tables = _APPLICATION_ACL
    if tuple(tables) != _APPLICATION_ACL_TABLES:
        raise AssertionError("application ACL matrix must track application tables")
    cur.execute(
        "SELECT oid, rolsuper, rolbypassrls FROM pg_catalog.pg_roles WHERE rolname=%s",
        (application_role,),
    )
    role = cur.fetchone()
    if role is None:
        raise MigrationIntegrityError("application role ACL is unsafe")
    if bool(role[1]) or bool(role[2]):
        if profile == "development":
            return False
        raise MigrationIntegrityError("application role ACL is unsafe")
    role_oid = int(role[0])
    if profile == "production":
        cur.execute(
            """
            WITH RECURSIVE reachable(oid) AS (
                SELECT %s::oid
                UNION
                SELECT membership.roleid
                FROM pg_catalog.pg_auth_members membership
                JOIN reachable ON membership.member=reachable.oid
            )
            SELECT role.rolname, role.rolsuper, role.rolbypassrls,
                   EXISTS (
                       SELECT 1 FROM pg_catalog.pg_namespace namespace
                       WHERE namespace.nspname=current_schema() AND namespace.nspowner=role.oid
                   ) OR EXISTS (
                       SELECT 1 FROM pg_catalog.pg_class relation
                       JOIN pg_catalog.pg_namespace namespace ON namespace.oid=relation.relnamespace
                       WHERE namespace.nspname=current_schema()
                         AND relation.relname=ANY(%s) AND relation.relowner=role.oid
                   )
            FROM reachable JOIN pg_catalog.pg_roles role ON role.oid=reachable.oid
            """,
            (role_oid, list(_MANAGED_TABLES)),
        )
        for role_name, is_super, bypass_rls, owns_managed in cur.fetchall():
            if (
                bool(is_super)
                or bool(bypass_rls)
                or bool(owns_managed)
                or str(role_name) in {"pg_read_all_data", "pg_write_all_data"}
            ):
                raise MigrationIntegrityError("application role can SET ROLE to unsafe privilege")
    cur.execute(
        """
        SELECT n.nspowner = %s, n.nspowner, a.grantee, grantee_role.rolname,
               a.privilege_type, a.is_grantable
        FROM pg_catalog.pg_namespace n
        LEFT JOIN LATERAL pg_catalog.aclexplode(n.nspacl) a ON true
        LEFT JOIN pg_catalog.pg_roles grantee_role ON grantee_role.oid=a.grantee
        WHERE n.nspname=current_schema()
        """,
        (role_oid,),
    )
    schema_rows = tuple(cur.fetchall())
    if not schema_rows or bool(schema_rows[0][0]):
        raise MigrationIntegrityError("application role schema ACL is not least privilege")
    app_usage = False
    for _is_owner, schema_owner, grantee, grantee_name, privilege, grantable in schema_rows:
        if grantee is None:
            continue
        if int(grantee) == int(schema_owner):
            continue
        if bool(grantable) or str(privilege) == "CREATE":
            raise MigrationIntegrityError("application role schema ACL is not least privilege")
        if int(grantee) == role_oid and str(privilege) == "USAGE":
            app_usage = True
            continue
        if ingress_role is not None and grantee_name == ingress_role and str(privilege) == "USAGE":
            continue
        raise MigrationIntegrityError("application role schema ACL is not least privilege")
    if not app_usage:
        raise MigrationIntegrityError("application role schema ACL is not least privilege")
    cur.execute(
        """
        SELECT c.relname, c.relowner = %s, a.attname
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        LEFT JOIN pg_catalog.pg_attribute a ON a.attrelid=c.oid AND a.attnum>0
          AND NOT a.attisdropped AND a.attacl IS NOT NULL
        WHERE n.nspname=current_schema() AND c.relname = ANY(%s)
          AND (c.relowner=%s OR a.attname IS NOT NULL)
        """,
        (role_oid, list((*tables, _LEDGER_TABLE, _CAPABILITIES_TABLE)), role_oid),
    )
    if cur.fetchall():
        raise MigrationIntegrityError("application role has unsafe managed ownership or column ACL")
    cur.execute(
        """
        SELECT c.relname, c.relowner, a.grantee, a.privilege_type, a.is_grantable
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        CROSS JOIN LATERAL pg_catalog.aclexplode(c.relacl) a
        WHERE n.nspname=current_schema()
          AND c.relname = ANY(%s)
        ORDER BY c.relname, a.grantee, a.privilege_type
        """,
        (list((*tables, _LEDGER_TABLE, _CAPABILITIES_TABLE)),),
    )
    observed: dict[str, set[str]] = {name: set() for name in tables}
    for table, owner, grantee, privilege, grantable in cur.fetchall():
        name = str(table)
        if name not in tables or bool(grantable):
            raise MigrationIntegrityError("application role has unexpected managed ACL")
        if int(grantee) == int(owner):
            continue
        if int(grantee) != role_oid:
            raise MigrationIntegrityError("application role has unexpected managed ACL")
        observed[name].add(str(privilege))
    if observed != tables:
        raise MigrationIntegrityError("application role table ACL is not least privilege")
    for sequence_name in ("memplex_changelog_id_seq", "memplex_sync_outbox_stream_seq_seq"):
        cur.execute(
            """
            SELECT c.relowner, a.grantee, a.privilege_type, a.is_grantable
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(c.relacl) a
            WHERE n.nspname=current_schema() AND c.relkind='S' AND c.relname=%s
            """,
            (sequence_name,),
        )
        application_sequence_acl = tuple(
            (grantee, privilege, grantable)
            for owner, grantee, privilege, grantable in cur.fetchall()
            if int(grantee) != int(owner)
        )
        if application_sequence_acl != ((role_oid, "USAGE", False),):
            raise MigrationIntegrityError("application role sequence ACL is not least privilege")
    cur.execute(
        """
        SELECT procedure.proname, procedure.proowner, privilege.grantee,
               privilege.privilege_type, privilege.is_grantable
        FROM pg_catalog.pg_proc procedure
        JOIN pg_catalog.pg_namespace namespace ON namespace.oid=procedure.pronamespace
        CROSS JOIN LATERAL pg_catalog.aclexplode(procedure.proacl) privilege
        WHERE namespace.nspname=current_schema() AND procedure.proname = ANY(%s)
        ORDER BY procedure.proname, privilege.grantee, privilege.privilege_type
        """,
        (list(_SYNC_FUNCTIONS),),
    )
    observed_functions: dict[str, set[str]] = {name: set() for name in _SYNC_FUNCTIONS}
    for name, owner, grantee, privilege, grantable in cur.fetchall():
        if int(grantee) == int(owner):
            continue
        expected_app = str(name) in _APPLICATION_ACL_FUNCTIONS and int(grantee) == role_oid
        expected_ingress = (
            ingress_role is not None
            and str(name) == "memplex_sync_apply_inbound"
            and str(grantee) != "0"
        )
        if bool(grantable) or int(grantee) == 0 or str(privilege) != "EXECUTE":
            raise MigrationIntegrityError("application role function ACL is not least privilege")
        if expected_app:
            observed_functions[str(name)].add(str(privilege))
            continue
        if expected_ingress:
            cur.execute("SELECT oid FROM pg_catalog.pg_roles WHERE rolname=%s", (ingress_role,))
            ingress_row = cur.fetchone()
            if ingress_row is not None and int(grantee) == int(ingress_row[0]):
                continue
        raise MigrationIntegrityError("application role function ACL is not least privilege")
    expected_functions = {
        name: ({"EXECUTE"} if name in _APPLICATION_ACL_FUNCTIONS else set())
        for name in _SYNC_FUNCTIONS
    }
    if observed_functions != expected_functions:
        raise MigrationIntegrityError("application role function ACL is not least privilege")
    return True

def _verify_ingress_acl(
    cur: Any,
    contract: IngressAclContract,
    profile: str,
    *,
    application_role: str | None = None,
) -> bool:
    """Verify the inbound LOGIN has no ambient database write capability.

    The ingress service is intentionally not an application variant: it may
    execute one owner-owned function and nothing else in this schema.
    """
    if type(contract) is not IngressAclContract:
        raise TypeError("ingress ACL contract must be exact IngressAclContract")
    cur.execute(
        "SELECT oid, rolcanlogin, rolsuper, rolbypassrls FROM pg_catalog.pg_roles WHERE rolname=%s",
        (contract.role,),
    )
    row = cur.fetchone()
    if row is None or not bool(row[1]) or bool(row[2]) or bool(row[3]):
        raise MigrationIntegrityError("ingress role ACL is unsafe")
    cur.execute(
        """
        SELECT EXISTS (
            SELECT 1 FROM memplex_sync_ingress_principals
            WHERE role_name=%s::name AND enabled
        )
        """,
        (contract.role,),
    )
    if cur.fetchone() != (True,):
        raise MigrationIntegrityError("ingress role is not owner-bound")
    ingress_oid = int(row[0])
    application_oid: int | None = None
    if application_role is not None:
        cur.execute("SELECT oid FROM pg_catalog.pg_roles WHERE rolname=%s", (application_role,))
        application_row = cur.fetchone()
        if application_row is None:
            raise MigrationIntegrityError("ingress role ACL is unsafe")
        application_oid = int(application_row[0])
    if profile == "production":
        cur.execute(
            """
            WITH RECURSIVE reachable(oid) AS (
                SELECT %s::oid
                UNION
                SELECT membership.roleid
                FROM pg_catalog.pg_auth_members membership
                JOIN reachable ON membership.member=reachable.oid
            )
            SELECT role.rolname, role.rolsuper, role.rolbypassrls,
                   EXISTS (
                       SELECT 1 FROM pg_catalog.pg_namespace namespace
                       WHERE namespace.nspname=current_schema() AND namespace.nspowner=role.oid
                   ) OR EXISTS (
                       SELECT 1 FROM pg_catalog.pg_class relation
                       JOIN pg_catalog.pg_namespace namespace ON namespace.oid=relation.relnamespace
                       WHERE namespace.nspname=current_schema()
                         AND relation.relname=ANY(%s) AND relation.relowner=role.oid
                   )
            FROM reachable JOIN pg_catalog.pg_roles role ON role.oid=reachable.oid
            """,
            (ingress_oid, list(_MANAGED_TABLES)),
        )
        for role_name, is_super, bypass_rls, owns_managed in cur.fetchall():
            if (
                bool(is_super)
                or bool(bypass_rls)
                or bool(owns_managed)
                or str(role_name) in {"pg_read_all_data", "pg_write_all_data"}
            ):
                raise MigrationIntegrityError("ingress role can SET ROLE to unsafe privilege")
    cur.execute(
        """
        SELECT n.nspowner, a.grantee, grantee_role.rolname, a.privilege_type, a.is_grantable
        FROM pg_catalog.pg_namespace n
        LEFT JOIN LATERAL pg_catalog.aclexplode(n.nspacl) a ON true
        LEFT JOIN pg_catalog.pg_roles grantee_role ON grantee_role.oid=a.grantee
        WHERE n.nspname=current_schema()
        """
    )
    ingress_usage = False
    for owner, grantee, grantee_name, privilege, grantable in cur.fetchall():
        if grantee is None or int(grantee) == int(owner):
            continue
        if bool(grantable) or str(privilege) != "USAGE":
            raise MigrationIntegrityError("ingress role schema ACL is unsafe")
        if int(grantee) == ingress_oid:
            ingress_usage = True
            continue
        if application_role is not None and grantee_name == application_role:
            continue
        raise MigrationIntegrityError("ingress role schema ACL is unsafe")
    if not ingress_usage:
        raise MigrationIntegrityError("ingress role schema ACL is unsafe")
    for table in _MANAGED_TABLES:
        cur.execute(
            "SELECT has_table_privilege(%s::name, %s::regclass, 'SELECT,INSERT,UPDATE,DELETE,TRUNCATE,REFERENCES,TRIGGER')",
            (contract.role, table),
        )
        # The scoped runner's current schema is safer than composing an
        # untrusted name; use to_regclass below when parameter_status is not
        # populated by psycopg2.
        value = cur.fetchone()
        if value not in ((False,), None):
            raise MigrationIntegrityError("ingress role has relation ACL")
    for sequence_name in ("memplex_changelog_id_seq", "memplex_sync_outbox_stream_seq_seq"):
        cur.execute(
            "SELECT has_sequence_privilege(%s::name, %s::regclass, 'USAGE,SELECT,UPDATE')",
            (contract.role, sequence_name),
        )
        if cur.fetchone() != (False,):
            raise MigrationIntegrityError("ingress role has sequence ACL")
    cur.execute(
        """
        SELECT c.relname, c.relowner, a.grantee, a.privilege_type, a.is_grantable
        FROM pg_catalog.pg_class c
        JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
        CROSS JOIN LATERAL pg_catalog.aclexplode(c.relacl) a
        WHERE n.nspname=current_schema() AND c.relname = ANY(%s)
        """,
        (list(_MANAGED_TABLES),),
    )
    observed_relation_acl: dict[str, set[tuple[int, str]]] = {
        name: set() for name in _MANAGED_TABLES
    }
    for name, owner, grantee, privilege, grantable in cur.fetchall():
        if int(grantee) == int(owner):
            continue
        if bool(grantable):
            raise MigrationIntegrityError("ingress role relation ACL is unsafe")
        observed_relation_acl[str(name)].add((int(grantee), str(privilege)))
    expected_relation_acl: dict[str, set[tuple[int, str]]] = {
        name: set() for name in _MANAGED_TABLES
    }
    if application_oid is not None:
        for name, privileges in _APPLICATION_ACL.items():
            expected_relation_acl[name] = {(application_oid, privilege) for privilege in privileges}
    if observed_relation_acl != expected_relation_acl:
        raise MigrationIntegrityError("ingress role relation ACL is unsafe")
    for sequence_name in ("memplex_changelog_id_seq", "memplex_sync_outbox_stream_seq_seq"):
        cur.execute(
            """
            SELECT c.relowner, a.grantee, a.privilege_type, a.is_grantable
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace
            CROSS JOIN LATERAL pg_catalog.aclexplode(c.relacl) a
            WHERE n.nspname=current_schema() AND c.relkind='S' AND c.relname=%s
            """,
            (sequence_name,),
        )
        sequence_rows = cur.fetchall()
        if any(bool(grantable) for owner, grantee, privilege, grantable in sequence_rows):
            raise MigrationIntegrityError("ingress role sequence ACL is unsafe")
        observed_sequence_acl = {
            (int(grantee), str(privilege))
            for owner, grantee, privilege, grantable in sequence_rows
            if int(grantee) != int(owner) and not bool(grantable)
        }
        expected_sequence_acl = (
            {(application_oid, "USAGE")} if application_oid is not None else set()
        )
        if observed_sequence_acl != expected_sequence_acl:
            raise MigrationIntegrityError("ingress role sequence ACL is unsafe")
    cur.execute(
        """
        SELECT procedure.proname, procedure.proowner, privilege.grantee,
               privilege.privilege_type, privilege.is_grantable
        FROM pg_catalog.pg_proc procedure
        JOIN pg_catalog.pg_namespace namespace ON namespace.oid=procedure.pronamespace
        CROSS JOIN LATERAL pg_catalog.aclexplode(procedure.proacl) privilege
        WHERE namespace.nspname=current_schema() AND procedure.proname = ANY(%s)
        ORDER BY procedure.proname, privilege.grantee, privilege.privilege_type
        """,
        (list(_SYNC_FUNCTIONS),),
    )
    observed_functions: dict[str, set[tuple[int, str]]] = {
        name: set() for name in _SYNC_FUNCTIONS
    }
    for name, owner, grantee, privilege, grantable in cur.fetchall():
        if int(grantee) == int(owner):
            continue
        if bool(grantable) or int(grantee) == 0 or str(privilege) != "EXECUTE":
            raise MigrationIntegrityError("ingress role function ACL is not least privilege")
        observed_functions[str(name)].add((int(grantee), str(privilege)))
    expected_functions: dict[str, set[tuple[int, str]]] = {
        name: set() for name in _SYNC_FUNCTIONS
    }
    expected_functions["memplex_sync_apply_inbound"].add((ingress_oid, "EXECUTE"))
    if application_oid is not None:
        for name in _APPLICATION_ACL_FUNCTIONS:
            expected_functions[name].add((application_oid, "EXECUTE"))
    if observed_functions != expected_functions:
        raise MigrationIntegrityError("ingress role function ACL is not least privilege")
    for function in _SYNC_FUNCTIONS:
        signature = "bytea,text" if function == "memplex_sync_apply_inbound" else (
            "name,text" if function == "memplex_configure_sync_ingress_principal" else
            "text,text" if function == "memplex_sync_require_canonical_entity_key" else
            "text,text,text" if function == "memplex_sync_require_canonical_version" else
            "jsonb" if function == "memplex_sync_encode_string_array" else
            "text" if function == "memplex_configure_sync_local_identity" else
            "text,bigint" if function == "memplex_sync_assert_delivery_quota" else
            "timestamptz,timestamptz,integer" if function == "memplex_sync_compact" else ""
        )
        cur.execute(
            "SELECT has_function_privilege(%s::name, %s::regprocedure, 'EXECUTE')",
            (contract.role, f'{function}({signature})'),
        )
        if cur.fetchone() != ((function == "memplex_sync_apply_inbound"),):
            raise MigrationIntegrityError("ingress role function ACL is not least privilege")
    return True


def _verify_acl_contracts(
    cur: Any,
    *,
    application_acl: ApplicationAclContract | None,
    ingress_acl: IngressAclContract | None,
    profile: str | None,
) -> bool:
    """Verify the one exact union of independently deployed service roles."""
    if application_acl is not None and type(application_acl) is not ApplicationAclContract:
        raise TypeError("application ACL contract must be exact ApplicationAclContract")
    if ingress_acl is not None and type(ingress_acl) is not IngressAclContract:
        raise TypeError("ingress ACL contract must be exact IngressAclContract")
    if application_acl is None and ingress_acl is None:
        return False
    if profile not in {"development", "production"}:
        raise ValueError("ACL verification requires development or production profile")
    if application_acl is not None:
        _verify_application_acl(
            cur,
            application_acl,
            profile,
            ingress_role=ingress_acl.role if ingress_acl is not None else None,
        )
    if ingress_acl is not None:
        _verify_ingress_acl(
            cur,
            ingress_acl,
            profile,
            application_role=application_acl.role if application_acl is not None else None,
        )
    return True
