"""G009 signed capacity, soak, and chaos evidence contract."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from memplex.capacity_chaos import (
    CapacityChaosEvidence,
    CapacityChaosEvidenceError,
    WorkloadMetrics,
    capacity_chaos_contract_sha256,
    read_capacity_chaos_evidence,
    write_capacity_chaos_evidence,
)


def _timestamp(offset: timedelta = timedelta()) -> str:
    return (datetime.now(UTC) + offset).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _report(**overrides: object) -> CapacityChaosEvidence:
    now = datetime.now(UTC)
    values: dict[str, object] = {
        "report_id": "018f7f1d-7c9e-7c31-9d34-35f6a91e2bb8",
        "generated_at": now.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "window_started_at": (now - timedelta(seconds=62)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "window_ended_at": (now - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "memplex_version": "3.3.0",
        "python_version": "3.13.5",
        "postgres_version": "16.4",
        "platform": "macOS-15.6",
        "machine_arch": "arm64",
        "cpu_count": 10,
        "memory_bytes": 32 * 1024**3,
        "function_count": 100_000,
        "edge_count": 1_000_000,
        "soak_seconds": 61.0,
        "operations_count": 3_000,
        "throughput_ops_per_second": 49.18,
        "read": WorkloadMetrics(1_000, 0, 2.0, 5.0, 10.0),
        "write": WorkloadMetrics(1_000, 0, 3.0, 8.0, 20.0),
        "sync": WorkloadMetrics(1_000, 0, 3.0, 9.0, 22.0),
        "error_rate": 0.0,
        "rss_peak_bytes": 512 * 1024**2,
        "queue_depth_end": 0,
        "outbox_max_age_seconds": 0.0,
        "rpo_lost_events": 0,
        "rto_seconds": 1.5,
        "data_digest_before": "1" * 64,
        "data_digest_after": "1" * 64,
        "chaos": {
            "database": "passed",
            "network": "passed",
            "disk": "passed",
            "term": "passed",
            "kill": "passed",
            "duplicate_delivery": "passed",
            "redis": "not_applicable",
        },
        "redis_reason": "redis_not_in_supported_topology",
        "key_id": "g009-key",
        "signing_key": b"c" * 32,
    }
    values.update(overrides)
    return CapacityChaosEvidence.create(**values)


def test_capacity_chaos_evidence_accepts_only_full_industrial_gate() -> None:
    report = _report()
    report.verify(b"c" * 32, expected_version="3.3.0")

    assert report.industrial_gate_closing is True
    assert report.contract_sha256 == capacity_chaos_contract_sha256()


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("function_count", 99_999),
        ("edge_count", 999_999),
        ("soak_seconds", 59.999),
        ("rss_peak_bytes", 2 * 1024**3 + 1),
        ("queue_depth_end", 1),
        ("outbox_max_age_seconds", 30.001),
        ("rpo_lost_events", 1),
        ("rto_seconds", 30.001),
        ("data_digest_after", "2" * 64),
    ),
)
def test_capacity_chaos_gate_rejects_any_failed_threshold(
    field: str, value: object
) -> None:
    overrides = {field: value}
    if field == "soak_seconds":
        overrides["throughput_ops_per_second"] = round(3_000 / float(value), 6)
    assert _report(**overrides).industrial_gate_closing is False


@pytest.mark.parametrize("workload", ("read", "write", "sync"))
def test_capacity_chaos_gate_requires_samples_and_latency_slo(workload: str) -> None:
    limit = 250.0 if workload == "read" else 500.0
    assert _report(
        **{
            workload: WorkloadMetrics(0, 0, 0.0, 0.0, 0.0),
            "operations_count": 2_000,
            "throughput_ops_per_second": round(2_000 / 61.0, 6),
        }
    ).industrial_gate_closing is False
    assert _report(
        **{
            workload: WorkloadMetrics(100, 0, 1.0, 2.0, limit + 0.001),
            "operations_count": 2_100,
            "throughput_ops_per_second": round(2_100 / 61.0, 6),
        }
    ).industrial_gate_closing is False


def test_capacity_chaos_gate_requires_minimum_operations_and_error_rate() -> None:
    low = WorkloadMetrics(333, 0, 1.0, 2.0, 3.0)
    assert _report(
        read=low,
        write=low,
        sync=low,
        operations_count=999,
        throughput_ops_per_second=round(999 / 61.0, 6),
    ).industrial_gate_closing is False
    failed = WorkloadMetrics(1_000, 4, 1.0, 2.0, 3.0)
    assert _report(
        read=failed,
        operations_count=3_000,
        error_rate=4 / 3_000,
    ).industrial_gate_closing is False


@pytest.mark.parametrize(
    "scenario",
    ("database", "network", "disk", "term", "kill", "duplicate_delivery"),
)
def test_capacity_chaos_gate_requires_every_real_chaos_scenario(scenario: str) -> None:
    chaos = dict(_report().chaos)
    chaos[scenario] = "failed"
    assert _report(chaos=chaos).industrial_gate_closing is False


def test_capacity_chaos_redis_boundary_is_exact() -> None:
    assert _report(redis_reason="redis_test_skipped").industrial_gate_closing is False
    chaos = dict(_report().chaos)
    chaos["redis"] = "passed"
    assert _report(chaos=chaos).industrial_gate_closing is False


def test_capacity_chaos_tamper_and_freshness_fail_closed(tmp_path: Path) -> None:
    report = _report()
    raw = report.to_dict()
    raw["edge_count"] = 1_000_001
    tampered = CapacityChaosEvidence.from_dict(raw)
    with pytest.raises(CapacityChaosEvidenceError, match="capacity_chaos_evidence_invalid"):
        tampered.verify(b"c" * 32, expected_version="3.3.0")

    now = datetime.now(UTC)
    stale_end = now - timedelta(hours=25, seconds=1)
    cases = (
        {
            "generated_at": (now - timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "window_started_at": (stale_end - timedelta(seconds=61)).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "window_ended_at": stale_end.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        },
        {"generated_at": _timestamp(timedelta(minutes=6))},
    )
    for values in cases:
        stale = _report(**values)
        with pytest.raises(CapacityChaosEvidenceError, match="capacity_chaos_freshness_invalid"):
            stale.verify(b"c" * 32, expected_version="3.3.0")

    path = tmp_path / "capacity.json"
    write_capacity_chaos_evidence(path, report)
    loaded = read_capacity_chaos_evidence(path)
    loaded.verify(b"c" * 32, expected_version="3.3.0")
    assert json.loads(path.read_bytes())["report_id"] == report.report_id


def test_capacity_chaos_writer_rejects_symlink_ancestor(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "linked"
    link.symlink_to(real, target_is_directory=True)

    with pytest.raises(CapacityChaosEvidenceError, match="capacity_chaos_evidence_invalid"):
        write_capacity_chaos_evidence(link / "report.json", _report())
    assert list(real.iterdir()) == []


def test_capacity_chaos_offline_verifier_is_redacted(tmp_path: Path) -> None:
    report = tmp_path / "capacity-secret-path.json"
    write_capacity_chaos_evidence(report, _report())
    root = Path(__file__).resolve().parent.parent
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(root)
    environment["MEMPLEX_CAPACITY_CHAOS_HMAC_KEY"] = "63" * 32
    result = subprocess.run(
        [
            sys.executable,
            "scripts/verify_g009_capacity_chaos_evidence.py",
            "--report",
            str(report),
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0
    assert json.loads(result.stdout)["verified"] is True
    assert str(tmp_path) not in result.stdout + result.stderr
    assert environment["MEMPLEX_CAPACITY_CHAOS_HMAC_KEY"] not in result.stdout + result.stderr

    raw = json.loads(report.read_bytes())
    raw["signature"] = "0" * 64
    report.write_text(json.dumps(raw), encoding="utf-8")
    rejected = subprocess.run(
        [
            sys.executable,
            "scripts/verify_g009_capacity_chaos_evidence.py",
            "--report",
            str(report),
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert rejected.returncode == 1
    assert rejected.stdout.strip() == (
        '{"error":"capacity_chaos_evidence_invalid","verified":false}'
    )
    assert str(tmp_path) not in rejected.stdout + rejected.stderr
