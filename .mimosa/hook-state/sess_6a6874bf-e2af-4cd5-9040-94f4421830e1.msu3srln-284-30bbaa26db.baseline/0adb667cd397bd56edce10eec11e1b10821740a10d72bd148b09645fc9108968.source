"""Test data models: FieldValue, Function, Fact, Preference, Observation,
validate_func_id, MemoryNode type system."""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from datetime import datetime, timedelta, timezone

import pytest

from memplex.models import (
    MAX_FUNC_ID_LENGTH,
    EdgeType,
    FeedbackVerdict,
    FieldValue,
    GraphData,
    GraphEdge,
    IntentType,
    MemoryFeedback,
    QueryScope,
    SourceType,
    domain_node_id,
    validate_belongs_to_edges,
    validate_domain,
    validate_func_id,
)
from memplex.models.memory import (
    DEFAULT_OBSERVATION_CATEGORY,
    OBSERVATION_CATEGORIES,
    Fact,
    Function,
    Memory,
    MemoryNode,
    Observation,
    Preference,
    create_memory_node,
)

# ── FieldValue ────────────────────────────────────────────────────────


class TestFieldValue:
    def test_create_defaults(self):
        fv = FieldValue(desc="hello")
        assert fv.desc == "hello"
        assert fv.sources == []
        assert fv.source_method == "rule_based"
        assert fv.weight == 1.0
        assert fv.observation is None
        assert fv.created_at is None
        assert fv.status == "active"

    def test_create_full(self):
        now = datetime.now(timezone.utc)
        fv = FieldValue(
            desc="trigger text",
            sources=["text:para_1"],
            source_method="llm_semantic",
            weight=0.85,
            observation=0.9,
            created_at=now,
            status="active",
        )
        assert fv.desc == "trigger text"
        assert fv.sources == ["text:para_1"]
        assert fv.source_method == "llm_semantic"
        assert fv.weight == 0.85
        assert fv.observation == 0.9
        assert fv.created_at == now
        assert fv.status == "active"

    def test_multiple_field_values(self):
        """Multiple FieldValues can coexist in a list."""
        fvs = [
            FieldValue(desc="first trigger"),
            FieldValue(desc="second trigger"),
            FieldValue(desc="third trigger"),
        ]
        assert len(fvs) == 3
        assert fvs[0].desc == "first trigger"
        assert fvs[1].desc == "second trigger"
        assert fvs[2].desc == "third trigger"


# ── validate_func_id ──────────────────────────────────────────────────


class TestValidateFuncId:
    def test_valid_simple(self):
        assert validate_func_id("func_abc123") == "func_abc123"

    def test_valid_with_hyphen_underscore(self):
        assert validate_func_id("my-func_v2") == "my-func_v2"

    def test_invalid_special_chars(self):
        with pytest.raises(ValueError, match="非法字符"):
            validate_func_id("func with spaces")

    def test_invalid_unicode(self):
        with pytest.raises(ValueError, match="非法字符"):
            validate_func_id("函数ID")

    def test_too_long(self):
        long_id = "a" * (MAX_FUNC_ID_LENGTH + 1)
        with pytest.raises(ValueError, match="过长"):
            validate_func_id(long_id)

    def test_at_max_length(self):
        ok_id = "a" * MAX_FUNC_ID_LENGTH
        assert validate_func_id(ok_id) == ok_id


class TestValidateDomain:
    @pytest.mark.parametrize("domain", ["", "0", "  spaced  ", "认证模块"])
    def test_string_and_none_domains_are_valid(self, domain):
        assert validate_domain(domain) == domain
        assert validate_domain(None) is None

    @pytest.mark.parametrize("domain", [True, False, 0, 1, 1.5, {}, [], ()])
    def test_non_string_domains_are_rejected(self, domain):
        with pytest.raises(ValueError, match="domain"):
            validate_domain(domain)


class TestBelongsToValidation:
    @pytest.mark.parametrize("domain", ["0", "  spaced  ", "中文 领域"])
    def test_exact_nonempty_string_domain_accepts_shared_virtual_target(self, domain):
        source = Function(id="func_belongs_valid", name="Source", domain=domain)
        validate_belongs_to_edges(
            [source],
            [GraphEdge(source.id, domain_node_id(domain), EdgeType.BELONGS_TO.value)],
        )

    @pytest.mark.parametrize("domain", [None, ""])
    def test_missing_or_empty_domain_rejects_belongs_to(self, domain):
        source = Function(id="func_belongs_invalid", name="Source", domain=domain)
        with pytest.raises(ValueError, match="BELONGS_TO"):
            validate_belongs_to_edges(
                [source],
                [GraphEdge(source.id, "domain_forged", EdgeType.BELONGS_TO.value)],
            )

    def test_missing_source_and_mutated_non_string_domain_reject_belongs_to(self):
        source = Function(id="func_belongs_mutated", name="Source", domain="auth")
        source.domain = []
        with pytest.raises(ValueError, match="domain"):
            validate_belongs_to_edges(
                [source],
                [GraphEdge(source.id, "domain_auth", EdgeType.BELONGS_TO.value)],
            )
        with pytest.raises(ValueError, match="BELONGS_TO"):
            validate_belongs_to_edges(
                [],
                [GraphEdge("missing", "domain_auth", EdgeType.BELONGS_TO.value)],
            )

    def test_lowercase_domain_namespace_is_reserved(self):
        with pytest.raises(ValueError, match="保留"):
            validate_func_id("domain_auth")
        # The reservation is intentionally case-sensitive: historical IDs
        # with an upper-case prefix are not GraphBuilder virtual nodes.
        assert validate_func_id("DOMAIN_auth") == "DOMAIN_auth"


# ── Function ──────────────────────────────────────────────────────────


class TestFunction:
    def test_create_basic(self):
        func = Function(id="func_test1", name="Test Function")
        assert func.id == "func_test1"
        assert func.name == "Test Function"
        assert func.memory_type == "function"
        assert func.trigger == []
        assert func.condition == []
        assert func.action == []
        assert func.benefit == []
        assert func.created_at is not None
        assert func.updated_at is not None

    def test_constructor_rejects_graph_builder_virtual_namespace(self):
        with pytest.raises(ValueError, match="保留"):
            Function(id="domain_auth", name="virtual")

    @pytest.mark.parametrize("domain", [True, 0, 1.5, {}, [], ()])
    def test_constructor_rejects_non_string_domain(self, domain):
        with pytest.raises(ValueError, match="domain"):
            Function(id="func_domain_type", name="bad", domain=domain)

    def test_from_dict_rejects_graph_builder_virtual_namespace(self):
        with pytest.raises(ValueError, match="保留"):
            Function.from_dict({"id": "domain_auth", "name": "virtual"})

    def test_from_dict_rejects_non_string_domain(self):
        with pytest.raises(ValueError, match="domain"):
            Function.from_dict({"id": "func_bad_domain", "name": "bad", "domain": []})

    def test_create_with_field_values(self):
        func = Function(
            id="func_fv_test",
            name="Login",
            trigger=[
                FieldValue(desc="user clicks login", sources=["text"]),
                FieldValue(desc="user submits form", sources=["text"]),
            ],
            action=[
                FieldValue(desc="validate credentials", sources=["text"]),
            ],
            benefit=[
                FieldValue(desc="user is authenticated", sources=["text"]),
            ],
        )
        assert len(func.trigger) == 2
        assert func.trigger[0].desc == "user clicks login"
        assert len(func.action) == 1
        assert len(func.benefit) == 1

    def test_invalid_id_raises(self):
        with pytest.raises(ValueError):
            Function(id="bad id!", name="Bad")

    def test_name_normalized(self):
        func = Function(id="func_norm", name="Test", name_normalized="test")
        assert func.name_normalized == "test"

    def test_source_type(self):
        func = Function(
            id="func_src",
            name="Test",
            source_type=SourceType.CODE,
        )
        assert func.source_type == SourceType.CODE


# ── Fact ──────────────────────────────────────────────────────────────


class TestFact:
    def test_create(self):
        fact = Fact(
            id="fact_1",
            name="Definition",
            subject="API",
            predicate="is",
            object_="REST interface",
        )
        assert fact.memory_type == "fact"
        assert fact.subject == "API"
        assert fact.predicate == "is"
        assert fact.object_ == "REST interface"

    def test_default_fields(self):
        fact = Fact(id="fact_d", name="D")
        assert fact.valid_until is None

    def test_to_dict_uses_object_key(self):
        fact = Fact(id="fact_s", name="S", subject="地球", predicate="是", object_="行星")
        d = fact.to_dict()
        assert d["object"] == "行星"
        assert "object_" not in d
        assert d["memory_type"] == "fact"
        assert d["subject"] == "地球"

    def test_from_dict_accepts_object_key(self):
        fact = Fact.from_dict({"id": "fact_s", "subject": "地球", "object": "行星"})
        assert fact.object_ == "行星"
        assert fact.subject == "地球"

    def test_from_dict_accepts_legacy_object__key(self):
        fact = Fact.from_dict({"id": "fact_s", "object_": "行星"})
        assert fact.object_ == "行星"

    def test_roundtrip(self):
        fact = Fact(
            id="fact_rt",
            name="RT",
            subject="API",
            predicate="is",
            object_="REST interface",
            valid_until="2030-01-01",
            domain="arch",
        )
        restored = Fact.from_dict(fact.to_dict())
        assert restored.id == fact.id
        assert restored.subject == fact.subject
        assert restored.predicate == fact.predicate
        assert restored.object_ == fact.object_
        assert restored.valid_until == fact.valid_until
        assert restored.domain == fact.domain


# ── Preference ────────────────────────────────────────────────────────


class TestPreference:
    def test_create(self):
        pref = Preference(
            id="pref_1",
            name="UI Theme",
            aspect="theme",
            preference="dark mode",
        )
        assert pref.memory_type == "preference"
        assert pref.aspect == "theme"
        assert pref.preference == "dark mode"

    def test_default_fields(self):
        pref = Preference(id="pref_d", name="D")
        assert pref.subject_id is None

    def test_roundtrip(self):
        pref = Preference(
            id="pref_rt",
            name="UI Theme",
            aspect="theme",
            preference="dark mode",
            subject_id="user-1",
            domain="ui",
        )
        d = pref.to_dict()
        assert d["memory_type"] == "preference"
        assert d["aspect"] == "theme"
        restored = Preference.from_dict(d)
        assert restored.id == pref.id
        assert restored.aspect == pref.aspect
        assert restored.preference == pref.preference
        assert restored.subject_id == pref.subject_id
        assert restored.domain == pref.domain

    def test_from_dict_tolerates_missing_keys(self):
        pref = Preference.from_dict({"id": "pref_x"})
        assert pref.id == "pref_x"
        assert pref.aspect == ""
        assert pref.preference == ""
        assert pref.subject_id is None


# ── Observation ───────────────────────────────────────────────────────


class TestObservation:
    def test_create(self):
        obs = Observation(
            id="obs_1",
            name="Error Spike",
            event="500 errors increased",
            context="after deploy v2",
            actor="system",
        )
        assert obs.memory_type == "observation"
        assert obs.event == "500 errors increased"
        assert obs.context == "after deploy v2"
        assert obs.actor == "system"

    def test_default_actor(self):
        obs = Observation(id="obs_d", name="D")
        assert obs.actor == "system"

    def test_default_category_is_note(self):
        obs = Observation(id="obs_c", name="C")
        assert obs.category == "note"
        assert DEFAULT_OBSERVATION_CATEGORY == "note"

    def test_observation_categories_constant(self):
        assert OBSERVATION_CATEGORIES == ("bugfix", "decision", "change", "discovery", "note")


# ── MemoryNode type system ───────────────────────────────────────────


class TestMemoryNodeTypeSystem:
    def test_memory_alias(self):
        assert Memory is MemoryNode

    def test_function_is_memory_node(self):
        func = Function(id="func_mn", name="MN")
        assert isinstance(func, MemoryNode)

    def test_fact_is_memory_node(self):
        fact = Fact(id="fact_mn", name="MN")
        assert isinstance(fact, MemoryNode)

    def test_preference_is_memory_node(self):
        pref = Preference(id="pref_mn", name="MN")
        assert isinstance(pref, MemoryNode)

    def test_observation_is_memory_node(self):
        obs = Observation(id="obs_mn", name="MN")
        assert isinstance(obs, MemoryNode)

    def test_create_memory_node_function(self):
        node = create_memory_node("function", id="func_cmn", name="Created")
        assert isinstance(node, Function)
        assert node.memory_type == "function"

    def test_create_memory_node_fact(self):
        node = create_memory_node("fact", id="fact_cmn", name="Created")
        assert isinstance(node, Fact)

    def test_create_memory_node_preference(self):
        node = create_memory_node("preference", id="pref_cmn", name="Created")
        assert isinstance(node, Preference)

    def test_create_memory_node_observation(self):
        node = create_memory_node("observation", id="obs_cmn", name="Created")
        assert isinstance(node, Observation)

    def test_create_memory_node_unknown_raises(self):
        with pytest.raises(ValueError, match="Unknown memory_type"):
            create_memory_node("unknown", id="bad", name="Bad")


# ── Enums / auxiliary ────────────────────────────────────────────────


class TestEnums:
    def test_source_type_values(self):
        assert SourceType.REQUIREMENT.value == "requirement"
        assert SourceType.WIKI.value == "wiki"

    def test_edge_type_values(self):
        assert EdgeType.REFERENCES.value == "REFERENCES"
        assert EdgeType.DEPENDS_ON.value == "DEPENDS_ON"
        assert EdgeType.CONFLICTS_WITH.value == "CONFLICTS_WITH"

    def test_query_scope_values(self):
        assert QueryScope.IMMEDIATE.value == "immediate"
        assert QueryScope.ALL.value == "all"

    def test_feedback_verdict_values(self):
        assert FeedbackVerdict.CORRECT.value == "correct"
        assert FeedbackVerdict.WRONG.value == "wrong"

    def test_intent_type_values(self):
        assert IntentType.IMMEDIATE.value == "immediate"
        assert IntentType.SYNTHESIS.value == "synthesis"


# ── GraphEdge / GraphData ────────────────────────────────────────────


class TestGraphTypes:
    def test_graph_edge(self):
        edge = GraphEdge(
            source="func_a",
            target="func_b",
            edge_type="REFERENCES",
            weight=0.9,
            evidence=["cross-reference"],
        )
        assert edge.source == "func_a"
        assert edge.target == "func_b"
        assert edge.edge_type == "REFERENCES"

    def test_graph_data_defaults(self):
        gd = GraphData()
        assert gd.nodes == []
        assert gd.edges == []

    def test_graph_data_with_content(self):
        gd = GraphData(
            nodes=[1, 2],
            edges=[GraphEdge(source="a", target="b", edge_type="REFERENCES")],
        )
        assert len(gd.nodes) == 2
        assert len(gd.edges) == 1


# ── to_dict / from_dict roundtrips ───────────────────────────────────


class TestFieldValueSerialization:
    def test_roundtrip_full(self):
        now = datetime.now(timezone.utc)
        fv = FieldValue(
            desc="trigger text",
            sources=["text:para_1", "wiki:p2"],
            source_method="llm_semantic",
            weight=0.85,
            observation=0.9,
            created_at=now,
            status="disputed",
        )
        d = fv.to_dict()
        # JSON-safe: datetime serialized to ISO string
        assert d["created_at"] == now.isoformat()
        restored = FieldValue.from_dict(d)
        assert restored == fv

    def test_roundtrip_defaults(self):
        fv = FieldValue(desc="x")
        restored = FieldValue.from_dict(fv.to_dict())
        assert restored == fv

    def test_from_dict_tolerates_missing_keys(self):
        fv = FieldValue.from_dict({"desc": "only desc"})
        assert fv.desc == "only desc"
        assert fv.sources == []
        assert fv.weight == 1.0
        assert fv.status == "active"


class TestFunctionSerialization:
    def _full_function(self):
        return Function(
            id="func_ser",
            name="Serialize Me",
            name_normalized="serialize me",
            domain="testing",
            confidence=0.7,
            source_type=SourceType.MEETING,
            owner="alice",
            version=3,
            origin_session="sess_1",
            access_count=5,
            last_accessed_at="2026-01-01T00:00:00+00:00",
            source_paragraphs=["p1", "p2"],
            needs_review=True,
            needs_review_until="2026-02-01T00:00:00+00:00",
            content_hash="abc123",
            trigger=[FieldValue(desc="t1", weight=0.4, observation=0.8)],
            condition=[FieldValue(desc="c1")],
            action=[FieldValue(desc="a1", status="deprecated")],
            benefit=[],
            attributes={"k": "v"},
            cross_references=[{"target": "func_other"}],
            priority_from_source="high",
            source_authority="authoritative",
        )

    def test_roundtrip_all_fields(self):
        func = self._full_function()
        restored = Function.from_dict(func.to_dict())
        assert restored == func

    def test_to_dict_covers_drift_prone_fields(self):
        """Regression: http_api._function_from_dict used to drop these."""
        d = self._full_function().to_dict()
        for key in (
            "needs_review_until",
            "priority_from_source",
            "source_authority",
            "content_hash",
        ):
            assert key in d
        # FieldValue sub-fields that were dropped by the sync payload path
        fv_dict = d["trigger"][0]
        for key in ("observation", "created_at", "status", "source_method"):
            assert key in fv_dict

    def test_from_dict_source_type_fallback(self):
        func = Function.from_dict({"id": "func_x", "source_type": "not-a-type"})
        assert func.source_type is SourceType.WIKI

    def test_from_dict_tolerates_missing_keys(self):
        func = Function.from_dict({"id": "func_min"})
        assert func.id == "func_min"
        assert func.memory_type == "function"
        assert func.trigger == []
        assert func.created_at is not None  # __post_init__ fills it


class TestObservationSerialization:
    def test_roundtrip_all_fields(self):
        obs = Observation(
            id="obs_ser",
            name="Deploy Spike",
            domain="ops",
            confidence=0.6,
            source_type=SourceType.CODE,
            owner="bob",
            version=2,
            origin_session="sess_9",
            access_count=1,
            source_paragraphs=["p"],
            needs_review=True,
            needs_review_until="2026-03-01T00:00:00+00:00",
            content_hash="hash",
            event="latency spiked",
            context="after v3 rollout",
            observed_at="2026-01-02T03:04:05+00:00",
            actor="deploy-bot",
        )
        restored = Observation.from_dict(obs.to_dict())
        assert restored == obs

    def test_from_dict_defaults(self):
        obs = Observation.from_dict({"id": "obs_min"})
        assert obs.memory_type == "observation"
        assert obs.actor == "system"

    def test_category_roundtrip(self):
        obs = Observation(id="obs_cat", name="Cat", event="deploy fixed", category="bugfix")
        assert obs.to_dict()["category"] == "bugfix"
        restored = Observation.from_dict(obs.to_dict())
        assert restored.category == "bugfix"
        assert restored == obs

    def test_from_dict_missing_category_defaults_to_note(self):
        # Legacy serialized data predates the category key.
        d = Observation(id="obs_legacy", name="Legacy", event="e").to_dict()
        del d["category"]
        assert Observation.from_dict(d).category == "note"


# ── MemoryFeedback timezone normalization ────────────────────────────


class TestMemoryFeedbackTimezone:
    def test_aware_timestamp_normalized_to_naive(self):
        """Regression: Postgres TIMESTAMPTZ (asyncpg) yields tz-aware
        datetimes while lite/SQLite produce naive ones; mixing them raised
        TypeError on comparison/sort."""
        aware = datetime.now(timezone.utc)
        fb = MemoryFeedback(
            memory_id="m1",
            field_role="trigger",
            value_index=0,
            verdict=FeedbackVerdict.CORRECT,
            timestamp=aware,
        )
        assert fb.timestamp.tzinfo is None
        assert fb.timestamp == aware.replace(tzinfo=None)

    def test_naive_timestamp_unchanged(self):
        naive = datetime(2026, 1, 1, 12, 0, 0)
        fb = MemoryFeedback(
            memory_id="m1",
            field_role="trigger",
            value_index=0,
            verdict=FeedbackVerdict.WRONG,
            timestamp=naive,
        )
        assert fb.timestamp == naive

    def test_mixed_aware_and_naive_are_comparable(self):
        aware_fb = MemoryFeedback(
            memory_id="m1",
            field_role="trigger",
            value_index=0,
            verdict=FeedbackVerdict.CORRECT,
            timestamp=datetime.now(timezone.utc),
            needs_review_until=datetime.now(timezone.utc) + timedelta(days=7),
            resolved_at=datetime.now(timezone.utc),
        )
        naive_fb = MemoryFeedback(
            memory_id="m2",
            field_role="action",
            value_index=1,
            verdict=FeedbackVerdict.WRONG,
        )
        # Must not raise TypeError
        ordered = sorted([aware_fb, naive_fb], key=lambda fb: fb.timestamp)
        assert len(ordered) == 2
        assert aware_fb.needs_review_until.tzinfo is None
        assert aware_fb.resolved_at.tzinfo is None
