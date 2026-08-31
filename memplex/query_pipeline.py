"""QueryPipeline -- the read-side query pipeline extracted from MemplexService.

This module holds the six-stage ``query`` machinery (intent detection is
injected, then parallel multi-path retrieval, merge/dedupe, authorization
and metadata filters, rerank, injection filter, access-count update, token
budget). ``MemplexService.query`` resolves the request-scoped authorization
context and store, captures its *current* collaborators into a fresh
``QueryPipeline`` (so tests that monkeypatch service instance attributes
stay live), and delegates.

The pipeline never imports host adapters; it depends on the authorization
gate, retrieval collaborators, and the injection-risk registry only.
"""

from __future__ import annotations

import concurrent.futures
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional

from memplex.llm.injection_guard import drop_injection_suspected
from memplex.models import QueryResult, QueryScope, SearchResult
from memplex.query_explainer import build_query_explanation
from memplex.retrieval.multi_path import MultiPathRetriever
from memplex.retrieval.reranker import Reranker

if TYPE_CHECKING:
    from memplex.auth import AuthorizationContext
    from memplex.authorization import AuthorizationGate
    from memplex.config import MemplexConfig
    from memplex.llm import LLMEnhancer
    from memplex.llm.injection_guard import InjectionRiskRegistry
    from memplex.retrieval.embedding import EmbeddingService, Vector
    from memplex.retrieval.reranker import CrossEncoderReranker

logger = logging.getLogger(__name__)


class QueryPipeline:
    """One read-side query execution with its collaborators pre-bound.

    Instances are single-use: :meth:`MemplexService.query` builds one per
    call so the pipeline always sees the service's current attribute values
    (reranker/retriever/LLM monkeypatches included). ``store`` is the
    request-scoped storage facade; ``base_store`` is the service-owned root
    store used to decide whether the shared retriever/reranker apply or a
    scoped one must be constructed (a scoped backend is never handed the
    tenant-global wiki searcher).
    """

    def __init__(
        self,
        *,
        config: MemplexConfig,
        store: Any,
        base_store: Any,
        retriever: MultiPathRetriever,
        embedding_service: Optional[EmbeddingService],
        llm: Optional[LLMEnhancer],
        reranker: Optional[Reranker],
        cross_reranker: Optional[CrossEncoderReranker],
        injection_risks: InjectionRiskRegistry,
        auth: AuthorizationGate,
        detect_scope: Callable[[str], QueryScope],
        compute_hyde_vector: Callable[[str], Optional[Vector]],
    ) -> None:
        self._config = config
        self._store = store
        self._base_store = base_store
        self._retriever = retriever
        self._embedding_service = embedding_service
        self._llm = llm
        self._reranker = reranker
        self._cross_reranker = cross_reranker
        self._injection_risks = injection_risks
        self._auth = auth
        self._detect_scope = detect_scope
        self._compute_hyde_vector = compute_hyde_vector

    def run(
        self,
        text: str,
        top_k: int = 10,
        owner: Optional[str] = None,
        max_tokens: int = 4000,
        namespace_filter: Optional[
            Dict[str, Optional[str]] | List[Dict[str, Optional[str]]]
        ] = None,
        explain: bool = False,
        *,
        context: AuthorizationContext,
    ) -> QueryResult:
        """Execute the six-stage query pipeline and return a QueryResult.

        Pipeline:
        1. Intent detection (LLM first, keyword fallback).
        2. Parallel multi-path retrieval (ThreadPoolExecutor, 3 workers).
        3. Merge + deduplicate by ``func_id`` (keep highest score).
        4. Rerank (6-dim bi-encoder + optional cross-encoder).
        5. Update ``access_count`` (persisted).
        6. Token budget truncation (greedy by ``relevance_score``).
        """
        store = self._store
        # A compiled wiki index is not tenant-addressable.  On a scoped
        # backend, use its SQL/FTS path instead of producing global wiki
        # candidates and attempting to remove them afterwards.
        retriever = (
            self._retriever
            if store is self._base_store
            else MultiPathRetriever(
                store,
                embedding_service=self._embedding_service,
                wiki_searcher=None,
            )
        )
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

        # Pre-compute query_vector (multi-path reuse). Query-side embeds
        # use embed_query (transform-only) so TF-IDF corpus statistics do
        # not drift with query history.
        query_vector: Optional[Vector] = None
        if self._embedding_service is not None:
            if self._llm is not None and self._config.embedding.hyde_enabled:
                query_vector = self._compute_hyde_vector(text)
            query_vector = query_vector or self._embedding_service.embed_query(text)
        if trace is not None:
            trace["embedding"]["query_vector_available"] = query_vector is not None

        all_results = self._parallel_scope_search(
            retriever, text, scope, top_k, query_vector, trace
        )
        candidate_budget = max(0, int(top_k))
        # Merge results
        results = MultiPathRetriever.merge_multi_path(all_results)
        if trace is not None:
            trace["stages"].append(
                {
                    "stage": "merge_deduplicate",
                    "input_paths": len(all_results),
                    "candidate_budget": candidate_budget,
                    "candidates": len(results),
                }
            )
        before_authorization = len(results)
        results = self._auth.filter_authorized_results(results, context)
        if trace is not None:
            trace["stages"].append(
                {
                    "stage": "authorization_filter",
                    "before": before_authorization,
                    "after": len(results),
                    "boundary": "Authenticated tenant and visibility ACL filter.",
                }
            )
        if namespace_filter:
            before_namespace = len(results)
            results = retriever.filter_by_namespace(results, namespace_filter)
            if trace is not None:
                trace["stages"].append(
                    {
                        "stage": "namespace_filter",
                        "before": before_namespace,
                        "after": len(results),
                        "boundary": "exact-match metadata filter; not an ACL engine",
                    }
                )
        if owner is not None:
            before_owner = len(results)
            results = self._filter_by_owner(results, owner, context=context)
            if trace is not None:
                trace["stages"].append(
                    {
                        "stage": "owner_filter",
                        "owner": owner,
                        "before": before_owner,
                        "after": len(results),
                        "boundary": "exact-match owner filter; not an ACL engine",
                    }
                )

        # Stage 1: 6-dim bi-encoder rerank
        if self._reranker is not None:
            before_rerank = len(results)
            reranker = (
                self._reranker
                if store is self._base_store
                else Reranker(
                    embedding_service=self._embedding_service,
                    weights=self._config.reranker.weights,
                    storage=store,
                    recency_halflife_days=self._config.reranker.recency_halflife_days,
                )
            )
            results = reranker.rerank(text, results, top_k * 2, query_vector)
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
            results = drop_injection_suspected(
                results,
                self._auth.typed_lookup_for(context),
                risk_registry=self._injection_risks,
            )
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
                store.increment_access_batch([r.func_id for r in results])
            except Exception as exc:
                logger.debug(
                    "increment_access_batch failed (frequency signal lost for %d results): %s",
                    len(results),
                    exc,
                )

        latency = int((datetime.now() - start).total_seconds() * 1000)

        results, used, truncated = self._apply_token_budget(results, max_tokens, trace)
        if trace is not None:
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
            max_tokens=max_tokens,
            truncated=truncated,
            explanation=build_query_explanation(trace),
        )

    def _parallel_scope_search(
        self,
        retriever: MultiPathRetriever,
        text: str,
        scope: QueryScope,
        top_k: int,
        query_vector: Optional[Vector],
        trace: Optional[Dict[str, Any]],
    ) -> List[List[SearchResult]]:
        """Fan out the scoped retrieval paths under one global budget."""
        # Parallel multi-path retrieval. ``top_k`` is one global candidate
        # budget, not a per-path allowance: otherwise QueryScope.ALL silently
        # triples model-controlled work. Split the budget as evenly as
        # possible; when the budget is smaller than the number of paths, the
        # zero-budget paths are not scheduled.
        searches: list[tuple[str, Any, tuple[Any, ...]]] = []
        if scope in (QueryScope.IMMEDIATE, QueryScope.ALL):
            searches.append(("rag", retriever.rag_search, (query_vector,)))
        if scope in (QueryScope.SYNTHESIS, QueryScope.ALL):
            searches.append(("wiki", retriever.wiki_search, ()))
        if scope in (QueryScope.RELATION, QueryScope.ALL):
            searches.append(("graph", retriever.graph_search, (query_vector,)))

        candidate_budget = max(0, int(top_k))
        per_path, remainder = divmod(candidate_budget, len(searches))
        futures: Dict[concurrent.futures.Future, tuple[str, int]] = {}
        with ThreadPoolExecutor(max_workers=len(searches)) as pool:
            for index, (path, search, extra_args) in enumerate(searches):
                path_budget = per_path + (1 if index < remainder else 0)
                if path_budget == 0:
                    continue
                futures[pool.submit(search, text, path_budget, *extra_args)] = (
                    path,
                    path_budget,
                )

            all_results: List[List[SearchResult]] = []
            for future in as_completed(futures):
                path, path_budget = futures[future]
                try:
                    path_results = future.result()
                    all_results.append(path_results)
                    if trace is not None:
                        trace["stages"].append(
                            {
                                "stage": f"{path}_search",
                                "status": "ok",
                                "candidate_budget": path_budget,
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
                                "candidate_budget": path_budget,
                                "candidates": 0,
                            }
                        )

        return all_results

    @staticmethod
    def _apply_token_budget(
        results: List[SearchResult], max_tokens: int, trace: Optional[Dict[str, Any]]
    ) -> tuple[List[SearchResult], int, bool]:
        """Greedy token-budget truncation by relevance order."""
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
        return results, used, truncated

    def _filter_by_owner(
        self,
        results: List[SearchResult],
        owner: str,
        *,
        context: AuthorizationContext,
    ) -> List[SearchResult]:
        """Keep only results whose stored node is owned by *owner*.

        The node's ``owner`` is resolved through the typed-node facade
        (Function first, then the optional Fact/Preference APIs). Nodes
        without an owner never match. Lookup failures keep the result --
        a store hiccup must not silently drop legitimate memory (same
        availability posture as the injection filter).
        """
        kept: List[SearchResult] = []
        for r in results:
            try:
                lookup = self._auth.typed_lookup_for(context)
                node = lookup.get(r.func_id)
            except Exception as exc:
                logger.debug("owner filter: lookup failed for %s, keeping result: %s", r.func_id, exc)
                node = None
            if node is None:
                kept.append(r)
                continue
            if getattr(node, "owner", None) == owner:
                kept.append(r)
        return kept
