"""Backup manifest, signature, and disaster-recovery data contracts."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

import memplex.backup as backup_module
from memplex.backup import (
    BackupArtifactWriter,
    BackupConfigurationError,
    BackupIntegrityError,
    BackupManifest,
    PitrReadiness,
    load_backup_signing_key,
    run_restore_drill,
    verify_backup_artifact,
)
from memplex.models import Function, SourceDocument, SourceType
from memplex.storage.lite.store import LiteMemoryStore
from scripts.verify_g005_backup_restore import main as verify_g005_main


def _valid_manifest_dict() -> dict[str, object]:
    return {
        "format_version": 1,
        "backup_id": "018f7f1d-7c9e-7c31-9d34-35f6a91e2bb8",
        "created_at": "2026-08-11T12:34:56.123456Z",
        "backend": "postgres",
        "database": "memplex",
        "schema": "tenant_a",
        "migration_version": 5,
        "payload_name": "payload.dump",
        "payload_sha256": "a" * 64,
        "payload_size": 123,
        "pg_dump_version": "16.2",
        "server_version": "16.2",
        "consistency": "pg_dump_snapshot",
        "key_id": "backup-key-2026-08",
        "signature": "b" * 64,
    }


@pytest.mark.parametrize(
    "mutate",
    (
        lambda item: item.__setitem__("format_version", True),
        lambda item: item.__setitem__("future", 1),
        lambda item: item.__setitem__("payload_sha256", "0" * 63),
        lambda item: item.__setitem__("payload_name", "../payload.dump"),
        lambda item: item.__setitem__("created_at", "2026-08-11T24:00:00.000000Z"),
        lambda item: item.__setitem__("schema", "tenant\x00a"),
        lambda item: item.__setitem__("payload_size", -1),
    ),
)
def test_backup_manifest_rejects_weak_or_noncanonical_fields(mutate) -> None:
    raw = _valid_manifest_dict()
    mutate(raw)

    with pytest.raises(BackupIntegrityError, match="backup_manifest_invalid"):
        BackupManifest.from_dict(raw)


def test_backup_manifest_sign_and_verify_are_canonical_and_detached() -> None:
    key = bytes(range(32))
    raw = _valid_manifest_dict()
    raw["signature"] = "0" * 64
    manifest = BackupManifest.from_dict(raw).signed(key)
    encoded = manifest.canonical_bytes()

    assert json.loads(encoded)["signature"] == manifest.signature
    assert manifest.signature != "0" * 64
    manifest.verify(key)
    assert BackupManifest.from_dict(json.loads(encoded)) == manifest

    tampered = {**manifest.to_dict(), "payload_size": manifest.payload_size + 1}
    with pytest.raises(BackupIntegrityError, match="backup_signature_invalid"):
        BackupManifest.from_dict(tampered).verify(key)


@pytest.mark.parametrize(
    "encoded",
    (
        "not-base64",
        base64.b64encode(b"short").decode("ascii"),
        base64.b64encode(bytes(range(32))).decode("ascii").rstrip("="),
    ),
)
def test_backup_signing_key_rejects_invalid_or_noncanonical_base64(
    monkeypatch: pytest.MonkeyPatch, encoded: str
) -> None:
    monkeypatch.setenv("MEMPLEX_BACKUP_HMAC_KEY", encoded)

    with pytest.raises(BackupConfigurationError, match="backup_signing_key_invalid"):
        load_backup_signing_key()


def test_backup_signing_key_loads_exact_32_bytes_without_exposure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = bytes(range(32))
    encoded = base64.b64encode(key).decode("ascii")
    monkeypatch.setenv("MEMPLEX_BACKUP_HMAC_KEY", encoded)

    assert load_backup_signing_key() == key
    assert encoded not in repr(load_backup_signing_key)


def _manifest_fields() -> dict[str, object]:
    raw = _valid_manifest_dict()
    for key in ("payload_sha256", "payload_size", "key_id", "signature"):
        del raw[key]
    return raw


def _publish(tmp_path: Path) -> tuple[Path, bytes]:
    key = bytes(range(32))
    source = tmp_path / "source.dump"
    source.write_bytes(b"memplex-backup-payload")
    writer = BackupArtifactWriter(
        tmp_path / "backups",
        key=key,
        key_id="backup-key-2026-08",
        max_bytes=1024,
    )
    return writer.publish(manifest_fields=_manifest_fields(), payload_source=source), key


def test_backup_artifact_publish_and_verify_roundtrip(tmp_path: Path) -> None:
    artifact, key = _publish(tmp_path)

    verification = verify_backup_artifact(artifact, key)

    assert verification.verified is True
    assert verification.backup_id == _manifest_fields()["backup_id"]
    assert verification.payload_size == len(b"memplex-backup-payload")
    assert {item.name for item in artifact.iterdir()} == {"manifest.json", "payload.dump"}
    assert artifact.stat().st_mode & 0o777 == 0o700
    assert all(item.stat().st_mode & 0o777 == 0o600 for item in artifact.iterdir())


def test_backup_publish_pins_source_descriptor_before_path_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = bytes(range(32))
    source = tmp_path / "source.dump"
    source.write_bytes(b"trusted-payload")
    replacement = tmp_path / "replacement.dump"
    replacement.write_bytes(b"attacker-payload")
    root = tmp_path / "backups"
    writer = BackupArtifactWriter(root, key=key, key_id="key", max_bytes=1024)
    real_fchmod = os.fchmod
    swapped = False

    def _swap_after_source_open(descriptor: int, mode: int) -> None:
        nonlocal swapped
        if not swapped:
            os.replace(replacement, source)
            swapped = True
        real_fchmod(descriptor, mode)

    monkeypatch.setattr(os, "fchmod", _swap_after_source_open)

    artifact = writer.publish(
        manifest_fields=_manifest_fields(), payload_source=source
    )

    assert swapped is True
    assert source.read_bytes() == b"attacker-payload"
    assert (artifact / "payload.dump").read_bytes() == b"trusted-payload"
    assert verify_backup_artifact(artifact, key).verified is True


@pytest.mark.parametrize("fault_call", (1, 2, 3, 4))
def test_backup_publish_fsync_fault_never_exposes_partial_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault_call: int
) -> None:
    key = bytes(range(32))
    source = tmp_path / "source.dump"
    source.write_bytes(b"payload")
    root = tmp_path / "backups"
    writer = BackupArtifactWriter(root, key=key, key_id="key", max_bytes=1024)
    real_fsync = os.fsync
    calls = 0

    def _fault(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == fault_call:
            raise OSError("injected fsync failure")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", _fault)

    with pytest.raises(BackupIntegrityError):
        writer.publish(manifest_fields=_manifest_fields(), payload_source=source)

    final = root / str(_manifest_fields()["backup_id"])
    if fault_call < 4:
        assert not final.exists()
    else:
        assert verify_backup_artifact(final, key).verified is True
    assert not list(root.glob(".backup-*.tmp"))


def test_backup_publish_rename_failure_preserves_no_final_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = bytes(range(32))
    source = tmp_path / "source.dump"
    source.write_bytes(b"payload")
    root = tmp_path / "backups"
    writer = BackupArtifactWriter(root, key=key, key_id="key", max_bytes=1024)

    monkeypatch.setattr(
        backup_module,
        "_rename_directory_noreplace",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("rename")),
    )

    with pytest.raises(BackupIntegrityError, match="backup_publish_failed"):
        writer.publish(manifest_fields=_manifest_fields(), payload_source=source)

    assert not (root / str(_manifest_fields()["backup_id"])).exists()
    assert not list(root.glob(".backup-*.tmp"))


def test_backup_publish_never_replaces_concurrently_reserved_final_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = bytes(range(32))
    source = tmp_path / "source.dump"
    source.write_bytes(b"payload")
    root = tmp_path / "backups"
    final = root / str(_manifest_fields()["backup_id"])
    writer = BackupArtifactWriter(root, key=key, key_id="key", max_bytes=1024)
    real_rename = backup_module._rename_directory_noreplace
    injected = False

    def _reserve_before_publish(
        source_name: str,
        destination_name: str,
        *,
        source_dir_fd: int,
        destination_dir_fd: int,
    ) -> None:
        nonlocal injected
        if not injected:
            os.mkdir(destination_name, mode=0o700, dir_fd=destination_dir_fd)
            injected = True
        real_rename(
            source_name,
            destination_name,
            source_dir_fd=source_dir_fd,
            destination_dir_fd=destination_dir_fd,
        )

    monkeypatch.setattr(
        backup_module, "_rename_directory_noreplace", _reserve_before_publish
    )

    with pytest.raises(BackupIntegrityError, match="backup_publish_failed"):
        writer.publish(manifest_fields=_manifest_fields(), payload_source=source)

    assert injected is True
    assert final.is_dir()
    assert list(final.iterdir()) == []
    assert not list(root.glob(".backup-*.tmp"))


def test_backup_publish_pins_destination_root_descriptor_before_path_swap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = bytes(range(32))
    source = tmp_path / "source.dump"
    source.write_bytes(b"confidential-payload")
    root = tmp_path / "backups"
    detached_root = tmp_path / "detached-backups"
    attacker_root = tmp_path / "attacker"
    attacker_root.mkdir()
    writer = BackupArtifactWriter(root, key=key, key_id="key", max_bytes=1024)
    real_fchmod = os.fchmod
    swapped = False

    def _swap_root_after_open(descriptor: int, mode: int) -> None:
        nonlocal swapped
        if not swapped:
            os.rename(root, detached_root)
            root.symlink_to(attacker_root, target_is_directory=True)
            swapped = True
        real_fchmod(descriptor, mode)

    monkeypatch.setattr(os, "fchmod", _swap_root_after_open)

    with pytest.raises(BackupIntegrityError, match="backup_publish_failed"):
        writer.publish(manifest_fields=_manifest_fields(), payload_source=source)

    assert swapped is True
    assert list(attacker_root.iterdir()) == []
    assert list(detached_root.iterdir()) == []


@pytest.mark.parametrize("tamper", ("payload", "manifest", "extra", "symlink"))
def test_verify_rejects_tamper_extra_file_and_symlink(
    tmp_path: Path, tamper: str
) -> None:
    artifact, key = _publish(tmp_path)
    if tamper == "payload":
        (artifact / "payload.dump").write_bytes(b"tampered")
    elif tamper == "manifest":
        raw = json.loads((artifact / "manifest.json").read_text(encoding="utf-8"))
        raw["payload_size"] += 1
        (artifact / "manifest.json").write_text(json.dumps(raw), encoding="utf-8")
    elif tamper == "extra":
        (artifact / "extra").write_text("unexpected", encoding="utf-8")
    else:
        payload = artifact / "payload.dump"
        outside = tmp_path / "outside.dump"
        outside.write_bytes(payload.read_bytes())
        payload.unlink()
        payload.symlink_to(outside)

    with pytest.raises(BackupIntegrityError, match="backup_artifact_invalid"):
        verify_backup_artifact(artifact, key)


def test_verify_pins_artifact_directory_before_path_rebind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    key = bytes(range(32))
    original_source = tmp_path / "original.dump"
    original_source.write_bytes(b"original-payload")
    original = BackupArtifactWriter(
        tmp_path / "original-root", key=key, key_id="key", max_bytes=1024
    ).publish(manifest_fields=_manifest_fields(), payload_source=original_source)
    replacement_fields = _manifest_fields()
    replacement_fields["backup_id"] = "018f7f1d-7c9e-7c31-9d34-35f6a91e2bb9"
    replacement_source = tmp_path / "replacement.dump"
    replacement_source.write_bytes(b"replacement-payload")
    replacement = BackupArtifactWriter(
        tmp_path / "replacement-root", key=key, key_id="key", max_bytes=1024
    ).publish(
        manifest_fields=replacement_fields, payload_source=replacement_source
    )
    detached = tmp_path / "detached-original"
    real_listdir = os.listdir
    rebound = False

    def _rebind_after_directory_open(path: os.PathLike[str] | int) -> list[str]:
        nonlocal rebound
        if type(path) is int and not rebound:
            original.rename(detached)
            replacement.rename(original)
            rebound = True
        return real_listdir(path)

    monkeypatch.setattr(os, "listdir", _rebind_after_directory_open)

    verification = verify_backup_artifact(original, key)

    assert rebound is True
    assert verification.backup_id == _manifest_fields()["backup_id"]
    assert verification.payload_size == len(b"original-payload")


def test_lite_backup_restore_roundtrip_replaces_pair_and_rebuilds_fts(
    tmp_path: Path,
) -> None:
    path = tmp_path / "store" / "memory.json"
    store = LiteMemoryStore(path, deployment_profile="development")
    source = SourceDocument(type="test", source_type=SourceType.WIKI)
    store.add(Function(id="before", name="before", name_normalized="before"), source)
    manifest = store.create_backup(
        tmp_path / "backups", bytes(range(32)), "lite-key"
    )
    artifact = tmp_path / "backups" / manifest.backup_id
    store.delete("before")
    store.add(Function(id="after", name="after", name_normalized="after"), source)

    store.restore_backup(artifact, bytes(range(32)))

    assert store.get("before") is not None
    assert store.get("after") is None
    assert [item.func_id for item in store.fts_search("before", top_k=5)] == ["before"]
    assert store.fts_search("after", top_k=5) == []
    reopened = LiteMemoryStore(path, deployment_profile="development")
    assert reopened.get("before") is not None
    assert reopened.get("after") is None


def test_lite_backup_is_development_only_before_artifact_mutation(tmp_path: Path) -> None:
    store = LiteMemoryStore(
        tmp_path / "store" / "memory.json", deployment_profile="production"
    )

    with pytest.raises(BackupConfigurationError, match="lite_backup_development_only"):
        store.create_backup(tmp_path / "backups", bytes(range(32)), "key")

    assert not (tmp_path / "backups").exists()


def test_lite_restore_rejects_tampered_artifact_without_touching_current_pair(
    tmp_path: Path,
) -> None:
    path = tmp_path / "store" / "memory.json"
    store = LiteMemoryStore(path, deployment_profile="development")
    source = SourceDocument(type="test", source_type=SourceType.WIKI)
    store.add(Function(id="stable", name="stable", name_normalized="stable"), source)
    manifest = store.create_backup(
        tmp_path / "backups", bytes(range(32)), "lite-key"
    )
    artifact = tmp_path / "backups" / manifest.backup_id
    before = (path.read_bytes(), path.with_name("changelog.json").read_bytes())
    (artifact / "payload.dump").write_bytes(b"tampered")

    with pytest.raises(BackupIntegrityError, match="backup_artifact_invalid"):
        store.restore_backup(artifact, bytes(range(32)))

    assert (path.read_bytes(), path.with_name("changelog.json").read_bytes()) == before
    assert store.get("stable") is not None


def test_restore_drill_closes_only_when_pitr_data_rpo_and_rto_pass() -> None:
    key = bytes(range(32))
    ready = PitrReadiness(True, "replica", "on", True, True, 10)
    result = run_restore_drill(
        backup_id="018f7f1d-7c9e-7c31-9d34-35f6a91e2bb8",
        backup_completed_at="2026-08-11T12:00:00.000000Z",
        fault_cutoff_at="2026-08-11T12:01:00.000000Z",
        restore_started_at="2026-08-11T12:02:00.000000Z",
        restore_verified_at="2026-08-11T12:04:00.000000Z",
        rpo_target_seconds=300,
        rto_target_seconds=300,
        data_digest="a" * 64,
        data_verified=True,
        pitr=ready,
        key_id="dr-key",
        signing_key=key,
    )

    assert result.observed_rpo_seconds == 60.0
    assert result.observed_rto_seconds == 120.0
    assert result.industrial_gate_closing is True
    result.verify(key)


def test_restore_drill_fails_gate_when_rpo_rto_or_pitr_fails() -> None:
    result = run_restore_drill(
        backup_id="018f7f1d-7c9e-7c31-9d34-35f6a91e2bb8",
        backup_completed_at="2026-08-11T12:00:00.000000Z",
        fault_cutoff_at="2026-08-11T12:10:00.000000Z",
        restore_started_at="2026-08-11T12:11:00.000000Z",
        restore_verified_at="2026-08-11T13:00:00.000000Z",
        rpo_target_seconds=300,
        rto_target_seconds=300,
        data_digest="b" * 64,
        data_verified=True,
        pitr=PitrReadiness(False, "replica", "off", False, True, 10),
        key_id="dr-key",
        signing_key=bytes(range(32)),
    )

    assert result.industrial_gate_closing is False


def test_g005_verifier_accepts_only_matching_signed_passing_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    artifact, key = _publish(tmp_path)
    manifest = BackupManifest.from_dict(
        json.loads((artifact / "manifest.json").read_text(encoding="utf-8"))
    )
    report = run_restore_drill(
        backup_id=manifest.backup_id,
        backup_completed_at=manifest.created_at,
        fault_cutoff_at=manifest.created_at,
        restore_started_at=manifest.created_at,
        restore_verified_at=manifest.created_at,
        rpo_target_seconds=300,
        rto_target_seconds=300,
        data_digest=manifest.payload_sha256,
        data_verified=True,
        pitr=PitrReadiness(True, "replica", "on", True, True, 10),
        key_id="dr-key",
        signing_key=key,
    )
    report_path = tmp_path / "drill.json"
    report_path.write_bytes(report.canonical_bytes())
    monkeypatch.setenv("MEMPLEX_BACKUP_HMAC_KEY", base64.b64encode(key).decode("ascii"))

    result = verify_g005_main(
        ["--artifact", str(artifact), "--drill-report", str(report_path)]
    )

    assert result == 0
    output = json.loads(capsys.readouterr().out)
    assert output["status"] == "verified"
    assert output["gate"] == "backup_restore_dr"
