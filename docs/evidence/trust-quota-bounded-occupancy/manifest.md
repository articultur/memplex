# F2b: quota-based bounded occupancy — implementation and feasibility

**Input**: F2a reproduced the exclusion-only verdict — `MEMPLEX_TRUST_PENALTY`
has no setting that separates malicious from legitimate tier-2 content
(0.5 zeroes ASR but drops legit tier-2 recall to 0.4; every effective
setting is exclusionary). The paper's proposed replacement is quota-based
bounded occupancy. This evaluates that replacement on our pipeline.

## Mechanism

`MEMPLEX_TRUST_QUOTA` (lite store, off by default) caps how many tier<=2
results may hold a slot in the top-k window. `_enforce_trust_quota` walks
the penalty-ordered list and admits at most Q low-tier hits; excess hits
leave the result set entirely — the window shrinks below top_k when
admissible items run out (containment bought with recall). When the quota
is active, the lexical leg over-fetches 2×top_k candidates (BM25 is cheap)
because the single-leg path otherwise truncates to top_k before the quota
could act; the default path is byte-identical when the quota is off.

- Code: `memplex/storage/lite/store.py` — `_trust_quota()`,
  `_enforce_trust_quota()`, 4 retrieval tails + fallback-fill re-enforce.
- Tests: `tests/test_trust_tier.py` — 4 new cases (window bound via the
  real search path, quota-0 exclusion, strict shrink semantics, default-off
  and parse-failure identity). All 10 pass; ruff + mypy clean.
- Impact analysis (GitNexus, fresh index): LOW risk, 1 direct caller
  (LiteMemoryStore.vector_search), lite module only — the postgres backend
  has no trust-weight mechanism to mirror.

## Evidence: the flood the weight cannot stop, the quota can

Five poisoned tier-2 payloads, all paraphrases naming a different fake
manager, compete for one probe ("Who is my manager?"), penalty off so the
quota is the only active knob. 83 clean tier-4 memories per store.

| config | poisoned slots in top-8 (of 5) | legit tier-2 recall (5 facts) | clean recall |
|--------|-------------------------------:|------------------------------:|-------------:|
| no quota            | 4 | 1.0 | 1.0 |
| quota = 2           | 2 | 1.0 | 1.0 |
| quota = 1           | 1 | 1.0 | 1.0 |
| quota = 0           | 0 | 0.0 | 1.0 |

Occupancy is bounded to exactly Q slots regardless of flood size, and
legit tier-2 recall stays 1.0 whenever Q >= 1 — a store with few
legitimate low-tier items is unaffected. This is the separation the
additive weight could not deliver: at penalty 0.5 the ASR gain came with
legit recall 0.4; the quota gets containment without collateral recall
loss. Residual: with Q=1 one poisoned slot remains — bounded, not
eliminated; the shipped penalty 0.5 composes (it zeroes single-item
laundering in competitive pools, F2a) and premise resolution still filters
superseded claims.

## Feasibility on the single-writer write path

Read-side quota (shipped here): the enforcement point is the retrieval
tail, independent of the write path; it composes with single-writer
serialization because it reads only immutable tiers at query time.

Write-side capacity (shipped as `MEMPLEX_TRUST_TIER2_CAP`, default off):
`_enforce_tier2_paragraph_cap` bounds the *store's* low-trust occupancy —
after each paragraph write, unconsolidated tier<=2 rows past the cap are
evicted oldest-first inside the same commit. Rows stamped
`consolidated_into` by the F3 pass are sustained and exempt, so promotion
is not undone by the cap. Enforced inside the single-writer lock with no
new indexes; the counter check is O(rows) per write, acceptable for an
opt-in knob. Contract tests cover: oldest-first eviction past the cap,
tier-4 exemption, consolidated-row exemption, default-off identity, and
parse-failure fail-closed (tests/test_trust_tier.py, 4 new cases).
Lite-only, matching the read-side quota's documented scope.

## Limitations (honest)

- Q=0 excludes every tier-2 item, including legitimate ones — the
  exclusionary endpoint, not a default.
- Single-item laundering is not a quota target: with one poison per topic
  it still wins its single slot. That stays with the penalty + premise
  resolution + clean-item priority.
- Opt-in only; shipped behaviour is unchanged (`_trust_quota() < 0` is the
  identity path, over-fetch gated on the same check).
- Lite-only, matching the penalty's existing lite-only scope.

Repro: `python scripts/trust_weight_reproduction.py` (quota_arm section).
Artifacts: `benchmarks/results/trust-weight-reproduction/{records,summary}.json`
