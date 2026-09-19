#!/usr/bin/env python3
"""Profile the write path on an already-large store (tail-window sampling).

Seeds N docs unprofiled, then cProfiles a small tail window of batches so
the profile shows where large-corpus REGULAR commits spend time (the 20k
-> 100k throughput collapse is superlinear somewhere in the normal path;
the full-decode audit is exonerated at x1.1).
"""

from __future__ import annotations

import cProfile
import io
import os
import pathlib
import pstats
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

N_SEED = int(sys.argv[1]) if len(sys.argv) > 1 else 50000
TAIL_BATCHES = int(sys.argv[2]) if len(sys.argv) > 2 else 25
BATCH = 200


def seed_batch(store, batch: int) -> None:
    from memplex.models import FieldValue, Function, SourceDocument, SourceType

    with store.deferred_commit():
        for i in range(BATCH):
            idx = batch * BATCH + i
            store.add(
                Function(
                    id=f"bench-f-{idx}",
                    name=f"bench fact {idx} about topic {idx % 500}",
                    name_normalized=f"bench-fact-{idx}",
                    domain=f"topic-{idx % 500}",
                    memory_type="function",
                    source_type=SourceType.WIKI,
                    action=[FieldValue(desc=f"fact number {idx} with unique token tok{idx}")],
                ),
                SourceDocument(type="bench", content=f"content {idx} tok{idx}", source_type=SourceType.WIKI),
            )


def main() -> int:
    config = load_config()
    config.storage.backend = "lite"
    config.storage.path = str(pathlib.Path(tempfile.mkdtemp()) / "s.sqlite3")
    config.llm.query_enhancement = False
    svc = MemplexService(config=config)
    svc.start()
    t0 = time.time()
    for batch in range(N_SEED // BATCH):
        seed_batch(svc.store, batch)
    print(f"seeded {N_SEED} in {time.time()-t0:.0f}s", flush=True)

    profiler = cProfile.Profile()
    t1 = time.time()
    profiler.enable()
    for batch in range(N_SEED // BATCH, N_SEED // BATCH + TAIL_BATCHES):
        seed_batch(svc.store, batch)
    profiler.disable()
    print(f"tail {TAIL_BATCHES} batches in {time.time()-t1:.1f}s", flush=True)

    out = io.StringIO()
    stats = pstats.Stats(profiler, stream=out)
    stats.sort_stats("cumulative").print_stats(18)
    print(out.getvalue())
    for probe in ("from_dict", "_load_authoritative_locked"):
        out2 = io.StringIO()
        st2 = pstats.Stats(profiler, stream=out2)
        st2.print_callers(probe)
        print(f"\n===== CALLERS of {probe} =====")
        print("\n".join(out2.getvalue().splitlines()[:26]))
    svc.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
