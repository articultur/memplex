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

    def restore(
        self,
        *,
        migration_dsn: str,
        artifact: Path,
        signing_key: bytes,
        target_schema: str,
    ) -> RestoreResult:
        started = time.monotonic()
        with open_verified_backup_artifact(Path(artifact), signing_key) as opened:
            manifest = opened.manifest
            if (
                manifest.backend != "postgres"
                or type(target_schema) is not str
                or target_schema != manifest.schema
                or manifest.database != self._target.database
                or target_schema != self._target.schema
            ):
                raise BackupIntegrityError("postgres_restore_target_mismatch")

            environment = _libpq_environment(migration_dsn)
            server_version, exists, _ = self._inspect_restore_state(
                migration_dsn, target_schema
            )
            if exists:
                raise BackupIntegrityError("postgres_restore_target_exists")
            if _parse_major(self._client_tools().pg_restore_version) != _parse_major(
                server_version
            ):
                raise BackupIntegrityError("postgres_client_server_major_mismatch")
            try:
                with tempfile.TemporaryDirectory(
                    prefix=".postgres-restore-"
                ) as restore_dir:
                    os.chmod(restore_dir, 0o700)
                    payload = Path(restore_dir) / "payload.dump"
                    opened.copy_payload_to(payload)
                    completed = subprocess.run(
                        self._pg_restore_argv(payload),
                        shell=False,
                        check=False,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=self._timeout_seconds,
                        env=environment,
                    )
            except subprocess.TimeoutExpired as exc:
                raise _fixed_error("postgres_restore_command_timeout", exc)
            except (OSError, subprocess.SubprocessError) as exc:
                raise _fixed_error("postgres_restore_command_failed", exc)
        if completed.returncode != 0:
            raise BackupIntegrityError("postgres_restore_command_failed")

        _, exists_after, migration_version = self._inspect_restore_state(
            migration_dsn, target_schema
        )
        if not exists_after or migration_version != manifest.migration_version:
            raise BackupIntegrityError("postgres_restore_readback_failed")
        factory = None
        if self._connection_factory is not None:
            connection_factory_impl = self._connection_factory

            def connection_factory() -> Any:
                return connection_factory_impl(migration_dsn)

            factory = connection_factory
        try:
            final = PostgresMigrationRunner(
                migration_dsn, connection_factory=factory
            ).status(
                expected_target=self._target,
                application_acl=self._application_acl,
                ingress_acl=self._ingress_acl,
                deployment_profile=self._deployment_profile,
            )
        except Exception as exc:  # noqa: BLE001 - broad catch, re-raised/wrapped below
            raise _fixed_error("postgres_restore_readback_failed", exc)
        if final.state != "ready" or final.current_version != manifest.migration_version:
            raise BackupIntegrityError("postgres_restore_readback_failed")
        return RestoreResult(
            backup_id=manifest.backup_id,
            database=manifest.database,
            schema=manifest.schema,
            restored=True,
            elapsed_seconds=time.monotonic() - started,
        )
