"""Attribution-layer regression tests for scripts/gap_forensics.py.

Covers the two verified measurement defects fixed on 2026-10-23:

- defect-1: ``context_has_answer`` gated on ``len(gold_answer) >= 3``,
  so every 1-2 character gold answer (mostly bare digits from
  counting questions) was reported absent from the context; 9 of the
  11 context-stage rows in benchmarks/results/gap-forensics/ have
  1-2 digit gold answers, so "context lacks the answer" was never
  established for them.
- defect-2: retrieval passed when ANY gold session surfaced in the
  top-24, but counting/aggregation questions need the complete
  enumeration of all gold sessions; attribution now judges
  counting-intent questions on full coverage.

The attribution judgment is extracted into pure functions so these
properties are testable without seeding a store.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from scripts.gap_forensics import (
    attribute_stage,
    context_has_answer,
    counting_intent,
)

# The signal list of the harness runner's counting_intent() heuristic
# (scripts/run_lme_official_j.py) as of 2026-10-23; pinned by the drift
# guard below.
_HARNESS_COUNTING_SIGNALS = [
    "how many",
    "how much",
    "how often",
    "number of",
    "list all",
    "list every",
    "all the",
    "what are all",
]


class TestImportIsSideEffectFree:
    """Importing this module must not mutate the process environment.

    The attribution helpers are pure functions; the forensics run profile
    (MEMPLEX_STORAGE_BACKEND / MEMPLEX_LLM_QUERY_ENHANCEMENT /
    MEMPLEX_PARAGRAPH_FUSION=primary) is applied by main(), not at import.
    A module-level setdefault leaked MEMPLEX_PARAGRAPH_FUSION=primary into
    the pytest process and changed ``LiteMemoryStore.vector_search``
    behaviour for every suite-ordered test after this file (the vector
    leg empties on extraction-node corpora under primary fusion), which
    is exactly the class of cross-test interference the drift guard above
    documents for the harness runner.
    """

    def test_module_body_sets_no_memplex_env_vars(self, monkeypatch) -> None:
        # Reload the module under an env scrubbed of the profile vars the
        # historical module-level setdefaults used to plant. Conftest
        # legitimately owns MEMPLEX_STORAGE_BACKEND, so only the two vars
        # nothing else in the test process may set are asserted.
        import importlib

        import scripts.gap_forensics as gap_forensics_module

        for var in ("MEMPLEX_PARAGRAPH_FUSION", "MEMPLEX_LLM_QUERY_ENHANCEMENT"):
            monkeypatch.delenv(var, raising=False)
        importlib.reload(gap_forensics_module)
        assert "MEMPLEX_PARAGRAPH_FUSION" not in os.environ
        assert "MEMPLEX_LLM_QUERY_ENHANCEMENT" not in os.environ


class TestContextHasAnswer:
    """Defect-1 regression: the removed len>=3 gate and token boundaries."""

    def test_single_digit_answer_present_in_context(self) -> None:
        assert context_has_answer("4", "user: I have 4 cats now") is True

    def test_two_digit_answer_present_in_context(self) -> None:
        assert context_has_answer("17", "user: I saw 17 patients today") is True

    def test_answer_absent_from_context(self) -> None:
        assert context_has_answer("17", "user: nothing numeric here") is False

    def test_empty_gold_answer_is_absent(self) -> None:
        assert context_has_answer("", "anything at all") is False
        # normalizes to empty -> no evidence of an answer
        assert context_has_answer("!!!", "anything at all") is False

    def test_substring_false_positive_avoided(self) -> None:
        # "2" must not match inside "20" or "1720"
        assert context_has_answer("2", "the total was 20 items") is False
        assert context_has_answer("2", "I ran 1720 meters today") is False

    def test_digit_boundary_prefix_and_suffix(self) -> None:
        assert context_has_answer("17", "exactly 172 patients shown") is False
        assert context_has_answer("17", "session 17 ends here") is True
        assert context_has_answer("4", "the 4th visit happened") is False

    def test_three_digit_numeric_uses_token_boundary(self) -> None:
        assert context_has_answer("720", "paid $1720 in fees") is False
        assert context_has_answer("720", "paid $720 in fees") is True

    def test_normalization_strips_case_and_currency(self) -> None:
        assert context_has_answer("$720", "Paid 720 dollars total") is True

    def test_long_answer_keeps_substring_semantics(self) -> None:
        assert (
            context_has_answer(
                "February 1st", "the visit was on February 1st, 2023"
            )
            is True
        )
        assert (
            context_has_answer("February 1st", "the visit was on March 1st")
            is False
        )


class TestCountingIntent:
    """The keyword classifier is copied from the harness runner."""

    def test_counting_signals_detected(self) -> None:
        assert counting_intent("How many doctors did I visit?") is True
        assert counting_intent("List all the countries I went to") is True
        assert counting_intent("What are all the pets I owned?") is True

    def test_non_counting_question(self) -> None:
        assert counting_intent("What is my sister's name?") is False
        assert counting_intent("Where did I stay in Rome?") is False

    def test_signals_match_harness_runner_source(self) -> None:
        # Drift guard, deliberately text-level: importing the runner would
        # execute its embedding env setdefaults (bge-m3 / mps) inside the
        # test process and could silently change forensics behavior.
        source = (_PROJECT_ROOT / "scripts" / "run_lme_official_j.py").read_text()
        match = re.search(
            r"def counting_intent\(.*?signals = \((?P<body>.*?)\)\n",
            source,
            re.DOTALL,
        )
        assert match, "counting_intent() signals block missing from harness runner"
        runner_signals = re.findall(r'"([^"]*)"', match.group("body"))
        assert runner_signals == _HARNESS_COUNTING_SIGNALS


class TestAttributeStage:
    """Stage attribution: full-coverage rule for counting questions."""

    @staticmethod
    def _kw(**over: object) -> dict[str, object]:
        base: dict[str, object] = {
            "gold_count": 4,
            "write_lost_count": 0,
            "any_covered": False,
            "full_covered": False,
            "counting": False,
            "answer_in_context": False,
        }
        base.update(over)
        return base

    def test_all_gold_sessions_write_lost_is_write(self) -> None:
        assert attribute_stage(**self._kw(write_lost_count=4)) == "write"  # type: ignore[arg-type]

    def test_counting_partial_coverage_is_retrieval(self) -> None:
        # Partial gold coverage is not enough for a counting/aggregation
        # question even though some session surfaced (any_covered=True).
        assert (
            attribute_stage(**self._kw(counting=True, any_covered=True, full_covered=False))  # type: ignore[arg-type]
            == "retrieval"
        )

    def test_counting_partial_coverage_beats_context_stage(self) -> None:
        # Retrieval is checked before context: a counting question with
        # partial coverage is a retrieval loss even if the answer text
        # happens to sit in the assembled context.
        assert (
            attribute_stage(
                **self._kw(counting=True, any_covered=True, full_covered=False, answer_in_context=True)  # type: ignore[arg-type]
            )
            == "retrieval"
        )

    def test_counting_full_coverage_and_answer_in_context_is_answerer(self) -> None:
        assert (
            attribute_stage(
                **self._kw(counting=True, any_covered=True, full_covered=True, answer_in_context=True)  # type: ignore[arg-type]
            )
            == "answerer"
        )

    def test_counting_full_coverage_but_answer_missing_is_context(self) -> None:
        assert (
            attribute_stage(
                **self._kw(counting=True, any_covered=True, full_covered=True)  # type: ignore[arg-type]
            )
            == "context"
        )

    def test_non_counting_any_hit_is_not_retrieval(self) -> None:
        assert (
            attribute_stage(**self._kw(any_covered=True, full_covered=False))  # type: ignore[arg-type]
            == "context"
        )
        assert (
            attribute_stage(
                **self._kw(any_covered=True, full_covered=False, answer_in_context=True)  # type: ignore[arg-type]
            )
            == "answerer"
        )

    def test_non_counting_no_hit_is_retrieval(self) -> None:
        assert attribute_stage(**self._kw(any_covered=False, full_covered=False)) == "retrieval"  # type: ignore[arg-type]

    def test_partial_write_loss_falls_through(self) -> None:
        # Existing semantics preserved: only COMPLETE write loss
        # attributes to the write stage.
        assert (
            attribute_stage(**self._kw(write_lost_count=1, counting=True))  # type: ignore[arg-type]
            == "retrieval"
        )

    def test_no_gold_sessions_never_attributes_write_or_retrieval(self) -> None:
        assert (
            attribute_stage(**self._kw(gold_count=0, write_lost_count=0))  # type: ignore[arg-type]
            == "context"
        )
