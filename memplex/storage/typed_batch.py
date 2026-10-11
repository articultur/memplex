"""Optional typed-write values shared by the service and native stores.

These frozen containers do not make their mutable model nodes immutable.
Callers must deep-copy nodes at planning and storage admission boundaries.
Input indices always enumerate facts first, then preferences; supersession
operations refer to existing facts and therefore have no input index.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from memplex.models import Fact, Preference

TypedNode = Fact | Preference


@dataclass(frozen=True)
class TypedBatchInput:
    """Incoming nodes, with duplicates and caller order preserved."""

    facts: tuple[Fact, ...]
    preferences: tuple[Preference, ...]


@dataclass(frozen=True)
class TypedBatchSnapshot:
    """The store generation and its existing fact-list snapshot."""

    generation: int
    facts: tuple[Fact, ...]


@dataclass(frozen=True)
class TypedWriteOperation:
    """One ordered input or existing-fact supersession write."""

    node: TypedNode
    input_index: int | None
    purpose: Literal["input", "supersede"]


@dataclass(frozen=True)
class TypedWritePlan:
    """Detached operations and generated incoming-fact timestamp supplements."""

    operations: tuple[TypedWriteOperation, ...]
    fact_valid_from: tuple[tuple[int, str], ...]


@dataclass(frozen=True)
class TypedWriteRejection:
    """A rejected operation without a successful persistence acknowledgement."""

    operation_index: int
    input_index: int | None
    memory_id: str
    code: str


@dataclass(frozen=True)
class TypedBatchResult:
    """Committed acknowledgements or an unsupported/no-accepted-write result.

    Unsupported results have empty sequences and no commit information; the
    runner must not call the planner or mutate state. Supported batches with
    no accepted operations likewise have no committed generation/transaction.
    """

    supported: bool
    committed_generation: int | None
    transaction_id: str | None
    successful_input_indices: tuple[int, ...]
    committed_superseded_ids: tuple[str, ...]
    rejected: tuple[TypedWriteRejection, ...]
    fact_valid_from: tuple[tuple[int, str], ...]


TypedWritePlanner = Callable[[TypedBatchSnapshot, TypedBatchInput], TypedWritePlan]
TypedBatchRunner = Callable[[TypedBatchInput, TypedWritePlanner], TypedBatchResult]
