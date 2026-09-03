"""G003/G004 收口门的 fail-closed 签名部署证据。

调用方显式提供期望部署值。本模块刻意不依赖 Git、环境推断或包元数据：
readiness reporter 决定当前部署工件，再以这些精确值验证签名报告。
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
from base64 import b64decode, b64encode
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from secrets import token_hex
from typing import Any, NoReturn

_SCHEMA_VERSION = 1
_MAX_EVIDENCE_BYTES = 128 * 1024
_GATE_IDS = frozenset({"schema_migrations_atomicity", "durable_sync_backpressure"})
_EVIDENCE_KEYS = frozenset(
    {
        "schema_version",
        "gate_id",
        "memplex_version",
        "source_sha256",
        "artifact_sha256",
        "deployment_id",
        "target_identity_sha256",
        "generated_at",
        "status",
        "run_result_sha256",
        "key_id",
        "signature",
    }
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_KEY_ID_RE = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


class ReadinessEvidenceError(RuntimeError):
    """不包含路径或密钥的固定证据错误。"""

    def __init__(self) -> None:
        super().__init__("industrial_gate_evidence_invalid")


def _fail() -> NoReturn:
    raise ReadinessEvidenceError()


def _exact_text(value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        _fail()
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        _fail()
    return value


def _sha256(value: object) -> str:
    text = _exact_text(value)
    if _SHA256_RE.fullmatch(text) is None:
        _fail()
    return text


def _key_id(value: object) -> str:
    text = _exact_text(value)
    if _KEY_ID_RE.fullmatch(text) is None:
        _fail()
    return text


def _timestamp(value: object) -> datetime:
    text = _exact_text(value)
    try:
        parsed = datetime.strptime(text, _TIMESTAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        _fail()
    if parsed.strftime(_TIMESTAMP_FORMAT) != text:
        _fail()
    return parsed


def _timestamp_from_datetime(value: object) -> str:
    if type(value) is not datetime or value.tzinfo is None:
        _fail()
    return value.astimezone(UTC).strftime(_TIMESTAMP_FORMAT)


def _signing_key(value: object) -> bytes:
    if type(value) is not bytes or len(value) != 32:
        _fail()
    return value


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        _fail()


def _no_duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            _fail()
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class DeploymentEvidenceBinding:
    """产品从当前部署上下文显式提供的绑定值。"""

    memplex_version: str
    source_sha256: str
    artifact_sha256: str
    deployment_id: str
    target_identity_sha256: str

    def __post_init__(self) -> None:
        _exact_text(self.memplex_version)
        _sha256(self.source_sha256)
        _sha256(self.artifact_sha256)
        _exact_text(self.deployment_id)
        _sha256(self.target_identity_sha256)

    @classmethod
    def from_values(
        cls,
        *,
        memplex_version: str,
        source_sha256: str,
        artifact_sha256: str,
        deployment_id: str,
        target_identity_sha256: str,
    ) -> DeploymentEvidenceBinding:
        return cls(
            memplex_version=memplex_version,
            source_sha256=source_sha256,
            artifact_sha256=artifact_sha256,
            deployment_id=deployment_id,
            target_identity_sha256=target_identity_sha256,
        )


def load_deployment_evidence_binding_from_environment(
    *, memplex_version: str
) -> DeploymentEvidenceBinding:
    """加载共享部署身份，不回显任何环境变量值。

    调用方显式传入已安装版本，解析器绝不从 Git 或包元数据推断源码状态。
    """

    try:
        return DeploymentEvidenceBinding.from_values(
            memplex_version=memplex_version,
            source_sha256=_exact_text(os.environ.get("MEMPLEX_SOURCE_SHA256")),
            artifact_sha256=_exact_text(os.environ.get("MEMPLEX_ARTIFACT_SHA256")),
            deployment_id=_exact_text(os.environ.get("MEMPLEX_DEPLOYMENT_ID")),
            target_identity_sha256=_exact_text(os.environ.get("MEMPLEX_TARGET_IDENTITY_SHA256")),
        )
    except (ReadinessEvidenceError, TypeError, ValueError):
        _fail()


def load_signing_key_from_environment(env_name: str) -> bytes:
    """加载规范标准 Base64 编码的恰好 32 字节密钥。"""

    try:
        encoded = _exact_text(env_name)
        value = _exact_text(os.environ.get(encoded))
        decoded = b64decode(value.encode("ascii"), validate=True)
        if len(decoded) != 32 or b64encode(decoded).decode("ascii") != value:
            _fail()
        return decoded
    except ReadinessEvidenceError:
        raise
    except (TypeError, ValueError, UnicodeError):
        _fail()


def load_expected_key_id_from_environment(env_name: str) -> str:
    """加载一个非空精确 key id，并在 verify 时比较。"""

    try:
        return _key_id(os.environ.get(_exact_text(env_name)))
    except ReadinessEvidenceError:
        raise
    except (TypeError, ValueError):
        _fail()


@dataclass(frozen=True, slots=True)
class IndustrialGateEvidence:
    """一份经 HMAC 认证、精确 schema 的 G003/G004 通过门结果。"""

    schema_version: int
    gate_id: str
    memplex_version: str
    source_sha256: str
    artifact_sha256: str
    deployment_id: str
    target_identity_sha256: str
    generated_at: str
    status: str
    run_result_sha256: str
    key_id: str
    signature: str

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != _SCHEMA_VERSION:
            _fail()
        if _exact_text(self.gate_id) not in _GATE_IDS:
            _fail()
        _exact_text(self.memplex_version)
        _sha256(self.source_sha256)
        _sha256(self.artifact_sha256)
        _exact_text(self.deployment_id)
        _sha256(self.target_identity_sha256)
        _timestamp(self.generated_at)
        if self.status != "passed":
            _fail()
        _sha256(self.run_result_sha256)
        _key_id(self.key_id)
        _sha256(self.signature)

    @classmethod
    def create(
        cls,
        *,
        gate_id: str,
        binding: DeploymentEvidenceBinding,
        run_result_sha256: str,
        key_id: str,
        signing_key: bytes,
        generated_at: datetime,
    ) -> IndustrialGateEvidence:
        if type(binding) is not DeploymentEvidenceBinding:
            _fail()
        unsigned = cls(
            schema_version=_SCHEMA_VERSION,
            gate_id=gate_id,
            memplex_version=binding.memplex_version,
            source_sha256=binding.source_sha256,
            artifact_sha256=binding.artifact_sha256,
            deployment_id=binding.deployment_id,
            target_identity_sha256=binding.target_identity_sha256,
            generated_at=_timestamp_from_datetime(generated_at),
            status="passed",
            run_result_sha256=run_result_sha256,
            key_id=key_id,
            signature="0" * 64,
        )
        signature = hmac.new(
            _signing_key(signing_key), unsigned.canonical_unsigned_bytes(), hashlib.sha256
        ).hexdigest()
        return cls(**{**unsigned.to_dict(), "signature": signature})

    @classmethod
    def from_dict(cls, payload: object) -> IndustrialGateEvidence:
        try:
            if type(payload) is not dict or frozenset(payload) != _EVIDENCE_KEYS:
                _fail()
            return cls(
                schema_version=payload["schema_version"],
                gate_id=payload["gate_id"],
                memplex_version=payload["memplex_version"],
                source_sha256=payload["source_sha256"],
                artifact_sha256=payload["artifact_sha256"],
                deployment_id=payload["deployment_id"],
                target_identity_sha256=payload["target_identity_sha256"],
                generated_at=payload["generated_at"],
                status=payload["status"],
                run_result_sha256=payload["run_result_sha256"],
                key_id=payload["key_id"],
                signature=payload["signature"],
            )
        except ReadinessEvidenceError:
            raise
        except (KeyError, TypeError, ValueError):
            _fail()

    @classmethod
    def from_json(cls, payload: object) -> IndustrialGateEvidence:
        try:
            if type(payload) is not bytes or len(payload) > _MAX_EVIDENCE_BYTES:
                _fail()
            if payload.startswith(b"\xef\xbb\xbf"):
                _fail()
            raw = json.loads(payload.decode("utf-8"), object_pairs_hook=_no_duplicate_object)
            return cls.from_dict(raw)
        except ReadinessEvidenceError:
            raise
        except (UnicodeError, ValueError, TypeError):
            _fail()

    def binding(self) -> DeploymentEvidenceBinding:
        return DeploymentEvidenceBinding.from_values(
            memplex_version=self.memplex_version,
            source_sha256=self.source_sha256,
            artifact_sha256=self.artifact_sha256,
            deployment_id=self.deployment_id,
            target_identity_sha256=self.target_identity_sha256,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "gate_id": self.gate_id,
            "memplex_version": self.memplex_version,
            "source_sha256": self.source_sha256,
            "artifact_sha256": self.artifact_sha256,
            "deployment_id": self.deployment_id,
            "target_identity_sha256": self.target_identity_sha256,
            "generated_at": self.generated_at,
            "status": self.status,
            "run_result_sha256": self.run_result_sha256,
            "key_id": self.key_id,
            "signature": self.signature,
        }

    def canonical_unsigned_bytes(self) -> bytes:
        payload = self.to_dict()
        payload.pop("signature")
        return _canonical_json(payload)

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.to_dict())

    def verify(
        self,
        *,
        expected_gate_id: str,
        expected_binding: DeploymentEvidenceBinding,
        expected_key_id: str,
        signing_key: bytes,
        now: datetime,
        max_age: timedelta,
    ) -> None:
        try:
            if type(expected_binding) is not DeploymentEvidenceBinding:
                _fail()
            expected_gate_id = _exact_text(expected_gate_id)
            if expected_gate_id not in _GATE_IDS:
                _fail()
            expected_key_id = _key_id(expected_key_id)
            if (
                self.gate_id != expected_gate_id
                or self.binding() != expected_binding
                or self.key_id != expected_key_id
            ):
                _fail()
            if type(now) is not datetime or now.tzinfo is None:
                _fail()
            if type(max_age) is not timedelta or max_age <= timedelta(0):
                _fail()
            checked_at = now.astimezone(UTC)
            generated_at = _timestamp(self.generated_at)
            if generated_at > checked_at or checked_at - generated_at > max_age:
                _fail()
            expected = hmac.new(
                _signing_key(signing_key), self.canonical_unsigned_bytes(), hashlib.sha256
            ).hexdigest()
            if not hmac.compare_digest(expected, self.signature):
                _fail()
        except ReadinessEvidenceError:
            raise
        except (TypeError, ValueError, OverflowError):
            _fail()


def _open_parent_directory(path: Path) -> int:
    if not isinstance(path, Path) or not path.name or path.name in {".", ".."}:
        _fail()
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    parent = path.parent
    directory_fd = os.open(os.sep if parent.is_absolute() else ".", flags)
    try:
        components = parent.parts[1:] if parent.is_absolute() else parent.parts
        for component in components:
            if component in {"", "."}:
                continue
            if component == "..":
                _fail()
            next_fd = os.open(component, flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        return directory_fd
    except BaseException:
        os.close(directory_fd)
        raise


def write_industrial_gate_evidence(path: Path, evidence: IndustrialGateEvidence) -> None:
    """原子发布一个常规证据文件，且不穿越符号链接。"""

    directory_fd = -1
    temporary_name = f".{getattr(path, 'name', 'evidence')}.{token_hex(16)}.tmp"
    temporary_created = False
    try:
        if type(evidence) is not IndustrialGateEvidence:
            _fail()
        directory_fd = _open_parent_directory(path)
        try:
            existing = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            _fail()
        fd = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_fd,
        )
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
        os.rename(temporary_name, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        temporary_created = False
        os.fsync(directory_fd)
    except ReadinessEvidenceError:
        raise
    except (OSError, TypeError, ValueError, UnicodeError):
        _fail()
    finally:
        if directory_fd >= 0:
            if temporary_created:
                try:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                except OSError:
                    pass
            os.close(directory_fd)


def read_industrial_gate_evidence(path: Path) -> IndustrialGateEvidence:
    """读取一个有大小上限的常规证据文件，且不跟随符号链接。"""

    try:
        directory_fd = _open_parent_directory(path)
        try:
            fd = os.open(path.name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_fd)
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_EVIDENCE_BYTES:
                    _fail()
                payload = os.read(fd, _MAX_EVIDENCE_BYTES + 1)
            finally:
                os.close(fd)
        finally:
            os.close(directory_fd)
        if len(payload) > _MAX_EVIDENCE_BYTES:
            _fail()
        return IndustrialGateEvidence.from_json(payload)
    except ReadinessEvidenceError:
        raise
    except (OSError, TypeError, ValueError, UnicodeError):
        _fail()
