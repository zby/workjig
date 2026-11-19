# Tests

## Organization

| Location | Purpose | Default / CI |
|---|---|---|
| `tests/test_*.py` | Runtime unit, file-based and deterministic subprocess tests | Included |
| `tests/examples/` | Example snippets and commands with deterministic executable substitutes | Included |
| `tests/live/` | Examples against installed, authenticated Claude, Codex, Pi and Antigravity (`agy`) CLIs | Excluded; explicit opt-in |

The normal suite is deterministic and credential-free. It needs Python and the
locked development dependencies, not installed coding harnesses or model access.
Run it from the package root:

```sh
uv sync --locked
uv run pytest                  # all deterministic tests; no real model calls
uv run pytest -m unit          # direct function and file-based tests
uv run pytest -m subprocess    # deterministic worker-process integration
uv run pytest tests/examples   # deterministic example coverage
```

Every test must have exactly one execution-boundary marker (`unit` or
`subprocess`). Collection rejects missing or overlapping markers; unknown
markers are also errors. Entirely subprocess-based modules use a module marker.
Mixed modules mark each test. Select by boundary with `-m`, and by subject with
a path or `-k`. Live tests additionally carry the `live` marker and must remain
under `tests/live/`; that directory is excluded unless `--live-smoke` is supplied.
Explicitly selecting a live test file without the flag fails before execution.
This is collection separation, not a suite of automatically skipped tests.

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
| `test_view.py` | Read-only record view: validation, snapshots, launch paths, reading beside a live controller | Mixed |
| `test_continuation_validation.py` | Recorded execution controls and continuation authorization | Unit |
| `test_review.py` | Regression probes for output attribution and observed parallel limits | Subprocess |
| `test_collection.py` | Default exclusion, explicit-file guard and opted-in collection of live tests | Unit |
| `examples/test_examples.py` | README snippets, every example's help entry point, and single/all probe commands with substitutes | Mixed |
| `examples/test_simulated_harness.py` | Script-backed example delivery, effects, synthetic logs, replay and worker problems | Subprocess |
| `examples/test_smoke.py` | Manual probe-command argument validation; launches no harness | Unit |
| `live/test_readme.py` | README Claude example and Codex/Pi/Antigravity alternatives: delivery and reuse | Live subprocess |
| `live/test_probes.py` | Real single-harness probe commands: exact file copy and reuse | Live subprocess |

`unit` includes temporary-file tests, not just pure in-memory functions.
`subprocess` includes tests whose fixtures launch workers, even if the assertion
itself checks parsing or a persisted record. In the normal suite those workers
are local deterministic programs owned by the test, not live harnesses.

Example coverage preserves the real factories' remaining arguments and output
protocols while replacing only their executable prefixes with Python scripts.
Full ordinary-file workflow execution remains in `test_acceptance.py`; the
simulator workflow lives under `tests/examples/`. Their worker scripts execute
as part of those workflows. These checks do not establish live compatibility.

Test parsing, classification and validation directly rather than repeating the
whole runtime lifecycle for every input. Keep representative subprocess cases
for execution guarantees: files, effects, bounded retries, restart and cleanup.
Use private temporary state and settle owned processes; missing prerequisites
are failures, not reasons to skip.

## Real-harness smoke tests (manual opt-in)

**These tests make real model calls and consume account usage.** Install and
authenticate the CLIs first. The README suite uses the models and permissions
printed in the README; the probe suite uses the defaults in
[`examples/smoke_harness.py`](../examples/smoke_harness.py). Those models must be
available to your accounts. Permissions are not a sandbox: use a suitable test
environment. No missing prerequisite, authentication failure or unavailable
model is converted into a skip.

Inspect collection without calling a harness:

```sh
uv run pytest tests/live --live-smoke --collect-only -q
```

To deliberately run all eight live cases, choose a **fresh private artifact path**:

```sh
uv run pytest tests/live --live-smoke -v --basetemp=/tmp/my-workjig-live-run
```

Pytest clears an existing `--basetemp` directory: never point it at valuable
files or an earlier run you want to retain. Workdirs, records, prompts, delivered
files and raw logs remain beneath that directory after the run. Without
`--basetemp`, pytest's normal temporary-directory retention applies. Logs may
contain sensitive data; inspect before sharing.

Each test permits at most one physical launch with a 180-second timeout and
checks reuse without an additional launch. The full suite permits up to eight
launches (four README calls and four probe calls), with up to 24 minutes of
worker wait time plus setup/cleanup overhead. It has no automatic retries.
README cases check delivery of a nonempty summary, not its semantic quality;
probe cases check an exact file copy. The single/all command dispatch is also
tested deterministically, without repeating all live calls in `all` mode.

Select a narrower operation to spend less usage:

```sh
uv run pytest tests/live/test_readme.py --live-smoke -k claude -v
uv run pytest tests/live/test_probes.py --live-smoke -k codex -v
```

These tests are not part of normal CI. Adding or collecting them is not evidence
that a real invocation passed. Record actual probes, versions and evidence
limits in [harness evidence](../docs/harness-evidence.md). Direct manual probe
commands remain available in [usage](../docs/usage.md#examples-and-real-harness-probes).

## GitHub CI

[CI](../.github/workflows/ci.yml) runs both deterministic boundary groups on Linux
with Python 3.11–3.14 and on Windows with Python 3.14. Separate checks run lint,
formatting and the build. No model credentials, harness installations or real
model calls are configured. A local pass does not establish that every CI
matrix entry has passed remotely.

When checking or regenerating `uv.lock`, ignore machine-local uv configuration
so personal resolver settings do not leak into the shared lockfile:

```sh
uv --no-config lock --check
uv --no-config lock                 # only when intentionally updating the lock
uv --no-config sync --locked        # reproduce CI without personal uv settings
```

Captured CLI streams in `adr/evidence/` are static test inputs: replaying them
in pytest does not invoke the harness or establish current live support.
