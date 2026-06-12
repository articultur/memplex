"""MemplexService -- unified user-facing entry point.

Orchestrates intent detection -> multi-path retrieval -> Rerank -> return.
Users call ``service.query(text)`` and never need to know about scopes,
retrieval paths, or ranking internals.

Usage::

    from memplex import MemplexService

    svc = MemplexService()          # uses default config
    svc.start()                    # start background worker

    result = svc.query("登录函数在哪")
    for r in result.results:
        print(r.name, r.relevance_score)

    svc.stop()
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from memplex.compaction import CompactionPipeline
from memplex.config import MemplexConfig, load_config
from memplex.core import CoreEngine
from memplex.llm import LLMEnhancer
from memplex.llm.injection_guard import IndirectInjectionGuard
from memplex.llm.provider import create_provider
from memplex.models import (
    CompactionResult,
    CompactionScope,
    ExtractedData,
    FeedbackVerdict,
    Function,
    MemoryFeedback,
    QueryResult,
    QueryScope,
    SearchResult,
    SourceDocument,
    SourceType,
    UpdateResult,
)
from memplex.processing.graph_builder import GraphBuilder
from memplex.retrieval.embedding import EmbeddingService, Vector
from memplex.retrieval.reranker import CrossEncoderReranker, Reranker
from memplex.storage import MemoryStore, create_store
from memplex.storage.feedback import FeedbackStore, create_feedback_store
from memplex.worker import BackgroundTask, BackgroundWorker

logger = logging.getLogger(__name__)


# ── Helper ─────────────────────────────────────────────────────────────


def _package_version() -> str:
    """Resolve Memplex version, preferring source-tree pyproject metadata."""
    try:
        import tomllib

        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        if pyproject.exists():
            project = tomllib.loads(pyproject.read_text(encoding="utf-8")).get(
                "project", {}
            )
            if project.get("name") == "memplex" and project.get("version"):
                return str(project["version"])
    except Exception:
        pass

    from importlib.metadata import version as pkg_version

    return pkg_version("memplex")


def _detect_memory_type(text: str) -> str:
    """Heuristic: classify text into a memory type.

    Returns one of ``"function"`` | ``"fact"`` | ``"preference"`` |
    ``"observation"``.
    """
    text_lower = text.lower()

    # Observation patterns
    obs_keywords = [
        "observe",
        "observed",
        "noticed",
        "happened",
        "occurred",
        "事件",
        "观察",
        "发生",
        "记录",
    ]
    if any(k in text_lower for k in obs_keywords):
        return "observation"

    # Preference patterns
    pref_keywords = [
        "prefer",
        "like",
        "dislike",
        "want",
        "always",
        "never",
        "喜欢",
        "偏好",
        "讨厌",
        "倾向",
        "总是",
        "从不",
    ]
    if any(k in text_lower for k in pref_keywords):
        return "preference"

    # Fact patterns
    fact_keywords = [
        "is",
        "are",
        "means",
        "defined as",
        "refers to",
        "是",
        "意味着",
        "定义为",
        "指的是",
        "事实",
    ]
    if any(k in text_lower for k in fact_keywords):
        return "fact"

    # Default: function (procedural / action-oriented)
    return "function"


# ── MemplexService ──────────────────────────────────────────────────────


class MemplexService:
    """Unified user-facing entry point for Memplex.

    Responsibilities:
        Intent detection -> multi-path retrieval -> Rerank -> return.

    Does **not** hold data; delegates to ``MemoryStore``,
    ``EmbeddingService``, ``Reranker``, ``LLMEnhancer``,
    ``BackgroundWorker``, and ``CompactionPipeline``.

    Parameters
    ----------
    config:
        Full :class:`MemplexConfig`.  When ``None``, loaded via
        :func:`load_config`.
    """

    def __init__(self, config: Optional[MemplexConfig] = None) -> None:
        self._config = config or load_config()
        cfg = self._config

        # ── Resolve backend (only "lite" is currently implemented) ──
        _implemented_backends = {"lite"}
        backend = cfg.storage.backend
        if backend not in _implemented_backends:
            logger.warning(
                "Storage backend %r not available, falling back to 'lite'",
                backend,
            )
            backend = "lite"

        # ── Storage ─────────────────────────────────────────────
        self.store: MemoryStore = create_store(
            backend=backend,
            path=cfg.storage.path,
        )

        # ── Embedding ───────────────────────────────────────────
        self._embedding_service = EmbeddingService(
            model=cfg.embedding.model,
            dimension=cfg.embedding.dimension,
            storage=self.store,
        )

        # ── Reranker ────────────────────────────────────────────
        self._reranker = Reranker(
            embedding_service=self._embedding_service,
            weights=cfg.reranker.weights,
            storage=self.store,
        )

        # ── Cross-encoder (stage 2, optional) ───────────────────
        self._cross_reranker = CrossEncoderReranker(
            model_name=cfg.reranker.cross_encoder_model,
            enabled=cfg.reranker.cross_encoder_enabled,
        )

        # ── LLM Enhancer (optional) ─────────────────────────────
        self._llm: Optional[LLMEnhancer] = None
        self._init_llm(cfg)

        # ── Feedback store ──────────────────────────────────────
        feedback_path = Path(cfg.storage.path).expanduser() / "feedback.json"
        self._feedback_store: FeedbackStore = create_feedback_store(
            backend=backend,
            path=feedback_path,
        )

        # ── Compaction pipeline ─────────────────────────────────
        self._compaction = CompactionPipeline(
            store=self.store,
            embedding_service=self._embedding_service,
            config=cfg,
        )

        # ── Background worker ───────────────────────────────────
        self._worker = BackgroundWorker(compaction_pipeline=self._compaction)

        # ── Graph builder ───────────────────────────────────────
        self._graph_builder = GraphBuilder(
            store=self.store,
            config=cfg,
        )

        # ── Core engine (extraction pipeline) ──────────────────
        self._engine = CoreEngine(store=self.store)

        # ── Injection scan tracking ─────────────────────────────
        self._injection_scans_24h: Dict[
            str, int
        ] = {}  # keyed by date string "YYYY-MM-DD"

    # ── LLM initialisation ──────────────────────────────────────

    def _init_llm(self, cfg: MemplexConfig) -> None:
        """Try to create an LLMEnhancer; silently skip on failure."""
        try:
            provider = create_provider(
                provider=cfg.llm.provider,
                anthropic_api_key=cfg.llm.anthropic_api_key,
                local_endpoint=cfg.llm.local_endpoint,
                local_model=cfg.llm.local_model,
                fallback_chain=cfg.llm.fallback_chain,
            )
            self._llm = LLMEnhancer(llm_provider=provider, config=cfg.llm)
        except Exception as exc:
            logger.info(
                "LLM enhancer not available (%s); using rule-based fallback", exc
            )
            self._llm = None

    # ── Injection scan helper ──────────────────────────────────────

    @staticmethod
    def _extract_scan_text(func: object, memory_type: str) -> str:
        """Extract the relevant text fields for injection scanning by memory type."""
        if memory_type == "function":
            return " ".join(
                fv.desc
                for role in ("trigger", "condition", "action", "benefit")
                for fv in getattr(func, role, [])
            )
        if memory_type == "fact":
            return " ".join(
                filter(
                    None,
                    [
                        getattr(func, "subject", ""),
                        getattr(func, "predicate", ""),
                        getattr(func, "object_", ""),
                    ],
                )
            )
        if memory_type == "preference":
            return " ".join(
                filter(
                    None,
                    [
                        getattr(func, "aspect", ""),
                        getattr(func, "preference", ""),
                    ],
                )
            )
        if memory_type == "observation":
            return " ".join(
                filter(
                    None,
                    [
                        getattr(func, "event", ""),
                        getattr(func, "context", ""),
                    ],
                )
            )
        # Unknown type: scan all string attributes
        return " ".join(str(v) for v in vars(func).values() if isinstance(v, str))

    # ════════════════════════════════════════════════════════════════
    #  Core query
    # ════════════════════════════════════════════════════════════════

    def query(
        self,
        text: str,
        top_k: int = 10,
        owner: Optional[str] = None,
        max_tokens: int = 4000,
        namespace_filter: Optional[Dict[str, str]] = None,
    ) -> QueryResult:
        """Unified query entry point.

        Pipeline:
        1. Intent detection (LLM first, keyword fallback).
        2. Parallel multi-path retrieval (ThreadPoolExecutor, 3 workers).
        3. Merge + deduplicate by ``func_id`` (keep highest score).
        4. Rerank (5-dim bi-encoder + optional cross-encoder).
        5. Update ``access_count`` (persisted).
        6. Token budget truncation (greedy by ``relevance_score``).

        Parameters
        ----------
        text:
            User query string.
        top_k:
            Maximum results to return.
        owner:
            Optional owner filter.
        max_tokens:
            Token budget for the result set (0 = unlimited).
            Estimated as ``len(summary) // 4``.
        namespace_filter:
            Optional exact-match metadata filter applied before rerank and
            ``access_count`` updates. Used by agent adapters to isolate turns.
        """
        scope = self._detect_scope(text)
        start = datetime.now()

        # Pre-compute query_vector (multi-path reuse)
        query_vector: Optional[Vector] = None
        if self._embedding_service is not None:
            if self._llm is not None and self._config.embedding.hyde_enabled:
                query_vector = self._compute_hyde_vector(text)
            query_vector = query_vector or self._embedding_service.embed(text)

        # Parallel multi-path retrieval
        futures: Dict[concurrent.futures.Future, str] = {}
        with ThreadPoolExecutor(max_workers=3) as pool:
            if scope in (QueryScope.IMMEDIATE, QueryScope.ALL):
                futures[pool.submit(self._rag_search, text, top_k, query_vector)] = (
                    "rag"
                )
            if scope in (QueryScope.SYNTHESIS, QueryScope.ALL):
                futures[pool.submit(self._wiki_search, text, top_k)] = "wiki"
            if scope in (QueryScope.RELATION, QueryScope.ALL):
                futures[pool.submit(self._graph_search, text, top_k, query_vector)] = (
                    "graph"
                )

            all_results: List[List[SearchResult]] = []
            for future in as_completed(futures):
                try:
                    all_results.append(future.result())
                except Exception as exc:
                    logger.warning("Search path %s failed: %s", futures[future], exc)

        # Merge results
        results = self._merge_multi_path(all_results)
        if namespace_filter:
            results = self._filter_results_by_namespace(results, namespace_filter)

        # Stage 1: 5-dim bi-encoder rerank
        if self._reranker is not None:
            results = self._reranker.rerank(text, results, top_k * 2, query_vector)

        # Stage 2: Cross-encoder precision rerank (optional)
        if self._cross_reranker is not None:
            results = self._cross_reranker.rerank(text, results)

        results = results[:top_k]

        # Update access_count (must persist for Reranker frequency dimension)
        for r in results:
            try:
                self.store.increment_access(r.func_id)
            except Exception:
                pass

        latency = int((datetime.now() - start).total_seconds() * 1000)

        # Token budget truncation (greedy, by relevance_score desc)
        truncated = False
        used = 0
        if max_tokens > 0:
            kept: List[SearchResult] = []
            for r in results:
                est = max(r.token_estimate, len(r.summary) // 4 + 1)
                r.token_estimate = est
                if used + est <= max_tokens:
                    kept.append(r)
                    used += est
                else:
                    truncated = True
            results = kept
        else:
            used = sum(r.token_estimate for r in results)

        return QueryResult(
            results=results,
            scope=scope,
            latency_ms=latency,
            tokens_used=used,
            truncated=truncated,
        )

    async def query_async(
        self,
        text: str,
        top_k: int = 10,
        owner: Optional[str] = None,
        max_tokens: int = 4000,
        namespace_filter: Optional[Dict[str, str]] = None,
    ) -> QueryResult:
        """Async version of :meth:`query`.

        Runs the synchronous ``query`` in a thread pool so it does not
        block the event loop (for FastAPI / MCP Server use).
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            lambda: self.query(
                text,
                top_k=top_k,
                owner=owner,
                max_tokens=max_tokens,
                namespace_filter=namespace_filter,
            ),
        )

    # ════════════════════════════════════════════════════════════════
    #  Intent detection
    # ════════════════════════════════════════════════════════════════

    def _detect_scope(self, text: str) -> QueryScope:
        """Intent detection: LLM path (priority) then keyword fallback.

        LLM path: calls ``enhance_query()`` and maps the returned intent
        to a :class:`QueryScope`.

        Keyword path: multi-label scoring; highest score wins.  Ties
        resolve to ``ALL`` (multi-path merge).
        """
        # LLM path (priority)
        if self._llm is not None and self._llm.config.query_enhancement:
            try:
                try:
                    asyncio.get_running_loop()
                    # Inside an existing event loop (FastAPI/MCP) --
                    # use a thread to avoid nested loop issues.
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _pool:
                        enhanced = _pool.submit(
                            asyncio.run, self._llm.enhance_query(text)
                        ).result(timeout=5.0)
                except RuntimeError:
                    # No running loop (CLI / sync call)
                    enhanced = asyncio.run(self._llm.enhance_query(text))

                intent_map = {
                    "search": QueryScope.IMMEDIATE,
                    "understand": QueryScope.SYNTHESIS,
                    "compare": QueryScope.ALL,
                    "relation": QueryScope.RELATION,
                }
                return intent_map.get(enhanced.intent, QueryScope.IMMEDIATE)
            except Exception:
                pass  # LLM failed, fall through to keyword

        # Keyword fallback
        text_lower = text.lower()
        negation_prefixes = [
            "不",
            "没有",
            "没",
            "非",
            "不是",
            "un",
            "not",
            "no ",
            "non-",
        ]
        cleaned = text_lower
        for neg in negation_prefixes:
            cleaned = cleaned.replace(neg, " ")

        scope_keywords = {
            QueryScope.RELATION: [
                "影响",
                "依赖",
                "调用",
                "关系",
                "哪些",
                "affect",
                "depend",
                "call",
                "relation",
                "impact",
            ],
            QueryScope.SYNTHESIS: [
                "设计",
                "架构",
                "概述",
                "整体",
                "概念",
                "原理",
                "design",
                "architecture",
                "overview",
                "concept",
                "how does",
            ],
            QueryScope.IMMEDIATE: [
                "在哪",
                "定义",
                "是什么",
                "查找",
                "搜索",
                "where",
                "define",
                "what is",
                "find",
                "search",
            ],
        }

        scores = {
            scope: sum(1 for k in kw if k in cleaned)
            for scope, kw in scope_keywords.items()
        }
        max_score = max(scores.values())
        if max_score == 0:
            return QueryScope.IMMEDIATE
        top_scopes = [s for s, v in scores.items() if v == max_score]
        return QueryScope.ALL if len(top_scopes) > 1 else top_scopes[0]

    # ════════════════════════════════════════════════════════════════
    #  Multi-path retrieval
    # ════════════════════════════════════════════════════════════════

    def _rag_search(
        self,
        text: str,
        top_k: int,
        query_vector: Optional[Vector] = None,
    ) -> List[SearchResult]:
        """RAG vector + FTS hybrid search with FTS fallback."""
        results = self.store.vector_search(text, top_k)
        # FTS fallback when vector search returns nothing
        if not results:
            results = self.store.fts_search(text, top_k)
        # Pre-fill vector_cache for Reranker reuse
        if query_vector is not None:
            for r in results:
                r.vector_cache = query_vector
        return results

    def _wiki_search(self, text: str, top_k: int) -> List[SearchResult]:
        """Wiki layer: FTS-based search over compiled wiki pages.

        Falls back to ``store.fts_search`` when no WikiCompiler is
        available.
        """
        return self.store.fts_search(text, top_k)

    def _graph_search(
        self,
        text: str,
        top_k: int,
        query_vector: Optional[Vector] = None,
    ) -> List[SearchResult]:
        """Incremental graph traversal search.

        1. Vector search to find seed Functions (top_k=3).
        2. Expand 1-hop neighbours via ``get_neighbors()``.
        3. Filter to relation-type edges.
        """
        seed_results = self.store.vector_search(text, top_k=3)
        if not seed_results:
            seed_results = self.store.fts_search(text, top_k=3)
        if not seed_results:
            return []

        results: List[SearchResult] = []
        seen: set = set()

        for seed in seed_results:
            if seed.func_id in seen:
                continue
            seen.add(seed.func_id)
            if query_vector is not None:
                seed.vector_cache = query_vector
            results.append(seed)

            # Incremental: only get this seed's neighbours
            try:
                neighbors = self.store.get_neighbors(seed.func_id, max_hops=1)
            except Exception:
                continue
            for neighbor in neighbors:
                if neighbor.id not in seen:
                    results.append(
                        SearchResult(
                            func_id=neighbor.id,
                            name=neighbor.name,
                            domain=neighbor.domain or "",
                            relevance_score=0.5,
                            summary=neighbor.name,
                            created_at=(
                                datetime.fromisoformat(neighbor.created_at)
                                if isinstance(neighbor.created_at, str)
                                and neighbor.created_at
                                else neighbor.created_at
                            ),
                            updated_at=(
                                datetime.fromisoformat(neighbor.updated_at)
                                if isinstance(neighbor.updated_at, str)
                                and neighbor.updated_at
                                else neighbor.updated_at
                            ),
                            origin=neighbor.origin_session or "",
                        )
                    )
                    seen.add(neighbor.id)

        return results[:top_k]

    @staticmethod
    def _merge_multi_path(
        result_lists: List[List[SearchResult]],
    ) -> List[SearchResult]:
        """Merge multi-path results; deduplicate by ``func_id``, keeping
        the highest ``relevance_score``."""
        seen: Dict[str, SearchResult] = {}
        for results in result_lists:
            for r in results:
                if (
                    r.func_id not in seen
                    or r.relevance_score > seen[r.func_id].relevance_score
                ):
                    seen[r.func_id] = r
        return sorted(seen.values(), key=lambda x: x.relevance_score, reverse=True)

    def _filter_results_by_namespace(
        self,
        results: List[SearchResult],
        namespace_filter: Dict[str, str],
    ) -> List[SearchResult]:
        """Keep only results whose stored metadata matches a namespace."""

        filtered: List[SearchResult] = []
        for result in results:
            func = self.store.get(result.func_id)
            if func is None:
                continue
            attrs = getattr(func, "attributes", {}) or {}
            if all(attrs.get(key) == value for key, value in namespace_filter.items()):
                filtered.append(result)
        return filtered

    # ════════════════════════════════════════════════════════════════
    #  HyDE
    # ════════════════════════════════════════════════════════════════

    def _compute_hyde_vector(self, text: str) -> Optional[Vector]:
        """Generate a HyDE (Hypothetical Document Embedding) vector.

        Uses ThreadPoolExecutor to isolate ``asyncio.run`` so it works
        in all environments (with or without a running event loop).

        Returns ``None`` on failure; the caller falls back to a raw
        query vector.
        """
        if self._llm is None:
            return None
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                hyde_text = pool.submit(
                    asyncio.run,
                    self._llm.enhance_query_hyde_text(text),
                ).result(timeout=5.0)
            return self._embedding_service.embed(hyde_text)
        except Exception as exc:
            logger.warning("HyDE failed, falling back to raw query vector: %s", exc)
            return None

    # ════════════════════════════════════════════════════════════════
    #  Write operations
    # ════════════════════════════════════════════════════════════════

    def write(self, source: SourceDocument) -> ExtractedData:
        """Write new content: extract Functions -> build graph edges
        -> ``store.merge()``.

        Parameters
        ----------
        source:
            The source document to ingest.

        Returns
        -------
        ExtractedData
            The extracted Functions and graph data (including any
            merge results).
        """
        # 1. CoreEngine: full extraction pipeline
        extracted = self._engine.extract(source)

        # 2. Merge into store
        if extracted.functions:
            # Scan each extracted function for indirect injection attacks
            today = datetime.now().strftime("%Y-%m-%d")
            for func in extracted.functions:
                memory_type = getattr(func, "memory_type", "function")
                scan_text = self._extract_scan_text(func, memory_type)
                if IndirectInjectionGuard.scan(scan_text):
                    logger.warning(
                        "Indirect injection detected in function %s (type=%s), skipped.",
                        func.id,
                        memory_type,
                    )
                    self._injection_scans_24h[today] = (
                        self._injection_scans_24h.get(today, 0) + 1
                    )

            self.store.merge(extracted.graph)

            # Invalidate graph builder cache so next write sees new data
            self._graph_builder.invalidate_cache()

        # 3. Background tasks (index build, wiki compile, etc.)
        # These are submitted to the background worker asynchronously.
        try:
            for func in extracted.functions:
                self._worker.submit(
                    BackgroundTask.BUILD_INDEX,
                    {"func_id": func.id, "source_id": source.id if source else None},
                )
        except Exception:
            pass  # Background tasks are best-effort

        return extracted

    def write_text(
        self,
        text: str,
        source_type: str = "text",
    ) -> ExtractedData:
        """Convenience: write raw text content.

        Parameters
        ----------
        text:
            Raw text to ingest.
        source_type:
            Source type string (``"text"`` | ``"file"`` | ``"url"`` |
            ``"clipboard"``).

        Returns
        -------
        ExtractedData
        """
        source = SourceDocument(
            type=source_type,
            content=text,
            source_type=SourceType.WIKI,
        )
        return self.write(source)

    # ── Extraction helper ────────────────────────────────────────

    def _extract_functions(self, source: SourceDocument) -> list:
        """Rule-based Function extraction from a SourceDocument.

        Splits content into paragraphs, creates one Function per
        paragraph with detected trigger/action fields.
        """
        content = source.content or ""
        if not content.strip():
            return []

        paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
        functions: list = []
        import hashlib

        for i, para in enumerate(paragraphs):
            # Generate stable ID from content hash
            content_hash = hashlib.sha256(para.encode()).hexdigest()[:16]
            func_id = f"func_{content_hash}"

            memory_type = _detect_memory_type(para)

            # Extract trigger/action via simple heuristics
            sentences = [s.strip() for s in para.split("。") if s.strip()]
            if not sentences:
                sentences = [s.strip() for s in para.split(".") if s.strip()]

            from memplex.models import FieldValue

            triggers = []
            actions = []
            for s in sentences[:5]:
                fv = FieldValue(
                    desc=s,
                    sources=[source.type],
                    source_method="rule_based",
                    weight=0.7,
                )
                # Heuristic: first sentence is trigger, rest are action
                if not triggers:
                    triggers.append(fv)
                else:
                    actions.append(fv)

            from memplex.models.memory import Function as Func

            func = Func(
                id=func_id,
                name=para[:50] + ("..." if len(para) > 50 else ""),
                domain=None,
                trigger=triggers,
                action=actions,
                source_type=source.source_type,
                content_hash=hashlib.sha256(para.encode()).hexdigest(),
            )
            functions.append(func)

        return functions

    # ════════════════════════════════════════════════════════════════
    #  Memory operations
    # ════════════════════════════════════════════════════════════════

    def get(self, memory_id: str) -> Optional[Function]:
        """Retrieve a single Function by ID, or ``None``."""
        return self.store.get(memory_id)

    def update_memory(
        self,
        memory_id: str,
        role: str,
        new_value: str,
    ) -> UpdateResult:
        """Update a Function's field value.

        Parameters
        ----------
        memory_id:
            Function ID to update.
        role:
            Which role field to modify (``"trigger"`` | ``"condition"`` |
            ``"action"`` | ``"benefit"``).
        new_value:
            New description text for the FieldValue.

        Returns
        -------
        UpdateResult
        """
        func = self.store.get(memory_id)
        if func is None:
            return UpdateResult(
                memory_id=memory_id,
                role=role,
                new_value=new_value,
                success=False,
                error="Function not found",
            )

        old_value = None
        values = getattr(func, role, None)
        if values is None:
            return UpdateResult(
                memory_id=memory_id,
                role=role,
                new_value=new_value,
                success=False,
                error=f"Unknown role: {role}",
            )

        if values:
            old_value = values[0].desc

        from memplex.models import FieldValue

        values.insert(
            0,
            FieldValue(
                desc=new_value,
                sources=["manual"],
                source_method="manual",
                weight=1.0,
            ),
        )

        # Re-merge to persist
        from memplex.models import SourceDocument as SD

        self.store.add(func, SD(type="manual_update", source_type=SourceType.WIKI))

        return UpdateResult(
            memory_id=memory_id,
            role=role,
            old_value=old_value,
            new_value=new_value,
            version=func.version,
            success=True,
        )

    def delete(self, memory_id: str) -> None:
        """Soft-delete a Function by ID."""
        self.store.delete(memory_id)

    # ════════════════════════════════════════════════════════════════
    #  Feedback
    # ════════════════════════════════════════════════════════════════

    def submit_feedback(
        self,
        memory_id: str,
        field_role: str,
        value_index: int,
        verdict: str,
        reason: Optional[str] = None,
    ) -> None:
        """Submit user feedback on a memory field value.

        Parameters
        ----------
        memory_id:
            Function ID.
        field_role:
            Role of the field (``"trigger"`` | ``"action"`` | ...).
        value_index:
            Index within the FieldValue list.
        verdict:
            ``"correct"`` | ``"wrong"`` | ``"alternative"``.
        reason:
            Optional free-text explanation.
        """
        fb = MemoryFeedback(
            memory_id=memory_id,
            field_role=field_role,
            value_index=value_index,
            verdict=FeedbackVerdict(verdict),
            reason=reason,
            source="user",
        )
        self._feedback_store.record(fb)

    def apply_resolution(
        self,
        memory_id: str,
        field_role: str,
        action: str,
        new_value: Optional[str] = None,
    ) -> dict:
        """Apply a resolution to a pending feedback review.

        Parameters
        ----------
        memory_id:
            Function ID.
        field_role:
            Field role under review.
        action:
            ``"accept"`` | ``"reject"`` | ``"merge"``.
        new_value:
            Replacement value when action is ``"merge"``.

        Returns
        -------
        dict
            ``{"status": "resolved", "action": action}``
        """
        self._feedback_store.resolve(memory_id, field_role, action)

        if action == "merge" and new_value:
            self.update_memory(memory_id, field_role, new_value)

        return {"status": "resolved", "action": action}

    def get_pending_reviews(
        self,
        owner: Optional[str] = None,
        limit: int = 100,
    ) -> list:
        """Return pending feedback reviews, optionally filtered by owner.

        Returns
        -------
        list[PendingReview]
        """
        pending = self._feedback_store.get_pending()
        if owner is not None:
            pending = [p for p in pending if getattr(p, "source", None) == owner]
        return pending[:limit]

    # ════════════════════════════════════════════════════════════════
    #  Management
    # ════════════════════════════════════════════════════════════════

    def health(self) -> dict:
        """Return health / readiness status.

        Returns
        -------
        dict
            Keys: ``status``, ``backend``, ``functions_total``, ``edges_total``,
            ``queue_depth``, ``last_compaction``, ``injection_scans_detected_24h``,
            ``dead_letters_pending``, ``version``.
        """
        # Determine overall status
        try:
            self.store.list_functions(limit=1)
            storage_status = "ok"
        except Exception as exc:
            storage_status = f"error: {exc}"

        worker_running = self._worker._running
        all_ok = (
            storage_status == "ok"
            and self._embedding_service is not None
            and worker_running
        )
        has_warnings = storage_status != "ok" or not worker_running
        status = "healthy" if all_ok else ("warning" if has_warnings else "degraded")

        # Count functions
        try:
            funcs = self.store.list_functions(limit=100000)
            functions_total = len(funcs)
        except Exception:
            functions_total = 0

        # Count edges
        try:
            graph = self.store.get_graph()
            edges_total = len(graph.edges)
        except Exception:
            edges_total = 0

        # Queue depth
        queue_depth = self._worker.queue_depth

        # Last compaction
        last_compaction = self._worker.last_compaction
        if last_compaction is not None:
            last_compaction = last_compaction.isoformat()

        # Injection scans detected in last 24h
        today = datetime.now().strftime("%Y-%m-%d")
        injection_scans_detected_24h = self._injection_scans_24h.get(today, 0)

        # Dead letters (failed tasks)
        dead_letters_pending = self._worker.dead_letters_pending()

        # Version
        version = _package_version()

        return {
            "status": status,
            "backend": self._config.storage.backend,
            "functions_total": functions_total,
            "edges_total": edges_total,
            "queue_depth": queue_depth,
            "last_compaction": last_compaction,
            "injection_scans_detected_24h": injection_scans_detected_24h,
            "dead_letters_pending": dead_letters_pending,
            "version": version,
        }

    def stats(self) -> dict:
        """Return storage and usage statistics."""
        try:
            funcs = self.store.list_functions(limit=100000)
            total = len(funcs)
        except Exception:
            total = 0

        graph = self.store.get_graph()
        total_edges = len(graph.edges)

        return {
            "total_functions": total,
            "total_edges": total_edges,
            "storage_backend": self._config.storage.backend,
            "embedding_model": self._config.embedding.model,
        }

    def compact(self, scope: str = "project") -> CompactionResult:
        """Run the compaction pipeline synchronously.

        Parameters
        ----------
        scope:
            ``"session"`` | ``"project"`` | ``"global"``.
        """
        compaction_scope = CompactionScope(scope)
        # CompactionPipeline.run is async; run it in a thread
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            result = pool.submit(
                asyncio.run, self._compaction.run(compaction_scope)
            ).result()
        # Record last compaction timestamp
        self._worker._last_compaction = datetime.now()
        return result

    def start(self) -> None:
        """Start the background worker thread."""
        self._worker.start()

    def stop(self) -> None:
        """Stop the background worker thread."""
        self._worker.stop()

    # ── Memory type detection ─────────────────────────────────────

    @staticmethod
    def _detect_memory_type(text: str) -> str:
        """Classify content into a primary memory type.

        Returns one of ``"function"`` | ``"fact"`` | ``"preference"`` |
        ``"observation"``.
        """
        return _detect_memory_type(text)
