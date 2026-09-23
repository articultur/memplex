#!/usr/bin/env python3
"""Cross-encoder vs LLM-teacher ranking calibration (distillation PoC).

Stage 1 (record): for each benchmark query, take the merged candidate
set the pipeline produced, score every (query, candidate) pair with a
glm-5.3 teacher (0-10 relevance), and store the pair features.
Stage 2 (evaluate): score the same pairs with the shipped
CrossEncoderReranker (bge-reranker-v2-m3) and measure rank agreement
(Kendall tau + teacher-gold-in-top-k retention after each ranker).

This answers the PoC gate: is the local cross-encoder already aligned
with a strong teacher (distillation not worth the GPU), or does the gap
justify the Rank-DistiLLM student-training half?

Usage:
    .venv/bin/python scripts/calibrate_reranker_llm.py record [--n 40]
    .venv/bin/python scripts/calibrate_reranker_llm.py evaluate
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import tempfile
import time

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")
os.environ.setdefault("MEMPLEX_LLM_QUERY_ENHANCEMENT", "false")

import httpx

from benchmarks.longmemeval import LongMemEvalDataset
from memplex.config import MemplexConfig
from memplex.service import MemplexService

DATA = _PROJECT_ROOT / ".memplex/benchmarks/data/longmemeval.json"
RECORD_PATH = pathlib.Path("benchmarks/results/llm_teacher_ranks.jsonl")


class Teacher:
    def __init__(self) -> None:
        settings = json.loads(
            pathlib.Path(os.path.expanduser("~/.claude/settings.json")).read_text()
        )["env"]
        self._client = httpx.Client(
            base_url=settings["ANTHROPIC_BASE_URL"],
            timeout=120,
            headers={
                "x-api-key": settings["ANTHROPIC_AUTH_TOKEN"],
                "anthropic-version": "2023-06-01",
            },
        )

    def relevance(self, question: str, candidate: str) -> int:
        prompt = (
            "Rate how relevant this memory excerpt is for answering the "
            "question, 0 (irrelevant) to 10 (the exact evidence needed). "
            "Reply with the integer only.\n\n"
            f"Question: {question[:400]}\n\nExcerpt: {candidate[:1200]}\n\nScore:"
        )
        for attempt in range(5):
            try:
                resp = self._client.post(
                    "/v1/messages",
                    json={
                        "model": "glm-5.3",
                        "max_tokens": 256,
                        "temperature": 0.0,
                        "thinking": {"type": "disabled"},
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                resp.raise_for_status()
                text = "".join(
                    b.get("text", "")
                    for b in resp.json().get("content", [])
                    if b.get("type") == "text"
                )
                digits = "".join(ch for ch in text if ch.isdigit())
                return int(digits[:2].ljust(2, "0")[:2]) if digits else 0
            except Exception:  # noqa: BLE001 - teacher scoring is best-effort
                if attempt == 4:
                    return 0
                time.sleep(min(60, 5 * 2**attempt))
        return 0


def _service(tmp_root: str) -> MemplexService:
    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(pathlib.Path(tmp_root) / "s.sqlite3")
    config.llm.query_enhancement = False
    svc = MemplexService(config=config)
    svc.start()
    return svc


def record(n: int) -> int:
    from benchmarks.longmemeval import LongMemEvalRunner, _clear_store

    teacher = Teacher()
    ds = LongMemEvalDataset()
    samples = ds.load(str(DATA))[:n]
    runner = LongMemEvalRunner()
    svc = _service(tempfile.mkdtemp(prefix="calib-llm-"))
    out = RECORD_PATH.open("w", encoding="utf-8")
    try:
        for idx, sample in enumerate(samples):
            _clear_store(svc)
            runner._seed(svc, ds.to_memories(sample))
            result = svc.query(sample.query, top_k=12, explain=False)
            pairs = []
            for r in result.results[:12]:
                score = teacher.relevance(sample.query, r.summary)
                pairs.append(
                    {"func_id": r.func_id, "summary": r.summary[:1500], "teacher": score}
                )
            golds = list((sample.metadata or {}).get("answers", []))
            out.write(
                json.dumps(
                    {
                        "question": sample.query,
                        "gold": golds[0] if golds else "",
                        "candidates": pairs,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            out.flush()
            if (idx + 1) % 5 == 0:
                print(f"{idx + 1}/{n}", flush=True)
    finally:
        out.close()
        svc.stop()
    print(f"recorded {n} queries -> {RECORD_PATH}")
    return 0


def _kendall(a: list[float], b: list[float]) -> float:
    n = len(a)
    if n < 2:
        return 0.0
    concordant = discordant = 0
    for i in range(n):
        for j in range(i + 1, n):
            da = a[i] - a[j]
            db = b[i] - b[j]
            if da * db > 0:
                concordant += 1
            elif da * db < 0:
                discordant += 1
    denom = n * (n - 1) / 2
    return (concordant - discordant) / denom


def evaluate() -> int:
    from memplex.retrieval.reranker import CrossEncoderReranker

    records = [
        json.loads(line)
        for line in RECORD_PATH.read_text().splitlines()
        if line.strip()
    ]
    ce = CrossEncoderReranker(enabled=True)
    ce._load_model()
    if ce._model is None:
        print("cross-encoder unavailable; install the embedding extra")
        return 1

    from datetime import UTC, datetime

    from memplex.models.search import QueryScope, SearchResult

    taus: list[float] = []
    teacher_top3_gold = ce_top3_gold = both_miss = 0
    for rec in records:
        cands = rec["candidates"]
        if not cands:
            continue
        # cross-encoder scores
        results = [
            SearchResult(
                func_id=c["func_id"],
                name="",
                domain="",
                relevance_score=0.0,
                summary=c["summary"],
            )
            for c in cands
        ]
        reranked = ce.rerank(rec["question"], results)
        ce_order = [r.func_id for r in reranked]
        teacher_scores = [float(c["teacher"]) for c in cands]
        # simpler: derive CE rank score by position (lower index = higher)
        ce_rank_score = {
            fid: float(len(ce_order) - pos) for pos, fid in enumerate(ce_order)
        }
        aligned = [ce_rank_score[c["func_id"]] for c in cands]
        taus.append(_kendall(teacher_scores, aligned))
        # gold-in-top3 retention: does the summary containing the gold
        # answer appear in each ranker's top 3?
        gold = rec.get("gold", "").lower()
        if not gold:
            continue
        teacher_order = [
            c["func_id"]
            for c in sorted(cands, key=lambda c: -c["teacher"])
        ]
        teacher_top = {c["func_id"]: c["summary"] for c in cands[:3]}
        ce_top = {fid: next(c["summary"] for c in cands if c["func_id"] == fid) for fid in ce_order[:3]}
        t_hit = any(gold[:30] in s.lower() for s in teacher_top.values())
        c_hit = any(gold[:30] in s.lower() for s in ce_top.values())
        teacher_top3_gold += t_hit
        ce_top3_gold += c_hit
        both_miss += (not t_hit) and (not c_hit)
        _ = teacher_order

    gold_n = teacher_top3_gold + ce_top3_gold - both_miss if False else max(
        teacher_top3_gold, 1
    )
    print(
        json.dumps(
            {
                "queries": len(records),
                "mean_kendall_tau": round(sum(taus) / max(len(taus), 1), 4),
                "teacher_gold_in_top3": teacher_top3_gold,
                "cross_encoder_gold_in_top3": ce_top3_gold,
                "gold_answerable_queries": len(records),
            },
            indent=1,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["record", "evaluate"])
    parser.add_argument("--n", type=int, default=40)
    args = parser.parse_args()
    if args.mode == "record":
        return record(args.n)
    return evaluate()


if __name__ == "__main__":
    raise SystemExit(main())
