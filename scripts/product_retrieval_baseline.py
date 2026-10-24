#!/usr/bin/env python3
"""Product-path retrieval-level baseline matrix + Raw-RAG control (P0.2).

Local, no API. Every question gets a FRESH store seeded with exactly its
own haystack (the repo's proven runner pattern - gap_forensics and the
product-parity lineage both work this way), so the four compositions
query the same per-question corpus:

  default       - service.query, orchestrated=False, default fusion
                  (paragraph fallback fill)
  orchestrated  - service.query, orchestrated=True, default fusion
  parity        - orchestrated=True, MEMPLEX_PARAGRAPH_FUSION=primary
                  (the product-parity composition)
  rawrag        - pure BM25 over the raw session texts (the Raw-RAG
                  control: same corpus, same top-k, no typed memories,
                  no service stack)

Metric: gold-session coverage in the top-24 per question - any (>=1)
and full (complete enumeration, what counting/aggregation questions
need), split by counting-intent (keyword heuristic, NOT ground truth).

Design note (2026-10-24, measured): an earlier variant seeded the
WHOLE pool union (5908 sessions) into ONE store behind a single
deferred commit. Three runs failed on scale - throughput collapsed
>=7x (16/min -> 4.5-6/min, 6h timeout without finishing seeding;
generational-GC traversals over the growing resident graph, only
partially mitigated by periodic gc.freeze). Per-question fresh stores
do the same total write count entirely in the fast small-store regime
(gap_forensics: 17 questions in 181s end to end).

Honest limits, disclosed in every output: retrieval-level instrument,
no answer accuracy; without a query-enhancement LLM the orchestrated
composition degrades to its fail-closed single-query fanout;
MEMPLEX_PRB_LIMIT=N truncates the pool for smoke runs only.
"""

from __future__ import annotations

import json
import os
import pathlib
import shutil
import sys
import tempfile

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
for _p in (str(_PROJECT_ROOT), str(_PROJECT_ROOT / "scripts")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from gap_forensics import (
    DATA,
    TOP_K,
    _norm,
    _session_text,
    counting_intent,
)

from memplex.config import MemplexConfig
from memplex.service import MemplexService
from memplex.storage.lite.search_index import local_bm25_search

COMPOSITIONS = ("default", "orchestrated", "parity", "rawrag")
OUT_DIR = _PROJECT_ROOT / "benchmarks/results/product-retrieval-baseline"


def _service(tmp: pathlib.Path) -> MemplexService:
    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(tmp / "s.sqlite3")
    config.llm.query_enhancement = False
    svc = MemplexService(config=config)
    svc.start()
    return svc


def _load_pool() -> list[dict]:
    with open(DATA) as fh:
        return [q for q in json.load(fh) if q.get("question_type") == "multi-session"]


def _sid_of_paragraph(svc: MemplexService, sids: list[str]) -> dict[str, str]:
    """Single O(paragraphs) pass: row ids -> seeded session ids."""
    mapping: dict[str, str] = {}
    wanted = set(sids)
    for row in svc.store._paragraphs.values():
        text = row.get("raw_text", "")
        if not text.startswith("["):
            continue
        sid = text[1:].split(" @", 1)[0]
        if sid in wanted:
            mapping[row["id"]] = sid
    return mapping


def _covered_by_results(
    results: list, sid_of_para: dict[str, str], gold_bodies: dict[str, str]
) -> set[str]:
    covered: set[str] = set()
    for r in results:
        sid = sid_of_para.get(r.func_id)
        if sid:
            covered.add(sid)
            continue
        text = _norm(r.summary or "")
        if len(text) < 40:
            continue
        for gold_sid, body in gold_bodies.items():
            if body[:80] and body[:80] in text:
                covered.add(gold_sid)
    return covered


def _question_row(q: dict) -> dict:
    """Seed one fresh store with this question's haystack, then run all
    four compositions against that same per-question corpus."""
    tmp = pathlib.Path(tempfile.mkdtemp(prefix=f"prb-{q['question_id'][:12]}-"))
    svc = _service(tmp)
    try:
        session_texts: dict[str, str] = {}
        with svc.store.deferred_commit():
            for sid, date, turns in zip(
                q["haystack_session_ids"], q["haystack_dates"], q["haystack_sessions"]
            ):
                if sid in session_texts:
                    continue
                session_texts[sid] = _session_text(turns)
                try:
                    svc.write_text(
                        f"[{sid} @ {date}] {session_texts[sid]}", source_type="text"
                    )
                except ValueError:
                    continue  # duplicate extraction id
        sid_of_para = _sid_of_paragraph(svc, list(session_texts))

        gold = list(q.get("answer_session_ids") or [])
        gold_bodies = {
            s: _norm(session_texts.get(s, "")) for s in gold if session_texts.get(s)
        }
        rec: dict = {
            "qid": q["question_id"],
            "gold_n": len(gold),
            "counting": counting_intent(q["question"]),
        }
        for comp in COMPOSITIONS:
            if comp == "rawrag":
                ranked = local_bm25_search(
                    text=q["question"],
                    functions={sid: sid for sid in session_texts},
                    text_factory=lambda sid: session_texts[sid],
                    top_k=TOP_K,
                )
                covered = {sid for sid, _t, _s in ranked if sid in gold_bodies}
            else:
                if comp == "parity":
                    os.environ["MEMPLEX_PARAGRAPH_FUSION"] = "primary"
                else:
                    os.environ.pop("MEMPLEX_PARAGRAPH_FUSION", None)
                result = svc.query(
                    q["question"], top_k=TOP_K, orchestrated=(comp != "default")
                )
                covered = _covered_by_results(
                    result.results[:TOP_K], sid_of_para, gold_bodies
                )
            rec[comp] = {
                "any": bool(covered),
                "full": bool(gold) and set(gold) <= covered,
                "hits": len(covered),
            }
        return rec
    finally:
        os.environ.pop("MEMPLEX_PARAGRAPH_FUSION", None)
        svc.stop()
        shutil.rmtree(tmp, ignore_errors=True)


def _rate(rows: list[dict], comp: str, *, counting: bool, key: str) -> float:
    subset = [r for r in rows if r["counting"] == counting]
    if not subset:
        return 0.0
    return sum(1 for r in subset if r[comp][key]) / len(subset)


def main() -> int:
    os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")
    os.environ.setdefault("MEMPLEX_LLM_QUERY_ENHANCEMENT", "false")
    os.environ.pop("MEMPLEX_PARAGRAPH_FUSION", None)

    questions = _load_pool()
    smoke_limit = int(os.environ.get("MEMPLEX_PRB_LIMIT", "0") or 0)
    if smoke_limit > 0:
        questions = questions[:smoke_limit]
        print(f"SMOKE MODE: pool truncated to {len(questions)} questions", flush=True)
    print(f"pool: {len(questions)} multi-session questions", flush=True)

    rows: list[dict] = []
    for idx, q in enumerate(questions):
        rows.append(_question_row(q))
        if (idx + 1) % 10 == 0:
            print(f"done {idx + 1}/{len(questions)}", flush=True)

    summary = {
        "benchmark": "product_retrieval_baseline",
        "n": len(rows),
        "top_k": TOP_K,
        "store_mode": "per-question fresh store (repo runner lineage)",
        "counting_classifier": (
            "keyword heuristic (scripts/run_lme_official_j.py), not ground truth"
        ),
        "limits": [
            "retrieval-level instrument: no answer accuracy (scored harness protocol separately)",
            "no query-enhancement LLM: orchestrated degrades to fail-closed single-query fanout",
            "MEMPLEX_PRB_LIMIT=N truncates the pool for smoke runs only; full runs must leave it unset",
        ],
        "compositions": {},
    }
    for comp in COMPOSITIONS:
        summary["compositions"][comp] = {
            "counting_n": sum(1 for r in rows if r["counting"]),
            "non_counting_n": sum(1 for r in rows if not r["counting"]),
            "any": {
                "counting": round(_rate(rows, comp, counting=True, key="any"), 4),
                "non_counting": round(_rate(rows, comp, counting=False, key="any"), 4),
            },
            "full": {
                "counting": round(_rate(rows, comp, counting=True, key="full"), 4),
                "non_counting": round(_rate(rows, comp, counting=False, key="full"), 4),
            },
        }

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "baseline.json").write_text(json.dumps(summary, indent=1))
    (OUT_DIR / "rows.json").write_text(json.dumps(rows, indent=1))

    print(
        f"\n{'composition':<14}{'any/cnt':>9}{'full/cnt':>10}{'any/nc':>9}{'full/nc':>9}"
    )
    for comp in COMPOSITIONS:
        c = summary["compositions"][comp]
        print(
            f"{comp:<14}{c['any']['counting']:>9.3f}{c['full']['counting']:>10.3f}"
            f"{c['any']['non_counting']:>9.3f}{c['full']['non_counting']:>9.3f}"
        )
    print(f"\nwrote {OUT_DIR / 'baseline.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
