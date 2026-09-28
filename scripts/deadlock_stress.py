#!/usr/bin/env python3
"""B4 end-to-end verification: multi-process concurrent lite stress.

The 2026-09-23 incident: 7 concurrent runner processes, 4 silently
deadlocked (%CPU=0, cond_wait inside RLock paths) during
embedding-heavy write+query loops. The fixes shipped since -
EmbeddingService RLock serialization, the batch-embedding clamp, and
the B4 single-writer queue - were never end-to-end re-verified under
multi-process load. This script reproduces the scenario shape on CPU
(the MPS device is confirmed unstable and is not part of this run):
N independent processes, each with its own store, bge-m3 embedding
active on the orchestrated query path, bounded rounds. Any child that
fails to exit inside its watchdog is the deadlock signature.

No network: queries run locally (query_enhancement disabled) and the
embedder is the local bge-m3 model.
"""

from __future__ import annotations

import json
import multiprocessing
import os
import pathlib
import sys
import tempfile
import time

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

SEED_TEXTS = [
    "I prefer green tea in the morning and espresso after lunch.",
    "My manager is Aria Tanaka and we have a 1:1 every Monday.",
    "I run twice a week, usually Tuesday and Thursday evenings.",
    "The kitchen renovation finishes next month; the contractor is Marlowe Bros.",
    "I keep my tax documents in the grey folder, second drawer.",
    "The office moved to the fourth floor; my desk is by the window.",
    "I play chess online in the evenings, rapid format mostly.",
    "I backup my photo library to the NAS every Sunday night.",
]
QUERIES = [
    "What do I drink in the morning?",
    "Who is my manager?",
    "When do I run?",
    "Who is the renovation contractor?",
    "Where do I keep my tax documents?",
]


def _child(worker_id: int, rounds: int, result_path: str) -> None:
    os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")
    os.environ.setdefault("MEMPLEX_LLM_QUERY_ENHANCEMENT", "false")
    os.environ.setdefault("MEMPLEX_EMBEDDING_MODEL", "bge-m3")
    os.environ.setdefault("MEMPLEX_EMBEDDING_DIMENSION", "1024")
    os.environ.setdefault("MEMPLEX_EMBEDDING_DEVICE", "cpu")
    from memplex.config import load_config
    from memplex.service import MemplexService

    store_dir = pathlib.Path(tempfile.mkdtemp(prefix=f"stress-{worker_id}-"))
    # load_config (not a bare MemplexConfig) so the MEMPLEX_* env above
    # actually reaches the embedder; a bare config silently falls back
    # to TF-IDF and this stress would never touch the embedding path.
    cfg = load_config()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(store_dir / "s.sqlite3")
    cfg.llm.query_enhancement = False
    svc = MemplexService(config=cfg)
    svc.start()
    written = 0
    queried = 0
    embedding_model = getattr(cfg.embedding, "model", "unset")
    try:
        for rnd in range(rounds):
            for text in SEED_TEXTS:
                svc.write_text(text, source_type="text")
                written += 1
            for question in QUERIES:
                svc.query(question, top_k=8, orchestrated=True, explain=False)
                queried += 1
    finally:
        svc.stop()
    pathlib.Path(result_path).write_text(
        json.dumps({
            "worker": worker_id,
            "written": written,
            "queried": queried,
            "embedding_model": embedding_model,
        })
    )


def main() -> int:
    procs = int(os.environ.get("STRESS_PROCS", "4"))
    rounds = int(os.environ.get("STRESS_ROUNDS", "2"))
    watchdog = float(os.environ.get("STRESS_WATCHDOG_SECONDS", "420"))
    out_dir = pathlib.Path(
        os.environ.get(
            "STRESS_OUT", "benchmarks/results/deadlock-stress"
        )
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    context = multiprocessing.get_context("spawn")
    workers = []
    started = time.monotonic()
    for i in range(procs):
        proc = context.Process(
            target=_child,
            args=(i, rounds, str(out_dir / f"worker-{i}.json")),
        )
        proc.start()
        workers.append(proc)

    rows = []
    verdict = "pass"
    for i, proc in enumerate(workers):
        proc.join(timeout=watchdog)
        row = {
            "worker": i,
            "exitcode": proc.exitcode,
            "hung": proc.is_alive(),
        }
        if proc.is_alive():
            verdict = "fail"
            proc.terminate()
            proc.join(timeout=30)
            if proc.is_alive():
                proc.kill()
        result_file = out_dir / f"worker-{i}.json"
        if result_file.exists():
            row.update(json.loads(result_file.read_text()))
        rows.append(row)
        if row.get("hung"):
            print(f"worker {i}: DEADLOCK SIGNATURE (no exit inside watchdog)", flush=True)

    elapsed = time.monotonic() - started
    summary = {
        "benchmark": "deadlock_stress",
        "protocol": (
            f"{procs} independent processes, {rounds} rounds of "
            f"{len(SEED_TEXTS)} writes + {len(QUERIES)} orchestrated queries, "
            f"CPU bge-m3 embedding, watchdog {watchdog}s; hang = deadlock signature"
        ),
        "verdict": verdict,
        "elapsed_s": round(elapsed, 1),
        "workers": rows,
    }
    print(json.dumps(summary, indent=1), flush=True)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=1))
    return 0 if verdict == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
