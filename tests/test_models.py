"""Test data models: FieldValue, Function, Fact, Preference, Observation,
validate_func_id, MemoryNode type system."""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from datetime import datetime, timezone

import pytest

from memplex.models import (
    MAX_FUNC_ID_LENGTH,
    EdgeType,
    FeedbackVerdict,
    FieldValue,
    GraphData,
    GraphEdge,
    IntentType,
    QueryScope,
    SourceType,
    validate_func_id,
)
from memplex.models.memory import (
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
