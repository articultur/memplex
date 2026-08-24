"""Real PostgreSQL logical-backup integration checks."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from memplex.backup import BackupIntegrityError, BackupManifest, verify_backup_artifact
from memplex.storage.migrations import PostgresMigrationRunner, discover_migrations
from memplex.storage.postgres_backup import (
    PostgresBackupExecutor,
    PostgresClientTools,
    inspect_pitr_readiness,
)


def _pgserver_tools() -> PostgresClientTools:
    pgserver = pytest.importorskip("pgserver", reason="pgserver client tools unavailable")
    root = Path(pgserver.__file__).resolve().parent / "pginstall" / "bin"
    return PostgresClientTools(
        pg_dump=root / "pg_dump",
        pg_restore=root / "pg_restore",
        pg_dump_version="16.2",
        pg_restore_version="16.2",
    )


def _latest_migration_version() -> int:
    return discover_migrations()[-1].version


def test_real_postgres_backup_contains_business_sync_and_migration_catalogue(
    pg_function_dsn: str, tmp_path: Path
) -> None:
    psycopg2 = pytest.importorskip(
        "psycopg2", reason="psycopg2 not installed (use .venv-pgcheck)"
    )
    runner = PostgresMigrationRunner(pg_function_dsn)
    assert runner.apply().state == "ready"
    target = runner.inspect_target()
    connection = psycopg2.connect(pg_function_dsn)
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            INSERT INTO memplex_functions
                (id, data, tenant_id, owner_subject, workspace, visibility,
                 source_agent, source_session)
            VALUES
                ('backup-function', '{"name":"Backup Function"}', 'tenant-backup',
                 'subject-backup', 'workspace-backup', 'private', 'agent-backup',
                 'session-backup')
            """
        )
        connection.commit()
        cursor.close()
    finally:
        connection.close()

    key = bytes(range(32))
    tools = _pgserver_tools()
    manifest = PostgresBackupExecutor(
        expected_target=target,
        tools=tools,
        timeout_seconds=30,
    ).create(
        migration_dsn=pg_function_dsn,
        destination=tmp_path / "backups",
        signing_key=key,
        key_id="integration-key",
    )
    artifact = tmp_path / "backups" / manifest.backup_id

    assert manifest.schema == target.schema
    assert manifest.database == target.database
    assert manifest.migration_version == _latest_migration_version()
    assert verify_backup_artifact(artifact, key).verified is True
    listed = subprocess.run(
        (str(tools.pg_restore), "--list", str(artifact / "payload.dump")),
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    assert target.schema in listed
    assert "memplex_functions" in listed
    assert "memplex_sync_outbox" in listed
    assert "memplex_schema_migrations" in listed
    assert "fts_functions_idx" in listed


def test_real_postgres_backup_restore_roundtrip_requires_absent_same_schema(
    pg_function_dsn: str, tmp_path: Path
) -> None:
    psycopg2 = pytest.importorskip(
        "psycopg2", reason="psycopg2 not installed (use .venv-pgcheck)"
    )
    runner = PostgresMigrationRunner(pg_function_dsn)
    assert runner.apply().state == "ready"
    target = runner.inspect_target()
    connection = psycopg2.connect(pg_function_dsn)
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            INSERT INTO memplex_functions
                (id, data, tenant_id, owner_subject, workspace, visibility,
                 source_agent, source_session)
            VALUES
                ('restore-function', '{"name":"Restore Function"}', 'tenant-restore',
                 'subject-restore', 'workspace-restore', 'private', 'agent-restore',
                 'session-restore')
            """
        )
        connection.commit()
        cursor.close()
    finally:
        connection.close()
    key = bytes(reversed(range(32)))
    tools = _pgserver_tools()
    executor = PostgresBackupExecutor(expected_target=target, tools=tools, timeout_seconds=30)
    manifest = executor.create(
        migration_dsn=pg_function_dsn,
        destination=tmp_path / "backups",
        signing_key=key,
        key_id="restore-key",
    )
    artifact = tmp_path / "backups" / manifest.backup_id

    with pytest.raises(BackupIntegrityError, match="postgres_restore_target_exists"):
        executor.restore(
            migration_dsn=pg_function_dsn,
            artifact=artifact,
            signing_key=key,
            target_schema=target.schema,
        )

    connection = psycopg2.connect(pg_function_dsn)
    try:
        cursor = connection.cursor()
        cursor.execute(f'DROP SCHEMA "{target.schema}" CASCADE')
        connection.commit()
        cursor.close()
    finally:
        connection.close()
    result = executor.restore(
        migration_dsn=pg_function_dsn,
        artifact=artifact,
        signing_key=key,
        target_schema=target.schema,
    )

    assert result.restored is True
    assert result.schema == target.schema
    connection = psycopg2.connect(pg_function_dsn)
    try:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT data->>'name' FROM memplex_functions WHERE id='restore-function'"
        )
        assert cursor.fetchone() == ("Restore Function",)
        cursor.execute("SELECT MAX(version) FROM memplex_schema_migrations")
        assert cursor.fetchone() == (_latest_migration_version(),)
        cursor.close()
    finally:
        connection.close()


def test_real_postgres_pitr_readiness_reports_server_settings(
    pg_function_dsn: str,
) -> None:
    readiness = inspect_pitr_readiness(pg_function_dsn)

    assert readiness.wal_level in {"minimal", "replica", "logical"}
    assert readiness.archive_mode in {"off", "on", "always"}
    assert type(readiness.max_wal_senders) is int
    assert readiness.ready is (
        readiness.wal_level in {"replica", "logical"}
        and readiness.archive_mode in {"on", "always"}
        and readiness.archive_command_configured
        and readiness.full_page_writes
        and readiness.max_wal_senders > 0
    )


def test_real_backup_restore_preserves_100001_outbox_rows_and_identity_sequence(
    pg_function_dsn: str, tmp_path: Path
) -> None:
    psycopg2 = pytest.importorskip(
        "psycopg2", reason="psycopg2 not installed (use .venv-pgcheck)"
    )
    runner = PostgresMigrationRunner(pg_function_dsn)
    assert runner.apply().state == "ready"
    target = runner.inspect_target()
    connection = psycopg2.connect(pg_function_dsn)
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            INSERT INTO memplex_sync_outbox
                (tenant_id,event_id,origin_node_id,node_type,entity_key,operation,
                 version_key,payload,visibility,owner_subject_id)
            SELECT 'tenant-backlog', 'event-' || item, 'node-local', 'function',
                   'entity-' || item, 'tombstone', 'version-' || item, NULL,
                   'user', 'subject-backlog'
            FROM generate_series(1, 100001) AS item
            """
        )
        connection.commit()
        cursor.close()
    finally:
        connection.close()

    key = bytes(range(31, -1, -1))
    executor = PostgresBackupExecutor(
        expected_target=target, tools=_pgserver_tools(), timeout_seconds=60
    )
    manifest = executor.create(
        migration_dsn=pg_function_dsn,
        destination=tmp_path / "backups",
        signing_key=key,
        key_id="backlog-key",
    )
    artifact = tmp_path / "backups" / manifest.backup_id
    connection = psycopg2.connect(pg_function_dsn)
    try:
        cursor = connection.cursor()
        cursor.execute(f'DROP SCHEMA "{target.schema}" CASCADE')
        connection.commit()
        cursor.close()
    finally:
        connection.close()

    executor.restore(
        migration_dsn=pg_function_dsn,
        artifact=artifact,
        signing_key=key,
        target_schema=target.schema,
    )

    connection = psycopg2.connect(pg_function_dsn)
    try:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT COUNT(*), MIN(stream_seq), MAX(stream_seq) "
            "FROM memplex_sync_outbox WHERE tenant_id='tenant-backlog'"
        )
        assert cursor.fetchone() == (100001, 1, 100001)
        cursor.execute("SELECT last_value, is_called FROM memplex_sync_outbox_stream_seq_seq")
        assert cursor.fetchone() == (100001, True)
        cursor.close()
    finally:
        connection.close()


def test_real_signed_but_corrupt_dump_rolls_back_target_schema(
    pg_function_dsn: str, tmp_path: Path
) -> None:
    psycopg2 = pytest.importorskip(
        "psycopg2", reason="psycopg2 not installed (use .venv-pgcheck)"
    )
    runner = PostgresMigrationRunner(pg_function_dsn)
    assert runner.apply().state == "ready"
    target = runner.inspect_target()
    key = bytes(range(32))
    executor = PostgresBackupExecutor(
        expected_target=target, tools=_pgserver_tools(), timeout_seconds=30
    )
    manifest = executor.create(
        migration_dsn=pg_function_dsn,
        destination=tmp_path / "backups",
        signing_key=key,
        key_id="corrupt-key",
    )
    artifact = tmp_path / "backups" / manifest.backup_id
    connection = psycopg2.connect(pg_function_dsn)
    try:
        cursor = connection.cursor()
        cursor.execute(f'DROP SCHEMA "{target.schema}" CASCADE')
        connection.commit()
        cursor.close()
    finally:
        connection.close()

    payload_path = artifact / "payload.dump"
    corrupted = payload_path.read_bytes()[:128]
    payload_path.write_bytes(corrupted)
    raw = json.loads((artifact / "manifest.json").read_text(encoding="utf-8"))
    raw["payload_size"] = len(corrupted)
    raw["payload_sha256"] = hashlib.sha256(corrupted).hexdigest()
    raw["signature"] = "0" * 64
    resigned = BackupManifest.from_dict(raw).signed(key)
    (artifact / "manifest.json").write_bytes(resigned.canonical_bytes())
    assert verify_backup_artifact(artifact, key).verified is True

    with pytest.raises(BackupIntegrityError, match="postgres_restore_command_failed"):
        executor.restore(
            migration_dsn=pg_function_dsn,
            artifact=artifact,
            signing_key=key,
            target_schema=target.schema,
        )

    connection = psycopg2.connect(pg_function_dsn)
    try:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT EXISTS(SELECT 1 FROM pg_catalog.pg_namespace WHERE nspname=%s)",
            (target.schema,),
        )
        assert cursor.fetchone() == (False,)
        cursor.close()
    finally:
        connection.close()


def _grant_application_acl_contract(pg_dsn: str, role: str) -> None:
    """Grant one login role the exact production application ACL contract."""
    psycopg2 = pytest.importorskip(
        "psycopg2", reason="psycopg2 not installed (use .venv-pgcheck)"
    )
    from psycopg2 import sql as pg_sql

    connection = psycopg2.connect(pg_dsn)
    try:
        cursor = connection.cursor()
        try:
            cursor.execute("SELECT current_schema()")
            schema = cursor.fetchone()[0]
            cursor.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (role,))
            if cursor.fetchone() is None:
                cursor.execute(
                    pg_sql.SQL("CREATE ROLE {} LOGIN").format(pg_sql.Identifier(role))
                )
            cursor.execute(
                pg_sql.SQL("REVOKE USAGE, CREATE ON SCHEMA {} FROM PUBLIC").format(
                    pg_sql.Identifier(schema)
                )
            )
            cursor.execute(
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
                "memplex_sync_deliveries",
                "memplex_sync_cursors",
                "memplex_sync_snapshots",
                "memplex_background_tasks",
            ):
                cursor.execute(
                    pg_sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO {}").format(
                        pg_sql.Identifier(table), pg_sql.Identifier(role)
                    )
                )
            for table in (
                "memplex_changelog",
                "memplex_sync_outbox",
                "memplex_sync_snapshot_items",
            ):
                cursor.execute(
                    pg_sql.SQL("GRANT SELECT, INSERT, DELETE ON {} TO {}").format(
                        pg_sql.Identifier(table), pg_sql.Identifier(role)
                    )
                )
            for table in (
                "memplex_sync_entity_versions",
                "memplex_sync_targets",
                "memplex_sync_stream_state",
            ):
                cursor.execute(
                    pg_sql.SQL("GRANT SELECT, INSERT, UPDATE ON {} TO {}").format(
                        pg_sql.Identifier(table), pg_sql.Identifier(role)
                    )
                )
            for table in ("memplex_sync_inbox", "memplex_sync_batches"):
                cursor.execute(
                    pg_sql.SQL("GRANT SELECT, INSERT ON {} TO {}").format(
                        pg_sql.Identifier(table), pg_sql.Identifier(role)
                    )
                )
            cursor.execute(
                pg_sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON feedback TO {}").format(
                    pg_sql.Identifier(role)
                )
            )
            for sequence in (
                "memplex_changelog_id_seq",
                "memplex_sync_outbox_stream_seq_seq",
            ):
                cursor.execute(
                    pg_sql.SQL("GRANT USAGE ON SEQUENCE {} TO {}").format(
                        pg_sql.Identifier(sequence), pg_sql.Identifier(role)
                    )
                )
            for function, arguments in (
                ("memplex_sync_capture_before", ""),
                ("memplex_sync_capture_local_change", ""),
                ("memplex_sync_assert_delivery_quota", "TEXT, BIGINT"),
                ("memplex_sync_snapshot_admission_counts", ""),
                ("memplex_sync_compact", "TIMESTAMPTZ, TIMESTAMPTZ, INTEGER"),
            ):
                cursor.execute(
                    pg_sql.SQL("GRANT EXECUTE ON FUNCTION {}({}) TO {}").format(
                        pg_sql.Identifier(function),
                        pg_sql.SQL(arguments),
                        pg_sql.Identifier(role),
                    )
                )
            connection.commit()
        finally:
            cursor.close()
    finally:
        connection.close()


def _drop_schema(pg_dsn: str, schema: str) -> None:
    psycopg2 = pytest.importorskip(
        "psycopg2", reason="psycopg2 not installed (use .venv-pgcheck)"
    )
    connection = psycopg2.connect(pg_dsn)
    try:
        cursor = connection.cursor()
        cursor.execute(f'DROP SCHEMA "{schema}" CASCADE')
        connection.commit()
        cursor.close()
    finally:
        connection.close()


def test_real_cli_backup_restore_and_drill_survive_schema_drop(
    pg_function_dsn: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    """CLI restore/drill must rebuild their context after disaster drops the schema."""
    import argparse
    import base64
    import os

    psycopg2 = pytest.importorskip(
        "psycopg2", reason="psycopg2 not installed (use .venv-pgcheck)"
    )
    from memplex.adapters.cli import _cmd_storage_backup

    runner = PostgresMigrationRunner(pg_function_dsn)
    assert runner.apply().state == "ready"
    target = runner.inspect_target()
    connection = psycopg2.connect(pg_function_dsn)
    try:
        cursor = connection.cursor()
        cursor.execute(
            """
            INSERT INTO memplex_functions
                (id, data, tenant_id, owner_subject, workspace, visibility,
                 source_agent, source_session)
            VALUES
                ('cli-restore-function', '{"name":"Cli Restore Function"}',
                 'tenant-cli', 'subject-cli', 'workspace-cli', 'private',
                 'agent-cli', 'session-cli')
            """
        )
        connection.commit()
        cursor.close()
    finally:
        connection.close()
    _grant_application_acl_contract(pg_function_dsn, "memplex_app")

    key = bytes(range(32))
    app_dsn = psycopg2.extensions.make_dsn(pg_function_dsn, user="memplex_app")
    monkeypatch.setenv("MEMPLEX_STORAGE_BACKEND", "postgres")
    monkeypatch.setenv("MEMPLEX_STORAGE_PATH", app_dsn)
    monkeypatch.setenv("MEMPLEX_STORAGE_MIGRATION_DSN", pg_function_dsn)
    monkeypatch.setenv("MEMPLEX_BACKUP_HMAC_KEY", base64.b64encode(key).decode())
    monkeypatch.setenv("MEMPLEX_BACKUP_KEY_ID", "cli-drill-key")
    tools = _pgserver_tools()
    monkeypatch.setenv(
        "PATH", f"{tools.pg_dump.parent}{os.pathsep}{os.environ.get('PATH', '')}"
    )

    def run_cli(**kwargs):
        args = argparse.Namespace(config=None, output="json", **kwargs)
        rc = _cmd_storage_backup(args)
        captured = capsys.readouterr()
        assert rc == 0, captured.err
        return json.loads(captured.out)

    created = run_cli(backup_command="create", destination=str(tmp_path / "backups"))
    artifact = tmp_path / "backups" / created["backup_id"]
    assert created["schema"] == target.schema

    _drop_schema(pg_function_dsn, target.schema)
    restored = run_cli(
        backup_command="restore",
        artifact=str(artifact),
        target_schema=target.schema,
    )
    assert restored["restored"] is True
    assert restored["schema"] == target.schema

    connection = psycopg2.connect(pg_function_dsn)
    try:
        cursor = connection.cursor()
        cursor.execute(
            "SELECT data->>'name' FROM memplex_functions WHERE id='cli-restore-function'"
        )
        assert cursor.fetchone() == ("Cli Restore Function",)
        cursor.close()
    finally:
        connection.close()

    _drop_schema(pg_function_dsn, target.schema)
    drill = run_cli(
        backup_command="drill",
        artifact=str(artifact),
        target_schema=target.schema,
    )
    assert "observed_rpo_seconds" in drill
    assert "observed_rto_seconds" in drill
    assert "industrial_gate_closing" in drill
