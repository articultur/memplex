# PR #40: cancellation timeout test investigation

Investigation date: 2026-10-23. Local platform: Linux, CPython 3.11.17.
Base: `228f1ed4321ed639bab6e015c837de8f0f659405`.
Dependency head: `c74c9dc11606c688c48603fa1372f57ed18371de`.

## Original failure and history

- [PR #40](https://github.com/articultur/memplex/pull/40) changes only `uv.lock`.
- [CI attempt 1](https://github.com/articultur/memplex/actions/runs/39392473271/attempts/1):
  23 successful jobs, one failed job: macOS / Python 3.11,
  [job 118527058220](https://github.com/articultur/memplex/actions/runs/39392473271/job/118527058220).
  Its full log reports 4,981 passed, 1 failed, 26 skipped and 236 subtests passed.
  The only failure is the 150 ms elapsed-time assertion in
  `test_async_entry_point_is_bounded_when_provider_suppresses_cancellation`:
  `320.446906125 - 320.277248708 = 0.169657417` seconds.
  The runner used CPython 3.11.9 / macOS arm64.
- Retrying only that job on the unchanged PR head succeeded:
  [attempt 2, job 120329034309](https://github.com/articultur/memplex/actions/runs/39392473271/job/120329034309).
  The full log explicitly shows the original test passing; the test step reports
  4,982 passed, 26 skipped, one warning and 236 subtests passed in 552.70 seconds.
- Main CI runs [37715964228](https://github.com/articultur/memplex/actions/runs/37715964228)
  and [37716043567](https://github.com/articultur/memplex/actions/runs/37716043567)
  both succeeded after the test was introduced in `614153b`.
  The full log of macOS/Python 3.11 job `113118139089` explicitly shows this test passing.
  This is a limited history sample, not a measured long-term flake rate.

## Diagnosis

The original assertion times `asyncio.run(enhancer.factualize(...))`, including
event-loop startup, default-executor dispatch, thread creation and loop shutdown.
Production `factualize` delegates to `factualize_sync` via `asyncio.to_thread`;
`run_bounded_capture` starts an isolated daemon worker and calls real
`threading.Event.wait` with the configured 20 ms budget. Provider completion is
not required for the foreground timeout result.

On unchanged main, 100 sequential calls with worker cleanup all returned timeout:
minimum 21.153 ms, median 21.911 ms, maximum 26.884 ms. No 150 ms failure occurred
without injected delay on this host. Inserting 170 ms before actual executor
dispatch reproduces the old assertion failure on unchanged main (192.772 ms),
while the result remains `timeout`:

```python
import asyncio
import pytest

original = asyncio.to_thread

async def delayed_dispatch(*args, **kwargs):
    await asyncio.sleep(0.17)
    return await original(*args, **kwargs)

asyncio.to_thread = delayed_dispatch
raise SystemExit(pytest.main([
    "tests/test_factual_capture_validation.py::"
    "test_async_entry_point_is_bounded_when_provider_suppresses_cancellation", "-q",
]))
```

This establishes a scheduler-sensitive test oracle independent of the upgrade.
The unchanged test on the PR #40 lock also fails under the same injected delay
(192.878 ms), still returning `timeout`.
It does not reconstruct the exact scheduling delay of the original macOS job.
No production cancellation defect was reproduced.

## Test-only correction and safety checks

- Keep the real asynchronous entry point, real threads, real cancellation and
  configured 20 ms foreground wait. An observing Event subclass delegates to
  the real wait and checks its actual timeout argument is exactly `0.02`.
- The provider suppresses repeated cancellation and cannot complete until the
  test explicitly releases it. Require a `timeout` result and observed
  cancellation while the provider is still unfinished. This checks ordering
  rather than relative sleep durations.
- A five-second watchdog detects deadlock; it is not the capture budget.
  Always release the provider and wait for the daemon's completion signal,
  which follows capture-slot release, so repeated runs do not leak workers.
- Mutation checks: changing the foreground wait to `None` fails with the
  watchdog's `TimeoutError`; changing its budget to `timeout * 10` fails with
  `0.2 != 0.02`. Thus the correction rejects both waiting for an uncooperative
  provider and silently inflating the configured deadline.

## Lockfile and compatibility

Parsed TOML comparison finds exactly two changed package records:

| Package | Main | PR #40 | Wheel records |
| --- | --- | --- | --- |
| aiohttp | 3.14.3 | 3.14.4 | 100 -> 142 |
| yarl | 1.24.5 | 1.25.1 | 86 -> 120 |

Other package records and project requirements are unchanged. The changed
records contain new versions, source distribution metadata and wheel metadata;
their dependency names/markers are unchanged in the lock representation.
Installed aiohttp 3.14.4 metadata raises its yarl floor to `>=1.25.1,<2.0`.
Both packages declare Python `>=3.10`, compatible with the 3.11-3.13 CI matrix.
See the [upstream release](https://github.com/aio-libs/aiohttp/releases/tag/v3.14.4)
and [version comparison](https://github.com/aio-libs/aiohttp/compare/v3.14.3...v3.14.4).
The upstream diff includes broader HTTP/cookie/parser changes, so the version
number alone is not evidence of compatibility.

No `aiohttp`/`yarl` imports occur in Memplex production code or tests. In the dev
environment aiohttp is brought in by `cosmic-ray`; the optional dependency graph
also includes it through `kubernetes`. The failing test uses an offline provider
and does not execute either HTTP library.

The exact PR #40 lock installs successfully with `uv sync --locked --extra dev
--python 3.11`; `uv lock --check` and `uv pip check` pass (70 installed packages).
Factual capture, HTTP API and sync tests: 228 passed, one existing Starlette/httpx
deprecation warning. A real loopback aiohttp smoke check verifies a request
times out and a subsequent JSON request on the same ClientSession succeeds.

With the corrected test, both main and PR #40 locks pass 100 normal runs and 20
runs with 170 ms injected dispatch delay. Test harness elapsed times include
cleanup: main normal 22.759-32.717 ms, delayed 532.600-538.449 ms;
PR #40 normal 23.612-34.287 ms, delayed 532.735-541.047 ms.

## Scope

The fix changes only the cancellation test and this evidence note. Production
code, dependency lock and workflow definitions are unchanged. The independent
fix targets main; PR #40 remains a separate dependency decision. Local results
do not substitute for macOS CI. Full-suite and remote CI results are recorded
in the fix PR's validation section.

## Local completion gates

On main plus this test-only correction, all required lite gates passed:

| Command | Result |
| --- | --- |
| `uv lock --check` | 172 packages resolved; lock unchanged |
| `.venv/bin/ruff check memplex tests` | All checks passed |
| `.venv/bin/lint-imports` | 4 contracts kept, 0 broken |
| `.venv/bin/mypy` | No issues in 128 source files |
| `MEMPLEX_INSTALL_MATRIX_PYTHON=3.11 .venv/bin/python -m pytest tests -q --cov=memplex --cov-fail-under=75` | 4,982 passed, 26 skipped, 236 subtests passed; 81.87% coverage; 550.65 seconds |

The one warning is the existing Starlette/httpx deprecation also present in
the original CI log. No storage/sync/migration implementation changed; a new
local real-PostgreSQL run is not required by the repository's scope rule.
GitNexus impact found no resolved upstream test callers; detect-changes reports
zero affected processes and low risk. Diff inspection confirms only one test
function changed (some neighboring unchanged functions are listed by the graph
tool because their line positions moved).
