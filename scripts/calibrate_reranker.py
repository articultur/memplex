#!/usr/bin/env python3
"""Record per-dimension reranker features during a paraphrase run, then
search weights offline.

The live run records, for every query and every merged candidate, the six
per-dimension scores the Reranker computes plus the ground-truth hit flag
and the deterministic tie-break key. Weight search afterwards is pure
arithmetic over the recorded features -- no further live runs needed.

Usage::

    .venv/bin/python scripts/calibrate_reranker.py record
    .venv/bin/python scripts/calibrate_reranker.py search
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

RECORD_PATH = Path(
    os.environ.get("CALIBRATION_RECORD", ".memplex/benchmarks/reranker_features.json")
)

DIMS = (
    "raw_relevance",
    "semantic_similarity",
    "recency_decay",
    "source_authority",
    "frequency",
    "confidence",
)


def record() -> None:
    os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")
    os.environ.setdefault("MEMPLEX_LLM_QUERY_ENHANCEMENT", "false")
    os.environ["MEMPLEX_STORAGE_PATH"] = tempfile.mkdtemp(prefix="calib-store-")

    from benchmarks.paraphrase_data import QUERIES, fact_by_id
    from memplex.retrieval import reranker as reranker_module

    # Patch the weighted-sum step to record per-dimension features. The
    # original scoring logic is preserved verbatim -- only the recording
    # side-effect is added.
    features: list[dict[str, object]] = []

    original_rerank = reranker_module.Reranker.rerank

    def recording_rerank(self, query, results, top_k=10, query_vector=None):
        from datetime import datetime, timezone

        if not results:
            return []
        if query_vector is None:
            query_vector = self._embed_query_text(query)
        node_map = {}
        if self.storage is not None:
            unique_ids = list({r.func_id for r in results})
            for fid in unique_ids:
                try:
                    node = self.storage.get(fid)
                    if node is not None:
                        node_map[fid] = node
                except Exception:  # noqa: BLE001 - feature recording must not fail the live run
                    node_map[fid] = None
        per_query = []
        for r in results:
            raw_score = r.relevance_score
            result_vector = (
                r.vector_cache
                if r.vector_cache is not None
                else self._embed_query_text(r.summary)
            )
            semantic = reranker_module.cosine_similarity(query_vector, result_vector)
            recency = self._recency_decay(r.updated_at)
            source_weight = self._source_weight(r.source_type)
            func = node_map.get(r.func_id)
            frequency = self._frequency_score(func) if func else 0.5
            confidence = self._confidence_score(func)
            per_query.append(
                {
                    "func_id": r.func_id,
                    "dims": [
                        raw_score,
                        semantic,
                        recency,
                        source_weight,
                        frequency,
                        confidence,
                    ],
                    "tiebreak": reranker_module._recency_timestamp(r.updated_at),
                }
            )
        features.append({"query_features": per_query, "n_results": len(results)})
        return original_rerank(self, query, results, top_k=top_k, query_vector=query_vector)

    reranker_module.Reranker.rerank = recording_rerank

    # Import after patching so the service sees the patched class method.
    from benchmarks.paraphrase_data import FACTS
    from benchmarks.paraphrase_eval import (
        DEFAULT_POPQA_PATH,
        load_distractors,
        run_queries,
        seed_documents,
    )
    from memplex.service import MemplexService

    documents = [{"id": f["id"], "text": f["text"]} for f in FACTS]
    documents += load_distractors(DEFAULT_POPQA_PATH, FACTS, limit=200)

    svc = MemplexService()
    svc.start()
    try:
        seed_documents(svc, documents)
        run_queries(svc, [{"text": q["text"], **q} for q in QUERIES])
    finally:
        svc.stop()
        reranker_module.Reranker.rerank = original_rerank

    # The service calls rerank once per query, so recorded calls align with
    # QUERIES order; a query whose rerank was skipped (empty candidates)
    # simply drops out of the calibration set.
    out = []
    for i, feature in enumerate(features):
        expected = fact_by_id(QUERIES[i]["fact_id"])["id"]
        for candidate in feature["query_features"]:
            candidate["expected"] = candidate["func_id"] == expected
        out.append(
            {
                "query_index": i,
                "overlap": QUERIES[i]["overlap"],
                "candidates": feature["query_features"],
            }
        )
    RECORD_PATH.parent.mkdir(parents=True, exist_ok=True)
    RECORD_PATH.write_text(json.dumps(out), encoding="utf-8")
    print(f"recorded {len(out)} queries, {sum(len(q['candidates']) for q in out)} candidates -> {RECORD_PATH}")


def _recall_at_1(entries: list[dict], weights: list[float]) -> float:
    hits = 0
    for entry in entries:
        ranked = sorted(
            entry["candidates"],
            key=lambda c: (
                -sum(w * d for w, d in zip(weights, c["dims"])),
                -c["tiebreak"],
                c["func_id"],
            ),
        )
        if ranked and ranked[0]["expected"]:
            hits += 1
    return hits / len(entries)


def search() -> None:
    entries = json.loads(RECORD_PATH.read_text(encoding="utf-8"))
    baseline = [0.25, 0.30, 0.15, 0.10, 0.10, 0.10]
    print("baseline recall@1:", round(_recall_at_1(entries, baseline), 4))

    # Coordinate descent with normalization, exploring multiplicative moves.
    import itertools
    import random

    rng = random.Random(17)

    def normalized(vec: list[float]) -> list[float]:
        total = sum(vec)
        return [v / total for v in vec]

    def score(vec: list[float]) -> float:
        return _recall_at_1(entries, normalized(vec))

    current = list(baseline)
    best = score(current)
    steps = [0.5, 0.75, 1.25, 1.5, 2.0]
    for _pass in range(3):
        improved = False
        for dim in range(6):
            for step in steps:
                candidate = list(current)
                candidate[dim] *= step
                value = score(candidate)
                if value > best + 1e-9:
                    best, current, improved = value, normalized(candidate), True
        if not improved:
            break
    print("best weights:", {d: round(w, 4) for d, w in zip(DIMS, normalized(current))})
    print("best recall@1:", round(best, 4))
    by_overlap: dict[str, list] = {}
    for entry in entries:
        by_overlap.setdefault(entry["overlap"], []).append(entry)
    final = normalized(current)
    for level, subset in sorted(by_overlap.items()):
        print(
            f"  {level:6s} n={len(subset)} baseline@1={_recall_at_1(subset, baseline):.4f}"
            f" calibrated@1={_recall_at_1(subset, final):.4f}"
        )


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "search"
    if mode == "record":
        record()
    else:
        search()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
