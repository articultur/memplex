"""Signed machine evidence for the four supported agent host lifecycles."""

from __future__ import annotations

import hmac
import json
import os
import stat
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any, Mapping

_SCHEMA_VERSION = 2
_HOSTS = ("claude-code", "codex", "hermes", "openclaw")
_CHECKS = (
    "capture_recall",
    "config_drift",
    "cross_host",
    "fault_rollback",
    "identity_isolation",
    "install",
    "real_cli_start",
    "restart",
    "uninstall_restore",
    "upgrade",
)
_SHA256_HEX_LENGTH = 64
_MAX_EVIDENCE_BYTES = 128 * 1024
_MAX_EVIDENCE_AGE = timedelta(hours=24)
_MAX_FUTURE_SKEW = timedelta(minutes=5)
_HERMES_REVISION = "3c27eb6234bf91b8ceee9e9071591b31e9b148cb"
_HERMES_PROVIDER_SHA256 = "678c9150852f2018182e08622ae25b495360fd5099747f823c35e00cce08d8dd"


class HostLifecycleIntegrityError(RuntimeError):
    """Stable, redacted failure for host lifecycle evidence."""

    def __init__(self, code: str = "host_lifecycle_integrity") -> None:
        self.code = code
        super().__init__("host lifecycle evidence integrity check failed")


def _fail(code: str = "host_lifecycle_integrity") -> None:
    raise HostLifecycleIntegrityError(code)


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise HostLifecycleIntegrityError() from exc


def _exact_text(value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        _fail()
    return value


def _sha256_text(value: object) -> str:
    text = _exact_text(value)
    if len(text) != _SHA256_HEX_LENGTH:
        _fail()
    try:
        bytes.fromhex(text)
    except ValueError as exc:
        raise HostLifecycleIntegrityError() from exc
    return text


def _timestamp(value: object) -> datetime:
    text = _exact_text(value)
    try:
        parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=timezone.utc
        )
    except ValueError as exc:
        raise HostLifecycleIntegrityError() from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ") != text:
        _fail()
    return parsed


def _open_parent_directory(path: Path) -> int:
    if not isinstance(path, Path) or not path.name or path.name in {".", ".."}:
        _fail("host_lifecycle_evidence_invalid")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent = path.parent
    directory_fd = os.open(os.sep if parent.is_absolute() else ".", flags)
    try:
        components = parent.parts[1:] if parent.is_absolute() else parent.parts
        for component in components:
            if component in {"", "."}:
                continue
            if component == "..":
                _fail("host_lifecycle_evidence_invalid")
            next_fd = os.open(component, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        return directory_fd
    except BaseException:
        os.close(directory_fd)
        raise


def _contract_files() -> dict[str, tuple[Path, ...]]:
    package_root = Path(__file__).resolve().parent
    plugin_root = package_root / "_plugin"
    installer = package_root / "adapters" / "agent_installer.py"
    shared = (
        plugin_root / "scripts" / "hook-runner.py",
        plugin_root / "scripts" / "mcp-server.sh",
    )
    return {
        "claude-code": (
            installer,
            plugin_root / ".claude-plugin" / "plugin.json",
            plugin_root / ".mcp.json",
            plugin_root / "hooks" / "hooks.json",
            *shared,
        ),
        "codex": (
            installer,
            plugin_root / ".codex-plugin" / "plugin.json",
            plugin_root / ".codex.mcp.json",
            plugin_root / "hooks" / "hooks-codex.json",
            plugin_root / "scripts" / "codex-plugin.sh",
            *shared,
        ),
        "openclaw": (
            installer,
            package_root / "adapters" / "openclaw_plugin.py",
        ),
        "hermes": (
            installer,
            package_root / "adapters" / "hermes_memory_provider.py",
        ),
    }


def current_host_contract_digests() -> dict[str, str]:
    """Bind evidence to the exact packaged integration code for every host."""

    result: dict[str, str] = {}
    for host, paths in _contract_files().items():
        digest = sha256()
        for path in paths:
            if path.is_symlink() or not path.is_file():
                _fail("host_contract_missing")
            payload = path.read_bytes()
            digest.update(path.name.encode("utf-8"))
            digest.update(b"\x00")
            digest.update(len(payload).to_bytes(8, "big"))
            digest.update(payload)
        result[host] = digest.hexdigest()
    return result


@dataclass(frozen=True, slots=True)
class HostLifecycleProof:
    host: str
    cli_version: str
    contract_sha256: str
    checks: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.host not in _HOSTS:
            _fail()
        _exact_text(self.cli_version)
        _sha256_text(self.contract_sha256)
        if type(self.checks) is not tuple or self.checks != _CHECKS:
            _fail("host_lifecycle_checks_incomplete")

    def to_dict(self) -> dict[str, object]:
        return {
            "host": self.host,
            "cli_version": self.cli_version,
            "contract_sha256": self.contract_sha256,
            "checks": list(self.checks),
        }

    @classmethod
    def from_dict(cls, payload: object) -> HostLifecycleProof:
        if type(payload) is not dict or frozenset(payload) != {
            "host",
            "cli_version",
            "contract_sha256",
            "checks",
        }:
            _fail()
        checks = payload["checks"]
        if type(checks) is not list or any(type(item) is not str for item in checks):
            _fail()
        return cls(
            host=payload["host"],  # type: ignore[arg-type]
            cli_version=payload["cli_version"],  # type: ignore[arg-type]
            contract_sha256=payload["contract_sha256"],  # type: ignore[arg-type]
            checks=tuple(checks),
        )


@dataclass(frozen=True, slots=True)
class HostLifecycleEvidence:
    schema_version: int
    memplex_version: str
    generated_at: str
    hermes_source_revision: str
    hermes_provider_sha256: str
    hosts: tuple[HostLifecycleProof, ...]
    key_id: str
    status: str
    signature: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != _SCHEMA_VERSION:
            _fail()
        _exact_text(self.memplex_version)
        _timestamp(self.generated_at)
        if self.hermes_source_revision != _HERMES_REVISION:
            _fail("hermes_source_revision_mismatch")
        if self.hermes_provider_sha256 != _HERMES_PROVIDER_SHA256:
            _fail("hermes_provider_digest_mismatch")
        if (
            type(self.hosts) is not tuple
            or tuple(item.host for item in self.hosts) != _HOSTS
            or any(type(item) is not HostLifecycleProof for item in self.hosts)
        ):
            _fail("host_lifecycle_matrix_incomplete")
        _exact_text(self.key_id)
        if self.status != "passed":
            _fail("host_lifecycle_not_passing")
        _sha256_text(self.signature)

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "memplex_version": self.memplex_version,
            "generated_at": self.generated_at,
            "hermes_source_revision": self.hermes_source_revision,
            "hermes_provider_sha256": self.hermes_provider_sha256,
            "hosts": [host.to_dict() for host in self.hosts],
            "key_id": self.key_id,
            "status": self.status,
            "signature": self.signature,
        }

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.to_dict())

    def canonical_unsigned_bytes(self) -> bytes:
        payload = self.to_dict()
        payload.pop("signature")
        return _canonical_json(payload)

    def verify(
        self,
        signing_key: bytes,
        *,
        expected_version: str,
        now: datetime | None = None,
    ) -> None:
        if type(signing_key) is not bytes or len(signing_key) != 32:
            _fail("host_lifecycle_key_invalid")
        if self.memplex_version != _exact_text(expected_version):
            _fail("host_lifecycle_version_mismatch")
        checked_at = datetime.now(timezone.utc) if now is None else now
        if type(checked_at) is not datetime or checked_at.tzinfo is None:
            _fail("host_lifecycle_freshness_invalid")
        checked_at = checked_at.astimezone(timezone.utc)
        generated_at = _timestamp(self.generated_at)
        if (
            generated_at > checked_at + _MAX_FUTURE_SKEW
            or checked_at - generated_at > _MAX_EVIDENCE_AGE
        ):
            _fail("host_lifecycle_freshness_invalid")
        expected_digests = current_host_contract_digests()
        if any(
            proof.contract_sha256 != expected_digests[proof.host] for proof in self.hosts
        ):
            _fail("host_lifecycle_contract_mismatch")
        expected_signature = hmac.new(
            signing_key,
            self.canonical_unsigned_bytes(),
            sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected_signature, self.signature):
            _fail("host_lifecycle_signature_invalid")

    @classmethod
    def from_dict(cls, payload: object) -> HostLifecycleEvidence:
        if type(payload) is not dict or frozenset(payload) != {
            "schema_version",
            "memplex_version",
            "generated_at",
            "hermes_source_revision",
            "hermes_provider_sha256",
            "hosts",
            "key_id",
            "status",
            "signature",
        }:
            _fail()
        hosts = payload["hosts"]
        if type(hosts) is not list:
            _fail()
        return cls(
            schema_version=payload["schema_version"],  # type: ignore[arg-type]
            memplex_version=payload["memplex_version"],  # type: ignore[arg-type]
            generated_at=payload["generated_at"],  # type: ignore[arg-type]
            hermes_source_revision=payload["hermes_source_revision"],  # type: ignore[arg-type]
            hermes_provider_sha256=payload["hermes_provider_sha256"],  # type: ignore[arg-type]
            hosts=tuple(HostLifecycleProof.from_dict(item) for item in hosts),
            key_id=payload["key_id"],  # type: ignore[arg-type]
            status=payload["status"],  # type: ignore[arg-type]
            signature=payload["signature"],  # type: ignore[arg-type]
        )

    @classmethod
    def create(
        cls,
        *,
        memplex_version: str,
        cli_versions: Mapping[str, str],
        key_id: str,
        signing_key: bytes,
        generated_at: datetime | None = None,
    ) -> HostLifecycleEvidence:
        if type(signing_key) is not bytes or len(signing_key) != 32:
            _fail("host_lifecycle_key_invalid")
        if type(cli_versions) is not dict or tuple(sorted(cli_versions)) != _HOSTS:
            _fail("host_lifecycle_matrix_incomplete")
        digests = current_host_contract_digests()
        hosts = tuple(
            HostLifecycleProof(
                host=host,
                cli_version=cli_versions[host],
                contract_sha256=digests[host],
                checks=_CHECKS,
            )
            for host in _HOSTS
        )
        generated = datetime.now(timezone.utc) if generated_at is None else generated_at
        if type(generated) is not datetime or generated.tzinfo is None:
            _fail("host_lifecycle_freshness_invalid")
        generated_text = generated.astimezone(timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )
        unsigned = cls(
            schema_version=_SCHEMA_VERSION,
            memplex_version=memplex_version,
            generated_at=generated_text,
            hermes_source_revision=_HERMES_REVISION,
            hermes_provider_sha256=_HERMES_PROVIDER_SHA256,
            hosts=hosts,
            key_id=key_id,
            status="passed",
            signature="0" * 64,
        )
        signature = hmac.new(signing_key, unsigned.canonical_unsigned_bytes(), sha256).hexdigest()
        return cls(**{**unsigned.to_dict(), "hosts": hosts, "signature": signature})  # type: ignore[arg-type]


def read_host_lifecycle_evidence(path: Path) -> HostLifecycleEvidence:
    """Read one regular, non-symlink evidence file with a strict size cap."""

    try:
        directory_fd = _open_parent_directory(path)
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path.name, flags, dir_fd=directory_fd)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_EVIDENCE_BYTES:
                    _fail("host_lifecycle_evidence_invalid")
                payload = os.read(fd, _MAX_EVIDENCE_BYTES + 1)
            finally:
                os.close(fd)
        finally:
            os.close(directory_fd)
        if len(payload) > _MAX_EVIDENCE_BYTES:
            _fail("host_lifecycle_evidence_invalid")
        parsed: Any = json.loads(payload)
        return HostLifecycleEvidence.from_dict(parsed)
    except HostLifecycleIntegrityError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise HostLifecycleIntegrityError("host_lifecycle_evidence_invalid") from exc


def write_host_lifecycle_evidence(path: Path, evidence: HostLifecycleEvidence) -> None:
    directory_fd = -1
    temporary_name = f".{path.name}.{uuid.uuid4().hex}.tmp"
    temporary_created = False
    try:
        directory_fd = _open_parent_directory(path)
        try:
            existing = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            _fail("host_lifecycle_evidence_invalid")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(temporary_name, flags, 0o600, dir_fd=directory_fd)
        temporary_created = True
        try:
            view = memoryview(evidence.canonical_bytes() + b"\n")
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(
            temporary_name,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temporary_created = False
        os.fsync(directory_fd)
    except HostLifecycleIntegrityError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise HostLifecycleIntegrityError("host_lifecycle_evidence_invalid") from exc
    finally:
        if directory_fd >= 0:
            if temporary_created:
                try:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                except OSError:
                    pass
            os.close(directory_fd)
