#!/usr/bin/env python3
"""LongMemEval-V2 web MEDIUM tier: screenshot-injection paired A/B.

Adapts the archived web-small multimodal slice (scripts/run_lme_v2_mm.py)
to the medium tier, keeping the medium runner's (scripts/run_lme_v2.py)
union-store protocol, quota circuit breaker, records.jsonl resume, and
--store-dir persistence:

  - arm "text": retrieval context only, answerer = glm-5.3-flash
  - arm "mm":   SAME context + top retrieval hits' state screenshots
                injected as base64 image blocks (anchor resolution on
                verbatim "[traj @ url]" fragments), same answerer model
  - both arms share one retrieval configuration and one context, so the
    paired delta is attributable to the screenshots alone; judge stays
    glm-5.3 (archived firstpoint protocol).

UNION LOWER BOUND (medium-tier disclosure, same wording as
docs/evidence/lme2-v2c-medium-full/manifest.json): the official medium
tier gives each question a DISTINCT 387-500-trajectory haystack and the
official harness builds fresh per-question memory. This runner seeds the
UNION of the sliced question set's haystacks ONCE into a persistent
store and answers every question against it - a superset of any single
question's haystack, so retrieval faces more distractors than the
official per-question setting and every accuracy number is a LOWER
BOUND for the protocol. The union scope is pinned by a fingerprint in
<store-dir>/seed_union.txt; if the scope changes (different --limit or
question set) the store is reseeded rather than answering against a
stale corpus.

The answerer glm-5.3-flash runs with thinking explicitly disabled
(short-output task); bigmodel 1310 (weekly/monthly quota) aborts with
exit code 2 - resumable, rerun the same command - instead of silently
scoring a wall of empty answers wrong; bigmodel 1301 content-safety
rejections degrade to an empty answer for that arm only (deterministic
per text, scored wrong, which is the honest cost).

Usage:
    .venv/bin/python scripts/run_lme_v2_mm_medium.py [--limit N] \
        [--max-images 6] [--concurrency 4] [--out DIR] \
        [--store-dir DIR] [--orchestrated] [--self-check]

Resume: appends one JSON line per question (both arms in one record, so
pairing is atomic) to <out>/records.jsonl and skips ids already
present; the persistent --store-dir keeps the seeded corpus so seeding
never repeats.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
import pathlib
import re
import struct
import sys
import tempfile
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed

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
TIER = "medium"
TOP_K = 12
ANSWERER_MODEL = "glm-5.3-flash"
JUDGE_MODEL = "glm-5.3"


# ── API proxy (Anthropic-compatible wire, credentials from user settings) ──


class QuotaExhaustedError(RuntimeError):
    """bigmodel 1310: the weekly/monthly cap is hit - stop, never retry."""


class GlmProxy:
    def __init__(self) -> None:
        settings = json.loads(
            pathlib.Path(os.path.expanduser("~/.claude/settings.json")).read_text()
        )["env"]
        # trust_env=False: a dead local system proxy (QuickQ-style env
        # vars) otherwise hijacks httpx into connection-refused even
        # though the endpoint is directly reachable.
        self._client = httpx.Client(
            base_url=settings["ANTHROPIC_BASE_URL"],
            timeout=180,
            trust_env=False,
            headers={
                "x-api-key": settings["ANTHROPIC_AUTH_TOKEN"],
                "anthropic-version": "2023-06-01",
            },
        )

    def _post(
        self, model: str, content, max_tokens: int, disable_thinking: bool
    ) -> str:
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": 0.0,
            "messages": [{"role": "user", "content": content}],
        }
        if disable_thinking:
            # glm-5.3-flash is a thinking model; answer extraction is a
            # short-output task, so thinking would only burn the token
            # budget before the text block.
            payload["thinking"] = {"type": "disabled"}
        for attempt in range(5):
            try:
                resp = self._client.post("/v1/messages", json=payload)
                if resp.status_code == 429 and "1310" in resp.text:
                    # Monthly/weekly cap: retrying cannot succeed, and an
                    # empty answer would be silently scored wrong.
                    raise QuotaExhaustedError(resp.text[:200])
                if resp.status_code == 400 and "1301" in resp.text[:300]:
                    # Content-safety rejection: deterministic per text;
                    # degrade to an empty answer instead of killing the run.
                    return ""
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

    def complete(
        self, prompt: str, *, model: str, max_tokens: int, disable_thinking: bool = False
    ) -> str:
        return self._post(model, prompt, max_tokens, disable_thinking)

    def complete_vision(
        self,
        prompt: str,
        images: list[tuple[str, str]],
        *,
        model: str,
        max_tokens: int,
        disable_thinking: bool = False,
    ) -> str:
        content: list[dict] = [{"type": "text", "text": prompt}]
        for media, data_b64 in images:
            content.append(
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": media, "data": data_b64},
                }
            )
        return self._post(model, content, max_tokens, disable_thinking)


# ── Scoring: verbatim firstpoint protocol (run_lme_v2.py / run_lme_v2_mm.py) ──


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
    except QuotaExhaustedError:
        raise
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
        a11y_cap = int(os.environ.get("LME2_A11Y_CAP", "6000"))
        a11y = (state.get("accessibility_tree") or "")[:a11y_cap]
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


def load_image_b64(screenshot_field: str, *, root: pathlib.Path = WEB_SCREENS):
    """web_screens mirrors screenshots/<traj>/<step>.png; downscale to
    JPEG (max edge 1024) to keep the request body small."""
    if not screenshot_field:
        return None
    rel = "/".join(screenshot_field.split("/")[1:])  # drop "screenshots/"
    path = pathlib.Path(root) / rel
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


def context_from_summaries(summaries: list[str]) -> str:
    return "\n".join(f"- {s[:1500]}" for s in summaries[:TOP_K])


def images_for_summaries(
    summaries: list[str],
    trajs: dict,
    max_images: int,
    *,
    root: pathlib.Path = WEB_SCREENS,
) -> list[tuple[str, str]]:
    images: list[tuple[str, str]] = []
    for s in summaries[:TOP_K]:
        for field in screenshot_fields_for_summary(s, trajs):
            packed = load_image_b64(field, root=root)
            if packed is not None:
                images.append(packed)
                if len(images) >= max_images:
                    return images
    return images


# ── Medium union seeding (run_lme_v2.py --tier medium protocol) ────────


def load_web_questions() -> list[dict]:
    with open(DATA_ROOT / "questions.jsonl") as fh:
        rows = [json.loads(line) for line in fh if line.strip()]
    return [r for r in rows if r["domain"] == "web"]


def load_medium_haystack() -> dict[str, list[str]]:
    with open(DATA_ROOT / f"haystacks/lme_v2_{TIER}.json") as fh:
        return json.load(fh)


def union_haystack_ids(haystack: dict[str, list[str]], question_ids: list[str]) -> list[str]:
    """Sorted union of the per-question medium haystacks - the seeded
    superset. Every question's official haystack is a subset of this
    union, so the shared store adds distractors and accuracy is a lower
    bound for the per-question protocol (see module docstring)."""
    union: set[str] = set()
    for qid in question_ids:
        union.update(haystack[qid])  # missing ids fail loudly, not silently
    return sorted(union)


def load_trajectories(wanted: set[str]) -> dict[str, dict]:
    """Stream the 1.1GB trajectories file once, keeping union ids only."""
    trajs: dict[str, dict] = {}
    with open(DATA_ROOT / "trajectories.jsonl") as fh:
        for line in fh:
            if not line.strip():
                continue
            t = json.loads(line)
            if t["id"] in wanted:
                trajs[t["id"]] = t
    return trajs


def _union_fingerprint(union_ids: list[str]) -> str:
    payload = json.dumps(
        {
            "tier": TIER,
            "a11y_cap": os.environ.get("LME2_A11Y_CAP", "6000"),
            "ids": union_ids,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def _seed_union_store(svc, trajs: dict, union_ids: list[str], store_root: pathlib.Path):
    """Seed the union haystack once; the marker pins the union scope so a
    scope change reseeds instead of answering against a stale corpus."""
    marker = store_root / "seed_union.txt"
    fp = _union_fingerprint(union_ids)
    has_corpus = bool(
        getattr(svc.store, "_functions", None) or getattr(svc.store, "_facts", None)
    )
    if marker.exists() and marker.read_text().strip() == fp and has_corpus:
        print(
            f"reusing seeded union store: {len(union_ids)} trajectories "
            f"(fp={fp[:12]})",
            flush=True,
        )
        return
    from benchmarks.longmemeval import _clear_store

    _clear_store(svc)
    needed = [trajs[tid] for tid in union_ids if tid in trajs]
    with svc.store.deferred_commit():
        for traj in needed:
            for text in trajectory_to_texts(traj):
                svc.write_text(text, source_type="text")
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text(fp + "\n")
    print(
        f"seeded union haystack: {len(needed)}/{len(union_ids)} trajectories "
        f"(superset of any per-question subset; lower-bound protocol)",
        flush=True,
    )


# ── Paired A/B per question ────────────────────────────────────────────


_ANSWER_PROMPT = (
    "Answer using ONLY the memory excerpts below{extra}. If they do not "
    "contain the answer, say what is missing concisely.\n\nExcerpts:\n"
    "{context}\n\nQuestion: {question}\n\nAnswer concisely:"
)


def run_arm(prompt: str, images: list[tuple[str, str]], proxy: GlmProxy) -> str:
    try:
        if images:
            return proxy.complete_vision(
                prompt,
                images,
                model=ANSWERER_MODEL,
                max_tokens=1024,
                disable_thinking=True,
            ).strip()
        return proxy.complete(
            prompt,
            model=ANSWERER_MODEL,
            max_tokens=1024,
            disable_thinking=True,
        ).strip()
    except QuotaExhaustedError:
        raise
    except Exception as exc:  # noqa: BLE001 - degrade one arm, keep pairing
        print(f"generation failed: {exc}", flush=True)
        return ""


def process_question(
    question: dict,
    svc,
    trajs: dict,
    *,
    proxy: GlmProxy,
    max_images: int,
    orchestrated: bool,
    retrieve_lock: threading.Lock,
    abort: threading.Event,
) -> dict | None:
    if abort.is_set():
        return None
    # svc.query is not share-thread safe: retrieval is serialized (same
    # constraint as the archived mm runner), generation runs in parallel.
    with retrieve_lock:
        t0 = time.time()
        retrieved = svc.query(
            question["question"], top_k=TOP_K, orchestrated=orchestrated, explain=False
        )
        latency = time.time() - t0
    summaries = [r.summary for r in retrieved.results[:TOP_K]]
    context = context_from_summaries(summaries)
    images = images_for_summaries(summaries, trajs, max_images)
    record = {
        "id": question["id"],
        "question_type": question["question_type"],
        "n_images": len(images),
        "latency_s": round(latency, 3),
    }
    for arm in ("text", "mm"):
        extra = " and screenshots" if arm == "mm" else ""
        prompt = _ANSWER_PROMPT.format(
            extra=extra, context=context[:20000], question=question["question"]
        )
        answer = run_arm(prompt, images if arm == "mm" else [], proxy)
        correct = score_answer(question, answer) if answer else False
        record[arm] = {"correct": correct, "answer": answer[:200]}
    return record


def summarize(rows: list[dict], *, orchestrated: bool, wall_s: float) -> tuple[dict, dict]:
    def arm_acc(arm: str) -> dict:
        flags = [bool(r[arm]["correct"]) for r in rows]
        return {"n": len(flags), "accuracy": round(sum(flags) / max(len(flags), 1), 4)}

    def by_type(arm: str) -> dict:
        buckets: dict[str, list[bool]] = {}
        for r in rows:
            buckets.setdefault(r["question_type"], []).append(bool(r[arm]["correct"]))
        return {
            k: round(sum(v) / len(v), 4)
            for k, v in sorted(buckets.items())
        }

    paired = {
        r["id"]: {
            "question_type": r["question_type"],
            "n_images": r["n_images"],
            "text": r["text"]["correct"],
            "mm": r["mm"]["correct"],
        }
        for r in rows
    }
    lat = sorted(r["latency_s"] for r in rows)
    summary = {
        "benchmark": f"longmemeval_v2_web_{TIER}_multimodal_full",
        "design": (
            "paired A/B screenshot injection; union-store superset haystack "
            "(each question's official 387-500-traj subset is contained in "
            "the seeded union, so accuracy is a LOWER BOUND; cf. manifest "
            "lme2-v2c-medium-full)"
        ),
        "answerer": ANSWERER_MODEL,
        "judge": JUDGE_MODEL,
        "retrieval": "orchestrated" if orchestrated else "plain (shared by both arms)",
        "n": len(rows),
        "text_arm": arm_acc("text"),
        "mm_arm": arm_acc("mm"),
        "flips_up": sum(1 for r in rows if not r["text"]["correct"] and r["mm"]["correct"]),
        "flips_down": sum(1 for r in rows if r["text"]["correct"] and not r["mm"]["correct"]),
        "by_type_text": by_type("text"),
        "by_type_mm": by_type("mm"),
        "mean_images": round(sum(r["n_images"] for r in rows) / max(len(rows), 1), 2),
        "latency_p50_s": lat[len(lat) // 2] if lat else 0,
        "latency_p95_s": lat[int(len(lat) * 0.95)] if lat else 0,
        "wall_s": round(wall_s, 1),
    }
    return summary, paired


# ── Self-check: pure-function path, 3 synthetic mini trajectories ──────


def _png_1x1() -> bytes:
    """Minimal valid 1x1 RGB PNG built with stdlib only (green pixel)."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)  # 1x1, 8-bit, RGB
    idat = zlib.compress(b"\x00\xff\x00\x00")  # filter byte + one RGB pixel
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(
        b"IEND", b""
    )


class _StubJudge:
    """Stand-in judge for the llm_* scoring branch: no external API."""

    def complete(self, prompt: str, *, model: str, max_tokens: int) -> str:
        return "YES"


def _self_check() -> int:
    """Walk 播种→检索→锚点解析→注入内容组装→评分函数 as pure functions:
    in-memory synthetic trajectories, 1x1 PNG placeholders, stub judge.
    No big-data reads, no MemplexService, no external API calls."""
    print("== self-check: 3 synthetic mini trajectories, pure-function path ==")
    trajs = {
        "aa11bb22": {
            "id": "aa11bb22",
            "environment": "webarena-reddit",
            "goal": "post news",
            "outcome": "done",
            "states": [
                {
                    "url": "https://forum.example/home",
                    "action": "click new post",
                    "thought": "open editor",
                    "accessibility_tree": "textbox title; textbox body",
                    "state_index": 0,
                    "screenshot": "screenshots/aa11bb22/0.png",
                },
                {
                    "url": "https://forum.example/home",
                    "action": "type title",
                    "thought": "fill title",
                    "accessibility_tree": "textbox title=hello",
                    "state_index": 1,
                    "screenshot": "screenshots/aa11bb22/1.png",
                },
            ],
        },
        "cc33dd44": {
            "id": "cc33dd44",
            "environment": "webarena-cms",
            "goal": "edit page",
            "outcome": "done",
            "states": [
                {
                    "url": "https://cms.example/edit",
                    "action": "click save",
                    "thought": "persist",
                    "accessibility_tree": "button save",
                    "state_index": 0,
                    "screenshot": "screenshots/cc33dd44/0.png",
                }
            ],
        },
        "ee55ff66": {
            "id": "ee55ff66",
            "environment": "webarena-onestopshop",
            "goal": "buy item",
            "outcome": "done",
            "states": [
                {
                    "url": "https://shop.example/cart",
                    "action": "click checkout",
                    "thought": "pay",
                    "accessibility_tree": "button checkout",
                    "state_index": 0,
                    "screenshot": "screenshots/ee55ff66/0.png",
                }
            ],
        },
    }
    haystack = {
        "q1": ["cc33dd44", "aa11bb22"],
        "q2": ["ee55ff66", "cc33dd44"],
        "q3": ["aa11bb22", "ee55ff66"],
    }

    # 1) 播种: union scope over the sliced question set + text projection.
    union_ids = union_haystack_ids(haystack, list(haystack))
    assert union_ids == ["aa11bb22", "cc33dd44", "ee55ff66"], union_ids
    seeded = {tid: trajectory_to_texts(trajs[tid]) for tid in union_ids}
    n_docs = sum(len(v) for v in seeded.values())
    assert n_docs == 3 + 4, n_docs  # one header per traj + 4 states
    print(f"[seed] union={union_ids}")
    print(f"[seed] docs={n_docs}; sample: {seeded['aa11bb22'][1][:110]}")

    # 2) 检索: context assembly from result summaries (anchors preserved
    #    by the extractor rewrite, per the mm runner's contract).
    summaries = seeded["aa11bb22"][1:]
    context = context_from_summaries(summaries)
    assert "[aa11bb22 @ https://forum.example/home]" in context
    print(f"[retrieve] context head: {context[:150]}")

    # 3) 锚点解析: (traj, url) anchor -> first+last state shots.
    fields = images_for_summaries(summaries, trajs, max_images=6, root=pathlib.Path("/nonexistent"))
    assert fields == [], fields  # no placeholder files yet: anchors resolve nothing
    per_summary = screenshot_fields_for_summary(summaries[0], trajs)
    assert per_summary == ["screenshots/aa11bb22/0.png", "screenshots/aa11bb22/1.png"], (
        per_summary
    )
    print(f"[anchor] {per_summary}")

    # 4) 注入内容组装: 1x1 PNG placeholders -> base64 image blocks.
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        for field in per_summary:
            rel = "/".join(field.split("/")[1:])
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(_png_1x1())
        images = images_for_summaries(summaries, trajs, max_images=6, root=root)
        # 2 summaries x (first+last state shots): cross-summary dedupe is
        # deliberately NOT done - same behavior as the archived mm
        # runner's _images_for_results (per-summary dedupe only).
        assert len(images) == 4, [m for m, _ in images]
        assert all(media in {"image/jpeg", "image/png"} for media, _ in images)
        assert all(len(b64) > 0 for _, b64 in images)
        print(f"[inject] {len(images)} blocks: " + ", ".join(f"{m}:{len(b)}B" for m, b in images))

    # 5) 评分函数: all evaluator families, stub judge for llm_* (no API).
    global _PROXY
    _PROXY = _StubJudge()
    cases = [
        (
            {
                "id": "sc-mc-neg",
                "question": "q",
                "eval_function": "mc_choice_match|require_non_empty=true",
                "answer": "false",
            },
            "It is true.",
            False,
        ),
        (
            {
                "id": "sc-mc-pos",
                "question": "q",
                "eval_function": "mc_choice_match|require_non_empty=true",
                "answer": "false",
            },
            "The statement is false.",
            True,
        ),
        (
            {
                "id": "sc-phrase",
                "question": "q",
                "eval_function": "norm_phrase_set_match",
                "answer": "post news; vote button",
            },
            "You can post news and there is a vote button.",
            True,
        ),
        (
            {
                "id": "sc-llm",
                "question": "q",
                "eval_function": "llm_abstention_checker",
                "answer": "reference",
            },
            "model answer",
            True,
        ),
    ]
    for q, ans, want in cases:
        got = score_answer(q, ans)
        spec = q["eval_function"].split("|")[0]
        print(f"[score] {q['id']} spec={spec} -> {got} (want {want})")
        assert got == want, (q["id"], got, want)

    # Union superset property (the disclosure core): every per-question
    # haystack is contained in the seeded union.
    for ids in haystack.values():
        assert set(ids) <= set(union_ids)
    print("[union] every per-question haystack ⊆ seeded union (lower-bound protocol)")
    print("SELF-CHECK OK")
    return 0


# ── Main ────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser(
        description="web medium tier paired A/B: text vs screenshot injection"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="slice to the first N web questions (default: full 240)",
    )
    parser.add_argument("--max-images", type=int, default=6)
    parser.add_argument(
        "--concurrency", type=int, default=4, help="generation parallelism"
    )
    parser.add_argument("--out", default="benchmarks/results/lme2-v2c-web-mm-medium")
    parser.add_argument(
        "--store-dir",
        default="/tmp/lme2-mm-medium-store",
        help="persistent store dir; reused across restarts so union seeding runs once",
    )
    parser.add_argument(
        "--orchestrated",
        action="store_true",
        help="orchestrated retrieval (default plain: orchestrated deadlocks on a "
        "large reused store, observed reproducibly; both arms share one "
        "retrieval configuration either way)",
    )
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="pure-function path check (3 synthetic mini trajectories, 1x1 PNG "
        "placeholders, stub judge); no big data, no external API, then exit",
    )
    args = parser.parse_args()
    if args.self_check:
        return _self_check()

    questions = load_web_questions()
    if args.limit:
        questions = questions[: args.limit]
    print(f"web {TIER} questions in scope: {len(questions)}", flush=True)
    haystack = load_medium_haystack()
    union_ids = union_haystack_ids(haystack, [q["id"] for q in questions])
    print(
        f"union haystack: {len(union_ids)} trajectories "
        "(superset of any per-question subset; lower-bound protocol)",
        flush=True,
    )

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    records_path = out / "records.jsonl"
    done: set[str] = set()
    if records_path.exists():
        for line in records_path.read_text().splitlines():
            if line.strip():
                done.add(json.loads(line)["id"])
        print(f"resume: {len(done)} questions already recorded", flush=True)
    todo = [q for q in questions if q["id"] not in done]

    global _PROXY
    proxy = GlmProxy()
    _PROXY = proxy

    t_start = time.time()
    quota_hit = False
    if todo:
        trajs = load_trajectories(set(union_ids))
        print(f"loaded {len(trajs)} union trajectories", flush=True)
        config = load_config()
        config.storage.backend = "lite"
        store_root = pathlib.Path(args.store_dir)
        store_root.mkdir(parents=True, exist_ok=True)
        config.storage.path = str(store_root / "s.sqlite3")
        config.llm.query_enhancement = False
        svc = MemplexService(config=config)
        svc.start()
        try:
            _seed_union_store(svc, trajs, union_ids, store_root)
            retrieve_lock = threading.Lock()
            abort = threading.Event()
            ex = ThreadPoolExecutor(max_workers=max(1, args.concurrency))
            futures = {
                ex.submit(
                    process_question,
                    q,
                    svc,
                    trajs,
                    proxy=proxy,
                    max_images=args.max_images,
                    orchestrated=args.orchestrated,
                    retrieve_lock=retrieve_lock,
                    abort=abort,
                ): q["id"]
                for q in todo
            }
            n_done = 0
            try:
                for fut in as_completed(futures):
                    qid = futures[fut]
                    try:
                        rec = fut.result()
                    except QuotaExhaustedError:
                        quota_hit = True
                        break
                    except Exception as exc:  # noqa: BLE001 - one bad question stays resumable
                        print(f"{qid}: task failed: {exc}", flush=True)
                        continue
                    if rec is None:
                        continue
                    n_done += 1
                    with open(records_path, "a", encoding="utf-8") as fh:
                        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                    print(
                        f"{n_done}/{len(todo)} {qid} "
                        f"text={rec['text']['correct']} mm={rec['mm']['correct']} "
                        f"imgs={rec['n_images']} lat={rec['latency_s']}s",
                        flush=True,
                    )
            finally:
                if quota_hit:
                    abort.set()
                    for f in futures:
                        f.cancel()
                ex.shutdown(wait=True, cancel_futures=True)
        finally:
            svc.stop()
    else:
        print("nothing to do: all in-scope questions already recorded", flush=True)

    if quota_hit:
        print(
            "bigmodel quota exhausted (1310) - aborted for resume (exit 2); "
            "rerun the same command to continue",
            flush=True,
        )
        return 2

    rows = [
        json.loads(line)
        for line in records_path.read_text().splitlines()
        if line.strip()
    ]
    summary, paired = summarize(
        rows, orchestrated=args.orchestrated, wall_s=time.time() - t_start
    )
    print(json.dumps(summary, indent=1), flush=True)
    (out / "records.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1))
    (out / "paired.json").write_text(json.dumps(paired, indent=1))
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
