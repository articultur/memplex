"""G006 signed SLO evidence and shipped alert rules."""

from __future__ import annotations

import base64
import os
import selectors
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path

import pytest

from memplex.config import MemplexConfig
from memplex.operations import (
    OperationsEvidenceError,
    OperationsEvidenceReport,
    OperationsReadinessBinding,
    alert_rules_bytes,
    alert_rules_sha256,
    create_operations_evidence,
    load_operations_report,
    write_operations_report_atomic,
)

_BINDING = OperationsReadinessBinding(
    deployment_id="production-us-east-1-20260812",
    source_sha256="1" * 64,
    artifact_sha256="2" * 64,
    target_identity_sha256="3" * 64,
    expected_key_id="ops-key",
)


def _config() -> MemplexConfig:
    config = MemplexConfig()
    config.operations.report_key_id = "ops-key"
    return config


def _metrics(
    *,
    successful: int = 999,
    p95: float = 100.0,
    latency_sample_count: int = 128,
) -> dict[str, object]:
    return {
        "request_count": 1000,
        "successful_requests": successful,
        "latency_sample_count": latency_sample_count,
        "p95_latency_ms": p95,
    }


def _create(*, successful: int = 999, p95: float = 100.0, drained: bool = True):
    return create_operations_evidence(
        metrics_snapshot=_metrics(successful=successful, p95=p95),
        shutdown_result={
            "request_drained": drained,
            "deadline_exceeded": not drained,
        },
        config=_config(),
        report_id="018f7f1d-7c9e-7c31-9d34-35f6a91e2bb8",
        window_started_at="2026-08-11T12:00:00.000000Z",
        window_ended_at="2026-08-11T12:05:00.000000Z",
        generated_at="2026-08-11T12:05:01.000000Z",
        readiness_binding=_BINDING,
        signing_key=b"o" * 32,
    )


def test_operations_evidence_closes_only_when_all_targets_and_drain_pass() -> None:
    passing = _create()
    passing.verify(b"o" * 32)
    assert passing.industrial_gate_closing is True
    assert _create(successful=998).industrial_gate_closing is False
    assert _create(p95=251.0).industrial_gate_closing is False
    assert _create(drained=False).industrial_gate_closing is False


def test_operations_evidence_rejects_stale_or_cross_deployment_gate_closure() -> None:
    report = _create()
    report.verify_readiness(
        b"o" * 32,
        binding=_BINDING,
        now="2026-08-11T12:15:00.000000Z",
    )

    stale = _BINDING
    with pytest.raises(OperationsEvidenceError, match="operations_report_invalid"):
        report.verify_readiness(
            b"o" * 32,
            binding=stale,
            now="2026-08-11T12:20:02.000000Z",
        )
    with pytest.raises(OperationsEvidenceError, match="operations_report_invalid"):
        report.verify_readiness(
            b"o" * 32,
            binding=OperationsReadinessBinding(
                deployment_id="different-production-deployment",
                source_sha256="1" * 64,
                artifact_sha256="2" * 64,
                target_identity_sha256="3" * 64,
                expected_key_id="ops-key",
            ),
            now="2026-08-11T12:15:00.000000Z",
        )


def test_operations_evidence_rejects_fresh_signature_over_stale_observation() -> None:
    report = create_operations_evidence(
        metrics_snapshot=_metrics(),
        shutdown_result={"request_drained": True, "deadline_exceeded": False},
        config=_config(),
        report_id="018f7f1d-7c9e-7c31-9d34-35f6a91e2bb8",
        window_started_at="2026-07-12T12:00:00.000000Z",
        window_ended_at="2026-07-12T12:05:00.000000Z",
        generated_at="2026-08-11T12:14:59.000000Z",
        readiness_binding=_BINDING,
        signing_key=b"o" * 32,
    )

    with pytest.raises(OperationsEvidenceError, match="operations_report_invalid"):
        report.verify_readiness(
            b"o" * 32,
            binding=_BINDING,
            now="2026-08-11T12:15:00.000000Z",
        )


def test_operations_evidence_rejects_window_ending_after_generation() -> None:
    with pytest.raises(OperationsEvidenceError, match="operations_report_invalid"):
        create_operations_evidence(
            metrics_snapshot=_metrics(),
            shutdown_result={"request_drained": True, "deadline_exceeded": False},
            config=_config(),
            report_id="018f7f1d-7c9e-7c31-9d34-35f6a91e2bb8",
            window_started_at="2026-08-11T12:00:00.000000Z",
            window_ended_at="2026-08-11T12:05:00.000000Z",
            generated_at="2026-08-11T12:04:59.000000Z",
            readiness_binding=_BINDING,
            signing_key=b"o" * 32,
        )


@pytest.mark.parametrize(
    "changed_binding",
    [
        OperationsReadinessBinding(
            deployment_id="production-us-east-1-20260812",
            source_sha256="4" * 64,
            artifact_sha256="2" * 64,
            target_identity_sha256="3" * 64,
            expected_key_id="ops-key",
        ),
        OperationsReadinessBinding(
            deployment_id="production-us-east-1-20260812",
            source_sha256="1" * 64,
            artifact_sha256="5" * 64,
            target_identity_sha256="3" * 64,
            expected_key_id="ops-key",
        ),
        OperationsReadinessBinding(
            deployment_id="production-us-east-1-20260812",
            source_sha256="1" * 64,
            artifact_sha256="2" * 64,
            target_identity_sha256="6" * 64,
            expected_key_id="ops-key",
        ),
        OperationsReadinessBinding(
            deployment_id="production-us-east-1-20260812",
            source_sha256="1" * 64,
            artifact_sha256="2" * 64,
            target_identity_sha256="3" * 64,
            expected_key_id="previous-ops-key",
        ),
    ],
)
def test_operations_evidence_rejects_each_mismatched_signed_binding(
    changed_binding: OperationsReadinessBinding,
) -> None:
    config = _config()
    config.operations.report_key_id = changed_binding.expected_key_id
    changed = create_operations_evidence(
        metrics_snapshot=_metrics(),
        shutdown_result={"request_drained": True, "deadline_exceeded": False},
        config=config,
        report_id="018f7f1d-7c9e-7c31-9d34-35f6a91e2bb8",
        window_started_at="2026-08-11T12:00:00.000000Z",
        window_ended_at="2026-08-11T12:05:00.000000Z",
        generated_at="2026-08-11T12:05:01.000000Z",
        readiness_binding=changed_binding,
        signing_key=b"o" * 32,
    )
    with pytest.raises(OperationsEvidenceError, match="operations_report_invalid"):
        changed.verify_readiness(
            b"o" * 32,
            binding=_BINDING,
            now="2026-08-11T12:15:00.000000Z",
        )


def test_operations_evidence_refuses_to_sign_a_report_with_the_wrong_key_id() -> None:
    with pytest.raises(OperationsEvidenceError, match="operations_report_invalid"):
        OperationsEvidenceReport.create(
            report_id="018f7f1d-7c9e-7c31-9d34-35f6a91e2bb8",
            generated_at="2026-08-11T12:05:01.000000Z",
            window_started_at="2026-08-11T12:00:00.000000Z",
            window_ended_at="2026-08-11T12:05:00.000000Z",
            readiness_binding=_BINDING,
            request_count=1000,
            successful_requests=999,
            latency_sample_count=128,
            availability=0.999,
            error_rate=0.001,
            p95_latency_ms=100.0,
            availability_target=0.999,
            error_rate_target=0.001,
            p95_latency_target_ms=250.0,
            shutdown_drained=True,
            shutdown_deadline_exceeded=False,
            alert_rules_sha256="a" * 64,
            key_id="wrong-ops-key",
            signing_key=b"o" * 32,
        )


def test_operations_evidence_requires_full_window_and_sample_gate() -> None:
    report = create_operations_evidence(
        metrics_snapshot={
            "request_count": 999,
            "successful_requests": 999,
            "latency_sample_count": 128,
            "p95_latency_ms": 100.0,
        },
        shutdown_result={"request_drained": True, "deadline_exceeded": False},
        config=_config(),
        report_id="018f7f1d-7c9e-7c31-9d34-35f6a91e2bb8",
        window_started_at="2026-08-11T12:00:01.000000Z",
        window_ended_at="2026-08-11T12:05:00.000000Z",
        generated_at="2026-08-11T12:05:01.000000Z",
        readiness_binding=_BINDING,
        signing_key=b"o" * 32,
    )
    assert report.industrial_gate_closing is False


def test_operations_evidence_requires_enough_latency_samples_for_gate_closure() -> None:
    report = create_operations_evidence(
        metrics_snapshot=_metrics(latency_sample_count=127),
        shutdown_result={"request_drained": True, "deadline_exceeded": False},
        config=_config(),
        report_id="018f7f1d-7c9e-7c31-9d34-35f6a91e2bb8",
        window_started_at="2026-08-11T12:00:00.000000Z",
        window_ended_at="2026-08-11T12:05:00.000000Z",
        generated_at="2026-08-11T12:05:01.000000Z",
        readiness_binding=_BINDING,
        signing_key=b"o" * 32,
    )
    assert report.industrial_gate_closing is False
    with pytest.raises(OperationsEvidenceError, match="operations_report_invalid"):
        report.verify_readiness(
            b"o" * 32,
            binding=_BINDING,
            now="2026-08-11T12:15:00.000000Z",
        )


@pytest.mark.parametrize("value", [True, 1_001])
def test_operations_evidence_rejects_weak_or_unbounded_latency_sample_count(
    value: object,
) -> None:
    raw = _create().to_dict()
    raw["latency_sample_count"] = value
    with pytest.raises(OperationsEvidenceError, match="operations_report_invalid"):
        OperationsEvidenceReport.from_dict(raw)


def test_operations_evidence_signature_covers_latency_sample_count() -> None:
    raw = _create().to_dict()
    raw["latency_sample_count"] = 129
    tampered = OperationsEvidenceReport.from_dict(raw)
    with pytest.raises(OperationsEvidenceError, match="operations_report_signature_invalid"):
        tampered.verify(b"o" * 32)


def test_packaged_and_operator_alert_rules_are_exact_and_low_cardinality() -> None:
    packaged = alert_rules_bytes()
    operator = Path("deploy/prometheus/memplex-alerts.yml").read_bytes()
    assert packaged == operator
    assert len(alert_rules_sha256()) == 64
    text = packaged.decode("utf-8")
    for name in (
        "MemplexNotReady",
        "MemplexErrorBudgetBurn",
        "MemplexP95LatencyHigh",
        "MemplexWorkerBacklog",
        "MemplexSyncBacklog",
        "MemplexDeadLettersPresent",
        "MemplexPoolSaturated",
        "MemplexShutdownDeadlineExceeded",
    ):
        assert f"alert: {name}" in text
    for forbidden in ("tenant_id", "subject_id", "workspace_id", "memory_id", "exception"):
        assert forbidden not in text


def test_signed_report_file_roundtrip_does_not_expose_key(tmp_path, monkeypatch) -> None:
    key = b"o" * 32
    encoded = base64.b64encode(key).decode("ascii")
    monkeypatch.setenv("MEMPLEX_OPERATIONS_HMAC_KEY", encoded)
    path = tmp_path / "operations-report.json"
    path.write_bytes(_create().to_json())
    loaded = load_operations_report(path)
    loaded.verify(key)
    assert encoded.encode("ascii") not in path.read_bytes()


def test_operations_report_loader_rejects_a_symlink_in_an_ancestor(tmp_path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    report = real / "report.json"
    report.write_bytes(_create().to_json())
    link = tmp_path / "linked-parent"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(OperationsEvidenceError) as caught:
        load_operations_report(link / "report.json")

    assert str(caught.value) == "operations_report_invalid"
    assert str(link) not in str(caught.value)


def test_operations_report_loader_rejects_a_file_symlink(tmp_path) -> None:
    report = tmp_path / "report.json"
    report.write_bytes(_create().to_json())
    link = tmp_path / "linked-report.json"
    link.symlink_to(report)

    with pytest.raises(OperationsEvidenceError) as caught:
        load_operations_report(link)

    assert str(caught.value) == "operations_report_invalid"
    assert str(link) not in str(caught.value)


def test_operations_report_loader_rejects_a_directory(tmp_path) -> None:
    directory = tmp_path / "report-directory"
    directory.mkdir()

    with pytest.raises(OperationsEvidenceError) as caught:
        load_operations_report(directory)

    assert str(caught.value) == "operations_report_invalid"
    assert str(directory) not in str(caught.value)


def _cleanup_fifo_probe(process, failure: BaseException | None) -> None:
    reaped = False
    for _ in range(2):
        try:
            if process.poll() is None:
                process.kill()
        except BaseException as exc:  # noqa: BLE001 -- independently reap and retry this owned child
            if failure is None:
                failure = exc
        try:
            process.wait(timeout=2)
            reaped = True
        except BaseException as exc:  # noqa: BLE001 -- retain the primary failure through bounded cleanup
            if failure is None:
                failure = exc
        if reaped:
            break
    for pipe in (process.stdin, process.stdout, process.stderr):
        try:
            if pipe is not None:
                pipe.close()
        except BaseException as exc:  # noqa: BLE001 -- close every pipe without masking the primary failure
            if failure is None:
                failure = exc
    if not reaped:
        message = "operations-report child not reaped"
        if failure is None:
            failure = AssertionError(message)
        else:
            failure.add_note(message)
    if failure is not None:
        raise failure


@pytest.mark.parametrize("probe_mode", ["normal", "delayed_start", "never_ready", "early_eof", "blocking_loader"])
def test_operations_report_loader_rejects_a_fifo_without_blocking(tmp_path, probe_mode) -> None:
    fifo = tmp_path / "report.fifo"
    os.mkfifo(fifo)
    script = r"""
import sys
import os
import time
from pathlib import Path
if sys.argv[2] == "delayed_start":
    time.sleep(1.25)
from memplex.operations import OperationsEvidenceError, load_operations_report
if sys.argv[2] == "never_ready":
    sys.stdin.readline()
if sys.argv[2] == "early_eof":
    raise SystemExit(5)
os.write(sys.stdout.fileno(), b"R")
if sys.stdin.readline() != "start\n":
    raise SystemExit(4)
try:
    if sys.argv[2] == "blocking_loader":
        os.open(sys.argv[1], os.O_RDONLY)
    load_operations_report(Path(sys.argv[1]))
except OperationsEvidenceError as exc:
    raise SystemExit(0 if str(exc) == "operations_report_invalid" else 2)
raise SystemExit(3)
"""

    command = [sys.executable, "-c", script, str(fifo), probe_mode]
    process = subprocess.Popen(
        command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )
    failure: BaseException | None = None
    try:
        assert process.stdout is not None
        # Import readiness has its own bounded setup watchdog. The loader
        # retains the original one-second deadline after the start signal.
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout, selectors.EVENT_READ)
            ready = selector.select(timeout=10)
        startup_failure = probe_mode in {"never_ready", "early_eof"}
        with pytest.raises(AssertionError) if startup_failure else nullcontext():
            assert ready, "operations-report probe startup timed out"
            assert os.read(process.stdout.fileno(), 1) == b"R", "probe exited before ready"
        if startup_failure:
            return
        if probe_mode == "blocking_loader":
            with pytest.raises(subprocess.TimeoutExpired):
                process.communicate(input="start\n", timeout=1)
            assert process.poll() is None
            return
        stdout, stderr = process.communicate(input="start\n", timeout=1)
        assert process.returncode == 0
        assert str(fifo) not in stdout + stderr
    except BaseException as exc:  # noqa: BLE001 -- preserve the probe failure through owned-child cleanup
        failure = exc
    finally:
        _cleanup_fifo_probe(process, failure)


@pytest.mark.parametrize("has_primary_failure", [False, True])
@pytest.mark.parametrize(
    "cleanup_fault", ["kill_interrupt", "kill_error", "wait_timeout", "wait_interrupt", "persistent_wait"]
)
def test_fifo_probe_cleanup_faults_preserve_failure_and_bounds(
    tmp_path, monkeypatch, has_primary_failure, cleanup_fault,
) -> None:
    original_popen = subprocess.Popen
    primary = AssertionError("original FIFO probe failure")
    injected = (
        OSError("kill failed") if cleanup_fault == "kill_error"
        else subprocess.TimeoutExpired("owned FIFO probe", 2)
        if cleanup_fault in {"wait_timeout", "persistent_wait"}
        else KeyboardInterrupt("cleanup interrupted")
    )
    owned = []

    class CleanupFaultPopen(original_popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.cleaning = False
            self.cleanup_waits = []
            self.cleanup_kills = 0
            owned.append(self)

        def communicate(self, *args, **kwargs):
            try:
                if has_primary_failure:
                    raise primary
                return super().communicate(*args, **kwargs)
            finally:
                self.cleaning = True

        def kill(self):
            self.cleanup_kills += 1
            if cleanup_fault.startswith("kill_") and self.cleanup_kills == 1:
                raise injected
            return super().kill()

        def wait(self, timeout=None):
            if self.cleaning:
                self.cleanup_waits.append(timeout)
                assert timeout is not None, "FIFO cleanup attempted an unbounded wait"
                if cleanup_fault == "persistent_wait" or (
                    cleanup_fault.startswith("wait_") and len(self.cleanup_waits) == 1
                ):
                    raise injected
            return super().wait(timeout=timeout)

    monkeypatch.setattr(subprocess, "Popen", CleanupFaultPopen)
    try:
        with pytest.raises(BaseException) as caught:
            test_operations_report_loader_rejects_a_fifo_without_blocking(
                tmp_path, "normal" if has_primary_failure else "blocking_loader",
            )
        assert len(owned) == 1
        process = owned[0]
        assert caught.value is (primary if has_primary_failure else injected)
        assert process.cleanup_waits and all(timeout == 2 for timeout in process.cleanup_waits)
        assert all(pipe.closed for pipe in (process.stdin, process.stdout, process.stderr))
        if cleanup_fault == "persistent_wait":
            assert "operations-report child not reaped" in getattr(caught.value, "__notes__", [])
            assert len(process.cleanup_waits) == 2
        else:
            assert process.returncode is not None
        if cleanup_fault.startswith("kill_"):
            assert process.cleanup_kills == 2
    finally:
        # The real owned child is also reaped when the regression assertion is RED.
        for process in owned:
            if process.poll() is None:
                original_popen.kill(process)
            original_popen.wait(process, timeout=2)
            for pipe in (process.stdin, process.stdout, process.stderr):
                if pipe is not None:
                    pipe.close()


def test_operations_report_loader_accepts_128_kib_and_rejects_one_more_byte(
    tmp_path,
) -> None:
    payload = _create().to_json()
    at_limit = tmp_path / "at-limit.json"
    at_limit.write_bytes(payload + b" " * (131_072 - len(payload)))
    assert load_operations_report(at_limit).report_id == _create().report_id

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(payload + b" " * (131_073 - len(payload)))
    with pytest.raises(OperationsEvidenceError) as caught:
        load_operations_report(oversized)

    assert str(caught.value) == "operations_report_invalid"
    assert str(oversized) not in str(caught.value)


def test_atomic_report_writer_rejects_a_symlink_in_any_ancestor(tmp_path) -> None:
    real = tmp_path / "real"
    nested = real / "nested"
    nested.mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    redirected = nested / "report.json"

    with pytest.raises(
        OperationsEvidenceError, match="operations_report_output_invalid"
    ):
        write_operations_report_atomic(link / "nested" / "report.json", _create())

    assert not redirected.exists()
