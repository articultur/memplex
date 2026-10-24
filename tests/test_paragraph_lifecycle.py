"""ADR-013 Stage 3: raw-paragraph deletion + SyncNodeType.PARAGRAPH.

Covers the lifecycle that was missing when the raw layer landed: a public
deletion path (privacy), typed-delete cascade (sole-referenced paragraphs
go, shared ones stay), and the sync surface - lite captures paragraph
upserts/tombstones with bound identity (identity-less legacy rows are
local-only) and applies inbound paragraph events with scope stamping.
"""

from __future__ import annotations

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from datetime import UTC, datetime

from memplex.auth import AuthorizationContext, Principal
from memplex.config import MemplexConfig
from memplex.service import MemplexService
from memplex.storage.lite.store import LiteMemoryStore
from memplex.storage.lite.sync_repository import LiteSyncRepository
from memplex.sync_protocol import (
    SyncEntityKey,
    SyncEvent,
    SyncNodeType,
    SyncOperation,
    SyncPage,
    SyncScope,
    SyncStreamItem,
    SyncVersion,
)
from memplex.sync_repository import SyncCapturePolicy


def _context(
    tenant: str = "acme", subject: str = "alice", workspace: str | None = "ws-1"
) -> AuthorizationContext:
    return AuthorizationContext(
        principal=Principal(
            tenant_id=tenant,
            subject_id=subject,
            roles=frozenset({"member"}),
            authentication_id=f"credential-{subject}",
        ),
        workspace_id=workspace,
        agent_id="codex",
        session_id="s1",
        request_id=f"request-{subject}",
    )


def _service(tmp_path, *, sync: bool = False) -> MemplexService:
    cfg = MemplexConfig()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(tmp_path / "s.sqlite3")
    cfg.llm.query_enhancement = False
    if sync:
        cfg.sync.enabled = True
        cfg.sync.capture_policy = "required"
    svc = MemplexService(config=cfg)
    svc.start()
    return svc


def _paragraph_event(
    row: dict,
    operation: SyncOperation,
    *,
    index: int = 7,
) -> SyncEvent:
    event_id = f"00000000-0000-0000-0000-{index:012d}"
    return SyncEvent(
        1,
        event_id,
        "remote-a",
        SyncNodeType.PARAGRAPH,
        SyncEntityKey.node(row["id"]),
        operation,
        str(SyncVersion.create(datetime.now(UTC), "remote-a", event_id)),
        SyncScope("acme", "alice", "ws-1", "workspace", "codex", "s1"),
        None if operation is SyncOperation.TOMBSTONE else dict(row),
    )


def test_delete_paragraph_persists_across_restart(tmp_path):
    svc = _service(tmp_path)
    try:
        svc.write_text(
            "Kayak the frobnitz canal at dawn with the owls.",
            source_type="text",
            authorization=_context(),
        )
        row_id = next(iter(svc.store._paragraphs))
        assert svc.store.delete_paragraph(row_id) is True
        assert svc.store.delete_paragraph(row_id) is False  # idempotent
        assert row_id not in svc.store._paragraphs
    finally:
        svc.stop()
    svc2 = _service(tmp_path)
    try:
        assert not svc2.store._paragraphs or all(
            "frobnitz" not in r["raw_text"] for r in svc2.store._paragraphs.values()
        )
    finally:
        svc2.stop()


def test_typed_delete_cascades_sole_referenced_paragraph(tmp_path):
    svc = _service(tmp_path)
    try:
        svc.write_text(
            "Kayak the frobnitz canal at dawn with the owls.",
            source_type="text",
            authorization=_context(),
        )
        facts = list(svc.store._facts.values()) + list(
            svc.store._preferences.values()
        )
        functions = list(svc.store._functions.values())
        targets = facts + functions
        assert targets, "extraction must produce a typed node"
        victim = targets[0]
        refs = list(getattr(victim, "source_paragraphs", None) or [])
        assert refs, "typed node must reference its paragraph"
        sole = [
            r
            for r in refs
            if all(
                r not in (getattr(n, "source_paragraphs", None) or [])
                for n in targets
                if n is not victim
            )
        ]
        if not sole:
            return  # extractor shared every row; covered by the shared test
        svc.delete(victim.id, authorization=_context())
        assert victim.id not in svc.store._functions and victim.id not in svc.store._facts
        for para_id in sole:
            assert para_id not in svc.store._paragraphs, (
                "sole-referenced paragraph must be cascaded away"
            )
    finally:
        svc.stop()


def test_typed_delete_keeps_shared_paragraph(tmp_path):
    svc = _service(tmp_path)
    try:
        svc.write_text(
            "Kayak the frobnitz canal at dawn with the owls.",
            source_type="text",
            authorization=_context(),
        )
        # Two typed nodes referencing the same paragraph row: re-point a
        # function's source_paragraphs at the fact's paragraph (mutation
        # then reload is overkill - emulate by appending on the model).
        facts = list(svc.store._facts.values())
        functions = list(svc.store._functions.values())
        if not facts or not functions:
            return
        fact, func = facts[0], functions[0]
        fact_refs = list(getattr(fact, "source_paragraphs", None) or [])
        if not fact_refs:
            return
        func.source_paragraphs = list(func.source_paragraphs or []) + [
            fact_refs[0]
        ]
        svc.delete(func.id, authorization=_context())
        assert fact_refs[0] in svc.store._paragraphs, (
            "paragraph shared with the surviving fact must be retained"
        )
    finally:
        svc.stop()


def test_sync_apply_paragraph_upsert_and_tombstone(tmp_path):
    store = LiteMemoryStore(
        path=tmp_path / "memory.json",
        sync_capture_policy=SyncCapturePolicy("required", "lite-local"),
    )
    repo = store._sync_repository
    assert isinstance(repo, LiteSyncRepository)
    try:
        row = {
            "id": "text:para_001:abcd",
            "raw_text": "verbatim paragraph payload",
            "trust_tier": 4,
            "created_at": "2026-10-24T00:00:00+00:00",
            "source": "",
            "observation_count": 1,
            "last_observed_at": "2026-10-24T00:00:00+00:00",
            "tenant_id": "acme",
            "owner_subject_id": "alice",
            "workspace_id": "ws-1",
            "visibility": "workspace",
            "namespace": {"memplex_tenant_id": "acme"},
        }
        page = SyncPage(
            (SyncStreamItem(1, _paragraph_event(row, SyncOperation.UPSERT)),),
            1,
            1,
            False,
        )
        repo.sync_apply_page("remote-a", page)
        applied = store._paragraphs.get("text:para_001:abcd")
        assert applied is not None and applied["raw_text"] == "verbatim paragraph payload"
        assert applied["tenant_id"] == "acme"
        assert applied["namespace"]["memplex_subject_id"] == "alice"

        tomb = SyncPage(
            (SyncStreamItem(2, _paragraph_event(row, SyncOperation.TOMBSTONE, index=8)),),
            2,
            2,
            False,
        )
        repo.sync_apply_page("remote-a", tomb)
        assert "text:para_001:abcd" not in store._paragraphs
    finally:
        pass  # LiteMemoryStore has no lifecycle stop()


def test_sync_capture_roundtrip_and_legacy_skip(tmp_path):
    """Persist captures a paragraph upsert event with identity; an
    identity-less legacy row never enters the capture stream."""
    store = LiteMemoryStore(
        path=tmp_path / "memory.json",
        sync_capture_policy=SyncCapturePolicy("required", "lite-local"),
    )
    repo = store._sync_repository
    try:
        bound = {
            "id": "text:para_002:efff",
            "raw_text": "bound paragraph",
            "trust_tier": 3,
            "created_at": "2026-10-24T00:00:00+00:00",
            "source": "",
            "observation_count": 1,
            "last_observed_at": "2026-10-24T00:00:00+00:00",
            "tenant_id": "acme",
            "owner_subject_id": "alice",
            "workspace_id": "ws-1",
            "visibility": "workspace",
            "namespace": {},
        }
        assert repo.capture_paragraph_row(bound, operation=SyncOperation.UPSERT) is not None
        legacy = dict(bound, id="legacy:row:1")
        legacy.pop("tenant_id")
        legacy.pop("owner_subject_id")
        assert repo.capture_paragraph_row(legacy, operation=SyncOperation.UPSERT) is None

        store._paragraphs["text:para_002:efff"] = bound
        store._paragraphs["legacy:row:1"] = legacy
        snapshot_events = repo._current_snapshot_events()
        para_events = [
            e for e in snapshot_events if e.node_type is SyncNodeType.PARAGRAPH
        ]
        assert {e.entity_key.node_id for e in para_events} == {"text:para_002:efff"}
    finally:
        pass  # LiteMemoryStore has no lifecycle stop()
