#!/usr/bin/env bash
# Codex 原生插件入口；统一启动 Hook 与 MCP，避免依赖用户 shell 的 Python 别名。
set -euo pipefail

_PY="${MEMPLEX_PYTHON:-$(command -v python3 || command -v python || true)}"
if [ -z "$_PY" ]; then
  echo "memplex: python not found (set MEMPLEX_PYTHON)" >&2
  exit 1
fi

_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_PLUGIN_ROOT="${PLUGIN_ROOT:-$(cd "$_SCRIPT_DIR/.." && pwd)}"
export PLUGIN_ROOT="$_PLUGIN_ROOT"
_IDENTITY_PATH="$_PLUGIN_ROOT/memplex-agent.json"
_SOURCE_ROOT=""
if [ -f "$_IDENTITY_PATH" ]; then
  _SOURCE_ROOT="$(
    "$_PY" - "$_IDENTITY_PATH" <<'PY'
import json
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
try:
    payload = json.loads(path.read_text(encoding="utf-8"))
except Exception:
    raise SystemExit(0)

if isinstance(payload, dict):
    source_root = payload.get("source_root")
    if isinstance(source_root, str) and source_root.strip():
        print(source_root.strip())
PY
  )"
fi
if [ -n "$_SOURCE_ROOT" ]; then
  export PYTHONPATH="$_SOURCE_ROOT${PYTHONPATH:+:$PYTHONPATH}"
fi

exec "$_PY" -m memplex.adapters.codex_plugin "$@"
