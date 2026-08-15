#!/usr/bin/env python3
"""离线验证 G009 签名证据；公开输出不包含路径、密钥或异常。"""

from __future__ import annotations

import argparse
import json
from importlib.metadata import version
from pathlib import Path

from memplex.capacity_chaos import (
    CapacityChaosEvidenceError,
    load_capacity_chaos_signing_key,
    read_capacity_chaos_evidence,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    try:
        report = read_capacity_chaos_evidence(Path(args.report))
        report.verify(
            load_capacity_chaos_signing_key(), expected_version=version("memplex")
        )
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "verified": True,
                    "report_id": report.report_id,
                    "function_count": report.function_count,
                    "edge_count": report.edge_count,
                    "operations_count": report.operations_count,
                    "throughput_ops_per_second": report.throughput_ops_per_second,
                    "error_rate": report.error_rate,
                    "rto_seconds": report.rto_seconds,
                    "industrial_gate_closing": report.industrial_gate_closing,
                },
                sort_keys=True,
                separators=(",", ":"),
            )
        )
        return 0
    except (CapacityChaosEvidenceError, OSError, TypeError, ValueError):
        print('{"error":"capacity_chaos_evidence_invalid","verified":false}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
