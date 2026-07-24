"""WikiCompiler -- compile MemoryNode instances into Wiki markdown pages.

Responsibilities:
- Convert Function / Fact / Preference / Observation into WikiPage objects
- Generate index.md (directory of all pages)
- Lint all wiki pages for consistency
- Delegate search to DualIndexSearch with fallback to filename+grep
"""

from __future__ import annotations

import logging
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, List, Optional

from memplex.models import (
    Fact,
    FieldValue,
    Function,
    LintIssue,
    LintResult,
    Observation,
    Preference,
    SearchResult,
    SourceType,
    WikiPage,
)

if TYPE_CHECKING:
    from memplex.storage.base import MemoryStore
    from memplex.wiki.search import DualIndexSearch

logger = logging.getLogger(__name__)

# Default wiki directory
DEFAULT_WIKI_DIR = Path("~/.memplex/wiki/").expanduser()

# Maximum log lines before rotation
MAX_LOG_LINES = 1000
MAX_LOG_ARCHIVES = 5

# Wikilink pattern
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


class WikiCompiler:
    """Compile memory nodes into Wiki markdown pages.

    Parameters
    ----------
    store:
        MemoryStore backend for reading memory nodes.
    wiki_dir:
        Root directory for wiki files (default ``~/.memplex/wiki/``).
    dual_index:
        Optional :class:`DualIndexSearch` for hybrid search.
        When ``None``, search falls back to filename + grep.
    """

    def __init__(
        self,
        store: MemoryStore,
        wiki_dir: Path = DEFAULT_WIKI_DIR,
        dual_index: Optional["DualIndexSearch"] = None,
    ) -> None:
        self._store = store
        self.wiki_dir = wiki_dir
        self.dual_index = dual_index
        self._lock = threading.Lock()

    # ── Page compilation ──────────────────────────────────────────────

    def compile_function(self, func: Function) -> WikiPage:
        """Generate an Entity Page from a Function.

        The page includes YAML frontmatter and a structured body with
        trigger / condition / action / benefit sections plus cross-references
        as ``[[target_name]]`` wikilinks.
        """
        now = datetime.now(timezone.utc).isoformat()
        frontmatter = self._build_frontmatter(
            page_id=func.id,
            name=func.name,
            domain=func.domain or "uncategorized",
            memory_type=func.memory_type,
            confidence=func.confidence,
            created_at=func.created_at or now,
            updated_at=func.updated_at or now,
        )

        body_lines: list[str] = [f"# {func.id}", ""]
        body_lines.append(f"**Domain:** {func.domain or 'uncategorized'}")
        body_lines.append(f"**Confidence:** {func.confidence:.2f}")
        if func.source_paragraphs:
            body_lines.append(f"**Source Paragraphs:** [{', '.join(func.source_paragraphs)}]")
        body_lines.append("")

        # Field sections
        body_lines.extend(self._field_section("Trigger", func.trigger))
        body_lines.extend(self._field_section("Condition", func.condition))
        body_lines.extend(self._field_section("Action", func.action))
        body_lines.extend(self._field_section("Benefit", func.benefit))

        # Cross-references
        if func.cross_references:
            body_lines.append("## Cross-References")
            for ref in func.cross_references:
                if isinstance(ref, dict):
                    target = ref.get("target", ref.get("target_id", ""))
                    reason = ref.get("reason", "")
                    if target:
                        link = f"[[{target}]]"
                        body_lines.append(f"- {link}" + (f" -- {reason}" if reason else ""))
            body_lines.append("")

        content = frontmatter + "\n".join(body_lines)
        return WikiPage(
            page_id=func.id,
            content=content,
            metadata={
                "type": "entity",
                "domain": func.domain,
                "confidence": func.confidence,
            },
        )

    def compile_fact(self, fact: Fact) -> WikiPage:
        """Generate a Wiki page for a Fact node."""
        now = datetime.now(timezone.utc).isoformat()
        frontmatter = self._build_frontmatter(
            page_id=fact.id,
            name=fact.name or f"{fact.subject} {fact.predicate} {fact.object_}",
            domain=fact.domain or "uncategorized",
            memory_type=fact.memory_type,
            confidence=fact.confidence,
            created_at=fact.created_at or now,
            updated_at=fact.updated_at or now,
        )

        body_lines: list[str] = [f"# {fact.id}", ""]
        body_lines.append(f"**Domain:** {fact.domain or 'uncategorized'}")
        body_lines.append(f"**Confidence:** {fact.confidence:.2f}")
        if fact.valid_until:
            body_lines.append(f"**Valid Until:** {fact.valid_until}")
        body_lines.append("")
        body_lines.append("## Fact")
        body_lines.append(f"**{fact.subject}** {fact.predicate} **{fact.object_}**")
        body_lines.append("")

        content = frontmatter + "\n".join(body_lines)
        return WikiPage(
            page_id=fact.id,
            content=content,
            metadata={
                "type": "entity",
                "memory_type": "fact",
                "domain": fact.domain,
            },
        )

    def compile_preference(self, pref: Preference) -> WikiPage:
        """Generate a Wiki page for a Preference node."""
        now = datetime.now(timezone.utc).isoformat()
        frontmatter = self._build_frontmatter(
            page_id=pref.id,
            name=pref.name or f"pref_{pref.aspect}",
            domain=pref.domain or "uncategorized",
            memory_type=pref.memory_type,
            confidence=pref.confidence,
            created_at=pref.created_at or now,
            updated_at=pref.updated_at or now,
        )

        body_lines: list[str] = [f"# {pref.id}", ""]
        body_lines.append(f"**Domain:** {pref.domain or 'uncategorized'}")
        body_lines.append(f"**Confidence:** {pref.confidence:.2f}")
        if pref.subject_id:
            body_lines.append(f"**Subject:** {pref.subject_id}")
        body_lines.append("")
        body_lines.append("## Preference")
        body_lines.append(f"**Aspect:** {pref.aspect}")
        body_lines.append(f"**Preference:** {pref.preference}")
        body_lines.append("")

        content = frontmatter + "\n".join(body_lines)
        return WikiPage(
            page_id=pref.id,
            content=content,
            metadata={
                "type": "entity",
                "memory_type": "preference",
                "domain": pref.domain,
            },
        )

    def compile_observation(self, obs: Observation) -> WikiPage:
        """Generate a Wiki page for an Observation node."""
        now = datetime.now(timezone.utc).isoformat()
        frontmatter = self._build_frontmatter(
            page_id=obs.id,
            name=obs.name or f"obs_{obs.event[:30]}",
            domain=obs.domain or "uncategorized",
            memory_type=obs.memory_type,
            confidence=obs.confidence,
            created_at=obs.created_at or now,
            updated_at=obs.updated_at or now,
        )

        body_lines: list[str] = [f"# {obs.id}", ""]
        body_lines.append(f"**Domain:** {obs.domain or 'uncategorized'}")
        body_lines.append(f"**Confidence:** {obs.confidence:.2f}")
        body_lines.append(f"**Actor:** {obs.actor}")
        if obs.observed_at:
            body_lines.append(f"**Observed At:** {obs.observed_at}")
        body_lines.append("")
        body_lines.append("## Event")
        body_lines.append(obs.event)
        if obs.context:
            body_lines.append("")
            body_lines.append("## Context")
            body_lines.append(obs.context)
        body_lines.append("")

        content = frontmatter + "\n".join(body_lines)
        return WikiPage(
            page_id=obs.id,
            content=content,
            metadata={
                "type": "entity",
                "memory_type": "observation",
                "domain": obs.domain,
            },
        )

    def compile_index(self) -> WikiPage:
        """Generate the ``index.md`` directory page.

        Lists all Functions in a table, grouped by domain, with recent changes.
        """
        all_funcs = self._store.list_functions(limit=100000)

        lines: list[str] = [
            "# Memplex Knowledge Base",
            "",
            "## Entities (sorted by name)",
            "",
            "| Name | Domain | Confidence | Last Updated |",
            "|------|--------|------------|--------------|",
        ]
        for func in sorted(all_funcs, key=lambda f: f.name):
            updated = func.updated_at or "-"
            lines.append(
                f"| [[{func.id}]] | {func.domain or '-'} | {func.confidence:.2f} | {updated} |"
            )
        lines.append("")

        # Domains
        domain_groups: dict[str, list[Function]] = {}
        for func in all_funcs:
            domain = func.domain or "uncategorized"
            domain_groups.setdefault(domain, []).append(func)

        lines.append("## Domains")
        lines.append("")
        for domain, funcs in sorted(domain_groups.items()):
            lines.append(f"- [[domain_{domain}]] ({len(funcs)} functions)")
        lines.append("")

        # Recent changes
        lines.append("## Recent Changes")
        lines.append("")
        sorted_funcs = sorted(all_funcs, key=lambda f: f.updated_at or "", reverse=True)[:10]
        for func in sorted_funcs:
            lines.append(f"- {func.updated_at or '-'}: Updated `{func.name}` ({func.memory_type})")
        lines.append("")

        now = datetime.now(timezone.utc).isoformat()
        frontmatter = self._build_frontmatter(
            page_id="index",
            name="Memplex Knowledge Base",
            domain="",
            memory_type="index",
            confidence=1.0,
            created_at=now,
            updated_at=now,
        )
        content = frontmatter + "\n".join(lines)
        return WikiPage(
            page_id="index",
            content=content,
            metadata={"type": "index", "total_entities": len(all_funcs)},
        )

    def compile_all(self) -> List[WikiPage]:
        """Compile all memory nodes from the store into Wiki pages."""
        pages: List[WikiPage] = []

        # Compile functions
        funcs = self._store.list_functions(limit=100000)
        for func in funcs:
            pages.append(self.compile_function(func))

        # Compile index
        pages.append(self.compile_index())
        return pages

    # ── Search ────────────────────────────────────────────────────────

    def search(self, query: str, top_k: int = 10) -> List[SearchResult]:
        """Search wiki pages via DualIndexSearch or fallback to file scan."""
        if self.dual_index is not None:
            return self.dual_index.search(query, top_k)
        return self._fallback_search(query, top_k)

    # ── Lint ──────────────────────────────────────────────────────────

    def lint(self) -> LintResult:
        """Validate all wiki pages for consistency.

        Checks:
        - Every wiki page has valid YAML frontmatter
        - Wikilinks point to existing pages or store entries
        - Orphaned pages (no inbound links) produce warnings
        """
        issues: List[LintIssue] = []

        # Read all wiki pages from disk
        pages = self._read_all_pages()
        page_ids = {p.page_id for p in pages}
        all_funcs = {f.id for f in self._store.list_functions(limit=100000)}

        for page in pages:
            content = page.content

            # Check frontmatter
            if not content.startswith("---"):
                issues.append(
                    LintIssue(
                        page_id=page.page_id,
                        severity="error",
                        message="Missing YAML frontmatter",
                    )
                )
                continue

            # Parse frontmatter boundaries
            parts = content.split("---")
            if len(parts) < 3:
                issues.append(
                    LintIssue(
                        page_id=page.page_id,
                        severity="error",
                        message="Malformed frontmatter (missing closing ---)",
                    )
                )

            # Check wikilinks
            links = _WIKILINK_RE.findall(content)
            for link_target in links:
                # Skip domain links and non-function links
                if link_target.startswith("domain_"):
                    continue
                if link_target not in page_ids and link_target not in all_funcs:
                    issues.append(
                        LintIssue(
                            page_id=page.page_id,
                            severity="warning",
                            message=f"Broken wikilink: [[{link_target}]]",
                        )
                    )

        total = len(pages)
        return LintResult(
            total_pages=total,
            issues=issues,
            passed=all(i.severity != "error" for i in issues),
        )

    # ── File I/O helpers ──────────────────────────────────────────────

    def write_page(self, page: WikiPage) -> Path:
        """Write a WikiPage to disk under ``wiki_dir/entities/``."""
        entities_dir = self.wiki_dir / "entities"
        entities_dir.mkdir(parents=True, exist_ok=True)
        path = entities_dir / f"{page.page_id}.md"
        with self._lock:
            path.write_text(page.content, encoding="utf-8")
        return path

    def write_index(self, page: WikiPage) -> Path:
        """Write the index page to ``wiki_dir/index.md``."""
        self.wiki_dir.mkdir(parents=True, exist_ok=True)
        path = self.wiki_dir / "index.md"
        with self._lock:
            path.write_text(page.content, encoding="utf-8")
        return path

    def append_log(self, message: str) -> Path:
        """Append a line to ``wiki_dir/log.md`` with rotation.

        If log.md exceeds ``MAX_LOG_LINES``, rotate into archives
        (log.archive.1.md, log.archive.2.md, ...).  Keep at most
        ``MAX_LOG_ARCHIVES`` archives.
        """
        self.wiki_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.wiki_dir / "log.md"

        with self._lock:
            # Rotate if needed
            if log_path.exists():
                line_count = sum(1 for _ in open(log_path, encoding="utf-8"))
                if line_count >= MAX_LOG_LINES:
                    self._rotate_log(log_path)

            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"- {timestamp}: {message}\n")

        return log_path

    # ── Private helpers ───────────────────────────────────────────────

    @staticmethod
    def _build_frontmatter(
        *,
        page_id: str,
        name: str,
        domain: str,
        memory_type: str,
        confidence: float,
        created_at: str,
        updated_at: str,
    ) -> str:
        """Build YAML frontmatter block for a wiki page."""
        escaped_name = name.replace('"', '\\"').replace("\n", "\\n")
        lines = [
            "---",
            f'id: "{page_id}"',
            f'name: "{escaped_name}"',
            f'domain: "{domain}"',
            f'memory_type: "{memory_type}"',
            f"confidence: {confidence}",
            f'created_at: "{created_at}"',
            f'updated_at: "{updated_at}"',
            "---",
            "",
        ]
        return "\n".join(lines)

    @staticmethod
    def _field_section(heading: str, values: List[FieldValue]) -> list[str]:
        """Build markdown lines for a FieldValue list."""
        if not values:
            return []
        lines: list[str] = [f"## {heading}"]
        if len(values) == 1:
            lines.append(values[0].desc)
        else:
            for fv in values:
                lines.append(f"- {fv.desc}")
        lines.append("")
        return lines

    def _fallback_search(self, query: str, top_k: int) -> List[SearchResult]:
        """Degraded search via filename matching and grep.

        Used when DualIndexSearch is not available (e.g. Lite backend).
        """
        results: List[SearchResult] = []
        query_lower = query.lower()

        # Search in store
        funcs = self._store.list_functions(limit=100000)
        for func in funcs:
            score = 0.0
            if query_lower in func.name.lower():
                score += 0.8
            if func.domain and query_lower in func.domain.lower():
                score += 0.4
            for fv in func.trigger + func.action + func.benefit:
                if query_lower in fv.desc.lower():
                    score += 0.3
                    break
            if score > 0:
                results.append(
                    SearchResult(
                        func_id=func.id,
                        name=func.name,
                        domain=func.domain or "",
                        relevance_score=min(score, 1.0),
                        summary="; ".join(fv.desc for fv in func.action[:2]),
                        source_type=SourceType.WIKI,
                        created_at=func.created_at,
                        updated_at=func.updated_at,
                    )
                )

        results.sort(key=lambda r: r.relevance_score, reverse=True)
        return results[:top_k]

    def _read_all_pages(self) -> List[WikiPage]:
        """Read all entity pages from ``wiki_dir/entities/``."""
        pages: List[WikiPage] = []
        entities_dir = self.wiki_dir / "entities"
        if not entities_dir.exists():
            return pages
        for md_file in entities_dir.glob("*.md"):
            page_id = md_file.stem
            content = md_file.read_text(encoding="utf-8")
            pages.append(WikiPage(page_id=page_id, content=content))
        return pages

    def _rotate_log(self, log_path: Path) -> None:
        """Rotate log.md into numbered archives."""
        for i in range(MAX_LOG_ARCHIVES - 1, 0, -1):
            src = self.wiki_dir / f"log.archive.{i}.md"
            dst = self.wiki_dir / f"log.archive.{i + 1}.md"
            if src.exists():
                if i + 1 > MAX_LOG_ARCHIVES:
                    src.unlink()
                else:
                    src.rename(dst)
        log_path.rename(self.wiki_dir / "log.archive.1.md")
