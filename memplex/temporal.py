"""Bi-temporal fact validity (Zep/Graphiti-style supersede semantics).

Business time vs system time:
- ``valid_from`` / ``invalid_at`` on :class:`~memplex.models.Fact` describe
  the interval a fact was TRUE in the world (business time).
- ``created_at`` / ``updated_at`` (inherited) record when the system learned
  it (system time). Keeping both axes means a query can ask both "what did we
  believe on date X" and "what was true on date X".

Semantics:
- A stored fact with ``invalid_at is None`` is currently valid.
- When a new fact arrives with the same (subject, predicate) and a different
  object, the stored one is **superseded, not deleted**: ``invalid_at`` is
  stamped with the new fact's ``valid_from``. Supersession is blocked across
  tenant boundaries — the caller passes the facts its store scope returned.
- ``as_of`` filtering selects facts valid at an arbitrary point in time,
  which is what makes "agent 改口" (changed its mind) history auditable.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from memplex.models import Fact


def now_iso() -> str:
    """UTC now in the canonical ISO form used across memplex timestamps."""
    return datetime.now(UTC).isoformat()


def _key(fact: Fact) -> tuple[str, str]:
    """Stable contradiction key: same subject+predicate ⇒ same claim slot."""
    return (
        (getattr(fact, "subject", "") or "").strip().lower(),
        (getattr(fact, "predicate", "") or "").strip().lower(),
    )


def is_valid_at(fact: Fact, as_of: datetime | None = None) -> bool:
    """Whether *fact*'s business-time interval covers *as_of* (default now).

    Invalid dates on the fields are treated as absent rather than raising:
    a malformed stamp must never hide a fact from a security-relevant read.
    """
    when = as_of or datetime.now(UTC)

    def parse(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed

    start = parse(getattr(fact, "valid_from", None))
    end = parse(getattr(fact, "invalid_at", None))
    shelf = parse(getattr(fact, "valid_until", None))  # pre-existing expiry field
    if start is not None and when < start:
        return False
    if end is not None and when >= end:
        return False
    return not (shelf is not None and when >= shelf)


def supersede_contradicted(
    new_fact: Fact, existing: Iterable[Fact], *, now: str | None = None
) -> list[Fact]:
    """Return the stored facts *new_fact* supersedes, stamped ``invalid_at``.

    Mutation contract: only facts sharing the (subject, predicate) slot that
    are still valid AND carry a different id are superseded; the caller
    persists the returned facts (upsert by id). ``valid_until`` expiry is a
    separate concern — a fact past its own shelf life is not "contradicted".
    """
    stamp = now or now_iso()
    slot = _key(new_fact)
    new_id = getattr(new_fact, "id", None)
    superseded: list[Fact] = []
    for fact in existing:
        if getattr(fact, "id", None) == new_id:
            continue
        if _key(fact) != slot:
            continue
        if getattr(fact, "invalid_at", None) is not None:
            continue  # already superseded
        if not is_valid_at(fact):
            continue  # expired by its own valid_until
        fact.invalid_at = stamp
        superseded.append(fact)
    return superseded


def facts_valid_at(facts: Iterable[Fact], as_of: datetime | None = None) -> list[Fact]:
    """Filter an iterable of facts down to those valid at *as_of*."""
    return [fact for fact in facts if is_valid_at(fact, as_of)]
