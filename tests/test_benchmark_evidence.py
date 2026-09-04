"""Contract tests for the G003 aggregate benchmark evidence bundle."""

from __future__ import annotations

import hashlib
import json
import platform
import re
import shutil
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from benchmarks.base import BenchmarkResult
from benchmarks.evidence import create_bundle, verify_bundle

PROJECT_ROOT = Path(__file__).resolve().parent.parent
G003_DIMENSIONS = (
    "retrieval",
    "temporal_multihop",
    "acl",
    "sync",
    "latency_capacity",
    "recovery",
    "host_integration",
)


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


def _write_dataset(path: Path, ids: list[str]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([{"id": sample_id} for sample_id in ids]))
    return path


def _dataset(path: Path, *, synthetic: bool = False) -> dict[str, object]:
    return {
        "path": str(path),
        "source_kind": "generated" if synthetic else "downloaded",
        "synthetic": synthetic,
    }


def _coverage() -> dict[str, dict[str, str]]:
    return {
        dimension: {"status": "passed", "reason": "aggregate metric recorded"}
        for dimension in G003_DIMENSIONS
    }


def _results() -> list[BenchmarkResult]:
    return [
        BenchmarkResult(
            name="g003",
            dataset="fixture",
            metric="recall@5",
            value=0.75,
            latency_ms=12,
            samples=3,
            timestamp="2026-08-29T12:00:00Z",
        ),
        BenchmarkResult(
            name="g003",
            dataset="fixture",
            metric="mrr",
            value=0.5,
            latency_ms=9,
            samples=3,
            timestamp="2026-08-29T12:00:01Z",
        ),
    ]


def _create(
    run_dir: Path,
    dataset_path: Path,
    *,
    synthetic: bool = False,
    coverage: dict[str, dict[str, str]] | None = None,
    raw_status: str | None = None,
    raw_reason: str | None = "per-sample traces were not captured",
) -> dict[str, object]:
    return create_bundle(
        run_dir=run_dir,
        results=_results(),
        dataset_files=[_dataset(dataset_path, synthetic=synthetic)],
        config={"seed": 17, "warm": True, "top_k": 5},
        coverage=coverage or _coverage(),
        command=[
            sys.executable,
            "-m",
            "benchmarks.benchmark_cli",
            "--api-key",
            "benchmark-secret",
        ],
        raw_status=raw_status,
        raw_reason=raw_reason,
    )


def _load_manifest(run_dir: Path) -> dict[str, object]:
    return json.loads((run_dir / "manifest.json").read_bytes())


def _load_datasets(run_dir: Path) -> list[dict[str, object]]:
    return json.loads((run_dir / "datasets.json").read_bytes())


def _rewrite_checksums(run_dir: Path) -> None:
    lines = []
    for name in ("datasets.json", "manifest.json", "results.jsonl"):
        digest = hashlib.sha256((run_dir / name).read_bytes()).hexdigest()
        lines.append(f"{digest}  {name}\n")
    (run_dir / "checksums.sha256").write_text("".join(lines))


@pytest.fixture
def dataset_file(tmp_path: Path) -> Path:
    return _write_dataset(tmp_path / "dataset.json", ["sample-c", "sample-a", "sample-b"])


def test_create_bundle_finalizes_only_the_four_portable_contract_files(
    tmp_path: Path, dataset_file: Path
) -> None:
    run_dir = tmp_path / "run"

    _create(run_dir, dataset_file)

    assert {path.name for path in run_dir.iterdir()} == {
        "datasets.json",
        "manifest.json",
        "results.jsonl",
        "checksums.sha256",
    }


def test_create_bundle_refuses_to_overwrite_an_existing_run_directory(
    tmp_path: Path, dataset_file: Path
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    sentinel = run_dir / "owned-by-another-run"
    sentinel.write_text("preserve")

    with pytest.raises(FileExistsError):
        _create(run_dir, dataset_file)

    assert sentinel.read_text() == "preserve"


def test_create_bundle_leaves_no_partial_run_when_canonicalization_fails(
    tmp_path: Path, dataset_file: Path
) -> None:
    run_dir = tmp_path / "run"
    bad_result = _results()[0]
    bad_result.value = float("nan")

    with pytest.raises(ValueError):
        create_bundle(
            run_dir,
            [bad_result],
            [_dataset(dataset_file)],
            {"seed": 17, "warm": True, "top_k": 5},
            _coverage(),
            [sys.executable, "-m", "benchmarks.benchmark_cli"],
            None,
            "raw unavailable",
        )

    assert not run_dir.exists()


def test_manifest_records_source_environment_lock_and_reproduction_inputs(
    tmp_path: Path, dataset_file: Path
) -> None:
    run_dir = tmp_path / "run"

    _create(run_dir, dataset_file)
    manifest = _load_manifest(run_dir)

    assert manifest["schema_version"] == 1
    assert set(manifest["source"]) == {"commit", "branch", "dirty", "diff_digest"}
    assert re.fullmatch(r"[0-9a-f]{40}", manifest["source"]["commit"])
    assert re.fullmatch(r"[0-9a-f]{64}", manifest["source"]["diff_digest"])
    assert manifest["environment"] == {
        "os": platform.system(),
        "arch": platform.machine(),
        "python": platform.python_version(),
    }
    assert manifest["uv_lock_digest"] == hashlib.sha256(
        (PROJECT_ROOT / "uv.lock").read_bytes()
    ).hexdigest()
    assert manifest["config"] == {"seed": 17, "top_k": 5, "warm": True}


def test_manifest_records_explicit_synthetic_dataset_provenance(
    tmp_path: Path, dataset_file: Path
) -> None:
    run_dir = tmp_path / "run"

    _create(run_dir, dataset_file, synthetic=True)
    provenance = _load_manifest(run_dir)["datasets"][0]
    records = json.loads(dataset_file.read_bytes())

    assert provenance == {
        "digest": hashlib.sha256(_canonical(records)).hexdigest(),
        "name": "dataset.json",
        "path": "datasets.json",
        "sample_count": 3,
        "sample_ids_digest": hashlib.sha256(
            _canonical(["sample-a", "sample-b", "sample-c"])
        ).hexdigest(),
        "source_kind": "generated",
        "synthetic": True,
    }


def test_datasets_file_canonical_embeds_records_and_source_metadata(
    tmp_path: Path, dataset_file: Path
) -> None:
    run_dir = tmp_path / "run"

    _create(run_dir, dataset_file, synthetic=True)

    datasets_bytes = (run_dir / "datasets.json").read_bytes()
    assert datasets_bytes == _canonical(
        [
            {
                "name": "dataset.json",
                "records": [
                    {"id": "sample-c"},
                    {"id": "sample-a"},
                    {"id": "sample-b"},
                ],
                "source_kind": "generated",
                "synthetic": True,
            }
        ]
    ) + b"\n"


def test_create_bundle_rejects_the_same_dataset_identity_twice(
    tmp_path: Path, dataset_file: Path
) -> None:
    with pytest.raises(ValueError, match="dataset.*duplicate|duplicate.*dataset"):
        create_bundle(
            run_dir=tmp_path / "run",
            results=_results(),
            dataset_files=[_dataset(dataset_file), _dataset(dataset_file)],
            config={"seed": 17, "warm": True, "top_k": 5},
            coverage=_coverage(),
            command=[sys.executable, "-m", "benchmarks.benchmark_cli"],
            raw_status=None,
            raw_reason="per-sample traces were not captured",
        )


def test_create_bundle_rejects_distinct_datasets_with_the_same_name(
    tmp_path: Path,
) -> None:
    first = _write_dataset(tmp_path / "first" / "shared.json", ["sample-a"])
    second = _write_dataset(tmp_path / "second" / "shared.json", ["sample-b"])

    with pytest.raises(ValueError, match="dataset.*name|name.*duplicate"):
        create_bundle(
            run_dir=tmp_path / "run",
            results=_results(),
            dataset_files=[_dataset(first), _dataset(second)],
            config={"seed": 17, "warm": True, "top_k": 5},
            coverage=_coverage(),
            command=[sys.executable, "-m", "benchmarks.benchmark_cli"],
            raw_status=None,
            raw_reason="per-sample traces were not captured",
        )


def test_dataset_sample_id_digest_is_independent_of_json_record_order(tmp_path: Path) -> None:
    first = _write_dataset(tmp_path / "first.json", ["z", "a", "m"])
    second = _write_dataset(tmp_path / "second.json", ["m", "z", "a"])

    _create(tmp_path / "run-one", first)
    _create(tmp_path / "run-two", second)

    first_data = _load_manifest(tmp_path / "run-one")["datasets"][0]
    second_data = _load_manifest(tmp_path / "run-two")["datasets"][0]
    assert first_data["sample_count"] == second_data["sample_count"] == 3
    assert first_data["sample_ids_digest"] == second_data["sample_ids_digest"]


def test_dataset_sample_identity_accepts_each_stable_id_key(tmp_path: Path) -> None:
    dataset = tmp_path / "heterogeneous.json"
    dataset.write_text(
        json.dumps(
            [
                {"id": "sample-1", "text": "first"},
                {"question_id": "question-2", "question": "second"},
                {"conversation_id": "conversation-3", "messages": []},
            ]
        )
    )

    _create(tmp_path / "run", dataset)

    provenance = _load_manifest(tmp_path / "run")["datasets"][0]
    expected_ids = ["conversation-3", "question-2", "sample-1"]
    assert provenance["sample_count"] == 3
    assert provenance["sample_ids_digest"] == hashlib.sha256(
        _canonical(expected_ids)
    ).hexdigest()


def test_fallback_record_ids_are_canonical_and_independent_of_record_order(
    tmp_path: Path,
) -> None:
    records = [
        {"question": "Which city?", "answers": ["Paris", "Lyon"]},
        {"messages": [{"role": "user", "content": "Hello"}], "turn": 1},
    ]
    first = tmp_path / "fallback-first.json"
    second = tmp_path / "fallback-second.json"
    first.write_text(json.dumps(records))
    second.write_text(json.dumps(list(reversed(records))))

    _create(tmp_path / "run-one", first)
    _create(tmp_path / "run-two", second)

    first_provenance = _load_manifest(tmp_path / "run-one")["datasets"][0]
    second_provenance = _load_manifest(tmp_path / "run-two")["datasets"][0]
    fallback_ids = sorted(hashlib.sha256(_canonical(record)).hexdigest() for record in records)
    expected_digest = hashlib.sha256(_canonical(fallback_ids)).hexdigest()
    assert first_provenance["sample_ids_digest"] == expected_digest
    assert second_provenance["sample_ids_digest"] == expected_digest


def test_create_bundle_rejects_duplicate_dataset_sample_ids(tmp_path: Path) -> None:
    dataset = _write_dataset(tmp_path / "duplicate.json", ["sample-a", "sample-a"])

    with pytest.raises(ValueError, match="duplicate|unique"):
        _create(tmp_path / "run", dataset)


def test_create_bundle_rejects_duplicate_explicit_ids_across_stable_keys(
    tmp_path: Path,
) -> None:
    dataset = tmp_path / "duplicate-explicit.json"
    dataset.write_text(
        json.dumps([{"question_id": "duplicate"}, {"conversation_id": "duplicate"}])
    )

    with pytest.raises(ValueError, match="duplicate|unique"):
        _create(tmp_path / "run", dataset)


def test_create_bundle_rejects_duplicate_identical_fallback_records(
    tmp_path: Path,
) -> None:
    record = {"question": "same", "answers": ["same answer"]}
    dataset = tmp_path / "duplicate-fallback.json"
    dataset.write_text(json.dumps([record, record]))

    with pytest.raises(ValueError, match="duplicate|unique"):
        _create(tmp_path / "run", dataset)


def test_create_bundle_rejects_empty_results(
    tmp_path: Path, dataset_file: Path
) -> None:
    with pytest.raises(ValueError, match="result"):
        create_bundle(
            run_dir=tmp_path / "run",
            results=[],
            dataset_files=[_dataset(dataset_file)],
            config={"seed": 17, "warm": True, "top_k": 5},
            coverage=_coverage(),
            command=[sys.executable, "-m", "benchmarks.benchmark_cli"],
            raw_status=None,
            raw_reason="per-sample traces were not captured",
        )


@pytest.mark.parametrize("invalid_value", [-0.01, 1.01, float("inf"), float("nan")])
def test_create_bundle_rejects_out_of_range_normalized_metric(
    tmp_path: Path, dataset_file: Path, invalid_value: float
) -> None:
    result = _results()[0]
    result.metric = "hop_precision@1"
    result.value = invalid_value

    with pytest.raises(ValueError, match="normalized metric"):
        create_bundle(
            run_dir=tmp_path / "run",
            results=[result],
            dataset_files=[_dataset(dataset_file)],
            config={"top_k": 1},
            coverage=_coverage(),
            command=["benchmark"],
            raw_status=None,
            raw_reason="per-sample traces were not captured",
        )


def test_create_bundle_accepts_unbounded_metric_above_one(
    tmp_path: Path, dataset_file: Path
) -> None:
    result = _results()[0]
    result.metric = "latency_p95_ms"
    result.value = 37.5
    run_dir = tmp_path / "run"

    create_bundle(
        run_dir=run_dir,
        results=[result],
        dataset_files=[_dataset(dataset_file)],
        config={"top_k": 1},
        coverage=_coverage(),
        command=["benchmark"],
        raw_status=None,
        raw_reason="per-sample traces were not captured",
    )

    assert verify_bundle(run_dir)["evidence_level"] == "E1"


def test_manifest_has_exact_g003_coverage_dimensions_with_status_and_reason(
    tmp_path: Path, dataset_file: Path
) -> None:
    run_dir = tmp_path / "run"
    coverage = _coverage()
    coverage["temporal_multihop"] = {
        "status": "failed",
        "reason": "bounded one-hop retrieval is not general multi-hop reasoning",
    }

    _create(run_dir, dataset_file, coverage=coverage)
    recorded = _load_manifest(run_dir)["coverage"]

    assert tuple(recorded) == tuple(sorted(G003_DIMENSIONS))
    assert all(set(item) == {"reason", "status"} for item in recorded.values())
    assert recorded["temporal_multihop"]["status"] == "failed"


def test_create_bundle_rejects_a_missing_g003_coverage_dimension(
    tmp_path: Path, dataset_file: Path
) -> None:
    coverage = _coverage()
    coverage.pop("recovery")

    with pytest.raises(ValueError, match="coverage"):
        _create(tmp_path / "run", dataset_file, coverage=coverage)


def test_create_bundle_rejects_an_extra_g003_coverage_dimension(
    tmp_path: Path, dataset_file: Path
) -> None:
    coverage = _coverage()
    coverage["cost"] = {"status": "passed", "reason": "not a G003 dimension"}

    with pytest.raises(ValueError, match="coverage"):
        _create(tmp_path / "run", dataset_file, coverage=coverage)


@pytest.mark.parametrize(
    "status", ["passed", "failed", "unavailable", "not_measured"]
)
def test_create_bundle_accepts_each_defined_coverage_status(
    tmp_path: Path, dataset_file: Path, status: str
) -> None:
    coverage = _coverage()
    coverage["recovery"] = {"status": status, "reason": "explicit coverage outcome"}

    _create(tmp_path / "run", dataset_file, coverage=coverage)


def test_create_bundle_rejects_coverage_status_outside_the_defined_set(
    tmp_path: Path, dataset_file: Path
) -> None:
    coverage = _coverage()
    coverage["recovery"] = {"status": "skipped", "reason": "ambiguous outcome"}

    with pytest.raises(ValueError, match="coverage status"):
        _create(tmp_path / "run", dataset_file, coverage=coverage)


def test_raw_unavailable_is_null_with_a_reason_and_never_a_numeric_zero(
    tmp_path: Path, dataset_file: Path
) -> None:
    run_dir = tmp_path / "run"

    _create(run_dir, dataset_file)
    raw = _load_manifest(run_dir)["raw"]

    assert raw == {"reason": "per-sample traces were not captured", "status": None}
    assert 0 not in raw.values()


def test_create_bundle_rejects_complete_raw_status_for_aggregate_only_bundle(
    tmp_path: Path, dataset_file: Path
) -> None:
    with pytest.raises(ValueError, match="raw|aggregate"):
        _create(
            tmp_path / "run",
            dataset_file,
            raw_status="complete",
            raw_reason=None,
        )


@pytest.mark.parametrize("raw_reason", [None, "", "   "])
def test_create_bundle_requires_nonempty_reason_for_missing_raw_evidence(
    tmp_path: Path, dataset_file: Path, raw_reason: str | None
) -> None:
    with pytest.raises(ValueError, match="reason"):
        _create(tmp_path / "run", dataset_file, raw_reason=raw_reason)


def test_manifest_redacts_command_secrets_and_records_cwd_and_timestamps(
    tmp_path: Path, dataset_file: Path
) -> None:
    run_dir = tmp_path / "run"

    _create(run_dir, dataset_file)
    manifest = _load_manifest(run_dir)
    serialized = _canonical(manifest)

    assert b"benchmark-secret" not in serialized
    assert manifest["command"][-1] == "<redacted>"
    assert manifest["cwd"] == str(PROJECT_ROOT)
    assert set(manifest["timestamps"]) == {"created_at", "finalized_at"}
    assert all(value.endswith("Z") for value in manifest["timestamps"].values())


def test_manifest_reports_failed_and_unavailable_reasons_as_limitations(
    tmp_path: Path, dataset_file: Path
) -> None:
    coverage = _coverage()
    coverage["host_integration"] = {
        "status": "unavailable",
        "reason": "four real hosts were not available",
    }
    run_dir = tmp_path / "run"

    _create(run_dir, dataset_file, coverage=coverage)
    limitations = _load_manifest(run_dir)["limitations"]

    assert "four real hosts were not available" in limitations
    assert "per-sample traces were not captured" in limitations


def test_aggregate_only_bundle_has_e1_evidence_level(
    tmp_path: Path, dataset_file: Path
) -> None:
    run_dir = tmp_path / "run"

    _create(run_dir, dataset_file)

    assert _load_manifest(run_dir)["evidence_level"] == "E1"


def test_synthetic_aggregate_only_bundle_has_e1_evidence_level(
    tmp_path: Path, dataset_file: Path
) -> None:
    run_dir = tmp_path / "run"

    _create(run_dir, dataset_file, synthetic=True)

    assert _load_manifest(run_dir)["evidence_level"] == "E1"


def test_clean_source_cannot_raise_aggregate_only_bundle_above_e1(
    tmp_path: Path, dataset_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "benchmarks.evidence._source_provenance",
        lambda: {
            "commit": "a" * 40,
            "branch": "main",
            "dirty": False,
            "diff_digest": "b" * 64,
        },
    )
    run_dir = tmp_path / "run"

    _create(run_dir, dataset_file)

    assert _load_manifest(run_dir)["evidence_level"] == "E1"


def test_dirty_source_cannot_raise_aggregate_only_bundle_above_e1(
    tmp_path: Path, dataset_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "benchmarks.evidence._source_provenance",
        lambda: {
            "commit": "a" * 40,
            "branch": "main",
            "dirty": True,
            "diff_digest": "b" * 64,
        },
    )
    run_dir = tmp_path / "run"

    _create(run_dir, dataset_file)

    assert _load_manifest(run_dir)["evidence_level"] == "E1"


def test_two_logically_identical_bundles_have_stable_non_timing_fields(
    tmp_path: Path, dataset_file: Path
) -> None:
    _create(tmp_path / "first", dataset_file, synthetic=True)
    _create(tmp_path / "second", dataset_file, synthetic=True)
    first = _load_manifest(tmp_path / "first")
    second = _load_manifest(tmp_path / "second")

    for field in (
        "schema_version",
        "source",
        "environment",
        "uv_lock_digest",
        "datasets",
        "config",
        "command",
        "cwd",
        "coverage",
        "limitations",
        "raw",
        "evidence_level",
    ):
        assert first[field] == second[field]


def test_manifest_and_result_lines_are_canonical_sorted_json(
    tmp_path: Path, dataset_file: Path
) -> None:
    run_dir = tmp_path / "run"

    _create(run_dir, dataset_file)

    manifest_bytes = (run_dir / "manifest.json").read_bytes()
    assert manifest_bytes == _canonical(json.loads(manifest_bytes)) + b"\n"
    for line in (run_dir / "results.jsonl").read_bytes().splitlines(keepends=True):
        assert line == _canonical(json.loads(line)) + b"\n"


def test_checksums_cover_all_three_payload_files_in_canonical_name_order(
    tmp_path: Path, dataset_file: Path
) -> None:
    run_dir = tmp_path / "run"

    _create(run_dir, dataset_file)

    expected = "".join(
        f"{hashlib.sha256((run_dir / name).read_bytes()).hexdigest()}  {name}\n"
        for name in ("datasets.json", "manifest.json", "results.jsonl")
    )
    assert (run_dir / "checksums.sha256").read_text() == expected
    assert verify_bundle(run_dir) == _load_manifest(run_dir)


@pytest.mark.parametrize(
    "missing",
    ["datasets.json", "manifest.json", "results.jsonl", "checksums.sha256"],
)
def test_verifier_rejects_a_missing_contract_file(
    tmp_path: Path, dataset_file: Path, missing: str
) -> None:
    run_dir = tmp_path / "run"
    _create(run_dir, dataset_file)
    (run_dir / missing).unlink()

    with pytest.raises(ValueError):
        verify_bundle(run_dir)


def test_verifier_rejects_an_extra_file(tmp_path: Path, dataset_file: Path) -> None:
    run_dir = tmp_path / "run"
    _create(run_dir, dataset_file)
    (run_dir / "untracked.txt").write_text("not checksummed")

    with pytest.raises(ValueError):
        verify_bundle(run_dir)


def test_verifier_rejects_a_symlinked_contract_file(tmp_path: Path, dataset_file: Path) -> None:
    run_dir = tmp_path / "run"
    _create(run_dir, dataset_file)
    outside = tmp_path / "outside-results.jsonl"
    shutil.copy2(run_dir / "results.jsonl", outside)
    (run_dir / "results.jsonl").unlink()
    (run_dir / "results.jsonl").symlink_to(outside)

    with pytest.raises(ValueError):
        verify_bundle(run_dir)


def test_verifier_rejects_checksum_path_traversal(tmp_path: Path, dataset_file: Path) -> None:
    run_dir = tmp_path / "run"
    _create(run_dir, dataset_file)
    checksum = "0" * 64
    (run_dir / "checksums.sha256").write_text(f"{checksum}  ../outside\n")

    with pytest.raises(ValueError):
        verify_bundle(run_dir)


def test_verifier_rejects_a_duplicate_checksum_entry(
    tmp_path: Path, dataset_file: Path
) -> None:
    run_dir = tmp_path / "run"
    _create(run_dir, dataset_file)
    checksum_file = run_dir / "checksums.sha256"
    first_line = checksum_file.read_text().splitlines(keepends=True)[0]
    checksum_file.write_text(checksum_file.read_text() + first_line)

    with pytest.raises(ValueError):
        verify_bundle(run_dir)


def test_verifier_rejects_result_tampering(tmp_path: Path, dataset_file: Path) -> None:
    run_dir = tmp_path / "run"
    _create(run_dir, dataset_file)
    (run_dir / "results.jsonl").write_text('{"value":1}\n')

    with pytest.raises(ValueError):
        verify_bundle(run_dir)


def test_verifier_rejects_embedded_dataset_tampering_with_rewritten_checksums(
    tmp_path: Path, dataset_file: Path
) -> None:
    run_dir = tmp_path / "run"
    _create(run_dir, dataset_file)
    datasets = _load_datasets(run_dir)
    datasets[0]["records"][0]["id"] = "attacker-replaced-id"
    (run_dir / "datasets.json").write_bytes(_canonical(datasets) + b"\n")
    _rewrite_checksums(run_dir)

    with pytest.raises(ValueError, match="dataset|digest|sample"):
        verify_bundle(run_dir)


def test_verifier_rejects_out_of_range_normalized_metric_with_valid_checksums(
    tmp_path: Path, dataset_file: Path
) -> None:
    run_dir = tmp_path / "run"
    _create(run_dir, dataset_file)
    results = [json.loads(line) for line in (run_dir / "results.jsonl").read_text().splitlines()]
    results[0]["value"] = 1.01
    (run_dir / "results.jsonl").write_bytes(
        b"".join(_canonical(result) + b"\n" for result in results)
    )
    _rewrite_checksums(run_dir)

    with pytest.raises(ValueError, match="normalized metric"):
        verify_bundle(run_dir)


@pytest.mark.parametrize("forged_level", ["E2", "E3"])
def test_verifier_rejects_forged_non_e1_evidence_level_with_valid_checksums(
    tmp_path: Path, dataset_file: Path, forged_level: str
) -> None:
    run_dir = tmp_path / "run"
    _create(run_dir, dataset_file)
    manifest = _load_manifest(run_dir)
    manifest["evidence_level"] = forged_level
    (run_dir / "manifest.json").write_bytes(_canonical(manifest) + b"\n")
    _rewrite_checksums(run_dir)

    with pytest.raises(ValueError, match="evidence.level|evidence_level|level"):
        verify_bundle(run_dir)


@pytest.mark.parametrize("payload", [b"", b"{}\n", b"[]\n"])
def test_verifier_rejects_empty_or_incomplete_rows_with_valid_checksums(
    tmp_path: Path, dataset_file: Path, payload: bytes
) -> None:
    run_dir = tmp_path / "run"
    _create(run_dir, dataset_file)
    (run_dir / "results.jsonl").write_bytes(payload)
    _rewrite_checksums(run_dir)
    with pytest.raises(ValueError, match="result"):
        verify_bundle(run_dir)


@pytest.mark.parametrize(
    "field",
    ["benchmark", "dataset", "metric", "value", "latency_ms", "samples", "timestamp"],
)
def test_verifier_requires_every_result_field(
    tmp_path: Path, dataset_file: Path, field: str
) -> None:
    run_dir = tmp_path / "run"
    _create(run_dir, dataset_file)
    row = _results()[0].to_dict()
    del row[field]
    (run_dir / "results.jsonl").write_bytes(_canonical(row) + b"\n")
    _rewrite_checksums(run_dir)
    with pytest.raises(ValueError, match="result"):
        verify_bundle(run_dir)


@pytest.mark.parametrize("operation", ["create", "verify"])
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", ""), ("dataset", []), ("metric", "  "),
        ("value", "0.5"), ("value", True),
        ("latency_ms", -1), ("latency_ms", True), ("latency_ms", "12"),
        ("latency_p50_ms", -1.5), ("latency_p99_ms", "9"),
        ("samples", -1), ("samples", True), ("samples", "3"),
        ("timestamp", "not-a-time"), ("timestamp", None),
    ],
)
def test_create_and_verify_enforce_result_field_types(
    tmp_path: Path, dataset_file: Path, operation: str, field: str, value: object
) -> None:
    run_dir = tmp_path / "run"
    result = _results()[0]
    # Use an unbounded metric so the normalized-metric guard cannot mask schema gaps.
    result.metric = "latency_p95_ms"
    if operation == "verify":
        _create(run_dir, dataset_file)
    setattr(result, field, value)
    if operation == "create":
        with pytest.raises(ValueError):
            create_bundle(
                run_dir, [result], [_dataset(dataset_file)], {}, _coverage(),
                ["benchmark"], None, "no raw traces",
            )
        assert not run_dir.exists()
    else:
        (run_dir / "results.jsonl").write_bytes(_canonical(result.to_dict()) + b"\n")
        _rewrite_checksums(run_dir)
        with pytest.raises(ValueError):
            verify_bundle(run_dir)


def test_float_latency_and_percentile_fields_round_trip(
    tmp_path: Path, dataset_file: Path
) -> None:
    """Since the perf_counter migration, latency_ms is a float millisecond
    mean and latency_p50_ms/latency_p99_ms are optional extras; bundles with
    them must create and verify cleanly."""
    run_dir = tmp_path / "run"
    result = _results()[0]
    result.latency_ms = 12.375
    result.latency_p50_ms = 11.0
    result.latency_p99_ms = 40.5
    create_bundle(
        run_dir, [result], [_dataset(dataset_file)], {}, _coverage(),
        ["benchmark"], None, "no raw traces",
    )
    verify_bundle(run_dir)
    row = json.loads((run_dir / "results.jsonl").read_text().splitlines()[0])
    assert row["latency_ms"] == 12.375
    assert row["latency_p50_ms"] == 11.0
    assert row["latency_p99_ms"] == 40.5


@pytest.mark.parametrize(
    "field",
    [
        "schema_version", "source", "environment", "uv_lock_digest", "timestamps",
        "cwd", "command", "config", "datasets", "coverage", "limitations", "raw",
        "evidence_level",
    ],
)
def test_verifier_requires_every_manifest_field(
    tmp_path: Path, dataset_file: Path, field: str
) -> None:
    run_dir = tmp_path / "run"
    _create(run_dir, dataset_file)
    manifest = _load_manifest(run_dir)
    del manifest[field]
    (run_dir / "manifest.json").write_bytes(_canonical(manifest) + b"\n")
    _rewrite_checksums(run_dir)
    with pytest.raises(ValueError):
        verify_bundle(run_dir)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("schema_version",), True), (("schema_version",), 2),
        (("source",), {}), (("source", "commit"), "not-a-commit"),
        (("source", "branch"), None), (("source", "dirty"), "true"),
        (("source", "diff_digest"), "bad"),
        (("environment",), {}), (("environment", "os"), []),
        (("environment", "arch"), ""), (("environment", "python"), 3),
        (("uv_lock_digest",), "bad"), (("timestamps",), {}),
        (("timestamps", "created_at"), "not-a-time"),
        (("timestamps", "finalized_at"), None), (("cwd",), 1),
        (("command",), []), (("command",), [1]), (("config",), []),
        (("limitations",), "missing"), (("limitations",), [None]),
    ],
)
def test_verifier_rejects_malformed_manifest_provenance(
    tmp_path: Path, dataset_file: Path, path: tuple[str, ...], value: object
) -> None:
    run_dir = tmp_path / "run"
    _create(run_dir, dataset_file)
    manifest = _load_manifest(run_dir)
    target = manifest
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    (run_dir / "manifest.json").write_bytes(_canonical(manifest) + b"\n")
    _rewrite_checksums(run_dir)
    with pytest.raises(ValueError):
        verify_bundle(run_dir)


@pytest.mark.parametrize("missing", ["commit", "branch", "dirty", "diff_digest"])
def test_create_rejects_incomplete_source_provenance(
    tmp_path: Path, dataset_file: Path, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    source = {"commit": "a" * 40, "branch": "main", "dirty": True, "diff_digest": "b" * 64}
    del source[missing]
    monkeypatch.setattr("benchmarks.evidence._source_provenance", lambda: deepcopy(source))
    run_dir = tmp_path / "run"
    with pytest.raises(ValueError, match="source"):
        _create(run_dir, dataset_file)
    assert not run_dir.exists()


SECRET_STRINGS = [
    "https://example.invalid/?api_key=FAKE",
    "https://example.invalid/#access_token=FAKE",
    "https://example.invalid/?safe=1#token=FAKE",
    "https://example.invalid/?api%5fkey=FAKE",
    "https://example.invalid/#FAKE",
    "https://example.invalid/?api_key=FAKE REVIEW PASSWORD",
    "https://example.invalid/?api_key=FAKE\tREVIEW\tPASSWORD",
    "https://example.invalid/?api_key=FAKE\nREVIEW\nPASSWORD",
    "https://example.invalid/?api_key=FAKE\rREVIEW\rPASSWORD",
    "https://example.invalid/#access_token=FAKE REVIEW PASSWORD",
    "https://example.invalid/#access_token=FAKE\tREVIEW\tPASSWORD",
    "https://example.invalid/#access_token=FAKE\nREVIEW\nPASSWORD",
    "https://example.invalid/#access_token=FAKE\rREVIEW\rPASSWORD",
    "https://example.invalid/?<redacted> REVIEW PASSWORD",
    "https://example.invalid/?<redacted>\tREVIEW\nPASSWORD",
    "https://user:FAKE@example.invalid/path",
    "postgresql://review:FAKE REVIEW PASSWORD@localhost/example",
    "postgresql://review:FAKE\tREVIEW\tPASSWORD@localhost/example",
    "postgresql://review:FAKE\nREVIEW\nPASSWORD@localhost/example",
    "postgresql://review:FAKE\rREVIEW\rPASSWORD@localhost/example",
    "postgresql://FAKE REVIEW USER:password@localhost/example",
    "postgresql://review:%46%41%4b%45%20REVIEW%20PASSWORD@localhost/example",
    "postgresql://review:FAKE?REVIEW PASSWORD@localhost/example",
    "postgresql://review:FAKE#REVIEW PASSWORD@localhost/example",
    "postgresql://review:password@FAKE@localhost/example",
    "postgresql://review:FAKE/REVIEW@localhost/example",
    "postgresql://review:FAKE/REVIEW@extra@localhost/example",
    "https://review:FAKE/REVIEW?part#fragment@example.invalid/",
    "endpoint=https://example.invalid/?api_key=FAKE",
    "host=localhost dbname=benchmark user=alice password=FAKE",
    "host=localhost password = 'FAKE with spaces' dbname=benchmark",
    r"host=localhost password='FAKE\'escaped' user=alice",
    "host=localhost sslpassword=FAKE",
]


@pytest.mark.parametrize("secret", SECRET_STRINGS)
@pytest.mark.parametrize("surface", ["config", "command"])
def test_create_redacts_url_and_libpq_credentials(
    tmp_path: Path, dataset_file: Path, secret: str, surface: str
) -> None:
    run_dir = tmp_path / "run"
    manifest = create_bundle(
        run_dir, _results(), [_dataset(dataset_file)],
        {"nested": [{"endpoint": secret, "storage_path": secret}]} if surface == "config" else {},
        _coverage(), ["benchmark", "--url", secret, f"--endpoint={secret}"]
        if surface == "command" else ["benchmark"],
        None, "no raw traces",
    )
    assert "FAKE" not in (run_dir / "manifest.json").read_text()
    assert "%46%41%4b%45" not in (run_dir / "manifest.json").read_text()
    assert manifest[surface] == (
        {"nested": [{"endpoint": "<redacted>", "storage_path": "<redacted>"}]}
        if surface == "config" else ["benchmark", "--url", "<redacted>", "<redacted>"]
    )
    assert verify_bundle(run_dir) == manifest


@pytest.mark.parametrize("secret", SECRET_STRINGS)
@pytest.mark.parametrize("surface", ["config", "command", "command_inline"])
def test_verify_rejects_unredacted_url_and_libpq_credentials(
    tmp_path: Path, dataset_file: Path, secret: str, surface: str
) -> None:
    run_dir = tmp_path / "run"
    _create(run_dir, dataset_file)
    manifest = _load_manifest(run_dir)
    if surface == "config":
        manifest["config"] = {"storage_path": secret}
    else:
        manifest["command"] = (
            ["benchmark", "--url", secret] if surface == "command"
            else ["benchmark", f"--endpoint={secret}"]
        )
    (run_dir / "manifest.json").write_bytes(_canonical(manifest) + b"\n")
    _rewrite_checksums(run_dir)
    with pytest.raises(ValueError, match="unredacted"):
        verify_bundle(run_dir)


@pytest.mark.parametrize("surface", ["config", "command"])
def test_create_redacts_entire_scalar_for_ambiguous_uri_userinfo(
    tmp_path: Path, dataset_file: Path, surface: str
) -> None:
    secret = "endpoint=postgresql://review:FAKE@REVIEW PASSWORD@localhost/example"
    run_dir = tmp_path / "run"
    manifest = create_bundle(
        run_dir, _results(), [_dataset(dataset_file)],
        {"endpoint": secret} if surface == "config" else {},
        _coverage(), ["benchmark", secret] if surface == "command" else ["benchmark"],
        None, "no raw traces",
    )

    assert manifest[surface] == (
        {"endpoint": "<redacted>"} if surface == "config" else ["benchmark", "<redacted>"]
    )
    assert verify_bundle(run_dir) == manifest


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://example.invalid/api/v1",
        "postgresql://localhost:5432/example",
        "postgresql:///example",
        "endpoint=https://example.invalid/api/v1",
    ],
)
def test_create_preserves_credential_free_endpoints(
    tmp_path: Path, dataset_file: Path, endpoint: str
) -> None:
    run_dir = tmp_path / "run"
    config = {"nested": [{"endpoint": endpoint}]}
    command = ["benchmark", "--url", endpoint, f"--endpoint={endpoint}"]
    manifest = create_bundle(
        run_dir, _results(), [_dataset(dataset_file)], config, _coverage(), command,
        None, "no raw traces",
    )

    assert manifest["config"] == config
    assert manifest["command"] == command
    assert verify_bundle(run_dir) == manifest


@pytest.mark.parametrize("synthetic", [False, True])
@pytest.mark.parametrize("operation", ["create", "verify"])
@pytest.mark.parametrize(
    "raw",
    [
        {"status": "complete", "reason": None},
        {"status": 0, "reason": "no raw traces"},
        {"status": False, "reason": "no raw traces"},
        {"status": None, "reason": None},
        {"status": None, "reason": " "},
    ],
)
def test_aggregate_raw_contract_has_no_synthetic_exemption(
    tmp_path: Path, dataset_file: Path, synthetic: bool, operation: str, raw: dict
) -> None:
    run_dir = tmp_path / "run"
    if operation == "create":
        with pytest.raises(ValueError, match="raw|reason"):
            _create(run_dir, dataset_file, synthetic=synthetic,
                    raw_status=raw["status"], raw_reason=raw["reason"])
        assert not run_dir.exists()
    else:
        _create(run_dir, dataset_file, synthetic=synthetic)
        manifest = _load_manifest(run_dir)
        manifest["raw"] = raw
        (run_dir / "manifest.json").write_bytes(_canonical(manifest) + b"\n")
        _rewrite_checksums(run_dir)
        with pytest.raises(ValueError, match="raw|reason"):
            verify_bundle(run_dir)


@pytest.mark.parametrize(("field", "value"), [("synthetic", 0), ("sample_count", 3.0)])
def test_verify_rejects_equal_but_wrong_typed_dataset_provenance(
    tmp_path: Path, dataset_file: Path, field: str, value: object
) -> None:
    run_dir = tmp_path / "run"
    _create(run_dir, dataset_file)
    manifest = _load_manifest(run_dir)
    manifest["datasets"][0][field] = value
    (run_dir / "manifest.json").write_bytes(_canonical(manifest) + b"\n")
    _rewrite_checksums(run_dir)
    with pytest.raises(ValueError, match="dataset"):
        verify_bundle(run_dir)


def test_result_timestamp_preserves_existing_naive_iso_format(
    tmp_path: Path, dataset_file: Path
) -> None:
    result = _results()[0]
    result.timestamp = "2026-08-29T12:00:00.123456"
    run_dir = tmp_path / "run"
    create_bundle(run_dir, [result], [_dataset(dataset_file)], {}, _coverage(),
                  ["benchmark"], None, "no raw traces")
    assert verify_bundle(run_dir)["evidence_level"] == "E1"
    assert json.loads((run_dir / "results.jsonl").read_text())["timestamp"] == result.timestamp
