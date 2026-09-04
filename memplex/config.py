"""Memplex configuration system.

Supports loading from YAML files, environment variable overrides (MEMPLEX_*),
and sensible defaults when no configuration is found.

Priority: MEMPLEX_* env vars > config.yaml > defaults
"""

import json
import logging
import math
import os
import shlex
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

logger = logging.getLogger(__name__)

_POSTGRES_IDENTITY_FIELDS = (
    "host",
    "hostaddr",
    "port",
    "dbname",
    "user",
    "service",
)
_POSTGRES_DSN_KEYS = frozenset(
    {
        "application_name",
        "channel_binding",
        "client_encoding",
        "connect_timeout",
        "dbname",
        "fallback_application_name",
        "gssencmode",
        "host",
        "hostaddr",
        "keepalives",
        "keepalives_count",
        "keepalives_idle",
        "keepalives_interval",
        "krbsrvname",
        "options",
        "passfile",
        "password",
        "port",
        "replication",
        "requirepeer",
        "requiressl",
        "service",
        "sslcert",
        "sslcompression",
        "sslcrl",
        "sslcrldir",
        "sslkey",
        "sslmode",
        "sslpassword",
        "sslrootcert",
        "sslsni",
        "target_session_attrs",
        "tcp_user_timeout",
        "user",
    }
)


def _parse_postgres_dsn_without_driver(value: str) -> dict[str, str]:
    if value.startswith(("postgres://", "postgresql://")):
        try:
            parsed_url = urlsplit(value)
            query_items = parse_qsl(
                parsed_url.query,
                keep_blank_values=True,
                strict_parsing=True,
            )
            parsed: dict[str, str] = {}
            for key, item in query_items:
                if key not in _POSTGRES_DSN_KEYS or key in parsed:
                    raise ValueError
                parsed[key] = item
            if parsed_url.fragment:
                raise ValueError
            if parsed_url.username is not None:
                if "user" in parsed:
                    raise ValueError
                parsed["user"] = unquote(parsed_url.username)
            if parsed_url.password is not None:
                if "password" in parsed:
                    raise ValueError
                parsed["password"] = unquote(parsed_url.password)
            if parsed_url.hostname is not None:
                if "host" in parsed:
                    raise ValueError
                parsed["host"] = parsed_url.hostname
            if parsed_url.port is not None:
                if "port" in parsed:
                    raise ValueError
                parsed["port"] = str(parsed_url.port)
            if parsed_url.path not in {"", "/"}:
                if "dbname" in parsed:
                    raise ValueError
                parsed["dbname"] = unquote(parsed_url.path[1:])
        except (TypeError, ValueError) as exc:
            raise ValueError("PostgreSQL DSN is invalid") from exc
    else:
        try:
            tokens = shlex.split(value, posix=True)
        except ValueError as exc:
            raise ValueError("PostgreSQL DSN is invalid") from exc
        parsed = {}
        for token in tokens:
            if "=" not in token:
                raise ValueError("PostgreSQL DSN is invalid")
            key, item = token.split("=", 1)
            if not key or key not in _POSTGRES_DSN_KEYS or key in parsed:
                raise ValueError("PostgreSQL DSN is invalid")
            parsed[key] = item
    if not parsed or any("\x00" in item for item in parsed.values()):
        raise ValueError("PostgreSQL DSN is invalid")
    return parsed


def postgres_dsn_identity(value: object) -> tuple[str | None, ...]:
    """Return a secret-free canonical PostgreSQL connection identity.

    Production validation uses libpq's parser rather than accepting any
    non-empty string. Passwords and non-identity connection options are
    deliberately excluded from the result and from every error message.
    """

    if type(value) is not str or not value or value != value.strip():
        raise ValueError("PostgreSQL DSN must be a non-empty exact string")
    try:
        from psycopg2.extensions import parse_dsn  # type: ignore
    except ImportError:
        parsed = _parse_postgres_dsn_without_driver(value)
    else:
        try:
            parsed = parse_dsn(value)
        except Exception as exc:
            raise ValueError("PostgreSQL DSN is invalid") from exc
        if type(parsed) is not dict:
            raise ValueError("PostgreSQL DSN is invalid")
    identity = dict(parsed)
    if identity.get("host") and not identity.get("port"):
        identity["port"] = "5432"
    return tuple(identity.get(field) for field in _POSTGRES_IDENTITY_FIELDS)


def validate_sync_remote_url(
    remote_url: str, *, profile: str = "development"
) -> str:
    """Validate and normalize one sync transport URL without exposing it."""
    if type(remote_url) is not str or not remote_url or remote_url != remote_url.strip():
        raise ValueError("sync remote URL must be a non-empty exact string")
    if type(profile) is not str:
        raise TypeError("deployment profile must be a string")
    normalized = remote_url.rstrip("/")
    try:
        parsed = urlsplit(normalized)
    except ValueError as exc:
        raise ValueError("sync remote URL is invalid") from exc
    hostname = parsed.hostname
    is_loopback = hostname in {"localhost", "127.0.0.1", "::1"}
    if (
        parsed.username is not None
        or parsed.password is not None
        or not hostname
        or bool(parsed.query)
        or bool(parsed.fragment)
    ):
        raise ValueError("sync remote URL is invalid")
    if parsed.scheme == "https":
        return normalized
    if (
        parsed.scheme == "http"
        and is_loopback
        and profile.strip().lower() == "development"
    ):
        return normalized
    raise ValueError("sync remote URL must use HTTPS outside development loopback")

# ── Sub-configurations ──────────────────────────────────────────


@dataclass
class StorageConfig:
    """Storage backend configuration.

    ``"lite"`` provides single-process development storage (JSON persistence
    with a SQLite FTS5 sidecar index); ``"postgres"`` is the supported
    production backend. ``"standard"`` and ``"enterprise"`` are reserved
    roadmap names that currently map to Lite for development compatibility.
    """

    backend: str = "lite"  # lite (development) | postgres (production)
    path: str = field(default="~/.memplex", repr=False)
    migration_dsn: str | None = field(default=None, repr=False)
    inbound_dsn: str | None = field(default=None, repr=False)


@dataclass
class EmbeddingConfig:
    """Embedding model configuration."""

    model: str = "default"  # default=local, optional local ONNX; hf:<id>=HF
    dimension: int = 384
    batch_size: int = 32
    contextual_retrieval: bool = True
    hyde_enabled: bool = True


@dataclass
class RerankerConfig:
    """Reranker scoring configuration."""

    weights: dict[str, float] = field(
        default_factory=lambda: {
            "raw_relevance": 0.25,
            "semantic_similarity": 0.30,
            "recency_decay": 0.15,
            "source_authority": 0.10,
            "frequency": 0.10,
            "confidence": 0.10,
        }
    )
    # Exponential recency half-life in days: score = exp(-days / halflife).
    # Default 60 (~0.61 at 30 days); Mnemosyne-style tunable knob.
    recency_halflife_days: float = 60.0
    cross_encoder_enabled: bool = False
    cross_encoder_model: str = "BAAI/bge-reranker-v2-m3"


@dataclass
class AgentDomainConfig:
    """Agent→domain binding for domain-scoped recall.

    Maps agent names to the knowledge domains they own or serve; the
    agent runtime ANDs a ``domain ∈ bound`` filter into every recall so a
    domain agent only sees its domain's knowledge. An empty list (the
    default) means unscoped — the agent sees everything its ACL allows.
    """

    agent_domains: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class SleepTimeConfig:
    """Idle-time maintenance + inference precompute (memplex/sleep_time.py)."""

    enabled: bool = False
    interval_seconds: float = 3600.0
    idle_grace_seconds: float = 300.0
    precompute_top_k: int = 20


@dataclass
class WorkingMemoryConfig:
    """Hot-context tier (memplex/working_memory.py), opt-in."""

    enabled: bool = False
    max_entries: int = 64
    default_ttl_seconds: float = 900.0
    inject_limit: int = 8


@dataclass
class CompactionConfig:
    """Compaction pipeline configuration."""

    dedup_threshold: float = 0.95
    chunk_threshold: int = 20000
    warn_threshold: int = 50000
    hard_limit: int = 500000
    field_max_values: int = 20
    needs_review_ttl_days: int = 30
    prune_confidence_threshold: float = 0.3
    prune_max_age_days: int = 180
    prune_min_access_count: int = 0
    dedup_use_faiss: bool = True


@dataclass
class GraphConfig:
    """Graph configuration.

    ``semantic_similar_threshold`` / ``semantic_similar_max_edges`` govern
    SEMANTIC_SIMILAR edge detection in
    :class:`memplex.processing.graph_builder.GraphBuilder` (only active when
    an embedding service is injected).  ``community_detection_enabled`` /
    ``community_min_size`` govern community detection in
    :class:`memplex.wiki.community.GraphCommunityDetector` as wired by
    :class:`memplex.wiki.compiler.WikiCompiler`.
    """

    semantic_similar_threshold: float = 0.85
    semantic_similar_max_edges: int = 10
    community_detection_enabled: bool = True
    community_min_size: int = 3


@dataclass
class WikiConfig:
    """Wiki layer configuration.

    ``dir`` is the root directory for compiled wiki pages; ``enabled``
    gates wiki compilation (e.g. the worker's COMPILE_WIKI handler skips
    gracefully when ``False``).
    """

    dir: str = "~/.memplex/wiki"
    enabled: bool = True


@dataclass
class RetrievalConfig:
    """Retrieval configuration.

    ``retrieval_budget_multiplier`` decouples the candidate budget from the
    caller-facing ``top_k``: each query fans out with
    ``max(top_k * multiplier, top_k)`` candidates so merge/rerank see more
    than the final ``results[:top_k]`` window. ``max_retrieval_budget`` is
    the server-side cost ceiling the derived budget is clamped to.
    """

    default_max_tokens: int = 4000
    skill_max_tokens: int = 2000
    injection_scan_enabled: bool = True
    retrieval_budget_multiplier: int = 4
    max_retrieval_budget: int = 500
    # Bounded graph traversal depth for the graph retrieval path. 1 keeps
    # the historical seed+one-hop behaviour; 2 admits two-hop neighbours
    # under the same budget ceiling. This is a bounded expansion knob --
    # it is not a claim of generic multi-hop reasoning.
    graph_max_hops: int = 1

    def __post_init__(self) -> None:
        for name in ("retrieval_budget_multiplier", "max_retrieval_budget"):
            value = getattr(self, name)
            if type(value) is not int:
                raise TypeError(f"retrieval.{name} must be an exact int")
            if value <= 0:
                raise ValueError(f"retrieval.{name} must be positive")
        hops = self.graph_max_hops
        if type(hops) is not int or not 1 <= hops <= 2:
            raise ValueError("retrieval.graph_max_hops must be 1 or 2")


@dataclass
class LLMConfig:
    """LLM provider configuration."""

    query_enhancement: bool = True
    observation_compression: bool = True
    # retain()-style factual capture on write (coreference resolution +
    # temporal normalisation); requires a real LLM provider, off by default.
    factual_capture: bool = False
    provider: str = "anthropic"
    anthropic_api_key: str | None = None  # falls back to ANTHROPIC_API_KEY env var
    local_endpoint: str | None = None
    local_model: str | None = None
    fallback_chain: list[str] = field(default_factory=lambda: ["anthropic"])
    max_input_length: int = 10000


@dataclass
class LoggingConfig:
    """Logging configuration."""

    level: str = "INFO"


@dataclass
class DeploymentConfig:
    """Deployment support contract.

    ``development`` preserves the local-first developer experience. The
    ``production`` profile is deliberately narrower: PostgreSQL is the only
    supported production persistence backend. Additional production gates
    are reported by ``memplex readiness`` and are closed by later industrial
    hardening work.
    """

    profile: str = "development"  # development | production


@dataclass
class SyncConfig:
    """可靠同步的冻结容量与 cursor 签名配置。

    密钥和未来 remote URL 都不能通过 ``repr`` 泄露；网络调用方必须先经
    :meth:`validate_remote_url` 校验，不能把未校验 URL 交给 HTTP client。
    """

    enabled: bool = False
    node_id: str = ""
    targets: dict[str, str] = field(default_factory=dict, repr=False)
    max_batch_events: int = 1000
    max_batch_bytes: int = 4 * 1024 * 1024
    page_size: int = 500
    max_page_size: int = 1000
    claim_size: int = 100
    max_in_flight: int = 4
    per_target_in_flight: int = 1
    max_pending_events: int = 100000
    max_attempts: int = 8
    lease_seconds: int = 30
    drain_timeout_seconds: int = 30
    cursor_ttl_seconds: int = 900
    cursor_signing_key_id: str = ""
    cursor_signing_secret: str = field(default="", repr=False)
    cursor_previous_signing_keys: dict[str, str] = field(default_factory=dict, repr=False)
    consumer_ttl_seconds: int = 86400
    retention_min_seconds: int = 86400
    max_snapshot_items: int = 1000000
    max_active_snapshots_per_tenant: int = 2
    max_active_snapshots_per_remote: int = 1
    snapshot_create_timeout_seconds: int = 30

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if type(self.enabled) is not bool:
            raise TypeError("sync.enabled must be bool")
        positive_ints = (
            "max_batch_events", "max_batch_bytes", "page_size", "max_page_size",
            "claim_size", "max_in_flight", "per_target_in_flight",
            "max_pending_events", "max_attempts", "lease_seconds",
            "drain_timeout_seconds", "cursor_ttl_seconds", "consumer_ttl_seconds",
            "retention_min_seconds", "max_snapshot_items",
            "max_active_snapshots_per_tenant", "max_active_snapshots_per_remote",
            "snapshot_create_timeout_seconds",
        )
        for name in positive_ints:
            value = getattr(self, name)
            if type(value) is not int:
                raise TypeError(f"sync.{name} must be an exact int")
            if value <= 0:
                raise ValueError(f"sync.{name} must be positive")
        if self.page_size > self.max_page_size:
            raise ValueError("sync.page_size cannot exceed sync.max_page_size")
        self._validate_protocol_hard_caps()
        for name in ("node_id", "cursor_signing_key_id", "cursor_signing_secret"):
            value = getattr(self, name)
            if type(value) is not str:
                raise TypeError(f"sync.{name} must be a string")
        if type(self.targets) is not dict:
            raise TypeError("sync.targets must be an exact dict")
        if self.targets and not self.node_id:
            raise ValueError("sync targets require a non-empty local node_id")
        seen_urls: set[str] = set()
        detached_targets: dict[str, str] = {}
        for remote_node_id, remote_url in self.targets.items():
            if (
                type(remote_node_id) is not str
                or not remote_node_id
                or remote_node_id != remote_node_id.strip()
            ):
                raise ValueError(
                    "sync.targets keys must be exact non-empty remote node ids"
                )
            if remote_node_id == self.node_id:
                raise ValueError("sync target cannot be the local node_id")
            if (
                type(remote_url) is not str
                or not remote_url
                or remote_url != remote_url.strip()
            ):
                raise ValueError(
                    "sync.targets values must be exact non-empty transport URLs"
                )
            if remote_url in seen_urls:
                raise ValueError(
                    "one transport URL cannot identify multiple remote nodes"
                )
            seen_urls.add(remote_url)
            detached_targets[remote_node_id] = remote_url
        self.targets = detached_targets
        if type(self.cursor_previous_signing_keys) is not dict:
            raise TypeError("sync.cursor_previous_signing_keys must be an exact dict")
        for key_id, secret in self.cursor_previous_signing_keys.items():
            if type(key_id) is not str or not key_id or type(secret) is not str or not secret:
                raise ValueError("sync previous signing keys must use non-empty string ids and secrets")
            if len(secret.encode("utf-8")) < 32:
                raise ValueError("sync previous signing secrets must be at least 32 bytes")
            if key_id == self.cursor_signing_key_id:
                raise ValueError("sync previous signing keys cannot replace the active key")
        if self.enabled:
            if not self.node_id:
                raise ValueError("enabled sync requires a non-empty sync.node_id")
            if not self.cursor_signing_key_id:
                raise ValueError("enabled sync requires a cursor signing key id")
            if len(self.cursor_signing_secret.encode("utf-8")) < 32:
                raise ValueError("enabled sync requires a cursor signing secret of at least 32 bytes")

    def _validate_protocol_hard_caps(self) -> None:
        """Enforce the sync protocol's fixed wire-format ceilings."""
        if self.max_batch_events > 1000:
            raise ValueError("sync.max_batch_events cannot exceed protocol hard cap 1000")
        if self.max_batch_bytes > 4 * 1024 * 1024:
            raise ValueError("sync.max_batch_bytes cannot exceed protocol hard cap 4MiB")
        if self.max_page_size > 1000:
            raise ValueError("sync.max_page_size cannot exceed protocol hard cap 1000")

    def validate_remote_url(self, remote_url: str, *, profile: str = "development") -> str:
        """Validate a remote before a network client sees it.

        HTTPS is mandatory except for explicit development loopback endpoints;
        URLs containing userinfo are never accepted because they are too easy
        to expose in diagnostics or a connection error.
        """
        return validate_sync_remote_url(remote_url, profile=profile)


@dataclass
class WorkerConfig:
    """后台 worker 的独立、硬上界配置。"""

    queue_capacity: int = 1000
    claim_size: int = 32
    max_attempts: int = 3
    lease_seconds: int = 60
    drain_timeout_seconds: int = 30

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        for name in ("queue_capacity", "claim_size", "max_attempts", "lease_seconds", "drain_timeout_seconds"):
            value = getattr(self, name)
            if type(value) is not int:
                raise TypeError(f"worker.{name} must be an exact int")
            if value <= 0:
                raise ValueError(f"worker.{name} must be positive")


@dataclass
class BackupConfig:
    """Backup/restore bounds; signing key material is never stored here."""

    directory: str = "~/.memplex/backups"
    key_id: str = ""
    max_artifact_bytes: int = 64 * 1024 * 1024 * 1024
    restore_timeout_seconds: int = 3600
    rpo_target_seconds: int = 300
    rto_target_seconds: int = 1800

    def __post_init__(self) -> None:
        if type(self.directory) is not str or not self.directory:
            raise ValueError("backup.directory must be a non-empty exact string")
        if type(self.key_id) is not str:
            raise TypeError("backup.key_id must be an exact string")
        for name in (
            "max_artifact_bytes",
            "restore_timeout_seconds",
            "rpo_target_seconds",
            "rto_target_seconds",
        ):
            value = getattr(self, name)
            if type(value) is not int:
                raise TypeError(f"backup.{name} must be an exact int")
            if value <= 0:
                raise ValueError(f"backup.{name} must be positive")


@dataclass
class OperationsConfig:
    """生产启动、停机和 SLO 的固定边界；签名密钥保持 env-only。"""

    request_drain_timeout_seconds: int = 15
    availability_target: float = 0.999
    p95_latency_target_ms: float = 250.0
    error_rate_target: float = 0.001
    report_key_id: str = ""

    def __post_init__(self) -> None:
        for name in ("request_drain_timeout_seconds",):
            value = getattr(self, name)
            if type(value) is not int:
                raise TypeError(f"operations.{name} must be an exact int")
            if value <= 0:
                raise ValueError(f"operations.{name} must be positive")
        for name in ("availability_target", "error_rate_target"):
            value = getattr(self, name)
            if type(value) is not float:
                raise TypeError(f"operations.{name} must be an exact float")
            if not math.isfinite(value) or value < 0.0 or value > 1.0:
                raise ValueError(f"operations.{name} must be between zero and one")
        if type(self.p95_latency_target_ms) is not float:
            raise TypeError("operations.p95_latency_target_ms must be an exact float")
        if not math.isfinite(self.p95_latency_target_ms) or self.p95_latency_target_ms <= 0.0:
            raise ValueError("operations.p95_latency_target_ms must be positive")
        if type(self.report_key_id) is not str:
            raise TypeError("operations.report_key_id must be an exact string")


# ── Top-level configuration ─────────────────────────────────────


@dataclass
class MemplexConfig:
    """Top-level Memplex configuration.

    Collects all sub-configurations into a single object.
    Use ``load_config()`` to create an instance.
    """

    storage: StorageConfig = field(default_factory=StorageConfig)
    embedding: EmbeddingConfig = field(default_factory=EmbeddingConfig)
    reranker: RerankerConfig = field(default_factory=RerankerConfig)
    working_memory: WorkingMemoryConfig = field(default_factory=WorkingMemoryConfig)
    sleep_time: SleepTimeConfig = field(default_factory=SleepTimeConfig)
    agent_domains: AgentDomainConfig = field(default_factory=AgentDomainConfig)
    compaction: CompactionConfig = field(default_factory=CompactionConfig)
    graph: GraphConfig = field(default_factory=GraphConfig)
    wiki: WikiConfig = field(default_factory=WikiConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    deployment: DeploymentConfig = field(default_factory=DeploymentConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    backup: BackupConfig = field(default_factory=BackupConfig)
    operations: OperationsConfig = field(default_factory=OperationsConfig)


# ── Env-var mapping ─────────────────────────────────────────────

# Maps ``MEMPLEX_<SECTION>_<KEY>`` environment variables to
# ``(MemplexConfig field, sub-config field)`` pairs.
#
# Example: ``MEMPLEX_STORAGE_BACKEND=enterprise`` sets
# ``config.storage.backend = "enterprise"``.
#
# Keys not listed here are still resolved dynamically in
# ``_apply_env_overrides()`` using the same naming convention.

_ENV_TYPE_COERCIONS: dict[str, type] = {
    # StorageConfig
    "storage.backend": str,
    "storage.path": str,
    "storage.migration_dsn": str,
    "storage.inbound_dsn": str,
    # EmbeddingConfig
    "embedding.model": str,
    "embedding.dimension": int,
    "embedding.batch_size": int,
    "embedding.contextual_retrieval": bool,
    "embedding.hyde_enabled": bool,
    # RerankerConfig
    "reranker.cross_encoder_enabled": bool,
    "reranker.cross_encoder_model": str,
    "reranker.recency_halflife_days": float,
    # CompactionConfig
    "compaction.dedup_threshold": float,
    "compaction.chunk_threshold": int,
    "compaction.warn_threshold": int,
    "compaction.hard_limit": int,
    "compaction.field_max_values": int,
    "compaction.needs_review_ttl_days": int,
    "compaction.prune_confidence_threshold": float,
    "compaction.prune_max_age_days": int,
    "compaction.prune_min_access_count": int,
    "compaction.dedup_use_faiss": bool,
    # GraphConfig
    "graph.semantic_similar_threshold": float,
    "graph.semantic_similar_max_edges": int,
    "graph.community_detection_enabled": bool,
    "graph.community_min_size": int,
    # WikiConfig
    "wiki.dir": str,
    "wiki.enabled": bool,
    # RetrievalConfig
    "retrieval.default_max_tokens": int,
    "retrieval.skill_max_tokens": int,
    "retrieval.injection_scan_enabled": bool,
    "retrieval.retrieval_budget_multiplier": int,
    "retrieval.graph_max_hops": int,
    "retrieval.max_retrieval_budget": int,
    # LLMConfig
    "llm.query_enhancement": bool,
    "llm.factual_capture": bool,
    "working_memory.enabled": bool,
    "working_memory.max_entries": int,
    "working_memory.default_ttl_seconds": float,
    "working_memory.inject_limit": int,
    "sleep_time.enabled": bool,
    "sleep_time.interval_seconds": float,
    "sleep_time.idle_grace_seconds": float,
    "sleep_time.precompute_top_k": int,
    "llm.observation_compression": bool,
    "llm.provider": str,
    "llm.anthropic_api_key": str,
    "llm.local_endpoint": str,
    "llm.local_model": str,
    "llm.max_input_length": int,
    # LoggingConfig
    "logging.level": str,
    # DeploymentConfig
    "deployment.profile": str,
    # SyncConfig (previous-key map is parsed separately to avoid lossy coercion)
    "sync.enabled": bool,
    "sync.node_id": str,
    "sync.max_batch_events": int,
    "sync.max_batch_bytes": int,
    "sync.page_size": int,
    "sync.max_page_size": int,
    "sync.claim_size": int,
    "sync.max_in_flight": int,
    "sync.per_target_in_flight": int,
    "sync.max_pending_events": int,
    "sync.max_attempts": int,
    "sync.lease_seconds": int,
    "sync.drain_timeout_seconds": int,
    "sync.cursor_ttl_seconds": int,
    "sync.cursor_signing_key_id": str,
    "sync.cursor_signing_secret": str,
    "sync.consumer_ttl_seconds": int,
    "sync.retention_min_seconds": int,
    "sync.max_snapshot_items": int,
    "sync.max_active_snapshots_per_tenant": int,
    "sync.max_active_snapshots_per_remote": int,
    "sync.snapshot_create_timeout_seconds": int,
    # WorkerConfig
    "worker.queue_capacity": int,
    "worker.claim_size": int,
    "worker.max_attempts": int,
    "worker.lease_seconds": int,
    "worker.drain_timeout_seconds": int,
    # BackupConfig (HMAC key is intentionally environment-only, outside config)
    "backup.directory": str,
    "backup.key_id": str,
    "backup.max_artifact_bytes": int,
    "backup.restore_timeout_seconds": int,
    "backup.rpo_target_seconds": int,
    "backup.rto_target_seconds": int,
    # OperationsConfig (HMAC key is intentionally environment-only)
    "operations.request_drain_timeout_seconds": int,
    "operations.availability_target": float,
    "operations.p95_latency_target_ms": float,
    "operations.error_rate_target": float,
    "operations.report_key_id": str,
}


def _coerce(value: str, target_type: type) -> Any:
    """Coerce a string value to the target type."""
    if target_type is bool:
        return value.lower() in ("true", "1", "yes", "on")
    if target_type is int:
        return int(value)
    if target_type is float:
        return float(value)
    return value


def _coerce_sync_enabled(value: str) -> bool:
    """Parse the security-sensitive sync switch without legacy bool fallback."""
    if type(value) is not str or not value or value != value.strip():
        raise ValueError("MEMPLEX_SYNC_ENABLED must be an explicit true/false value")
    normalized = value.lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise ValueError("MEMPLEX_SYNC_ENABLED must be an explicit true/false value")


def _apply_env_overrides(config: MemplexConfig) -> None:
    """Apply MEMPLEX_* environment variable overrides to *config*.

    Convention: ``MEMPLEX_{SECTION}_{KEY}`` (upper-cased).

    For the ``weights`` dict in ``reranker``, individual keys can be set via
    ``MEMPLEX_RERANKER_WEIGHTS_RAW_RELEVANCE=0.3``.
    """
    for dotted_path, target_type in _ENV_TYPE_COERCIONS.items():
        parts = dotted_path.split(".")
        section_name = parts[0]
        key_name = parts[1]
        env_key = f"MEMPLEX_{section_name.upper()}_{key_name.upper()}"
        env_value = os.environ.get(env_key)
        if env_value is not None:
            sub_config = getattr(config, section_name)
            coerced = (
                _coerce_sync_enabled(env_value)
                if dotted_path == "sync.enabled"
                else _coerce(env_value, target_type)
            )
            setattr(sub_config, key_name, coerced)

    # Handle reranker.weights sub-dict via MEMPLEX_RERANKER_WEIGHTS_<KEY>
    weights_env_prefix = "MEMPLEX_RERANKER_WEIGHTS_"
    for env_key, env_value in os.environ.items():
        if env_key.startswith(weights_env_prefix):
            weight_key = env_key[len(weights_env_prefix) :].lower()
            try:
                config.reranker.weights[weight_key] = float(env_value)
            except (ValueError, TypeError):
                logger.warning("Invalid weight value for %s: %s", env_key, env_value)

    # Handle LLM fallback_chain via MEMPLEX_LLM_FALLBACK_CHAIN (comma-separated)
    fallback_env = os.environ.get("MEMPLEX_LLM_FALLBACK_CHAIN")
    if fallback_env is not None:
        config.llm.fallback_chain = [s.strip() for s in fallback_env.split(",") if s.strip()]

    # Fall back to the standard ANTHROPIC_API_KEY env var when the
    # MEMPLEX_LLM_ANTHROPIC_API_KEY override was not provided.
    if config.llm.anthropic_api_key is None:
        config.llm.anthropic_api_key = os.environ.get("ANTHROPIC_API_KEY")

    previous_keys_env = os.environ.get("MEMPLEX_SYNC_CURSOR_PREVIOUS_SIGNING_KEYS")
    if previous_keys_env is not None:
        try:
            parsed_previous_keys = json.loads(previous_keys_env)
        except json.JSONDecodeError as exc:
            raise ValueError("MEMPLEX_SYNC_CURSOR_PREVIOUS_SIGNING_KEYS must be JSON object") from exc
        if type(parsed_previous_keys) is not dict:
            raise ValueError("MEMPLEX_SYNC_CURSOR_PREVIOUS_SIGNING_KEYS must be JSON object")
        config.sync.cursor_previous_signing_keys = parsed_previous_keys

    targets_env = os.environ.get("MEMPLEX_SYNC_TARGETS_JSON")
    if targets_env is not None:
        try:
            parsed_targets = json.loads(targets_env)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "MEMPLEX_SYNC_TARGETS_JSON must be a JSON object"
            ) from exc
        if type(parsed_targets) is not dict:
            raise ValueError("MEMPLEX_SYNC_TARGETS_JSON must be a JSON object")
        config.sync.targets = parsed_targets


# ── YAML loading helpers ────────────────────────────────────────


class _ConfigYamlParseError(ValueError):
    """YAML parse failure retained until the deployment profile is known."""


class _ConfigYamlShapeError(ValueError):
    """A present YAML file must have a mapping root for production."""


def _parse_yaml(path: Path) -> dict[str, Any] | None:
    """Try to parse a YAML file; return ``None`` if pyyaml is not available."""
    try:
        import yaml  # optional dependency
    except ImportError:
        logger.warning(
            "PyYAML is not installed; config file %s will be ignored. "
            "Install it with: pip install pyyaml",
            path,
        )
        return None

    if not path.exists():
        return None

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        # Do not include parser excerpts: they can contain DSNs or other
        # credentials.  Production later turns this into a fail-closed error.
        raise _ConfigYamlParseError("configuration YAML could not be parsed") from exc

    if not isinstance(data, dict):
        raise _ConfigYamlShapeError("configuration YAML root must be a mapping")
    return data


def _dict_to_dataclass(cls: type, data: dict[str, Any]) -> Any:
    """Recursively convert a plain dict to a dataclass instance.

    Only keys that match known fields are used; unknown keys are silently
    ignored so that config files with extra sections don't crash.
    """
    known_fields = {f.name for f in fields(cls)}
    kwargs: dict[str, Any] = {}

    for f in fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        field_type: Any = f.type

        # Resolve string annotations to actual types
        if isinstance(field_type, str):
            # field.type is a string like "StorageConfig"
            # Try to resolve from module globals
            import sys

            module = sys.modules.get(cls.__module__, None)
            if module is not None:
                field_type = getattr(module, field_type, None)
            else:
                field_type = None

        # Handle nested dataclasses
        if isinstance(value, dict) and hasattr(field_type, "__dataclass_fields__"):
            kwargs[f.name] = _dict_to_dataclass(field_type, value)
        else:
            kwargs[f.name] = value

    # Filter out keys not in known_fields to avoid TypeError
    kwargs = {k: v for k, v in kwargs.items() if k in known_fields}
    return cls(**kwargs)


# ── Public API ──────────────────────────────────────────────────

_DEFAULT_CONFIG_PATH = Path("~/.memplex/config.yaml").expanduser()


def _unknown_storage_yaml_fields(data: dict[str, Any]) -> tuple[str, ...]:
    """Return only unknown storage field names, never their configured values."""
    storage = data.get("storage")
    if not isinstance(storage, dict):
        return ()
    known = {item.name for item in fields(StorageConfig)}
    return tuple(sorted(str(name) for name in storage if name not in known))


def load_config(path: str | None = None) -> MemplexConfig:
    """Load Memplex configuration.

    Resolution order (highest priority first):
    1. ``MEMPLEX_*`` environment variables
    2. YAML config file at *path* (or ``~/.memplex/config.yaml``)
    3. Built-in defaults

    Parameters
    ----------
    path:
        Explicit path to a YAML config file.  When ``None`` the default
        location ``~/.memplex/config.yaml`` is tried.  If the file does
        not exist, defaults are used.

    Returns
    -------
    MemplexConfig
        Fully resolved configuration object.
    """
    config = MemplexConfig()

    # Resolve config file path
    config_path = Path(path).expanduser() if path else _DEFAULT_CONFIG_PATH

    # Load YAML overlay
    yaml_parse_error = False
    yaml_shape_error = False
    try:
        yaml_data = _parse_yaml(config_path)
    except _ConfigYamlParseError:
        yaml_parse_error = True
        yaml_data = None
        logger.error("Failed to parse config file %s", config_path)
    except _ConfigYamlShapeError:
        yaml_shape_error = True
        yaml_data = None
        logger.error("Config file %s has an invalid root structure", config_path)
    unknown_storage_fields = (
        _unknown_storage_yaml_fields(yaml_data) if yaml_data is not None else ()
    )
    if yaml_data:
        config = _dict_to_dataclass(MemplexConfig, yaml_data)
        logger.debug("Loaded config from %s", config_path)
    else:
        logger.debug(
            "No config file found at %s, using defaults",
            config_path,
        )

    # Apply environment variable overrides (highest priority)
    _apply_env_overrides(config)
    config.sync.validate()
    for remote_url in config.sync.targets.values():
        config.sync.validate_remote_url(
            remote_url,
            profile=config.deployment.profile,
        )
    config.worker.validate()

    profile, backend = normalize_deployment_contract(config)
    if profile == "production" and backend == "postgres":
        if yaml_parse_error:
            raise ValueError("production configuration YAML could not be parsed")
        if yaml_shape_error:
            raise ValueError("production configuration YAML root must be a mapping")
        if unknown_storage_fields:
            raise ValueError(
                "production storage configuration has unknown field(s): "
                + ", ".join(unknown_storage_fields)
            )

    return config


def normalize_deployment_contract(config: MemplexConfig) -> tuple[str, str]:
    """Canonicalize deployment values shared by startup and readiness checks."""

    profile = str(config.deployment.profile).strip().lower()
    backend = str(config.storage.backend).strip().lower()
    config.deployment.profile = profile
    config.storage.backend = backend
    return profile, backend


def validate_deployment_contract(config: MemplexConfig) -> None:
    """Reject deployment topologies that Memplex does not support."""

    profile, backend = normalize_deployment_contract(config)
    if profile not in {"development", "production"}:
        raise ValueError(
            "deployment.profile must be 'development' or 'production', "
            f"got {config.deployment.profile!r}"
        )
    if profile == "production" and backend != "postgres":
        raise ValueError(
            "production deployment requires the postgres storage backend; "
            "Lite is supported only for single-process development"
        )
    if profile == "production":
        if type(config.storage.path) is not str or not config.storage.path.strip():
            raise ValueError("production postgres storage requires a non-empty application DSN")
        if type(config.storage.migration_dsn) is not str or not config.storage.migration_dsn.strip():
            raise ValueError("production postgres storage requires a non-empty migration DSN")
        try:
            application_identity = postgres_dsn_identity(config.storage.path)
        except ValueError as exc:
            raise ValueError("production postgres application DSN is invalid") from exc
        try:
            migration_identity = postgres_dsn_identity(config.storage.migration_dsn)
        except ValueError as exc:
            raise ValueError("production postgres migration DSN is invalid") from exc
        if application_identity == migration_identity:
            raise ValueError(
                "production postgres application and migration identities must be distinct"
            )
    if config.sync.enabled and backend == "postgres":
        if (
            type(config.storage.migration_dsn) is not str
            or not config.storage.migration_dsn.strip()
        ):
            raise ValueError("sync-enabled storage requires a non-empty migration DSN")
        if type(config.storage.inbound_dsn) is not str or not config.storage.inbound_dsn.strip():
            raise ValueError("sync-enabled storage requires a non-empty inbound DSN")
        inbound_dsn = config.storage.inbound_dsn.strip()
        application_dsn = config.storage.path.strip() if type(config.storage.path) is str else None
        migration_dsn = (
            config.storage.migration_dsn.strip()
            if type(config.storage.migration_dsn) is str
            else None
        )
        if inbound_dsn == application_dsn:
            raise ValueError("sync-enabled inbound DSN must be distinct from application DSN")
        if inbound_dsn == migration_dsn:
            raise ValueError("sync-enabled inbound DSN must be distinct from migration DSN")
        if application_dsn == migration_dsn:
            raise ValueError("sync-enabled migration DSN must be distinct from application DSN")
