"""Memplex CLI -- command-line interface using argparse.

Usage::

    memplex query "login function"
    memplex write --text "some observation"
    memplex write --file ./notes.txt
    memplex write --url https://example.com/doc
    memplex get func_abc123
    memplex delete func_abc123
    memplex feedback func_abc123 --role trigger --index 0 --verdict correct
    memplex pending
    memplex compact --scope project
    memplex health
    memplex stats
    memplex setup            # Install into detected local agents
    memplex install --agent codex
    memplex uninstall --agent openclaw
    memplex agent install --agent all
    memplex agent uninstall --agent openclaw
    memplex unsetup          # Uninstall Claude Code plugin

Global options::

    --config <path>     Path to config YAML file
    --output json|table Output format (default: table)
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Optional, Sequence

from memplex.adapters._shared import dataclass_to_dict as _dataclass_to_dict

# ── Helpers ─────────────────────────────────────────────────────────


def _make_service(config_path: Optional[str] = None):
    """Create and return a MemplexService instance."""
    from memplex.config import load_config
    from memplex.service import MemplexService

    config = load_config(path=config_path)
    return MemplexService(config=config)


def _fmt(data, output: str) -> str:
    """Format *data* for the chosen output mode."""
    if output == "json":
        return json.dumps(data, indent=2, default=str, ensure_ascii=False)

    # table / plain text
    if isinstance(data, list):
        if not data:
            return "(empty)"
        lines = []
        for item in data:
            if isinstance(item, dict):
                lines.append(_dict_to_table(item))
            else:
                lines.append(str(item))
        return "\n---\n".join(lines)

    if isinstance(data, dict):
        return _dict_to_table(data)

    return str(data)


def _dict_to_table(d: dict, indent: int = 0) -> str:
    """Recursively format a dict as indented key-value lines."""
    prefix = "  " * indent
    lines = []
    for k, v in d.items():
        if isinstance(v, dict):
            lines.append(f"{prefix}{k}:")
            lines.append(_dict_to_table(v, indent + 1))
        elif isinstance(v, list):
            lines.append(f"{prefix}{k}:")
            for item in v:
                if isinstance(item, dict):
                    lines.append(_dict_to_table(item, indent + 1))
                else:
                    lines.append(f"{prefix}  - {item}")
        else:
            lines.append(f"{prefix}{k}: {v}")
    return "\n".join(lines)


def _result_to_dict(result) -> dict:
    """Convert a SearchResult / QueryResult / dataclass to a dict."""
    if hasattr(result, "__dataclass_fields__"):
        return asdict(result)
    if isinstance(result, dict):
        return result
    return {"value": str(result)}


# ── Command implementations ────────────────────────────────────────


def cmd_query(args: argparse.Namespace) -> int:
    """Execute a memory query."""
    svc = _make_service(getattr(args, "config", None))
    try:
        result = svc.query(
            text=args.text,
            top_k=getattr(args, "top_k", 10),
            max_tokens=getattr(args, "max_tokens", 4000),
            explain=getattr(args, "explain", False),
        )

        out = []
        for r in result.results:
            out.append(
                {
                    "id": r.func_id,
                    "name": r.name,
                    "relevance": round(r.relevance_score, 4),
                    "summary": r.summary,
                    "scope": r.domain,
                }
            )

        payload = {
            "total": len(out),
            "scope": result.scope.value if hasattr(result.scope, "value") else str(result.scope),
            "latency_ms": result.latency_ms,
            "tokens_used": result.tokens_used,
            "truncated": result.truncated,
            "results": out,
        }
        if getattr(args, "explain", False):
            payload["explanation"] = result.explanation
        print(_fmt(payload, args.output))
        return 0
    finally:
        svc.stop()


def cmd_write(args: argparse.Namespace) -> int:
    """Write new content into memory."""
    svc = _make_service(getattr(args, "config", None))
    try:
        if args.text:
            content = args.text
            source_type = "text"
        elif args.file:
            with open(args.file, "r", encoding="utf-8") as f:
                content = f.read()
            source_type = "file"
        elif args.url:
            content = args.url
            source_type = "url"
        else:
            print("Error: provide --text, --file, or --url", file=sys.stderr)
            return 1

        result = svc.write_text(text=content, source_type=source_type)

        out = {
            "functions_extracted": len(result.functions),
            "edges": len(result.graph.edges),
            "function_ids": [f.id for f in result.functions],
        }
        print(_fmt(out, args.output))
        return 0
    finally:
        svc.stop()


def cmd_get(args: argparse.Namespace) -> int:
    """Retrieve a single memory by ID."""
    svc = _make_service(getattr(args, "config", None))
    try:
        func = svc.get(args.memory_id)
        if func is None:
            print(f"Memory not found: {args.memory_id}", file=sys.stderr)
            return 1

        print(_fmt(_dataclass_to_dict(func), args.output))
        return 0
    finally:
        svc.stop()


def cmd_delete(args: argparse.Namespace) -> int:
    """Delete a memory by ID."""
    svc = _make_service(getattr(args, "config", None))
    try:
        svc.delete(args.memory_id)
        print(_fmt({"status": "deleted", "id": args.memory_id}, args.output))
        return 0
    finally:
        svc.stop()


def cmd_feedback(args: argparse.Namespace) -> int:
    """Submit feedback for a memory field value."""
    svc = _make_service(getattr(args, "config", None))
    try:
        svc.submit_feedback(
            memory_id=args.memory_id,
            field_role=args.role,
            value_index=args.index,
            verdict=args.verdict,
        )
        print(
            _fmt(
                {
                    "status": "recorded",
                    "memory_id": args.memory_id,
                    "role": args.role,
                    "index": args.index,
                    "verdict": args.verdict,
                },
                args.output,
            )
        )
        return 0
    finally:
        svc.stop()


def cmd_pending(args: argparse.Namespace) -> int:
    """List pending reviews."""
    svc = _make_service(getattr(args, "config", None))
    try:
        reviews = svc.get_pending_reviews()
        out = [_dataclass_to_dict(r) for r in reviews]
        print(_fmt({"total": len(out), "reviews": out}, args.output))
        return 0
    finally:
        svc.stop()


def cmd_compact(args: argparse.Namespace) -> int:
    """Run the compaction pipeline."""
    svc = _make_service(getattr(args, "config", None))
    try:
        result = svc.compact(scope=getattr(args, "scope", "project"))
        out = _dataclass_to_dict(result)
        print(_fmt(out, args.output))
        return 0
    finally:
        svc.stop()


def cmd_health(args: argparse.Namespace) -> int:
    """Health check."""
    svc = _make_service(getattr(args, "config", None))
    try:
        info = svc.health()
        print(_fmt(info, args.output))
        return 0 if info.get("status") in {"healthy", "warning"} else 1
    finally:
        svc.stop()


def cmd_stats(args: argparse.Namespace) -> int:
    """Display statistics."""
    svc = _make_service(getattr(args, "config", None))
    try:
        info = svc.stats()
        print(_fmt(info, args.output))
        return 0
    finally:
        svc.stop()


def cmd_doctor(args: argparse.Namespace) -> int:
    """Run productized readiness checks."""
    from memplex.product import run_doctor

    svc = _make_service(getattr(args, "config", None))
    try:
        report = run_doctor(
            svc,
            agent=getattr(args, "agent", "codex"),
            profile=getattr(args, "profile", None),
            smoke=getattr(args, "smoke", False) or getattr(args, "fix", False),
        )
        print(_fmt(report, args.output))
        return 0 if report["status"] == "pass" else 1
    finally:
        svc.stop()


def _service_storage_namespace(svc) -> str:
    """Deprecated: use ``svc.storage_namespace()`` instead.

    Kept temporarily for any external callers; routes to the public
    service method so the storage-internal ``_path`` read no longer
    happens in adapter code.
    """
    return svc.storage_namespace()


def cmd_scope(args: argparse.Namespace) -> int:
    """Visibility-first scope commands."""
    from memplex.product import scope_catalog, scope_explain, scope_preview

    action = getattr(args, "scope_command", None)
    if action == "list":
        print(_fmt(scope_catalog(), args.output))
        return 0

    svc = _make_service(getattr(args, "config", None))
    try:
        explained = scope_explain(
            agent=getattr(args, "agent", "codex"),
            user_id=getattr(args, "user_id", None),
            session_id=getattr(args, "session_id", "default"),
            project_path=getattr(args, "project_path", None),
            storage_namespace=svc.storage_namespace(),
        )
        if action == "explain":
            print(_fmt(explained, args.output))
            return 0
        if action == "preview":
            print(
                _fmt(
                    scope_preview(
                        svc,
                        explained["namespace_filter"],
                        limit=getattr(args, "limit", 10),
                    ),
                    args.output,
                )
            )
            return 0
    finally:
        svc.stop()

    print("Error: unknown scope command", file=sys.stderr)
    return 1


def cmd_policy(args: argparse.Namespace) -> int:
    """Show recall/capture policy."""
    svc = _make_service(getattr(args, "config", None))
    try:
        print(_fmt(svc.policy(agent=getattr(args, "agent", "codex")), args.output))
        return 0
    finally:
        svc.stop()


def cmd_inbox(args: argparse.Namespace) -> int:
    """Review pending memory items through an inbox vocabulary."""
    action = getattr(args, "inbox_command", "list")
    svc = _make_service(getattr(args, "config", None))
    try:
        if action in {None, "list"}:
            reviews = svc.get_pending_reviews(limit=getattr(args, "limit", 100))
            print(
                _fmt({"total": len(reviews), "reviews": _dataclass_to_dict(reviews)}, args.output)
            )
            return 0
        if action == "show":
            reviews = [
                review
                for review in svc.get_pending_reviews(limit=100000)
                if review.memory_id == args.memory_id
            ]
            memory = svc.get(args.memory_id)
            print(
                _fmt(
                    {
                        "memory": _dataclass_to_dict(memory) if memory is not None else None,
                        "reviews": _dataclass_to_dict(reviews),
                    },
                    args.output,
                )
            )
            return 0 if memory is not None or reviews else 1
        if action in {"accept", "reject", "merge"}:
            result = svc.apply_resolution(
                memory_id=args.memory_id,
                field_role=args.field_role,
                action=action,
                new_value=getattr(args, "value", None),
            )
            print(_fmt(result, args.output))
            return 0
    finally:
        svc.stop()

    print("Error: unknown inbox command", file=sys.stderr)
    return 1


def cmd_corpus(args: argparse.Namespace) -> int:
    """Manifest-driven canonical corpus commands."""
    from memplex.product import corpus_index, corpus_preview, corpus_recall

    action = getattr(args, "corpus_command", None)
    if action == "preview":
        print(_fmt(corpus_preview(args.manifest, limit=getattr(args, "limit", 100)), args.output))
        return 0

    svc = _make_service(getattr(args, "config", None))
    try:
        if action == "index":
            print(
                _fmt(
                    corpus_index(
                        svc,
                        args.manifest,
                        dry_run=getattr(args, "dry_run", False),
                    ),
                    args.output,
                )
            )
            return 0
        if action == "recall":
            print(
                _fmt(
                    corpus_recall(
                        svc,
                        args.query,
                        top_k=getattr(args, "top_k", 10),
                        max_tokens=getattr(args, "max_tokens", 4000),
                    ),
                    args.output,
                )
            )
            return 0
    finally:
        svc.stop()

    print("Error: unknown corpus command", file=sys.stderr)
    return 1


def cmd_report(args: argparse.Namespace) -> int:
    """Generate an operator report."""
    from memplex.product import operator_report

    svc = _make_service(getattr(args, "config", None))
    try:
        print(_fmt(operator_report(svc, agent=getattr(args, "agent", "codex")), args.output))
        return 0
    finally:
        svc.stop()


def cmd_agent(args: argparse.Namespace) -> int:
    """Portable agent integration commands."""
    from memplex.adapters.agent_installer import install_agent, uninstall_agent
    from memplex.adapters.agent_runtime import (
        AgentMemoryRuntime,
        get_agent_manifest,
        list_agent_profiles,
    )

    action = getattr(args, "agent_command", None)
    if action == "list":
        print(_fmt(list_agent_profiles(), args.output))
        return 0
    if action == "manifest":
        print(_fmt(get_agent_manifest(args.agent), args.output))
        return 0
    if action == "install":
        result = install_agent(
            args.agent,
            target_dir=getattr(args, "target_dir", None),
            user_id=getattr(args, "user_id", None),
            project_path=getattr(args, "project_path", None),
            dry_run=getattr(args, "dry_run", False),
        )
        print(_fmt(_dataclass_to_dict(result), args.output))
        return 0
    if action == "uninstall":
        result = uninstall_agent(
            args.agent,
            target_dir=getattr(args, "target_dir", None),
            dry_run=getattr(args, "dry_run", False),
        )
        print(_fmt(_dataclass_to_dict(result), args.output))
        return 0

    svc = _make_service(getattr(args, "config", None))
    try:
        runtime = AgentMemoryRuntime(
            service=svc,
            agent=getattr(args, "agent", "codex"),
            user_id=getattr(args, "user_id", None),
            session_id=getattr(args, "session_id", "default"),
            project_path=getattr(args, "project_path", None),
            top_k=getattr(args, "top_k", 5),
            token_budget=getattr(args, "token_budget", 1500),
        )
        if action == "recall":
            recalled = runtime.before_prompt(args.prompt)
            print(_fmt(recalled.__dict__, args.output))
            return 0
        if action == "capture":
            runtime.after_response(
                user_message=args.user_message,
                assistant_message=args.assistant_message,
                next_prompt_hint=getattr(args, "next_prompt_hint", None),
            )
            print(_fmt({"status": "captured", "agent": runtime.agent}, args.output))
            return 0
    finally:
        svc.stop()

    print("Error: unknown agent command", file=sys.stderr)
    return 1


# ── Claude Code Plugin Setup ────────────────────────────────────────

_PLUGIN_AUTHOR = "articultur"
_PLUGIN_NAME = "memplex"


def _get_marketplace_dir() -> Path:
    """Return the Claude Code marketplace target directory."""
    claude_dir = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
    return claude_dir / "plugins" / "marketplaces" / _PLUGIN_AUTHOR


def cmd_setup(args: argparse.Namespace) -> int:
    """Install or uninstall Memplex in local agent hosts."""
    from memplex.adapters.agent_installer import install_agent, uninstall_agent
    from memplex.product import setup_profile

    should_uninstall = getattr(args, "uninstall", False) or args.command == "uninstall"
    if should_uninstall:
        result = uninstall_agent(
            args.agent,
            target_dir=getattr(args, "target_dir", None),
            dry_run=getattr(args, "dry_run", False),
        )
    else:
        result = install_agent(
            args.agent,
            target_dir=getattr(args, "target_dir", None),
            user_id=getattr(args, "user_id", None),
            project_path=getattr(args, "project_path", None),
            dry_run=getattr(args, "dry_run", False),
        )
    profile = setup_profile(getattr(args, "profile", None))
    output = _dataclass_to_dict(result)
    if profile is not None:
        output = {"profile": profile, "result": output}
    print(_fmt(output, args.output))
    return 0


def cmd_unsetup(args: argparse.Namespace) -> int:
    """Uninstall Memplex Claude Code plugin."""
    market_dir = _get_marketplace_dir()

    print("Memplex Plugin Uninstall")
    print("=" * 40)

    if not market_dir.exists():
        print("  Plugin not installed (directory not found).")
        return 0

    shutil.rmtree(market_dir)
    print(f"  Removed: {market_dir}")
    print("\nMemplex plugin uninstalled. Restart Claude Code to apply.")
    return 0


# ── Argument parser ────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    """Build and return the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="memplex",
        description="Memplex -- multi-agent memory system",
    )
    parser.add_argument("--config", default=None, help="Path to config YAML file")
    parser.add_argument(
        "--output",
        choices=["json", "table"],
        default="table",
        help="Output format (default: table)",
    )

    sub = parser.add_subparsers(dest="command", help="Available commands")

    _add_query_parsers(sub)
    _add_write_parsers(sub)
    _add_review_diag_parsers(sub)
    _add_product_parsers(sub)
    _add_agent_parsers(sub)
    _add_setup_parsers(sub)

    return parser


# ── Parser builders (split by domain from build_parser) ──────────────
# Each helper registers one cluster of subcommands on the shared ``sub``
# subparsers object. build_parser() just calls them in order. Adding a
# new command means extending the relevant helper (or adding a new one)
# instead of editing a 230-line function.


def _add_query_parsers(sub) -> None:
    """query + recall (the two recall-style commands)."""
    p_query = sub.add_parser("query", help="Query memory")
    p_query.add_argument("text", help="Query text")
    p_query.add_argument("--top-k", type=int, default=10, help="Max results")
    p_query.add_argument("--max-tokens", type=int, default=4000, help="Token budget")
    p_query.add_argument(
        "--explain",
        action="store_true",
        help="Explain retrieval stages, scores, filters, and token budget",
    )

    p_recall = sub.add_parser("recall", help="Recall memory (alias for query)")
    p_recall.add_argument("text", help="Recall query")
    p_recall.add_argument("--top-k", type=int, default=10, help="Max results")
    p_recall.add_argument("--max-tokens", type=int, default=4000, help="Token budget")
    p_recall.add_argument("--explain", action="store_true", help="Explain retrieval stages")


def _add_write_parsers(sub) -> None:
    """write / get / delete / feedback (memory mutation commands)."""
    p_write = sub.add_parser("write", help="Write content to memory")
    p_write.add_argument("--text", help="Raw text to write")
    p_write.add_argument("--file", help="File path to read and write")
    p_write.add_argument("--url", help="URL to write")

    p_get = sub.add_parser("get", help="Get memory by ID")
    p_get.add_argument("memory_id", help="Memory ID")

    p_del = sub.add_parser("delete", help="Delete memory by ID")
    p_del.add_argument("memory_id", help="Memory ID")

    p_fb = sub.add_parser("feedback", help="Submit feedback on a memory field")
    p_fb.add_argument("memory_id", help="Memory ID")
    p_fb.add_argument("--role", required=True, help="Field role (trigger|action|condition|benefit)")
    p_fb.add_argument("--index", type=int, required=True, help="Value index")
    p_fb.add_argument(
        "--verdict",
        required=True,
        choices=["correct", "wrong"],
        help="Verdict",
    )


def _add_review_diag_parsers(sub) -> None:
    """pending / compact / health / stats / doctor (review + diagnostics)."""
    sub.add_parser("pending", help="List pending reviews")

    p_compact = sub.add_parser("compact", help="Run compaction pipeline")
    p_compact.add_argument(
        "--scope",
        default="project",
        choices=["session", "project", "global"],
        help="Compaction scope (default: project)",
    )

    sub.add_parser("health", help="Health check")
    sub.add_parser("stats", help="Show statistics")

    p_doctor = sub.add_parser("doctor", help="Check Memplex product readiness")
    p_doctor.add_argument("--agent", default="codex")
    p_doctor.add_argument("--profile", choices=["local", "privacy", "max-recall", "team"])
    p_doctor.add_argument("--smoke", action="store_true", help="Run capture/recall smoke")
    p_doctor.add_argument("--fix", action="store_true", help="Run safe local smoke checks")


def _add_product_parsers(sub) -> None:
    """scope / policy / inbox / corpus / report (operator workflow commands)."""
    # -- scope --
    p_scope = sub.add_parser("scope", help="Explain visibility scopes")
    scope_sub = p_scope.add_subparsers(dest="scope_command", help="Scope command")
    scope_sub.add_parser("list", help="List visibility scopes")
    for name in ("explain", "preview"):
        p_scope_cmd = scope_sub.add_parser(name, help=f"{name.title()} agent namespace")
        p_scope_cmd.add_argument("--agent", default="codex")
        p_scope_cmd.add_argument("--user-id", default=None)
        p_scope_cmd.add_argument("--session-id", default="default")
        p_scope_cmd.add_argument("--project-path", default=None)
        if name == "preview":
            p_scope_cmd.add_argument("--limit", type=int, default=10)

    # -- policy --
    p_policy = sub.add_parser("policy", help="Show recall/capture policy")
    policy_sub = p_policy.add_subparsers(dest="policy_command", help="Policy command")
    p_policy_show = policy_sub.add_parser("show", help="Show current policy")
    p_policy_show.add_argument("--agent", default="codex")

    # -- inbox --
    p_inbox = sub.add_parser("inbox", help="Review pending memory inbox")
    inbox_sub = p_inbox.add_subparsers(dest="inbox_command", help="Inbox command")
    p_inbox_list = inbox_sub.add_parser("list", help="List pending reviews")
    p_inbox_list.add_argument("--limit", type=int, default=100)
    p_inbox_show = inbox_sub.add_parser("show", help="Show pending review and memory")
    p_inbox_show.add_argument("memory_id")
    for name in ("accept", "reject"):
        p_inbox_resolve = inbox_sub.add_parser(name, help=f"{name.title()} pending review")
        p_inbox_resolve.add_argument("memory_id")
        p_inbox_resolve.add_argument("--field-role", required=True)
    p_inbox_merge = inbox_sub.add_parser("merge", help="Merge a replacement value")
    p_inbox_merge.add_argument("memory_id")
    p_inbox_merge.add_argument("--field-role", required=True)
    p_inbox_merge.add_argument("--value", required=True)

    # -- corpus --
    p_corpus = sub.add_parser("corpus", help="Manifest-driven canonical corpus")
    corpus_sub = p_corpus.add_subparsers(dest="corpus_command", help="Corpus command")
    p_corpus_preview = corpus_sub.add_parser("preview", help="Preview manifest files")
    p_corpus_preview.add_argument("--manifest", required=True)
    p_corpus_preview.add_argument("--limit", type=int, default=100)
    p_corpus_index = corpus_sub.add_parser("index", help="Index selected corpus files")
    p_corpus_index.add_argument("--manifest", required=True)
    p_corpus_index.add_argument("--dry-run", action="store_true")
    p_corpus_recall = corpus_sub.add_parser("recall", help="Recall indexed corpus entries")
    p_corpus_recall.add_argument("query")
    p_corpus_recall.add_argument("--top-k", type=int, default=10)
    p_corpus_recall.add_argument("--max-tokens", type=int, default=4000)

    # -- report --
    p_report = sub.add_parser("report", help="Generate an operator report")
    p_report.add_argument("--agent", default="codex")


def _add_agent_parsers(sub) -> None:
    """agent (nested: list / manifest / install / uninstall / recall / capture)."""
    p_agent = sub.add_parser("agent", help="Portable agent integration commands")
    agent_sub = p_agent.add_subparsers(dest="agent_command", help="Agent integration command")
    agent_sub.add_parser("list", help="List supported agent profiles")

    p_agent_manifest = agent_sub.add_parser("manifest", help="Show agent manifest")
    p_agent_manifest.add_argument(
        "--agent",
        default="codex",
        help="Agent id: codex | claude-code | openclaw | hermes | all",
    )

    p_agent_install = agent_sub.add_parser("install", help="Install Memplex into an agent host")
    p_agent_install.add_argument(
        "--agent",
        default="all",
        help="Agent id: auto | codex | claude-code | openclaw | hermes | all",
    )
    p_agent_install.add_argument(
        "--target-dir",
        default=None,
        help="Override the agent config root directory for this install",
    )
    p_agent_install.add_argument("--user-id", default=None)
    p_agent_install.add_argument("--project-path", default=None)
    p_agent_install.add_argument(
        "--dry-run", action="store_true", help="Show planned files without writing"
    )

    p_agent_uninstall = agent_sub.add_parser(
        "uninstall", help="Uninstall Memplex from an agent host"
    )
    p_agent_uninstall.add_argument(
        "--agent",
        default="all",
        help="Agent id: auto | codex | claude-code | openclaw | hermes | all",
    )
    p_agent_uninstall.add_argument(
        "--target-dir",
        default=None,
        help="Override the agent config root directory for this uninstall",
    )
    p_agent_uninstall.add_argument(
        "--dry-run", action="store_true", help="Show planned files without writing"
    )

    p_agent_recall = agent_sub.add_parser("recall", help="Recall memories for prompt")
    p_agent_recall.add_argument("prompt", help="Prompt to recall against")
    p_agent_recall.add_argument("--agent", default="codex")
    p_agent_recall.add_argument("--user-id", default=None)
    p_agent_recall.add_argument("--session-id", default="default")
    p_agent_recall.add_argument("--project-path", default=None)
    p_agent_recall.add_argument("--top-k", type=int, default=5)
    p_agent_recall.add_argument("--token-budget", type=int, default=1500)

    p_agent_capture = agent_sub.add_parser("capture", help="Capture a completed agent turn")
    p_agent_capture.add_argument("--agent", default="codex")
    p_agent_capture.add_argument("--user-id", default=None)
    p_agent_capture.add_argument("--session-id", default="default")
    p_agent_capture.add_argument("--project-path", default=None)
    p_agent_capture.add_argument("--user-message", required=True)
    p_agent_capture.add_argument("--assistant-message", required=True)
    p_agent_capture.add_argument("--next-prompt-hint", default=None)


def _add_setup_parsers(sub) -> None:
    """setup / install / stepup / uninstall / unsetup (top-level install aliases)."""
    for name in ("setup", "install", "stepup"):
        _add_one_setup_parser(sub, name, uninstall=False)
    _add_one_setup_parser(sub, "uninstall", uninstall=True)

    sub.add_parser("unsetup", help="Uninstall Memplex Claude Code plugin")


def _add_one_setup_parser(sub, name: str, *, uninstall: bool = False):
    help_text = (
        "Uninstall Memplex from local agent hosts"
        if uninstall
        else "Set up Memplex in detected local agent hosts"
    )
    p_setup = sub.add_parser(name, help=help_text)
    p_setup.add_argument(
        "--agent",
        default="auto",
        help="Agent id: auto | codex | claude-code | openclaw | hermes | all",
    )
    p_setup.add_argument(
        "--target-dir",
        default=None,
        help="Override the selected agent config root directory",
    )
    p_setup.add_argument("--user-id", default=None)
    p_setup.add_argument("--project-path", default=None)
    if not uninstall:
        p_setup.add_argument(
            "--profile",
            choices=["local", "privacy", "max-recall", "team"],
            default=None,
            help="Transparent setup profile",
        )
    p_setup.add_argument(
        "--dry-run", action="store_true", help="Show planned files without writing"
    )
    if not uninstall:
        p_setup.add_argument(
            "--uninstall", action="store_true", help="Uninstall instead of install"
        )
    return p_setup


# ── Entry point ─────────────────────────────────────────────────────


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point.

    Parameters
    ----------
    argv:
        Argument list.  Defaults to ``sys.argv[1:]``.
    """
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    dispatch = {
        "query": cmd_query,
        "recall": cmd_query,
        "write": cmd_write,
        "get": cmd_get,
        "delete": cmd_delete,
        "feedback": cmd_feedback,
        "pending": cmd_pending,
        "compact": cmd_compact,
        "health": cmd_health,
        "stats": cmd_stats,
        "doctor": cmd_doctor,
        "scope": cmd_scope,
        "policy": cmd_policy,
        "inbox": cmd_inbox,
        "corpus": cmd_corpus,
        "report": cmd_report,
        "agent": cmd_agent,
        "setup": cmd_setup,
        "install": cmd_setup,
        "stepup": cmd_setup,
        "uninstall": cmd_setup,
        "unsetup": cmd_unsetup,
    }

    handler = dispatch.get(args.command)
    if handler is None:
        parser.print_help()
        return 1

    try:
        return handler(args)
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
