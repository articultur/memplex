"""Benchmark evaluator: orchestrates multi-dataset runs with JSONL output.

This module provides BenchmarkEvaluator which:
    - Loads and runs benchmarks across all registered datasets
    - Supports warm (seed memories) and cold (no seeding) modes
    - Writes results incrementally to JSONL for resumable runs
    - Appends per-query retrieval traces (explain=True) to a sibling
      ``<output stem>.traces.jsonl`` file
    - Generates markdown summary reports
    - Supports parallel execution via ThreadPoolExecutor
"""

from __future__ import annotations

import json
import logging
import tempfile
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any

from benchmarks.base import (
    BenchmarkResult,
    BenchmarkRunnerFactory,
    BenchmarkSample,
    EvaluationDataset,
)
from memplex.service import MemplexService

logger = logging.getLogger(__name__)


def make_benchmark_service() -> MemplexService:
    """Create a service with an isolated store for benchmark runs.

    Benchmarks must be reproducible: running against the user's default
    ``~/.memplex`` store accumulates access counts and stale timestamps
    across runs, which contaminates recency/frequency-sensitive metrics.
    Lite runs therefore get a fresh temporary storage path; postgres runs
    keep their configured DSN (isolating the schema is the operator's
    responsibility).
    """
    from memplex.config import MemplexConfig

    config = MemplexConfig()
    if config.storage.backend == "lite":
        config.storage.path = tempfile.mkdtemp(prefix="memplex-bench-")
    return MemplexService(config=config)


class _ExplainTraceProxy:
    """Service proxy that forces ``explain=True`` and records query traces.

    Benchmark runners call ``service.query(...)`` directly; intercepting at
    the evaluator boundary keeps every runner unchanged while each traced
    query appends one controlled-reference record (ids, scores, counts,
    timings — never memory content) to the evaluator's trace sink. Every
    attribute other than ``query`` delegates to the wrapped service.
    """

    def __init__(
        self,
        service: MemplexService,
        sink: Callable[[dict[str, Any]], None],
    ) -> None:
        self._service = service
        self._sink = sink

    def query(self, text: str, *args: Any, **kwargs: Any) -> Any:
        # A positional ``explain`` would clash with the forced kwarg.
        if len(args) >= 6:
            args = args[:5]
        kwargs["explain"] = True
        result = self._service.query(text, *args, **kwargs)
        if result.explanation is not None:
            self._sink(result.explanation)
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self._service, name)


class BenchmarkEvaluator:
    """Orchestrates benchmark runs across all datasets.

    Parameters
    ----------
    service:
        MemplexService instance to evaluate.
    output_dir:
        Directory for results and reports. Defaults to ``.memplex/benchmarks``.
    output_file:
        File name (within ``output_dir``) for JSONL results.
        Defaults to ``results.jsonl``.
    capture_traces:
        If True (default), run every benchmark query with ``explain=True``
        and append one JSON object per query to ``<output stem>.traces.jsonl``
        next to the results file. Traces carry controlled references only
        (candidate ids, scores, counts, timings) — never memory content.
    """

    def __init__(
        self,
        service: MemplexService,
        output_dir: str = ".memplex/benchmarks",
        output_file: str = "results.jsonl",
        capture_traces: bool = True,
    ) -> None:
        self.service = service
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.output_file = output_file
        self.capture_traces = capture_traces
        self._trace_lock = threading.Lock()

    # ── Public API ──────────────────────────────────────────────────────────────

    def run_all(
        self,
        dataset_paths: dict[str, str],
        retrieval_k: int = 10,
        write_memories: bool = True,
        parallel: bool = False,
        max_workers: int = 4,
        warmup_rounds: int = 1,
    ) -> dict[str, list[BenchmarkResult]]:
        """Run all registered benchmarks.

        Parameters
        ----------
        dataset_paths:
            Mapping from dataset name to file path.
            E.g. ``{"locomo": "/data/locomo/test.json", "nq": "/data/nq/dev.json"}``.
        retrieval_k:
            Top-K for retrieval benchmarks (default 10).
        write_memories:
            If True, seed memories before each run (``warm`` mode).
            If False, measure cold retrieval performance.
        parallel:
            If True, run benchmarks in parallel across datasets.
        max_workers:
            Maximum parallel workers when ``parallel=True``.
        warmup_rounds:
            Untimed warmup queries issued after seeding and before the timed
            run (default 1). Warms FTS/connection caches so first-call
            overhead does not pollute the measured latencies. ``0`` disables.

        Returns
        -------
        dict[str, list[BenchmarkResult]]
            Per-dataset list of benchmark results.
        """
        results: dict[str, list[BenchmarkResult]] = {}

        if parallel:
            results = self._run_parallel(
                dataset_paths, retrieval_k, write_memories, max_workers, warmup_rounds
            )
        else:
            results = self._run_sequential(
                dataset_paths, retrieval_k, write_memories, warmup_rounds
            )

        # Write results to JSONL
        self._write_jsonl(results)

        return results

    def run_single(
        self,
        dataset_name: str,
        dataset_path: str,
        retrieval_k: int = 10,
        write_memories: bool = True,
        warmup_rounds: int = 1,
    ) -> list[BenchmarkResult]:
        """Run a single named benchmark.

        Parameters
        ----------
        dataset_name:
            Name of the dataset (e.g. "locomo", "nq", "popqa").
        dataset_path:
            Path to the dataset file.
        retrieval_k:
            Top-K for retrieval benchmarks.
        write_memories:
            If True, seed memories before running (``warm`` mode).
        warmup_rounds:
            Untimed warmup queries before the timed run (``0`` disables).

        Returns
        -------
        list[BenchmarkResult]
        """
        return self._run_benchmark(
            dataset_name, dataset_path, retrieval_k, write_memories, warmup_rounds
        )

    def report(
        self,
        results: dict[str, list[BenchmarkResult]],
        format: str = "markdown",
    ) -> str:
        """Format benchmark results as a comparison table.

        Parameters
        ----------
        results:
            Per-dataset results from ``run_all`` or ``run_single``.
        format:
            Output format: ``"markdown"`` (default) or ``"json"``.

        Returns
        -------
        str
            Formatted report string.
        """
        if format == "json":
            return self._report_json(results)
        return self._report_markdown(results)

    # ── Internal ──────────────────────────────────────────────────────────────

    def _run_sequential(
        self,
        dataset_paths: dict[str, str],
        retrieval_k: int,
        write_memories: bool,
        warmup_rounds: int,
    ) -> dict[str, list[BenchmarkResult]]:
        """Run benchmarks sequentially (one dataset at a time)."""
        results: dict[str, list[BenchmarkResult]] = {}

        for dataset_name, dataset_path in dataset_paths.items():
            try:
                dataset_results = self._run_benchmark(
                    dataset_name, dataset_path, retrieval_k, write_memories, warmup_rounds
                )
                results[dataset_name] = dataset_results
                logger.info(
                    "%s benchmark completed: %d results",
                    dataset_name,
                    len(dataset_results),
                )
            except Exception as exc:  # noqa: BLE001 - logged degradation path
                logger.error("Failed to run %s benchmark: %s", dataset_name, exc)
                results[dataset_name] = []

        return results

    def _run_parallel(
        self,
        dataset_paths: dict[str, str],
        retrieval_k: int,
        write_memories: bool,
        max_workers: int,
        warmup_rounds: int,
    ) -> dict[str, list[BenchmarkResult]]:
        """Run benchmarks in parallel across datasets."""
        results: dict[str, list[BenchmarkResult]] = {}

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(
                    self._run_benchmark,
                    name,
                    path,
                    retrieval_k,
                    write_memories,
                    warmup_rounds,
                ): name
                for name, path in dataset_paths.items()
            }

            for future in as_completed(futures):
                name = futures[future]
                try:
                    results[name] = future.result()
                    logger.info(
                        "%s benchmark completed: %d results",
                        name,
                        len(results[name]),
                    )
                except Exception as exc:  # noqa: BLE001 - logged degradation path
                    logger.error("Failed to run %s benchmark: %s", name, exc)
                    results[name] = []

        return results

    def _run_benchmark(
        self,
        dataset_name: str,
        dataset_path: str,
        retrieval_k: int,
        write_memories: bool,
        warmup_rounds: int = 1,
    ) -> list[BenchmarkResult]:
        """Load dataset and run both retrieval and generation benchmarks."""
        # Create dataset and runner (raises KeyError for unknown datasets)
        dataset = BenchmarkRunnerFactory.create_dataset(dataset_name)
        runner = BenchmarkRunnerFactory.create_runner(dataset_name)

        # Load samples
        samples = dataset.load(dataset_path)
        if not samples:
            logger.warning("No samples loaded from %s", dataset_path)
            return []

        logger.info(
            "Running %s benchmark: %d samples, warm=%s",
            dataset_name,
            len(samples),
            write_memories,
        )

        results: list[BenchmarkResult] = []

        # Seed memories if warm mode
        if write_memories:
            self._seed_memories(dataset, samples)

        # Warmup: untimed queries so first-call overhead (FTS cache, DB
        # connection setup) does not pollute the runners' measured latencies.
        self._warmup(samples, retrieval_k, warmup_rounds)

        # Benchmark queries run through the explain proxy so per-query
        # traces land in <output stem>.traces.jsonl; warmup stays untraced
        # because it is measurement hygiene, not evaluated workload.
        query_service: Any = self.service
        if self.capture_traces:
            query_service = _ExplainTraceProxy(
                self.service,
                lambda explanation: self._append_trace(dataset_name, explanation),
            )

        # Run retrieval benchmark
        start_time = time.perf_counter()
        retrieval_results = runner.run_retrieval(query_service, samples, top_k=retrieval_k)
        results.extend(retrieval_results)

        # Run generation benchmark
        generation_results = runner.run_generation(query_service, samples)
        results.extend(generation_results)

        elapsed = time.perf_counter() - start_time
        logger.info(
            "%s benchmark done in %.2fs: %d results",
            dataset_name,
            elapsed,
            len(results),
        )

        return results

    def _warmup(
        self,
        samples: list[BenchmarkSample],
        retrieval_k: int,
        rounds: int,
    ) -> None:
        """Issue ``rounds`` untimed queries to warm caches before measurement.

        Failures are logged at debug level and never abort the run — warmup
        is a measurement-hygiene step, not part of the evaluated workload.
        """
        if rounds <= 0 or not samples:
            return
        for i in range(rounds):
            sample = samples[i % len(samples)]
            try:
                self.service.query(sample.query, top_k=retrieval_k)
            except Exception as exc:  # noqa: BLE001 - logged degradation path
                logger.debug("Warmup query failed for sample %s: %s", sample.id, exc)

    def _seed_memories(
        self,
        dataset: EvaluationDataset,
        samples: list[BenchmarkSample],
    ) -> None:
        """Seed memplex with benchmark data.

        For each sample, converts it to a SourceDocument and writes it
        into the service. This is the ``warm`` mode setup step.

        For datasets that carry memory objects in metadata (memory_benchmark,
        locomo), seeds the actual Fact/Preference/Observation directly. For
        standard datasets, uses write() to extract and store Functions.
        """
        seeded = 0
        for sample in samples:
            try:
                source_doc = dataset.to_memories(sample)
                metadata = getattr(source_doc, "metadata", None) or {}

                # Check for direct memory seeding (memory_benchmark style)
                if hasattr(dataset, "get_memory_id"):
                    mem_type = metadata.get("memory_type", "fact")
                    memory = metadata.get("memory")
                    if memory is not None:
                        self._seed_direct_memory(memory, mem_type)
                        seeded += 1
                        continue

                # Check for memory_objects list (locomo style)
                memory_objects = metadata.get("memory_objects", [])
                if memory_objects:
                    mem_type = metadata.get("memory_type", "fact")
                    for mem_obj in memory_objects:
                        self._seed_direct_memory(mem_obj, mem_type)
                    seeded += 1
                    continue

                self.service.write(source_doc)
                seeded += 1
            except Exception as exc:  # noqa: BLE001 - logged degradation path
                logger.warning("Failed to seed sample %s: %s", sample.id, exc)
                continue

        logger.info("Seeded %d/%d memories", seeded, len(samples))

    def _seed_direct_memory(self, memory, memory_type: str) -> None:
        """Seed a memory object directly into the store.

        Converts typed benchmark memories to searchable Function records.
        """
        from memplex.models.memory import Fact, Function, Observation, Preference
        from memplex.models.source import SourceDocument, SourceType

        if memory_type == "fact" and isinstance(memory, Fact):
            func = Function(
                id=memory.id,
                name=memory.name or memory.subject,
                name_normalized=(memory.name or memory.subject or "")
                .lower()
                .strip()
                .replace(" ", "_"),
                domain=None,
                memory_type="function",
                source_type=memory.source_type,
                created_at=memory.created_at,
                updated_at=memory.updated_at,
            )
            content = (
                f"{memory.subject} {memory.predicate} {memory.object_}".strip()
                if memory.subject
                else memory.object_
            )
            source = SourceDocument(
                type="benchmark",
                content=content,
                source_type=SourceType.WIKI,
            )
            self.service.store.add(func, source)

        elif memory_type == "preference" and isinstance(memory, Preference):
            name = memory.name or f"Preference: {memory.aspect}"
            func = Function(
                id=memory.id,
                name=name,
                name_normalized=name.lower().strip().replace(" ", "_"),
                domain=None,
                memory_type="function",
                source_type=memory.source_type,
                created_at=memory.created_at,
                updated_at=memory.updated_at,
            )
            source = SourceDocument(
                type="benchmark",
                content=memory.preference,
                source_type=SourceType.WIKI,
            )
            self.service.store.add(func, source)

        elif memory_type == "observation" and isinstance(memory, Observation):
            name = memory.name or f"Observed: {memory.event[:50]}"
            func = Function(
                id=memory.id,
                name=name,
                name_normalized=name.lower().strip().replace(" ", "_"),
                domain=None,
                memory_type="function",
                source_type=memory.source_type,
                created_at=memory.created_at,
                updated_at=memory.updated_at,
            )
            source = SourceDocument(
                type="benchmark",
                content=f"{memory.event} {memory.context}".strip(),
                source_type=SourceType.WIKI,
            )
            self.service.store.add(func, source)

    def _traces_path(self) -> Path:
        """Per-query trace JSONL path, next to the results file."""
        return self.output_dir / f"{Path(self.output_file).stem}.traces.jsonl"

    def _append_trace(self, dataset_name: str, explanation: dict[str, Any]) -> None:
        """Append one per-query trace line to the traces JSONL file."""
        line = json.dumps(
            {"dataset": dataset_name, "trace": explanation}, default=str
        )
        with self._trace_lock, open(self._traces_path(), "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def _write_jsonl(
        self,
        results: dict[str, list[BenchmarkResult]],
    ) -> None:
        """Append all results to a JSONL file, one JSON object per line."""
        output_path = self.output_dir / self.output_file
        mode = "a" if output_path.exists() else "w"

        with open(output_path, mode, encoding="utf-8") as fh:
            for dataset_results in results.values():
                fh.writelines(json.dumps(result.to_dict(), default=str) + "\n" for result in dataset_results)

        logger.info("Results written to %s", output_path)

    def _report_markdown(
        self,
        results: dict[str, list[BenchmarkResult]],
    ) -> str:
        """Format results as a markdown comparison table."""
        if not results:
            return "_No results to display._"

        has_percentiles = any(
            r.latency_p50_ms is not None
            for dataset_results in results.values()
            for r in dataset_results
        )
        if has_percentiles:
            lines: list[str] = [
                "# Memplex Benchmark Results",
                "",
                f"_Generated: {datetime.now(UTC).isoformat().replace('+00:00', 'Z')}_",
                "",
                "## Summary",
                "",
                "| Dataset | Metric | Value | Samples | Latency mean/p50/p99 (ms) |",
                "|---------|--------|-------|---------|---------------------------|",
            ]
        else:
            lines = [
                "# Memplex Benchmark Results",
                "",
                f"_Generated: {datetime.now(UTC).isoformat().replace('+00:00', 'Z')}_",
                "",
                "## Summary",
                "",
                "| Dataset | Metric | Value | Samples | Latency (ms) |",
                "|---------|--------|-------|---------|--------------|",
            ]

        for dataset_name, dataset_results in sorted(results.items()):
            for r in dataset_results:
                if has_percentiles:
                    p50 = f"{r.latency_p50_ms:.1f}" if r.latency_p50_ms is not None else "-"
                    p99 = f"{r.latency_p99_ms:.1f}" if r.latency_p99_ms is not None else "-"
                    latency_cell = f"{r.latency_ms:.1f}/{p50}/{p99}"
                else:
                    latency_cell = f"{r.latency_ms:.1f}"
                lines.append(
                    f"| {r.dataset} | {r.metric} | {r.value:.4f} | {r.samples} | {latency_cell} |"
                )

        lines.append("")
        lines.append("## Per-Dataset Breakdown")
        lines.append("")

        for dataset_name, dataset_results in sorted(results.items()):
            if not dataset_results:
                continue

            lines.append(f"### {dataset_name}")
            lines.append("")
            lines.append("| Metric | Value |")
            lines.append("|--------|-------|")

            # Deduplicate by metric (take best value per metric)
            seen_metrics: dict[str, float] = {}
            for r in dataset_results:
                key = r.metric
                if key not in seen_metrics or r.value > seen_metrics[key]:
                    seen_metrics[key] = r.value

            for metric, value in sorted(seen_metrics.items()):
                lines.append(f"| {metric} | {value:.4f} |")

            lines.append("")

        return "\n".join(lines)

    def _report_json(
        self,
        results: dict[str, list[BenchmarkResult]],
    ) -> str:
        """Format results as JSON."""
        output: dict[str, Any] = {
            "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "benchmarks": {},
        }

        for dataset_name, dataset_results in results.items():
            if not dataset_results:
                continue
            output["benchmarks"][dataset_name] = [r.to_dict() for r in dataset_results]

        return json.dumps(output, indent=2, default=str)


# ── CLI helper ────────────────────────────────────────────────────────────────


def run_benchmark_cli(
    dataset: str,
    path: str,
    output: str = ".memplex/benchmarks/results.jsonl",
    warm: bool = True,
    retrieval_k: int = 10,
    parallel: bool = False,
) -> dict[str, list[BenchmarkResult]]:
    """Convenience function for running a benchmark from the CLI.

    Parameters
    ----------
    dataset:
        Dataset name (``locomo``, ``nq``, ``triviaqa``, ``popqa``, ``hotpotqa``, ``all``).
    path:
        Path to the dataset file.
    output:
        Output file path for JSONL results.
    warm:
        If True, seed memories before running.
    retrieval_k:
        Top-K for retrieval benchmarks.
    parallel:
        If True, run benchmarks in parallel.

    Returns
    -------
    dict[str, list[BenchmarkResult]]
    """
    svc = make_benchmark_service()
    svc.start()

    try:
        evaluator = BenchmarkEvaluator(
            svc,
            output_dir=str(Path(output).parent),
            output_file=Path(output).name,
        )

        # Resolve "all" to all available datasets
        if dataset == "all":
            available = BenchmarkRunnerFactory.available_datasets()
            dataset_paths = {name: path for name in available}
        else:
            dataset_paths = {dataset: path}

        results = evaluator.run_all(
            dataset_paths,
            retrieval_k=retrieval_k,
            write_memories=warm,
            parallel=parallel,
        )

        print(evaluator.report(results, format="markdown"))
        return results

    finally:
        svc.stop()
