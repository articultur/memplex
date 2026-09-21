#!/usr/bin/env python3
"""Benchmark-defect audit for official-J failures (Stanford taxonomy).

Classifies every FAILED question into the three flaw categories from
"Fantastic Bugs and Where to Find Them in AI Benchmarks" (ambiguous
question / incorrect answer key / grading issue) versus genuine system
miss, using the official judge inputs (question, gold, hypothesis,
retrieved context). Produces the honest-floor number: how many of the
residual failures no system change can score.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time

import httpx

RUN = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "benchmarks/results/lme-j500-v12")
OUT = pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else "benchmarks/results/lme-v12-defect-audit")
OUT.mkdir(parents=True, exist_ok=True)

PROMPT = """You are auditing a benchmark failure for BENCHMARK DEFECTS (not system quality). Given the question, the gold answer, the system's answer, and the retrieved evidence the system actually saw, classify the PRIMARY issue:

D1 AMBIGUOUS QUESTION - the question admits multiple valid interpretations; other defensible answers exist that the gold key does not accept.
D2 INCORRECT ANSWER KEY - the gold answer is factually wrong or unverifiable from the haystack.
D3 GRADING ISSUE - the system's answer is semantically correct/equivalent but a literal grader (or this judge prompt) marks it wrong (format, granularity, synonym, off-by-inclusive-count).
S  SYSTEM MISS - the question is clear, the gold is right, and the system genuinely failed (wrong fact, missing evidence, bad count of distinct items).

Question: {question}

Gold answer: {gold}

System answer: {hypothesis}

Evidence the system saw (truncated): {context}

Reply with exactly one code (D1/D2/D3/S) then one short sentence.
"""


def main() -> int:
    settings = json.loads(
        pathlib.Path(os.path.expanduser("~/.claude/settings.json")).read_text()
    )["env"]
    client = httpx.Client(
        base_url=settings["ANTHROPIC_BASE_URL"],
        timeout=120,
        headers={
            "x-api-key": settings["ANTHROPIC_AUTH_TOKEN"],
            "anthropic-version": "2023-06-01",
        },
    )
    records = [
        json.loads(line)
        for line in (RUN / "hypotheses.jsonl").read_text().splitlines()
        if line.strip()
    ]
    dedup: dict[str, dict] = {}
    for r in records:
        dedup[r["question_id"]] = r
    failures = [r for r in dedup.values() if r["autoeval_label"] is False]
    print(f"failures to audit: {len(failures)}", flush=True)

    out_path = OUT / "defects.jsonl"
    done: set[str] = set()
    if out_path.exists():
        for line in out_path.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                if row["code"] in {"D1", "D2", "D3", "S"}:
                    done.add(row["question_id"])

    with open(out_path, "a", encoding="utf-8") as out:
        for i, r in enumerate(failures):
            if r["question_id"] in done:
                continue
            prompt = PROMPT.format(
                question=r.get("question", "")[:500],
                gold=r["answer"][:300],
                hypothesis=r["hypothesis"][:600],
                context=r.get("retrieved_context", "")[:8000],
            )
            import re as _re

            text = ""
            for attempt in range(5):
                try:
                    resp = client.post(
                        "/v1/messages",
                        json={
                            "model": "glm-5.3",
                            "max_tokens": 512,
                            "temperature": 0,
                            "thinking": {"type": "disabled"},
                            "messages": [{"role": "user", "content": prompt}],
                        },
                    )
                    resp.raise_for_status()
                    text = "".join(
                        b.get("text", "")
                        for b in resp.json().get("content", [])
                        if b.get("type") == "text"
                    )
                    break
                except Exception:
                    time.sleep(min(60, 5 * 2**attempt))
            match = _re.search(r"\b(D1|D2|D3|S)\b", text)
            code = match.group(1) if match else ""
            out.write(
                json.dumps(
                    {
                        "question_id": r["question_id"],
                        "question_type": r["question_type"],
                        "code": code,
                        "note": text.strip()[:250],
                        "gold": r["answer"][:120],
                        "hypothesis": r["hypothesis"][:200],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            out.flush()
            if (i + 1) % 10 == 0:
                print(f"{i + 1}/{len(failures)}", flush=True)

    from collections import Counter, defaultdict

    rows = [json.loads(l) for l in out_path.read_text().splitlines() if l.strip()]
    final = {r["question_id"]: r for r in rows}
    counts = Counter(r["code"] for r in final.values())
    by_type: dict[str, Counter] = defaultdict(Counter)
    for r in final.values():
        by_type[r["question_type"]][r["code"]] += 1
    print("overall:", dict(counts), f"n={len(final)}")
    for qtype, counter in sorted(by_type.items()):
        print(f"  {qtype:28s} {dict(counter)}")
    flaw_total = counts["D1"] + counts["D2"] + counts["D3"]
    if final:
        print(
            f"honest floor: {flaw_total}/{len(final)} failures are benchmark "
            f"defects -> corrected ceiling ≈ "
            f"{(500 - flaw_total) / 500:.3f} for a perfect-defect-free system"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
