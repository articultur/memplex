#!/usr/bin/env python3
"""离线验证 G006 签名 SLO 报告；公开输出不包含路径或异常。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from memplex.operations import (
    OperationsEvidenceError,
    alert_rules_sha256,
    load_operations_report,
    load_operations_signing_key,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    args = parser.parse_args()
    try:
        report = load_operations_report(Path(args.report))
        report.verify(load_operations_signing_key())
        valid = (
            report.alert_rules_sha256 == alert_rules_sha256()
            and report.industrial_gate_closing
        )
        payload = {
            "schema_version": 1,
            "verified": valid,
            "report_id": report.report_id,
            "key_id": report.key_id,
            "request_count": report.request_count,
            "availability": report.availability,
            "error_rate": report.error_rate,
            "p95_latency_ms": report.p95_latency_ms,
            "shutdown_drained": report.shutdown_drained,
            "industrial_gate_closing": report.industrial_gate_closing,
        }
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return 0 if valid else 1
    except (OperationsEvidenceError, OSError, TypeError, ValueError):
        print('{"error":"operations_evidence_invalid","verified":false}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
