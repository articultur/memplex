#!/usr/bin/env bash
# Memplex Claude Code hook launcher.
# Resolves plugin root from official variables and executes hook-runner.py.
set -euo pipefail

if [ "$#" -lt 1 ]; then
  echo "memplex: missing hook command" >&2
  exit 1
fi

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [ ! -f "${SCRIPT_DIR}/hook-runner.py" ]; then
  echo "memplex: hook-runner.py not found in ${SCRIPT_DIR}" >&2
  exit 1
fi

if [ -z "${MEMPLEX_PYTHON:-}" ]; then
  MEMPLEX_PYTHON="$(command -v python3 || command -v python || true)"
fi
if [ -z "${MEMPLEX_PYTHON}" ]; then
  echo "memplex: python not found (set MEMPLEX_PYTHON)" >&2
  exit 1
fi

exec "${MEMPLEX_PYTHON}" "${SCRIPT_DIR}/hook-runner.py" "$@"
