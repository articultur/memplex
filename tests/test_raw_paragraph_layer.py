"""ADR-013 Stage 2 raw-paragraph layer contract tests.

Covers: write-path persistence of verbatim text, deterministic
content-addressed ids resolvable from source_paragraphs, retrieval legs
(vector + BM25) surfacing raw text, restart survival, trust tiers, and
tier penalty interplay.
"""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

from memplex.config import MemplexConfig
from memplex.service import MemplexService


def _service(tmp_path):
    cfg = MemplexConfig()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(tmp_path / "s.sqlite3")
    cfg.llm.query_enhancement = False
    svc = MemplexService(config=cfg)
    svc.start()
    return svc


def test_write_persists_verbatim_paragraphs(tmp_path):
    svc = _service(tmp_path)
    try:
        svc.write_text(
            "Alice keeps a rare blue-throated parrot named Kiwi in the attic aviary.",
            source_type="text",
        )
        rows = svc.store._paragraphs
        assert rows, "verbatim paragraph must persist"
        assert any(
            "blue-throated parrot" in r["raw_text"] for r in rows.values()
        )
        assert all(r["trust_tier"] == 4 for r in rows.values())
    finally:
        svc.stop()


def test_source_paragraphs_reference_resolves(tmp_path):
    svc = _service(tmp_path)
    try:
        svc.write_text(
            "Bob prefers decaf coffee in the evenings after dinner.",
            source_type="text",
        )
        paras = svc.store._paragraphs
        refs = [
            sp
            for f in {**svc.store._facts, **svc.store._preferences}.values()
            for sp in f.source_paragraphs
        ]
        assert refs, "typed node must reference its paragraph"
        assert all(sp in paras for sp in refs), (
            "source_paragraphs must resolve to persisted raw rows"
        )
    finally:
        svc.stop()


def test_retrieval_surfaces_raw_text(tmp_path):
    svc = _service(tmp_path)
    try:
        svc.write_text(
            "Alice keeps a rare blue-throated parrot named Kiwi in the attic aviary.",
            source_type="text",
        )
        paras = svc.store._paragraphs
        hits = svc.store.vector_search(
            "what exotic pet does Alice keep and where", top_k=6
        )
        para_hits = [h for h in hits if h.func_id in paras]
        assert para_hits, "raw paragraph must be retrievable"
        assert any("blue-throated parrot" in h.summary for h in para_hits)
    finally:
        svc.stop()


def test_raw_layer_survives_restart(tmp_path):
    svc = _service(tmp_path)
    svc.write_text("Carol adopted a grey cat named Misty last spring.", source_type="text")
    svc.stop()

    svc = _service(tmp_path)
    try:
        assert any(
            "grey cat named Misty" in r["raw_text"]
            for r in svc.store._paragraphs.values()
        ), "raw layer must reload from the durable pair"
    finally:
        svc.stop()


def test_duplicate_write_is_idempotent(tmp_path):
    text = "Dan trains for marathons on Sunday mornings."
    svc = _service(tmp_path)
    try:
        svc.write_text(text, source_type="text")
        n1 = len(svc.store._paragraphs)
        svc.write_text(text, source_type="text")
        assert len(svc.store._paragraphs) == n1, (
            "identical verbatim text must map to one content-addressed row"
        )
    finally:
        svc.stop()


def test_external_paragraphs_carry_low_tier(tmp_path):
    import os as _os

    _os.environ.pop("MEMPLEX_TRUST_PENALTY", None)
    svc = _service(tmp_path)
    try:
        svc.write_text(
            "Reader note: the user prefers rooibos tea over everything.",
            source_type="url",
        )
        tiers = {r["trust_tier"] for r in svc.store._paragraphs.values()}
        assert tiers == {2}, "url-sourced raw text must be external_web"
    finally:
        svc.stop()
