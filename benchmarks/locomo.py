"""LoCoMo (ACL 2024) benchmark: long-term conversation memory retention.

LoCoMo dataset format (from https://github.com/snap-research/locomo):
    JSON with {conversation_id, turns[], ground_truth_memories[]}
    Each turn: {speaker, text, timestamp}
    ground_truth_memories: list of {memory_id, content, session_id}

This module provides:
    - LocomoDataset: loads LoCoMo format, converts to BenchmarkSample list
    - LocomoRunner: implements BenchmarkRunner for retrieval + generation
    - Metrics: recency accuracy, persona consistency, event tracking
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from benchmarks.base import (
    BenchmarkResult,
    BenchmarkRunner,
    BenchmarkSample,
    EvaluationDataset,
)
from memplex.models import SourceDocument, SourceType
from memplex.service import MemplexService

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


# ── Internal sample type ───────────────────────────────────────────────────────


@dataclass
class LocomoSample:
    """Internal LoCoMo sample with conversation-specific fields.

    Converted to BenchmarkSample before passing to runners.
    """

    id: str
    query: str
    expected_ids: List[str]
    expected_answer: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    # LoCoMo-specific fields
    conversation_id: str = ""
    turns: List[Dict[str, str]] = field(default_factory=list)
    ground_truth_memories: List[Dict[str, str]] = field(default_factory=list)

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
    """Loads LoCoMo format JSON files.

    Supports:
        - Question answering: resolve a factual question from conversation
        - Event summarization: summarize events discussed in the conversation
        - Multi-modal conversation: multi-speaker dialogue with mixed content types
    """

    def __init__(self, path: Optional[str] = None):
        self.path = path

    def load(self, path: str) -> List[BenchmarkSample]:
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
            raise ValueError(
                f"Unexpected LoCoMo format in {load_path}: "
                "top-level must be dict or list"
            )

        samples: List[BenchmarkSample] = []
        for conv in conversations:
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

    def _make_qa_sample(
        self,
        conv_id: str,
        turns: List[Dict[str, str]],
        memories: List[Dict[str, str]],
    ) -> LocomoSample:
        """Build a question-answering sample from a LoCoMo conversation.

        Query is the last user turn; expected answer is the next assistant turn.
        """
        user_turns = [
            t
            for t in turns
            if t.get("speaker", "").lower() in ("user", "human", "question")
        ]
        assistant_turns = [
            t
            for t in turns
            if t.get("speaker", "").lower() in ("assistant", "system", "agent")
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
        turns: List[Dict[str, str]],
        memories: List[Dict[str, str]],
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
        turns: List[Dict[str, str]],
        memories: List[Dict[str, str]],
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
                    created_at=datetime.utcnow().isoformat(),
                    updated_at=datetime.utcnow().isoformat(),
                )
                memory_objects.append(obs)
            else:
                # QA facts -> Fact memories
                fact = Fact(
                    id=mem_id,
                    name=f"Conversation fact: {mem_content[:50]}",
                    content=mem_content,
                    subject=session_id or conv_id,
                    predicate="contains",
                    object_=mem_content,
                    memory_type="fact",
                    source_type=SourceType.MEETING,
                    created_at=datetime.utcnow().isoformat(),
                    updated_at=datetime.utcnow().isoformat(),
                )
                memory_objects.append(fact)

        return SourceDocument(
            type="locomo_conversation",
            content=content,
            source_path=f"locomo://{conv_id}",
            source_type=SourceType.MEETING,
            metadata={
                "memory_objects": memory_objects,
                "memory_type": "observation"
                if sample_type == "summarization"
                else "fact",
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

    def __init__(self, dataset: Optional[LocomoDataset] = None):
        self.dataset = dataset or LocomoDataset()

    def run_retrieval(
        self,
        service: MemplexService,
        samples: List[BenchmarkSample],
        top_k: int = 10,
    ) -> List[BenchmarkResult]:
        """Run retrieval benchmarks on LoCoMo samples.

        For each sample:
            1. Seed memory with the conversation history via write()
            2. Issue the query (a later-turn question)
            3. Check if ground-truth memory IDs appear in top_k results
            4. Compute recall@K, precision@K, MRR
        """
        from benchmarks.metrics import mrr, precision_at_k, recall_at_k

        results: List[BenchmarkResult] = []
        timestamp = datetime.utcnow().isoformat()

        recall_scores: List[float] = []
        precision_scores: List[float] = []
        mrr_scores: List[float] = []
        latencies: List[int] = []

        for sample in samples:
            # Seed conversation into memplex
            source_doc = self.dataset.to_memories(sample)
            start = datetime.now()
            service.write(source_doc)

            # Issue the query
            query_result = service.query(sample.query, top_k=top_k)
            latency = int((datetime.now() - start).total_seconds() * 1000)
            latencies.append(latency)

            retrieved_ids = [r.func_id for r in query_result.results]
            expected_ids = sample.expected_ids

            recall_scores.append(recall_at_k(retrieved_ids, expected_ids, top_k))
            precision_scores.append(precision_at_k(retrieved_ids, expected_ids, top_k))
            mrr_scores.append(mrr(retrieved_ids, expected_ids))

        n = len(samples)
        if n == 0:
            return []

        avg_latency = int(sum(latencies) / n)

        results.append(
            BenchmarkResult(
                name="locomo_retrieval",
                dataset=self.DATASET_NAME,
                metric="recall@10",
                value=round(sum(recall_scores) / n, 4),
                latency_ms=avg_latency,
                samples=n,
                timestamp=timestamp,
            )
        )
        results.append(
            BenchmarkResult(
                name="locomo_retrieval",
                dataset=self.DATASET_NAME,
                metric="precision@10",
                value=round(sum(precision_scores) / n, 4),
                latency_ms=avg_latency,
                samples=n,
                timestamp=timestamp,
            )
        )
        results.append(
            BenchmarkResult(
                name="locomo_retrieval",
                dataset=self.DATASET_NAME,
                metric="mrr",
                value=round(sum(mrr_scores) / n, 4),
                latency_ms=avg_latency,
                samples=n,
                timestamp=timestamp,
            )
        )

        # Task-specific sub-metrics
        results.extend(self._run_recency_accuracy(service, samples, top_k, timestamp))
        results.extend(
            self._run_persona_consistency(service, samples, top_k, timestamp)
        )
        results.extend(self._run_event_tracking(service, samples, top_k, timestamp))

        return results

    def run_generation(
        self,
        service: MemplexService,
        samples: List[BenchmarkSample],
    ) -> List[BenchmarkResult]:
        """Run generation benchmarks on LoCoMo samples.

        For summarization samples, evaluates whether the generated summary
        captures the ground-truth events. For QA samples, evaluates exact
        match and BLEU against expected answer.
        """
        from benchmarks.metrics import bleu, exact_match, rouge_l

        results: List[BenchmarkResult] = []
        timestamp = datetime.utcnow().isoformat()

        bleu_scores: List[float] = []
        rouge_scores: List[float] = []
        em_scores: List[float] = []
        latencies: List[int] = []

        for sample in samples:
            if sample.expected_answer is None:
                continue

            start = datetime.now()
            query_result = service.query(sample.query, top_k=1)
            latency = int((datetime.now() - start).total_seconds() * 1000)
            latencies.append(latency)

            prediction = query_result.results[0].summary if query_result.results else ""

            bleu_scores.append(bleu(prediction, sample.expected_answer))
            rouge_scores.append(rouge_l(prediction, sample.expected_answer))
            em_scores.append(exact_match(prediction, sample.expected_answer))

        avg_latency = int(sum(latencies) / len(latencies)) if latencies else 0

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
                )
            )

        return results

    # ── Sub-metrics ─────────────────────────────────────────────────────────

    def _run_recency_accuracy(
        self,
        service: MemplexService,
        samples: List[BenchmarkSample],
        top_k: int,
        timestamp: str,
    ) -> List[BenchmarkResult]:
        """Compute recency accuracy: does memplex retrieve most recent memories first?

        Measures how well the recency dimension of the 5-dim reranker orders
        results. Ground truth is temporal ordering from conversation turns.
        """
        scores: List[float] = []
        latencies: List[int] = []

        for sample in samples:
            # Reconstruct ground_truth_memories from metadata
            gt_memories = sample.metadata.get("ground_truth_memories", [])
            expected_order = [m["memory_id"] for m in gt_memories if "memory_id" in m]

            start = datetime.now()
            query_result = service.query(sample.query, top_k=top_k)
            latency = int((datetime.now() - start).total_seconds() * 1000)
            latencies.append(latency)

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
                latency_ms=int(sum(latencies) / n),
                samples=n,
                timestamp=timestamp,
            )
        ]

    def _run_persona_consistency(
        self,
        service: MemplexService,
        samples: List[BenchmarkSample],
        top_k: int,
        timestamp: str,
    ) -> List[BenchmarkResult]:
        """Compute persona consistency: are speaker-specific memories retrieved correctly?

        For multi-speaker conversations, verify that queries about a specific
        speaker retrieve memories attributed to that speaker.
        """
        scores: List[float] = []
        latencies: List[int] = []

        for sample in samples:
            speakers = sample.metadata.get("speakers", [])
            if len(speakers) < 2:
                continue  # Need multiple speakers for persona consistency

            start = datetime.now()
            query_result = service.query(sample.query, top_k=top_k)
            latency = int((datetime.now() - start).total_seconds() * 1000)
            latencies.append(latency)

            target_speaker = sample.metadata.get("target_speaker", speakers[0])
            retrieved_text = " ".join(r.summary.lower() for r in query_result.results)
            score = 1.0 if target_speaker.lower() in retrieved_text else 0.0
            scores.append(score)

        n = len(scores)
        if n == 0:
            return []

        return [
            BenchmarkResult(
                name="locomo_persona",
                dataset=self.DATASET_NAME,
                metric="persona_consistency",
                value=round(sum(scores) / n, 4),
                latency_ms=int(sum(latencies) / n),
                samples=n,
                timestamp=timestamp,
            )
        ]

    def _run_event_tracking(
        self,
        service: MemplexService,
        samples: List[BenchmarkSample],
        top_k: int,
        timestamp: str,
    ) -> List[BenchmarkResult]:
        """Compute event tracking: how well does memplex track events across turns?

        For each event mentioned in ground_truth_memories, check if it is
        retrieved when querying about recent conversation context.
        """
        scores: List[float] = []
        latencies: List[int] = []

        for sample in samples:
            events = sample.metadata.get("events", [])
            if not events:
                continue

            start = datetime.now()
            query_result = service.query(sample.query, top_k=top_k)
            latency = int((datetime.now() - start).total_seconds() * 1000)
            latencies.append(latency)

            retrieved_text = " ".join(r.summary.lower() for r in query_result.results)
            matches = sum(1 for e in events if e.lower() in retrieved_text)
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
                latency_ms=int(sum(latencies) / n),
                samples=n,
                timestamp=timestamp,
            )
        ]

    # ── Metric helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _score_recency(retrieved: List[str], expected: List[str]) -> float:
        """Score how well retrieved order matches temporal (recency) order.

        Returns 1.0 if retrieved order perfectly matches expected (most recent first),
        decreasing as order diverges.
        """
        if not expected or not retrieved:
            return 0.0
        pos_map = {rid: i for i, rid in enumerate(retrieved)}
        correct = sum(
            1 for i, eid in enumerate(expected) if eid in pos_map and pos_map[eid] == i
        )
        return correct / len(expected)


# ── Factory registration ─────────────────────────────────────────────────────────

from benchmarks.base import BenchmarkRunnerFactory

BenchmarkRunnerFactory.register_benchmark(
    name="locomo",
    runner_cls=LocomoRunner,
    dataset_cls=LocomoDataset,
)
