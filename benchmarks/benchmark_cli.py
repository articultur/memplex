"""CLI interface for running benchmarks.

Provides ``run_benchmark_command()`` which is called by the CLI's
``memplex benchmark run`` subcommand.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional

from benchmarks.base import BenchmarkResult
from benchmarks.evaluator import BenchmarkEvaluator
from benchmarks.loader import download_dataset, list_available_datasets
from memplex.service import MemplexService

logger = logging.getLogger(__name__)


# ── Dataset resolution ─────────────────────────────────────────────────────────

_DATASET_ALIASES: Dict[str, List[str]] = {
    "nq_trivia": ["nq", "triviaqa"],
    "popqa_hotpot": ["popqa", "hotpotqa"],
}


def _resolve_datasets(dataset: str) -> List[str]:
    """Resolve 'all' or composite names to individual dataset names."""
    if dataset == "all":
        return list_available_datasets()
    if dataset in _DATASET_ALIASES:
        return _DATASET_ALIASES[dataset]
    return [dataset]


def _resolve_path(
    dataset: str,
    explicit_path: Optional[str],
    auto_download: bool,
) -> str:
    """Resolve dataset path: explicit path → cached file → download."""
    if explicit_path:
        p = Path(explicit_path)
        if p.exists():
            return explicit_path
        raise FileNotFoundError(f"Dataset path not found: {explicit_path}")

    if not auto_download:
        raise ValueError(
            f"No path provided for dataset '{dataset}'. "
            "Use --path or run without --no-auto-download to auto-download."
        )

    # Auto-download
    print(f"[download] Fetching {dataset} dataset...", file=sys.stderr)
    path = download_dataset(dataset)
    print(f"[download] Saved to: {path}", file=sys.stderr)
    return str(path)


# ── Benchmark run ─────────────────────────────────────────────────────────────


def run_benchmark_command(
    dataset: str,
    path: Optional[str],
    output: str = ".memplex/benchmarks/results.jsonl",
    warm: bool = True,
    retrieval_k: int = 10,
    parallel: bool = False,
    auto_download: bool = True,
) -> Dict[str, List[BenchmarkResult]]:
    """Run one or more benchmarks from the CLI.

    Parameters
    ----------
    dataset:
        Dataset name: ``locomo``, ``nq``, ``triviaqa``, ``popqa``, ``hotpotqa``,
        ``all``.
    path:
        Explicit dataset file path. If not provided, auto-downloads using
        :func:`~benchmarks.loader.download_dataset`.
    output:
        Output JSONL file path.
    warm:
        If True, seed memories before running (``warm`` mode).
    retrieval_k:
        Top-K for retrieval benchmarks.
    parallel:
        If True, run multiple datasets in parallel.
    auto_download:
        If True, download datasets from HuggingFace (or generate synthetic)
        when ``path`` is not provided.

    Returns
    -------
    Dict[str, List[BenchmarkResult]]
        Per-dataset results.
    """
    svc = MemplexService()
    svc.start()

    try:
        evaluator = BenchmarkEvaluator(
            svc,
            output_dir=str(Path(output).parent),
        )

        # Resolve datasets
        dataset_names = _resolve_datasets(dataset)

        # Resolve paths
        if len(dataset_names) == 1 and path:
            # Single dataset with explicit path
            resolved_paths = {dataset_names[0]: path}
        elif len(dataset_names) == 1:
            resolved_paths = {
                dataset_names[0]: _resolve_path(dataset_names[0], path, auto_download)
            }
        else:
            # Multiple datasets — path must be a directory or auto-download each
            if path and not auto_download:
                # Use path as base directory
                base = Path(path)
                if not base.is_dir():
                    raise NotADirectoryError(
                        f"--path must be a directory for multi-dataset runs: {path}"
                    )
                resolved_paths = {}
                for name in dataset_names:
                    p = base / f"{name}.json"
                    if p.exists():
                        resolved_paths[name] = str(p)
                    elif auto_download:
                        print(f"[download] Fetching {name}...", file=sys.stderr)
                        resolved_paths[name] = str(download_dataset(name))
                    else:
                        raise FileNotFoundError(f"Dataset not found: {p}")
            else:
                # Auto-download each
                resolved_paths = {}
                for name in dataset_names:
                    print(f"[download] Fetching {name}...", file=sys.stderr)
                    resolved_paths[name] = str(download_dataset(name))

        # Print run plan
        print("\n=== Memplex Benchmark Run ===", file=sys.stderr)
        print(f"  Datasets : {', '.join(sorted(resolved_paths.keys()))}", file=sys.stderr)
        print(f"  Mode     : {'warm' if warm else 'cold'}", file=sys.stderr)
        print(f"  Top-K    : {retrieval_k}", file=sys.stderr)
        print(f"  Parallel : {parallel}", file=sys.stderr)
        print(f"  Output   : {output}", file=sys.stderr)
        print("  Samples  :", file=sys.stderr)
        for name, p in sorted(resolved_paths.items()):
            # Quick count for display
            import json

            try:
                with open(p) as f:
                    raw = json.load(f)
                count = len(raw) if isinstance(raw, list) else 1
            except Exception:
                count = "?"
            print(f"    - {name}: {count} samples ({p})", file=sys.stderr)
        print(file=sys.stderr)

        # Run benchmarks
        results = evaluator.run_all(
            dataset_paths=resolved_paths,
            retrieval_k=retrieval_k,
            write_memories=warm,
            parallel=parallel,
        )

        # Print summary
        _print_sample_scores(results)

        return results

    finally:
        svc.stop()


def _print_sample_scores(results: Dict[str, List[BenchmarkResult]]) -> None:
    """Print a compact score summary to stdout."""
    if not results:
        print("\nNo results produced.")
        return

    print("\n=== Benchmark Results ===")
    for dataset_name, dataset_results in sorted(results.items()):
        if not dataset_results:
            continue
        print(f"\n[{dataset_name}]")
        # Deduplicate by metric, show best value
        seen: Dict[str, float] = {}
        for r in dataset_results:
            key = r.metric
            if key not in seen or r.value > seen[key]:
                seen[key] = r.value
        for metric, value in sorted(seen.items()):
            print(f"  {metric:30s}: {value:.4f}")
