"""Small real-PostgreSQL smoke test for the G009 runner."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def test_g009_runner_executes_real_scale_shape_and_chaos_smoke(
    pg_function_dsn: str, tmp_path: Path
) -> None:
    root = Path(__file__).resolve().parent.parent
    report = tmp_path / "capacity.json"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(root)
    environment["MEMPLEX_CAPACITY_CHAOS_HMAC_KEY"] = "63" * 32
    result = subprocess.run(
        [
            sys.executable,
            "scripts/verify_g009_capacity_chaos.py",
            "--dsn",
            pg_function_dsn,
            "--workdir",
            str(tmp_path),
            "--evidence-output",
            str(report),
            "--functions",
            "1000",
            "--edges",
            "10000",
            "--soak-seconds",
            "1",
            "--concurrency",
            "3",
            "--allow-non-closing",
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    public = json.loads(result.stdout)
    assert public["function_count"] == 1000
    assert public["edge_count"] == 10000
    assert public["error_rate"] == 0.0
    assert public["verified"] is False
    evidence = json.loads(report.read_bytes())
    assert evidence["chaos"] == {
        "database": "passed",
        "disk": "passed",
        "duplicate_delivery": "passed",
        "kill": "passed",
        "network": "passed",
        "redis": "not_applicable",
        "term": "passed",
    }
