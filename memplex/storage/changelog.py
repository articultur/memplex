"""ChangelogStore -- lightweight append-only event log.

Lite implementation: in-memory list persisted to a JSON file.
"""

from __future__ import annotations

import json
import logging
import tempfile
from datetime import datetime
from pathlib import Path
from typing import List, Optional

from memplex.models import ChangelogEvent

logger = logging.getLogger(__name__)


class ChangelogStore:
    """Append-only changelog with JSON persistence.

    Data path: ``~/.memplex/changelog.json``
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self._path = path or Path("~/.memplex/changelog.json").expanduser()
        self._events: List[ChangelogEvent] = []
        self._load()

    # ── Public API ──────────────────────────────────────────────────

    def append(self, event: ChangelogEvent) -> None:
        """Append an event and persist."""
        self._events.append(event)
        self._save()

    def get_timeline(
        self,
        func_id: str,
        limit: int = 20,
    ) -> List[ChangelogEvent]:
        """Return the most recent events for *func_id*, newest first."""
        matching = [e for e in self._events if e.func_id == func_id]
        matching.sort(key=lambda e: e.timestamp, reverse=True)
        return matching[:limit]

    def clear(self) -> None:
        """Remove all events."""
        self._events.clear()
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
        ts = data["timestamp"]
        if isinstance(ts, str):
            ts = datetime.fromisoformat(ts)
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
