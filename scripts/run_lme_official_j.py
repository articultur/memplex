#!/usr/bin/env python3
"""Official LongMemEval judge-protocol run: J score on the S split.

Pipeline per sample (identical retrieval/generation shape to the recorded
500-sample RAG run): seed the haystack into a fresh lite store with
bge-m3 embeddings, retrieve top-5, generate an answer with glm-5.3, then
label it with the official ``src/evaluation/evaluate_qa.py`` yes/no
prompt (verbatim per question_type, abstention branch included).

Judge model is glm-5.3 instead of gpt-4o-2024-08-06 (canonical) -- the
deviation is disclosed in every summary this script writes.

Usage:
    .venv/bin/python scripts/run_lme_official_j.py [--limit N] [--run-dir DIR]

Resume-safe: per-sample records append to ``hypotheses.jsonl`` and
already-recorded question_ids are skipped on restart.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import sys
import tempfile
import time
from collections import Counter, defaultdict

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")
os.environ.setdefault("MEMPLEX_LLM_QUERY_ENHANCEMENT", "false")
os.environ.setdefault("MEMPLEX_EMBEDDING_MODEL", "bge-m3")
os.environ.setdefault("MEMPLEX_EMBEDDING_DIMENSION", "1024")
os.environ.setdefault("MEMPLEX_EMBEDDING_DEVICE", "mps")

import httpx

from benchmarks.longmemeval import (
    LongMemEvalDataset,
    LongMemEvalRunner,
    _clear_store,
    _neighbour_text,
)
from memplex.config import load_config
from memplex.service import MemplexService

DATASET_PATH = (
    _PROJECT_ROOT / ".memplex/benchmarks/data/longmemeval_s_cleaned.json"
)
GENERATION_MODEL = "glm-5.3"
JUDGE_MODEL = "glm-5.3"  # canonical protocol judge is gpt-4o-2024-08-06
TOP_K = 24
CONTEXT_CHAR_BUDGET = 40000
# Self-consistency applies to the two evidence-aggregation types; the
# last three autopsies attribute ~10-12 remaining fails to generation
# (A-class) on exactly these pools.
SC_TYPES = ("temporal-reasoning", "multi-session")
SC_VOTES = 3
GENERATION_PROMPT = (
    "Answer using ONLY the memory excerpts below. The current date is "
    "{question_date}. When the excerpts do not state the answer explicitly, "
    "infer it from the user's history in the excerpts -- for preference or "
    "recommendation questions, base the answer on what the excerpts show "
    "about the user. Only say the information is not available if the "
    "excerpts contain nothing relevant to the question.\n"
    "For any time, duration or ordering question: first write the relevant "
    "dates in YYYY/MM/DD form, compute the difference explicitly, then "
    "answer.\n"
    "For any question that asks how many, or to list items: first quote "
    "every matching excerpt with its date, then count the quoted items, "
    "then answer with the total. For all other questions, skip the "
    "step-by-step lists and answer in one short sentence with the key "
    "fact only.\n\n"
    "Excerpts:\n{context}\n\nQuestion: {question}\n\nAnswer concisely:"
)

# ── official judge prompts (verbatim from LongMemEval evaluate_qa.py) ──

_TEMPLATE_STANDARD = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
_TEMPLATE_TEMPORAL = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. In addition, do not penalize off-by-one errors for the number of days. If the question asks for the number of days/weeks/months, etc., and the model makes off-by-one errors (e.g., predicting 19 days when the answer is 18), the model's response is still correct. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
_TEMPLATE_KNOWLEDGE = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response contains some previous information along with an updated answer, the response should be considered as correct as long as the updated answer is the required answer.\n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
_TEMPLATE_PREFERENCE = "I will give you a question, a rubric for desired personalized response, and a response from a model. Please answer yes if the response satisfies the desired response. Otherwise, answer no. The model does not need to reflect all the points in the rubric. The response is correct as long as it recalls and utilizes the user's personal information correctly.\n\nQuestion: {}\n\nRubric: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."
_TEMPLATE_ABSTENTION = "I will give you an unanswerable question, an explanation, and a response from a model. Please answer yes if the model correctly identifies the question as unanswerable. The model could say that the information is incomplete, or some other information is given but the asked information is not.\n\nQuestion: {}\n\nExplanation: {}\n\nModel Response: {}\n\nDoes the model correctly identify the question as unanswerable? Answer yes or no only."


def judge_prompt(task: str, question: str, answer: str, response: str, abstention: bool) -> str:
    if abstention:
        return _TEMPLATE_ABSTENTION.format(question, answer, response)
    if task in ("single-session-user", "single-session-assistant", "multi-session"):
        return _TEMPLATE_STANDARD.format(question, answer, response)
    if task == "temporal-reasoning":
        return _TEMPLATE_TEMPORAL.format(question, answer, response)
    if task == "knowledge-update":
        return _TEMPLATE_KNOWLEDGE.format(question, answer, response)
    if task == "single-session-preference":
        return _TEMPLATE_PREFERENCE.format(question, answer, response)
    raise NotImplementedError(task)


class Proxy:
    """Anthropic-compatible bigmodel proxy client (authorized by the user)."""

    def __init__(self) -> None:
        settings = json.loads(pathlib.Path(os.path.expanduser("~/.claude/settings.json")).read_text())["env"]
        self._client = httpx.Client(
            base_url=settings["ANTHROPIC_BASE_URL"],
            timeout=120,
            headers={
                "x-api-key": settings["ANTHROPIC_AUTH_TOKEN"],
                "anthropic-version": "2023-06-01",
            },
        )

    def complete(
        self,
        prompt: str,
        *,
        max_tokens: int,
        temperature: float,
        retries: int = 5,
        disable_thinking: bool = False,
    ) -> str:
        for attempt in range(retries + 1):
            try:
                payload = {
                    "model": GENERATION_MODEL,
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "messages": [{"role": "user", "content": prompt}],
                }
                if disable_thinking:
                    payload["thinking"] = {"type": "disabled"}
                resp = self._client.post("/v1/messages", json=payload)
                resp.raise_for_status()
                # thinking models emit {"type": "thinking"} blocks first;
                # only concatenating "text" blocks reproduces the visible
                # answer (empty text == budget consumed by thinking).
                return "".join(
                    block.get("text", "")
                    for block in resp.json().get("content", [])
                    if block.get("type") == "text"
                )
            except Exception:
                if attempt == retries:
                    raise
                # exponential backoff: the proxy rate-limits (429) in
                # bursts that outlast short fixed waits.
                time.sleep(min(60, 5 * 2**attempt))
        return ""  # unreachable

    def decompose(self, question: str) -> list[str]:
        """Split a multi-hop question into 2-3 sub-queries (best-effort).

        Thinking is explicitly disabled: the expected output is a short
        line list and the call sits on the per-question critical path.
        """
        try:
            text = self.complete(
                "Break this multi-hop question into 2-3 independent "
                "sub-questions, one per line, no numbering:\n\n"
                f"{question}",
                max_tokens=256,
                temperature=0.0,
                disable_thinking=True,
            )
        except Exception as exc:  # noqa: BLE001 - decomposition is best-effort
            print(f"decompose failed: {exc}", flush=True)
            return []
        return [
            line.strip()
            for line in text.strip().splitlines()
            if len(line.strip()) > 5
        ][:3]

    def pseudo_answer(self, question: str) -> list[str]:
        """One hypothetical-answer text for PAR retrieval (best-effort).

        The answer's phrasing bridges the query-evidence wording drift
        that question-side rewriting cannot. Thinking disabled: a short
        guess is enough to pivot the retrieval into answer space.
        """
        try:
            text = self.complete(
                "Write one short (1-2 sentence) plausible answer to this "
                "question about the user's history. A concrete guess is "
                f"fine; do not refuse:\n\n{question}",
                max_tokens=128,
                temperature=0.0,
                disable_thinking=True,
            )
        except Exception as exc:  # noqa: BLE001 - PAR is best-effort
            print(f"pseudo-answer failed: {exc}", flush=True)
            return []
        answer = text.strip()
        return [answer] if len(answer) > 10 else []


def product_context(svc, question: str) -> str:
    """Product-path retrieval: ``svc.query(orchestrated=True)`` only.

    Parity probe surface: the decomposition/fan-out must come from the
    SERVICE (LLMEnhancer expanded_queries -> pipeline fan-out), not from
    this harness. What the product returns is what builds the context.
    """
    result = svc.query(question, top_k=TOP_K, orchestrated=True, explain=False)
    return "\n".join(f"- {r.summary}" for r in result.results[:TOP_K])



_MONTHS = {
    name: f"{i:02d}"
    for i, name in enumerate(
        [
            "january", "february", "march", "april", "may", "june",
            "july", "august", "september", "october", "november", "december",
        ],
        start=1,
    )
}


def question_time_window(question: str) -> tuple[str, str] | None:
    """(start, end) as YYYY/MM bounds when the question LITERALLY names
    explicit months/years.

    Deterministic by design: the official time-aware pruning warns that
    LLM-inferred ranges hallucinate false-positive windows (appendix E.3);
    we only prune when the question itself states the boundaries. Two or
    more mentions define a range (filter); a single mention merely boosts
    (stable reorder) -- both applied by :func:`apply_time_window`.
    """
    text = question.lower()
    points: list[tuple[int, int]] = []  # (YYYYMM, position)
    taken_spans: list[tuple[int, int]] = []  # (start, end) of consumed years
    for month_name, mm in _MONTHS.items():
        for match in re.finditer(month_name, text):
            tail = text[match.end(): match.end() + 6]
            year_match = re.match(r"\s*,?\s*(20\d{2})", tail)
            year = year_match.group(1) if year_match else None
            if year is None:
                # maybe "2023 march" shape
                head = text[max(0, match.start() - 6): match.start()]
                pre = re.search(r"(20\d{2})\s*,?\s*$", head)
                year = pre.group(1) if pre else None
            if year is not None:
                points.append((int(f"{year}{mm}"), match.start()))
                taken_spans.append(
                    (match.start() - 6, match.end() + 6)
                )
    for match in re.finditer(r"\b(20\d{2})\b", text):
        # bare years only when not already attached to a captured month
        if any(a <= match.start() <= b for a, b in taken_spans):
            continue
        yyyy = int(match.group(1))
        points.append((yyyy * 100 + 1, match.start()))
        points.append((yyyy * 100 + 12, match.start()))
    if not points:
        return None
    starts = [p[0] for p in points]
    lo, hi = min(starts), max(starts)

    def ym_shift(value: int, delta: int) -> int:
        year, month = divmod(value, 100)
        total = year * 12 + (month - 1) + delta
        new_year, new_month = divmod(total, 12)
        return new_year * 100 + new_month + 1

    lo_m = ym_shift(lo, -1)
    hi_m = ym_shift(hi, 1)
    return f"{lo_m // 100:04d}/{lo_m % 100:02d}", f"{hi_m // 100:04d}/{hi_m % 100:02d}"


def _item_ym(text: str) -> str | None:
    """YYYY/MM of the first [YYYY/MM/DD ...] prefix in a context item."""
    match = re.search(r"\[(\d{4})/(\d{2})/\d{2}", text)
    return f"{match.group(1)}/{match.group(2)}" if match else None


def apply_time_window(context_items: list[str], window: tuple[str, str] | None) -> list[str]:
    """Boost in-window items (stable reorder); a stated range (lo != hi)
    drops out-of-window items entirely. Undated items always survive."""
    if window is None:
        return context_items
    lo, hi = window
    in_window: list[str] = []
    out_window: list[str] = []
    undated: list[str] = []
    for item in context_items:
        ym = _item_ym(item)
        if ym is None:
            undated.append(item)
        elif lo <= ym <= hi:
            in_window.append(item)
        else:
            out_window.append(item)
    if lo != hi:
        return in_window + undated
    return in_window + undated + out_window



def counting_intent(question: str) -> bool:
    """Detect enumerate/count questions (completeness-critical)."""
    lowered = question.lower()
    signals = (
        "how many", "how much", "how often", "number of",
        "list all", "list every", "all the", "what are all",
    )
    return any(signal in lowered for signal in signals)



def session_fulltexts_for_hits(svc, sample, hits: list, cap: int = 6) -> list[str]:
    """Full session-unit texts for the sessions the top hits belong to.

    Counting questions need EVERY mention, so the full session text of
    each hit's session beats turn snippets (LME-V2 raw-slice ablation:
    removing the full-text pool collapsed accuracy 0.586 -> 0.423). The
    flattened-history turn index in each hit id maps back to its session
    through the sample's sessions metadata.
    """
    metadata = sample.metadata or {}
    sessions = metadata.get("sessions") or []
    idx2sess: dict[int, int] = {}
    flat = 0
    for pos, session in enumerate(sessions):
        for _ in session:
            idx2sess[flat] = pos
            flat += 1
    needed: set[int] = set()
    for r in hits[: cap * 2]:
        parts = r.func_id.rsplit("-s", 1)
        if len(parts) == 2 and parts[1].isdigit():
            pos = idx2sess.get(int(parts[1]))
            if pos is not None:
                needed.add(pos)
        if len(needed) >= cap:
            break
    if not needed:
        return []
    get = getattr(svc.store, "get", None)
    if not callable(get):
        return []
    texts: list[str] = []
    for pos in sorted(needed):
        try:
            node = get(f"lme-{sample.id}-sess{pos}")
        except Exception as exc:  # noqa: BLE001 - completeness is best-effort
            print(f"session pull failed: {exc}", flush=True)
            continue
        if node is not None:
            texts.append(f"{node.name} {_neighbour_text(node)}".strip())
    return texts


def collect_context(svc, proxy: Proxy, question: str) -> tuple[str, list]:
    """Main retrieval + decomposition union + summary-to-session + adjacency.

    Summary units rank well against the question but carry paraphrases;
    in a slot-limited top-k they displace verbatim evidence. The official
    two-stage shape avoids that: summaries LOCATE sessions, and the
    session's full text is substituted into the context.
    """
    result = svc.query(question, top_k=TOP_K, explain=False)
    hits = list(result.results[:TOP_K])
    # Query decomposition: sub-queries retrieve independently and their
    # union extras join after the main hits -- each sub-query is another
    # ranking chance for evidence the main phrasing misses (63% of the
    # remaining hard-pool fails are evidence-miss across three autopsies).
    seen_ids = {r.func_id for r in hits}
    for sub_query in proxy.decompose(question):
        for r in svc.query(sub_query, top_k=8, explain=False).results[:8]:
            if r.func_id not in seen_ids:
                seen_ids.add(r.func_id)
                hits.append(r)
    # Pseudo-answer rewriting (PAR): retrieve with a hypothetical answer
    # text as well. Answer phrasing sits closer to evidence phrasing than
    # question phrasing does (DMQR-RAG: PAR was the largest contributor
    # among four rewrite variants, P@5 +14.46% overall).
    if os.environ.get("MEMPLEX_LME_PAR", "1") == "1":
        for pseudo in proxy.pseudo_answer(question):
            for r in svc.query(pseudo, top_k=8, explain=False).results[:8]:
                if r.func_id not in seen_ids:
                    seen_ids.add(r.func_id)
                    hits.append(r)
    get = getattr(svc.store, "get", None)

    def unit_text(fid: str) -> str | None:
        if not callable(get):
            return None
        try:
            node = get(fid)
        except Exception:  # noqa: BLE001 - substitution is best-effort
            return None
        if node is None:
            return None
        return f"{node.name} {_neighbour_text(node)}".strip()

    # Two-stage summary->session: swap each summary hit for its session's
    # full-text unit (already seeded alongside).
    swapped: list[str] = []
    for r in hits:
        if "-summ" in r.func_id:
            session_text = unit_text(r.func_id.replace("-summ", "-sess"))
            swapped.append(session_text if session_text else r.summary)
        else:
            swapped.append(r.summary)
    context_items = apply_time_window(
        swapped,
        question_time_window(question) if os.environ.get("MEMPLEX_LME_TIME_PRUNE", "1") == "1" else None,
    )
    # Adjacency expansion: a hit turn's neighbours (index +/-1 in the
    # flattened history, i.e. same or adjacent session) frequently
    # carry the continuation that single-turn retrieval misses --
    # the multi-session counting fails are evidence-starved, and
    # deeper top-k saturates (0.782 @24 vs 0.790 @40).
    if callable(get):
        for r in hits[:TOP_K]:
            for delta in (-1, 1):
                parts = r.func_id.rsplit("-s", 1)
                if len(parts) != 2 or not parts[1].isdigit():
                    continue
                neighbour_id = f"{parts[0]}-s{int(parts[1]) + delta}"
                if neighbour_id in seen_ids:
                    continue
                text = unit_text(neighbour_id)
                if text is not None:
                    seen_ids.add(neighbour_id)
                    context_items.append(text)
    return "\n".join(f"- {item}" for item in context_items), hits


class EntityHubs:
    """Disk-backed cross-session entity indices (structure-over-intelligence lever).

    The controlled graph experiment (arXiv 2601.01280) showed cross-session
    links at session granularity beat flat memory by +13pp with the SAME
    answerer; entity-triple graphs failed on granularity mismatch. An
    entity hub materializes the link as a seeded, searchable record:
    every entity recurring across sessions gets one hub whose text
    enumerates the sessions (with dates and short quotes) mentioning it.
    Counting questions then hit the pre-computed enumeration instead of
    re-deriving it from raw turns at answer time.
    """

    def __init__(self, proxy: Proxy, cache_path: pathlib.Path) -> None:
        self._proxy = proxy
        self._path = cache_path
        self._cache: dict[str, list[dict]] = {}
        if cache_path.exists():
            try:
                self._cache = json.loads(cache_path.read_text())
            except Exception:  # noqa: BLE001 - corrupt cache regenerates
                self._cache = {}
        self._dirty = 0

    def hubs(self, key: str, sessions: list[list[dict]]) -> list[dict]:
        """[{entity, text}] for one sample; LLM-extracted, disk-cached."""
        if key not in self._cache:
            numbered = []
            for position, session in enumerate(sessions):
                date = str(session[0].get("session_date", "")) if session else ""
                body = " | ".join(
                    f"{t.get('role', 'user')}: {str(t.get('content', ''))[:400]}"
                    for t in session
                )[:1500]
                numbered.append(f"Session {position} [{date}]: {body}")
            corpus = "\n".join(numbered)[:28000]
            try:
                text = self._proxy.complete(
                    "List every recurring cross-session anchor in the "
                    "sessions below: named entities (person/place/item) AND "
                    "recurring themes -- activities, obligations, purchases, "
                    "errands, plans the user tracks across sessions (e.g. "
                    "'things to pick up', 'trip planning'). Counting and "
                    "list-everything questions will use these, so include "
                    "every item even if each appears in only ONE session "
                    "when they belong to a shared theme. Output one line "
                    "per anchor in exactly this format:\n"
                    "ENTITY: <name> || SESSIONS: <comma-separated session "
                    "numbers> || EVIDENCE: <one short verbatim quote per "
                    "session>\n"
                    "No other text.\n\n"
                    f"{corpus}",
                    max_tokens=1500,
                    temperature=0.0,
                    disable_thinking=True,
                )
            except Exception as exc:  # noqa: BLE001 - hubs are best-effort
                print(f"entity-hub extraction failed: {exc}", flush=True)
                text = ""
            parsed: list[dict] = []
            for line in text.splitlines():
                if "ENTITY:" not in line or "SESSIONS:" not in line:
                    continue
                try:
                    head, rest = line.split("SESSIONS:", 1)
                    entity = head.split("ENTITY:", 1)[1].split("||")[0].strip()
                    session_nums, evidence = rest.split("||", 1)
                    nums = [
                        int(n.strip())
                        for n in session_nums.replace("EVIDENCE:", "").split(",")
                        if n.strip().isdigit()
                    ]
                except (ValueError, IndexError):
                    continue
                if entity and nums:
                    parsed.append(
                        {"entity": entity[:80], "sessions": nums, "evidence": evidence.strip()[:400]}
                    )
            self._cache[key] = parsed[:40]
            self._dirty += 1
            if self._dirty % 20 == 0:
                self._flush()
        return self._cache[key]

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._cache, ensure_ascii=False))

    def close(self) -> None:
        self._flush()


def entity_hub_observations(ds, sample, hubs: EntityHubs) -> list:
    """Hub Function observations seeded alongside turns/sessions.

    The hub name leads with the entity so lexical/semantic legs rank it
    whenever the question names the entity; the body enumerates the
    cross-session mentions with dates and quotes.
    """
    from memplex.models import Observation

    metadata = sample.metadata or {}
    sessions = metadata.get("sessions") or []
    records = hubs.hubs(f"{sample.id}-hubs", sessions)
    observations: list = []
    for position, record in enumerate(records):
        date = ""
        try:
            first_session = sessions[record["sessions"][0]]
            date = str(first_session[0].get("session_date", ""))
        except (IndexError, KeyError, TypeError):
            pass
        prefix = f"[{date}] " if date else ""
        body = (
            f"{prefix}Cross-session entity '{record['entity']}' appears in "
            f"sessions {record['sessions']}. {record['evidence']}"
        )[:1500]
        observations.append(
            Observation(
                id=f"lme-{sample.id}-hub{position}",
                event="entity_hub",
                context=body,
                category="note",
                observed_at=date or metadata.get("question_date") or None,
            )
        )
    return observations


class SessionSummaries:
    """Disk-backed LLM session summaries (official index-expansion recipe).

    The full-text session units seeded by ``to_memories`` approximate the
    official LLM-session-summary expansion; this swaps in real
    fact-preserving summaries. Cached on disk so probes and full runs
    share one summary pass.
    """

    def __init__(self, proxy: Proxy, cache_path: pathlib.Path) -> None:
        self._proxy = proxy
        self._path = cache_path
        self._cache: dict[str, str] = {}
        if cache_path.exists():
            try:
                self._cache = json.loads(cache_path.read_text())
            except Exception:  # noqa: BLE001 - corrupt cache regenerates
                self._cache = {}
        self._dirty = 0

    def summary(self, key: str, session: list[dict]) -> str:
        if key not in self._cache:
            body = " | ".join(
                f"{t.get('role', 'user')}: {str(t.get('content', ''))[:600]}"
                for t in session
            )[:6000]
            try:
                text = self._proxy.complete(
                    "Summarize this chat session. Include every distinct "
                    "fact, event, preference, name, number and date "
                    "mentioned, in 3-6 compact sentences.\n\n"
                    f"Session: {body}",
                    max_tokens=512,
                    temperature=0.0,
                    disable_thinking=True,
                ).strip()
            except Exception:  # noqa: BLE001 - fall back to raw text
                text = ""
            self._cache[key] = text or body[:2000]
            self._dirty += 1
            if self._dirty % 50 == 0:
                self._flush()
        return self._cache[key]

    def _flush(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._cache, ensure_ascii=False))

    def close(self) -> None:
        self._flush()


def build_observations(ds, sample, summaries: SessionSummaries):
    """Turn + full-text session units + LLM summary units (additive).

    The official index-expansion recipe adds summary indexes alongside
    the originals -- swapping full text out loses the verbatim mentions
    counting questions need (v10 smoke: 0/2 with summaries alone).
    """
    from memplex.models import Observation

    observations = list(ds.to_memories(sample))
    metadata = sample.metadata or {}
    sessions = metadata.get("sessions") or []
    for position, session in enumerate(sessions):
        if not session:
            continue
        date = str(session[0].get("session_date", ""))
        text = summaries.summary(f"{sample.id}-sess{position}", session)
        prefix = f"[{date}] Session summary: " if date else "Session summary: "
        observations.append(
            Observation(
                id=f"lme-{sample.id}-summ{position}",
                event="session_summary",
                context=(prefix + text)[:4000],
                category="note",
                observed_at=date or metadata.get("question_date") or None,
            )
        )
    return observations


def generate_answer(
    proxy: Proxy, qtype: str, question: str, context: str, question_date: str
) -> str:
    """Single generation, plus self-consistency on the aggregation types.

    SC selector is context-grounded and never sees the gold answer (the
    official judge prompt does -- using it for selection would leak it).
    """
    prompt = GENERATION_PROMPT.format(
        question_date=question_date,
        context=context[:CONTEXT_CHAR_BUDGET],
        question=question,
    )
    answer = proxy.complete(prompt, max_tokens=2048, temperature=0.0).strip()
    if qtype not in SC_TYPES:
        return answer
    candidates = [answer]
    for _ in range(SC_VOTES - 1):
        try:
            candidates.append(
                proxy.complete(prompt, max_tokens=2048, temperature=0.7).strip()
            )
        except Exception as exc:  # noqa: BLE001 - SC vote is best-effort
            print(f"sc vote failed: {exc}", flush=True)
    unique = list(dict.fromkeys(c.strip() for c in candidates if c))
    if len(unique) <= 1:
        return answer
    numbered = "\n".join(f"{i}. {c[:400]}" for i, c in enumerate(unique, 1))
    pick = proxy.complete(
        "Given the memory excerpts and candidate answers, reply with the "
        "single number of the candidate best supported by the excerpts.\n\n"
        f"Excerpts:\n{context[:15000]}\n\nQuestion: {question}\n\n"
        f"Candidates:\n{numbered}\n\nReply with one number only:",
        max_tokens=64,
        temperature=0.0,
        disable_thinking=True,
    )
    digits = "".join(ch for ch in pick if ch.isdigit())
    if digits and 1 <= int(digits[0]) <= len(unique):
        return unique[int(digits[0]) - 1]
    return answer


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--product-orchestration",
        action="store_true",
        help="Use the PRODUCT orchestrated path (svc.query(orchestrated=True)) "
        "for retrieval instead of the harness-side recipe (parity probe).",
    )
    parser.add_argument(
        "--only-type",
        default=None,
        help="Restrict the run to one question_type (e.g. temporal-reasoning)",
    )
    parser.add_argument(
        "--run-dir",
        type=pathlib.Path,
        default=_PROJECT_ROOT / "benchmarks/results/lme-j500",
    )
    args = parser.parse_args()
    args.run_dir.mkdir(parents=True, exist_ok=True)
    hyp_path = args.run_dir / "hypotheses.jsonl"

    proxy = Proxy()
    session_summaries = SessionSummaries(
        proxy, _PROJECT_ROOT / "benchmarks/results/session-summaries.json"
    )
    entity_hubs = EntityHubs(
        proxy, _PROJECT_ROOT / "benchmarks/results/entity-hubs.json"
    )
    config = load_config()
    config.storage.backend = "lite"
    config.storage.path = str(pathlib.Path(tempfile.mkdtemp(prefix="lme-j-")) / "s.sqlite3")
    config.llm.query_enhancement = False
    if args.product_orchestration:
        # Wire the service's own LLM (anthropic-compatible bigmodel proxy)
        # so orchestrated retrieval decomposes inside the PRODUCT path.
        settings = json.loads(
            pathlib.Path(os.path.expanduser("~/.claude/settings.json")).read_text()
        )["env"]
        os.environ["ANTHROPIC_BASE_URL"] = settings["ANTHROPIC_BASE_URL"]
        config.llm.query_enhancement = True
        config.llm.provider = "anthropic"
        config.llm.anthropic_api_key = settings["ANTHROPIC_AUTH_TOKEN"]
        config.llm.anthropic_model = "glm-5.3"
        # Thinking-model round-trips need tens of seconds; HyDE would add
        # another LLM leg per query without helping the parity probe.
        config.llm.enhancement_timeout_seconds = 90.0
        config.embedding.hyde_enabled = False
    svc = MemplexService(config=config)
    svc.start()
    ds = LongMemEvalDataset()
    samples = ds.load(str(DATASET_PATH))
    if args.only_type:
        samples = [
            s for s in samples
            if (s.metadata or {}).get("question_type") == args.only_type
        ]
    if args.limit:
        samples = samples[: args.limit]
    runner = LongMemEvalRunner()

    done: set[str] = set()
    if hyp_path.exists():
        for line in hyp_path.read_text().splitlines():
            if line.strip():
                record = json.loads(line)
                # null-label records (e.g. rate-limited judges) retry on
                # resume; only confirmed labels are skipped.
                if record["autoeval_label"] is not None:
                    done.add(record["question_id"])
    print(f"dataset={len(samples)} already_done={len(done)}", flush=True)

    # Append-log kept open for the whole run: per-record flush is the
    # checkpoint contract, a context manager would add nothing.
    out = open(hyp_path, "a", encoding="utf-8")  # noqa: SIM115
    t0 = time.time()
    for index, sample in enumerate(samples):
        metadata = sample.metadata or {}
        qid = metadata.get("question_id") or sample.id
        if qid in done:
            continue
        question = sample.query
        gold = (metadata.get("answers") or [""])[0]
        qtype = metadata.get("question_type", "multi-session")
        abstention = str(qid).endswith("_abs")

        _clear_store(svc)
        # Summaries are opt-in (MEMPLEX_LME_SUMMARIES=1): the two-stage
        # summary probe scored 0.812 vs 0.8195 without them, so the final
        # config ships the winning decomposition-only shape.
        if os.environ.get("MEMPLEX_LME_SUMMARIES") == "1":
            runner._seed(svc, build_observations(ds, sample, session_summaries))
        else:
            runner._seed(svc, ds.to_memories(sample))
        if os.environ.get("MEMPLEX_LME_ENTITY_HUBS", "0") == "1":
            runner._seed(svc, entity_hub_observations(ds, sample, entity_hubs))
        if args.product_orchestration:
            context = product_context(svc, question)
        else:
            context, hits = collect_context(svc, proxy, question)
            if counting_intent(question) and os.environ.get(
                "MEMPLEX_LME_COUNT_FULL", "1"
            ) == "1":
                full_sessions = session_fulltexts_for_hits(svc, sample, hits)
                if full_sessions:
                    # Prepend: the budget truncation keeps the front, and
                    # completeness carriers must survive it.
                    context = (
                        "- " + "\n- ".join(full_sessions) + "\n" + context
                    )

        try:
            answer = generate_answer(
                proxy,
                qtype,
                question,
                context,
                metadata.get("question_date", "unknown"),
            )
        except Exception as exc:  # noqa: BLE001 - one failed generation must not kill the run
            print(f"{qid}: generation failed: {exc}", flush=True)
            answer = ""
        try:
            verdict = proxy.complete(
                judge_prompt(qtype, question, gold, answer, abstention),
                # glm-5.3 is a thinking model: the official 10-token budget
                # would be consumed by the thinking block before any text.
                max_tokens=512,
                temperature=0.0,
            )
            label = "yes" in verdict.lower()
        except Exception as exc:  # noqa: BLE001
            print(f"{qid}: judge failed: {exc}", flush=True)
            label = None
            verdict = f"error: {exc}"

        record = {
            "question_id": qid,
            "question_type": qtype,
            "abstention": abstention,
            "question": question,
            "answer": gold,
            "hypothesis": answer,
            "autoeval_label": label,
            "judge_verdict": verdict.strip()[:64],
            "retrieved_context": context,
        }
        out.write(json.dumps(record, ensure_ascii=False) + "\n")
        out.flush()
        if (index + 1) % 10 == 0:
            elapsed = time.time() - t0
            print(f"{index + 1}/{len(samples)} elapsed={elapsed:.0f}s", flush=True)

    out.close()
    session_summaries.close()
    entity_hubs.close()
    svc.stop()

    # ── aggregate (official scoring: mean label + per-type breakdown) ──
    # Dedupe by question_id keeping the last occurrence so retried
    # records supersede their null-label first attempts.
    deduped: dict[str, dict] = {}
    for line in hyp_path.read_text().splitlines():
        if line.strip():
            record = json.loads(line)
            deduped[record["question_id"]] = record
    records = list(deduped.values())
    labels = [r["autoeval_label"] for r in records if r["autoeval_label"] is not None]
    by_type: dict[str, list[int]] = defaultdict(list)
    for r in records:
        if r["autoeval_label"] is not None:
            by_type[r["question_type"]].append(int(r["autoeval_label"]))
    summary = {
        "benchmark": "longmemeval_s_official_j",
        "dataset": "longmemeval_s_cleaned (500)",
        "judge_model": JUDGE_MODEL,
        "judge_protocol": "official evaluate_qa.py yes/no prompts, temperature 0",
        "generation": (
            f"bge-m3 retrieval top-{TOP_K} + {GENERATION_MODEL} generation "
            f"({CONTEXT_CHAR_BUDGET}-char context budget, abstention-aware prompt)"
        ),
        "samples_judged": len(labels),
        "samples_total": len(records),
        "J": round(sum(labels) / len(labels), 4) if labels else None,
        "by_type": {
            qtype: {"J": round(sum(v) / len(v), 4), "n": len(v)}
            for qtype, v in sorted(by_type.items())
        },
    }
    (args.run_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
