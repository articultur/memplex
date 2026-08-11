"""Discovery and immutable-content checks for packaged PostgreSQL migrations.

Applying migrations and inspecting database fingerprints deliberately arrive in
G003 Task 2.  Task 1 keeps this module side-effect free so it can validate
resources from an installed wheel without opening a PostgreSQL connection.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from hashlib import sha256
from importlib import resources
from typing import Any, Final, Literal

from memplex.models import domain_node_id

_APPROVED_0002_CHECKSUM = "8e932e605e4eb36f6ec410c5a589001133a2db29ba902ff34227ae0a223e9a16"
_MIGRATION_NAME = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z][a-z0-9_]*)\.sql$")
_SQL_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

# Deliberately fixed, reviewed once, and kept independent of schema/migration
# names.  This is a signed PostgreSQL bigint accepted by pg_advisory_xact_lock.
_MIGRATION_LOCK_KEY: Final[int] = -4_710_001_234_567_890_123
_LEDGER_TABLE: Final[str] = "memplex_schema_migrations"
_CAPABILITIES_TABLE: Final[str] = "memplex_schema_capabilities"
_MAX_VECTOR_DIM: Final[int] = 16_000
_CORE_TABLES: Final[tuple[str, ...]] = (
    "memplex_functions",
    "memplex_edges",
    "memplex_observations",
    "memplex_facts",
    "memplex_preferences",
    "memplex_changelog",
)
_SYNC_TABLES: Final[tuple[str, ...]] = (
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
)
_LEGACY_CORE_TABLES: Final[tuple[str, ...]] = (
    "memplex_functions",
    "memplex_edges",
    "memplex_observations",
    "memplex_changelog",
)
_MANAGED_TABLES: Final[tuple[str, ...]] = (
    *_CORE_TABLES,
    *_SYNC_TABLES,
    "feedback",
    _CAPABILITIES_TABLE,
    _LEDGER_TABLE,
)
_APPLICATION_ACL_TABLES: Final[tuple[str, ...]] = (*_CORE_TABLES, "feedback", *_SYNC_TABLES)
_APPLICATION_ACL: Final[dict[str, frozenset[str]]] = {
    "memplex_functions": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "memplex_edges": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "memplex_observations": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "memplex_facts": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "memplex_preferences": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "memplex_changelog": frozenset({"SELECT", "INSERT", "DELETE"}),
    "feedback": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "memplex_sync_outbox": frozenset({"SELECT", "INSERT", "DELETE"}),
    "memplex_sync_entity_versions": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "memplex_sync_inbox": frozenset({"SELECT", "INSERT"}),
    "memplex_sync_batches": frozenset({"SELECT", "INSERT"}),
    "memplex_sync_targets": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "memplex_sync_deliveries": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "memplex_sync_cursors": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "memplex_sync_stream_state": frozenset({"SELECT", "INSERT", "UPDATE"}),
    "memplex_sync_local_identity": frozenset(),
    "memplex_sync_ingress_principals": frozenset(),
    "memplex_sync_snapshots": frozenset({"SELECT", "INSERT", "UPDATE", "DELETE"}),
    "memplex_sync_snapshot_items": frozenset({"SELECT", "INSERT", "DELETE"}),
}
_APPLICATION_ACL_FUNCTIONS: Final[tuple[str, ...]] = (
    "memplex_sync_capture_before",
    "memplex_sync_capture_local_change",
    "memplex_sync_assert_delivery_quota",
    "memplex_sync_snapshot_admission_counts",
    "memplex_sync_compact",
)
_SYNC_FUNCTIONS: Final[tuple[str, ...]] = (
    "memplex_configure_sync_local_identity",
    "memplex_sync_assert_delivery_quota",
    "memplex_sync_snapshot_admission_counts",
    "memplex_sync_compact",
    "memplex_sync_capture_before",
    "memplex_sync_capture_local_change",
    "memplex_configure_sync_ingress_principal",
    "memplex_sync_apply_inbound",
    "memplex_sync_require_canonical_entity_key",
    "memplex_sync_require_canonical_version",
    "memplex_sync_encode_string_array",
)
_ACL_COLUMNS: Final[tuple[str, ...]] = (
    "tenant_id",
    "owner_subject",
    "workspace",
    "visibility",
    "source_agent",
    "source_session",
)

# ``pg_class`` is the authoritative managed-object namespace. PostgreSQL
# represents primary-key backing indexes and BIGSERIAL sequences there too, so
# scanning only ordinary relations would let a memplex-named object hide behind
# an ignored relkind.
_KNOWN_MEMPLEX_RELATION_KINDS: Final[dict[str, str]] = {
    **{name: "r" for name in (*_CORE_TABLES, _CAPABILITIES_TABLE, _LEDGER_TABLE)},
    **{name: "r" for name in _SYNC_TABLES},
    **{f"{name}_pkey": "i" for name in (*_CORE_TABLES, _CAPABILITIES_TABLE, _LEDGER_TABLE)},
    "memplex_changelog_id_seq": "S",
    "memplex_sync_local_identity_pkey": "i",
    "memplex_sync_local_identity_node_id_key": "i",
    "memplex_sync_ingress_principals_pkey": "i",
    "memplex_functions_tenant_updated_idx": "i",
    "memplex_functions_tenant_idx": "i",
    "memplex_edges_tenant_source_type_target_idx": "i",
    "memplex_edges_tenant_target_type_source_idx": "i",
    "memplex_edges_tenant_idx": "i",
    "memplex_edges_tenant_target_function_idx": "i",
    "memplex_observations_tenant_idx": "i",
    "memplex_facts_tenant_idx": "i",
    "memplex_preferences_tenant_idx": "i",
    "memplex_changelog_tenant_idx": "i",
    "memplex_functions_workspace_normalized_name_key": "i",
    "memplex_functions_user_normalized_name_key": "i",
    "memplex_functions_session_normalized_name_key": "i",
    **{f"{name}_pkey": "i" for name in _SYNC_TABLES},
    "memplex_sync_targets_tenant_id_remote_node_id_key": "i",
    "memplex_sync_snapshots_tenant_id_remote_id_consumer_id_requ_key": "i",
    "memplex_sync_outbox_tenant_id_origin_node_id_event_id_key": "i",
    "memplex_sync_outbox_stream_seq_seq": "S",
    "memplex_sync_outbox_tenant_stream_idx": "i",
    "memplex_sync_deliveries_claim_idx": "i",
    "memplex_sync_deliveries_retention_idx": "i",
    "memplex_sync_cursors_tenant_after_idx": "i",
    "memplex_sync_snapshots_expiry_idx": "i",
}

_CORE_POLICY_DIGESTS: Final[tuple[str, str]] = (
    "966aa3ee5c0224eafbba062f2bfee28f4a34b0f0f86f9ee23b72e6f62d08de0d",
    "3f1fe3be69457e86e5c3f595d60141abcd127e987d5a7d01eab84f871c0d4493",
)
_FEEDBACK_CURRENT_POLICY_DIGESTS: Final[tuple[str, str]] = (
    "5b5ddc963fccdcb941c7eccbcb646b053ef079d7bf47272964fc184891152025",
    "f2c2439d4d159bdc189dc2cd46d116b69799483dd879af36d05f9b03ee98e679",
)
_FEEDBACK_RUNTIME_V1_POLICY_DIGESTS: Final[tuple[str, str]] = (
    "dfa495e062435c70a0cbd2e3b971cdce89abc48a19b7a215897ad7b7fe03e5c2",
    "8c8dd0553aab40d8e7c042fd759fa97ded54317596a291407d6a864fda2cb5c6",
)
_SEARCH_TSV_GENERATION_DIGEST: Final[str] = (
    "542630b9761cfc19062f8e4c97e4b75cdfabc0f715170d779c5d33c0ec6968d7"
)


class MigrationIntegrityError(RuntimeError):
    """Raised when versioned SQL resources cannot be safely trusted."""


@dataclass(frozen=True, slots=True)
class Migration:
    """One immutable SQL resource discovered from the installed package."""

    version: int
    name: str
    sql_bytes: bytes
    checksum: str


@dataclass(frozen=True, slots=True)
class MigrationPlan:
    """Read-only migration status returned by the Task 2 runner state machine."""

    current_version: int
    known_version: int
    pending: tuple[Migration, ...]
    state: Literal["ready", "upgrade_required", "blocked"]


@dataclass(frozen=True, slots=True)
class SchemaFingerprint:
    """Canonical, catalogue-derived classification of the current schema."""

    kind: Literal["empty", "pre_g002_3_2_7", "post_g002_current", "unknown"]
    digest: str
    variant: str = ""


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
class ApplicationAclContract:
    """Data-only authorization allowance for one exact application role."""

    role: str

    def __post_init__(self) -> None:
        if type(self.role) is not str or not self.role:
            raise TypeError("application ACL role must be an exact non-empty str")


@dataclass(frozen=True, slots=True)
class IngressAclContract:
    """Exact relation-less LOGIN principal allowed to submit inbound batches."""

    role: str

    def __post_init__(self) -> None:
        if type(self.role) is not str or not self.role:
            raise TypeError("ingress ACL role must be an exact non-empty str")


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


@dataclass(frozen=True, slots=True)
class _LedgerEntry:
    version: int
    name: str
    checksum: str
    execution_mode: str
    baseline_fingerprint: str | None


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


def _normalise_sql(value: str | None) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower()


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


def _verify_target_identity(
    conn: Any,
    cur: Any,
    expected_target: PostgresTargetIdentity | None,
) -> PostgresTargetIdentity:
    actual_target = inspect_postgres_connection_target(conn, cur)
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


def _catalog_snapshot(cur: Any) -> dict[str, Any]:
    """Read all managed catalogue facts without issuing DDL or table locks."""
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
        tables[name] = {
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
    return variant.endswith("_reliable_sync_v5") or "_reliable_sync_v5_vector_" in variant


def _allowed_adoption_baselines(current_variant: str) -> frozenset[str]:
    """Bind adopted rows to the exact current whole-schema classifier.

    A final runtime feedback layout cannot show whether feedback already existed
    before 0003, so only its two documented runtime ancestors are accepted.
    """
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
    if (
        not has_integrity_indexes
        and feedback is None
        and capabilities is None
    ):
        variant = f"post_g002_{layout}"
        if vector_dim is not None:
            variant = f"{variant}_vector_{vector_dim}"
    elif (
        layout == "runtime_v1"
        and not has_integrity_indexes
        and feedback is not None
        and _matches_runtime_feedback_v1(feedback)
        and capabilities is None
    ):
        variant = "post_g002_runtime_v1_feedback_v1"
        if vector_dim is not None:
            variant = f"{variant}_vector_{vector_dim}"
    elif (
        has_integrity_indexes
        and feedback is not None
        and _matches_current_feedback(feedback)
        and capabilities is not None
        and _matches_capabilities(capabilities, snapshot["capabilities"], vector_dim=vector_dim)
    ):
        variant = f"post_g002_{layout}"
        if has_edge_integrity:
            variant = f"{variant}_edge_integrity"
        variant = f"{variant}_current"
        if vector_dim is not None:
            variant = f"{variant}_vector_{vector_dim}"
    else:
        return SchemaFingerprint("unknown", digest, "unknown")
    if has_reliable_sync:
        if not variant.endswith("_current") and "_current_vector_" not in variant:
            return SchemaFingerprint("unknown", digest, "unknown")
        variant = (
            variant.replace("_vector_", "_reliable_sync_v5_vector_", 1)
            if "_vector_" in variant
            else f"{variant}_reliable_sync_v5"
        )
    elif any(name in tables for name in _SYNC_TABLES):
        return SchemaFingerprint("unknown", digest, "unknown")
    return SchemaFingerprint("post_g002_current", _variant_digest(variant), variant)


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
        if 2 <= current < 4 and _is_edge_integrity_current_variant(fingerprint.variant):
            raise MigrationIntegrityError("migration ledger does not match the schema")
        if 4 <= current < 5 and _is_reliable_sync_current_variant(fingerprint.variant):
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
            except BaseException as error:
                if cleanup_error is None:
                    cleanup_error = error
        if primary_error is None and cleanup_error is not None:
            raise cleanup_error


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
        if (
            digest != _vector_parameter_digest(dimension)
            or final.state != "ready"
            or fingerprint.kind != "post_g002_current"
            or not (
                fingerprint.variant.endswith(f"_current_vector_{dimension}")
                or fingerprint.variant.endswith(f"_reliable_sync_v5_vector_{dimension}")
            )
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
            if (
                (
                    fingerprint.variant.endswith(f"_current_vector_{request.dim}")
                    or fingerprint.variant.endswith(f"_reliable_sync_v5_vector_{request.dim}")
                )
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
                and (
                    fingerprint.variant.endswith("_current")
                    or fingerprint.variant.endswith("_reliable_sync_v5")
                )
                and "_vector_" not in fingerprint.variant
                and capabilities == ()
            ):
                return VectorCapabilityStatus(state="degraded", dim=request.dim)
            raise MigrationIntegrityError("PostgreSQL vector capability is not ready")
