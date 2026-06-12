"""Memplex configuration system.

Supports loading from YAML files, environment variable overrides (MEMPLEX_*),
and sensible defaults when no configuration is found.

Priority: MEMPLEX_* env vars > config.yaml > defaults
"""

import logging
import os
from dataclasses import dataclass, field, fields
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ── Sub-configurations ──────────────────────────────────────────


@dataclass
class StorageConfig:
    """Storage backend configuration."""

    backend: str = "standard"  # lite | standard | enterprise
    path: str = "~/.memplex"


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

    weights: Dict[str, float] = field(
        default_factory=lambda: {
            "raw_relevance": 0.25,
            "semantic_similarity": 0.30,
            "recency_decay": 0.15,
            "source_authority": 0.15,
            "frequency": 0.15,
        }
    )
    cross_encoder_enabled: bool = False
    cross_encoder_model: str = "BAAI/bge-reranker-v2-m3"


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
    """Graph configuration."""

    semantic_similar_threshold: float = 0.85
    semantic_similar_max_edges: int = 10
    semantic_similar_ttl_days: int = 30
    semantic_similar_sync_on_merge: bool = False
    community_detection_enabled: bool = True
    community_min_size: int = 3


@dataclass
class RetrievalConfig:
    """Retrieval configuration."""

    default_max_tokens: int = 4000
    skill_max_tokens: int = 2000
    injection_scan_enabled: bool = True


@dataclass
class LLMConfig:
    """LLM provider configuration."""

    semantic_extraction: bool = True
    query_enhancement: bool = True
    conflict_resolution: bool = True
    summarization: bool = True
    reranking: bool = True
    provider: str = "anthropic"
    anthropic_api_key: Optional[str] = None
    local_endpoint: Optional[str] = None
    local_model: Optional[str] = None
    fallback_chain: List[str] = field(default_factory=lambda: ["anthropic"])
    max_input_length: int = 10000


@dataclass
class ObservationConfig:
    """Observation rate-limiting configuration."""

    max_per_minute: int = 20


@dataclass
class LoggingConfig:
    """Logging configuration."""

    level: str = "INFO"
    sanitize_sensitive: bool = True


@dataclass
class EncryptionConfig:
    """Encryption configuration."""

    enabled: bool = False
    key_path: str = "~/.memplex/.enc_key"


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
    compaction: CompactionConfig = field(default_factory=CompactionConfig)
    graph: GraphConfig = field(default_factory=GraphConfig)
    retrieval: RetrievalConfig = field(default_factory=RetrievalConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    observation: ObservationConfig = field(default_factory=ObservationConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)
    encryption: EncryptionConfig = field(default_factory=EncryptionConfig)


# ── Env-var mapping ─────────────────────────────────────────────

# Maps ``MEMPLEX_<SECTION>_<KEY>`` environment variables to
# ``(MemplexConfig field, sub-config field)`` pairs.
#
# Example: ``MEMPLEX_STORAGE_BACKEND=enterprise`` sets
# ``config.storage.backend = "enterprise"``.
#
# Keys not listed here are still resolved dynamically in
# ``_apply_env_overrides()`` using the same naming convention.

_ENV_TYPE_COERCIONS: Dict[str, type] = {
    # StorageConfig
    "storage.backend": str,
    "storage.path": str,
    # EmbeddingConfig
    "embedding.model": str,
    "embedding.dimension": int,
    "embedding.batch_size": int,
    "embedding.contextual_retrieval": bool,
    "embedding.hyde_enabled": bool,
    # RerankerConfig
    "reranker.cross_encoder_enabled": bool,
    "reranker.cross_encoder_model": str,
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
    "graph.semantic_similar_ttl_days": int,
    "graph.semantic_similar_sync_on_merge": bool,
    "graph.community_detection_enabled": bool,
    "graph.community_min_size": int,
    # RetrievalConfig
    "retrieval.default_max_tokens": int,
    "retrieval.skill_max_tokens": int,
    "retrieval.injection_scan_enabled": bool,
    # LLMConfig
    "llm.semantic_extraction": bool,
    "llm.query_enhancement": bool,
    "llm.conflict_resolution": bool,
    "llm.summarization": bool,
    "llm.reranking": bool,
    "llm.provider": str,
    "llm.anthropic_api_key": str,
    "llm.local_endpoint": str,
    "llm.local_model": str,
    "llm.max_input_length": int,
    # ObservationConfig
    "observation.max_per_minute": int,
    # LoggingConfig
    "logging.level": str,
    "logging.sanitize_sensitive": bool,
    # EncryptionConfig
    "encryption.enabled": bool,
    "encryption.key_path": str,
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
            setattr(sub_config, key_name, _coerce(env_value, target_type))

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


# ── YAML loading helpers ────────────────────────────────────────


def _parse_yaml(path: Path) -> Optional[Dict[str, Any]]:
    """Try to parse a YAML file; return ``None`` if pyyaml is not available."""
    try:
        import yaml  # optional dependency
    except ImportError:
        logger.debug("PyYAML not installed, skipping config file: %s", path)
        return None

    if not path.exists():
        return None

    with open(path, "r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)

    return data if isinstance(data, dict) else None


def _dict_to_dataclass(cls: type, data: Dict[str, Any]) -> Any:
    """Recursively convert a plain dict to a dataclass instance.

    Only keys that match known fields are used; unknown keys are silently
    ignored so that config files with extra sections don't crash.
    """
    known_fields = {f.name for f in fields(cls)}
    kwargs: Dict[str, Any] = {}

    for f in fields(cls):
        if f.name not in data:
            continue
        value = data[f.name]
        field_type = f.type

        # Resolve string annotations to actual types
        if isinstance(field_type, str):
            # field.type is a string like "StorageConfig"
            # Try to resolve from module globals
            import sys

            field_type = sys.modules.get(cls.__module__, None)
            if field_type is not None:
                field_type = getattr(field_type, f.type, None)

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


def load_config(path: Optional[str] = None) -> MemplexConfig:
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
    yaml_data = _parse_yaml(config_path)
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

    return config
