#!/usr/bin/env bash
# Compatibility wrapper for the generic Memplex agent installer.

set -euo pipefail

SCRIPT_URL="${MEMPLEX_INSTALL_AGENT_SCRIPT_URL:-https://raw.githubusercontent.com/articultur/memplex/main/scripts/install-agent.sh}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
LOCAL_SCRIPT="$SCRIPT_DIR/install-agent.sh"

if [[ -x "$LOCAL_SCRIPT" ]]; then
  exec "$LOCAL_SCRIPT" --agent hermes "$@"
else
  curl -fsSL "$SCRIPT_URL" | bash -s -- --agent hermes "$@"
fi
