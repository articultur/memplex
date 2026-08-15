#!/usr/bin/env python3
"""Verify signed G005 backup and disaster-recovery evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

from memplex.backup import (
    BackupIntegrityError,
    drill_result_from_json,
    load_backup_signing_key,
    load_verified_backup_manifest,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Verify signed G005 evidence")
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--drill-report", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        key = load_backup_signing_key()
        artifact = Path(args.artifact)
        manifest = load_verified_backup_manifest(artifact, key)
        report = drill_result_from_json(Path(args.drill_report).read_bytes())
        report.verify(key)
        if (
            report.backup_id != manifest.backup_id
            or report.data_digest != manifest.payload_sha256
            or not report.industrial_gate_closing
        ):
            raise BackupIntegrityError("g005_evidence_invalid")
        payload = {
            "schema_version": 1,
            "gate": "backup_restore_dr",
            "status": "verified",
            "backup_id": manifest.backup_id,
            "backend": manifest.backend,
            "database": manifest.database,
            "schema": manifest.schema,
            "payload_size": manifest.payload_size,
            "observed_rpo_seconds": report.observed_rpo_seconds,
            "observed_rto_seconds": report.observed_rto_seconds,
            "evidence_sha256": hashlib.sha256(report.canonical_bytes()).hexdigest(),
        }
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        return 0
    except Exception:
        print(
            json.dumps(
                {
                    "schema_version": 1,
                    "gate": "backup_restore_dr",
                    "status": "invalid",
                    "code": "g005_evidence_invalid",
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
