"""Data-only backup CLI boundaries."""

from __future__ import annotations

import json

from memplex.adapters import cli
from memplex.backup import BackupVerification, RestoreResult


def test_backup_parser_exposes_create_verify_restore_and_pitr() -> None:
    parser = cli.build_parser()

    create = parser.parse_args(
        ["storage", "backup", "create", "--destination", "/tmp/backups"]
    )
    verify = parser.parse_args(["storage", "backup", "verify", "/tmp/artifact"])
    restore = parser.parse_args(
        [
            "storage",
            "backup",
            "restore",
            "/tmp/artifact",
            "--target-schema",
            "tenant_a",
        ]
    )
    pitr = parser.parse_args(["storage", "backup", "pitr-status"])

    assert (create.storage_command, create.backup_command) == ("backup", "create")
    assert (verify.storage_command, verify.backup_command) == ("backup", "verify")
    assert (restore.storage_command, restore.target_schema) == ("backup", "tenant_a")
    assert (pitr.storage_command, pitr.backup_command) == ("backup", "pitr-status")


def test_backup_cli_never_constructs_service(monkeypatch, capsys) -> None:
    class Context:
        def create(self, _destination):
            return {
                "backup_id": "018f7f1d-7c9e-7c31-9d34-35f6a91e2bb8",
                "backend": "postgres",
                "database": "memplex",
                "schema": "tenant_a",
                "migration_version": 5,
                "payload_size": 123,
                "verified": True,
            }

    monkeypatch.setattr(cli, "_make_service", lambda *_: (_ for _ in ()).throw(AssertionError))
    monkeypatch.setattr(cli, "_build_backup_command_context", lambda _path: Context())

    result = cli.main(
        [
            "--output",
            "json",
            "storage",
            "backup",
            "create",
            "--destination",
            "/private/secret/backups",
        ]
    )

    assert result == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["backup_id"] == "018f7f1d-7c9e-7c31-9d34-35f6a91e2bb8"
    assert "/private/secret" not in json.dumps(payload)


def test_backup_verify_and_restore_emit_fixed_public_shapes(monkeypatch, capsys) -> None:
    class Context:
        def verify(self, _artifact):
            return BackupVerification(True, "backup-id", "postgres", "db", "tenant", 99)

        def restore(self, _artifact, _schema):
            return RestoreResult("backup-id", "db", "tenant", True, 1.25)

    monkeypatch.setattr(cli, "_build_backup_command_context", lambda _path: Context())
    monkeypatch.setattr(
        cli, "_build_backup_verification_context", lambda _path: Context()
    )

    assert cli.main(["--output", "json", "storage", "backup", "verify", "/secret/a"]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified == {
        "backup_id": "backup-id",
        "backend": "postgres",
        "database": "db",
        "payload_size": 99,
        "schema": "tenant",
        "verified": True,
    }

    assert (
        cli.main(
            [
                "--output",
                "json",
                "storage",
                "backup",
                "restore",
                "/secret/a",
                "--target-schema",
                "tenant",
            ]
        )
        == 0
    )
    restored = json.loads(capsys.readouterr().out)
    assert restored == {
        "backup_id": "backup-id",
        "database": "db",
        "elapsed_seconds": 1.25,
        "restored": True,
        "schema": "tenant",
    }


def test_backup_cli_redacts_paths_secrets_and_driver_errors(monkeypatch, capsys) -> None:
    secret = "postgresql://admin:top-secret@db.example/memplex"

    def _fail(_path):
        raise RuntimeError(f"driver exploded {secret} /private/artifact")

    monkeypatch.setattr(cli, "_build_backup_verification_context", _fail)

    result = cli.main(
        ["--output", "json", "storage", "backup", "verify", "/private/artifact"]
    )

    assert result == 1
    output = capsys.readouterr()
    combined = output.out + output.err
    assert "top-secret" not in combined
    assert "/private/artifact" not in combined
    assert "driver exploded" not in combined
    assert json.loads(output.err)["code"] == "backup_command_failed"


def test_backup_drill_emits_complete_signed_report_for_offline_verification(
    monkeypatch, capsys
) -> None:
    report = {
        "backup_id": "018f7f1d-7c9e-7c31-9d34-35f6a91e2bb8",
        "backup_completed_at": "2026-08-11T12:00:00.000000Z",
        "fault_cutoff_at": "2026-08-11T12:00:05.000000Z",
        "restore_started_at": "2026-08-11T12:01:00.000000Z",
        "restore_verified_at": "2026-08-11T12:01:10.000000Z",
        "observed_rpo_seconds": 5.0,
        "observed_rto_seconds": 10.0,
        "rpo_target_seconds": 300,
        "rto_target_seconds": 1800,
        "data_digest": "a" * 64,
        "data_verified": True,
        "pitr_ready": True,
        "industrial_gate_closing": True,
        "key_id": "g005-key",
        "signature": "b" * 64,
    }

    class Context:
        def drill(self, _artifact, _target_schema):
            return report

    monkeypatch.setattr(cli, "_build_backup_command_context", lambda _path: Context())

    result = cli.main(
        [
            "--output",
            "json",
            "storage",
            "backup",
            "drill",
            "--artifact",
            "/secret/artifact",
            "--target-schema",
            "tenant_a",
        ]
    )

    assert result == 0
    assert json.loads(capsys.readouterr().out) == report
