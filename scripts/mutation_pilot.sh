#!/usr/bin/env bash
# Mutation-testing pilot (cosmic-ray, sync_ingress).
# Runtime ~15 min locally. Not wired into per-PR CI; run before releases.
set -euo pipefail
cd "$(dirname "$0")/.."
SESSION=$(mktemp).sqlite
# cosmic-ray mutates the module IN PLACE; restore it even on failure/interrupt.
trap 'git checkout -- memplex/sync_ingress.py; rm -f "$SESSION"' EXIT
cosmic-ray init pyproject.toml "$SESSION"
cosmic-ray exec pyproject.toml "$SESSION"
cr-report "$SESSION" | tail -5
