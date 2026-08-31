"""Memplex benchmark framework for evaluating memory retrieval and storage."""

from .base import (
    BenchmarkResult,
    BenchmarkRunner,
    BenchmarkRunnerFactory,
    BenchmarkSample,
    BenchmarkSourceDocument,
    EvaluationDataset,
)
from .evaluator import (
    BenchmarkEvaluator,
)
from .loader import (
    download_dataset,
    list_available_datasets,
)
from .locomo import (
    LocomoDataset,
    LocomoRunner,
)
from .longmemeval import (
    LongMemEvalDataset,
    LongMemEvalRunner,
)
from .memory_eval import (
    MemoryBenchmarkDataset,
    MemoryBenchmarkRunner,
)
from .metrics import (
    MemoryMetrics,
    aggregate_metrics,
    answer_contains,
    bleu,
    exact_match,
    f1_score,
    mrr,
    ndcg_at_k,
    precision_at_k,
    recall_at_k,
    rouge_l,
)
from .nq_trivia import (
    NQDataset,
    NQTriviaDataset,
    NQTriviaRunner,
    TriviaQADataset,
)
from .popqa_hotpot import (
    HotpotQADataset,
    PopQADataset,
    PopQAHotpotDataset,
    PopQAHotpotRunner,
)

__all__ = [
    # Base
    "BenchmarkSample",
    "BenchmarkResult",
    "EvaluationDataset",
    "BenchmarkRunner",
    "BenchmarkRunnerFactory",
    "BenchmarkSourceDocument",
    # Metrics
    "precision_at_k",
    "recall_at_k",
    "mrr",
    "ndcg_at_k",
    "bleu",
    "rouge_l",
    "f1_score",
    "exact_match",
    "answer_contains",
    "MemoryMetrics",
    "aggregate_metrics",
    # NQ + TriviaQA
    "NQDataset",
    "NQTriviaDataset",
    "NQTriviaRunner",
    "TriviaQADataset",
    # PopQA + HotpotQA
    "HotpotQADataset",
    "PopQADataset",
    "PopQAHotpotDataset",
    "PopQAHotpotRunner",
    # LoCoMo
    "LocomoDataset",
    "LocomoRunner",
    # LongMemEval
    "LongMemEvalDataset",
    "LongMemEvalRunner",
    # Memory benchmark
    "MemoryBenchmarkDataset",
    "MemoryBenchmarkRunner",
    # Evaluator + Loader
    "BenchmarkEvaluator",
    "download_dataset",
    "list_available_datasets",
]
