#!/usr/bin/env python3
"""STALE-style implicit-invalidation baseline over the real product path.

Measures how often retrieval still surfaces a memory that a later
observation has implicitly invalidated (no explicit negation anywhere).
Three outcomes per scenario:
  - stale_reuse   : the OLD value surfaces in recall (the failure mode)
  - resolved      : the NEW value surfaces and the old one does not
  - both          : both surface (memory layer did not resolve; any
                    correctness would be model-side luck)

Scenario shape: seed fact -> later session writes an observation that
implicitly invalidates it (moving cities without saying "no longer"),
then probe with a neutral question.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import tempfile

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")
os.environ.setdefault("MEMPLEX_LLM_QUERY_ENHANCEMENT", "false")

from memplex.config import load_config
from memplex.service import MemplexService

SCENARIOS = [
    ("Where do I live?", "I live in Berlin with my two cats.", "The moving boxes finally arrived at our new flat in Vienna.", "Berlin", "Vienna"),
    ("Which company do I work for?", "I work at Nordwind Logistics as a planner.", "First week at Halden Marine - onboarding done.", "Nordwind", "Halden"),
    ("What phone do I use?", "My daily phone is a Pixel 8.", "Setting up my new iPhone as the primary device tonight.", "Pixel", "iPhone"),
    ("Who is my manager?", "My manager is Tobias Ree.", "Reporting to Elena Marsh now that the reorg landed.", "Tobias", "Elena"),
    ("What language am I learning?", "I am learning Japanese this year.", "Switched my evening class to Portuguese - level A2.", "Japanese", "Portuguese"),
    ("Where is my home office?", "My home office is in the attic room.", "The new garden studio is wired up, moving the desk there.", "attic", "garden studio"),
    ("Which gym do I go to?", "I train at Ironhaus on Elm Street.", "First session at Riverside Fitness with the new membership.", "Ironhaus", "Riverside"),
    ("What car do I drive?", "I drive a blue Volvo V60.", "Picked up the silver Skoda from the dealer today.", "Volvo", "Skoda"),
    ("What is my main project?", "My main project is the billing migration.", "All my focus is on the observability rollout this quarter.", "billing", "observability"),
    ("Which bank do I use?", "I do my banking with Sparkasse.", "Moved my accounts to Merkur Bank last week.", "Sparkasse", "Merkur"),
    ("What coffee machine do I have?", "We have a DeLonghi espresso machine.", "The new Jura arrived and the DeLonghi is boxed up for the neighbours.", "DeLonghi", "Jura"),
    ("Where do I volunteer?", "I volunteer at the animal shelter on Saturdays.", "Started coaching the youth football team instead this season.", "animal shelter", "youth football"),
    ("What is my commute?", "I commute by bike, twenty minutes each way.", "Taking the new metro line since they opened the station.", "bike", "metro"),
    ("Which editor do I use?", "I use Neovim for everything.", "Fully switched over to Zed this month.", "Neovim", "Zed"),
    ("What insurance do I have?", "My health insurance is with AOK.", "Switched to Barmer through the employer portal.", "AOK", "Barmer"),
    ("Where do I keep my notes?", "I keep all notes in Obsidian vaults.", "Migrating everything into the company Notion workspace.", "Obsidian", "Notion"),
    ("What band do I follow?", "I follow The Paper Kites and see them live whenever possible.", "Deep into GYGI lately, booked three of their shows.", "Paper Kites", "GYGI"),
    ("What's my diet?", "I eat vegetarian at home.", "Started including fish again after the nutritionist visit.", "vegetarian", "fish"),
    ("Which podcast do I listen to?", "My daily listen is The Daily.", "Binged my way into Search Engine instead - it's my commute staple now.", "The Daily", "Search Engine"),
    ("Where do I get my hair cut?", "I get my hair cut at Salon Brunnen.", "New place called Figaro on the corner does me now.", "Brunnen", "Figaro"),
    ("What timezone do I work in?", "I work Central European Time.", "Fully shifted to the Singapore schedule with the new team.", "Central European", "Singapore"),
    ("Which Linux distro do I run?", "My laptop runs Arch Linux.", "Reformatted everything to NixOS over the weekend.", "Arch", "NixOS"),
    ("What's my running goal?", "I'm training for a 10k in autumn.", "Signed up for the half marathon instead - the 10k sold out.", "10k", "half marathon"),
    ("Where do I buy coffee beans?", "I buy beans at the roastery on Main Street.", "Ordering from theSubscriptionRoaster since they opened shipping here.", "Main Street", "SubscriptionRoaster"),
    ("Who is my dentist?", "My dentist is Dr Weber.", "Registered with Dr Okonjo after the office moved.", "Weber", "Okonjo"),
]


def run_scenario(idx: int, question: str, seed_fact: str, later_obs: str, old_marker: str, new_marker: str) -> dict:
    store_dir = pathlib.Path(tempfile.mkdtemp(prefix=f"stale{idx}-"))
    config = load_config()
    config.storage.backend = "lite"
    config.storage.path = str(store_dir / "s.sqlite3")
    config.llm.query_enhancement = False
    svc = MemplexService(config=config)
    svc.start()
    try:
        svc.write_text(seed_fact, source_type="text")
        svc.write_text(later_obs, source_type="text")
        result = svc.query(question, top_k=8, orchestrated=True, explain=False)
        summaries = [r.summary for r in result.results[:8]]
        recall = "\n".join(summaries).lower()
        old_in = old_marker.lower() in recall
        new_in = new_marker.lower() in recall
        old_rank = next(
            (i for i, s in enumerate(summaries) if old_marker.lower() in s.lower()),
            None,
        )
        new_rank = next(
            (i for i, s in enumerate(summaries) if new_marker.lower() in s.lower()),
            None,
        )
        if old_in and not new_in:
            outcome = "stale_reuse"
        elif new_in and not old_in:
            outcome = "resolved"
        elif old_in and new_in:
            outcome = "both"
        else:
            outcome = "neither"
        # Rank-sensitive view: the newer evidence ranking above the stale
        # one is the premise-resistance win condition when the pool is
        # too small to evict anything from top-k.
        new_before_old = (
            new_rank is not None and old_rank is not None and new_rank < old_rank
        )
        return {
            "index": idx,
            "question": question,
            "outcome": outcome,
            "new_before_old": new_before_old,
        }
    finally:
        svc.stop()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="benchmarks/results/stale-baseline")
    args = parser.parse_args()

    rows = [run_scenario(i, *s) for i, s in enumerate(SCENARIOS)]
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["outcome"]] = counts.get(r["outcome"], 0) + 1
    n = len(rows)
    summary = {
        "benchmark": "stale_memory_implicit_invalidation",
        "protocol": "seed fact -> later session implicit-invalidation observation -> neutral probe; outcomes by marker presence in top-8 recall",
        "n": n,
        "counts": counts,
        "stale_reuse_rate": round(counts.get("stale_reuse", 0) / n, 3),
        "resolved_rate": round(counts.get("resolved", 0) / n, 3),
        "both_rate": round(counts.get("both", 0) / n, 3),
        "new_before_old_rate": round(
            sum(r["new_before_old"] for r in rows) / n, 3
        ),
    }
    print(json.dumps(summary, indent=1), flush=True)

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "records.json").write_text(json.dumps(rows, indent=1))
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
