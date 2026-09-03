#!/usr/bin/env python3
"""Run and verify strict G003 benchmark evidence bundles.

Two mutually exclusive run modes: ``--synthetic`` (explicitly generated
data, the E1 smoke path) and ``--data-dir`` (public-data mode). Public
mode is fail-closed: a dataset is accepted only as a pre-placed local
``<name>.json`` file or via the loader's HuggingFace map -- datasets
whose only resolution would be the synthetic generator are rejected with
an explicit error instead of silently substituting generated data.
"""

from __future__ import annotations

import argparse
import json
import random
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from benchmarks.base import BenchmarkResult
from benchmarks.benchmark_cli import run_benchmark_command
from benchmarks.evidence import create_bundle, verify_bundle
from benchmarks.loader import download_dataset

CONCRETE_DATASETS = (
    "hotpotqa",
    "locomo",
    "longmemeval",
    "memory_benchmark",
    "nq",
    "popqa",
    "triviaqa",
)
_FILE_DATASETS = frozenset(CONCRETE_DATASETS) - {"memory_benchmark"}
_TEMPORAL_DATASETS = frozenset({"hotpotqa", "longmemeval"})


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Create or verify G003 aggregate benchmark evidence."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="run fresh benchmarks")
    run.add_argument("--synthetic", action="store_true")
    run.add_argument("--data-dir", type=Path, default=None)
    run.add_argument("--num-samples", type=_positive_int, default=None)
    run.add_argument(
        "--dataset", required=True, choices=("all", *CONCRETE_DATASETS)
    )
    run.add_argument("--top-k", type=_positive_int, default=10)
    run.add_argument("--seed", type=int, default=17)
    run.add_argument("--run-dir", type=Path, required=True)

    verify = subparsers.add_parser("verify", help="verify an existing bundle")
    verify.add_argument("--run-dir", type=Path, required=True)
    return parser


def _coverage(datasets: Sequence[str], *, public: bool = False) -> dict[str, dict[str, str]]:
    kind = "Public" if public else "Synthetic"
    temporal_measured = any(name in _TEMPORAL_DATASETS for name in datasets)
    return {
        "retrieval": {
            "status": "passed",
            "reason": f"{kind} retrieval benchmark aggregates were produced.",
        },
        "temporal_multihop": {
            "status": "passed" if temporal_measured else "not_measured",
            "reason": (
                "{kind} temporal or multi-hop benchmark aggregates were produced."
                if temporal_measured
                else "The selected dataset does not measure temporal or multi-hop behavior."
            ),
        },
        "acl": {
            "status": "not_measured",
            "reason": f"{kind} retrieval benchmarks do not exercise ACL enforcement.",
        },
        "sync": {
            "status": "not_measured",
            "reason": f"{kind} retrieval benchmarks do not exercise synchronization.",
        },
        "latency_capacity": {
            "status": "not_measured",
            "reason": "Aggregate benchmark latency is not a capacity measurement.",
        },
        "recovery": {
            "status": "not_measured",
            "reason": f"{kind} retrieval benchmarks do not exercise recovery.",
        },
        "host_integration": {
            "status": "not_measured",
            "reason": f"{kind} retrieval benchmarks do not exercise host integration.",
        },
    }


def _recorded_command(args: argparse.Namespace) -> list[str]:
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "run",
    ]
    if args.data_dir is not None:
        command += ["--data-dir", str(args.data_dir)]
        if args.num_samples is not None:
            command += ["--num-samples", str(args.num_samples)]
    else:
        command.append("--synthetic")
    return command + [
        "--dataset",
        str(args.dataset),
        "--top-k",
        str(args.top_k),
        "--seed",
        str(args.seed),
        "--run-dir",
        str(args.run_dir),
    ]


def _resolve_public_dataset(name: str, data_dir: Path, num_samples: int | None) -> tuple[Path, str]:
    """Resolve one public dataset fail-closed; never generate synthetic.

    A dataset is accepted as a pre-placed ``<name>.json`` file in
    *data_dir* (source: local file) or through the loader's HuggingFace
    map (source: HuggingFace fetch into *data_dir*). Anything else --
    datasets whose only fallback would be the synthetic generator --
    raises instead of substituting.
    """
    from benchmarks.loader import HF_DATASET_IDS

    local = data_dir / f"{name}.json"
    if local.exists():
        return local, "public_local_file"
    if name not in HF_DATASET_IDS:
        raise ValueError(
            f"public run: dataset '{name}' has neither a local file "
            f"({local}) nor a HuggingFace mapping; refusing to silently "
            "substitute the synthetic generator"
        )
    # Call the HuggingFace fetch directly: download_dataset would silently
    # fall back to the synthetic generator when the fetch itself fails
    # (network error, removed dataset), which must never wear a public label.
    from benchmarks.loader import _fetch_from_huggingface

    fetched = _fetch_from_huggingface(name, num_samples=num_samples)
    if fetched is None:
        raise ValueError(
            f"public run: HuggingFace fetch for '{name}' failed; refusing "
            "to silently substitute the synthetic generator"
        )
    import shutil

    resolved = data_dir / f"{name}.json"
    shutil.move(str(fetched), resolved)
    return resolved, "public_huggingface"


def _run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    public = args.data_dir is not None
    if args.synthetic and public:
        parser.error("--synthetic and --data-dir are mutually exclusive")
    if not args.synthetic and not public:
        parser.error("run requires explicit --synthetic or --data-dir mode")

    run_dir: Path = args.run_dir
    if run_dir.exists() or run_dir.is_symlink():
        raise FileExistsError(run_dir)

    selected = CONCRETE_DATASETS if args.dataset == "all" else (args.dataset,)
    random.seed(args.seed)

    results: list[BenchmarkResult] = []
    dataset_files: list[dict[str, object]] = []
    warm_by_dataset: dict[str, bool] = {}
    data_dir = Path(tempfile.mkdtemp(prefix="memplex-g003-data-"))
    with tempfile.TemporaryDirectory(prefix="memplex-g003-results-") as temporary:
        output_dir = Path(temporary)

        for dataset in selected:
            if dataset in _FILE_DATASETS:
                if public:
                    dataset_path, source_kind = _resolve_public_dataset(
                        dataset, args.data_dir, args.num_samples
                    )
                    synthetic = False
                else:
                    dataset_path = download_dataset(
                        dataset,
                        str(data_dir),
                        force_synthetic=True,
                    )
                    source_kind = "generated_synthetic"
                    synthetic = True
                benchmark_path = str(dataset_path)
            else:
                dataset_path = data_dir / f"{dataset}.json"
                if public:
                    raise ValueError(
                        f"public run: '{dataset}' has no file-based public form"
                    )
                dataset_path.write_text(
                    json.dumps([{"id": f"{dataset}-generated-in-code"}]),
                    encoding="utf-8",
                )
                benchmark_path = ""
                source_kind = "generated_in_code"
                synthetic = True

            dataset_files.append(
                {
                    "path": str(dataset_path),
                    "source_kind": source_kind,
                    "synthetic": synthetic,
                }
            )
            warm_by_dataset[dataset] = dataset != "longmemeval"
            benchmark_results = run_benchmark_command(
                dataset=dataset,
                path=benchmark_path,
                output=str(output_dir / f"{dataset}.jsonl"),
                warm=warm_by_dataset[dataset],
                retrieval_k=args.top_k,
                parallel=False,
                auto_download=False,
                force_synthetic=not public,
            )
            dataset_results = [
                result
                for grouped_results in benchmark_results.values()
                for result in grouped_results
            ]
            if not dataset_results:
                raise ValueError(f"benchmark dataset '{dataset}' returned no results")
            results.extend(dataset_results)

        manifest = create_bundle(
            run_dir=run_dir,
            results=results,
            dataset_files=dataset_files,
            config={
                "dataset": args.dataset,
                "seed": args.seed,
                "synthetic": not public,
                "public": public,
                "top_k": args.top_k,
                "warm_by_dataset": warm_by_dataset,
            },
            coverage=_coverage(selected, public=public),
            command=_recorded_command(args),
            raw_status=None,
            raw_reason=(
                "Only aggregate BenchmarkResult values were retained; per-sample raw "
                "traces were not captured."
            ),
        )

    print(json.dumps({"evidence_level": manifest["evidence_level"]}, sort_keys=True))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.command == "verify":
        manifest = verify_bundle(args.run_dir)
        print(json.dumps({"evidence_level": manifest["evidence_level"]}, sort_keys=True))
        return 0
    return _run(args, parser)


if __name__ == "__main__":
    raise SystemExit(main())
