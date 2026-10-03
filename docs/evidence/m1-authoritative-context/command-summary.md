# Memplex M1 local verification evidence

Available local gates PASS; mandatory full M1/release acceptance INCOMPLETE.
Only manifest.json and command-summary.md are included. Neutral artifact IDs identify separately retained technical evidence by SHA256 and size. No bundled-log or external-access claim.
Portable roots: <SOURCE_ROOT> is the checkout; <TOOLS_ROOT> is the isolated writable tools/runtime/cache root.
Original baseline and unchanged HEAD: 7b64f87cd90bac5393e31ea33000482d3a2597e0. Candidate remains uncommitted; candidate commit SHA: none.
Tested source: 663 files; inventory SHA256 325ee1f9818c0519a3af81ae9140444edaac49965b64f06fc26f4afeffeeb421.
Baseline-relative implementation diff: 348162 bytes; SHA256 874ceaa0db2c4a53ee45ffd023a6522ee0d77e543fce78bf08aaafb14ebc76da. Includes tracked changes and seven untracked source additions; excludes these two generated outputs. Complete delivered-patch hash is recorded externally to avoid self-reference.
Source freeze: 2026-10-03T02:59:08.167220Z; after-gate check: 2026-10-03T03:06:41.944178Z. Source hashes, modes and implementation diff unchanged.

## Exact fresh gates
Working directory: <SOURCE_ROOT>. Interpreter: <SOURCE_ROOT>/.venv/bin/python.
HOME=<TOOLS_ROOT>/baseline-home
UV_CACHE_DIR=<TOOLS_ROOT>/uv-cache
UV_PYTHON_INSTALL_DIR=<TOOLS_ROOT>/python-runtimes
npm_config_update_notifier=false
NO_UPDATE_NOTIFIER=1
GITNEXUS_NO_UPDATE_NOTIFIER=1
npm_config_cache=<TOOLS_ROOT>/npm-cache
PYTHONDONTWRITEBYTECODE=1
No PYTEST_ADDOPTS/PYTEST_PLUGINS/external PostgreSQL DSN override. No verifier-added global UV_OFFLINE. Notifier, cache, HOME and managed-Python settings retained.
Python: 3.12.14 (main, Aug 25 2026, 14:00:49) [Clang 22.1.3 ]; 71 distribution records; lock SHA256 36826cd7bdb0bceacd0f419fecad1cd4a2d20da4e4b66cab57070af2ba9360c6.
No concurrent benchmark or second test process during the exact final full run.
- uv lock --check
  UTC 2026-10-03T02:59:08.252250Z → 2026-10-03T02:59:08.272038Z; exit 0; evidence-current-lock-log; SHA256 bd5db5965344b10bd50403c613c2936b03183f97597001770598f71c6bbea3bf; 29 bytes
- .venv/bin/ruff check memplex tests
  UTC 2026-10-03T02:59:08.272563Z → 2026-10-03T02:59:08.281989Z; exit 0; evidence-current-ruff-log; SHA256 82b3e6a6c090a57601d22943bd23fca9218d1031dbe5a7b754092f9a156b4f18; 19 bytes
- .venv/bin/lint-imports
  UTC 2026-10-03T02:59:08.282312Z → 2026-10-03T02:59:08.401861Z; exit 0; evidence-current-imports-log; SHA256 2fa05eb4e9b033e74658c82ce620c62f9f3a826651e77cdcc431dab5324c450c; 1058 bytes
- .venv/bin/mypy
  UTC 2026-10-03T02:59:08.402554Z → 2026-10-03T02:59:08.526971Z; exit 0; evidence-current-mypy-log; SHA256 5525ee69f7acc60739d39ebd40e8c0169c4d79d0b48708365b122aee5ada2f48; 45 bytes
- git diff --check
  UTC 2026-10-03T02:59:08.527305Z → 2026-10-03T02:59:08.540762Z; exit 0; evidence-current-diff-check-log; SHA256 e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855; 0 bytes
- .venv/bin/python -m pytest tests -q --cov=memplex --cov-fail-under=75
  UTC 2026-10-03T02:59:08.541045Z → 2026-10-03T03:06:41.871627Z; exit 0; evidence-current-full-lite-log; SHA256 de7cf5ae295e9ae97f00a2ce175d7d31c6e65f50fcae41d8b25a0dda3811ef38; 16613 bytes
4466 passed, 25 skipped, 1 warning, 236 subtests passed in 450.25s (0:07:30)
Coverage 81.25% >=75%; zero failures/errors. One unchunked, unfiltered final-source invocation. Skip reasons are not reconstructed from aggregate -q output. Focused selections overlap and are not added.
Static gates: lock172 packages; Ruff clean; imports144 files/395 dependencies,4 kept/0 broken; mypy128 source files; diff check clean.

## Corrections and preserved history
MCP structured reads now serialize accepted current committed objects after canonical tenant/own/source-lineage checks and existing safety/host restrictions. Preview counts and samples include only accepted current records. Default detail is current; explicit temporal history and pending-review inspection retain their supported semantics. Pending/denied reads are neutral, without rejected IDs. Ordinary CRUD and unbound operator preview stay unchanged.
CLI default/explain JSON and human output use shared current wrapped fragments and public-trace filtering. Ranked candidates do not spend the final memory budget on old summaries; runtime candidate budgeting has the same narrow correction. Order, scores, canonical identity and existing candidate/source limits remain; CLI top_k is capped at500 before the raw producer call. Zero CLI budget returns empty; upper ceiling32000.
Genuine initial RED:34 failed/2 controls passed. Additional CLI budget RED:4 failed; runtime budget RED:2 failed. Later focused passes and corrected probe assertions are separate evidence, not reconstructed RED. All three original diagnostic scripts remain unchanged.
First correction aggregate:3 failed/4459 passed/25 skipped/236 subtests/1 warning,81.25% coverage. All three incomplete Observation fixture positives were retained and bound through the existing canonical identity helper; no ACL widening. The failed log and source checkpoint remain separate from the final run.
Earlier accepted full checkpoint:4398 passed/25 skipped/236 subtests/1 warning/81.19% coverage. It is historical after these corrections. Earlier failed aggregate:7 failed/4377 passed/25 skipped,81.18%; three CLI identity regressions and four UV_OFFLINE installation confounds remain separately documented.
The earlier explicit-context/CLI generated-fallback repair remains intact. Current correction and evidence await scoped independent re-review.

## Remaining limits
Real PostgreSQL+pgvector mandatory BLOCKED/NOT RUN:zero new cases executed. Prior normal/escalated AF_UNIX socket EPERM remains the blocker. Collection-only and mocked SQL are not certification; no PG/TCP/security/existing/remote database retry.
Remote six-leg Ubuntu/macOS×Python3.11/3.12/3.13 CI plus PG/security/installation gates remain unauthorized/not run. No candidate commit, push, publication or original-baseline CI substitution.
Actual Claude/Hermes availability skips are not installation/lifecycle certificates.
Generic Python service.query, HTTP GET /memories and corpus_recall remain ranked candidate/operator inspection producers; no demonstrated advertised model consumer in the audited routes. Generated/packaged agent assets contain no CLI scope-preview invocation; the getting-started example is operator inspection, while the agent-integration visible-sample promise maps to corrected MCP preview. The raw Python/HTTP/corpus and unbound operator outputs are not certified finalized model context.

## Budget and earlier measured performance
Budget is complete joined memory fragments, wrappers and separators under the existing nonempty len(text)//4+1 / empty0 estimator. JSON/table envelopes, repeated metadata and query/trace diagnostics are extra host overhead; no tokenizer or whole-transport-cap claim. Historical observed MCP estimates remain26 fragment/639 serialized/613 overhead.
Synthetic benchmark measurements remain bound to their earlier source checkpoint. Runtime/context/adapter code changed afterward; no fresh final-candidate timing or unchanged-core claim is made. Prior mixed tails and rw write median regression remain visible in the manifest.
Observation publication retains O(N) resident-map rebuild work and memory cost, including ordinary durable access writes. Bounded final reads do not make the whole live path O(candidate). No universal speedup, SOTA, SLA, full-M1/release acceptance or cross-host overlapping-operation global-linearizability claim.
Pre-edit actual impacts preserve dynamic/omitted callers as UNKNOWN. Additional runtime candidate-budget impact was HIGH:7 symbols,2 direct callers,4 process groups. Historical full-graph truncations remain disclosed; no absent-edge safety inference.
