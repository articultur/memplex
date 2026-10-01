#!/usr/bin/env python3
"""Paired-diff run report: totals must never mask regressions.

Compares two official-J runs (summary.json + hypotheses.jsonl each) and
reports per question type: J before/after, the delta, and the per-question
flip counts - fixed (wrong->right), regressed (right->wrong), net-new.
The point of the format is the regressions column: a positive total J can
hide real regressions in a pool, so this report makes them visible.

Usage: python scripts/paired_diff_report.py BASE_RUN NEW_RUN [--out DIR]
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys


def _verdict_correct(value: object) -> bool:
    return str(value).strip().lower() in {"yes", "true", "1", "correct"}


def _load(run_dir: pathlib.Path) -> dict:
    summary = json.loads((run_dir / "summary.json").read_text())
    hypotheses_file = run_dir / "hypotheses.jsonl"
    per_question: dict[str, dict[str, object]] = {}
    if hypotheses_file.exists():
        for line in hypotheses_file.read_text().splitlines():
            if not line.strip():
                continue
            row = json.loads(line)
            qid = row.get("question_id")
            # autoeval_label is the field summaries score from;
            # judge_verdict is the raw judge text (they can differ by
            # label post-processing - the v131->v132 reconciliation
            # measured 6/5 flips on the label vs 8/5 on the raw text).
            verdict = row.get("autoeval_label")
            if verdict is None:
                verdict = row.get("judge_verdict")
            if qid is not None and verdict is not None:
                per_question[str(qid)] = {
                    "correct": _verdict_correct(verdict),
                    "type": row.get("question_type"),
                }
    return {"summary": summary, "per_question": per_question}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("base")
    parser.add_argument("new")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    base = _load(pathlib.Path(args.base))
    new = _load(pathlib.Path(args.new))
    base_types = base["summary"].get("by_type", {})
    new_types = new["summary"].get("by_type", {})

    types = sorted(set(base_types) | set(new_types))
    flips: dict[str, dict[str, int]] = {t: {"fixed": 0, "regressed": 0} for t in types}
    for qid, base_row in base["per_question"].items():
        new_row = new["per_question"].get(qid)
        if new_row is None:
            continue
        qtype = base_row.get("type") or new_row.get("type")
        if qtype not in flips:
            continue
        was = bool(base_row["correct"])
        now = bool(new_row["correct"])
        if not was and now:
            flips[qtype]["fixed"] += 1
        elif was and not now:
            flips[qtype]["regressed"] += 1

    rows = []
    for t in types:
        b = base_types.get(t, {"J": 0.0, "n": 0})
        n = new_types.get(t, {"J": 0.0, "n": 0})
        b_n, n_n = int(b.get("n", 0)), int(n.get("n", 0))
        b_correct = round(b.get("J", 0.0) * b_n)
        n_correct = round(n.get("J", 0.0) * n_n)
        rows.append({
            "type": t,
            "base_J": round(b.get("J", 0.0), 4),
            "new_J": round(n.get("J", 0.0), 4),
            "delta_J": round(n.get("J", 0.0) - b.get("J", 0.0), 4),
            "base_n": b_n,
            "new_n": n_n,
            "fixed": flips[t]["fixed"],
            "regressed": flips[t]["regressed"],
            "net_new": n_correct - b_correct,
        })

    b_total = base["summary"].get("J", 0.0)
    n_total = new["summary"].get("J", 0.0)
    total_row = {
        "type": "TOTAL",
        "base_J": round(b_total, 4),
        "new_J": round(n_total, 4),
        "delta_J": round(n_total - b_total, 4),
        "base_n": int(base["summary"].get("samples_judged", 0)),
        "new_n": int(new["summary"].get("samples_judged", 0)),
        "fixed": sum(r["fixed"] for r in rows),
        "regressed": sum(r["regressed"] for r in rows),
        "net_new": sum(r["net_new"] for r in rows),
    }

    report = {
        "benchmark": "paired_diff_report",
        "base_run": str(pathlib.Path(args.base)),
        "new_run": str(pathlib.Path(args.new)),
        "types": rows,
        "total": total_row,
    }
    print(
        "type                 baseJ   newJ    dJ     fixed regress net_new",
        flush=True,
    )
    for r in rows + [total_row]:
        print(
            f"{r['type']:<20} {r['base_J']:>7} {r['new_J']:>7} "
            f"{r['delta_J']:>+7} {r['fixed']:>5} {r['regressed']:>8} "
            f"{r['net_new']:>+7}",
            flush=True,
        )
    if args.out:
        out = pathlib.Path(args.out)
        out.mkdir(parents=True, exist_ok=True)
        (out / "paired-diff.json").write_text(json.dumps(report, indent=1))
    # A run that gains overall but regresses in any pool must exit 1 so
    # CI can treat masked regressions as failures.
    return 1 if any(r["regressed"] > 0 for r in rows) else 0


if __name__ == "__main__":
    raise SystemExit(main())
