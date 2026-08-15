#!/usr/bin/env bash
# Compatibility wrapper for the generic Memplex agent installer.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
LOCAL_SCRIPT="$SCRIPT_DIR/install-agent.sh"

if [[ -x "$LOCAL_SCRIPT" ]]; then
  exec "$LOCAL_SCRIPT" --agent hermes "$@"
fi

printf '%s\n' '[memplex-agent] error: packaged install-agent.sh is missing' >&2
exit 1
