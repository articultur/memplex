#!/usr/bin/env python3
"""C2 write-time enrichment probe: contextual prefix (Anthropic-style).

Paired retrieval probe on 20 LongMemEval-S questions (seed 17):
  - base arm : session-unit seeding, the v4+ granularity
  - ctx arm  : each session unit gets a ~50-100 token LLM-generated
               situating prefix prepended BEFORE embedding/indexing
Metric: session-level gold-evidence recall@8 (probe discipline: pure
retrieval face, no answerer - the enrichment can only move recall).

Expectations calibrated to the independent ECIR replication (+0.005-
0.012 nDCG): a mild positive; the vendor's -49% failure-rate claim is
explicitly NOT the bar here.
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import random
import sys
import tempfile
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

DATA = _PROJECT_ROOT / ".memplex/benchmarks/data/longmemeval_s_cleaned.json"
PREFIX_MODEL = "glm-5.3-flash"

_PREFIX_PROMPT = (
    "You will be given one session from a user's months-long conversation "
    "history. Give a short succinct context (about 50-100 tokens) situating "
    "this session within the user's history for the purposes of improving "
    "search retrieval: what the session is about, who is involved, and any "
    "durable facts, plans or preferences the user states. Answer only with "
    "the succinct context and nothing else.\n\nSession:\n{session}"
)


class Proxy:
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

    def complete(self, prompt: str) -> str:
        for attempt in range(5):
            try:
                resp = self._client.post(
                    "/v1/messages",
                    json={
                        "model": PREFIX_MODEL,
                        "max_tokens": 400,
                        "temperature": 0.0,
                        # glm-5.3-flash is a thinking model; the prefix is a
                        # short summarization task, so thinking would only
                        # burn the token budget (observed as HTTP 400).
                        "thinking": {"type": "disabled"},
                        "messages": [{"role": "user", "content": prompt}],
                    },
                )
                resp.raise_for_status()
                return "".join(
                    b.get("text", "")
                    for b in resp.json().get("content", [])
                    if b.get("type") == "text"
                ).strip()
            except httpx.HTTPStatusError as exc:
                body = exc.response.text[:300]
                # Content-safety rejections (bigmodel code 1301) are
                # deterministic for a given session text: retrying cannot
                # help. Degrade to no prefix - the ctx arm then seeds that
                # session verbatim, same as the base arm, so the pairing
                # stays clean.
                if exc.response.status_code == 400 and "1301" in body:
                    return ""
                print(f"prefix call {exc.response.status_code}: {body}", flush=True)
                if attempt == 4:
                    raise
                time.sleep(min(60, 5 * 2**attempt))
            except Exception:
                if attempt == 4:
                    raise
                time.sleep(min(60, 5 * 2**attempt))
        return ""


def session_fulltext(turns: list[dict]) -> str:
    parts = []
    for turn in turns:
        role = turn.get("role", "user")
        content = turn.get("content", "")
        parts.append(f"{role}: {content}")
    return "\n".join(parts)


def generate_prefixes(questions: list[dict], proxy: Proxy, cache_path: pathlib.Path) -> dict[str, str]:
    cache: dict[str, str] = {}
    if cache_path.exists():
        for line in cache_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                cache[row["sid"]] = row["prefix"]
    todos: list[tuple[str, str]] = []
    for q in questions:
        for sid, turns in zip(q["haystack_session_ids"], q["haystack_sessions"]):
            if sid not in cache:
                todos.append((sid, session_fulltext(turns)))
    print(f"prefixes: {len(cache)} cached, {len(todos)} to generate", flush=True)

    def one(item: tuple[str, str]) -> tuple[str, str]:
        sid, text = item
        # Cap the session text: the prefix situates the unit, full text
        # stays in the seeded document either way.
        prefix = proxy.complete(_PREFIX_PROMPT.format(session=text[:6000]))
        return sid, prefix

    with ThreadPoolExecutor(max_workers=4) as ex, open(cache_path, "a", encoding="utf-8") as fh:
        for i, (sid, prefix) in enumerate(ex.map(one, todos)):
                cache[sid] = prefix
                fh.write(json.dumps({"sid": sid, "prefix": prefix}) + "\n")
                fh.flush()
                if (i + 1) % 50 == 0:
                    print(f"  generated {i + 1}/{len(todos)}", flush=True)
    return cache


def run_arm(questions: list[dict], arm: str, prefixes: dict[str, str]) -> list[dict]:
    rows = []
    for qi, q in enumerate(questions):
        store_dir = pathlib.Path(tempfile.mkdtemp(prefix=f"c2-{arm}{qi}-"))
        config = load_config()
        config.storage.backend = "lite"
        config.storage.path = str(store_dir / "s.sqlite3")
        config.llm.query_enhancement = False
        svc = MemplexService(config=config)
        svc.start()
        try:
            with svc.store.deferred_commit():
                for sid, date, turns in zip(
                    q["haystack_session_ids"], q["haystack_dates"], q["haystack_sessions"]
                ):
                    text = session_fulltext(turns)
                    if arm == "ctx" and prefixes.get(sid):
                        text = f"{prefixes[sid]}\n{text}"
                    try:
                        svc.write_text(f"[{sid} @ {date}] {text}", source_type="text")
                    except ValueError:
                        # Content-hash id collision: another session already
                        # contributed the same extracted node - the memory
                        # is in the store, which is all retrieval needs.
                        continue
            result = svc.query(q["question"], top_k=8, orchestrated=False, explain=False)
            recall = "\n".join(r.summary for r in result.results[:8])
            gold_ids = q["answer_session_ids"]
            hits = sum(1 for gid in gold_ids if f"[{gid} @" in recall)
            hit = hits > 0
            rows.append(
                {
                    "qid": q["question_id"],
                    "qtype": q["question_type"],
                    "gold_ids": gold_ids,
                    "hit": hit,
                    "hits": hits,
                }
            )
            print(f"  {arm} {qi + 1}/{len(questions)} {q['question_id']} hit={hit}", flush=True)
        finally:
            svc.stop()
    return rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--n", type=int, default=20)
    parser.add_argument("--seed", type=int, default=17)
    parser.add_argument("--prefix-cache", default="/tmp/c2-prefix-cache.jsonl")
    parser.add_argument("--out", default="benchmarks/results/c2-contextual-probe")
    args = parser.parse_args()

    with open(DATA) as fh:
        data = json.load(fh)
    # Seeded sampler for reproducible probe selection - not a
    # cryptographic randomness source.
    rng = random.Random(args.seed)
    questions = rng.sample(data, args.n)
    print(f"probing {args.n} questions (seed {args.seed})", flush=True)

    proxy = Proxy()
    prefixes = generate_prefixes(questions, proxy, pathlib.Path(args.prefix_cache))

    base_rows = run_arm(questions, "base", prefixes)
    ctx_rows = run_arm(questions, "ctx", prefixes)

    paired = {
        r["qid"]: {"base": r["hit"], "ctx": c["hit"], "qtype": r["qtype"]}
        for r, c in zip(base_rows, ctx_rows)
    }
    flips_up = sum(1 for v in paired.values() if not v["base"] and v["ctx"])
    flips_down = sum(1 for v in paired.values() if v["base"] and not v["ctx"])

    def acc(rows: list[dict]) -> float:
        return sum(r["hit"] for r in rows) / max(len(rows), 1)

    summary = {
        "benchmark": "contextual_prefix_probe",
        "design": "paired 20-question retrieval probe; ctx arm prepends a ~50-100 token glm-5.3-flash situating prefix to each seeded session unit before embedding",
        "n": args.n,
        "base_recall8": round(acc(base_rows), 4),
        "ctx_recall8": round(acc(ctx_rows), 4),
        "delta": round(acc(ctx_rows) - acc(base_rows), 4),
        "flips_up": flips_up,
        "flips_down": flips_down,
    }
    print(json.dumps(summary, indent=1), flush=True)

    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "base.json").write_text(json.dumps(base_rows, indent=1))
    (out / "ctx.json").write_text(json.dumps(ctx_rows, indent=1))
    (out / "paired.json").write_text(json.dumps(paired, indent=1))
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
