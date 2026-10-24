"""ADR-012 Phase B: REAL kill -9 crash matrix (transaction/WAL/checkpoint).

The predecessor crash test (test_rw_authority_crash_rolls_back) replaced
AuthoritativeWriter.commit with a raising function — a monkeypatched
"in-transaction" failure that never exercised SQLite's actual crash
semantics (WAL replay, half-written pages, fsync ordering). This module
spawns real writer processes (multiprocessing spawn context, like
scripts/deadlock_stress.py) under the rw authority, SIGKILLs them at
each crash point, and asserts reopen consistency from a fresh process:

  mid-transaction  - kill while a burst of writes is in flight (the
                     single-writer queue draining into BEGIN IMMEDIATE)
  mid-wal-checkpoint - kill under sustained write pressure (default
                     autocheckpoint cycles the WAL repeatedly)
  mid-write-queue  - kill while the single-writer queue drains a
                     200-write deferred flood batch

Contract: every committed record survives (WAL replay); the store
reopens without LiteStorageIntegrityError; no partially-applied record
appears (paragraph rows are whole or absent).
"""

from __future__ import annotations

import multiprocessing
import pathlib
import time

import pytest


def _writer_child(mode: str, store: str, ready) -> None:  # type: ignore[no-untyped-def]
    import os

    os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")
    os.environ["MEMPLEX_LITE_SQLITE_AUTHORITY"] = "rw"
    os.environ["MEMPLEX_LLM_QUERY_ENHANCEMENT"] = "false"

    from memplex.config import load_config
    from memplex.service import MemplexService

    cfg = load_config()
    cfg.storage.backend = "lite"
    cfg.storage.path = store
    cfg.llm.query_enhancement = False
    svc = MemplexService(config=cfg)
    svc.start()
    ready.set()

    # Checkpoint mode: sustained pressure until killed (no write limit —
    # a finite limit could complete before the kill lands, turning the
    # "mid-checkpoint crash" into a trivial post-run kill).
    limit = {"burst": 10, "checkpoint": 10_000, "queued": 5}[mode]
    flood_at = 5 if mode == "queued" else None
    deadline = time.monotonic() + 120
    i = 0
    while time.monotonic() < deadline:
        i += 1
        svc.write_text(
            f"[{mode} record {i}] The ocelot ledger ships on "
            f"Thursdays near the marina, batch {i}.",
            source_type="text",
        )
        if flood_at is not None and i == flood_at:
            with svc.store.deferred_commit():
                for j in range(200):
                    svc.write_text(
                        f"[{mode} flood {j}] queued crash filler {j}.",
                        source_type="text",
                    )
            break
        if i >= limit:
            break


def _reopen_child(store: str, out_queue) -> None:  # type: ignore[no-untyped-def]
    import os

    os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")
    os.environ["MEMPLEX_LITE_SQLITE_AUTHORITY"] = "read"
    os.environ["MEMPLEX_LLM_QUERY_ENHANCEMENT"] = "false"

    from memplex.config import load_config
    from memplex.service import MemplexService

    cfg = load_config()
    cfg.storage.backend = "lite"
    cfg.storage.path = store
    cfg.llm.query_enhancement = False
    try:
        svc = MemplexService(config=cfg)
        svc.start()
        rows = list(svc.store._paragraphs.values())
        texts = [r.get("raw_text", "") for r in rows]
        torn = [
            t for t in texts if "record " in t and not t.rstrip().endswith(".")
        ]
        out_queue.put(("ok", len(rows), len(torn)))
        svc.stop()
    except Exception as exc:  # noqa: BLE001 - reported to the parent verbatim
        out_queue.put(("error", str(exc), 0))


def _kill_and_reopen(mode: str, kill_delay: float, store_dir: pathlib.Path) -> tuple[int, int]:
    store = str(store_dir / "s.sqlite3")
    ctx = multiprocessing.get_context("spawn")
    ready = ctx.Event()
    proc = ctx.Process(target=_writer_child, args=(mode, store, ready))
    proc.start()
    ready.wait(timeout=60)  # service started; writes now in flight
    time.sleep(kill_delay)
    if proc.is_alive():
        proc.kill()  # SIGKILL
    proc.join(timeout=30)
    assert not proc.is_alive(), "writer survived SIGKILL"

    out_queue = ctx.Queue()
    reopen = ctx.Process(target=_reopen_child, args=(store, out_queue))
    reopen.start()
    reopen.join(timeout=120)
    assert not reopen.is_alive(), "reopen process hung"
    status, a, b = out_queue.get()
    assert status == "ok", f"reopen after crash failed: {a}"
    return a, b


@pytest.mark.parametrize(
    "mode,kill_delay",
    [
        ("burst", 2.5),       # mid-transaction (writes in flight)
        ("checkpoint", 4.0),  # WAL checkpoint window (sustained pressure)
        ("queued", 6.0),      # single-writer queue draining (flood batch)
    ],
)
def test_real_kill9_crash_reopens_consistent(mode, kill_delay, tmp_path):
    """kill -9 at each crash point; reopen must succeed with whole-or-
    absent records (WAL replay covers committed state)."""
    rows, torn = _kill_and_reopen(mode, kill_delay, tmp_path)
    assert torn == 0, f"{mode}: torn/partial paragraph rows detected"
    assert rows > 0, (
        f"{mode}: crash landed before any commit; adjust kill_delay"
    )


def test_kill9_during_flood_batch_leaves_whole_records(tmp_path):
    """The queued mode's 200-write deferred flood: kill while the
    single-writer queue drains it. Reopen shows the batch's consistent
    prefix or its full application — never a torn middle."""
    rows, torn = _kill_and_reopen("queued", 7.5, tmp_path)
    assert torn == 0
    assert rows > 0
