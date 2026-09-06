"""CLI interface for running benchmarks.

Provides ``run_benchmark_command()``, the entry point invoked by the
``memplex benchmark run`` subcommand (see ``memplex/adapters/cli.py``)::

    memplex benchmark list
    memplex benchmark run --dataset locomo --synthetic --top-k 10

The ``benchmarks`` package is not shipped in the distribution, so the
subcommand only works from a source checkout.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

from benchmarks.base import BenchmarkResult
from benchmarks.evaluator import BenchmarkEvaluator, make_benchmark_service
from benchmarks.loader import download_dataset, list_available_datasets

logger = logging.getLogger(__name__)


# ── Dataset resolution ─────────────────────────────────────────────────────────

_DATASET_ALIASES: dict[str, list[str]] = {
    "nq_trivia": ["nq", "triviaqa"],
    "popqa_hotpot": ["popqa", "hotpotqa"],
}

# Datasets whose EvaluationDataset generates samples in code; their
# ``load(path)`` ignores the path, so no file download/resolution is needed
# (previously the CLI tried to download "memory_benchmark" and failed with
# "Unknown dataset").
_SELF_GENERATED_DATASETS = frozenset({"memory_benchmark"})


def _resolve_datasets(dataset: str) -> list[str]:
    """Resolve 'all' or composite names to individual dataset names."""
    if dataset == "all":
        # Every runnable benchmark: file-backed datasets plus self-generated
        # ones (memory_benchmark needs no file). Composite aliases
        # (nq_trivia, popqa_hotpot) are excluded — their members are listed
        # individually already.
        return sorted(set(list_available_datasets()) | _SELF_GENERATED_DATASETS)
    if dataset in _DATASET_ALIASES:
        return _DATASET_ALIASES[dataset]
    return [dataset]


def _resolve_path(
    dataset: str,
    explicit_path: str | None,
    auto_download: bool,
    force_synthetic: bool = False,
) -> str:
    """Resolve dataset path: explicit path → cached file → download.

    Self-generated datasets (``memory_benchmark``) need no file: their
    ``EvaluationDataset.load`` builds samples in code and ignores the
    path, so an empty placeholder is returned.
    """
    if explicit_path:
        p = Path(explicit_path)
        if p.exists():
            return explicit_path
        raise FileNotFoundError(f"Dataset path not found: {explicit_path}")

    if dataset in _SELF_GENERATED_DATASETS:
        return ""

    if not auto_download:
        raise ValueError(
            f"No path provided for dataset '{dataset}'. "
            "Use --path or run without --no-auto-download to auto-download."
        )

    # Auto-download
    print(f"[download] Fetching {dataset} dataset...", file=sys.stderr)
    path = download_dataset(dataset, force_synthetic=force_synthetic)
    print(f"[download] Saved to: {path}", file=sys.stderr)
    return str(path)


# ── Benchmark run ─────────────────────────────────────────────────────────────


def run_benchmark_command(
    dataset: str,
    path: str | None,
    output: str = ".memplex/benchmarks/results.jsonl",
    warm: bool = True,
    retrieval_k: int = 10,
    parallel: bool = False,
    auto_download: bool = True,
    force_synthetic: bool = False,
    capture_traces: bool = True,
) -> dict[str, list[BenchmarkResult]]:
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
    force_synthetic:
        If True, skip HuggingFace downloads and generate synthetic data
        directly (forwarded to :func:`~benchmarks.loader.download_dataset`).

    Returns
    -------
    Dict[str, List[BenchmarkResult]]
        Per-dataset results.
    """
    svc = make_benchmark_service()
    svc.start()

    try:
        evaluator = BenchmarkEvaluator(
            svc,
            output_dir=str(Path(output).parent),
            output_file=Path(output).name,
            capture_traces=capture_traces,
        )

        # Resolve datasets
        dataset_names = _resolve_datasets(dataset)

        # Resolve paths
        if len(dataset_names) == 1 and path:
            # Single dataset with explicit path
            resolved_paths = {dataset_names[0]: path}
        elif len(dataset_names) == 1:
            resolved_paths = {
                dataset_names[0]: _resolve_path(
                    dataset_names[0], path, auto_download, force_synthetic
                )
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
                    if name in _SELF_GENERATED_DATASETS:
                        resolved_paths[name] = ""
                        continue
                    p = base / f"{name}.json"
                    if p.exists():
                        resolved_paths[name] = str(p)
                    elif auto_download:
                        print(f"[download] Fetching {name}...", file=sys.stderr)
                        resolved_paths[name] = str(
                            download_dataset(name, force_synthetic=force_synthetic)
                        )
                    else:
                        raise FileNotFoundError(f"Dataset not found: {p}")
            else:
                # Auto-download each (self-generated datasets resolve to "")
                resolved_paths = {}
                for name in dataset_names:
                    resolved_paths[name] = _resolve_path(
                        name, None, auto_download, force_synthetic
                    )

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

            if not p:
                print(f"    - {name}: generated in code (no dataset file)", file=sys.stderr)
                continue
            try:
                with open(p) as f:
                    raw = json.load(f)
                count = len(raw) if isinstance(raw, list) else 1
            except Exception:  # noqa: BLE001 - broad catch with explicit fallback handling
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


def _print_sample_scores(results: dict[str, list[BenchmarkResult]]) -> None:
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
        seen: dict[str, float] = {}
        for r in dataset_results:
            key = r.metric
            if key not in seen or r.value > seen[key]:
                seen[key] = r.value
        for metric, value in sorted(seen.items()):
            print(f"  {metric:30s}: {value:.4f}")
