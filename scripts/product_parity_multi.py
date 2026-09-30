#!/usr/bin/env python3
"""ADR-013 Stage-2 acceptance: answer-level product parity (multi pool).

The acceptance target: the product path (write_text -> orchestrated
query, raw-paragraph layer on) with the same answerer and official
judge as the J-score harness reaches >= 0.8045 on the multi-session
pool - the level the v4-era harness showed while the P0-era product
probe sat at 0.797. Each question: fresh service, seed the haystack
sessions through the real write path (date-threaded headers), retrieve
with orchestrated=True, answer with the harness generation prompt
(verbatim), judge with the official standard template (verbatim).

Resume-safe (per-question JSONL); shardable for parallel execution.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import tempfile
import time

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")
os.environ.setdefault("MEMPLEX_LLM_QUERY_ENHANCEMENT", "false")
os.environ.setdefault("MEMPLEX_EMBEDDING_MODEL", "bge-m3")
os.environ.setdefault("MEMPLEX_EMBEDDING_DIMENSION", "1024")
os.environ.setdefault("MEMPLEX_EMBEDDING_DEVICE", "mps")
os.environ.setdefault("PYTORCH_MPS_HIGH_WATERMARK_RATIO", "0.0")

import httpx

from memplex.config import load_config
from memplex.service import MemplexService

sys.path.insert(0, str(_PROJECT_ROOT / "scripts"))
from run_lme_official_j import Proxy as _HarnessProxy
from run_lme_official_j import generate_answer as _harness_generate_answer

DATA = _PROJECT_ROOT / ".memplex/benchmarks/data/longmemeval_s_cleaned.json"
TOP_K = 24
CONTEXT_CHAR_BUDGET = 40000

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

JUDGE_TEMPLATE = "I will give you a question, a correct answer, and a response from a model. Please answer yes if the response contains the correct answer. Otherwise, answer no. If the response is equivalent to the correct answer or contains all the intermediate steps to get the correct answer, you should also answer yes. If the response only contains a subset of the information required by the answer, answer no. \n\nQuestion: {}\n\nCorrect Answer: {}\n\nModel Response: {}\n\nIs the model response correct? Answer yes or no only."


class QuotaExhaustedError(RuntimeError):
    """bigmodel 1310: the weekly/monthly cap is hit - stop, never retry."""


class Proxy:
    """Anthropic-compatible bigmodel client (user-authorized endpoint).

    trust_env=False: a dead local system proxy otherwise hijacks httpx.
    """

    def __init__(self, model: str) -> None:
        settings = json.loads(
            pathlib.Path(os.path.expanduser("~/.claude/settings.json")).read_text()
        )["env"]
        self._model = model
        self._client = httpx.Client(
            base_url=settings["ANTHROPIC_BASE_URL"],
            timeout=600,
            trust_env=False,
            headers={
                "x-api-key": settings["ANTHROPIC_AUTH_TOKEN"],
                "anthropic-version": "2023-06-01",
            },
        )

    def complete(
        self, prompt: str, *, max_tokens: int, temperature: float = 0.0, disable_thinking: bool = False
    ) -> str:
        payload = {
            "model": self._model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if disable_thinking:
            payload["thinking"] = {"type": "disabled"}
        for attempt in range(5):
            try:
                resp = self._client.post("/v1/messages", json=payload)
                if resp.status_code == 429 and "1310" in resp.text:
                    # Quota cap: retrying cannot succeed, and silent
                    # empty answers would corrupt the run (the v2
                    # empty-answer lesson).
                    raise QuotaExhaustedError(resp.text[:200])
                resp.raise_for_status()
                return "".join(
                    b.get("text", "")
                    for b in resp.json().get("content", [])
                    if b.get("type") == "text"
                ).strip()
            except httpx.HTTPStatusError as exc:
                # Content-safety rejections (code 1301) are deterministic
                # per text: degrade to empty answer instead of killing the
                # run; the judge scores it wrong, which is the honest cost
                # of an unanswerable-by-filter question.
                if exc.response.status_code == 400 and "1301" in exc.response.text[:300]:
                    return ""
                if attempt == 4:
                    raise
                time.sleep(min(60, 5 * 2**attempt))
        return ""


def run_question(q: dict, answerer: Proxy, judge: Proxy, *, hubs: bool = False) -> dict:
    store_dir = pathlib.Path(tempfile.mkdtemp(prefix=f"pp-{q['question_id']}-"))
    cfg = load_config()
    cfg.storage.backend = "lite"
    cfg.storage.path = str(store_dir / "s.sqlite3")
    cfg.llm.query_enhancement = False
    svc = MemplexService(config=cfg)
    svc.start()
    try:
        with svc.store.deferred_commit():
            for sid, date, turns in zip(
                q["haystack_session_ids"], q["haystack_dates"], q["haystack_sessions"]
            ):
                text = "\n".join(
                    f"{t.get('role', 'user')}: {t.get('content', '')}" for t in turns
                )
                try:
                    svc.write_text(f"[{sid} @ {date}] {text}", source_type="text")
                except ValueError:
                    continue  # duplicate extraction id: memory already stored
        if hubs:
            # Entity-hub arm (product port of the v13 recipe): the seeded
            # texts carry "[sid @ date]" prefixes; attribute paragraphs to
            # their session, then materialize cross-session anchors as
            # searchable Function records before the query.
            import re as _re

            for row in getattr(svc.store, "_paragraphs", {}).values():
                m = _re.match(r"\[([^\] ]+) @ ([^\]]+)\]", row.get("raw_text", ""))
                if m:
                    row["source"] = m.group(1)
            os.environ["MEMPLEX_ENTITY_HUBS"] = "1"
            from memplex.entity_hubs import build_entity_hubs

            def _hub_llm(prompt: str) -> str:
                return answerer.complete(
                    prompt, max_tokens=1500, disable_thinking=True
                )

            hub_report = build_entity_hubs(svc.store, _hub_llm)
            print(f"    hubs: {hub_report.hubs} built", flush=True)
        result = svc.query(q["question"], top_k=TOP_K, orchestrated=True, explain=False)
        # Raw-first context: paragraphs are the verbatim authoritative
        # layer; when the retrieval surface carries them they win the
        # context slots, extraction digests backfill the remainder.
        paragraphs = getattr(svc.store, "_paragraphs", {})
        context_parts = []
        for r in result.results[:TOP_K]:
            para = paragraphs.get(r.func_id)
            context_parts.append(para["raw_text"] if para else r.summary)
        context = "\n\n".join(context_parts)
    finally:
        svc.stop()

    # Full orchestration tier: the harness's own answer path (SC three
    # votes + context-grounded selector on multi-session), fed by the
    # harness Proxy so thinking/temperature handling matches exactly.
    harness_proxy = _HarnessProxy()
    _harness_patched = False
    try:
        answer = _harness_generate_answer(
            harness_proxy, "multi-session", q["question"],
            context[:CONTEXT_CHAR_BUDGET], q["question_date"],
        )
    except Exception as exc:  # noqa: BLE001 - degrade to direct answer
        if not _harness_patched:
            _harness_patched = True
        print(f"  harness answer failed ({exc}); direct fallback", flush=True)
        prompt = GENERATION_PROMPT.format(
            question_date=q["question_date"],
            context=context[:CONTEXT_CHAR_BUDGET],
            question=q["question"],
        )
        answer = answerer.complete(prompt, max_tokens=2048).strip()
    verdict = judge.complete(
        JUDGE_TEMPLATE.format(q["question"], q["answer"], answer[:2000]),
        max_tokens=512,
    )
    correct = verdict.strip().lower().startswith("yes")
    return {
        "qid": q["question_id"],
        "correct": correct,
        "answer": answer[:200],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard-index", type=int, default=0)
    parser.add_argument("--shard-count", type=int, default=1)
    parser.add_argument("--out", default="benchmarks/results/product-parity-multi")
    parser.add_argument(
        "--hubs",
        action="store_true",
        help="build cross-session entity hubs from the seeded sessions "
        "before the query (product port of the v13 recipe)",
    )
    args = parser.parse_args()

    with open(DATA) as fh:
        data = [q for q in json.load(fh) if q["question_type"] == "multi-session"]
    shard = data[args.shard_index :: args.shard_count]

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    resume = out / f"shard{args.shard_index}.jsonl"
    done: set[str] = set()
    if resume.exists():
        for line in resume.read_text(encoding="utf-8").splitlines():
            if line.strip():
                done.add(json.loads(line)["qid"])
    todo = [q for q in shard if q["question_id"] not in done]
    print(f"shard {args.shard_index}/{args.shard_count}: {len(todo)} to run ({len(done)} resumed)", flush=True)

    answerer = Proxy("glm-5.3")
    judge = Proxy("glm-5.3")
    with open(resume, "a", encoding="utf-8") as fh:
        for i, q in enumerate(todo):
            row = run_question(q, answerer, judge, hubs=args.hubs)
            fh.write(json.dumps(row) + "\n")
            fh.flush()
            print(f"  {i + 1}/{len(todo)} {q['question_id']} correct={row['correct']}", flush=True)

    rows = [json.loads(line) for line in resume.read_text().splitlines() if line.strip()]
    acc = sum(r["correct"] for r in rows) / max(len(rows), 1)
    summary = {
        "benchmark": "product_parity_multi",
        "hubs": args.hubs,
        "pool": "multi-session",
        "n": len(rows),
        "accuracy": round(acc, 4),
        "baseline_product_p0": 0.797,
        "target_harness_v4": 0.8045,
    }
    print(json.dumps(summary, indent=1), flush=True)
    (out / f"summary{args.shard_index}.json").write_text(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
