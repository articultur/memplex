"""Benchmark evaluator: orchestrates multi-dataset runs with JSONL output.

This module provides BenchmarkEvaluator which:
    - Loads and runs benchmarks across all registered datasets
    - Supports warm (seed memories) and cold (no seeding) modes
    - Writes results incrementally to JSONL for resumable runs
    - Generates markdown summary reports
    - Supports parallel execution via ThreadPoolExecutor
"""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List

from benchmarks.base import (
    BenchmarkResult,
    BenchmarkRunnerFactory,
    BenchmarkSample,
    EvaluationDataset,
)
from memplex.service import MemplexService

logger = logging.getLogger(__name__)


class BenchmarkEvaluator:
    """Orchestrates benchmark runs across all datasets.

    Parameters
    ----------
    service:
        MemplexService instance to evaluate.
    output_dir:
        Directory for results and reports. Defaults to ``.memplex/benchmarks``.
    """

    def __init__(
        self,
        service: MemplexService,
        output_dir: str = ".memplex/benchmarks",
    ) -> None:
        self.service = service
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)

    # ── Public API ──────────────────────────────────────────────────────────────

    def run_all(
        self,
        dataset_paths: Dict[str, str],
        retrieval_k: int = 10,
        write_memories: bool = True,
        parallel: bool = False,
        max_workers: int = 4,
    ) -> Dict[str, List[BenchmarkResult]]:
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

        Returns
        -------
        Dict[str, List[BenchmarkResult]]
            Per-dataset list of benchmark results.
        """
        results: Dict[str, List[BenchmarkResult]] = {}

        if parallel:
            results = self._run_parallel(
                dataset_paths, retrieval_k, write_memories, max_workers
            )
        else:
            results = self._run_sequential(dataset_paths, retrieval_k, write_memories)

        # Write results to JSONL
        self._write_jsonl(results)

        return results

    def run_single(
        self,
        dataset_name: str,
        dataset_path: str,
        retrieval_k: int = 10,
        write_memories: bool = True,
    ) -> List[BenchmarkResult]:
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

        Returns
        -------
        List[BenchmarkResult]
        """
        return self._run_benchmark(
            dataset_name, dataset_path, retrieval_k, write_memories
        )

    def report(
        self,
        results: Dict[str, List[BenchmarkResult]],
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
        dataset_paths: Dict[str, str],
        retrieval_k: int,
        write_memories: bool,
    ) -> Dict[str, List[BenchmarkResult]]:
        """Run benchmarks sequentially (one dataset at a time)."""
        results: Dict[str, List[BenchmarkResult]] = {}

        for dataset_name, dataset_path in dataset_paths.items():
            try:
                dataset_results = self._run_benchmark(
                    dataset_name, dataset_path, retrieval_k, write_memories
                )
                results[dataset_name] = dataset_results
                logger.info(
                    "%s benchmark completed: %d results",
                    dataset_name,
                    len(dataset_results),
                )
            except Exception as exc:
                logger.error("Failed to run %s benchmark: %s", dataset_name, exc)
                results[dataset_name] = []

        return results

    def _run_parallel(
        self,
        dataset_paths: Dict[str, str],
        retrieval_k: int,
        write_memories: bool,
        max_workers: int,
    ) -> Dict[str, List[BenchmarkResult]]:
        """Run benchmarks in parallel across datasets."""
        results: Dict[str, List[BenchmarkResult]] = {}

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(
                    self._run_benchmark,
                    name,
                    path,
                    retrieval_k,
                    write_memories,
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
                except Exception as exc:
                    logger.error("Failed to run %s benchmark: %s", name, exc)
                    results[name] = []

        return results

    def _run_benchmark(
        self,
        dataset_name: str,
        dataset_path: str,
        retrieval_k: int,
        write_memories: bool,
    ) -> List[BenchmarkResult]:
        """Load dataset and run both retrieval and generation benchmarks."""
        # Create dataset and runner
        try:
            dataset = BenchmarkRunnerFactory.create_dataset(dataset_name)
            runner = BenchmarkRunnerFactory.create_runner(dataset_name)
        except KeyError:
            # Fallback: try LocomoDataset/LocomoRunner directly
            from benchmarks.locomo import LocomoDataset, LocomoRunner

            dataset = LocomoDataset()
            runner = LocomoRunner(dataset)

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

        results: List[BenchmarkResult] = []

        # Seed memories if warm mode
        if write_memories:
            self._seed_memories(dataset, samples)

        # Run retrieval benchmark
        start_time = time.perf_counter()
        retrieval_results = runner.run_retrieval(
            self.service, samples, top_k=retrieval_k
        )
        results.extend(retrieval_results)

        # Run generation benchmark
        generation_results = runner.run_generation(self.service, samples)
        results.extend(generation_results)

        elapsed = time.perf_counter() - start_time
        logger.info(
            "%s benchmark done in %.2fs: %d results",
            dataset_name,
            elapsed,
            len(results),
        )

        return results

    def _seed_memories(
        self,
        dataset: EvaluationDataset,
        samples: List[BenchmarkSample],
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
                metadata = source_doc.metadata or {}

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
            except Exception as exc:
                logger.debug("Failed to seed sample %s: %s", sample.id, exc)
                continue

        logger.info("Seeded %d/%d memories", seeded, len(samples))

    def _seed_direct_memory(self, memory, memory_type: str) -> None:
        """Seed a memory object directly into the store.

        Handles Fact, Preference, and Observation types by converting them
        to Function storage format.
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
                memory_type="fact",
                source_type=memory.source_type,
                created_at=memory.created_at,
                updated_at=memory.updated_at,
                trigger=[],
                condition=[],
                action=[],
                benefit=[],
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
            func = Function(
                id=memory.id,
                name=memory.name or f"Preference: {memory.aspect}",
                name_normalized=(memory.name or f"Preference: {memory.aspect}")
                .lower()
                .strip()
                .replace(" ", "_"),
                domain=None,
                memory_type="preference",
                source_type=memory.source_type,
                created_at=memory.created_at,
                updated_at=memory.updated_at,
                trigger=[],
                condition=[],
                action=[],
                benefit=[],
            )
            source = SourceDocument(
                type="benchmark",
                content=memory.preference,
                source_type=SourceType.WIKI,
            )
            self.service.store.add(func, source)

        elif memory_type == "observation" and isinstance(memory, Observation):
            func = Function(
                id=memory.id,
                name=memory.name or f"Observed: {memory.event[:50]}",
                name_normalized=(memory.name or f"Observed: {memory.event[:50]}")
                .lower()
                .strip()
                .replace(" ", "_"),
                domain=None,
                memory_type="observation",
                source_type=memory.source_type,
                created_at=memory.created_at,
                updated_at=memory.updated_at,
                trigger=[],
                condition=[],
                action=[],
                benefit=[],
            )
            source = SourceDocument(
                type="benchmark",
                content=f"{memory.event} {memory.context}".strip(),
                source_type=SourceType.WIKI,
            )
            self.service.store.add(func, source)

    def _write_jsonl(
        self,
        results: Dict[str, List[BenchmarkResult]],
    ) -> None:
        """Append all results to a JSONL file, one JSON object per line."""
        output_path = self.output_dir / "results.jsonl"
        mode = "a" if output_path.exists() else "w"

        with open(output_path, mode, encoding="utf-8") as fh:
            for dataset_name, dataset_results in results.items():
                for result in dataset_results:
                    fh.write(json.dumps(result.to_dict(), default=str) + "\n")

        logger.info("Results written to %s", output_path)

    def _report_markdown(
        self,
        results: Dict[str, List[BenchmarkResult]],
    ) -> str:
        """Format results as a markdown comparison table."""
        if not results:
            return "_No results to display._"

        lines: List[str] = [
            "# Memplex Benchmark Results",
            "",
            f"_Generated: {datetime.utcnow().isoformat()}Z_",
            "",
            "## Summary",
            "",
            "| Dataset | Metric | Value | Samples | Latency (ms) |",
            "|---------|--------|-------|---------|--------------|",
        ]

        for dataset_name, dataset_results in sorted(results.items()):
            for r in dataset_results:
                lines.append(
                    f"| {r.dataset} | {r.metric} | "
                    f"{r.value:.4f} | {r.samples} | {r.latency_ms} |"
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
            seen_metrics: Dict[str, float] = {}
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
        results: Dict[str, List[BenchmarkResult]],
    ) -> str:
        """Format results as JSON."""
        output: Dict[str, Any] = {
            "generated_at": datetime.utcnow().isoformat() + "Z",
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
) -> Dict[str, List[BenchmarkResult]]:
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
    Dict[str, List[BenchmarkResult]]
    """
    from memplex.service import MemplexService

    svc = MemplexService()
    svc.start()

    try:
        evaluator = BenchmarkEvaluator(svc, output_dir=str(Path(output).parent))

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
