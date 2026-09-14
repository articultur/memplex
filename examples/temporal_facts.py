#!/usr/bin/env python3
"""Bi-temporal fact history: corrections supersede, history survives.

Memplex never deletes a contradicted fact. A correction stamps the old
value with ``invalid_at`` and keeps it, so ``list_facts(as_of=...)`` can
reconstruct exactly what was believed at any point in time.

Runs fully offline against a throwaway Lite store:

    .venv/bin/python examples/temporal_facts.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")
os.environ.setdefault("MEMPLEX_LLM_QUERY_ENHANCEMENT", "false")

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from memplex.config import load_config
from memplex.models import Fact
from memplex.service import MemplexService
from memplex.temporal import now_iso, supersede_contradicted

JAN = "2026-01-01T00:00:00+00:00"
JUN = "2026-06-01T00:00:00+00:00"


def _fact(object_: str, valid_from: str) -> Fact:
    return Fact(
        id=f"fact-db-{object_}",
        tenant_id="t1",
        owner_subject_id="alice",
        workspace_id="w1",
        updated_at=valid_from,
        valid_from=valid_from,
        subject="db",
        predicate="is",
        object_=object_,
    )


def main() -> int:
    config = load_config()
    config.storage.backend = "lite"
    config.storage.path = str(Path(tempfile.mkdtemp(prefix="memplex-example-")))

    svc = MemplexService(config=config)
    svc.start()
    try:
        # January belief: the database is MySQL.
        old = _fact("mysql", JAN)
        svc.store.add_fact(old)

        # June correction: migrated to PostgreSQL. The write path stamps
        # the contradicted January fact with invalid_at instead of
        # deleting it; here we drive the same helper explicitly.
        new = _fact("postgres", JUN)
        superseded = supersede_contradicted(new, svc.store.list_facts(), now=JUN)
        for fact in superseded:
            svc.store.add_fact(fact)
        svc.store.add_fact(new)

        current = [f.object_ for f in svc.list_facts()]
        march = [f.object_ for f in svc.list_facts(as_of="2026-03-01T00:00:00+00:00")]
        full_history = [
            f.object_ for f in svc.list_facts(include_invalidated=True)
        ]

        print(f"current belief      : {current}")
        print(f"as of 2026-03       : {march}")
        print(f"full history        : {full_history}")
        print(f"now_iso sample      : {now_iso()}")
        assert current == ["postgres"], current
        assert march == ["mysql"], march
        assert set(full_history) == {"mysql", "postgres"}, full_history
        print("\nOK: correction superseded, point-in-time history preserved.")
        return 0
    finally:
        svc.stop()


if __name__ == "__main__":
    raise SystemExit(main())
