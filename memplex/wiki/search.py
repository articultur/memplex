"""DualIndexSearch -- hybrid FTS + vector search for wiki pages.

Combines full-text keyword search with vector semantic search using
Reciprocal Rank Fusion (RRF) to merge results.

ID mapping convention: Wiki page filename = Function.id + ".md"
FTS and vector results are unified by Function.id, so RRF merging is
unambiguous.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List

from memplex.models import (
    SearchResult,
    SourceType,
    WikiPage,
)

if TYPE_CHECKING:
    from memplex.retrieval.embedding import EmbeddingService

logger = logging.getLogger(__name__)

# Wikilink pattern
_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


class DualIndexSearch:
    """Hybrid FTS + vector search over wiki pages.

    Parameters
    ----------
    wiki_dir:
        Root directory for wiki files.
    embedding_service:
        EmbeddingService for generating and comparing vector embeddings.
    """

    def __init__(
        self,
        wiki_dir: Path,
        embedding_service: EmbeddingService,
    ) -> None:
        self.wiki_dir = wiki_dir
        self._embedding = embedding_service

        # In-memory FTS index: page_id -> content (lowercased)
        self._fts_index: Dict[str, str] = {}
        # In-memory vector index: page_id -> embedding vector
        self._vector_index: Dict[str, List[float]] = {}

    # ── Public API ────────────────────────────────────────────────────

    def add_page(self, page: WikiPage) -> None:
        """Add a wiki page to both FTS and vector indices.

        Parameters
        ----------
        page:
            The WikiPage to index.
        """
        # FTS index: store lowered content for keyword matching
        self._fts_index[page.page_id] = page.content.lower()

        # Vector index: embed the page content
        try:
            vector = self._embedding.embed(page.content)
            self._vector_index[page.page_id] = vector
        except Exception:
            logger.warning(
                "Failed to embed page %s, skipping vector index",
                page.page_id,
            )

    def search(self, query: str, top_k: int = 10) -> List[SearchResult]:
        """Execute hybrid FTS + vector search with RRF merging.

        Steps:
        1. FTS keyword search over indexed page content
        2. Vector semantic search using embedding similarity
        3. Merge results with Reciprocal Rank Fusion

        Parameters
        ----------
        query:
            Search query string.
        top_k:
            Maximum number of results to return.
        """
        # 1. FTS search
        fts_results = self._fts_search(query)

        # 2. Vector search
        vector_results = self._vector_search(query)

        # 3. RRF merge
        merged = self._reciprocal_rank_fusion(fts_results, vector_results)

        return merged[:top_k]

    def rebuild_index(self) -> None:
        """Rebuild both FTS and vector indices from wiki files on disk.

        Reads all ``*.md`` files from ``wiki_dir/entities/`` and
        re-indexes them.
        """
        self._fts_index.clear()
        self._vector_index.clear()

        entities_dir = self.wiki_dir / "entities"
        if not entities_dir.exists():
            logger.info("No entities directory at %s, index empty", entities_dir)
            return

        for md_file in entities_dir.glob("*.md"):
            page_id = md_file.stem
            content = md_file.read_text(encoding="utf-8")
            page = WikiPage(page_id=page_id, content=content)
            self.add_page(page)

        logger.info(
            "Rebuilt index: %d pages (fts=%d, vector=%d)",
            len(self._fts_index),
            len(self._fts_index),
            len(self._vector_index),
        )

    # ── RRF merge ─────────────────────────────────────────────────────

    @staticmethod
    def _reciprocal_rank_fusion(
        fts_results: List[SearchResult],
        vector_results: List[SearchResult],
        k: int = 60,
    ) -> List[SearchResult]:
        """Merge FTS and vector results using Reciprocal Rank Fusion.

        RRF score = sum of 1/(k + rank_i) across both result sets.

        Parameters
        ----------
        fts_results:
            Results from full-text search.
        vector_results:
            Results from vector similarity search.
        k:
            RRF constant (default 60).  Higher k dampens the effect
            of individual rank positions.
        """
        item_map: Dict[str, SearchResult] = {}
        for item in fts_results + vector_results:
            item_map[item.func_id] = item

        scores: Dict[str, float] = {}
        for rank, item in enumerate(fts_results):
            scores[item.func_id] = scores.get(item.func_id, 0.0) + 1.0 / (k + rank + 1)
        for rank, item in enumerate(vector_results):
            scores[item.func_id] = scores.get(item.func_id, 0.0) + 1.0 / (k + rank + 1)

        sorted_ids = sorted(scores.items(), key=lambda x: x[1], reverse=True)

        results: List[SearchResult] = []
        for page_id, rrf_score in sorted_ids:
            item = item_map.get(page_id)
            if item is None:
                continue
            # Create a new result with the RRF score
            results.append(
                SearchResult(
                    func_id=item.func_id,
                    name=item.name,
                    domain=item.domain,
                    relevance_score=rrf_score,
                    summary=item.summary,
                    source_type=item.source_type,
                    created_at=item.created_at,
                    updated_at=item.updated_at,
                )
            )
        return results

    # ── Private: FTS search ───────────────────────────────────────────

    def _fts_search(self, query: str) -> List[SearchResult]:
        """Keyword search over indexed page content.

        Simple TF-based scoring: count of query word occurrences.
        """
        query_lower = query.lower()
        query_terms = set(query_lower.split())

        results: List[SearchResult] = []
        for page_id, content in self._fts_index.items():
            score = 0.0
            for term in query_terms:
                count = content.count(term)
                if count > 0:
                    score += min(count / 10.0, 1.0)
            if score > 0:
                # Extract a brief summary from content
                summary = self._extract_summary(content)
                results.append(
                    SearchResult(
                        func_id=page_id,
                        name=page_id,
                        domain="",
                        relevance_score=score,
                        summary=summary,
                        source_type=SourceType.WIKI,
                    )
                )

        results.sort(key=lambda r: r.relevance_score, reverse=True)
        return results

    # ── Private: vector search ────────────────────────────────────────

    def _vector_search(self, query: str) -> List[SearchResult]:
        """Semantic search using embedding cosine similarity."""
        if not self._vector_index:
            return []

        try:
            query_vector = self._embedding.embed(query)
        except Exception:
            logger.warning("Failed to embed query, skipping vector search")
            return []

        results: List[SearchResult] = []
        for page_id, doc_vector in self._vector_index.items():
            similarity = self._cosine_similarity(query_vector, doc_vector)
            if similarity > 0.1:  # Minimum relevance threshold
                content = self._fts_index.get(page_id, "")
                summary = self._extract_summary(content)
                results.append(
                    SearchResult(
                        func_id=page_id,
                        name=page_id,
                        domain="",
                        relevance_score=float(similarity),
                        summary=summary,
                        source_type=SourceType.WIKI,
                    )
                )

        results.sort(key=lambda r: r.relevance_score, reverse=True)
        return results

    # ── Utility helpers ───────────────────────────────────────────────

    @staticmethod
    def _cosine_similarity(a: List[float], b: List[float]) -> float:
        """Compute cosine similarity between two vectors."""
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = sum(x * x for x in a) ** 0.5
        norm_b = sum(x * x for x in b) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    @staticmethod
    def _extract_summary(content: str, max_len: int = 200) -> str:
        """Extract a brief summary from page content.

        Skips frontmatter and headings, returns the first substantive line.
        """
        in_frontmatter = False
        frontmatter_dashes = 0
        for line in content.splitlines():
            stripped = line.strip()
            if stripped == "---":
                frontmatter_dashes += 1
                if frontmatter_dashes == 1:
                    in_frontmatter = True
                    continue
                elif frontmatter_dashes == 2:
                    in_frontmatter = False
                    continue
            if in_frontmatter:
                continue
            if stripped and not stripped.startswith("#") and not stripped.startswith("|"):
                return stripped[:max_len]
        return ""
