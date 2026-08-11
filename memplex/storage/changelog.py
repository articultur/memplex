"""ChangelogStore -- lightweight append-only event log.

Lite implementation: in-memory list persisted to a JSON file.
"""

from __future__ import annotations

import copy
import json
import logging
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from memplex.models import ChangelogEvent

logger = logging.getLogger(__name__)

_EVENT_KEYS = {"func_id", "timestamp", "event_type", "description", "source", "actor"}


class ChangelogStore:
    """Append-only changelog with JSON persistence.

    Data path: ``~/.memplex/changelog.json``
    """

    def __init__(self, path: Optional[Path] = None, *, managed: bool = False) -> None:
        self._path = path or Path("~/.memplex/changelog.json").expanduser()
        self._managed = managed
        self._events: List[ChangelogEvent] = []
        if not managed:
            self._load()

    # ── Public API ──────────────────────────────────────────────────

    def append(self, event: ChangelogEvent) -> None:
        """Append an event and persist."""
        self._events.append(copy.deepcopy(event))
        if not self._managed:
            self._save()

    def snapshot(self) -> List[ChangelogEvent]:
        """Return a detached snapshot; managed Lite owns disk publication."""
        return copy.deepcopy(self._events)

    def replace(self, events: List[ChangelogEvent]) -> None:
        """Replace in-memory events.  Managed stores never write independently."""
        self._events = copy.deepcopy(events)
        if not self._managed:
            self._save()

    def get_timeline(
        self,
        func_id: str,
        limit: int = 20,
    ) -> List[ChangelogEvent]:
        """Return the most recent events for *func_id*, newest first."""
        matching = [e for e in self._events if e.func_id == func_id]
        matching.sort(key=lambda e: e.timestamp, reverse=True)
        return copy.deepcopy(matching[:limit])

    def clear(self) -> None:
        """Remove all events."""
        self._events.clear()
        if not self._managed:
            self._save()

    # ── Persistence helpers ─────────────────────────────────────────

    @staticmethod
    def _serialize_event(event: ChangelogEvent) -> dict:
        return {
            "func_id": event.func_id,
            "timestamp": (
                event.timestamp.isoformat()
                if isinstance(event.timestamp, datetime)
                else event.timestamp
            ),
            "event_type": event.event_type,
            "description": event.description,
            "source": event.source,
            "actor": event.actor,
        }

    @staticmethod
    def _deserialize_event(data: dict) -> ChangelogEvent:
        # Changelog events participate in the authoritative Lite pair.  They
        # must be fully typed before publication; otherwise a bool/number
        # timestamp can survive recovery and crash timeline sorting later.
        if type(data) is not dict or set(data) != _EVENT_KEYS:
            raise ValueError("invalid changelog event schema")
        for name in ("func_id", "event_type", "description", "source", "actor"):
            if type(data[name]) is not str:
                raise ValueError(f"invalid changelog event {name}")
        if not data["func_id"] or not data["event_type"] or not data["description"] or not data["actor"]:
            raise ValueError("empty required changelog event field")
        timestamp = data["timestamp"]
        if type(timestamp) is not str or "T" not in timestamp:
            raise ValueError("invalid changelog event timestamp")
        try:
            ts = datetime.fromisoformat(timestamp)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid changelog event timestamp") from exc
        return ChangelogEvent(
            func_id=data["func_id"],
            timestamp=ts,
            event_type=data["event_type"],
            description=data["description"],
            source=data["source"],
            actor=data["actor"],
        )

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            self._events = [self._deserialize_event(d) for d in raw]
        except Exception:
            logger.warning("Failed to load changelog from %s", self._path)

    def _save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        data = [self._serialize_event(e) for e in self._events]
        # Atomic write: write to temp then rename
        tmp_fd, tmp_path = tempfile.mkstemp(dir=str(self._path.parent), suffix=".tmp")
        try:
            with open(tmp_fd, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            Path(tmp_path).replace(self._path)
        except Exception:
            Path(tmp_path).unlink(missing_ok=True)
            raise
