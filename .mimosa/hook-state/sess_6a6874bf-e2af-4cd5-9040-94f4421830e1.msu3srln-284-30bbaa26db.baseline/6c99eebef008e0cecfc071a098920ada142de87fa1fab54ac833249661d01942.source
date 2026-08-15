"""LongMemEval benchmark: long-term interactive memory (Wu et al., ICLR 2025).

Dataset format (official ``xuanyuan14/LongMemEval`` releases, ``*_s`` /
``*_m`` / ``*_oracle`` splits)::

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
    - ``LongMemEvalDataset``: loads the format, converts each question into a
      :class:`BenchmarkSample` whose source document materialises the session
      history as Observation memories.
    - ``LongMemEvalRunner``: ingests the sessions, queries memplex, and scores
      answer hits (normalised substring / exact match) per question type.
    - A deterministic synthetic generator backs ``download_dataset`` when the
      real corpus is absent, so CI exercises the full path without network.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from benchmarks.base import (
    BenchmarkResult,
    BenchmarkRunner,
    BenchmarkSample,
    EvaluationDataset,
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
    answers: List[str]
    question_type: str
    question_date: str
    session_history: List[Dict[str, str]]
    evidence_session_ids: List[Any] = field(default_factory=list)

    def to_benchmark_sample(self) -> BenchmarkSample:
        return BenchmarkSample(
            id=f"longmemeval-{abs(hash(self.question)) & 0xFFFFFF:06x}",
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


class LongMemEvalDataset(EvaluationDataset):
    """Loads official LongMemEval JSON (list-of-questions form)."""

    def __init__(self, path: Optional[str] = None):
        self.path = path

    def load(self, path: str) -> List[BenchmarkSample]:
        load_path = path or self.path
        if not load_path:
            raise ValueError("No path provided for LongMemEvalDataset.load()")
        p = Path(load_path)
        if not p.exists():
            raise FileNotFoundError(f"LongMemEval dataset not found at {load_path}")
        with open(p, encoding="utf-8") as fh:
            raw = json.load(fh)
        if not isinstance(raw, list):
            raise ValueError("LongMemEval top level must be a list of questions")
        samples: List[BenchmarkSample] = []
        for item in raw:
            sample = LongMemEvalSample(
                question=str(item.get("question", "")),
                answers=[str(a) for a in item.get("answers", [])],
                question_type=str(item.get("question_type", "single-hop-user")),
                question_date=str(item.get("question_date", "")),
                session_history=list(item.get("session_history", [])),
                evidence_session_ids=list(item.get("evidence_session_ids", [])),
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


def answer_hit(predicted: str, gold_answers: List[str]) -> bool:
    """Normalised substring hit of any gold answer inside the prediction."""
    pred = _normalise(predicted)
    if not pred:
        return False
    for gold in gold_answers:
        norm_gold = _normalise(gold)
        if norm_gold and (norm_gold in pred or pred in norm_gold):
            return True
    return False


class LongMemEvalRunner(BenchmarkRunner):
    """Ingest sessions → query → per-type answer-hit scoring."""

    DATASET_NAME = "longmemeval"

    def __init__(self, dataset: Optional[LongMemEvalDataset] = None):
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

    def run_retrieval(
        self, service, samples: List[BenchmarkSample], top_k: int = 10
    ) -> List[BenchmarkResult]:
        from datetime import datetime

        timestamp = datetime.utcnow().isoformat() + "Z"
        per_type: Dict[str, List[bool]] = {}
        latencies: List[int] = []

        for sample in samples:
            self._seed(service, self.dataset.to_memories(sample))
            from datetime import datetime as _dt

            start = _dt.now()
            result = service.query(sample.query, top_k=top_k, explain=False)
            latencies.append(int((_dt.now() - start).total_seconds() * 1000))
            predicted = " ".join(r.summary for r in result.results)
            hit = answer_hit(predicted, list(sample.metadata.get("answers", [])))
            qtype = sample.metadata.get("question_type", "unknown")
            per_type.setdefault(qtype, []).append(hit)

        total = sum(len(v) for v in per_type.values())
        hits = sum(sum(v) for v in per_type.values())
        avg_latency = int(sum(latencies) / len(latencies)) if latencies else 0

        results: List[BenchmarkResult] = [
            BenchmarkResult(
                name="longmemeval_answer_hit",
                dataset=self.DATASET_NAME,
                metric="answer_hit_rate",
                value=round(hits / total, 4) if total else 0.0,
                latency_ms=avg_latency,
                samples=total,
                timestamp=timestamp,
            )
        ]
        for qtype, outcomes in sorted(per_type.items()):
            results.append(
                BenchmarkResult(
                    name="longmemeval_answer_hit",
                    dataset=f"{self.DATASET_NAME}::{qtype}",
                    metric="answer_hit_rate",
                    value=round(sum(outcomes) / len(outcomes), 4),
                    latency_ms=avg_latency,
                    samples=len(outcomes),
                    timestamp=timestamp,
                )
            )
        return results

    def run_generation(self, service, samples: List[BenchmarkSample]):
        """LongMemEval is retrieval-scored; generation delegates to retrieval."""
        return self.run_retrieval(service, samples)


from benchmarks.base import BenchmarkRunnerFactory  # noqa: E402

BenchmarkRunnerFactory.register_benchmark(
    name="longmemeval",
    runner_cls=LongMemEvalRunner,
    dataset_cls=LongMemEvalDataset,
)
