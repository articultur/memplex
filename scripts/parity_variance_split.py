#!/usr/bin/env python3
"""Variance split for the 0.7669 parity reading.

Re-runs the answer (SC path) and judge N times over the STORED
retrieved contexts of the aligned parity run - zero seeding/embedding
machine time. Separates answer/judge run-variance from the single-shot
reading; the embedding-device component (MPS vs CPU) is explicitly NOT
covered and stays attributed.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import time
from concurrent.futures import ThreadPoolExecutor

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))

from product_parity_multi import Proxy
from run_lme_official_j import generate_answer, judge_prompt


def one_trial(row: dict, proxy) -> bool:
    qtype = row["question_type"]
    answer = generate_answer(
        proxy, qtype, row["question"], row["retrieved_context"], "unknown"
    )
    verdict = proxy.complete(
        judge_prompt(qtype, row["question"], row["answer"], answer, row["abstention"]),
        max_tokens=512,
        temperature=0.0,
    )
    return "yes" in verdict.lower()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument(
        "--source",
        default="benchmarks/results/po-parity-multi/hypotheses.jsonl",
    )
    parser.add_argument("--out", default="benchmarks/results/po-parity-variance")
    args = parser.parse_args()

    with open(args.source) as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    proxy = Proxy("glm-5.3")
    scores = []
    per_row = {r["question_id"]: [] for r in rows}
    for t in range(args.trials):
        with ThreadPoolExecutor(max_workers=6) as ex:
            labels = list(ex.map(lambda r: one_trial(r, proxy), rows))
        acc = sum(labels) / len(labels)
        scores.append(acc)
        for r, lab in zip(rows, labels):
            per_row[r["question_id"]].append(lab)
        print(f"trial {t + 1}: {acc:.4f}", flush=True)

    mean = sum(scores) / len(scores)
    spread = max(scores) - min(scores)
    stable = sum(1 for v in per_row.values() if len(set(v)) == 1)
    summary = {
        "benchmark": "parity_variance_split",
        "protocol": f"answer(SC)+judge re-run x{args.trials} over stored contexts; embedding/device variance NOT covered",
        "n": len(rows),
        "trial_accuracies": [round(s, 4) for s in scores],
        "mean": round(mean, 4),
        "spread": round(spread, 4),
        "stable_questions": stable,
        "flipping_questions": len(rows) - stable,
        "single_shot_reading": 0.7669,
    }
    print(json.dumps(summary, indent=1), flush=True)
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
