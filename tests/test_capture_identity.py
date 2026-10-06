"""Capture-only identity transforms keep authority out of conversation text."""

from dataclasses import replace

import pytest

from memplex.auth import AuthorizationContext, Principal, bind_node_identity
from memplex.capture_identity import (
    capture_scope,
    capture_scopes_match,
    capture_source_hint,
    is_captured,
    scope_captured_data,
)
from memplex.models import ExtractedData, Fact, Function, GraphData, GraphEdge, Preference


def _context(**changes):
    context = AuthorizationContext(
        Principal("tenant", "alice"), "workspace", agent_id="codex", session_id="first",
    )
    return replace(context, **changes)


def _bound(node, context=None, visibility="workspace"):
    return bind_node_identity(node, context or _context(), visibility=visibility)


def test_capture_rekeys_shared_objects_once_and_only_internal_graph_endpoints():
    function = _bound(Function(id="func_original", name="Capture action", name_normalized="capture action"))
    fact = _bound(Fact(id="fact_original", subject="parcel", predicate="is", object_="amber"))
    preference = _bound(Preference(id="pref_original", name="Preference"))
    function.source_paragraphs = ["raw_unchanged"]
    function.content_hash = "original-content-hash"
    graph = GraphData(nodes=[function], edges=[
        GraphEdge("func_original", "fact_original", "REFERENCES"),
        GraphEdge("func_original", "func_external", "REFERENCES"),
        GraphEdge("func_original", "domain_example", "BELONGS_TO"),
    ])
    extracted = ExtractedData(functions=[function], facts=[fact], preferences=[preference], graph=graph)

    scope_captured_data(extracted)

    assert function is graph.nodes[0]
    assert all(is_captured(node) for node in [function, fact, preference])
    assert graph.edges[0].source == function.id
    assert graph.edges[0].target == fact.id
    assert graph.edges[1].target == "func_external"
    assert graph.edges[2].target == "domain_example"
    assert function.name == "Capture action"
    assert function.name_normalized != "capture action"
    assert "alice" not in function.name_normalized
    assert function.source_paragraphs == ["raw_unchanged"]
    assert function.content_hash == "original-content-hash"


@pytest.mark.parametrize("visibility,same", [("workspace", True), ("session", False), ("user", True)])
def test_typed_capture_identity_obeys_visibility_across_fresh_sessions(visibility, same):
    ids = []
    for session in ["first", "fresh"]:
        fact = _bound(Fact(id="fact_content"), _context(session_id=session), visibility)
        scope_captured_data(ExtractedData(facts=[fact]))
        ids.append(fact.id)
    assert (ids[0] == ids[1]) is same


def test_raw_source_identity_is_finer_than_workspace_capture_identity():
    first = _context()
    fresh = _context(session_id="fresh")

    assert capture_scope(_bound(Fact(), first)) == capture_scope(_bound(Fact(), fresh))
    assert capture_source_hint(first) != capture_source_hint(fresh)
    assert "alice" not in capture_source_hint(first)
    assert "workspace" not in capture_source_hint(first)


@pytest.mark.parametrize("field,value", [("tenant_id", None), ("owner_subject_id", ""), ("visibility", "unknown")])
def test_capture_guard_does_not_fall_back_to_editable_namespace(field, value):
    captured = _bound(Fact(id="fact_capture_v1_example"))
    other = _bound(Fact(id="fact_direct"))
    setattr(captured, field, value)
    captured.namespace.update({
        "memplex_tenant_id": "tenant", "memplex_subject_id": "alice",
        "memplex_visibility": "workspace", "memplex_capture_scope": "spoofed",
    })

    assert is_captured(captured)
    assert capture_scope(captured) is None
    assert not capture_scopes_match(captured, other)
    assert not capture_scopes_match(other, captured)


def test_namespace_annotation_cannot_remove_or_create_capture_marker():
    captured = _bound(Fact(id="fact_capture_v1_example"))
    ordinary = _bound(Fact(id="fact_direct"))
    captured.namespace.clear()
    ordinary.namespace["memplex_capture_scope"] = "capture-v1"

    assert is_captured(captured)
    assert not is_captured(ordinary)
    assert capture_scopes_match(captured, ordinary)


def test_unmarked_fact_pairs_keep_existing_combination_behavior():
    first = _bound(Fact(id="fact_first"))
    other = _bound(Fact(id="fact_other"), _context(principal=Principal("other", "bob")))

    assert capture_scopes_match(first, other)


def test_capture_rekey_rejects_incomplete_canonical_identity():
    extracted = ExtractedData(facts=[Fact(id="fact_original")])

    with pytest.raises(ValueError, match="complete canonical identity"):
        scope_captured_data(extracted)
    assert extracted.facts[0].id == "fact_original"


@pytest.mark.parametrize("field,value", [("visibility", []), ("provenance", None), ("provenance", [])])
def test_malformed_capture_scope_fails_closed_instead_of_raising(field, value):
    captured = _bound(Fact(id="fact_capture_v1_example"), visibility="session")
    setattr(captured, field, value)

    assert capture_scope(captured) is None


@pytest.mark.parametrize("visibility", ["user", "workspace"])
@pytest.mark.parametrize("change", [{"session_id": "fresh"}, {"workspace_id": "other"}, {"agent_id": "hermes"}])
def test_function_physical_identity_separates_immutable_postgres_writers(visibility, change):
    functions = []
    for context in [_context(), _context(**change)]:
        function = _bound(Function(id="func_content", name="Same name"), context, visibility)
        scope_captured_data(ExtractedData(functions=[function], graph=GraphData(nodes=[function])))
        functions.append(function)

    assert functions[0].id != functions[1].id
    assert functions[0].name_normalized != functions[1].name_normalized
    assert functions[0].name == functions[1].name == "Same name"


def test_capture_graph_rekey_drops_alias_self_edges_and_duplicate_edges():
    previous = _bound(Function(id="func_same", name="Recorded"))
    scope_captured_data(ExtractedData(functions=[previous]))
    current = _bound(Function(id="func_same", name="Recorded"))
    graph = GraphData(nodes=[current], edges=[
        GraphEdge("func_same", previous.id, "ASSOCIATED_WITH"),
        GraphEdge("func_same", "func_external", "REFERENCES"),
        GraphEdge(previous.id, "func_external", "REFERENCES"),
    ])

    scope_captured_data(ExtractedData(functions=[current], graph=graph))

    assert [(edge.source, edge.target, edge.edge_type) for edge in graph.edges] == [
        (current.id, "func_external", "REFERENCES"),
    ]
