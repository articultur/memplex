"""Tests for sleep-time compute (memplex/sleep_time.py + wiring)."""

from __future__ import annotations

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from memplex.config import MemplexConfig  # noqa: E402
from memplex.models import (  # noqa: E402
    Fact,
    Function,
    GraphData,
    GraphEdge,
    SourceType,
)
from memplex.service import MemplexService  # noqa: E402
from memplex.sleep_time import SleepTimeAgent  # noqa: E402


def _service(tmp_path, *, sleep_enabled=False, working_memory=True):
    cfg = MemplexConfig()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(tmp_path)
    cfg.working_memory.enabled = working_memory
    cfg.sleep_time.enabled = sleep_enabled
    svc = MemplexService(config=cfg)
    svc.start()
    return svc


def _hot_function(fid, name, access_count):
    return Function(
        id=fid,
        name=name,
        name_normalized=name.lower(),
        domain=None,
        memory_type="function",
        source_type=SourceType.MEETING,
        access_count=access_count,
    )


def test_run_once_improves_and_reports(tmp_path):
    svc = _service(tmp_path, working_memory=False)
    try:
        svc.store.add_fact(
            Fact(
                id="f1",
                tenant_id="t1",
                owner_subject_id="a",
                workspace_id="w1",
                subject="db",
                predicate="is",
                object_="mysql",
                updated_at="2026-01-01T00:00:00+00:00",
            )
        )
        agent = SleepTimeAgent(svc)
        report = agent.run_once()
        assert set(report) == {"improved", "pinned_inferences"}
        assert report["improved"]["index_rebuilt"] is True
        assert report["pinned_inferences"] == 0  # no working memory tier
    finally:
        svc.stop()


def test_precompute_pins_associations_into_working_memory(tmp_path):
    svc = _service(tmp_path, working_memory=True)
    try:
        # Two hot functions joined by an edge.
        a = _hot_function("fa", "auth-service", 10)
        b = _hot_function("fb", "user-db", 7)
        cold = _hot_function("fc", "never-queried", 0)
        for fn in (a, b, cold):
            svc.store.add(fn, None)
        svc.store.merge(
            GraphData(nodes=[], edges=[GraphEdge(source="fa", target="fb", edge_type="REFERENCES")])
        )
        agent = SleepTimeAgent(svc, precompute_top_k=10)
        report = agent.run_once()
        # Only accessed functions precompute; cold ones never do.
        assert report["pinned_inferences"] >= 1
        ctx = svc._working_memory.recall_context(limit=20)
        sleep_entries = [line for line in ctx if line.startswith("[SLEEP-TIME]")]
        assert sleep_entries, ctx
        assert any("auth-service" in line for line in sleep_entries)
        assert not any("never-queried" in line for line in sleep_entries)
    finally:
        svc.stop()


def test_daemon_starts_only_when_enabled(tmp_path):
    svc_off = _service(tmp_path / "off", sleep_enabled=False)
    try:
        assert svc_off._sleep_time._thread is None
    finally:
        svc_off.stop()
    svc_on = _service(tmp_path / "on", sleep_enabled=True)
    try:
        assert svc_on._sleep_time._thread is not None
        assert svc_on._sleep_time._thread.is_alive()
    finally:
        svc_on.stop()  # stop() joins the daemon


def test_stop_is_idempotent_after_service_stop(tmp_path):
    svc = _service(tmp_path, sleep_enabled=True)
    svc.stop()
    assert svc._sleep_time._thread is None
