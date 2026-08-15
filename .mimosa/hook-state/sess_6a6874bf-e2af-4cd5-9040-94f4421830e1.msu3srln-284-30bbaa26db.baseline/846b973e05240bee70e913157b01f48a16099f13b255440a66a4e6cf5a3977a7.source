#!/usr/bin/env bash
# Install Memplex into a local agent host without requiring a source checkout.

set -euo pipefail

DEFAULT_PACKAGE="memplex==3.3.0"

usage() {
  cat <<'USAGE'
Usage:
  install-agent.sh [options]

Options:
  --agent <name>          Agent to install: auto, codex, claude-code, openclaw,
                          hermes, or all. Default: auto.
  --project-path <path>   Project path used for Memplex memory isolation.
                          Default: current directory.
  --user-id <id>          Memplex user id. Default: $USER or local-user.
  --venv-dir <path>       Persistent Python environment for Memplex.
                          Default: $XDG_DATA_HOME/memplex/agent-venv or
                          ~/.local/share/memplex/agent-venv.
  --codex-home <path>     Codex config directory, exported as CODEX_HOME.
  --claude-config-dir <path>
                          Claude Code config directory, exported as
                          CLAUDE_CONFIG_DIR.
  --openclaw-config-dir <path>
                          OpenClaw config directory, exported as
                          OPENCLAW_CONFIG_DIR.
  --hermes-config-dir <path>
                          Hermes config directory, exported as HERMES_CONFIG_DIR.
  --uninstall             Remove the selected Memplex integration.
  --dry-run               Print commands without executing them.
  -h, --help              Show this help.

Environment overrides:
  MEMPLEX_AGENT, MEMPLEX_PROJECT_PATH, MEMPLEX_USER_ID,
  MEMPLEX_VENV_DIR, MEMPLEX_DRY_RUN, CODEX_HOME, CLAUDE_CONFIG_DIR,
  OPENCLAW_CONFIG_DIR, HERMES_CONFIG_DIR, PYTHON

Examples:
  npx memplex@3.3.0 setup
  npx memplex@3.3.0 setup --agent codex
  npx memplex@3.3.0 setup --agent all
USAGE
}

log() {
  printf '[memplex-agent] %s\n' "$*"
}

die() {
  printf '[memplex-agent] error: %s\n' "$*" >&2
  exit 1
}

shell_quote() {
  printf '%q' "$1"
}

run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '+'
    for arg in "$@"; do
      printf ' '
      shell_quote "$arg"
    done
    printf '\n'
    return 0
  fi
  "$@"
}

find_python() {
  if [[ -n "${PYTHON:-}" ]]; then
    printf '%s\n' "$PYTHON"
    return 0
  fi
  if command -v python3 >/dev/null 2>&1; then
    command -v python3
    return 0
  fi
  if command -v python >/dev/null 2>&1; then
    command -v python
    return 0
  fi
  return 1
}

agent_supported() {
  case "$1" in
    codex|claude-code|openclaw|hermes|all|auto)
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

detect_agents() {
  local found=()
  if [[ -d "${CODEX_HOME:-$HOME/.codex}" ]] || command -v codex >/dev/null 2>&1; then
    found+=("codex")
  fi
  if [[ -d "${CLAUDE_CONFIG_DIR:-$HOME/.claude}" ]] || command -v claude >/dev/null 2>&1; then
    found+=("claude-code")
  fi
  if [[ -d "${OPENCLAW_CONFIG_DIR:-$HOME/.openclaw}" ]] || command -v openclaw >/dev/null 2>&1; then
    found+=("openclaw")
  fi
  if [[ -d "${HERMES_CONFIG_DIR:-$HOME/.hermes}" ]] || command -v hermes >/dev/null 2>&1; then
    found+=("hermes")
  fi
  printf '%s\n' "${found[@]}"
}

install_memplex() {
  if [[ "$DRY_RUN" != "1" ]]; then
    mkdir -p "$(dirname "$MEMPLEX_VENV_DIR")"
  fi
  if command -v uv >/dev/null 2>&1; then
    log "using uv to create persistent environment: $MEMPLEX_VENV_DIR"
    run uv venv "$MEMPLEX_VENV_DIR"
    run uv pip install --python "$MEMPLEX_VENV_DIR/bin/python" --upgrade "$DEFAULT_PACKAGE"
    return 0
  fi

  local python_bin
  python_bin="$(find_python)" || die "python3/python not found; install Python 3.11+ or uv first"
  log "uv not found; using $python_bin and pip"
  run "$python_bin" -m venv "$MEMPLEX_VENV_DIR"
  run "$MEMPLEX_VENV_DIR/bin/python" -m pip install --upgrade pip
  run "$MEMPLEX_VENV_DIR/bin/python" -m pip install --upgrade "$DEFAULT_PACKAGE"
}

run_agent_action() {
  local selected_agent="$1"
  if [[ "$ACTION" == "install" ]]; then
    run "$MEMPLEX_PYTHON" -m memplex agent install \
      --agent "$selected_agent" \
      --user-id "$MEMPLEX_USER_ID" \
      --project-path "$MEMPLEX_PROJECT_PATH"
  else
    run "$MEMPLEX_PYTHON" -m memplex agent uninstall --agent "$selected_agent"
  fi
}

ACTION="install"
AGENT="${MEMPLEX_AGENT:-auto}"
DRY_RUN="${MEMPLEX_DRY_RUN:-0}"
MEMPLEX_PROJECT_PATH="${MEMPLEX_PROJECT_PATH:-$PWD}"
MEMPLEX_USER_ID="${MEMPLEX_USER_ID:-${USER:-local-user}}"
DATA_HOME="${XDG_DATA_HOME:-${HOME:-$PWD}/.local/share}"
MEMPLEX_VENV_DIR="${MEMPLEX_VENV_DIR:-$DATA_HOME/memplex/agent-venv}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --agent)
      [[ $# -ge 2 ]] || die "$1 requires a value"
      AGENT="$2"
      shift 2
      ;;
    --project-path)
      [[ $# -ge 2 ]] || die "$1 requires a value"
      MEMPLEX_PROJECT_PATH="$2"
      shift 2
      ;;
    --user-id)
      [[ $# -ge 2 ]] || die "$1 requires a value"
      MEMPLEX_USER_ID="$2"
      shift 2
      ;;
    --venv-dir)
      [[ $# -ge 2 ]] || die "$1 requires a value"
      MEMPLEX_VENV_DIR="$2"
      shift 2
      ;;
    --codex-home)
      [[ $# -ge 2 ]] || die "$1 requires a value"
      export CODEX_HOME="$2"
      shift 2
      ;;
    --claude-config-dir)
      [[ $# -ge 2 ]] || die "$1 requires a value"
      export CLAUDE_CONFIG_DIR="$2"
      shift 2
      ;;
    --openclaw-config-dir)
      [[ $# -ge 2 ]] || die "$1 requires a value"
      export OPENCLAW_CONFIG_DIR="$2"
      shift 2
      ;;
    --hermes-config-dir)
      [[ $# -ge 2 ]] || die "$1 requires a value"
      export HERMES_CONFIG_DIR="$2"
      shift 2
      ;;
    --uninstall)
      ACTION="uninstall"
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      die "unknown option: $1"
      ;;
  esac
done

agent_supported "$AGENT" || die "unsupported agent: $AGENT"

if [[ "$MEMPLEX_VENV_DIR" != /* ]]; then
  MEMPLEX_VENV_DIR="$PWD/$MEMPLEX_VENV_DIR"
fi
MEMPLEX_PYTHON="$MEMPLEX_VENV_DIR/bin/python"
export MEMPLEX_PYTHON

SELECTED_AGENTS=()
if [[ "$AGENT" == "auto" ]]; then
  while IFS= read -r detected_agent; do
    [[ -n "$detected_agent" ]] && SELECTED_AGENTS+=("$detected_agent")
  done < <(detect_agents)
  if [[ "${#SELECTED_AGENTS[@]}" -eq 0 ]]; then
    die "no supported local agents detected; rerun with --agent codex|claude-code|openclaw|hermes|all"
  fi
  log "detected agents: ${SELECTED_AGENTS[*]}"
else
  SELECTED_AGENTS=("$AGENT")
fi

if [[ "$ACTION" == "install" ]]; then
  install_memplex
else
  if [[ ! -x "$MEMPLEX_PYTHON" && "$DRY_RUN" != "1" ]]; then
    die "managed Memplex Python not found at $MEMPLEX_PYTHON"
  fi
fi

for selected_agent in "${SELECTED_AGENTS[@]}"; do
  run_agent_action "$selected_agent"
done

log "Memplex $ACTION complete for agent: $AGENT"
