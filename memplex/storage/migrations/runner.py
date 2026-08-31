"""Discovery and immutable-content checks for packaged PostgreSQL migrations.

Applying migrations and inspecting database fingerprints deliberately arrive in
G003 Task 2.  Task 1 keeps this module side-effect free so it can validate
resources from an installed wheel without opening a PostgreSQL connection.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from importlib import resources
from typing import Any, Final, Literal

from memplex.storage.migrations._constants import (
    _ACL_COLUMNS,
    _APPLICATION_ACL,
    _APPLICATION_ACL_FUNCTIONS,
    _APPLICATION_ACL_TABLES,
    _CAPABILITIES_TABLE,
    _CORE_POLICY_DIGESTS,
    _CORE_TABLES,
    _FEEDBACK_CURRENT_POLICY_DIGESTS,
    _FEEDBACK_RUNTIME_V1_POLICY_DIGESTS,
    _KNOWN_MEMPLEX_RELATION_KINDS,
    _LEDGER_TABLE,
    _LEGACY_CORE_TABLES,
    _MANAGED_TABLES,
    _MAX_VECTOR_DIM,
    _SEARCH_TSV_GENERATION_DIGEST,
    _SYNC_FUNCTIONS,
    _SYNC_TABLES,
    _TASK_TABLES,
    ApplicationAclContract,
    IngressAclContract,
    Migration,
    MigrationIntegrityError,
    MigrationPlan,
    SchemaFingerprint,
    SchemaVariantFeatures,
    _LedgerEntry,
)

_APPROVED_0002_CHECKSUM = "8e932e605e4eb36f6ec410c5a589001133a2db29ba902ff34227ae0a223e9a16"
_MIGRATION_NAME = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z][a-z0-9_]*)\.sql$")
_SQL_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Deliberately fixed, reviewed once, and kept independent of schema/migration
# names.  This is a signed PostgreSQL bigint accepted by pg_advisory_xact_lock.
_MIGRATION_LOCK_KEY: Final[int] = -4_710_001_234_567_890_123


@dataclass(frozen=True, slots=True)
class PostgresTargetIdentity:
    """Credential-free identity of one resolved PostgreSQL migration target."""

    database: str
    schema: str
    server_address: str | None
    server_port: int | None
    declared_database: str | None = field(default=None, compare=False, repr=False)
    declared_host: str | None = field(default=None, compare=False, repr=False)
    declared_hostaddr: str | None = field(default=None, compare=False, repr=False)
    declared_port: int | None = field(default=None, compare=False, repr=False)


@dataclass(frozen=True, slots=True)
class PostgresApplicationPrincipal:
    """Exact business-pool identity observed on one PostgreSQL session."""

    role: str
    session_role: str

    def __post_init__(self) -> None:
        if (
            type(self.role) is not str
            or not self.role
            or type(self.session_role) is not str
            or not self.session_role
        ):
            raise TypeError("PostgreSQL application principal fields must be exact non-empty str")


@dataclass(frozen=True, slots=True)
class VectorCapabilityRequest:
    """An immutable request for the one controlled pgvector capability."""

    dim: int
    policy: Literal["required", "best_effort", "disabled"]

    def __post_init__(self) -> None:
        minimum = 0 if self.policy == "disabled" else 1
        if (
            isinstance(self.dim, bool)
            or not isinstance(self.dim, int)
            or not minimum <= self.dim <= _MAX_VECTOR_DIM
        ):
            raise ValueError(f"vector dimension must be between {minimum} and {_MAX_VECTOR_DIM}")
        if self.policy not in {"required", "best_effort", "disabled"}:
            raise ValueError("vector policy must be required, best_effort, or disabled")


@dataclass(frozen=True, slots=True)
class VectorCapabilityStatus:
    """Result of controlled pgvector negotiation, never inferred from a store."""

    state: Literal["ready", "degraded", "disabled"]
    dim: int
    parameter_digest: str | None = None


def _migration_resources() -> dict[str, bytes]:
    """Read SQL bytes through package resources, including from installed wheels."""
    package = resources.files("memplex.storage.migrations")
    return {
        item.name: item.read_bytes()
        for item in package.iterdir()
        if item.name.endswith(".sql") and item.is_file()
    }


def _parse_migration(filename: str, sql_bytes: bytes) -> Migration:
    match = _MIGRATION_NAME.fullmatch(filename)
    if match is None:
        raise MigrationIntegrityError(f"invalid migration resource name: {filename!r}")
    return Migration(
        version=int(match.group("version")),
        name=match.group("name"),
        sql_bytes=sql_bytes,
        checksum=sha256(sql_bytes).hexdigest(),
    )


def _is_unquoted_identifier_continuation(character: str) -> bool:
    """Conservatively recognise a character that can adjoin a PG identifier."""
    return bool(character) and (
        not character.isascii() or character.isalnum() or character in {"_", "$"}
    )


def _masked_sql(sql: str) -> str:
    """Mask PostgreSQL comments and quoted content while preserving offsets."""
    chars = list(sql)
    index = 0
    length = len(sql)

    def mask(start: int, end: int) -> None:
        for position in range(start, end):
            if chars[position] != "\n":
                chars[position] = " "

    while index < length:
        if sql.startswith("--", index):
            end = min(
                (position for position in (sql.find("\r", index), sql.find("\n", index)) if position >= 0),
                default=length,
            )
            mask(index, end)
            index = end
        elif sql.startswith("/*", index):
            start = index
            depth = 1
            index += 2
            while index < length and depth:
                if sql.startswith("/*", index):
                    depth += 1
                    index += 2
                elif sql.startswith("*/", index):
                    depth -= 1
                    index += 2
                else:
                    index += 1
            if depth:
                raise MigrationIntegrityError("unterminated block comment in migration SQL")
            mask(start, index)
        elif sql[index] in ("'", '"'):
            quote = sql[index]
            escape_string = (
                quote == "'"
                and index > 0
                and sql[index - 1] in {"E", "e"}
                and (index == 1 or not _is_unquoted_identifier_continuation(sql[index - 2]))
            )
            start = index
            end = index + 1
            closed = False
            while end < length:
                if escape_string and sql[end] == "\\":
                    end += 2
                    continue
                if sql[end] == quote:
                    if end + 1 < length and sql[end + 1] == quote:
                        end += 2
                        continue
                    end += 1
                    closed = True
                    break
                end += 1
            if not closed:
                kind = "escape string" if escape_string else "quoted SQL literal"
                raise MigrationIntegrityError(f"unterminated {kind} in migration SQL")
            mask(start, end)
            index = end
        elif sql[index] == "$":
            if index > 0 and _is_unquoted_identifier_continuation(sql[index - 1]):
                index += 1
                continue
            tag = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", sql[index:])
            if tag is None:
                index += 1
                continue
            delimiter = tag.group(0)
            end = sql.find(delimiter, index + len(delimiter))
            if end == -1:
                raise MigrationIntegrityError("unterminated dollar-quoted string in migration SQL")
            end += len(delimiter)
            mask(index, end)
            index = end
        else:
            index += 1
    return "".join(chars)


def _sql_statements(sql: str) -> tuple[tuple[str, int, int], ...]:
    """Return non-empty statements as (normalised text, start, semicolon)."""
    masked = _masked_sql(sql)
    statements: list[tuple[str, int, int]] = []
    start = 0
    for end, character in enumerate(masked):
        if character != ";":
            continue
        statement = masked[start:end].strip()
        if statement:
            statements.append((statement, start, end))
        start = end + 1
    tail = masked[start:].strip()
    if tail:
        statements.append((tail, start, len(masked)))
    return tuple(statements)


def _transaction_control_statement(statement: str) -> str | None:
    """Recognise PostgreSQL transaction grammar at a masked statement start."""
    words = tuple(word.upper() for word in _SQL_WORD.findall(statement))
    if not words:
        return None
    if words[0] in {"BEGIN", "COMMIT", "END", "ROLLBACK", "ABORT", "SAVEPOINT", "RELEASE"}:
        return words[0]
    if words[:2] == ("START", "TRANSACTION"):
        return "START TRANSACTION"
    if words[:2] == ("PREPARE", "TRANSACTION"):
        return "PREPARE TRANSACTION"
    if words[:2] == ("SET", "TRANSACTION"):
        return "SET TRANSACTION"
    if words[:5] == ("SET", "SESSION", "CHARACTERISTICS", "AS", "TRANSACTION"):
        return "SET SESSION CHARACTERISTICS AS TRANSACTION"
    return None


def _validate_0002_checksum(migrations: tuple[Migration, ...]) -> None:
    migration = next((item for item in migrations if item.version == 2), None)
    if migration is None:
        raise MigrationIntegrityError("required immutable migration 0002 is missing")
    if migration.checksum != _APPROVED_0002_CHECKSUM:
        raise MigrationIntegrityError("migration 0002 checksum does not match the approved immutable bytes")


def _body_for_execution(migration: Migration) -> bytes:
    """Validate and remove 0002's sole allowed standalone transaction shell."""
    if migration.version != 2:
        _validate_sql_transaction_contract(migration)
        return migration.sql_bytes
    if migration.checksum != _APPROVED_0002_CHECKSUM:
        raise MigrationIntegrityError("migration 0002 checksum does not match the approved immutable bytes")

    sql = migration.sql_bytes.decode("utf-8")
    statements = _sql_statements(sql)
    transaction_controls = tuple(
        (index, control)
        for index, (statement, _, _) in enumerate(statements)
        if (control := _transaction_control_statement(statement)) is not None
    )
    if (
        len(statements) < 2
        or statements[0][0].upper() != "BEGIN"
        or statements[-1][0].upper() != "COMMIT"
        or transaction_controls != ((0, "BEGIN"), (len(statements) - 1, "COMMIT"))
    ):
        raise MigrationIntegrityError(
            "migration 0002 must contain exactly one outer BEGIN; ... COMMIT; transaction shell"
        )
    return migration.sql_bytes[statements[0][2] + 1 : statements[-1][1]]


def _validate_sql_transaction_contract(migration: Migration) -> None:
    if migration.version == 2:
        _body_for_execution(migration)
        return
    statements = _sql_statements(migration.sql_bytes.decode("utf-8"))
    if any(_transaction_control_statement(statement) is not None for statement, _, _ in statements):
        raise MigrationIntegrityError(
            f"transaction control is not permitted in migration {migration.version:04d}"
        )


def discover_migrations() -> tuple[Migration, ...]:
    """Discover continuously versioned, immutable migration resources."""
    migrations = tuple(
        _parse_migration(filename, sql_bytes)
        for filename, sql_bytes in sorted(_migration_resources().items())
    )
    versions = [migration.version for migration in migrations]
    if not versions or versions != list(range(1, len(versions) + 1)):
        raise MigrationIntegrityError(f"migration versions must be continuous from 1: {versions}")
    _validate_0002_checksum(migrations)
    for migration in migrations:
        _validate_sql_transaction_contract(migration)
    return migrations


def _quote_identifier(identifier: str) -> str:
    """Quote one PostgreSQL identifier without interpolating untrusted syntax."""
    return '"' + identifier.replace('"', '""') + '"'


def _declared_dsn_value(parameters: Any, name: str) -> str | None:
    """Return one allow-listed libpq diagnostic value without retaining secrets."""
    if not isinstance(parameters, dict):
        return None
    value = parameters.get(name)
    if type(value) is not str or not value:
        return None
    return value


def inspect_postgres_connection_target(conn: Any, cur: Any) -> PostgresTargetIdentity:
    """Inspect a borrowed connection without taking ownership of it or its cursor."""
    cur.execute(
        """
        SELECT pg_catalog.current_database(), pg_catalog.current_schema(),
               pg_catalog.inet_server_addr()::text,
               pg_catalog.inet_server_port()
        """
    )
    row = cur.fetchone()
    if (
        row is None
        or len(row) != 4
        or type(row[0]) is not str
        or not row[0]
        or type(row[1]) is not str
        or not row[1]
        or (row[2] is not None and (type(row[2]) is not str or not row[2]))
        or (
            row[3] is not None
            and (type(row[3]) is not int or not 1 <= row[3] <= 65_535)
        )
    ):
        raise MigrationIntegrityError("PostgreSQL target identity cannot be inspected")

    declared_database, declared_host, declared_hostaddr, declared_port = (
        _declared_dsn_diagnostics(conn)
    )

    return PostgresTargetIdentity(
        database=row[0],
        schema=row[1],
        server_address=row[2],
        server_port=row[3],
        declared_database=declared_database,
        declared_host=declared_host,
        declared_hostaddr=declared_hostaddr,
        declared_port=declared_port,
    )


def _declared_dsn_diagnostics(conn: Any) -> tuple[str | None, str | None, str | None, int | None]:
    """Read optional libpq DSN diagnostics without retaining secret values."""
    try:
        getter = getattr(conn, "get_dsn_parameters", None)
        parameters = getter() if callable(getter) else {}
        declared_database = _declared_dsn_value(parameters, "dbname")
        declared_host = _declared_dsn_value(parameters, "host")
        declared_hostaddr = _declared_dsn_value(parameters, "hostaddr")
        declared_port_text = _declared_dsn_value(parameters, "port")
    except Exception:  # noqa: BLE001 - optional diagnostics must never gate the actual target.
        declared_database = None
        declared_host = None
        declared_hostaddr = None
        declared_port_text = None
    try:
        declared_port = None if declared_port_text is None else int(declared_port_text)
    except (TypeError, ValueError, OverflowError):
        declared_port = None
    if declared_port is not None and not 1 <= declared_port <= 65_535:
        declared_port = None
    return declared_database, declared_host, declared_hostaddr, declared_port


def _restore_schema_from_search_path(search_path: str) -> str:
    """Resolve the single pinned restore schema from a search_path setting."""
    entry = search_path.strip()
    if not entry or "," in entry:
        raise MigrationIntegrityError("PostgreSQL target identity cannot be inspected")
    if entry.startswith('"'):
        if len(entry) < 2 or not entry.endswith('"'):
            raise MigrationIntegrityError("PostgreSQL target identity cannot be inspected")
        inner = entry[1:-1]
        if '"' in inner.replace('""', ""):
            raise MigrationIntegrityError("PostgreSQL target identity cannot be inspected")
        schema = inner.replace('""', '"')
    else:
        schema = entry.lower()
        if schema == "$user":
            raise MigrationIntegrityError("PostgreSQL target identity cannot be inspected")
    if not schema or "\x00" in schema:
        raise MigrationIntegrityError("PostgreSQL target identity cannot be inspected")
    return schema


def inspect_postgres_restore_connection_target(conn: Any, cur: Any) -> PostgresTargetIdentity:
    """Inspect a borrowed connection's target identity, tolerating an absent schema.

    Strict inspection runs first; only when current_schema() no longer resolves
    (post-disaster, pre-restore) does the fallback pin the identity's schema to
    the connection's single-entry search_path.  The connection is borrowed: it
    is neither closed nor rolled back here.
    """
    try:
        return inspect_postgres_connection_target(conn, cur)
    except MigrationIntegrityError:
        pass
    cur.execute(
        """
        SELECT pg_catalog.current_database(),
               pg_catalog.inet_server_addr()::text,
               pg_catalog.inet_server_port(),
               pg_catalog.current_setting('search_path')
        """
    )
    row = cur.fetchone()
    if (
        row is None
        or len(row) != 4
        or type(row[0]) is not str
        or not row[0]
        or (row[1] is not None and (type(row[1]) is not str or not row[1]))
        or (
            row[2] is not None
            and (type(row[2]) is not int or not 1 <= row[2] <= 65_535)
        )
        or type(row[3]) is not str
    ):
        raise MigrationIntegrityError("PostgreSQL target identity cannot be inspected")
    declared_database, declared_host, declared_hostaddr, declared_port = (
        _declared_dsn_diagnostics(conn)
    )
    return PostgresTargetIdentity(
        database=row[0],
        schema=_restore_schema_from_search_path(row[3]),
        server_address=row[1],
        server_port=row[2],
        declared_database=declared_database,
        declared_host=declared_host,
        declared_hostaddr=declared_hostaddr,
        declared_port=declared_port,
    )


def _target_identity_key(
    target: Any,
) -> tuple[str | None, int | None, str, str]:
    """Extract an exact native key without invoking foreign comparison methods."""
    if type(target) is not PostgresTargetIdentity:
        raise MigrationIntegrityError("PostgreSQL expected target identity is invalid")
    if (
        type(target.database) is not str
        or not target.database
        or type(target.schema) is not str
        or not target.schema
        or (
            target.server_address is not None
            and (type(target.server_address) is not str or not target.server_address)
        )
        or (
            target.server_port is not None
            and (
                type(target.server_port) is not int
                or not 1 <= target.server_port <= 65_535
            )
        )
    ):
        raise MigrationIntegrityError("PostgreSQL expected target identity is invalid")
    return (
        target.server_address,
        target.server_port,
        target.database,
        target.schema,
    )


def _read_application_principal(cur: Any) -> PostgresApplicationPrincipal:
    """Read the exact login identity of the current session."""
    cur.execute("SELECT current_user, session_user")
    row = cur.fetchone()
    if (
        row is None
        or len(row) != 2
        or type(row[0]) is not str
        or type(row[1]) is not str
    ):
        raise MigrationIntegrityError("PostgreSQL application principal is invalid")
    return PostgresApplicationPrincipal(row[0], row[1])


def _verify_target_identity(
    conn: Any,
    cur: Any,
    expected_target: PostgresTargetIdentity | None,
    *,
    inspector: Any = inspect_postgres_connection_target,
) -> PostgresTargetIdentity:
    actual_target = inspector(conn, cur)
    if expected_target is not None and _target_identity_key(actual_target) != _target_identity_key(
        expected_target
    ):
        raise MigrationIntegrityError("PostgreSQL target identity does not match expected target")
    return actual_target


def _confine_to_current_schema(cur: Any) -> str:
    """Pin this transaction to the first, server-resolved schema in its path."""
    cur.execute("SELECT pg_catalog.current_schema()")
    row = cur.fetchone()
    if row is None or not isinstance(row[0], str) or not row[0]:
        raise MigrationIntegrityError("unrecognised migration schema")
    schema = row[0]
    cur.execute(
        "SELECT pg_catalog.set_config('search_path', %s, true)",
        (_quote_identifier(schema),),
    )
    return schema


def _vector_extension_type(cur: Any) -> tuple[str, int] | None:
    """Return the installed extension's owning schema and its exact vector type OID."""
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
    row = cur.fetchone()
    return None if row is None else (str(row[0]), int(row[1]))



# Re-export the split-out whole-catalogue snapshot reader so
# schema_fingerprint's bare-name call and any external import path keep
# resolving.
# Re-export the split-out catalogue verification helpers so existing
# ``from ...runner import _matches_post_core`` /
# ``runner._EDGE_TARGET_FUNCTION_EXPRESSION`` paths and bare-name
# references in this module keep resolving.  Shared schema constants and
# data classes come from ``_constants`` (imported at the top).
from memplex.storage.migrations.catalogue_checks import (  # noqa: F401
    _EDGE_FOREIGN_KEY_SHAPES,
    _EDGE_TARGET_FUNCTION_EXPRESSION,
    _FIXED_AUDITED_ADOPTION_VARIANTS,
    _INDEX_SIGNATURES,
    _RESERVED_DOMAIN_ID_CHECK_EXPRESSION,
    _RUNTIME_VECTOR_VARIANT,
    _SYNC_COLUMN_NAMES,
    _SYNC_FUNCTION_DEFINITION_DIGESTS,
    _SYNC_INDEXES,
    _SYNC_PRIMARY_KEYS,
    _SYNC_TABLE_SIGNATURE_DIGESTS,
    _allowed_adoption_baselines,
    _background_tasks_catalogue_matches,
    _changelog_serial_default_matches,
    _column_defaults,
    _column_shape,
    _defaults_match,
    _edge_integrity_matches,
    _expression_digest,
    _feedback_columns,
    _feedback_defaults_match,
    _fingerprint_digest,
    _has_exact_indexes,
    _has_expected_core_constraints,
    _has_only_primary_key,
    _has_sequential_attnums,
    _index_matches,
    _index_signature,
    _is_audited_adoption_variant,
    _is_background_tasks_current_variant,
    _is_edge_integrity_current_variant,
    _is_reliable_sync_current_variant,
    _legacy_base_columns,
    _legacy_vector_dimension,
    _managed_table_catalogue_matches,
    _matches_capabilities,
    _matches_current_feedback,
    _matches_feedback,
    _matches_legacy_feedback,
    _matches_post_core,
    _matches_pre_core,
    _matches_runtime_feedback_v1,
    _normalise_sql,
    _policy_matches,
    _policy_matches_core,
    _post_column_shape,
    _post_core_columns,
    _post_core_defaults_match,
    _post_primary_key,
    _primary_key,
    _reliable_sync_catalogue_matches,
    _required_core_indexes,
    _reserved_domain_id_check_matches,
    _search_tsv_matches,
    _sync_function_digest,
    _sync_table_catalogue_matches,
    _sync_table_signature,
    _variant_digest,
    _vector_parameter_digest,
)
from memplex.storage.migrations.catalogue_snapshot import (  # noqa: F401,E402
    _catalog_snapshot,
)


def schema_fingerprint(
    cur: Any, *, permit_application_acl: bool = False
) -> SchemaFingerprint:
    """Classify the schema through catalogues, never by a runner-owned marker."""
    snapshot = _catalog_snapshot(cur)
    # Readiness may deliberately grant one exact business principal the
    # product's minimum table rights.  It is a deployment concern, not a
    # schema-shape variant; the caller has already checked the ACL byte-for-
    # byte through ``_verify_application_acl``.  All other callers retain the
    # original NULL-ACL fingerprint contract.
    if permit_application_acl:
        for name in _APPLICATION_ACL_TABLES:
            table = snapshot["tables"].get(name)
            if table is not None:
                table["acl_is_default"] = True
        if snapshot["changelog_sequence"] is not None:
            snapshot["changelog_sequence"]["acl_is_default"] = True
    digest = _fingerprint_digest(snapshot)
    tables = snapshot["tables"]
    if snapshot["unexpected"]:
        return SchemaFingerprint("unknown", digest, "unknown")
    visible = set(tables) - {_LEDGER_TABLE}
    if not visible:
        return SchemaFingerprint("empty", _variant_digest("empty"), "empty")
    if _matches_pre_core(tables, snapshot) and _CAPABILITIES_TABLE not in visible:
        return SchemaFingerprint(
            "pre_g002_3_2_7",
            _variant_digest("pre_g002_3_2_7"),
            "pre_g002_3_2_7",
        )
    has_reliable_sync = _reliable_sync_catalogue_matches(tables, snapshot)
    has_background_tasks = _background_tasks_catalogue_matches(tables)
    # v5 deliberately adds capture triggers to the existing business tables.
    # Earlier catalogue classifiers require no user triggers, so present a
    # trigger-free copy only while recognising the immutable G003 core.
    core_tables = tables
    if has_reliable_sync:
        capture_tables = {
            "memplex_functions",
            "memplex_edges",
            "memplex_observations",
            "memplex_facts",
            "memplex_preferences",
        }
        core_tables = {
            name: ({**table, "triggers": ()} if name in capture_tables else table)
            for name, table in tables.items()
        }
    core = _matches_post_core(core_tables, snapshot)
    if core is None:
        return SchemaFingerprint("unknown", digest, "unknown")
    layout, has_integrity_indexes, vector_dim, has_edge_integrity = core
    feedback = tables.get("feedback")
    capabilities = tables.get(_CAPABILITIES_TABLE)
    # The variant classification is feature-structured; the display name is
    # rendered once from the features at the end.
    feedback_v1 = False
    current = False
    if (
        not has_integrity_indexes
        and feedback is None
        and capabilities is None
    ):
        pass
    elif (
        layout == "runtime_v1"
        and not has_integrity_indexes
        and feedback is not None
        and _matches_runtime_feedback_v1(feedback)
        and capabilities is None
    ):
        feedback_v1 = True
    elif (
        has_integrity_indexes
        and feedback is not None
        and _matches_current_feedback(feedback)
        and capabilities is not None
        and _matches_capabilities(capabilities, snapshot["capabilities"], vector_dim=vector_dim)
    ):
        current = True
    else:
        return SchemaFingerprint("unknown", digest, "unknown")
    if has_reliable_sync:
        if not current:
            return SchemaFingerprint("unknown", digest, "unknown")
    elif any(name in tables for name in _SYNC_TABLES):
        return SchemaFingerprint("unknown", digest, "unknown")
    if has_background_tasks:
        if not has_reliable_sync:
            return SchemaFingerprint("unknown", digest, "unknown")
    elif any(name in tables for name in _TASK_TABLES):
        return SchemaFingerprint("unknown", digest, "unknown")
    features = SchemaVariantFeatures(
        layout=layout,
        feedback_v1=feedback_v1,
        current=current,
        has_edge_integrity=has_edge_integrity and current,
        has_reliable_sync=has_reliable_sync,
        has_background_tasks=has_background_tasks,
        vector_dim=vector_dim,
    )
    variant = features.display_name()
    return SchemaFingerprint("post_g002_current", _variant_digest(variant), variant, features)



# Re-export the split-out ACL verification entry points so existing
# ``from ...runner import _verify_acl_contracts`` paths keep resolving.
from memplex.storage.migrations.acl_verification import (  # noqa: F401
    _verify_acl_contracts,
    _verify_application_acl,
    _verify_ingress_acl,
)

# Re-export the split-out observed-state ledger functions so existing
# ``from ...runner import _read_ledger_if_present`` paths and the class's
# bare-name calls (and the test suite's monkeypatch of
# ``runner._read_ledger_if_present``) keep resolving.
from memplex.storage.migrations.ledger_state import (  # noqa: F401
    _plan_from_observed_state,
    _read_ledger_if_present,
    _validate_ledger,
    _validate_legacy_belongs_to_edges,
)


class PostgresMigrationRunner:
    """Read-only first, catalogue-gated PostgreSQL migration execution."""

    def __init__(self, dsn: str, connection_factory: Callable[[], Any] | None = None) -> None:
        self.dsn = dsn
        self.connection_factory = connection_factory
        self._migrations = discover_migrations()

    def _open_connection(self) -> Any:
        if self.connection_factory is not None:
            return self.connection_factory()
        try:
            import psycopg2  # type: ignore
        except ImportError as exc:
            raise ImportError(
                "PostgresMigrationRunner requires psycopg2. Install with: pip install memplex[postgres]."
            ) from exc
        return psycopg2.connect(self.dsn)

    @contextmanager
    def _short_connection(self, *, readonly: bool) -> Iterator[Any]:
        conn = self._open_connection()
        try:
            if hasattr(conn, "autocommit"):
                conn.autocommit = False
            if readonly and hasattr(conn, "set_session"):
                conn.set_session(readonly=True, autocommit=False)
            yield conn
        except BaseException:
            try:
                conn.rollback()
            except BaseException as cleanup_error:  # noqa: BLE001
                # The original catalogue/migration failure is the useful one.
                _ = cleanup_error
            try:
                conn.close()
            except BaseException as cleanup_error:  # noqa: BLE001
                _ = cleanup_error
            raise
        else:
            if readonly:
                rollback_error: BaseException | None = None
                try:
                    conn.rollback()
                except BaseException as exc:  # noqa: BLE001
                    rollback_error = exc
                try:
                    conn.close()
                except BaseException:
                    if rollback_error is None:
                        raise
                if rollback_error is not None:
                    raise rollback_error
            else:
                conn.close()

    @staticmethod
    def _close_cursor(cursor: Any) -> None:
        close = getattr(cursor, "close", None)
        if close is not None:
            close()

    @contextmanager
    def _short_cursor(self, conn: Any) -> Iterator[Any]:
        """Close one cursor while preserving a primary catalogue/DDL failure."""
        cursor = conn.cursor()
        try:
            yield cursor
        except BaseException:
            try:
                self._close_cursor(cursor)
            except BaseException as cleanup_error:  # noqa: BLE001
                _ = cleanup_error
            raise
        else:
            self._close_cursor(cursor)

    def inspect_target(self) -> PostgresTargetIdentity:
        """Inspect one connection's resolved server, database, and schema identity."""
        with (
            self._short_connection(readonly=True) as conn,
            self._short_cursor(conn) as cur,
        ):
            return _verify_target_identity(conn, cur, None)

    def inspect_application_principal(
        self, *, expected_target: PostgresTargetIdentity | None = None
    ) -> PostgresApplicationPrincipal:
        """Read target and session identity together from an app connection."""
        with (
            self._short_connection(readonly=True) as conn,
            self._short_cursor(conn) as cur,
        ):
            _verify_target_identity(conn, cur, expected_target)
            return _read_application_principal(cur)

    def inspect_restore_target(self) -> PostgresTargetIdentity:
        """Inspect target identity, tolerating an absent schema before a restore."""
        with (
            self._short_connection(readonly=True) as conn,
            self._short_cursor(conn) as cur,
        ):
            return _verify_target_identity(
                conn, cur, None, inspector=inspect_postgres_restore_connection_target
            )

    def inspect_restore_application_principal(
        self, *, expected_target: PostgresTargetIdentity | None = None
    ) -> PostgresApplicationPrincipal:
        """Read the application principal, tolerating an absent schema pre-restore."""
        with (
            self._short_connection(readonly=True) as conn,
            self._short_cursor(conn) as cur,
        ):
            _verify_target_identity(
                conn,
                cur,
                expected_target,
                inspector=inspect_postgres_restore_connection_target,
            )
            return _read_application_principal(cur)

    def plan(
        self,
        *,
        expected_target: PostgresTargetIdentity | None = None,
        application_acl: ApplicationAclContract | None = None,
        ingress_acl: IngressAclContract | None = None,
        deployment_profile: str | None = None,
    ) -> MigrationPlan:
        with (
            self._short_connection(readonly=True) as conn,
            self._short_cursor(conn) as cur,
        ):
            if expected_target is not None:
                _verify_target_identity(conn, cur, expected_target)
            _confine_to_current_schema(cur)
            application_acl_permitted = _verify_acl_contracts(
                cur,
                application_acl=application_acl,
                ingress_acl=ingress_acl,
                profile=deployment_profile,
            )
            return _plan_from_observed_state(
                _read_ledger_if_present(cur),
                schema_fingerprint(
                    cur, permit_application_acl=application_acl_permitted
                ),
                self._migrations,
            )

    def status(
        self,
        *,
        expected_target: PostgresTargetIdentity | None = None,
        application_acl: ApplicationAclContract | None = None,
        ingress_acl: IngressAclContract | None = None,
        deployment_profile: str | None = None,
    ) -> MigrationPlan:
        return self.plan(
            expected_target=expected_target,
            application_acl=application_acl,
            ingress_acl=ingress_acl,
            deployment_profile=deployment_profile,
        )

    def _create_ledger(self, cur: Any) -> None:
        cur.execute(
            """
            CREATE TABLE memplex_schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                checksum TEXT NOT NULL,
                applied_at TIMESTAMPTZ NOT NULL,
                execution_mode TEXT NOT NULL,
                baseline_fingerprint TEXT
            )
            """
        )

    def _insert_ledger_row(
        self,
        cur: Any,
        migration: Migration,
        execution_mode: Literal["executed", "adopted"],
        baseline_fingerprint: str | None = None,
    ) -> None:
        cur.execute(
            """
            INSERT INTO memplex_schema_migrations
                (version, name, checksum, applied_at, execution_mode, baseline_fingerprint)
            VALUES (%s, %s, %s, CURRENT_TIMESTAMP, %s, %s)
            """,
            (migration.version, migration.name, migration.checksum, execution_mode, baseline_fingerprint),
        )

    @staticmethod
    def _record_vector_capability(cur: Any, dimension: int) -> str:
        digest = _vector_parameter_digest(dimension)
        cur.execute(
            "SELECT parameter_digest FROM memplex_schema_capabilities WHERE capability_name = %s",
            ("pgvector_embedding",),
        )
        existing = cur.fetchone()
        if existing is None:
            cur.execute(
                """
                INSERT INTO memplex_schema_capabilities
                    (capability_name, parameter_digest, applied_at)
                VALUES (%s, %s, CURRENT_TIMESTAMP)
                """,
                ("pgvector_embedding", digest),
            )
        elif existing[0] != digest:
            raise MigrationIntegrityError("vector capability parameter digest does not match")
        return digest

    def _execute_pre_g002_baseline(self, cur: Any) -> None:
        """Fill only the two tables absent from the pinned 3.2.7 catalogue."""
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS memplex_facts (
                id TEXT PRIMARY KEY,
                data JSONB NOT NULL,
                updated_at TIMESTAMPTZ
            )
            """
        )
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS memplex_preferences (
                id TEXT PRIMARY KEY,
                data JSONB NOT NULL,
                updated_at TIMESTAMPTZ
            )
            """
        )

    def _apply_plan_in_current_transaction(
        self, cur: Any, entries: tuple[_LedgerEntry, ...], fingerprint: SchemaFingerprint
    ) -> None:
        if not entries:
            self._create_ledger(cur)
            if fingerprint.kind == "post_g002_current":
                if not _is_audited_adoption_variant(fingerprint.variant):
                    raise MigrationIntegrityError("unrecognised legacy schema")
                self._insert_ledger_row(cur, self._migrations[0], "adopted", fingerprint.digest)
                self._insert_ledger_row(cur, self._migrations[1], "adopted", fingerprint.digest)
                entries = _read_ledger_if_present(cur)
            else:
                entries = ()
        applied_versions = {entry.version for entry in entries}
        for migration in self._migrations:
            if migration.version in applied_versions:
                continue
            if migration.version == 1 and fingerprint.kind == "pre_g002_3_2_7":
                self._execute_pre_g002_baseline(cur)
            else:
                if migration.version == 4:
                    _validate_legacy_belongs_to_edges(cur)
                cur.execute(_body_for_execution(migration).decode("utf-8"))
            self._insert_ledger_row(cur, migration, "executed")
        legacy_vector_dimension = _legacy_vector_dimension(fingerprint.variant)
        if legacy_vector_dimension is not None:
            self._record_vector_capability(cur, legacy_vector_dimension)

    def _locked_plan_and_apply(
        self,
        cur: Any,
        *,
        application_acl: ApplicationAclContract | None = None,
        ingress_acl: IngressAclContract | None = None,
    ) -> MigrationPlan:
        if application_acl is not None and type(application_acl) is not ApplicationAclContract:
            raise TypeError("application ACL contract must be exact ApplicationAclContract")
        if ingress_acl is not None and type(ingress_acl) is not IngressAclContract:
            raise TypeError("ingress ACL contract must be exact IngressAclContract")
        _confine_to_current_schema(cur)
        cur.execute("SELECT pg_advisory_xact_lock(%s::bigint)", (_MIGRATION_LOCK_KEY,))
        entries = _read_ledger_if_present(cur)
        # Applying immutable DDL must work before an operator grants the
        # application role.  Exact ACL validation is intentionally deferred
        # to the fresh readiness readback.
        fingerprint = schema_fingerprint(
            cur, permit_application_acl=application_acl is not None or ingress_acl is not None
        )
        _plan_from_observed_state(entries, fingerprint, self._migrations)
        self._apply_plan_in_current_transaction(cur, entries, fingerprint)
        final = _plan_from_observed_state(
            _read_ledger_if_present(cur),
            schema_fingerprint(
                cur, permit_application_acl=application_acl is not None or ingress_acl is not None
            ),
            self._migrations,
        )
        if final.state != "ready":
            raise MigrationIntegrityError("migration application did not converge")
        return final

    def _verify_vector_capability_convergence(
        self,
        cur: Any,
        dimension: int,
        digest: str,
        *,
        application_acl: ApplicationAclContract | None = None,
        ingress_acl: IngressAclContract | None = None,
    ) -> None:
        """Prove the capability row and embedding survived as one ready catalogue."""
        fingerprint = schema_fingerprint(
            cur, permit_application_acl=application_acl is not None or ingress_acl is not None
        )
        final = _plan_from_observed_state(
            _read_ledger_if_present(cur), fingerprint, self._migrations
        )
        features = fingerprint.features
        if (
            digest != _vector_parameter_digest(dimension)
            or final.state != "ready"
            or fingerprint.kind != "post_g002_current"
            or features is None
            or not features.current
            or features.vector_dim != dimension
        ):
            raise MigrationIntegrityError("vector capability did not converge")

    def apply(
        self,
        dry_run: bool = False,
        *,
        expected_target: PostgresTargetIdentity | None = None,
        application_acl: ApplicationAclContract | None = None,
        ingress_acl: IngressAclContract | None = None,
        deployment_profile: str | None = None,
    ) -> MigrationPlan:
        if dry_run:
            return self.plan(
                expected_target=expected_target,
                application_acl=application_acl,
                ingress_acl=ingress_acl,
                deployment_profile=deployment_profile,
            )
        conn = self._open_connection()
        try:
            if hasattr(conn, "autocommit"):
                conn.autocommit = False
            cur = conn.cursor()
            try:
                if expected_target is not None:
                    _verify_target_identity(conn, cur, expected_target)
                final = self._locked_plan_and_apply(
                    cur, application_acl=application_acl, ingress_acl=ingress_acl
                )
            except BaseException:
                try:
                    self._close_cursor(cur)
                except BaseException as cleanup_error:  # noqa: BLE001
                    _ = cleanup_error
                raise
            self._close_cursor(cur)
            conn.commit()
        except BaseException:
            try:
                conn.rollback()
            except BaseException as cleanup_error:  # noqa: BLE001
                _ = cleanup_error
            try:
                conn.close()
            except BaseException as cleanup_error:  # noqa: BLE001
                _ = cleanup_error
            raise
        else:
            try:
                conn.close()
            except BaseException as cleanup_error:  # noqa: BLE001
                # Commit has made ``final`` authoritative.  Close is best effort.
                _ = cleanup_error
            return final

    def ensure_vector_capability(
        self,
        request: VectorCapabilityRequest,
        deployment_profile: str,
        *,
        expected_target: PostgresTargetIdentity | None = None,
        application_acl: ApplicationAclContract | None = None,
        ingress_acl: IngressAclContract | None = None,
    ) -> VectorCapabilityStatus:
        """Apply/verify pgvector in the same locked transaction as migrations."""
        profile = deployment_profile.strip().lower() if type(deployment_profile) is str else ""
        if profile not in {"development", "production"}:
            raise ValueError("vector capability requires development or production profile")
        if request.policy == "disabled":
            if expected_target is not None and type(expected_target) is not PostgresTargetIdentity:
                raise MigrationIntegrityError("PostgreSQL target identity does not match expected target")
            return VectorCapabilityStatus(state="disabled", dim=request.dim)
        with (
            self._short_connection(readonly=False) as conn,
            self._short_cursor(conn) as cur,
        ):
            if expected_target is not None:
                _verify_target_identity(conn, cur, expected_target)
            if application_acl is not None or ingress_acl is not None:
                _confine_to_current_schema(cur)
                _verify_acl_contracts(
                    cur,
                    application_acl=application_acl,
                    ingress_acl=ingress_acl,
                    profile=profile,
                )
            self._locked_plan_and_apply(
                cur, application_acl=application_acl, ingress_acl=ingress_acl
            )
            extension = _vector_extension_type(cur)
            if extension is None:
                cur.execute(
                    "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_available_extensions WHERE name = 'vector')"
                )
                available = bool(cur.fetchone()[0])
                if not available:
                    if request.policy == "required":
                        raise MigrationIntegrityError("required vector capability is unavailable")
                    conn.commit()
                    return VectorCapabilityStatus(state="degraded", dim=request.dim)
                cur.execute("SAVEPOINT memplex_vector_capability")
                try:
                    cur.execute("CREATE EXTENSION IF NOT EXISTS vector")
                except BaseException:  # noqa: BLE001
                    cur.execute("ROLLBACK TO SAVEPOINT memplex_vector_capability")
                    cur.execute("RELEASE SAVEPOINT memplex_vector_capability")
                    if request.policy == "required":
                        raise MigrationIntegrityError("required vector capability is unavailable")
                    conn.commit()
                    return VectorCapabilityStatus(state="degraded", dim=request.dim)
                else:
                    cur.execute("RELEASE SAVEPOINT memplex_vector_capability")
                extension = _vector_extension_type(cur)
            if extension is None:
                if request.policy == "required":
                    raise MigrationIntegrityError("required vector capability is unavailable")
                conn.commit()
                return VectorCapabilityStatus(state="degraded", dim=request.dim)
            extension_schema, vector_type_oid = extension
            cur.execute(
                """
                SELECT a.atttypid, a.atttypmod,
                       pg_catalog.format_type(a.atttypid, a.atttypmod)
                FROM pg_catalog.pg_attribute AS a
                WHERE a.attrelid = 'memplex_functions'::regclass
                  AND a.attname = 'embedding' AND NOT a.attisdropped
                """
            )
            row = cur.fetchone()
            if row is None:
                type_reference = f"{_quote_identifier(extension_schema)}.vector({request.dim})"
                cur.execute(
                    f"ALTER TABLE memplex_functions ADD COLUMN embedding {type_reference}"
                )
                cur.execute(
                    """
                    SELECT a.atttypid, a.atttypmod,
                           pg_catalog.format_type(a.atttypid, a.atttypmod)
                    FROM pg_catalog.pg_attribute AS a
                    WHERE a.attrelid = 'memplex_functions'::regclass
                      AND a.attname = 'embedding' AND NOT a.attisdropped
                    """
                )
                row = cur.fetchone()
            if row is None or int(row[0]) != vector_type_oid or int(row[1]) != request.dim:
                raise MigrationIntegrityError("vector capability has an unrecognised shape")
            digest = self._record_vector_capability(cur, request.dim)
            self._verify_vector_capability_convergence(
                cur,
                request.dim,
                digest,
                application_acl=application_acl,
                ingress_acl=ingress_acl,
            )
            conn.commit()
            return VectorCapabilityStatus(state="ready", dim=request.dim, parameter_digest=digest)

    def verify_storage_readiness(
        self,
        request: VectorCapabilityRequest,
        deployment_profile: str,
        *,
        expected_target: PostgresTargetIdentity,
        application_acl: ApplicationAclContract | None = None,
        ingress_acl: IngressAclContract | None = None,
    ) -> VectorCapabilityStatus:
        """Read back the exact catalogue a migration attempt left behind.

        This deliberately opens a new read-only connection.  The mutating
        migration/capability result is provisional; only this independent
        catalogue and ledger observation is suitable for publishing a
        business-pool capability.
        """
        if type(request) is not VectorCapabilityRequest:
            raise TypeError("storage readiness requires an exact VectorCapabilityRequest")
        if type(expected_target) is not PostgresTargetIdentity:
            raise TypeError("storage readiness requires an exact PostgresTargetIdentity")
        profile = deployment_profile.strip().lower() if type(deployment_profile) is str else ""
        if profile not in {"development", "production"}:
            raise ValueError("storage readiness requires development or production profile")
        if request.dim == 0:
            if request.policy != "disabled":
                raise ValueError("vector dim=0 requires disabled policy")
        elif request.policy != ("required" if profile == "production" else "best_effort"):
            raise ValueError("vector policy is inconsistent with deployment profile")

        with (
            self._short_connection(readonly=True) as conn,
            self._short_cursor(conn) as cur,
        ):
            _verify_target_identity(conn, cur, expected_target)
            _confine_to_current_schema(cur)
            application_acl_permitted = _verify_acl_contracts(
                cur,
                application_acl=application_acl,
                ingress_acl=ingress_acl,
                profile=profile,
            )
            fingerprint = schema_fingerprint(
                cur, permit_application_acl=application_acl_permitted
            )
            plan = _plan_from_observed_state(
                _read_ledger_if_present(cur), fingerprint, self._migrations
            )
            if plan.state != "ready" or fingerprint.kind != "post_g002_current":
                raise MigrationIntegrityError("PostgreSQL storage catalogue is not ready")

            if request.policy == "disabled":
                return VectorCapabilityStatus(state="disabled", dim=0)

            cur.execute(
                """
                SELECT capability_name, parameter_digest
                FROM memplex_schema_capabilities
                ORDER BY capability_name
                """
            )
            capabilities = tuple((str(name), str(digest)) for name, digest in cur.fetchall())
            expected_digest = _vector_parameter_digest(request.dim)
            features = fingerprint.features
            if (
                features is not None
                and features.current
                and features.vector_dim == request.dim
                and capabilities == (("pgvector_embedding", expected_digest),)
            ):
                return VectorCapabilityStatus(
                    state="ready",
                    dim=request.dim,
                    parameter_digest=expected_digest,
                )
            if (
                profile == "development"
                and request.policy == "best_effort"
                and features is not None
                and features.current
                and features.vector_dim is None
                and capabilities == ()
            ):
                return VectorCapabilityStatus(state="degraded", dim=request.dim)
            raise MigrationIntegrityError("PostgreSQL vector capability is not ready")
