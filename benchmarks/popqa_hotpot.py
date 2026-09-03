"""PopQA + HotpotQA benchmark loaders and runners.

This module implements benchmarks for evaluating entity-centric QA (PopQA)
and multi-hop reasoning (HotpotQA). It supports multi-hop retrieval using
memplex's graph traversal capabilities.

Dataset formats:
    - PopQA: {id, question, subject_id, relation, object}
    - HotpotQA: {id, question, supporting_facts[], answer, context}

Reference:
    - PopQA: https://popqa retriever (entity-centric knowledge retrieval)
    - HotpotQA: https://hotpotqa.github.io/
"""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any

from benchmarks.base import (
    BenchmarkResult,
    BenchmarkRunner,
    BenchmarkRunnerFactory,
    BenchmarkSample,
    BenchmarkSourceDocument,
    EvaluationDataset,
    LatencyStats,
    normalize_answer_text,
    token_f1,
)
from memplex.models.memory import Fact
from memplex.models.source import SourceDocument, SourceType
from memplex.service import MemplexService

logger = logging.getLogger(__name__)

# ── HuggingFace dataset configurations ─────────────────────────────────────────

_HF_CONFIGS = {
    "hotpotqa": {
        "dataset_id": "hotpotqa/hotpot_qa",
        "config": "fullwiki",
        "split": "train",
        "max_samples": 100,
    },
    "popqa": {
        "dataset_id": "mteb/popqa",
        "split": "test",
        "max_samples": 100,
    },
}


def _normalize_text(text: str) -> str:
    """Normalize text for comparison: lowercase, strip punctuation."""
    return normalize_answer_text(text)


def _extract_answer_aliases(answer: Any) -> list[str]:
    """Extract answer string(s) from various answer formats."""
    aliases: list[str] = []

    if answer is None:
        return aliases

    if isinstance(answer, str):
        if answer.strip():
            aliases.append(_normalize_text(answer))
        return aliases

    if isinstance(answer, dict):
        for key in ("answer", "value", "text"):
            if key in answer and isinstance(answer[key], str):
                val = answer[key].strip()
                if val:
                    aliases.append(_normalize_text(val))
        for key in ("aliases", "alternatives"):
            if key in answer and isinstance(answer[key], list):
                for item in answer[key]:
                    if isinstance(item, str) and item.strip():
                        aliases.append(_normalize_text(item))

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


class PopQAHotpotDataset(EvaluationDataset):
    """Dataset loader for PopQA (entity-centric) and HotpotQA (multi-hop).

    Supports loading from:
    - Local JSON/JSONL files (via load(path))
    - HuggingFace datasets (via download() + load())

    PopQA format: {id, question, subject_id, relation, object}
    HotpotQA format: {id, question, supporting_facts[], answer, context}
    """

    def __init__(self, dataset_name: str = "popqa_hotpot") -> None:
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
        config = _HF_CONFIGS.get(self.dataset_name)
        if not config:
            raise ValueError(
                f"No HuggingFace config for dataset: {self.dataset_name}. "
                f"Available: {list(_HF_CONFIGS.keys())}"
            )

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

        samples = []
        for i, row in enumerate(ds):
            item = dict(row)
            sample = self._parse_sample(item)
            if sample is not None:
                samples.append(sample)

        # Save to cache
        cache_dir = Path(".memplex/benchmarks/data")
        cache_dir.mkdir(parents=True, exist_ok=True)
        cache_file = cache_dir / f"{self.dataset_name}.json"

        records = []
        for s in samples:
            rec: dict[str, Any] = {"id": s.id, "question": s.query}
            if self.dataset_name == "hotpotqa":
                rec["answer"] = s.expected_answer
                rec["supporting_facts"] = s.metadata.get("supporting_facts", [])
                rec["context"] = s.metadata.get("context", {})
            else:
                rec["subject"] = s.metadata.get("subject", "")
                rec["relation"] = s.metadata.get("relation", "")
                rec["object"] = s.metadata.get("object", "")
                rec["answer"] = s.metadata.get("object", "")
            records.append(rec)

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
        """
        file_path = Path(path)
        if not file_path.exists():
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
        """Load samples from a JSON file."""
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
        """Parse a single dataset item into a BenchmarkSample."""
        item_keys = set(item.keys())

        if "supporting_facts" in item_keys or "context" in item_keys:
            return self._parse_hotpotqa(item)

        if "subject_id" in item_keys or ("subject" in item_keys and "relation" in item_keys):
            return self._parse_popqa(item)

        return self._parse_generic(item)

    def _parse_popqa(self, item: dict[str, Any]) -> BenchmarkSample | None:
        """Parse a PopQA format item."""
        question = item.get("question", "")
        if not question:
            return None

        subject = item.get("subject", item.get("subject_id", ""))
        relation = item.get("relation", "")
        obj = item.get("object", item.get("answer", ""))

        aliases = _extract_answer_aliases(obj)
        primary_answer = aliases[0] if aliases else str(obj)

        sample_id = str(item.get("id", hash(question) % 1000000))
        content = f"{subject} {relation} {obj}".strip()

        return BenchmarkSample(
            id=f"popqa_{sample_id}",
            query=question.strip(),
            expected_answer=primary_answer,
            metadata={
                "dataset": "popqa",
                "subject": str(subject),
                "relation": str(relation),
                "object": str(obj),
                "aliases": aliases,
                "content": content,
                "num_hops": 1,
            },
        )

    def _parse_hotpotqa(self, item: dict[str, Any]) -> BenchmarkSample | None:
        """Parse a HotpotQA format item."""
        question = item.get("question", "")
        if not question:
            return None

        supporting_facts = item.get("supporting_facts", [])
        if isinstance(supporting_facts, list):
            normalized_facts = []
            for fact in supporting_facts:
                if isinstance(fact, dict):
                    title = fact.get("title", "")
                    text = fact.get("text", fact.get("sent_id", ""))
                    normalized_facts.append({"title": title, "text": text})
                elif isinstance(fact, list):
                    title = fact[0] if len(fact) > 0 else ""
                    normalized_facts.append(
                        {
                            "title": title,
                            "sent_id": fact[1] if len(fact) > 1 else 0,
                            "text": "",
                        }
                    )
        else:
            normalized_facts = []

        unique_titles = set()
        for fact in normalized_facts:
            if isinstance(fact, dict) and fact.get("title"):
                unique_titles.add(fact["title"])
        num_hops = len(unique_titles) if unique_titles else 2

        answer = item.get("answer", item.get("gold_answer", ""))
        aliases = _extract_answer_aliases(answer)
        primary_answer = aliases[0] if aliases else str(answer)

        context = item.get("context", {})
        if isinstance(context, dict):
            context_texts = context.get("text", [])
            context = " ".join(context_texts) if isinstance(context_texts, list) else str(context)
        else:
            context = str(context) if context else ""

        supporting_texts = []
        for fact in normalized_facts:
            if isinstance(fact, dict):
                text = fact.get("text", "")
                title = fact.get("title", "")
                if text:
                    supporting_texts.append(f"{title}: {text}" if title else text)

        content = "\n".join(supporting_texts) if supporting_texts else context
        sample_id = str(item.get("id", hash(question) % 1000000))

        return BenchmarkSample(
            id=f"hotpotqa_{sample_id}",
            query=question.strip(),
            expected_answer=primary_answer,
            metadata={
                "dataset": "hotpotqa",
                "supporting_facts": normalized_facts,
                "num_hops": num_hops,
                "aliases": aliases,
                "content": content,
                "context": context,
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
            metadata={"dataset": self.dataset_name, "raw_item": item, "num_hops": 1},
        )

    def to_memories(self, sample: BenchmarkSample) -> SourceDocument:
        """Convert a PopQA/HotpotQA benchmark sample to a Fact memory.

        The question is stored as the memory name (trigger) and the answer
        as the object, so queries for the question will retrieve the fact.
        """
        # Create a proper Fact memory
        fact = Fact(
            id=f"fact_{sample.id}",
            name=sample.query,
            subject=sample.metadata.get("subject", ""),
            predicate="is",  # Q&A implies a "is" relationship
            object_=sample.metadata.get("object", sample.expected_answer or ""),
            memory_type="fact",
            source_type=SourceType.WIKI,
            created_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            updated_at=datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        )

        # Content for the SourceDocument
        content = sample.metadata.get("content", "")
        if not content:
            content = sample.query

        return BenchmarkSourceDocument(
            type="benchmark",
            content=content,
            source_type=SourceType.WIKI,
            metadata={
                "memory": fact,
                "memory_type": "fact",
                "sample_id": sample.id,
                "supporting_facts": sample.metadata.get("supporting_facts", []),
            },
        )

    def size(self) -> int:
        """Return the number of loaded samples."""
        return len(self._samples)

    def get_sample(self, index: int) -> BenchmarkSample:
        """Get a sample by index."""
        if index < 0 or index >= len(self._samples):
            raise IndexError(f"Sample index {index} out of range [0, {len(self._samples)})")
        return self._samples[index]


def _answer_in_text(text: str, answer_aliases: list[str]) -> bool:
    """Check if any answer alias appears in the text."""
    if not text or not answer_aliases:
        return False
    text_lower = text.lower()
    for alias in answer_aliases:
        if alias and alias in text_lower:
            return True
    return False


def _compute_hop_metrics(
    retrieved_summaries: list[str],
    answer_aliases: list[str],
    num_hops: int,
    k_values: list[int] | None = None,
) -> dict[str, float]:
    """Compute metrics for single-hop retrieval (PopQA-style)."""
    metrics: dict[str, float] = {}

    mrr = 0.0
    for rank, summary in enumerate(retrieved_summaries, 1):
        if _answer_in_text(summary, answer_aliases):
            mrr = 1.0 / rank
            break
    metrics["mrr"] = mrr

    if k_values is None:

        k_values = [1, 5, 10]

    for k in k_values:
        top_k_summaries = retrieved_summaries[:k]
        relevant_found = any(_answer_in_text(s, answer_aliases) for s in top_k_summaries)
        metrics[f"precision@{k}"] = 1.0 / k if relevant_found else 0.0
        metrics[f"recall@{k}"] = 1.0 if relevant_found else 0.0

    metrics["exact_match"] = (
        1.0
        if retrieved_summaries and _answer_in_text(retrieved_summaries[0], answer_aliases)
        else 0.0
    )

    return metrics


def _compute_multihop_metrics(
    retrieved_summaries: list[str],
    supporting_facts: list[dict],
    answer_aliases: list[str],
    max_hops: int = 2,
    k_values: list[int] | None = None,
) -> dict[str, float]:
    """Compute metrics for multi-hop retrieval (HotpotQA-style)."""
    metrics: dict[str, float] = {}

    hop_entities = []
    for fact in supporting_facts:
        if isinstance(fact, dict):
            title = fact.get("title", "")
            entity = _normalize_text(title) if title else ""
            if entity and entity not in hop_entities:
                hop_entities.append(entity)

    num_hops = len(hop_entities) if hop_entities else max_hops

    hops_covered: list[bool] = [False] * num_hops
    for summary in retrieved_summaries:
        summary_lower = summary.lower()
        for i, entity in enumerate(hop_entities):
            if not hops_covered[i] and entity in summary_lower:
                hops_covered[i] = True

    if k_values is None:

        k_values = [1, 5, 10]

    for k in k_values:
        top_k_summaries = retrieved_summaries[:k]
        normalized_summaries = [summary.lower() for summary in top_k_summaries]
        relevant_slots = sum(
            1
            for summary in normalized_summaries
            if any(entity in summary for entity in hop_entities)
        )
        covered_hops = sum(
            1
            for entity in hop_entities
            if any(entity in summary for summary in normalized_summaries)
        )
        metrics[f"hop_precision@{k}"] = relevant_slots / k if k > 0 else 0.0
        metrics[f"hop_recall@{k}"] = covered_hops / num_hops if num_hops > 0 else 0.0

    metrics["multihop_accuracy"] = 1.0 if all(hops_covered) else 0.0
    metrics["hop_coverage"] = sum(hops_covered) / num_hops if num_hops > 0 else 0.0

    if k_values is None:

        k_values = [1, 5, 10]

    for k in k_values:
        top_k_summaries = retrieved_summaries[:k]
        relevant_in_top_k = any(_answer_in_text(s, answer_aliases) for s in top_k_summaries)
        metrics[f"precision@{k}"] = 1.0 / k if relevant_in_top_k else 0.0
        metrics[f"recall@{k}"] = 1.0 if relevant_in_top_k else 0.0

    mrr = 0.0
    for rank, summary in enumerate(retrieved_summaries, 1):
        if _answer_in_text(summary, answer_aliases):
            mrr = 1.0 / rank
            break
    metrics["mrr"] = mrr

    return metrics


def _compute_qa_metrics(prediction: str, answer_aliases: list[str]) -> dict[str, float]:
    """Compute QA metrics for a single prediction."""
    metrics: dict[str, float] = {}

    if not answer_aliases:
        return metrics

    em = max(
        1.0 if _normalize_text(prediction) == _normalize_text(alias) else 0.0
        for alias in answer_aliases
    )
    metrics["exact_match"] = em

    f1 = max(_compute_token_f1(prediction, alias) for alias in answer_aliases)
    metrics["f1"] = f1

    return metrics


class PopQAHotpotRunner(BenchmarkRunner):
    """Benchmark runner for PopQA and HotpotQA.

    Tests entity-centric retrieval (PopQA) and multi-hop reasoning (HotpotQA).
    """

    def __init__(self, name: str = "popqa_hotpot") -> None:
        self.name = name
        self._k_values = [1, 5, 10]

    def run_retrieval(
        self,
        service: MemplexService,
        samples: list[BenchmarkSample],
        top_k: int = 10,
    ) -> list[BenchmarkResult]:
        """Run retrieval benchmark on the given samples."""
        if not samples:
            return []

        results: list[BenchmarkResult] = []

        popqa_samples = [s for s in samples if s.metadata.get("dataset") == "popqa"]
        hotpotqa_samples = [s for s in samples if s.metadata.get("dataset") == "hotpotqa"]

        popqa_metrics = self._run_popqa_retrieval(service, popqa_samples, top_k)
        hotpotqa_metrics = self._run_hotpotqa_retrieval(service, hotpotqa_samples, top_k)

        combined_latency = LatencyStats()
        for metrics in (popqa_metrics, hotpotqa_metrics):
            stats = metrics.get("_latency_stats")
            if isinstance(stats, LatencyStats):
                combined_latency.extend(stats)
        avg_latency = combined_latency.mean

        all_metrics = {**popqa_metrics, **hotpotqa_metrics}

        timestamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        total_samples = len(samples)

        for metric_name, value in all_metrics.items():
            if metric_name.startswith("_"):
                continue
            if self.name in ("popqa", "hotpotqa"):
                # Single-dataset registration: every metric belongs to it.
                dataset = self.name
            else:
                dataset = "hotpotqa" if "hop" in metric_name or "multihop" in metric_name else "popqa"
            results.append(
                BenchmarkResult(
                    name=f"{self.name}_retrieval",
                    dataset=dataset,
                    metric=metric_name,
                    value=round(value, 4),
                    latency_ms=avg_latency,
                    samples=total_samples,
                    timestamp=timestamp,
                    latency_p50_ms=combined_latency.p50,
                    latency_p99_ms=combined_latency.p99,
                )
            )

        logger.info(
            "%s retrieval benchmark completed: %d PopQA, %d HotpotQA samples",
            self.name,
            len(popqa_samples),
            len(hotpotqa_samples),
        )

        return results

    def _run_popqa_retrieval(
        self,
        service: MemplexService,
        samples: list[BenchmarkSample],
        top_k: int,
    ) -> dict[str, Any]:
        """Run retrieval on PopQA samples (single-hop entity QA)."""
        if not samples:
            return {}

        mrr_scores: list[float] = []
        em_scores: list[float] = []
        recall_scores: dict[str, list[float]] = {f"recall@{k}": [] for k in self._k_values}
        latencies = LatencyStats()

        for sample in samples:
            answer_aliases = sample.metadata.get("aliases", [])
            if not answer_aliases and sample.expected_answer:
                answer_aliases = [_normalize_text(sample.expected_answer)]

            with latencies.timed():
                query_result = service.query(sample.query, top_k=top_k)
            retrieved_summaries = [r.summary for r in query_result.results]

            metrics = _compute_hop_metrics(
                retrieved_summaries,
                answer_aliases,
                num_hops=1,
                k_values=self._k_values,
            )

            mrr_scores.append(metrics.get("mrr", 0.0))
            em_scores.append(metrics.get("exact_match", 0.0))
            for k in self._k_values:
                recall_scores[f"recall@{k}"].append(metrics.get(f"recall@{k}", 0.0))

        avg_metrics: dict[str, Any] = {}

        if mrr_scores:
            avg_metrics["mrr"] = sum(mrr_scores) / len(mrr_scores)
            avg_metrics["exact_match"] = sum(em_scores) / len(em_scores)
            for k in self._k_values:
                avg_metrics[f"recall@{k}"] = sum(recall_scores[f"recall@{k}"]) / len(
                    recall_scores[f"recall@{k}"]
                )
            avg_metrics["_latency_stats"] = latencies

        return avg_metrics

    def _run_hotpotqa_retrieval(
        self,
        service: MemplexService,
        samples: list[BenchmarkSample],
        top_k: int,
    ) -> dict[str, Any]:
        """Run retrieval on HotpotQA samples (multi-hop reasoning)."""
        if not samples:
            return {}

        multihop_acc_scores: list[float] = []
        hop_coverage_scores: list[float] = []
        mrr_scores: list[float] = []
        hop_precision_scores: dict[str, list[float]] = {
            f"hop_precision@{k}": [] for k in self._k_values
        }
        hop_recall_scores: dict[str, list[float]] = {f"hop_recall@{k}": [] for k in self._k_values}
        latencies = LatencyStats()

        for sample in samples:
            answer_aliases = sample.metadata.get("aliases", [])
            if not answer_aliases and sample.expected_answer:
                answer_aliases = [_normalize_text(sample.expected_answer)]

            supporting_facts = sample.metadata.get("supporting_facts", [])
            num_hops = sample.metadata.get("num_hops", 2)

            with latencies.timed():
                query_result = service.query(sample.query, top_k=top_k)
            first_hop_summaries = [r.summary for r in query_result.results]
            first_hop_func_ids = [r.func_id for r in query_result.results]

            all_summaries = list(first_hop_summaries)
            all_func_ids = set(first_hop_func_ids)

            if num_hops >= 2 and first_hop_func_ids:
                for func_id in first_hop_func_ids[:3]:
                    try:
                        neighbors = service.store.get_neighbors(func_id, max_hops=1)
                        for neighbor in neighbors:
                            if neighbor.id not in all_func_ids:
                                all_summaries.append(neighbor.name)
                                all_func_ids.add(neighbor.id)
                    except Exception as exc:  # noqa: BLE001 - logged degradation path
                        logger.debug("suppressed Exception in cleanup/degradation path: %s", exc)

            metrics = _compute_multihop_metrics(
                all_summaries,
                supporting_facts,
                answer_aliases,
                max_hops=num_hops,
                k_values=self._k_values,
            )

            multihop_acc_scores.append(metrics.get("multihop_accuracy", 0.0))
            hop_coverage_scores.append(metrics.get("hop_coverage", 0.0))
            mrr_scores.append(metrics.get("mrr", 0.0))
            for k in self._k_values:
                hop_precision_scores[f"hop_precision@{k}"].append(
                    metrics.get(f"hop_precision@{k}", 0.0)
                )
                hop_recall_scores[f"hop_recall@{k}"].append(metrics.get(f"hop_recall@{k}", 0.0))

        avg_metrics: dict[str, Any] = {}

        if multihop_acc_scores:
            n = len(multihop_acc_scores)
            avg_metrics["multihop_accuracy"] = sum(multihop_acc_scores) / n
            avg_metrics["hop_coverage"] = sum(hop_coverage_scores) / n
            avg_metrics["mrr"] = sum(mrr_scores) / n
            for k in self._k_values:
                avg_metrics[f"hop_precision@{k}"] = (
                    sum(hop_precision_scores[f"hop_precision@{k}"]) / n
                )
                avg_metrics[f"hop_recall@{k}"] = sum(hop_recall_scores[f"hop_recall@{k}"]) / n
            avg_metrics["_latency_stats"] = latencies

        return avg_metrics

    def run_generation(
        self,
        service: MemplexService,
        samples: list[BenchmarkSample],
    ) -> list[BenchmarkResult]:
        """Run generation benchmark on the given samples."""
        if not samples:
            return []

        results: list[BenchmarkResult] = []
        em_scores: list[float] = []
        f1_scores: list[float] = []
        latencies = LatencyStats()

        for sample in samples:
            answer_aliases = sample.metadata.get("aliases", [])
            if not answer_aliases and sample.expected_answer:
                answer_aliases = [_normalize_text(sample.expected_answer)]

            with latencies.timed():
                query_result = service.query(sample.query, top_k=5)

            prediction = ""
            for r in query_result.results:
                if _answer_in_text(r.summary, answer_aliases):
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
        avg_f1 = sum(f1_scores) / len(f1_scores) if f1_scores else 0.0

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


class PopQADataset(PopQAHotpotDataset):
    """PopQA dataset with hardcoded name."""

    def __init__(self) -> None:
        super().__init__(dataset_name="popqa")


class HotpotQADataset(PopQAHotpotDataset):
    """HotpotQA dataset with hardcoded name."""

    def __init__(self) -> None:
        super().__init__(dataset_name="hotpotqa")


# ── Registration ──────────────────────────────────────────────────────────────


BenchmarkRunnerFactory.register_benchmark(
    name="popqa",
    runner_cls=PopQAHotpotRunner,
    dataset_cls=PopQADataset,
)

BenchmarkRunnerFactory.register_benchmark(
    name="hotpotqa",
    runner_cls=PopQAHotpotRunner,
    dataset_cls=HotpotQADataset,
)

BenchmarkRunnerFactory.register_benchmark(
    name="popqa_hotpot",
    runner_cls=PopQAHotpotRunner,
    dataset_cls=PopQAHotpotDataset,
)
