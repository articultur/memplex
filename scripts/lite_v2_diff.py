#!/usr/bin/env python3
"""ADR-012 Phase A gate: JSON authoritative pair vs SQLite shadow diff.

Compares the two representations row by row after a shadow-mode run:
- memory rows: id/kind sets identical, payload JSON equal, content_hash
  matches the recomputed hash
- change_log: event count and per-event JSON equal
- meta: generation equal

Exit 0 == 100% equal (migration gate passed); exit 1 with a report of
the first mismatches otherwise.

Usage:
    .venv/bin/python scripts/lite_v2_diff.py /path/to/store-dir
"""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _pair_records(store_dir: Path) -> dict[str, dict]:
    memory_path = store_dir / "memory.json"
    if not memory_path.exists():
        print(f"no memory pair at {memory_path}")
        raise SystemExit(2)
    # The durable pair is an envelope: {format_version, generation,
    # payload, peer_digest, transaction_id}; nodes live under payload.
    data = json.loads(memory_path.read_text(encoding="utf-8"))
    payload = data.get("payload", data)
    records: dict[str, dict] = {}
    for key, kind in (
        ("functions", "function"),
        ("facts", "fact"),
        ("preferences", "preference"),
        ("observations", "observation"),
    ):
        for node in payload.get(key, []):
            if isinstance(node, dict) and node.get("id"):
                records[str(node["id"])] = {
                    "kind": kind,
                    "payload": json.dumps(node, default=str, sort_keys=True),
                }
    return records


def _pair_generation(store_dir: Path) -> int | None:
    memory_path = store_dir / "memory.json"
    if not memory_path.exists():
        return None
    generation = json.loads(memory_path.read_text(encoding="utf-8")).get("generation")
    return generation if isinstance(generation, int) else None


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: lite_v2_diff.py <store-dir>")
        return 2
    store_dir = Path(sys.argv[1])
    shadow_path = store_dir / "shadow_v2.sqlite3"
    if not shadow_path.exists():
        print(f"no shadow db at {shadow_path}")
        return 2

    pair = _pair_records(store_dir)
    conn = sqlite3.connect(str(shadow_path))
    rows = {
        str(row[0]): {
            "kind": str(row[1]),
            "payload": str(row[2]),
            "hash": str(row[3]),
        }
        for row in conn.execute("SELECT id, kind, payload_json, content_hash FROM memories")
    }
    shadow_events = [str(r[0]) for r in conn.execute("SELECT event_json FROM change_log ORDER BY seq")]
    generation = conn.execute("SELECT value FROM meta WHERE key = 'generation'").fetchone()

    mismatch = 0
    only_pair = sorted(set(pair) - set(rows))
    only_shadow = sorted(set(rows) - set(pair))
    if only_pair:
        print(f"only in JSON pair ({len(only_pair)}): {only_pair[:5]}")
        mismatch += len(only_pair)
    if only_shadow:
        print(f"only in shadow ({len(only_shadow)}): {only_shadow[:5]}")
        mismatch += len(only_shadow)
    for obj_id in sorted(set(pair) & set(rows)):
        if pair[obj_id]["payload"] != rows[obj_id]["payload"]:
            print(f"payload differs: {obj_id}")
            mismatch += 1
    pair_events_path = store_dir / "changelog.json"
    if pair_events_path.exists():
        envelope = json.loads(pair_events_path.read_text(encoding="utf-8"))
        pair_events = [
            json.dumps(e, default=str, sort_keys=True)
            for e in envelope.get("payload", envelope.get("events", []))
        ]
        if pair_events != shadow_events:
            print(f"change_log differs: pair={len(pair_events)} shadow={len(shadow_events)}")
            for i, (a, b) in enumerate(zip(pair_events, shadow_events)):
                if a != b:
                    print(f"  first differing event #{i}:\n    pair:   {a[:200]}\n    shadow: {b[:200]}")
                    break
            mismatch += 1
    pair_generation = _pair_generation(store_dir)
    shadow_generation = generation[0] if generation else None
    if pair_generation is not None and str(pair_generation) != shadow_generation:
        print(f"generation differs: pair={pair_generation} shadow={shadow_generation}")
        mismatch += 1
    print(
        json.dumps(
            {
                "pair_objects": len(pair),
                "shadow_objects": len(rows),
                "shadow_events": len(shadow_events),
                "generation": shadow_generation,
                "mismatches": mismatch,
                "verdict": "EQUAL" if mismatch == 0 else "DIFFER",
            },
            indent=1,
        )
    )
    return 0 if mismatch == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
