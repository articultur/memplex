"""ADR-013 F-series: cross-session entity hubs (product port of v13).

The v13 harness recipe (J 0.894 -> 0.916's largest single-pool lever):
materialize cross-session links as searchable records. Every entity or
recurring theme that spans sessions gets one hub whose text enumerates
the sessions (with dates and short quotes) mentioning it; counting and
list-everything questions then hit the pre-computed enumeration instead
of re-deriving it from raw turns at answer time. The controlled graph
experiment (arXiv 2601.01280) showed cross-session links at session
granularity beat flat memory by +13pp with the same answerer.

Product shape (maintenance pass, like premise_resolution/consolidation):

* Sessions are inferred from the paragraph layer: rows grouped by their
  ``source`` hint, ordered by first-seen timestamp, each summarized to
  the first 1,500 characters of joined text (same budget as the harness
  recipe).
* An LLM pass (``complete_fn``) lists recurring anchors in the frozen
  ``ENTITY || SESSIONS || EVIDENCE`` line format.
* Each anchor becomes a Function whose name leads with the entity (so
  lexical/semantic legs rank it whenever a question names the entity)
  and whose action field carries the enumeration. The id is derived
  from entity+sessions, so re-running the pass never duplicates hubs.
* No query-side gating in v1: the harness's type gating protected
  benchmark pools from hub noise; in the product the entity-leading
  name is the natural gate (questions that do not name the entity do
  not lexically hit the hub).

Disabled by default (``MEMPLEX_ENTITY_HUBS=1``); never raises into the
host.
"""

from __future__ import annotations

import hashlib
import logging
import os
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

_ENABLED_KEY = "MEMPLEX_ENTITY_HUBS"
_HUB_KEY = "entity_hub"
_MAX_SESSIONS = 24
_SESSION_CHAR_BUDGET = 1500
_MAX_HUBS = 40

_EXTRACT_PROMPT = (
    "List every recurring cross-session anchor in the sessions below: "
    "named entities (person/place/item) AND recurring themes -- "
    "activities, obligations, purchases, errands, plans the user tracks "
    "across sessions (e.g. 'things to pick up', 'trip planning'). "
    "Counting and list-everything questions will use these, so include "
    "every item even if each appears in only ONE session when they "
    "belong to a shared theme. Output one line per anchor in exactly "
    "this format:\n"
    "ENTITY: <name> || SESSIONS: <comma-separated session numbers> || "
    "EVIDENCE: <one short verbatim quote per session>\n"
    "No other text.\n\n{corpus}"
)


@dataclass
class HubReport:
    """What one hub pass did; serializable to dict."""

    enabled: bool = False
    sessions: int = 0
    hubs: int = 0
    created: list[str] = field(default_factory=list)
    skipped_existing: int = 0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "sessions": self.sessions,
            "hubs": self.hubs,
            "created": self.created,
            "skipped_existing": self.skipped_existing,
            "note": self.note,
        }


def parse_hub_lines(text: str) -> list[dict[str, Any]]:
    """Parse the frozen ``ENTITY || SESSIONS || EVIDENCE`` output format."""
    parsed: list[dict[str, Any]] = []
    for line in text.splitlines():
        if "ENTITY:" not in line or "SESSIONS:" not in line:
            continue
        try:
            head, rest = line.split("SESSIONS:", 1)
            entity = head.split("ENTITY:", 1)[1].split("||")[0].strip()
            session_nums, evidence = rest.split("||", 1)
            nums = [
                int(n.strip())
                for n in session_nums.replace("EVIDENCE:", "").split(",")
                if n.strip().isdigit()
            ]
        except (ValueError, IndexError):
            continue
        if entity and nums:
            parsed.append(
                {
                    "entity": entity[:80],
                    "sessions": nums,
                    "evidence": evidence.strip()[:400],
                }
            )
    return parsed[:_MAX_HUBS]


def _sessions_from_paragraphs(
    paragraphs: dict[str, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Group paragraph rows by source hint into ordered sessions."""
    by_source: dict[str, list[dict[str, Any]]] = {}
    for row in paragraphs.values():
        if not isinstance(row, dict):
            continue
        if row.get("premise_superseded"):
            continue
        source = str(row.get("source") or "")[:200] or "default"
        by_source.setdefault(source, []).append(row)
    sessions: list[dict[str, Any]] = []
    for source in sorted(by_source):
        rows = sorted(
            by_source[source], key=lambda r: str(r.get("created_at") or "")
        )
        date = str(rows[0].get("created_at") or "")[:10]
        text = " | ".join(str(r.get("raw_text") or "") for r in rows)
        sessions.append(
            {"source": source, "date": date, "text": text[:_SESSION_CHAR_BUDGET]}
        )
    return sessions[:_MAX_SESSIONS]


def build_entity_hubs(store: Any, complete_fn: Any) -> HubReport:
    """Run one hub-building maintenance pass over *store*.

    *store* is any store exposing ``_paragraphs`` (dict rows) and an
    ``add`` upsert; *complete_fn* is an LLM callable taking a prompt and
    returning text. Facades without the paragraph layer make the pass a
    no-op (reported, never raised).
    """
    if os.environ.get(_ENABLED_KEY) != "1":
        return HubReport(note=f"{_ENABLED_KEY}=1 required (off by default)")
    paragraphs = getattr(store, "_paragraphs", None)
    if not isinstance(paragraphs, dict) or not paragraphs:
        return HubReport(enabled=True, note="no paragraph layer exposed")

    sessions = _sessions_from_paragraphs(paragraphs)
    if len(sessions) < 2:
        return HubReport(
            enabled=True,
            sessions=len(sessions),
            note="hubs need >= 2 sessions (source groups)",
        )

    numbered = [
        f"Session {i} [{s['date']}]: {s['text']}"
        for i, s in enumerate(sessions)
    ]
    corpus = "\n".join(numbered)[:28000]
    try:
        text = complete_fn(_EXTRACT_PROMPT.format(corpus=corpus))
    except Exception as exc:  # noqa: BLE001 - hubs are best-effort
        logger.debug("entity-hub extraction failed: %s", exc)
        return HubReport(enabled=True, sessions=len(sessions), note=f"extraction failed: {exc}")

    records = parse_hub_lines(text or "")
    report = HubReport(enabled=True, sessions=len(sessions), hubs=len(records))

    from memplex.models import FieldValue, Function, SourceType

    for record in records:
        entity = str(record["entity"])
        nums = [int(n) for n in record["sessions"]]
        node_id = "hub-" + hashlib.sha256(
            f"{entity}|{sorted(nums)}".encode()
        ).hexdigest()[:12]
        if node_id in getattr(store, "_functions", {}):
            report.skipped_existing += 1
            continue
        first = sessions[nums[0]] if nums[0] < len(sessions) else None
        prefix = f"[{first['date']}] " if first and first.get("date") else ""
        body = (
            f"{prefix}Cross-session entity '{entity}' appears in sessions "
            f"{nums}. {record['evidence']}"
        )[:1500]
        hub = Function(
            id=node_id,
            name=entity,
            name_normalized=entity.lower(),
            domain="entity-hub",
            source_type=SourceType.WIKI,
            action=[FieldValue(desc=body)],
        )
        hub.namespace = {_HUB_KEY: entity}
        try:
            from memplex.models import SourceDocument

            store.add(hub, SourceDocument(type="hub_update", source_type=hub.source_type))
            report.created.append(node_id)
        except TypeError:
            # Duck-typed stores with a single-argument upsert (test doubles).
            add = getattr(store, "add", None)
            if callable(add):
                add(hub)
                report.created.append(node_id)
    return report
