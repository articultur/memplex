"""Dataset loader utility with auto-download support.

Provides ``download_dataset()`` which:
    1. Tries HuggingFace datasets (when network is available)
    2. Falls back to generating synthetic data for testing/development

Supported datasets and their HuggingFace IDs:
    - popqa:     "mteb/popqa"
    - hotpotqa:  "hotpotqa"
    - nq:        "natural_questions"
    - triviaqa:  "triviaqa"
    - locomo:    (GitHub SNAP Research, synthetic fallback)
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


# ── HuggingFace dataset IDs ────────────────────────────────────────────────────

HF_DATASET_IDS: Dict[str, str] = {
    "popqa": "mteb/popqa",
    "hotpotqa": "hotpotqa",
    "nq": "natural_questions",
    "triviaqa": "triviaqa",
}


def _fetch_from_huggingface(
    dataset_name: str,
    split: str = "test",
    num_samples: Optional[int] = None,
) -> Optional[Path]:
    """Try to download a dataset from HuggingFace.

    Returns the path to the cached JSON file, or ``None`` on failure.
    """
    hf_id = HF_DATASET_IDS.get(dataset_name)
    if not hf_id:
        return None

    try:
        from datasets import load_dataset

        logger.info("Fetching %s from HuggingFace: %s", dataset_name, hf_id)

        if dataset_name == "hotpotqa":
            ds = load_dataset("hotpotqa/hotpot_qa", "fullwiki", split=split)
        elif dataset_name == "nq":
            ds = load_dataset("natural_questions", split=split)
        elif dataset_name == "triviaqa":
            ds = load_dataset("triviaqa", "rc", split=split)
        elif dataset_name == "popqa":
            ds = load_dataset("mteb/popqa", split=split)
        else:
            ds = load_dataset(hf_id, split=split)

        if num_samples is not None:
            ds = ds.select(range(min(num_samples, len(ds))))

        records = [dict(row) for row in ds]

        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as fh:
            json.dump(records, fh, indent=2, default=str)
            tmp_path = fh.name

        logger.info(
            "Downloaded %d %s samples from HuggingFace to %s",
            len(records),
            dataset_name,
            tmp_path,
        )
        return Path(tmp_path)

    except Exception as exc:
        logger.warning(
            "HuggingFace fetch failed for %s (%s): %s. Falling back to synthetic data.",
            dataset_name,
            hf_id,
            exc,
        )
        return None


# ── Synthetic data generators ───────────────────────────────────────────────────


def _generate_popqa_synthetic(path: Path) -> Path:
    """Generate synthetic PopQA-like samples for development/testing."""
    samples = [
        {
            "id": f"popqa_{i}",
            "question": question,
            "subject": subject,
            "relation": relation,
            "object": obj,
            "subject_id": f"entity_{i}",
        }
        for i, (question, subject, relation, obj) in enumerate(
            [
                (
                    "What city is the Eiffel Tower in?",
                    "Eiffel Tower",
                    "located in",
                    "Paris",
                ),
                (
                    "Who wrote Romeo and Juliet?",
                    "Romeo and Juliet",
                    "author",
                    "William Shakespeare",
                ),
                ("What is the capital of Japan?", "Japan", "capital", "Tokyo"),
                (
                    "What company created Python?",
                    "Python",
                    "created by",
                    "Guido van Rossum",
                ),
                (
                    "What year did Apollo 11 land?",
                    "Apollo 11",
                    "year of landing",
                    "1969",
                ),
            ],
            start=1,
        )
    ]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(samples, fh, indent=2)
    logger.info("Generated %d synthetic PopQA samples at %s", len(samples), path)
    return path


def _generate_hotpotqa_synthetic(path: Path) -> Path:
    """Generate synthetic HotpotQA-like samples for development/testing."""
    samples = [
        {
            "id": f"hotpotqa_{i}",
            "question": question,
            "answer": answer,
            "supporting_facts": [{"title": t, "text": tx} for t, tx in facts],
            "context": {"text": [f"{t}: {tx}" for t, tx in facts]},
        }
        for i, (question, answer, facts) in enumerate(
            [
                (
                    "What is the capital of the country where the Eiffel Tower is located?",
                    "Paris",
                    [
                        ("Eiffel Tower", "Landmark in Paris."),
                        ("France", "Country with capital Paris."),
                    ],
                ),
                (
                    "Who is the author of The Great Gatsby?",
                    "F. Scott Fitzgerald",
                    [
                        ("The Great Gatsby", "Novel by F. Scott Fitzgerald."),
                        ("F. Scott Fitzgerald", "American author."),
                    ],
                ),
                (
                    "What is the largest planet in our solar system?",
                    "Jupiter",
                    [
                        ("Jupiter", "Largest planet in the solar system."),
                        ("Solar System", "Contains 8 planets."),
                    ],
                ),
            ],
            start=1,
        )
    ]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(samples, fh, indent=2)
    logger.info("Generated %d synthetic HotpotQA samples at %s", len(samples), path)
    return path


def _generate_nq_synthetic(path: Path) -> Path:
    """Generate synthetic Natural Questions-like samples."""
    samples = [
        {
            "id": f"nq_{i}",
            "question_text": question,
            "question": question,
            "answer": [answer] if isinstance(answer, str) else answer,
        }
        for i, (question, answer) in enumerate(
            [
                ("Who is the president of the United States?", "Barack Obama"),
                ("What is the capital of France?", "Paris"),
                ("How many continents are there?", "7"),
                ("What is the largest ocean?", "Pacific Ocean"),
                ("Who wrote Hamlet?", "William Shakespeare"),
            ],
            start=1,
        )
    ]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(samples, fh, indent=2)
    logger.info("Generated %d synthetic NQ samples at %s", len(samples), path)
    return path


def _generate_locomo_synthetic(path: Path) -> Path:
    """Generate synthetic LoCoMo-like conversation samples."""
    samples = [
        {
            "conversation_id": f"locomo_conv_{i}",
            "type": "qa",
            "turns": [
                {"speaker": "user", "text": q, "timestamp": f"2024-01-0{i}T10:00:00"},
                {
                    "speaker": "assistant",
                    "text": a,
                    "timestamp": f"2024-01-0{i}T10:00:05",
                },
            ],
            "ground_truth_memories": [
                {"memory_id": f"mem_{i}_0", "content": q},
                {"memory_id": f"mem_{i}_1", "content": a},
            ],
        }
        for i, (q, a) in enumerate(
            [
                (
                    "What is Python used for?",
                    "Python is a versatile programming language used for web development, data science, AI, and automation.",
                ),
                (
                    "How do I define a function in Python?",
                    "In Python, you define a function using the 'def' keyword, followed by the function name and parameters.",
                ),
                (
                    "What is a list in Python?",
                    "A list is an ordered, mutable collection of items in Python, defined with square brackets.",
                ),
            ],
            start=1,
        )
    ]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(samples, fh, indent=2)
    logger.info("Generated %d synthetic LoCoMo samples at %s", len(samples), path)
    return path


_SYNTHETIC_GENERATORS: Dict[str, callable] = {
    "popqa": _generate_popqa_synthetic,
    "hotpotqa": _generate_hotpotqa_synthetic,
    "nq": _generate_nq_synthetic,
    "triviaqa": _generate_nq_synthetic,  # Reuse NQ format
    "locomo": _generate_locomo_synthetic,
}


# ── Public API ─────────────────────────────────────────────────────────────────


def download_dataset(
    dataset_name: str,
    output_dir: Optional[str] = None,
    split: str = "test",
    num_samples: Optional[int] = None,
    force_synthetic: bool = False,
) -> Path:
    """Download or generate a benchmark dataset.

    Resolution order:
        1. If ``output_dir/file`` already exists, return that path
        2. Try HuggingFace datasets (network fetch)
        3. Fall back to synthetic data generation

    Parameters
    ----------
    dataset_name:
        One of: ``"locomo"``, ``"nq"``, ``"triviaqa"``, ``"popqa"``, ``"hotpotqa"``.
    output_dir:
        Directory to store the dataset file. Defaults to ``.memplex/benchmarks/data``.
    split:
        Which split to load from HuggingFace (e.g. ``"test"``, ``"validation"``).
        Ignored when falling back to synthetic data.
    num_samples:
        Maximum number of samples to fetch. Defaults to all.
        Useful for quick testing.
    force_synthetic:
        If True, skip HuggingFace and generate synthetic data directly.

    Returns
    -------
    Path
        Path to the JSON dataset file.
    """
    output_dir = Path(output_dir or ".memplex/benchmarks/data")
    output_dir.mkdir(parents=True, exist_ok=True)

    dataset_name = dataset_name.lower().strip()

    # Check for existing file first
    expected_file = output_dir / f"{dataset_name}.json"
    if expected_file.exists():
        logger.info("Using cached dataset: %s", expected_file)
        return expected_file

    # Try HuggingFace
    if not force_synthetic:
        hf_path = _fetch_from_huggingface(dataset_name, split=split, num_samples=num_samples)
        if hf_path is not None:
            # Copy to our data directory
            dest = output_dir / f"{dataset_name}.json"
            import shutil

            shutil.copy(hf_path, dest)
            Path(hf_path).unlink(missing_ok=True)
            return dest

    # Fall back to synthetic
    if dataset_name in _SYNTHETIC_GENERATORS:
        generator = _SYNTHETIC_GENERATORS[dataset_name]
        return generator(expected_file)

    raise ValueError(
        f"Unknown dataset: {dataset_name!r}. Available: {list(_SYNTHETIC_GENERATORS.keys())}"
    )


def list_available_datasets() -> List[str]:
    """Return list of supported dataset names."""
    return list(_SYNTHETIC_GENERATORS.keys())
