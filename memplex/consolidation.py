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
  statement across sessions graduate to a typed node. Clustering is
  lexical (content-word Jaccard >= 0.4 plus a shared-token anchor) by
  default and semantic (cosine >= 0.85 on running cluster means) when an
  embedder is supplied. The gate is repetition: >=
  MEMPLEX_CONSOLIDATION_MIN_OBSERVATIONS rows in a cluster spanning >=
  MEMPLEX_CONSOLIDATION_MIN_SPAN_DAYS days. Preference-looking clusters
  graduate as Preference nodes; everything else as a "stated" Fact. The
  node carries trust_tier = min of the cluster (merge-takes-min),
  namespace stamps {"consolidated": ts, "observations": n}, and the
  source paragraph ids. Since v3 the node text also carries a synthesized
  cadence suffix (`` (observed N times across D days)``, N=observations,
  D=rounded span in days) whenever the cluster spans >= 1 day, so the
  frequency is retrievable from text, not just counted in the stamp.
  Cluster rows get a ``consolidated_into`` stamp,
  which makes promotion idempotent and marks them sustained (they then
  survive paragraph eviction).
* Forgetting: paragraphs older than
  MEMPLEX_CONSOLIDATION_PARAGRAPH_TTL_DAYS that were never consolidated
  are evicted from the episodic layer - algorithmic forgetting of
  one-off noise. Deletion rides the normal commit cycle (the store
  re-serializes the resident paragraph dict); a concurrent peer write
  that triggers the stale-base fold can delay the reclaim by one cycle
  (the fold cannot tell a maintenance delete from a missed peer write).

Disabled by default (MEMPLEX_CONSOLIDATION=1 enables). The pass never
raises into the host; every failure degrades to a report entry.

Known limits, deliberately: verbatim repeats collapse to one paragraph
row at write time (content-addressed dedup), so the promotion signal is
rephrased repetition - counting exact repeats needs a write-path
observation counter (future work, keeps this pass offline). Word-form
drift ("wednesday" vs "wednesdays") can split lexical clusters; the
embedder path covers it at the cost of a batch embedding pass.
"""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

_ENABLED_KEY = "MEMPLEX_CONSOLIDATION"
_MIN_OBSERVATIONS_KEY = "MEMPLEX_CONSOLIDATION_MIN_OBSERVATIONS"
_MIN_SPAN_DAYS_KEY = "MEMPLEX_CONSOLIDATION_MIN_SPAN_DAYS"
_PARAGRAPH_TTL_DAYS_KEY = "MEMPLEX_CONSOLIDATION_PARAGRAPH_TTL_DAYS"
_EMBED_THRESHOLD_KEY = "MEMPLEX_CONSOLIDATION_EMBED_THRESHOLD"

_CONSOLIDATED_KEY = "consolidated_into"
_JACCARD_THRESHOLD = 0.4
_MIN_SHARED_TOKENS = 3

_PREFERENCE_RE = re.compile(
    r"^(?:i|we|the user|my)\s+"
    r"(?:prefer|prefers|like|likes|love|loves|hate|hates|enjoy|enjoys|"
    r"want|wants|need|needs|use|uses|drink|drinks|eat|eats|wear|wears)\b",
    re.IGNORECASE,
)

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


def _env_float(key: str, default: float) -> float:
    try:
        return float(os.environ.get(key, str(default)))
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
    embedder: Any = None,
    threshold: float = 0.85,
) -> list[list[tuple[str, dict[str, Any]]]]:
    """Cluster not-yet-consolidated rows.

    Lexical (content-word Jaccard) by default; with an *embedder* (any
    object exposing ``embed_batch(texts) -> vectors``, e.g. the
    EmbeddingService) rows cluster by cosine similarity to the running
    cluster mean at >= *threshold* - this catches rephrases that share no
    surface form. Embedder failure falls back to lexical clustering.
    """
    if embedder is not None:
        try:
            return _cluster_embedded(parseable, embedder, threshold)
        except Exception as exc:  # noqa: BLE001 - degrade, never raise
            logger.debug("embedder clustering failed, lexical fallback: %s", exc)
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


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return dot / (norm_a * norm_b)


def _cluster_embedded(
    parseable: dict[str, dict[str, Any]],
    embedder: Any,
    threshold: float,
) -> list[list[tuple[str, dict[str, Any]]]]:
    rows = [
        (row_id, row, str(row.get("raw_text") or "").strip())
        for row_id, row in parseable.items()
        if not row.get(_CONSOLIDATED_KEY)
        and str(row.get("raw_text") or "").strip()
    ]
    if not rows:
        return []
    vectors = embedder.embed_batch([text for _, _, text in rows])
    clusters: list[list[tuple[str, dict[str, Any]]]] = []
    means: list[list[float]] = []
    for (row_id, row, _text), vector in zip(rows, vectors):
        placed = False
        for i, mean in enumerate(means):
            if _cosine(vector, mean) >= threshold:
                clusters[i].append((row_id, row))
                n = len(clusters[i])
                means[i] = [
                    ((n - 1) * m + v) / n for m, v in zip(mean, vector)
                ]
                placed = True
                break
        if not placed:
            clusters.append([(row_id, row)])
            means.append(list(vector))
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


def _cadence_suffix(observations: int, span_days: float) -> str:
    """Synthesized cadence for a promoted node's text facet ("" when the
    span is too short to carry one).

    A node that counts repetition only in a namespace stamp keeps the
    cadence off every text surface: retrieval then surfaces the evidence
    yet never the answer to "how often?". The format is a frozen
    contract (the habit-semantics probe keys on "times across"); a
    sub-day span rounds to 0 days and is deliberately left verbatim.
    """
    if span_days < 1.0:
        return ""
    return f" (observed {observations} times across {round(span_days)} days)"


def consolidate(
    store: Any,
    *,
    now: datetime | None = None,
    embedder: Any = None,
) -> ConsolidationReport:
    """Run one offline consolidation pass over *store*; returns the report.

    *store* is any store exposing a ``_paragraphs`` dict of rows
    ({"id", "raw_text", "trust_tier", "created_at", ...}), an
    ``add_fact``/``add_preference`` upsert and a ``deferred_commit``
    context manager - i.e. the lite store or a facade over it. Facades
    that hide the paragraph layer make the pass a no-op (reported, not
    raised). *embedder* (optional, ``embed_batch`` API) upgrades
    clustering from lexical to semantic at
    MEMPLEX_CONSOLIDATION_EMBED_THRESHOLD (default 0.85).
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
    embed_threshold = _env_float(_EMBED_THRESHOLD_KEY, 0.85)
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

    clusters = _cluster(parseable, embedder=embedder, threshold=embed_threshold)
    report.clusters = len(clusters)

    from memplex.models import Fact, Preference, SourceType

    promoted_nodes: list[tuple[str, Any]] = []
    for rows in clusters:
        # F3 observation counter: a row re-observed N times from the same
        # source carries observation_count=N (write-path dedup keeps one
        # row), so the repetition gate sums counts; legacy rows default
        # to 1 and reduce to the old row-count gate. The span runs from
        # first creation to last observation.
        timestamps = []
        observations = 0
        for _, r in rows:
            created = _parse_ts(r.get("created_at"))
            if created is not None:
                timestamps.append(created)
            last = _parse_ts(r.get("last_observed_at")) or created
            if last is not None:
                timestamps.append(last)
            observations += int(r.get("observation_count", 1) or 1)
        valid = [t for t in timestamps if t is not None]
        span_days = 0.0
        if len(valid) >= 2:
            span_days = (max(valid) - min(valid)).total_seconds() / 86400.0
        if observations < min_observations or span_days < float(min_span_days):
            continue

        canonical = _canonical_text(rows)
        tiers = [int(r.get("trust_tier", 3)) for _, r in rows]
        node_id = "consol-" + hashlib.sha256(canonical.encode()).hexdigest()[:12]
        # v3 cadence synthesis: the suffix rides the text facet so the
        # frequency is retrievable. The node id stays keyed on the raw
        # canonical text and add_* upserts replace the text wholesale, so
        # re-promotion of the same cluster yields a byte-identical node -
        # the suffix can never stack.
        text = canonical + _cadence_suffix(observations, span_days)
        earliest = min(valid).isoformat()
        latest = max(valid).isoformat()
        stamps = {
            "consolidated": current.isoformat(),
            "observations": str(observations),
        }
        if _PREFERENCE_RE.match(canonical):
            node = Preference(
                id=node_id,
                aspect="",
                preference=text,
                source_type=SourceType.WIKI,
                trust_tier=min(tiers),
                created_at=earliest,
                updated_at=latest,
                source_paragraphs=[row_id for row_id, _ in rows],
            )
            node.namespace = stamps
            promoted_nodes.append(("preference", node))
        else:
            fact = Fact(
                id=node_id,
                subject="user",
                predicate="stated",
                object_=text,
                source_type=SourceType.WIKI,
                trust_tier=min(tiers),
                created_at=earliest,
                updated_at=latest,
                source_paragraphs=[row_id for row_id, _ in rows],
            )
            fact.namespace = stamps
            promoted_nodes.append(("fact", fact))

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

    if not promoted_nodes and not evict:
        return report

    try:
        with store.deferred_commit():
            for kind, node in promoted_nodes:
                if kind == "preference":
                    store.add_preference(node)
                else:
                    store.add_fact(node)
            for row_id in evict:
                paragraphs.pop(row_id, None)
            # Stamps ride the same atomic commit as the promotion, so a
            # crash cannot split the pair (re-promotion stays harmless
            # anyway: the node id is deterministic and add_* merges).
            for _, node in promoted_nodes:
                for row_id in node.source_paragraphs:
                    row = paragraphs.get(row_id)
                    if row is not None:
                        row[_CONSOLIDATED_KEY] = node.id
    except Exception as exc:  # noqa: BLE001 - degradation, not a crash
        logger.debug("consolidation commit failed: %s", exc)
        report.note = f"commit failed: {exc}"
        return report

    report.promoted = [node.id for _, node in promoted_nodes]
    report.evicted = evict
    return report
