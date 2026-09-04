"""Natural Questions + TriviaQA benchmark loaders and runners.

This module implements benchmarks for evaluating factual recall via open-domain
question answering retrieval. It supports both Natural Questions and TriviaQA
datasets in JSON and JSONL formats.

Dataset formats:
    - Natural Questions: {question_id, question_text, document_token, answer}
    - TriviaQA: {question_id, question_text, answer, evidence_tokens}

Reference:
    - Natural Questions: https://ai.google.com/research/NaturalQuestions
    - TriviaQA: https://nlp.cs.washington.edu/triviaqa/
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any, Optional

from benchmarks.base import (
    BenchmarkResult,
    BenchmarkRunner,
    BenchmarkRunnerFactory,
    BenchmarkSample,
    EvaluationDataset,
    LatencyStats,
    normalize_answer_text,
    token_f1,
)
from memplex.models.source import SourceDocument, SourceType
from memplex.service import MemplexService

logger = logging.getLogger(__name__)

# ── Answer extraction helpers (for HuggingFace configs) ────────────────────────


def _extract_nq_answer(item: dict[str, Any]) -> Any:
    """Extract answer from Natural Questions format."""
    if "annotations" in item and isinstance(item["annotations"], list):
        annotations = item["annotations"]
        if annotations:
            ann = annotations[0]
            if ann.get("short_answers"):
                short_ans = ann["short_answers"][0]
                if isinstance(short_ans, dict):
                    return short_ans.get("text", "")
            if "long_answer" in ann:
                la = ann["long_answer"]
                if isinstance(la, dict):
                    return la.get("text", "")
    return item.get("answer", "")


def _extract_triviaqa_answer(item: dict[str, Any]) -> Any:
    """Extract answer from TriviaQA format."""
    answer = item.get("answer", {})
    if isinstance(answer, dict):
        return answer.get("Value", str(answer))
    return answer


def _build_triviaqa_context(item: dict[str, Any]) -> str:
    """Build context string from TriviaQA search results."""
    search_results = item.get("search_results", {})
    if isinstance(search_results, dict):
        web_results = search_results.get("web_results", [])
        if web_results:
            return " ".join(
                r.get("description", r.get("snippet", ""))
                for r in web_results[:3]
                if isinstance(r, dict)
            )
    return ""


# ── HuggingFace dataset configurations ─────────────────────────────────────────

_HF_CONFIGS = {
    "natural_questions": {
        # Uses sentence-transformers version which is pre-processed and smaller
        "dataset_id": "sentence-transformers/natural-questions",
        "split": "default",
        "max_samples": 100,
        "field_mapping": {
            "id": lambda x: str(x.get("id", "")),
            "question": lambda x: x.get("query", x.get("question", "")),
            "answer": lambda x: x.get("answer", ""),
            "context": lambda x: "",
        },
    },
    "triviaqa": {
        "dataset_id": "mandarjoshi/trivia_qa",
        "config": "rc.nocontext",
        "split": "validation",
        "max_samples": 100,
        "field_mapping": {
            "id": lambda x: str(x.get("question_id", "")),
            "question": lambda x: x.get("question", ""),
            "answer": lambda x: _extract_triviaqa_answer(x),
            "context": lambda x: "",
        },
    },
}

# ── Answer extraction utilities ────────────────────────────────────────────────


def _normalize_text(text: str) -> str:
    """Normalize text for comparison: lowercase, strip punctuation."""
    return normalize_answer_text(text)


def _extract_answer_aliases(answer: Any) -> list[str]:
    """Extract answer string(s) from NQ/TriviaQA answer format.

    NQ answers can be:
        - List of strings (short answers)
        - Dict with 'longAnswerCandidates' and 'annotations'
        - Simple string

    TriviaQA answers can be:
        - Dict with 'Value' string
        - Simple string

    Returns a list of normalized answer strings for flexible matching.
    """
    aliases: list[str] = []

    if answer is None:
        return aliases

    if isinstance(answer, str):
        if answer.strip():
            aliases.append(_normalize_text(answer))
        return aliases

    if isinstance(answer, dict):
        if "Value" in answer and isinstance(answer["Value"], str):
            val = answer["Value"].strip()
            if val:
                aliases.append(_normalize_text(val))
                if val.endswith("."):
                    aliases.append(_normalize_text(val[:-1]))
        if "Aliases" in answer and isinstance(answer["Aliases"], list):
            for alias in answer["Aliases"]:
                if isinstance(alias, str) and alias.strip():
                    aliases.append(_normalize_text(alias))

    if isinstance(answer, list):
        for item in answer:
            aliases.extend(_extract_answer_aliases(item))

    seen: set[str] = set()
    unique: list[str] = []
    for a in aliases:
        if a and a not in seen:
            seen.add(a)
            unique.append(a)

    return unique


def _compute_token_f1(prediction: str, reference: str) -> float:
    """Compute token-level F1 between prediction and reference strings."""
    return token_f1(prediction, reference)


def _exact_match_score(prediction: str, reference: str) -> float:
    """Return 1.0 if normalized strings match exactly, else 0.0."""
    return 1.0 if _normalize_text(prediction) == _normalize_text(reference) else 0.0


# ── Dataset ───────────────────────────────────────────────────────────────────


class NQTriviaDataset(EvaluationDataset):
    """Dataset loader for Natural Questions and TriviaQA.

    Supports loading from:
    - Local JSON/JSONL files (via load(path))
    - HuggingFace datasets (via download() + load())

    Args:
        dataset_name: Optional name for this dataset instance (e.g., "nq", "triviaqa").
    """

    def __init__(self, dataset_name: str = "nq_trivia") -> None:
        self.dataset_name = dataset_name
        self._samples: list[BenchmarkSample] = []
        self._hf_loaded = False

    def download(self, num_samples: int | None = None) -> str:
        """Download dataset from HuggingFace and save locally.

        Args:
            num_samples: Maximum number of samples to download.

        Returns:
            Path to the saved JSON file.
        """
        if self.dataset_name == "natural_questions":
            config = _HF_CONFIGS["natural_questions"]
        elif self.dataset_name == "triviaqa":
            config = _HF_CONFIGS["triviaqa"]
        else:
            # Try generic download
            return self._download_generic(num_samples)

        return self._download_from_huggingface(config, num_samples)

    def _download_from_huggingface(
        self,
        config: dict[str, Any],
        num_samples: int | None = None,
    ) -> str:
        """Download a specific dataset configuration from HuggingFace."""
        try:
            from datasets import load_dataset
        except ImportError as exc:
            raise ImportError(
                "datasets library required for HuggingFace downloads. "
                "Install with: pip install datasets"
            ) from exc

        hf_id = config["dataset_id"]
        split = config["split"]
        max_samples = num_samples or config.get("max_samples", 100)
        config_name = config.get("config")

        logger.info(
            "Downloading %s from HuggingFace: %s (split=%s, max=%d)",
            self.dataset_name,
            hf_id,
            split,
            max_samples,
        )

        try:
            if config_name:
                ds = load_dataset(hf_id, config_name, split=split)
            else:
                ds = load_dataset(hf_id, split=split)
        except Exception as exc:  # noqa: BLE001 - logged degradation path
            logger.warning(
                "Failed to load %s with config %s: %s. Retrying without config...",
                hf_id,
                config_name,
                exc,
            )
            try:
                ds = load_dataset(hf_id, split=split)
            except Exception as exc2:
                raise ValueError(
                    f"Could not download {self.dataset_name} from HuggingFace. "
                    f"Tried: {hf_id} (config={config_name}, split={split}). "
                    f"Error: {exc2}"
                ) from exc2

        if max_samples and max_samples < len(ds):
            ds = ds.select(range(min(max_samples, len(ds))))

        field_map = config["field_mapping"]
        samples = []

        for i, row in enumerate(ds):
            item = dict(row)
            sample_id = field_map["id"](item)
            question = field_map["question"](item)
            answer = field_map["answer"](item)
            context = field_map["context"](item)

            if not question:
                continue

            aliases = _extract_answer_aliases(answer)
            primary_answer = aliases[0] if aliases else ""

            samples.append(
                BenchmarkSample(
                    id=f"{self.dataset_name}_{sample_id or i}",
                    query=question.strip(),
                    expected_answer=primary_answer,
                    metadata={
                        "dataset": self.dataset_name,
                        "aliases": aliases,
                        "context": context,
                        "raw_answer": answer,
                    },
                )
            )

        # Save to cache
        cache_dir = Path(".memplex/benchmarks/data")
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{self.dataset_name}.json"

        records = [
            {
                "id": s.id,
                "question": s.query,
                "answer": s.metadata.get("raw_answer", ""),
                "context": s.metadata.get("context", ""),
            }
            for s in samples
        ]
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2, default=str)

        logger.info(
            "Downloaded %d %s samples to %s",
            len(samples),
            self.dataset_name,
            cache_file,
        )
        self._hf_loaded = True
        return str(cache_file)

    def _download_generic(self, num_samples: int | None = None) -> str:
        """Generic HuggingFace download attempt."""
        hf_id = (
            "natural_questions" if "natural" in self.dataset_name.lower() else "mAlexSie/TriviaQA"
        )
        max_samples = num_samples or 100

        try:
            from datasets import load_dataset
        except ImportError as exc:
            raise ImportError(
                "datasets library required for HuggingFace downloads. "
                "Install with: pip install datasets"
            ) from exc

        logger.info(
            "Attempting generic download for %s from %s",
            self.dataset_name,
            hf_id,
        )

        try:
            if "natural" in self.dataset_name.lower():
                ds = load_dataset("natural_questions", split="validation")
            else:
                ds = load_dataset("mAlexSie/TriviaQA", split="train")
        except Exception:  # noqa: BLE001 - broad catch with explicit fallback handling
            ds = None

        if ds is None:
            raise ValueError(
                f"Could not download {self.dataset_name} from HuggingFace. Tried: {hf_id}"
            )

        if max_samples and max_samples < len(ds):
            ds = ds.select(range(min(max_samples, len(ds))))

        samples = []
        for i, row in enumerate(ds):
            item = dict(row)
            question = item.get("question_text", item.get("question", ""))
            if not question:
                continue

            answer = ""
            if "annotations" in item:
                ann = item["annotations"]
                if isinstance(ann, list) and ann:
                    sa = ann[0].get("short_answers", [])
                    if sa:
                        answer = sa[0].get("text", "")
            if not answer:
                answer = item.get("answer", item.get("answer", ""))

            context = ""
            if "document" in item:
                doc = item["document"]
                if isinstance(doc, dict):
                    context = doc.get("text", "")
                else:
                    context = str(doc)

            aliases = _extract_answer_aliases(answer)
            primary_answer = aliases[0] if aliases else ""

            samples.append(
                BenchmarkSample(
                    id=f"{self.dataset_name}_{i}",
                    query=question.strip(),
                    expected_answer=primary_answer,
                    metadata={
                        "dataset": self.dataset_name,
                        "aliases": aliases,
                        "context": context,
                        "raw_answer": answer,
                    },
                )
            )

        cache_dir = Path(".memplex/benchmarks/data")
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{self.dataset_name}.json"

        records = [
            {
                "id": s.id,
                "question": s.query,
                "answer": s.metadata.get("raw_answer", ""),
                "context": s.metadata.get("context", ""),
            }
            for s in samples
        ]
        with open(cache_file, "w", encoding="utf-8") as f:
            json.dump(records, f, indent=2, default=str)

        logger.info(
            "Downloaded %d %s samples to %s",
            len(samples),
            self.dataset_name,
            cache_file,
        )
        self._hf_loaded = True
        return str(cache_file)

    def load(self, path: str) -> list[BenchmarkSample]:
        """Load benchmark samples from a JSON/JSONL file or HuggingFace.

        First tries to load from the given path. If the file does not exist,
        attempts to download from HuggingFace (caching the result locally).

        Args:
            path: Path to the dataset file, or name of dataset to download.

        Returns:
            List of BenchmarkSample instances.

        Raises:
            FileNotFoundError: If the dataset file does not exist and
                HuggingFace download also fails.
            ValueError: If the file format is not recognized.
        """
        file_path = Path(path)
        if not file_path.exists():
            # Try HuggingFace download first, then fall back to error
            logger.info(
                "File %s not found, attempting HuggingFace download for %s",
                path,
                self.dataset_name,
            )
            try:
                downloaded_path = self.download()
                file_path = Path(downloaded_path)
            except Exception as download_exc:
                raise FileNotFoundError(
                    f"Dataset file not found: {path}, and HuggingFace download failed: {download_exc}"
                ) from download_exc

        samples: list[BenchmarkSample] = []
        file_ext = file_path.suffix.lower()

        try:
            if file_ext == ".jsonl":
                samples = self._load_jsonl(file_path)
            elif file_ext == ".json":
                samples = self._load_json(file_path)
            else:
                with open(file_path, "r", encoding="utf-8") as f:
                    first_char = f.read(1)
                if first_char == "[":
                    samples = self._load_json(file_path)
                else:
                    samples = self._load_jsonl(file_path)

            logger.info(
                "Loaded %d samples from %s (%s)",
                len(samples),
                file_path.name,
                self.dataset_name,
            )
            self._samples = samples
            return samples

        except Exception as exc:
            logger.error("Failed to load dataset from %s: %s", path, exc)
            raise

    def _load_json(self, file_path: Path) -> list[BenchmarkSample]:
        """Load samples from a JSON file.

        Supports two formats:
            1. List of question objects at the root
            2. Dict with 'data' key containing the list (NQ official format)
        """
        with open(file_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict) and "data" in data:
            data = data["data"]

        if not isinstance(data, list):
            raise ValueError(f"Expected list of samples in JSON file, got {type(data).__name__}")  # noqa: TRY004 - exact-type check is deliberate (blocks bool/int equivalence and subclass bypass)

        samples = []
        for item in data:
            sample = self._parse_sample(item)
            if sample is not None:
                samples.append(sample)

        return samples

    def _load_jsonl(self, file_path: Path) -> list[BenchmarkSample]:
        """Load samples from a JSONL (JSON Lines) file."""
        samples = []
        with open(file_path, "r", encoding="utf-8") as f:
            for line_num, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                    sample = self._parse_sample(item)
                    if sample is not None:
                        samples.append(sample)
                except json.JSONDecodeError as exc:
                    logger.warning("Skipping invalid JSON on line %d: %s", line_num, exc)
                    continue

        return samples

    def _parse_sample(self, item: dict[str, Any]) -> BenchmarkSample | None:
        """Parse a single dataset item into a BenchmarkSample.

        Handles both NQ and TriviaQA formats by detecting available fields.
        """
        item_keys = set(item.keys())

        if "answer" in item_keys and "search_results" in item_keys:
            return self._parse_triviaqa(item)

        if "question" in item_keys or "question_text" in item_keys:
            return self._parse_natural_questions(item)

        return self._parse_generic(item)

    def _parse_triviaqa(self, item: dict[str, Any]) -> BenchmarkSample | None:
        """Parse a TriviaQA format item."""
        question = item.get("question", item.get("question_text", ""))
        if not question:
            return None

        answer_data = item.get("answer", {})
        aliases = _extract_answer_aliases(answer_data)
        primary_answer = aliases[0] if aliases else ""

        context = ""
        search_results = item.get("search_results", {})
        if isinstance(search_results, dict):
            web_results = search_results.get("web_results", [])
            if web_results and len(web_results) > 0:
                context = " ".join(
                    r.get("description", r.get("snippet", ""))
                    for r in web_results[:3]
                    if isinstance(r, dict)
                )

        sample_id = str(item.get("question_id", item.get("id", "")))
        if not sample_id:
            return None

        return BenchmarkSample(
            id=f"triviaqa_{sample_id}",
            query=question.strip(),
            expected_answer=primary_answer,
            metadata={
                "dataset": "triviaqa",
                "aliases": aliases,
                "context": context,
                "raw_answer": answer_data,
            },
        )

    def _parse_natural_questions(self, item: dict[str, Any]) -> BenchmarkSample | None:
        """Parse a Natural Questions format item."""
        question = item.get("question", item.get("question_text", ""))
        if not question:
            return None

        answer_data = None

        if "answer" in item:
            answer_data = item["answer"]

        if "annotations" in item and isinstance(item["annotations"], list):
            annotations = item["annotations"]
            if annotations:
                ann = annotations[0]
                if ann.get("short_answers"):
                    short_ans = ann["short_answers"][0]
                    if isinstance(short_ans, dict):
                        answer_data = short_ans.get("text", "")

        context = item.get("context", item.get("document", ""))
        if isinstance(context, dict):
            context = context.get("text", str(context))

        aliases = _extract_answer_aliases(answer_data)
        primary_answer = aliases[0] if aliases else ""

        sample_id = str(item.get("id", item.get("question_id", "")))
        if not sample_id:
            return None

        return BenchmarkSample(
            id=f"nq_{sample_id}",
            query=question.strip(),
            expected_answer=primary_answer,
            metadata={
                "dataset": "natural_questions",
                "aliases": aliases,
                "context": context if context else "",
                "raw_answer": answer_data,
            },
        )

    def _parse_generic(self, item: dict[str, Any]) -> BenchmarkSample | None:
        """Parse a generic item with minimal required fields."""
        question = (
            item.get("query")
            or item.get("question")
            or item.get("question_text")
            or item.get("text")
            or ""
        )
        answer = item.get("answer") or item.get("expected_answer") or item.get("target") or ""

        if not question:
            return None

        sample_id = str(item.get("id", item.get("sample_id", hash(str(question)) % 1000000)))

        return BenchmarkSample(
            id=f"generic_{sample_id}",
            query=question.strip() if isinstance(question, str) else str(question),
            expected_answer=str(answer).strip() if answer else None,
            metadata={"dataset": self.dataset_name, "raw_item": item},
        )

    def to_memories(self, sample: BenchmarkSample) -> SourceDocument:
        """Convert a benchmark sample to a SourceDocument for ingestion.

        For open-domain QA, this creates a SourceDocument from the sample's
        context (Wikipedia paragraph or search results) for seeding into memplex.

        Args:
            sample: The benchmark sample to convert.

        Returns:
            SourceDocument suitable for MemplexService.write().
        """
        context = sample.metadata.get("context", "")
        if context:
            content = f"Question: {sample.query}\n\nContext: {context}"
        else:
            content = sample.query

        return SourceDocument(
            type="benchmark",
            content=content,
            source_type=SourceType.WIKI,
        )

    def size(self) -> int:
        """Return the number of loaded samples."""
        return len(self._samples)

    def get_sample(self, index: int) -> BenchmarkSample:
        """Get a sample by index."""
        if index < 0 or index >= len(self._samples):
            raise IndexError(f"Sample index {index} out of range [0, {len(self._samples)})")
        return self._samples[index]


# ── Metrics helpers ───────────────────────────────────────────────────────────


def _answer_in_summary(
    summary: str,
    answer_aliases: list[str],
) -> bool:
    """Check if any answer alias appears in the summary text."""
    if not summary or not answer_aliases:
        return False
    summary_lower = summary.lower()
    for alias in answer_aliases:
        if alias and alias in summary_lower:
            return True
    return False


def _compute_retrieval_metrics(
    retrieved_summaries: list[str],
    answer_aliases: list[str],
    k_values: list[int] | None = None,
) -> dict[str, float]:
    """Compute retrieval metrics for a single query.

    Args:
        retrieved_summaries: List of result summaries from memplex.query().
        answer_aliases: List of acceptable answer strings (normalized).
        k_values: K values for Precision@K and Recall@K.

    Returns:
        Dict mapping metric names to values.
    """
    metrics: dict[str, float] = {}

    mrr = 0.0
    for rank, summary in enumerate(retrieved_summaries, 1):
        if _answer_in_summary(summary, answer_aliases):
            mrr = 1.0 / rank
            break
    metrics["mrr"] = mrr

    # Aliases are alternative surface forms of ONE answer, not multiple
    # relevant items. Recall@k is therefore binary per question (standard QA
    # convention, same as popqa): 1.0 when any alias appears in the top-k.
    # The previous formula divided by the alias count (often 10-30), which
    # structurally capped recall near 0.1 even on perfect retrieval.
    if k_values is None:
        k_values = [1, 5, 10]
    for k in k_values:
        top_k_summaries = retrieved_summaries[:k]
        relevant_slots = sum(
            1 for summary in top_k_summaries if _answer_in_summary(summary, answer_aliases)
        )

        precision = relevant_slots / k if k > 0 else 0.0
        metrics[f"precision@{k}"] = precision

        metrics[f"recall@{k}"] = 1.0 if relevant_slots > 0 else 0.0

    return metrics


def _compute_qa_metrics(
    prediction: str,
    answer_aliases: list[str],
) -> dict[str, float]:
    """Compute QA metrics for a single prediction.

    Args:
        prediction: Predicted answer string.
        answer_aliases: List of acceptable answer strings.

    Returns:
        Dict mapping metric names to values.
    """
    metrics: dict[str, float] = {}

    if not answer_aliases:
        return metrics

    em = max(_exact_match_score(prediction, alias) for alias in answer_aliases)
    metrics["exact_match"] = em

    f1 = max(_compute_token_f1(prediction, alias) for alias in answer_aliases)
    metrics["f1"] = f1

    return metrics


# ── Runner ───────────────────────────────────────────────────────────────────


class NQTriviaRunner(BenchmarkRunner):
    """Benchmark runner for Natural Questions and TriviaQA.

    Tests factual recall via open-domain QA retrieval. The runner:
    1. Optionally seeds memory with context from the dataset
    2. Issues each question as a query to MemplexService
    3. Checks if retrieved summaries contain the answer string
    4. Computes Precision@K, MRR for retrieval and exact match, F1 for QA

    Args:
        name: Name for this runner instance.
    """

    def __init__(self, name: str = "nq_trivia") -> None:
        self.name = name
        self._k_values = [1, 5, 10]

    def run_retrieval(
        self,
        service: MemplexService,
        samples: list[BenchmarkSample],
        top_k: int = 10,
    ) -> list[BenchmarkResult]:
        """Run retrieval benchmark on the given samples.

        Args:
            service: MemplexService instance to evaluate.
            samples: List of benchmark samples to evaluate.
            top_k: Number of top results to consider.

        Returns:
            List of BenchmarkResult instances with retrieval metrics.
        """
        if not samples:
            return []

        results: list[BenchmarkResult] = []

        recall_scores: dict[str, list[float]] = {f"recall@{k}": [] for k in self._k_values}
        precision_scores: dict[str, list[float]] = {f"precision@{k}": [] for k in self._k_values}
        mrr_scores: list[float] = []
        latencies = LatencyStats()

        for sample in samples:
            answer_aliases = sample.metadata.get("aliases", [])
            if not answer_aliases and sample.expected_answer:
                answer_aliases = [_normalize_text(sample.expected_answer)]

            with latencies.timed():
                query_result = service.query(sample.query, top_k=top_k)

            retrieved_summaries = [r.summary for r in query_result.results]
            if not retrieved_summaries:
                retrieved_summaries = []

            metrics = _compute_retrieval_metrics(
                retrieved_summaries, answer_aliases, self._k_values
            )

            mrr_scores.append(metrics.get("mrr", 0.0))
            for k in self._k_values:
                recall_scores[f"recall@{k}"].append(metrics.get(f"recall@{k}", 0.0))
                precision_scores[f"precision@{k}"].append(metrics.get(f"precision@{k}", 0.0))

        avg_latency = latencies.mean
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")

        avg_mrr = sum(mrr_scores) / len(mrr_scores) if mrr_scores else 0.0
        results.append(
            BenchmarkResult(
                name=f"{self.name}_retrieval",
                dataset=self.name,
                metric="mrr",
                value=round(avg_mrr, 4),
                latency_ms=avg_latency,
                samples=len(samples),
                timestamp=timestamp,
                latency_p50_ms=latencies.p50,
                latency_p99_ms=latencies.p99,
            )
        )

        for k in self._k_values:
            avg_precision = sum(precision_scores[f"precision@{k}"]) / len(
                precision_scores[f"precision@{k}"]
            )
            results.append(
                BenchmarkResult(
                    name=f"{self.name}_retrieval",
                    dataset=self.name,
                    metric=f"precision@{k}",
                    value=round(avg_precision, 4),
                    latency_ms=avg_latency,
                    samples=len(samples),
                    timestamp=timestamp,
                    latency_p50_ms=latencies.p50,
                    latency_p99_ms=latencies.p99,
                )
            )

            avg_recall = sum(recall_scores[f"recall@{k}"]) / len(recall_scores[f"recall@{k}"])
            results.append(
                BenchmarkResult(
                    name=f"{self.name}_retrieval",
                    dataset=self.name,
                    metric=f"recall@{k}",
                    value=round(avg_recall, 4),
                    latency_ms=avg_latency,
                    samples=len(samples),
                    timestamp=timestamp,
                    latency_p50_ms=latencies.p50,
                    latency_p99_ms=latencies.p99,
                )
            )

        logger.info(
            "%s retrieval benchmark: MRR=%.4f, P@10=%.4f, R@10=%.4f",
            self.name,
            avg_mrr,
            sum(precision_scores["precision@10"]) / len(precision_scores["precision@10"]),
            sum(recall_scores["recall@10"]) / len(recall_scores["recall@10"]),
        )

        return results

    def run_generation(
        self,
        service: MemplexService,
        samples: list[BenchmarkSample],
    ) -> list[BenchmarkResult]:
        """Run generation benchmark on the given samples.

        This evaluates the quality of answers generated by retrieving
        relevant memories and checking answer accuracy.

        Args:
            service: MemplexService instance to evaluate.
            samples: List of benchmark samples to evaluate.

        Returns:
            List of BenchmarkResult instances with generation/QA metrics.
        """
        if not samples:
            return []

        results: list[BenchmarkResult] = []
        latencies = LatencyStats()

        em_scores: list[float] = []
        f1_scores: list[float] = []

        for sample in samples:
            answer_aliases = sample.metadata.get("aliases", [])
            if not answer_aliases and sample.expected_answer:
                answer_aliases = [_normalize_text(sample.expected_answer)]

            with latencies.timed():
                query_result = service.query(sample.query, top_k=5)

            prediction = ""
            for r in query_result.results:
                if _answer_in_summary(r.summary, answer_aliases):
                    prediction = r.summary
                    break
            if not prediction and query_result.results:
                prediction = query_result.results[0].summary

            qa_metrics = _compute_qa_metrics(prediction, answer_aliases)
            em_scores.append(qa_metrics.get("exact_match", 0.0))
            f1_scores.append(qa_metrics.get("f1", 0.0))

        avg_latency = latencies.mean
        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")

        avg_em = sum(em_scores) / len(em_scores) if em_scores else 0.0
        results.append(
            BenchmarkResult(
                name=f"{self.name}_generation",
                dataset=self.name,
                metric="exact_match",
                value=round(avg_em, 4),
                latency_ms=avg_latency,
                samples=len(samples),
                timestamp=timestamp,
                latency_p50_ms=latencies.p50,
                latency_p99_ms=latencies.p99,
            )
        )

        avg_f1 = sum(f1_scores) / len(f1_scores) if f1_scores else 0.0
        results.append(
            BenchmarkResult(
                name=f"{self.name}_generation",
                dataset=self.name,
                metric="f1",
                value=round(avg_f1, 4),
                latency_ms=avg_latency,
                samples=len(samples),
                timestamp=timestamp,
                latency_p50_ms=latencies.p50,
                latency_p99_ms=latencies.p99,
            )
        )

        logger.info(
            "%s generation benchmark: EM=%.4f, F1=%.4f",
            self.name,
            avg_em,
            avg_f1,
        )

        return results


# ── Subclasses for specific dataset names ──────────────────────────────────────


class NQDataset(NQTriviaDataset):
    """Natural Questions dataset with hardcoded name."""

    def __init__(self) -> None:
        super().__init__(dataset_name="natural_questions")


class TriviaQADataset(NQTriviaDataset):
    """TriviaQA dataset with hardcoded name."""

    def __init__(self) -> None:
        super().__init__(dataset_name="triviaqa")


# ── Registration ──────────────────────────────────────────────────────────────


BenchmarkRunnerFactory.register_benchmark(
    name="nq",
    runner_cls=NQTriviaRunner,
    dataset_cls=NQDataset,
)

BenchmarkRunnerFactory.register_benchmark(
    name="triviaqa",
    runner_cls=NQTriviaRunner,
    dataset_cls=TriviaQADataset,
)

BenchmarkRunnerFactory.register_benchmark(
    name="nq_trivia",
    runner_cls=NQTriviaRunner,
    dataset_cls=NQTriviaDataset,
)
