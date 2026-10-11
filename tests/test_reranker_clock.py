"""A rerank's elapsed work must not change its candidates' time reference."""

import math
from datetime import UTC, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from memplex.models import Function, SearchResult
from memplex.retrieval import reranker as reranker_module
from memplex.retrieval.reranker import Reranker

REFERENCE_TIME = datetime(2026, 10, 9, 12, tzinfo=UTC)
PAST = REFERENCE_TIME - timedelta(days=30, hours=12)


@pytest.fixture
def clock(monkeypatch):
    state = SimpleNamespace(step=timedelta(0), reads=0)

    def now(tz):
        assert tz is UTC
        value = REFERENCE_TIME + state.step * state.reads
        state.reads += 1
        return value

    monkeypatch.setattr(
        reranker_module,
        "datetime",
        SimpleNamespace(now=now, fromisoformat=datetime.fromisoformat),
    )
    return state


def _candidate(func_id, updated_at=None):
    return SearchResult(
        func_id=func_id,
        name=func_id,
        domain="test",
        relevance_score=0.5,
        summary="same summary",
        updated_at=updated_at,
        vector_cache=[1.0],
    )


@pytest.mark.parametrize("dimension", ["recency", "frequency"])
@pytest.mark.parametrize("step_seconds", [0, 2])
@pytest.mark.parametrize("reverse_input", [False, True])
@pytest.mark.parametrize("top_k", [1, 2])
def test_rerank_order_and_top_k_ignore_per_hit_clock_drift(
    clock, dimension, step_seconds, reverse_input, top_k
):
    """Per-hit reads can reverse a real one-second freshness advantage."""
    clock.step = timedelta(seconds=step_seconds)
    older = REFERENCE_TIME - timedelta(days=1)
    newer = older + timedelta(seconds=1)
    candidates = [
        _candidate("older", older if dimension == "recency" else None),
        _candidate("newer", newer if dimension == "recency" else None),
    ]
    nodes = {
        name: Function(
            id=name,
            access_count=100,
            last_accessed_at=accessed.isoformat(),
            created_at=PAST.isoformat(),
        )
        for name, accessed in [("older", older), ("newer", newer)]
    }
    storage = SimpleNamespace(get_many=lambda ids: {key: nodes[key] for key in ids})
    ranker = Reranker(
        SimpleNamespace(), storage=storage if dimension == "frequency" else None
    )
    if reverse_input:
        candidates.reverse()

    ranked = ranker.rerank("query", candidates, top_k=top_k, query_vector=[1.0])

    assert [result.func_id for result in ranked] == ["newer", "older"][:top_k]


def test_nonempty_rerank_reads_clock_once_for_both_dimensions(clock):
    clock.step = timedelta(days=1)
    nodes = {
        name: Function(
            id=name,
            access_count=100,
            last_accessed_at=PAST.isoformat(),
            created_at=PAST.isoformat(),
        )
        for name in ["first", "second"]
    }
    storage = SimpleNamespace(get_many=lambda ids: {key: nodes[key] for key in ids})
    ranker = Reranker(SimpleNamespace(), storage=storage)
    candidates = [_candidate(name, PAST.isoformat()) for name in nodes]

    for expected_reads in [1, 2]:
        ranked = ranker.rerank("query", candidates, query_vector=[1.0])
        assert [result.func_id for result in ranked] == ["first", "second"]
        assert clock.reads == expected_reads


def test_empty_rerank_does_not_read_clock(clock):
    assert Reranker(SimpleNamespace()).rerank("query", []) == []
    assert clock.reads == 0


@pytest.mark.parametrize("use_reference", [False, True])
@pytest.mark.parametrize(
    ("timestamp", "recency", "access_recency", "has_valid_time"),
    [
        pytest.param(None, 0.5, 0.3, False, id="missing"),
        pytest.param("invalid", 0.5, 0.3, False, id="invalid"),
        pytest.param(PAST, math.exp(-30.5 / 60), math.exp(-30 / 60), True, id="utc"),
        pytest.param(
            PAST.replace(tzinfo=None),
            math.exp(-30.5 / 60),
            math.exp(-30 / 60),
            True,
            id="naive-utc",
        ),
        pytest.param(
            PAST.astimezone(timezone(timedelta(hours=8))),
            math.exp(-30.5 / 60),
            math.exp(-30 / 60),
            True,
            id="offset",
        ),
        pytest.param(
            PAST.isoformat(), math.exp(-30.5 / 60), math.exp(-30 / 60), True, id="iso"
        ),
        pytest.param(REFERENCE_TIME + timedelta(days=1), 1.0, 1.0, True, id="future"),
    ],
)
def test_time_scorers_preserve_timestamp_behavior(
    clock, use_reference, timestamp, recency, access_recency, has_valid_time
):
    kwargs = {"reference_time": REFERENCE_TIME} if use_reference else {}
    ranker = Reranker(SimpleNamespace())
    node = SimpleNamespace(access_count=100, last_accessed_at=timestamp)

    assert ranker._recency_decay(timestamp, **kwargs) == pytest.approx(recency)
    assert Reranker._frequency_score(node, **kwargs) == pytest.approx(0.6 + 0.4 * access_recency)
    assert clock.reads == (2 if has_valid_time and not use_reference else 0)


def test_direct_time_scorers_read_current_clock_when_reference_omitted(clock):
    clock.step = timedelta(days=2)
    ranker = Reranker(SimpleNamespace())
    node = SimpleNamespace(access_count=100, last_accessed_at=PAST.isoformat())

    assert ranker._recency_decay(PAST) == pytest.approx(math.exp(-30.5 / 60))
    assert ranker._recency_decay(PAST) == pytest.approx(math.exp(-32.5 / 60))
    assert clock.reads == 2

    clock.reads = 0
    assert Reranker._frequency_score(node) == pytest.approx(0.6 + 0.4 * math.exp(-30 / 60))
    assert Reranker._frequency_score(node) == pytest.approx(0.6 + 0.4 * math.exp(-32 / 60))
    assert clock.reads == 2
