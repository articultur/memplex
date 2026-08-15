"""PostgreSQL backup client-tool and subprocess boundaries."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from memplex.backup import (
    BackupArtifactWriter,
    BackupConfigurationError,
    BackupIntegrityError,
)
from memplex.storage.migrations import PostgresTargetIdentity
from memplex.storage.postgres_backup import (
    PostgresBackupExecutor,
    PostgresClientTools,
    _libpq_environment,
)


def _target() -> PostgresTargetIdentity:
    return PostgresTargetIdentity(
        database="memplex",
        schema="tenant_a",
        server_address="127.0.0.1",
        server_port=5432,
    )


def test_libpq_environment_is_allowlisted_and_does_not_retain_dsn(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://other:leak@wrong.invalid/db")
    monkeypatch.setenv("PGPASSWORD", "inherited-secret")
    dsn = (
        "postgresql://backup_user:dsn-secret@127.0.0.1:5432/memplex"
        "?sslmode=require&options=-csearch_path%3Dtenant_a"
    )

    environment = _libpq_environment(dsn)

    assert environment == {
        "LANG": "C",
        "LC_ALL": "C",
        "PGDATABASE": "memplex",
        "PGHOST": "127.0.0.1",
        "PGOPTIONS": "-csearch_path=tenant_a",
        "PGPASSWORD": "dsn-secret",
        "PGPORT": "5432",
        "PGSSLMODE": "require",
        "PGUSER": "backup_user",
    }
    assert dsn not in repr(environment)
    assert "inherited-secret" not in repr(environment)


def test_client_tool_discovery_requires_matching_pg_dump_and_restore(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: None)

    with pytest.raises(BackupConfigurationError, match="postgres_client_tools_missing"):
        PostgresClientTools.discover()


def test_pg_dump_argv_has_no_dsn_password_or_shell(tmp_path: Path) -> None:
    tools = PostgresClientTools(
        pg_dump=Path("/opt/postgresql/bin/pg_dump"),
        pg_restore=Path("/opt/postgresql/bin/pg_restore"),
        pg_dump_version="16.2",
        pg_restore_version="16.2",
    )
    executor = PostgresBackupExecutor(expected_target=_target(), tools=tools)
    payload = tmp_path / "payload.dump"
    dsn = "postgresql://backup_user:top-secret@127.0.0.1:5432/memplex"

    argv = executor._pg_dump_argv(payload)

    assert argv == (
        "/opt/postgresql/bin/pg_dump",
        "--format=custom",
        "--compress=9",
        "--schema=tenant_a",
        "--no-password",
        f"--file={payload}",
    )
    assert dsn not in " ".join(argv)
    assert "top-secret" not in " ".join(argv)
    assert all(item not in {"sh", "bash", "zsh", "-c"} for item in argv)

    restore_argv = executor._pg_restore_argv(payload)
    assert restore_argv == (
        "/opt/postgresql/bin/pg_restore",
        "--single-transaction",
        "--exit-on-error",
        "--no-password",
        "--dbname=memplex",
        str(payload),
    )
    assert dsn not in " ".join(restore_argv)
    assert "top-secret" not in " ".join(restore_argv)


@pytest.mark.parametrize("client_version,server_version", (("15.9", "16.2"), ("17", "16")))
def test_backup_rejects_tool_server_major_mismatch_before_artifact(
    tmp_path: Path, client_version: str, server_version: str
) -> None:
    tools = PostgresClientTools(
        pg_dump=Path("/opt/postgresql/bin/pg_dump"),
        pg_restore=Path("/opt/postgresql/bin/pg_restore"),
        pg_dump_version=client_version,
        pg_restore_version=client_version,
    )
    executor = PostgresBackupExecutor(expected_target=_target(), tools=tools)

    with pytest.raises(BackupIntegrityError, match="postgres_client_server_major_mismatch"):
        executor._require_matching_major(server_version)

    assert not list(tmp_path.iterdir())


def test_backup_executor_rejects_weak_expected_target() -> None:
    with pytest.raises(BackupConfigurationError, match="postgres_backup_target_invalid"):
        PostgresBackupExecutor(expected_target={"database": "memplex"})  # type: ignore[arg-type]


def test_libpq_environment_rejects_invalid_dsn_without_exposing_it() -> None:
    secret = "not-a-valid-dsn:password=should-never-appear"

    with pytest.raises(BackupConfigurationError, match="postgres_backup_dsn_invalid") as raised:
        _libpq_environment(secret)

    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)
    assert "password" not in str(raised.value)


def test_restore_consumes_the_same_artifact_inode_that_was_verified(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = bytes(range(32))

    def _artifact(root_name: str, backup_id: str, payload: bytes) -> Path:
        source = tmp_path / f"{root_name}.dump"
        source.write_bytes(payload)
        return BackupArtifactWriter(
            tmp_path / root_name, key=key, key_id="key", max_bytes=1024
        ).publish(
            manifest_fields={
                "format_version": 1,
                "backup_id": backup_id,
                "created_at": "2026-08-11T12:00:00.000000Z",
                "backend": "postgres",
                "database": "memplex",
                "schema": "tenant_a",
                "migration_version": 5,
                "payload_name": "payload.dump",
                "pg_dump_version": "16.2",
                "server_version": "16.2",
                "consistency": "pg_dump_snapshot",
            },
            payload_source=source,
        )

    original = _artifact(
        "original-root", "018f7f1d-7c9e-7c31-9d34-35f6a91e2bb8", b"ORIGINAL"
    )
    replacement = _artifact(
        "replacement-root", "018f7f1d-7c9e-7c31-9d34-35f6a91e2bb9", b"EVIL"
    )
    detached = tmp_path / "detached-original"
    tools = PostgresClientTools(
        pg_dump=Path("/opt/postgresql/bin/pg_dump"),
        pg_restore=Path("/opt/postgresql/bin/pg_restore"),
        pg_dump_version="16.2",
        pg_restore_version="16.2",
    )
    executor = PostgresBackupExecutor(expected_target=_target(), tools=tools)
    inspected = 0

    def _inspect(_dsn: str, _schema: str) -> tuple[str, bool, int]:
        nonlocal inspected
        inspected += 1
        if inspected == 1:
            original.rename(detached)
            replacement.rename(original)
            return "16.2", False, 0
        return "16.2", True, 5

    consumed: list[bytes] = []

    def _run(argv: tuple[str, ...], **_kwargs: object) -> SimpleNamespace:
        consumed.append(Path(argv[-1]).read_bytes())
        return SimpleNamespace(returncode=0)

    class _Runner:
        def __init__(self, *_args: object, **_kwargs: object) -> None:
            pass

        def status(self, **_kwargs: object) -> SimpleNamespace:
            return SimpleNamespace(state="ready", current_version=5)

    monkeypatch.setattr(executor, "_inspect_restore_state", _inspect)
    monkeypatch.setattr("subprocess.run", _run)
    monkeypatch.setattr("memplex.storage.postgres_backup.PostgresMigrationRunner", _Runner)

    result = executor.restore(
        migration_dsn="postgresql://user:password@127.0.0.1:5432/memplex",
        artifact=original,
        signing_key=key,
        target_schema="tenant_a",
    )

    assert result.backup_id == "018f7f1d-7c9e-7c31-9d34-35f6a91e2bb8"
    assert consumed == [b"ORIGINAL"]
    assert (detached / "payload.dump").read_bytes() == b"ORIGINAL"
