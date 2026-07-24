#!/usr/bin/env python3
"""Memplex Hook Runner -- dispatches lifecycle hooks for Claude Code.

Called by plugin/hooks/hooks.json with subcommands:
    setup           - Environment check on plugin install
    session-start   - Load project context on session start
    prompt-submit   - Inject relevant memories on user prompt
    file-context    - PreToolUse context for Read operations
    observation     - Auto-collect observation from tool usage
    summarize       - Session summary and compaction

Usage:
    python hook-runner.py setup
    python hook-runner.py session-start
    python hook-runner.py prompt-submit
    python hook-runner.py file-context
    python hook-runner.py observation <tool_name> <session_id>
    python hook-runner.py summarize

Output contract (Claude Code hook):
    {"continue":true,"suppressOutput":true} - Non-blocking, no output shown
    stdout content - Injected as context
    exit 0 - Success
    exit 1 - Non-blocking error
    exit 2 - Blocking error
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import warnings
from pathlib import Path
from typing import Any, Optional

# Suppress noisy logging from dependencies
logging.getLogger("sentence_transformers").setLevel(logging.ERROR)
logging.getLogger("urllib3").setLevel(logging.WARNING)
warnings.filterwarnings("ignore", message=".*unauthenticated requests.*")

# Output contract prefix for Claude Code hooks
OUTPUT_CONTRACT = '{"continue":true,"suppressOutput":true}'

# Rate limiting
_RATE_FILE = Path("/tmp/.memplex_last_obs")
_RATE_LIMIT_SECONDS = 30

# Private tag stripping
_PRIVATE_TAG_RE = re.compile(r"<private>.*?</private>", re.DOTALL)


def _strip_private_tags(text: str) -> str:
    return _PRIVATE_TAG_RE.sub("", text)


def _find_plugin_root() -> Optional[Path]:
    """Find the plugin root using Claude Code's convention.

    Searches in order:
    1. MEMPLEX_PLUGIN_ROOT env var
    2. PLUGIN_ROOT env var
    3. Claude plugin cache: ~/.claude/plugins/cache/articultur/memplex/<version>/
    4. Claude marketplace: ~/.claude/plugins/marketplaces/articultur/plugin/
    """
    # Check env vars first
    for env_var in ("MEMPLEX_PLUGIN_ROOT", "PLUGIN_ROOT"):
        root = os.environ.get(env_var, "")
        if root:
            p = Path(root)
            if p.exists():
                return p

    # Search standard locations
    claude_config = os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude")

    # Try cache versions
    cache_base = Path(claude_config) / "plugins" / "cache" / "articultur" / "memplex"
    if cache_base.exists():
        # Get latest version directory
        versions = []
        for d in cache_base.iterdir():
            if d.is_dir():
                versions.append(d)
        if versions:
            # Sort by name (version directories)
            versions.sort(key=lambda x: x.name, reverse=True)
            for v in versions:
                scripts_dir = v / "scripts"
                if scripts_dir.exists():
                    hook_script = scripts_dir / "hook-runner.py"
                    if hook_script.exists():
                        return scripts_dir.parent

    # Try marketplace
    marketplace = Path(claude_config) / "plugins" / "marketplaces" / "articultur" / "plugin"
    if marketplace.exists():
        scripts_dir = marketplace / "scripts"
        if scripts_dir.exists() and (scripts_dir / "hook-runner.py").exists():
            return marketplace

    return None


def _ensure_memplex_importable() -> None:
    """Ensure memplex package is importable."""
    if "memplex" in sys.modules:
        return

    # Try to find the project root (parent of plugin directory)
    plugin_root = _find_plugin_root()
    if plugin_root:
        project_root = plugin_root.parent.parent
        if str(project_root) not in sys.path:
            sys.path.insert(0, str(project_root))


def _init_service():
    """Initialize MemplexService."""
    _ensure_memplex_importable()
    from memplex.config import load_config
    from memplex.service import MemplexService

    cfg = load_config()
    return MemplexService(config=cfg)


def _project_path() -> str:
    return os.environ.get("MEMPLEX_PROJECT_ROOT") or os.getcwd()


def _session_id(default: str = "claude-code") -> str:
    return os.environ.get("MEMPLEX_SESSION_ID") or default


def _user_id() -> str:
    return os.environ.get("MEMPLEX_USER_ID") or os.environ.get("USER") or "default"


def _init_runtime(session_id: str = ""):
    """Initialize the shared agent runtime for Claude Code hooks."""
    _ensure_memplex_importable()
    from memplex.adapters.agent_runtime import AgentMemoryRuntime

    return AgentMemoryRuntime(
        agent="claude-code",
        user_id=_user_id(),
        session_id=session_id or _session_id(),
        project_path=_project_path(),
    )


def _rate_file() -> Path:
    return Path(os.environ.get("MEMPLEX_OBS_RATE_FILE", str(_RATE_FILE)))


def _read_stdin_json() -> dict[str, Any]:
    try:
        if sys.stdin.isatty():
            return {}
        raw = sys.stdin.read()
        if not raw.strip():
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _hook_payload(data: dict[str, Any]) -> dict[str, Any]:
    payload = data.get("tool_input", data)
    return payload if isinstance(payload, dict) else {}


def _first_text(data: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _tool_name(data: dict[str, Any], fallback: str = "") -> str:
    return (
        fallback
        or os.environ.get("MEMPLEX_TOOL_NAME")
        or _first_text(data, "tool_name", "toolName", "name")
        or "unknown"
    )


def _print_contract(content: str = "") -> None:
    """Print output with Claude Code contract."""
    if content:
        print(content)
    print(OUTPUT_CONTRACT)


def _package_version(memplex_module: Any) -> str:
    """Resolve package version, preferring source-tree pyproject metadata."""
    try:
        import tomllib

        for parent in Path(__file__).resolve().parents:
            pyproject = parent / "pyproject.toml"
            if not pyproject.exists():
                continue
            project = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {})
            if project.get("name") == "memplex" and project.get("version"):
                return str(project["version"])
    except Exception:
        pass

    try:
        from importlib.metadata import version as pkg_version

        return pkg_version("memplex")
    except Exception:
        return getattr(memplex_module, "__version__", "unknown")


def cmd_setup() -> None:
    """Check environment on plugin install."""
    try:
        _ensure_memplex_importable()
        import memplex
        from memplex.config import load_config

        # Verify memplex is importable
        version = _package_version(memplex)

        # Check config
        try:
            cfg = load_config()
        except Exception:
            # Config doesn't exist yet, that's okay for setup
            cfg = None

        # Initialize service if config exists
        if cfg:
            from memplex.service import MemplexService

            service = MemplexService(config=cfg)
            health = service.health()
            print(f"[Memplex] v{version} installed. Status: {health.get('status', 'unknown')}")
        else:
            print(f"[Memplex] v{version} installed. Run 'memplex config init' to configure.")

    except ImportError as e:
        print(f"[Memplex] Setup failed: {e}", file=sys.stderr)
        print("[Memplex] Install memplex: pip install memplex", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"[Memplex] Setup warning: {e}", file=sys.stderr)
        # Non-fatal, exit 0

    _print_contract()
    sys.exit(0)


def cmd_session_start() -> None:
    """Load project context and inject relevant memories."""
    try:
        runtime = _init_runtime()
        recalled = runtime.before_prompt(f"session start {_project_path()}")
        if recalled.context:
            _print_contract("[Memplex Context]\n" + recalled.context)
        else:
            _print_contract("[Memplex] No memories yet for this project.")
    except Exception as e:
        print(f"[Memplex] session-start: {e}", file=sys.stderr)
        _print_contract()
    sys.exit(0)


def cmd_prompt_submit() -> None:
    """Inject relevant memories based on user prompt context.

    This hook runs on every user prompt submission. It reads the prompt
    from stdin and queries for relevant memories to inject.
    """
    try:
        data = _read_stdin_json()
        prompt = _first_text(data, "text", "prompt", "message", "user_prompt")

        if not prompt:
            _print_contract()
            sys.exit(0)

        recalled = _init_runtime().before_prompt(prompt)
        if recalled.context:
            _print_contract("[Memplex] Related memories:\n" + recalled.context)
        else:
            _print_contract()

    except Exception as e:
        print(f"[Memplex] prompt-submit: {e}", file=sys.stderr)
        _print_contract()
    sys.exit(0)


def cmd_file_context() -> None:
    """PreToolUse context for Read operations.

    When Claude Code is about to read files, this hook can inject
    relevant memories about those files or their content.
    """
    try:
        data = _read_stdin_json()
        payload = _hook_payload(data)
        file_path = _first_text(payload, "file_path", "path")

        if not file_path:
            _print_contract()
            sys.exit(0)

        filename = Path(file_path).name
        runtime = _init_runtime()
        recalled = runtime.before_prompt(filename)
        if not recalled.context:
            recalled = runtime.before_prompt(f"file {filename} {file_path}")
        if recalled.context:
            _print_contract("[Memplex] Related to this file:\n" + recalled.context)
        else:
            _print_contract()

    except Exception as e:
        print(f"[Memplex] file-context: {e}", file=sys.stderr)
        _print_contract()
    sys.exit(0)


def cmd_observation(tool_name: str = "", session_id: str = "") -> None:
    """Auto-collect observation from tool usage."""
    data = _read_stdin_json()
    payload = _hook_payload(data)
    tool_name = _tool_name(data, tool_name)

    # Rate limit
    rate_file = _rate_file()
    if rate_file.exists():
        try:
            last = float(rate_file.read_text().strip())
            if time.time() - last < _RATE_LIMIT_SECONDS:
                _print_contract()
                sys.exit(0)
        except (ValueError, OSError):
            pass

    if tool_name == "Bash" and "command" in payload:
        tool_input = f"Bash: {str(payload['command'])[:200]}"
    elif tool_name in ("Read", "Edit", "Write") and "file_path" in payload:
        tool_input = f"{tool_name}: {payload['file_path']}"
    else:
        tool_input = json.dumps(payload or data, ensure_ascii=False)[:300]

    tool_input = _strip_private_tags(tool_input)
    if not tool_input:
        _print_contract()
        sys.exit(0)

    obs_text = f"[{tool_name}] {tool_input}"

    try:
        runtime = _init_runtime(session_id=session_id)
        runtime.after_response(
            user_message=obs_text,
            assistant_message="Observed Claude Code tool use.",
            metadata={"tool_name": tool_name, "tool_input": payload},
        )
    except Exception as e:
        print(f"[Memplex] observation write skipped: {e}", file=sys.stderr)

    # Update rate limit
    try:
        rate_file.write_text(str(time.time()))
    except OSError:
        pass

    _print_contract()
    sys.exit(0)


def cmd_summarize() -> None:
    """Session summary and compaction."""
    service = None
    try:
        service = _init_service()
        compaction = service.compact(scope=os.environ.get("MEMPLEX_COMPACTION_SCOPE", "project"))
        stats = service.stats()

        summary = (
            "[Memplex] Session complete. "
            f"Memories: {stats.get('total_functions', 0)}, "
            f"Edges: {stats.get('total_edges', 0)}, "
            "Compaction: "
            f"processed={compaction.total_processed}, "
            f"merged={compaction.total_merged}, "
            f"removed={compaction.total_removed}"
        )
        print(summary)
        print(OUTPUT_CONTRACT)
    except Exception as e:
        print(f"[Memplex] summarize: {e}", file=sys.stderr)
        _print_contract()
    finally:
        if service is not None:
            service.stop()
    sys.exit(0)


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: hook-runner.py <command> [args]", file=sys.stderr)
        sys.exit(1)

    command = sys.argv[1]

    if command == "setup":
        cmd_setup()
    elif command == "session-start":
        cmd_session_start()
    elif command == "prompt-submit":
        cmd_prompt_submit()
    elif command == "file-context":
        cmd_file_context()
    elif command == "observation":
        tool_name = sys.argv[2] if len(sys.argv) > 2 else ""
        session_id = sys.argv[3] if len(sys.argv) > 3 else ""
        cmd_observation(tool_name, session_id)
    elif command in ("summarize", "session-stop"):
        cmd_summarize()
    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
