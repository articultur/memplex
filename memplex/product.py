"""Productized operator workflows for Memplex.

These helpers keep CLI/MCP surfaces thin while preserving Memplex's core
local-first architecture. They do not add a storage backend, remote embedding
default, hidden ACL layer, or repo-wide indexer.
"""

from __future__ import annotations

import fnmatch
import logging
import tomllib
from pathlib import Path
from typing import Any, Iterable, Optional

from memplex.config import MemplexConfig

logger = logging.getLogger(__name__)


SETUP_PROFILES: dict[str, dict[str, Any]] = {
    "local": {
        "description": "Offline-friendly local defaults: lite storage, local retrieval, no remote embedding default.",
        "auto_recall": True,
        "auto_capture": "auto",
        "review_required": False,
        "remote_embedding_default": False,
    },
    "privacy": {
        "description": "Privacy-first defaults: local retrieval, review-gated capture, no remote providers.",
        "auto_recall": True,
        "auto_capture": "review",
        "review_required": True,
        "remote_embedding_default": False,
    },
    "max-recall": {
        "description": "Higher recall budget with explicit cost/safety visibility.",
        "auto_recall": True,
        "auto_capture": "auto",
        "review_required": False,
        "remote_embedding_default": False,
        "recommended_top_k": 10,
        "recommended_token_budget": 4000,
    },
    "team": {
        "description": "Project memory is shared deliberately; user and agent-local memory stay separated.",
        "auto_recall": True,
        "auto_capture": "review",
        "review_required": True,
        "remote_embedding_default": False,
    },
}

SCOPE_DESCRIPTIONS: dict[str, str] = {
    "session": "Only this agent session/conversation.",
    "project": "The current project path.",
    "user": "User-wide memory for this operator.",
    "agent": "Agent-specific memory such as codex, claude-code, openclaw, or hermes.",
    "global": "Explicitly shared memory. Memplex does not promote data here implicitly.",
}

PRIVATE_CORPUS_PATTERNS = (
    ".codex",
    ".agents",
    ".claude",
    ".git",
    ".gitnexus",
    ".env",
    ".env.*",
    "*secret*",
    "*credential*",
    "*token*",
)


def setup_profile(name: Optional[str]) -> Optional[dict[str, Any]]:
    """Return a named setup profile, or ``None`` when no profile was requested."""

    if name is None:
        return None
    if name not in SETUP_PROFILES:
        known = ", ".join(sorted(SETUP_PROFILES))
        raise ValueError(f"Unknown setup profile {name!r}. Known profiles: {known}")
    return {"name": name, **SETUP_PROFILES[name]}


def scope_catalog() -> dict[str, Any]:
    """Return the visibility-first scope vocabulary."""

    return {
        "boundary": "Visibility map only; not an ACL engine.",
        "scopes": SCOPE_DESCRIPTIONS,
    }


def scope_explain(
    *,
    agent: str,
    user_id: Optional[str],
    session_id: str,
    project_path: Optional[str],
    storage_namespace: str,
) -> dict[str, Any]:
    """Explain the namespace metadata a runtime will use."""

    project = str(Path(project_path or Path.cwd()).resolve())
    user = user_id or "default"
    return {
        "agent": agent,
        "scope_boundary": "Visibility-first metadata projection; not an ACL engine; enforcement remains in runtime/store filters.",
        "read_visibility": ["agent", "user", "session", "project", "storage_namespace"],
        "write_visibility": ["agent", "user", "session", "project", "storage_namespace"],
        "namespace_filter": {
            "memplex_agent": agent,
            "memplex_user_id": user,
            "memplex_session_id": session_id or "default",
            "memplex_project_path": project,
            "memplex_storage_namespace": storage_namespace,
        },
        "catalog": SCOPE_DESCRIPTIONS,
    }


def scope_preview(service, namespace_filter: dict[str, str], *, limit: int = 10) -> dict[str, Any]:
    """Count and sample memories matching a namespace filter."""

    try:
        funcs = service.store.list_functions(limit=100000)
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc),
            "namespace_filter": namespace_filter,
        }

    matches = []
    for func in funcs:
        attrs = getattr(func, "attributes", {}) or {}
        if all(attrs.get(key) == value for key, value in namespace_filter.items()):
            matches.append(
                {
                    "id": func.id,
                    "name": func.name,
                    "memory_type": getattr(func, "memory_type", "function"),
                    "domain": func.domain,
                }
            )

    return {
        "status": "ok",
        "boundary": "Preview only; does not grant or change access.",
        "namespace_filter": namespace_filter,
        "total_functions": len(funcs),
        "matching": len(matches),
        "sample": matches[:limit],
    }


def policy_show(config: MemplexConfig, *, agent: str = "codex") -> dict[str, Any]:
    """Return the recall/capture policy Memplex will use by default."""

    embedding_model = config.embedding.model
    remote_embedding = embedding_model.startswith(("hf:", "openai:", "anthropic:"))
    return {
        "agent": agent,
        "auto_recall": True,
        "auto_capture": "auto",
        "max_injected_tokens": config.retrieval.default_max_tokens,
        "skill_token_budget": config.retrieval.skill_max_tokens,
        "injection_scan_enabled": config.retrieval.injection_scan_enabled,
        "embedding": {
            "model": embedding_model,
            "remote_default": remote_embedding,
            "boundary": "Remote embeddings are opt-in; Memplex does not require them.",
        },
        "reranker": {
            "weights": dict(config.reranker.weights),
            "cross_encoder_enabled": config.reranker.cross_encoder_enabled,
            "cross_encoder_model": config.reranker.cross_encoder_model,
        },
        "scope_boundary": "Policy display does not mutate scope; not an ACL engine.",
    }


def _normalise_manifest(raw: dict[str, Any], manifest_path: Path) -> dict[str, Any]:
    corpus = raw.get("corpus", raw)
    include = corpus.get("include", [])
    deny = corpus.get("deny", corpus.get("exclude", []))
    if isinstance(include, str):
        include = [include]
    if isinstance(deny, str):
        deny = [deny]
    root_value = corpus.get("root", ".")
    root = (manifest_path.parent / root_value).resolve()
    return {
        "name": corpus.get("name", manifest_path.stem),
        "scope": corpus.get("scope", "project"),
        "root": root,
        "include": list(include),
        "deny": list(PRIVATE_CORPUS_PATTERNS) + list(deny),
        "read_only": bool(corpus.get("read_only", True)),
    }


def load_corpus_manifest(path: str | Path) -> dict[str, Any]:
    """Load a TOML corpus manifest."""

    manifest_path = Path(path).expanduser().resolve()
    raw = tomllib.loads(manifest_path.read_text(encoding="utf-8"))
    manifest = _normalise_manifest(raw, manifest_path)
    manifest["manifest_path"] = str(manifest_path)
    return manifest


def _is_denied(path: Path, root: Path, patterns: Iterable[str]) -> bool:
    rel_path = path.relative_to(root)
    rel = rel_path.as_posix()
    parts = rel_path.parts
    name = rel_path.name.lower()

    private_dirs = {".codex", ".agents", ".claude", ".git", ".gitnexus"}
    if any(part.lower() in private_dirs for part in parts):
        return True

    if name == ".env" or name.startswith(".env."):
        return True

    sensitive_name_fragments = ("secret", "credential", "token")
    if any(fragment in name for fragment in sensitive_name_fragments):
        return True

    return any(
        fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(rel_path.name, pattern)
        for pattern in patterns
    )


def corpus_preview(path: str | Path, *, limit: int = 100) -> dict[str, Any]:
    """Preview files selected by a canonical corpus manifest."""

    manifest = load_corpus_manifest(path)
    root: Path = manifest["root"]
    included: list[dict[str, Any]] = []
    denied: list[dict[str, Any]] = []
    seen: set[Path] = set()

    for pattern in manifest["include"]:
        for match in root.glob(pattern):
            if not match.is_file():
                continue
            resolved = match.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            if not resolved.is_relative_to(root):
                denied.append({"path": str(resolved), "reason": "outside_root"})
                continue
            if _is_denied(resolved, root, manifest["deny"]):
                denied.append(
                    {
                        "path": str(resolved.relative_to(root)),
                        "reason": "private_or_denied",
                    }
                )
                continue
            included.append(
                {
                    "path": str(resolved.relative_to(root)),
                    "bytes": resolved.stat().st_size,
                }
            )

    return {
        "status": "ok",
        "boundary": "Opt-in manifest preview; no repo-wide implicit indexing.",
        "manifest": {
            "name": manifest["name"],
            "scope": manifest["scope"],
            "root": str(root),
            "include": manifest["include"],
            "deny": manifest["deny"],
            "read_only": manifest["read_only"],
        },
        "included_count": len(included),
        "denied_count": len(denied),
        "included": included[:limit],
        "denied": denied[:limit],
    }


def corpus_index(service, path: str | Path, *, dry_run: bool = False) -> dict[str, Any]:
    """Index manifest-selected files as reviewable canonical corpus memories."""

    preview = corpus_preview(path, limit=100000)
    if dry_run:
        return {"status": "dry_run", **preview}

    manifest = load_corpus_manifest(path)
    root: Path = manifest["root"]
    indexed: list[dict[str, Any]] = []
    for item in preview["included"]:
        source_path = root / item["path"]
        text = source_path.read_text(encoding="utf-8")
        payload = (
            "Canonical Memplex corpus source.\n"
            f"Corpus: {manifest['name']}\n"
            f"Scope: {manifest['scope']}\n"
            f"Source Path: {item['path']}\n\n"
            f"{text}"
        )
        result = service.write_text(payload, source_type="file")
        attrs = {
            "memplex_corpus": "true",
            "memplex_corpus_name": manifest["name"],
            "memplex_corpus_scope": manifest["scope"],
            "memplex_source_path": item["path"],
            "memplex_manifest_path": manifest["manifest_path"],
            "memplex_canonical_read_only": str(manifest["read_only"]).lower(),
        }
        annotated = service.annotate_memories(
            [func.id for func in result.functions],
            attributes=attrs,
            needs_review=True,
        )
        for stored in annotated:
            indexed.append({"id": stored.id, "name": stored.name, "source_path": item["path"]})
    return {
        "status": "indexed",
        "boundary": "Canonical sources were indexed as read-only, reviewable memory; source files were not mutated.",
        "indexed_count": len(indexed),
        "indexed": indexed,
        "denied_count": preview["denied_count"],
        "denied": preview["denied"],
    }


def corpus_recall(service, query: str, *, top_k: int = 10, max_tokens: int = 4000) -> dict[str, Any]:
    """Recall only memories stamped as canonical corpus entries."""

    result = service.query(
        query,
        top_k=max(top_k * 3, top_k),
        max_tokens=max_tokens,
        namespace_filter={"memplex_corpus": "true"},
        explain=True,
    )
    entries = []
    for item in result.results:
        func = service.store.get(item.func_id)
        attrs = getattr(func, "attributes", {}) if func is not None else {}
        if attrs.get("memplex_corpus") != "true":
            continue
        entries.append(
            {
                "id": item.func_id,
                "name": item.name,
                "relevance": item.relevance_score,
                "summary": item.summary,
                "source_path": attrs.get("memplex_source_path"),
                "corpus": attrs.get("memplex_corpus_name"),
            }
        )
        if len(entries) >= top_k:
            break
    return {
        "total": len(entries),
        "results": entries,
        "explanation": result.explanation,
    }


def run_doctor(
    service,
    *,
    agent: str = "codex",
    profile: Optional[str] = None,
    smoke: bool = False,
) -> dict[str, Any]:
    """Run productized readiness checks."""

    from memplex.adapters.agent_runtime import get_agent_manifest

    checks: list[dict[str, Any]] = []
    health = service.health()
    checks.append(
        {
            "name": "service_health",
            "status": "pass" if health.get("status") in {"healthy", "warning"} else "fail",
            "details": health,
        }
    )

    try:
        manifest = get_agent_manifest(agent)
        checks.append(
            {
                "name": "agent_manifest",
                "status": "pass",
                "details": {
                    "agent": manifest["name"],
                    "manifest_version": manifest.get("version"),
                    "hooks": sorted(manifest.get("hooks", {}).keys()),
                },
            }
        )
    except Exception as exc:
        checks.append({"name": "agent_manifest", "status": "fail", "error": str(exc)})

    if profile is not None:
        checks.append(
            {
                "name": "setup_profile",
                "status": "pass",
                "details": setup_profile(profile),
            }
        )

    policy = service.policy(agent=agent)
    checks.append(
        {
            "name": "offline_first_boundary",
            "status": "pass",
            "details": {
                "embedding_model": policy["embedding"]["model"],
                "remote_default": policy["embedding"]["remote_default"],
                "boundary": "Remote embeddings are optional and never required by doctor.",
            },
        }
    )

    if smoke:
        canary = "memplex-doctor-smoke-token"
        captured_ids: list[str] = []
        try:
            result = service.write_text(f"{canary}: doctor smoke capture and recall.")
            captured_ids = [func.id for func in result.functions]
            query = service.query(canary, top_k=3, explain=True)
            found = any(canary in item.summary for item in query.results)
            details = {
                "captured": len(result.functions),
                "recalled": found,
                "explanation": query.explanation,
            }
            status = "pass" if found else "fail"
        except Exception as exc:
            details = {"error": str(exc), "captured_ids": captured_ids}
            status = "fail"
        finally:
            for memory_id in captured_ids:
                try:
                    service.delete(memory_id)
                except Exception:
                    logger.debug("Failed to clean doctor smoke memory %s", memory_id)
        checks.append(
            {
                "name": "capture_recall_smoke",
                "status": status,
                "details": details,
            }
        )

    failed = [check for check in checks if check["status"] == "fail"]
    status = "pass" if not failed else "fail"
    return {
        "status": status,
        "agent": agent,
        "profile": setup_profile(profile),
        "checks": checks,
        "next_steps": [] if not failed else ["Run memplex doctor --agent <agent> --smoke after fixing failed checks."],
    }


def lifecycle_counts(service) -> dict[str, int]:
    """Return derived lifecycle labels without changing storage schema."""

    counts = {"working": 0, "trusted": 0, "project": 0, "archived": 0, "blocked": 0}
    try:
        funcs = service.store.list_functions(limit=100000)
    except Exception:
        return counts
    for func in funcs:
        attrs = getattr(func, "attributes", {}) or {}
        if getattr(func, "needs_review", False):
            counts["blocked"] += 1
        elif attrs.get("memplex_corpus") == "true":
            counts["project"] += 1
        elif getattr(func, "access_count", 0) > 0:
            counts["trusted"] += 1
        else:
            counts["working"] += 1
    return counts


def operator_report(service, *, agent: str = "codex") -> dict[str, Any]:
    """Generate a local operator report."""

    pending = service.get_pending_reviews(limit=100)
    return {
        "health": service.health(),
        "stats": service.stats(),
        "policy": service.policy(agent=agent),
        "scope_catalog": scope_catalog(),
        "pending_reviews": len(pending),
        "lifecycle": {
            "boundary": "Derived labels only; not a canonical storage schema.",
            "counts": lifecycle_counts(service),
        },
    }
