#!/usr/bin/env python3
"""Product-gap forensics: where do the 13 net questions die? (review phase 3)

The harness multi-session pool scores 115/133 (0.8647); the product
write_text path scores 102/133 (0.7669) on the same questions with the
same answerer. This script re-runs ONLY the product-side retrieval stack
locally (no API) for every harness-only-correct question and attributes
the loss to the first failing stage:

  write      - a gold-evidence session has no paragraph in the store
               (the write path dropped it);
  retrieval  - gold sessions are stored but none surface in the top-24
               orchestrated results;
  context    - gold sessions surface but the assembled answer context
               (raw-first, budget-truncated) lacks the gold answer text
               - including the harness's COUNT_FULL full-text prepend,
               which the product path has no equivalent of;
  answerer   - the context contains the gold answer text and the loss is
               on the generation side.
"""

from __future__ import annotations

import json
import os
import pathlib
import re
import sys
import tempfile

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")
os.environ.setdefault("MEMPLEX_LLM_QUERY_ENHANCEMENT", "false")
os.environ.setdefault("MEMPLEX_PARAGRAPH_FUSION", "primary")

from memplex.config import MemplexConfig
from memplex.service import MemplexService

DATA = _PROJECT_ROOT / ".memplex/benchmarks/data/longmemeval_s_cleaned.json"
TOP_K = 24
CONTEXT_BUDGET = 20000

HARNESS_RUN = (
    _PROJECT_ROOT / "benchmarks/results/lme-j500-recheck-multi/hypotheses.jsonl"
)
PRODUCT_RUN = (
    _PROJECT_ROOT / "docs/evidence/product-parity-primary/shard0.jsonl"
)


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]", "", text.lower()).strip()


def _session_text(session: list[dict]) -> str:
    return "\n".join(
        f"{t.get('role', 'user')}: {t.get('content', '')}" for t in session
    )


def _gap_questions() -> list[str]:
    harness = {}
    # split on \n only: U+2028/2029 inside answers are legal JSON
    # string chars that splitlines() would over-split into torn lines.
    for line in HARNESS_RUN.read_text().split("\n"):
        if line.strip():
            r = json.loads(line)
            harness[r["question_id"].replace("longmemeval-", "")] = bool(
                r["autoeval_label"]
            )
    product = {}
    for line in PRODUCT_RUN.read_text().split("\n"):
        if line.strip():
            r = json.loads(line)
            product[r["qid"]] = bool(r["correct"])
    both = set(harness) & set(product)
    return sorted(q for q in both if harness[q] and not product[q])


def _build_service(tmp: pathlib.Path) -> MemplexService:
    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(tmp / "s.sqlite3")
    config.llm.query_enhancement = False
    svc = MemplexService(config=config)
    svc.start()
    return svc


def _seed(svc: MemplexService, q: dict) -> None:
    with svc.store.deferred_commit():
        for sid, date, turns in zip(
            q["haystack_session_ids"], q["haystack_dates"], q["haystack_sessions"]
        ):
            text = _session_text(turns)
            try:
                svc.write_text(f"[{sid} @ {date}] {text}", source_type="text")
            except ValueError:
                continue  # duplicate extraction id


def forensic(q: dict) -> dict:
    gold_sids = list(q.get("answer_session_ids") or [])
    gold_answer = _norm(str(q.get("answer") or ""))
    tmp = pathlib.Path(tempfile.mkdtemp(prefix=f"gf-{q['question_id']}-"))
    svc = _build_service(tmp)
    try:
        _seed(svc, q)

        # Stage A: write preservation - every gold session's text lives
        # in the paragraph layer (seeded with "[sid @ date]" prefixes).
        paragraphs = list(svc.store._paragraphs.values())
        stored = {}
        for sid in gold_sids:
            stored[sid] = any(
                row["raw_text"].startswith(f"[{sid} @") for row in paragraphs
            )
        write_lost = [s for s, ok in stored.items() if not ok]

        # Stage B: retrieval recall - any top-24 result covering a gold
        # session: a paragraph hit resolves to its sid via the seeded
        # prefix; an extraction digest counts when its text overlaps the
        # normalized session body.
        result = svc.query(q["question"], top_k=TOP_K, orchestrated=True, explain=False)
        top = result.results[:TOP_K]
        sid_of_para = {
            row["id"]: sid
            for sid in gold_sids
            for row in paragraphs
            if row["raw_text"].startswith(f"[{sid} @")
        }
        body_by_sid = {}
        for i, sid in enumerate(q["haystack_session_ids"]):
            if sid in gold_sids and i < len(q["haystack_sessions"]):
                body_by_sid[sid] = _norm(_session_text(q["haystack_sessions"][i]))
        covered = set()
        for r in top:
            sid = sid_of_para.get(r.func_id)
            if sid:
                covered.add(sid)
                continue
            text = _norm(r.summary or "")
            if len(text) < 40:
                continue
            for sid in gold_sids:
                body = body_by_sid.get(sid, "")
                if body[:80] and body[:80] in text:
                    covered.add(sid)
        retrieval_lost = [s for s in gold_sids if s not in covered and stored.get(s)]

        # Stage C: context containment - the runner's raw-first context
        # assembly under its budget.
        para_by_id = {row["id"]: row for row in paragraphs}
        parts = []
        for r in top:
            para = para_by_id.get(r.func_id)
            parts.append(para["raw_text"] if para else r.summary)
        context = "\n\n".join(parts)[:CONTEXT_BUDGET]
        context_has_answer = bool(gold_answer) and len(gold_answer) >= 3 and (
            gold_answer in _norm(context)
        )

        if write_lost and len(write_lost) == len(gold_sids):
            stage = "write"
        elif not covered and gold_sids:
            stage = "retrieval"
        elif not context_has_answer:
            stage = "context"
        else:
            stage = "answerer"
        return {
            "qid": q["question_id"],
            "gold_sids": gold_sids,
            "write_lost": write_lost,
            "retrieved_gold": sorted(covered),
            "context_has_answer": context_has_answer,
            "stage": stage,
        }
    finally:
        svc.stop()


def main() -> int:
    gap = set(_gap_questions())
    with open(DATA) as fh:
        data = [
            q
            for q in json.load(fh)
            if q.get("question_type") == "multi-session" and q["question_id"] in gap
        ]
    print(f"forensics on {len(data)} harness-only-correct questions", flush=True)
    rows = [forensic(q) for q in data]
    import collections

    counts = collections.Counter(r["stage"] for r in rows)
    summary = {
        "benchmark": "product_gap_forensics",
        "n": len(rows),
        "stage_counts": dict(counts),
        "rows": rows,
    }
    print(json.dumps({k: v for k, v in summary.items() if k != "rows"}, indent=1))
    out = pathlib.Path("benchmarks/results/gap-forensics")
    out.mkdir(parents=True, exist_ok=True)
    (out / "records.json").write_text(json.dumps(rows, indent=1))
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
