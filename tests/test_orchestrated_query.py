"""Orchestrated retrieval (``query(orchestrated=True)``) contract tests.

The orchestration decomposes a query into LLM sub-queries and fans each
out through multi-path retrieval before the shared merge/rerank. These
tests pin the contract offline: fan-out happens, is bounded, degrades
fail-closed, dedupes by func_id, and defaults to off.
"""

import os

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")
os.environ.setdefault("MEMPLEX_LLM_QUERY_ENHANCEMENT", "false")

import pytest

from memplex.config import MemplexConfig
from memplex.llm.enhancer import LLMEnhancer
from memplex.models import EnhancedQuery
from memplex.service import MemplexService


class _ScriptedProvider:
    """LLMProvider stub whose complete_json returns scripted expansions.

    The real ``LLMEnhancer.enhance_query`` builds the EnhancedQuery itself
    from ``complete_json`` output, so that is the method to script.
    """

    def __init__(self, expanded: list[str], intent: str = "search") -> None:
        self._expanded = expanded
        self._intent = intent
        self.calls = 0

    async def complete_json(self, prompt: str) -> dict:
        self.calls += 1
        return {
            "intent": self._intent,
            "expanded_queries": list(self._expanded),
            "related_concepts": [],
        }

    async def complete(self, prompt: str) -> str:  # pragma: no cover - unused
        return ""


class _FailingProvider(_ScriptedProvider):
    async def complete_json(self, prompt: str) -> dict:
        raise RuntimeError("provider down")


def _service(tmp_path, provider=None) -> MemplexService:
    config = MemplexConfig()
    config.storage.backend = "lite"
    config.storage.path = str(tmp_path)
    config.llm.query_enhancement = provider is not None
    svc = MemplexService(config=config)
    if provider is not None:
        svc._llm = LLMEnhancer(provider, config.llm)
    svc.start()
    svc.write_text(
        "Alice keeps a blue parrot named Kiwi. Bob's guitar lessons are on Tuesdays.",
        source_type="text",
    )
    svc.write_text(
        "The Paris trip is planned for June. Kiwi loves sunflower seeds.",
        source_type="text",
    )
    return svc


def _plain(svc: MemplexService, **kw) -> list[str]:
    result = svc.query("What pet does Alice keep?", top_k=5, explain=True, **kw)
    return [r.func_id for r in result.results]


def test_orchestrated_fanout_appears_in_trace_and_expands_recall(tmp_path):
    provider = _ScriptedProvider(["parrot named Kiwi diet", "Alice pets"])
    svc = _service(tmp_path, provider)
    try:
        result = svc.query(
            "What pet does Alice keep?", top_k=5, explain=True, orchestrated=True
        )
        retrieval = (result.explanation or {}).get("retrieval", {})
        fan = retrieval.get("orchestrated_fanout")
        assert fan is not None
        assert fan["sub_queries"] == 2
    finally:
        svc.stop()


def test_orchestrated_dedupes_union_by_func_id(tmp_path):
    provider = _ScriptedProvider(["blue parrot", "parrot named Kiwi"])
    svc = _service(tmp_path, provider)
    try:
        ids = _plain(svc, orchestrated=True)
        assert len(ids) == len(set(ids)), "union pool must dedupe by func_id"
    finally:
        svc.stop()


def test_orchestrated_fails_closed_to_single_query(tmp_path):
    svc = _service(tmp_path, _FailingProvider([]))
    try:
        result = svc.query(
            "What pet does Alice keep?", top_k=5, explain=True, orchestrated=True
        )
        retrieval = (result.explanation or {}).get("retrieval", {})
        assert "orchestrated_fanout" not in retrieval
        assert result.results, "degraded single-query path must still answer"
    finally:
        svc.stop()


def test_orchestrated_without_llm_degrades_silently(tmp_path):
    svc = _service(tmp_path, provider=None)  # no enhancer at all
    try:
        result = svc.query(
            "What pet does Alice keep?", top_k=5, explain=True, orchestrated=True
        )
        retrieval = (result.explanation or {}).get("retrieval", {})
        assert "orchestrated_fanout" not in retrieval
        assert result.results
    finally:
        svc.stop()


def test_orchestration_sub_queries_sanitized_and_capped():
    enhanced = EnhancedQuery(
        original="q",
        expanded=["", "  ", "q", "valid one", "valid two", "valid three", "valid four"],
        intent="search",
    )
    subs = MemplexService._orchestration_sub_queries("q", enhanced)
    assert subs == ["valid one", "valid two", "valid three"]


def test_config_default_off_and_env_override(tmp_path, monkeypatch):
    assert MemplexConfig().retrieval.orchestrated is False
    monkeypatch.setenv("MEMPLEX_RETRIEVAL_ORCHESTRATED", "true")
    # Env overrides are applied by load_config (env > YAML > defaults),
    # not by bare MemplexConfig construction.
    from memplex.config import load_config

    resolved = load_config(path=str(tmp_path / "nonexistent.yaml"))
    assert resolved.retrieval.orchestrated is True
    svc = _service(tmp_path, _ScriptedProvider(["alice pets"]))
    try:
        svc._config.retrieval.orchestrated = True
        result = svc.query("What pet does Alice keep?", top_k=5, explain=True)
        retrieval = (result.explanation or {}).get("retrieval", {})
        assert "orchestrated_fanout" in retrieval, "config default must enable fan-out"
    finally:
        svc.stop()
        monkeypatch.delenv("MEMPLEX_RETRIEVAL_ORCHESTRATED")


class _ParProvider(_ScriptedProvider):
    """complete_json for enhancement + generate_hypothetical for PAR."""

    def __init__(self, expanded, hypothetical):
        super().__init__(expanded)
        self._hypothetical = hypothetical

    async def generate_hypothetical(self, query: str) -> str:
        return self._hypothetical


def test_orchestrated_par_leg_joins_fanout(tmp_path):
    provider = _ParProvider(["alice pets", "blue parrot"], "The user keeps a blue parrot named Kiwi.")
    svc = _service(tmp_path, provider)
    try:
        result = svc.query(
            "What pet does Alice keep?", top_k=5, explain=True, orchestrated=True
        )
        fan = (result.explanation or {}).get("retrieval", {}).get("orchestrated_fanout")
        assert fan is not None
        # expanded kept to 2 + 1 PAR leg = 3 sub-queries
        assert fan["sub_queries"] == 3
    finally:
        svc.stop()


def test_orchestrated_par_leg_fails_closed(tmp_path, monkeypatch):
    provider = _ParProvider(["alice pets"], "")

    async def boom(query):
        raise RuntimeError("provider down")

    provider.generate_hypothetical = boom
    svc = _service(tmp_path, provider)
    try:
        result = svc.query(
            "What pet does Alice keep?", top_k=5, explain=True, orchestrated=True
        )
        fan = (result.explanation or {}).get("retrieval", {}).get("orchestrated_fanout")
        assert fan is not None
        assert fan["sub_queries"] == 1  # only the expanded leg survives
        assert result.results
    finally:
        svc.stop()


def test_orchestrated_par_env_optout(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMPLEX_ORCHESTRATED_PAR", "0")
    provider = _ParProvider(["alice pets", "blue parrot"], "The user keeps a blue parrot named Kiwi.")
    svc = _service(tmp_path, provider)
    try:
        result = svc.query(
            "What pet does Alice keep?", top_k=5, explain=True, orchestrated=True
        )
        fan = (result.explanation or {}).get("retrieval", {}).get("orchestrated_fanout")
        assert fan is not None
        assert fan["sub_queries"] == 2  # PAR leg disabled
    finally:
        svc.stop()
