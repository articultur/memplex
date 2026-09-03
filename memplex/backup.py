"""Strict backup manifests and disaster-recovery data contracts."""

from __future__ import annotations

import base64
import binascii
import ctypes
import hashlib
import hmac
import json
import math
import os
import re
import stat
import sys
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timezone
from pathlib import Path, PurePath
from typing import Any, Self


class BackupIntegrityError(RuntimeError):
    """Raised when a backup artifact or manifest cannot be trusted."""


class BackupConfigurationError(RuntimeError):
    """Raised when the operator backup boundary is incomplete or unsafe."""


_MANIFEST_KEYS = frozenset(
    {
        "format_version",
        "backup_id",
        "created_at",
        "backend",
        "database",
        "schema",
        "migration_version",
        "payload_name",
        "payload_sha256",
        "payload_size",
        "pg_dump_version",
        "server_version",
        "consistency",
        "key_id",
        "signature",
    }
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_UTC_MICROSECOND_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z$"
)
_PUBLISHED_PAYLOAD_NAME = "payload.dump"
_PUBLISHED_MANIFEST_NAME = "manifest.json"
_MAX_MANIFEST_BYTES = 1024 * 1024
_MANIFEST_INPUT_KEYS = _MANIFEST_KEYS - {
    "payload_sha256",
    "payload_size",
    "key_id",
    "signature",
}


def _invalid_manifest() -> BackupIntegrityError:
    return BackupIntegrityError("backup_manifest_invalid")


def _require_string(value: object, *, basename: bool = False) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise _invalid_manifest()
    if "\x00" in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise _invalid_manifest()
    if basename and (
        PurePath(value).name != value or "/" in value or "\\" in value or value in {".", ".."}
    ):
        raise _invalid_manifest()
    return value


def _require_nonnegative_int(value: object) -> int:
    if type(value) is not int or value < 0:
        raise _invalid_manifest()
    return value


def _require_utc_microsecond(value: object) -> str:
    text = _require_string(value)
    if _UTC_MICROSECOND_RE.fullmatch(text) is None:
        raise _invalid_manifest()
    try:
        parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    except ValueError as exc:
        raise _invalid_manifest() from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ") != text:
        raise _invalid_manifest()
    return text


@dataclass(frozen=True, slots=True)
class BackupManifest:
    """Signed, exact-schema description of one published backup payload."""

    format_version: int
    backup_id: str
    created_at: str
    backend: str
    database: str
    schema: str
    migration_version: int
    payload_name: str
    payload_sha256: str
    payload_size: int
    pg_dump_version: str
    server_version: str
    consistency: str
    key_id: str
    signature: str

    @classmethod
    def from_dict(cls, raw: object) -> BackupManifest:
        try:
            if type(raw) is not dict or set(raw) != _MANIFEST_KEYS:
                raise _invalid_manifest()
            format_version = _require_nonnegative_int(raw["format_version"])
            if format_version != 1:
                raise _invalid_manifest()
            backup_id = _require_string(raw["backup_id"])
            parsed_id = uuid.UUID(backup_id)
            if str(parsed_id) != backup_id:
                raise _invalid_manifest()
            created_at = _require_utc_microsecond(raw["created_at"])
            backend = _require_string(raw["backend"])
            if backend not in {"postgres", "lite"}:
                raise _invalid_manifest()
            database = _require_string(raw["database"])
            schema = _require_string(raw["schema"])
            migration_version = _require_nonnegative_int(raw["migration_version"])
            payload_name = _require_string(raw["payload_name"], basename=True)
            payload_sha256 = _require_string(raw["payload_sha256"])
            if _SHA256_RE.fullmatch(payload_sha256) is None:
                raise _invalid_manifest()
            payload_size = _require_nonnegative_int(raw["payload_size"])
            pg_dump_version = _require_string(raw["pg_dump_version"])
            server_version = _require_string(raw["server_version"])
            consistency = _require_string(raw["consistency"])
            expected_consistency = {
                "postgres": "pg_dump_snapshot",
                "lite": "lite_pair_generation",
            }[backend]
            if consistency != expected_consistency:
                raise _invalid_manifest()
            key_id = _require_string(raw["key_id"])
            signature = _require_string(raw["signature"])
            if _SHA256_RE.fullmatch(signature) is None:
                raise _invalid_manifest()
            return cls(
                format_version=format_version,
                backup_id=backup_id,
                created_at=created_at,
                backend=backend,
                database=database,
                schema=schema,
                migration_version=migration_version,
                payload_name=payload_name,
                payload_sha256=payload_sha256,
                payload_size=payload_size,
                pg_dump_version=pg_dump_version,
                server_version=server_version,
                consistency=consistency,
                key_id=key_id,
                signature=signature,
            )
        except BackupIntegrityError:
            raise
        except (TypeError, ValueError, KeyError) as exc:
            raise _invalid_manifest() from exc

    def to_dict(self) -> dict[str, object]:
        return {
            "format_version": self.format_version,
            "backup_id": self.backup_id,
            "created_at": self.created_at,
            "backend": self.backend,
            "database": self.database,
            "schema": self.schema,
            "migration_version": self.migration_version,
            "payload_name": self.payload_name,
            "payload_sha256": self.payload_sha256,
            "payload_size": self.payload_size,
            "pg_dump_version": self.pg_dump_version,
            "server_version": self.server_version,
            "consistency": self.consistency,
            "key_id": self.key_id,
            "signature": self.signature,
        }

    def unsigned_dict(self) -> dict[str, object]:
        raw = self.to_dict()
        del raw["signature"]
        return raw

    @staticmethod
    def _canonical_bytes(raw: dict[str, object]) -> bytes:
        return json.dumps(
            raw,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def canonical_unsigned_bytes(self) -> bytes:
        return self._canonical_bytes(self.unsigned_dict())

    def canonical_bytes(self) -> bytes:
        return self._canonical_bytes(self.to_dict())

    def signed(self, key: bytes) -> BackupManifest:
        if type(key) is not bytes or len(key) != 32:
            raise BackupConfigurationError("backup_signing_key_invalid")
        signature = hmac.new(key, self.canonical_unsigned_bytes(), hashlib.sha256).hexdigest()
        return replace(self, signature=signature)

    def verify(self, key: bytes) -> None:
        if type(key) is not bytes or len(key) != 32:
            raise BackupConfigurationError("backup_signing_key_invalid")
        expected = hmac.new(
            key, self.canonical_unsigned_bytes(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, self.signature):
            raise BackupIntegrityError("backup_signature_invalid")


@dataclass(frozen=True, slots=True)
class BackupVerification:
    verified: bool
    backup_id: str
    backend: str
    database: str
    schema: str
    payload_size: int


class OpenedBackupArtifact:
    """One verified artifact pinned to directory and payload descriptors."""

    __slots__ = ("_directory_fd", "_payload_fd", "manifest", "verification")

    def __init__(
        self,
        *,
        directory_fd: int,
        payload_fd: int,
        manifest: BackupManifest,
        verification: BackupVerification,
    ) -> None:
        self._directory_fd = directory_fd
        self._payload_fd = payload_fd
        self.manifest = manifest
        self.verification = verification

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type: object, *_exc: object) -> None:
        try:
            self.close()
        except BaseException:
            if exc_type is None:
                raise

    def close(self) -> None:
        payload_fd, self._payload_fd = self._payload_fd, -1
        directory_fd, self._directory_fd = self._directory_fd, -1
        primary: BaseException | None = None
        if payload_fd >= 0:
            try:
                os.close(payload_fd)
            except BaseException as exc:  # noqa: BLE001 - shutdown/cleanup semantics; primary error stays authoritative
                primary = exc
        if directory_fd >= 0:
            try:
                os.close(directory_fd)
            except BaseException:
                if primary is None:
                    raise
        if primary is not None:
            raise primary

    def copy_payload_to(self, destination: Path) -> None:
        if self._payload_fd < 0:
            raise BackupIntegrityError("backup_artifact_invalid")
        destination_fd = -1
        try:
            destination_fd = os.open(
                destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600
            )
            os.fchmod(destination_fd, 0o600)
            digest = hashlib.sha256()
            size = 0
            offset = 0
            while True:
                if hasattr(os, "pread"):
                    chunk = os.pread(self._payload_fd, 1024 * 1024, offset)
                else:
                    os.lseek(self._payload_fd, offset, os.SEEK_SET)
                    chunk = os.read(self._payload_fd, 1024 * 1024)
                if not chunk:
                    break
                offset += len(chunk)
                size += len(chunk)
                if size > self.manifest.payload_size:
                    raise BackupIntegrityError("backup_artifact_invalid")
                digest.update(chunk)
                view = memoryview(chunk)
                while view:
                    written = os.write(destination_fd, view)
                    view = view[written:]
            os.fsync(destination_fd)
            metadata = os.fstat(self._payload_fd)
            if size != self.manifest.payload_size or metadata.st_size != size:
                raise BackupIntegrityError("backup_artifact_invalid")
            if not hmac.compare_digest(
                digest.hexdigest(), self.manifest.payload_sha256
            ):
                raise BackupIntegrityError("backup_artifact_invalid")
        except BackupIntegrityError:
            raise
        except OSError as exc:
            raise BackupIntegrityError("backup_artifact_invalid") from exc
        finally:
            if destination_fd >= 0:
                os.close(destination_fd)


@dataclass(frozen=True, slots=True)
class RestoreResult:
    backup_id: str
    database: str
    schema: str
    restored: bool
    elapsed_seconds: float


@dataclass(frozen=True, slots=True)
class PitrReadiness:
    ready: bool
    wal_level: str
    archive_mode: str
    archive_command_configured: bool
    full_page_writes: bool
    max_wal_senders: int

    def __post_init__(self) -> None:
        if (
            type(self.ready) is not bool
            or self.wal_level not in {"minimal", "replica", "logical"}
            or self.archive_mode not in {"off", "on", "always"}
            or type(self.archive_command_configured) is not bool
            or type(self.full_page_writes) is not bool
            or type(self.max_wal_senders) is not int
            or self.max_wal_senders < 0
        ):
            raise BackupIntegrityError("postgres_pitr_settings_invalid")
        expected = (
            self.wal_level in {"replica", "logical"}
            and self.archive_mode in {"on", "always"}
            and self.archive_command_configured
            and self.full_page_writes
            and self.max_wal_senders > 0
        )
        if self.ready is not expected:
            raise BackupIntegrityError("postgres_pitr_settings_invalid")


@dataclass(frozen=True, slots=True)
class DisasterRecoveryDrillResult:
    backup_id: str
    backup_completed_at: str
    fault_cutoff_at: str
    restore_started_at: str
    restore_verified_at: str
    observed_rpo_seconds: float
    observed_rto_seconds: float
    rpo_target_seconds: int
    rto_target_seconds: int
    data_digest: str
    data_verified: bool
    pitr_ready: bool
    industrial_gate_closing: bool
    key_id: str
    signature: str

    @classmethod
    def from_dict(cls, raw: object) -> DisasterRecoveryDrillResult:
        keys = {
            "backup_id",
            "backup_completed_at",
            "fault_cutoff_at",
            "restore_started_at",
            "restore_verified_at",
            "observed_rpo_seconds",
            "observed_rto_seconds",
            "rpo_target_seconds",
            "rto_target_seconds",
            "data_digest",
            "data_verified",
            "pitr_ready",
            "industrial_gate_closing",
            "key_id",
            "signature",
        }
        try:
            if type(raw) is not dict or set(raw) != keys:
                raise BackupIntegrityError("backup_drill_invalid")
            backup_id = _require_string(raw["backup_id"])
            if str(uuid.UUID(backup_id)) != backup_id:
                raise BackupIntegrityError("backup_drill_invalid")
            timestamps = tuple(
                _require_utc_microsecond(raw[name])
                for name in (
                    "backup_completed_at",
                    "fault_cutoff_at",
                    "restore_started_at",
                    "restore_verified_at",
                )
            )
            observed_rpo = raw["observed_rpo_seconds"]
            observed_rto = raw["observed_rto_seconds"]
            if (
                type(observed_rpo) is not float
                or not math.isfinite(observed_rpo)
                or observed_rpo < 0
                or type(observed_rto) is not float
                or not math.isfinite(observed_rto)
                or observed_rto < 0
            ):
                raise BackupIntegrityError("backup_drill_invalid")
            rpo_target = raw["rpo_target_seconds"]
            rto_target = raw["rto_target_seconds"]
            if (
                type(rpo_target) is not int
                or rpo_target <= 0
                or type(rto_target) is not int
                or rto_target <= 0
            ):
                raise BackupIntegrityError("backup_drill_invalid")
            data_digest = _require_string(raw["data_digest"])
            signature = _require_string(raw["signature"])
            if (
                _SHA256_RE.fullmatch(data_digest) is None
                or _SHA256_RE.fullmatch(signature) is None
                or type(raw["data_verified"]) is not bool
                or type(raw["pitr_ready"]) is not bool
                or type(raw["industrial_gate_closing"]) is not bool
            ):
                raise BackupIntegrityError("backup_drill_invalid")
            expected_gate = (
                raw["pitr_ready"]
                and raw["data_verified"]
                and observed_rpo <= rpo_target
                and observed_rto <= rto_target
            )
            if raw["industrial_gate_closing"] is not expected_gate:
                raise BackupIntegrityError("backup_drill_invalid")
            return cls(
                backup_id=backup_id,
                backup_completed_at=timestamps[0],
                fault_cutoff_at=timestamps[1],
                restore_started_at=timestamps[2],
                restore_verified_at=timestamps[3],
                observed_rpo_seconds=observed_rpo,
                observed_rto_seconds=observed_rto,
                rpo_target_seconds=rpo_target,
                rto_target_seconds=rto_target,
                data_digest=data_digest,
                data_verified=raw["data_verified"],
                pitr_ready=raw["pitr_ready"],
                industrial_gate_closing=raw["industrial_gate_closing"],
                key_id=_require_string(raw["key_id"]),
                signature=signature,
            )
        except BackupIntegrityError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise BackupIntegrityError("backup_drill_invalid") from exc

    def unsigned_dict(self) -> dict[str, object]:
        return {
            "backup_id": self.backup_id,
            "backup_completed_at": self.backup_completed_at,
            "fault_cutoff_at": self.fault_cutoff_at,
            "restore_started_at": self.restore_started_at,
            "restore_verified_at": self.restore_verified_at,
            "observed_rpo_seconds": self.observed_rpo_seconds,
            "observed_rto_seconds": self.observed_rto_seconds,
            "rpo_target_seconds": self.rpo_target_seconds,
            "rto_target_seconds": self.rto_target_seconds,
            "data_digest": self.data_digest,
            "data_verified": self.data_verified,
            "pitr_ready": self.pitr_ready,
            "industrial_gate_closing": self.industrial_gate_closing,
            "key_id": self.key_id,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self.unsigned_dict(), "signature": self.signature}

    def canonical_unsigned_bytes(self) -> bytes:
        return BackupManifest._canonical_bytes(self.unsigned_dict())

    def canonical_bytes(self) -> bytes:
        return BackupManifest._canonical_bytes(self.to_dict())

    def verify(self, key: bytes) -> None:
        _require_signing_key(key)
        expected = hmac.new(
            key, self.canonical_unsigned_bytes(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, self.signature):
            raise BackupIntegrityError("backup_drill_signature_invalid")


def load_backup_signing_key(environ: dict[str, str] | None = None) -> bytes:
    """Load one canonical base64-encoded 32-byte backup HMAC key."""
    source: dict[str, str] | os._Environ[str] = os.environ if environ is None else environ
    encoded = source.get("MEMPLEX_BACKUP_HMAC_KEY")
    if type(encoded) is not str or not encoded or encoded != encoded.strip():
        raise BackupConfigurationError("backup_signing_key_invalid")
    try:
        key = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise BackupConfigurationError("backup_signing_key_invalid") from exc
    if len(key) != 32 or base64.b64encode(key).decode("ascii") != encoded:
        raise BackupConfigurationError("backup_signing_key_invalid")
    return key


def manifest_from_json(data: bytes) -> BackupManifest:
    """Decode exact UTF-8 JSON without accepting duplicate object keys."""

    def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise BackupIntegrityError("backup_manifest_invalid")
            result[key] = value
        return result

    try:
        raw = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupIntegrityError("backup_manifest_invalid") from exc
    return BackupManifest.from_dict(raw)


def run_restore_drill(
    *,
    backup_id: str,
    backup_completed_at: str,
    fault_cutoff_at: str,
    restore_started_at: str,
    restore_verified_at: str,
    rpo_target_seconds: int,
    rto_target_seconds: int,
    data_digest: str,
    data_verified: bool,
    pitr: PitrReadiness,
    key_id: str,
    signing_key: bytes,
) -> DisasterRecoveryDrillResult:
    """Create a signed, measured DR-drill result without retaining secrets."""
    try:
        parsed_id = uuid.UUID(_require_string(backup_id))
        if str(parsed_id) != backup_id:
            raise _invalid_manifest()
        completed_text = _require_utc_microsecond(backup_completed_at)
        cutoff_text = _require_utc_microsecond(fault_cutoff_at)
        started_text = _require_utc_microsecond(restore_started_at)
        verified_text = _require_utc_microsecond(restore_verified_at)
        if type(rpo_target_seconds) is not int or rpo_target_seconds <= 0:
            raise _invalid_manifest()
        if type(rto_target_seconds) is not int or rto_target_seconds <= 0:
            raise _invalid_manifest()
        if type(data_verified) is not bool or type(pitr) is not PitrReadiness:
            raise _invalid_manifest()
        digest = _require_string(data_digest)
        if _SHA256_RE.fullmatch(digest) is None:
            raise _invalid_manifest()
        validated_key_id = _require_string(key_id)
        key = _require_signing_key(signing_key)
        completed = datetime.strptime(completed_text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
        cutoff = datetime.strptime(cutoff_text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
        started = datetime.strptime(started_text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
        verified = datetime.strptime(verified_text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
        observed_rpo = (cutoff - completed).total_seconds()
        observed_rto = (verified - started).total_seconds()
        if observed_rpo < 0 or observed_rto < 0:
            raise _invalid_manifest()
        gate = (
            pitr.ready
            and data_verified
            and observed_rpo <= rpo_target_seconds
            and observed_rto <= rto_target_seconds
        )
        unsigned = DisasterRecoveryDrillResult(
            backup_id=backup_id,
            backup_completed_at=completed_text,
            fault_cutoff_at=cutoff_text,
            restore_started_at=started_text,
            restore_verified_at=verified_text,
            observed_rpo_seconds=observed_rpo,
            observed_rto_seconds=observed_rto,
            rpo_target_seconds=rpo_target_seconds,
            rto_target_seconds=rto_target_seconds,
            data_digest=digest,
            data_verified=data_verified,
            pitr_ready=pitr.ready,
            industrial_gate_closing=gate,
            key_id=validated_key_id,
            signature="0" * 64,
        )
        signature = hmac.new(
            key, unsigned.canonical_unsigned_bytes(), hashlib.sha256
        ).hexdigest()
        return replace(unsigned, signature=signature)
    except BackupConfigurationError:
        raise
    except BackupIntegrityError:
        raise
    except (TypeError, ValueError) as exc:
        raise BackupIntegrityError("backup_drill_invalid") from exc


def drill_result_from_json(data: bytes) -> DisasterRecoveryDrillResult:
    """Decode one exact signed DR result without accepting duplicate keys."""

    def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise BackupIntegrityError("backup_drill_invalid")
            result[key] = value
        return result

    try:
        raw = json.loads(data.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupIntegrityError("backup_drill_invalid") from exc
    return DisasterRecoveryDrillResult.from_dict(raw)


def _require_signing_key(key: bytes) -> bytes:
    if type(key) is not bytes or len(key) != 32:
        raise BackupConfigurationError("backup_signing_key_invalid")
    return key


def _open_regular_readonly(path: Path) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise BackupIntegrityError("backup_artifact_invalid")
        return descriptor, metadata
    except BaseException:
        os.close(descriptor)
        raise


def _open_regular_at(directory_fd: int, name: str) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(name, flags, dir_fd=directory_fd)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise BackupIntegrityError("backup_artifact_invalid")
        return descriptor, metadata
    except BaseException:
        os.close(descriptor)
        raise


def _rename_directory_noreplace(
    source: str,
    destination: str,
    *,
    source_dir_fd: int,
    destination_dir_fd: int,
) -> None:
    """Atomically publish a directory without replacing an existing name."""
    source_text = os.fsdecode(source)
    destination_text = os.fsdecode(destination)
    if sys.platform == "darwin":
        library = ctypes.CDLL(None, use_errno=True)
        rename_exclusive = library.renameatx_np
        rename_exclusive.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        rename_exclusive.restype = ctypes.c_int
        result = rename_exclusive(
            source_dir_fd,
            os.fsencode(source_text),
            destination_dir_fd,
            os.fsencode(destination_text),
            0x00000004,
        )
    elif sys.platform.startswith("linux"):
        library = ctypes.CDLL(None, use_errno=True)
        try:
            rename_exclusive = library.renameat2
        except AttributeError as exc:
            raise BackupConfigurationError("backup_atomic_publish_unsupported") from exc
        rename_exclusive.argtypes = (
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_uint,
        )
        rename_exclusive.restype = ctypes.c_int
        result = rename_exclusive(
            source_dir_fd,
            os.fsencode(source_text),
            destination_dir_fd,
            os.fsencode(destination_text),
            0x00000001,
        )
    else:
        raise BackupConfigurationError("backup_atomic_publish_unsupported")
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number), destination_text)


def _open_directory_fd(path: Path) -> tuple[int, os.stat_result]:
    if os.name != "posix":
        raise BackupConfigurationError("backup_atomic_publish_unsupported")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISDIR(metadata.st_mode):
            raise BackupIntegrityError("backup_artifact_invalid")
        return descriptor, metadata
    except BaseException:
        os.close(descriptor)
        raise


def _directory_identity_matches(path: Path, expected: os.stat_result) -> bool:
    try:
        current = path.lstat()
    except OSError:
        return False
    return (
        stat.S_ISDIR(current.st_mode)
        and not path.is_symlink()
        and current.st_dev == expected.st_dev
        and current.st_ino == expected.st_ino
    )


def _cleanup_temporary_artifact(
    root_fd: int, temporary_fd: int, temporary_name: str
) -> None:
    if temporary_fd >= 0:
        for member in (_PUBLISHED_MANIFEST_NAME, _PUBLISHED_PAYLOAD_NAME):
            try:
                os.unlink(member, dir_fd=temporary_fd)
            except OSError:
                pass
        try:
            os.close(temporary_fd)
        except OSError:
            pass
    try:
        os.rmdir(temporary_name, dir_fd=root_fd)
    except OSError:
        pass


class BackupArtifactWriter:
    """Publish one signed backup artifact without exposing a partial final path."""

    def __init__(self, root: Path, *, key: bytes, key_id: str, max_bytes: int) -> None:
        self._root = Path(root)
        self._key = _require_signing_key(key)
        try:
            self._key_id = _require_string(key_id)
        except BackupIntegrityError as exc:
            raise BackupConfigurationError("backup_key_id_invalid") from exc
        if type(max_bytes) is not int or max_bytes <= 0:
            raise BackupConfigurationError("backup_max_artifact_bytes_invalid")
        self._max_bytes = max_bytes

    def publish(
        self,
        *,
        manifest_fields: dict[str, object],
        payload_source: Path,
    ) -> Path:
        temporary_name: str | None = None
        temporary_fd = -1
        root_fd = -1
        final: Path | None = None
        renamed = False
        source_fd = -1
        try:
            if type(manifest_fields) is not dict or set(manifest_fields) != _MANIFEST_INPUT_KEYS:
                raise BackupIntegrityError("backup_manifest_invalid")
            source = Path(payload_source)
            source_fd, _ = _open_regular_readonly(source)

            self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
            root_fd, root_metadata = _open_directory_fd(self._root)
            os.fchmod(root_fd, 0o700)
            if not _directory_identity_matches(self._root, root_metadata):
                raise BackupIntegrityError("backup_artifact_invalid")

            backup_id = manifest_fields.get("backup_id")
            if type(backup_id) is not str:
                raise BackupIntegrityError("backup_manifest_invalid")
            final = self._root / backup_id
            temporary_name = f".backup-{uuid.uuid4().hex}.tmp"
            os.mkdir(temporary_name, mode=0o700, dir_fd=root_fd)
            temporary_fd = os.open(
                temporary_name,
                os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                dir_fd=root_fd,
            )
            os.fchmod(temporary_fd, 0o700)
            digest = hashlib.sha256()
            size = 0
            destination_fd = os.open(
                _PUBLISHED_PAYLOAD_NAME,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=temporary_fd,
            )
            try:
                os.fchmod(destination_fd, 0o600)
                while chunk := os.read(source_fd, 1024 * 1024):
                    size += len(chunk)
                    if size > self._max_bytes:
                        raise BackupIntegrityError("backup_payload_too_large")
                    digest.update(chunk)
                    view = memoryview(chunk)
                    while view:
                        written = os.write(destination_fd, view)
                        view = view[written:]
                os.fsync(destination_fd)
            finally:
                os.close(destination_fd)

            raw = dict(manifest_fields)
            raw.update(
                {
                    "payload_sha256": digest.hexdigest(),
                    "payload_size": size,
                    "key_id": self._key_id,
                    "signature": "0" * 64,
                }
            )
            manifest = BackupManifest.from_dict(raw).signed(self._key)
            manifest_fd = os.open(
                _PUBLISHED_MANIFEST_NAME,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=temporary_fd,
            )
            try:
                os.fchmod(manifest_fd, 0o600)
                encoded = manifest.canonical_bytes()
                view = memoryview(encoded)
                while view:
                    written = os.write(manifest_fd, view)
                    view = view[written:]
                os.fsync(manifest_fd)
            finally:
                os.close(manifest_fd)

            os.fsync(temporary_fd)
            if not _directory_identity_matches(self._root, root_metadata):
                raise BackupIntegrityError("backup_artifact_invalid")
            _rename_directory_noreplace(
                temporary_name,
                backup_id,
                source_dir_fd=root_fd,
                destination_dir_fd=root_fd,
            )
            renamed = True
            os.close(temporary_fd)
            temporary_fd = -1
            temporary_name = None
            os.fsync(root_fd)
            if not _directory_identity_matches(self._root, root_metadata):
                raise BackupIntegrityError("backup_artifact_invalid")
            return final
        except BackupConfigurationError:
            if temporary_name is not None and root_fd >= 0:
                _cleanup_temporary_artifact(root_fd, temporary_fd, temporary_name)
                temporary_fd = -1
            raise
        except Exception as exc:
            if temporary_name is not None and root_fd >= 0:
                _cleanup_temporary_artifact(root_fd, temporary_fd, temporary_name)
                temporary_fd = -1
            if renamed:
                raise BackupIntegrityError("backup_publish_outcome_unknown") from exc
            raise BackupIntegrityError("backup_publish_failed") from exc
        except BaseException:
            if temporary_name is not None and root_fd >= 0:
                _cleanup_temporary_artifact(root_fd, temporary_fd, temporary_name)
                temporary_fd = -1
            raise
        finally:
            if temporary_fd >= 0:
                os.close(temporary_fd)
            if root_fd >= 0:
                os.close(root_fd)
            if source_fd >= 0:
                os.close(source_fd)


def open_verified_backup_artifact(path: Path, key: bytes) -> OpenedBackupArtifact:
    """Open and verify one artifact while pinning its directory and payload inode."""
    _require_signing_key(key)
    artifact_fd = -1
    manifest_fd = -1
    payload_fd = -1
    try:
        artifact = Path(path)
        artifact_fd, _ = _open_directory_fd(artifact)
        children = os.listdir(artifact_fd)
        if (
            len(children) != 2
            or set(children) != {_PUBLISHED_MANIFEST_NAME, _PUBLISHED_PAYLOAD_NAME}
        ):
            raise BackupIntegrityError("backup_artifact_invalid")

        manifest_fd, manifest_metadata = _open_regular_at(
            artifact_fd, _PUBLISHED_MANIFEST_NAME
        )
        if manifest_metadata.st_size > _MAX_MANIFEST_BYTES:
            raise BackupIntegrityError("backup_artifact_invalid")
        manifest_bytes = bytearray()
        while chunk := os.read(manifest_fd, 64 * 1024):
            manifest_bytes.extend(chunk)
            if len(manifest_bytes) > _MAX_MANIFEST_BYTES:
                raise BackupIntegrityError("backup_artifact_invalid")
        os.close(manifest_fd)
        manifest_fd = -1
        manifest = manifest_from_json(bytes(manifest_bytes))
        if manifest.payload_name != _PUBLISHED_PAYLOAD_NAME:
            raise BackupIntegrityError("backup_artifact_invalid")
        manifest.verify(key)

        payload_fd, payload_metadata = _open_regular_at(
            artifact_fd, _PUBLISHED_PAYLOAD_NAME
        )
        digest = hashlib.sha256()
        size = 0
        while chunk := os.read(payload_fd, 1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
        if size != manifest.payload_size or payload_metadata.st_size != manifest.payload_size:
            raise BackupIntegrityError("backup_artifact_invalid")
        if not hmac.compare_digest(digest.hexdigest(), manifest.payload_sha256):
            raise BackupIntegrityError("backup_artifact_invalid")
        os.lseek(payload_fd, 0, os.SEEK_SET)
        verification = BackupVerification(
            verified=True,
            backup_id=manifest.backup_id,
            backend=manifest.backend,
            database=manifest.database,
            schema=manifest.schema,
            payload_size=manifest.payload_size,
        )
        opened = OpenedBackupArtifact(
            directory_fd=artifact_fd,
            payload_fd=payload_fd,
            manifest=manifest,
            verification=verification,
        )
        artifact_fd = -1
        payload_fd = -1
        return opened
    except BackupConfigurationError:
        raise
    except Exception as exc:
        raise BackupIntegrityError("backup_artifact_invalid") from exc
    finally:
        if manifest_fd >= 0:
            os.close(manifest_fd)
        if payload_fd >= 0:
            os.close(payload_fd)
        if artifact_fd >= 0:
            os.close(artifact_fd)


def load_verified_backup_manifest(path: Path, key: bytes) -> BackupManifest:
    """Return the signed manifest from the same pinned artifact that was verified."""
    with open_verified_backup_artifact(path, key) as opened:
        return opened.manifest


def verify_backup_artifact(path: Path, key: bytes) -> BackupVerification:
    """Verify an artifact directory, its signature, and its payload digest."""
    with open_verified_backup_artifact(path, key) as opened:
        return opened.verification
