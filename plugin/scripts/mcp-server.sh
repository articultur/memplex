#!/usr/bin/env bash
# Memplex MCP server launcher.
# Resolves the Python interpreter via MEMPLEX_PYTHON, falling back to
# python3/python on PATH (mirrors the strategy in hooks/hooks.json).
set -euo pipefail

_PY="${MEMPLEX_PYTHON:-$(command -v python3 || command -v python || true)}"
if [ -z "$_PY" ]; then
  echo "memplex: python not found (set MEMPLEX_PYTHON)" >&2
  exit 1
fi

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_PLUGIN_ROOT="${MEMPLEX_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-$(cd "${_SCRIPT_DIR}/.." && pwd)}}"
PLUGIN_ROOT="${PLUGIN_ROOT:-${_PLUGIN_ROOT}}"
export PLUGIN_ROOT

exec "$_PY" "$_SCRIPT_DIR/hook-runner.py" mcp "$@"
