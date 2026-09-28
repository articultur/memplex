# F4 first cut: habit-semantics probe — the gap is cadence synthesis, not retrieval

Second-battlefield assessment predicted the habit gap ("检索纯词面").
This probe measures it. `scripts/habit_semantics_probe.py`, three arms
on a competitive 151-document pool (83 personal-history items + 10
habit-adjacent distractors + habits), orchestrated top-8, bge-m3 CPU.

## Results (n=40 habit-shaped questions)

| arm | statement/evidence recall | cadence recall |
|-----|--------------------------:|---------------:|
| plain sentences ("I water the aloe every Wednesday.") | 1.00 | 1.00 |
| typed Facts (frequency-bearing predicate + habit namespace) | 1.00 | 1.00 |
| implicit mentions, pre-consolidation | 1.00 (evidence) | **0.00** |
| implicit mentions, post F3 consolidation | 1.00 (evidence) | **0.00** |

Implicit arm: 4 varied one-off mentions per habit ("Watered the aloe
this morning." etc.), no explicit cadence anywhere; cadence metric
requires the habit's subject AND its ground-truth cadence token in one
retrieved line (bare token hits from unrelated memories are
contamination — the raw token metric read 0.75 from pool noise before
this coupling).

## Conclusions

1. **Retrieval is not the habit gap.** Explicit habit statements are
   perfectly retrievable in a competitive pool, in both encodings — and
   typed habit encoding buys zero retrieval gain over plain sentences.
2. **The gap is cadence synthesis.** For implicit habits (the LifeBench
   case), the evidence is fully retrievable, but the cadence exists in
   no text surface — and the F3 consolidation pass, as shipped, promotes
   the cluster verbatim (canonical text = one mention) without
   synthesizing frequency, so "How often do I water the aloe?" can
   surface the mentions but never the answer.
3. **v3 direction, quantified**: the consolidation promote step should
   synthesize the cadence into the sustained node's text surface
   ("observed N times across D days ≈ weekly" from the observation
   counter + span, both now first-class on paragraph rows). That turns
   the 0.00 into a measurable retrieval target; the probe harness here
   is the fixed measurement for it.

Honesty notes: synthetic 8-habit corpus, two implicit habits only
(aloe/grandmother); 1.0 recall ceilings in the explicit arms reflect a
 lexically distinctive synthetic corpus, not a claim about LifeBench
 difficulty. Protocol quirks encountered and fixed during the probe:
 arm-specific recall targets (typed summaries are not verbatim
 sentences), pool contamination in the cadence token, and pool-size
 saturation (28 docs → 151 docs).

Artifacts: `benchmarks/results/habit-semantics-probe/` (gitignored).
