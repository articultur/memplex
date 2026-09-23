#!/usr/bin/env python3
"""LongMemEval-V2 V2-a smoke: Memplex memory adapter over text trajectories.

Implements the official ``Memory`` interface (insert/query) on top of
MemplexService and runs the small-tier text-only pipeline end to end:
per question, seed the 100-trajectory haystack (text projection: goal +
per-state url/action/thought), retrieve with orchestrated query, answer
with glm-5.3, and score with the deterministic evaluator specs
(norm_phrase_set_match / mc_choice_match; llm_* checkers fall back to
substring for the smoke). Reports accuracy + query latency (p50/p95).

Usage:
    .venv/bin/python scripts/run_lme_v2.py [--limit N] [--domain web]
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

_PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

os.environ.setdefault("MEMPLEX_STORAGE_BACKEND", "lite")
os.environ.setdefault("MEMPLEX_LLM_QUERY_ENHANCEMENT", "false")
os.environ.setdefault("MEMPLEX_EMBEDDING_MODEL", "bge-m3")
os.environ.setdefault("MEMPLEX_EMBEDDING_DIMENSION", "1024")
os.environ.setdefault("MEMPLEX_EMBEDDING_DEVICE", "mps")
# 100-trajectory haystacks overflow the default MPS watermark in the
# smoke; ratio 0 lets the allocator use the shared pool fully.
os.environ.setdefault("PYTORCH_MPS_HIGH_WATERMARK_RATIO", "0.0")

import httpx

from memplex.config import load_config
from memplex.service import MemplexService

DATA_ROOT = _PROJECT_ROOT / ".memplex/benchmarks/data/longmemeval-v2"
_PROXY: object = None  # set in main(); judges must not run before it
TOP_K = 12


def load_questions(domain: str | None) -> list[dict]:
    with open(DATA_ROOT / "questions.jsonl") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    if domain:
        rows = [r for r in rows if r["domain"] == domain]
    return rows


def load_haystack() -> dict[str, list[str]]:
    with open(DATA_ROOT / "haystacks/lme_v2_small.json") as fh:
        return json.load(fh)


def load_trajectories() -> dict[str, dict]:
    trajs: dict[str, dict] = {}
    with open(DATA_ROOT / "trajectories.jsonl") as fh:
        for line in fh:
            if line.strip():
                t = json.loads(line)
                trajs[t["id"]] = t
    return trajs


def trajectory_to_texts(traj: dict) -> list[str]:
    """State-level text projection (matches the official slice granularity).

    One memory per state keeps documents small (embedding-friendly, no
    truncation cliff) and lets retrieval surface the exact state whose
    a11y tree carries the answer; a goal header document per trajectory
    preserves the task context.
    """
    header = (
        f"[trajectory {traj['id']} env={traj.get('environment', '')}] "
        f"Goal: {traj.get('goal', '')} Outcome: {traj.get('outcome', '')}"
    )
    texts = [header]
    for state in traj.get("states", []):
        url = state.get("url", "")
        action = state.get("action") or ""
        thought = state.get("thought") or ""
        a11y = (state.get("accessibility_tree") or "")[:6000]
        texts.append(
            f"[{traj['id']} @ {url}] action={action} thought={thought} obs={a11y}"
        )
    return texts


class GlmProxy:
    def __init__(self) -> None:
        settings = json.loads(
            pathlib.Path(os.path.expanduser("~/.claude/settings.json")).read_text()
        )["env"]
        self._client = httpx.Client(
            base_url=settings["ANTHROPIC_BASE_URL"],
            timeout=120,
            headers={
                "x-api-key": settings["ANTHROPIC_AUTH_TOKEN"],
                "anthropic-version": "2023-06-01",
            },
        )

    def complete(self, prompt: str, *, max_tokens: int, temperature: float = 0.0) -> str:
        for attempt in range(5):
            try:
                resp = self._client.post(
                    "/v1/messages",
                    json={
                        "model": "glm-5.3",
                        "max_tokens": max_tokens,
                        "temperature": temperature,
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                resp.raise_for_status()
                return "".join(
                    b.get("text", "")
                    for b in resp.json().get("content", [])
                    if b.get("type") == "text"
                )
            except Exception:
                if attempt == 4:
                    raise
                time.sleep(min(60, 5 * 2**attempt))
        return ""


def _normalize(text: str) -> list[str]:
    lowered = text.lower().replace("-", " ")
    lowered = re.sub(r"[^\w\s]", " ", lowered)
    return [t for t in lowered.split() if t]


_JUDGE_PROMPT = (
    "You are grading a question about a deployed software environment. "
    "Given the question, the reference answer, and the model's answer, "
    "reply with exactly YES or NO: does the model's answer match the "
    "reference's substance (equivalent statements count; the model may "
    "add detail but must not contradict or miss the key point)?\n\n"
    "Question: {question}\n\nReference: {gold}\n\nModel answer: "
    "{answer}\n\nReply YES or NO only:"
)


def llm_judge(question: dict, gold: str, answer: str) -> bool:
    """glm-5.3 judge for llm_abstention/llm_gotchas checker specs."""
    try:
        verdict = _PROXY.complete(
            _JUDGE_PROMPT.format(
                question=question["question"][:600],
                gold=gold[:400],
                answer=answer[:600],
            ),
            max_tokens=256,
        )
    except Exception as exc:  # noqa: BLE001 - judge failure scores wrong, never crashes the run
        print(f"judge failed for {question['id']}: {exc}", flush=True)
        return False
    return verdict.strip().upper().startswith("YES")


def score_answer(question: dict, answer: str) -> bool:
    spec = question["eval_function"].split("|")[0]
    gold = str(question.get("answer", "")).strip()
    ans = answer.strip()
    if spec == "mc_choice_match":
        gold_norm = gold.strip().lower()
        if gold_norm in {"true", "false"}:
            answer_norm = ans.strip().lower()
            verdict = "true" if re.search(r"true", answer_norm) else (
                "false" if re.search(r"false", answer_norm) else ""
            )
            return verdict == gold_norm
        m = re.search(r"\b([A-E])\b", ans.upper())
        return bool(m) and m.group(1) == gold.strip().upper()
    if spec.startswith("llm_"):
        return llm_judge(question, gold, ans)
    # norm_phrase_set_match[_ordered]: gold phrases separated by ; or ,
    separators = ";," if "," in (question["eval_function"]) else ";"
    phrases = [p for p in re.split(f"[{separators}]", gold) if p.strip()]
    if not phrases:
        return bool(ans)
    if spec.endswith("_ordered"):
        pos = [ans.lower().find(p.lower()) for p in phrases]
        found = [i for i, p in zip(pos, phrases) if p.lower() in ans.lower()]
        return len(found) == len(phrases) and (
            pos == sorted(pos) if all(p >= 0 for p in pos) else False
        )
    hits = sum(1 for p in phrases if p.lower() in ans.lower())
    return hits >= max(1, int(0.6 * len(phrases)))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--domain", default="web")
    args = parser.parse_args()

    questions = load_questions(args.domain)[: args.limit]
    haystack = load_haystack()
    print(f"questions: {len(questions)} (domain={args.domain})", flush=True)

    global _PROXY
    proxy = GlmProxy()
    _PROXY = proxy
    config = load_config()
    config.storage.backend = "lite"
    config.storage.path = str(
        pathlib.Path(tempfile.mkdtemp(prefix="lme2-")) / "s.sqlite3"
    )
    config.llm.query_enhancement = False
    svc = MemplexService(config=config)
    svc.start()

    results = []
    t_start = time.time()
    for qi, question in enumerate(questions):
        traj_ids = haystack[question["id"]]
        # Seed only unseen trajectories: within a domain all questions
        # share the 100-trajectory haystack, so clear only on domain
        # change (V2 small tier property from SCHEMA.md).
        if qi == 0 or question["domain"] != results[-1]["domain"]:
            from benchmarks.longmemeval import _clear_store

            _clear_store(svc)
            with svc.store.deferred_commit():
                for tid in traj_ids:
                    # trajectories resolved lazily below
                    pass
        # resolve + seed (smoke: reload trajectories dict once, outside loop)
        if qi == 0 or question["domain"] != results[-1]["domain"]:
            _clear_store(svc)
            trajs = load_trajectories()
            needed = [trajs[tid] for tid in traj_ids if tid in trajs]
            with svc.store.deferred_commit():
                for traj in needed:
                    for text in trajectory_to_texts(traj):
                        svc.write_text(text, source_type="text")
            print(f"seeded {len(needed)} trajectories for domain {question['domain']}", flush=True)

        t0 = time.time()
        retrieved = svc.query(question["question"], top_k=TOP_K, orchestrated=True, explain=False)
        latency = time.time() - t0
        context = "\n".join(f"- {r.summary[:1500]}" for r in retrieved.results[:TOP_K])

        try:
            answer = proxy.complete(
                "Answer using ONLY the memory excerpts below. If they do "
                "not contain the answer, say what is missing concisely.\n\n"
                f"Excerpts:\n{context[:20000]}\n\nQuestion: {question['question']}\n\nAnswer concisely:",
                max_tokens=1024,
            ).strip()
        except Exception as exc:  # noqa: BLE001
            print(f"{question['id']}: generation failed {exc}", flush=True)
            answer = ""
        correct = score_answer(question, answer) if answer else False
        results.append(
            {
                "id": question["id"],
                "domain": question["domain"],
                "question_type": question["question_type"],
                "latency_s": round(latency, 3),
                "correct": correct,
                "answer": answer[:200],
            }
        )
        print(
            f"{qi + 1}/{len(questions)} {question['id']} type={question['question_type']} "
            f"correct={correct} latency={latency:.2f}s",
            flush=True,
        )
    svc.stop()

    acc = sum(r["correct"] for r in results) / max(len(results), 1)
    lat = sorted(r["latency_s"] for r in results)
    p50 = lat[len(lat) // 2] if lat else 0
    p95 = lat[int(len(lat) * 0.95)] if lat else 0
    summary = {
        "benchmark": "longmemeval_v2_small",
        "domain": args.domain,
        "n": len(results),
        "accuracy": round(acc, 4),
        "latency_p50_s": round(p50, 3),
        "latency_p95_s": round(p95, 3),
        "wall_s": round(time.time() - t_start, 1),
    }
    print(json.dumps(summary, indent=1))
    out = pathlib.Path("benchmarks/results/lme2-smoke")
    out.mkdir(parents=True, exist_ok=True)
    (out / "records.json").write_text(json.dumps(results, ensure_ascii=False, indent=1))
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
