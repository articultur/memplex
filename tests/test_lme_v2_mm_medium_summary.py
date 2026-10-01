"""Paired-summary statistics for run_lme_v2_mm_medium.summarize.

Pins the measurement-integrity contract (external review phase 1 follow-up):
- flips_up/flips_down count ONLY rows both arms judged; an unjudged (None)
  side must never count as a flip in either direction (unjudged→correct is
  not an improvement).
- summary["paired_stats"] restates both arms' accuracy on the SAME
  both-judged question set (the two overall per-arm accuracies may sit on
  different judged subsets) and lists unjudged counts separately.
"""

from scripts.run_lme_v2_mm_medium import summarize


def _row(rid: str, text_correct, mm_correct) -> dict:
    return {
        "id": rid,
        "question_type": "recall",
        "n_images": 1,
        "text": {"correct": text_correct, "answer": "…"},
        "mm": {"correct": mm_correct, "answer": "…"},
        "latency_s": 1.0,
    }


ROWS = [
    _row("q1", None, True),    # unjudged text → must NOT count as flip up
    _row("q2", False, True),   # genuine improvement (the only flip up)
    _row("q3", True, True),    # both judged, both correct
    _row("q4", False, None),   # unjudged mm, no flip either way
    _row("q5", True, None),    # unjudged mm → must NOT count as flip down
]


def test_flips_exclude_unjudged_sides():
    summary, _ = summarize(ROWS, orchestrated=False, wall_s=1.0)
    assert summary["flips_up"] == 1, (
        "unjudged→correct must not count as an improvement; only q2 is a "
        f"genuine flip up, got {summary['flips_up']}"
    )
    assert summary["flips_down"] == 0, (
        "correct→unjudged must not count as a regression; got "
        f"{summary['flips_down']}"
    )


def test_paired_stats_same_question_set():
    summary, _ = summarize(ROWS, orchestrated=False, wall_s=1.0)
    ps = summary["paired_stats"]
    # Both-judged subset = {q2, q3}; unjudged counted separately, never dropped
    # silently.
    assert ps["n_both_judged"] == 2
    assert ps["n_unjudged_text"] == 1  # q1
    assert ps["n_unjudged_mm"] == 2    # q4, q5
    # Same-set accuracies: text {F, T} = 0.5 vs mm {T, T} = 1.0.
    assert ps["text_acc"] == 0.5
    assert ps["mm_acc"] == 1.0
    assert ps["delta_pp"] == 50.0
    assert ps["flips_up"] == 1 and ps["flips_down"] == 0


def test_overall_arm_accuracies_keep_own_subsets():
    # Per-arm overall accuracy keeps its own judged subset (with n_unjudged
    # disclosed) — the paired block is the comparable same-set view, not a
    # replacement of the per-arm numbers.
    summary, _ = summarize(ROWS, orchestrated=False, wall_s=1.0)
    # text judged: q2 F, q3 T, q4 F, q5 T → n=4, acc=0.5, unjudged q1.
    assert summary["text_arm"]["n"] == 4 and summary["text_arm"]["n_unjudged"] == 1
    assert summary["text_arm"]["accuracy"] == 0.5
    # mm judged: q1 T, q2 T, q3 T → n=3, acc=1.0, unjudged q4/q5.
    assert summary["mm_arm"]["n"] == 3 and summary["mm_arm"]["n_unjudged"] == 2
    assert summary["mm_arm"]["accuracy"] == 1.0
