"""G006 signed SLO evidence and shipped alert rules."""

from __future__ import annotations

import base64
import os
import subprocess
import sys
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


def test_operations_report_loader_rejects_a_fifo_without_blocking(tmp_path) -> None:
    fifo = tmp_path / "report.fifo"
    os.mkfifo(fifo)
    script = """
import sys
from pathlib import Path
from memplex.operations import OperationsEvidenceError, load_operations_report
try:
    load_operations_report(Path(sys.argv[1]))
except OperationsEvidenceError as exc:
    raise SystemExit(0 if str(exc) == "operations_report_invalid" else 2)
raise SystemExit(3)
"""

    completed = subprocess.run(
        [sys.executable, "-c", script, str(fifo)],
        check=False,
        capture_output=True,
        text=True,
        timeout=1,
    )

    assert completed.returncode == 0
    assert str(fifo) not in completed.stdout + completed.stderr


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
