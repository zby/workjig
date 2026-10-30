# Tests

The normal suite is deterministic and credential-free. It needs Python and the
locked development dependencies, not installed coding harnesses or model access.
Run it from the package root:

```sh
uv sync --locked
uv run pytest                  # everything below
uv run pytest -m unit          # direct function and file-based tests
uv run pytest -m subprocess    # deterministic worker-process integration
```

Every test must have exactly one execution-boundary marker. Collection rejects
missing or overlapping markers; unknown markers are also errors. Entirely
subprocess-based modules use a module marker. Mixed modules mark each test.
Select by boundary with `-m`, and by subject with a filename or `-k`.

## Where coverage lives

| Module | Subject | Boundary |
|---|---|---|
| `test_adapters.py` | CLI construction and synthetic evidence parsing | Unit |
| `test_platform.py` | Command resolution and POSIX session start, with the platform module patched | Unit |
| `test_windows_process.py` | Windows job-object ownership policy against a fake `kernel32`, runnable on any host | Unit |
| `test_outcomes.py` | Captured/synthetic stream classification; representative runtime outcome, retry and replay cases | Mixed |
| `test_core.py` | Request validation, locks, durable accounting, process ownership and cleanup | Mixed |
| `test_acceptance.py` | Public API and ordinary-file workflow, parallel settlement, corrections and recovery | Mixed |
| `test_metadata.py` | Untrusted worker problem/evidence files, including FIFOs and timeouts | Subprocess |
| `test_record_validation.py` | Corruption and lifecycle validation against records produced by deterministic workers | Subprocess |
| `test_continuation.py` | Execution-control changes, total ceilings, usage-stop approval and recovery | Subprocess |
| `test_continuation_validation.py` | Recorded execution controls and continuation authorization | Unit |
| `test_review.py` | Regression probes for output attribution and observed parallel limits | Subprocess |
| `test_smoke.py` | Manual smoke-command argument validation; launches no harness | Unit |

`unit` includes temporary-file tests, not just pure in-memory functions.
`subprocess` includes tests whose fixtures launch workers, even if the assertion
itself checks parsing or a persisted record. A subprocess test is not a live
harness test: the workers are local deterministic programs owned by the test.

Test parsing, classification and validation directly rather than repeating the
whole runtime lifecycle for every input. Keep representative subprocess cases
for execution guarantees: files, effects, bounded retries, restart and cleanup.
Use private temporary state and settle owned processes; missing prerequisites
are failures, not reasons to skip.

## GitHub CI

[CI](../.github/workflows/ci.yml) runs both groups on Linux with Python 3.11–3.14
on pushes and pull requests. Separate checks run lint, formatting and the build.
No model credentials, harness installations or real model calls are configured.
A local pass does not establish that every CI matrix entry has passed remotely.

When checking or regenerating `uv.lock`, ignore machine-local uv configuration
so personal resolver settings do not leak into the shared lockfile:

```sh
uv --no-config lock --check
uv --no-config lock                 # only when intentionally updating the lock
uv --no-config sync --locked        # reproduce CI without personal uv settings
```

## Real-harness probes stay manual

[`examples/smoke_harness.py`](../examples/smoke_harness.py) deliberately invokes
an installed harness and model, checks a delivered file, then verifies reuse.
It is outside pytest and is not run by GitHub CI. See the
[manual probe commands](../docs/usage.md#examples-and-real-harness-probes).

Captured CLI streams in `adr/evidence/` are static test inputs: replaying them
in pytest does not invoke the harness or establish current live support.
Actual probes, tested CLI versions and evidence limits belong in the
[harness evidence](../docs/harness-evidence.md).
