#!/usr/bin/env python3
"""Re-judge a saved official-J run with a different judge model.

Reads the per-question records written by ``run_lme_official_j.py``
(``hypotheses.jsonl``) and labels the SAME ``hypothesis`` answers again
with the verbatim official ``evaluate_qa.py`` prompts. Nothing about the
answers changes -- this measures judge-identity sensitivity of a score
that was produced under a glm-5.3 self-judge.

Two transports:
- ``anthropic`` (default): bigmodel Anthropic-compatible endpoint, same
  one the original run used, so a glm-5.2 re-judge differs from the
  original glm-5.3 judging by the model id only.
- ``openai``: any OpenAI-compatible endpoint (``OPENAI_BASE_URL`` +
  ``OPENAI_API_KEY``, e.g. api.openai.com with judge model
  ``gpt-4o-2024-08-06``) -- the official-caliber pass.

Usage:
    .venv/bin/python scripts/rejudge_official_j.py \
        --records docs/evidence/g003-lme500-official-j-v11/hypotheses.jsonl \
        --judge-model glm-5.2 --out benchmarks/results/lme-j500-rejudge-glm52

Resume-safe: per-record verdicts append to ``rejudge.jsonl``; confirmed
labels are skipped on restart; aggregation dedupes keeping the last.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

import httpx

# ── official judge prompts (verbatim mirror of run_lme_official_j.py) ──

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


class AnthropicJudge:
    """bigmodel Anthropic-protocol judge -- mirrors run_lme_official_j.Proxy."""

    def __init__(self, model: str) -> None:
        settings = json.loads(
            pathlib.Path(os.path.expanduser("~/.claude/settings.json")).read_text()
        )["env"]
        self._model = model
        self._client = httpx.Client(
            base_url=settings["ANTHROPIC_BASE_URL"],
            timeout=120,
            trust_env=False,  # QuickQ env proxies hijack localhost/direct calls
            headers={
                "x-api-key": settings["ANTHROPIC_AUTH_TOKEN"],
                "anthropic-version": "2023-06-01",
            },
        )

    def verdict(self, prompt: str, max_tokens: int) -> str:
        for attempt in range(6):
            try:
                resp = self._client.post(
                    "/v1/messages",
                    json={
                        "model": self._model,
                        "max_tokens": max_tokens,
                        "temperature": 0.0,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                resp.raise_for_status()
                return "".join(
                    block.get("text", "")
                    for block in resp.json().get("content", [])
                    if block.get("type") == "text"
                )
            except Exception:
                if attempt == 5:
                    raise
                time.sleep(min(60, 5 * 2**attempt))
        return ""  # unreachable


class OpenAIJudge:
    """OpenAI-protocol judge (official caliber: gpt-4o-2024-08-06)."""

    def __init__(self, model: str) -> None:
        self._model = model
        self._client = httpx.Client(
            base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            timeout=120,
            trust_env=False,
            headers={"Authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"},
        )

    def verdict(self, prompt: str, max_tokens: int) -> str:
        for attempt in range(6):
            try:
                resp = self._client.post(
                    "/chat/completions",
                    json={
                        "model": self._model,
                        "max_tokens": max_tokens,
                        "temperature": 0.0,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                resp.raise_for_status()
                return resp.json()["choices"][0]["message"].get("content") or ""
            except Exception:
                if attempt == 5:
                    raise
                time.sleep(min(60, 5 * 2**attempt))
        return ""  # unreachable


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--records", type=pathlib.Path, required=True)
    parser.add_argument("--judge-model", required=True)
    parser.add_argument("--protocol", choices=("anthropic", "openai"), default="anthropic")
    parser.add_argument("--max-tokens", type=int, default=512)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--out", type=pathlib.Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    out_path = args.out / "rejudge.jsonl"

    records: dict[str, dict] = {}
    for line in args.records.read_text().splitlines():
        if line.strip():
            record = json.loads(line)
            records[record["question_id"]] = record
    original = list(records.values())
    if args.limit:
        original = original[: args.limit]

    done: set[str] = set()
    if out_path.exists():
        for line in out_path.read_text().splitlines():
            if line.strip():
                record = json.loads(line)
                if record["rejudge_label"] is not None:
                    done.add(record["question_id"])
    todo = [r for r in original if r["question_id"] not in done]
    print(
        f"records={len(original)} judge={args.judge_model} protocol={args.protocol} "
        f"already_done={len(done)} todo={len(todo)}",
        flush=True,
    )

    judge = AnthropicJudge(args.judge_model) if args.protocol == "anthropic" else OpenAIJudge(args.judge_model)
    out = open(out_path, "a", encoding="utf-8")  # noqa: SIM115 - append log for the whole run
    lock_write = threading.Lock()
    counter = {"n": 0}
    t0 = time.time()

    def work(record: dict) -> None:
        prompt = judge_prompt(
            record["question_type"],
            record["question"],
            record["answer"],
            record["hypothesis"],
            record["abstention"],
        )
        try:
            verdict = judge.verdict(prompt, args.max_tokens)
            label = "yes" in verdict.lower()
        except Exception as exc:  # noqa: BLE001 - null labels retry on resume
            print(f"{record['question_id']}: judge failed: {exc}", flush=True)
            verdict, label = f"error: {exc}", None
        line = json.dumps(
            {
                "question_id": record["question_id"],
                "question_type": record["question_type"],
                "original_label": record["autoeval_label"],
                "rejudge_label": label,
                "judge_verdict": verdict.strip()[:64],
            },
            ensure_ascii=False,
        )
        with lock_write:
            out.write(line + "\n")
            out.flush()
            counter["n"] += 1
            if counter["n"] % 25 == 0:
                print(f"{counter['n']}/{len(todo)} elapsed={time.time() - t0:.0f}s", flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        list(pool.map(work, todo))
    out.close()

    # ── aggregate: side-by-side against the original labels ──
    rejudged: dict[str, dict] = {}
    for line in out_path.read_text().splitlines():
        if line.strip():
            record = json.loads(line)
            rejudged[record["question_id"]] = record
    triples = [
        (bool(records[qid]["autoeval_label"]), bool(rec["rejudge_label"]), rec["question_type"])
        for qid, rec in rejudged.items()
        if rec["rejudge_label"] is not None and records[qid]["autoeval_label"] is not None
    ]
    by_type: dict[str, list[tuple[bool, bool]]] = defaultdict(list)
    yes_to_no = no_to_yes = 0
    for original_label, new, qtype in triples:
        by_type[qtype].append((original_label, new))
        if original_label and not new:
            yes_to_no += 1
        elif new and not original_label:
            no_to_yes += 1
    summary = {
        "benchmark": "longmemeval_s_official_j_rejudge",
        "source_records": str(args.records),
        "original_judge": "glm-5.3",
        "rejudge_model": args.judge_model,
        "rejudge_protocol": args.protocol,
        "judge_protocol": "official evaluate_qa.py yes/no prompts, temperature 0",
        "samples_paired": len(triples),
        "original_J": round(sum(1 for o, _, _ in triples if o) / len(triples), 4) if triples else None,
        "rejudged_J": round(sum(1 for _, n, _ in triples if n) / len(triples), 4) if triples else None,
        "agreement": round(sum(1 for o, n, _ in triples if o == n) / len(triples), 4) if triples else None,
        "flips_yes_to_no": yes_to_no,
        "flips_no_to_yes": no_to_yes,
        "by_type": {
            qtype: {
                "original_J": round(sum(1 for o, _ in v if o) / len(v), 4),
                "rejudged_J": round(sum(1 for _, n in v if n) / len(v), 4),
                "n": len(v),
            }
            for qtype, v in sorted(by_type.items())
        },
    }
    (args.out / "summary.json").write_text(json.dumps(summary, indent=2))
    print(json.dumps(summary, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
