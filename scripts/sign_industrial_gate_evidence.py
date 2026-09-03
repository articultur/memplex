#!/usr/bin/env python3
"""为 G003/G004 已通过的独立 verifier result 生成签名部署证据。"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import stat
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memplex.readiness_evidence import (
    DeploymentEvidenceBinding,
    IndustrialGateEvidence,
    ReadinessEvidenceError,
    load_deployment_evidence_binding_from_environment,
    load_expected_key_id_from_environment,
    load_signing_key_from_environment,
    write_industrial_gate_evidence,
)

_GATE_IDS = frozenset({"schema_migrations_atomicity", "durable_sync_backpressure"})
_MAX_RUN_RESULT_BYTES = 16 * 1024 * 1024
_KEY_ENV = "MEMPLEX_INDUSTRIAL_EVIDENCE_HMAC_KEY"
_KEY_ID_ENV = "MEMPLEX_INDUSTRIAL_EVIDENCE_KEY_ID"
_MAX_RESULT_AGE = timedelta(minutes=15)
_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_RESULT_KEYS = frozenset(
    {
        "schema_version",
        "gate_id",
        "verifier_id",
        "verifier_contract_sha256",
        "status",
        "memplex_version",
        "source_sha256",
        "artifact_sha256",
        "deployment_id",
        "target_identity_sha256",
        "started_at",
        "completed_at",
        "checks",
    }
)
_CHECK_KEYS = frozenset({"id", "status", "evidence_sha256"})
_VERIFIER_CONTRACTS = {
    "schema_migrations_atomicity": (
        "memplex-g003-storage-integrity-v1",
        (
            "least_privilege_application_acl",
            "migration_plan_status_apply",
            "packaged_migration_discovery",
            "postgresql_storage_regression",
            "storage_crash_atomicity",
        ),
    ),
    "durable_sync_backpressure": (
        "memplex-g004-reliable-sync-v1",
        (
            "bounded_pagination_backpressure",
            "durable_outbox_inbox",
            "idempotent_replay",
            "postgresql_sync_regression",
            "typed_tombstone_propagation",
        ),
    ),
}
_ERROR = {"schema_version": 1, "status": "failed", "error": "industrial_gate_evidence_invalid"}


class _InputError(RuntimeError):
    """不包含输入内容、路径或密钥的固定失败。"""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise _InputError()


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(
        description=(
            "仅对满足当前部署绑定、固定verifier合同且所有必需检查均passed的"
            "独立 verifier result 签名；key holder承担attestation。"
        )
    )
    parser.add_argument("--gate-id", required=True, choices=sorted(_GATE_IDS))
    parser.add_argument("--run-result", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--key-id", required=True)
    return parser


def _fail() -> None:
    raise _InputError()


def _exact_text(value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        _fail()
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        _fail()
    return value


def _sha256(value: object) -> str:
    text = _exact_text(value)
    if _SHA256_RE.fullmatch(text) is None:
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


def _no_duplicate_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if type(key) is not str or key in result:
            _fail()
        result[key] = value
    return result


def _verifier_contract_sha256(gate_id: str) -> str:
    verifier_id, required_checks = _VERIFIER_CONTRACTS[gate_id]
    contract = {
        "gate_id": gate_id,
        "required_checks": list(required_checks),
        "schema_version": 1,
        "verifier_id": verifier_id,
    }
    return hashlib.sha256(
        json.dumps(contract, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _validate_verifier_result(
    payload: bytes,
    *,
    gate_id: str,
    binding: DeploymentEvidenceBinding,
    installed_version: str,
    now: datetime,
) -> None:
    try:
        raw = json.loads(payload.decode("utf-8"), object_pairs_hook=_no_duplicate_object)
    except _InputError:
        raise
    except (json.JSONDecodeError, TypeError, UnicodeError):
        _fail()
    if type(raw) is not dict or frozenset(raw) != _RESULT_KEYS:
        _fail()
    verifier_id, required_checks = _VERIFIER_CONTRACTS[gate_id]
    if (
        type(raw["schema_version"]) is not int
        or raw["schema_version"] != 1
        or raw["gate_id"] != gate_id
        or raw["verifier_id"] != verifier_id
        or raw["verifier_contract_sha256"] != _verifier_contract_sha256(gate_id)
        or raw["status"] != "passed"
        or raw["memplex_version"] != installed_version
        or raw["source_sha256"] != binding.source_sha256
        or raw["artifact_sha256"] != binding.artifact_sha256
        or raw["deployment_id"] != binding.deployment_id
        or raw["target_identity_sha256"] != binding.target_identity_sha256
    ):
        _fail()
    _exact_text(raw["memplex_version"])
    _sha256(raw["source_sha256"])
    _sha256(raw["artifact_sha256"])
    _exact_text(raw["deployment_id"])
    _sha256(raw["target_identity_sha256"])
    started = _timestamp(raw["started_at"])
    completed = _timestamp(raw["completed_at"])
    if completed < started or completed > now or now - completed > _MAX_RESULT_AGE:
        _fail()
    checks = raw["checks"]
    if type(checks) is not list or len(checks) != len(required_checks):
        _fail()
    observed_ids: list[str] = []
    for check in checks:
        if type(check) is not dict or frozenset(check) != _CHECK_KEYS:
            _fail()
        check_id = _exact_text(check["id"])
        if check["status"] != "passed":
            _fail()
        _sha256(check["evidence_sha256"])
        observed_ids.append(check_id)
    if tuple(observed_ids) != required_checks:
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


def _read_regular_file(path: Path) -> tuple[bytes, tuple[int, int]]:
    directory_fd = -1
    fd = -1
    try:
        directory_fd = _open_parent_directory(path)
        fd = os.open(
            path.name,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_NONBLOCK", 0),
            dir_fd=directory_fd,
        )
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > _MAX_RUN_RESULT_BYTES:
            _fail()
        remaining = info.st_size
        chunks: list[bytes] = []
        while remaining:
            chunk = os.read(fd, min(remaining, 1024 * 1024))
            if not chunk:
                _fail()
            chunks.append(chunk)
            remaining -= len(chunk)
        if os.read(fd, 1):
            _fail()
        after = os.fstat(fd)
        if (
            after.st_dev != info.st_dev
            or after.st_ino != info.st_ino
            or after.st_size != info.st_size
        ):
            _fail()
        payload = b"".join(chunks)
        return payload, (info.st_dev, info.st_ino)
    except _InputError:
        raise
    except (OSError, TypeError, ValueError):
        _fail()
    finally:
        if fd >= 0:
            os.close(fd)
        if directory_fd >= 0:
            os.close(directory_fd)


def _reject_output_alias(path: Path, input_identity: tuple[int, int]) -> None:
    directory_fd = -1
    try:
        directory_fd = _open_parent_directory(path)
        try:
            info = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode) or (info.st_dev, info.st_ino) == input_identity:
            _fail()
    except _InputError:
        raise
    except (OSError, TypeError, ValueError):
        _fail()
    finally:
        if directory_fd >= 0:
            os.close(directory_fd)


def _signed_evidence(args: argparse.Namespace) -> IndustrialGateEvidence:
    if args.gate_id not in _GATE_IDS:
        _fail()
    try:
        run_result = Path(args.run_result)
        output = Path(args.output)
        installed_version = importlib.metadata.version("memplex")
    except (TypeError, ValueError, importlib.metadata.PackageNotFoundError):
        _fail()
    payload, input_identity = _read_regular_file(run_result)
    _reject_output_alias(output, input_identity)
    try:
        binding = load_deployment_evidence_binding_from_environment(
            memplex_version=installed_version
        )
        signing_key = load_signing_key_from_environment(_KEY_ENV)
        expected_key_id = load_expected_key_id_from_environment(_KEY_ID_ENV)
        if args.key_id != expected_key_id:
            _fail()
        _validate_verifier_result(
            payload,
            gate_id=args.gate_id,
            binding=binding,
            installed_version=installed_version,
            now=datetime.now(UTC),
        )
        return IndustrialGateEvidence.create(
            gate_id=args.gate_id,
            binding=binding,
            run_result_sha256=hashlib.sha256(payload).hexdigest(),
            key_id=args.key_id,
            signing_key=signing_key,
            generated_at=datetime.now(UTC),
        )
    except (ReadinessEvidenceError, TypeError, ValueError, OverflowError):
        _fail()


def main() -> int:
    try:
        args = _parser().parse_args()
        evidence = _signed_evidence(args)
        write_industrial_gate_evidence(Path(args.output), evidence)
    except SystemExit:
        raise
    except (OSError, _InputError, ReadinessEvidenceError, TypeError, ValueError, UnicodeError):
        print('{"schema_version":1,"status":"failed","error":"industrial_gate_evidence_invalid"}')
        return 2
    print(
        '{"schema_version":1,"status":"signed","gate":"'
        + evidence.gate_id
        + '"}'
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
