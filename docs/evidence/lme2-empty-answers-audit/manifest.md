# web/enterprise-medium empty answers: root cause audited and fixed

13.8% of web-medium (33/240) and 5.7% of enterprise-medium (12/211)
answers came back empty and were silently scored wrong. Workflow-run
audit (investigator + independent confirmer, record-id level):

## Root cause (confirmed)

glm-5.3 on the bigmodel Anthropic-compatible endpoint thinks by default,
and thinking tokens share the max_tokens budget. The runner's answerer
call (`scripts/run_lme_v2.py` GlmProxy.complete) passed no thinking
parameter and capped max_tokens at 1024: on reasoning-heavy questions
the thinking chain burned the whole budget — HTTP 200,
stop_reason=max_tokens, content blocks contain only a thinking block —
and the text-block join produced "".

Evidence: all 33 empty records follow the 200+no-text-block path (zero
"generation failed"/"judge failed"/"quota exhausted" lines in both run
logs — the exception paths provably never fired); 5 direct reproductions
of failed records (web 73297783 / 9ed5a14d / 1defc293 / 3c0c1b19, ent
73cbefdc) showed the identical signature (output_tokens=1024, all in
thinking); fix variants verified — thinking disabled yields text at
87-147 tokens, max_tokens=8192 lets the worst case finish at 5805.
Type distribution corroborates: procedure-family questions (heaviest
reasoning) have the highest empty rates in both domains (web 26.2%/25%,
ent 21.9%) vs static-environment-abs 3.2%. Honest limit: per-record
stop_reason was not persisted, so "all 33" rests on 5 reproductions
plus distribution match, not per-record proof.

Ruled out with evidence: 1301 content safety (0 hits; reddit — the
most UGC-heavy environment — has the LOWEST empty rate), timeouts/
network (exception path never printed), 1310 quota (run completed).

Collateral finding (same mechanism, silent): the llm judge used
max_tokens=256 with default thinking — thinking could burn it and
silently score False with no log; 156/451 (34.6%) of questions use
llm judging.

## Fix (shipped in scripts/run_lme_v2.py)

- `complete_answer()`: max_tokens 8192 (thinking finishes, worst
  measured case 5805), then if the response still has no text, one
  retry with thinking disabled (87-147 tokens) — an answer always
  lands. Verified live: a reasoning-heavy probe returns a full
  structured answer at stage 1.
- Judge call: thinking disabled (short-output task).
- Protocol note: already-published small/medium numbers carry this
  measurement artifact; the ceiling on web-medium is +13.8pp (33/240)
  if every recovered answer scored right. A re-run with the fixed
  runner quantifies the true recovery.

The mm-medium runner (`scripts/run_lme_v2_mm_medium.py`) ships the same
two-stage answerer from the start.
