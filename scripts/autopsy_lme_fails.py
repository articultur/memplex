#!/usr/bin/env python3
"""Failure autopsy for the official-J run: retrieval miss vs generation miss.

For every failed temporal-reasoning / multi-session question in the v3
records (which carry the retrieved context), ask glm-5.3 to classify:

  A - context already contains everything needed to answer correctly
      (retrieval fine; the generator failed)
  B - context is missing some evidence turns the answer needs
      (retrieval miss)
  C - the needed evidence is present but the DATES needed for the time
      reasoning are not in the context (date-index retrieval miss)
  D - the question needs information absent from the whole haystack shape
      provided (structural)

Output: per-question records + aggregate counts per type.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time

import httpx

import sys
RUN = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "benchmarks/results/lme-j500-v3")
OUT = pathlib.Path(sys.argv[2] if len(sys.argv) > 2 else "benchmarks/results/lme-autopsy")
OUT.mkdir(parents=True, exist_ok=True)

PROMPT = """You are auditing a retrieval-augmented QA failure. I give you the question, the correct answer, and the EXACT context that was retrieved and shown to the answer model (each excerpt prefixed with [date]). The answer model answered incorrectly.

Classify the ROOT cause with exactly one letter:
A - The context already contains all the information needed to produce the correct answer (the answer model failed anyway).
B - The context is missing one or more evidence excerpts the correct answer requires (retrieval miss).
C - The evidence is present but the dates/time information needed to reason to the correct answer is missing or incomplete in the context.
D - The question cannot be answered from this context shape at all.

Question: {question}

Correct Answer: {answer}

Context:
{context}

Reply with exactly one letter (A/B/C/D) then one short sentence.
"""


def main() -> int:
    settings = json.loads(pathlib.Path(os.path.expanduser("~/.claude/settings.json")).read_text())["env"]
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
    targets = [
        r
        for r in records
        if r["question_type"] in ("temporal-reasoning", "multi-session")
        and r["autoeval_label"] is False
    ]
    print(f"failed targets: {len(targets)}", flush=True)

    out_path = OUT / "autopsy.jsonl"
    done = set()
    if out_path.exists():
        for line in out_path.read_text().splitlines():
            if line.strip():
                record = json.loads(line)
                if record["cause"] in {"A", "B", "C", "D"}:
                    done.add(record["question_id"])

    with open(out_path, "a", encoding="utf-8") as out:
        for i, r in enumerate(targets):
            if r["question_id"] in done:
                continue
            prompt = PROMPT.format(
                question=r["question"],
                answer=r["answer"],
                context=r["retrieved_context"][:20000],
            )
            for attempt in range(5):
                try:
                    resp = client.post(
                        "/v1/messages",
                        json={
                            "model": "glm-5.3",
                            "max_tokens": 256,
                            "temperature": 0,
                            # classification does not need reasoning;
                            # disabled thinking keeps the verdict in the
                            # text block regardless of context length.
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
                except Exception:  # noqa: BLE001 - one failed classification must not kill the audit
                    if attempt == 4:
                        text = ""
                    time.sleep(min(60, 5 * 2**attempt))
            import re

            match = re.search(r"\b([ABCD])\b", text)
            letter = match.group(1) if match else ""
            out.write(
                json.dumps(
                    {
                        "question_id": r["question_id"],
                        "question_type": r["question_type"],
                        "cause": letter if letter in "ABCD" else "X",
                        "note": text.strip()[:200],
                        "hypothesis": r["hypothesis"][:150],
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            out.flush()
            if (i + 1) % 10 == 0:
                print(f"{i + 1}/{len(targets)}", flush=True)

    from collections import Counter

    deduped: dict[str, dict] = {}
    for line in out_path.read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            deduped[row["question_id"]] = row
    rows = list(deduped.values())
    for qtype in ("temporal-reasoning", "multi-session"):
        subset = [r for r in rows if r["question_type"] == qtype]
        print(qtype, Counter(r["cause"] for r in subset), f"n={len(subset)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
