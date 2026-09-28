"""ADR-013 F3 rule-based consolidation contract tests.

Covers: default-off gating, near-duplicate promotion (rephrased
repetition across a time span), idempotency, TTL forgetting with
sustained-row protection, threshold gates, and the no-paragraph-layer
degradation.
"""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from datetime import UTC, datetime, timedelta

from memplex.consolidation import consolidate
from memplex.storage.lite.store import LiteMemoryStore


def _row(row_id: str, text: str, created: datetime, tier: int = 4) -> dict:
    return {
        "id": row_id,
        "raw_text": text,
        "trust_tier": tier,
        "created_at": created.isoformat(),
        "source": "test",
    }


def _store(tmp_path) -> LiteMemoryStore:
    return LiteMemoryStore(path=tmp_path / "m.json")


def test_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMPLEX_CONSOLIDATION", raising=False)
    store = _store(tmp_path)
    store._paragraphs["p1"] = _row("p1", "some text", datetime.now(UTC))
    report = consolidate(store)
    assert not report.enabled
    assert "MEMPLEX_CONSOLIDATION" in report.note
    assert "p1" in store._paragraphs, "disabled pass must not mutate"


def test_promotion_from_near_duplicate_rephrases(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMPLEX_CONSOLIDATION", "1")
    monkeypatch.delenv("MEMPLEX_CONSOLIDATION_MIN_OBSERVATIONS", raising=False)
    monkeypatch.delenv("MEMPLEX_CONSOLIDATION_MIN_SPAN_DAYS", raising=False)
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    store = _store(tmp_path)
    store._paragraphs["p1"] = _row("p1", "I take vitamin D in the mornings.", base)
    store._paragraphs["p2"] = _row(
        "p2", "I always take vitamin D in the mornings.", base + timedelta(days=1)
    )
    store._paragraphs["p3"] = _row(
        "p3", "I take vitamin D every morning now.", base + timedelta(days=3)
    )
    report = consolidate(store, now=base + timedelta(days=4))
    assert report.promoted, "3 rephrases across 3 days must promote"
    assert len(report.promoted) == 1
    node_id = report.promoted[0]
    fact = store._facts[node_id]
    assert fact.subject == "user" and fact.predicate == "stated"
    assert "vitamin" in fact.object_
    assert fact.trust_tier == 4
    assert fact.namespace["observations"] == "3"
    for row_id in ("p1", "p2", "p3"):
        assert store._paragraphs[row_id]["consolidated_into"] == node_id

    # Idempotency: a second pass promotes nothing new.
    second = consolidate(store, now=base + timedelta(days=5))
    assert second.promoted == []


def test_min_tier_merge_on_promotion(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMPLEX_CONSOLIDATION", "1")
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    store = _store(tmp_path)
    store._paragraphs["p1"] = _row(
        "p1", "The build requires Python 3.12.", base, tier=4
    )
    store._paragraphs["p2"] = _row(
        "p2", "The build needs Python 3.12 or newer.", base + timedelta(days=1), tier=2
    )
    store._paragraphs["p3"] = _row(
        "p3", "The build uses Python 3.12.", base + timedelta(days=2), tier=3
    )
    report = consolidate(store, now=base + timedelta(days=3))
    assert report.promoted
    assert store._facts[report.promoted[0]].trust_tier == 2, (
        "promotion must take the min tier, never launder upward"
    )


def test_ttl_eviction_spares_consolidated(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMPLEX_CONSOLIDATION", "1")
    base = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    store = _store(tmp_path)
    store._paragraphs["old_noise"] = _row(
        "old_noise", "The meeting room is booked today.", base
    )
    store._paragraphs["sustained"] = _row(
        "sustained", "I take vitamin D in the mornings.", base
    )
    store._paragraphs["sustained"]["consolidated_into"] = "consol-deadbeef"
    store._paragraphs["fresh"] = _row(
        "fresh", "A new note from this week.", base + timedelta(days=120)
    )
    now = base + timedelta(days=120)
    report = consolidate(store, now=now)
    assert "old_noise" in report.evicted
    assert "old_noise" not in store._paragraphs
    assert "sustained" in store._paragraphs, "consolidated rows survive the TTL"
    assert "fresh" in store._paragraphs


def test_observation_and_span_gates(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMPLEX_CONSOLIDATION", "1")
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    store = _store(tmp_path)
    # Only two rows: below the default min-observations gate.
    store._paragraphs["a1"] = _row("a1", "We run the sync job nightly.", base)
    store._paragraphs["a2"] = _row(
        "a2", "The sync job runs nightly here.", base + timedelta(days=2)
    )
    # Three rows but all on one day: below the min-span gate.
    store._paragraphs["b1"] = _row("b1", "The deploy target is cobalt.", base)
    store._paragraphs["b2"] = _row(
        "b2", "Our deploy target is the cobalt node.", base + timedelta(hours=2)
    )
    store._paragraphs["b3"] = _row(
        "b3", "Deploys go to the cobalt node now.", base + timedelta(hours=4)
    )
    report = consolidate(store, now=base + timedelta(days=3))
    assert report.promoted == [], "gates must hold promotion back"


def test_no_paragraph_layer_degrades(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMPLEX_CONSOLIDATION", "1")

    class BareStore:
        pass

    report = consolidate(BareStore())
    assert report.enabled
    assert "no paragraph layer" in report.note
    assert report.promoted == [] and report.evicted == []
