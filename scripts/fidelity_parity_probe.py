#!/usr/bin/env python3
"""ADR-013 Stage 2 acceptance: fidelity parity probe.

Paired 20-question retrieval probe on the PRODUCT path (write_text ->
orchestrated query), contrasting the raw-paragraph layer off vs on:
  - base arm: MEMPLEX_RAW_PARAGRAPH_LAYER=0 (extraction nodes only)
  - raw arm : layer on (verbatim paragraphs join both retrieval legs)
Metric: session-level gold-evidence recall@8. The 0.797 -> >=0.8045
J-score parity target lives at the answer level; this probe measures
its retrieval precondition (the answer-bearing unit being retrievable
from the product path), which is the only face the raw layer changes.

Single process, two phases: the env toggle is read at write time by
each fresh service, so flipping it between phases is sufficient.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import random
import sys
import tempfile

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")
os.environ.setdefault("MEMPLEX_LLM_QUERY_ENHANCEMENT", "false")
os.environ.setdefault("MEMPLEX_EMBEDDING_MODEL", "bge-m3")
os.environ.setdefault("MEMPLEX_EMBEDDING_DIMENSION", "1024")
os.environ.setdefault("MEMPLEX_EMBEDDING_DEVICE", "mps")
os.environ.setdefault("PYTORCH_MPS_HIGH_WATERMARK_RATIO", "0.0")

from memplex.config import load_config
from memplex.service import MemplexService

DATA = _PROJECT_ROOT / ".memplex/benchmarks/data/longmemeval_s_cleaned.json"


def run_arm(questions: list[dict], arm: str) -> list[dict]:
    rows = []
    for qi, q in enumerate(questions):
        store_dir = pathlib.Path(tempfile.mkdtemp(prefix=f"fp-{arm}{qi}-"))
        cfg = load_config()
        cfg.storage.backend = "lite"
        cfg.storage.path = str(store_dir / "s.sqlite3")
        cfg.llm.query_enhancement = False
        svc = MemplexService(config=cfg)
        svc.start()
        try:
            with svc.store.deferred_commit():
                for sid, date, turns in zip(
                    q["haystack_session_ids"], q["haystack_dates"], q["haystack_sessions"]
                ):
                    text = "\n".join(
                        f"{t.get('role', 'user')}: {t.get('content', '')}" for t in turns
                    )
                    try:
                        svc.write_text(f"[{sid} @ {date}] {text}", source_type="text")
                    except ValueError:
                        continue  # duplicate extraction id: memory already stored
            result = svc.query(q["question"], top_k=8, orchestrated=True, explain=False)
            recall = "\n".join(r.summary for r in result.results[:8])
            gold_ids = q["answer_session_ids"]
            hit = any(f"[{gid} @" in recall for gid in gold_ids)
            rows.append({"qid": q["question_id"], "hit": hit})
            print(f"  {arm} {qi + 1}/{len(questions)} {q['question_id']} hit={hit}", flush=True)
        finally:
            svc.stop()
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=20)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument(
        "--arm",
        choices=("both", "base", "raw"),
        default="both",
        help="run one arm only; existing arm files are kept",
    )
    parser.add_argument("--out", default="benchmarks/results/fidelity-parity-probe")
    args = parser.parse_args()

    with open(DATA) as fh:
        data = json.load(fh)
    # Seeded sampler for reproducible probe selection (same seed and
    # pool as the C2 probe for comparability) - not a cryptographic
    # randomness source.
    rng = random.Random(args.seed)
    questions = rng.sample(data, args.n)

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    results = {}
    arms = (("base", "0"), ("raw", "1")) if args.arm == "both" else ((args.arm, "0" if args.arm == "base" else "1"),)
    for arm, toggle in arms:
        os.environ["MEMPLEX_RAW_PARAGRAPH_LAYER"] = toggle
        print(f"=== arm {arm} (MEMPLEX_RAW_PARAGRAPH_LAYER={toggle}) ===", flush=True)
        rows = run_arm(questions, arm)
        (out / f"{arm}.json").write_text(json.dumps(rows, indent=1))
        results[arm] = sum(r["hit"] for r in rows) / max(len(rows), 1)
    os.environ.pop("MEMPLEX_RAW_PARAGRAPH_LAYER", None)

    existing = out / "summary.json"
    prior = json.loads(existing.read_text()) if existing.exists() else {}
    base_val = results.get("base", prior.get("base_recall8"))
    raw_val = results.get("raw", prior.get("raw_recall8"))
    summary = {
        "benchmark": "fidelity_parity_probe",
        "design": "product write_text -> orchestrated query, 20 questions seed 17; base = extraction nodes only, raw = verbatim paragraph layer joins retrieval; metric session-level gold recall@8",
        "n": args.n,
        "base_recall8": round(base_val, 4) if base_val is not None else None,
        "raw_recall8": round(raw_val, 4) if raw_val is not None else None,
        "delta": round(raw_val - base_val, 4) if (raw_val is not None and base_val is not None) else None,
    }
    print(json.dumps(summary, indent=1), flush=True)
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
