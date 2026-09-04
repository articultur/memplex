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

import pytest

from memplex.models import (
    Fact,
    FieldValue,
    Function,
    GraphData,
    Observation,
    Preference,
    SearchResult,
    SourceType,
    WikiPage,
)
from memplex.wiki.community import Community, GraphCommunityDetector
from memplex.wiki.compiler import WikiCompiler
from memplex.wiki.generator import LLMWikiGenerator
from memplex.wiki.search import DualIndexSearch

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


class _FullStubStore(_StubStore):
    """Store exposing the optional per-type listing methods."""

    def __init__(self, funcs=None, facts=None, prefs=None, observations=None):
        super().__init__(funcs)
        self._facts = facts or []
        self._prefs = prefs or []
        self._observations = observations or []

    def list_facts(self, limit=100):
        return list(self._facts)[:limit]

    def list_preferences(self, limit=100):
        return list(self._prefs)[:limit]

    def list_observations(self, limit=100):
        return list(self._observations)[:limit]


class _StubEmbedding:
    DIM = 8

    def embed(self, text):
        vec = [0.0] * self.DIM
        for tok in text.lower().split():
            vec[hash(tok) % self.DIM] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class _FixedEmbedding:
    """Every text embeds to the same unit vector (similarity always 1.0)."""

    def embed(self, text):
        return [1.0, 0.0]


class _StubLLM:
    def __init__(self, text="generated", json_result=None, fail=False):
        self._text = text
        self._json = json_result or {}
        self._fail = fail

    async def complete(self, prompt):
        if self._fail:
            raise RuntimeError("llm down")
        return self._text

    async def complete_json(self, prompt):
        if self._fail:
            raise RuntimeError("llm down")
        return self._json


class _StubEnhancer:
    def __init__(self, llm):
        self.llm = llm


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


def test_rotate_log_discards_oldest_archive(tmp_path):
    """The oldest archive (>= MAX_LOG_ARCHIVES) is dropped on rotation."""
    compiler = WikiCompiler(store=_StubStore(), wiki_dir=tmp_path)
    (tmp_path / "log.archive.5.md").write_text("stale\n", encoding="utf-8")
    log_path = tmp_path / "log.md"
    log_path.write_text("current\n", encoding="utf-8")
    compiler._rotate_log(log_path)
    assert not (tmp_path / "log.archive.5.md").exists()
    assert (tmp_path / "log.archive.1.md").read_text(encoding="utf-8") == "current\n"


# ── compile_all: all memory types + domain pages ─────────────────────


def test_compile_all_covers_all_memory_types(tmp_path):
    store = _FullStubStore(
        funcs=[_func(fid="f1")],
        facts=[
            Fact(
                id="fact_1",
                name="api is rest",
                subject="API",
                predicate="is",
                object_="REST",
                source_type=SourceType.WIKI,
            )
        ],
        prefs=[
            Preference(
                id="pref_1",
                name="dark theme",
                aspect="theme",
                preference="dark",
                source_type=SourceType.WIKI,
            )
        ],
        observations=[
            Observation(
                id="obs_1",
                name="deploy failed",
                event="deploy failed",
                source_type=SourceType.WIKI,
            )
        ],
    )
    compiler = WikiCompiler(store=store, wiki_dir=tmp_path)
    pages = compiler.compile_all()
    ids = {p.page_id for p in pages}
    assert {"f1", "fact_1", "pref_1", "obs_1", "index"} <= ids


def test_compile_all_dispatches_nodes_by_memory_type(tmp_path):
    """A store returning non-Function nodes still compiles them correctly."""
    store = _StubStore(
        funcs=[
            Fact(
                id="fact_9",
                name="x",
                subject="a",
                predicate="b",
                object_="c",
                source_type=SourceType.WIKI,
            )
        ]
    )
    compiler = WikiCompiler(store=store, wiki_dir=tmp_path)
    ids = {p.page_id for p in compiler.compile_all()}
    assert "fact_9" in ids


def test_compile_all_generates_domain_pages(tmp_path):
    funcs = [_func(fid="f1", domain="auth"), _func(fid="f2", domain="db")]
    compiler = WikiCompiler(store=_StubStore(funcs=funcs), wiki_dir=tmp_path)
    pages = compiler.compile_all()
    ids = {p.page_id for p in pages}
    assert "domain_auth" in ids
    assert "domain_db" in ids
    domain_page = next(p for p in pages if p.page_id == "domain_auth")
    assert "[[f1]]" in domain_page.content


def test_compile_domain_page_lists_member_entities(tmp_path):
    compiler = WikiCompiler(store=_StubStore(), wiki_dir=tmp_path)
    page = compiler.compile_domain_page("auth", [_func(fid="f1", domain="auth")])
    assert page.page_id == "domain_auth"
    assert "[[f1]]" in page.content
    assert page.content.startswith("---")


# ── WikiCompiler.lint ────────────────────────────────────────────────


def _frontmatter_page(pid, body=""):
    content = (
        "---\n"
        f'id: "{pid}"\n'
        'name: "x"\n'
        'domain: "auth"\n'
        'memory_type: "function"\n'
        "confidence: 1.0\n"
        'created_at: "2024-01-01"\n'
        'updated_at: "2024-01-01"\n'
        "---\n\n" + body
    )
    return _page(pid, content)


def test_lint_clean_wiki_passes(tmp_path):
    """compile_all output written to disk lints with zero issues."""
    funcs = [_func(fid="f1", domain="auth")]
    compiler = WikiCompiler(store=_StubStore(funcs=funcs), wiki_dir=tmp_path)
    for page in compiler.compile_all():
        if page.page_id == "index":
            compiler.write_index(page)
        else:
            compiler.write_page(page)
    result = compiler.lint()
    assert result.passed
    assert result.issues == []


def test_lint_missing_frontmatter_is_error(tmp_path):
    compiler = WikiCompiler(store=_StubStore(), wiki_dir=tmp_path)
    compiler.write_page(_page("p1", "# no frontmatter here"))
    result = compiler.lint()
    assert not result.passed
    assert any(
        i.severity == "error" and "frontmatter" in i.message.lower() for i in result.issues
    )


def test_lint_invalid_yaml_frontmatter_is_error(tmp_path):
    compiler = WikiCompiler(store=_StubStore(), wiki_dir=tmp_path)
    compiler.write_page(_page("bad", "---\n: broken: [\n---\n\n# body"))
    result = compiler.lint()
    assert not result.passed
    assert any(i.severity == "error" and "YAML" in i.message for i in result.issues)


def test_lint_unterminated_frontmatter_is_error(tmp_path):
    compiler = WikiCompiler(store=_StubStore(), wiki_dir=tmp_path)
    compiler.write_page(_page("bad", '---\nid: "x"\n'))
    result = compiler.lint()
    assert not result.passed
    assert any(i.severity == "error" for i in result.issues)


def test_lint_broken_wikilink_warning(tmp_path):
    compiler = WikiCompiler(store=_StubStore(), wiki_dir=tmp_path)
    compiler.write_page(_frontmatter_page("p1", "links to [[ghost_page]]"))
    result = compiler.lint()
    assert any("Broken wikilink: [[ghost_page]]" in i.message for i in result.issues)


def test_lint_domain_link_resolves_against_store_domains(tmp_path):
    store = _StubStore(funcs=[_func(fid="f1", domain="auth")])
    compiler = WikiCompiler(store=store, wiki_dir=tmp_path)
    compiler.write_page(_frontmatter_page("p1", "see [[domain_auth]]"))
    result = compiler.lint()
    assert not any("Broken wikilink" in i.message for i in result.issues)


def test_lint_unknown_domain_link_warns(tmp_path):
    """No blanket domain_ skip: unknown domain links are broken."""
    compiler = WikiCompiler(store=_StubStore(), wiki_dir=tmp_path)
    compiler.write_page(_frontmatter_page("p1", "see [[domain_nowhere]]"))
    result = compiler.lint()
    assert any("Broken wikilink: [[domain_nowhere]]" in i.message for i in result.issues)


def test_lint_orphaned_page_warning(tmp_path):
    compiler = WikiCompiler(store=_StubStore(), wiki_dir=tmp_path)
    compiler.write_page(_frontmatter_page("p_linker", "links to [[p_linked]]"))
    compiler.write_page(_frontmatter_page("p_linked", "no links"))
    result = compiler.lint()
    orphan_ids = {i.page_id for i in result.issues if "Orphaned" in i.message}
    assert orphan_ids == {"p_linker"}


def test_lint_index_links_prevent_orphan_warning(tmp_path):
    compiler = WikiCompiler(store=_StubStore(), wiki_dir=tmp_path)
    compiler.write_page(_frontmatter_page("p1", "no links"))
    idx = WikiPage(page_id="index", content="# Index\n\n- [[p1]]", metadata={})
    compiler.write_index(idx)
    result = compiler.lint()
    assert not any("Orphaned" in i.message for i in result.issues)


# ── DualIndexSearch: FTS word boundaries, RRF, vector threshold ──────


def test_fts_search_matches_word_boundaries(tmp_path):
    """'ai' must not match inside 'said'."""
    idx = DualIndexSearch(wiki_dir=tmp_path, embedding_service=_StubEmbedding())
    idx.add_page(_page("p1", "he said nothing relevant"))
    idx.add_page(_page("p2", "the ai model"))
    ids = {r.func_id for r in idx._fts_search("ai")}
    assert "p2" in ids
    assert "p1" not in ids


def _result(fid):
    return SearchResult(
        func_id=fid,
        name=fid,
        domain="",
        relevance_score=1.0,
        summary="",
        source_type=SourceType.WIKI,
    )


def test_rrf_merges_fts_and_vector_results():
    fts = [_result("a"), _result("b")]
    vec = [_result("b"), _result("c")]
    merged = DualIndexSearch._reciprocal_rank_fusion(fts, vec)
    ids = [r.func_id for r in merged]
    assert set(ids) == {"a", "b", "c"}
    # b appears in both lists -> highest RRF score
    assert ids[0] == "b"


def test_dual_index_search_includes_vector_only_hit(tmp_path):
    """A page matching only via vector similarity is still returned."""
    idx = DualIndexSearch(wiki_dir=tmp_path, embedding_service=_FixedEmbedding())
    idx.add_page(_page("p_vec", "zzz qqq"))
    results = idx.search("nomatchterm", top_k=5)
    assert "p_vec" in {r.func_id for r in results}


def test_dual_index_vector_threshold_configurable(tmp_path):
    idx = DualIndexSearch(
        wiki_dir=tmp_path,
        embedding_service=_FixedEmbedding(),
        vector_threshold=1.1,  # above the max possible cosine similarity
    )
    idx.add_page(_page("p1", "zzz"))
    assert idx.search("nomatch", top_k=5) == []


# ── LLMWikiGenerator (stubbed LLM) ───────────────────────────────────


async def test_generate_entity_page_calls_llm():
    gen = LLMWikiGenerator(llm_enhancer=_StubEnhancer(_StubLLM(text="wiki page text")))
    out = await gen.generate_entity_page(_func())
    assert out == "wiki page text"


async def test_update_cross_references_injects_links_and_sep():
    llm = _StubLLM(json_result={"related": [{"id": "other_page", "reason": "related"}]})
    gen = LLMWikiGenerator(llm_enhancer=_StubEnhancer(llm))
    pages = [_page("p1", "# p1\n\nbody"), _page("p2", "# p2\n\nbody")]
    updated = await gen.update_cross_references(pages)
    cross_ref_section = updated[0].content.split("Cross-References (LLM)")[1]
    assert "[[other_page]]" in cross_ref_section
    assert "---" in cross_ref_section


async def test_update_cross_references_custom_sep():
    llm = _StubLLM(json_result={"related": [{"id": "x", "reason": "r"}]})
    gen = LLMWikiGenerator(llm_enhancer=_StubEnhancer(llm), sep="***")
    updated = await gen.update_cross_references([_page("p1", "body")])
    assert "***" in updated[0].content


async def test_update_cross_references_failure_keeps_original():
    gen = LLMWikiGenerator(llm_enhancer=_StubEnhancer(_StubLLM(fail=True)))
    updated = await gen.update_cross_references([_page("p1", "original body")])
    assert updated[0].content == "original body"


# ── Package exports ──────────────────────────────────────────────────


def test_wiki_package_exports_graph_community_detector():
    import memplex.wiki

    assert memplex.wiki.GraphCommunityDetector is GraphCommunityDetector


# ── WikiCompiler community wiring (Wave 2a: graph.community_* config) ──


class _GraphStubStore(_StubStore):
    """Store stub exposing get_graph/get for community detection."""

    def get_graph(self, func_ids=None):
        return GraphData(nodes=list(self._funcs), edges=[])

    def get(self, memory_id):
        return next((f for f in self._funcs if f.id == memory_id), None)


def _graph_config(enabled=True, min_size=3):
    from memplex.config import GraphConfig

    return GraphConfig(community_detection_enabled=enabled, community_min_size=min_size)


def test_compile_communities_empty_without_graph_config(tmp_path):
    """Default behaviour unchanged: no graph_config -> no community pages."""
    funcs = [_func(fid=f"f{i}") for i in range(3)]
    compiler = WikiCompiler(store=_GraphStubStore(funcs), wiki_dir=tmp_path)
    assert compiler.compile_communities() == []


def test_compile_communities_disabled_by_config(tmp_path):
    funcs = [_func(fid=f"f{i}") for i in range(3)]
    compiler = WikiCompiler(
        store=_GraphStubStore(funcs),
        wiki_dir=tmp_path,
        graph_config=_graph_config(enabled=False),
    )
    assert compiler.compile_communities() == []


def test_compile_communities_emits_concept_pages(tmp_path):
    funcs = [_func(fid=f"f{i}", domain="auth") for i in range(3)]
    compiler = WikiCompiler(
        store=_GraphStubStore(funcs),
        wiki_dir=tmp_path,
        graph_config=_graph_config(enabled=True, min_size=3),
    )
    pages = compiler.compile_communities()
    assert len(pages) == 1
    assert pages[0].page_id.startswith("community_")
    assert "[[f0]]" in pages[0].content


def test_compile_communities_min_size_filters_small_communities(tmp_path):
    funcs = [_func(fid=f"f{i}", domain="auth") for i in range(3)]
    compiler = WikiCompiler(
        store=_GraphStubStore(funcs),
        wiki_dir=tmp_path,
        graph_config=_graph_config(enabled=True, min_size=10),
    )
    assert compiler.compile_communities() == []


def test_compile_communities_skips_store_without_get_graph(tmp_path):
    compiler = WikiCompiler(
        store=_StubStore([_func()]),
        wiki_dir=tmp_path,
        graph_config=_graph_config(enabled=True),
    )
    assert compiler.compile_communities() == []


def test_compile_all_includes_community_pages_when_enabled(tmp_path):
    funcs = [_func(fid=f"f{i}", domain="auth") for i in range(3)]
    compiler = WikiCompiler(
        store=_GraphStubStore(funcs),
        wiki_dir=tmp_path,
        graph_config=_graph_config(enabled=True, min_size=3),
    )
    pages = compiler.compile_all()
    assert any(p.page_id.startswith("community_") for p in pages)


def test_compile_all_excludes_community_pages_when_disabled(tmp_path):
    funcs = [_func(fid=f"f{i}", domain="auth") for i in range(3)]
    compiler = WikiCompiler(
        store=_GraphStubStore(funcs),
        wiki_dir=tmp_path,
        graph_config=_graph_config(enabled=False),
    )
    pages = compiler.compile_all()
    assert not any(p.page_id.startswith("community_") for p in pages)
