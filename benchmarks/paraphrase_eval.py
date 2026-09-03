"""Paraphrase-robustness evaluation for the default lexical retriever.

Quantifies the lexical-vs-semantic gap: seeds the paraphrase dataset
(:mod:`benchmarks.paraphrase_data`) plus PopQA distractor documents into a
throwaway lite-backend store, runs every paraphrase query through
``MemplexService.query``, and reports recall@k (k=1,5,10) overall and
stratified by lexical-overlap level (high/medium/low).

Usage::

    .venv/bin/python benchmarks/paraphrase_eval.py
    .venv/bin/python benchmarks/paraphrase_eval.py --distractors 200 \
        --output .memplex/benchmarks/paraphrase_baseline.json

The store lives in a temporary directory (``MEMPLEX_STORAGE_PATH``) and is
deleted afterwards; ``MEMPLEX_STORAGE_BACKEND=lite`` and
``MEMPLEX_LLM_QUERY_ENHANCEMENT=false`` keep the run on the pure lexical path.
"""

from __future__ import annotations

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")
os.environ.setdefault("MEMPLEX_LLM_QUERY_ENHANCEMENT", "false")

import argparse
import json
import logging
import math
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

if __package__ in (None, ""):  # direct `python benchmarks/paraphrase_eval.py`
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks.paraphrase_data import DATASET_VERSION, FACTS, OVERLAP_LEVELS, QUERIES
from memplex.config import load_config
from memplex.models.memory import Function
from memplex.models.source import SourceDocument, SourceType

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_POPQA_PATH = REPO_ROOT / ".memplex" / "benchmarks" / "data" / "popqa_real_1000.jsonl"
DEFAULT_OUTPUT_PATH = REPO_ROOT / ".memplex" / "benchmarks" / "paraphrase_baseline.json"

#: Uniform creation timestamp for every seeded document so the reranker's
#: recency-decay dimension cannot confound the recall comparison.
SEED_TIMESTAMP = "2025-01-01T00:00:00"

TOP_KS = (1, 5, 10)


# ── Distractor loading ────────────────────────────────────────────────────────


def load_distractors(
    path: Path,
    facts: Sequence[Dict[str, str]],
    limit: int = 200,
) -> List[Dict[str, str]]:
    """Load PopQA entries as distractor documents, skipping subject collisions.

    A PopQA entry is skipped when its subject appears in any fact text, or any
    fact subject appears in its question — otherwise the "distractor" would be
    a legitimate second answer and distort the baseline.

    Each returned document: ``{"id": "distractor_NNNN", "text": "question object"}``.
    """
    fact_texts = [f["text"].lower() for f in facts]
    fact_subjects = [f["subject"].lower() for f in facts]

    distractors: List[Dict[str, str]] = []
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            if len(distractors) >= limit:
                break
            line = line.strip()
            if not line:
                continue
            entry = json.loads(line)
            subject = str(entry.get("subject", "")).strip()
            question = str(entry.get("question", "")).strip()
            obj = str(entry.get("object", "")).strip()
            if not subject or not question:
                continue
            subject_l = subject.lower()
            question_l = question.lower()
            if any(subject_l in ft for ft in fact_texts):
                continue
            if any(fs and fs in question_l for fs in fact_subjects):
                continue
            distractors.append(
                {
                    "id": f"distractor_{len(distractors) + 1:04d}",
                    "text": f"{question} {obj}".strip(),
                }
            )
    return distractors


# ── Seeding ───────────────────────────────────────────────────────────────────


def _seed_one(service: Any, doc_id: str, text: str) -> None:
    """Seed one document as a Function record with a deterministic ID.

    Follows the direct-seed pattern of ``MemoryBenchmarkRunner._seed_fact``:
    the searchable text for the lite backend is the Function name
    (``_function_to_search_text``), so the fact/distractor text goes there.
    """
    func = Function(
        id=doc_id,
        name=text,
        name_normalized=text.lower().strip().replace(" ", "_")[:100],
        domain=None,
        memory_type="function",
        source_type=SourceType.WIKI,
        created_at=SEED_TIMESTAMP,
        updated_at=SEED_TIMESTAMP,
    )
    source = SourceDocument(
        type="paraphrase_eval",
        content=text,
        source_type=SourceType.WIKI,
    )
    service.store.add(func, source)


def seed_documents(service: Any, documents: Iterable[Dict[str, str]]) -> int:
    """Seed ``{"id", "text"}`` documents into the service store."""
    count = 0
    for doc in documents:
        _seed_one(service, doc["id"], doc["text"])
        count += 1
    return count


# ── Metrics ───────────────────────────────────────────────────────────────────


def compute_recall(
    records: Sequence[Dict[str, Any]],
    ks: Sequence[int] = TOP_KS,
) -> Dict[str, Any]:
    """Compute recall@k overall and per overlap level.

    Each record: ``{"fact_id", "overlap", "retrieved_ids"}`` where
    ``retrieved_ids`` is the ranked ID list for that query. A query is a hit at
    k when its ``fact_id`` appears within the first k retrieved IDs.
    """

    def _hit_fraction(subset: Sequence[Dict[str, Any]], k: int) -> float:
        if not subset:
            return 0.0
        hits = sum(1 for r in subset if r["fact_id"] in r["retrieved_ids"][:k])
        return round(hits / len(subset), 4)

    overall = {f"recall@{k}": _hit_fraction(records, k) for k in ks}
    by_overlap: Dict[str, Any] = {}
    for level in OVERLAP_LEVELS:
        subset = [r for r in records if r["overlap"] == level]
        by_overlap[level] = {
            "n": len(subset),
            **{f"recall@{k}": _hit_fraction(subset, k) for k in ks},
        }
    return {
        "n": len(records),
        "overall": overall,
        "by_overlap": by_overlap,
    }


def _latency_summary(samples: Sequence[float]) -> Dict[str, float]:
    """Mean/p50/p99 (nearest-rank) in milliseconds over float-ms samples."""
    if not samples:
        return {"mean": 0.0, "p50": 0.0, "p99": 0.0}
    ordered = sorted(samples)

    def pct(q: float) -> float:
        rank = max(1, math.ceil(q / 100.0 * len(ordered)))
        return ordered[min(rank, len(ordered)) - 1]

    return {
        "mean": round(sum(samples) / len(samples), 3),
        "p50": round(pct(50), 3),
        "p99": round(pct(99), 3),
    }


# ── Evaluation loop ───────────────────────────────────────────────────────────


def run_queries(
    service: Any,
    queries: Sequence[Dict[str, Any]],
    top_k: int = max(TOP_KS),
) -> Dict[str, Any]:
    """Run every query and return per-query records plus latency stats."""
    records: List[Dict[str, Any]] = []
    latencies: List[float] = []
    for query in queries:
        start = time.perf_counter()
        result = service.query(query["text"], top_k=top_k)
        latencies.append((time.perf_counter() - start) * 1000.0)
        records.append(
            {
                "query_id": query["id"],
                "fact_id": query["fact_id"],
                "overlap": query["overlap"],
                "retrieved_ids": [r.func_id for r in result.results],
            }
        )
    return {"records": records, "latency_ms": _latency_summary(latencies)}


def build_service(storage_path: str) -> Any:
    """Build a MemplexService on the lite backend rooted at *storage_path*."""
    os.environ["MEMPLEX_STORAGE_PATH"] = storage_path
    config = load_config()
    config.storage.backend = "lite"
    config.storage.path = storage_path
    config.llm.query_enhancement = False
    from memplex.service import MemplexService

    return MemplexService(config=config)


def build_report(
    records: Sequence[Dict[str, Any]],
    latency_ms: Dict[str, float],
    num_distractors: int,
    ks: Sequence[int] = TOP_KS,
) -> Dict[str, Any]:
    """Assemble the JSON-serializable baseline report."""
    metrics = compute_recall(records, ks)
    return {
        "dataset": "paraphrase_robustness",
        "dataset_version": DATASET_VERSION,
        "backend": "lite",
        "retriever": "default lexical stack (TF-IDF embedder + FTS5/BM25 sidecar); "
        "no semantic embedding model",
        "num_facts": len(FACTS),
        "num_queries": len(QUERIES),
        "num_distractors": num_distractors,
        "ks": list(ks),
        "n": metrics["n"],
        "overall": metrics["overall"],
        "by_overlap": metrics["by_overlap"],
        "latency_ms": latency_ms,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── CLI ───────────────────────────────────────────────────────────────────────


def main(argv: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """Run the paraphrase baseline and write the JSON report."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--popqa", type=Path, default=DEFAULT_POPQA_PATH,
                        help="PopQA JSONL used for distractor documents")
    parser.add_argument("--distractors", type=int, default=200,
                        help="number of distractor documents to seed")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH,
                        help="where to write the JSON baseline report")
    parser.add_argument("--top-k", type=int, default=max(TOP_KS),
                        help="retrieval depth (must be >= max reported k)")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )
    if args.top_k < max(TOP_KS):
        parser.error(f"--top-k must be >= {max(TOP_KS)}")

    fact_docs = [{"id": f"parafact_{f['id']}", "text": f["text"]} for f in FACTS]
    fact_id_map = {f["id"]: f"parafact_{f['id']}" for f in FACTS}
    distractors = load_distractors(args.popqa, FACTS, limit=args.distractors)
    logger.info("seeding %d facts + %d distractors", len(fact_docs), len(distractors))

    with tempfile.TemporaryDirectory(prefix="memplex_paraphrase_") as tmpdir:
        service = build_service(tmpdir)
        try:
            seed_documents(service, fact_docs)
            seed_documents(service, distractors)

            queries = [
                {**q, "fact_id": fact_id_map[q["fact_id"]]} for q in QUERIES
            ]
            run = run_queries(service, queries, top_k=args.top_k)
        finally:
            service.stop()

    report = build_report(run["records"], run["latency_ms"], len(distractors))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False)
        handle.write("\n")

    print(json.dumps({"overall": report["overall"], "by_overlap": report["by_overlap"]}, indent=2))
    print(f"report written to {args.output}")
    return report


if __name__ == "__main__":
    main()
