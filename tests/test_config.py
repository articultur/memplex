"""Test configuration system: defaults, environment variable overrides."""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")


import pytest

from memplex.config import (
    CompactionConfig,
    EmbeddingConfig,
    EncryptionConfig,
    GraphConfig,
    LLMConfig,
    LoggingConfig,
    MemplexConfig,
    ObservationConfig,
    RerankerConfig,
    RetrievalConfig,
    StorageConfig,
    _coerce,
    load_config,
)

# ── Default configuration ────────────────────────────────────────────


class TestDefaultConfig:
    def test_load_config_returns_memplex_config(self):
        cfg = load_config()
        assert isinstance(cfg, MemplexConfig)

    def test_storage_defaults(self):
        cfg = MemplexConfig()
        assert isinstance(cfg.storage, StorageConfig)
        assert cfg.storage.backend == "standard"
        assert cfg.storage.path == "~/.memplex"

    def test_embedding_defaults(self):
        cfg = MemplexConfig()
        assert isinstance(cfg.embedding, EmbeddingConfig)
        assert cfg.embedding.model == "default"
        assert cfg.embedding.dimension == 384
        assert cfg.embedding.batch_size == 32
        assert cfg.embedding.contextual_retrieval is True
        assert cfg.embedding.hyde_enabled is True

    def test_reranker_defaults(self):
        cfg = MemplexConfig()
        assert isinstance(cfg.reranker, RerankerConfig)
        assert "raw_relevance" in cfg.reranker.weights
        assert cfg.reranker.cross_encoder_enabled is False

    def test_compaction_defaults(self):
        cfg = MemplexConfig()
        assert isinstance(cfg.compaction, CompactionConfig)
        assert cfg.compaction.dedup_threshold == 0.95
        assert cfg.compaction.field_max_values == 20

    def test_graph_defaults(self):
        cfg = MemplexConfig()
        assert isinstance(cfg.graph, GraphConfig)
        assert cfg.graph.semantic_similar_threshold == 0.85
        assert cfg.graph.community_detection_enabled is True

    def test_retrieval_defaults(self):
        cfg = MemplexConfig()
        assert isinstance(cfg.retrieval, RetrievalConfig)
        assert cfg.retrieval.default_max_tokens == 4000
        assert cfg.retrieval.injection_scan_enabled is True

    def test_llm_defaults(self):
        cfg = MemplexConfig()
        assert isinstance(cfg.llm, LLMConfig)
        assert cfg.llm.semantic_extraction is True
        assert cfg.llm.query_enhancement is True
        assert cfg.llm.provider == "anthropic"

    def test_observation_defaults(self):
        cfg = MemplexConfig()
        assert isinstance(cfg.observation, ObservationConfig)
        assert cfg.observation.max_per_minute == 20

    def test_logging_defaults(self):
        cfg = MemplexConfig()
        assert isinstance(cfg.logging, LoggingConfig)
        assert cfg.logging.level == "INFO"

    def test_encryption_defaults(self):
        cfg = MemplexConfig()
        assert isinstance(cfg.encryption, EncryptionConfig)
        assert cfg.encryption.enabled is False


# ── Coerce helper ────────────────────────────────────────────────────


class TestCoerce:
    def test_coerce_bool_true(self):
        assert _coerce("true", bool) is True
        assert _coerce("1", bool) is True
        assert _coerce("yes", bool) is True
        assert _coerce("on", bool) is True

    def test_coerce_bool_false(self):
        assert _coerce("false", bool) is False
        assert _coerce("0", bool) is False
        assert _coerce("no", bool) is False

    def test_coerce_int(self):
        assert _coerce("42", int) == 42
        assert _coerce("0", int) == 0

    def test_coerce_float(self):
        assert _coerce("3.14", float) == pytest.approx(3.14)
        assert _coerce("0.95", float) == pytest.approx(0.95)

    def test_coerce_str(self):
        assert _coerce("hello", str) == "hello"


# ── Environment variable overrides ──────────────────────────────────


class TestEnvOverrides:
    def test_storage_backend_override(self, monkeypatch):
        monkeypatch.setenv("MEMPLEX_STORAGE_BACKEND", "lite")
        cfg = load_config()
        assert cfg.storage.backend == "lite"

    def test_storage_path_override(self, monkeypatch):
        monkeypatch.setenv("MEMPLEX_STORAGE_PATH", "/tmp/test_memplex")
        cfg = load_config()
        assert cfg.storage.path == "/tmp/test_memplex"

    def test_embedding_dimension_override(self, monkeypatch):
        monkeypatch.setenv("MEMPLEX_EMBEDDING_DIMENSION", "768")
        cfg = load_config()
        assert cfg.embedding.dimension == 768

    def test_embedding_hyde_override(self, monkeypatch):
        monkeypatch.setenv("MEMPLEX_EMBEDDING_HYDE_ENABLED", "false")
        cfg = load_config()
        assert cfg.embedding.hyde_enabled is False

    def test_embedding_model_offline_override(self, monkeypatch):
        monkeypatch.setenv("MEMPLEX_EMBEDDING_MODEL", "tfidf")
        cfg = load_config()
        assert cfg.embedding.model == "tfidf"

    def test_llm_provider_override(self, monkeypatch):
        monkeypatch.setenv("MEMPLEX_LLM_PROVIDER", "local")
        cfg = load_config()
        assert cfg.llm.provider == "local"

    def test_llm_fallback_chain_override(self, monkeypatch):
        monkeypatch.setenv("MEMPLEX_LLM_FALLBACK_CHAIN", "anthropic, local, rule-based")
        cfg = load_config()
        assert cfg.llm.fallback_chain == ["anthropic", "local", "rule-based"]

    def test_logging_level_override(self, monkeypatch):
        monkeypatch.setenv("MEMPLEX_LOGGING_LEVEL", "DEBUG")
        cfg = load_config()
        assert cfg.logging.level == "DEBUG"

    def test_encryption_enabled_override(self, monkeypatch):
        monkeypatch.setenv("MEMPLEX_ENCRYPTION_ENABLED", "true")
        cfg = load_config()
        assert cfg.encryption.enabled is True

    def test_compaction_dedup_threshold_override(self, monkeypatch):
        monkeypatch.setenv("MEMPLEX_COMPACTION_DEDUP_THRESHOLD", "0.80")
        cfg = load_config()
        assert cfg.compaction.dedup_threshold == pytest.approx(0.80)

    def test_graph_threshold_override(self, monkeypatch):
        monkeypatch.setenv("MEMPLEX_GRAPH_SEMANTIC_SIMILAR_THRESHOLD", "0.9")
        cfg = load_config()
        assert cfg.graph.semantic_similar_threshold == pytest.approx(0.9)

    def test_reranker_weight_override(self, monkeypatch):
        monkeypatch.setenv("MEMPLEX_RERANKER_WEIGHTS_RAW_RELEVANCE", "0.4")
        cfg = load_config()
        assert cfg.reranker.weights["raw_relevance"] == pytest.approx(0.4)

    def test_multiple_overrides(self, monkeypatch):
        monkeypatch.setenv("MEMPLEX_STORAGE_BACKEND", "lite")
        monkeypatch.setenv("MEMPLEX_EMBEDDING_MODEL", "bge-m3")
        monkeypatch.setenv("MEMPLEX_LLM_QUERY_ENHANCEMENT", "false")
        cfg = load_config()
        assert cfg.storage.backend == "lite"
        assert cfg.embedding.model == "bge-m3"
        assert cfg.llm.query_enhancement is False


# ── Config file loading ──────────────────────────────────────────────


class TestConfigFileLoading:
    def test_load_from_nonexistent_path(self, tmp_path):
        cfg = load_config(path=str(tmp_path / "nonexistent.yaml"))
        # Should fall back to defaults
        assert isinstance(cfg, MemplexConfig)
