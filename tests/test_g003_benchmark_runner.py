"""TDD contracts for the reproducible G003 benchmark runner."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import benchmarks
from benchmarks.base import BenchmarkResult
from scripts import run_g003_benchmark as runner


CONCRETE_DATASETS = (
    "hotpotqa",
    "locomo",
    "longmemeval",
    "memory_benchmark",
    "nq",
    "popqa",
    "triviaqa",
)
FILE_DATASETS = tuple(name for name in CONCRETE_DATASETS if name != "memory_benchmark")
G003_DIMENSIONS = (
    "retrieval",
    "temporal_multihop",
    "acl",
    "sync",
    "latency_capacity",
    "recovery",
    "host_integration",
)


def test_importing_benchmarks_registers_longmemeval_dataset_and_runner() -> None:
    assert "longmemeval" in benchmarks.BenchmarkRunnerFactory.available_datasets()
    assert "longmemeval" in benchmarks.BenchmarkRunnerFactory.available_runners()


def _result(dataset: str, metric: str = "recall@5") -> BenchmarkResult:
    return BenchmarkResult(
        name=f"{dataset}_retrieval",
        dataset=dataset,
        metric=metric,
        value=0.75,
        latency_ms=12,
        samples=3,
        timestamp="2026-08-29T12:00:00Z",
    )


def _install_run_doubles(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> dict[str, Any]:
    captured: dict[str, Any] = {
        "downloads": [],
        "benchmark_calls": [],
        "bundle_calls": [],
    }

    def fake_download_dataset(
        dataset_name: str,
        output_dir: str,
        *,
        force_synthetic: bool,
        **_: object,
    ) -> Path:
        destination = Path(output_dir) / f"{dataset_name}.json"
        assert not destination.exists(), "synthetic input must be freshly generated"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps([{"id": f"{dataset_name}-sample"}]))
        captured["downloads"].append(
            {
                "dataset": dataset_name,
                "output_dir": Path(output_dir),
                "force_synthetic": force_synthetic,
            }
        )
        return destination

    def fake_run_benchmark_command(**kwargs: object) -> dict[str, list[BenchmarkResult]]:
        captured["benchmark_calls"].append(kwargs)
        dataset = str(kwargs["dataset"])
        return {dataset: [_result(dataset), _result(dataset, "mrr")]}

    def fake_create_bundle(**kwargs: object) -> dict[str, object]:
        captured["bundle_calls"].append(kwargs)
        return {"evidence_level": "E1"}

    monkeypatch.setattr(runner, "download_dataset", fake_download_dataset)
    monkeypatch.setattr(runner, "run_benchmark_command", fake_run_benchmark_command)
    monkeypatch.setattr(runner, "create_bundle", fake_create_bundle)
    monkeypatch.chdir(tmp_path)
    return captured


def test_run_requires_explicit_synthetic_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        runner,
        "run_benchmark_command",
        lambda **_: pytest.fail("a real benchmark must not be used as a fallback"),
    )

    with pytest.raises(SystemExit) as raised:
        runner.main(
            ["run", "--dataset", "locomo", "--run-dir", str(tmp_path / "run")]
        )

    assert raised.value.code != 0


def test_run_forwards_single_dataset_top_k_seed_and_run_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _install_run_doubles(monkeypatch, tmp_path)
    run_dir = tmp_path / "g003-run"

    assert (
        runner.main(
            [
                "run",
                "--synthetic",
                "--dataset",
                "locomo",
                "--top-k",
                "7",
                "--seed",
                "23",
                "--run-dir",
                str(run_dir),
            ]
        )
        == 0
    )

    benchmark_call = captured["benchmark_calls"][0]
    bundle_call = captured["bundle_calls"][0]
    assert benchmark_call["dataset"] == "locomo"
    assert benchmark_call["retrieval_k"] == 7
    assert Path(str(benchmark_call["path"])).is_file()
    assert Path(str(benchmark_call["output"])).parent != run_dir
    assert bundle_call["run_dir"] == run_dir
    assert bundle_call["config"]["seed"] == 23
    assert bundle_call["config"]["top_k"] == 7


def test_all_resolves_every_concrete_dataset_without_composite_duplicates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _install_run_doubles(monkeypatch, tmp_path)

    assert (
        runner.main(
            [
                "run",
                "--synthetic",
                "--dataset",
                "all",
                "--run-dir",
                str(tmp_path / "run"),
            ]
        )
        == 0
    )

    selected = tuple(call["dataset"] for call in captured["benchmark_calls"])
    assert selected == CONCRETE_DATASETS


def test_synthetic_run_freshly_generates_each_file_backed_dataset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _install_run_doubles(monkeypatch, tmp_path)

    runner.main(
        [
            "run",
            "--synthetic",
            "--dataset",
            "all",
            "--run-dir",
            str(tmp_path / "run"),
        ]
    )

    assert tuple(call["dataset"] for call in captured["downloads"]) == FILE_DATASETS
    assert all(call["force_synthetic"] is True for call in captured["downloads"])
    assert len({call["output_dir"] for call in captured["downloads"]}) == 1


def test_each_benchmark_uses_an_explicit_input_and_temporary_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _install_run_doubles(monkeypatch, tmp_path)
    run_dir = tmp_path / "run"

    runner.main(
        ["run", "--synthetic", "--dataset", "all", "--run-dir", str(run_dir)]
    )

    for call in captured["benchmark_calls"]:
        if call["dataset"] == "memory_benchmark":
            assert call["path"] == ""
        else:
            assert Path(str(call["path"])).is_file()
        assert call["auto_download"] is False
        assert call["force_synthetic"] is True
        assert Path(str(call["output"])).parent != run_dir


@pytest.mark.parametrize(
    ("dataset", "expected_warm"),
    tuple((name, name != "longmemeval") for name in CONCRETE_DATASETS),
)
def test_longmemeval_runs_cold_while_every_other_dataset_runs_warm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dataset: str,
    expected_warm: bool,
) -> None:
    captured = _install_run_doubles(monkeypatch, tmp_path)

    runner.main(
        [
            "run",
            "--synthetic",
            "--dataset",
            dataset,
            "--run-dir",
            str(tmp_path / "run"),
        ]
    )

    assert captured["benchmark_calls"][0]["warm"] is expected_warm


def test_run_flattens_benchmark_results_before_bundle_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _install_run_doubles(monkeypatch, tmp_path)

    runner.main(
        [
            "run",
            "--synthetic",
            "--dataset",
            "longmemeval",
            "--run-dir",
            str(tmp_path / "run"),
        ]
    )

    results = captured["bundle_calls"][0]["results"]
    assert [result.metric for result in results] == ["recall@5", "mrr"]
    assert all(isinstance(result, BenchmarkResult) for result in results)


def test_all_fails_without_bundling_when_any_selected_dataset_has_no_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _install_run_doubles(monkeypatch, tmp_path)

    def benchmark_with_one_empty_dataset(
        **kwargs: object,
    ) -> dict[str, list[BenchmarkResult]]:
        dataset = str(kwargs["dataset"])
        if dataset == "longmemeval":
            return {dataset: []}
        return {dataset: [_result(dataset)]}

    monkeypatch.setattr(
        runner, "run_benchmark_command", benchmark_with_one_empty_dataset
    )

    with pytest.raises(ValueError, match="result|longmemeval|empty"):
        runner.main(
            [
                "run",
                "--synthetic",
                "--dataset",
                "all",
                "--run-dir",
                str(tmp_path / "run"),
            ]
        )

    assert captured["bundle_calls"] == []


@pytest.mark.parametrize(
    ("dataset", "temporal_status"),
    (("locomo", "not_measured"), ("hotpotqa", "passed"), ("longmemeval", "passed")),
)
def test_run_records_exact_conservative_coverage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    dataset: str,
    temporal_status: str,
) -> None:
    captured = _install_run_doubles(monkeypatch, tmp_path)

    runner.main(
        [
            "run",
            "--synthetic",
            "--dataset",
            dataset,
            "--run-dir",
            str(tmp_path / "run"),
        ]
    )

    coverage = captured["bundle_calls"][0]["coverage"]
    assert tuple(coverage) == G003_DIMENSIONS
    assert {name: item["status"] for name, item in coverage.items()} == {
        "retrieval": "passed",
        "temporal_multihop": temporal_status,
        "acl": "not_measured",
        "sync": "not_measured",
        "latency_capacity": "not_measured",
        "recovery": "not_measured",
        "host_integration": "not_measured",
    }
    assert all(item["reason"].strip() for item in coverage.values())


def test_aggregate_bundle_records_raw_evidence_as_unavailable_with_reason(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    captured = _install_run_doubles(monkeypatch, tmp_path)

    runner.main(
        [
            "run",
            "--synthetic",
            "--dataset",
            "locomo",
            "--run-dir",
            str(tmp_path / "run"),
        ]
    )

    bundle_call = captured["bundle_calls"][0]
    assert bundle_call["raw_status"] is None
    assert isinstance(bundle_call["raw_reason"], str)
    assert bundle_call["raw_reason"].strip()


def test_verify_mode_calls_bundle_verifier_and_prints_evidence_level(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = tmp_path / "run"
    seen: list[Path] = []

    def fake_verify_bundle(path: Path) -> dict[str, object]:
        seen.append(path)
        return {"evidence_level": "E1"}

    monkeypatch.setattr(runner, "verify_bundle", fake_verify_bundle)

    assert runner.main(["verify", "--run-dir", str(run_dir)]) == 0
    assert seen == [run_dir]
    assert "E1" in capsys.readouterr().out


def test_run_refuses_an_existing_run_directory_before_starting_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    sentinel = run_dir / "owned-by-another-run"
    sentinel.write_text("preserve")
    monkeypatch.setattr(
        runner,
        "download_dataset",
        lambda *_, **__: pytest.fail("existing run must fail before dataset generation"),
    )

    with pytest.raises(FileExistsError):
        runner.main(
            [
                "run",
                "--synthetic",
                "--dataset",
                "locomo",
                "--run-dir",
                str(run_dir),
            ]
        )

    assert sentinel.read_text() == "preserve"


def test_public_output_and_recorded_command_do_not_expose_environment_secrets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured = _install_run_doubles(monkeypatch, tmp_path)
    secret = "g003-super-secret-value"
    monkeypatch.setenv("MEMPLEX_API_KEY", secret)

    runner.main(
        [
            "run",
            "--synthetic",
            "--dataset",
            "locomo",
            "--run-dir",
            str(tmp_path / "run"),
        ]
    )

    public_output = capsys.readouterr()
    command = captured["bundle_calls"][0]["command"]
    assert secret not in public_output.out
    assert secret not in public_output.err
    assert secret not in json.dumps(command)


def test_script_help_runs_from_repo_root_without_import_errors() -> None:
    project_root = Path(__file__).resolve().parent.parent

    completed = subprocess.run(
        [
            str(project_root / ".venv" / "bin" / "python"),
            str(project_root / "scripts" / "run_g003_benchmark.py"),
            "--help",
        ],
        cwd=project_root,
        capture_output=True,
        check=False,
        text=True,
    )

    output = completed.stdout + completed.stderr
    assert completed.returncode == 0, output
    assert "ModuleNotFoundError" not in output


@pytest.mark.parametrize("selection", ["all", *CONCRETE_DATASETS])
def test_manifest_warm_settings_match_actual_dataset_invocations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, selection: str
) -> None:
    # Keep actual creation/verification; only replace expensive benchmark execution.
    real_create_bundle = runner.create_bundle
    captured = _install_run_doubles(monkeypatch, tmp_path)
    monkeypatch.setattr(runner, "create_bundle", real_create_bundle)
    run_dir = tmp_path / "run"
    runner.main(["run", "--synthetic", "--dataset", selection, "--run-dir", str(run_dir)])
    manifest = runner.verify_bundle(run_dir)
    warm_by_dataset = manifest["config"].get("warm_by_dataset")
    assert "warm" not in manifest["config"], "a global warm flag misrepresents mixed runs"
    expected = {
        "hotpotqa": True, "locomo": True, "longmemeval": False,
        "memory_benchmark": True, "nq": True, "popqa": True, "triviaqa": True,
    }
    if selection != "all":
        expected = {selection: expected[selection]}
    assert warm_by_dataset == expected
    assert {call["dataset"]: call["warm"] for call in captured["benchmark_calls"]} == warm_by_dataset
