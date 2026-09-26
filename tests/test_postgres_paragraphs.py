"""ADR-013 Stage 2 / Phase-B B3: PG raw-paragraph layer contract tests.

Real PostgreSQL via the shared conftest fixtures (pgserver-backed);
assertions go entirely through the store API. The whole module skips
when pgserver/psycopg2 are absent.
"""

import os
import pathlib
import sys

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest

pytest.importorskip("psycopg2")
pytest.importorskip("pgserver")

from memplex.models.paragraph import Paragraph, persisted_paragraph_id


@pytest.fixture
def pg_store(pg_function_dsn):
    from memplex.storage.postgres import PostgresMemoryStore
    from memplex.storage.postgres_resources import PostgresStorageResources, VectorCapabilityRequest

    resources = PostgresStorageResources(dsn=pg_function_dsn)
    resources.ensure_ready(
        VectorCapabilityRequest(dim=0, policy="disabled"), "development"
    )
    s = PostgresMemoryStore(dsn=pg_function_dsn, ready_pool=resources.ready_pool)
    s.clear()
    yield s
    resources.close()


def test_persist_and_read_back(pg_store):
    para = Paragraph(
        id="para_001",
        source="text#para_001",
        section="",
        raw_text="Alice keeps a rare blue-throated parrot named Kiwi.",
    )
    pg_store.persist_paragraphs([para], trust_tier=4, source_hint="text")

    expected_id = persisted_paragraph_id("text", "para_001", para.raw_text.strip())
    row = pg_store.get_paragraph(expected_id)
    assert row is not None, "the content-addressed id must resolve on PG too"
    assert row["raw_text"] == para.raw_text.strip()
    assert row["trust_tier"] == 4


def test_persist_is_idempotent(pg_store):
    para = Paragraph(id="para_002", source="", section="", raw_text="Idempotent row.")
    pg_store.persist_paragraphs([para], trust_tier=3, source_hint="text")
    pg_store.persist_paragraphs([para], trust_tier=3, source_hint="text")
    expected_id = persisted_paragraph_id("text", "para_002", "Idempotent row.")
    row = pg_store.get_paragraph(expected_id)
    assert row is not None


def test_missing_paragraph_reads_none(pg_store):
    assert pg_store.get_paragraph("text:para_999:deadbeef") is None
