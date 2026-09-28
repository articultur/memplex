# Deadlock fix end-to-end verification (B4 follow-up)

The 2026-09-23 incident — 7 concurrent runner processes, 4 silently
deadlocked (%CPU=0, cond_wait inside RLock paths) under embedding-heavy
write+query loops — was fixed by EmbeddingService RLock serialization,
the batch-embedding clamp, and the B4 single-writer queue, but the fix
was recorded as "hypothetical, never end-to-end re-verified under
multi-process load". This closes that gap.

## Protocol

`scripts/deadlock_stress.py`: 4 independent processes (spawn), each with
its own lite store, the real bge-m3 embedder (CPU) on the orchestrated
query path, 2 rounds of 8 writes + 5 queries each, 420s watchdog. A
child that fails to exit is the deadlock signature.

## Result

- verdict: **pass** — all 4 workers exited cleanly (exitcode 0, no hangs),
  elapsed 24.1s.
- Every worker result records `embedding_model: "bge-m3"` — the first
  attempt of this script ran a bare `MemplexConfig()` and silently fell
  back to TF-IDF (0.9s elapsed), which would have verified nothing; the
  corrected run uses `load_config()` and the model evidence is in the
  artifacts.

## Honest scope

- CPU device only: the MPS backend is confirmed unstable on this host
  and was the locus of the original wedge; this run verifies the RLock
  serialization + single-writer queue under multi-process CPU load, not
  the MPS-specific torch wedge.
- Scale: 4 processes × 2 rounds is a bounded smoke of the incident
  shape, not a soak test; the in-suite single-writer contract tests
  (3 writers + 2 readers) cover the thread-level races permanently.

Artifacts: `benchmarks/results/deadlock-stress/` (gitignored).
