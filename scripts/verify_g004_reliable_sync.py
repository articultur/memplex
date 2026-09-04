#!/usr/bin/env python3
"""运行 G004 的 100001-event PostgreSQL 有界分页机器验证。"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(tempfile.gettempdir())
        / "memplex-g004-reliable-sync-report.json",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    environment = os.environ.copy()
    environment.pop("ALL_PROXY", None)
    environment.pop("all_proxy", None)
    environment["MEMPLEX_G004_REPORT_PATH"] = str(output)
    command = [
        sys.executable,
        "-m",
        "pytest",
        "-q",
        "tests/test_sync_postgres_integration.py::"
        + "test_postgres_pages_100001_mixed_events_with_bounded_monotonic_cursor",
    ]
    started = time.monotonic()
    completed = subprocess.run(command, env=environment, check=False)
    elapsed = time.monotonic() - started
    if completed.returncode != 0:
        return completed.returncode
    try:
        report = json.loads(output.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("G004 report was not produced") from exc
    required = {
        "event_count",
        "page_count",
        "page_size",
        "duplicate_receipts",
        "final_digest_sha256",
        "peak_rss",
        "pool_high_watermark",
        "pool_max_connections",
        "outbox_count",
        "dead_letters",
        "python",
        "platform",
        "machine",
        "postgresql",
    }
    if type(report) is not dict or set(report) != required:
        raise RuntimeError("G004 report has an invalid schema")
    if report["event_count"] != 100001 or report["page_count"] != 101:
        raise RuntimeError("G004 backlog evidence is incomplete")
    if report["page_size"] > 1000 or report["dead_letters"] != 0:
        raise RuntimeError("G004 bounded delivery evidence failed")
    if report["outbox_count"] != 100001:
        raise RuntimeError("G004 outbox count does not match the generated backlog")
    report["verification_seconds"] = round(elapsed, 6)
    temporary = output.with_name(f".{output.name}.tmp")
    temporary.write_text(
        json.dumps(
            report,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + "\n",
        encoding="utf-8",
    )
    os.replace(temporary, output)
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
