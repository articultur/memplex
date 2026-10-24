#!/usr/bin/env python3
"""ADR-012 Phase B crash-matrix child runner (spawned by tests).

Modes:
  writer MODE STORE   - write loop under rw authority until mode-specific
                        stop (burst=10 writes, checkpoint=40 sustained,
                        queued=5 + 200-write deferred flood); the parent
                        SIGKILLs this process mid-flight.
  reopen STORE        - reopen under read authority from a FRESH process;
                        print "<rows> <torn>" where torn counts paragraph
                        rows whose text looks half-applied.

Whole-or-absent contract: WAL replay covers committed state; a torn
record (truncated text, missing batch suffix) is a crash-safety failure.
"""

from __future__ import annotations

import sys


def _writer(mode: str, store: str) -> None:
    import os
    import time

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

    limit = {"burst": 10, "checkpoint": 40, "queued": 5}[mode]
    flood_at = 5 if mode == "queued" else None
    deadline = time.monotonic() + 60
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


def _reopen(store: str) -> None:
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
    svc = MemplexService(config=cfg)
    svc.start()
    rows = list(svc.store._paragraphs.values())
    texts = [r.get("raw_text", "") for r in rows]
    torn = [t for t in texts if "record " in t and not t.rstrip().endswith(".")]
    print(len(rows), len(torn))
    svc.stop()


def main() -> int:
    argv = sys.argv[1:]
    if len(argv) >= 3 and argv[0] == "writer":
        _writer(argv[1], argv[2])
        return 0
    if len(argv) >= 2 and argv[0] == "reopen":
        _reopen(argv[1])
        return 0
    print("usage: rw_crash_child.py writer MODE STORE | reopen STORE", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
