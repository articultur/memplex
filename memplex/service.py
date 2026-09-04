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
import os
from collections.abc import Callable, Iterable
from datetime import UTC, datetime, timezone
from pathlib import Path
from threading import Condition, RLock
from typing import TYPE_CHECKING, Any, Literal, Optional, cast

from memplex.auth import (
    AuthorizationContext,
    MemoryNotFoundError,
    PrincipalRegistry,
    bind_node_identity,
    resolve_environment_authorization,
)
from memplex.authorization import AuthorizationGate, _TypedNodeLookup
from memplex.compaction import CompactionPipeline
from memplex.config import MemplexConfig, load_config, validate_deployment_contract
from memplex.core import CoreEngine
from memplex.intent import detect_memory_type as _detect_memory_type
from memplex.intent import detect_scope_by_keywords
from memplex.llm import LLMEnhancer
from memplex.llm.injection_guard import (
    IndirectInjectionGuard,
    InjectionRiskRegistry,
    InjectionScanCounter,
)
from memplex.llm.provider import create_provider
from memplex.models import (
    CompactionResult,
    CompactionScope,
    ExtractedData,
    Fact,
    FeedbackVerdict,
    Function,
    MemoryFeedback,
    Observation,
    QueryResult,
    QueryScope,
    SearchResult,
    SourceDocument,
    SourceType,
    UpdateResult,
)
from memplex.processing.graph_builder import GraphBuilder
from memplex.query_pipeline import QueryPipeline
from memplex.retrieval.embedding import EmbeddingService, Vector
from memplex.retrieval.multi_path import MultiPathRetriever
from memplex.retrieval.reranker import CrossEncoderReranker, Reranker
from memplex.storage import MemoryStore, create_store
from memplex.storage.feedback import FeedbackStore, create_feedback_store
from memplex.storage.migrations.runner import VectorCapabilityRequest
from memplex.storage.pool import (
    PostgresStorageResources,
    PostgresSyncStorageResources,
)
from memplex.sync_repository import SyncCapturePolicy
from memplex.worker import BackgroundTask, BackgroundWorker

if TYPE_CHECKING:
    from memplex.sync_dispatcher import PullResult
    from memplex.sync_protocol import SyncDrainResult

logger = logging.getLogger(__name__)

# Optional callback registered by the HTTP adapter at startup so the
# service health surface can report the SSE subscriber count without a
# reverse domain→adapter import. Returns int; never raises.
_sse_subscriber_count_provider: Callable[[], int] | None = None


def register_sse_subscriber_count_provider(provider: Callable[[], int]) -> None:
    """Register the adapter-owned SSE subscriber-count callback.

    Host adapters (the HTTP API) call this once at startup so the service
    health surface (:meth:`MemplexService._sync_health`) can report the SSE
    subscriber count without a reverse domain→adapter import. *provider*
    must return an ``int``; the health surface fails closed to ``0`` if it
    raises. Replaces any previously registered provider.
    """
    global _sse_subscriber_count_provider
    _sse_subscriber_count_provider = provider


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
    except Exception as exc:  # noqa: BLE001 - logged degradation path
        logger.debug("pyproject version resolution failed, falling back to importlib: %s", exc)

    from importlib.metadata import version as pkg_version

    return pkg_version("memplex")


# ``_detect_memory_type`` is imported from ``memplex.intent`` (see import
# block above) and re-exported here under its original underscored name so
# that ``from memplex.service import _detect_memory_type`` keeps working.
# ``MemplexService._detect_memory_type`` (the bound staticmethod at the
# bottom of this file) forwards to the same implementation.
# ``_TypedNodeLookup`` and the authorization/ACL visibility logic are
# imported from ``memplex.authorization`` above and re-exported here so
# existing ``from memplex.service import _TypedNodeLookup`` imports keep
# working.


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

    def __init__(self, config: MemplexConfig | None = None) -> None:
        from memplex.operations import RuntimeLifecycle

        self._config = config or load_config()
        cfg = self._config
        # Authorization gate is constructed early because store construction
        # (below) already needs the production-profile check. Stores are
        # resolved lazily via providers so the gate always reads the service's
        # current ``store`` / ``_feedback_store`` (never a stale snapshot).
        from memplex.sleep_time import SleepTimeAgent
        from memplex.working_memory import WorkingMemory

        self._sleep_time = SleepTimeAgent(
            self,
            interval_seconds=cfg.sleep_time.interval_seconds,
            idle_grace_seconds=cfg.sleep_time.idle_grace_seconds,
            precompute_top_k=cfg.sleep_time.precompute_top_k,
        )
        self._working_memory = (
            WorkingMemory(
                max_entries=cfg.working_memory.max_entries,
                default_ttl_seconds=cfg.working_memory.default_ttl_seconds,
            )
            if cfg.working_memory.enabled
            else None
        )

        self._auth = AuthorizationGate(
            cfg, lambda: self.store, lambda: self._feedback_store
        )
        validate_deployment_contract(cfg)
        self._lifecycle_condition = Condition(RLock())
        self._postgres_resource_close_state = "open"
        self._postgres_resource_close_error: BaseException | None = None
        self._service_stop_state = "open"
        self._service_stop_error: BaseException | None = None
        self._service_stop_result: dict[str, object] | None = None
        self._sync_dispatcher: Any | None = None
        self._runtime_lifecycle = RuntimeLifecycle()

        # ── Resolve backend ("lite" and "postgres" are implemented;
        #    "standard"/"enterprise" map to lite inside create_store) ──
        _implemented_backends = {"lite", "postgres"}
        backend = cfg.storage.backend
        if backend not in _implemented_backends:
            logger.warning(
                "Storage backend %r not available, falling back to 'lite'",
                backend,
            )
            backend = "lite"

        # ── Storage ─────────────────────────────────────────────
        self._postgres_resources: (
            PostgresStorageResources | PostgresSyncStorageResources | None
        ) = None
        storage_kwargs: dict[str, Any] = {}
        if backend == "postgres":
            vector_dim = int(os.environ.get("MEMPLEX_PGVECTOR_DIM", "0") or 0)
            vector_policy: Literal["required", "best_effort", "disabled"] = (
                "disabled" if vector_dim == 0 else
                ("required" if self._is_production_profile() else "best_effort")
            )
            storage_path = str(cfg.storage.path)
            if cfg.sync.enabled:
                migration_dsn = cfg.storage.migration_dsn
                inbound_dsn = cfg.storage.inbound_dsn
                if type(migration_dsn) is not str or not migration_dsn.strip():
                    raise ValueError("sync-enabled storage requires a non-empty migration DSN")
                if type(inbound_dsn) is not str or not inbound_dsn.strip():
                    raise ValueError("sync-enabled inbound DSN is required")
                self._postgres_resources = PostgresSyncStorageResources(
                    app_dsn=storage_path,
                    migration_dsn=migration_dsn,
                    inbound_dsn=inbound_dsn,
                )
            else:
                self._postgres_resources = PostgresStorageResources(
                    dsn=storage_path,
                    migration_dsn=cfg.storage.migration_dsn,
                )
            self._postgres_resources.ensure_ready(
                request=VectorCapabilityRequest(dim=vector_dim, policy=vector_policy),
                deployment_profile=str(cfg.deployment.profile),
            )
            storage_kwargs["ready_pool"] = self._postgres_resources.ready_pool
            if cfg.sync.enabled:
                # Constructed as PostgresSyncStorageResources above under the
                # same condition; the intervening ensure_ready() call clears
                # mypy's assignment narrowing. cast is a runtime no-op.
                storage_kwargs["inbound_executor"] = cast(
                    PostgresSyncStorageResources, self._postgres_resources
                ).executor
                storage_kwargs["sync_capture_policy"] = SyncCapturePolicy(
                    "required",
                    local_node_id=cfg.sync.node_id,
                )
                storage_kwargs["sync_max_attempts"] = cfg.sync.max_attempts
                storage_kwargs["sync_snapshot_ttl_seconds"] = (
                    cfg.sync.cursor_ttl_seconds
                )
                storage_kwargs["sync_max_snapshot_items"] = (
                    cfg.sync.max_snapshot_items
                )
                storage_kwargs["sync_max_active_snapshots_per_tenant"] = (
                    cfg.sync.max_active_snapshots_per_tenant
                )
                storage_kwargs["sync_max_active_snapshots_per_remote"] = (
                    cfg.sync.max_active_snapshots_per_remote
                )
                storage_kwargs["sync_snapshot_create_timeout_seconds"] = (
                    cfg.sync.snapshot_create_timeout_seconds
                )
                storage_kwargs["sync_consumer_ttl_seconds"] = (
                    cfg.sync.consumer_ttl_seconds
                )
                storage_kwargs["sync_retention_min_seconds"] = (
                    cfg.sync.retention_min_seconds
                )
        elif cfg.sync.enabled:
            storage_kwargs["sync_capture_policy"] = SyncCapturePolicy(
                "required",
                local_node_id=cfg.sync.node_id,
            )
            storage_kwargs["sync_max_pending_events"] = cfg.sync.max_pending_events
            storage_kwargs["sync_max_attempts"] = cfg.sync.max_attempts
            storage_kwargs["sync_snapshot_ttl_seconds"] = cfg.sync.cursor_ttl_seconds
            storage_kwargs["sync_max_snapshot_items"] = cfg.sync.max_snapshot_items
            storage_kwargs["sync_max_active_snapshots_per_tenant"] = (
                cfg.sync.max_active_snapshots_per_tenant
            )
            storage_kwargs["sync_max_active_snapshots_per_remote"] = (
                cfg.sync.max_active_snapshots_per_remote
            )
            storage_kwargs["sync_consumer_ttl_seconds"] = (
                cfg.sync.consumer_ttl_seconds
            )
            storage_kwargs["sync_retention_min_seconds"] = (
                cfg.sync.retention_min_seconds
            )
        try:
            self._initialize_runtime(cfg, backend, storage_kwargs)
        except BaseException:
            try:
                self._close_postgres_resources_once()
            except BaseException as exc:  # noqa: BLE001 - shutdown/cleanup semantics; primary error stays authoritative
                # Construction failure is the caller-visible cause.  Resource
                # shutdown is still attempted exactly once and its state is
                # retained for diagnostic inspection.
                logger.debug("suppressed BaseException: %s", exc)
            raise

    def _initialize_runtime(
        self,
        cfg: MemplexConfig,
        backend: str,
        storage_kwargs: dict[str, Any],
    ) -> None:
        """Construct all runtime collaborators after storage readiness."""
        self.store: MemoryStore = create_store(
            backend=backend,
            path=cfg.storage.path,
            require_authorization=self._is_production_profile(),
            deployment_profile=str(cfg.deployment.profile),
            **storage_kwargs,
        )

        if cfg.sync.enabled and cfg.sync.targets:
            self._initialize_sync_dispatcher(cfg)

        # ── Typed-node lookup facade (Function + Fact/Preference) ──
        self._typed_lookup = _TypedNodeLookup(self.store)

        # ── Embedding ───────────────────────────────────────────
        self._embedding_service = EmbeddingService(
            model=cfg.embedding.model,
            dimension=cfg.embedding.dimension,
            storage=self.store,
            batch_size=cfg.embedding.batch_size,
            contextual_retrieval=cfg.embedding.contextual_retrieval,
        )

        # ── pgvector embedder injection ─────────────────────────
        # The postgres backend's hybrid search (tsv + vector RRF) needs
        # an embedder to light up its vector leg; create_store stays
        # embedder-free (the embedding service did not exist yet), so the
        # shared service is injected here. Duck-typed: lite stores and
        # sync-wrapped stores without a setter are unaffected.
        _pg_store = getattr(self.store, "local", self.store)
        _set_embedder = getattr(_pg_store, "set_embedder", None)
        if callable(_set_embedder):
            _set_embedder(self._embedding_service)

        # ── Reranker ────────────────────────────────────────────
        self._reranker = Reranker(
            embedding_service=self._embedding_service,
            weights=cfg.reranker.weights,
            storage=self.store,
            recency_halflife_days=cfg.reranker.recency_halflife_days,
        )

        # ── Cross-encoder (stage 2, optional) ───────────────────
        self._cross_reranker = CrossEncoderReranker(
            model_name=cfg.reranker.cross_encoder_model,
            enabled=cfg.reranker.cross_encoder_enabled,
        )

        # ── LLM Enhancer (optional) ─────────────────────────────
        self._llm: LLMEnhancer | None = None
        self._init_llm(cfg)

        # ── Feedback store ──────────────────────────────────────
        if backend == "postgres":
            # Postgres feedback is synchronous (psycopg2); it receives the
            # same service-owned ready pool as the main store.
            self._feedback_store: FeedbackStore = create_feedback_store(
                backend=backend,
                dsn=str(cfg.storage.path),
                require_authorization=self._is_production_profile(),
                ready_pool=self._postgres_resources.ready_pool if self._postgres_resources else None,
            )
        else:
            feedback_path = Path(cfg.storage.path).expanduser() / "feedback.json"
            self._feedback_store = create_feedback_store(
                backend=backend,
                path=feedback_path,
            )

        # ── Core engine (extraction pipeline) ──────────────────
        self._engine = CoreEngine(store=self.store)

        # ── Authorization / ACL gate ────────────────────────────
        # Owns tenancy/workspace/user/session visibility so the service no
        # longer mixes ACL enforcement with memory orchestration. The gate
        # resolves ``self.store`` / ``self._feedback_store`` lazily.
        # (Constructed early in __init__; nothing to bind here.)

        # ── Compaction pipeline ─────────────────────────────────
        self._compaction = CompactionPipeline(
            store=self.store,
            embedding_service=self._embedding_service,
            config=cfg,
        )

        # ── Background worker ───────────────────────────────────
        # Share the live collaborators so task handlers (index build,
        # wiki compile, vector refresh) operate on the same store /
        # engine / embedding service instead of lazily building private
        # default instances whose results would not be shared.
        worker_storage_path = (
            Path(cfg.storage.path).expanduser() / "tasks.json"
            if backend == "lite"
            else None
        )
        task_repository = None
        if backend == "postgres":
            from memplex.storage.postgres_tasks import PostgresTaskRepository

            if self._postgres_resources is None:  # pragma: no cover - construction invariant
                raise RuntimeError("PostgreSQL task repository requires ready resources")
            task_repository = PostgresTaskRepository(
                ready_pool=self._postgres_resources.ready_pool
            )
        self._worker = BackgroundWorker(
            storage_path=worker_storage_path,
            task_repository=task_repository,
            compaction_pipeline=self._compaction,
            store=self.store,
            engine=self._engine,
            embedding_service=self._embedding_service,
            config=cfg,
        )

        # ── Graph builder ───────────────────────────────────────
        self._graph_builder = GraphBuilder(
            store=self.store,
            config=cfg,
            embedding_service=self._embedding_service,
        )

        # ── Multi-path retrieval ───────────────────────────────
        self._retriever = MultiPathRetriever(
            self.store,
            embedding_service=self._embedding_service,
            wiki_searcher=self._build_wiki_searcher(cfg),
        )

        # ── Injection scan tracking ─────────────────────────────
        self._injection_scans = InjectionScanCounter()
        self._injection_risks = InjectionRiskRegistry()

    def _initialize_sync_dispatcher(self, cfg: MemplexConfig) -> None:
        """Bind configured peer identities to the durable repository."""
        from memplex.sync_dispatcher import SyncDispatcher

        repository: Any = self.store
        if self._is_production_profile():
            context = resolve_environment_authorization(
                agent_id=None,
                provenance={"transport": "sync-dispatcher"},
                require_registry=True,
            )
            if context is None:  # pragma: no cover - require_registry guard
                raise PermissionError("sync dispatcher principal is unavailable")
            if context.agent_id != cfg.sync.node_id:
                raise PermissionError(
                    "sync dispatcher principal does not match local node identity"
                )
            registry = PrincipalRegistry.from_environment()
            if registry is None:  # pragma: no cover - require_registry guard
                raise PermissionError("sync dispatcher principal registry is unavailable")
            tenant_ids = {item.tenant_id for item in registry.credentials}
            if tenant_ids != {context.principal.tenant_id}:
                raise PermissionError(
                    "sync-enabled production requires a single-tenant principal registry"
                )
            authorize = getattr(repository, "authorized", None)
            if not callable(authorize):
                raise TypeError("sync-enabled production store must be authorizable")
            repository = authorize(context)

        targets = {
            target_id: cfg.sync.validate_remote_url(
                url, profile=str(cfg.deployment.profile)
            )
            for target_id, url in cfg.sync.targets.items()
        }
        for target_id in targets:
            repository.sync_register_target(target_id, bootstrap="future")

        headers: dict[str, str] = {}
        principal_token = os.environ.get("MEMPLEX_PRINCIPAL_TOKEN")
        if principal_token:
            headers["X-API-Key"] = principal_token
        self._sync_dispatcher = SyncDispatcher(
            repository,
            targets=targets,
            local_node_id=cfg.sync.node_id,
            headers=headers,
            claim_size=cfg.sync.claim_size,
            max_in_flight=cfg.sync.max_in_flight,
            per_target_in_flight=cfg.sync.per_target_in_flight,
            lease_seconds=cfg.sync.lease_seconds,
            max_response_bytes=cfg.sync.max_batch_bytes,
        )

    def _close_postgres_resources_once(self) -> None:
        """Close the owned resource at most once; concurrent callers agree."""
        resources = self._postgres_resources
        if resources is None:
            return
        with self._lifecycle_condition:
            if self._postgres_resource_close_state == "closed":
                return
            if self._postgres_resource_close_state == "faulted":
                assert self._postgres_resource_close_error is not None
                raise self._postgres_resource_close_error
            if self._postgres_resource_close_state == "closing":
                while self._postgres_resource_close_state == "closing":
                    self._lifecycle_condition.wait()
                if self._postgres_resource_close_state == "faulted":
                    assert self._postgres_resource_close_error is not None
                    raise self._postgres_resource_close_error
                return
            self._postgres_resource_close_state = "closing"
        try:
            resources.close(wait=True)
        except BaseException as exc:
            with self._lifecycle_condition:
                self._postgres_resource_close_error = exc
                self._postgres_resource_close_state = "faulted"
                self._lifecycle_condition.notify_all()
            raise
        with self._lifecycle_condition:
            self._postgres_resource_close_state = "closed"
            self._lifecycle_condition.notify_all()

    # ── Authorization / ACL (delegated to AuthorizationGate) ────────
    # All tenancy/workspace/user/session visibility logic lives in
    # ``self._auth`` (memplex/authorization.py). These thin wrappers keep the
    # service's internal call sites and the public ``service._store_for`` /
    # ``service._require_authorization`` API stable while removing the logic
    # from the service itself.

    def _require_authorization(
        self, context: AuthorizationContext | None
    ) -> AuthorizationContext:
        """Require adapter-bound identity outside the local development profile."""
        return self._auth.require_authorization(context)

    def _is_production_profile(self) -> bool:
        """Whether this service is running under the production contract."""
        return self._auth.is_production()

    def _store_for(self, context: AuthorizationContext) -> Any:
        """Return an immutable request-scoped storage facade when supported."""
        return self._auth.store_for(context)

    def _feedback_store_for(self, context: AuthorizationContext) -> FeedbackStore:
        """Return the request-scoped feedback facade for production calls."""
        return self._auth.feedback_store_for(context)

    def _typed_lookup_for(self, context: AuthorizationContext) -> _TypedNodeLookup:
        """Build a typed lookup over the same request-scoped storage facade."""
        return self._auth.typed_lookup_for(context)

    @staticmethod
    def _identity_value(node: Any, field_name: str, namespace_key: str) -> str | None:
        """Resolve a node identity field, accepting the stable namespace copy."""
        return AuthorizationGate.identity_value(node, field_name, namespace_key)

    @staticmethod
    def _is_local_development_context(context: AuthorizationContext) -> bool:
        """Whether *context* is the explicit compatibility trust boundary."""
        return AuthorizationGate.is_local_development_context(context)

    def _is_node_visible(self, node: Any, context: AuthorizationContext) -> bool:
        """Return whether *node* is in the authenticated caller's ACL scope."""
        return self._auth.is_node_visible(node, context)

    def _visible_node(self, memory_id: str, context: AuthorizationContext) -> Any:
        """Load one node and hide inaccessible identifiers from callers."""
        return self._auth.visible_node(memory_id, context)

    def _require_visible_node(self, memory_id: str, context: AuthorizationContext) -> Any:
        """Return a visible node or raise the uniform opaque mutation error."""
        return self._auth.require_visible_node(memory_id, context)

    def _filter_authorized_results(
        self, results: list[SearchResult], context: AuthorizationContext
    ) -> list[SearchResult]:
        """Drop inaccessible search candidates before any ranking side effect."""
        return self._auth.filter_authorized_results(results, context)

    @staticmethod
    def _bind_extracted_identity(
        extracted: ExtractedData,
        context: AuthorizationContext,
        *,
        visibility: str = "workspace",
    ) -> None:
        """Stamp every extraction product before any store operation begins."""
        AuthorizationGate.bind_extracted_identity(extracted, context, visibility=visibility)

    # ── LLM initialisation ──────────────────────────────────────

    def _init_llm(self, cfg: MemplexConfig) -> None:
        """Try to create an LLMEnhancer; silently skip on failure."""
        try:
            provider = create_provider(
                provider=cfg.llm.provider,
                anthropic_api_key=cfg.llm.anthropic_api_key,
                local_endpoint=cast(str, cfg.llm.local_endpoint),
                local_model=cast(str, cfg.llm.local_model),
                fallback_chain=cfg.llm.fallback_chain,
            )
            self._llm = LLMEnhancer(llm_provider=provider, config=cfg.llm)
        except Exception as exc:  # noqa: BLE001 - logged degradation path
            logger.info("LLM enhancer not available (%s); using rule-based fallback", exc)
            self._llm = None

    # ── Wiki searcher initialisation ────────────────────────────────

    def _build_wiki_searcher(self, cfg: MemplexConfig) -> object | None:
        """Build a DualIndexSearch over compiled wiki pages when enabled.

        Returns ``None`` when wiki is disabled or construction fails; the
        retriever then passes wiki_search through to ``store.fts_search``.
        """
        wiki_cfg = getattr(cfg, "wiki", None)
        if wiki_cfg is None or not getattr(wiki_cfg, "enabled", False):
            return None
        try:
            from memplex.wiki.search import DualIndexSearch

            return DualIndexSearch(
                wiki_dir=Path(wiki_cfg.dir).expanduser(),
                embedding_service=self._embedding_service,
            )
        except Exception as exc:  # noqa: BLE001 - logged degradation path
            logger.debug("wiki searcher init failed; wiki path falls back to FTS: %s", exc)
            return None

    # ── Injection scan helper ──────────────────────────────────────
    # ``_extract_scan_text`` lives on ``IndirectInjectionGuard`` (llm/
    # injection_guard.py) and is shared by both the write path (here,
    # in ``write()``) and the read path (``filter_and_wrap``). Keeping a
    # single copy prevents the two paths from drifting when a new memory
    # type is added.

    def _mark_injection_suspected(self, nodes: Iterable[object]) -> None:
        """Scan typed nodes before persistence and retain only internal risk state.

        Functions keep their historical serialized marker for compatibility.
        Fact, Preference, and Observation carry no protocol-polluting marker:
        their bounded in-process registry entry is backed by mandatory typed
        content scans at every model-facing read.
        """
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        self._injection_scans.prune(today)
        seen: set[int] = set()
        for node in nodes:
            if id(node) in seen:
                continue
            seen.add(id(node))
            if not IndirectInjectionGuard.is_suspected(node):
                continue
            node_id = str(getattr(node, "id", "") or "")
            self._injection_risks.mark(node_id)
            attrs = getattr(node, "attributes", None)
            if isinstance(attrs, dict):
                attrs["memplex_injection_suspected"] = "true"
            self._injection_scans.increment(today)
            logger.warning(
                "Indirect injection suspected in memory %s (type=%s); retained but withheld from model reads.",
                node_id or "?",
                getattr(node, "memory_type", "unknown"),
            )

    def scan_nodes_before_persistence(self, nodes: Iterable[object]) -> None:
        """Run the shared model-visible scan before any caller persists nodes."""
        self._mark_injection_suspected(nodes)

    def scan_sync_events_before_persistence(self, events: Iterable[object]) -> None:
        """Decode typed sync upserts and register their risk before apply.

        Repository implementations retain authority over canonical protocol
        and LWW validation.  This service boundary only ensures that every
        model-visible typed payload is assessed before the repository can
        publish it.
        """
        from memplex.models import Fact, Function, MemoryNode, Observation, Preference
        from memplex.sync_protocol import SyncNodeType, SyncOperation

        model_by_type: dict[SyncNodeType, type[MemoryNode]] = {
            SyncNodeType.FUNCTION: Function,
            SyncNodeType.FACT: Fact,
            SyncNodeType.PREFERENCE: Preference,
            SyncNodeType.OBSERVATION: Observation,
        }
        nodes: list[object] = []
        for event in events:
            if getattr(event, "operation", None) is not SyncOperation.UPSERT:
                continue
            node_type = getattr(event, "node_type", None)
            if not isinstance(node_type, SyncNodeType):
                continue
            model = model_by_type.get(node_type)
            if model is None:
                continue
            to_dict = getattr(event, "to_dict", None)
            if not callable(to_dict):
                raise TypeError("typed sync event must expose to_dict")
            raw = to_dict().get("payload")
            if type(raw) is not dict:
                raise TypeError("typed sync upsert payload must be an object")
            nodes.append(model.from_dict(raw))
        self._mark_injection_suspected(nodes)

    def is_safe_for_model(self, node: object) -> bool:
        """Return whether a loaded typed node may be serialized to a model."""
        try:
            node_id = str(getattr(node, "id", "") or "")
        except Exception as exc:  # noqa: BLE001 - logged degradation path
            logger.warning("injection node id inspection failed closed: %s", exc)
            return False
        if self._injection_risks.contains(node_id):
            return False
        suspected = IndirectInjectionGuard.is_suspected(node)
        if suspected:
            self._injection_risks.mark(node_id)
        return not suspected

    # ════════════════════════════════════════════════════════════════
    #  Core query
    # ════════════════════════════════════════════════════════════════


    def query(
        self,
        text: str,
        top_k: int = 10,
        owner: str | None = None,
        max_tokens: int = 4000,
        namespace_filter: dict[str, str | None] | list[dict[str, str | None]] | None = None,
        explain: bool = False,
        *,
        authorization: AuthorizationContext | None = None,
    ) -> QueryResult:
        """Unified query entry point.

        Pipeline:
        1. Intent detection (LLM first, keyword fallback).
        2. Parallel multi-path retrieval (ThreadPoolExecutor, 3 workers).
        3. Merge + deduplicate by ``func_id`` (keep highest score).
        4. Rerank (6-dim bi-encoder + optional cross-encoder).
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
            ``access_count`` updates. A list expresses OR alternatives. Used
            by agent adapters to enforce visibility before touching results.
        explain:
            Include a product-facing retrieval trace that explains the stages,
            filters, budgets, and final injected candidates.

        The stage-by-stage machinery lives in
        :class:`memplex.query_pipeline.QueryPipeline`; this method resolves
        authorization, binds the current collaborators, and delegates.
        """
        context = self._require_authorization(authorization)
        # The pipeline is built per call from the service's current
        # attributes so monkeypatched instance attributes (reranker /
        # retriever / _detect_scope in tests) stay live.
        pipeline = QueryPipeline(
            config=self._config,
            store=self._store_for(context),
            base_store=self.store,
            retriever=self._retriever,
            embedding_service=self._embedding_service,
            llm=self._llm,
            reranker=self._reranker,
            cross_reranker=self._cross_reranker,
            injection_risks=self._injection_risks,
            auth=self._auth,
            detect_scope=self._detect_scope,
            compute_hyde_vector=self._compute_hyde_vector,
        )
        return pipeline.run(
            text,
            top_k=top_k,
            owner=owner,
            max_tokens=max_tokens,
            namespace_filter=namespace_filter,
            explain=explain,
            context=context,
        )

    async def query_async(
        self,
        text: str,
        top_k: int = 10,
        owner: str | None = None,
        max_tokens: int = 4000,
        namespace_filter: dict[str, str | None] | list[dict[str, str | None]] | None = None,
        explain: bool = False,
        *,
        authorization: AuthorizationContext | None = None,
    ) -> QueryResult:
        """Async version of :meth:`query`.

        Runs the synchronous ``query`` in a thread pool so it does not
        block the event loop (for FastAPI / MCP Server use).
        """
        context = self._require_authorization(authorization)
        store = self._store_for(context)
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
                authorization=context,
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
            except Exception as exc:  # noqa: BLE001 - logged degradation path
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
        attributes: dict[str, Any] | None = None,
        needs_review: bool | None = None,
        authorization: AuthorizationContext | None = None,
    ) -> list[Function]:
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

        Typed nodes (Fact / Preference, resolvable via the optional
        ``get_fact`` / ``get_preference`` store APIs) carry no free-form
        ``attributes`` map. For them, only ``memplex_*`` keys are projected
        into the base ``namespace`` field; ``needs_review`` is persisted for
        every memory type.
        """

        context = self._require_authorization(authorization)
        store = self._store_for(context)
        memory_ids = list(memory_ids)
        visible_nodes = [self._require_visible_node(memory_id, context) for memory_id in memory_ids]
        controlled_annotate = getattr(store, "annotate_nodes", None)
        if callable(controlled_annotate):
            # One COW pair commit for a mixed Function/Fact/Preference batch.
            # Visibility is resolved for every ID before this call, so a bad
            # ID cannot leave an earlier sibling partially published.
            return controlled_annotate(
                memory_ids,
                attributes=attributes,
                needs_review=needs_review,
            )
        updated: list[Function] = []
        typed_updated: list = []
        for node in visible_nodes:
            if isinstance(node, Function):
                func = node
                if attributes:
                    func.attributes.update(attributes)
                if needs_review is not None:
                    func.needs_review = needs_review
                updated.append(func)
                continue
            if attributes:
                # Typed nodes have no free-form ``attributes`` field, but
                # Memplex namespace keys are part of their MemoryNode base
                # projection.  Applying them here makes annotate_memories a
                # real persistence boundary even for stores whose getters do
                # not return live objects (for example PostgreSQL adapters).
                node.namespace.update(
                    {
                        str(key): str(value)
                        for key, value in attributes.items()
                        if str(key).startswith("memplex_") and value is not None
                    }
                )
            if needs_review is not None:
                node.needs_review = needs_review
            typed_updated.append(node)

        if not updated and not typed_updated:
            return []

        source = SourceDocument(type="metadata_update", source_type=SourceType.WIKI)
        for func in updated:
            replace = getattr(store, "replace_function", None)
            if callable(replace):
                replace(func)
            else:
                store.add(func, source)
        for node in typed_updated:
            add = getattr(
                store,
                "add_fact" if getattr(node, "memory_type", "") == "fact" else "add_preference",
                None,
            )
            if callable(add):
                add(node)

        return updated + typed_updated

    # ════════════════════════════════════════════════════════════════
    #  HyDE
    # ════════════════════════════════════════════════════════════════

    def _augment_with_facts(self, content: str) -> str:
        """Append retain()-style extracted facts to capture content (best-effort).

        Runs :meth:`LLMEnhancer.factualize` in a worker thread (same
        isolation pattern as ``_compute_hyde_vector``); failures or empty
        results leave *content* unchanged.
        """
        if self._llm is None:
            return content
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                facts = pool.submit(
                    asyncio.run, self._llm.factualize(content)
                ).result(timeout=10.0)
        except Exception as exc:  # noqa: BLE001 - logged degradation path
            logger.debug("factual capture failed, keeping original content: %s", exc)
            return content
        if not facts:
            return content
        return content + "\n\nExtracted facts:\n" + "\n".join(f"- {fact}" for fact in facts)

    def _compute_hyde_vector(self, text: str) -> Vector | None:
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
            return self._embedding_service.embed_query(hyde_text)
        except Exception as exc:  # noqa: BLE001 - logged degradation path
            logger.warning("HyDE failed, falling back to raw query vector: %s", exc)
            return None

    # ════════════════════════════════════════════════════════════════
    #  Write operations
    # ════════════════════════════════════════════════════════════════

    def write(
        self,
        source: SourceDocument,
        *,
        visibility: str = "workspace",
        authorization: AuthorizationContext | None = None,
    ) -> ExtractedData:
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
        context = self._require_authorization(authorization)
        store = self._store_for(context)

        # 0. Strip <private>...</private> blocks before extraction so
        #    operator-marked secrets never reach storage. Applies to every
        #    write caller (CLI/HTTP/MCP/corpus/agent_runtime), not only the
        #    Claude Code hook runner which already stripped these.
        from memplex.privacy import strip_private_tags

        if source.content:
            source.content = strip_private_tags(source.content)

        # 0b. retain()-style factual capture (opt-in): when a real LLM is
        # available and ``llm.factual_capture`` is enabled, self-contained
        # temporally-normalised facts are appended to the document content
        # so the rule-based extractor stores them verbatim as typed nodes.
        if (
            source.content
            and self._llm is not None
            and self._config.llm.factual_capture
        ):
            source.content = self._augment_with_facts(source.content)

        # 1. CoreEngine: full extraction pipeline.  PostgreSQL graph-edge
        # detection reads existing memories, so it receives the same scoped
        # facade as persistence instead of consulting the shared base store.
        engine = self._engine if store is self.store else CoreEngine(store=store)
        extracted = engine.extract(source)

        # Identity must be bound before any typed-node store write, graph
        # merge, or background work observes an extracted memory.
        self._bind_extracted_identity(extracted, context, visibility=visibility)

        # 1b. Scan all extracted typed nodes before any persistence path.
        self.scan_nodes_before_persistence(
            [*extracted.functions, *extracted.facts, *extracted.preferences]
        )

        # 1b2. Working-memory tier (opt-in): typed captures also land in the
        # TTL hot-context store for automatic injection on the next recall.
        if self._working_memory is not None:
            # Scope hot-context per workspace (V3 fix): entries are
            # invisible across workspaces; within a workspace the tier is
            # the team's shared hot context (matching the knowledge-tier
            # visibility model).
            scope = f"tenant:{context.principal.tenant_id}"
            for node in [*extracted.facts, *extracted.preferences]:
                content = getattr(node, "preference", None) or getattr(node, "context", None) or ""
                key = getattr(node, "id", None)
                if content and key:
                    self._working_memory.add(str(key), str(content), scope=scope)
            for func in extracted.functions:
                name = getattr(func, "name", "")
                if name:
                    self._working_memory.add(f"fn:{func.id}", name, scope=scope)

        # 1c. Persist Fact / Preference nodes. Previously only Functions
        #     were stored, so fact/preference-intent paragraphs (e.g.
        #     "I prefer ...") were extracted and then silently dropped,
        #     making them unrecallable. Duck-typed: backends without the
        #     optional typed APIs skip with a debug trace.
        self._persist_typed_nodes(extracted, store=store)

        # 2. Persist Functions and graph edges after the unified typed scan.
        #    Functions retain the historical marker; other node types are
        #    guarded by their typed content and bounded internal risk cache.
        if extracted.functions:
            store.merge(extracted.graph)

            # Invalidate graph builder cache so next write sees new data
            self._graph_builder.invalidate_cache()

        # 3. Background tasks (index build, wiki compile, etc.)
        # These are submitted to the background worker asynchronously.
        if not self._is_production_profile():
            try:
                for func in extracted.functions:
                    self._worker.submit(
                        BackgroundTask.BUILD_INDEX,
                        # NOTE: SourceDocument has no ``id`` attribute; this
                        # lookup always raises AttributeError at runtime and
                        # is swallowed by the surrounding ``except``. Kept
                        # byte-for-byte via cast; tracked as a latent bug.
                        {"func_id": func.id, "source_id": cast(Any, source).id if source else None},
                    )
            except Exception as exc:  # noqa: BLE001 - logged degradation path
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
        if not self._is_production_profile():
            self._maybe_schedule_compaction(store=store)

        return extracted

    def _persist_typed_nodes(self, extracted: ExtractedData, *, store: Any = None) -> None:
        """Persist extracted Fact / Preference nodes through the store's
        optional typed APIs.

        Duck-typed: when the backend does not implement ``add_fact`` /
        ``add_preference`` the nodes are skipped with a debug log instead
        of failing the write. Individual persistence failures are also
        best-effort (debug-logged) so one bad node cannot lose the rest
        of the extraction.
        """
        store = self.store if store is None else store
        for kind, nodes, method_name in (
            ("fact", extracted.facts, "add_fact"),
            ("preference", extracted.preferences, "add_preference"),
        ):
            if not nodes:
                continue
            add = getattr(store, method_name, None)
            if not callable(add):
                logger.debug(
                    "store %s has no %s; skipping %d extracted %s node(s)",
                    type(store).__name__,
                    method_name,
                    len(nodes),
                    kind,
                )
                continue
            if kind == "fact":
                self._supersede_contradicted_facts_batch(nodes, store)
            for node in nodes:
                try:
                    add(node)
                except Exception as exc:  # noqa: BLE001 - logged degradation path
                    logger.debug(
                        "%s persistence failed for %s: %s",
                        kind,
                        getattr(node, "id", "?"),
                        exc,
                    )

    def _supersede_contradicted_facts_batch(
        self, facts: Iterable[Fact], store: Any
    ) -> None:
        """Bi-temporal supersede (Zep-style) for a batch of new facts.

        Loads the existing-fact list once (not per-fact), then stamps
        ``invalid_at`` on stored facts occupying the same
        (subject, predicate) slot so contradicted claims are retained for
        point-in-time queries instead of being silently overwritten.
        Best-effort: a listing failure skips supersession (the write
        itself proceeds).
        """
        from memplex import temporal

        list_facts = getattr(store, "list_facts", None)
        if not callable(list_facts):
            return
        try:
            existing = list_facts()
        except Exception as exc:  # noqa: BLE001 - logged degradation path
            logger.debug("fact supersession listing failed: %s", exc)
            return

        for new_fact in facts:
            if not getattr(new_fact, "valid_from", None):
                new_fact.valid_from = temporal.now_iso()
            superseded = temporal.supersede_contradicted(new_fact, existing)
            for old_fact in superseded:
                try:
                    store.add_fact(old_fact)
                except Exception as exc:  # noqa: BLE001 - logged degradation path
                    logger.debug(
                        "fact supersede persist failed for %s: %s", old_fact.id, exc
                    )
            if superseded:
                logger.debug("superseded %d contradicted fact(s)", len(superseded))

    def _maybe_schedule_compaction(self, *, store: Any = None) -> None:
        """Submit a background compaction when the corpus crosses thresholds.

        Reads ``compaction.warn_threshold`` (soft trigger) and
        ``compaction.hard_limit`` (force trigger) from config. Best-effort:
        a full worker queue or a counting failure never blocks the write.
        """
        try:
            store = self.store if store is None else store
            total = self._count_functions_exact(store)
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
        except Exception as exc:  # noqa: BLE001 - logged degradation path
            logger.debug("compaction scheduling check failed: %s", exc)

    @staticmethod
    def _count_functions_exact(store: Any) -> int:
        """Count Functions via the lightweight contract, falling back to pagination."""
        count = getattr(store, "count_functions", None)
        if callable(count):
            return int(count())
        total = 0
        while True:
            page = store.list_functions(offset=total, limit=10_000)
            count = len(page)
            total += count
            if count < 10_000:
                return total

    def write_text(
        self,
        text: str,
        source_type: str = "text",
        *,
        visibility: str = "workspace",
        authorization: AuthorizationContext | None = None,
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
        context = self._require_authorization(authorization)
        source = SourceDocument(
            type=source_type,
            content=text,
            source_type=SourceType.WIKI,
        )
        return self.write(source, visibility=visibility, authorization=context)

    # ════════════════════════════════════════════════════════════════
    #  Memory operations
    # ════════════════════════════════════════════════════════════════

    def get(
        self,
        memory_id: str,
        *,
        authorization: AuthorizationContext | None = None,
    ) -> Function | None:
        """Retrieve a single memory node by ID, or ``None``.

        Functions resolve through ``store.get``; Fact/Preference nodes
        resolve through the optional typed store APIs (``get_fact`` /
        ``get_preference``) so capture/recall flows treat every memory
        type uniformly (previously a typed id always returned ``None``).
        """
        context = self._require_authorization(authorization)
        # The unbound development API is intentionally a compatibility
        # surface for local tooling. Production never reaches this branch
        # because ``_require_authorization`` rejects absent credentials.
        if authorization is None and self._is_local_development_context(context):
            node = self._typed_lookup_for(context).get(memory_id)
        else:
            node = self._visible_node(memory_id, context)
        return node if node is None or self.is_safe_for_model(node) else None

    def get_timeline(
        self,
        memory_id: str,
        limit: int = 20,
        *,
        authorization: AuthorizationContext | None = None,
    ) -> list:
        """Return a visible memory's changelog, hiding cross-scope IDs."""
        context = self._require_authorization(authorization)
        if self._visible_node(memory_id, context) is None:
            return []
        try:
            return list(self._store_for(context).get_timeline(memory_id, limit=limit))
        except Exception as exc:  # noqa: BLE001 - logged degradation path
            logger.debug("get_timeline failed for %s: %s", memory_id, exc)
            return []

    def list_facts(
        self,
        as_of: str | None = None,
        limit: int = 1000,
        *,
        include_invalidated: bool = False,
        authorization: AuthorizationContext | None = None,
    ) -> list[Fact]:
        """List Fact nodes through the store's optional API, temporally scoped.

        Bi-temporal read surface: by default only facts whose
        (``valid_from``, ``invalid_at``) business-time interval covers *now*
        are returned. ``as_of`` (ISO datetime string) selects any point in
        time — the auditable history of superseded claims. Pass
        ``include_invalidated=True`` to bypass temporal filtering entirely.
        Visibility and injection safety follow the same rules as the other
        list surfaces.
        """
        from datetime import datetime as _dt

        from memplex import temporal

        context = self._require_authorization(authorization)
        store = self._store_for(context)
        list_fn = getattr(store, "list_facts", None)
        if not callable(list_fn):
            logger.debug(
                "store %s has no list_facts; returning empty list",
                type(store).__name__,
            )
            return []
        try:
            facts = list(list_fn(limit=limit))
            visible = facts if (
                authorization is None and self._is_local_development_context(context)
            ) else [
                fact for fact in facts if self._is_node_visible(fact, context)
            ]
            safe = [fact for fact in visible if self.is_safe_for_model(fact)]
            if include_invalidated:
                return safe
            point = None
            if as_of is not None:
                try:
                    point = _dt.fromisoformat(as_of)
                except ValueError:
                    point = None  # malformed as_of degrades to "now", never 500s
            return temporal.facts_valid_at(safe, as_of=point)
        except Exception as exc:  # noqa: BLE001 - logged degradation path
            logger.debug("list_facts failed: %s", exc)
            return []

    def list_observations(
        self,
        category: str | None = None,
        owner: str | None = None,
        limit: int = 1000,
        *,
        authorization: AuthorizationContext | None = None,
    ) -> list[Observation]:
        """List captured Observation nodes through the store's optional API.

        Thin pass-through to ``store.list_observations`` with *category*
        (see ``OBSERVATION_CATEGORIES``) and *owner* (namespace) filters.
        Duck-typed: backends without Observation listing support return
        ``[]`` with a debug trace instead of failing the caller.
        """
        context = self._require_authorization(authorization)
        store = self._store_for(context)
        list_fn = getattr(store, "list_observations", None)
        if not callable(list_fn):
            logger.debug(
                "store %s has no list_observations; returning empty list",
                type(store).__name__,
            )
            return []
        try:
            observations = list(list_fn(limit=limit, category=category, owner=owner))
            visible = observations if (
                authorization is None and self._is_local_development_context(context)
            ) else [
                observation
                for observation in observations
                if self._is_node_visible(observation, context)
            ]
            return [observation for observation in visible if self.is_safe_for_model(observation)]
        except Exception as exc:  # noqa: BLE001 - logged degradation path
            logger.debug("list_observations failed: %s", exc)
            return []

    def add_observation(
        self,
        observation: Observation,
        *,
        visibility: str = "workspace",
        authorization: AuthorizationContext | None = None,
    ) -> Observation:
        """Bind and persist an Observation through the authorized store."""

        context = self._require_authorization(authorization)
        bind_node_identity(observation, context, visibility=visibility)
        self.scan_nodes_before_persistence([observation])
        add = getattr(self._store_for(context), "add_observation", None)
        if not callable(add):
            raise NotImplementedError(
                f"store {type(self.store).__name__} has no add_observation API"
            )
        add(observation)
        return observation

    def update_memory(
        self,
        memory_id: str,
        role: str,
        new_value: str,
        *,
        authorization: AuthorizationContext | None = None,
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
        context = self._require_authorization(authorization)
        func = self._require_visible_node(memory_id, context)
        if not isinstance(func, Function):
            raise MemoryNotFoundError("Memory not found")

        old_value = None
        if (
            type(role) is not str
            or type(new_value) is not str
            or role not in {"trigger", "condition", "action", "benefit"}
        ):
            return UpdateResult(
                memory_id=memory_id,
                role="",
                new_value="",
                success=False,
                error="Unknown role",
            )
        values = getattr(func, role)

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
        self.scan_nodes_before_persistence([func])

        # Lite uses explicit replacement so a detached snapshot is never
        # accidentally routed through the name-merge semantics.
        from memplex.models import SourceDocument as SD

        store = self._store_for(context)
        replace = getattr(store, "replace_function", None)
        if callable(replace):
            replace(func)
        else:
            store.add(func, SD(type="manual_update", source_type=SourceType.WIKI))

        return UpdateResult(
            memory_id=memory_id,
            role=role,
            old_value=old_value,
            new_value=new_value,
            version=func.version,
            success=True,
        )

    def delete(
        self,
        memory_id: str,
        *,
        authorization: AuthorizationContext | None = None,
    ) -> None:
        """Delete a visible memory through its typed store boundary."""
        context = self._require_authorization(authorization)
        node = self._require_visible_node(memory_id, context)
        delete_method = {
            "fact": "delete_fact",
            "preference": "delete_preference",
            "observation": "delete_observation",
        }.get(getattr(node, "memory_type", ""), "delete")
        delete = getattr(self._store_for(context), delete_method, None)
        if not callable(delete):
            raise MemoryNotFoundError("Memory not found")
        delete(memory_id)

    # ════════════════════════════════════════════════════════════════
    #  Feedback
    # ════════════════════════════════════════════════════════════════

    def submit_feedback(
        self,
        memory_id: str,
        field_role: str,
        value_index: int,
        verdict: str,
        reason: str | None = None,
        *,
        authorization: AuthorizationContext | None = None,
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
        context = self._require_authorization(authorization)
        node = self._require_visible_node(memory_id, context)
        visibility = str(getattr(node, "visibility", None) or "workspace").strip().lower()
        fb = MemoryFeedback(
            memory_id=memory_id,
            field_role=field_role,
            value_index=value_index,
            verdict=FeedbackVerdict(verdict),
            reason=reason,
            source=context.principal.subject_id,
            owner=context.principal.subject_id,
            tenant_id=context.principal.tenant_id,
            owner_subject_id=context.principal.subject_id,
            workspace_id=context.workspace_id,
            visibility=visibility,
            provenance={
                **context.provenance,
                "agent_id": context.agent_id,
                "authentication_id": context.principal.authentication_id or "",
                "request_id": context.request_id,
                "session_id": context.session_id,
            },
        )
        self._feedback_store_for(context).record(fb)

    def apply_resolution(
        self,
        memory_id: str,
        field_role: str,
        action: str,
        new_value: str | None = None,
        *,
        authorization: AuthorizationContext | None = None,
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
        context = self._require_authorization(authorization)
        self._require_visible_node(memory_id, context)
        self._feedback_store_for(context).resolve(memory_id, field_role, action)

        if action == "merge" and new_value:
            self.update_memory(
                memory_id,
                field_role,
                new_value,
                authorization=context,
            )

        return {"status": "resolved", "action": action}

    def get_pending_reviews(
        self,
        owner: str | None = None,
        limit: int = 100,
        *,
        authorization: AuthorizationContext | None = None,
    ) -> list:
        """Return pending feedback reviews, optionally filtered by owner.

        Returns
        -------
        list[PendingReview]
        """
        context = self._require_authorization(authorization)
        pending = []
        for review in self._feedback_store_for(context).get_pending():
            node = self._visible_node(review.memory_id, context)
            if node is None:
                continue
            # ``owner`` remains a compatibility narrowing filter, but is
            # evaluated against the related memory rather than feedback
            # metadata supplied by a caller.
            if owner is not None and getattr(node, "owner", None) != owner:
                continue
            pending.append(review)
            if len(pending) >= limit:
                break
        return pending

    # ════════════════════════════════════════════════════════════════
    #  Management
    # ════════════════════════════════════════════════════════════════

    def health(self) -> dict:
        """Return health / readiness status.

        Status semantics are component-based: ``"healthy"`` when storage
        responds and the embedding service is present, ``"warning"`` when
        the embedding service is missing (retrieval degrades to FTS-only),
        ``"degraded"`` when storage errors. Whether the background worker
        has been started is reported separately via ``worker_running`` --
        not calling :meth:`start` is a lifecycle choice, not a component
        failure, so pre-start health no longer reports a spurious warning.

        Returns
        -------
        dict
            Keys: ``status``, ``backend``, ``functions_total``, ``edges_total``,
            ``queue_depth``, ``last_compaction``, ``injection_scans_detected_24h``,
            ``dead_letters_pending``, ``worker_running``, ``version``.
        """
        # Determine overall status
        try:
            self.store.list_functions(limit=1)
            storage_status = "ok"
        except Exception as exc:  # noqa: BLE001 - broad catch with explicit fallback handling
            storage_status = f"error: {exc}"

        worker_running = self._worker._running
        if storage_status != "ok":
            status = "degraded"
        elif self._embedding_service is None:
            status = "warning"
        else:
            status = "healthy"

        # Count functions
        try:
            functions_total = self._count_functions_exact(self.store)
        except Exception as exc:  # noqa: BLE001 - logged degradation path
            logger.debug("health: list_functions failed, reporting 0: %s", exc)
            functions_total = 0

        # Count edges
        try:
            graph = self.store.get_graph()
            edges_total = len(graph.edges)
        except Exception as exc:  # noqa: BLE001 - logged degradation path
            logger.debug("health: get_graph failed, reporting 0 edges: %s", exc)
            edges_total = 0

        # Queue depth
        queue_depth = self._worker.queue_depth

        # Last compaction
        last_compaction = self._worker.last_compaction
        last_compaction_iso: str | None = None
        if last_compaction is not None:
            last_compaction_iso = last_compaction.isoformat()

        # Injection scans detected in last 24h (prune stale date keys so
        # the map cannot grow one entry per day forever).
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        self._injection_scans.prune(today)
        injection_scans_detected_24h = self._injection_scans.count(today)

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
            "last_compaction": last_compaction_iso,
            "injection_scans_detected_24h": injection_scans_detected_24h,
            "dead_letters_pending": dead_letters_pending,
            "worker_running": worker_running,
            "version": version,
            # Scalability/load indicators (for auto-adaptive decisions
            # and operator visibility into congestion).
            "sync": self._sync_health(),
        }

    def runtime_status(self) -> dict[str, object]:
        """Return a fixed, low-information runtime status for operators."""
        return {
            "schema_version": 1,
            "lifecycle": self._runtime_lifecycle.state,
            "worker_running": bool(self._worker._running),
            "sync_enabled": self._sync_dispatcher is not None,
        }

    def operations_metrics_status(self) -> dict[str, object]:
        """Return only bounded counters consumed by the metrics endpoint."""
        try:
            from memplex.worker import TaskStatus

            worker_counts = self._worker._task_store.status_counts()
            worker_pending = worker_counts.get(TaskStatus.PENDING, 0)
            worker_leased = worker_counts.get(TaskStatus.RUNNING, 0)
            worker_dead_letters = worker_counts.get(TaskStatus.FAILED, 0)
        except Exception:  # noqa: BLE001 - broad catch with explicit fallback handling
            worker_pending = 0
            worker_leased = 0
            worker_dead_letters = 0
        sync = self.sync_status()
        # ``_postgres_resources`` is a PostgresSyncStorageResources when sync
        # ingress is enabled, and that wrapper deliberately exposes no pool
        # counters.  Degrade the three gauges to 0 in that case (same as the
        # no-resources case) instead of raising AttributeError.
        resources = self._postgres_resources
        return {
            "runtime_state": self._runtime_lifecycle.state,
            "worker_pending": worker_pending,
            "worker_leased": worker_leased,
            "worker_dead_letters": worker_dead_letters,
            "sync_pending": sync["pending"],
            "sync_leased": sync["leased"],
            "sync_dead_letters": sync["dead_letters"],
            "pool_business_leases": getattr(resources, "business_lease_count", 0),
            "pool_high_watermark": getattr(resources, "pool_high_watermark", 0),
            "pool_max_connections": getattr(resources, "pool_max_connections", 0),
            "shutdown_deadline_exceeded_total": 0,
        }

    def readiness_status(self) -> dict[str, object]:
        """Return fail-closed orchestration readiness without secret detail."""
        lifecycle = self._runtime_lifecycle.state
        storage_ready = True
        resources = self._postgres_resources
        if resources is not None:
            storage_ready = resources.state == "READY"
        elif lifecycle == "ready":
            try:
                self.store.list_functions(limit=1)
            except Exception:  # noqa: BLE001 - broad catch with explicit fallback handling
                storage_ready = False
        if lifecycle == "ready" and not storage_ready:
            try:
                self._runtime_lifecycle.mark_faulted()
            except RuntimeError as exc:
                logger.debug("suppressed RuntimeError in cleanup/degradation path: %s", exc)
            lifecycle = self._runtime_lifecycle.state
        ready = lifecycle == "ready" and storage_ready
        return {
            "schema_version": 1,
            "status": "ready" if ready else "not_ready",
            "lifecycle": lifecycle,
            "storage": "ready" if storage_ready else "unavailable",
        }

    def begin_draining(self) -> None:
        """Close readiness before any shutdown work begins."""
        if self._runtime_lifecycle.state in {"starting", "ready"}:
            self._runtime_lifecycle.start_draining()

    def _resolve_store_path(self) -> str:
        """Safely resolve the underlying storage path for display.

        Handles both bare stores (LiteMemoryStore) and wrapped stores
        (SyncableStore wrapping a local store) without raising.
        """
        # Try the store directly first.
        p = getattr(self.store, "_path", None)
        if p is not None:
            return str(p)
        # SyncableStore wraps a local store -- access it safely.
        local = getattr(self.store, "local", None)
        if local is not None:
            p = getattr(local, "_path", None)
            if p is not None:
                return str(p)
        return "unknown"

    def _sync_health(self) -> dict:
        """Return sync-layer congestion indicators.

        Operators and adaptive logic read these to decide whether to scale
        (add read-replicas, enable Redis, increase pull interval) or
        degrade (drop SSE connections to polling).
        """
        from memplex.sync import SyncableStore

        info: dict = {"enabled": False}
        if not isinstance(self.store, SyncableStore):
            return info
        store = self.store
        cfg = store._config
        info["enabled"] = cfg.active
        info["sse_enabled"] = cfg.sse_enabled
        info["sse_active"] = store._sse_thread is not None and store._sse_thread.is_alive()
        info["auto_pull_active"] = (
            store._auto_pull_thread is not None and store._auto_pull_thread.is_alive()
        )
        info["push_failures"] = store._push_failures
        info["pending_push_tasks"] = store.pending_push_tasks
        info["last_pull_at"] = store.last_pull_at
        # SSE subscriber count via the domain-owned registration point
        # (adapters register a provider at startup; no reverse import).
        try:
            info["sse_subscribers"] = _sse_subscriber_count_provider() if _sse_subscriber_count_provider else 0
        except Exception:  # noqa: BLE001 - broad catch with explicit fallback handling
            info["sse_subscribers"] = 0
        return info

    def stats(self) -> dict:
        """Return storage and usage statistics."""
        try:
            total = self._count_functions_exact(self.store)
        except Exception as exc:  # noqa: BLE001 - logged degradation path
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
        results: list[SearchResult],
        *,
        max_tokens: int | None = None,
        authorization: AuthorizationContext | None = None,
    ) -> str | None:
        """Filter injection-suspected results and wrap the rest for LLM context.

        Wraps :meth:`IndirectInjectionGuard.filter_and_wrap` so adapters
        (notably ``agent_runtime``) do not import the guard directly. Keeps
        the read-path injection defence behind the service boundary.

        The guard is handed the typed-node facade so Fact/Preference hits
        (resolvable via the optional ``get_fact`` / ``get_preference``
        store APIs) are wrapped for context instead of being dropped as
        unresolvable ids.
        """
        context = self._require_authorization(authorization)
        return IndirectInjectionGuard.filter_and_wrap(
            results,
            self._typed_lookup_for(context),
            risk_registry=self._injection_risks,
        )

    # ── Knowledge tiering: promotion + cross-agent sharing ───────────

    _KNOWLEDGE_TIERS = frozenset({"personal", "domain", "team"})

    def promote(
        self,
        memory_id: str,
        tier: str,
        *,
        authorization: AuthorizationContext | None = None,
    ) -> dict[str, object]:
        """Promote one memory into a curated knowledge tier.

        The memory→knowledge lifecycle: a captured node (tier ``None``)
        gains ``knowledge_tier`` ∈ {personal, domain, team} and the read
        scope implied by that tier — ``team`` widens visibility to the
        workspace so every member agent recalls it. Promotion is
        provenance-stamped (who promoted, when) and idempotent.

        Authorization: only the memory owner (or local development) may
        promote — a cross-agent **grant holder can read but never widen**
        (V1 fix: promoting someone else's private memory to team would
        leak it workspace-wide through a read-only grant).
        """
        if tier not in self._KNOWLEDGE_TIERS:
            raise ValueError(f"tier must be one of {sorted(self._KNOWLEDGE_TIERS)}")
        context = self._require_authorization(authorization)
        node = self._require_visible_node(memory_id, context)

        owner_subject = self._identity_value(node, "owner_subject_id", "memplex_subject_id")
        is_owner = owner_subject == context.principal.subject_id
        if not (is_owner or self._is_local_development_context(context)):
            raise PermissionError(
                "only the memory owner can promote to a knowledge tier"
            )

        node.knowledge_tier = tier
        if tier == "team":
            # Team knowledge is workspace-shared by definition.
            node.visibility = "workspace"
        node.version = getattr(node, "version", 1) + 1
        provenance = dict(getattr(node, "provenance", {}) or {})
        provenance["promoted_by"] = context.principal.subject_id
        provenance["promoted_at"] = datetime.now(UTC).isoformat()
        provenance["promoted_to_tier"] = tier
        node.provenance = provenance

        store = self._store_for(context)
        add = getattr(store, "add_fact", None)
        if callable(add) and getattr(node, "memory_type", "") == "fact":
            add(node)
        else:
            add_fn = getattr(store, "add", None)
            if callable(add_fn):
                from memplex.models import SourceDocument

                add_fn(node, SourceDocument(
                    type="promotion", content="", source_type=node.source_type
                ))
        return {
            "memory_id": memory_id,
            "tier": tier,
            "visibility": node.visibility,
            "version": node.version,
        }

    def share_with(
        self,
        memory_id: str,
        agent_id: str,
        *,
        authorization: AuthorizationContext | None = None,
    ) -> dict[str, object]:
        """Grant one named agent read access to a user-private memory.

        Cross-agent directed sharing for the multi-agent team model: the
        grant lands in the node namespace (``memplex_grants``) and the
        authorization gate honours it for ``visibility="user"`` nodes.
        Only the owner (or local development) may share; grants are
        additive and idempotent.
        """
        if not agent_id or not str(agent_id).strip():
            raise ValueError("agent_id must be a non-empty string")
        agent_id = str(agent_id).strip()
        # V2 fix: the grant store is a comma-joined list in the node
        # namespace; a comma/whitespace-bearing id would silently split
        # into multiple grants at read time.
        if any(ch in agent_id for ch in ",\t\n\r"):
            raise ValueError("agent_id must not contain commas or whitespace")
        context = self._require_authorization(authorization)
        node = self._require_visible_node(memory_id, context)

        owner_subject = self._identity_value(node, "owner_subject_id", "memplex_subject_id")
        is_owner = owner_subject == context.principal.subject_id
        if not (is_owner or self._is_local_development_context(context)):
            raise PermissionError("only the memory owner can share a private memory")

        namespace = dict(getattr(node, "namespace", {}) or {})
        grants = [
            part.strip()
            for part in str(namespace.get("memplex_grants", "")).split(",")
            if part.strip()
        ]
        if agent_id not in grants:
            grants.append(agent_id)
        namespace["memplex_grants"] = ",".join(sorted(set(grants)))
        node.namespace = namespace

        store = self._store_for(context)
        add = getattr(store, "add_fact", None)
        if callable(add) and getattr(node, "memory_type", "") == "fact":
            add(node)
        return {"memory_id": memory_id, "granted_agents": grants}

    def improve(
        self,
        *,
        authorization: AuthorizationContext | None = None,
    ) -> dict[str, object]:
        """Run the proactive maintenance pass (Cognee ``improve``-style).

        Dedupes contradicting facts into bi-temporal history, expires
        shelf-lapsed facts, and rebuilds the search index. Read-only with
        respect to Functions/Preferences — this is the fact-layer
        counterpart to :meth:`compact`. See ``memplex/improve.py``.
        """
        from memplex.improve import improve_facts

        context = self._require_authorization(authorization)
        store = self._store_for(context)
        report = improve_facts(store)
        # Graph-builder cache must not serve pre-maintenance adjacency.
        self._graph_builder.invalidate_cache()
        return report

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
        self._worker._last_compaction = datetime.now(UTC)
        return result

    def start(self) -> None:
        """Start the background worker thread (+ auto-pull/SSE if configured)."""
        if self._runtime_lifecycle.state == "ready":
            return
        try:
            self._worker.start()
            if self._sync_dispatcher is not None:
                self._sync_dispatcher.start()
            # If the store is sync-enabled, start the periodic pull thread and
            # the SSE push-notification listener so this node stays current with
            # the central server without manual 'sync sync pull'.
            from memplex.sync import SyncableStore

            if isinstance(self.store, SyncableStore):
                self.store.start_auto_pull()
                self.store.start_sse_listener()
            if self._config.sleep_time.enabled:
                self._sleep_time.start()
            self._runtime_lifecycle.mark_ready()
        except BaseException:
            if self._runtime_lifecycle.state == "starting":
                self._runtime_lifecycle.mark_faulted()
            raise

    def stop(self, *, drain_sync: bool = True) -> dict[str, object]:
        """Stop the background worker thread (+ auto-pull/SSE if running).

        Pending async sync pushes are flushed first so writes accepted
        before shutdown reach the remote instead of dying with the
        process.
        """
        with self._lifecycle_condition:
            if self._service_stop_state == "stopped":
                assert self._service_stop_result is not None
                return self._service_stop_result
            if self._service_stop_state == "faulted":
                assert self._service_stop_error is not None
                raise self._service_stop_error
            if self._service_stop_state == "stopping":
                while self._service_stop_state == "stopping":
                    self._lifecycle_condition.wait()
                if self._service_stop_state == "faulted":
                    assert self._service_stop_error is not None
                    raise self._service_stop_error
                assert self._service_stop_result is not None
                return self._service_stop_result
            self._service_stop_state = "stopping"
            self.begin_draining()
        try:
            self._sleep_time.stop()
            result = self._stop_runtime(drain_sync=drain_sync)
        except BaseException as exc:
            if self._runtime_lifecycle.state == "draining":
                self._runtime_lifecycle.mark_faulted()
            with self._lifecycle_condition:
                self._service_stop_error = exc
                self._service_stop_state = "faulted"
                self._lifecycle_condition.notify_all()
            raise
        with self._lifecycle_condition:
            if self._runtime_lifecycle.state == "draining":
                self._runtime_lifecycle.mark_stopped()
            self._service_stop_result = result
            self._service_stop_state = "stopped"
            self._lifecycle_condition.notify_all()
            return result

    def _stop_runtime(self, *, drain_sync: bool) -> dict[str, object]:
        """Perform the single-owner shutdown sequence used by :meth:`stop`."""
        from memplex.sync import SyncableStore

        primary_error: BaseException | None = None
        sync_result = None
        worker_result = None
        try:
            if (
                self._sync_dispatcher is not None
                and not drain_sync
                and self._sync_dispatcher.running
            ):
                raise RuntimeError(
                    "cannot skip sync drain after dispatcher start"
                )
            if self._sync_dispatcher is not None and drain_sync:
                sync_result = self._sync_dispatcher.stop(
                    self._config.sync.drain_timeout_seconds
                )
            if isinstance(self.store, SyncableStore):
                self.store.flush_push()
                self.store.stop_sse_listener()
                self.store.stop_auto_pull()
            worker_result = self._worker.stop(
                timeout=self._config.worker.drain_timeout_seconds
            )
        except BaseException as exc:  # noqa: BLE001 - shutdown/cleanup semantics; primary error stays authoritative
            primary_error = exc
        try:
            self._close_postgres_resources_once()
        except BaseException:
            if primary_error is None:
                raise
        if primary_error is not None:
            raise primary_error
        return {
            "sync": None if sync_result is None else sync_result.to_dict(),
            "worker": None if worker_result is None else worker_result.to_dict(),
        }

    def sync_status(self) -> dict[str, int]:
        """Return durable sync counters without exposing peer transport data."""
        if self._sync_dispatcher is None:
            return {
                "pending": 0,
                "leased": 0,
                "delivered": 0,
                "disabled_targets": 0,
                "dead_letters": 0,
            }
        return self._sync_dispatcher.status().to_dict()

    def drain_sync(self, deadline: float | None = None) -> SyncDrainResult:
        """Synchronously drain local-origin durable deliveries."""
        if self._sync_dispatcher is None:
            from memplex.sync_protocol import SyncDrainResult

            return SyncDrainResult(True, 0, 0, 0, 0, False)
        timeout = (
            self._config.sync.drain_timeout_seconds
            if deadline is None
            else deadline
        )
        return self._sync_dispatcher.drain(timeout)

    def replay_sync_dead_letter(self, target_id: str, event_id: str) -> bool:
        """Replay one durable terminal delivery by stable peer identity."""
        if self._sync_dispatcher is None:
            return False
        return self._sync_dispatcher.replay(target_id, event_id)

    def list_sync_dead_letters(self, *, limit: int = 100) -> list[dict[str, object]]:
        """List fixed-code DLQ entries without peer URLs or exceptions."""
        if self._sync_dispatcher is None:
            return []
        return [
            item.to_dict()
            for item in self._sync_dispatcher.list_dead_letters(limit=limit)
        ]

    def pull_sync(
        self,
        target_id: str,
        *,
        max_pages: int = 100,
    ) -> PullResult:
        """Pull bounded signed pages from one configured peer."""
        if self._sync_dispatcher is None:
            raise RuntimeError("sync dispatcher is not configured")
        return self._sync_dispatcher.pull(
            target_id,
            max_pages=max_pages,
            page_size=self._config.sync.page_size,
        )

    # ── Memory type detection ─────────────────────────────────────
    # Exposed as a bound staticmethod for backward-compatible callers
    # (e.g. ``MemplexService._detect_memory_type(text)``); the real
    # implementation lives at module scope above.
    _detect_memory_type = staticmethod(_detect_memory_type)
