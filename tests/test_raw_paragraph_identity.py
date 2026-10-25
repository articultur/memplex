"""Raw-paragraph identity + fail-closed recall under the MERGED semantics.

Post-merge contract (factual lifecycle line vs our P0 line, reconciled):

- Paragraph rows gain write-bound identity ONLY through the authorized
  ``persist_paragraphs(authorization=...)`` path (the factual-capture
  pipeline). Plain ``service.write_text`` writes stay identity-less and
  are local-development-readable only - fail-closed for every
  authenticated tenant, and never adopted or widened by a later
  authorized replay.
- ``_RawParagraphView`` projects the identity fields ``is_node_visible``
  reads; its ``to_dict`` puts ``raw_text`` under the read-side injection
  scan.
"""

from __future__ import annotations

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from memplex.auth import AuthorizationContext, Principal, local_development_context
from memplex.authorization import AuthorizationGate, _RawParagraphView
from memplex.config import MemplexConfig
from memplex.service import MemplexService
from memplex.storage.lite.store import LiteMemoryStore

MARKER = "frobnitz"
SENTENCE = "Kayak the frobnitz canal at dawn with the owls."


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


def _service(tmp_path) -> MemplexService:
    cfg = MemplexConfig()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(tmp_path / "s.sqlite3")
    cfg.llm.query_enhancement = False
    svc = MemplexService(config=cfg)
    svc.start()
    return svc


def _paragraphs_of(svc: MemplexService) -> list[dict]:
    return [
        row for row in svc.store._paragraphs.values() if MARKER in row["raw_text"]
    ]


def _surfaced(svc: MemplexService, result) -> bool:
    for r in result.results:
        row = svc.store._paragraphs.get(r.func_id)
        if row is not None and MARKER in row["raw_text"]:
            return True
    return False


def test_plain_write_paragraphs_stay_identityless_fail_closed(tmp_path):
    svc = _service(tmp_path)
    try:
        svc.write_text(SENTENCE, source_type="text", authorization=_context())
        rows = _paragraphs_of(svc)
        assert rows, "verbatim paragraph must persist"
        assert all(row.get("tenant_id") is None for row in rows), (
            "plain writes bind NO identity - the factual lifecycle contract; "
            "identity arrives only via the authorized factual-capture path"
        )
        # Fail-closed for the authenticated writer itself (store ACL)...
        mine = svc.query(f"{MARKER} canal", top_k=8, authorization=_context())
        assert not _surfaced(svc, mine), (
            "identity-less paragraphs are local-development-only"
        )
        # ...and readable through the local-development compatibility context.
        compat = svc.query(f"{MARKER} canal", top_k=8)
        assert _surfaced(svc, compat)
    finally:
        svc.stop()


def test_authorized_bind_path_stamps_identity_and_isolates(tmp_path):
    store = LiteMemoryStore(tmp_path / "memory.json")
    try:
        from memplex.models.paragraph import Paragraph

        para = Paragraph(id="para_001", source="test", section="t", raw_text=SENTENCE)
        store.persist_paragraphs(
            [para], trust_tier=4, source_hint="text", authorization=_context()
        )
        (row,) = _paragraphs_of_store(store)
        assert row["tenant_id"] == "acme"
        assert row["owner_subject_id"] == "alice"
        assert row["namespace"]["memplex_tenant_id"] == "acme"
    finally:
        pass


def _paragraphs_of_store(store: LiteMemoryStore) -> list[dict]:
    return [row for row in store._paragraphs.values() if MARKER in row["raw_text"]]


def test_authorized_row_visible_to_own_tenant_only(tmp_path):
    svc = _service(tmp_path)
    try:
        from memplex.models.paragraph import Paragraph

        svc.store.persist_paragraphs(
            [Paragraph(id="para_001", source="test", section="t", raw_text=SENTENCE)],
            trust_tier=4,
            source_hint="text",
            authorization=_context(),
            visibility="user",
        )
        owner = _context()
        rows = _paragraphs_of(svc)
        assert rows, "paragraph persisted"
        row_id = rows[0]["id"]
        assert svc.get(row_id, authorization=owner) is not None, (
            "the binding tenant resolves its own row"
        )
        assert svc.get(row_id, authorization=_context(tenant="other")) is None
        assert svc.get(row_id, authorization=_context(subject="bob")) is None, (
            "user-visibility rows stay owner-only"
        )
        # (The unauthenticated service.get is the documented local-development
        # compatibility surface: it resolves ids without the visibility
        # gate; filtered recall - the runtime path - enforces it.)
    finally:
        svc.stop()


def test_identity_survives_pair_roundtrip(tmp_path):
    store = LiteMemoryStore(tmp_path / "memory.json")
    from memplex.models.paragraph import Paragraph

    store.persist_paragraphs(
        [Paragraph(id="para_001", source="test", section="t", raw_text=SENTENCE)],
        trust_tier=4,
        source_hint="text",
        authorization=_context(),
    )
    store2 = LiteMemoryStore(tmp_path / "memory.json")
    (row,) = _paragraphs_of_store(store2)
    assert row["tenant_id"] == "acme"
    assert row["namespace"]["memplex_tenant_id"] == "acme"


def test_first_write_identity_never_widened_by_replay(tmp_path):
    """The factual-lifecycle replay rule at the store level: a bound row
    keeps its first-write identity; an identity-less row is never adopted."""
    store = LiteMemoryStore(tmp_path / "memory.json")
    from memplex.models.paragraph import Paragraph

    store.persist_paragraphs(
        [Paragraph(id="para_001", source="test", section="t", raw_text=SENTENCE)],
        trust_tier=4,
        source_hint="text",
    )
    store.persist_paragraphs(
        [Paragraph(id="para_001", source="test", section="t", raw_text=SENTENCE)],
        trust_tier=4,
        source_hint="text",
        authorization=_context(tenant="other"),
    )
    (row,) = _paragraphs_of_store(store)
    assert row.get("tenant_id") is None, (
        "replay must not adopt or widen a legacy identity-less row"
    )
    assert row["observation_count"] == 2, "content-addressed dedup bumped once"


def test_raw_paragraph_view_scan_surface():
    view = _RawParagraphView(
        {
            "id": "p1",
            "raw_text": "verbatim body",
            "owner_subject_id": "alice",
            "tenant_id": "acme",
            "visibility": "user",
        }
    )
    scanned = view.to_dict()
    assert "verbatim body" in scanned["raw_text"]
    assert set(scanned) == set(_RawParagraphView.__slots__), (
        "the full projected surface goes under the bounded safety scan"
    )
    assert view.owner_subject_id == "alice"
    legacy = _RawParagraphView({"id": "p2", "raw_text": "x", "owner_subject": "bob"})
    assert legacy.owner_subject_id == "bob"
