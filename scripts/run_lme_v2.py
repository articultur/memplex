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
    .venv/bin/python scripts/run_lme_v2.py [--limit N] [--domain web|enterprise]
        [--out DIR]

Resume: appends one JSON line per question to <out>/records.jsonl and
skips ids already present, so an interrupted run continues where it
stopped. Quota exhaustion (bigmodel 1310) aborts with exit code 2 -
resumable - instead of silently scoring a wall of empty answers wrong.
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


def load_haystack(tier: str = "small") -> dict[str, list[str]]:
    with open(DATA_ROOT / f"haystacks/lme_v2_{tier}.json") as fh:
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
        a11y_cap = int(os.environ.get("LME2_A11Y_CAP", "6000"))
        a11y = (state.get("accessibility_tree") or "")[:a11y_cap]
        texts.append(
            f"[{traj['id']} @ {url}] action={action} thought={thought} obs={a11y}"
        )
    return texts


class QuotaExhaustedError(RuntimeError):
    """bigmodel 1310: the weekly/monthly cap is hit - stop, never retry."""


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

    def complete(
        self,
        prompt: str,
        *,
        max_tokens: int,
        temperature: float = 0.0,
        thinking: str = "default",
    ) -> str:
        """One completion. ``thinking="disabled"`` short-circuits the
        thinking block (glm-5.3 on the bigmodel endpoint thinks by
        default, and thinking tokens share the max_tokens budget)."""
        body = {
            "model": "glm-5.3",
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": prompt}],
        }
        if thinking == "disabled":
            body["thinking"] = {"type": "disabled"}
        for attempt in range(5):
            try:
                resp = self._client.post("/v1/messages", json=body)
                if resp.status_code == 429 and "1310" in resp.text:
                    # Monthly/weekly cap: retrying cannot succeed, and an
                    # empty answer would be silently scored wrong.
                    raise QuotaExhaustedError(resp.text[:200])
                resp.raise_for_status()
                return "".join(
                    b.get("text", "")
                    for b in resp.json().get("content", [])
                    if b.get("type") == "text"
                )
            except QuotaExhaustedError:
                raise
            except Exception:
                if attempt == 4:
                    raise
                time.sleep(min(60, 5 * 2**attempt))
        return ""

    def complete_answer(self, prompt: str) -> str:
        """Answerer path with the empty-answer degradation fixed.

        glm-5.3 thinks by default and thinking shares max_tokens: on
        reasoning-heavy questions a 1024 cap burned the whole budget on
        the thinking block (HTTP 200, stop_reason=max_tokens, no text
        block) - the measured cause of all 33 web-medium empty answers
        (docs/evidence/lme2-empty-answers-audit). Two-stage fix: give
        thinking room to finish (8192; the reproduced worst case needed
        5805), and if the response still carries no text, retry once
        with thinking disabled (87-147 tokens observed) so an answer is
        always produced.
        """
        answer = self.complete(prompt, max_tokens=8192)
        if answer.strip():
            return answer
        return self.complete(prompt, max_tokens=1024, thinking="disabled")


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
            thinking="disabled",
        )
    except QuotaExhaustedError:
        raise
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
    parser.add_argument(
        "--tier",
        default="small",
        choices=["small", "medium"],
        help="haystack tier; medium gives each question a distinct "
        "500-trajectory subset (official protocol: fresh per-question "
        "memory). This runner seeds the domain UNION once - a superset "
        "of any single question's haystack, so retrieval faces more "
        "distractors and accuracy is a lower bound for the protocol.",
    )
    parser.add_argument("--out", default="benchmarks/results/lme2-smoke")
    parser.add_argument(
        "--store-dir",
        default=None,
        help="persistent store directory (default: fresh mkdtemp); a long "
        "run resumes with its corpus already seeded and backfilled",
    )
    args = parser.parse_args()

    questions = load_questions(args.domain)[: args.limit]
    haystack = load_haystack(args.tier)
    print(f"questions: {len(questions)} (domain={args.domain})", flush=True)

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    records_path = out / "records.jsonl"
    done: set[str] = set()
    done_domains: set[str] = set()
    if records_path.exists():
        for line in records_path.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                done.add(row["id"])
                done_domains.add(row.get("domain", ""))
        print(f"resume: {len(done)} questions already recorded", flush=True)
    questions = [q for q in questions if q["id"] not in done]

    resumed = bool(done)
    global _PROXY
    proxy = GlmProxy()
    _PROXY = proxy
    config = load_config()
    config.storage.backend = "lite"
    if args.store_dir:
        store_root = pathlib.Path(args.store_dir)
        store_root.mkdir(parents=True, exist_ok=True)
    else:
        store_root = pathlib.Path(tempfile.mkdtemp(prefix="lme2-"))
    config.storage.path = str(store_root / "s.sqlite3")
    config.llm.query_enhancement = False
    svc = MemplexService(config=config)
    svc.start()

    results = []
    t_start = time.time()
    current_domain = None
    for qi, question in enumerate(questions):
        traj_ids = haystack[question["id"]]
        # Within a domain all questions share the 100-trajectory
        # haystack (V2 small tier property from SCHEMA.md), so seed
        # only on domain change.
        if question["domain"] != current_domain:
            current_domain = question["domain"]
            store_has_corpus = bool(svc.store._functions or svc.store._facts)
            if resumed and args.store_dir and store_has_corpus and (
                question["domain"] in done_domains
            ):
                # Persistent-store resume: the corpus is already seeded
                # and vector-backfilled; re-seeding would discard it.
                print(
                    f"resume: reusing seeded corpus for domain {current_domain}",
                    flush=True,
                )
                continue
            from benchmarks.longmemeval import _clear_store

            _clear_store(svc)
            trajs = load_trajectories()
            if args.tier == "medium":
                union_ids: set[str] = set()
                for q in questions:
                    union_ids.update(haystack.get(q["id"], []))
                traj_ids = sorted(union_ids)
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
            answer = proxy.complete_answer(
                "Answer using ONLY the memory excerpts below. If they do "
                "not contain the answer, say what is missing concisely.\n\n"
                f"Excerpts:\n{context[:20000]}\n\nQuestion: {question['question']}\n\nAnswer concisely:",
            ).strip()
        except QuotaExhaustedError:
            print(
                f"{question['id']}: bigmodel quota exhausted (1310) - aborting "
                "for resume; empty answers would be silently scored wrong",
                flush=True,
            )
            svc.stop()
            return 2
        except Exception as exc:  # noqa: BLE001
            print(f"{question['id']}: generation failed {exc}", flush=True)
            answer = ""
        try:
            correct = score_answer(question, answer) if answer else False
        except QuotaExhaustedError:
            print(
                f"{question['id']}: bigmodel quota exhausted (1310) in judge - "
                "aborting for resume",
                flush=True,
            )
            svc.stop()
            return 2
        row = {
            "id": question["id"],
            "domain": question["domain"],
            "question_type": question["question_type"],
            "latency_s": round(latency, 3),
            "correct": correct,
            "answer": answer[:200],
        }
        results.append(row)
        with open(records_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(
            f"{qi + 1}/{len(questions)} {question['id']} type={question['question_type']} "
            f"correct={correct} latency={latency:.2f}s",
            flush=True,
        )
    svc.stop()

    # Aggregate over ALL records (resume included), not just this session.
    all_rows = [
        json.loads(line)
        for line in records_path.read_text().splitlines()
        if line.strip()
    ]
    acc = sum(bool(r["correct"]) for r in all_rows) / max(len(all_rows), 1)
    lat = sorted(r["latency_s"] for r in all_rows)
    p50 = lat[len(lat) // 2] if lat else 0
    p95 = lat[int(len(lat) * 0.95)] if lat else 0
    summary = {
        "benchmark": f"longmemeval_v2_{args.tier}",
        "domain": args.domain,
        "n": len(all_rows),
        "accuracy": round(acc, 4),
        "latency_p50_s": round(p50, 3),
        "latency_p95_s": round(p95, 3),
        "wall_s": round(time.time() - t_start, 1),
    }
    print(json.dumps(summary, indent=1))
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
