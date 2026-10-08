"""Small, deterministic inputs and a literal planner for typed-batch tests."""

from memplex.models import Fact, Preference
from memplex.storage.typed_batch import (
    TypedBatchInput,
    TypedBatchSnapshot,
    TypedWriteOperation,
    TypedWritePlan,
)


def make_fact(node_id: str, value: str = "value") -> Fact:
    return Fact(id=node_id, subject="alice", predicate="likes", object_=value)


def make_preference(node_id: str, value: str = "dark") -> Preference:
    return Preference(id=node_id, aspect="theme", preference=value)


def literal_plan(snapshot: TypedBatchSnapshot, inputs: TypedBatchInput) -> TypedWritePlan:
    return TypedWritePlan(
        operations=tuple(
            TypedWriteOperation(node=node, input_index=index, purpose="input")
            for index, node in enumerate((*inputs.facts, *inputs.preferences))
        ),
        fact_valid_from=(),
    )
