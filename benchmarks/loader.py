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
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)


# ── HuggingFace dataset IDs ────────────────────────────────────────────────────

HF_DATASET_IDS: dict[str, str] = {
    "popqa": "mteb/popqa",
    "hotpotqa": "hotpotqa",
    "nq": "natural_questions",
    "triviaqa": "triviaqa",
}


def _fetch_from_huggingface(
    dataset_name: str,
    split: str = "test",
    num_samples: int | None = None,
) -> Path | None:
    """Try to download a dataset from HuggingFace.

    Returns the path to the cached JSON file, or ``None`` on failure.
    """
    hf_id = HF_DATASET_IDS.get(dataset_name)
    if not hf_id:
        return None

    try:
        from datasets import load_dataset

        logger.info("Fetching %s from HuggingFace: %s", dataset_name, hf_id)

        # Parquet-native canonical releases; the historical script-based
        # ids (natural_questions, triviaqa) stopped loading with datasets
        # 5.x, and answers only exist on the validation split.
        _HF_SPECS = {
            "hotpotqa": ("hotpotqa/hotpot_qa", "fullwiki", "validation"),
            "nq": ("google-research-datasets/natural_questions", "dev", "validation"),
            "triviaqa": ("mandarjoshi/trivia_qa", "rc.nocontext", "validation"),
        }
        if dataset_name in _HF_SPECS:
            repo, config, split_override = _HF_SPECS[dataset_name]
            ds = load_dataset(repo, config, split=split_override)
        elif dataset_name == "popqa":
            # mteb/popqa was removed from the Hub; akariasai/PopQA is the
            # canonical release (subj/prop/obj -> subject/relation/object).
            ds = load_dataset("akariasai/PopQA", split=split)
            ds = ds.rename_columns({"subj": "subject", "prop": "relation", "obj": "object"})
        else:
            ds = load_dataset(hf_id, split=split)

        if num_samples is not None:
            ds = ds.select(range(min(num_samples, len(ds))))

        records = [dict(row) for row in ds]
        # Evidence provenance requires string sample identities; integer ids
        # from upstream releases are normalised here rather than rejected.
        for record in records:
            if isinstance(record.get("id"), int):
                record["id"] = str(record["id"])

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

    except Exception as exc:  # noqa: BLE001 - logged degradation path
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
    """Generate synthetic Natural Questions-like samples.

    Each sample carries a ``context`` sentence that states the answer, so
    the benchmark measures retrieval over an answer-bearing corpus (the
    question-only shape tested nothing: the answer could never be retrieved
    because it was never seeded).
    """
    samples = [
        {
            "id": f"nq_{i}",
            "question_text": question,
            "question": question,
            "answer": [answer] if isinstance(answer, str) else answer,
            "context": context,
        }
        for i, (question, answer, context) in enumerate(
            [
                (
                    "Who is the president of the United States?",
                    "Barack Obama",
                    "Barack Obama is the president of the United States.",
                ),
                (
                    "What is the capital of France?",
                    "Paris",
                    "Paris is the capital and largest city of France.",
                ),
                (
                    "How many continents are there?",
                    "7",
                    "There are 7 continents on Earth.",
                ),
                (
                    "What is the largest ocean?",
                    "Pacific Ocean",
                    "The Pacific Ocean is the largest ocean on Earth.",
                ),
                (
                    "Who wrote Hamlet?",
                    "William Shakespeare",
                    "William Shakespeare wrote the tragedy Hamlet.",
                ),
            ],
            start=1,
        )
    ]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(samples, fh, indent=2)
    logger.info("Generated %d synthetic NQ samples at %s", len(samples), path)
    return path


def _generate_locomo_synthetic(path: Path) -> Path:
    """Generate synthetic LoCoMo-like conversation samples.

    Each conversation states two facts with template-symmetric wording (same
    length, same query-term overlap, corpus-symmetric term statistics), so
    lexical relevance ties and the recency dimension measurably decides the
    order. Dates are relative to generation time so the memories sit within
    the reranker's recency horizon instead of all decaying to ~0. The query
    is not contained verbatim in either memory.
    """
    now = datetime.now(UTC)
    t_old = (now - timedelta(days=14)).isoformat()
    t_new = (now - timedelta(days=7)).isoformat()
    t_query = now.isoformat()

    raw_samples = [
        (
            "locomo_conv_1",
            "My mountain bike is bright red since Saturday.",
            "My racing helmet is deep blue since Friday.",
            "What color is my bike and helmet?",
            "Your mountain bike is red and your racing helmet is blue.",
        ),
        (
            "locomo_conv_2",
            "My Python course began with basic syntax lessons.",
            "My Python project ended with working flask code.",
            "How is my Python course and project?",
            "Your Python course covered basic syntax and your project produced working Flask code.",
        ),
        (
            "locomo_conv_3",
            "My tomato plants received plain water last week.",
            "My tomato plants received rich fertilizer this week.",
            "How are my tomato plants doing?",
            "Your tomato plants received water last week and fertilizer this week.",
        ),
    ]
    samples = [
        {
            "conversation_id": conv_id,
            "type": "qa",
            "turns": [
                {"speaker": "user", "text": fact_old, "timestamp": t_old},
                {"speaker": "assistant", "text": "Noted, thanks for sharing.", "timestamp": t_old},
                {"speaker": "user", "text": fact_new, "timestamp": t_new},
                {"speaker": "assistant", "text": "Got it, thanks for the update.", "timestamp": t_new},
                {"speaker": "user", "text": question, "timestamp": t_query},
                {"speaker": "assistant", "text": answer, "timestamp": t_query},
            ],
            "ground_truth_memories": [
                {"memory_id": f"{conv_id}_mem_0", "content": fact_old, "timestamp": t_old},
                {"memory_id": f"{conv_id}_mem_1", "content": fact_new, "timestamp": t_new},
            ],
        }
        for conv_id, fact_old, fact_new, question, answer in raw_samples
    ]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(samples, fh, indent=2)
    logger.info("Generated %d synthetic LoCoMo samples at %s", len(samples), path)
    return path


def _generate_triviaqa_synthetic(path: Path) -> Path:
    """Generate synthetic TriviaQA-like samples (question + answer aliases).

    Each sample carries one web-result snippet whose description states the
    answer, so the seeded corpus actually contains the answer (previously
    ``web_results`` was empty and only the question text was seeded, making
    answer retrieval impossible by construction).
    """
    samples = [
        {
            "question_id": f"tc_{i}",
            "question": question,
            "answer": {"Value": value, "Aliases": aliases},
            "search_results": {"web_results": [{"description": snippet}]},
        }
        for i, (question, value, aliases, snippet) in enumerate(
            [
                (
                    "Which planet is known as the Red Planet?",
                    "Mars",
                    ["Mars", "the Red Planet"],
                    "Mars, the fourth planet from the Sun, is known as the Red Planet.",
                ),
                (
                    "Who painted the Mona Lisa?",
                    "Leonardo da Vinci",
                    ["Leonardo da Vinci", "Da Vinci"],
                    "The Mona Lisa was painted by Leonardo da Vinci.",
                ),
                (
                    "What is the chemical symbol for gold?",
                    "Au",
                    ["Au", "AU"],
                    "The chemical symbol for gold is Au.",
                ),
                (
                    "Which ocean is the deepest?",
                    "Pacific Ocean",
                    ["Pacific Ocean", "the Pacific"],
                    "The Pacific Ocean is the deepest ocean in the world.",
                ),
                (
                    "Who wrote the play 'Romeo and Juliet'?",
                    "William Shakespeare",
                    ["William Shakespeare", "Shakespeare"],
                    "William Shakespeare wrote the play Romeo and Juliet.",
                ),
            ],
            start=1,
        )
    ]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(samples, fh, indent=2)
    logger.info("Generated %d synthetic TriviaQA samples at %s", len(samples), path)
    return path



def _generate_longmemeval_synthetic(path: Path) -> Path:
    """Generate a deterministic synthetic LongMemEval-format question set.

    Mirrors the official schema (question/question_type/answers/
    question_date/session_history) so the loader and runner exercise the
    real code path in CI without a network fetch.
    """
    questions = [
        {
            "question": "Which programming language did the user adopt for data analysis?",
            "question_type": "single-hop-user",
            "answers": ["Python"],
            "question_date": "2025/3/12 10:00",
            "evidence_session_ids": [0],
            "session_history": [
                {"role": "user", "content": "I finally switched all my data analysis work to Python."},
                {"role": "assistant", "content": "Noted - Python is now your primary analysis language."},
            ],
        },
        {
            "question": "How many books did the user finish in March, combining both updates?",
            "question_type": "multi-hop",
            "answers": ["5"],
            "question_date": "2025/4/1 9:00",
            "evidence_session_ids": [0, 1],
            "session_history": [
                {"role": "user", "content": "I finished 2 books this March."},
                {"role": "assistant", "content": "Two books logged for March."},
                {"role": "user", "content": "Correction - I finished 3 more books late March."},
                {"role": "assistant", "content": "Three additional books recorded."},
            ],
        },
        {
            "question": "What is the user's current preferred editor after the switch?",
            "question_type": "knowledge-update",
            "answers": ["Neovim"],
            "question_date": "2025/5/2 15:30",
            "evidence_session_ids": [1],
            "session_history": [
                {"role": "user", "content": "I used VS Code for years but I now prefer Neovim."},
                {"role": "assistant", "content": "Preference updated to Neovim."},
            ],
        },
    ]
    path.write_text(json.dumps(questions, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("Generated %d synthetic LongMemEval samples at %s", len(questions), path)
    return path

_SYNTHETIC_GENERATORS: dict[str, callable] = {
    "popqa": _generate_popqa_synthetic,
    "hotpotqa": _generate_hotpotqa_synthetic,
    "nq": _generate_nq_synthetic,
    "triviaqa": _generate_triviaqa_synthetic,
    "locomo": _generate_locomo_synthetic,
    "longmemeval": _generate_longmemeval_synthetic,
}


# ── Public API ─────────────────────────────────────────────────────────────────


def download_dataset(
    dataset_name: str,
    output_dir: str | None = None,
    split: str = "test",
    num_samples: int | None = None,
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
        One of: ``"locomo"``, ``"longmemeval"``, ``"nq"``, ``"triviaqa"``, ``"popqa"``, ``"hotpotqa"``.
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

    # Check for existing file first. Skipped when force_synthetic is set:
    # ``--synthetic`` promises freshly generated data, and a stale cache
    # (e.g. a previous HuggingFace download) would silently override it.
    expected_file = output_dir / f"{dataset_name}.json"
    if expected_file.exists() and not force_synthetic:
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


def list_available_datasets() -> list[str]:
    """Return list of supported dataset names."""
    return list(_SYNTHETIC_GENERATORS.keys())
