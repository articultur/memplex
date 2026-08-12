"""Pure catalogue verification helpers, split from ``runner.py``.

These functions inspect an already-captured catalogue snapshot (plain dicts)
and report whether table column shapes, indexes, policies, defaults, sync
signatures, and vector-variant baselines match the expected schema. They are
pure (only ``hashlib``/``json``/stdlib) and re-exported from
``memplex.storage.migrations.runner`` for import-path stability.
"""

from __future__ import annotations

import json
import re
from hashlib import sha256
from typing import Any, Final, Literal

# Schema constants live in ``runner`` (they are also consumed by
# ``_catalog_snapshot`` / ``schema_fingerprint`` there). Importing them here is
# an ordered circular import: ``runner`` only imports *this* module at its very
# end (the re-export below), by which point every constant below is already
# defined, so the borrow resolves cleanly.
from memplex.storage.migrations.runner import (
    _ACL_COLUMNS,
    _CAPABILITIES_TABLE,
    _CORE_POLICY_DIGESTS,
    _CORE_TABLES,
    _FEEDBACK_CURRENT_POLICY_DIGESTS,
    _FEEDBACK_RUNTIME_V1_POLICY_DIGESTS,
    _LEDGER_TABLE,
    _LEGACY_CORE_TABLES,
    _MAX_VECTOR_DIM,
    _SEARCH_TSV_GENERATION_DIGEST,
    _SYNC_FUNCTIONS,
    _SYNC_TABLES,
    _TASK_TABLES,
)


def _normalise_sql(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower()


def _fingerprint_digest(snapshot: dict[str, Any]) -> str:
    """Hash a stable representation so adoption records explain their baseline."""
    canonical = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(canonical.encode("utf-8")).hexdigest()


def _column_shape(table: dict[str, Any]) -> tuple[tuple[str, str, bool, str], ...]:
    return tuple(
        (name, data_type, bool(not_null), generated or "")
        for name, _number, data_type, not_null, _default, generated in table["columns"]
    )


def _column_defaults(table: dict[str, Any]) -> dict[str, str | None]:
    return {name: default for name, _number, _type, _not_null, default, _generated in table["columns"]}


def _has_only_primary_key(table: dict[str, Any]) -> bool:
    return all(kind == "p" for kind, _name, _definition in table["constraints"])


def _has_sequential_attnums(table: dict[str, Any]) -> bool:
    return tuple(column[1] for column in table["columns"]) == tuple(
        range(1, len(table["columns"]) + 1)
    )


def _expression_digest(value: str | None) -> str:
    return sha256(_normalise_sql(value).encode("utf-8")).hexdigest()


def _defaults_match(table: dict[str, Any], expected: dict[str, str]) -> bool:
    """Compare every non-generated default; omitted means exactly no default."""
    return all(
        bool(generated)
        or _normalise_sql(default) == _normalise_sql(expected.get(column, ""))
        for column, _number, _type, _not_null, default, generated in table["columns"]
    )


def _search_tsv_matches(table: dict[str, Any]) -> bool:
    column = next((item for item in table["columns"] if item[0] == "search_tsv"), None)
    return column is not None and column[5] == "s" and _expression_digest(column[4]) == _SEARCH_TSV_GENERATION_DIGEST


def _index_signature(
    *,
    unique: bool,
    method: str,
    definition: str,
    keys: tuple[str, ...],
    predicate: str | None = None,
    included: tuple[str, ...] = (),
    valid: bool = True,
) -> tuple[bool, str, bool, tuple[str, ...], tuple[str, ...], str, str]:
    return (
        unique,
        method,
        valid,
        tuple(_normalise_sql(key) for key in keys),
        tuple(_normalise_sql(column) for column in included),
        _normalise_sql(definition),
        _normalise_sql(predicate),
    )


_INDEX_SIGNATURES: Final[dict[str, tuple[bool, str, bool, tuple[str, ...], tuple[str, ...], str, str]]] = {
    "fts_functions_idx": _index_signature(
        unique=False,
        method="gin",
        keys=("search_tsv",),
        definition="CREATE INDEX fts_functions_idx ON memplex_functions USING gin (search_tsv)",
    ),
    "memplex_functions_tenant_updated_idx": _index_signature(
        unique=False,
        method="btree",
        keys=("tenant_id", "updated_at"),
        definition=(
            "CREATE INDEX memplex_functions_tenant_updated_idx ON memplex_functions "
            "USING btree (tenant_id, updated_at DESC)"
        ),
    ),
    "memplex_functions_tenant_idx": _index_signature(
        unique=False,
        method="btree",
        keys=("tenant_id",),
        definition="CREATE INDEX memplex_functions_tenant_idx ON memplex_functions USING btree (tenant_id)",
    ),
    "memplex_edges_tenant_source_type_target_idx": _index_signature(
        unique=False,
        method="btree",
        keys=("tenant_id", "source", "edge_type", "target"),
        definition=(
            "CREATE INDEX memplex_edges_tenant_source_type_target_idx ON memplex_edges "
            "USING btree (tenant_id, source, edge_type, target)"
        ),
    ),
    "memplex_edges_tenant_target_type_source_idx": _index_signature(
        unique=False,
        method="btree",
        keys=("tenant_id", "target", "edge_type", "source"),
        definition=(
            "CREATE INDEX memplex_edges_tenant_target_type_source_idx ON memplex_edges "
            "USING btree (tenant_id, target, edge_type, source)"
        ),
    ),
    "memplex_edges_tenant_idx": _index_signature(
        unique=False,
        method="btree",
        keys=("tenant_id",),
        definition="CREATE INDEX memplex_edges_tenant_idx ON memplex_edges USING btree (tenant_id)",
    ),
    "memplex_edges_tenant_target_function_idx": _index_signature(
        unique=False,
        method="btree",
        keys=("tenant_id", "target_function"),
        definition=(
            "CREATE INDEX memplex_edges_tenant_target_function_idx ON memplex_edges "
            "USING btree (tenant_id, target_function) WHERE target_function IS NOT NULL"
        ),
        predicate="(target_function IS NOT NULL)",
    ),
    "memplex_observations_tenant_idx": _index_signature(
        unique=False,
        method="btree",
        keys=("tenant_id",),
        definition="CREATE INDEX memplex_observations_tenant_idx ON memplex_observations USING btree (tenant_id)",
    ),
    "memplex_facts_tenant_idx": _index_signature(
        unique=False,
        method="btree",
        keys=("tenant_id",),
        definition="CREATE INDEX memplex_facts_tenant_idx ON memplex_facts USING btree (tenant_id)",
    ),
    "memplex_preferences_tenant_idx": _index_signature(
        unique=False,
        method="btree",
        keys=("tenant_id",),
        definition="CREATE INDEX memplex_preferences_tenant_idx ON memplex_preferences USING btree (tenant_id)",
    ),
    "memplex_changelog_tenant_idx": _index_signature(
        unique=False,
        method="btree",
        keys=("tenant_id",),
        definition="CREATE INDEX memplex_changelog_tenant_idx ON memplex_changelog USING btree (tenant_id)",
    ),
    "memplex_functions_workspace_normalized_name_key": _index_signature(
        unique=True,
        method="btree",
        keys=(
            "tenant_id",
            "workspace",
            "lower(btrim(COALESCE(data ->> 'name_normalized'::text, data ->> 'name'::text, ''::text)))",
        ),
        definition=(
            "CREATE UNIQUE INDEX memplex_functions_workspace_normalized_name_key ON memplex_functions "
            "USING btree (tenant_id, workspace, lower(btrim(COALESCE(data ->> 'name_normalized'::text, "
            "data ->> 'name'::text, ''::text)))) WHERE visibility = 'workspace'::text AND "
            "btrim(COALESCE(data ->> 'name_normalized'::text, data ->> 'name'::text, ''::text)) <> ''::text"
        ),
        predicate=(
            "((visibility = 'workspace'::text) AND (btrim(COALESCE((data ->> 'name_normalized'::text), "
            "(data ->> 'name'::text), ''::text)) <> ''::text))"
        ),
    ),
    "memplex_functions_user_normalized_name_key": _index_signature(
        unique=True,
        method="btree",
        keys=(
            "tenant_id",
            "owner_subject",
            "lower(btrim(COALESCE(data ->> 'name_normalized'::text, data ->> 'name'::text, ''::text)))",
        ),
        definition=(
            "CREATE UNIQUE INDEX memplex_functions_user_normalized_name_key ON memplex_functions "
            "USING btree (tenant_id, owner_subject, lower(btrim(COALESCE(data ->> 'name_normalized'::text, "
            "data ->> 'name'::text, ''::text)))) WHERE visibility = 'user'::text AND "
            "btrim(COALESCE(data ->> 'name_normalized'::text, data ->> 'name'::text, ''::text)) <> ''::text"
        ),
        predicate=(
            "((visibility = 'user'::text) AND (btrim(COALESCE((data ->> 'name_normalized'::text), "
            "(data ->> 'name'::text), ''::text)) <> ''::text))"
        ),
    ),
    "memplex_functions_session_normalized_name_key": _index_signature(
        unique=True,
        method="btree",
        keys=(
            "tenant_id",
            "workspace",
            "owner_subject",
            "source_agent",
            "source_session",
            "lower(btrim(COALESCE(data ->> 'name_normalized'::text, data ->> 'name'::text, ''::text)))",
        ),
        definition=(
            "CREATE UNIQUE INDEX memplex_functions_session_normalized_name_key ON memplex_functions "
            "USING btree (tenant_id, workspace, owner_subject, source_agent, source_session, "
            "lower(btrim(COALESCE(data ->> 'name_normalized'::text, data ->> 'name'::text, ''::text)))) "
            "WHERE visibility = 'session'::text AND btrim(COALESCE(data ->> 'name_normalized'::text, "
            "data ->> 'name'::text, ''::text)) <> ''::text"
        ),
        predicate=(
            "((visibility = 'session'::text) AND (btrim(COALESCE((data ->> 'name_normalized'::text), "
            "(data ->> 'name'::text), ''::text)) <> ''::text))"
        ),
    ),
    "feedback_tenant_memory_idx": _index_signature(
        unique=False,
        method="btree",
        keys=("tenant_id", "memory_id", '"timestamp"'),
        definition=(
            'CREATE INDEX feedback_tenant_memory_idx ON feedback USING btree '
            '(tenant_id, memory_id, "timestamp" DESC)'
        ),
    ),
}


def _index_matches(
    row: tuple[Any, ...], expected: tuple[bool, str, bool, tuple[str, ...], tuple[str, ...], str, str]
) -> bool:
    (
        _name,
        unique,
        method,
        valid,
        key_count,
        total_count,
        keys,
        included,
        definition,
        predicate,
        owner_is_current_user,
        acl_is_default,
        persistence,
        is_not_partition,
    ) = row
    expected_unique, expected_method, expected_valid, expected_keys, expected_included, expected_definition, expected_predicate = expected
    return (
        unique == expected_unique
        and method == expected_method
        and valid == expected_valid
        and key_count == len(expected_keys)
        and total_count == len(expected_keys) + len(expected_included)
        and tuple(_normalise_sql(key) for key in keys) == expected_keys
        and tuple(_normalise_sql(column) for column in included) == expected_included
        and _normalise_sql(definition) == expected_definition
        and _normalise_sql(predicate) == expected_predicate
        and owner_is_current_user
        and acl_is_default
        and persistence == "p"
        and is_not_partition
    )


def _has_exact_indexes(table: dict[str, Any], expected_names: set[str]) -> bool:
    indexes = {row[0]: row for row in table["indexes"]}
    return set(indexes) == expected_names and all(
        _index_matches(indexes[name], _INDEX_SIGNATURES[name]) for name in expected_names
    )


def _managed_table_catalogue_matches(table: dict[str, Any]) -> bool:
    """Keep managed table authority, durability and write hooks exact."""
    expected_primary = (
        False,
        False,
        True,
        True,
        True,
        True,
        True,
        True,
        True,
        True,
        True,
        True,
    )
    return (
        table["owner_is_current_user"]
        and table["acl_is_default"]
        and table["persistence"] == "p"
        and not table["is_partition"]
        and table["has_no_inheritance"]
        and all(table["column_acls_are_default"].values())
        and all(table["column_collations_are_type_default"].values())
        and table["primary_index_catalog"]
        == ((expected_primary,) if table["primary_key"] else ())
        and not table["rules"]
        and not table["triggers"]
    )


def _legacy_base_columns(table_name: str, *, changelog_bigint: bool) -> tuple[tuple[str, str, bool, str], ...]:
    if table_name == "memplex_functions":
        return (
            ("id", "text", True, ""),
            ("data", "jsonb", True, ""),
            ("updated_at", "timestamp with time zone", False, ""),
            ("search_tsv", "tsvector", False, "s"),
        )
    if table_name == "memplex_edges":
        return (
            ("source", "text", True, ""),
            ("target", "text", True, ""),
            ("edge_type", "text", True, ""),
            ("weight", "real", False, ""),
            ("evidence", "jsonb", False, ""),
            ("created_at", "timestamp with time zone", False, ""),
        )
    if table_name == "memplex_observations":
        return (
            ("id", "text", True, ""),
            ("data", "jsonb", True, ""),
            ("created_at", "timestamp with time zone", False, ""),
        )
    if table_name in {"memplex_facts", "memplex_preferences"}:
        return (
            ("id", "text", True, ""),
            ("data", "jsonb", True, ""),
            ("updated_at", "timestamp with time zone", False, ""),
        )
    if table_name == "memplex_changelog":
        return (
            ("id", "bigint" if changelog_bigint else "integer", True, ""),
            ("func_id", "text", False, ""),
            ("ts", "timestamp with time zone", False, ""),
            ("event_type", "text", False, ""),
            ("description", "text", False, ""),
            ("source", "text", False, ""),
            ("actor", "text", False, ""),
        )
    raise AssertionError(f"unhandled core table: {table_name}")


def _primary_key(table_name: str) -> tuple[str, ...]:
    if table_name == "memplex_functions":
        return ("id",)
    if table_name == "memplex_edges":
        return ("source", "target", "edge_type")
    if table_name in {"memplex_observations", "memplex_facts", "memplex_preferences", "memplex_changelog"}:
        return ("id",)
    raise AssertionError(f"unhandled core table: {table_name}")


def _post_primary_key(table_name: str) -> tuple[str, ...]:
    return ("tenant_id", *_primary_key(table_name))


def _required_core_indexes(
    include_integrity_indexes: bool, *, include_edge_integrity: bool = False
) -> dict[str, set[str]]:
    indexes = {
        "memplex_functions": {
            "fts_functions_idx",
            "memplex_functions_tenant_updated_idx",
            "memplex_functions_tenant_idx",
        },
        "memplex_edges": {
            "memplex_edges_tenant_source_type_target_idx",
            "memplex_edges_tenant_target_type_source_idx",
            "memplex_edges_tenant_idx",
        },
        "memplex_observations": {"memplex_observations_tenant_idx"},
        "memplex_facts": {"memplex_facts_tenant_idx"},
        "memplex_preferences": {"memplex_preferences_tenant_idx"},
        "memplex_changelog": {"memplex_changelog_tenant_idx"},
    }
    if include_integrity_indexes:
        indexes["memplex_functions"] |= {
            "memplex_functions_workspace_normalized_name_key",
            "memplex_functions_user_normalized_name_key",
            "memplex_functions_session_normalized_name_key",
        }
    if include_edge_integrity:
        indexes["memplex_edges"].add("memplex_edges_tenant_target_function_idx")
    return indexes


def _policy_matches(
    table: dict[str, Any], *, policy_name: str, digests: tuple[str, str]
) -> bool:
    if not table["rls"] or not table["force_rls"] or len(table["policies"]) != 1:
        return False
    name, command, permissive, roles, qualify, check = table["policies"][0]
    return (
        name == policy_name
        and command == "*"
        and permissive
        and roles == (0,)
        and (_expression_digest(qualify), _expression_digest(check)) == digests
    )


def _policy_matches_core(table_name: str, table: dict[str, Any]) -> bool:
    return _policy_matches(
        table,
        policy_name=f"{table_name}_scope",
        digests=_CORE_POLICY_DIGESTS,
    )


def _post_core_columns(
    table_name: str,
    *,
    layout: Literal["migration_v2", "runtime_v1"],
    has_embedding: bool,
    changelog_bigint: bool,
    has_edge_integrity: bool,
) -> tuple[tuple[str, str, bool, str], ...]:
    base = _legacy_base_columns(table_name, changelog_bigint=changelog_bigint)
    acl = tuple((column, "text", True, "") for column in _ACL_COLUMNS)
    if layout == "migration_v2":
        columns = (*base, *acl)
    elif table_name == "memplex_functions":
        columns = (acl[0], *base[:-1], *acl[1:], base[-1])
    else:
        columns = (acl[0], *base, *acl[1:])
    if table_name == "memplex_functions" and has_embedding:
        columns = (*columns, ("embedding", "__vector__", False, ""))
    if table_name == "memplex_edges" and has_edge_integrity:
        columns = (*columns, ("target_function", "text", False, "s"))
    return columns


def _changelog_serial_default_matches(
    table: dict[str, Any], snapshot: dict[str, Any], *, sequence_bigint: bool
) -> bool:
    """Require the exact target-local serial sequence and its nextval dependency."""
    default = _column_defaults(table).get("id")
    expected_oid = snapshot["changelog_sequence_oid"]
    sequence = snapshot["changelog_sequence"]
    expected_maximum = 9_223_372_036_854_775_807 if sequence_bigint else 2_147_483_647
    return (
        expected_oid is not None
        and sequence
        == {
            "kind": "S",
            "owner_is_current_user": True,
            "acl_is_default": True,
            "persistence": "p",
            "is_partition": False,
            "type": "bigint" if sequence_bigint else "integer",
            "start": 1,
            "increment": 1,
            "minimum": 1,
            "maximum": expected_maximum,
            "cache": 1,
            "cycle": False,
            "dependencies": (("a", True, True),),
        }
        and table["default_dependencies"].get("id") == expected_oid
        and re.fullmatch(r"nextval\('[^']+'::regclass\)", _normalise_sql(default)) is not None
    )


def _post_core_defaults_match(
    table_name: str,
    table: dict[str, Any],
    snapshot: dict[str, Any],
    *,
    layout: Literal["migration_v2", "runtime_v1"],
    changelog_bigint: bool,
) -> bool:
    expected: dict[str, str] = {}
    if table_name == "memplex_changelog":
        # migration-v2 legacy327 is a fixed historical pair: a post-G002
        # integer id can retain the original BIGSERIAL descriptor, while the
        # pinned pre-G002 SERIAL route keeps its int4 descriptor through 0002.
        # No other sequence parameter is optional.
        sequence_forms = (True,) if changelog_bigint else (True, False)
        if not any(
            _changelog_serial_default_matches(table, snapshot, sequence_bigint=sequence_bigint)
            for sequence_bigint in sequence_forms
        ):
            return False
        expected["id"] = _column_defaults(table)["id"]
    if layout == "runtime_v1":
        expected.update(
            {
                "visibility": "'workspace'::text",
                "source_agent": "''::text",
                "source_session": "''::text",
            }
        )
    return _defaults_match(table, expected)


def _post_column_shape(table: dict[str, Any]) -> tuple[tuple[str, str, bool, str], ...]:
    """Retain the exact vector typmod check separately from its structural slot."""
    return tuple(
        (name, "__vector__" if name == "embedding" else data_type, not_null, generated)
        for name, data_type, not_null, generated in _column_shape(table)
    )


_EDGE_TARGET_FUNCTION_EXPRESSION: Final[str] = (
    "CASE WHEN (edge_type = 'BELONGS_TO'::text) THEN NULL::text ELSE target END"
)
_RESERVED_DOMAIN_ID_CHECK_EXPRESSION: Final[str] = "(NOT starts_with(id, 'domain_'::text))"
_EDGE_FOREIGN_KEY_SHAPES: Final[
    tuple[tuple[str, tuple[str, ...], str, tuple[str, ...], str, str, str, bool, bool, bool], ...]
] = (
    (
        "memplex_edges_source_function_fk",
        ("tenant_id", "source"),
        "memplex_functions",
        ("tenant_id", "id"),
        "a",
        "c",
        "s",
        False,
        False,
        True,
    ),
    (
        "memplex_edges_target_function_fk",
        ("tenant_id", "target_function"),
        "memplex_functions",
        ("tenant_id", "id"),
        "a",
        "c",
        "s",
        False,
        False,
        True,
    ),
)


def _edge_integrity_matches(table: dict[str, Any]) -> bool:
    """Recognise only the immutable 0004 edge endpoint contract."""
    target_function = next(
        (column for column in table["columns"] if column[0] == "target_function"), None
    )
    return (
        target_function is not None
        and target_function[5] == "s"
        and _normalise_sql(target_function[4]) == _normalise_sql(_EDGE_TARGET_FUNCTION_EXPRESSION)
        and all(
            target_oid > 0 and target_is_current_schema
            for (
                _name,
                _source_columns,
                _target_schema,
                target_oid,
                _target_relation,
                _target_columns,
                _update_action,
                _delete_action,
                _match_type,
                _deferrable,
                _deferred,
                _validated,
                target_is_current_schema,
            ) in table["foreign_keys"]
        )
        and tuple(
            (
                name,
                source_columns,
                target_relation,
                target_columns,
                update_action,
                delete_action,
                match_type,
                deferrable,
                deferred,
                validated,
            )
            for (
                name,
                source_columns,
                _target_schema,
                _target_oid,
                target_relation,
                target_columns,
                update_action,
                delete_action,
                match_type,
                deferrable,
                deferred,
                validated,
                _target_is_current_schema,
            ) in table["foreign_keys"]
        )
        == _EDGE_FOREIGN_KEY_SHAPES
        and tuple(kind for kind, _name, _definition in table["constraints"]) == ("p", "f", "f")
    )


def _reserved_domain_id_check_matches(table: dict[str, Any]) -> bool:
    """Recognise the exact v4 virtual-node namespace boundary."""
    return (
        tuple(table["checks"])
        == (
            (
                "memplex_functions_reserved_domain_id_check",
                _RESERVED_DOMAIN_ID_CHECK_EXPRESSION,
                True,
                False,
                False,
                False,
                True,
                0,
            ),
        )
        and tuple(kind for kind, _name, _definition in table["constraints"]) == ("p", "c")
    )


def _has_expected_core_constraints(
    table_name: str, table: dict[str, Any], *, has_edge_integrity: bool
) -> bool:
    if table_name == "memplex_functions":
        return (
            _reserved_domain_id_check_matches(table)
            if has_edge_integrity
            else _has_only_primary_key(table)
        )
    if table_name != "memplex_edges":
        return _has_only_primary_key(table)
    if not has_edge_integrity:
        return _has_only_primary_key(table)
    return _edge_integrity_matches(table)


def _matches_post_core(
    tables: dict[str, Any], snapshot: dict[str, Any]
) -> tuple[str, bool, int | None, bool] | None:
    if not set(_CORE_TABLES).issubset(tables):
        return None
    function_columns = {column[0]: column for column in tables["memplex_functions"]["columns"]}
    embedding = function_columns.get("embedding")
    vector_dim: int | None = None
    if embedding is not None:
        match = re.fullmatch(r"(?:.+\.)?vector\(([1-9][0-9]*)\)", str(embedding[2]))
        if (
            snapshot["vector_extension"] is None
            or match is None
            or int(match.group(1)) > _MAX_VECTOR_DIM
            or tables["memplex_functions"]["type_oids"].get("embedding")
            != snapshot["vector_extension"][1]
            or tables["memplex_functions"]["type_mods"].get("embedding") != int(match.group(1))
            or embedding[3]
            or embedding[5]
            or embedding[4] is not None
        ):
            return None
        vector_dim = int(match.group(1))
    for layout in ("migration_v2", "runtime_v1"):
        changelog_forms = (False, True) if layout == "migration_v2" else (True,)
        for changelog_bigint in changelog_forms:
            for include_integrity_indexes in (False, True):
                for include_edge_integrity in (False, True):
                    expected_indexes = _required_core_indexes(
                        include_integrity_indexes,
                        include_edge_integrity=include_edge_integrity,
                    )
                    if all(
                        _has_sequential_attnums(tables[table_name])
                        and _managed_table_catalogue_matches(tables[table_name])
                        and _post_column_shape(tables[table_name])
                        == _post_core_columns(
                            table_name,
                            layout=layout,
                            has_embedding=embedding is not None
                            and table_name == "memplex_functions",
                            changelog_bigint=changelog_bigint,
                            has_edge_integrity=include_edge_integrity,
                        )
                        and table["primary_key"] == _post_primary_key(table_name)
                        and _has_expected_core_constraints(
                            table_name, table, has_edge_integrity=include_edge_integrity
                        )
                        and _has_exact_indexes(table, expected_indexes[table_name])
                        and _policy_matches_core(table_name, table)
                        and _post_core_defaults_match(
                            table_name,
                            table,
                            snapshot,
                            layout=layout,
                            changelog_bigint=changelog_bigint,
                        )
                        and (table_name != "memplex_functions" or _search_tsv_matches(table))
                        for table_name in _CORE_TABLES
                        for table in (tables[table_name],)
                    ):
                        candidate_layout = (
                            layout
                            if layout == "runtime_v1"
                            else ("migration_v2" if changelog_bigint else "migration_v2_legacy327")
                        )
                        return candidate_layout, include_integrity_indexes, vector_dim, include_edge_integrity
    return None


def _matches_pre_core(tables: dict[str, Any], snapshot: dict[str, Any]) -> bool:
    names = set(tables)
    core_names = names - {"feedback", _CAPABILITIES_TABLE, _LEDGER_TABLE}
    if core_names not in (set(_LEGACY_CORE_TABLES), set(_CORE_TABLES)):
        return False
    changelog_bigint = "memplex_facts" in core_names
    for table_name in core_names:
        table = tables[table_name]
        expected_indexes = {"fts_functions_idx"} if table_name == "memplex_functions" else set()
        expected_defaults: dict[str, str] = {}
        if table_name == "memplex_changelog":
            if not _changelog_serial_default_matches(
                table, snapshot, sequence_bigint=changelog_bigint
            ):
                return False
            expected_defaults["id"] = _column_defaults(table)["id"] or ""
        if (
            not _has_sequential_attnums(table)
            or not _managed_table_catalogue_matches(table)
            or table["rls"]
            or table["force_rls"]
            or table["policies"]
            or not _has_only_primary_key(table)
            or table["primary_key"] != _primary_key(table_name)
            or _column_shape(table)
            != _legacy_base_columns(table_name, changelog_bigint=changelog_bigint)
            or not _defaults_match(table, expected_defaults)
            or not _has_exact_indexes(table, expected_indexes)
            or (table_name == "memplex_functions" and not _search_tsv_matches(table))
        ):
            return False
    feedback = tables.get("feedback")
    return feedback is None or _matches_legacy_feedback(feedback)


def _feedback_columns(*, current: bool) -> tuple[tuple[str, str, bool, str], ...]:
    base = (
        ("memory_id", "text", True, ""),
        ("field_role", "text", True, ""),
        ("value_index", "integer", False, ""),
        ("verdict", "text", True, ""),
        ("reason", "text", False, ""),
        ("source", "text", False, ""),
        ("timestamp", "timestamp with time zone", False, ""),
        ("owner", "text", False, ""),
        ("feedback_type", "text", False, ""),
        ("old_value", "text", False, ""),
        ("new_value", "text", False, ""),
        ("needs_review", "boolean", False, ""),
        ("needs_review_until", "timestamp with time zone", False, ""),
        ("resolved_at", "timestamp with time zone", False, ""),
        ("resolution", "text", False, ""),
    )
    if not current:
        return base
    return (
        *base,
        ("tenant_id", "text", True, ""),
        ("owner_subject_id", "text", True, ""),
        ("workspace_id", "text", True, ""),
        ("visibility", "text", True, ""),
        ("provenance", "jsonb", True, ""),
    )


def _feedback_defaults_match(table: dict[str, Any], *, current: bool) -> bool:
    expected = {
        "value_index": "0",
        "source": "'user'::text",
        "feedback_type": "'field_value'::text",
        "needs_review": "true",
    }
    if current:
        expected |= {"visibility": "'workspace'::text", "provenance": "'{}'::jsonb"}
    return _defaults_match(table, expected)


def _matches_legacy_feedback(table: dict[str, Any]) -> bool:
    return (
        _has_sequential_attnums(table)
        and _managed_table_catalogue_matches(table)
        and _column_shape(table) == _feedback_columns(current=False)
        and _feedback_defaults_match(table, current=False)
        and not table["primary_key"]
        and not table["constraints"]
        and not table["indexes"]
        and not table["rls"]
        and not table["force_rls"]
        and not table["policies"]
    )


def _matches_feedback(
    table: dict[str, Any], *, policy_digests: tuple[str, str]
) -> bool:
    return (
        _has_sequential_attnums(table)
        and _managed_table_catalogue_matches(table)
        and _column_shape(table) == _feedback_columns(current=True)
        and _feedback_defaults_match(table, current=True)
        and not table["primary_key"]
        and not table["constraints"]
        and _has_exact_indexes(table, {"feedback_tenant_memory_idx"})
        and _policy_matches(
            table,
            policy_name="feedback_tenant_scope",
            digests=policy_digests,
        )
    )


def _matches_current_feedback(table: dict[str, Any]) -> bool:
    return _matches_feedback(table, policy_digests=_FEEDBACK_CURRENT_POLICY_DIGESTS)


def _matches_runtime_feedback_v1(table: dict[str, Any]) -> bool:
    return _matches_feedback(table, policy_digests=_FEEDBACK_RUNTIME_V1_POLICY_DIGESTS)


def _matches_capabilities(
    table: dict[str, Any], rows: tuple[tuple[str, str], ...] | None, *, vector_dim: int | None
) -> bool:
    expected_rows = (
        ()
        if vector_dim is None
        else (("pgvector_embedding", sha256(f"pgvector:{vector_dim}".encode("ascii")).hexdigest()),)
    )
    return (
        _has_sequential_attnums(table)
        and _managed_table_catalogue_matches(table)
        and _column_shape(table)
        == (
            ("capability_name", "text", True, ""),
            ("parameter_digest", "text", True, ""),
            ("applied_at", "timestamp with time zone", True, ""),
        )
        and table["primary_key"] == ("capability_name",)
        and _has_only_primary_key(table)
        and _defaults_match(table, {})
        and not table["indexes"]
        and not table["rls"]
        and not table["force_rls"]
        and not table["policies"]
        and rows == expected_rows
    )


_SYNC_COLUMN_NAMES: Final[dict[str, tuple[str, ...]]] = {
    "memplex_sync_outbox": (
        "tenant_id", "stream_seq", "event_id", "origin_node_id", "node_type", "entity_key",
        "operation", "version_key", "payload", "visibility", "owner_subject_id", "workspace_id",
        "agent_id", "session_id", "created_at",
    ),
    "memplex_sync_entity_versions": (
        "tenant_id", "node_type", "entity_key", "version_key", "deleted", "event_id", "last_stream_seq",
    ),
    "memplex_sync_inbox": (
        "tenant_id", "origin_node_id", "event_id", "outcome", "applied_stream_seq", "received_at",
    ),
    "memplex_sync_batches": (
        "tenant_id", "origin_node_id", "batch_id", "request_sha256", "response", "created_at",
    ),
    "memplex_sync_targets": (
        "tenant_id", "target_id", "remote_node_id", "bootstrap_seq", "enabled",
    ),
    "memplex_sync_deliveries": (
        "tenant_id", "target_id", "stream_seq", "state", "attempt_count", "next_attempt_at",
        "lease_owner", "lease_until", "last_error_code",
    ),
    "memplex_sync_cursors": ("tenant_id", "remote_id", "consumer_id", "after_seq", "updated_at"),
    "memplex_sync_stream_state": ("tenant_id", "retention_floor", "compacted_through"),
    "memplex_sync_local_identity": ("singleton", "node_id"),
    "memplex_sync_ingress_principals": ("role_name", "remote_node_id", "enabled"),
    "memplex_sync_snapshots": (
        "tenant_id", "snapshot_id", "remote_id", "consumer_id", "request_id", "resume_seq", "expires_at",
    ),
    "memplex_sync_snapshot_items": ("tenant_id", "snapshot_id", "node_type", "entity_key", "event"),
}
_SYNC_PRIMARY_KEYS: Final[dict[str, tuple[str, ...]]] = {
    "memplex_sync_outbox": ("tenant_id", "stream_seq"),
    "memplex_sync_entity_versions": ("tenant_id", "node_type", "entity_key"),
    "memplex_sync_inbox": ("tenant_id", "origin_node_id", "event_id"),
    "memplex_sync_batches": ("tenant_id", "origin_node_id", "batch_id"),
    "memplex_sync_targets": ("tenant_id", "target_id"),
    "memplex_sync_deliveries": ("tenant_id", "target_id", "stream_seq"),
    "memplex_sync_cursors": ("tenant_id", "remote_id", "consumer_id"),
    "memplex_sync_stream_state": ("tenant_id",),
    "memplex_sync_local_identity": ("singleton",),
    "memplex_sync_ingress_principals": ("role_name", "remote_node_id"),
    "memplex_sync_snapshots": ("tenant_id", "snapshot_id"),
    "memplex_sync_snapshot_items": ("tenant_id", "snapshot_id", "node_type", "entity_key"),
}
_SYNC_INDEXES: Final[dict[str, set[str]]] = {
    "memplex_sync_outbox": {"memplex_sync_outbox_tenant_id_origin_node_id_event_id_key", "memplex_sync_outbox_tenant_stream_idx"},
    "memplex_sync_entity_versions": set(),
    "memplex_sync_inbox": set(),
    "memplex_sync_batches": set(),
    "memplex_sync_targets": {"memplex_sync_targets_tenant_id_remote_node_id_key"},
    "memplex_sync_deliveries": {"memplex_sync_deliveries_claim_idx", "memplex_sync_deliveries_retention_idx"},
    "memplex_sync_cursors": {"memplex_sync_cursors_tenant_after_idx"},
    "memplex_sync_stream_state": set(),
    "memplex_sync_local_identity": {"memplex_sync_local_identity_node_id_key"},
    "memplex_sync_ingress_principals": set(),
    "memplex_sync_snapshots": {"memplex_sync_snapshots_tenant_id_remote_id_consumer_id_requ_key", "memplex_sync_snapshots_expiry_idx"},
    "memplex_sync_snapshot_items": set(),
}
_SYNC_TABLE_SIGNATURE_DIGESTS: Final[dict[str, str]] = {
    "memplex_sync_outbox": "b02230ec141f55e5d93128a0fd15c8004387aecaaedf219b42f9bd44e99814ae",
    "memplex_sync_entity_versions": "bfc257c8197b6a23c76a6a2fa6309ab2bca25872fd5c858dbf58d79a82618f9b",
    "memplex_sync_inbox": "ec4203d3edeae0a061a9da963a3e04206b4b8c3adc1865648db3ef8c10de0fdf",
    "memplex_sync_batches": "190a5670443d6c25a240fb9a088ecee76b7edb3fca30b0a84db518deabe2205d",
    "memplex_sync_targets": "ed820d9f7b969cadab73423cb98f144e3a6d34de2a93e241b21574ee8bd95e59",
    "memplex_sync_deliveries": "380ec136aeac03bba764dbdde2e5358e729db01490da4a0d7975b58ff3c03d53",
    "memplex_sync_cursors": "d423dacd4319af123e325351ad513417c9594992090dcf7c59b01c2c651e0870",
    "memplex_sync_stream_state": "9d50be7f995573731293842efee5363934133969bef4a617934920c162aca020",
    "memplex_sync_local_identity": "2870f0af4c6af20307c61751901a9f79b168797568fb8d3937bd522e10023dd5",
    "memplex_sync_ingress_principals": "805f9e3c6cd2604203b08a8fcc1e8ab2de3fcd7280f66cc3aff5e7db1a0380af",
    "memplex_sync_snapshots": "24fdbaac7d22f13e6277b82591936825458a771bd8087f0bc1007e889b8b17ba",
    "memplex_sync_snapshot_items": "b52000460f16e10331186dea3a787f9ed82b9f6c4f55120db3cec0c57e64869a",
}


def _sync_table_signature(table: dict[str, Any]) -> str:
    """Stable v5 catalogue identity excluding target-local OIDs only."""
    value = {
        "rls": table["rls"],
        "force": table["force_rls"],
        "owner": table["owner_is_current_user"],
        "acl": table["acl_is_default"],
        "columns": tuple(
            (name, data_type, not_null, _normalise_sql(default), generated, table["identities"].get(name, ""))
            for name, _number, data_type, not_null, default, generated in table["columns"]
        ),
        "constraints": tuple((kind, name, _normalise_sql(definition)) for kind, name, definition in table["constraints"]),
        "checks": tuple((name, _normalise_sql(expression), *values) for name, expression, *values in table["checks"]),
        "foreign_keys": tuple(
            (name, source, relation, target, update, delete, match, deferrable, deferred, valid, local)
            for name, source, _schema, _oid, relation, target, update, delete, match, deferrable, deferred, valid, local in table["foreign_keys"]
        ),
        "indexes": tuple(
            (name, unique, method, valid, keys, included, _normalise_sql(definition), _normalise_sql(predicate), owner, acl, persistence, partition)
            for name, unique, method, valid, _key_count, _total_count, keys, included, definition, predicate, owner, acl, persistence, partition in table["indexes"]
        ),
        "policies": tuple(
            (name, command, permissive, roles, _normalise_sql(qualify), _normalise_sql(check))
            for name, command, permissive, roles, qualify, check in table["policies"]
        ),
    }
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()


def _sync_table_catalogue_matches(name: str, table: dict[str, Any]) -> bool:
    """Verify the v5 durable-state boundary from parsed catalogues, not SQL text."""
    return (
        _has_sequential_attnums(table)
        and _managed_table_catalogue_matches(table)
        and tuple(column[0] for column in table["columns"]) == _SYNC_COLUMN_NAMES[name]
        and table["primary_key"] == _SYNC_PRIMARY_KEYS[name]
        and table["rls"]
        and table["force_rls"]
        and len(table["policies"]) == 1
        and table["policies"][0][0] == f"{name}_scope"
        and table["policies"][0][1:4] == ("*", True, (0,))
        and set(index[0] for index in table["indexes"]) == _SYNC_INDEXES[name]
        and _SYNC_TABLE_SIGNATURE_DIGESTS[name]
        and _sync_table_signature(table) == _SYNC_TABLE_SIGNATURE_DIGESTS[name]
    )


_SYNC_FUNCTION_DEFINITION_DIGESTS: Final[dict[str, str]] = {
    # Immutable 0005 bodies, normalised with the arbitrary target schema removed.
    "memplex_configure_sync_local_identity": "d66d5827126b532203c4a283bb53b7ce6b5cda4d0be2d0c64f4cc0754a958d16",
    "memplex_configure_sync_ingress_principal": "7b2a50859d6cf79fb0c0199cc879bac5ee8108ded848d6c12a24a88b1217a505",
    "memplex_sync_assert_delivery_quota": "ccfbcbf73467e46a24052aa7e3541aab1ec15d03d5bf275aada65fd002be4885",
    "memplex_sync_snapshot_admission_counts": "a45bfd3e843e098a76c61b2d2b46e88cd31d75024c63ef2359b183fd5dee052f",
    "memplex_sync_compact": "705e0bd26b5bdbf9dce531e9259965c6eb025faff0924435d621336bfd9ac537",
    "memplex_sync_capture_before": "19cfb8858f42a0c679284adf61e735620b201fc9dc4112e821dba4f35c7abb5c",
    "memplex_sync_capture_local_change": "926d28ca2f7886e51469224aeebb7e47e85eb705055b6ebe54ee1d957a243b93",
    "memplex_sync_apply_inbound": "a156f824e111216892ae448383f551dd9a6724647a28bdbb70499214d32d52c6",
    "memplex_sync_require_canonical_entity_key": "f9b71745e32089b8dbd8022e7177dd8d2262024f4934b2f6e32226fb7aa0e37f",
    "memplex_sync_require_canonical_version": "8673168f07bd865ab603f0a08a675d403fb9888806512c55aea3bfdff9e384ee",
    "memplex_sync_encode_string_array": "4653737a1d04efda6cd07a15859f2b573e99a4107fa014ddfaa4290d1c491de4",
}


def _sync_function_digest(definition: str) -> str:
    canonical = re.sub(r"CREATE OR REPLACE FUNCTION [^(]+", "CREATE OR REPLACE FUNCTION <schema>", definition, count=1)
    canonical = re.sub(
        r"SET search_path TO 'pg_catalog', '(?:''|[^'])*'",
        "SET search_path TO 'pg_catalog', <schema>",
        canonical,
        count=1,
    )
    return sha256(_normalise_sql(canonical).encode("utf-8")).hexdigest()


def _reliable_sync_catalogue_matches(tables: dict[str, Any], snapshot: dict[str, Any]) -> bool:
    """Recognise v5 only when every durable table and core capture hook exists."""
    if not all(name in tables and _sync_table_catalogue_matches(name, tables[name]) for name in _SYNC_TABLES):
        return False
    function_oids = {row[0]: row[1] for row in snapshot["sync_functions"]}
    expected_trigger_rows = {
        "memplex_functions": {
            ("memplex_sync_functions_before", 31, "O", function_oids.get("memplex_sync_capture_before"), "66756e6374696f6e00"),
            ("memplex_sync_functions_after", 29, "O", function_oids.get("memplex_sync_capture_local_change"), "66756e6374696f6e00"),
        },
        "memplex_edges": {
            ("memplex_sync_edges_before", 31, "O", function_oids.get("memplex_sync_capture_before"), "6564676500"),
            ("memplex_sync_edges_after", 29, "O", function_oids.get("memplex_sync_capture_local_change"), "6564676500"),
        },
        "memplex_observations": {
            ("memplex_sync_observations_before", 31, "O", function_oids.get("memplex_sync_capture_before"), "6f62736572766174696f6e00"),
            ("memplex_sync_observations_after", 29, "O", function_oids.get("memplex_sync_capture_local_change"), "6f62736572766174696f6e00"),
        },
        "memplex_facts": {
            ("memplex_sync_facts_before", 31, "O", function_oids.get("memplex_sync_capture_before"), "6661637400"),
            ("memplex_sync_facts_after", 29, "O", function_oids.get("memplex_sync_capture_local_change"), "6661637400"),
        },
        "memplex_preferences": {
            ("memplex_sync_preferences_before", 31, "O", function_oids.get("memplex_sync_capture_before"), "707265666572656e636500"),
            ("memplex_sync_preferences_after", 29, "O", function_oids.get("memplex_sync_capture_local_change"), "707265666572656e636500"),
        },
    }
    triggers_match = all(
        set(tables[table_name]["triggers"]) == rows
        for table_name, rows in expected_trigger_rows.items()
    )
    expected_functions = set(_SYNC_FUNCTIONS)
    function_rows = snapshot["sync_functions"]
    functions_match = (
        {row[0] for row in function_rows} == expected_functions
        and all(
            row[2] and row[3] and row[5]
            and row[4] == (f"search_path=pg_catalog, {snapshot['schema_path']}",)
            and _SYNC_FUNCTION_DEFINITION_DIGESTS[row[0]]
            and _sync_function_digest(row[6]) == _SYNC_FUNCTION_DEFINITION_DIGESTS[row[0]]
            for row in function_rows
        )
    )
    return triggers_match and functions_match


_BACKGROUND_TASK_SIGNATURE_DIGEST: Final[str] = (
    "95777fa30e7d95e7089d8cf8dff7da117f1fdef36f00de7167558f2083b5cdf0"
)


def _background_tasks_catalogue_matches(tables: dict[str, Any]) -> bool:
    """Recognise the exact immutable 0006 task catalogue."""
    if set(_TASK_TABLES) != {"memplex_background_tasks"}:
        return False
    table = tables.get("memplex_background_tasks")
    return bool(
        table is not None
        and _has_sequential_attnums(table)
        and _managed_table_catalogue_matches(table)
        and tuple(column[0] for column in table["columns"])
        == (
            "task_id",
            "task_type",
            "status",
            "created_at",
            "completed_at",
            "payload",
            "result",
            "error",
            "retry_count",
            "max_retries",
            "next_attempt_at",
            "lease_until",
            "lease_id",
            "last_error_code",
        )
        and table["primary_key"] == ("task_id",)
        and not table["rls"]
        and not table["force_rls"]
        and not table["policies"]
        and {index[0] for index in table["indexes"]}
        == {
            "memplex_background_tasks_due_idx",
            "memplex_background_tasks_lease_idx",
        }
        and _sync_table_signature(table) == _BACKGROUND_TASK_SIGNATURE_DIGEST
    )


def _variant_digest(variant: str) -> str:
    return sha256(f"memplex-g003-catalogue-v1:{variant}".encode()).hexdigest()


def _vector_parameter_digest(dimension: int) -> str:
    return sha256(f"pgvector:{dimension}".encode("ascii")).hexdigest()


_FIXED_AUDITED_ADOPTION_VARIANTS: Final[frozenset[str]] = frozenset(
    {
        "post_g002_migration_v2",
        "post_g002_migration_v2_legacy327",
        "post_g002_runtime_v1",
        "post_g002_runtime_v1_feedback_v1",
        "post_g002_migration_v2_current",
        "post_g002_migration_v2_legacy327_current",
        "post_g002_runtime_v1_current",
    }
)
_RUNTIME_VECTOR_VARIANT: Final[re.Pattern[str]] = re.compile(
    r"^post_g002_runtime_v1(?P<feedback>_feedback_v1)?(?:_current)?_vector_(?P<dimension>[1-9][0-9]*)$"
)


def _legacy_vector_dimension(variant: str) -> int | None:
    match = _RUNTIME_VECTOR_VARIANT.fullmatch(variant)
    if match is None:
        return None
    dimension = int(match.group("dimension"))
    return dimension if dimension <= _MAX_VECTOR_DIM else None


def _is_audited_adoption_variant(variant: str) -> bool:
    return variant in _FIXED_AUDITED_ADOPTION_VARIANTS or _legacy_vector_dimension(variant) is not None


def _is_edge_integrity_current_variant(variant: str) -> bool:
    return "_edge_integrity_current" in variant


def _is_reliable_sync_current_variant(variant: str) -> bool:
    return variant.endswith("_reliable_sync_v5") or "_reliable_sync_v5" in variant


def _is_background_tasks_current_variant(variant: str) -> bool:
    return variant.endswith("_background_tasks_v6") or "_background_tasks_v6_vector_" in variant


def _allowed_adoption_baselines(current_variant: str) -> frozenset[str]:
    """Bind adopted rows to the exact current whole-schema classifier.

    A final runtime feedback layout cannot show whether feedback already existed
    before 0003, so only its two documented runtime ancestors are accepted.
    """
    if "_background_tasks_v6_vector_" in current_variant:
        return _allowed_adoption_baselines(
            current_variant.replace("_background_tasks_v6_vector_", "_vector_", 1)
        )
    if current_variant.endswith("_background_tasks_v6"):
        return _allowed_adoption_baselines(
            current_variant.removesuffix("_background_tasks_v6")
        )
    if "_reliable_sync_v5_vector_" in current_variant:
        baseline_variant = current_variant.replace("_reliable_sync_v5_vector_", "_vector_", 1)
        if "_current_vector_" not in baseline_variant:
            baseline_variant = baseline_variant.replace("_vector_", "_current_vector_", 1)
        return _allowed_adoption_baselines(baseline_variant)
    if _is_reliable_sync_current_variant(current_variant):
        return _allowed_adoption_baselines(current_variant.replace("_reliable_sync_v5", "", 1))
    if _is_edge_integrity_current_variant(current_variant):
        return _allowed_adoption_baselines(
            current_variant.replace("_edge_integrity_current", "_current", 1)
        )
    fixed = {
        "post_g002_migration_v2_current": frozenset(
            {"post_g002_migration_v2", "post_g002_migration_v2_current"}
        ),
        "post_g002_migration_v2_legacy327_current": frozenset(
            {"post_g002_migration_v2_legacy327", "post_g002_migration_v2_legacy327_current"}
        ),
        "post_g002_runtime_v1_current": frozenset(
            {
                "post_g002_runtime_v1",
                "post_g002_runtime_v1_feedback_v1",
                "post_g002_runtime_v1_current",
            }
        ),
    }
    if current_variant in fixed:
        return fixed[current_variant]
    dimension = _legacy_vector_dimension(current_variant)
    if dimension is None or "_current_vector_" not in current_variant:
        return frozenset()
    return frozenset(
        {
            f"post_g002_runtime_v1_vector_{dimension}",
            f"post_g002_runtime_v1_feedback_v1_vector_{dimension}",
            f"post_g002_runtime_v1_current_vector_{dimension}",
        }
    )
