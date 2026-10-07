# Live BigModel host-lifecycle E2E

This opt-in runner joins real BigModel GLM-5.3 calls to Memplex's actual MCP
stdio capture, retrieval, update, identity, and deletion boundaries. It creates
only synthetic randomized marker codes in a disposable SQLite store. It never
reads user configuration, a production memory store, or another repository's
secrets. It is separate from automatic pull-request CI.

## What a pass proves

Four independent live capture turns produce assistant acknowledgments. Capture
prompts state that the fixture contains no real secrets or credentials and that
the host application handles storage. The session-scoped fixture uses an
ordinary fictional Juniper display label; session privacy is still enforced by
MCP metadata and actual identities, never by wording in the label. The host
then submits the actual user/assistant turns through `memory_turn_end`. Thirteen
fresh live reader requests receive only a neutral question and the actual
`memory_turn_begin` context. They never receive the expected answer, original
capture chat, another reader's history, or fixture metadata.

The runner checks exact answers **and** actual memory readbacks and context.
Random markers are generated after startup, so the model cannot know them from
training. Missing, stale, foreign-scope, or deleted markers fail deterministic
checks even if a model claims success. There is no LLM judge.

The Memplex server's internal extraction and embedding are deliberately
`rule-based` and `tfidf`; this is **live host/model lifecycle evidence**, not a
claim that Memplex's optional LLM extraction provider, PostgreSQL, or a complete
agent's autonomous tool-selection policy has been tested. Function add/update
and delete calls are driven by the host fixture, not selected by a model.

## Cases and ceilings

| Case | Live calls | Non-model evidence |
| --- | ---: | --- |
| Absent-memory control | 1 | Empty/irrelevant recalled context; UNKNOWN |
| Capture and immediate recall | 2 | Fact plus structured Observation readback |
| Restart and fresh-session recall | 1 | A new MCP process, persisted context |
| Fact correction/current-only | 2 | New current fact; old absent from current facts/context |
| Function edit/current-only | 1 | New active action; old action retained as deprecated |
| Session owner can recall | 2 | Captured session-private marker in owner context |
| Other session cannot recall | 1 | Private marker absent from retrieved context |
| Other workspace cannot recall | 1 | Workspace-visible Atlas and all known foreign markers absent |
| Other user cannot recall | 1 | Private marker absent from retrieved context |
| Other user cannot overwrite owner | 3 | Alice and Bob independently read their own value |
| Delete fact without resurrecting old value | 1 | Current fact ID absent; old/new absent from context |
| Delete Function | 1 | Deleted Function marker absent from context |

At most 17 serial requests, with zero retries and no fallback model or endpoint.
Each request explicitly caps generated output at 8,192 tokens: at most 139,264
requested output tokens in total, including reasoning under the vendor's
accounting. There are at most 20,000 characters per prompt and 12,000 characters
of recalled context. Oversized input fails rather than silently truncating.
Requests have 90-second HTTP inactivity/per-phase timeouts (not a total
wall-clock request cap), MCP batches 30-second process timeouts,
and the suite has a 1,200-second elapsed-time deadline checked before each
operation. The manual workflow adds a 21-minute live-step and 30-minute job
hard stop. Limits bound workload, not a promised monetary price. Actual
reported input/output usage is retained per completed provider response;
missing usage fails rather than becoming zero. Failed/timed-out calls can
still incur charges without returning usage.

## Credential and dispatch procedure

1. The repository owner personally adds `MEMPLEX_LIVE_API_KEY` to the Memplex
   repository's Actions secrets. It may contain the same approved BigModel key,
   but its value must never be extracted from another repository, put in a
   workflow input, committed, or pasted into a chat.
2. Review the exact four-file change and commit. A draft PR is not a paid-run
   approval. Confirm the intended commit, test scope, and live execution.
3. GitHub requires a new manual workflow file to exist on the default branch
   before dispatch. Obtain explicit merge approval after PR review and green
   offline CI; then review the merged SHA and confirm the paid run. Do not
   bootstrap it through another secret-bearing workflow. See
   [GitHub's manual-workflow requirement](https://docs.github.com/en/actions/how-tos/manage-workflow-runs/manually-run-a-workflow).
4. The manual workflow requires both `github.actor` and
   `github.triggering_actor` to be the repository owner, an exact `reviewed_sha`
   matching the workflow run's SHA, and `confirm_live=true`. No push, PR,
   schedule, or reusable-workflow trigger can run it. Re-running also incurs
   charges and requires separate execution approval.
5. The secret is exposed only to the bounded live step. Checkout disables
   persisted Git credentials; workflow permissions are read-only. Dependency
   installation and the offline harness checks occur without the API key.

Equivalent command in an already securely configured environment:

```sh
uv run python scripts/run_live_memory_e2e.py \
  --confirm-live --reviewed-sha <exact-reviewed-40-character-commit-sha>
```

The CLI rejects a dirty/untracked checkout before reading the key and requires
the runner itself to be tracked at the reviewed commit.

The CLI never reads `~/.claude/settings.json`, ambient Anthropic credentials,
or skillFlow secrets. It accepts no endpoint/model override. The MCP child
receives an allowlisted environment without the provider key.

## Provider compatibility

The fixed URL is `https://open.bigmodel.cn/api/anthropic/v1/messages`, model
`glm-5.3`. Requests follow BigModel's minimal Anthropic-compatible example and
omit `thinking` and `reasoning_effort`. GLM-5.3 requires thinking; do not copy
`thinking.type=disabled` from older benchmark scripts. Returned model identity,
completed response status, nonempty text, and usage are checked. Reasoning
blocks are neither treated as answers nor saved.

Official sources:
- [Anthropic-compatible endpoint and minimal GLM-5.3 request](https://docs.bigmodel.cn/cn/guide/develop/claude/introduction)
- [GLM-5.3 model restrictions](https://docs.bigmodel.cn/cn/guide/models/text/glm-5.3)
- [Output-token semantics](https://docs.bigmodel.cn/cn/guide/start/concept-param)

The GLM-5.3 page states that accounts which have ever subscribed to GLM Coding
Plan, even expired subscriptions, currently may use its model API only through
OpenAI Chat Completion. An incompatible account or response is a blocker, not
a reason to silently change endpoint or model. Review a separate explicit
protocol change before retrying.

## Evidence and interpretation

`artifacts/live-memory-e2e.json` records the commit and runner digest, limits,
case outcomes, expected synthetic codes, exact synthetic model prompts/text
answers, provider-reported usage, and exact MCP calls/readbacks. Temporary
fixture paths are normalized. Request headers, credentials, raw provider error
bodies, reasoning, environment dumps, and production/user data are excluded.
The public-repository workflow uploads only this file, for seven days.

A completed live pass means all 12 cases passed. A skipped job, missing key,
model refusal/truncation, transport failure, absent usage, timeout, incomplete
case set, or offline test-double run is never a live pass. Offline tests run the
real MCP subprocess and storage with a deterministic model-boundary double;
they validate the harness, not GLM-5.3 behavior.


### First live attempt (preserved)

[Run 37588724909](https://github.com/articultur/memplex/actions/runs/37588724909)
at commit `c001c83e25e92a24e31ef62575183a19e1fe4561` stopped after eight
provider calls with `capture_ack_format`. The first five cases passed; call
eight refused the fictional `MXPRIVATE-...` fixture before its
`memory_turn_end` write. The remaining seven cases were not executed. This
was not a complete pass or evidence of an isolation/deletion failure.
Provider-reported usage was 756 input and 1,938 output tokens.

The revised fixture uses `MXJUNIPER-...` and explicitly describes host-owned
storage of nonsecret synthetic data. Exact ACK validation, ACL metadata,
reader isolation, scoring, and workload limits are unchanged. A later run is
separate evidence and does not change the original result.
