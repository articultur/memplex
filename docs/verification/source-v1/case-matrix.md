# W09 frozen source-validity case matrix

This is the Task 13 test inventory and scoped lifecycle realization on the accepted Task 12 foundation `21adf79c17d3a92e6e9a9d087f44ea20f5d4e4e4`, tree `08264d83228a76e8f986bad3831efa87cf7d3354`. The production source-contract default remains `off`. This document does not enable a domain or claim rollout readiness.

## Fixed inputs and oracles

The fixture JSON is a transcription of approved SPEC 13.1 and 13.2, not a recording of product output. The loader lives only in `tests/source_v1_case_support.py`; `LifecycleCase` has the eight planned fields. Each row explicitly names its change, read surface, identity boundary, allowed symbolic IDs, forbidden old body/citation/metadata sentinels, first rejection reason, independent control, timeline, principal and scope. There is no arbitrary expected-output field.

| Fixture | Inventory | SHA-256 |
| --- | ---: | --- |
| `tests/fixtures/source_v1/base_cases.json` | 120 | `3edcb31bcc6c14c09da848be61093f05754338c4a2014504ba493a3e7f93d608` |
| `tests/fixtures/source_v1/adversarial_cases.json` | 40 | `734dfb56b3cefd51225f315b0fb29a513bbb6c2b1fe8925072119f27c8caefae` |

The exact product is six changes (`content`, `delete`, `auth`, `expiry`, `upstream`, `incarnation`) × five surfaces (`query`, `get`, `context`, `hot`, `prefetch`) × four identity boundaries (`scope`, `subject`, `workspace`, `tenant`). IDs are `B-{change}-{surface}-{identity}`. Tests verify both each ID-to-tuple binding and the actual tuple product, so a mislabeled dimension cannot silently remove a change.

`D` resolves only to the actual successful publication receipt. `U` is an independently registered current source in the reader's admitted scope, owned by the real reader, with no dependency on S. Every base row requires exactly U and its body, while forbidding D's old body, citation and metadata. The ranked nomination deliberately carries all three obsolete sentinels, including in fields belonging to U; only final current projection can satisfy the oracle. Direct get serializes the actual returned nodes. Context, hot and prefetch assert the returned body and positive count, as well as a separate current assembly's exact IDs. No test accepts an empty response as success.

## Actual Task 13 branches

Each supported backend has 432 base test branches: the 120 base cells, with query additionally split into service and real HTTP search (144 branches), across warm, cold-cache and trusted-restart conditions. Lite and PostgreSQL are separate pytest parameters and native implementations. A parametrized PG skip is not a passed native execution.

Every cell creates a real S and D, verifies D can be read in its legitimate scope, nominates its real ID, commits the source change, and only then begins the tested read. The upstream cell uses D → M → S. Incarnation cells delete and legitimately recreate S with the same logical ID/content, prove a new incarnation, and reject the stale old-token write. Auth cells change only ACL revision. Expiry uses the accepted controllable-authoritative-L test seam: the originally live, future-expiring S keeps its reference unchanged; the test wrapper calls the real native session clock/lifetime checks, then supplies a deterministic L after expiry. This is a clock fixture, not a claim of a measured database clock advance.

Warm hot and prefetch references are seeded before the change commit and consumed afterward. Cold cases clear caches and create a fresh runtime; restart cases reopen the same durable bank through trusted admission. The cold/restart read still receives the frozen old ID nomination, without carrying a prior successful validity conclusion. These are same-process cold opens; cross-process ordering and crash recovery remain Task 14.

For `subject`, Alice's private D is rejected for admitted Bob, whose independent U is visible. The fixed native first reason is Lite `denied`, PostgreSQL `missing`: the PostgreSQL relational ACL conceals the private node before Python's ACL traversal. The exact root-level `backend_reason_overrides` map is schema-pinned as `lite: {}` and `postgres: {subject: missing}`; one reason is resolved before each assertion. It applies to exactly the 30 subject rows, leaving the allowed U, forbidden payload and every other reason unchanged. A separate positive test first proves the same admitted different subject cannot read a private ungranted D, commits an auth-only named-reader grant on its source, creates a fresh granted private publication, proves that subject can now read, proves an ungranted subject cannot, then revokes/regrants S and proves the old D never recertifies. Access-count/last-access changes retain the exact source token and digest with readable D. A separate independent E2 publication and U both survive invalidation of E1's publication.

For workspace/tenant base reads, S and D live in their legitimate original native authority; the reader and visible U live in a second genuine same-backend authority admitted to the different scope. A V1 authority binds exactly one tenant/workspace, so these cells prove combined authority/scope exclusion. They are not an independent proof of tenant-only or workspace-only validation. The first read failure is honestly `missing` in the reader authority. Separate native denied-input controls retain authority and every other SourceKey coordinate while changing only workspace, then only tenant: each dependency publication returns `unsupported_semantics`, advances no commit sequence and creates no target.

| Base condition | First read rejection |
| --- | --- |
| Own scope: content, deletion, necessary-upstream revision, old incarnation | `source_changed` |
| Own scope: ACL revision | `auth_changed` |
| Own scope: unchanged source expired at L | `expired` |
| Admitted other subject, Alice-private D | Lite `denied`; PostgreSQL `missing` |
| Real D from another admitted authority/scope | `missing` |

SQLite authority, legacy wrapper, sync-enabled V1 and off/legacy reader configurations have explicit native rejection tests. They are DENY configurations, not supported-backend skips. Earlier unknown-version/model-roundtrip/captured-input/restore tests remain in their accepted modules and CI workloads.

## Frozen A01–A40 and Task 14 boundary

All 40 original scenario, allowed-behavior and forbidden-behavior strings are retained verbatim. Their JSON `execution` is explicitly `frozen_scenario_mapping_not_execution`. Their reserved sentinels and reason identify the named branch; mixed old/new, boundary-inside/outside and missing/lookup-error scenarios retain their full original prose and are not collapsed into one asserted outcome. Manifest presence does not mean an A case executed.

| A IDs | Existing scoped regression seams or Task 14 work |
| --- | --- |
| A01–A04 | T10 final-L ordering and native transaction tests; T14 named C/R/L and cross-process interleavings pending |
| A05–A09 | `test_source_v1_validation.py` shared-ancestor, cycles/missing, closure/depth and fairness controls; T14 native interleaving/evidence mapping pending |
| A10 | T10 single-L expiry plus T13 expiry base cells; T14 controlled final-clock boundary pending |
| A11–A13 | `test_source_v1_publication.py` worker fence, late target and deletion/ABA controls; T14 named old-worker interleavings pending |
| A14–A16 | T13 incarnation and grant/regrant controls; earlier source-CAS/projection regressions remain |
| A17 | `test_source_v1_policy.py` current policy/principal regressions |
| A18–A20 | Earlier native source/publication fault and cross-process controls; T14 named crash/concurrency matrix pending |
| A21 | T13 mutation/read cases operate without rebuilding or models |
| A22–A24 | T10/T11 HTTP projection, reranker and cache-consumption regressions; T13 service/HTTP and hot/prefetch base branches |
| A25–A26 | T12 durable admission/restore quarantine; T13 trusted cold reopen; T14 crash/restore-restart evidence pending |
| A27–A31 | T12 sync/old-reader rejection, T1/T5 four-node identity roundtrip, T7/T8 ordinary writer rejection |
| A32 | T13 independent E2 positive; T1/T2 alternatives rejection remains a separate contract |
| A33–A36 | Existing carrier/output integrity, writer admission, unbound/historical and monotonic-time regressions; T14 timeout evidence pending |
| A37 | `test_source_v1_capture.py` full actual-input manifest and independent B-only publication controls |
| A38–A39 | Existing target incarnation ABA and recursive current-publication controls; T14 old-worker ordering pending |
| A40 | Existing capture/internal typed source and raw-deletion read controls |

These are source mappings, not claims that this Task 13 local command reran every mapped test. Task 15 will consolidate actual A-scenario evidence across accepted tasks and Task 14.

## Evidence and limits

The original manifest RED failed at the explicit missing-fixture assertions (six failures, exit 1), before fixture implementation; it was not an import/syntax error. The strict inventory then passed. The original uniform-`denied` candidate is retained; independent static review of native resolution order justified the narrow backend annotation before PostgreSQL execution. Missing/malformed backend-reason metadata had a separate observed RED before the exact map and loader check were implemented. The CI-list pin also had an observed RED before adding the new native files. Existing correct lifecycle product behavior passed as regression GREEN; no product defect was injected to manufacture RED. The initial synthetic author's omitted `trust_tier=1` was rejected by the product and corrected in test setup; that failed attempt remains retained.

Each lifecycle call records backend, actual principal/scope, logical receipt IDs, temperature/entry, last reached stage, first native rejection, successful unique snapshots retained by a separate validation probe and that probe's validation-fence-body interval, plus positive U/candidate fractions. The ordinary pytest PASSED/FAILED/SKIPPED outcome is authoritative. Standard JUnit artifacts retain finite test properties and original failure/skip information. Explicit `junit_logging=no` and `junit_log_passing_tests=false` prevent captured stdout/stderr from being newly exported. The existing repository-pinned upload action and unique platform/Python/head artifact names retain seven independent job records through ordinary official artifact readback. No test output hook or product observer is installed; a missing completed-oracle stage is never reported as pass. `diagnostic_validation_fence_body_ns` covers the explicitly timed diagnostic validator body inside a real fence. `diagnostic_fence_request_elapsed_ns` begins before requesting that fence and ends after exit; it includes acquisition, reconciliation and exit and is an upper bound on fence hold time, not an exact lock-duration measurement. Neither interval measures the earlier public critical section or claims a performance benefit. `successful_unique_source_snapshots` is not a count of all native resolve/read calls; failed or unresolved reads are not counted in that field. `diagnostic_source_lookup_attempts` and `diagnostic_resolve_id_attempts` separately count the actual calls on the explicitly owned diagnostic session instance while delegating to its original native methods. They do not count the earlier public request or modify product method bodies. `public_read_elapsed_ns` stops immediately after the named public read and excludes the subsequent independent proof. Local JUnit and raw logs retain these properties and exact first-failure tracebacks.

Local focused results and exact source/evidence hashes are in the immutable author report. External PostgreSQL was unavailable locally; all native PG entries remain pending exact hosted CI and independent review. The release workload retains its existing six full matrices, native PG/backup/sync files, coverage floor, 250 ms product budget, dependencies and ranking behavior. Only the necessary native test-list/pin and standard JUnit retention are added. No long local full run substitutes for or duplicates those exact release gates.


## PostgreSQL named-grant repair

[CI189, attempt 1](https://github.com/articultur/memplex/actions/runs/38645028958)
confirmed the named-grant availability failure on frozen commit
`30dc51e942a28f4061735d0221db9bcba414dd69`: 599 of 600 mandatory native
IDs passed, and the Bob ALLOW assertion failed. Its subsequent PostgreSQL
Charlie/revoke/regrant/U assertions were not reached.
The original assertion and the 120+40 fixture meanings remain unchanged.

The isolated repair adds immutable migration `0010_source_read_grants`, a
canonical current-head grant projection, and seven SELECT-only policies. V1
source reads and actual lexical/vector/list nomination use that projection;
the existing mutation predicates and legacy SQL selector retain their prior
rules. A private grantee receives no update, delete, re-share or target rights.
The service's current-policy, recursive source and final-L fence remains the
validity boundary. SQL row grants do not cache recursive authorization.

Occupied-bank upgrade requires the existing maintenance owner, READ COMMITTED
before the first query, fixed eight-table exclusive locks, and one transaction.
The bounded temporary NO FORCE scope retains enabled RLS, verifies existing
canonical payload/identity/locator/digests, projects existing grants, and
restores FORCE before exact catalogue/ledger convergence and commit. All private
row verification occurs before restoration. A failure before commit rolls back
the transaction; a lost commit acknowledgement remains unknown until a fresh
exact target/ledger/catalogue readback. Runtime never receives this connection.
The existing complete application ACL already includes the head and authority
SELECT rights used by these policies; no application privilege is added.

`test_source_v1_read_grants.py` retains separate native upgrade, normal
service/HTTP discovery, real lexical/vector nomination, read-only grant,
rollback, lock, bootstrap, drift and commit-acknowledgement controls. Its v9
fixture uses a real previous schema and canonical bound DTO seed, not execution
of a historical writer. It records the effective maintenance role and session
role separately, proves non-superuser/non-BYPASSRLS execution and table ownership,
and uses a distinct ordinary application role. Native parametrization and local
skips are not execution evidence. The repair native controls remain pending
actual PostgreSQL execution. Acceptance requires the unchanged original 600
mandatory native IDs and these 22 new native controls on the exact reviewed
CI candidate.
