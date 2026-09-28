"""ADR-013 F3: rule-based offline consolidation (episodic -> sustained).

TSM (arXiv:2601.07468) reports up to +12.2% absolute on LongMemEval /
LoCoMo by consolidating temporally-adjacent, semantically-related point
memories into sustained memories on a semantic time axis, with
algorithmic forgetting of what does not consolidate. Auto-Dreamer trains
the same loop with GRPO; that component is closed for this project (RL
out of scope), so this module is the rule/heuristic version of the
architecture: same promotion/forgetting shape, no training, no LLM.

Mechanism (all offline - the query path is never touched):

* Promotion: raw paragraphs (the episodic layer) that rephrase the same
  statement across sessions graduate to a typed Fact node. Clustering is
  lexical (content-word Jaccard >= 0.4 plus a shared-token anchor), the
  gate is repetition: >= MEMPLEX_CONSOLIDATION_MIN_OBSERVATIONS rows in a
  cluster spanning >= MEMPLEX_CONSOLIDATION_MIN_SPAN_DAYS days. The node
  carries trust_tier = min of the cluster (merge-takes-min), namespace
  stamps {"consolidated": ts, "observations": n}, and the source
  paragraph ids. Cluster rows get a ``consolidated_into`` stamp, which
  makes promotion idempotent and marks them sustained (they then survive
  paragraph eviction).
* Forgetting: paragraphs older than
  MEMPLEX_CONSOLIDATION_PARAGRAPH_TTL_DAYS that were never consolidated
  are evicted from the episodic layer - algorithmic forgetting of
  one-off noise. Deletion rides the normal commit cycle (the store
  re-serializes the resident paragraph dict); a concurrent peer write
  that triggers the stale-base fold can delay the reclaim by one cycle
  (the fold cannot tell a maintenance delete from a missed peer write).

Disabled by default (MEMPLEX_CONSOLIDATION=1 enables). The pass never
raises into the host; every failure degrades to a report entry.

Known v1 limits, deliberately: verbatim repeats collapse to one
paragraph row at write time (content-addressed dedup), so the promotion
signal is rephrased repetition - counting exact repeats needs a
write-path observation counter (future work, keeps this pass offline).
Promotion does not guess fact vs preference: everything graduates as a
"stated" Fact rather than a lossily-classified Preference.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

_ENABLED_KEY = "MEMPLEX_CONSOLIDATION"
_MIN_OBSERVATIONS_KEY = "MEMPLEX_CONSOLIDATION_MIN_OBSERVATIONS"
_MIN_SPAN_DAYS_KEY = "MEMPLEX_CONSOLIDATION_MIN_SPAN_DAYS"
_PARAGRAPH_TTL_DAYS_KEY = "MEMPLEX_CONSOLIDATION_PARAGRAPH_TTL_DAYS"

_CONSOLIDATED_KEY = "consolidated_into"
_JACCARD_THRESHOLD = 0.4
_MIN_SHARED_TOKENS = 3

_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "have", "has",
    "was", "are", "were", "been", "will", "would", "could", "should",
    "about", "after", "into", "over", "under", "your", "their", "them",
    "they", "you", "her", "his", "our", "its", "not", "but", "all",
    "any", "some", "just", "now", "then", "than", "also", "very", "get",
    "got", "did", "does", "each", "out", "off", "down", "what", "when",
    "where", "which", "who", "how", "why", "can", "may", "might", "must",
    # Short function words kept out of the content set so token overlap
    # rides on nouns/verbs/version numbers instead of glue words.
    "a", "i", "we", "he", "she", "it", "is", "am", "to", "of", "in",
    "on", "at", "by", "if", "or", "as", "an", "so", "do", "go", "up",
    "no", "me", "my", "be",
}


def _content_tokens(text: str) -> list[str]:
    tokens = []
    for raw in text.lower().split():
        token = raw.strip(".,;:!?\"'()[]")
        if not token or token in _STOPWORDS:
            continue
        # Version-style tokens ("3.12", "docs.acme.dev") stay one unit.
        core = token.replace(".", "")
        if core and core.isalnum():
            tokens.append(token)
    return tokens


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _near_duplicate(text: str, cluster_texts: list[str]) -> bool:
    tokens = set(_content_tokens(text))
    for other in cluster_texts:
        other_tokens = set(_content_tokens(other))
        if (
            _jaccard(tokens, other_tokens) >= _JACCARD_THRESHOLD
            and len(tokens & other_tokens) >= _MIN_SHARED_TOKENS
        ):
            return True
    return False


@dataclass
class ConsolidationReport:
    """What one consolidation pass did; serializable to dict."""

    enabled: bool = False
    clusters: int = 0
    promoted: list[str] = field(default_factory=list)
    evicted: list[str] = field(default_factory=list)
    skipped_unparseable: int = 0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "clusters": self.clusters,
            "promoted": self.promoted,
            "evicted": self.evicted,
            "skipped_unparseable": self.skipped_unparseable,
            "note": self.note,
        }


def _env_int(key: str, default: int) -> int:
    try:
        return int(os.environ.get(key, str(default)))
    except ValueError:
        return default


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


def _cluster(
    parseable: dict[str, dict[str, Any]],
) -> list[list[tuple[str, dict[str, Any]]]]:
    """Greedy near-duplicate clustering of not-yet-consolidated rows."""
    clusters: list[list[tuple[str, dict[str, Any]]]] = []
    cluster_texts: list[list[str]] = []
    for row_id, row in parseable.items():
        if row.get(_CONSOLIDATED_KEY):
            continue
        text = str(row.get("raw_text") or "").strip()
        if not text:
            continue
        placed = False
        for i, texts in enumerate(cluster_texts):
            if _near_duplicate(text, texts):
                clusters[i].append((row_id, row))
                texts.append(text)
                placed = True
                break
        if not placed:
            clusters.append([(row_id, row)])
            cluster_texts.append([text])
    return clusters


def _canonical_text(rows: list[tuple[str, dict[str, Any]]]) -> str:
    """Most frequent row text; ties resolve to the earliest."""
    counts: dict[str, int] = {}
    order: dict[str, int] = {}
    for i, (_, row) in enumerate(rows):
        text = str(row.get("raw_text") or "").strip()
        counts[text] = counts.get(text, 0) + 1
        order.setdefault(text, i)
    return max(counts, key=lambda t: (counts[t], -order[t]))


def consolidate(store: Any, *, now: datetime | None = None) -> ConsolidationReport:
    """Run one offline consolidation pass over *store*; returns the report.

    *store* is any store exposing a ``_paragraphs`` dict of rows
    ({"id", "raw_text", "trust_tier", "created_at", ...}), an
    ``add_fact`` upsert and a ``deferred_commit`` context manager -
    i.e. the lite store or a facade over it. Facades that hide the
    paragraph layer make the pass a no-op (reported, not raised).
    """
    if os.environ.get(_ENABLED_KEY) != "1":
        return ConsolidationReport(
            note=f"{_ENABLED_KEY}=1 required (off by default)"
        )
    paragraphs = getattr(store, "_paragraphs", None)
    if not isinstance(paragraphs, dict):
        return ConsolidationReport(enabled=True, note="no paragraph layer exposed")

    min_observations = _env_int(_MIN_OBSERVATIONS_KEY, 3)
    min_span_days = _env_int(_MIN_SPAN_DAYS_KEY, 1)
    ttl_days = _env_int(_PARAGRAPH_TTL_DAYS_KEY, 90)
    report = ConsolidationReport(enabled=True)

    current = now or datetime.now(UTC)
    parseable: dict[str, dict[str, Any]] = {}
    for row_id, row in paragraphs.items():
        if not isinstance(row, dict):
            continue
        if _parse_ts(row.get("created_at")) is None:
            report.skipped_unparseable += 1
            continue
        parseable[row_id] = row

    clusters = _cluster(parseable)
    report.clusters = len(clusters)

    from memplex.models import Fact, SourceType

    promoted_facts: list[Fact] = []
    for rows in clusters:
        timestamps = [_parse_ts(r.get("created_at")) for _, r in rows]
        valid = [t for t in timestamps if t is not None]
        span_days = 0.0
        if len(valid) >= 2:
            span_days = (max(valid) - min(valid)).total_seconds() / 86400.0
        if len(rows) < min_observations or span_days < float(min_span_days):
            continue

        canonical = _canonical_text(rows)
        tiers = [int(r.get("trust_tier", 3)) for _, r in rows]
        node_id = "consol-" + hashlib.sha256(canonical.encode()).hexdigest()[:12]
        earliest = min(valid).isoformat()
        latest = max(valid).isoformat()
        fact = Fact(
            id=node_id,
            subject="user",
            predicate="stated",
            object_=canonical,
            source_type=SourceType.WIKI,
            trust_tier=min(tiers),
            created_at=earliest,
            updated_at=latest,
            source_paragraphs=[row_id for row_id, _ in rows],
        )
        fact.namespace = {
            "consolidated": current.isoformat(),
            "observations": str(len(rows)),
        }
        promoted_facts.append(fact)

    # Forgetting: episodic rows past the TTL that never consolidated.
    evict: list[str] = []
    for row_id, row in parseable.items():
        if row.get(_CONSOLIDATED_KEY):
            continue
        created = _parse_ts(row.get("created_at"))
        if created is None:
            continue
        if (current - created).total_seconds() > ttl_days * 86400.0:
            evict.append(row_id)

    if not promoted_facts and not evict:
        return report

    try:
        with store.deferred_commit():
            for fact in promoted_facts:
                store.add_fact(fact)
            for row_id in evict:
                paragraphs.pop(row_id, None)
            # Stamps ride the same atomic commit as the promotion, so a
            # crash cannot split the pair (re-promotion stays harmless
            # anyway: the node id is deterministic and add_fact merges).
            for fact in promoted_facts:
                for row_id in fact.source_paragraphs:
                    row = paragraphs.get(row_id)
                    if row is not None:
                        row[_CONSOLIDATED_KEY] = fact.id
    except Exception as exc:  # noqa: BLE001 - degradation, not a crash
        logger.debug("consolidation commit failed: %s", exc)
        report.note = f"commit failed: {exc}"
        return report

    report.promoted = [fact.id for fact in promoted_facts]
    report.evicted = evict
    return report
