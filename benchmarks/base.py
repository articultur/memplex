"""Base benchmark framework for memplex evaluation.

Defines core interfaces: BenchmarkSample, BenchmarkResult, EvaluationDataset,
BenchmarkRunner, and BenchmarkRunnerFactory.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Type

from memplex.models.source import SourceDocument
from memplex.service import MemplexService

logger = logging.getLogger(__name__)


# ── Data Classes ──────────────────────────────────────────────────────────────


@dataclass
class BenchmarkSample:
    """A single benchmark sample containing query and ground truth.

    Attributes:
        id: Unique identifier for this sample.
        query: The search/retrieval query text.
        expected_ids: Ground-truth memory IDs for retrieval benchmarks.
        expected_answer: Optional ground-truth answer for generation benchmarks.
        metadata: Additional sample metadata (e.g., source dataset, difficulty).
    """

    id: str
    query: str
    expected_ids: List[str] = field(default_factory=list)
    expected_answer: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("BenchmarkSample.id must not be empty")
        if not self.query:
            raise ValueError("BenchmarkSample.query must not be empty")


@dataclass
class BenchmarkResult:
    """Result of a single benchmark run.

    Attributes:
        name: Name of the benchmark (e.g., "locomo_retrieval").
        dataset: Dataset name (e.g., "locomo_val").
        metric: Metric name (e.g., "recall@5", "mrr", "precision@10").
        value: Computed metric value (0.0 to 1.0 typically).
        latency_ms: Latency of the retrieval call in milliseconds.
        samples: Number of samples evaluated.
        timestamp: ISO timestamp of when the benchmark ran.
    """

    name: str
    dataset: str
    metric: str
    value: float
    latency_ms: int
    samples: int
    timestamp: str = field(default_factory=lambda: datetime.utcnow().isoformat() + "Z")

    def to_dict(self) -> Dict[str, Any]:
        """Serialize to dict for JSON serialization."""
        return {
            "benchmark": self.name,
            "dataset": self.dataset,
            "metric": self.metric,
            "value": self.value,
            "latency_ms": self.latency_ms,
            "samples": self.samples,
            "timestamp": self.timestamp,
        }


# ── Abstract Interfaces ───────────────────────────────────────────────────────


@dataclass
class BenchmarkSourceDocument(SourceDocument):
    """SourceDocument carrying benchmark memory payloads for direct seeding.

    ``SourceDocument`` itself has no metadata field; benchmark datasets use
    this subclass to hand Fact/Preference/Observation objects to
    ``BenchmarkEvaluator._seed_memories`` alongside the document content.
    """

    metadata: Dict[str, Any] = field(default_factory=dict)


class EvaluationDataset(ABC):
    """Abstract base for loading benchmark datasets.

    Each benchmark dataset (LoCoMo, NQ, TriviaQA, etc.) implements this
    interface to provide BenchmarkSample instances and convert them to
    SourceDocument for memplex ingestion.
    """

    @abstractmethod
    def load(self, path: str) -> List[BenchmarkSample]:
        """Load benchmark samples from a file or directory path.

        Args:
            path: Path to the dataset file (JSON, JSONL, etc.).

        Returns:
            List of BenchmarkSample instances.
        """
        ...

    @abstractmethod
    def to_memories(self, sample: BenchmarkSample) -> SourceDocument:
        """Convert a benchmark sample to a SourceDocument for ingestion.

        Args:
            sample: The benchmark sample to convert.

        Returns:
            SourceDocument suitable for passing to MemplexService.write().
        """
        ...


class BenchmarkRunner(ABC):
    """Abstract base for running benchmarks against MemplexService.

    Each benchmark (retrieval, generation) implements this interface
    to evaluate memplex on specific task types.
    """

    @abstractmethod
    def run_retrieval(
        self,
        service: MemplexService,
        samples: List[BenchmarkSample],
        top_k: int = 10,
    ) -> List[BenchmarkResult]:
        """Run retrieval benchmark.

        Args:
            service: MemplexService instance to evaluate.
            samples: List of benchmark samples to evaluate.
            top_k: Number of top results to consider for metrics.

        Returns:
            List of BenchmarkResult instances, one per metric computed.
        """
        ...

    @abstractmethod
    def run_generation(
        self,
        service: MemplexService,
        samples: List[BenchmarkSample],
    ) -> List[BenchmarkResult]:
        """Run generation benchmark.

        Args:
            service: MemplexService instance to evaluate.
            samples: List of benchmark samples to evaluate.

        Returns:
            List of BenchmarkResult instances, one per metric computed.
        """
        ...


# ── Factory ────────────────────────────────────────────────────────────────────


class BenchmarkRunnerFactory:
    """Factory for creating benchmark runner instances.

    Provides a registry of available benchmarks that can be instantiated
    by name for use in BenchmarkEvaluator.
    """

    _runners: Dict[str, Type[BenchmarkRunner]] = {}
    _datasets: Dict[str, Type[EvaluationDataset]] = {}

    @classmethod
    def register_runner(cls, name: str, runner_cls: Type[BenchmarkRunner]) -> None:
        """Register a benchmark runner class.

        Args:
            name: Unique name for the benchmark (e.g., "locomo").
            runner_cls: BenchmarkRunner subclass to instantiate.
        """
        if not issubclass(runner_cls, BenchmarkRunner):
            raise TypeError(f"{runner_cls.__name__} must be a BenchmarkRunner subclass")
        cls._runners[name] = runner_cls
        logger.debug("Registered benchmark runner: %s", name)

    @classmethod
    def register_dataset(cls, name: str, dataset_cls: Type[EvaluationDataset]) -> None:
        """Register a dataset class.

        Args:
            name: Unique name for the dataset (e.g., "locomo").
            dataset_cls: EvaluationDataset subclass to instantiate.
        """
        if not issubclass(dataset_cls, EvaluationDataset):
            raise TypeError(f"{dataset_cls.__name__} must be an EvaluationDataset subclass")
        cls._datasets[name] = dataset_cls
        logger.debug("Registered dataset: %s", name)

    @classmethod
    def create_runner(cls, name: str) -> BenchmarkRunner:
        """Create a benchmark runner instance by name.

        Args:
            name: Name of the runner to create.

        Returns:
            New BenchmarkRunner instance.

        Raises:
            KeyError: If no runner is registered under that name.
        """
        if name not in cls._runners:
            available = list(cls._runners.keys())
            raise KeyError(f"No runner registered for '{name}'. Available: {available}")
        runner = cls._runners[name]()
        # Runners shared across registrations (e.g. NQTriviaRunner serves
        # "nq"/"triviaqa"/"nq_trivia") default their label to the composite
        # name, which mislabels per-dataset results in JSONL output. Sync the
        # label with the registered name so results are attributable.
        if hasattr(runner, "name"):
            runner.name = name
        return runner

    @classmethod
    def create_dataset(cls, name: str) -> EvaluationDataset:
        """Create a dataset instance by name.

        Args:
            name: Name of the dataset to create.

        Returns:
            New EvaluationDataset instance.

        Raises:
            KeyError: If no dataset is registered under that name.
        """
        if name not in cls._datasets:
            available = list(cls._datasets.keys())
            raise KeyError(f"No dataset registered for '{name}'. Available: {available}")
        return cls._datasets[name]()

    @classmethod
    def available_runners(cls) -> List[str]:
        """Return list of registered runner names."""
        return list(cls._runners.keys())

    @classmethod
    def available_datasets(cls) -> List[str]:
        """Return list of registered dataset names."""
        return list(cls._datasets.keys())

    @classmethod
    def register_benchmark(
        cls,
        name: str,
        runner_cls: Type[BenchmarkRunner],
        dataset_cls: Type[EvaluationDataset],
    ) -> None:
        """Register both runner and dataset for a benchmark at once.

        Args:
            name: Unique name for the benchmark.
            runner_cls: BenchmarkRunner subclass.
            dataset_cls: EvaluationDataset subclass.
        """
        cls.register_runner(name, runner_cls)
        cls.register_dataset(name, dataset_cls)
