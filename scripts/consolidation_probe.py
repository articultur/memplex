#!/usr/bin/env python3
"""F3 probe: rule-based consolidation through the real write path.

Demonstrates the two consolidation mechanisms on a live service:
promotion (rephrased repetition across days graduates from the episodic
paragraph layer to a typed Fact) and forgetting (one-off noise older
than the TTL leaves the store), plus the default-off no-op. This is a
local mechanism demonstration, not a claim on TSM's +12.2% - that
number was measured on their benchmark and their architecture.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import tempfile
from datetime import UTC, datetime, timedelta

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")
os.environ.setdefault("MEMPLEX_LLM_QUERY_ENHANCEMENT", "false")

from memplex.config import MemplexConfig
from memplex.consolidation import consolidate
from memplex.service import MemplexService

REPHRASES = [
    "The aloe plant is watered every Wednesday.",
    "I water the aloe plant every Wednesday.",
    "The aloe plant gets water every Wednesday.",
]


def main() -> int:
    store_dir = pathlib.Path(tempfile.mkdtemp(prefix="consol-"))
    cfg = MemplexConfig()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(store_dir / "s.sqlite3")
    cfg.llm.query_enhancement = False
    svc = MemplexService(config=cfg)
    svc.start()
    try:
        # Default-off: the pass must be a no-op (the flag is read per
        # call, so the probe owns the enable/disable sequencing).
        os.environ.pop("MEMPLEX_CONSOLIDATION", None)
        off_report = consolidate(svc.store).to_dict()
        assert off_report["enabled"] is False, off_report
        os.environ["MEMPLEX_CONSOLIDATION"] = "1"

        # Real write path: one-off noise plus the rephrased repetition.
        svc.write_text("The meeting room on floor two is booked today.", source_type="text")
        for text in REPHRASES:
            svc.write_text(text, source_type="text")

        # Backdating is probe-only (the write path stamps real "now"):
        # spread the rephrases across days so the span gate sees them as
        # distinct sessions, and age the noise row past the TTL.
        aloe_rows = [
            row for row in svc.store._paragraphs.values()
            if "aloe" in row.get("raw_text", "")
        ]
        for i, row in enumerate(aloe_rows):
            row["created_at"] = (datetime.now(UTC) - timedelta(days=2 - i)).isoformat()
        old = datetime.now(UTC) - timedelta(days=200)
        for row in svc.store._paragraphs.values():
            if "meeting room" in row.get("raw_text", ""):
                row["created_at"] = old.isoformat()

        before_nodes = len(svc.store._facts)
        report = consolidate(svc.store).to_dict()
        after_nodes = len(svc.store._facts)

        probe = svc.query(
            "When do I water the aloe plant?", top_k=8, orchestrated=True, explain=False
        )
        surface = "\n".join(r.summary for r in probe.results[:8]).lower()
        promoted_retrievable = "aloe" in surface and "wednesday" in surface

        summary = {
            "benchmark": "consolidation_probe",
            "default_off_report": off_report,
            "consolidation_report": report,
            "facts_before": before_nodes,
            "facts_after": after_nodes,
            "promoted_node_in_store": any(
                "aloe" in f.object_.lower() for f in svc.store._facts.values()
            ),
            "promoted_retrievable": promoted_retrievable,
            "noise_row_evicted": all(
                "meeting room" not in row.get("raw_text", "")
                for row in svc.store._paragraphs.values()
            ),
        }
        print(json.dumps(summary, indent=1), flush=True)

        out = pathlib.Path("benchmarks/results/consolidation-probe")
        out.mkdir(parents=True, exist_ok=True)
        (out / "summary.json").write_text(json.dumps(summary, indent=1))
        return 0
    finally:
        svc.stop()


if __name__ == "__main__":
    raise SystemExit(main())
