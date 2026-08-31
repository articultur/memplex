"""Tests for the benchmarks framework: factory registration, seeding paths,
metric computation, and CLI output plumbing."""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

import json
import logging
from types import SimpleNamespace

import pytest

import benchmarks  # noqa: F401  (import triggers benchmark registration)
from benchmarks.base import (
    BenchmarkResult,
    BenchmarkRunnerFactory,
    BenchmarkSample,
    LatencyStats,
    token_f1,
)
from benchmarks.evaluator import BenchmarkEvaluator
from benchmarks.loader import download_dataset
from benchmarks.locomo import LocomoDataset, LocomoRunner
from benchmarks.memory_eval import (
    MemoryBenchmarkDataset,
    MemoryBenchmarkRunner,
)
from benchmarks.metrics import MemoryMetrics
from benchmarks.nq_trivia import TriviaQADataset
from benchmarks.popqa_hotpot import _compute_multihop_metrics
from memplex.models.memory import Fact

# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture
def service(tmp_path):
    from memplex.config import MemplexConfig
    from memplex.service import MemplexService

    cfg = MemplexConfig()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(tmp_path / "store")
    cfg.llm.query_enhancement = False
    svc = MemplexService(config=cfg)
    yield svc
    svc.stop()


# ── Factory registration ───────────────────────────────────────────────


class TestFactoryRegistration:
    EXPECTED = {
        "locomo",
        "nq",
        "triviaqa",
        "nq_trivia",
        "popqa",
        "hotpotqa",
        "popqa_hotpot",
        "memory_benchmark",
    }

    def test_all_benchmarks_registered(self):
        assert self.EXPECTED <= set(BenchmarkRunnerFactory.available_datasets())
        assert self.EXPECTED <= set(BenchmarkRunnerFactory.available_runners())

    def test_memory_benchmark_registered_via_package_import(self):
        """benchmarks/__init__.py must import memory_eval so registration runs."""
        runner = BenchmarkRunnerFactory.create_runner("memory_benchmark")
        dataset = BenchmarkRunnerFactory.create_dataset("memory_benchmark")
        assert isinstance(runner, MemoryBenchmarkRunner)
        assert isinstance(dataset, MemoryBenchmarkDataset)

    def test_create_unknown_dataset_raises(self):
        with pytest.raises(KeyError):
            BenchmarkRunnerFactory.create_dataset("does_not_exist")
        with pytest.raises(KeyError):
            BenchmarkRunnerFactory.create_runner("does_not_exist")

    def test_shared_runner_labeled_with_registered_name(self):
        """NQTriviaRunner/PopQAHotpotRunner serve several registrations; results
        must be labeled with the requested dataset, not the composite alias."""
        nq_runner = BenchmarkRunnerFactory.create_runner("nq")
        trivia_runner = BenchmarkRunnerFactory.create_runner("triviaqa")
        popqa_runner = BenchmarkRunnerFactory.create_runner("popqa")
        hotpot_runner = BenchmarkRunnerFactory.create_runner("hotpotqa")
        assert nq_runner.name == "nq"
        assert trivia_runner.name == "triviaqa"
        assert popqa_runner.name == "popqa"
        assert hotpot_runner.name == "hotpotqa"
        # Composite registrations keep their composite label.
        assert BenchmarkRunnerFactory.create_runner("nq_trivia").name == "nq_trivia"
        assert BenchmarkRunnerFactory.create_runner("popqa_hotpot").name == "popqa_hotpot"

    def test_evaluator_unknown_dataset_raises_no_silent_fallback(self, service, tmp_path):
        """evaluator must not silently fall back to LoCoMo for unknown names."""
        evaluator = BenchmarkEvaluator(service, output_dir=str(tmp_path / "out"))
        with pytest.raises(KeyError):
            evaluator.run_single("does_not_exist", str(tmp_path / "x.json"))


# ── LoCoMo seeding (Fact model conformance) ───────────────────────────


def _locomo_sample() -> BenchmarkSample:
    return BenchmarkSample(
        id="conv1_qa",
        query="What did we discuss?",
        expected_ids=["mem_1"],
        metadata={
            "type": "qa",
            "conversation_id": "conv1",
            "turns": [{"speaker": "user", "text": "hello", "timestamp": ""}],
            "ground_truth_memories": [
                {"memory_id": "mem_1", "content": "we discussed python", "session_id": "s1"}
            ],
        },
    )


class TestLocomoSeeding:
    def test_to_memories_builds_valid_fact(self):
        """Fact has no 'content' field — seeding must not raise TypeError."""
        doc = LocomoDataset().to_memories(_locomo_sample())
        memory_objects = doc.metadata["memory_objects"]
        assert len(memory_objects) == 1
        fact = memory_objects[0]
        assert isinstance(fact, Fact)
        assert fact.subject == "s1"
        assert fact.predicate == "contains"
        assert fact.object_ == "we discussed python"

    def test_seed_memories_via_evaluator(self, service, tmp_path, caplog):
        evaluator = BenchmarkEvaluator(service, output_dir=str(tmp_path / "out"))
        sample = _locomo_sample()
        with caplog.at_level(logging.WARNING, logger="benchmarks.evaluator"):
            evaluator._seed_memories(LocomoDataset(), [sample])
        assert service.store.get("mem_1") is not None
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING]

    def test_seed_failure_is_warning_visible(self, service, tmp_path, caplog):
        """A broken sample must surface at WARNING level, not be silently debug-logged."""

        class BrokenDataset(LocomoDataset):
            def to_memories(self, sample):
                raise TypeError("boom")

        evaluator = BenchmarkEvaluator(service, output_dir=str(tmp_path / "out"))
        with caplog.at_level(logging.WARNING, logger="benchmarks.evaluator"):
            evaluator._seed_memories(BrokenDataset(), [_locomo_sample()])
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("Failed to seed sample" in r.getMessage() for r in warnings)


# ── LoCoMo retrieval metric names ─────────────────────────────────────


class TestLocomoRunnerMetricNames:
    def test_metric_names_follow_top_k(self):
        """recall@K / precision@K names must track the requested top_k."""

        class StubService:
            def write(self, source_doc):
                pass

            def query(self, query, top_k=10):
                return SimpleNamespace(results=[])

        runner = LocomoRunner()
        results = runner.run_retrieval(StubService(), [_locomo_sample()], top_k=5)
        metric_names = {r.metric for r in results}
        assert "recall@5" in metric_names
        assert "precision@5" in metric_names
        assert "recall@10" not in metric_names


# ── Memory benchmark seeding ───────────────────────────────────────────


class TestMemoryBenchmark:
    def test_dataset_generates_samples(self):
        samples = MemoryBenchmarkDataset(num_facts=3, num_prefs=2, num_obs=2).load("")
        types = {s.metadata["memory_type"] for s in samples}
        assert types == {"fact", "preference", "observation"}

    def test_seed_memories_smoke(self, service):
        """Seeding synthetic facts/prefs/obs into lite storage must not blow up."""
        dataset = MemoryBenchmarkDataset(num_facts=3, num_prefs=2, num_obs=2)
        samples = dataset.load("")
        runner = MemoryBenchmarkRunner(dataset)
        runner._seed_memories(service, samples)
        for sample in samples:
            mem_id = sample.metadata["memory_id"]
            assert service.store.get(mem_id) is not None

    def test_fact_retention_end_to_end(self, service):
        dataset = MemoryBenchmarkDataset(num_facts=3, num_prefs=0, num_obs=0)
        samples = dataset.load("")
        runner = MemoryBenchmarkRunner(dataset)
        runner._seed_memories(service, samples)
        results = runner._run_fact_retention_test(
            service, samples, top_k=10, timestamp="t"
        )
        assert results, "expected fact retention results"
        retention = next(r for r in results if r.metric == "fact_retention_rate")
        assert 0.0 <= retention.value <= 1.0


# ── End-to-end smoke (lite storage, synthetic data) ───────────────────


class TestEndToEndSmoke:
    def test_run_single_locomo_synthetic(self, service, tmp_path):
        """Full warm-mode run: seed + retrieval + generation must not blow up."""
        path = download_dataset("locomo", output_dir=str(tmp_path), force_synthetic=True)
        evaluator = BenchmarkEvaluator(service, output_dir=str(tmp_path / "out"))
        results = evaluator.run_single("locomo", str(path), retrieval_k=5)
        assert results, "expected locomo benchmark results"
        metric_names = {r.metric for r in results}
        assert "recall@5" in metric_names
        assert "precision@5" in metric_names

    def test_run_single_memory_benchmark(self, service, tmp_path):
        """memory_benchmark must be reachable end-to-end via the evaluator."""
        evaluator = BenchmarkEvaluator(service, output_dir=str(tmp_path / "out"))
        results = evaluator.run_single("memory_benchmark", "ignored", retrieval_k=5)
        assert results, "expected memory_benchmark results"
        metric_names = {r.metric for r in results}
        assert "fact_retention_rate" in metric_names
        assert all(0.0 <= r.value <= 1.0 for r in results)


# ── MemoryMetrics ──────────────────────────────────────────────────────


class TestMemoryMetrics:
    def test_fact_retention_uses_direct_get(self):
        """fact_retention must not rely on unsupported 'id:' query syntax."""
        mem = SimpleNamespace(id="fact_1")
        svc = SimpleNamespace(get=lambda mid: mem if mid == "fact_1" else None)
        sample = BenchmarkSample(
            id="s1", query="q", expected_ids=["fact_1", "fact_missing"]
        )
        assert MemoryMetrics.fact_retention(svc, sample) == 0.5

    def test_recency_accuracy_with_function_objects(self):
        """service.get returns Function dataclasses, not dicts."""
        memories = {
            "m_old": SimpleNamespace(id="m_old", created_at="2024-01-01", access_count=5),
            "m_new": SimpleNamespace(id="m_new", created_at="2024-02-01", access_count=1),
        }
        svc = SimpleNamespace(get=lambda mid: memories.get(mid))
        sample = BenchmarkSample(id="s1", query="q", expected_ids=["m_old", "m_new"])
        score = MemoryMetrics.recency_accuracy(svc, sample)
        assert score == 1.0  # older memory has higher access count -> correct order

    def test_graph_connectivity_with_function_neighbors(self):
        """get_neighbors returns Function objects; id extraction must work."""
        target = SimpleNamespace(id="target")
        store = SimpleNamespace(get_neighbors=lambda fid: [target] if fid == "src" else [])
        svc = SimpleNamespace(store=store)
        sample = BenchmarkSample(
            id="s1", query="q", metadata={"required_edges": [("src", "target")]}
        )
        assert MemoryMetrics.graph_connectivity(svc, sample) == 1.0

    def test_graph_connectivity_missing_edge(self):
        store = SimpleNamespace(get_neighbors=lambda fid: [])
        svc = SimpleNamespace(store=store)
        sample = BenchmarkSample(
            id="s1", query="q", metadata={"required_edges": [("src", "target")]}
        )
        assert MemoryMetrics.graph_connectivity(svc, sample) == 0.0


# ── TriviaQA synthetic data ────────────────────────────────────────────


class TestTriviaQASynthetic:
    def test_triviaqa_synthetic_has_own_generator(self, tmp_path):
        """triviaqa synthetic data must be TriviaQA-shaped, not NQ-shaped."""
        path = download_dataset("triviaqa", output_dir=str(tmp_path), force_synthetic=True)
        with open(path, encoding="utf-8") as fh:
            records = json.load(fh)
        assert records, "expected synthetic triviaqa records"
        assert all("question_id" in r for r in records)
        assert all(isinstance(r["answer"], dict) and "Value" in r["answer"] for r in records)

    def test_triviaqa_synthetic_parses_with_answers(self, tmp_path):
        path = download_dataset("triviaqa", output_dir=str(tmp_path), force_synthetic=True)
        samples = TriviaQADataset().load(str(path))
        assert samples, "expected parsed triviaqa samples"
        assert all(s.expected_answer for s in samples)
        assert all(s.metadata.get("aliases") for s in samples)


# ── Multi-hop metric formulas ──────────────────────────────────────────


class TestMultihopMetrics:
    def test_top_one_result_covering_two_hops_has_unit_precision_and_recall(self):
        metrics = _compute_multihop_metrics(
            retrieved_summaries=["Alpha connects to Beta and contains the answer."],
            supporting_facts=[{"title": "Alpha"}, {"title": "Beta"}],
            answer_aliases=["answer"],
            k_values=[1],
        )

        assert metrics["hop_precision@1"] == 1.0
        assert metrics["hop_recall@1"] == 1.0

    def test_single_dataset_run_labels_all_metrics_with_dataset_name(self):
        """A hotpotqa-only run must not emit rows labeled 'popqa' in JSONL."""
        from benchmarks.popqa_hotpot import PopQAHotpotRunner

        class StubService:
            def query(self, query, top_k=10):
                return SimpleNamespace(results=[])

        sample = BenchmarkSample(
            id="h1",
            query="q",
            expected_answer="a",
            metadata={"dataset": "hotpotqa", "supporting_facts": [{"title": "T"}]},
        )
        runner = PopQAHotpotRunner(name="hotpotqa")
        results = runner.run_retrieval(StubService(), [sample], top_k=5)
        assert results, "expected retrieval results"
        assert {r.dataset for r in results} == {"hotpotqa"}

    def test_hop_precision_differs_from_hop_recall(self):
        """hop_precision@k is per retrieved slot; hop_recall@k is per required hop."""
        supporting_facts = [{"title": "Alpha"}, {"title": "Beta"}]
        # Only one of two hops covered in top-1
        metrics = _compute_multihop_metrics(
            ["Alpha is a thing"],
            supporting_facts,
            answer_aliases=[],
            max_hops=2,
            k_values=[1],
        )
        assert metrics["hop_precision@1"] == 1.0  # 1 covered hop / 1 slot
        assert metrics["hop_recall@1"] == 0.5  # 1 covered hop / 2 required hops


# ── Output file plumbing ───────────────────────────────────────────────


class TestOutputFile:
    def test_evaluator_respects_output_file(self, service, tmp_path):
        from benchmarks.base import BenchmarkResult

        evaluator = BenchmarkEvaluator(
            service, output_dir=str(tmp_path), output_file="custom.jsonl"
        )
        results = {
            "locomo": [
                BenchmarkResult(
                    name="b", dataset="locomo", metric="mrr", value=1.0,
                    latency_ms=0, samples=1,
                )
            ]
        }
        evaluator._write_jsonl(results)
        assert (tmp_path / "custom.jsonl").exists()
        assert not (tmp_path / "results.jsonl").exists()

    def test_cli_passes_output_filename(self, tmp_path, monkeypatch):
        """run_benchmark_command must forward the user-specified file name."""
        import benchmarks.benchmark_cli as cli

        captured = {}

        class FakeEvaluator:
            def __init__(self, svc, output_dir, output_file="results.jsonl"):
                captured["output_dir"] = output_dir
                captured["output_file"] = output_file

            def run_all(self, dataset_paths, **kwargs):
                return {}

        class FakeService:
            def start(self):
                pass

            def stop(self):
                pass

        dataset_file = tmp_path / "locomo.json"
        dataset_file.write_text("[]", encoding="utf-8")

        monkeypatch.setattr(cli, "BenchmarkEvaluator", FakeEvaluator)
        monkeypatch.setattr(cli, "MemplexService", lambda: FakeService())

        out = tmp_path / "my_results.jsonl"
        cli.run_benchmark_command(
            dataset="locomo",
            path=str(dataset_file),
            output=str(out),
        )
        assert captured["output_file"] == "my_results.jsonl"
        assert captured["output_dir"] == str(tmp_path)


class TestForceSynthetic:
    def test_force_synthetic_bypasses_stale_cache(self, tmp_path):
        """A cached file (e.g. an earlier HuggingFace download) must not be
        reused when force_synthetic=True — regenerate instead."""
        cached = tmp_path / "hotpotqa.json"
        cached.write_text(
            json.dumps([{"id": "stale_hf_sample", "question": "q", "answer": "a"}]),
            encoding="utf-8",
        )
        path = download_dataset("hotpotqa", output_dir=str(tmp_path), force_synthetic=True)
        with open(path, encoding="utf-8") as fh:
            records = json.load(fh)
        assert all(r["id"] != "stale_hf_sample" for r in records)
        assert all(r["id"].startswith("hotpotqa_") for r in records)

    def test_cached_file_reused_without_force_synthetic(self, tmp_path):
        """Default behavior unchanged: an existing cache file is reused."""
        cached = tmp_path / "popqa.json"
        cached.write_text("[]", encoding="utf-8")
        path = download_dataset("popqa", output_dir=str(tmp_path))
        assert path == cached

    def test_cli_forwards_force_synthetic_to_download(self, tmp_path, monkeypatch):
        """run_benchmark_command must forward force_synthetic to download_dataset."""
        import benchmarks.benchmark_cli as cli

        captured = {}

        class FakeEvaluator:
            def __init__(self, svc, output_dir, output_file="results.jsonl"):
                pass

            def run_all(self, dataset_paths, **kwargs):
                return {}

        class FakeService:
            def start(self):
                pass

            def stop(self):
                pass

        def _fake_download(name, force_synthetic=False):
            captured["name"] = name
            captured["force_synthetic"] = force_synthetic
            dataset_file = tmp_path / f"{name}.json"
            dataset_file.write_text("[]", encoding="utf-8")
            return dataset_file

        monkeypatch.setattr(cli, "BenchmarkEvaluator", FakeEvaluator)
        monkeypatch.setattr(cli, "MemplexService", lambda: FakeService())
        monkeypatch.setattr(cli, "download_dataset", _fake_download)

        cli.run_benchmark_command(
            dataset="locomo",
            path=None,
            output=str(tmp_path / "results.jsonl"),
            force_synthetic=True,
        )
        assert captured == {"name": "locomo", "force_synthetic": True}

    def test_cli_force_synthetic_defaults_off(self, tmp_path, monkeypatch):
        """Backward compatibility: omitting force_synthetic keeps downloads enabled."""
        import benchmarks.benchmark_cli as cli

        captured = {}

        class FakeEvaluator:
            def __init__(self, svc, output_dir, output_file="results.jsonl"):
                pass

            def run_all(self, dataset_paths, **kwargs):
                return {}

        class FakeService:
            def start(self):
                pass

            def stop(self):
                pass

        def _fake_download(name, force_synthetic=False):
            captured["force_synthetic"] = force_synthetic
            dataset_file = tmp_path / f"{name}.json"
            dataset_file.write_text("[]", encoding="utf-8")
            return dataset_file

        monkeypatch.setattr(cli, "BenchmarkEvaluator", FakeEvaluator)
        monkeypatch.setattr(cli, "MemplexService", lambda: FakeService())
        monkeypatch.setattr(cli, "download_dataset", _fake_download)

        cli.run_benchmark_command(
            dataset="locomo",
            path=None,
            output=str(tmp_path / "results.jsonl"),
        )
        assert captured["force_synthetic"] is False


# ── CLI dataset resolution ────────────────────────────────────────────


def test_resolve_path_self_generated_memory_benchmark():
    """memory_benchmark generates samples in code; the CLI must not try to
    download/resolve a dataset file for it."""
    from benchmarks.benchmark_cli import _resolve_path

    assert _resolve_path("memory_benchmark", None, auto_download=True) == ""
    # Also fine when auto_download is off (no file needed either way).
    assert _resolve_path("memory_benchmark", None, auto_download=False) == ""


def test_resolve_path_unknown_dataset_still_raises(tmp_path):
    from benchmarks.benchmark_cli import _resolve_path

    with pytest.raises((ValueError, FileNotFoundError)):
        _resolve_path("totally-unknown-dataset", None, auto_download=False)


def test_resolve_datasets_all_includes_memory_benchmark():
    """'all' must cover self-generated datasets too, not just file-backed ones."""
    from benchmarks.benchmark_cli import _resolve_datasets

    names = _resolve_datasets("all")
    assert "memory_benchmark" in names
    assert {"locomo", "nq", "triviaqa", "popqa", "hotpotqa"} <= set(names)
    # Composite aliases must not duplicate their members.
    assert "nq_trivia" not in names
    assert "popqa_hotpot" not in names


def test_resolve_datasets_alias_expands_to_members():
    from benchmarks.benchmark_cli import _resolve_datasets

    assert _resolve_datasets("nq_trivia") == ["nq", "triviaqa"]
    assert _resolve_datasets("popqa_hotpot") == ["popqa", "hotpotqa"]
    assert _resolve_datasets("locomo") == ["locomo"]


def test_cli_all_resolves_self_generated_without_download(tmp_path, monkeypatch):
    """--dataset all must not try to download a file for memory_benchmark."""
    import benchmarks.benchmark_cli as cli

    downloaded = []

    class FakeEvaluator:
        def __init__(self, svc, output_dir, output_file="results.jsonl"):
            pass

        def run_all(self, dataset_paths, **kwargs):
            captured["paths"] = dataset_paths
            return {}

    class FakeService:
        def start(self):
            pass

        def stop(self):
            pass

    def _fake_download(name, force_synthetic=False):
        downloaded.append(name)
        dataset_file = tmp_path / f"{name}.json"
        dataset_file.write_text("[]", encoding="utf-8")
        return dataset_file

    captured = {}
    monkeypatch.setattr(cli, "BenchmarkEvaluator", FakeEvaluator)
    monkeypatch.setattr(cli, "MemplexService", lambda: FakeService())
    monkeypatch.setattr(cli, "download_dataset", _fake_download)

    cli.run_benchmark_command(
        dataset="all",
        path=None,
        output=str(tmp_path / "results.jsonl"),
        force_synthetic=True,
    )
    assert captured["paths"]["memory_benchmark"] == ""
    assert "memory_benchmark" not in downloaded


# ── Latency measurement (perf_counter / float ms / percentiles) ────────────


class TestLatencyStats:
    def test_percentiles_and_mean(self):
        stats = LatencyStats()
        for value in (10.0, 20.0, 30.0, 40.0):
            stats.add(value)
        assert stats.mean == 25.0
        # Nearest-rank: p50 of 4 samples is the 2nd, p99 is the max.
        assert stats.p50 == 20.0
        assert stats.p99 == 40.0

    def test_empty_stats_are_zero(self):
        stats = LatencyStats()
        assert stats.mean == 0.0
        assert stats.p50 == 0.0
        assert stats.p99 == 0.0

    def test_timed_context_records_positive_float(self):
        stats = LatencyStats()
        with stats.timed():
            sum(range(1000))
        assert len(stats) == 1
        assert stats.mean > 0.0
        assert isinstance(stats.mean, float)

    def test_result_serializes_optional_percentiles(self):
        result = BenchmarkResult(
            name="b", dataset="d", metric="m", value=1.0,
            latency_ms=1.5, samples=1,
            latency_p50_ms=1.4, latency_p99_ms=2.0,
        )
        row = result.to_dict()
        assert row["latency_ms"] == 1.5
        assert row["latency_p50_ms"] == 1.4
        assert row["latency_p99_ms"] == 2.0
        # Unset percentiles keep the legacy exact key set.
        plain = BenchmarkResult(
            name="b", dataset="d", metric="m", value=1.0,
            latency_ms=1.5, samples=1,
        ).to_dict()
        assert "latency_p50_ms" not in plain
        assert "latency_p99_ms" not in plain


# ── Shared token-F1 ─────────────────────────────────────────────────────────


def test_token_f1_shared_helper():
    assert token_f1("the cat sat", "the cat") == pytest.approx(2 * (2 / 3) * 1 / ((2 / 3) + 1))
    assert token_f1("", "anything") == 0.0
    assert token_f1("alpha", "beta") == 0.0


# ── LoCoMo official locomo10.json format ────────────────────────────────────


class TestLocomoOfficialFormat:
    @staticmethod
    def _official_entry() -> dict:
        return {
            "sample_id": "conv-1",
            "qa": [
                {
                    "question": "Where does Alice work?",
                    "answer": "Acme",
                    "category": 1,
                    "evidence": ["D1:2"],
                },
                {
                    "question": "How many plants does Bob have?",
                    "answer": 42,  # numeric answers are stringified
                    "category": 2,
                    "evidence": ["D2:1", "D9:9"],  # D9:9 references no turn
                },
            ],
            "conversation": {
                "speaker_a": "Alice",
                "speaker_b": "Bob",
                # Out of declaration order on purpose: loader must sort.
                "session_2_date_time": "2024-02-01 10:00",
                "session_2": [
                    {"speaker": "Bob", "dia_id": "D2:1", "text": "I have many plants."}
                ],
                "session_1_date_time": "2024-01-01 09:00",
                "session_1": [
                    {"speaker": "Alice", "dia_id": "D1:1", "text": "Hi Bob."},
                    {"speaker": "Alice", "dia_id": "D1:2", "text": "I work at Acme."},
                ],
            },
        }

    def test_loads_official_format(self, tmp_path):
        path = tmp_path / "locomo10.json"
        path.write_text(json.dumps([self._official_entry()]), encoding="utf-8")

        samples = LocomoDataset().load(str(path))

        assert [s.id for s in samples] == ["conv-1_q0", "conv-1_q1"]
        first, second = samples
        assert first.query == "Where does Alice work?"
        assert first.expected_answer == "Acme"
        assert first.expected_ids == ["D1:2"]
        assert second.expected_answer == "42"
        assert second.expected_ids == ["D2:1", "D9:9"]
        # Turns flattened chronologically across sessions.
        turns = first.metadata["turns"]
        assert [t["dia_id"] for t in turns] == ["D1:1", "D1:2", "D2:1"]
        assert turns[0]["timestamp"] == "2024-01-01 09:00"
        assert first.metadata["speakers"] == ["Alice", "Bob"]
        # Evidence turns become ground-truth memories; unknown dia_ids get
        # empty content but keep their id for expected_ids alignment.
        assert first.metadata["ground_truth_memories"] == [
            {"memory_id": "D1:2", "content": "I work at Acme.", "session_id": "session_1"}
        ]
        assert second.metadata["ground_truth_memories"][1]["content"] == ""

    def test_legacy_synthetic_format_still_supported(self, tmp_path):
        path = download_dataset("locomo", output_dir=str(tmp_path), force_synthetic=True)
        samples = LocomoDataset().load(str(path))
        assert len(samples) == 3
        assert all(s.metadata["type"] == "qa" for s in samples)


# ── LoCoMo persona consistency (token-F1 against reference) ────────────────


class TestPersonaConsistency:
    @staticmethod
    def _sample() -> BenchmarkSample:
        return BenchmarkSample(
            id="conv_persona",
            query="What does Alice do?",
            metadata={
                "type": "conversation",
                "conversation_id": "conv_persona",
                "speakers": ["Alice", "Bob"],
                "target_speaker": "Alice",
                "turns": [
                    {"speaker": "Alice", "text": "I work at Acme as an engineer."},
                    {"speaker": "Bob", "text": "I bake bread on weekends."},
                ],
                "ground_truth_memories": [],
            },
        )

    @staticmethod
    def _service_with(summary: str):
        class StubService:
            def query(self, query, top_k=10):
                return SimpleNamespace(
                    results=[SimpleNamespace(summary=summary, func_id="f1")]
                )

        return StubService()

    def test_content_overlap_scores_one(self):
        runner = LocomoRunner()
        results = runner._run_persona_consistency(
            self._service_with("I work at Acme as an engineer."),
            [self._sample()],
            top_k=5,
            timestamp="t",
        )
        assert results[0].value == 1.0

    def test_unrelated_retrieval_scores_zero(self):
        runner = LocomoRunner()
        results = runner._run_persona_consistency(
            self._service_with("completely unrelated drizzle forecast"),
            [self._sample()],
            top_k=5,
            timestamp="t",
        )
        assert results[0].value == 0.0

    def test_other_speaker_content_alone_does_not_pass(self):
        """Retrieving only Bob's utterance must not count as Alice-consistent."""
        runner = LocomoRunner()
        results = runner._run_persona_consistency(
            self._service_with("I bake bread on weekends."),
            [self._sample()],
            top_k=5,
            timestamp="t",
        )
        assert results[0].value == 0.0


# ── LoCoMo event tracking (word-boundary, case-insensitive) ─────────────────


class TestEventMentioned:
    @pytest.mark.parametrize(
        "event,text,expected",
        [
            ("art", "the art gallery was quiet", True),
            ("Art", "the ART show opened", True),  # case-insensitive
            ("art", "Artemis launch coverage", False),  # word boundary
            ("book club", "our Book Club met", True),  # multi-word phrase
            ("book club", "club members read a book", False),  # word order matters
            ("", "anything", False),
        ],
    )
    def test_word_boundary_matching(self, event, text, expected):
        assert LocomoRunner._event_mentioned(event, text) is expected


# ── Evaluator warmup ────────────────────────────────────────────────────────


class TestEvaluatorWarmup:
    class CountingService:
        def __init__(self):
            self.queries = 0

        def query(self, query, top_k=10, **kwargs):
            self.queries += 1
            return SimpleNamespace(results=[])

    def test_warmup_issues_untimed_queries(self, tmp_path):
        service = self.CountingService()
        evaluator = BenchmarkEvaluator(service, output_dir=str(tmp_path / "out"))
        samples = [
            BenchmarkSample(id="s1", query="q1"),
            BenchmarkSample(id="s2", query="q2"),
        ]
        evaluator._warmup(samples, retrieval_k=5, rounds=3)
        assert service.queries == 3

    def test_warmup_zero_rounds_is_noop(self, tmp_path):
        service = self.CountingService()
        evaluator = BenchmarkEvaluator(service, output_dir=str(tmp_path / "out"))
        evaluator._warmup([BenchmarkSample(id="s1", query="q1")], retrieval_k=5, rounds=0)
        assert service.queries == 0

    def test_run_single_warmup_disabled_still_completes(self, service, tmp_path):
        path = download_dataset("locomo", output_dir=str(tmp_path), force_synthetic=True)
        evaluator = BenchmarkEvaluator(service, output_dir=str(tmp_path / "out"))
        results = evaluator.run_single("locomo", str(path), retrieval_k=5, warmup_rounds=0)
        assert results
