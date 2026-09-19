#!/usr/bin/env python3
"""Measure per-commit latency around full-decode audit windows.

Seeds N short documents through the normal write path (deferred batches)
and records wall-clock per commit, then reports the latency split between
audit-window commits (every _FULL_DECODE_AUDIT_INTERVAL-th) and regular
commits -- the write-stall evidence for the sharded-audit design.
"""

from __future__ import annotations

import os
import pathlib
import statistics
import sys
import tempfile
import time

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")
os.environ.setdefault("MEMPLEX_LLM_QUERY_ENHANCEMENT", "false")

from memplex.config import load_config  # noqa: E402
from memplex.service import MemplexService  # noqa: E402

N_DOCS = int(sys.argv[1]) if len(sys.argv) > 1 else 20000
BATCH = 200


def main() -> int:
    config = load_config()
    config.storage.backend = "lite"
    config.storage.path = str(pathlib.Path(tempfile.mkdtemp()) / "s.sqlite3")
    config.llm.query_enhancement = False
    svc = MemplexService(config=config)
    svc.start()
    store = svc.store

    from memplex.models import FieldValue, Function, SourceDocument, SourceType

    audit_interval = 32
    commit_times: list[tuple[int, float, int]] = []  # (batch_idx, seconds, corpus_size)
    t_all = time.time()
    for batch in range(N_DOCS // BATCH):
        t0 = time.time()
        with store.deferred_commit():
            for i in range(BATCH):
                idx = batch * BATCH + i
                func = Function(
                    id=f"bench-f-{idx}",
                    name=f"bench fact {idx} about topic {idx % 500}",
                    name_normalized=f"bench-fact-{idx}",
                    domain=f"topic-{idx % 500}",
                    memory_type="function",
                    source_type=SourceType.WIKI,
                    action=[FieldValue(desc=f"fact number {idx} with unique token tok{idx}")],
                )
                store.add(func, SourceDocument(type="bench", content=f"content {idx} tok{idx}", source_type=SourceType.WIKI))
        commit_times.append((batch, time.time() - t0, (batch + 1) * BATCH))
    total = time.time() - t_all

    regular = [d for i, d, _ in commit_times if (i + 1) % audit_interval != 0]
    audit = [d for i, d, _ in commit_times if (i + 1) % audit_interval == 0]
    print(f"seeded {N_DOCS} docs in {total:.1f}s ({N_DOCS/total:.0f} docs/s)")
    if regular:
        print(f"regular commits: n={len(regular)} p50={statistics.median(regular):.3f}s max={max(regular):.3f}s")
    if audit:
        print(f"audit commits:   n={len(audit)} p50={statistics.median(audit):.3f}s max={max(audit):.3f}s")
    if regular and audit:
        print(f"audit stall factor: median x{statistics.median(audit)/statistics.median(regular):.1f}, max x{max(audit)/max(regular):.1f}")
    svc.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
