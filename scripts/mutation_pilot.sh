#!/usr/bin/env bash
# Mutation-testing pilot (cosmic-ray, sync_ingress).
# Runtime ~15 min locally. Not wired into per-PR CI; the nightly
# mutation-nightly workflow runs this with MUTATION_PILOT_REPORT set and
# compares the cr-report outcome counts against the recorded baseline.
set -euo pipefail
cd "$(dirname "$0")/.."
SESSION=$(mktemp).sqlite
# cosmic-ray mutates the module IN PLACE; restore it even on failure/interrupt.
trap 'git checkout -- memplex/sync_ingress.py; rm -f "$SESSION"' EXIT
cosmic-ray init pyproject.toml "$SESSION"
cosmic-ray exec pyproject.toml "$SESSION"
if [ -n "${MUTATION_PILOT_REPORT:-}" ]; then
  cr-report "$SESSION" > "$MUTATION_PILOT_REPORT"
fi
cr-report "$SESSION" | tail -5
