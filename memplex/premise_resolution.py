"""ADR-013 B1: inference-level premise resolution (maintenance pass).

The STALE baseline (resolved 0/25) showed the memory layer never resolves
implicit invalidation - a later observation that makes an earlier memory
outdated without any explicit negation. Rule-based ranking fixes measured
ineffective (new_before_old 0.04-0.12); the gap is inference-level state
resolution, exactly as the STALE paper diagnoses.

This module is the maintenance-time LLM pass: it groups topically
similar memories, asks the model whether the later statement supersedes
the earlier one, and marks the loser with a ``premise_superseded``
namespace stamp. Retrieval filters the stamp, so subsequent queries
surface only the fresh value - the memory-layer resolution the B1
target demands.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

_SUPERSEDED_KEY = "premise_superseded"

_PAIR_PROMPT = (
    "You are auditing a personal memory store for stale entries. Given "
    "two memories from the same person's history, determine whether the "
    "LATER one supersedes the EARLIER one.\n\n"
    "Supersession means: the later information REPLACES the earlier "
    "statement as the person's current state. Common patterns:\n"
    "- Moving to a new city/address (earlier home is outdated)\n"
    "- Starting a new job (earlier employer is outdated)\n"
    "- Switching tools/products/preferences (earlier choice outdated)\n"
    "- Schedule/route changes (earlier plan outdated)\n"
    "- Relationship/role changes (earlier contact outdated)\n\n"
    "NOT supersession: complementary details, unrelated topics, or the "
    "later merely adding context to the earlier.\n\n"
    "EARLIER (older timestamp):\n{earlier}\n\n"
    "LATER (newer timestamp):\n{later}\n\n"
    "Does the later statement replace the earlier as current? YES or NO only:"
)


def _timestamp_of(node: Any) -> str:
    return str(getattr(node, "updated_at", None) or getattr(node, "created_at", None) or "")


def _node_text(node: Any) -> str:
    for attr in ("name",):
        value = getattr(node, attr, None)
        if value:
            return str(value)
    return repr(node)[:200]


def _same_topic(a: Any, b: Any, threshold: float = 0.0) -> bool:
    """Gate before the (expensive) LLM call.

    Deliberately permissive: implicit-invalidation pairs are lexically
    distant ("I live in Berlin" vs "moving boxes arrived in Vienna") -
    any shared word admits the pair, and the LLM makes the actual
    supersession decision. Set ``threshold`` higher to prune aggressively.
    """
    wa = set(str(_node_text(a)).lower().split())
    wb = set(str(_node_text(b)).lower().split())
    if not wa or not wb:
        return False
    if threshold <= 0.0:
        return bool(wa & wb)
    return len(wa & wb) / len(wa | wb) >= threshold


def resolve_premises(
    store: Any,
    complete_fn: Any,
    *,
    max_pairs: int = 32,
) -> int:
    """Run one maintenance pass; returns the number superseded.

    *store* is any store exposing ``list_functions`` / ``list_facts`` /
    ``list_preferences`` and an upsert path (``replace_function`` /
    duck-typed). *complete_fn* is an LLM callable taking a prompt string
    and returning the model text (the authorized proxy in production).
    """
    nodes: list[Any] = []
    for lister in ("list_functions", "list_facts", "list_preferences"):
        fn = getattr(store, lister, None)
        if not callable(fn):
            continue
        try:
            nodes.extend(fn(limit=100000))
        except TypeError:
            nodes.extend(fn())
        except Exception as exc:  # noqa: BLE001 - best-effort maintenance
            logger.debug("premise listing via %s failed: %s", lister, exc)

    # Skip already-superseded nodes.
    def _superseded(node: Any) -> bool:
        namespace = getattr(node, "namespace", None) or {}
        return isinstance(namespace, dict) and namespace.get(_SUPERSEDED_KEY)

    candidates = [n for n in nodes if not _superseded(n)]
    # Newest-first ordering pairs each node with everything older.
    candidates.sort(key=_timestamp_of, reverse=True)

    stamped = 0
    checked = 0
    for i, newer in enumerate(candidates):
        if checked >= max_pairs:
            break
        for older in candidates[i + 1 :]:
            if checked >= max_pairs:
                break
            if _timestamp_of(older) >= _timestamp_of(newer):
                continue
            if not _same_topic(newer, older):
                continue
            checked += 1
            try:
                verdict = complete_fn(
                    _PAIR_PROMPT.format(
                        earlier=_node_text(older), later=_node_text(newer)
                    )
                )
            except Exception as exc:  # noqa: BLE001 - LLM failure skips the pair
                logger.debug("premise pair LLM call failed: %s", exc)
                continue
            if "YES" not in str(verdict).upper()[:8]:
                continue
            namespace = getattr(older, "namespace", None)
            if not isinstance(namespace, dict):
                namespace = {}
                older.namespace = namespace
            namespace[_SUPERSEDED_KEY] = datetime.now(UTC).isoformat()
            stamped += 1
            _persist(store, older)
            # Also stamp the raw paragraphs this node references: the
            # paragraph layer retrieves verbatim text, which would
            # otherwise resurface the stale value even with the typed
            # node filtered.
            _stamp_paragraphs(store, older)

    if stamped:
        logger.info("premise resolution: %d nodes superseded", stamped)
    return stamped


def _stamp_paragraphs(store: Any, node: Any) -> None:
    """Mark the raw paragraphs a superseded node references."""
    paragraphs = getattr(store, "_paragraphs", None)
    if not isinstance(paragraphs, dict):
        return
    stamp = datetime.now(UTC).isoformat()
    for ref in getattr(node, "source_paragraphs", []) or []:
        row = paragraphs.get(ref)
        if row is not None:
            row["premise_superseded"] = stamp


def _persist(store: Any, node: Any) -> None:
    """Best-effort persistence of the namespace stamp."""
    for upsert_name in ("replace_function", "add", "add_fact", "add_preference"):
        upsert = getattr(store, upsert_name, None)
        if callable(upsert):
            try:
                if upsert_name == "replace_function":
                    upsert(node)
                else:
                    upsert(node)
                return
            except Exception as exc:  # noqa: BLE001 - logged, try next path
                logger.debug("premise persist via %s failed: %s", upsert_name, exc)


def is_premise_superseded(node_or_namespace: Any) -> bool:
    """Retrieval filter predicate: True when the node is superseded."""
    namespace = node_or_namespace
    if node_or_namespace is None:
        return False
    if hasattr(node_or_namespace, "namespace"):
        namespace = getattr(node_or_namespace, "namespace", None)
    return isinstance(namespace, dict) and bool(namespace.get(_SUPERSEDED_KEY))
