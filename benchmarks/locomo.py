"""LoCoMo (ACL 2024) benchmark: long-term conversation memory retention.

Supported input formats:

1. Official LoCoMo release (https://github.com/snap-research/locomo,
   ``data/locomo10.json``) — a JSON list of samples::

       [{
         "sample_id": "conv-26",
         "qa": [{"question": "...", "answer": "...", "category": 1,
                 "evidence": ["D1:3", ...]}, ...],
         "conversation": {
           "speaker_a": "...", "speaker_b": "...",
           "session_1_date_time": "...",
           "session_1": [{"speaker": "...", "dia_id": "D1:1", "text": "..."}, ...],
           "session_2_date_time": "...", "session_2": [...], ...
         }
       }, ...]

   Each ``qa`` entry becomes one QA :class:`BenchmarkSample`; its ``evidence``
   ``dia_id`` list becomes the retrieval ground truth.

2. Legacy synthetic format (memplex-generated fallback) — a JSON dict or
   list of dicts with ``{conversation_id, turns[], ground_truth_memories[]}``
   where each turn is ``{speaker, text, timestamp}`` and each ground-truth
   memory is ``{memory_id, content, session_id}``. A ``type`` field
   (``qa`` | ``summarization`` | ``conversation``) selects the sample shape.

This module provides:
    - LocomoDataset: loads either format, converts to BenchmarkSample list
    - LocomoRunner: implements BenchmarkRunner for retrieval + generation
    - Metrics: recency accuracy, persona consistency, event tracking
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from benchmarks.base import (
    BenchmarkResult,
    BenchmarkRunner,
    BenchmarkSample,
    BenchmarkSourceDocument,
    EvaluationDataset,
    LatencyStats,
    token_f1,
)
from memplex.models import SourceDocument, SourceType
from memplex.service import MemplexService

logger = logging.getLogger(__name__)

#: Token-F1 bar for counting a persona-consistency hit. 0.5 is the usual
#: "majority token overlap" partial-credit bar in QA evaluation: the retrieved
#: context must share at least half-weighted token overlap with one reference
#: (the expected answer, or one utterance of the target speaker).
PERSONA_F1_HIT_THRESHOLD = 0.5


def _parse_turn_timestamp(raw: Any) -> datetime | None:
    """Parse a LoCoMo turn timestamp.

    Accepts ISO-8601 (repo synthetic format) and the official locomo10
    style (``"1:56 pm on 8 May, 2023"``). Returns None when unparseable.
    """
    if not raw:
        return None
    text = str(raw).strip()
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    for fmt in ("%I:%M %p on %d %B, %Y", "%I:%M %p on %d %B %Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    return None


# ── Internal sample type ───────────────────────────────────────────────────────


@dataclass
class LocomoSample:
    """Internal LoCoMo sample with conversation-specific fields.

    Converted to BenchmarkSample before passing to runners.
    """

    id: str
    query: str
    expected_ids: list[str]
    expected_answer: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    # LoCoMo-specific fields
    conversation_id: str = ""
    turns: list[dict[str, str]] = field(default_factory=list)
    ground_truth_memories: list[dict[str, str]] = field(default_factory=list)

    def to_benchmark_sample(self) -> BenchmarkSample:
        """Convert to the public BenchmarkSample format.

        Includes LoCoMo-specific fields in metadata so to_memories() can
        reconstruct the full conversation context.
        """
        meta = dict(self.metadata)
        meta.setdefault("conversation_id", self.conversation_id)
        meta.setdefault("turns", self.turns)
        meta.setdefault("ground_truth_memories", self.ground_truth_memories)
        return BenchmarkSample(
            id=self.id,
            query=self.query,
            expected_ids=self.expected_ids,
            expected_answer=self.expected_answer,
            metadata=meta,
        )


# ── Dataset loader ──────────────────────────────────────────────────────────────


class LocomoDataset(EvaluationDataset):
    """Loads LoCoMo JSON files (official release or legacy synthetic format).

    Supports:
        - Question answering: resolve a factual question from conversation
        - Event summarization: summarize events discussed in the conversation
        - Multi-modal conversation: multi-speaker dialogue with mixed content types
    """

    def __init__(self, path: str | None = None):
        self.path = path

    def load(self, path: str) -> list[BenchmarkSample]:
        """Load LoCoMo samples from a JSON file.

        Parameters
        ----------
        path:
            Path to the LoCoMo JSON file. Falls back to ``self.path``.

        Returns
        -------
        List[BenchmarkSample]
            Parsed samples ready for benchmarking.
        """
        load_path = path or self.path
        if not load_path:
            raise ValueError("No path provided for LocomoDataset.load()")

        p = Path(load_path)
        if not p.exists():
            raise FileNotFoundError(f"LoCoMo dataset not found: {load_path}")

        with open(p, encoding="utf-8") as fh:
            raw = json.load(fh)

        # LoCoMo may be a single conversation dict or a list of conversations
        if isinstance(raw, dict):
            conversations = [raw]
        elif isinstance(raw, list):
            conversations = raw
        else:
            raise ValueError(  # noqa: TRY004 - exact-type check is deliberate (blocks bool/int equivalence and subclass bypass)
                f"Unexpected LoCoMo format in {load_path}: top-level must be dict or list"
            )

        samples: list[BenchmarkSample] = []
        for conv in conversations:
            if self._is_official_entry(conv):
                samples.extend(s.to_benchmark_sample() for s in self._parse_official(conv))
                continue

            conv_id = conv.get("conversation_id", conv.get("id", "unknown"))
            turns = conv.get("turns", [])
            memories = conv.get("ground_truth_memories", [])
            sample_type = conv.get("type", "qa")  # qa | summarization | conversation

            if sample_type == "summarization":
                sample = self._make_summarization_sample(conv_id, turns, memories)
            elif sample_type == "conversation":
                sample = self._make_conversation_sample(conv_id, turns, memories)
            else:
                sample = self._make_qa_sample(conv_id, turns, memories)

            samples.append(sample.to_benchmark_sample())

        logger.info("Loaded %d LoCoMo samples from %s", len(samples), load_path)
        return samples

    # ── Official locomo10.json format ────────────────────────────────────────

    @staticmethod
    def _is_official_entry(conv: Any) -> bool:
        """Official entries carry ``sample_id`` + ``conversation`` + ``qa``."""
        return (
            isinstance(conv, dict)
            and "sample_id" in conv
            and isinstance(conv.get("conversation"), dict)
            and isinstance(conv.get("qa"), list)
        )

    def _parse_official(self, entry: dict[str, Any]) -> list[LocomoSample]:
        """Parse one official LoCoMo sample into one QA sample per ``qa`` entry.

        Sessions are flattened in chronological order (``session_N`` sorted by
        N); each QA's ``evidence`` ``dia_id`` list becomes ``expected_ids``
        and the referenced turns become ``ground_truth_memories`` so the
        standard seeding/retrieval path works unchanged.
        """
        sample_id = str(entry.get("sample_id", "unknown"))
        convo = entry.get("conversation") or {}

        session_keys = sorted(
            (k for k in convo if re.fullmatch(r"session_\d+", k)),
            key=lambda k: int(k.rsplit("_", 1)[1]),
        )
        turns: list[dict[str, str]] = []
        for session_key in session_keys:
            session_dt = str(convo.get(f"{session_key}_date_time", ""))
            for turn in convo.get(session_key) or []:
                if not isinstance(turn, dict):
                    continue
                turns.append(
                    {
                        "speaker": str(turn.get("speaker", "")),
                        "text": str(turn.get("text", "")),
                        "timestamp": session_dt,
                        "dia_id": str(turn.get("dia_id", "")),
                        "session": session_key,
                    }
                )
        turn_by_dia = {t["dia_id"]: t for t in turns if t["dia_id"]}
        speakers = [
            str(convo.get(key, ""))
            for key in ("speaker_a", "speaker_b")
            if convo.get(key)
        ]

        samples: list[LocomoSample] = []
        for index, qa in enumerate(entry.get("qa") or []):
            if not isinstance(qa, dict):
                continue
            question = str(qa.get("question", "") or "").strip()
            if not question:
                continue
            answer = qa.get("answer")
            expected_answer = str(answer) if answer is not None else None
            evidence_ids = [str(e) for e in qa.get("evidence", []) or []]
            memories = []
            for dia_id in evidence_ids:
                turn = turn_by_dia.get(dia_id, {})
                memories.append(
                    {
                        "memory_id": dia_id,
                        "content": turn.get("text", ""),
                        "session_id": turn.get("session", ""),
                        "timestamp": turn.get("timestamp", ""),
                    }
                )
            samples.append(
                LocomoSample(
                    id=f"{sample_id}_q{index}",
                    query=question,
                    expected_ids=evidence_ids,
                    expected_answer=expected_answer,
                    metadata={
                        "type": "qa",
                        "turn_count": len(turns),
                        "conversation_id": sample_id,
                        "category": qa.get("category"),
                        "speakers": speakers,
                        "evidence": evidence_ids,
                    },
                    conversation_id=sample_id,
                    turns=turns,
                    ground_truth_memories=memories,
                )
            )
        return samples

    def _make_qa_sample(
        self,
        conv_id: str,
        turns: list[dict[str, str]],
        memories: list[dict[str, str]],
    ) -> LocomoSample:
        """Build a question-answering sample from a LoCoMo conversation.

        Query is the last user turn; expected answer is the next assistant turn.
        """
        user_turns = [
            t for t in turns if t.get("speaker", "").lower() in ("user", "human", "question")
        ]
        assistant_turns = [
            t for t in turns if t.get("speaker", "").lower() in ("assistant", "system", "agent")
        ]

        query = user_turns[-1]["text"] if user_turns else ""
        expected_answer = assistant_turns[-1]["text"] if assistant_turns else None
        expected_ids = [m["memory_id"] for m in memories if "memory_id" in m]

        return LocomoSample(
            id=f"{conv_id}_qa",
            query=query,
            expected_ids=expected_ids,
            expected_answer=expected_answer,
            metadata={
                "type": "qa",
                "turn_count": len(turns),
                "conversation_id": conv_id,
            },
            conversation_id=conv_id,
            turns=turns,
            ground_truth_memories=memories,
        )

    def _make_summarization_sample(
        self,
        conv_id: str,
        turns: list[dict[str, str]],
        memories: list[dict[str, str]],
    ) -> LocomoSample:
        """Build an event summarization sample.

        Query asks for a summary; expected_answer contains key events.
        """
        query = "Summarize the key events discussed in this conversation."
        expected_events = [m.get("content", "") for m in memories if "content" in m]
        expected_ids = [m["memory_id"] for m in memories if "memory_id" in m]

        return LocomoSample(
            id=f"{conv_id}_summarization",
            query=query,
            expected_ids=expected_ids,
            expected_answer="\n".join(expected_events),
            metadata={
                "type": "summarization",
                "turn_count": len(turns),
                "conversation_id": conv_id,
                "events": expected_events,
            },
            conversation_id=conv_id,
            turns=turns,
            ground_truth_memories=memories,
        )

    def _make_conversation_sample(
        self,
        conv_id: str,
        turns: list[dict[str, str]],
        memories: list[dict[str, str]],
    ) -> LocomoSample:
        """Build a multi-modal conversation sample.

        Uses a mid-conversation turn as query and a later turn as ground truth.
        Tests memory retention across multi-speaker dialogue.
        """
        query_turn = turns[-2] if len(turns) >= 2 else turns[-1]
        query = query_turn.get("text", "")
        expected_ids = [m["memory_id"] for m in memories if "memory_id" in m]
        speakers = list({t.get("speaker", "unknown") for t in turns})

        return LocomoSample(
            id=f"{conv_id}_conversation",
            query=query,
            expected_ids=expected_ids,
            expected_answer=None,
            metadata={
                "type": "conversation",
                "turn_count": len(turns),
                "conversation_id": conv_id,
                "speakers": speakers,
                "query_turn_index": len(turns) - 2 if len(turns) >= 2 else 0,
            },
            conversation_id=conv_id,
            turns=turns,
            ground_truth_memories=memories,
        )

    def to_memories(self, sample: BenchmarkSample) -> SourceDocument:
        """Convert a LoCoMo conversation into memories for memplex ingestion.

        Each ground_truth_memory is converted to a Fact or Observation memory,
        with the full dialogue stored as context.

        For QA samples: creates Fact memories from ground_truth_memories
        For summarization samples: creates Observation memories for events
        """
        from memplex.models.memory import Fact, Observation

        turns = sample.metadata.get("turns", [])
        conv_id = sample.metadata.get("conversation_id", sample.id)
        memories = sample.metadata.get("ground_truth_memories", [])
        sample_type = sample.metadata.get("type", "qa")

        content_parts = []
        for turn in turns:
            speaker = turn.get("speaker", "unknown")
            text = turn.get("text", "")
            timestamp = turn.get("timestamp", "")
            ts_suffix = f" [{timestamp}]" if timestamp else ""
            content_parts.append(f"{speaker}{ts_suffix}: {text}")

        content = "\n".join(content_parts)

        # Convert ground_truth_memories to proper memory types
        memory_objects = []
        for mem in memories:
            mem_id = mem.get("memory_id", f"locomo_mem_{len(memory_objects)}")
            mem_content = mem.get("content", "")
            session_id = mem.get("session_id", "")
            # Persist the turn's real timestamp so the recency dimension has
            # a temporal signal to rank by; fall back to now when the source
            # carries none (or an unparseable one).
            mem_ts = _parse_turn_timestamp(mem.get("timestamp")) or datetime.now(UTC)
            mem_ts_iso = mem_ts.isoformat()

            if sample_type == "summarization":
                # Events -> Observation memories
                obs = Observation(
                    id=mem_id,
                    name=f"Event: {mem_content[:50]}",
                    event=mem_content,
                    context=f"Session: {session_id}",
                    actor="conversation",
                    memory_type="observation",
                    source_type=SourceType.MEETING,
                    created_at=mem_ts_iso,
                    updated_at=mem_ts_iso,
                )
                memory_objects.append(obs)
            else:
                # QA facts -> Fact memories
                fact = Fact(
                    id=mem_id,
                    name=f"Conversation fact: {mem_content[:50]}",
                    subject=session_id or conv_id,
                    predicate="contains",
                    object_=mem_content,
                    memory_type="fact",
                    source_type=SourceType.MEETING,
                    created_at=mem_ts_iso,
                    updated_at=mem_ts_iso,
                )
                memory_objects.append(fact)

        return BenchmarkSourceDocument(
            type="locomo_conversation",
            content=content,
            source_path=f"locomo://{conv_id}",
            source_type=SourceType.MEETING,
            metadata={
                "memory_objects": memory_objects,
                "memory_type": "observation" if sample_type == "summarization" else "fact",
                "conversation_id": conv_id,
                "sample_type": sample_type,
            },
        )


# ── Benchmark runner ───────────────────────────────────────────────────────────


class LocomoRunner(BenchmarkRunner):
    """Runs LoCoMo benchmarks against MemplexService.

    Tests long-term conversation memory retention across three subtasks:
        1. Question answering: recall facts from past dialogue turns
        2. Event summarization: summarize events discussed in conversation
        3. Multi-modal conversation: persona consistency + event tracking
    """

    DATASET_NAME = "locomo"

    def __init__(self, dataset: LocomoDataset | None = None):
        self.dataset = dataset or LocomoDataset()

    def run_retrieval(
        self,
        service: MemplexService,
        samples: list[BenchmarkSample],
        top_k: int = 10,
    ) -> list[BenchmarkResult]:
        """Run retrieval benchmarks on LoCoMo samples.

        For each sample:
            1. Seed memory with the conversation history via write()
            2. Issue the query (a later-turn question)
            3. Check if ground-truth memory IDs appear in top_k results
            4. Compute recall@K, precision@K, MRR
        """
        from benchmarks.metrics import mrr, precision_at_k, recall_at_k

        results: list[BenchmarkResult] = []
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")

        recall_scores: list[float] = []
        precision_scores: list[float] = []
        mrr_scores: list[float] = []
        latencies = LatencyStats()

        for sample in samples:
            # Seed conversation into memplex (outside the timed region; the
            # evaluator also seeds in warm mode, so keep this out of latency)
            source_doc = self.dataset.to_memories(sample)
            service.write(source_doc)

            # Issue the query
            with latencies.timed():
                query_result = service.query(sample.query, top_k=top_k)

            retrieved_ids = [r.func_id for r in query_result.results]
            expected_ids = sample.expected_ids

            recall_scores.append(recall_at_k(retrieved_ids, expected_ids, top_k))
            precision_scores.append(precision_at_k(retrieved_ids, expected_ids, top_k))
            mrr_scores.append(mrr(retrieved_ids, expected_ids))

        n = len(samples)
        if n == 0:
            return []

        def _latency_fields() -> dict[str, float]:
            return {
                "latency_ms": latencies.mean,
                "latency_p50_ms": latencies.p50,
                "latency_p99_ms": latencies.p99,
            }

        results.append(
            BenchmarkResult(
                name="locomo_retrieval",
                dataset=self.DATASET_NAME,
                metric=f"recall@{top_k}",
                value=round(sum(recall_scores) / n, 4),
                samples=n,
                timestamp=timestamp,
                **_latency_fields(),
            )
        )
        results.append(
            BenchmarkResult(
                name="locomo_retrieval",
                dataset=self.DATASET_NAME,
                metric=f"precision@{top_k}",
                value=round(sum(precision_scores) / n, 4),
                samples=n,
                timestamp=timestamp,
                **_latency_fields(),
            )
        )
        results.append(
            BenchmarkResult(
                name="locomo_retrieval",
                dataset=self.DATASET_NAME,
                metric="mrr",
                value=round(sum(mrr_scores) / n, 4),
                samples=n,
                timestamp=timestamp,
                **_latency_fields(),
            )
        )

        # Task-specific sub-metrics
        results.extend(self._run_recency_accuracy(service, samples, top_k, timestamp))
        results.extend(self._run_persona_consistency(service, samples, top_k, timestamp))
        results.extend(self._run_event_tracking(service, samples, top_k, timestamp))

        return results

    def run_generation(
        self,
        service: MemplexService,
        samples: list[BenchmarkSample],
    ) -> list[BenchmarkResult]:
        """Run generation benchmarks on LoCoMo samples.

        For summarization samples, evaluates whether the generated summary
        captures the ground-truth events. For QA samples, evaluates exact
        match and BLEU against expected answer.
        """
        from benchmarks.metrics import bleu, exact_match, rouge_l

        results: list[BenchmarkResult] = []
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")

        bleu_scores: list[float] = []
        rouge_scores: list[float] = []
        em_scores: list[float] = []
        latencies = LatencyStats()

        for sample in samples:
            if sample.expected_answer is None:
                continue

            with latencies.timed():
                query_result = service.query(sample.query, top_k=1)

            prediction = query_result.results[0].summary if query_result.results else ""

            bleu_scores.append(bleu(prediction, sample.expected_answer))
            rouge_scores.append(rouge_l(prediction, sample.expected_answer))
            em_scores.append(exact_match(prediction, sample.expected_answer))

        avg_latency = latencies.mean

        if bleu_scores:
            results.append(
                BenchmarkResult(
                    name="locomo_generation",
                    dataset=self.DATASET_NAME,
                    metric="bleu",
                    value=round(sum(bleu_scores) / len(bleu_scores), 4),
                    latency_ms=avg_latency,
                    samples=len(bleu_scores),
                    timestamp=timestamp,
                    latency_p50_ms=latencies.p50,
                    latency_p99_ms=latencies.p99,
                )
            )
        if rouge_scores:
            results.append(
                BenchmarkResult(
                    name="locomo_generation",
                    dataset=self.DATASET_NAME,
                    metric="rouge_l",
                    value=round(sum(rouge_scores) / len(rouge_scores), 4),
                    latency_ms=avg_latency,
                    samples=len(rouge_scores),
                    timestamp=timestamp,
                    latency_p50_ms=latencies.p50,
                    latency_p99_ms=latencies.p99,
                )
            )
        if em_scores:
            results.append(
                BenchmarkResult(
                    name="locomo_generation",
                    dataset=self.DATASET_NAME,
                    metric="exact_match",
                    value=round(sum(em_scores) / len(em_scores), 4),
                    latency_ms=avg_latency,
                    samples=len(em_scores),
                    timestamp=timestamp,
                    latency_p50_ms=latencies.p50,
                    latency_p99_ms=latencies.p99,
                )
            )

        return results

    # ── Sub-metrics ─────────────────────────────────────────────────────────

    def _run_recency_accuracy(
        self,
        service: MemplexService,
        samples: list[BenchmarkSample],
        top_k: int,
        timestamp: str,
    ) -> list[BenchmarkResult]:
        """Compute recency accuracy: does memplex retrieve most recent memories first?

        Measures how well the recency dimension of the 6-dim reranker orders
        results. Ground truth is temporal ordering from conversation turns.
        """
        scores: list[float] = []
        latencies = LatencyStats()

        for sample in samples:
            # Reconstruct ground_truth_memories from metadata
            gt_memories = sample.metadata.get("ground_truth_memories", [])
            gt_pairs = [
                (m["memory_id"], _parse_turn_timestamp(m.get("timestamp")))
                for m in gt_memories
                if "memory_id" in m
            ]
            # Expected order is temporal (most recent first), matching the
            # docstring; fall back to the source list order when timestamps
            # are missing or unparseable.
            if gt_pairs and all(ts is not None for _, ts in gt_pairs):
                gt_pairs.sort(key=lambda item: item[1], reverse=True)
            expected_order = [mid for mid, _ in gt_pairs]

            with latencies.timed():
                query_result = service.query(sample.query, top_k=top_k)

            retrieved_ids = [r.func_id for r in query_result.results]
            scores.append(self._score_recency(retrieved_ids, expected_order))

        n = len(scores)
        if n == 0:
            return []

        return [
            BenchmarkResult(
                name="locomo_recency",
                dataset=self.DATASET_NAME,
                metric="recency_accuracy",
                value=round(sum(scores) / n, 4),
                latency_ms=latencies.mean,
                samples=n,
                timestamp=timestamp,
                latency_p50_ms=latencies.p50,
                latency_p99_ms=latencies.p99,
            )
        ]

    def _run_persona_consistency(
        self,
        service: MemplexService,
        samples: list[BenchmarkSample],
        top_k: int,
        timestamp: str,
    ) -> list[BenchmarkResult]:
        """Compute persona consistency via token-overlap F1 against a reference.

        Metric definition: for each multi-speaker sample, the reference set is
        the sample's ``expected_answer`` when present, otherwise every turn
        uttered by ``metadata['target_speaker']`` (default: first speaker).
        The prediction is the concatenation of retrieved summaries. The sample
        scores 1.0 when ``token_f1(prediction, reference) >=
        PERSONA_F1_HIT_THRESHOLD`` (0.5, i.e. majority-weighted token overlap
        — the usual "mostly correct" bar in QA partial credit) for at least
        one reference, else 0.0. The reported metric is the mean over scored
        samples.

        Limitations: token overlap measures *content* recovery, not stylistic
        persona fidelity; short references make the 0.5 bar strict, and the
        per-utterance max favours retrieval that recovers any single
        speaker turn. The previous definition (target speaker's name appears
        anywhere in the retrieved text) scored 1.0 without checking content
        and is not comparable.
        """
        scores: list[float] = []
        latencies = LatencyStats()

        for sample in samples:
            speakers = sample.metadata.get("speakers", [])
            if len(speakers) < 2:
                continue  # Need multiple speakers for persona consistency

            target_speaker = sample.metadata.get("target_speaker", speakers[0])
            if sample.expected_answer:
                references = [sample.expected_answer]
            else:
                references = [
                    t.get("text", "")
                    for t in sample.metadata.get("turns", [])
                    if t.get("speaker") == target_speaker and t.get("text", "").strip()
                ]
            if not references:
                continue

            with latencies.timed():
                query_result = service.query(sample.query, top_k=top_k)

            retrieved_text = " ".join(r.summary for r in query_result.results)
            best_f1 = max(token_f1(retrieved_text, ref) for ref in references)
            scores.append(1.0 if best_f1 >= PERSONA_F1_HIT_THRESHOLD else 0.0)

        n = len(scores)
        if n == 0:
            return []

        return [
            BenchmarkResult(
                name="locomo_persona",
                dataset=self.DATASET_NAME,
                metric="persona_consistency",
                value=round(sum(scores) / n, 4),
                latency_ms=latencies.mean,
                samples=n,
                timestamp=timestamp,
                latency_p50_ms=latencies.p50,
                latency_p99_ms=latencies.p99,
            )
        ]

    @staticmethod
    def _event_mentioned(event: str, retrieved_text: str) -> bool:
        """Case-insensitive word-boundary phrase match of ``event`` in text.

        ``retrieved_text`` is matched after case-folding with a regex anchored
        on non-word boundaries, so ``"art"`` does not match ``"Artemis"`` and
        case variants (``"Art"`` vs ``"art"``) still hit. Multi-word event
        phrases must appear verbatim (modulo case).
        """
        needle = event.casefold().strip()
        if not needle:
            return False
        pattern = r"(?<!\w)" + re.escape(needle) + r"(?!\w)"
        return re.search(pattern, retrieved_text.casefold()) is not None

    def _run_event_tracking(
        self,
        service: MemplexService,
        samples: list[BenchmarkSample],
        top_k: int,
        timestamp: str,
    ) -> list[BenchmarkResult]:
        """Compute event tracking: how well does memplex track events across turns?

        Metric definition: per sample, the fraction of ``metadata['events']``
        whose phrase appears in the concatenated retrieved summaries under
        case-insensitive word-boundary matching (see :meth:`_event_mentioned`);
        the reported metric is the mean over samples with events.

        Limitations: verbatim keyword matching gives no credit for
        paraphrases or semantically equivalent phrasing, so this is a lower
        bound on event-recall ability, not a semantic-coverage measure.
        """
        scores: list[float] = []
        latencies = LatencyStats()

        for sample in samples:
            events = sample.metadata.get("events", [])
            if not events:
                continue

            with latencies.timed():
                query_result = service.query(sample.query, top_k=top_k)

            retrieved_text = " ".join(r.summary for r in query_result.results)
            matches = sum(1 for e in events if self._event_mentioned(e, retrieved_text))
            scores.append(matches / len(events))

        n = len(scores)
        if n == 0:
            return []

        return [
            BenchmarkResult(
                name="locomo_events",
                dataset=self.DATASET_NAME,
                metric="event_tracking",
                value=round(sum(scores) / n, 4),
                latency_ms=latencies.mean,
                samples=n,
                timestamp=timestamp,
                latency_p50_ms=latencies.p50,
                latency_p99_ms=latencies.p99,
            )
        ]

    # ── Metric helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _score_recency(retrieved: list[str], expected: list[str]) -> float:
        """Score temporal ordering among the ground-truth memories.

        Projects the retrieved list onto the expected ids (distractors are
        ignored — the metric claims "most recent memories first", not "no
        other relevant memories may rank above them") and returns the
        fraction of expected positions matched in the projected order.
        1.0 = perfect most-recent-first ordering; unretrieved ground truths
        count as misses.
        """
        if not expected or not retrieved:
            return 0.0
        expected_set = set(expected)
        projected = [rid for rid in retrieved if rid in expected_set]
        correct = sum(
            1 for i, eid in enumerate(expected) if i < len(projected) and projected[i] == eid
        )
        return correct / len(expected)


# ── Factory registration ─────────────────────────────────────────────────────────

from benchmarks.base import BenchmarkRunnerFactory

BenchmarkRunnerFactory.register_benchmark(
    name="locomo",
    runner_cls=LocomoRunner,
    dataset_cls=LocomoDataset,
)
