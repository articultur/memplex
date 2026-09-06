"""Contract tests for TriviaQA parquet-shape adaptation."""

from __future__ import annotations

from benchmarks.nq_trivia import NQTriviaDataset, TriviaQADataset, _build_triviaqa_context

# Original TriviaQA JSON releases nest a list of web-result dicts.
_LEGACY_SHAPE = {
    "question": "Which US city is known as the City of Brotherly Love?",
    "question_id": "tc_33",
    "answer": {
        "value": "Philadelphia",
        "Value": "Philadelphia",
        "aliases": ["Philly"],
        "Aliases": ["Philly"],
    },
    "search_results": {
        "web_results": [
            {"rank": 1, "description": "Philadelphia is known as the City of Brotherly Love."},
            {"rank": 2, "description": "Other result."},
        ]
    },
}

# Parquet-native HuggingFace rows expand search_results into parallel lists.
_PARQUET_SHAPE = {
    "question": "Which US city is known as the City of Brotherly Love?",
    "question_id": "tc_33",
    "answer": {
        "value": "Philadelphia",
        "Value": "Philadelphia",
        "aliases": ["Philly"],
        "Aliases": ["Philly"],
    },
    "search_results": {
        "rank": [1, 2],
        "description": [
            "Philadelphia is known as the City of Brotherly Love.",
            "Other result.",
        ],
        "title": ["Philadelphia - Wikipedia", "Sister Cities"],
        "url": ["https://example.com/1", "https://example.com/2"],
        "search_context": ["ctx1", "ctx2"],
    },
}

# rc rows may carry wiki entity pages instead of (or alongside) web results.
_ENTITY_PAGES_SHAPE = {
    "question": "Who wrote the novel Nineteen Eighty-Four?",
    "question_id": "tc_34",
    "answer": {"Value": "George Orwell", "Aliases": ["Eric Arthur Blair"]},
    "search_results": {"rank": [], "description": []},
    "entity_pages": {
        "title": ["Nineteen Eighty-Four", "George Orwell"],
        "wiki_content": [
            "Nineteen Eighty-Four is a dystopian novel by George Orwell.",
            "George Orwell was the pen name of Eric Arthur Blair.",
        ],
    },
}


def test_legacy_web_results_shape_yields_context() -> None:
    assert "Brotherly Love" in _build_triviaqa_context(_LEGACY_SHAPE)


def test_parquet_parallel_lists_shape_yields_context() -> None:
    assert "Brotherly Love" in _build_triviaqa_context(_PARQUET_SHAPE)


def test_entity_pages_fallback_yields_context() -> None:
    assert "George Orwell" in _build_triviaqa_context(_ENTITY_PAGES_SHAPE)


def test_dataset_parses_parquet_shape_into_seeded_sample() -> None:
    dataset = TriviaQADataset()
    sample = dataset._parse_sample(_PARQUET_SHAPE)
    assert sample is not None
    assert sample.query.startswith("Which US city")
    assert "philadelphia" in sample.metadata["aliases"]
    assert "Brotherly Love" in sample.metadata["context"]
    assert sample.id == "triviaqa_tc_33"


def test_dataset_parses_legacy_shape_into_seeded_sample() -> None:
    dataset = TriviaQADataset()
    sample = dataset._parse_sample(_LEGACY_SHAPE)
    assert sample is not None
    assert "Brotherly Love" in sample.metadata["context"]


def test_nocontext_row_without_evidence_reports_empty_context() -> None:
    row = {
        "question": "Bare question?",
        "question_id": "tc_35",
        "answer": {"Value": "answer"},
        # rc.nocontext rows simply omit search_results / entity_pages.
    }
    sample = NQTriviaDataset(dataset_name="triviaqa")._parse_sample(row)
    # Falls through to the NQ/generic parser; the question survives but
    # there is nothing seedable, which is exactly why the loader must use rc.
    assert sample is not None
    assert sample.metadata.get("context", "") == ""
