#!/usr/bin/env python3
"""Fast deterministic benchmark regression gate for CI.

Seeds 50 synthetic fact memories into a throwaway lite store and asserts
``fact_retention_rate`` (recall@10 over the seeded facts) clears a
conservative floor.  ``MemoryBenchmarkDataset`` content is index-derived
with no RNG, so the corpus is reproducible without seed plumbing; the
floor is set far below the measured value so the gate trips on retrieval
regressions, never on ranking noise.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
try:
    import benchmarks as _benchmarks_package
    import memplex as _memplex_package
except ModuleNotFoundError:
    sys.path.insert(0, str(PROJECT_ROOT))

from benchmarks.memory_eval import MemoryBenchmarkDataset, MemoryBenchmarkRunner
from memplex.config import MemplexConfig, StorageConfig
from memplex.service import MemplexService

TOP_K = 10
NUM_FACTS = 50
# Measured fact_retention_rate on this corpus is 1.0; the floor only
# catches a large retrieval regression.
RECALL_FLOOR = 0.50


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="memplex-bench-smoke-") as store_path:
        service = MemplexService(MemplexConfig(storage=StorageConfig(path=store_path)))
        service.start()
        try:
            dataset = MemoryBenchmarkDataset(num_facts=NUM_FACTS, num_prefs=0, num_obs=0)
            samples = dataset.load("")
            results = MemoryBenchmarkRunner(dataset).run_retrieval(service, samples, TOP_K)
        finally:
            service.stop()
    retention = next((r for r in results if r.metric == "fact_retention_rate"), None)
    if retention is None or retention.samples != NUM_FACTS:
        print("bench smoke: fact_retention_rate result missing or truncated", file=sys.stderr)
        return 1
    print(
        f"bench smoke: recall@{TOP_K} = {retention.value:.4f} "
        f"over {retention.samples} deterministic facts (floor {RECALL_FLOOR})"
    )
    if retention.value < RECALL_FLOOR:
        print(f"bench smoke FAILED: {retention.value:.4f} < {RECALL_FLOOR}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
