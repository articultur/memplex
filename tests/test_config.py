"""Test configuration system: defaults, environment variable overrides."""

import os
from pathlib import Path

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")


import pytest

from memplex.config import (
    BackupConfig,
    CompactionConfig,
    EmbeddingConfig,
    EncryptionConfig,
    GraphConfig,
    LLMConfig,
    LoggingConfig,
    MemplexConfig,
    ObservationConfig,
    OperationsConfig,
    RerankerConfig,
    RetrievalConfig,
    StorageConfig,
    SyncConfig,
    WikiConfig,
    WorkerConfig,
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
        # "lite" is the only implemented backend; MemplexService falls back
        # to it for any unavailable value, so the default itself must be the
        # implemented value (avoids a startup warning on every invocation).
        assert cfg.storage.backend == "lite"
        assert cfg.storage.path == "~/.memplex"

    def test_backup_defaults_are_exact_bounded_and_secret_free(self):
        cfg = MemplexConfig()

        assert isinstance(cfg.backup, BackupConfig)
        assert cfg.backup.directory == "~/.memplex/backups"
        assert cfg.backup.key_id == ""
        assert cfg.backup.max_artifact_bytes == 64 * 1024 * 1024 * 1024
        assert cfg.backup.restore_timeout_seconds == 3600
        assert cfg.backup.rpo_target_seconds == 300
        assert cfg.backup.rto_target_seconds == 1800
        assert "MEMPLEX_BACKUP_HMAC_KEY" not in repr(cfg)

    def test_backup_config_rejects_bool_and_nonpositive_bounds(self):
        with pytest.raises(TypeError, match="backup.max_artifact_bytes"):
            BackupConfig(max_artifact_bytes=True)
        with pytest.raises(ValueError, match="backup.rto_target_seconds"):
            BackupConfig(rto_target_seconds=0)

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
        assert cfg.llm.query_enhancement is True
        assert cfg.llm.observation_compression is True
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

    def test_wiki_defaults(self):
        cfg = MemplexConfig()
        assert isinstance(cfg.wiki, WikiConfig)
        assert cfg.wiki.dir == "~/.memplex/wiki"
        assert cfg.wiki.enabled is True

    def test_sync_and_worker_defaults_are_bounded_and_safe_to_render(self):
        cfg = MemplexConfig()
        assert isinstance(cfg.sync, SyncConfig)
        assert isinstance(cfg.worker, WorkerConfig)
        assert isinstance(cfg.operations, OperationsConfig)
        assert cfg.sync.max_batch_events == 1000
        assert cfg.sync.max_batch_bytes == 4 * 1024 * 1024
        assert "cursor_signing_secret" not in repr(cfg.sync)

    def test_operations_config_rejects_weak_bounds_and_invalid_targets(self):
        with pytest.raises(TypeError, match="operations.startup_timeout_seconds"):
            OperationsConfig(startup_timeout_seconds=True)
        with pytest.raises(ValueError, match="operations.shutdown_timeout_seconds"):
            OperationsConfig(shutdown_timeout_seconds=0)
        with pytest.raises(ValueError, match="operations.availability_target"):
            OperationsConfig(availability_target=1.1)
        with pytest.raises(ValueError, match="operations.error_rate_target"):
            OperationsConfig(error_rate_target=-0.1)
        with pytest.raises(ValueError, match="operations.p95_latency_target_ms"):
            OperationsConfig(p95_latency_target_ms=float("nan"))


class TestSyncConfig:
    def test_enabled_sync_requires_identity_and_a_32_byte_active_key(self):
        with pytest.raises(ValueError):
            SyncConfig(enabled=True)
        with pytest.raises(ValueError):
            SyncConfig(enabled=True, node_id="node-a", cursor_signing_key_id="active", cursor_signing_secret="short")
        config = SyncConfig(
            enabled=True,
            node_id="node-a",
            cursor_signing_key_id="active",
            cursor_signing_secret="s" * 32,
            cursor_previous_signing_keys={"previous": "p" * 32},
        )
        assert "s" * 32 not in repr(config)

    @pytest.mark.parametrize("field_name", ("max_batch_events", "max_batch_bytes", "page_size", "max_page_size", "max_pending_events", "max_snapshot_items"))
    def test_sync_bounds_are_exact_positive_ints(self, field_name):
        with pytest.raises((TypeError, ValueError)):
            SyncConfig(**{field_name: True})
        with pytest.raises((TypeError, ValueError)):
            SyncConfig(**{field_name: 0})

    def test_sync_config_rejects_previous_key_collision_and_unsafe_remote_url(self):
        with pytest.raises(ValueError):
            SyncConfig(cursor_signing_key_id="active", cursor_previous_signing_keys={"active": "p" * 32})
        with pytest.raises(ValueError):
            SyncConfig(cursor_previous_signing_keys={"previous": "short"})
        config = SyncConfig()
        with pytest.raises(ValueError):
            config.validate_remote_url("https://user:secret@example.test")
        with pytest.raises(ValueError):
            config.validate_remote_url("http://example.test")
        assert config.validate_remote_url("http://127.0.0.1:8000") == "http://127.0.0.1:8000"

    @pytest.mark.parametrize(
        ("field_name", "value"),
        (("max_batch_events", 1001), ("max_batch_bytes", 4 * 1024 * 1024 + 1), ("max_page_size", 1001)),
    )
    def test_sync_config_cannot_relax_global_protocol_caps(self, field_name, value):
        with pytest.raises(ValueError):
            SyncConfig(**{field_name: value})

    def test_sync_targets_bind_explicit_remote_identity_to_transport_only(self):
        config = SyncConfig(
            node_id="node-local",
            targets={"node-remote": "https://remote.example/sync"},
        )

        assert config.targets == {"node-remote": "https://remote.example/sync"}
        assert "remote.example" not in repr(config)

    @pytest.mark.parametrize(
        "targets",
        [
            [],
            {1: "https://remote.example"},
            {"": "https://remote.example"},
            {" node-remote": "https://remote.example"},
            {"node-remote": 1},
            {"node-remote": ""},
            {"node-local": "https://remote.example"},
            {
                "node-a": "https://remote.example",
                "node-b": "https://remote.example",
            },
        ],
    )
    def test_sync_targets_reject_weak_duplicate_or_self_identity(self, targets):
        with pytest.raises((TypeError, ValueError)):
            SyncConfig(node_id="node-local", targets=targets)

    def test_worker_bounds_are_exact_positive_ints(self):
        with pytest.raises(TypeError):
            WorkerConfig(queue_capacity=True)
        with pytest.raises(ValueError):
            WorkerConfig(drain_timeout_seconds=0)


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
    @pytest.mark.parametrize("value", ("", "maybe", " true", "true ", " yes "))
    def test_sync_enabled_env_rejects_invalid_or_whitespace_values(self, monkeypatch, value):
        monkeypatch.setenv("MEMPLEX_SYNC_ENABLED", value)
        with pytest.raises(ValueError, match="MEMPLEX_SYNC_ENABLED"):
            load_config()

    @pytest.mark.parametrize(("value", "expected"), (("true", True), ("FALSE", False), ("1", True), ("off", False)))
    def test_sync_enabled_env_accepts_only_explicit_boolean_vocabulary(self, monkeypatch, value, expected):
        monkeypatch.setenv("MEMPLEX_SYNC_ENABLED", value)
        monkeypatch.setenv("MEMPLEX_SYNC_NODE_ID", "node-a")
        monkeypatch.setenv("MEMPLEX_SYNC_CURSOR_SIGNING_KEY_ID", "active")
        monkeypatch.setenv("MEMPLEX_SYNC_CURSOR_SIGNING_SECRET", "s" * 32)
        assert load_config().sync.enabled is expected

    def test_sync_targets_json_environment_override_is_exact_and_secret_safe(
        self, monkeypatch
    ):
        monkeypatch.setenv(
            "MEMPLEX_SYNC_TARGETS_JSON",
            '{"node-remote":"https://remote.example/sync"}',
        )
        monkeypatch.setenv("MEMPLEX_SYNC_NODE_ID", "node-local")

        config = load_config()

        assert config.sync.targets == {
            "node-remote": "https://remote.example/sync"
        }
        assert "remote.example" not in repr(config.sync)

    @pytest.mark.parametrize(
        "raw",
        ("[]", "null", "{not-json", '{"node-a":1}'),
    )
    def test_sync_targets_json_environment_rejects_invalid_shape(
        self, monkeypatch, raw
    ):
        monkeypatch.setenv("MEMPLEX_SYNC_TARGETS_JSON", raw)
        with pytest.raises((TypeError, ValueError), match="MEMPLEX_SYNC_TARGETS_JSON|sync.targets"):
            load_config()

    def test_storage_backend_override(self, monkeypatch):
        monkeypatch.setenv("MEMPLEX_STORAGE_BACKEND", "lite")
        cfg = load_config()
        assert cfg.storage.backend == "lite"

    def test_storage_path_override(self, monkeypatch):
        monkeypatch.setenv("MEMPLEX_STORAGE_PATH", "/tmp/test_memplex")
        cfg = load_config()
        assert cfg.storage.path == "/tmp/test_memplex"

    def test_storage_migration_dsn_override(self, monkeypatch):
        monkeypatch.setenv("MEMPLEX_STORAGE_MIGRATION_DSN", "postgresql://admin.example/memplex")
        cfg = load_config()
        assert cfg.storage.migration_dsn == "postgresql://admin.example/memplex"

    def test_storage_inbound_dsn_override(self, monkeypatch):
        monkeypatch.setenv("MEMPLEX_STORAGE_INBOUND_DSN", "postgresql://inbound.example/memplex")
        cfg = load_config()
        assert cfg.storage.inbound_dsn == "postgresql://inbound.example/memplex"

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

    def test_operations_overrides_are_typed(self, monkeypatch):
        monkeypatch.setenv("MEMPLEX_OPERATIONS_STARTUP_TIMEOUT_SECONDS", "45")
        monkeypatch.setenv("MEMPLEX_OPERATIONS_AVAILABILITY_TARGET", "0.9995")
        monkeypatch.setenv("MEMPLEX_OPERATIONS_REPORT_KEY_ID", "ops-key")
        cfg = load_config()
        assert cfg.operations.startup_timeout_seconds == 45
        assert cfg.operations.availability_target == pytest.approx(0.9995)
        assert cfg.operations.report_key_id == "ops-key"

    def test_compaction_dedup_threshold_override(self, monkeypatch):
        monkeypatch.setenv("MEMPLEX_COMPACTION_DEDUP_THRESHOLD", "0.80")
        cfg = load_config()
        assert cfg.compaction.dedup_threshold == pytest.approx(0.80)

    def test_graph_threshold_override(self, monkeypatch):
        monkeypatch.setenv("MEMPLEX_GRAPH_SEMANTIC_SIMILAR_THRESHOLD", "0.9")
        cfg = load_config()
        assert cfg.graph.semantic_similar_threshold == pytest.approx(0.9)

    def test_wiki_dir_override(self, monkeypatch):
        monkeypatch.setenv("MEMPLEX_WIKI_DIR", "/tmp/test_memplex_wiki")
        cfg = load_config()
        assert cfg.wiki.dir == "/tmp/test_memplex_wiki"

    def test_wiki_enabled_override(self, monkeypatch):
        monkeypatch.setenv("MEMPLEX_WIKI_ENABLED", "false")
        cfg = load_config()
        assert cfg.wiki.enabled is False

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

    def test_llm_observation_compression_override(self, monkeypatch):
        monkeypatch.setenv("MEMPLEX_LLM_OBSERVATION_COMPRESSION", "false")
        cfg = load_config()
        assert cfg.llm.observation_compression is False

    def test_llm_observation_compression_default_true(self, monkeypatch):
        monkeypatch.delenv("MEMPLEX_LLM_OBSERVATION_COMPRESSION", raising=False)
        cfg = load_config()
        assert cfg.llm.observation_compression is True


# ── Config file loading ──────────────────────────────────────────────


class TestConfigFileLoading:
    def test_load_from_nonexistent_path(self, tmp_path):
        cfg = load_config(path=str(tmp_path / "nonexistent.yaml"))
        # Should fall back to defaults
        assert isinstance(cfg, MemplexConfig)

    def test_migration_dsn_environment_override_wins_over_yaml(self, tmp_path, monkeypatch):
        pytest.importorskip("yaml")
        path = Path(tmp_path) / "config.yaml"
        path.write_text(
            "storage:\n  migration_dsn: postgresql://yaml-admin.example/memplex\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("MEMPLEX_STORAGE_MIGRATION_DSN", "postgresql://env-admin.example/memplex")

        assert load_config(path=str(path)).storage.migration_dsn == "postgresql://env-admin.example/memplex"

    def test_production_postgres_rejects_invalid_yaml_without_echoing_contents(self, tmp_path, monkeypatch):
        pytest.importorskip("yaml")
        path = Path(tmp_path) / "config.yaml"
        secret = "postgresql://admin:parse-secret@example/memplex"
        path.write_text(f"storage: [ {secret}\n", encoding="utf-8")
        monkeypatch.setenv("MEMPLEX_DEPLOYMENT_PROFILE", "production")
        monkeypatch.setenv("MEMPLEX_STORAGE_BACKEND", "postgres")
        monkeypatch.setenv("MEMPLEX_STORAGE_PATH", "postgresql://app.example/memplex")
        monkeypatch.setenv("MEMPLEX_STORAGE_MIGRATION_DSN", "postgresql://admin.example/memplex")

        with pytest.raises(ValueError, match="YAML could not be parsed") as error:
            load_config(path=str(path))
        assert secret not in str(error.value)

    def test_production_postgres_rejects_unknown_storage_field_without_values(self, tmp_path, monkeypatch):
        pytest.importorskip("yaml")
        path = Path(tmp_path) / "config.yaml"
        secret = "postgresql://admin:typo-secret@example/memplex"
        path.write_text(
            "deployment:\n  profile: production\nstorage:\n"
            "  backend: postgres\n  path: postgresql://app.example/memplex\n"
            f"  migration_dns: {secret}\n",
            encoding="utf-8",
        )
        monkeypatch.setenv("MEMPLEX_DEPLOYMENT_PROFILE", "production")
        monkeypatch.setenv("MEMPLEX_STORAGE_BACKEND", "postgres")

        with pytest.raises(ValueError, match=r"unknown field\(s\): migration_dns") as error:
            load_config(path=str(path))
        assert secret not in str(error.value)

    def test_prod_and_dev_sync_disabled_keep_compat_without_inbound_dsn(self, tmp_path, monkeypatch):
        pytest.importorskip("yaml")
        path = Path(tmp_path) / "config.yaml"
        path.write_text("storage:\n  backend: postgres\n", encoding="utf-8")
        from memplex.config import validate_deployment_contract

        cfg = MemplexConfig()
        cfg.deployment.profile = "production"
        cfg.storage.backend = "postgres"
        cfg.storage.path = "postgresql://app.example/memplex"
        cfg.storage.migration_dsn = "postgresql://admin.example/memplex"
        cfg.sync.enabled = False
        validate_deployment_contract(cfg)

    def test_development_lite_sync_does_not_require_postgres_inbound_dsn(self):
        from memplex.config import validate_deployment_contract

        cfg = MemplexConfig()
        cfg.deployment.profile = "development"
        cfg.storage.backend = "lite"
        cfg.sync.enabled = True
        cfg.sync.node_id = "local-node"
        cfg.sync.cursor_signing_key_id = "active"
        cfg.sync.cursor_signing_secret = "x" * 32

        validate_deployment_contract(cfg)

        cfg = MemplexConfig()
        cfg.deployment.profile = "development"
        cfg.storage.backend = "postgres"
        cfg.storage.path = "postgresql://app.example/memplex"
        cfg.sync.enabled = False
        validate_deployment_contract(cfg)

    def test_production_sync_enabled_requires_inbound_dsn(self):
        from memplex.config import validate_deployment_contract

        cfg = MemplexConfig()
        cfg.deployment.profile = "production"
        cfg.storage.backend = "postgres"
        cfg.storage.path = "postgresql://app.example/memplex"
        cfg.storage.migration_dsn = "postgresql://admin.example/memplex"
        cfg.sync.enabled = True
        cfg.sync.node_id = "node-a"
        cfg.sync.cursor_signing_key_id = "active"
        cfg.sync.cursor_signing_secret = "s" * 32
        with pytest.raises(ValueError, match="inbound DSN"):
            validate_deployment_contract(cfg)

    def test_production_sync_enabled_rejects_same_inbound_dsn_as_application_or_migration(self):
        from memplex.config import validate_deployment_contract

        cfg = MemplexConfig()
        cfg.deployment.profile = "production"
        cfg.storage.backend = "postgres"
        cfg.storage.path = "postgresql://app.example/memplex"
        cfg.storage.migration_dsn = "postgresql://admin.example/memplex"
        cfg.sync.enabled = True
        cfg.sync.node_id = "node-a"
        cfg.sync.cursor_signing_key_id = "active"
        cfg.sync.cursor_signing_secret = "s" * 32
        cfg.storage.inbound_dsn = "postgresql://app.example/memplex"

        with pytest.raises(ValueError, match="distinct from application DSN"):
            validate_deployment_contract(cfg)

        cfg.storage.inbound_dsn = "postgresql://admin.example/memplex"
        with pytest.raises(ValueError, match="distinct from migration DSN"):
            validate_deployment_contract(cfg)

    def test_development_sync_enabled_postgres_requires_inbound_dsn(self):
        from memplex.config import validate_deployment_contract

        cfg = MemplexConfig()
        cfg.deployment.profile = "development"
        cfg.storage.backend = "postgres"
        cfg.storage.path = "postgresql://app.example/memplex"
        cfg.storage.migration_dsn = "postgresql://admin.example/memplex"
        cfg.sync.enabled = True
        cfg.sync.node_id = "node-a"
        cfg.sync.cursor_signing_key_id = "active"
        cfg.sync.cursor_signing_secret = "s" * 32

        with pytest.raises(ValueError, match="inbound DSN") as error:
            validate_deployment_contract(cfg)
        assert "postgresql://app.example/memplex" not in str(error.value)

    def test_development_sync_enabled_postgres_requires_distinct_migration_dsn(self):
        from memplex.config import validate_deployment_contract

        cfg = MemplexConfig()
        cfg.deployment.profile = "development"
        cfg.storage.backend = "postgres"
        cfg.storage.path = "postgresql://app.example/memplex"
        cfg.storage.inbound_dsn = "postgresql://inbound.example/memplex"
        cfg.sync.enabled = True
        cfg.sync.node_id = "node-a"
        cfg.sync.cursor_signing_key_id = "active"
        cfg.sync.cursor_signing_secret = "s" * 32

        with pytest.raises(ValueError, match="migration DSN"):
            validate_deployment_contract(cfg)

        cfg.storage.migration_dsn = cfg.storage.path
        with pytest.raises(ValueError, match="migration DSN must be distinct"):
            validate_deployment_contract(cfg)

    def test_storage_config_repr_redacts_inbound_dsn(self):
        config = MemplexConfig()
        config.storage.path = "postgresql://app:app-secret@example/memplex"
        config.storage.migration_dsn = "postgresql://admin:admin-secret@example/memplex"
        config.storage.inbound_dsn = "postgresql://inbound:inbound-secret@example/memplex"

        rendered = repr(config)
        assert "app-secret" not in rendered
        assert "admin-secret" not in rendered
        assert "inbound-secret" not in rendered

        from memplex.config import _ENV_TYPE_COERCIONS

        assert "storage.inbound_dsn" in _ENV_TYPE_COERCIONS

    @pytest.mark.parametrize("contents", ("[]\n", "null\n", "postgresql://admin:shape-secret@example/memplex\n"))
    def test_production_postgres_rejects_non_mapping_yaml_root(self, tmp_path, monkeypatch, contents):
        pytest.importorskip("yaml")
        path = Path(tmp_path) / "config.yaml"
        path.write_text(contents, encoding="utf-8")
        monkeypatch.setenv("MEMPLEX_DEPLOYMENT_PROFILE", "production")
        monkeypatch.setenv("MEMPLEX_STORAGE_BACKEND", "postgres")
        monkeypatch.setenv("MEMPLEX_STORAGE_PATH", "postgresql://app.example/memplex")
        monkeypatch.setenv("MEMPLEX_STORAGE_MIGRATION_DSN", "postgresql://admin.example/memplex")

        with pytest.raises(ValueError, match="root must be a mapping") as error:
            load_config(path=str(path))
        assert "shape-secret" not in str(error.value)

    def test_storage_config_repr_redacts_application_and_migration_dsns(self):
        config = MemplexConfig()
        config.storage.path = "postgresql://app:app-secret@example/memplex"
        config.storage.migration_dsn = "postgresql://admin:admin-secret@example/memplex"

        rendered = repr(config)
        assert "app-secret" not in rendered
        assert "admin-secret" not in rendered


# ── ANTHROPIC_API_KEY fallback ───────────────────────────────────────


class TestAnthropicApiKeyFallback:
    def test_standard_env_var_fallback(self, monkeypatch):
        """Regression: only MEMPLEX_LLM_ANTHROPIC_API_KEY was consulted;
        the standard ANTHROPIC_API_KEY env var must work as fallback."""
        monkeypatch.delenv("MEMPLEX_LLM_ANTHROPIC_API_KEY", raising=False)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-standard")
        cfg = load_config()
        assert cfg.llm.anthropic_api_key == "sk-ant-standard"

    def test_memplex_env_var_wins_over_standard(self, monkeypatch):
        monkeypatch.setenv("MEMPLEX_LLM_ANTHROPIC_API_KEY", "sk-ant-memplex")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-standard")
        cfg = load_config()
        assert cfg.llm.anthropic_api_key == "sk-ant-memplex"

    def test_no_key_anywhere_stays_none(self, monkeypatch):
        monkeypatch.delenv("MEMPLEX_LLM_ANTHROPIC_API_KEY", raising=False)
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
        cfg = load_config()
        assert cfg.llm.anthropic_api_key is None


# ── Removed dead config ──────────────────────────────────────────────


class TestRemovedDeadConfig:
    def test_llm_reranking_removed(self):
        """llm.reranking had no consumer (no LLM-reranking implementation
        exists); it was removed to avoid advertising a dead toggle."""
        assert not hasattr(LLMConfig(), "reranking")
        from memplex.config import _ENV_TYPE_COERCIONS

        assert "llm.reranking" not in _ENV_TYPE_COERCIONS

    def test_semantic_similar_ttl_and_sync_removed(self):
        """graph.semantic_similar_ttl_days / semantic_similar_sync_on_merge
        had no consumer (no edge-TTL expiry or merge-time resync machinery);
        they were removed when the remaining semantic_similar_* keys were
        wired into GraphBuilder."""
        assert not hasattr(GraphConfig(), "semantic_similar_ttl_days")
        assert not hasattr(GraphConfig(), "semantic_similar_sync_on_merge")
        from memplex.config import _ENV_TYPE_COERCIONS

        assert "graph.semantic_similar_ttl_days" not in _ENV_TYPE_COERCIONS
        assert "graph.semantic_similar_sync_on_merge" not in _ENV_TYPE_COERCIONS
