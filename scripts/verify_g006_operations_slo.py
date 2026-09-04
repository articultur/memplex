#!/usr/bin/env python3
"""离线验证 G006 签名 SLO 报告；公开输出不包含路径或异常。"""

from __future__ import annotations

import argparse
import json
import sys
from importlib.metadata import version
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memplex.config import load_config
from memplex.operations import (
    OperationsEvidenceError,
    OperationsReadinessBinding,
    load_operations_report,
    load_operations_signing_key,
)
from memplex.readiness_evidence import (
    ReadinessEvidenceError,
    load_deployment_evidence_binding_from_environment,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", required=True)
    parser.add_argument("--config", default=None)
    args = parser.parse_args()
    try:
        report = load_operations_report(Path(args.report))
        config = load_config(path=args.config)
        deployment = load_deployment_evidence_binding_from_environment(
            memplex_version=version("memplex")
        )
        binding = OperationsReadinessBinding(
            deployment_id=deployment.deployment_id,
            source_sha256=deployment.source_sha256,
            artifact_sha256=deployment.artifact_sha256,
            target_identity_sha256=deployment.target_identity_sha256,
            expected_key_id=config.operations.report_key_id,
        )
        report.verify_readiness(load_operations_signing_key(), binding=binding)
        payload = {
            "schema_version": 1,
            "verified": True,
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
        return 0
    except (OperationsEvidenceError, ReadinessEvidenceError, OSError, TypeError, ValueError):
        print('{"error":"operations_evidence_invalid","verified":false}')
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
