"""ADR-013 provenance trust-tier contract tests.

Covers: write-path attribution (text/url/file), merge-takes-min in the
deduplicator, retrieval score penalty for low-trust tiers (and its
opt-out), legacy-data defaulting, and dict round-trips.
"""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from pathlib import Path

from memplex.config import MemplexConfig
from memplex.models.memory import Function, SourceType, create_memory_node
from memplex.retrieval.dedup import MemoryDeduplicator
from memplex.service import MemplexService
from memplex.storage.lite.store import LiteMemoryStore


def _nodes_of(svc, attrs=("_functions", "_facts", "_preferences")):
    store = svc.store
    out = []
    for attr in attrs:
        out.extend(getattr(store, attr, {}).values())
    return out


def test_write_path_attributes_trust_tiers(tmp_path):
    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(tmp_path / "s.sqlite3")
    config.llm.query_enhancement = False
    svc = MemplexService(config=config)
    svc.start()
    try:
        svc.write_text("Alice keeps a blue parrot named Kiwi.", source_type="text")
        svc.write_text(
            "Reader note: the user prefers rooibos tea.", source_type="url"
        )
        url_nodes = _nodes_of(svc)
        tiers = {getattr(n, "trust_tier", None) for n in url_nodes}
        assert 4 in tiers, "user-authored text must be user_direct"
        assert 2 in tiers, "url-sourced content must be external_web"
    finally:
        svc.stop()


def test_legacy_data_defaults_to_session_derived():
    node = create_memory_node(
        "fact", id="f1", subject="u", predicate="likes", object_="tea"
    )
    assert node.trust_tier == 3


def test_tier_roundtrip_and_fail_closed_coercion():
    node = Function(
        id="fx",
        name="n",
        name_normalized="n",
        source_type=SourceType.WIKI,
        trust_tier=1,
    )
    d = node.to_dict()
    assert d["trust_tier"] == 1
    back = Function.from_dict(d)
    assert back.trust_tier == 1
    # Out-of-range and non-int values fail closed to the default.
    d["trust_tier"] = 9
    assert Function.from_dict(d).trust_tier == 3
    d["trust_tier"] = "2"
    assert Function.from_dict(d).trust_tier == 3


def test_merge_takes_min():
    hi = Function(
        id="a", name="n", name_normalized="n",
        source_type=SourceType.WIKI, trust_tier=4,
    )
    lo = Function(
        id="b", name="n2", name_normalized="n2",
        source_type=SourceType.WIKI, trust_tier=2,
    )
    # Both updated now; the deduplicator must min the tiers regardless of
    # which object survives as the merge base.
    dedup = MemoryDeduplicator(None, threshold=0.0)
    merged = dedup._merge_memories([hi, lo])
    assert merged.trust_tier == 2, "a merge must never launder authority upward"


def test_retrieval_penalty_demotes_low_trust(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMPLEX_TRUST_PENALTY", raising=False)
    store = LiteMemoryStore(path=tmp_path / "m.json")
    from memplex.models import Fact, SourceDocument

    store.add_fact(
        Fact(
            id="user_fact",
            subject="user",
            predicate="prefers",
            object_="rooibos tea",
            source_type=SourceType.WIKI,
            trust_tier=4,
        )
    )
    store.add_fact(
        Fact(
            id="ext_fact",
            subject="reader note",
            predicate="prefers",
            object_="rooibos tea",
            source_type=SourceType.WIKI,
            trust_tier=2,
        )
    )
    hits = store.vector_search("rooibos tea preferences", top_k=2)
    assert hits, "fts leg must surface the facts"
    assert hits[0].func_id == "user_fact", (
        "equal-relevance external content must not out-rank user history"
    )


def test_retrieval_penalty_opt_out(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMPLEX_TRUST_PENALTY", "0")
    store = LiteMemoryStore(path=tmp_path / "m.json")
    from memplex.models import Fact, SourceType

    store.add_fact(
        Fact(
            id="user_fact",
            subject="user",
            predicate="prefers",
            object_="rooibos tea",
            source_type=SourceType.WIKI,
            trust_tier=4,
        )
    )
    store.add_fact(
        Fact(
            id="ext_fact",
            subject="reader note rooibos",
            predicate="prefers",
            object_="rooibos tea rooibos",
            source_type=SourceType.WIKI,
            trust_tier=2,
        )
    )
    hits = store.vector_search("rooibos tea preferences rooibos tea", top_k=2)
    assert hits, "search must still return results with the penalty off"


def test_retrieval_quota_bounds_low_trust_window(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMPLEX_TRUST_PENALTY", raising=False)
    monkeypatch.setenv("MEMPLEX_TRUST_QUOTA", "1")
    store = LiteMemoryStore(path=tmp_path / "m.json")
    from memplex.models import Fact, SourceType

    store.add_fact(
        Fact(
            id="user_fact",
            subject="user",
            predicate="prefers",
            object_="rooibos tea",
            source_type=SourceType.WIKI,
            trust_tier=4,
        )
    )
    for i in range(3):
        store.add_fact(
            Fact(
                id=f"ext_fact_{i}",
                subject=f"note {i}",
                predicate="mentions",
                object_="rooibos tea rooibos",
                source_type=SourceType.WIKI,
                trust_tier=2,
            )
        )
    hits = store.vector_search("rooibos tea", top_k=3)
    assert hits, "search must still surface results with a quota set"
    low = [h for h in hits if h.trust_tier <= 2]
    assert len(low) <= 1, "quota must bound low-trust slots in the top-k window"


def test_retrieval_quota_zero_excludes_low_trust(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMPLEX_TRUST_PENALTY", raising=False)
    monkeypatch.setenv("MEMPLEX_TRUST_QUOTA", "0")
    store = LiteMemoryStore(path=tmp_path / "m.json")
    from memplex.models import Fact, SourceType

    store.add_fact(
        Fact(
            id="user_fact",
            subject="user",
            predicate="prefers",
            object_="rooibos tea",
            source_type=SourceType.WIKI,
            trust_tier=4,
        )
    )
    for i in range(3):
        store.add_fact(
            Fact(
                id=f"ext_fact_{i}",
                subject=f"note {i}",
                predicate="mentions",
                object_="rooibos tea rooibos",
                source_type=SourceType.WIKI,
                trust_tier=2,
            )
        )
    hits = store.vector_search("rooibos tea", top_k=3)
    assert hits and all(h.trust_tier > 2 for h in hits), (
        "quota 0 must keep low-trust tiers out of the window entirely"
    )


def test_trust_quota_shrinks_window_instead_of_admitting_excess(monkeypatch):
    monkeypatch.setenv("MEMPLEX_TRUST_QUOTA", "1")
    from memplex.models.search import SearchResult

    results = [
        SearchResult(func_id="h1", name="h", domain="x", relevance_score=0.9,
                     summary="s", trust_tier=4),
        SearchResult(func_id="l1", name="l", domain="x", relevance_score=0.8,
                     summary="s", trust_tier=2),
        SearchResult(func_id="l2", name="l", domain="x", relevance_score=0.7,
                     summary="s", trust_tier=2),
        SearchResult(func_id="h2", name="h", domain="x", relevance_score=0.6,
                     summary="s", trust_tier=4),
        SearchResult(func_id="l3", name="l", domain="x", relevance_score=0.5,
                     summary="s", trust_tier=2),
    ]
    ranked = LiteMemoryStore._enforce_trust_quota(results, top_k=3)
    assert [r.func_id for r in ranked] == ["h1", "l1", "h2"], (
        "the freed slot must go to the next high-tier hit, not the demoted one"
    )
    assert len(ranked) < len(results), (
        "excess low-tier hits leave the result set entirely (containment over recall)"
    )

    monkeypatch.setenv("MEMPLEX_TRUST_QUOTA", "0")
    ranked = LiteMemoryStore._enforce_trust_quota(results, top_k=3)
    assert [r.func_id for r in ranked] == ["h1", "h2"], (
        "quota 0 admits no low-tier item even when the window shrinks"
    )


def test_trust_quota_default_off_and_bad_value(monkeypatch):
    from memplex.models.search import SearchResult

    results = [
        SearchResult(func_id="l1", name="l", domain="x", relevance_score=0.8,
                     summary="s", trust_tier=2),
        SearchResult(func_id="l2", name="l", domain="x", relevance_score=0.7,
                     summary="s", trust_tier=2),
    ]
    monkeypatch.delenv("MEMPLEX_TRUST_QUOTA", raising=False)
    assert LiteMemoryStore._enforce_trust_quota(results, top_k=1) == results
    monkeypatch.setenv("MEMPLEX_TRUST_QUOTA", "not-an-int")
    assert LiteMemoryStore._enforce_trust_quota(results, top_k=1) == results, (
        "an unparsable quota must fail closed to no cap"
    )
