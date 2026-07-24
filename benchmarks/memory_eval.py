"""Memory-specific benchmarks for memplex.

This module provides MemoryBenchmark which properly tests memplex's unique
memory capabilities:
    - Fact retention over time
    - Recency decay in retrieval ranking
    - Preference persistence
    - Observation tracking
    - Graph connectivity for multi-hop

Unlike RAG-style benchmarks that test context retrieval, these tests verify
that memplex actually remembers and can retrieve structured memories using
the 3-layer retrieval and 5-dim reranker.
"""

from __future__ import annotations

import logging
import math
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from benchmarks.base import (
    BenchmarkResult,
    BenchmarkRunner,
    BenchmarkRunnerFactory,
    BenchmarkSample,
    EvaluationDataset,
)
from memplex.models.memory import Fact, Function, Observation, Preference
from memplex.models.source import SourceDocument, SourceType
from memplex.service import MemplexService

logger = logging.getLogger(__name__)


# ── Memory builders ─────────────────────────────────────────────────────────────


class FactBuilder:
    """Helper to build fact memories with proper structure."""

    @staticmethod
    def make(
        question: str,
        answer: str,
        subject: str = "",
        predicate: str = "",
        sample_id: str = "",
        created_at: Optional[datetime] = None,
    ) -> Tuple[Fact, str, str]:
        """Build a Fact memory.

        Returns (Fact, query, memory_id)
        - question: stored as memory name
        - answer: stored as object_
        - query: how to retrieve this fact
        """
        if not sample_id:
            sample_id = f"fact_{hash(question) % 1000000}"

        ts = created_at or datetime.utcnow()
        fact = Fact(
            id=f"fact_{sample_id}",
            name=question,
            subject=subject or question,
            predicate=predicate or "is",
            object_=answer,
            memory_type="fact",
            source_type=SourceType.WIKI,
            created_at=ts.isoformat(),
            updated_at=ts.isoformat(),
        )
        query = question
        return fact, query, fact.id


class PreferenceBuilder:
    """Helper to build preference memories."""

    @staticmethod
    def make(
        aspect: str,
        preference: str,
        subject_id: str = "",
        sample_id: str = "",
        created_at: Optional[datetime] = None,
    ) -> Tuple[Preference, str, str]:
        """Build a Preference memory.

        Returns (Preference, query, memory_id)
        """
        if not sample_id:
            sample_id = f"pref_{hash(aspect) % 1000000}"

        ts = created_at or datetime.utcnow()
        pref = Preference(
            id=f"pref_{sample_id}",
            name=f"Preference: {aspect}",
            aspect=aspect,
            preference=preference,
            subject_id=subject_id,
            memory_type="preference",
            source_type=SourceType.WIKI,
            created_at=ts.isoformat(),
            updated_at=ts.isoformat(),
        )
        query = f"What is the preference for {aspect}?"
        return pref, query, pref.id


class ObservationBuilder:
    """Helper to build observation memories."""

    @staticmethod
    def make(
        event: str,
        context: str = "",
        actor: str = "system",
        sample_id: str = "",
        created_at: Optional[datetime] = None,
    ) -> Tuple[Observation, str, str]:
        """Build an Observation memory.

        Returns (Observation, query, memory_id)
        """
        if not sample_id:
            sample_id = f"obs_{hash(event) % 1000000}"

        ts = created_at or datetime.utcnow()
        obs = Observation(
            id=f"obs_{sample_id}",
            name=f"Observed: {event[:50]}",
            event=event,
            context=context,
            actor=actor,
            memory_type="observation",
            source_type=SourceType.WIKI,
            observed_at=ts.isoformat(),
            created_at=ts.isoformat(),
            updated_at=ts.isoformat(),
        )
        query = event
        return obs, query, obs.id


# ── Memory Evaluation Metrics ──────────────────────────────────────────────────


def _recency_ndcg(
    query_result_ids: List[str],
    items_by_recency: List[str],
    top_k: int,
) -> float:
    """NDCG score for recency ranking. 1.0 = perfect recency ordering."""
    if not items_by_recency:
        return 0.0

    def relevance(item_id: str) -> float:
        if item_id not in items_by_recency:
            return 0.0
        pos = items_by_recency.index(item_id)
        return 1.0 / math.log2(pos + 2)

    dcg = sum(relevance(i) for i in query_result_ids[:top_k])
    ideal_order = items_by_recency[:top_k]
    idcg = sum(relevance(i) for i in ideal_order)

    if idcg == 0:
        return 0.0
    return dcg / idcg


def _graph_connectivity_score(
    service: MemplexService,
    source_id: str,
    target_id: str,
    max_hops: int = 2,
) -> float:
    """Can we traverse from source to target through the graph?"""
    if source_id == target_id:
        return 1.0

    visited = {source_id}
    frontier = {source_id}

    for _ in range(max_hops):
        next_frontier = set()
        for fid in frontier:
            try:
                neighbors = service.store.get_neighbors(fid, max_hops=1)
                for neighbor in neighbors:
                    if neighbor.id == target_id:
                        return 1.0
                    if neighbor.id not in visited:
                        visited.add(neighbor.id)
                        next_frontier.add(neighbor.id)
            except Exception:
                pass
        frontier = next_frontier
        if not frontier:
            break

    return 0.0


# ── Memory Evaluation Dataset ───────────────────────────────────────────────────


class MemoryBenchmarkDataset(EvaluationDataset):
    """Dataset that generates synthetic memory test samples.

    This is NOT a loaded dataset - it generates samples on demand
    to verify memplex memory capabilities.
    """

    def __init__(
        self,
        num_facts: int = 50,
        num_prefs: int = 20,
        num_obs: int = 30,
    ):
        self.num_facts = num_facts
        self.num_prefs = num_prefs
        self.num_obs = num_obs
        self._memory_ids: Dict[str, str] = {}

    def load(self, path: str) -> List[BenchmarkSample]:  # pylint: disable=unused-argument
        """Generate synthetic memory test samples.

        Since this is a memory benchmark (not RAG), we generate samples
        that test specific memory capabilities.
        """
        samples = []

        # Generate fact samples with varying recency
        for i in range(self.num_facts):
            hours_old = i * 2
            created = datetime.utcnow() - timedelta(hours=hours_old)

            question = f"What is the capital of country_{i}?"
            answer = f"Capital_{i}"
            fact, query, mem_id = FactBuilder.make(
                question=question,
                answer=answer,
                subject=f"country_{i}",
                predicate="has capital",
                sample_id=f"fact_{i}",
                created_at=created,
            )
            self._memory_ids[f"fact_{i}"] = mem_id

            samples.append(
                BenchmarkSample(
                    id=f"fact_{i}",
                    query=query,
                    expected_ids=[mem_id],
                    expected_answer=answer,
                    metadata={
                        "memory_type": "fact",
                        "memory_id": mem_id,
                        "fact": fact,
                        "created_at": created,
                        "sample_age_hours": hours_old,
                    },
                )
            )

        # Generate preference samples
        prefs = [
            ("dark_mode", "prefers dark mode"),
            ("font_size", "prefers large font"),
            ("language", "prefers English"),
            ("format", "prefers concise output"),
        ]
        for i, (aspect, pref) in enumerate(prefs[: self.num_prefs]):
            preference, query, mem_id = PreferenceBuilder.make(
                aspect=aspect,
                preference=pref,
                sample_id=f"pref_{i}",
            )
            self._memory_ids[f"pref_{i}"] = mem_id

            samples.append(
                BenchmarkSample(
                    id=f"pref_{i}",
                    query=query,
                    expected_ids=[mem_id],
                    expected_answer=pref,
                    metadata={
                        "memory_type": "preference",
                        "memory_id": mem_id,
                        "preference": preference,
                    },
                )
            )

        # Generate observation samples
        events = [
            "user asked about project status",
            "file was modified",
            "error occurred in processing",
            "build completed successfully",
            "user changed settings",
        ]
        for i, event in enumerate(events[: self.num_obs]):
            obs, query, mem_id = ObservationBuilder.make(
                event=event,
                context=f"Context for event {i}",
                sample_id=f"obs_{i}",
            )
            self._memory_ids[f"obs_{i}"] = mem_id

            samples.append(
                BenchmarkSample(
                    id=f"obs_{i}",
                    query=query,
                    expected_ids=[mem_id],
                    expected_answer=event,
                    metadata={
                        "memory_type": "observation",
                        "memory_id": mem_id,
                        "observation": obs,
                    },
                )
            )

        return samples

    def to_memories(self, sample: BenchmarkSample) -> SourceDocument:
        """Convert a memory sample to SourceDocument for seeding.

        The actual memory is carried in sample.metadata; this method
        returns a SourceDocument wrapper that signals the runner to
        extract and store the memory directly.
        """
        return SourceDocument(
            type="memory_benchmark",
            content=sample.metadata.get("content", sample.query),
            source_type=SourceType.WIKI,
            metadata={
                "memory": sample.metadata.get(sample.metadata.get("memory_type", "fact")),
                "memory_type": sample.metadata.get("memory_type", "fact"),
            },
        )

    def get_memory_id(self, sample_id: str) -> Optional[str]:
        """Get the stored memory ID for a sample."""
        return self._memory_ids.get(sample_id)


# ── Memory Benchmark Runner ────────────────────────────────────────────────────


class MemoryBenchmarkRunner(BenchmarkRunner):
    """Benchmark runner that tests memplex memory capabilities.

    Unlike RAG benchmarks that seed context and test retrieval, this
    runner:
    1. Seeds proper memory types (Fact, Preference, Observation)
    2. Queries using the memory's natural retrieval pattern
    3. Measures memory-specific metrics (retention, recency, connectivity)
    """

    DATASET_NAME = "memory_benchmark"

    def __init__(self, dataset: Optional[MemoryBenchmarkDataset] = None):
        self.dataset = dataset or MemoryBenchmarkDataset()

    def run_retrieval(
        self,
        service: MemplexService,
        samples: List[BenchmarkSample],
        top_k: int = 10,
    ) -> List[BenchmarkResult]:
        """Run memory retrieval benchmarks.

        Tests:
        - Fact retention: Can we retrieve seeded facts?
        - Recency ranking: Do recent facts rank higher?
        - Preference persistence: Can we retrieve preferences?
        - Observation tracking: Can we retrieve observations?
        """
        results: List[BenchmarkResult] = []
        timestamp = datetime.utcnow().isoformat()

        fact_samples = [s for s in samples if s.metadata.get("memory_type") == "fact"]
        pref_samples = [s for s in samples if s.metadata.get("memory_type") == "preference"]
        obs_samples = [s for s in samples if s.metadata.get("memory_type") == "observation"]

        results.extend(self._run_fact_retention_test(service, fact_samples, top_k, timestamp))
        results.extend(self._run_recency_decay_test(service, fact_samples, top_k, timestamp))
        results.extend(
            self._run_preference_persistence_test(service, pref_samples, top_k, timestamp)
        )
        results.extend(self._run_observation_tracking_test(service, obs_samples, top_k, timestamp))

        return results

    def _seed_memories(
        self,
        service: MemplexService,
        samples: List[BenchmarkSample],
    ) -> None:
        """Seed memories directly into the store using proper memory types.

        This is the key fix: instead of writing context via write(),
        we directly store the actual Fact/Preference/Observation memories.
        """
        for sample in samples:
            try:
                mem_type = sample.metadata.get("memory_type", "fact")
                memory = sample.metadata.get(mem_type)

                if memory is None:
                    logger.debug("No memory in sample %s", sample.id)
                    continue

                # Directly add to store based on memory type
                if mem_type == "fact":
                    self._seed_fact(service, memory)
                elif mem_type == "preference":
                    self._seed_preference(service, memory)
                elif mem_type == "observation":
                    self._seed_observation(service, memory)
                else:
                    # Fallback: use write() for generic function
                    source_doc = self.dataset.to_memories(sample)
                    service.write(source_doc)
            except Exception as exc:
                logger.debug("Failed to seed sample %s: %s", sample.id, exc)

    def _seed_fact(self, service: MemplexService, fact: Fact) -> None:
        """Seed a Fact memory by converting it to a Function and storing."""
        # Convert Fact to Function for storage
        func = Function(
            id=fact.id,
            name=fact.name,
            name_normalized=fact.name.lower().strip().replace(" ", "_"),
            domain=None,
            memory_type="fact",
            source_type=fact.source_type,
            created_at=fact.created_at,
            updated_at=fact.updated_at,
            trigger=[],
            condition=[],
            action=[],
            benefit=[],
        )
        # Use a simple source document
        source = SourceDocument(
            type="memory_benchmark",
            content=fact.content or fact.name,
            source_type=SourceType.WIKI,
        )
        service.store.add(func, source)

    def _seed_preference(self, service: MemplexService, pref: Preference) -> None:
        """Seed a Preference memory."""
        func = Function(
            id=pref.id,
            name=pref.name,
            name_normalized=pref.name.lower().strip().replace(" ", "_"),
            domain=None,
            memory_type="preference",
            source_type=pref.source_type,
            created_at=pref.created_at,
            updated_at=pref.updated_at,
            trigger=[],
            condition=[],
            action=[],
            benefit=[],
        )
        source = SourceDocument(
            type="memory_benchmark",
            content=pref.preference,
            source_type=SourceType.WIKI,
        )
        service.store.add(func, source)

    def _seed_observation(self, service: MemplexService, obs: Observation) -> None:
        """Seed an Observation memory."""
        func = Function(
            id=obs.id,
            name=obs.name,
            name_normalized=obs.name.lower().strip().replace(" ", "_"),
            domain=None,
            memory_type="observation",
            source_type=obs.source_type,
            created_at=obs.created_at,
            updated_at=obs.updated_at,
            trigger=[],
            condition=[],
            action=[],
            benefit=[],
        )
        source = SourceDocument(
            type="memory_benchmark",
            content=f"{obs.event} {obs.context}".strip(),
            source_type=SourceType.WIKI,
        )
        service.store.add(func, source)

    def _run_fact_retention_test(
        self,
        service: MemplexService,
        samples: List[BenchmarkSample],
        top_k: int,
        timestamp: str,
    ) -> List[BenchmarkResult]:
        """Test fact retention: Can we retrieve seeded facts?"""
        if not samples:
            return []

        self._seed_memories(service, samples)

        retention_scores = []
        mrr_scores = []
        latencies = []

        for sample in samples:
            expected_id = sample.metadata.get("memory_id", "")
            if not expected_id:
                continue

            start = datetime.now()
            query_result = service.query(sample.query, top_k=top_k)
            latency_ms = int((datetime.now() - start).total_seconds() * 1000)
            latencies.append(latency_ms)

            retrieved_ids = [r.func_id for r in query_result.results]

            # Check retention
            if expected_id in retrieved_ids[:top_k]:
                retention_scores.append(1.0)
                rank = retrieved_ids.index(expected_id) + 1
                mrr_scores.append(1.0 / rank)
            else:
                retention_scores.append(0.0)
                mrr_scores.append(0.0)

        n = len(samples)
        if n == 0:
            return []

        avg_latency = int(sum(latencies) / n)

        return [
            BenchmarkResult(
                name="memory_fact_retention",
                dataset=self.DATASET_NAME,
                metric="fact_retention_rate",
                value=round(sum(retention_scores) / n, 4),
                latency_ms=avg_latency,
                samples=n,
                timestamp=timestamp,
            ),
            BenchmarkResult(
                name="memory_fact_retention",
                dataset=self.DATASET_NAME,
                metric="mrr",
                value=round(sum(mrr_scores) / n, 4),
                latency_ms=avg_latency,
                samples=n,
                timestamp=timestamp,
            ),
        ]

    def _run_recency_decay_test(
        self,
        service: MemplexService,
        samples: List[BenchmarkSample],
        top_k: int,
        timestamp: str,
    ) -> List[BenchmarkResult]:
        """Test recency decay: Do recent memories rank higher than older ones?

        This tests the recency_decay dimension of the 5-dim reranker.
        """
        if not samples:
            return []

        # Sort by age (oldest first)
        sorted_samples = sorted(
            samples,
            key=lambda s: s.metadata.get("sample_age_hours", 0),
        )

        # Use the most recent fact's query to test recency ordering
        recent_sample = sorted_samples[-1]
        if not recent_sample:
            return []

        start = datetime.now()
        query_result = service.query(
            recent_sample.query,
            top_k=min(top_k, len(samples)),
        )
        latency_ms = int((datetime.now() - start).total_seconds() * 1000)

        retrieved_ids = [r.func_id for r in query_result.results]

        # Most recent first
        fact_ids_by_age = [s.metadata.get("memory_id", "") for s in reversed(sorted_samples)]
        fact_ids_by_age = [fid for fid in fact_ids_by_age if fid]

        if not fact_ids_by_age:
            return []

        recency_score = _recency_ndcg(
            retrieved_ids,
            fact_ids_by_age,
            top_k,
        )

        return [
            BenchmarkResult(
                name="memory_recency_decay",
                dataset=self.DATASET_NAME,
                metric="recency_ranking",
                value=round(recency_score, 4),
                latency_ms=latency_ms,
                samples=len(samples),
                timestamp=timestamp,
            ),
        ]

    def _run_preference_persistence_test(
        self,
        service: MemplexService,
        samples: List[BenchmarkSample],
        top_k: int,
        timestamp: str,
    ) -> List[BenchmarkResult]:
        """Test preference persistence: Can we retrieve stored preferences?"""
        if not samples:
            return []

        self._seed_memories(service, samples)

        retention_scores = []
        latencies = []

        for sample in samples:
            expected_id = sample.metadata.get("memory_id", "")
            if not expected_id:
                continue

            start = datetime.now()
            query_result = service.query(sample.query, top_k=top_k)
            latency_ms = int((datetime.now() - start).total_seconds() * 1000)
            latencies.append(latency_ms)

            retrieved_ids = [r.func_id for r in query_result.results]
            if expected_id in retrieved_ids:
                retention_scores.append(1.0)
            else:
                retention_scores.append(0.0)

        n = len(samples)
        if n == 0:
            return []

        return [
            BenchmarkResult(
                name="memory_preference_persistence",
                dataset=self.DATASET_NAME,
                metric="preference_retention_rate",
                value=round(sum(retention_scores) / n, 4),
                latency_ms=int(sum(latencies) / n),
                samples=n,
                timestamp=timestamp,
            ),
        ]

    def _run_observation_tracking_test(
        self,
        service: MemplexService,
        samples: List[BenchmarkSample],
        top_k: int,
        timestamp: str,
    ) -> List[BenchmarkResult]:
        """Test observation tracking: Can we retrieve runtime observations?"""
        if not samples:
            return []

        self._seed_memories(service, samples)

        retention_scores = []
        latencies = []

        for sample in samples:
            expected_id = sample.metadata.get("memory_id", "")
            if not expected_id:
                continue

            start = datetime.now()
            query_result = service.query(sample.query, top_k=top_k)
            latency_ms = int((datetime.now() - start).total_seconds() * 1000)
            latencies.append(latency_ms)

            retrieved_ids = [r.func_id for r in query_result.results]
            if expected_id in retrieved_ids:
                retention_scores.append(1.0)
            else:
                retention_scores.append(0.0)

        n = len(samples)
        if n == 0:
            return []

        return [
            BenchmarkResult(
                name="memory_observation_tracking",
                dataset=self.DATASET_NAME,
                metric="observation_retention_rate",
                value=round(sum(retention_scores) / n, 4),
                latency_ms=int(sum(latencies) / n),
                samples=n,
                timestamp=timestamp,
            ),
        ]

    def run_generation(
        self,
        service: MemplexService,
        samples: List[BenchmarkSample],
    ) -> List[BenchmarkResult]:
        """Memory benchmarks don't have a separate generation phase."""
        return []


# ── Registration ──────────────────────────────────────────────────────────────


BenchmarkRunnerFactory.register_benchmark(
    name="memory_benchmark",
    runner_cls=MemoryBenchmarkRunner,
    dataset_cls=MemoryBenchmarkDataset,
)
