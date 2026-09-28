#!/usr/bin/env python3
"""F4 first cut: does habit-typed encoding improve habit-question retrieval?

The second-battlefield assessment (docs/research/
second-battlefield-assessment-2026-09.md) maps the habit gap: habits land
as plain Fact text with no frequency semantics, and retrieval is purely
lexical. This probe measures the minimal implementation before any
product change: seed the same habit statements two ways -

  arm "plain": write_text of natural habit sentences (product default;
               extraction decides the node shape)
  arm "typed": typed Facts carrying the frequency in the predicate and
               a habit namespace ({"habit_cadence": ..., "habit_day":
               ...}) - the "reuse the Fact skeleton + namespace fields"
               minimal design from the assessment

- then probe with habit-shaped questions ("How often do I X?", "When do
I usually X?") and measure recall@8 of the habit statement and of the
cadence token specifically. Local, deterministic, no API.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")
os.environ.setdefault("MEMPLEX_LLM_QUERY_ENHANCEMENT", "false")
os.environ.setdefault("MEMPLEX_EMBEDDING_MODEL", "bge-m3")
os.environ.setdefault("MEMPLEX_EMBEDDING_DIMENSION", "1024")
os.environ.setdefault("MEMPLEX_EMBEDDING_DEVICE", "cpu")

from memplex.config import load_config
from memplex.service import MemplexService

# (statement, cadence token, day/frequency token, probe questions)
HABITS = [
    ("I water the aloe plant every Wednesday.", "every Wednesday", "Wednesday",
     ["How often do I water the aloe plant?", "When do I usually water the aloe?"]),
    ("I take out the recycling on Monday evenings.", "Monday evenings", "Monday",
     ["How often do I take out the recycling?", "When do I deal with the recycling?"]),
    ("I go for a swim every Saturday morning.", "every Saturday", "Saturday",
     ["How often do I go swimming?", "When is my usual swim?"]),
    ("I back up my laptop on the first of each month.", "first of each month", "month",
     ["How often do I back up my laptop?", "When do I do laptop backups?"]),
    ("I call my grandmother every Sunday afternoon.", "every Sunday", "Sunday",
     ["How often do I call my grandmother?", "When do I call grandma?"]),
    ("I meal-prep on Sunday evenings for the week.", "Sunday evenings", "Sunday",
     ["How often do I meal-prep?", "When do I prep meals?"]),
    ("I water the orchids twice a week, Tuesdays and Fridays.", "twice a week", "Tuesdays",
     ["How often do I water the orchids?", "When do I water the orchids?"]),
    ("I run the dishwasher at night before bed.", "at night", "night",
     ["How often does the dishwasher run?", "When do I run the dishwasher?"]),
]

FILLERS = [
    "My sister lives in Porto with her two dogs.",
    "The boiler service is due in November with HeatFix Ltd.",
    "I finished reading The Overstory last weekend.",
    "The office moved to the fourth floor; my desk is by the window.",
    "We adopted the cat from the shelter on Third Avenue two years ago.",
    "My glasses prescription changed; new lenses arrive Friday.",
    "The neighbours' names are Hanna and Yusuf; they moved in last spring.",
    "Our kitchen renovation finishes next month; the contractor is Marlowe Bros.",
    "I keep my tax documents in the grey folder, second drawer.",
    "My uncle retired and moved to the lakeside cottage.",
]

# One-off, habit-adjacent statements: they compete lexically with the
# habit probes (water/backup/call verbs) without carrying cadence - a
# small pool makes recall trivially 1.0, so the pool must be competitive.
HABIT_DISTRACTORS = [
    "I watered the fern yesterday because it was drooping.",
    "The tomatoes got extra water during the July heatwave.",
    "I ran a backup before the OS upgrade last month.",
    "I called the pharmacy about the prescription renewal this morning.",
    "I cooked pasta from scratch on my brother's birthday.",
    "I went swimming on the Portugal trip in 2022.",
    "The dishwasher broke and the repair visit is booked for Thursday.",
    "I prepped snacks for the road trip to the lakeside cottage.",
    "I emptied the recycling bin before the party on Friday.",
    "I called my dentist to move the checkup to another week.",
]


# Implicit-habit arm: scattered one-off event mentions with varied
# phrasing (no explicit cadence anywhere) - the LifeBench habit case.
# The consolidation pass promotes them; whether the promoted node's
# text surface then carries the cadence is exactly what this measures.
IMPLICIT_MENTIONS = {
    0: ["Watered the aloe this morning.",
        "Gave the aloe its drink before lunch.",
        "Aloe watering done, soil was dry.",
        "Watered the aloe again, drainage ran clear."],
    4: ["Called grandma about her bridge game.",
        "Phone call with grandmother, she sounded cheerful.",
        "Rang grandma to check on her knee.",
        "Talked with grandmother about the garden show."],
}
IMPLICIT_QUESTIONS = {
    0: ["How often do I water the aloe plant?", "When do I usually water the aloe?"],
    4: ["How often do I call my grandmother?", "When do I call grandma?"],
}
IMPLICIT_CADENCE = {0: "wednesday", 4: "sunday"}  # ground-truth cadence token
IMPLICIT_SUBJECT = {0: "aloe", 4: "grand"}  # subject marker per habit


def _cadence_with_subject(svc: MemplexService, question: str, habit_idx: int) -> bool:
    """True only when ONE retrieved line carries both the habit's
    subject and its cadence token - bare token hits from unrelated
    memories ("My plant watering day is Wednesday") are contamination,
    not habit information."""
    result = svc.query(question, top_k=8, orchestrated=True, explain=False)
    cadence = IMPLICIT_CADENCE[habit_idx]
    subject = IMPLICIT_SUBJECT[habit_idx]
    return any(
        cadence in line.lower() and subject in line.lower()
        for line in (r.summary for r in result.results[:8])
    )


def _build() -> MemplexService:
    store_dir = pathlib.Path(tempfile.mkdtemp(prefix="habit-"))
    cfg = load_config()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(store_dir / "s.sqlite3")
    cfg.llm.query_enhancement = False
    svc = MemplexService(config=cfg)
    svc.start()
    return svc


def _surface(svc: MemplexService, question: str) -> str:
    result = svc.query(question, top_k=8, orchestrated=True, explain=False)
    return "\n".join(r.summary for r in result.results[:8]).lower()


def run_arm(arm: str) -> list[dict]:
    # Competitive pool from the trust-weight reproduction script (83
    # personal-history items) plus habit-adjacent distractors: with a
    # small pool the top-8 surface covers a quarter of the corpus and
    # recall saturates at 1.0, which measures nothing.
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "trust_w", _PROJECT_ROOT / "scripts" / "trust_weight_reproduction.py"
    )
    trust_w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(trust_w)

    svc = _build()
    rows = []
    try:
        for filler in trust_w.CLEAN_SEED + trust_w.FILLERS + HABIT_DISTRACTORS:
            svc.write_text(filler, source_type="text")

        from memplex.models import Fact, SourceType

        for idx, (statement, cadence, day, _) in enumerate(HABITS):
            if arm == "plain":
                svc.write_text(statement, source_type="text")
            else:
                subject, verb, rest = statement[2:].split(" ", 2)
                fact = Fact(
                    id=f"habit-{idx}",
                    subject=subject,
                    predicate=f"habitually {verb}",
                    object_=rest,
                    source_type=SourceType.WIKI,
                    trust_tier=4,
                )
                fact.namespace = {
                    "habit_cadence": cadence,
                    "habit_marker": day.lower(),
                }
                svc.store.add_fact(fact)

        for idx, (statement, cadence, day, questions) in enumerate(HABITS):
            # The arm-specific recall target: the verbatim statement for
            # plain writes, the object fragment for typed facts (their
            # summary is "subject habitually verb rest", not the sentence).
            if arm == "plain":
                target = statement.lower()[:40]
            else:
                target = statement[2:].split(" ", 2)[2].lower()[:40]
            for q in questions:
                surface = _surface(svc, q)
                rows.append({
                    "arm": arm,
                    "habit": idx,
                    "question": q,
                    "statement_in_top8": target in surface,
                    "cadence_in_top8": cadence.lower() in surface,
                    "subject_in_top8": statement.split()[2].lower() in surface
                    if len(statement.split()) > 2 else False,
                })
    finally:
        svc.stop()
    return rows


def run_implicit_arm() -> list[dict]:
    """Scattered mentions, no explicit cadence: measure evidence recall
    and the structural absence of cadence in any retrievable surface,
    before and after the F3 consolidation pass."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "trust_w", _PROJECT_ROOT / "scripts" / "trust_weight_reproduction.py"
    )
    trust_w = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(trust_w)

    os.environ["MEMPLEX_CONSOLIDATION"] = "1"
    os.environ["MEMPLEX_CONSOLIDATION_MIN_OBSERVATIONS"] = "3"
    os.environ["MEMPLEX_CONSOLIDATION_MIN_SPAN_DAYS"] = "1"
    svc = _build()
    rows = []
    try:
        for filler in trust_w.CLEAN_SEED + trust_w.FILLERS + HABIT_DISTRACTORS:
            svc.write_text(filler, source_type="text")
        # Varied phrasings across backdated days so lexical clustering
        # has a chance; a11y of the probe: embeddings catch paraphrases.
        from datetime import UTC, datetime, timedelta

        base = datetime.now(UTC) - timedelta(days=6)
        for habit_idx, mentions in IMPLICIT_MENTIONS.items():
            for i, mention in enumerate(mentions):
                svc.write_text(mention, source_type="text")
                # backdate the paragraph row (probe-only; spans matter)
                for row in svc.store._paragraphs.values():
                    if row["raw_text"] == mention:
                        row["created_at"] = (
                            base + timedelta(days=i * 2)
                        ).isoformat()
                        row["last_observed_at"] = row["created_at"]

        for habit_idx, questions in IMPLICIT_QUESTIONS.items():
            for q in questions:
                surface = _surface(svc, q)
                mentions = IMPLICIT_MENTIONS[habit_idx]
                rows.append({
                    "arm": "implicit_pre",
                    "habit": habit_idx,
                    "question": q,
                    "evidence_in_top8": any(
                        m.lower()[:25] in surface for m in mentions
                    ),
                    "cadence_in_top8": _cadence_with_subject(svc, q, habit_idx),
                })

        from memplex.consolidation import consolidate

        report = consolidate(svc.store)
        for habit_idx, questions in IMPLICIT_QUESTIONS.items():
            for q in questions:
                surface = _surface(svc, q)
                mentions = IMPLICIT_MENTIONS[habit_idx]
                rows.append({
                    "arm": "implicit_post",
                    "habit": habit_idx,
                    "question": q,
                    "evidence_in_top8": any(
                        m.lower()[:25] in surface for m in mentions
                    ),
                    "cadence_in_top8": _cadence_with_subject(svc, q, habit_idx),
                })
        return rows
    finally:
        svc.stop()


def main() -> int:
    rows = run_arm("plain") + run_arm("typed") + run_implicit_arm()

    def stats(arm: str, key: str) -> float:
        sub = [r for r in rows if r["arm"] == arm]
        return round(sum(r[key] for r in sub) / max(len(sub), 1), 3)

    summary = {
        "benchmark": "habit_semantics_probe",
        "protocol": (
            "8 habits x 2 habit-shaped questions each; plain arm = "
            "write_text sentences, typed arm = Facts with habit namespace "
            "and frequency-bearing predicates; recall@8 of statement / "
            "cadence token / subject word"
        ),
        "n_questions": len(rows),
        "plain": {
            "statement_recall": stats("plain", "statement_in_top8"),
            "cadence_recall": stats("plain", "cadence_in_top8"),
            "subject_recall": stats("plain", "subject_in_top8"),
        },
        "typed": {
            "statement_recall": stats("typed", "statement_in_top8"),
            "cadence_recall": stats("typed", "cadence_in_top8"),
            "subject_recall": stats("typed", "subject_in_top8"),
        },
        "implicit_pre_consolidation": {
            "evidence_recall": stats("implicit_pre", "evidence_in_top8"),
            "cadence_recall": stats("implicit_pre", "cadence_in_top8"),
        },
        "implicit_post_consolidation": {
            "evidence_recall": stats("implicit_post", "evidence_in_top8"),
            "cadence_recall": stats("implicit_post", "cadence_in_top8"),
        },
    }
    print(json.dumps(summary, indent=1), flush=True)
    out = pathlib.Path("benchmarks/results/habit-semantics-probe")
    out.mkdir(parents=True, exist_ok=True)
    (out / "records.json").write_text(json.dumps(rows, indent=1))
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
