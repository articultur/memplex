# Mutation Testing

This documents the repo's mutation-testing pilot: why it exists, how to run
it, the current baseline, and the *equivalence argument* that explains why
the surviving mutants cannot (and need not) be killed.

## Why

Coverage measures which lines tests execute — not whether the assertions
would notice a change. Mutation testing measures **test strength**: it
systematically breaks the code and counts how often the suite notices.

## How to run

```bash
scripts/mutation_pilot.sh          # ~15 min locally; pre-release gate
```

The pilot targets `memplex/sync_ingress.py` (the frozen ingress validator:
pure logic, fully typed, in the mypy gate). Configuration lives in
`[cosmic-ray]` in `pyproject.toml`. cosmic-ray mutates the module **in
place** (restoring it afterwards via the script's exit trap), so results are
real — unlike sandboxed runners where mutant activation can silently fail.

## Baseline (2026-08-14)

| Outcome     | Count |
|-------------|-------|
| killed      | 77    |
| survived    | 33    |
| incompetent | 1     |
| **kill rate** | **~70%** (of 110 competent mutants) |

## Why the 33 survivors are (mostly) equivalent

28 of the 33 survivors are mutations of `_normalise_protocol_numbers` —
specifically of the payload-integer branch:

```python
integer = int(value)
return float(value) if abs(integer) > 2**53 - 1 else integer
```

The mutated forms collapse or shift the `2**53 - 1` threshold (e.g.
`2**53 → 2+53`, `2**53 → 2//53`, `> → >=`, `> → !=`, `53 → 54`), sending
*some or all* payload integers down the `float(...)` path instead of the
`int` path. These are **provably unkillable** given this repo's canonical
encoder:

1. Acceptance is byte-exact: `validate_ingress_batch` requires
   `batch.canonical_bytes == raw` (`sync_ingress.py`).
2. For any integer `n` with `|n| ≤ 2**53`, `float(n)` is exact
   (IEEE-754 binary64), and the JCS serializer renders both forms
   identically: `_serialize_jcs(42)` → `"42"`;
   `_serialize_jcs_float(42.0)` → `repr` is `"42.0"` → trailing `".0"`
   stripped → `"42"` (`sync_protocol.py::_serialize_jcs_float`).
3. Therefore converting any sub-threshold integer to float yields the
   **same canonical bytes**, the round-trip still compares equal, and the
   request is still accepted. No test input can distinguish the mutant from
   the original in that range.

The only observable boundary is `|n| > 2**53`, where `float(n)` rounds
(e.g. `9007199254740993 → 9007199254740992.0`) and the canonical bytes
differ. That boundary **is** tested
(`test_validator_rejects_noncanonical_binary64_wire_numbers`,
`test_validator_payload_int_boundary_is_exact_2_pow_53`), which is why the
mutants that *break* the huge-int behaviour are among the 77 killed.

The remaining 5 survivors are boolean/decorator mutations of comparable
equivalent shape (e.g. `!=`/`is not` swaps on singleton sentinels that
compare identically for every reachable input).

## Interpretation rules

- A kill-rate drop in CI/local runs is a **regression signal** (a test was
  weakened or deleted).
- A *rise* in survivors with unchanged tests means the module grew — re-run
  the equivalence analysis above before chasing them.
- Do not "fix" the suite to kill documented-equivalent mutants: any test
  that appears to kill them is asserting non-deterministic or
  implementation-detail behaviour and will be flaky.

## Extending the pilot

Add a module to `paths`/`module-path` in `[cosmic-ray]` (prefer pure-logic
modules already in the mypy gate). Runtime scales as
`mutants × targeted-test-time`; keep the targeted test file small and fast.
