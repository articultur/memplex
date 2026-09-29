#!/usr/bin/env python3
"""Official LAFS computation for Memplex's LongMemEval-V2 points.

Vendors the LAFS algorithm verbatim from the official repository
(xiaowu0162/LongMemEval-V2, leaderboard/compute_lafs.py, Apache-2.0):
LAFS = average of best-accuracy-under-budget over log-uniform latency
budgets in [1s, 200s]; a submission's gain is LAFS(ref ∪ submission) −
LAFS(ref). Reference frontier points are hard-coded from the paper's
main results table, per tier.

Our operating points come from the evidence manifests (accuracy in
percentage points at the recorded per-question p50 latency).
"""

from __future__ import annotations

import itertools
import json
import math
import pathlib
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Point:
    name: str
    acc: float  # accuracy in percentage points, e.g. 74.9
    latency: float  # query latency in seconds


T_MIN = 1.0
T_MAX = 200.0
FLOOR_ACC = 0.0

FIXED_FRONTIER_POINTS: dict[str, list[Point]] = {
    "small": [
        Point("RAG: query -> slice + notes", acc=51.0, latency=0.2),
        Point("Codex", acc=69.9, latency=177.2),
        Point("AgentRunbook-R", acc=58.6, latency=26.9),
        Point("AgentRunbook-C", acc=74.9, latency=108.3),
    ],
    "medium": [
        Point("RAG: query -> slice + notes", acc=45.9, latency=0.3),
        Point("Codex", acc=68.7, latency=185.8),
        Point("AgentRunbook-R", acc=57.0, latency=25.8),
        Point("AgentRunbook-C", acc=70.1, latency=139.9),
    ],
}


def pareto_frontier(points: list[Point]) -> list[Point]:
    sorted_points = sorted(points, key=lambda p: (p.latency, -p.acc))
    frontier: list[Point] = []
    best_acc = -float("inf")
    for point in sorted_points:
        if point.acc > best_acc:
            frontier.append(point)
            best_acc = point.acc
    return frontier


def best_acc_under_budget(
    points: list[Point], budget: float, floor_acc: float = FLOOR_ACC
) -> float:
    valid = [point.acc for point in points if point.latency <= budget]
    return max(valid) if valid else floor_acc


def lafs(
    points: list[Point],
    t_min: float = T_MIN,
    t_max: float = T_MAX,
    floor_acc: float = FLOOR_ACC,
) -> float:
    frontier = pareto_frontier(points)
    breakpoints = {t_min, t_max}
    for point in frontier:
        if t_min < point.latency < t_max:
            breakpoints.add(point.latency)
    breakpoints = sorted(breakpoints)
    denom = math.log(t_max / t_min)
    area = 0.0
    for left, right in itertools.pairwise(breakpoints):
        acc = best_acc_under_budget(frontier, left, floor_acc=floor_acc)
        area += acc * math.log(right / left)
    return area / denom


def summary_for_submission(
    tier: str, submission_points: list[Point]
) -> dict[str, Any]:
    reference = FIXED_FRONTIER_POINTS[tier]
    reference_lafs = lafs(reference)
    combined = lafs(reference + submission_points)
    return {
        "tier": tier,
        "t_min_seconds": T_MIN,
        "t_max_seconds": T_MAX,
        "reference_lafs": round(reference_lafs, 4),
        "submission_lafs": round(combined, 4),
        "lafs_gain": round(combined - reference_lafs, 4),
        "submission_points": [
            {"name": p.name, "accuracy": p.acc, "latency_seconds": p.latency}
            for p in submission_points
        ],
        "reference_frontier": [
            {"name": p.name, "accuracy": p.acc, "latency_seconds": p.latency}
            for p in pareto_frontier(reference)
        ],
    }


def main() -> int:
    # Memplex operating points (evidence: docs/evidence/
    # lme2-v2c-enterprise-full, lme2-v2b-web-mm-full text arm).
    submissions = {
        # Cleanest single-protocol point: run_lme_v2.py enterprise-small
        # full run, accuracy 0.1896 at per-question p50 3.331s.
        "small": [
            Point(
                "Memplex text-projection (enterprise, run_lme_v2 protocol)",
                acc=18.96,
                latency=3.331,
            ),
        ],
        "medium": [
            Point(
                "Memplex text-projection union-store (enterprise)",
                acc=19.43,
                latency=34.956,
            ),
            Point(
                "Memplex text-projection union-store (web)",
                acc=19.58,
                latency=2.296,
            ),
        ],
    }
    report = {
        "benchmark": "lme_v2_lafs",
        "source": "official compute_lafs.py algorithm vendored (Apache-2.0)",
        "tiers": {tier: summary_for_submission(tier, pts) for tier, pts in submissions.items()},
    }
    print(json.dumps(report, indent=1))
    out = pathlib.Path("benchmarks/results/lme2-lafs")
    out.mkdir(parents=True, exist_ok=True)
    (out / "summary.json").write_text(json.dumps(report, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
