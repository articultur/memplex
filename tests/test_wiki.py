"""Test the wiki layer: compiler, dual-index search, community detection.

Previously zero coverage (evaluation #1 FAIL: wiki/ at 0%). Covers the
public classes from each module: WikiCompiler (page compilation + write),
DualIndexSearch (FTS+vector hybrid + cosine), GraphCommunityDetector
(fallback domain grouping), and LLMWikiGenerator construction.
"""

import math
import os
from pathlib import Path

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")

import pytest  # noqa: E402

from memplex.models import (  # noqa: E402
    Fact,
    FieldValue,
    Function,
    GraphData,
    GraphEdge,
    Observation,
    Preference,
    SourceType,
    WikiPage,
)
from memplex.wiki.community import Community, GraphCommunityDetector  # noqa: E402
from memplex.wiki.compiler import WikiCompiler  # noqa: E402
from memplex.wiki.generator import LLMWikiGenerator  # noqa: E402
from memplex.wiki.search import DualIndexSearch  # noqa: E402

# ── Helpers ──────────────────────────────────────────────────────────


def _fv(desc):
    return FieldValue(desc=desc, sources=["t:1"], source_method="manual", weight=1.0)


def _func(fid="func_1", name="login", domain="auth"):
    return Function(
        id=fid,
        name=name,
        name_normalized=name.lower(),
        domain=domain,
        trigger=[_fv("user logs in")],
        action=[_fv("call authenticate()")],
        source_type=SourceType.CODE,
    )


class _StubStore:
    def __init__(self, funcs=None):
        self._funcs = funcs or []

    def list_functions(self, limit=100):
        return list(self._funcs)[:limit]


class _StubEmbedding:
    DIM = 8

    def embed(self, text):
        vec = [0.0] * self.DIM
        for tok in text.lower().split():
            vec[hash(tok) % self.DIM] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


def _page(pid, content):
    return WikiPage(page_id=pid, content=content, metadata={})


# ── Community ────────────────────────────────────────────────────────


def test_community_size_property():
    c = Community(community_id=0, node_ids=["a", "b", "c"])
    assert c.size == 3


def test_community_empty_size_zero():
    assert Community(community_id=0).size == 0


# ── GraphCommunityDetector ───────────────────────────────────────────


def _graph(nodes, edges=None):
    return GraphData(nodes=nodes, edges=edges or [])


def test_detector_fallback_domain_grouping_without_networkx():
    """With no strong edges (or no networkx), domains form the communities."""
    funcs = [
        _func(fid="f1", domain="auth"),
        _func(fid="f2", domain="auth"),
        _func(fid="f3", domain="db"),
    ]
    detector = GraphCommunityDetector(min_community_size=2)
    communities = detector.detect_communities(_graph(funcs))
    # auth has 2 nodes (>= threshold), db has 1 (< threshold, dropped).
    assert len(communities) == 1
    assert communities[0].size == 2


def test_detector_threshold_filters_small_communities():
    funcs = [_func(fid=f"f{i}", domain="auth") for i in range(3)] + [
        _func(fid="solo", domain="solo")
    ]
    detector = GraphCommunityDetector(min_community_size=2)
    communities = detector.detect_communities(_graph(funcs))
    # auth (3) kept; solo (1) dropped.
    sizes = sorted(c.size for c in communities)
    assert sizes == [3]


def test_detector_no_nodes_returns_empty():
    detector = GraphCommunityDetector()
    assert detector.detect_communities(_graph([])) == []


def test_detector_min_size_override_per_call():
    funcs = [_func(fid=f"f{i}", domain="x") for i in range(5)]
    detector = GraphCommunityDetector(min_community_size=10)
    # Default threshold 10 filters it out...
    assert detector.detect_communities(_graph(funcs)) == []
    # ...but a per-call override of 3 keeps it.
    out = detector.detect_communities(_graph(funcs), min_size=3)
    assert len(out) == 1 and out[0].size == 5


def test_detector_generate_concept_pages_empty_graph():
    detector = GraphCommunityDetector()
    assert detector.generate_concept_pages([], _StubStore()) == []


# ── WikiCompiler ─────────────────────────────────────────────────────


def test_compile_function_produces_page_with_sections(tmp_path):
    compiler = WikiCompiler(store=_StubStore(), wiki_dir=tmp_path)
    page = compiler.compile_function(_func())
    assert isinstance(page, WikiPage)
    assert "user logs in" in page.content
    assert "call authenticate()" in page.content


def test_compile_function_starts_with_frontmatter(tmp_path):
    compiler = WikiCompiler(store=_StubStore(), wiki_dir=tmp_path)
    page = compiler.compile_function(_func(domain="auth"))
    assert page.content.lstrip().startswith("---")


def test_compile_fact(tmp_path):
    compiler = WikiCompiler(store=_StubStore(), wiki_dir=tmp_path)
    fact = Fact(
        id="fact_1",
        name="api is rest",
        subject="API",
        predicate="is",
        object_="REST",
        confidence=0.9,
        source_type=SourceType.WIKI,
    )
    page = compiler.compile_fact(fact)
    assert "REST" in page.content


def test_compile_preference(tmp_path):
    compiler = WikiCompiler(store=_StubStore(), wiki_dir=tmp_path)
    pref = Preference(
        id="pref_1",
        name="dark theme",
        aspect="theme",
        preference="dark",
        confidence=0.8,
        source_type=SourceType.WIKI,
    )
    page = compiler.compile_preference(pref)
    assert "dark" in page.content.lower()


def test_compile_observation(tmp_path):
    compiler = WikiCompiler(store=_StubStore(), wiki_dir=tmp_path)
    obs = Observation(
        id="obs_1",
        name="deploy failed",
        event="deploy failed",
        context="at 3am",
        confidence=0.7,
        source_type=SourceType.WIKI,
    )
    page = compiler.compile_observation(obs)
    assert "deploy failed" in page.content


def test_compile_all_includes_function_pages(tmp_path):
    funcs = [_func(fid="f1"), _func(fid="f2", name="logout")]
    compiler = WikiCompiler(store=_StubStore(funcs=funcs), wiki_dir=tmp_path)
    pages = compiler.compile_all()
    assert len(pages) >= 2  # at least the per-function pages


def test_write_page_creates_file(tmp_path):
    compiler = WikiCompiler(store=_StubStore(), wiki_dir=tmp_path)
    page = compiler.compile_function(_func())
    written = compiler.write_page(page)
    assert Path(written).exists()


# ── DualIndexSearch ──────────────────────────────────────────────────


def test_dual_index_add_page_populates_fts(tmp_path):
    idx = DualIndexSearch(wiki_dir=tmp_path, embedding_service=_StubEmbedding())
    idx.add_page(_page("p1", "login authentication handler"))
    assert "p1" in idx._fts_index


def test_dual_index_search_returns_fts_hits(tmp_path):
    idx = DualIndexSearch(wiki_dir=tmp_path, embedding_service=_StubEmbedding())
    idx.add_page(_page("p1", "login authentication"))
    idx.add_page(_page("p2", "database connection pool"))
    results = idx.search("login", top_k=5)
    assert "p1" in {r.func_id for r in results}


def test_dual_index_search_empty_index_returns_empty(tmp_path):
    idx = DualIndexSearch(wiki_dir=tmp_path, embedding_service=_StubEmbedding())
    assert idx.search("anything", top_k=5) == []


def test_dual_index_cosine_similarity_static():
    assert DualIndexSearch._cosine_similarity([1.0, 0.0], [0.0, 1.0]) == 0.0
    assert DualIndexSearch._cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


def test_dual_index_extract_summary_truncates():
    out = DualIndexSearch._extract_summary("a b c d e f", max_len=3)
    assert len(out) <= 3


def test_dual_index_rebuild_clears_indices(tmp_path):
    idx = DualIndexSearch(wiki_dir=tmp_path, embedding_service=_StubEmbedding())
    idx.add_page(_page("p1", "x"))
    assert idx._fts_index
    idx.rebuild_index()
    assert idx._fts_index == {}


# ── LLMWikiGenerator (construction only) ─────────────────────────────


def test_llm_wiki_generator_constructs_with_none_enhancer():
    """Must be constructible without a live LLM enhancer for graceful degrade."""
    gen = LLMWikiGenerator(llm_enhancer=None)
    assert gen is not None


# ── WikiCompiler index/log/write operations ──────────────────────────


def test_compile_index_lists_compiled_pages(tmp_path):
    funcs = [_func(fid="f1", name="alpha"), _func(fid="f2", name="beta")]
    compiler = WikiCompiler(store=_StubStore(funcs=funcs), wiki_dir=tmp_path)
    index = compiler.compile_index()
    assert isinstance(index, WikiPage)
    assert "alpha" in index.content or "f1" in index.content
    assert "beta" in index.content or "f2" in index.content


def test_write_index_creates_index_md(tmp_path):
    compiler = WikiCompiler(store=_StubStore(), wiki_dir=tmp_path)
    idx = WikiPage(page_id="index", content="# Index\n\n- [[func_1]]", metadata={})
    written = compiler.write_index(idx)
    assert Path(written).exists()
    assert Path(written).name == "index.md"


def test_append_log_creates_and_appends(tmp_path):
    compiler = WikiCompiler(store=_StubStore(), wiki_dir=tmp_path)
    p1 = compiler.append_log("first entry")
    assert Path(p1).exists()
    compiler.append_log("second entry")
    content = Path(p1).read_text(encoding="utf-8")
    assert "first entry" in content
    assert "second entry" in content


def test_rotate_log_archives_existing_log(tmp_path):
    """_rotate_log moves the current log to log.archive.1.md."""
    compiler = WikiCompiler(store=_StubStore(), wiki_dir=tmp_path)
    log_path = tmp_path / "log.md"
    log_path.write_text("old content\n", encoding="utf-8")
    compiler._rotate_log(log_path)
    archive = tmp_path / "log.archive.1.md"
    assert archive.exists()
    assert "old content" in archive.read_text(encoding="utf-8")
