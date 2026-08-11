"""生产运行状态与签名 SLO 证据。

本模块只处理 data-only 的生命周期和证据格式，不启动服务、不访问网络，
也不保存任何 DSN、路径或密钥材料。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import stat
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path


class OperationsEvidenceError(ValueError):
    """运行证据无效，错误文本固定且不包含输入。"""


_REPORT_KEYS = frozenset(
    {
        "schema_version",
        "report_id",
        "window_started_at",
        "window_ended_at",
        "request_count",
        "successful_requests",
        "availability",
        "error_rate",
        "p95_latency_ms",
        "availability_target",
        "error_rate_target",
        "p95_latency_target_ms",
        "shutdown_drained",
        "shutdown_deadline_exceeded",
        "alert_rules_sha256",
        "industrial_gate_closing",
        "key_id",
        "signature",
    }
)


def _canonical_json(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise OperationsEvidenceError("operations_report_invalid") from exc


def _json_object_no_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise OperationsEvidenceError("operations_report_invalid")
        result[key] = value
    return result


def _require_string(value: object) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise OperationsEvidenceError("operations_report_invalid")
    if "\x00" in value or any(0xD800 <= ord(char) <= 0xDFFF for char in value):
        raise OperationsEvidenceError("operations_report_invalid")
    return value


def _require_timestamp(value: object) -> str:
    raw = _require_string(value)
    try:
        parsed = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as exc:
        raise OperationsEvidenceError("operations_report_invalid") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%S.%fZ") != raw:
        raise OperationsEvidenceError("operations_report_invalid")
    return raw


def _require_nonnegative_int(value: object) -> int:
    if type(value) is not int or value < 0:
        raise OperationsEvidenceError("operations_report_invalid")
    return value


def _require_finite_float(value: object, *, minimum: float, maximum: float | None = None) -> float:
    if type(value) is not float or not math.isfinite(value) or value < minimum:
        raise OperationsEvidenceError("operations_report_invalid")
    if maximum is not None and value > maximum:
        raise OperationsEvidenceError("operations_report_invalid")
    return value


@dataclass(frozen=True, slots=True)
class OperationsEvidenceReport:
    schema_version: int
    report_id: str
    window_started_at: str
    window_ended_at: str
    request_count: int
    successful_requests: int
    availability: float
    error_rate: float
    p95_latency_ms: float
    availability_target: float
    error_rate_target: float
    p95_latency_target_ms: float
    shutdown_drained: bool
    shutdown_deadline_exceeded: bool
    alert_rules_sha256: str
    industrial_gate_closing: bool
    key_id: str
    signature: str

    @classmethod
    def create(
        cls,
        *,
        report_id: str,
        window_started_at: str,
        window_ended_at: str,
        request_count: int,
        successful_requests: int,
        availability: float,
        error_rate: float,
        p95_latency_ms: float,
        availability_target: float,
        error_rate_target: float,
        p95_latency_target_ms: float,
        shutdown_drained: bool,
        shutdown_deadline_exceeded: bool,
        alert_rules_sha256: str,
        key_id: str,
        signing_key: bytes,
    ) -> "OperationsEvidenceReport":
        gate = (
            shutdown_drained
            and not shutdown_deadline_exceeded
            and availability + 1e-12 >= availability_target
            and error_rate <= error_rate_target + 1e-12
            and p95_latency_ms <= p95_latency_target_ms
            and request_count > 0
        )
        unsigned = cls(
            1,
            report_id,
            window_started_at,
            window_ended_at,
            request_count,
            successful_requests,
            availability,
            error_rate,
            p95_latency_ms,
            availability_target,
            error_rate_target,
            p95_latency_target_ms,
            shutdown_drained,
            shutdown_deadline_exceeded,
            alert_rules_sha256,
            gate,
            key_id,
            "0" * 64,
        )
        validated = cls.from_dict(unsigned.to_dict())
        signature = hmac.new(
            _require_key(signing_key),
            validated.canonical_unsigned_bytes(),
            hashlib.sha256,
        ).hexdigest()
        return replace(validated, signature=signature)

    @classmethod
    def from_dict(cls, raw: object) -> "OperationsEvidenceReport":
        try:
            if type(raw) is not dict or set(raw) != _REPORT_KEYS:
                raise OperationsEvidenceError("operations_report_invalid")
            if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
                raise OperationsEvidenceError("operations_report_invalid")
            report_id = _require_string(raw["report_id"])
            if str(uuid.UUID(report_id)) != report_id:
                raise OperationsEvidenceError("operations_report_invalid")
            started = _require_timestamp(raw["window_started_at"])
            ended = _require_timestamp(raw["window_ended_at"])
            if ended <= started:
                raise OperationsEvidenceError("operations_report_invalid")
            request_count = _require_nonnegative_int(raw["request_count"])
            successful = _require_nonnegative_int(raw["successful_requests"])
            if successful > request_count:
                raise OperationsEvidenceError("operations_report_invalid")
            availability = _require_finite_float(raw["availability"], minimum=0.0, maximum=1.0)
            error_rate = _require_finite_float(raw["error_rate"], minimum=0.0, maximum=1.0)
            p95 = _require_finite_float(raw["p95_latency_ms"], minimum=0.0)
            availability_target = _require_finite_float(raw["availability_target"], minimum=0.0, maximum=1.0)
            error_target = _require_finite_float(raw["error_rate_target"], minimum=0.0, maximum=1.0)
            p95_target = _require_finite_float(raw["p95_latency_target_ms"], minimum=0.0)
            if request_count:
                expected_availability = successful / request_count
                if not math.isclose(availability, expected_availability, rel_tol=0.0, abs_tol=1e-12):
                    raise OperationsEvidenceError("operations_report_invalid")
                if not math.isclose(error_rate, 1.0 - availability, rel_tol=0.0, abs_tol=1e-12):
                    raise OperationsEvidenceError("operations_report_invalid")
            elif availability != 0.0 or error_rate != 0.0:
                raise OperationsEvidenceError("operations_report_invalid")
            drained = raw["shutdown_drained"]
            exceeded = raw["shutdown_deadline_exceeded"]
            gate = raw["industrial_gate_closing"]
            if type(drained) is not bool or type(exceeded) is not bool or type(gate) is not bool:
                raise OperationsEvidenceError("operations_report_invalid")
            digest = _require_string(raw["alert_rules_sha256"])
            signature = _require_string(raw["signature"])
            if (
                len(digest) != 64
                or digest != digest.lower()
                or any(char not in "0123456789abcdef" for char in digest)
                or len(signature) != 64
                or signature != signature.lower()
                or any(char not in "0123456789abcdef" for char in signature)
            ):
                raise OperationsEvidenceError("operations_report_invalid")
            expected_gate = (
                request_count > 0
                and drained
                and not exceeded
                and availability + 1e-12 >= availability_target
                and error_rate <= error_target + 1e-12
                and p95 <= p95_target
            )
            if gate is not expected_gate:
                raise OperationsEvidenceError("operations_report_invalid")
            return cls(
                1,
                report_id,
                started,
                ended,
                request_count,
                successful,
                availability,
                error_rate,
                p95,
                availability_target,
                error_target,
                p95_target,
                drained,
                exceeded,
                digest,
                gate,
                _require_string(raw["key_id"]),
                signature,
            )
        except OperationsEvidenceError:
            raise
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise OperationsEvidenceError("operations_report_invalid") from exc

    @classmethod
    def from_json(cls, payload: bytes) -> "OperationsEvidenceReport":
        try:
            if type(payload) is not bytes:
                raise OperationsEvidenceError("operations_report_invalid")
            raw = json.loads(payload.decode("utf-8"), object_pairs_hook=_json_object_no_duplicates)
        except OperationsEvidenceError:
            raise
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise OperationsEvidenceError("operations_report_invalid") from exc
        return cls.from_dict(raw)

    def unsigned_dict(self) -> dict[str, object]:
        result = self.to_dict()
        result.pop("signature")
        return result

    def canonical_unsigned_bytes(self) -> bytes:
        return _canonical_json(self.unsigned_dict())

    def to_dict(self) -> dict[str, object]:
        return {key: getattr(self, key) for key in _REPORT_KEYS}

    def to_json(self) -> bytes:
        return _canonical_json(self.to_dict())

    def verify(self, signing_key: bytes) -> None:
        expected = hmac.new(
            _require_key(signing_key),
            self.canonical_unsigned_bytes(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(expected, self.signature):
            raise OperationsEvidenceError("operations_report_signature_invalid")


def _require_key(value: object) -> bytes:
    if type(value) is not bytes or len(value) != 32:
        raise OperationsEvidenceError("operations_signing_key_invalid")
    return value


def load_operations_signing_key() -> bytes:
    raw = os.environ.get("MEMPLEX_OPERATIONS_HMAC_KEY")
    try:
        if type(raw) is not str or not raw or raw != raw.strip():
            raise OperationsEvidenceError("operations_signing_key_invalid")
        decoded = base64.b64decode(raw, validate=True)
        if base64.b64encode(decoded).decode("ascii") != raw:
            raise OperationsEvidenceError("operations_signing_key_invalid")
        return _require_key(decoded)
    except OperationsEvidenceError:
        raise
    except (ValueError, TypeError) as exc:
        raise OperationsEvidenceError("operations_signing_key_invalid") from exc


def load_operations_report(path: Path) -> OperationsEvidenceReport:
    """Load one report without exposing its path or parser details."""
    try:
        if not isinstance(path, Path):
            raise OperationsEvidenceError("operations_report_invalid")
        return OperationsEvidenceReport.from_json(path.read_bytes())
    except OperationsEvidenceError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise OperationsEvidenceError("operations_report_invalid") from exc


def alert_rules_bytes() -> bytes:
    """Return the packaged canonical Prometheus rules bytes."""
    try:
        return (
            resources.files("memplex.operations_assets")
            .joinpath("memplex-alerts.yml")
            .read_bytes()
        )
    except (OSError, ModuleNotFoundError) as exc:
        raise OperationsEvidenceError("operations_alert_rules_unavailable") from exc


def alert_rules_sha256() -> str:
    return hashlib.sha256(alert_rules_bytes()).hexdigest()


def create_operations_evidence(
    *,
    metrics_snapshot: dict[str, object],
    shutdown_result: dict[str, object],
    config: object,
    report_id: str,
    window_started_at: str,
    window_ended_at: str,
    signing_key: bytes,
) -> OperationsEvidenceReport:
    """Build signed evidence from bounded metrics and a completed drain."""
    try:
        operations = getattr(config, "operations")
        request_count = metrics_snapshot["request_count"]
        successful_requests = metrics_snapshot["successful_requests"]
        p95_latency_ms = metrics_snapshot["p95_latency_ms"]
        if type(request_count) is not int or type(successful_requests) is not int:
            raise OperationsEvidenceError("operations_evidence_input_invalid")
        if type(p95_latency_ms) not in {int, float}:
            raise OperationsEvidenceError("operations_evidence_input_invalid")
        if request_count <= 0 or successful_requests < 0 or successful_requests > request_count:
            raise OperationsEvidenceError("operations_evidence_input_invalid")
        availability = float(successful_requests / request_count)
        error_rate = float(1.0 - availability)
        request_drained = shutdown_result["request_drained"]
        deadline_exceeded = shutdown_result["deadline_exceeded"]
        if type(request_drained) is not bool or type(deadline_exceeded) is not bool:
            raise OperationsEvidenceError("operations_evidence_input_invalid")
        return OperationsEvidenceReport.create(
            report_id=report_id,
            window_started_at=window_started_at,
            window_ended_at=window_ended_at,
            request_count=request_count,
            successful_requests=successful_requests,
            availability=availability,
            error_rate=error_rate,
            p95_latency_ms=float(p95_latency_ms),
            availability_target=operations.availability_target,
            error_rate_target=operations.error_rate_target,
            p95_latency_target_ms=operations.p95_latency_target_ms,
            shutdown_drained=request_drained,
            shutdown_deadline_exceeded=deadline_exceeded,
            alert_rules_sha256=alert_rules_sha256(),
            key_id=operations.report_key_id,
            signing_key=signing_key,
        )
    except OperationsEvidenceError:
        raise
    except (AttributeError, KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
        raise OperationsEvidenceError("operations_evidence_input_invalid") from exc


def utc_timestamp_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def write_operations_report_atomic(
    path: Path, report: OperationsEvidenceReport
) -> None:
    """Atomically replace one report through a pinned parent directory fd."""
    if not isinstance(path, Path) or not path.name or path.name in {".", ".."}:
        raise OperationsEvidenceError("operations_report_output_invalid")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    file_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = -1
    temp_name = ".%s.%s.tmp" % (path.name, uuid.uuid4().hex)
    temp_created = False
    try:
        parent = path.parent
        if parent.is_absolute():
            directory_fd = os.open(os.sep, directory_flags)
            components = parent.parts[1:]
        else:
            directory_fd = os.open(".", directory_flags)
            components = parent.parts
        for component in components:
            if component in {"", "."}:
                continue
            if component == "..":
                raise OperationsEvidenceError("operations_report_output_invalid")
            next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        try:
            existing = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        except FileNotFoundError:
            existing = None
        if existing is not None and not stat.S_ISREG(existing.st_mode):
            raise OperationsEvidenceError("operations_report_output_invalid")
        fd = os.open(temp_name, file_flags, 0o600, dir_fd=directory_fd)
        temp_created = True
        try:
            payload = report.to_json()
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short write")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.rename(
            temp_name,
            path.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        temp_created = False
        os.fsync(directory_fd)
    except OperationsEvidenceError:
        raise
    except BaseException as exc:
        raise OperationsEvidenceError("operations_report_output_invalid") from exc
    finally:
        if directory_fd >= 0:
            if temp_created:
                try:
                    os.unlink(temp_name, dir_fd=directory_fd)
                except OSError:
                    pass
            os.close(directory_fd)


class RuntimeLifecycle:
    """单向、线程安全的进程生命周期。"""

    _ALLOWED = {
        "starting": frozenset({"ready", "draining", "stopped", "faulted"}),
        "ready": frozenset({"draining", "faulted"}),
        "draining": frozenset({"stopped", "faulted"}),
        "stopped": frozenset(),
        "faulted": frozenset(),
    }

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._state = "starting"

    @property
    def state(self) -> str:
        with self._condition:
            return self._state

    def _transition(self, target: str) -> None:
        with self._condition:
            if target not in self._ALLOWED[self._state]:
                raise RuntimeError("operations_lifecycle_transition_invalid")
            self._state = target
            self._condition.notify_all()

    def mark_ready(self) -> None:
        self._transition("ready")

    def start_draining(self) -> None:
        self._transition("draining")

    def mark_stopped(self) -> None:
        self._transition("stopped")

    def mark_faulted(self) -> None:
        self._transition("faulted")


class RequestAdmission:
    """为 HTTP 业务请求提供有界、可等待的 admission 计数。"""

    def __init__(self) -> None:
        self._condition = threading.Condition(threading.RLock())
        self._accepting = True
        self._active = 0

    @property
    def active(self) -> int:
        with self._condition:
            return self._active

    @property
    def accepting(self) -> bool:
        with self._condition:
            return self._accepting

    def begin(self) -> bool:
        with self._condition:
            if not self._accepting:
                return False
            self._active += 1
            return True

    def end(self) -> None:
        with self._condition:
            if self._active <= 0:
                raise RuntimeError("operations_request_admission_unbalanced")
            self._active -= 1
            if self._active == 0:
                self._condition.notify_all()

    def start_draining(self) -> None:
        with self._condition:
            self._accepting = False
            self._condition.notify_all()

    def wait_for_zero(self, timeout_seconds: float) -> bool:
        if (
            type(timeout_seconds) not in {int, float}
            or not math.isfinite(float(timeout_seconds))
            or timeout_seconds < 0
        ):
            raise TypeError("operations_request_drain_timeout_invalid")
        deadline = time.monotonic() + float(timeout_seconds)
        with self._condition:
            while self._active:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True


class OperationsMetrics:
    """线程安全、固定 label 集的进程内 Prometheus 指标。"""

    _METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "OTHER")
    _STATUS_CLASSES = ("1xx", "2xx", "3xx", "4xx", "5xx")
    _BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0)

    def __init__(self, *, max_latency_samples: int = 100_000) -> None:
        if type(max_latency_samples) is not int or max_latency_samples <= 0:
            raise ValueError("operations_metrics_sample_bound_invalid")
        self._lock = threading.RLock()
        self._in_flight = 0
        self._requests = {
            (method, status): 0
            for method in self._METHODS
            for status in self._STATUS_CLASSES
        }
        self._bucket_counts = {
            method: [0] * (len(self._BUCKETS) + 1) for method in self._METHODS
        }
        self._duration_sum = {method: 0.0 for method in self._METHODS}
        self._duration_count = {method: 0 for method in self._METHODS}
        self._latencies: deque[float] = deque(maxlen=max_latency_samples)

    @staticmethod
    def _method(value: object) -> str:
        return value if type(value) is str and value in OperationsMetrics._METHODS[:-1] else "OTHER"

    @staticmethod
    def _status_class(status_code: object) -> str:
        if type(status_code) is int and 100 <= status_code <= 599:
            return f"{status_code // 100}xx"
        return "5xx"

    def begin_request(self) -> None:
        with self._lock:
            self._in_flight += 1

    def finish_request(self, method: object, status_code: object, duration_seconds: object) -> None:
        if type(duration_seconds) not in {int, float}:
            raise TypeError("operations_metrics_duration_invalid")
        duration = float(duration_seconds)
        if not math.isfinite(duration) or duration < 0.0:
            raise ValueError("operations_metrics_duration_invalid")
        normalized_method = self._method(method)
        status_class = self._status_class(status_code)
        with self._lock:
            if self._in_flight <= 0:
                raise RuntimeError("operations_metrics_request_unbalanced")
            self._in_flight -= 1
            self._requests[(normalized_method, status_class)] += 1
            for index, bucket in enumerate(self._BUCKETS):
                if duration <= bucket:
                    self._bucket_counts[normalized_method][index] += 1
            self._bucket_counts[normalized_method][-1] += 1
            self._duration_sum[normalized_method] += duration
            self._duration_count[normalized_method] += 1
            self._latencies.append(duration)

    def snapshot(self) -> dict[str, int | float]:
        with self._lock:
            request_count = sum(self._requests.values())
            failures = sum(
                count
                for (method, status), count in self._requests.items()
                if method in self._METHODS and status == "5xx"
            )
            samples = sorted(self._latencies)
            if samples:
                index = max(0, math.ceil(len(samples) * 0.95) - 1)
                p95_ms = samples[index] * 1000.0
            else:
                p95_ms = 0.0
            return {
                "request_count": request_count,
                "successful_requests": request_count - failures,
                "in_flight": self._in_flight,
                "latency_sample_count": len(samples),
                "p95_latency_ms": p95_ms,
            }

    @staticmethod
    def _number(value: object) -> str:
        if type(value) is bool or type(value) not in {int, float}:
            return "0"
        numeric = float(value)
        if not math.isfinite(numeric):
            return "0"
        if type(value) is int:
            return str(value)
        return format(numeric, ".17g")

    def render_prometheus(self, runtime: dict[str, object]) -> str:
        with self._lock:
            lines = [
                "# HELP memplex_http_requests_total Completed business HTTP requests",
                "# TYPE memplex_http_requests_total counter",
            ]
            for method in self._METHODS:
                for status in self._STATUS_CLASSES:
                    lines.append(
                        'memplex_http_requests_total{method="%s",status_class="%s"} %d'
                        % (method, status, self._requests[(method, status)])
                    )
            lines.extend(
                [
                    "# HELP memplex_http_request_duration_seconds Business HTTP request duration",
                    "# TYPE memplex_http_request_duration_seconds histogram",
                ]
            )
            for method in self._METHODS:
                for index, bucket in enumerate(self._BUCKETS):
                    lines.append(
                        'memplex_http_request_duration_seconds_bucket{method="%s",le="%s"} %d'
                        % (method, format(bucket, "g"), self._bucket_counts[method][index])
                    )
                lines.append(
                    'memplex_http_request_duration_seconds_bucket{method="%s",le="+Inf"} %d'
                    % (method, self._bucket_counts[method][-1])
                )
                lines.append(
                    'memplex_http_request_duration_seconds_sum{method="%s"} %s'
                    % (method, self._number(self._duration_sum[method]))
                )
                lines.append(
                    'memplex_http_request_duration_seconds_count{method="%s"} %d'
                    % (method, self._duration_count[method])
                )
            lines.extend(
                [
                    "# HELP memplex_http_in_flight Active admitted business requests",
                    "# TYPE memplex_http_in_flight gauge",
                    f"memplex_http_in_flight {self._in_flight}",
                    "# HELP memplex_runtime_state One when runtime is ready",
                    "# TYPE memplex_runtime_state gauge",
                    "memplex_runtime_state %d" % (1 if runtime.get("runtime_state") == "ready" else 0),
                ]
            )
            for name, help_text in (
                ("worker_pending", "Durable worker pending tasks"),
                ("worker_leased", "Durable worker leased tasks"),
                ("worker_dead_letters", "Durable worker dead letters"),
                ("sync_pending", "Durable sync pending deliveries"),
                ("sync_leased", "Durable sync leased deliveries"),
                ("sync_dead_letters", "Durable sync dead letters"),
                ("pool_business_leases", "PostgreSQL published business leases"),
                ("pool_high_watermark", "PostgreSQL business lease high watermark"),
                ("pool_max_connections", "PostgreSQL business pool capacity"),
                ("shutdown_deadline_exceeded_total", "Shutdown drain deadline exceedances"),
            ):
                metric = "memplex_" + name
                metric_type = "counter" if name.endswith("_total") else "gauge"
                lines.extend(
                    [
                        f"# HELP {metric} {help_text}",
                        f"# TYPE {metric} {metric_type}",
                        f"{metric} {self._number(runtime.get(name, 0))}",
                    ]
                )
            return "\n".join(lines) + "\n"

    def record_shutdown_deadline_exceeded(self) -> None:
        with self._lock:
            current = getattr(self, "_shutdown_deadline_exceeded_total", 0)
            self._shutdown_deadline_exceeded_total = current + 1

    @property
    def shutdown_deadline_exceeded_total(self) -> int:
        with self._lock:
            return getattr(self, "_shutdown_deadline_exceeded_total", 0)
