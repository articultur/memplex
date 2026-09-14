#!/usr/bin/env python3
"""Minimal Memplex closed loop: write memories, then recall them.

Runs fully offline against a throwaway Lite store in a temporary
directory. Try it from a source checkout:

    .venv/bin/python examples/quickstart.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

# Keep the run deterministic and API-free before any memplex import.
os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")
os.environ.setdefault("MEMPLEX_LLM_QUERY_ENHANCEMENT", "false")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from memplex.config import load_config
from memplex.service import MemplexService


def main() -> int:
    config = load_config()
    config.storage.backend = "lite"
    config.storage.path = str(Path(tempfile.mkdtemp(prefix="memplex-example-")))
    config.llm.query_enhancement = False

    svc = MemplexService(config=config)
    svc.start()
    try:
        svc.write_text(
            "We decided to use PostgreSQL 16 with pgvector for the memory "
            "store; the retention policy keeps contradicted facts forever.",
            source_type="conversation",
        )
        svc.write_text(
            "The retrieval stack reranks lexical, semantic and graph "
            "candidates with a six-dimension weighted score.",
            source_type="conversation",
        )

        result = svc.query("Which database backs the memory store?", top_k=3)
        print(f"query returned {len(result.results)} results "
              f"in {result.latency_ms} ms\n")
        for rank, hit in enumerate(result.results, start=1):
            print(f"{rank}. [{hit.relevance_score:.3f}] {hit.summary[:100]}")
    finally:
        svc.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
