"""Current-source checks limited to evidence-linked factual-capture records."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from typing import Any

from memplex.llm.injection_guard import IndirectInjectionGuard
from memplex.temporal import is_valid_at

# Semantic state only. Access counters and persistence timestamps change on
# ordinary reads and must not invalidate an otherwise unchanged derivation.
_SOURCE_FIELDS = frozenset({
    "raw_text", "subject", "predicate", "object", "action", "trigger", "precondition",
    "result", "condition", "benefit", "content_hash", "name", "name_normalized", "description", "preference",
    "aspect", "event", "context", "valid_from", "valid_until", "invalid_at", "trust_tier",
    "needs_review", "context_historical",
})


def source_fingerprint(node: Any) -> str:
    payload = node.to_dict()
    content = {key: value for key, value in payload.items() if key in _SOURCE_FIELDS}
    encoded = json.dumps(content, sort_keys=True, ensure_ascii=False, allow_nan=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def source_is_current(node: Any) -> bool:
    if getattr(node, "context_historical", False) or IndirectInjectionGuard.is_suspected(node):
        return False
    return getattr(node, "memory_type", "") != "fact" or is_valid_at(node)


def factual_sources_match(node: Any, lookup: Callable[[str], Any]) -> bool:
    """Do not serve a generated claim after its supporting content changes.

    This is a conservative re-capture boundary, not semantic re-inference.
    Existing non-factual-capture derivations retain their original behavior.
    """
    provenance = getattr(node, "provenance", {})
    if not isinstance(provenance, dict) or provenance.get("extraction") != "factual_capture_v1":
        return True
    try:
        expected = json.loads(provenance["source_snapshots"])
        refs = set(node.namespace["memplex_source_refs"].split(","))
        if type(expected) is not dict or not refs or set(expected) != refs:
            return False
        for source_id, fingerprint in expected.items():
            source = lookup(source_id)
            if (source is None or source.id != source_id or not source_is_current(source)
                    or source_fingerprint(source) != fingerprint):
                return False
    except Exception:  # noqa: BLE001 - uncertain or corrupt evidence fails closed
        return False
    return True


def capture_audit_provenance(node: Any) -> dict[str, str]:
    """Retain capture audit data, never caller identity or authentication claims.

    PostgreSQL rebinds write identity even for nodes already bound by the
    service. These fields describe inference and source evidence; they cannot
    supply a tenant, principal, agent, session, grant, or trust boundary.
    Unmarked legacy records keep the existing provenance-reset behavior.
    """
    provenance = getattr(node, "provenance", None)
    if not isinstance(provenance, dict) or not (
        provenance.get("extraction") == "factual_capture_v1"
        or provenance.get("capture_input") == "factual_capture_v1"
    ):
        return {}
    allowed = {
        "extraction", "authority", "author_role", "reference_datetime",
        "evidence", "source_snapshots", "capture_input",
    }
    return {key: value for key, value in provenance.items()
            if key in allowed and type(value) is str}
