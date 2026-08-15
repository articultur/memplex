#!/usr/bin/env python3
"""Verify one release bundle and emit redacted signed local readiness evidence."""

from __future__ import annotations

import argparse
import json
import os
import sys
from hashlib import sha256
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from memplex.release import (  # noqa: E402
    ReleaseEvidence,
    ReleaseIntegrityError,
    verify_release_bundle,
    verify_release_evidence,
    write_release_evidence_atomic,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-root", type=Path, default=PROJECT_ROOT)
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--evidence-output", type=Path, required=True)
    parser.add_argument("--key-id", default="local-release-gate-v1")
    parser.add_argument("--key-env", default="MEMPLEX_RELEASE_EVIDENCE_KEY")
    return parser


def main() -> int:
    args = _parser().parse_args()
    try:
        key_text = os.environ.get(args.key_env, "")
        if len(key_text) != 64:
            raise ReleaseIntegrityError("release_evidence_key_invalid")
        signing_key = bytes.fromhex(key_text)
        if len(signing_key) != 32:
            raise ReleaseIntegrityError("release_evidence_key_invalid")
        manifest = verify_release_bundle(args.project_root, args.bundle)
        manifest_bytes = (args.bundle / "release-manifest.json").read_bytes()
        evidence = ReleaseEvidence.create(
            manifest=manifest,
            manifest_sha256=sha256(manifest_bytes).hexdigest(),
            sbom_sha256=sha256((args.bundle / "release-sbom.cdx.json").read_bytes()).hexdigest(),
            checksums_sha256=sha256(
                (args.bundle / "release-checksums.json").read_bytes()
            ).hexdigest(),
            key_id=args.key_id,
            signing_key=signing_key,
        )
        write_release_evidence_atomic(args.evidence_output, evidence)
        verify_release_evidence(
            args.project_root,
            args.bundle,
            args.evidence_output.read_bytes(),
            signing_key=signing_key,
        )
    except (OSError, UnicodeError, ValueError, ReleaseIntegrityError):
        print('{"schema_version":1,"status":"failed"}')
        return 2
    print(json.dumps({"schema_version": 1, "status": "passed"}, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
