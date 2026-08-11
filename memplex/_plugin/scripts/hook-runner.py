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

import hashlib
import json
import logging
import os
import re
import sys
import tempfile
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
_RATE_LIMIT_SECONDS = 30

# Private tag stripping
_PRIVATE_TAG_RE = re.compile(r"<private>.*?</private>", re.DOTALL)


def _strip_private_tags(text: str) -> str:
    return _PRIVATE_TAG_RE.sub("", text)


def _sanitize_payload(value: Any) -> Any:
    """Recursively strip <private> tags from strings inside hook payloads."""
    if isinstance(value, str):
        return _strip_private_tags(value)
    if isinstance(value, dict):
        return {key: _sanitize_payload(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_payload(item) for item in value]
    return value


def _version_sort_key(path: Path) -> tuple[int, ...]:
    """Parse a version directory name into a comparable tuple.

    Non-numeric names sort below any real version.
    """
    try:
        return tuple(int(part) for part in path.name.split("."))
    except ValueError:
        return (0,)


def _find_plugin_root() -> Optional[Path]:
    """Find the plugin root using Claude Code's convention.

    Searches in order:
    1. MEMPLEX_PLUGIN_ROOT env var
    2. CLAUDE_PLUGIN_ROOT env var
    3. PLUGIN_ROOT env var (legacy compatibility)
    4. Claude plugin cache: ~/.claude/plugins/cache/articultur/memplex/<version>/
    5. Claude marketplace: ~/.claude/plugins/marketplaces/articultur/plugin/
    """
    # Check env vars first
    for env_var in ("MEMPLEX_PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT", "PLUGIN_ROOT"):
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
        # Get latest version directory (semantic version order, not lexicographic)
        versions = [d for d in cache_base.iterdir() if d.is_dir()]
        versions.sort(key=_version_sort_key, reverse=True)
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

    # Prefer an already-installed memplex; only inject paths as a fallback.
    try:
        import memplex  # noqa: F401

        return
    except ImportError:
        pass

    project_root = os.environ.get("MEMPLEX_PROJECT_ROOT")
    if project_root:
        candidate = Path(project_root)
        if (candidate / "memplex" / "__init__.py").exists() or (
            candidate / "pyproject.toml"
        ).exists():
            if str(candidate) not in sys.path:
                sys.path.insert(0, str(candidate))
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


def _managed_identity() -> dict[str, Any]:
    plugin_root = _find_plugin_root()
    claude_config = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    marketplace_identity = (
        claude_config / "plugins" / "marketplaces" / "articultur" / "plugin" / "memplex-agent.json"
    )
    plugin_data = os.environ.get("CLAUDE_PLUGIN_DATA", "")
    plugin_data_identity = Path(plugin_data) / "memplex-agent.json" if plugin_data else None
    candidates = [marketplace_identity]
    if plugin_root is not None:
        candidates.append(plugin_root / "memplex-agent.json")
    if plugin_data_identity is not None:
        candidates.append(plugin_data_identity)

    for identity_path in dict.fromkeys(candidates):
        try:
            identity = json.loads(identity_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(identity, dict):
            continue
        managed = identity.get("managed")
        if isinstance(managed, dict) and (
            managed.get("by") == "memplex" or managed.get("installer") == "memplex"
        ):
            configured_agent = str(identity.get("agent") or "").strip()
            user_id = str(identity.get("user_id") or "").strip()
            project_path = str(identity.get("project_path") or "").strip()
            if (configured_agent and configured_agent != "claude-code") or not user_id or not project_path:
                continue
            identity = {
                **identity,
                "user_id": user_id,
                "project_path": str(Path(project_path).expanduser().resolve(strict=False)),
            }
            if plugin_data_identity is not None and identity_path != plugin_data_identity:
                _persist_plugin_data_identity(plugin_data_identity, identity)
            return identity
    return {}


def _persist_plugin_data_identity(path: Path, identity: dict[str, Any]) -> None:
    """Mirror managed identity into Claude's update-stable plugin data directory."""
    temporary_path: Optional[Path] = None
    try:
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = None
        if current == identity:
            return

        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            json.dump(identity, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary_path.chmod(0o600)
        os.replace(temporary_path, path)
    except OSError as exc:
        logging.getLogger(__name__).warning(
            "Claude plugin identity persistence skipped: %s",
            exc,
        )
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass


def _project_path(data: Optional[dict[str, Any]] = None) -> str:
    identity = _managed_identity()
    return (
        str(identity.get("project_path") or "")
        or os.environ.get("MEMPLEX_PROJECT_ROOT")
        or _first_text(data or {}, "cwd", "project_path", "projectPath", "workspace_dir")
        or os.environ.get("CLAUDE_PROJECT_DIR")
        or os.getcwd()
    )


def _session_id(
    default: str = "claude-code",
    data: Optional[dict[str, Any]] = None,
) -> str:
    return (
        os.environ.get("MEMPLEX_SESSION_ID")
        or _first_text(data or {}, "session_id", "sessionId", "conversation_id")
        or default
    )


def _user_id(data: Optional[dict[str, Any]] = None) -> str:
    identity = _managed_identity()
    return (
        str(identity.get("user_id") or "")
        or os.environ.get("MEMPLEX_USER_ID")
        or _first_text(data or {}, "user_id", "userId")
        or os.environ.get("USER")
        or "default"
    )


def _init_runtime(session_id: str = "", data: Optional[dict[str, Any]] = None):
    """Initialize the shared agent runtime for Claude Code hooks."""
    _ensure_memplex_importable()
    from memplex.adapters.agent_runtime import AgentMemoryRuntime

    return AgentMemoryRuntime(
        agent="claude-code",
        user_id=_user_id(data),
        session_id=session_id or _session_id(data=data),
        project_path=_project_path(data),
    )


def _default_rate_file(data: Optional[dict[str, Any]] = None) -> Path:
    """Project-scoped rate-limit marker so concurrent projects don't throttle each other."""
    digest = hashlib.sha1(_project_path(data).encode("utf-8")).hexdigest()[:12]
    return Path(tempfile.gettempdir()) / f".memplex_last_obs_{digest}"


def _rate_file(data: Optional[dict[str, Any]] = None) -> Path:
    override = os.environ.get("MEMPLEX_OBS_RATE_FILE")
    return Path(override) if override else _default_rate_file(data)


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


def _session_start_query(data: Optional[dict[str, Any]] = None) -> str:
    """Build a meaningful recall query for session start.

    Defaults to project-name keywords (recall is already scoped to the
    project path); MEMPLEX_SESSION_QUERY overrides it.
    """
    override = os.environ.get("MEMPLEX_SESSION_QUERY", "").strip()
    if override:
        return override
    project_path = _project_path(data).rstrip("/")
    project_name = Path(project_path).name or project_path
    return f"{project_name} project context conventions decisions"


def cmd_session_start() -> None:
    """Load project context and inject relevant memories."""
    try:
        data = _read_stdin_json()
        runtime = _init_runtime(data=data)
        recalled = runtime.before_prompt(_session_start_query(data))
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

        recalled = _init_runtime(data=data).before_prompt(prompt)
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
        runtime = _init_runtime(data=data)
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


def _read_obs_rate_state(data: Optional[dict[str, Any]] = None) -> "tuple[float, str]":
    """Read (last_write_ts, last_event_key) from the observation rate file.

    The file holds JSON ``{"ts": ..., "key": ...}``; the legacy plain-float
    format (timestamp only, no dedup key) is still accepted.
    """
    try:
        raw = _rate_file(data).read_text().strip()
    except OSError:
        return 0.0, ""
    try:
        data = json.loads(raw)
    except ValueError:
        data = None
    if isinstance(data, dict):
        try:
            return float(data.get("ts", 0.0)), str(data.get("key", ""))
        except (TypeError, ValueError):
            return 0.0, ""
    try:
        return float(raw), ""
    except ValueError:
        return 0.0, ""


def _write_obs_rate_state(
    event_key: str,
    data: Optional[dict[str, Any]] = None,
) -> None:
    """Persist the rate-limit timestamp and dedup key for the next process."""
    try:
        _rate_file(data).write_text(json.dumps({"ts": time.time(), "key": event_key}))
    except OSError as exc:
        print(f"[Memplex] observation rate state skipped: {exc}", file=sys.stderr)


def cmd_observation(tool_name: str = "", session_id: str = "") -> None:
    """Auto-collect observation from tool usage."""
    data = _read_stdin_json()
    payload = _hook_payload(data)
    tool_name = _tool_name(data, tool_name)

    _ensure_memplex_importable()
    # Shared observation policy (single source: memplex.core.hooks.policy).
    from memplex.core.hooks.policy import tool_event_key, tool_narrative

    # Rate limit
    last_ts, last_key = _read_obs_rate_state(data)
    if time.time() - last_ts < _RATE_LIMIT_SECONDS:
        _print_contract()
        sys.exit(0)

    # Deduplicate consecutive identical tool events (survives the cooldown)
    event_key = tool_event_key(tool_name, payload)
    if last_key and event_key == last_key:
        _print_contract()
        sys.exit(0)

    obs_text = _strip_private_tags(tool_narrative(tool_name, payload))
    if not obs_text:
        _print_contract()
        sys.exit(0)

    try:
        runtime = _init_runtime(session_id=session_id, data=data)
        runtime.after_response(
            user_message=obs_text,
            assistant_message="Observed Claude Code tool use.",
            metadata={"tool_name": tool_name, "tool_input": _sanitize_payload(payload)},
        )
    except Exception as e:
        print(f"[Memplex] observation write skipped: {e}", file=sys.stderr)

    # Update rate limit timestamp + dedup key
    _write_obs_rate_state(event_key, data)

    _print_contract()
    sys.exit(0)


def cmd_mcp() -> None:
    """Start MCP server with a trusted Claude-Code identity context."""
    data: dict[str, Any] = {}

    # MCPServer takes its scope from environment variables.  A managed
    # launcher therefore replaces inherited scope fields, while retaining the
    # non-empty session supplied by Claude Code via _session_id().
    os.environ["MEMPLEX_AGENT_ID"] = "claude-code"
    os.environ["MEMPLEX_USER_ID"] = _user_id(data)
    os.environ["MEMPLEX_SESSION_ID"] = _session_id(
        default=f"claude-code-{os.getpid()}",
        data=data,
    )
    os.environ["MEMPLEX_PROJECT_ROOT"] = str(
        Path(_project_path(data)).expanduser().resolve(strict=False)
    )

    _ensure_memplex_importable()
    from memplex.adapters.mcp_server import MCPServer

    MCPServer().run()


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
    elif command == "mcp":
        cmd_mcp()
    elif command in ("summarize", "session-stop"):
        cmd_summarize()
    else:
        print(f"Unknown command: {command}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
