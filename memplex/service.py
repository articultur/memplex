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
from typing import Any, Dict, Iterable, List, Optional

from memplex.compaction import CompactionPipeline
from memplex.config import MemplexConfig, load_config
from memplex.core import CoreEngine
from memplex.intent import detect_memory_type as _detect_memory_type
from memplex.intent import detect_scope_by_keywords
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
from memplex.query_explainer import build_query_explanation
from memplex.retrieval.embedding import EmbeddingService, Vector
from memplex.retrieval.multi_path import MultiPathRetriever
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
            project = tomllib.loads(pyproject.read_text(encoding="utf-8")).get("project", {})
            if project.get("name") == "memplex" and project.get("version"):
                return str(project["version"])
    except Exception as exc:
        logger.debug("pyproject version resolution failed, falling back to importlib: %s", exc)

    from importlib.metadata import version as pkg_version

    return pkg_version("memplex")


# ``_detect_memory_type`` is imported from ``memplex.intent`` (see import
# block above) and re-exported here under its original underscored name so
# that ``from memplex.service import _detect_memory_type`` keeps working.
# ``MemplexService._detect_memory_type`` (the bound staticmethod at the
# bottom of this file) forwards to the same implementation.


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

        # ── Multi-path retrieval ───────────────────────────────
        self._retriever = MultiPathRetriever(self.store)

        # ── Injection scan tracking ─────────────────────────────
        self._injection_scans_24h: Dict[str, int] = {}  # keyed by date string "YYYY-MM-DD"

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
            logger.info("LLM enhancer not available (%s); using rule-based fallback", exc)
            self._llm = None

    # ── Injection scan helper ──────────────────────────────────────
    # ``_extract_scan_text`` lives on ``IndirectInjectionGuard`` (llm/
    # injection_guard.py) and is shared by both the write path (here,
    # in ``write()``) and the read path (``filter_and_wrap``). Keeping a
    # single copy prevents the two paths from drifting when a new memory
    # type is added.

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
        explain: bool = False,
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
        explain:
            Include a product-facing retrieval trace that explains the stages,
            filters, budgets, and final injected candidates.
        """
        scope = self._detect_scope(text)
        start = datetime.now()
        trace: Optional[Dict[str, Any]] = None
        if explain:
            trace = {
                "query": text,
                "top_k": top_k,
                "max_tokens": max_tokens,
                "scope": scope.value,
                "namespace_filter": namespace_filter or {},
                "embedding": {
                    "enabled": self._embedding_service is not None,
                    "model": self._config.embedding.model,
                    "hyde_enabled": bool(self._config.embedding.hyde_enabled),
                },
                "reranker": {
                    "enabled": self._reranker is not None,
                    "cross_encoder_enabled": self._cross_reranker is not None
                    and bool(getattr(self._cross_reranker, "enabled", False)),
                },
                "stages": [],
            }

        # Pre-compute query_vector (multi-path reuse)
        query_vector: Optional[Vector] = None
        if self._embedding_service is not None:
            if self._llm is not None and self._config.embedding.hyde_enabled:
                query_vector = self._compute_hyde_vector(text)
            query_vector = query_vector or self._embedding_service.embed(text)
        if trace is not None:
            trace["embedding"]["query_vector_available"] = query_vector is not None

        # Parallel multi-path retrieval
        futures: Dict[concurrent.futures.Future, str] = {}
        with ThreadPoolExecutor(max_workers=3) as pool:
            if scope in (QueryScope.IMMEDIATE, QueryScope.ALL):
                futures[pool.submit(self._retriever.rag_search, text, top_k, query_vector)] = "rag"
            if scope in (QueryScope.SYNTHESIS, QueryScope.ALL):
                futures[pool.submit(self._retriever.wiki_search, text, top_k)] = "wiki"
            if scope in (QueryScope.RELATION, QueryScope.ALL):
                futures[pool.submit(self._retriever.graph_search, text, top_k, query_vector)] = (
                    "graph"
                )

            all_results: List[List[SearchResult]] = []
            for future in as_completed(futures):
                path = futures[future]
                try:
                    path_results = future.result()
                    all_results.append(path_results)
                    if trace is not None:
                        trace["stages"].append(
                            {
                                "stage": f"{path}_search",
                                "status": "ok",
                                "candidates": len(path_results),
                            }
                        )
                except Exception as exc:
                    logger.warning("Search path %s failed: %s", path, exc)
                    if trace is not None:
                        trace["stages"].append(
                            {
                                "stage": f"{path}_search",
                                "status": "failed",
                                "error": str(exc),
                                "candidates": 0,
                            }
                        )

        # Merge results
        results = MultiPathRetriever.merge_multi_path(all_results)
        if trace is not None:
            trace["stages"].append(
                {
                    "stage": "merge_deduplicate",
                    "input_paths": len(all_results),
                    "candidates": len(results),
                }
            )
        if namespace_filter:
            before_namespace = len(results)
            results = self._retriever.filter_by_namespace(results, namespace_filter)
            if trace is not None:
                trace["stages"].append(
                    {
                        "stage": "namespace_filter",
                        "before": before_namespace,
                        "after": len(results),
                        "boundary": "exact-match metadata filter; not an ACL engine",
                    }
                )

        # Stage 1: 5-dim bi-encoder rerank
        if self._reranker is not None:
            before_rerank = len(results)
            results = self._reranker.rerank(text, results, top_k * 2, query_vector)
            if trace is not None:
                trace["stages"].append(
                    {
                        "stage": "rerank",
                        "before": before_rerank,
                        "after": len(results),
                        "weights": dict(self._config.reranker.weights),
                    }
                )

        # Stage 2: Cross-encoder precision rerank (optional)
        if self._cross_reranker is not None:
            before_cross = len(results)
            results = self._cross_reranker.rerank(text, results)
            if trace is not None:
                trace["stages"].append(
                    {
                        "stage": "cross_encoder_rerank",
                        "before": before_cross,
                        "after": len(results),
                        "model": self._config.reranker.cross_encoder_model,
                    }
                )

        # Injection-suspected results are dropped before top_k so they
        # neither occupy the token budget nor reach any LLM-facing caller
        # (MCP memory_search, HTTP /memories, CLI recall, AgentMemoryRuntime).
        # The flag is stamped at write time in write(); this is the read-side
        # enforcement that previously only AgentMemoryRuntime applied.
        if results:
            before_injection = len(results)
            results = self._drop_injection_suspected(results)
            if trace is not None and len(results) != before_injection:
                trace["stages"].append(
                    {
                        "stage": "injection_filter",
                        "before": before_injection,
                        "after": len(results),
                        "boundary": "Drops memories flagged memplex_injection_suspected=true at write time.",
                    }
                )

        results = results[:top_k]
        if trace is not None:
            trace["stages"].append(
                {
                    "stage": "top_k",
                    "limit": top_k,
                    "after": len(results),
                }
            )

        # Update access_count (must persist for Reranker frequency dimension).
        # Batched: a single persistence pass for all results instead of one
        # full-store rewrite per result. Best-effort -- a store hiccup here
        # must not fail the query, but we log at debug so a lost frequency
        # signal is diagnosable rather than silently swallowed.
        if results:
            try:
                self.store.increment_access_batch([r.func_id for r in results])
            except Exception as exc:
                logger.debug(
                    "increment_access_batch failed (frequency signal lost for %d results): %s",
                    len(results),
                    exc,
                )

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
        if trace is not None:
            trace["stages"].append(
                {
                    "stage": "token_budget",
                    "max_tokens": max_tokens,
                    "tokens_used": used,
                    "truncated": truncated,
                    "after": len(results),
                }
            )
            trace["final_results"] = [
                {
                    "id": r.func_id,
                    "name": r.name,
                    "score": r.relevance_score,
                    "domain": r.domain,
                    "token_estimate": r.token_estimate,
                    "source_type": getattr(r.source_type, "value", str(r.source_type)),
                }
                for r in results
            ]

        return QueryResult(
            results=results,
            scope=scope,
            latency_ms=latency,
            tokens_used=used,
            truncated=truncated,
            explanation=build_query_explanation(trace),
        )

    async def query_async(
        self,
        text: str,
        top_k: int = 10,
        owner: Optional[str] = None,
        max_tokens: int = 4000,
        namespace_filter: Optional[Dict[str, str]] = None,
        explain: bool = False,
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
                explain=explain,
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
                        enhanced = _pool.submit(asyncio.run, self._llm.enhance_query(text)).result(
                            timeout=5.0
                        )
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
            except Exception as exc:
                # LLM intent detection failed; fall through to keyword
                # scoring but keep a debug trace for diagnosis.
                logger.debug("LLM intent detection failed, using keyword fallback: %s", exc)

        # Keyword fallback (delegated to memplex.intent so the keyword
        # table and negation handling live in one testable place).
        return detect_scope_by_keywords(text)

    def annotate_memories(
        self,
        memory_ids: Iterable[str],
        *,
        attributes: Optional[Dict[str, Any]] = None,
        needs_review: Optional[bool] = None,
    ) -> List[Function]:
        """Annotate stored memories through the service boundary.

        This keeps product workflows from mutating store internals directly.
        Backends that return live objects can persist via their native save
        hook; other backends fall back to re-adding the updated function through
        the public store contract.

        Security contract: ``attributes`` is operator/system metadata
        (namespace tags, corpus markers, review flags) -- NOT LLM-facing
        content. It bypasses the injection scanner by design because it
        never reaches an LLM context window. Never pass untrusted
        user/document text as an attribute value; route such text through
        :meth:`write` / :meth:`write_text` instead, where it is scanned.
        """

        updated: List[Function] = []
        for memory_id in memory_ids:
            func = self.store.get(memory_id)
            if func is None:
                continue
            if attributes:
                func.attributes.update(attributes)
            if needs_review is not None:
                func.needs_review = needs_review
            updated.append(func)

        if not updated:
            return []

        save = getattr(self.store, "_save", None)
        if callable(save):
            save()
        else:
            source = SourceDocument(type="metadata_update", source_type=SourceType.WIKI)
            for func in updated:
                self.store.add(func, source)

        return updated

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
        # 0. Strip <private>...</private> blocks before extraction so
        #    operator-marked secrets never reach storage. Applies to every
        #    write caller (CLI/HTTP/MCP/corpus/agent_runtime), not only the
        #    Claude Code hook runner which already stripped these.
        from memplex.privacy import strip_private_tags

        if getattr(source, "content", None):
            source.content = strip_private_tags(source.content)

        # 1. CoreEngine: full extraction pipeline
        extracted = self._engine.extract(source)

        # 2. Flag indirect-injection-suspected functions at write time.
        #    Memories are RETAINED (co-located legitimate content must not be
        #    silently lost) but stamped ``memplex_injection_suspected=true``;
        #    the LLM-facing read path (IndirectInjectionGuard.filter_and_wrap)
        #    re-scans and omits them from injected context. Previously this
        #    only logged "skipped" while neither skipping nor flagging.
        if extracted.functions:
            today = datetime.now().strftime("%Y-%m-%d")
            for func in extracted.functions:
                memory_type = getattr(func, "memory_type", "function")
                scan_text = IndirectInjectionGuard._extract_scan_text(func, memory_type)
                if IndirectInjectionGuard.scan(scan_text):
                    logger.warning(
                        "Indirect injection suspected in function %s (type=%s); "
                        "retained but flagged, will be omitted at recall.",
                        func.id,
                        memory_type,
                    )
                    attrs = getattr(func, "attributes", None)
                    if isinstance(attrs, dict):
                        attrs["memplex_injection_suspected"] = "true"
                    self._injection_scans_24h[today] = self._injection_scans_24h.get(today, 0) + 1

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
        except Exception as exc:
            # Background tasks are best-effort, but a submission failure
            # should not vanish silently -- debug-log so the dropped index
            # build is traceable.
            logger.debug("background task submission failed: %s", exc)

        # 4. Threshold-triggered background compaction.
        # Previously compaction was manual-only (or Claude Code's Stop hook).
        # Now when the corpus crosses warn_threshold, schedule a compaction
        # in the background worker so the store does not grow unbounded on
        # the non-Claude paths (CLI/HTTP/MCP/Codex). Hard limit forces it
        # even if a previous run is still pending.
        self._maybe_schedule_compaction()

        return extracted

    def _maybe_schedule_compaction(self) -> None:
        """Submit a background compaction when the corpus crosses thresholds.

        Reads ``compaction.warn_threshold`` (soft trigger) and
        ``compaction.hard_limit`` (force trigger) from config. Best-effort:
        a full worker queue or a counting failure never blocks the write.
        """
        try:
            total = len(self.store.list_functions(limit=100000))
            warn = self._config.compaction.warn_threshold
            hard = self._config.compaction.hard_limit
            if total >= hard or (total >= warn and self._worker.queue_depth == 0):
                self._worker.submit(
                    BackgroundTask.COMPACTION,
                    {"scope": "project", "triggered_by": "threshold", "total": total},
                )
                logger.debug(
                    "scheduled background compaction (total=%d, warn=%d, hard=%d)",
                    total,
                    warn,
                    hard,
                )
        except Exception as exc:
            logger.debug("compaction scheduling check failed: %s", exc)

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

        # Injection scan on the manually-supplied value, mirroring the
        # write() path. update_memory accepts caller text that becomes LLM
        # context on recall, so it must not bypass the injection defence.
        # Suspected payloads flag the Function (read path drops it) rather
        # than rejecting the update -- legitimate co-located edits must
        # not be silently lost.
        if IndirectInjectionGuard.scan(new_value):
            logger.warning(
                "Indirect injection suspected in update_memory(%s, %s); "
                "retained but flagged, will be omitted at recall.",
                memory_id,
                role,
            )
            attrs = getattr(func, "attributes", None)
            if isinstance(attrs, dict):
                attrs["memplex_injection_suspected"] = "true"

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
        all_ok = storage_status == "ok" and self._embedding_service is not None and worker_running
        has_warnings = storage_status != "ok" or not worker_running
        status = "healthy" if all_ok else ("warning" if has_warnings else "degraded")

        # Count functions
        try:
            funcs = self.store.list_functions(limit=100000)
            functions_total = len(funcs)
        except Exception as exc:
            logger.debug("health: list_functions failed, reporting 0: %s", exc)
            functions_total = 0

        # Count edges
        try:
            graph = self.store.get_graph()
            edges_total = len(graph.edges)
        except Exception as exc:
            logger.debug("health: get_graph failed, reporting 0 edges: %s", exc)
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
        except Exception as exc:
            logger.debug("stats: list_functions failed, reporting 0: %s", exc)
            total = 0

        graph = self.store.get_graph()
        total_edges = len(graph.edges)

        return {
            "total_functions": total,
            "total_edges": total_edges,
            "storage_backend": self._config.storage.backend,
            "embedding_model": self._config.embedding.model,
        }

    def policy(self, *, agent: str = "codex") -> dict:
        """Return the recall/capture policy this service will use by default.

        Wraps :func:`memplex.product.policy_show` so adapters do not need
        to reach into ``self._config`` (a private attribute) to render the
        policy surface. The returned dict matches the product schema.
        """
        from memplex.product import policy_show

        return policy_show(self._config, agent=agent)

    def storage_namespace(self) -> str:
        """Return the storage namespace tag for this service instance.

        Used by adapters to stamp/recall memories by storage boundary
        without reading ``store._path`` (a storage-internal attribute).
        Falls back to ``"service:{id}"`` when the store exposes no path.
        """
        store_path = getattr(self.store, "_path", None)
        if store_path is None:
            return f"service:{id(self)}"
        return str(store_path)

    def filter_and_wrap_for_context(
        self,
        results: List[SearchResult],
        *,
        max_tokens: Optional[int] = None,
    ) -> Optional[str]:
        """Filter injection-suspected results and wrap the rest for LLM context.

        Wraps :meth:`IndirectInjectionGuard.filter_and_wrap` so adapters
        (notably ``agent_runtime``) do not import the guard directly. Keeps
        the read-path injection defence behind the service boundary.
        """
        return IndirectInjectionGuard.filter_and_wrap(results, self.store)

    def _drop_injection_suspected(self, results: List[SearchResult]) -> List[SearchResult]:
        """Drop results whose stored Function is flagged injection-suspected.

        Read-side enforcement paired with the write-time flag in ``write()``.
        A result is dropped when its Function's ``attributes`` map contains
        ``memplex_injection_suspected == "true"``. Failures to look up the
        Function (e.g. race with delete) keep the result rather than
        silently dropping legitimate memory.
        """
        kept: List[SearchResult] = []
        for r in results:
            try:
                func = self.store.get(r.func_id)
            except Exception as exc:
                logger.debug(
                    "injection filter: store.get failed for %s, keeping result: %s",
                    r.func_id,
                    exc,
                )
                func = None
            if func is not None:
                attrs = getattr(func, "attributes", {}) or {}
                if attrs.get("memplex_injection_suspected") == "true":
                    continue
            kept.append(r)
        return kept

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
            result = pool.submit(asyncio.run, self._compaction.run(compaction_scope)).result()
        # Record last compaction timestamp
        self._worker._last_compaction = datetime.now()
        return result

    def start(self) -> None:
        """Start the background worker thread (+ auto-pull if configured)."""
        self._worker.start()
        # If the store is sync-enabled and a positive auto-pull interval is
        # configured, start the periodic pull thread so this node stays
        # current with the central server without manual 'sync pull'.
        from memplex.sync import SyncableStore

        if isinstance(self.store, SyncableStore):
            self.store.start_auto_pull()

    def stop(self) -> None:
        """Stop the background worker thread (+ auto-pull if running)."""
        from memplex.sync import SyncableStore

        if isinstance(self.store, SyncableStore):
            self.store.stop_auto_pull()
        self._worker.stop()

    # ── Memory type detection ─────────────────────────────────────
    # Exposed as a bound staticmethod for backward-compatible callers
    # (e.g. ``MemplexService._detect_memory_type(text)``); the real
    # implementation lives at module scope above.
    _detect_memory_type = staticmethod(_detect_memory_type)
