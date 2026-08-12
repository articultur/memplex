#!/usr/bin/env bash
# Memplex Claude Code hook launcher；受管 identity 锁定解释器与 Claude 根目录。
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

PLUGIN_ROOT="${MEMPLEX_PLUGIN_ROOT:-${CLAUDE_PLUGIN_ROOT:-$(cd "${SCRIPT_DIR}/.." && pwd)}}"
export PLUGIN_ROOT MEMPLEX_PLUGIN_ROOT="$PLUGIN_ROOT" CLAUDE_PLUGIN_ROOT="$PLUGIN_ROOT"
IDENTITY_PATH="${PLUGIN_ROOT}/memplex-agent.json"
IDENTITY_PARSER="/usr/bin/python3"
if [ ! -x "$IDENTITY_PARSER" ]; then
  echo "memplex: managed launcher cannot validate installation identity; reinstall required" >&2
  exit 1
fi
if [ ! -f "$IDENTITY_PATH" ]; then
  echo "memplex: missing managed installation identity; reinstall required" >&2
  exit 1
fi

if ! identity_values="$("$IDENTITY_PARSER" - "$IDENTITY_PATH" claude-code "$PLUGIN_ROOT" <<'PY'
import json
import os
import pathlib
import sys


def reject_duplicates(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate key")
        value[key] = item
    return value


def absolute_text(value):
    return type(value) is str and bool(value) and value == value.strip() and "\x00" not in value and "\n" not in value and "\r" not in value and os.path.isabs(value)


def expected_host_root(plugin_root, agent):
    root = pathlib.Path(plugin_root).resolve(strict=True)
    fixed = {
        "codex": (("plugins", "marketplaces", "memplex", "plugin"),),
        "claude-code": (("plugins", "marketplaces", "articultur", "plugin"),),
    }[agent]
    versioned = {
        "codex": ("plugins", "cache", "memplex", "memplex"),
        "claude-code": ("plugins", "cache", "articultur", "memplex"),
    }[agent]
    for candidate in root.parents:
        relative = root.relative_to(candidate).parts
        if relative in fixed or (
            len(relative) == len(versioned) + 1
            and relative[:-1] == versioned
            and relative[-1]
        ):
            return candidate
    raise ValueError("plugin is outside an approved installation layout")


try:
    payload = json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"), object_pairs_hook=reject_duplicates)
    expected_keys = {"agent", "user_id", "project_path", "python", "source_root", "host_root", "managed"}
    if type(payload) is not dict or set(payload) != expected_keys or payload["agent"] != sys.argv[2]:
        raise ValueError("invalid identity shape")
    if not all(absolute_text(payload[key]) for key in ("project_path", "python", "source_root", "host_root")):
        raise ValueError("invalid identity path")
    if type(payload["user_id"]) is not str or not payload["user_id"].strip() or payload["user_id"] != payload["user_id"].strip():
        raise ValueError("invalid identity user")
    managed = payload["managed"]
    if type(managed) is not dict or set(managed) != {"by", "installer", "schema_version"} or managed.get("by") != "memplex" or managed.get("installer") != "memplex" or type(managed.get("schema_version")) is not int or managed["schema_version"] != 1:
        raise ValueError("invalid managed identity")
    if not pathlib.Path(payload["source_root"]).is_dir() or not pathlib.Path(payload["host_root"]).is_dir():
        raise ValueError("unavailable managed root")
    expected_root = expected_host_root(sys.argv[3], sys.argv[2])
    recorded_root = pathlib.Path(payload["host_root"])
    canonical_root = recorded_root.resolve(strict=True)
    if str(recorded_root) != str(canonical_root) or not os.path.samefile(canonical_root, expected_root):
        raise ValueError("managed host root mismatch")
except Exception:
    raise SystemExit(1)

sys.stdout.write("\x1f".join((payload["python"], payload["source_root"], payload["host_root"])))
PY
)"; then
  echo "memplex: installation identity is invalid; reinstall required" >&2
  exit 1
fi
IFS=$'\037' read -r MEMPLEX_PYTHON SOURCE_ROOT HOST_ROOT <<< "$identity_values"
if [ -z "${MEMPLEX_PYTHON:-}" ] || [ -z "${SOURCE_ROOT:-}" ] || [ -z "${HOST_ROOT:-}" ]; then
  echo "memplex: installation identity is invalid; reinstall required" >&2
  exit 1
fi
if [ ! -x "$MEMPLEX_PYTHON" ]; then
  echo "memplex: recorded Python interpreter is unavailable: $MEMPLEX_PYTHON" >&2
  exit 1
fi

export CLAUDE_CONFIG_DIR="$HOST_ROOT"
export PYTHONPATH="$SOURCE_ROOT${PYTHONPATH:+:$PYTHONPATH}"
exec "$MEMPLEX_PYTHON" "${SCRIPT_DIR}/hook-runner.py" "$@"
