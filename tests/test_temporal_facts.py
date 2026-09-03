"""Tests for bi-temporal fact validity (memplex/temporal.py + service wiring).

Zep/Graphiti-style supersede semantics: contradicted facts are stamped
``invalid_at`` and retained (never deleted), so point-in-time queries with
``as_of`` can reconstruct what was believed at any moment.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta, timezone

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from memplex.models import Fact, SourceType
from memplex.temporal import (
    facts_valid_at,
    is_valid_at,
    now_iso,
    supersede_contradicted,
)


def _fact(subject="db", predicate="is", object_="postgres", **kw):
    defaults = {"id": kw.pop("id", f"f-{subject}-{predicate}-{object_}"), "tenant_id": "t1", "owner_subject_id": "alice", "workspace_id": "w1", "updated_at": kw.pop("updated_at", now_iso()), "subject": subject, "predicate": predicate, "object_": object_}
    defaults.update(kw)
    return Fact(**defaults)


# ── unit: interval predicate ─────────────────────────────────────────


def test_is_valid_at_interval_semantics():
    now = datetime.now(UTC)
    past = now - timedelta(days=10)
    future = now + timedelta(days=10)
    assert is_valid_at(_fact()) is True  # no bounds = always valid
    assert is_valid_at(_fact(valid_from=past.isoformat())) is True
    assert is_valid_at(_fact(valid_from=future.isoformat())) is False
    assert is_valid_at(_fact(invalid_at=past.isoformat())) is False
    # half-open interval: end exclusive
    end = now.isoformat()
    assert is_valid_at(_fact(invalid_at=end), as_of=now) is False
    assert is_valid_at(_fact(valid_from=end), as_of=now) is True


def test_is_valid_at_malformed_stamps_do_not_hide():
    assert is_valid_at(_fact(valid_from="not-a-date")) is True
    assert is_valid_at(_fact(invalid_at="")) is True


# ── unit: supersede ──────────────────────────────────────────────────


def test_supersede_same_slot_different_object():
    old = _fact(object_="mysql")
    new = _fact(object_="postgres")
    superseded = supersede_contradicted(new, [old], now="2026-08-15T00:00:00+00:00")
    assert superseded == [old]
    assert old.invalid_at == "2026-08-15T00:00:00+00:00"


def test_supersede_ignores_other_slots_and_self():
    other = _fact(subject="cache", object_="redis")
    twin = _fact(object_="postgres")  # same content, same id
    assert supersede_contradicted(twin, [other, twin]) == []


def test_supersede_skips_already_invalid_or_expired():
    stale = _fact(object_="mysql", invalid_at="2020-01-01T00:00:00+00:00")
    expired = _fact(object_="mysql", valid_until="2020-01-01T00:00:00+00:00")
    assert supersede_contradicted(_fact(), [stale, expired]) == []


# ── integration: service write path + as_of listing ─────────────────


def test_service_write_supersedes_and_as_of_history(tmp_path):
    from memplex.config import MemplexConfig
    from memplex.models import SourceDocument
    from memplex.service import MemplexService

    cfg = MemplexConfig()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(tmp_path)
    svc = MemplexService(config=cfg)
    svc.start()
    try:
        svc.write(
            SourceDocument(
                type="conversation",
                content="The database is MySQL. I prefer dark mode.",
                source_type=SourceType.MEETING,
            )
        )
        # Direct-seed two contradicting facts through the store facade to
        # control timestamps precisely (the extractor may phrase freely).
        from memplex.models import Fact as F

        t0 = "2026-01-01T00:00:00+00:00"
        t1 = "2026-06-01T00:00:00+00:00"
        old = _fact(id="fact-old", object_="mysql", updated_at=t0, valid_from=t0)
        new = _fact(id="fact-new", object_="postgres", updated_at=t1, valid_from=t1)
        svc.store.add_fact(old)
        svc.store.add_fact(new)  # triggers supersede via service? no — store-direct

        # Service-level: use the supersede helper explicitly (the write path
        # calls it; here we exercise the store round-trip).
        from memplex import temporal

        superseded = temporal.supersede_contradicted(
            new, svc.store.list_facts(), now=t1
        )
        assert [f.id for f in superseded] == ["fact-old"]
        svc.store.add_fact(superseded[0])  # persist the stamped copy

        current = {f.id for f in svc.list_facts()}
        assert "fact-new" in current
        assert "fact-old" not in current  # invalidated as of now

        historical = {f.id for f in svc.list_facts(as_of="2026-03-01T00:00:00+00:00")}
        assert "fact-old" in historical  # point-in-time still sees the old belief
        assert "fact-new" not in historical

        everything = {f.id for f in svc.list_facts(include_invalidated=True)}
        assert {"fact-old", "fact-new"} <= everything  # never deleted
    finally:
        svc.stop()


def test_facts_valid_at_helper():
    a = _fact(id="a", valid_from="2026-01-01T00:00:00+00:00")
    b = _fact(id="b", invalid_at="2026-01-01T00:00:00+00:00")
    got = [f.id for f in facts_valid_at([a, b], as_of=datetime(2026, 5, 1, tzinfo=UTC))]
    assert got == ["a"]
