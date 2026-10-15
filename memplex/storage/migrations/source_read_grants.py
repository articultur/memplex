"""V10 SELECT-only grant predicates and locked, owner-run projection upgrade.

The head is the single relational grant authority. Its policy never reads a
payload or derivation record, so the links below are acyclic. These predicates
prove row visibility, not the recursive validity checked by the service fence.
"""
from __future__ import annotations

from typing import Any

_PAYLOAD_KINDS = {
    "memplex_paragraphs": "raw_paragraph", "memplex_functions": "function",
    "memplex_facts": "fact", "memplex_preferences": "preference",
    "memplex_observations": "observation",
}
_MAINTENANCE_TABLES = tuple(sorted((*_PAYLOAD_KINDS, "memplex_derivation_records",
                                  "memplex_source_authorities", "memplex_source_heads")))
_IDENTITY = ("tenant_id", "owner_subject", "workspace", "visibility", "source_agent", "source_session")


def _setting(name: str) -> str:
    return f"current_setting('memplex.{name}'::text, true)"


def _head_grant_sql(alias: str) -> str:
    p = alias + "." if alias else ""
    caller = f"COALESCE(NULLIF({_setting('agent_id')}, ''::text), {_setting('subject_id')})"
    return (
        f"({p}tenant_id <> '__memplex_legacy__'::text AND {p}tenant_id = {_setting('tenant_id')} "
        f"AND {p}workspace = {_setting('workspace_id')} AND {p}visibility = 'user'::text "
        f"AND {p}state = 'live'::text AND {p}source_class <> 'legacy_unverified'::text "
        f"AND {caller} <> ''::text "
        f"AND {caller} = ANY ({p}read_grants) "
        "AND EXISTS (SELECT 1 FROM memplex_source_authorities grant_authority WHERE ("
        f"grant_authority.authority_id = {p}authority_id "
        f"AND grant_authority.tenant_id = {p}tenant_id AND grant_authority.workspace = {p}workspace "
        "AND grant_authority.state = 'live'::text AND grant_authority.contract_version = 1)))"
    )


def _identity_link(left: str, right: str) -> str:
    return " AND ".join(f"{left}.{name} = {right}.{name}" for name in _IDENTITY)


def _payload_grant_sql(table: str, alias: str = "") -> str:
    kind = _PAYLOAD_KINDS[table]
    row = alias or table
    # CASE protects old unregistered/malformed locator fixtures from a JSON
    # cast: only a current explicitly granted head can enter this branch.
    locator = "(grant_head.payload_locator)::jsonb"
    return (
        "EXISTS (SELECT 1 FROM memplex_source_heads grant_head WHERE ("
        f"grant_head.kind = '{kind}'::text AND grant_head.logical_id = {row}.id "
        f"AND {_identity_link('grant_head', row)} AND CASE WHEN {_head_grant_sql('grant_head')} THEN ("
        f"({locator} ->> 'id'::text) = {row}.id "
        f"AND ({locator} ->> 'kind'::text) = grant_head.kind "
        f"AND ({locator} ->> 'authority_id'::text) = (grant_head.authority_id)::text "
        f"AND ({locator} ->> 'tenant_id'::text) = grant_head.tenant_id "
        f"AND ({locator} ->> 'workspace_id'::text) = grant_head.workspace) ELSE false END))"
    )


def _record_grant_sql(alias: str = "memplex_derivation_records") -> str:
    r = alias
    return (
        "EXISTS (SELECT 1 FROM memplex_source_heads grant_head WHERE ("
        f"grant_head.authority_id = {r}.authority_id AND grant_head.kind = 'fact'::text "
        "AND grant_head.source_class = 'derived_v1'::text "
        f"AND {_identity_link('grant_head', r)} AND {_head_grant_sql('grant_head')} "
        f"AND (({r}.record_kind = 'publication'::text AND grant_head.logical_id = {r}.record_key "
        f"AND ({r}.data ->> 'publication_id'::text) = {r}.record_key) "
        f"OR ({r}.record_kind = 'target'::text "
        f"AND grant_head.logical_id = ({r}.data #>> '{{token,current_publication}}'::text[]) "
        f"AND ({r}.data ->> 'derivation_key'::text) = {r}.record_key))))"
    )


def _grant_policy_expressions() -> dict[str, str]:
    return {"memplex_source_heads": _head_grant_sql("memplex_source_heads"),
            "memplex_derivation_records": _record_grant_sql(),
            **{table: _payload_grant_sql(table) for table in _PAYLOAD_KINDS}}


def _read_grant_catalogue_base(tables: dict[str, Any]) -> dict[str, Any] | None:
    """Verify the exact v10 delta, then expose the unchanged v9 shape.

No unknown policy/column is discarded. Old catalogue verifiers still check
every original mutation policy, constraint, identity, privilege and trigger.
"""
    from memplex.storage.migrations.catalogue_checks import _source_sql

    result = dict(tables)
    for name, expression in _grant_policy_expressions().items():
        table = tables.get(name)
        if table is None or not table["rls"] or not table["force_rls"]:
            return None
        added = [p for p in table["policies"] if p[0] == f"{name}_source_read_grants"]
        if len(added) != 1:
            return None
        policy, command, permissive, roles, using, check = added[0]
        if ((policy, command, permissive, roles, check) !=
                (f"{name}_source_read_grants", "r", True, (0,), None)
                or _source_sql((using or "").replace(name + ".", ""))
                != _source_sql(expression.replace(name + ".", ""))):
            return None
        result[name] = {**table, "policies": tuple(p for p in table["policies"] if p is not added[0])}
    head = result["memplex_source_heads"]
    columns = head["columns"]
    if (not columns or columns[-1] != ("read_grants", len(columns), "text[]", True, "'{}'::text[]", "")
            or head["identities"].get("read_grants") != ""
            or not head["column_acls_are_default"].get("read_grants")
            or not head["column_collations_are_type_default"].get("read_grants")):
        return None
    result["memplex_source_heads"] = {
        **head, "columns": columns[:-1],
        **{key: {k: v for k, v in head[key].items() if k != "read_grants"}
           for key in ("identities", "column_acls_are_default", "column_collations_are_type_default")},
    }
    return result


def _verify_maintenance_owner(cur: Any) -> None:
    from memplex.storage.migrations._constants import MigrationIntegrityError

    cur.execute(
        "SELECT c.relname, c.relowner = (SELECT oid FROM pg_catalog.pg_roles WHERE rolname=current_user), "
        "c.relrowsecurity, c.relforcerowsecurity FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=current_schema() AND c.relname = ANY(%s) AND c.relkind='r' ORDER BY c.relname",
        (list(_MAINTENANCE_TABLES),),
    )
    if tuple(cur.fetchall()) != tuple((name, True, True, True) for name in _MAINTENANCE_TABLES):
        raise MigrationIntegrityError("source read grants require the existing forced-RLS table owner")


def _backfill_head(cur: Any, row: tuple[Any, ...]) -> None:
    """Verify a committed head/locator/ACL/digest, then change only its projection."""
    import json

    from memplex.source_codec import _decode_value, _wire, semantic_digest
    from memplex.source_contract import SourceClass, SourceHead, SourceKey, SourceKind, SourceState
    from memplex.storage.migrations._constants import MigrationIntegrityError
    from memplex.storage.postgres_sources import (
        _IDENTITY_COLUMNS,
        _TABLES,
        _identity,
        _locator,
        _read_grants,
    )

    key = SourceKey(row[0], row[13], row[15], SourceKind(row[1]), row[2])
    head = _decode_value(SourceHead, {
        "ref": {"key": _wire(key), "token": {"incarnation": row[3], "content_revision": row[4],
                                               "auth_revision": row[5]},
                "digest_version": row[9], "semantic_digest": row[10]},
        "state": row[6], "source_class": row[7], "payload_locator": row[8], "last_commit_seq": row[11],
        **row[12],
    })
    cur.execute("SELECT commit_seq, contract_version FROM memplex_source_authorities "
                "WHERE authority_id=%s AND tenant_id=%s AND workspace=%s", (row[0], row[13], row[15]))
    authority = cur.fetchone()
    if authority is None or authority[1] != 1 or head.last_commit_seq > authority[0]:
        raise MigrationIntegrityError("source read grant authority linkage is invalid")
    grants: tuple[str, ...] = ()
    if head.state is not SourceState.DELETED:
        locator = json.loads(head.payload_locator)
        cur.execute(f"SELECT id, data, {_IDENTITY_COLUMNS} FROM {_TABLES[key.kind]} "
                    "WHERE id=%s AND tenant_id=%s AND workspace=%s", (locator["id"], key.tenant_id, key.workspace_id))
        payload_rows = cur.fetchall()
        if len(payload_rows) != 1:
            raise MigrationIntegrityError("source read grant payload mapping is not unique")
        payload_row = payload_rows[0]
        payload = payload_row[1]
        if (not isinstance(payload, dict) or payload_row[0] != payload.get("id")
                or tuple(payload_row[2:]) != tuple(row[13:19]) or _identity(payload) != tuple(row[13:19])
                or head.payload_locator != _locator(key, payload)):
            raise MigrationIntegrityError("source read grant locator or identity is invalid")
        canonical = {**payload, "id": key.logical_id}
        if semantic_digest(canonical, input_context=head.input_context, root_refs=head.root_refs) != head.ref.semantic_digest:
            raise MigrationIntegrityError("source read grant semantic digest is invalid")
        if head.source_class is not SourceClass.LEGACY_UNVERIFIED:
            grants = _read_grants(payload)
    cur.execute("UPDATE memplex_source_heads SET read_grants=%s::text[] WHERE authority_id=%s "
                "AND kind=%s AND logical_id=%s AND incarnation=%s AND content_revision=%s "
                "AND auth_revision=%s AND payload_locator=%s RETURNING read_grants",
                (list(grants), *row[:6], row[8]))
    if cur.fetchall() != [(list(grants),)]:
        raise MigrationIntegrityError("source read grant projection update is inconsistent")
    cur.execute("SELECT read_grants FROM memplex_source_heads WHERE authority_id=%s AND kind=%s AND logical_id=%s",
                tuple(row[:3]))
    if cur.fetchall() != [(list(grants),)]:
        raise MigrationIntegrityError("source read grant projection verification failed")


def _apply_source_read_grants_upgrade(cur: Any, sql_body: bytes) -> None:
    """One existing-owner transaction; no runtime connection or persistent bypass.

    The caller owns rollback/close and the sole commit. Restoration in an
    aborted transaction can fail; rollback, rather than finally SQL, is the
    recovery guarantee. No whole-bank reads occur after FORCE is restored.
    """
    from memplex.storage.migrations._constants import MigrationIntegrityError
    from memplex.storage.postgres_sources import _HEAD_COLUMNS

    cur.execute("SHOW transaction_isolation")
    if cur.fetchone() != ("read committed",):
        raise MigrationIntegrityError("source read grant upgrade requires READ COMMITTED")
    _verify_maintenance_owner(cur)
    for name in _MAINTENANCE_TABLES:
        cur.execute(f"LOCK TABLE {name} IN ACCESS EXCLUSIVE MODE")
    _verify_maintenance_owner(cur)
    changed: list[str] = []
    try:
        for name in _MAINTENANCE_TABLES:
            cur.execute(f"ALTER TABLE {name} NO FORCE ROW LEVEL SECURITY")
            changed.append(name)
        cur.execute(sql_body.decode("utf-8"))
        cur.execute(f"SELECT {_HEAD_COLUMNS} FROM memplex_source_heads ORDER BY authority_id, kind, logical_id")
        for row in cur.fetchall():
            _backfill_head(cur, row)
    except BaseException:
        for name in reversed(changed):
            try:
                cur.execute(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY")
            except BaseException as cleanup_error:  # noqa: BLE001 - primary error stays authoritative
                _ = cleanup_error
        raise
    else:
        restore_error: BaseException | None = None
        for name in reversed(changed):
            try:
                cur.execute(f"ALTER TABLE {name} FORCE ROW LEVEL SECURITY")
            except BaseException as exc:  # noqa: BLE001 - try every restoration, then preserve the first failure
                if restore_error is None:
                    restore_error = exc
        if restore_error is not None:
            raise restore_error
    _verify_maintenance_owner(cur)
