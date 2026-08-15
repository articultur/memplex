"""Tests for the improve() maintenance verb (memplex/improve.py + wiring)."""

from __future__ import annotations

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from memplex.config import MemplexConfig  # noqa: E402
from memplex.models import Fact, SourceType  # noqa: E402
from memplex.service import MemplexService  # noqa: E402


def _fact(fid, subject, predicate, object_, updated_at, **kw):
    base = dict(
        id=fid,
        tenant_id="t1",
        owner_subject_id="alice",
        workspace_id="w1",
        subject=subject,
        predicate=predicate,
        object_=object_,
        updated_at=updated_at,
        valid_from=updated_at,
    )
    base.update(kw)
    return Fact(**base)


def _service(tmp_path):
    cfg = MemplexConfig()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(tmp_path)
    svc = MemplexService(config=cfg)
    svc.start()
    return svc


def test_improve_dedupes_contradicting_valid_facts(tmp_path):
    svc = _service(tmp_path)
    try:
        # Two currently-valid facts in the same slot + one already superseded.
        svc.store.add_fact(_fact("a", "db", "is", "mysql", "2026-01-01T00:00:00+00:00"))
        svc.store.add_fact(_fact("b", "db", "is", "postgres", "2026-06-01T00:00:00+00:00"))
        svc.store.add_fact(
            _fact(
                "c",
                "db",
                "was",
                "sqlite",
                "2025-01-01T00:00:00+00:00",
                invalid_at="2025-06-01T00:00:00+00:00",
            )
        )
        report = svc.improve()
        assert report["deduplicated"] == 1
        # Newest survives; loser stamped but retained.
        live = {f.id for f in svc.list_facts()}
        assert live == {"b"}
        all_facts = {f.id for f in svc.list_facts(include_invalidated=True)}
        assert all_facts == {"a", "b", "c"}
        assert svc.store.get_fact("a").invalid_at is not None
        # Already-superseded history (c) untouched by dedup.
        assert svc.store.get_fact("c").invalid_at == "2025-06-01T00:00:00+00:00"
    finally:
        svc.stop()


def test_improve_expires_lapsed_shelf_life(tmp_path):
    svc = _service(tmp_path)
    try:
        svc.store.add_fact(
            _fact(
                "old-shelf",
                "token",
                "is",
                "abc123",
                "2025-01-01T00:00:00+00:00",
                valid_until="2026-01-01T00:00:00+00:00",
            )
        )
        svc.store.add_fact(_fact("fresh", "token", "is", "xyz", "2026-08-01T00:00:00+00:00"))
        report = svc.improve()
        assert report["expired"] == 1
        assert svc.store.get_fact("old-shelf").invalid_at is not None
        assert svc.store.get_fact("fresh").invalid_at is None
    finally:
        svc.stop()


def test_improve_rebuilds_index_and_reports(tmp_path):
    svc = _service(tmp_path)
    try:
        report = svc.improve()
        assert report["index_rebuilt"] is True
        assert set(report) == {"deduplicated", "expired", "index_rebuilt"}
    finally:
        svc.stop()


def test_improve_is_safe_on_empty_store(tmp_path):
    svc = _service(tmp_path)
    try:
        report = svc.improve()
        assert report == {"deduplicated": 0, "expired": 0, "index_rebuilt": True}
    finally:
        svc.stop()


def test_cli_exposes_improve(tmp_path):
    from memplex.adapters.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["improve"])
    assert args.command == "improve"
