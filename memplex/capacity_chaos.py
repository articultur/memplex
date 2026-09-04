"""G009 生产规模容量、soak 与 chaos 的签名机器证据。"""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import stat
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from types import MappingProxyType

_SCHEMA_VERSION = 1
_MAX_EVIDENCE_BYTES = 128 * 1024
_MAX_EVIDENCE_AGE = timedelta(hours=24)
_MAX_FUTURE_SKEW = timedelta(minutes=5)
_MIN_FUNCTIONS = 100_000
_MIN_EDGES = 1_000_000
_MIN_SOAK_SECONDS = 60.0
_MIN_OPERATIONS = 1_000
_MAX_ERROR_RATE = 0.001
_MAX_READ_P99_MS = 250.0
_MAX_WRITE_P99_MS = 500.0
_MAX_SYNC_P99_MS = 500.0
_MAX_RSS_BYTES = 2 * 1024**3
_MAX_OUTBOX_AGE_SECONDS = 30.0
_MAX_RTO_SECONDS = 30.0
_REAL_CHAOS_SCENARIOS = (
    "database",
    "network",
    "disk",
    "term",
    "kill",
    "duplicate_delivery",
)
_CHAOS_KEYS = frozenset((*_REAL_CHAOS_SCENARIOS, "redis"))
_REDIS_REASON = "redis_not_in_supported_topology"
_METRIC_KEYS = frozenset({"samples", "errors", "p50_ms", "p95_ms", "p99_ms"})
_REPORT_KEYS = frozenset(
    {
        "schema_version",
        "report_id",
        "generated_at",
        "window_started_at",
        "window_ended_at",
        "memplex_version",
        "python_version",
        "postgres_version",
        "platform",
        "machine_arch",
        "cpu_count",
        "memory_bytes",
        "function_count",
        "edge_count",
        "soak_seconds",
        "operations_count",
        "throughput_ops_per_second",
        "read",
        "write",
        "sync",
        "error_rate",
        "rss_peak_bytes",
        "queue_depth_end",
        "outbox_max_age_seconds",
        "rpo_lost_events",
        "rto_seconds",
        "data_digest_before",
        "data_digest_after",
        "chaos",
        "redis_reason",
        "contract_sha256",
        "industrial_gate_closing",
        "key_id",
        "signature",
    }
)


class CapacityChaosEvidenceError(RuntimeError):
    """固定、脱敏的 G009 evidence 错误。"""

    def __init__(self, code: str = "capacity_chaos_evidence_invalid") -> None:
        self.code = code
        super().__init__(code)


def _fail(code: str = "capacity_chaos_evidence_invalid") -> None:
    raise CapacityChaosEvidenceError(code)


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise CapacityChaosEvidenceError() from exc


def _exact_text(value: object) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        _fail()
    if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        _fail()
    return value


def _timestamp(value: object) -> datetime:
    text = _exact_text(value)
    try:
        parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
            tzinfo=UTC
        )
    except ValueError as exc:
        raise CapacityChaosEvidenceError() from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ") != text:
        _fail()
    return parsed


def _exact_int(value: object, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _fail()
    return value


def _finite_float(value: object, *, minimum: float = 0.0) -> float:
    if type(value) is not float or not math.isfinite(value) or value < minimum:
        _fail()
    return value


def _sha256(value: object) -> str:
    text = _exact_text(value)
    if len(text) != 64:
        _fail()
    try:
        bytes.fromhex(text)
    except ValueError as exc:
        raise CapacityChaosEvidenceError() from exc
    return text


def _signing_key(value: object) -> bytes:
    if type(value) is not bytes or len(value) < 32:
        _fail()
    return value


def capacity_chaos_contract_sha256() -> str:
    """Return the stable digest of the frozen G009 industrial thresholds."""

    contract = {
        "min_functions": _MIN_FUNCTIONS,
        "min_edges": _MIN_EDGES,
        "min_soak_seconds": _MIN_SOAK_SECONDS,
        "min_operations": _MIN_OPERATIONS,
        "max_error_rate": _MAX_ERROR_RATE,
        "max_read_p99_ms": _MAX_READ_P99_MS,
        "max_write_p99_ms": _MAX_WRITE_P99_MS,
        "max_sync_p99_ms": _MAX_SYNC_P99_MS,
        "max_rss_bytes": _MAX_RSS_BYTES,
        "max_outbox_age_seconds": _MAX_OUTBOX_AGE_SECONDS,
        "max_rto_seconds": _MAX_RTO_SECONDS,
        "chaos": list(_REAL_CHAOS_SCENARIOS),
        "redis": _REDIS_REASON,
    }
    return hashlib.sha256(_canonical_json(contract)).hexdigest()


@dataclass(frozen=True, slots=True)
class WorkloadMetrics:
    samples: int
    errors: int
    p50_ms: float
    p95_ms: float
    p99_ms: float

    def __post_init__(self) -> None:
        _exact_int(self.samples)
        _exact_int(self.errors)
        if self.errors > self.samples:
            _fail()
        p50 = _finite_float(self.p50_ms)
        p95 = _finite_float(self.p95_ms)
        p99 = _finite_float(self.p99_ms)
        if p50 > p95 or p95 > p99:
            _fail()

    @classmethod
    def from_dict(cls, raw: object) -> WorkloadMetrics:
        if type(raw) is not dict or set(raw) != _METRIC_KEYS:
            _fail()
        return cls(
            _exact_int(raw["samples"]),
            _exact_int(raw["errors"]),
            _finite_float(raw["p50_ms"]),
            _finite_float(raw["p95_ms"]),
            _finite_float(raw["p99_ms"]),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "samples": self.samples,
            "errors": self.errors,
            "p50_ms": self.p50_ms,
            "p95_ms": self.p95_ms,
            "p99_ms": self.p99_ms,
        }


def _industrial_gate(values: Mapping[str, object]) -> bool:
    read = values["read"]
    write = values["write"]
    sync = values["sync"]
    chaos = values["chaos"]
    assert isinstance(read, WorkloadMetrics)
    assert isinstance(write, WorkloadMetrics)
    assert isinstance(sync, WorkloadMetrics)
    assert isinstance(chaos, Mapping)
    return (
        values["function_count"] >= _MIN_FUNCTIONS
        and values["edge_count"] >= _MIN_EDGES
        and values["soak_seconds"] >= _MIN_SOAK_SECONDS
        and values["operations_count"] >= _MIN_OPERATIONS
        and read.samples > 0
        and write.samples > 0
        and sync.samples > 0
        and read.p99_ms <= _MAX_READ_P99_MS
        and write.p99_ms <= _MAX_WRITE_P99_MS
        and sync.p99_ms <= _MAX_SYNC_P99_MS
        and values["error_rate"] <= _MAX_ERROR_RATE
        and values["rss_peak_bytes"] <= _MAX_RSS_BYTES
        and values["queue_depth_end"] == 0
        and values["outbox_max_age_seconds"] <= _MAX_OUTBOX_AGE_SECONDS
        and values["rpo_lost_events"] == 0
        and values["rto_seconds"] <= _MAX_RTO_SECONDS
        and values["data_digest_before"] == values["data_digest_after"]
        and all(chaos[name] == "passed" for name in _REAL_CHAOS_SCENARIOS)
        and chaos["redis"] == "not_applicable"
        and values["redis_reason"] == _REDIS_REASON
    )


@dataclass(frozen=True, slots=True)
class CapacityChaosEvidence:
    schema_version: int
    report_id: str
    generated_at: str
    window_started_at: str
    window_ended_at: str
    memplex_version: str
    python_version: str
    postgres_version: str
    platform: str
    machine_arch: str
    cpu_count: int
    memory_bytes: int
    function_count: int
    edge_count: int
    soak_seconds: float
    operations_count: int
    throughput_ops_per_second: float
    read: WorkloadMetrics
    write: WorkloadMetrics
    sync: WorkloadMetrics
    error_rate: float
    rss_peak_bytes: int
    queue_depth_end: int
    outbox_max_age_seconds: float
    rpo_lost_events: int
    rto_seconds: float
    data_digest_before: str
    data_digest_after: str
    chaos: Mapping[str, str]
    redis_reason: str
    contract_sha256: str
    industrial_gate_closing: bool
    key_id: str
    signature: str

    @classmethod
    def create(cls, **raw: object) -> CapacityChaosEvidence:
        signing_key = _signing_key(raw.pop("signing_key", None))
        if "schema_version" in raw or "signature" in raw or "contract_sha256" in raw:
            _fail()
        for name in ("read", "write", "sync"):
            metric = raw.get(name)
            if isinstance(metric, WorkloadMetrics):
                raw[name] = metric.to_dict()
        chaos = raw.get("chaos")
        if isinstance(chaos, Mapping):
            raw["chaos"] = dict(chaos)
        values = {**raw, "schema_version": _SCHEMA_VERSION}
        provisional = cls.from_dict(
            {
                **values,
                "contract_sha256": capacity_chaos_contract_sha256(),
                "industrial_gate_closing": False,
                "signature": "0" * 64,
            },
            allow_gate_mismatch=True,
        )
        gate = _industrial_gate(provisional._gate_values())
        unsigned = replace(provisional, industrial_gate_closing=gate)
        signature = hmac.new(
            signing_key, unsigned.canonical_unsigned_bytes(), hashlib.sha256
        ).hexdigest()
        return replace(unsigned, signature=signature)

    @classmethod
    def from_dict(
        cls, raw: object, *, allow_gate_mismatch: bool = False
    ) -> CapacityChaosEvidence:
        try:
            if type(raw) is not dict or set(raw) != _REPORT_KEYS:
                _fail()
            if type(raw["schema_version"]) is not int or raw["schema_version"] != _SCHEMA_VERSION:
                _fail()
            report_id = _exact_text(raw["report_id"])
            if str(uuid.UUID(report_id)) != report_id:
                _fail()
            generated = _timestamp(raw["generated_at"])
            started = _timestamp(raw["window_started_at"])
            ended = _timestamp(raw["window_ended_at"])
            if ended <= started or generated < ended:
                _fail()
            chaos_raw = raw["chaos"]
            if type(chaos_raw) is not dict or set(chaos_raw) != _CHAOS_KEYS:
                _fail()
            chaos: dict[str, str] = {}
            for key in sorted(_CHAOS_KEYS):
                status = _exact_text(chaos_raw[key])
                if status not in {"passed", "failed", "not_applicable"}:
                    _fail()
                chaos[key] = status
            report = cls(
                _SCHEMA_VERSION,
                report_id,
                generated.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                started.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                ended.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                _exact_text(raw["memplex_version"]),
                _exact_text(raw["python_version"]),
                _exact_text(raw["postgres_version"]),
                _exact_text(raw["platform"]),
                _exact_text(raw["machine_arch"]),
                _exact_int(raw["cpu_count"], minimum=1),
                _exact_int(raw["memory_bytes"], minimum=1),
                _exact_int(raw["function_count"]),
                _exact_int(raw["edge_count"]),
                _finite_float(raw["soak_seconds"]),
                _exact_int(raw["operations_count"]),
                _finite_float(raw["throughput_ops_per_second"]),
                WorkloadMetrics.from_dict(raw["read"]),
                WorkloadMetrics.from_dict(raw["write"]),
                WorkloadMetrics.from_dict(raw["sync"]),
                _finite_float(raw["error_rate"]),
                _exact_int(raw["rss_peak_bytes"]),
                _exact_int(raw["queue_depth_end"]),
                _finite_float(raw["outbox_max_age_seconds"]),
                _exact_int(raw["rpo_lost_events"]),
                _finite_float(raw["rto_seconds"]),
                _sha256(raw["data_digest_before"]),
                _sha256(raw["data_digest_after"]),
                MappingProxyType(chaos),
                _exact_text(raw["redis_reason"]),
                _sha256(raw["contract_sha256"]),
                raw["industrial_gate_closing"],
                _exact_text(raw["key_id"]),
                _sha256(raw["signature"]),
            )
            if type(report.industrial_gate_closing) is not bool:
                _fail()
            total_samples = report.read.samples + report.write.samples + report.sync.samples
            total_errors = report.read.errors + report.write.errors + report.sync.errors
            if report.operations_count != total_samples:
                _fail()
            expected_error = total_errors / total_samples if total_samples else 0.0
            if not math.isclose(report.error_rate, expected_error, abs_tol=1e-12, rel_tol=0.0):
                _fail()
            expected_throughput = report.operations_count / report.soak_seconds if report.soak_seconds else 0.0
            if not math.isclose(
                report.throughput_ops_per_second,
                expected_throughput,
                abs_tol=0.01,
                rel_tol=0.0,
            ):
                _fail()
            expected_gate = _industrial_gate(report._gate_values())
            if not allow_gate_mismatch and report.industrial_gate_closing is not expected_gate:
                _fail()
            return report
        except CapacityChaosEvidenceError:
            raise
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
            raise CapacityChaosEvidenceError() from exc

    def _gate_values(self) -> dict[str, object]:
        return {
            "function_count": self.function_count,
            "edge_count": self.edge_count,
            "soak_seconds": self.soak_seconds,
            "operations_count": self.operations_count,
            "read": self.read,
            "write": self.write,
            "sync": self.sync,
            "error_rate": self.error_rate,
            "rss_peak_bytes": self.rss_peak_bytes,
            "queue_depth_end": self.queue_depth_end,
            "outbox_max_age_seconds": self.outbox_max_age_seconds,
            "rpo_lost_events": self.rpo_lost_events,
            "rto_seconds": self.rto_seconds,
            "data_digest_before": self.data_digest_before,
            "data_digest_after": self.data_digest_after,
            "chaos": self.chaos,
            "redis_reason": self.redis_reason,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "report_id": self.report_id,
            "generated_at": self.generated_at,
            "window_started_at": self.window_started_at,
            "window_ended_at": self.window_ended_at,
            "memplex_version": self.memplex_version,
            "python_version": self.python_version,
            "postgres_version": self.postgres_version,
            "platform": self.platform,
            "machine_arch": self.machine_arch,
            "cpu_count": self.cpu_count,
            "memory_bytes": self.memory_bytes,
            "function_count": self.function_count,
            "edge_count": self.edge_count,
            "soak_seconds": self.soak_seconds,
            "operations_count": self.operations_count,
            "throughput_ops_per_second": self.throughput_ops_per_second,
            "read": self.read.to_dict(),
            "write": self.write.to_dict(),
            "sync": self.sync.to_dict(),
            "error_rate": self.error_rate,
            "rss_peak_bytes": self.rss_peak_bytes,
            "queue_depth_end": self.queue_depth_end,
            "outbox_max_age_seconds": self.outbox_max_age_seconds,
            "rpo_lost_events": self.rpo_lost_events,
            "rto_seconds": self.rto_seconds,
            "data_digest_before": self.data_digest_before,
            "data_digest_after": self.data_digest_after,
            "chaos": dict(self.chaos),
            "redis_reason": self.redis_reason,
            "contract_sha256": self.contract_sha256,
            "industrial_gate_closing": self.industrial_gate_closing,
            "key_id": self.key_id,
            "signature": self.signature,
        }

    def canonical_unsigned_bytes(self) -> bytes:
        raw = self.to_dict()
        raw["signature"] = "0" * 64
        return _canonical_json(raw)

    def canonical_bytes(self) -> bytes:
        return _canonical_json(self.to_dict())

    def verify(self, signing_key: bytes, *, expected_version: str) -> None:
        parsed = CapacityChaosEvidence.from_dict(self.to_dict())
        if parsed.memplex_version != _exact_text(expected_version):
            _fail()
        if parsed.contract_sha256 != capacity_chaos_contract_sha256():
            _fail()
        expected = hmac.new(
            _signing_key(signing_key), parsed.canonical_unsigned_bytes(), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(expected, parsed.signature):
            _fail()
        now = datetime.now(UTC)
        generated = _timestamp(parsed.generated_at)
        if now - generated > _MAX_EVIDENCE_AGE or generated - now > _MAX_FUTURE_SKEW:
            _fail("capacity_chaos_freshness_invalid")
        if not parsed.industrial_gate_closing:
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


def write_capacity_chaos_evidence(path: Path, evidence: CapacityChaosEvidence) -> None:
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
            _fail()
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
        os.rename(temporary_name, path.name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        temporary_created = False
        os.fsync(directory_fd)
    except CapacityChaosEvidenceError:
        raise
    except BaseException as exc:
        raise CapacityChaosEvidenceError() from exc
    finally:
        if directory_fd >= 0:
            if temporary_created:
                try:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                except OSError:
                    pass
            os.close(directory_fd)


def read_capacity_chaos_evidence(path: Path) -> CapacityChaosEvidence:
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
        return CapacityChaosEvidence.from_dict(json.loads(payload))
    except CapacityChaosEvidenceError:
        raise
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise CapacityChaosEvidenceError() from exc


def load_capacity_chaos_signing_key() -> bytes:
    raw = os.environ.get("MEMPLEX_CAPACITY_CHAOS_HMAC_KEY")
    if type(raw) is not str or len(raw) != 64:
        _fail()
    try:
        return _signing_key(bytes.fromhex(raw))
    except ValueError as exc:
        raise CapacityChaosEvidenceError() from exc
