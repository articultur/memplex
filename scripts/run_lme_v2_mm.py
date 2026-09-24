#!/usr/bin/env python3
"""LongMemEval-V2 multimodal slice: screenshot injection A/B on web.

Paired two-arm experiment on the web small tier (100-trajectory shared
haystack, same text projection as the archived text-only firstpoint):
  - arm "text": identical pipeline, answerer = glm-5.3-flash, text only
  - arm "mm":   same pipeline + top retrieval hits' state screenshots
                injected as base64 image blocks, same answerer model
The judge stays glm-5.3 (archived firstpoint protocol). Stratified
sampling separates vision-needed (answer not recoverable from haystack
text) from text-recoverable questions, so the mm arm's delta is
attributable and the control stratum shows any regression.

Usage:
    .venv/bin/python scripts/run_lme_v2_mm.py [--probe] [--n-vn 40] \
        [--n-tr 40] [--max-images 6] [--concurrency 4]
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import io
import json
import os
import pathlib
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor

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

DATA_ROOT = _PROJECT_ROOT / ".memplex/benchmarks/data/longmemeval-v2"
WEB_SCREENS = DATA_ROOT / "web_screens"
RECOVERABILITY = pathlib.Path("/tmp/v2_web_recoverability.json")
TOP_K = 12
ANSWERER_MODEL = "glm-5.3-flash"
JUDGE_MODEL = "glm-5.3"


# ── API proxy (Anthropic-compatible wire, credentials from user settings) ──


class GlmProxy:
    def __init__(self) -> None:
        settings = json.loads(
            pathlib.Path(os.path.expanduser("~/.claude/settings.json")).read_text()
        )["env"]
        self._client = httpx.Client(
            base_url=settings["ANTHROPIC_BASE_URL"],
            timeout=180,
            headers={
                "x-api-key": settings["ANTHROPIC_AUTH_TOKEN"],
                "anthropic-version": "2023-06-01",
            },
        )

    def _post(self, model: str, content, max_tokens: int) -> str:
        for attempt in range(5):
            try:
                resp = self._client.post(
                    "/v1/messages",
                    json={
                        "model": model,
                        "max_tokens": max_tokens,
                        "temperature": 0.0,
                        "messages": [{"role": "user", "content": content}],
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

    def complete(self, prompt: str, *, model: str, max_tokens: int) -> str:
        return self._post(model, prompt, max_tokens)

    def complete_vision(
        self, prompt: str, images: list[tuple[str, str]], *, model: str, max_tokens: int
    ) -> str:
        content: list[dict] = [{"type": "text", "text": prompt}]
        for media, data_b64 in images:
            content.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media, "data": data_b64},
                }
            )
        return self._post(model, content, max_tokens)


# ── Scoring: verbatim firstpoint protocol (run_lme_v2.py) ──────────────

_JUDGE_PROMPT = (
    "You are grading a question about a deployed software environment. "
    "Given the question, the reference answer, and the model's answer, "
    "reply with exactly YES or NO: does the model's answer match the "
    "reference's substance (equivalent statements count; the model may "
    "add detail but must not contradict or miss the key point)?\n\n"
    "Question: {question}\n\nReference: {gold}\n\nModel answer: "
    "{answer}\n\nReply YES or NO only:"
)

_PROXY: GlmProxy | None = None


def llm_judge(question: dict, gold: str, answer: str) -> bool:
    try:
        verdict = _PROXY.complete(
            _JUDGE_PROMPT.format(
                question=question["question"][:600],
                gold=gold[:400],
                answer=answer[:600],
            ),
            model=JUDGE_MODEL,
            max_tokens=256,
        )
    except Exception as exc:  # noqa: BLE001 - judge failure scores wrong, never crashes
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


# ── Text projection + screenshot mapping (firstpoint-compatible) ───────


def trajectory_to_texts(traj: dict) -> list[str]:
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


# Retrieval summaries are extractor-rewritten node digests, not the
# seeded text; they do preserve verbatim "[traj @ url]" fragments, so
# screenshot resolution anchors on (traj_id, url) pairs instead of exact
# state prefixes. Per anchor we take the first and last state's shot
# (the visual answer usually lives at a page-level state).
_ANCHOR = re.compile(r"\[([0-9a-f]{6,12}) @ ([^\s\]]+)\]")


def screenshot_fields_for_summary(summary: str, trajs: dict) -> list[str]:
    picks: list[str] = []
    seen: set[tuple[str, int]] = set()
    for tid, url in _ANCHOR.findall(summary[:2000]):
        traj = trajs.get(tid)
        if traj is None:
            continue
        states = [s for s in traj.get("states", []) if s.get("url") == url]
        chosen = states[:1] + states[-1:] if len(states) > 1 else states
        for state in chosen:
            key = (tid, int(state.get("state_index", -1)))
            shot = state.get("screenshot") or ""
            if not shot or key in seen:
                continue
            seen.add(key)
            picks.append(shot)
    return picks


def load_image_b64(screenshot_field: str) -> tuple[str, str] | None:
    """web_screens mirrors screenshots/<traj>/<step>.png; downscale to
    JPEG (max edge 1024) to keep the request body small."""
    if not screenshot_field:
        return None
    rel = "/".join(screenshot_field.split("/")[1:])  # drop "screenshots/"
    path = WEB_SCREENS / rel
    if not path.exists():
        return None
    raw = path.read_bytes()
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(raw)).convert("RGB")
        img.thumbnail((1024, 1024))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=80)
        return ("image/jpeg", base64.b64encode(buf.getvalue()).decode())
    except ImportError:
        return ("image/png", base64.b64encode(raw).decode())


# ── Main ────────────────────────────────────────────────────────────────


def _job_from_cache(row: dict) -> dict:
    return {
        "question": None,
        "qid": row["id"],
        "stratum": row["stratum"],
        "context": row["context"],
        "images": [tuple(i) for i in row["images"]],
        "latency_s": row["latency_s"],
    }


def _sample_questions(args) -> tuple[list[tuple[str, str]], dict[str, dict]]:
    questions_all = []
    with open(DATA_ROOT / "questions.jsonl") as fh:
        for line in fh:
            if line.strip():
                questions_all.append(json.loads(line))
    web_questions = {q["id"]: q for q in questions_all if q["domain"] == "web"}
    scan = json.loads(RECOVERABILITY.read_text())
    # Seeded sampler for reproducible stratified sampling only — not a
    # cryptographic randomness source.
    rng = random.Random(17)
    vn_pool = sorted(scan["vision_needed"])
    tr_pool = sorted(scan["recoverable"])
    n_vn = 10 if args.probe else args.n_vn
    n_tr = 10 if args.probe else args.n_tr
    vn_ids = rng.sample(vn_pool, min(n_vn, len(vn_pool)))
    tr_ids = rng.sample(tr_pool, min(n_tr, len(tr_pool)))
    sampled = [(qid, "vision-needed") for qid in vn_ids] + [
        (qid, "text-recoverable") for qid in tr_ids
    ]
    print(
        f"sampled {len(sampled)} web questions "
        f"(vn={len(vn_ids)}, tr={len(tr_ids)}); probe={args.probe}",
        flush=True,
    )
    return sampled, web_questions


def _ensure_seeded(svc, trajs: dict, traj_ids: list, seed_marker: pathlib.Path) -> None:
    if seed_marker.exists():
        print("reusing seeded store", flush=True)
        return
    from benchmarks.longmemeval import _clear_store

    _clear_store(svc)
    seeded = 0
    with svc.store.deferred_commit():
        for tid in traj_ids:
            traj = trajs.get(tid)
            if traj is None:
                continue
            for text in trajectory_to_texts(traj):
                svc.write_text(text, source_type="text")
            seeded += 1
            if seeded % 10 == 0:
                print(f"seeding {seeded}/{len(traj_ids)}", flush=True)
    seed_marker.parent.mkdir(parents=True, exist_ok=True)
    seed_marker.write_text("ok\n")
    print("seeded haystack (text projection)", flush=True)


def _retrieve_jobs(
    svc,
    sampled: list[tuple[str, str]],
    web_questions: dict,
    trajs: dict,
    cached: dict[str, dict],
    cache_path: str,
    orchestrated: bool,
    max_images: int,
) -> list[dict]:
    """Serial retrieval over the shared service; incremental cache writes
    so a stall costs only the in-flight question."""
    jobs: list[dict] = []
    with contextlib.ExitStack() as stack:
        cache = (
            stack.enter_context(open(cache_path, "a", encoding="utf-8"))
            if cache_path
            else None
        )
        for qi, (qid, stratum) in enumerate(sampled):
            if qid in cached:
                jobs.append(_job_from_cache(cached[qid]))
                continue
            question = web_questions[qid]
            t0 = time.time()
            retrieved = svc.query(
                question["question"], top_k=TOP_K, orchestrated=orchestrated, explain=False
            )
            latency = time.time() - t0
            context = "\n".join(f"- {r.summary[:1500]}" for r in retrieved.results[:TOP_K])
            images = _images_for_results(retrieved.results, trajs, max_images)
            row = {
                "id": qid,
                "stratum": stratum,
                "context": context,
                "images": images,
                "latency_s": round(latency, 3),
            }
            if cache is not None:
                cache.write(json.dumps(row) + "\n")
                cache.flush()
            print(
                f"retrieved {qi + 1}/{len(sampled)} {qid} imgs={len(images)} "
                f"latency={latency:.1f}s",
                flush=True,
            )
            jobs.append({**_job_from_cache(row), "question": question})
    return jobs


def _images_for_results(results, trajs: dict, max_images: int) -> list[tuple[str, str]]:
    images: list[tuple[str, str]] = []
    for r in results[:TOP_K]:
        for field in screenshot_fields_for_summary(r.summary, trajs):
            packed = load_image_b64(field)
            if packed is not None:
                images.append(packed)
                if len(images) >= max_images:
                    return images
    return images


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--probe", action="store_true", help="10+10 quick calibration")
    parser.add_argument("--n-vn", type=int, default=40)
    parser.add_argument("--n-tr", type=int, default=40)
    parser.add_argument("--max-images", type=int, default=6)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--out", default="benchmarks/results/lme2-mm-slice")
    parser.add_argument(
        "--store-dir",
        default="/tmp/lme2-mm-store",
        help="fixed store dir; reused across restarts so seeding runs once",
    )
    parser.add_argument(
        "--retrieval-cache",
        default="",
        help="JSONL cache of retrieval results; present ids are resumed, "
        "so a stall costs only the in-flight item",
    )
    parser.add_argument(
        "--no-orchestrated",
        action="store_true",
        help="plain retrieval; orchestrated deadlocks on a large reused "
        "store (observed reproducibly), and the A/B causal claim only "
        "needs both arms to share one retrieval configuration",
    )
    args = parser.parse_args()

    sampled, web_questions = _sample_questions(args)
    with open(DATA_ROOT / "haystacks/lme_v2_small.json") as fh:
        haystack = json.load(fh)
    any_web_qid = next(iter(web_questions))
    traj_ids = haystack[any_web_qid]
    print(f"shared web haystack: {len(traj_ids)} trajectories", flush=True)

    # Load only web-haystack trajectories (streamed; file is ~1.1GB total).
    trajs: dict[str, dict] = {}
    wanted = set(traj_ids)
    with open(DATA_ROOT / "trajectories.jsonl") as fh:
        for line in fh:
            if not line.strip():
                continue
            t = json.loads(line)
            if t["id"] in wanted:
                trajs[t["id"]] = t
    print(f"loaded {len(trajs)} trajectories", flush=True)

    global _PROXY
    proxy = GlmProxy()
    _PROXY = proxy

    store_dir = pathlib.Path(args.store_dir)
    cached: dict[str, dict] = {}
    if args.retrieval_cache and pathlib.Path(args.retrieval_cache).exists():
        with open(args.retrieval_cache) as fh:
            for line in fh:
                if line.strip():
                    row = json.loads(line)
                    cached[row["id"]] = row
        print(f"retrieval cache: {len(cached)} ids resumed", flush=True)

    if len(cached) >= len(sampled):
        # Generation-only restart: no service, no seeding needed.
        jobs = [_job_from_cache(cached[qid]) for qid, _ in sampled]
        for job, (qid, _) in zip(jobs, sampled):
            job["question"] = web_questions[qid]
    else:
        config = load_config()
        config.storage.backend = "lite"
        config.storage.path = str(store_dir / "s.sqlite3")
        config.llm.query_enhancement = False
        svc = MemplexService(config=config)
        svc.start()
        _ensure_seeded(svc, trajs, traj_ids, store_dir / "seeded.txt")
        jobs = _retrieve_jobs(
            svc,
            sampled,
            web_questions,
            trajs,
            cached,
            args.retrieval_cache,
            orchestrated=not args.no_orchestrated,
            max_images=args.max_images,
        )
        svc.stop()
    print(f"retrieval done for {len(jobs)} questions", flush=True)

    answer_prompt = (
        "Answer using ONLY the memory excerpts below"
        "{extra}. If they do not contain the answer, say what is missing "
        "concisely.\n\nExcerpts:\n{context}\n\nQuestion: {question}\n\n"
        "Answer concisely:"
    )

    def run_arm(job: dict, arm: str) -> dict:
        question = job["question"]
        extra = " and screenshots" if arm == "mm" else ""
        prompt = answer_prompt.format(
            extra=extra, context=job["context"][:20000], question=question["question"]
        )
        try:
            if arm == "mm" and job["images"]:
                answer = proxy.complete_vision(
                    prompt, job["images"], model=ANSWERER_MODEL, max_tokens=1024
                ).strip()
            else:
                answer = proxy.complete(
                    prompt, model=ANSWERER_MODEL, max_tokens=1024
                ).strip()
        except Exception as exc:  # noqa: BLE001 - record failure, keep pairing
            print(f"{question['id']}/{arm}: generation failed {exc}", flush=True)
            answer = ""
        correct = score_answer(question, answer) if answer else False
        return {
            "id": question["id"],
            "stratum": job["stratum"],
            "question_type": question["question_type"],
            "arm": arm,
            "n_images": len(job["images"]),
            "latency_s": job["latency_s"],
            "correct": correct,
            "answer": answer[:200],
        }

    t_start = time.time()
    tasks = [(job, arm) for arm in ("text", "mm") for job in jobs]
    records: list[dict] = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        for rec in ex.map(lambda tj: run_arm(*tj), tasks):
            records.append(rec)
            print(
                f"{len(records)}/{len(tasks)} {rec['id']} {rec['arm']} "
                f"{rec['stratum']} correct={rec['correct']} imgs={rec['n_images']}",
                flush=True,
            )

    def arm_stats(arm: str, stratum: str | None = None) -> dict:
        rows = [
            r
            for r in records
            if r["arm"] == arm and (stratum is None or r["stratum"] == stratum)
        ]
        n = len(rows)
        acc = sum(r["correct"] for r in rows) / max(n, 1)
        return {"n": n, "accuracy": round(acc, 4)}

    paired = {}
    for qid in [j["question"]["id"] for j in jobs]:
        t = next(r for r in records if r["id"] == qid and r["arm"] == "text")
        m = next(r for r in records if r["id"] == qid and r["arm"] == "mm")
        paired[qid] = {"stratum": t["stratum"], "text": t["correct"], "mm": m["correct"]}
    flips_up = sum(1 for v in paired.values() if not v["text"] and v["mm"])
    flips_down = sum(1 for v in paired.values() if v["text"] and not v["mm"])

    summary = {
        "benchmark": "longmemeval_v2_web_small_multimodal_slice",
        "answerer": ANSWERER_MODEL,
        "judge": JUDGE_MODEL,
        "design": "paired A/B, screenshot injection, stratified (vision-needed vs text-recoverable)",
        "probe": args.probe,
        "text_arm": arm_stats("text"),
        "mm_arm": arm_stats("mm"),
        "text_arm_vn": arm_stats("text", "vision-needed"),
        "mm_arm_vn": arm_stats("mm", "vision-needed"),
        "text_arm_tr": arm_stats("text", "text-recoverable"),
        "mm_arm_tr": arm_stats("mm", "text-recoverable"),
        "flips_up": flips_up,
        "flips_down": flips_down,
        "wall_s": round(time.time() - t_start, 1),
    }
    print(json.dumps(summary, indent=1), flush=True)

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "records.json").write_text(json.dumps(records, ensure_ascii=False, indent=1))
    (out / "paired.json").write_text(json.dumps(paired, indent=1))
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
