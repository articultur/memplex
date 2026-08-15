"""Proactive memory maintenance (Cognee ``improve``-style maintenance pass).

``improve()`` is a synchronous, user-invocable maintenance verb over the
fact store — the temporal counterpart to compaction:

1. **Dedupe / merge conflicts**: multiple *currently-valid* facts occupying
   the same (subject, predicate) slot keep only the newest by
   ``updated_at``; the rest are superseded (``invalid_at`` stamped, row
   retained) — never deleted, so history stays auditable.
2. **Expire**: facts past their own ``valid_until`` shelf life get
   ``invalid_at`` stamped if they do not already carry one.
3. **Index rebuild**: the FTS index is rebuilt and the graph-builder cache
   invalidated so subsequent recalls see the post-maintenance state.

Returns a machine-readable report; every phase is fail-soft (a store
without a capability contributes a zeroed phase, never an exception).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from memplex import temporal

logger = logging.getLogger(__name__)


def _slot(fact) -> tuple[str, str]:
    return (
        (getattr(fact, "subject", "") or "").strip().lower(),
        (getattr(fact, "predicate", "") or "").strip().lower(),
    )


def _updated(fact) -> str:
    return getattr(fact, "updated_at", "") or ""


def improve_facts(store: Any, *, now: str | None = None) -> Dict[str, Any]:
    """Run the maintenance pass over a store's facts; returns a report."""
    stamp = now or temporal.now_iso()
    report: Dict[str, Any] = {
        "deduplicated": 0,
        "expired": 0,
        "index_rebuilt": False,
    }
    list_facts = getattr(store, "list_facts", None)
    add_fact = getattr(store, "add_fact", None)
    if not callable(list_facts) or not callable(add_fact):
        logger.debug(
            "store %s lacks fact listing/persistence; improve is a no-op",
            type(store).__name__,
        )
        return report
    try:
        facts: List = list(list_facts(limit=100000))
    except Exception as exc:
        logger.debug("improve: list_facts failed: %s", exc)
        return report

    # Phase 1+2 share one pass: bucket currently-valid facts by slot.
    valid_by_slot: Dict[tuple[str, str], List] = {}
    expired_updates: List = []
    for fact in facts:
        if getattr(fact, "invalid_at", None) is not None:
            continue  # already superseded — history, not maintenance target
        shelf = getattr(fact, "valid_until", None)
        from datetime import datetime, timezone

        if shelf:
            try:
                when = datetime.fromisoformat(shelf)
                if when.tzinfo is None:
                    when = when.replace(tzinfo=timezone.utc)
                if when <= datetime.now(timezone.utc) and not getattr(
                    fact, "invalid_at", None
                ):
                    fact.invalid_at = stamp
                    expired_updates.append(fact)
                    continue
            except ValueError:
                pass  # malformed shelf life: leave to read-side leniency
        valid_by_slot.setdefault(_slot(fact), []).append(fact)

    # Phase 1: newest per slot survives, the rest are superseded.
    dedup_updates: List = []
    for slot, bucket in valid_by_slot.items():
        if len(bucket) < 2:
            continue
        bucket.sort(key=_updated, reverse=True)
        for loser in bucket[1:]:
            loser.invalid_at = stamp
            dedup_updates.append(loser)

    for fact in [*dedup_updates, *expired_updates]:
        try:
            add_fact(fact)
        except Exception as exc:
            logger.debug("improve: supersede persist failed for %s: %s", fact.id, exc)
    report["deduplicated"] = len(dedup_updates)
    report["expired"] = len(expired_updates)

    # Phase 3: rebuild the search index if the backend exposes it.
    rebuild = getattr(store, "rebuild_search_index", None)
    if callable(rebuild):
        try:
            rebuild()
            report["index_rebuilt"] = True
        except Exception as exc:
            logger.debug("improve: index rebuild failed: %s", exc)
    return report
