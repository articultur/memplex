"""LongMemEval benchmark: long-term interactive memory (Wu et al., ICLR 2025).

Two on-disk schemas are supported; the loader auto-detects per entry.

Official schema (``xiaowu0162/longmemeval-cleaned`` releases, ``*_s`` /
``*_m`` / ``*_oracle`` splits)::

    [
      {
        "question_id": "...",
        "question_type": "single-session-user | single-session-assistant |
                          single-session-preference | temporal-reasoning |
                          knowledge-update | multi-session",
        "question": "...",
        "answer": "...",                    # single gold string
        "question_date": "YYYY/M/D H:MM",
        "haystack_session_ids": [...],
        "haystack_dates": [...],
        "haystack_sessions": [[{"role": "...", "content": "..."}, ...], ...],
        "answer_session_ids": [...]
      }, ...
    ]

Synthetic schema (the deterministic generator in ``benchmarks/loader.py``)::

    [
      {
        "question": "...",
        "question_type": "single-hop-user | single-hop-session |
                          multi-hop | temporal-reasoning | knowledge-update",
        "answers": ["..."],
        "question_date": "YYYY/M/D H:MM",
        "evidence_session_ids": [...],
        "session_history": [{"role": "...", "content": "..."}, ...]
      }, ...
    ]

This module:
    - ``LongMemEvalDataset``: loads either format, converts each question
      into a :class:`BenchmarkSample` whose source document materialises the
      session history as Observation memories. Official entries are
      recognised by the presence of ``haystack_sessions``: the single gold
      ``answer`` is wrapped into the answers list and the per-session
      haystack is flattened into one turn-level history (``haystack_dates``
      are accepted but not yet materialised as per-session timestamps).
    - ``LongMemEvalRunner``: ingests the sessions, queries memplex, and scores
      per question type. Primary metrics are token-F1 and exact match (max
      over gold answers, SQuAD convention); the normalised substring hit is
      kept only as the auxiliary diagnostic ``substring_hit_rate``.
    - A deterministic synthetic generator backs ``download_dataset`` when the
      real corpus is absent, so CI exercises the full path without network.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any

from benchmarks.base import (
    BenchmarkResult,
    BenchmarkRunner,
    BenchmarkSample,
    EvaluationDataset,
    LatencyStats,
    normalize_answer_text,
    token_f1,
)
from memplex.models import Observation

logger = logging.getLogger(__name__)

QUESTION_TYPES = (
    "single-hop-user",
    "single-hop-session",
    "multi-hop",
    "temporal-reasoning",
    "knowledge-update",
)


@dataclass
class LongMemEvalSample:
    """One LongMemEval question over its full session history."""

    question: str
    answers: list[str]
    question_type: str
    question_date: str
    session_history: list[dict[str, str]]
    evidence_session_ids: list[Any] = field(default_factory=list)
    question_id: str | None = None

    def to_benchmark_sample(self) -> BenchmarkSample:
        slug = self.question_id or f"{abs(hash(self.question)) & 0xFFFFFF:06x}"
        return BenchmarkSample(
            id=f"longmemeval-{slug}",
            query=self.question,
            expected_ids=[f"answer::{a}" for a in self.answers],
            expected_answer=self.answers[0] if self.answers else None,
            metadata={
                "benchmark": "longmemeval",
                "answers": list(self.answers),
                "question_type": self.question_type,
                "question_date": self.question_date,
                "evidence_session_ids": list(self.evidence_session_ids),
                "session_history": list(self.session_history),
            },
        )


def _parse_synthetic_entry(item: dict[str, Any]) -> LongMemEvalSample:
    """Parse the repo's synthetic schema (``answers`` list, flat history)."""
    return LongMemEvalSample(
        question=str(item.get("question", "")),
        answers=[str(a) for a in item.get("answers", [])],
        question_type=str(item.get("question_type", "single-hop-user")),
        question_date=str(item.get("question_date", "")),
        session_history=list(item.get("session_history", [])),
        evidence_session_ids=list(item.get("evidence_session_ids", [])),
    )


def _parse_official_entry(item: dict[str, Any]) -> LongMemEvalSample:
    """Parse the official schema (``answer`` string + ``haystack_sessions``).

    The single gold ``answer`` is wrapped into the answers list and the
    per-session haystack is flattened into one turn-level history, matching
    the shape the runner and ``to_memories`` already consume. Official
    question types (``single-session-user``, ``multi-session``, ...) are kept
    verbatim; abstention questions (``question_id`` ending in ``_abs``) load
    like any other entry.
    """
    raw_answer = item.get("answer")
    candidates = raw_answer if isinstance(raw_answer, list) else [raw_answer]
    answers = [str(a) for a in candidates if a is not None and str(a).strip()]
    session_history = [
        turn for session in item.get("haystack_sessions", []) for turn in session
    ]
    return LongMemEvalSample(
        question=str(item.get("question", "")),
        answers=answers,
        question_type=str(item.get("question_type", "single-hop-user")),
        question_date=str(item.get("question_date", "")),
        session_history=session_history,
        evidence_session_ids=list(item.get("answer_session_ids", [])),
        question_id=str(item["question_id"]) if item.get("question_id") else None,
    )


class LongMemEvalDataset(EvaluationDataset):
    """Loads LongMemEval JSON (list-of-questions form).

    Entries in the official haystack schema and the repo's synthetic flat
    schema may be mixed in one file; detection is per entry on the presence
    of ``haystack_sessions``.
    """

    def __init__(self, path: str | None = None):
        self.path = path

    def load(self, path: str) -> list[BenchmarkSample]:
        load_path = path or self.path
        if not load_path:
            raise ValueError("No path provided for LongMemEvalDataset.load()")
        p = Path(load_path)
        if not p.exists():
            raise FileNotFoundError(f"LongMemEval dataset not found at {load_path}")
        with open(p, encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, list):
            raise ValueError("LongMemEval top level must be a list of questions")  # noqa: TRY004 - exact-type check is deliberate (blocks bool/int equivalence and subclass bypass)
        samples: list[BenchmarkSample] = []
        for item in raw:
            sample = (
                _parse_official_entry(item)
                if "haystack_sessions" in item
                else _parse_synthetic_entry(item)
            )
            if not sample.question or not sample.answers:
                logger.debug("skipping malformed LongMemEval entry")
                continue
            samples.append(sample.to_benchmark_sample())
        logger.info("Loaded %d LongMemEval samples from %s", len(samples), load_path)
        return samples

    def to_memories(self, sample: BenchmarkSample):
        """Materialise the session history as typed Observation memories."""
        metadata = sample.metadata or {}
        history = metadata.get("session_history", [])
        observed = metadata.get("question_date", "")
        observations = []
        for index, turn in enumerate(history):
            observations.append(
                Observation(
                    id=f"lme-{sample.id}-s{index}",
                    event=str(turn.get("role", "user")),
                    context=str(turn.get("content", ""))[:2000],
                    category="note",
                    observed_at=observed or None,
                )
            )
        return observations


def _normalise(text: str) -> str:
    lowered = text.lower().strip()
    collapsed = re.sub(r"\s+", " ", lowered)
    stripped = re.sub(r"[^0-9a-z\u4e00-\u9fff ]", "", collapsed)
    return stripped


def answer_hit(predicted: str, gold_answers: list[str]) -> bool:
    """Auxiliary diagnostic: normalised gold-as-substring-of-prediction hit.

    One-directional on purpose: the previous bidirectional version also
    counted ``prediction in gold``, which inflates the score whenever the
    retrieved text is a short fragment of a longer gold answer. Kept as a
    diagnostic alongside the token-F1 / exact-match primary metrics.
    """
    pred = _normalise(predicted)
    if not pred:
        return False
    for gold in gold_answers:
        norm_gold = _normalise(gold)
        if norm_gold and norm_gold in pred:
            return True
    return False


class LongMemEvalRunner(BenchmarkRunner):
    """Ingest sessions → query → per-type token-F1/EM scoring.

    Scoring per question (max over gold answers, SQuAD convention):
        - ``token_f1``: token-overlap F1 between the concatenated retrieved
          summaries and the gold answer — the primary metric.
        - ``exact_match``: 1.0 when the normalised prediction equals a
          normalised gold answer. Because the "prediction" is a concatenation
          of retrieval snippets (not a generated answer), EM is expected to
          be near zero and is reported for honesty, not as a quality target.
        - ``substring_hit_rate``: auxiliary diagnostic, see :func:`answer_hit`.

    The official LongMemEval uses an LLM judge over generated answers; this
    runner has no generation step, so token-F1 over retrieved evidence is the
    closest reproducible proxy.
    """

    DATASET_NAME = "longmemeval"

    def __init__(self, dataset: LongMemEvalDataset | None = None):
        self.dataset = dataset or LongMemEvalDataset()

    def _seed(self, service, observations) -> None:
        """Seed sessions as searchable Function records (memory_eval recipe).

        The RAG path searches the Function FTS index, so typed memories are
        seeded as Function nodes whose name carries the session text — the
        same approach ``benchmarks/memory_eval.py`` uses for fact retention.
        """
        from memplex.models import Function, SourceDocument, SourceType

        for index, observation in enumerate(observations):
            text = f"{observation.event}: {observation.context}".strip()
            name = text[:120] or f"longmemeval-session-{index}"
            func = Function(
                id=observation.id,
                name=name,
                name_normalized=name.lower().strip().replace(" ", "_"),
                domain=None,
                memory_type="function",
                source_type=SourceType.MEETING,
            )
            source = SourceDocument(
                type="longmemeval",
                content=text,
                source_type=SourceType.MEETING,
            )
            service.store.add(func, source)

    @staticmethod
    def _score_sample(predicted: str, gold_answers: list[str]) -> dict[str, float]:
        """Score one prediction against its gold answers (max over golds)."""
        f1 = max((token_f1(predicted, gold) for gold in gold_answers), default=0.0)
        norm_pred = normalize_answer_text(predicted)
        em = (
            max(
                (1.0 if norm_pred and norm_pred == normalize_answer_text(gold) else 0.0)
                for gold in gold_answers
            )
            if gold_answers
            else 0.0
        )
        return {
            "token_f1": f1,
            "exact_match": em,
            "substring_hit": 1.0 if answer_hit(predicted, gold_answers) else 0.0,
        }

    def run_retrieval(
        self, service, samples: list[BenchmarkSample], top_k: int = 10
    ) -> list[BenchmarkResult]:
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        per_type: dict[str, list[dict[str, float]]] = {}
        latencies = LatencyStats()

        for sample in samples:
            self._seed(service, self.dataset.to_memories(sample))
            with latencies.timed():
                result = service.query(sample.query, top_k=top_k, explain=False)
            predicted = " ".join(r.summary for r in result.results)
            scores = self._score_sample(predicted, list(sample.metadata.get("answers", [])))
            qtype = sample.metadata.get("question_type", "unknown")
            per_type.setdefault(qtype, []).append(scores)

        all_scores = [score for scores in per_type.values() for score in scores]
        total = len(all_scores)

        results: list[BenchmarkResult] = []
        for metric in ("token_f1", "exact_match", "substring_hit_rate"):
            key = "substring_hit" if metric == "substring_hit_rate" else metric
            value = sum(s[key] for s in all_scores) / total if total else 0.0
            results.append(
                BenchmarkResult(
                    name="longmemeval_answer_quality",
                    dataset=self.DATASET_NAME,
                    metric=metric,
                    value=round(value, 4),
                    latency_ms=latencies.mean,
                    samples=total,
                    timestamp=timestamp,
                    latency_p50_ms=latencies.p50,
                    latency_p99_ms=latencies.p99,
                )
            )
        for qtype, outcomes in sorted(per_type.items()):
            value = sum(s["token_f1"] for s in outcomes) / len(outcomes)
            results.append(
                BenchmarkResult(
                    name="longmemeval_answer_quality",
                    dataset=f"{self.DATASET_NAME}::{qtype}",
                    metric="token_f1",
                    value=round(value, 4),
                    latency_ms=latencies.mean,
                    samples=len(outcomes),
                    timestamp=timestamp,
                    latency_p50_ms=latencies.p50,
                    latency_p99_ms=latencies.p99,
                )
            )
        return results

    def run_generation(self, service, samples: list[BenchmarkSample]):
        """LongMemEval is retrieval-scored; generation delegates to retrieval."""
        return self.run_retrieval(service, samples)


from benchmarks.base import BenchmarkRunnerFactory

BenchmarkRunnerFactory.register_benchmark(
    name="longmemeval",
    runner_cls=LongMemEvalRunner,
    dataset_cls=LongMemEvalDataset,
)
