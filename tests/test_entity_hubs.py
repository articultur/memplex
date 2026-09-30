"""Entity-hub maintenance-pass contract tests (product port of v13).

Covers: default-off gating, frozen line-format parsing, hub creation
with entity-leading names, determinism/idempotency, searchability of
the hub by entity name, <2-session no-op, LLM-failure degradation, and
the missing-paragraph-layer no-op.
"""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from datetime import UTC, datetime, timedelta

from memplex.entity_hubs import build_entity_hubs, parse_hub_lines
from memplex.storage.lite.store import LiteMemoryStore

FAKE_EXTRACT = (
    "ENTITY: Maya's piano recital || SESSIONS: 0,1,2 || EVIDENCE: "
    "'recital on Saturday' | 'practicing for the recital' | 'tickets printed'\n"
    "ENTITY: household repairs || SESSIONS: 0,2 || EVIDENCE: "
    "'leaky faucet' | 'plumber visit'\n"
    "not a hub line\n"
)


def _row(row_id: str, text: str, source: str, created: datetime) -> dict:
    return {
        "id": row_id,
        "raw_text": text,
        "trust_tier": 4,
        "created_at": created.isoformat(),
        "source": source,
    }


def _seeded_store(tmp_path) -> LiteMemoryStore:
    store = LiteMemoryStore(path=tmp_path / "m.json")
    base = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
    texts = {
        "s1": ["Maya's recital on Saturday came up in planning.", "The leaky faucet was reported."],
        "s2": ["She is practicing for the recital daily."],
        "s3": ["Recital tickets printed today.", "Plumber visit booked for the faucet."],
    }
    for si, source in enumerate(("s1", "s2", "s3")):
        for ti, text in enumerate(texts[source]):
            store._paragraphs[f"{source}-{ti}"] = _row(
                f"{source}-{ti}", text, source, base + timedelta(days=si)
            )
    return store


def _fake_llm(prompt: str) -> str:
    assert "ENTITY:" in prompt and "Session 0" in prompt
    return FAKE_EXTRACT


def test_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("MEMPLEX_ENTITY_HUBS", raising=False)
    store = _seeded_store(tmp_path)
    report = build_entity_hubs(store, _fake_llm)
    assert not report.enabled
    assert "MEMPLEX_ENTITY_HUBS" in report.note
    assert not store._functions


def test_parse_hub_lines_frozen_format():
    records = parse_hub_lines(FAKE_EXTRACT)
    assert [r["entity"] for r in records] == [
        "Maya's piano recital",
        "household repairs",
    ]
    assert records[0]["sessions"] == [0, 1, 2]
    assert "recital" in records[0]["evidence"]


def test_hubs_created_and_idempotent(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMPLEX_ENTITY_HUBS", "1")
    store = _seeded_store(tmp_path)
    report = build_entity_hubs(store, _fake_llm)
    assert len(report.created) == 2, "both extracted anchors become hubs"
    assert report.sessions == 3
    hub = next(f for f in store._functions.values() if "recital" in f.name)
    assert hub.domain == "entity-hub"
    assert hub.namespace.get("entity_hub")
    body = hub.action[0].desc
    assert "sessions [0, 1, 2]" in body
    assert "2026-06-01" in body, "the hub body leads with the first session date"

    second = build_entity_hubs(store, _fake_llm)
    assert second.created == [] and second.skipped_existing == 2, (
        "deterministic ids make the pass idempotent"
    )
    assert len(store._functions) == 2


def test_hub_searchable_by_entity_name(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMPLEX_ENTITY_HUBS", "1")
    store = _seeded_store(tmp_path)
    build_entity_hubs(store, _fake_llm)
    hits = store.vector_search("Maya recital sessions", top_k=5)
    hub_ids = {f.id for f in store._functions.values()}
    assert any(h.func_id in hub_ids for h in hits), (
        "a question naming the entity must surface its hub (lexical lead)"
    )


def test_needs_two_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMPLEX_ENTITY_HUBS", "1")
    store = LiteMemoryStore(path=tmp_path / "m.json")
    store._paragraphs["only"] = _row(
        "only", "one session only", "s1", datetime.now(UTC)
    )
    report = build_entity_hubs(store, _fake_llm)
    assert report.sessions == 1
    assert ">= 2 sessions" in report.note
    assert not store._functions


def test_llm_failure_degrades(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMPLEX_ENTITY_HUBS", "1")

    def boom(prompt: str) -> str:
        raise RuntimeError("proxy down")

    store = _seeded_store(tmp_path)
    report = build_entity_hubs(store, boom)
    assert report.enabled
    assert "extraction failed" in report.note
    assert not store._functions


def test_no_paragraph_layer_noop(monkeypatch):
    monkeypatch.setenv("MEMPLEX_ENTITY_HUBS", "1")

    class Bare:
        pass

    report = build_entity_hubs(Bare(), _fake_llm)
    assert report.enabled
    assert "no paragraph layer" in report.note
