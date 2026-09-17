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

    def complete(self, prompt: str, *, max_tokens: int, temperature: float, retries: int = 5) -> str:
        for attempt in range(retries + 1):
            try:
                resp = self._client.post(
                    "/v1/messages",
                    json={
                        "model": GENERATION_MODEL,
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
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


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
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
    config = load_config()
    config.storage.backend = "lite"
    config.storage.path = str(pathlib.Path(tempfile.mkdtemp(prefix="lme-j-")) / "s.sqlite3")
    config.llm.query_enhancement = False
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
        runner._seed(svc, ds.to_memories(sample))
        result = svc.query(question, top_k=TOP_K, explain=False)
        context_items = [r.summary for r in result.results[:TOP_K]]
        # Adjacency expansion: a hit turn's neighbours (index +/-1 in the
        # flattened history, i.e. same or adjacent session) frequently
        # carry the continuation that single-turn retrieval misses --
        # the multi-session counting fails are evidence-starved, and
        # deeper top-k saturates (0.782 @24 vs 0.790 @40).
        get = getattr(svc.store, "get", None)
        if callable(get):
            seen_ids = {r.func_id for r in result.results[:TOP_K]}
            for r in result.results[:TOP_K]:
                for delta in (-1, 1):
                    parts = r.func_id.rsplit("-s", 1)
                    if len(parts) != 2 or not parts[1].isdigit():
                        continue
                    neighbour_id = f"{parts[0]}-s{int(parts[1]) + delta}"
                    if neighbour_id in seen_ids:
                        continue
                    try:
                        neighbour = get(neighbour_id)
                    except Exception:  # noqa: BLE001 - expansion is best-effort
                        neighbour = None
                    if neighbour is not None:
                        seen_ids.add(neighbour_id)
                        context_items.append(
                            f"{neighbour.name} {_neighbour_text(neighbour)}".strip()
                        )
        context = "\n".join(f"- {item}" for item in context_items)

        try:
            answer = proxy.complete(
                GENERATION_PROMPT.format(
                    question_date=metadata.get("question_date", "unknown"),
                    context=context[:CONTEXT_CHAR_BUDGET],
                    question=question,
                ),
                max_tokens=2048,
                temperature=0.0,
            ).strip()
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
