# Security Scan Triage

Sealed deep-scan triage record. Re-run the scan after meaningful changes and
re-verify each category; do not treat this file as a permanent clearance.

## Scan under review

- scanId: `scan-2026-08-14T15-39-50.671Z-5094ba8e286a`
- seal: `sha256:cfadc46d0ebab6bc1f4b409864d620a06f7cb411de8c44eb1e344a5a565503aa`
- depth: deep · 77 packages · 61 findings (53 high / 6 medium / 2 low)
- boundary: `static_only_no_runtime_execution`

## Triage verdicts

### sql-injection "entry" cluster (~20 findings, scripts/verify_g009 + cross-file hops)

`_execute(dsn, statement, parameters)` and every `cursor.execute` in the
capacity-chaos tooling use `%s` placeholders with parameter tuples;
identifiers use `psycopg2.sql.Identifier`; workload values (`rng.randrange`,
counts) flow through parameters, never interpolation. The findings are
taint-analysis flags on the statement-as-parameter signature pattern.
**Verdict: false positive — already parameterized.**

### path-traversal "entry" cluster (~10 findings, evidence readers)

`read_industrial_gate_evidence` / `read_host_lifecycle_evidence` /
`read_capacity_chaos_evidence` / `read_release_evidence_file` implement
dir_fd-pinned opens, `O_NOFOLLOW`, `S_ISREG` + size caps (symlink/TOCTOU
safe); paths originate from operator-controlled env/config in single-tenant
CLI contexts. `write_operations_report_atomic` writes to an
operator-configured report path. **Verdict: false positive — hardened by
design.**

### 不可信命令参数向量 (npm installers ×2)

`spawnSync(process.execPath, [bin, "setup", ...process.argv.slice(2)])` —
forwarding the invoking user's own argv to the wrapped CLI is the wrapper's
entire purpose; same trust level as invoking the CLI directly.
**Verdict: false positive by design.**

### 硬编码凭据 (local.py:51)

`api_key="not-needed"` is the documented placeholder for local
OpenAI-compatible endpoints that require no auth. Not a secret.
**Verdict: false positive.**

### 不安全的随机数 (sync_dispatcher.py:286)

`random.uniform` computes full-jitter retry backoff windows — timing jitter,
not a cryptographic purpose; the adjacent comment documents the policy.
**Verdict: false positive (correct randomness class for the use).**

### Dependency advisories (3 advisories / 2 packages)

The offline advisory summary matched 2 packages without naming them in the
manifest. Local `pip-audit` cannot run in this sandbox (ensurepip SIGABRT —
same root cause as the git-hook `scanner_enobufs` notices). The CI
`security` job audits every extra strictly and is green on `origin`.
**Verdict: open — confirm via the next CI security job output; escalate any
advisory that lands on a runtime extra.**

## Rerun instructions

```
mimosa security_scan (deep) → compare findingCount/delta against this file
```

Any NEW finding category not covered above, or any category whose code
chANGED since this scan, must be re-triaged before dismissal.
