"""Fail-closed PostgreSQL logical backup execution."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from memplex.backup import (
    BackupArtifactWriter,
    BackupConfigurationError,
    BackupIntegrityError,
    BackupManifest,
    PitrReadiness,
    RestoreResult,
    load_verified_backup_manifest,
    open_verified_backup_artifact,
)

from .migrations import (
    ApplicationAclContract,
    IngressAclContract,
    PostgresMigrationRunner,
    PostgresTargetIdentity,
    inspect_postgres_connection_target,
)

_VERSION_RE = re.compile(r"^(\d+)(?:\.(\d+))?")
_TOOL_VERSION_RE = re.compile(r"\(PostgreSQL\)\s+(\d+(?:\.\d+){0,2})")
_LIBPQ_ENVIRONMENT = {
    "application_name": "PGAPPNAME",
    "channel_binding": "PGCHANNELBINDING",
    "connect_timeout": "PGCONNECT_TIMEOUT",
    "dbname": "PGDATABASE",
    "gssencmode": "PGGSSENCMODE",
    "host": "PGHOST",
    "hostaddr": "PGHOSTADDR",
    "krbsrvname": "PGKRBSRVNAME",
    "options": "PGOPTIONS",
    "passfile": "PGPASSFILE",
    "password": "PGPASSWORD",
    "port": "PGPORT",
    "requirepeer": "PGREQUIREPEER",
    "service": "PGSERVICE",
    "servicefile": "PGSERVICEFILE",
    "sslcert": "PGSSLCERT",
    "sslcrl": "PGSSLCRL",
    "sslkey": "PGSSLKEY",
    "sslmode": "PGSSLMODE",
    "sslpassword": "PGSSLPASSWORD",
    "sslrootcert": "PGSSLROOTCERT",
    "target_session_attrs": "PGTARGETSESSIONATTRS",
    "user": "PGUSER",
}


def inspect_pitr_readiness(
    migration_dsn: str,
    *,
    connection_factory: Callable[[str], Any] | None = None,
) -> PitrReadiness:
    """Read PostgreSQL PITR prerequisites without changing server settings."""
    _libpq_environment(migration_dsn)
    try:
        if connection_factory is not None:
            connection = connection_factory(migration_dsn)
        else:
            import psycopg2  # type: ignore

            connection = psycopg2.connect(migration_dsn)
    except Exception as exc:
        raise BackupConfigurationError("postgres_pitr_inspection_unavailable") from exc
    cursor = None
    primary: BaseException | None = None
    try:
        if hasattr(connection, "autocommit"):
            connection.autocommit = False
        if hasattr(connection, "set_session"):
            connection.set_session(readonly=True, autocommit=False)
        cursor = connection.cursor()
        cursor.execute(
            """
            SELECT current_setting('wal_level'),
                   current_setting('archive_mode'),
                   current_setting('archive_command'),
                   current_setting('full_page_writes'),
                   current_setting('max_wal_senders')
            """
        )
        row = cursor.fetchone()
        if (
            row is None
            or len(row) != 5
            or any(type(value) is not str for value in row)
        ):
            raise BackupIntegrityError("postgres_pitr_settings_invalid")
        try:
            max_wal_senders = int(row[4])
        except (TypeError, ValueError, OverflowError) as exc:
            raise BackupIntegrityError("postgres_pitr_settings_invalid") from exc
        archive_command = row[2].strip()
        archive_configured = bool(archive_command) and archive_command != "(disabled)"
        ready = (
            row[0] in {"replica", "logical"}
            and row[1] in {"on", "always"}
            and archive_configured
            and row[3] == "on"
            and max_wal_senders > 0
        )
        return PitrReadiness(
            ready=ready,
            wal_level=row[0],
            archive_mode=row[1],
            archive_command_configured=archive_configured,
            full_page_writes=row[3] == "on",
            max_wal_senders=max_wal_senders,
        )
    except BaseException as exc:
        primary = exc
        raise
    finally:
        if cursor is not None:
            try:
                cursor.close()
            except BaseException:
                if primary is None:
                    raise
        try:
            connection.rollback()
        except BaseException:
            if primary is None:
                raise
        try:
            connection.close()
        except BaseException:
            if primary is None:
                raise


def _fixed_error(code: str, cause: BaseException | None = None) -> BackupIntegrityError:
    error = BackupIntegrityError(code)
    if cause is not None:
        error.__cause__ = cause
    return error


def _parse_major(version: str) -> int:
    if type(version) is not str:
        raise BackupIntegrityError("postgres_version_invalid")
    match = _VERSION_RE.match(version)
    if match is None:
        raise BackupIntegrityError("postgres_version_invalid")
    return int(match.group(1))


def _libpq_environment(dsn: str) -> dict[str, str]:
    """Translate a DSN into an allow-listed, non-inherited libpq environment."""
    if type(dsn) is not str or not dsn:
        raise BackupConfigurationError("postgres_backup_dsn_invalid")
    try:
        try:
            from psycopg2.extensions import parse_dsn  # type: ignore
        except ImportError:
            parsed = urlsplit(dsn)
            if parsed.scheme not in {"postgres", "postgresql"} or not parsed.path:
                raise ValueError("unsupported PostgreSQL DSN")
            parameters = dict(parse_qsl(parsed.query, keep_blank_values=True))
            parameters.update(
                {
                    "dbname": unquote(parsed.path.removeprefix("/")),
                    "host": parsed.hostname or "",
                }
            )
            if parsed.username is not None:
                parameters["user"] = unquote(parsed.username)
            if parsed.password is not None:
                parameters["password"] = unquote(parsed.password)
            if parsed.port is not None:
                parameters["port"] = str(parsed.port)
        else:
            parameters = parse_dsn(dsn)
    except Exception as exc:
        raise BackupConfigurationError("postgres_backup_dsn_invalid") from exc
    environment = {"LANG": "C", "LC_ALL": "C"}
    for source, destination in _LIBPQ_ENVIRONMENT.items():
        value = parameters.get(source)
        if value is not None:
            environment[destination] = str(value)
    return environment


def _discover_tool_version(path: Path, name: str) -> str:
    try:
        completed = subprocess.run(
            (str(path), "--version"),
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            env={"LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise BackupConfigurationError("postgres_client_tools_invalid") from exc
    match = _TOOL_VERSION_RE.search(completed.stdout or "")
    if completed.returncode != 0 or match is None or name not in path.name:
        raise BackupConfigurationError("postgres_client_tools_invalid")
    return match.group(1)


@dataclass(frozen=True, slots=True)
class PostgresClientTools:
    pg_dump: Path
    pg_restore: Path
    pg_dump_version: str
    pg_restore_version: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.pg_dump, Path)
            or not isinstance(self.pg_restore, Path)
            or type(self.pg_dump_version) is not str
            or not self.pg_dump_version
            or type(self.pg_restore_version) is not str
            or not self.pg_restore_version
        ):
            raise BackupConfigurationError("postgres_client_tools_invalid")

    @classmethod
    def discover(cls) -> PostgresClientTools:
        dump = shutil.which("pg_dump")
        restore = shutil.which("pg_restore")
        if dump is None or restore is None:
            raise BackupConfigurationError("postgres_client_tools_missing")
        dump_path = Path(dump).resolve()
        restore_path = Path(restore).resolve()
        return cls(
            pg_dump=dump_path,
            pg_restore=restore_path,
            pg_dump_version=_discover_tool_version(dump_path, "pg_dump"),
            pg_restore_version=_discover_tool_version(restore_path, "pg_restore"),
        )


def _approved_restore_toc(text: str, schema: str) -> tuple[str, str]:
    """Constrain trusted first-party TOC objects, never parse/rewrite dump SQL."""
    from memplex.storage.migrations._constants import (
        _KNOWN_MEMPLEX_RELATION_KINDS,
        _MANAGED_TABLES,
        _SOURCE_READ_GRANT_TABLES,
        _SYNC_FUNCTIONS,
    )
    relations = set(_KNOWN_MEMPLEX_RELATION_KINDS) | {'fts_functions_idx', 'feedback', 'feedback_pkey',
                                                   'feedback_tenant_memory_idx'}
    functions = set(_SYNC_FUNCTIONS)
    constraints = relations | {'memplex_edges_source_function_fk', 'memplex_edges_target_function_fk',
                              'memplex_source_heads_authority_fk', 'memplex_derivation_records_authority_fk',
                              'memplex_sync_deliveries_tenant_id_target_id_fkey',
                              'memplex_sync_deliveries_tenant_id_stream_seq_fkey',
                              'memplex_sync_snapshot_items_tenant_id_snapshot_id_fkey'}
    triggers = {'memplex_sync_' + table + '_' + stage
                for table in ('functions', 'edges', 'observations', 'facts', 'preferences') for stage in ('before', 'after')}
    policies = {name + '_scope' for name in _MANAGED_TABLES} | {'feedback_tenant_scope'}
    read_policies = {name + '_source_read_grants': name for name in _SOURCE_READ_GRANT_TABLES}
    policies.update(read_policies)
    kinds = ('SEQUENCE OWNED BY', 'TABLE DATA', 'SEQUENCE SET', 'FK CONSTRAINT', 'ROW SECURITY',
             'DEFAULT ACL', 'CONSTRAINT', 'FUNCTION', 'SEQUENCE', 'SCHEMA', 'COMMENT', 'DEFAULT',
             'TABLE', 'INDEX', 'TRIGGER', 'POLICY', 'ACL')
    data, schema_acl, seen = [], [], set()
    for line in text.splitlines():
        if not line or line.startswith(';'):
            continue
        match = re.fullmatch(r'(\d+); (\d+) (\d+) (.+)', line)
        if match is None or match[1] in seen:
            raise ValueError('unexpected TOC')
        seen.add(match[1])
        tail = match[4]
        kind = next((kind for kind in kinds if tail.startswith(kind + ' ')), None)
        if kind is None:
            raise ValueError('unexpected TOC kind')
        rest = tail[len(kind) + 1:]
        if kind == 'SCHEMA':
            if not rest.startswith('- ' + schema + ' '):
                raise ValueError('foreign schema')
            continue
        if kind in {'COMMENT', 'ACL'} and rest.startswith('- SCHEMA ' + schema + ' '):
            if kind == 'ACL':
                schema_acl.append(line)
            continue
        if not rest.startswith(schema + ' '):
            raise ValueError('foreign object')
        body = rest[len(schema) + 1:]
        if kind == 'COMMENT':
            continue
        if kind == 'DEFAULT ACL':
            # Restore no target administrative defaults from source metadata.
            if not any(body.startswith('DEFAULT PRIVILEGES FOR ' + kind + ' ')
                       for kind in ('TABLES', 'SEQUENCES', 'FUNCTIONS')):
                raise ValueError('unexpected default privilege kind')
            continue
        if kind == 'ACL':
            object_kind, body = body.split(' ', 1)
            if object_kind not in {'TABLE', 'SEQUENCE', 'FUNCTION'}:
                raise ValueError('unexpected ACL object')
            name = body.split('(', 1)[0] if object_kind == 'FUNCTION' else body.split(' ', 1)[0]
            if name not in (functions if object_kind == 'FUNCTION' else relations):
                raise ValueError('unknown ACL object')
        elif kind == 'FUNCTION':
            if body.split('(', 1)[0] not in functions:
                raise ValueError('unknown function')
        else:
            fields = body.split()
            name = fields[0] if fields else ''
            allowed = set(_MANAGED_TABLES) if kind in {'TABLE', 'TABLE DATA', 'ROW SECURITY', 'DEFAULT',
                                                        'CONSTRAINT', 'FK CONSTRAINT', 'TRIGGER', 'POLICY'} else relations
            if name not in allowed:
                raise ValueError('unknown relation')
            if kind in {'CONSTRAINT', 'FK CONSTRAINT', 'TRIGGER', 'POLICY'}:
                names = constraints if kind in {'CONSTRAINT', 'FK CONSTRAINT'} else triggers if kind == 'TRIGGER' else policies
                if (len(fields) != 3 or fields[1] not in names
                        or (kind == 'POLICY' and read_policies.get(fields[1], name) != name)):
                    raise ValueError('unknown attached object')
            elif kind == 'DEFAULT' and (len(fields) != 3 or fields[1] not in {'id', 'stream_seq'}):
                raise ValueError('unknown default column')
        data.append(line)
    if not data or not any(' TABLE DATA ' in line for line in data):
        raise ValueError('empty managed archive')
    return '\n'.join(data) + '\n', '\n'.join(schema_acl) + ('\n' if schema_acl else '')


class PostgresBackupExecutor:
    """Create schema-scoped PostgreSQL backups bound to one inspected target."""

    def __init__(
        self,
        *,
        expected_target: PostgresTargetIdentity,
        tools: PostgresClientTools | None = None,
        timeout_seconds: int = 3600,
        connection_factory: Callable[[str], Any] | None = None,
        application_acl: ApplicationAclContract | None = None,
        ingress_acl: IngressAclContract | None = None,
        deployment_profile: str | None = None,
    ) -> None:
        if type(expected_target) is not PostgresTargetIdentity:
            raise BackupConfigurationError("postgres_backup_target_invalid")
        if (
            type(expected_target.database) is not str
            or not expected_target.database
            or type(expected_target.schema) is not str
            or not expected_target.schema
        ):
            raise BackupConfigurationError("postgres_backup_target_invalid")
        if type(timeout_seconds) is not int or timeout_seconds <= 0:
            raise BackupConfigurationError("postgres_backup_timeout_invalid")
        self._target = expected_target
        self._tools = tools
        if self._tools is not None and type(self._tools) is not PostgresClientTools:
            raise BackupConfigurationError("postgres_client_tools_invalid")
        self._timeout_seconds = timeout_seconds
        self._connection_factory = connection_factory
        if application_acl is not None and type(application_acl) is not ApplicationAclContract:
            raise BackupConfigurationError("postgres_backup_acl_invalid")
        if ingress_acl is not None and type(ingress_acl) is not IngressAclContract:
            raise BackupConfigurationError("postgres_backup_acl_invalid")
        if (application_acl is not None or ingress_acl is not None) and deployment_profile not in {
            "development",
            "production",
        }:
            raise BackupConfigurationError("postgres_backup_acl_invalid")
        self._application_acl = application_acl
        self._ingress_acl = ingress_acl
        self._deployment_profile = deployment_profile

    def _client_tools(self) -> PostgresClientTools:
        if self._tools is None:
            self._tools = PostgresClientTools.discover()
        return self._tools

    def _connect(self, dsn: str) -> Any:
        if self._connection_factory is not None:
            return self._connection_factory(dsn)
        try:
            import psycopg2
        except ImportError as exc:
            raise BackupConfigurationError("postgres_driver_missing") from exc
        return psycopg2.connect(dsn)

    def _inspect_metadata(self, dsn: str) -> tuple[str, int]:
        connection = self._connect(dsn)
        cursor = None
        primary: BaseException | None = None
        try:
            if hasattr(connection, "autocommit"):
                connection.autocommit = False
            if hasattr(connection, "set_session"):
                connection.set_session(readonly=True, autocommit=False)
            cursor = connection.cursor()
            actual = inspect_postgres_connection_target(connection, cursor)
            if actual != self._target:
                raise BackupIntegrityError("postgres_backup_target_mismatch")
            cursor.execute(
                """
                SELECT current_setting('server_version'),
                       COALESCE((SELECT MAX(version) FROM memplex_schema_migrations), 0)
                """
            )
            row = cursor.fetchone()
            if (
                row is None
                or len(row) != 2
                or type(row[0]) is not str
                or type(row[1]) is not int
                or row[1] < 0
            ):
                raise BackupIntegrityError("postgres_backup_catalog_invalid")
            return row[0], row[1]
        except BaseException as exc:
            primary = exc
            raise
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except BaseException:
                    if primary is None:
                        raise
            try:
                connection.rollback()
            except BaseException:
                if primary is None:
                    raise
            try:
                connection.close()
            except BaseException:
                if primary is None:
                    raise

    def _require_matching_major(self, server_version: str) -> None:
        if _parse_major(self._client_tools().pg_dump_version) != _parse_major(server_version):
            raise BackupIntegrityError("postgres_client_server_major_mismatch")

    def _pg_dump_argv(self, payload: Path) -> tuple[str, ...]:
        return (
            str(self._client_tools().pg_dump),
            "--format=custom",
            "--compress=9",
            f"--schema={self._target.schema}",
            "--no-password",
            f"--file={payload}",
        )

    def _pg_restore_argv(self, payload: Path) -> tuple[str, ...]:
        return (
            str(self._client_tools().pg_restore),
            "--single-transaction",
            "--exit-on-error",
            "--no-password",
            f"--dbname={self._target.database}",
            str(payload),
        )

    def _inspect_restore_state(self, dsn: str, schema: str) -> tuple[str, bool, int]:
        connection = self._connect(dsn)
        cursor = None
        primary: BaseException | None = None
        try:
            if hasattr(connection, "autocommit"):
                connection.autocommit = False
            if hasattr(connection, "set_session"):
                connection.set_session(readonly=True, autocommit=False)
            cursor = connection.cursor()
            cursor.execute(
                """
                SELECT pg_catalog.current_database(),
                       pg_catalog.inet_server_addr()::text,
                       pg_catalog.inet_server_port()
                """
            )
            identity_row = cursor.fetchone()
            if (
                identity_row is None
                or len(identity_row) != 3
                or identity_row[0] != self._target.database
                or identity_row[1] != self._target.server_address
                or identity_row[2] != self._target.server_port
            ):
                raise BackupIntegrityError("postgres_backup_target_mismatch")
            cursor.execute(
                """
                SELECT current_setting('server_version'),
                       EXISTS(SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname=%s)
                """,
                (schema,),
            )
            row = cursor.fetchone()
            if (
                row is None
                or len(row) != 2
                or type(row[0]) is not str
                or type(row[1]) is not bool
            ):
                raise BackupIntegrityError("postgres_restore_catalog_invalid")
            migration_version = 0
            if row[1]:
                cursor.execute(
                    "SELECT COALESCE(MAX(version), 0) FROM memplex_schema_migrations"
                )
                version_row = cursor.fetchone()
                if (
                    version_row is None
                    or len(version_row) != 1
                    or type(version_row[0]) is not int
                    or version_row[0] < 0
                ):
                    raise BackupIntegrityError("postgres_restore_catalog_invalid")
                migration_version = version_row[0]
            return row[0], row[1], migration_version
        except BaseException as exc:
            primary = exc
            raise
        finally:
            if cursor is not None:
                try:
                    cursor.close()
                except BaseException:
                    if primary is None:
                        raise
            try:
                connection.rollback()
            except BaseException:
                if primary is None:
                    raise
            try:
                connection.close()
            except BaseException:
                if primary is None:
                    raise

    def create(
        self,
        *,
        migration_dsn: str,
        destination: Path,
        signing_key: bytes,
        key_id: str,
        max_bytes: int = 64 * 1024**3,
    ) -> BackupManifest:
        environment = _libpq_environment(migration_dsn)
        server_version, migration_version = self._inspect_metadata(migration_dsn)
        self._require_matching_major(server_version)
        destination_path = Path(destination)
        try:
            destination_path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(destination_path, 0o700)
        except OSError as exc:
            raise _fixed_error("postgres_backup_destination_invalid", exc)

        backup_id = str(uuid.uuid4())
        writer = BackupArtifactWriter(
            destination_path,
            key=signing_key,
            key_id=key_id,
            max_bytes=max_bytes,
        )
        try:
            with tempfile.TemporaryDirectory(
                prefix=".postgres-backup-", dir=destination_path
            ) as capture_directory:
                os.chmod(capture_directory, 0o700)
                payload = Path(capture_directory) / "payload.dump"
                completed = subprocess.run(
                    self._pg_dump_argv(payload),
                    shell=False,
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=self._timeout_seconds,
                    env=environment,
                )
                if completed.returncode != 0:
                    raise BackupIntegrityError("postgres_backup_command_failed")
                if not payload.exists() or payload.is_symlink():
                    raise BackupIntegrityError("postgres_backup_payload_missing")
                final_server_version, final_migration_version = self._inspect_metadata(
                    migration_dsn
                )
                if (
                    final_server_version != server_version
                    or final_migration_version != migration_version
                ):
                    raise BackupIntegrityError("postgres_backup_catalog_changed")
                artifact = writer.publish(
                    manifest_fields={
                        "format_version": 1,
                        "backup_id": backup_id,
                        "created_at": datetime.now(UTC).strftime(
                            "%Y-%m-%dT%H:%M:%S.%fZ"
                        ),
                        "backend": "postgres",
                        "database": self._target.database,
                        "schema": self._target.schema,
                        "migration_version": migration_version,
                        "payload_name": "payload.dump",
                        "pg_dump_version": self._client_tools().pg_dump_version,
                        "server_version": server_version,
                        "consistency": "pg_dump_snapshot",
                    },
                    payload_source=payload,
                )
        except subprocess.TimeoutExpired as exc:
            raise _fixed_error("postgres_backup_command_timeout", exc)
        except (OSError, subprocess.SubprocessError) as exc:
            raise _fixed_error("postgres_backup_command_failed", exc)

        manifest = load_verified_backup_manifest(artifact, signing_key)
        if manifest.backup_id != backup_id:
            raise BackupIntegrityError("backup_publish_outcome_unknown")
        return manifest

    def _restore_authority(self, migration_dsn: str, scope: Any, *, issuer: object) -> Any:
        from memplex.source_compatibility import RestoreAuthority, RestoreTarget
        from memplex.source_contract import RejectionCode, SourceContractError
        from memplex.storage.postgres_resources import _source_target_identity
        from memplex.storage.source_versions import _RESOURCE_ISSUER
        if issuer is not _RESOURCE_ISSUER:
            raise SourceContractError(RejectionCode.UNSUPPORTED_CAPABILITY)
        self._source_restore_dsn = migration_dsn
        return RestoreAuthority(issuer=issuer, resource=self, target=RestoreTarget(
            "postgres", scope, _source_target_identity(self._target)))

    def _begin_source_restore(self, authority: Any) -> None:
        from contextlib import closing
        with closing(self._connect(self._source_restore_dsn)) as connection:
            self._reserve_restore(connection, backup_id=None, authority=authority,
                                  deadline=time.monotonic() + self._timeout_seconds)

    @staticmethod
    def _remaining_restore(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise BackupIntegrityError("postgres_restore_command_timeout")
        return remaining

    def _restore_owner(self, connection: Any, cursor: Any, deadline: float) -> None:
        from memplex.storage.postgres_resources import _source_target_lock
        remaining = max(1, int(self._remaining_restore(deadline) * 1000))
        cursor.execute("SELECT set_config('lock_timeout',%s,true), set_config('statement_timeout',%s,true)",
                       (f"{remaining}ms", f"{remaining}ms"))
        cursor.execute("SELECT current_database(),inet_server_addr()::text,inet_server_port()")
        if cursor.fetchone() != (self._target.database, self._target.server_address, self._target.server_port):
            raise BackupIntegrityError("postgres_restore_target_mismatch")
        _source_target_lock(cursor, self._target, session=True)

    def _check_restore_isolation(self, cursor: Any) -> str:
        """New-schema access barrier; observe existing rights, never change them."""
        cursor.execute("SELECT pg_catalog.current_schema()")
        if cursor.fetchone() != (self._target.schema,):
            raise BackupIntegrityError("postgres_restore_target_mismatch")
        cursor.execute("SELECT n.oid,n.nspowner,current_user,n.nspowner=to_regrole(current_user) "
                       "FROM pg_catalog.pg_namespace n WHERE n.nspname=%s", (self._target.schema,))
        row = cursor.fetchone()
        if row is None or row[3] is not True:
            raise BackupIntegrityError("postgres_restore_target_not_isolated")
        schema_oid, owner_oid, owner, _ = row
        # Match the existing conservative membership safety rule: a role may
        # SET ROLE through NOINHERIT memberships even without ambient access.
        cursor.execute("WITH RECURSIVE reachable(oid) AS ("
                       "SELECT oid FROM pg_catalog.pg_roles WHERE oid<>%s AND rolcanlogin AND NOT rolsuper "
                       "UNION SELECT m.roleid FROM pg_catalog.pg_auth_members m JOIN reachable r ON m.member=r.oid) "
                       "SELECT 1 FROM reachable WHERE "
                       "has_schema_privilege(oid,%s,'USAGE') OR has_schema_privilege(oid,%s,'CREATE') LIMIT 1",
                       (owner_oid, schema_oid, schema_oid))
        if cursor.fetchone() is not None:
            raise BackupIntegrityError("postgres_restore_target_not_isolated")
        cursor.execute("SELECT 1 FROM pg_catalog.pg_namespace n, LATERAL aclexplode(n.nspacl) a "
                       "WHERE n.oid=%s AND a.grantee<>n.nspowner LIMIT 1", (schema_oid,))
        if cursor.fetchone() is not None:
            raise BackupIntegrityError("postgres_restore_target_not_isolated")
        # Object defaults may reproduce the already approved app/ingress ACL.
        # They cannot grant schema access or introduce a new recipient/right.
        cursor.execute("SELECT d.defaclobjtype,a.grantee,r.rolname,a.privilege_type,a.is_grantable "
                       "FROM pg_catalog.pg_default_acl d CROSS JOIN LATERAL aclexplode(d.defaclacl) a "
                       "LEFT JOIN pg_catalog.pg_roles r ON r.oid=a.grantee "
                       "WHERE d.defaclrole=%s AND d.defaclnamespace IN (0,%s) AND a.grantee<>d.defaclrole",
                       (owner_oid, schema_oid))
        for kind, _grantee, role, privilege, grantable in cursor.fetchall():
            app = self._application_acl is not None and role == self._application_acl.role
            ingress = self._ingress_acl is not None and role == self._ingress_acl.role
            allowed = ((app and ((kind == 'r' and privilege in {'SELECT', 'INSERT', 'UPDATE', 'DELETE'})
                                  or (kind == 'S' and privilege == 'USAGE')
                                  or (kind == 'f' and privilege == 'EXECUTE')))
                       or (ingress and kind == 'f' and privilege == 'EXECUTE')
                       or (kind == 'f' and _grantee == 0 and privilege == 'EXECUTE'))
            if grantable or not allowed:
                raise BackupIntegrityError("postgres_restore_defaults_unsafe")
        return str(owner)

    def _reserve_restore(self, connection: Any, *, backup_id: str | None,
                         authority: Any, deadline: float) -> dict[str, Any]:
        from dataclasses import asdict

        from psycopg2 import sql

        from memplex.source_compatibility import RestoreAuthority
        from memplex.storage.postgres_resources import (
            _source_schema_marker,
            _source_target_identity,
            _write_source_schema_marker,
        )
        from memplex.storage.source_versions import _RESOURCE_ISSUER
        connection.set_session(isolation_level="READ COMMITTED", readonly=False, autocommit=False)
        with connection.cursor() as cursor:
            self._restore_owner(connection, cursor, deadline)
            explicit = authority is not None
            if explicit and (type(authority) is not RestoreAuthority or authority._issuer is not _RESOURCE_ISSUER
                             or authority._resource is not self or authority.target.backend != 'postgres'
                             or authority.target.storage_identity != _source_target_identity(self._target)):
                raise BackupIntegrityError("postgres_restore_authority_invalid")
            cursor.execute('SELECT current_user')
            owner = cursor.fetchone()[0]
            marker = {"version": 1, "state": "quarantined", "kind": "explicit_v1" if explicit else "legacy_candidate",
                      "restore_id": authority.receipt.restore_id if explicit else str(uuid.uuid4()),
                      "backup_id": backup_id, "storage_identity": _source_target_identity(self._target),
                      "owner": owner, "scope": asdict(authority.target.scope) if explicit else None}
            cursor.execute('SELECT EXISTS (SELECT 1 FROM pg_namespace WHERE nspname=%s)', (self._target.schema,))
            if cursor.fetchone()[0]:
                existing = _source_schema_marker(cursor, self._target.schema)
                if existing is None:
                    raise BackupIntegrityError("postgres_restore_target_exists")
                from memplex.source_contract import _uuid
                _uuid(existing.get('restore_id'))
                compare = dict(marker)
                compare['restore_id'] = existing.get('restore_id')
                if explicit:
                    compare['restore_id'] = authority.receipt.restore_id
                    # begin_v1_restore has no archive bytes yet; bind the first verified backup once.
                    if existing.get('backup_id') is None:
                        compare['backup_id'] = None
                if existing != compare:
                    raise BackupIntegrityError("postgres_restore_target_exists")
                cursor.execute("SELECT EXISTS(SELECT 1 FROM pg_class WHERE relnamespace=%s::regnamespace) OR "
                               "EXISTS(SELECT 1 FROM pg_proc WHERE pronamespace=%s::regnamespace) OR "
                               "EXISTS(SELECT 1 FROM pg_type WHERE typnamespace=%s::regnamespace)",
                               (self._target.schema,) * 3)
                if cursor.fetchone()[0]:
                    raise BackupIntegrityError("postgres_restore_target_exists")
                marker['restore_id'] = existing['restore_id']
            else:
                cursor.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(self._target.schema)))
            if self._check_restore_isolation(cursor) != owner:
                raise BackupIntegrityError("postgres_restore_target_not_isolated")
            _write_source_schema_marker(cursor, self._target.schema, marker)
        connection.commit()
        with connection.cursor() as cursor:
            if _source_schema_marker(cursor, self._target.schema) != marker:
                raise BackupIntegrityError("postgres_restore_quarantine_lost")
            self._check_restore_isolation(cursor)
        connection.commit()
        return marker

    def _restore_toc(self, payload: Path, environment: dict[str, str], deadline: float) -> tuple[str, str]:
        completed = subprocess.run((str(self._client_tools().pg_restore), '--list', str(payload)),
            shell=False, check=False, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=self._remaining_restore(deadline), env=environment)
        if completed.returncode != 0 or not isinstance(completed.stdout, bytes):
            raise BackupIntegrityError('postgres_restore_command_failed')
        try:
            return _approved_restore_toc(completed.stdout.decode('utf-8'), self._target.schema)
        except (ValueError, UnicodeError) as exc:
            raise BackupIntegrityError('postgres_restore_catalog_invalid') from exc

    def _restore_list(self, payload: Path, use_list: Path, environment: dict[str, str], deadline: float) -> None:
        argv = self._pg_restore_argv(payload)
        completed = subprocess.run((*argv[:-1], '--no-owner', f'--use-list={use_list}', argv[-1]),
            shell=False, check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=self._remaining_restore(deadline), env=environment)
        if completed.returncode != 0:
            raise BackupIntegrityError('postgres_restore_command_failed')

    def _quarantined_structure(self, cursor: Any, manifest: BackupManifest, authority: Any) -> None:
        """Validate restored structure without claiming application readiness."""
        from memplex.storage.migrations import discover_migrations
        from memplex.storage.migrations.runner import (
            _plan_from_observed_state,
            _read_ledger_if_present,
            schema_fingerprint,
        )
        from memplex.storage.postgres_resources import _source_global_proof
        permitted = self._quarantined_object_acls(cursor)
        plan = _plan_from_observed_state(_read_ledger_if_present(cursor),
            schema_fingerprint(cursor, permit_application_acl=permitted), discover_migrations())
        if plan.state != 'ready' or plan.current_version != manifest.migration_version:
            raise BackupIntegrityError('postgres_restore_readback_failed')
        _source_global_proof(cursor, self._target.schema, empty=False, scope=authority.target.scope)
        scope = authority.target.scope
        cursor.execute("SELECT set_config('memplex.tenant_id',%s,true),set_config('memplex.workspace_id',%s,true)",
                       (scope.tenant_id, scope.workspace_id))
        cursor.execute('SELECT authority_id::text,tenant_id,workspace,contract_version FROM memplex_source_authorities')
        if cursor.fetchall() != [(scope.authority_id, scope.tenant_id, scope.workspace_id, 1)]:
            raise BackupIntegrityError('postgres_restore_readback_failed')

    def _quarantined_object_acls(self, cursor: Any) -> bool:
        from memplex.storage.migrations._constants import (
            _APPLICATION_ACL,
            _CAPABILITIES_TABLE,
            _LEDGER_TABLE,
        )
        from memplex.storage.migrations.acl_verification import (
            _verify_application_function_acls,
            _verify_application_sequence_acls,
            _verify_ingress_production_roles,
        )
        role_oid = None
        if self._application_acl is not None:
            cursor.execute('SELECT oid,rolsuper,rolbypassrls FROM pg_roles WHERE rolname=%s', (self._application_acl.role,))
            row = cursor.fetchone()
            if row is None or row[1] or row[2]:
                raise BackupIntegrityError('postgres_restore_acl_invalid')
            role_oid = row[0]
            # This existing helper is the same recursive owner/superuser/
            # BYPASSRLS rule used by application ACL verification.
            _verify_ingress_production_roles(cursor, role_oid)
        if self._ingress_acl is not None:
            cursor.execute('SELECT oid FROM pg_roles WHERE rolname=%s', (self._ingress_acl.role,))
            ingress = cursor.fetchone()
            if ingress is None:
                raise BackupIntegrityError('postgres_restore_acl_invalid')
            _verify_ingress_production_roles(cursor, ingress[0])
        tables = [*_APPLICATION_ACL, _CAPABILITIES_TABLE, _LEDGER_TABLE]
        cursor.execute("SELECT c.relname,c.relowner=to_regrole(current_user),a.attname "
                       "FROM pg_class c LEFT JOIN pg_attribute a ON a.attrelid=c.oid AND a.attacl IS NOT NULL "
                       "WHERE c.relnamespace=%s::regnamespace AND c.relname=ANY(%s)", (self._target.schema, tables))
        rows = cursor.fetchall()
        if len(rows) != len(tables) or any(not row[1] or row[2] is not None for row in rows):
            raise BackupIntegrityError('postgres_restore_acl_invalid')
        cursor.execute("SELECT c.relname,c.relowner,a.grantee,a.privilege_type,a.is_grantable "
                       "FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a "
                       "WHERE c.relnamespace=%s::regnamespace", (self._target.schema,))
        observed: dict[str, set[str]] = {name: set() for name in _APPLICATION_ACL}
        for name, owner, grantee, privilege, grantable in cursor.fetchall():
            if grantee == owner:
                continue
            if role_oid is None or grantee != role_oid or grantable:
                raise BackupIntegrityError('postgres_restore_acl_invalid')
            if name in observed:
                observed[name].add(privilege)
            elif name not in {'memplex_changelog_id_seq', 'memplex_sync_outbox_stream_seq_seq'}:
                raise BackupIntegrityError('postgres_restore_acl_invalid')
        if role_oid is None:
            cursor.execute("SELECT p.proowner,a.grantee,r.rolname,p.proname,a.privilege_type,a.is_grantable "
                           "FROM pg_proc p CROSS JOIN LATERAL aclexplode(COALESCE(p.proacl,acldefault('f',p.proowner))) a "
                           "LEFT JOIN pg_roles r ON r.oid=a.grantee WHERE p.pronamespace=%s::regnamespace",
                           (self._target.schema,))
            for owner, grantee, role, function, privilege, grantable in cursor.fetchall():
                if owner == grantee:
                    continue
                if (self._ingress_acl is None or role != self._ingress_acl.role
                        or function != 'memplex_sync_apply_inbound' or privilege != 'EXECUTE' or grantable):
                    raise BackupIntegrityError('postgres_restore_acl_invalid')
            return False
        if observed != {name: set(rights) for name, rights in _APPLICATION_ACL.items()}:
            raise BackupIntegrityError('postgres_restore_acl_invalid')
        _verify_application_sequence_acls(cursor, role_oid)
        _verify_application_function_acls(cursor, role_oid,
                                          self._ingress_acl.role if self._ingress_acl is not None else None)
        return True

    def _normal_restore_status(self, migration_dsn: str, manifest: BackupManifest, deadline: float) -> None:
        def factory() -> Any:
            connection = self._connect(migration_dsn)
            try:
                with connection.cursor() as cursor:
                    remaining = max(1, int(self._remaining_restore(deadline) * 1000))
                    cursor.execute("SELECT set_config('statement_timeout',%s,false)", (f"{remaining}ms",))
                connection.commit()
                return connection
            except BaseException:
                connection.close()
                raise
        final = PostgresMigrationRunner(migration_dsn, connection_factory=factory).status(
            expected_target=self._target, application_acl=self._application_acl,
            ingress_acl=self._ingress_acl, deployment_profile=self._deployment_profile)
        if final.state != 'ready' or final.current_version != manifest.migration_version:
            raise BackupIntegrityError('postgres_restore_readback_failed')

    def restore(
        self, *, migration_dsn: str, artifact: Path, signing_key: bytes,
        target_schema: str, authority: Any = None,
    ) -> RestoreResult:
        """Restore only a fresh isolated target; unknown/V1 bytes stay quarantined."""
        from contextlib import closing

        from memplex.storage.postgres_resources import (
            _source_global_proof,
            _source_schema_marker,
            _write_source_schema_marker,
        )
        started = time.monotonic()
        deadline = started + self._timeout_seconds
        try:
            with open_verified_backup_artifact(Path(artifact), signing_key) as opened:
                manifest = opened.manifest
                if (manifest.backend != 'postgres' or type(target_schema) is not str
                        or target_schema != manifest.schema or manifest.database != self._target.database
                        or target_schema != self._target.schema):
                    raise BackupIntegrityError('postgres_restore_target_mismatch')
                environment = _libpq_environment(migration_dsn)
                with closing(self._connect(migration_dsn)) as connection:
                    with connection.cursor() as cursor:
                        cursor.execute("SELECT current_setting('server_version')")
                        server_version = cursor.fetchone()[0]
                    connection.rollback()
                    if _parse_major(self._client_tools().pg_restore_version) != _parse_major(server_version):
                        raise BackupIntegrityError('postgres_client_server_major_mismatch')
                    with tempfile.TemporaryDirectory(prefix='.postgres-restore-') as restore_dir:
                        payload = Path(restore_dir) / 'payload.dump'
                        opened.copy_payload_to(payload)
                        data, schema_acl = self._restore_toc(payload, environment, deadline)
                        data_list = Path(restore_dir) / 'objects.list'
                        acl_list = Path(restore_dir) / 'schema-acl.list'
                        data_list.write_text(data)
                        acl_list.write_text(schema_acl)
                        marker = self._reserve_restore(connection, backup_id=manifest.backup_id,
                                                       authority=authority, deadline=deadline)
                        self._restore_list(payload, data_list, environment, deadline)
                        with connection, connection.cursor() as cursor:
                            self._restore_owner(connection, cursor, deadline)
                            if _source_schema_marker(cursor, target_schema) != marker:
                                raise BackupIntegrityError('postgres_restore_quarantine_lost')
                            self._check_restore_isolation(cursor)
                            if authority is not None:
                                self._quarantined_structure(cursor, manifest, authority)
                            else:
                                _source_global_proof(cursor, target_schema, empty=False)
                        if authority is None:
                            if schema_acl:
                                self._restore_list(payload, acl_list, environment, deadline)
                            # This uses its own connection before the final AX locks.
                            self._normal_restore_status(migration_dsn, manifest, deadline)
                            with connection, connection.cursor() as cursor:
                                self._restore_owner(connection, cursor, deadline)
                                _source_global_proof(cursor, target_schema, empty=False)
                                if _source_schema_marker(cursor, target_schema) != marker:
                                    raise BackupIntegrityError('postgres_restore_quarantine_lost')
                                _write_source_schema_marker(cursor, target_schema, None)
        except subprocess.TimeoutExpired as exc:
            raise _fixed_error('postgres_restore_command_timeout', exc)
        except (OSError, subprocess.SubprocessError) as exc:
            raise _fixed_error('postgres_restore_command_failed', exc)
        except BackupIntegrityError:
            raise
        except Exception as exc:
            raise BackupIntegrityError('postgres_restore_quarantined') from exc
        return RestoreResult(backup_id=manifest.backup_id, database=manifest.database,
                             schema=manifest.schema, restored=True, elapsed_seconds=time.monotonic() - started)
