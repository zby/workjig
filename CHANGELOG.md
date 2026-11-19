# Changelog

Workjig is in early development: releases may change the public API and the
persisted run-record format without migration. Each entry states what changed
so callers can update and decide whether existing run directories remain usable.

## Unreleased

- Resolving a Codex launch reports a distinct `NativeSession.reason` for a
  missing `codex`, a helper that fails to start, times out, exits, errors or
  reports no path, instead of `no transcript location reported`. See
  `docs/usage.md`.
- `Run(..., retain_logs=True)` retains launch stdout and stderr by default. Set it
  to `False` (smoke CLI: `--no-verbose`) to omit both raw logs on new launches.
- Record format 4 retains task/input/delivered text once per distinct UTF-8 payload
  within each run. Copy `payloads/` with `run.json`; use fresh runs rather than
  migrating older records. The request hash and public text views are unchanged.
- Added Antigravity CLI (`agy` 1.5.12) adapter (`agy_worker`),
  `agy-stream-json` terminal-failure classification, `parse_evidence("agy", ...)`
  and native session tracking (`track_native_session=True`). See
  `docs/harness-evidence.md` for bounded probe evidence and limits.
- Opt in to launch-bound native transcript pointers in the harness adapters
  (Codex, Claude Code, Pi and Antigravity CLI). `RunView.native_session(launch)`
  locates the transcript without starting a harness. The controller no longer
  looks up a Codex transcript during a launch: a Codex receipt holds only the
  thread ID, so that call raises `UnresolvedSessionError` for it. Pass
  `resolve=True` to run the reader's `codex` with `CODEX_HOME` set to the
  parent of the recorded sessions directory. A symlinked Codex `sessions`
  directory cannot be resolved and is reported as unavailable. Only resolve trusted runs.
- Named input and output strings now preserve UTF-8 file line endings on new
  launches. Replay returns the string originally committed; it cannot recover
  line endings normalized by an older runtime. Use a fresh run when exact
  bytes matter.

## 0.4.1 - 2026-11-16

Two fixes found in review before 0.4.0 was uploaded; 0.4.0 was tagged but not
published to PyPI. The public API and run-record format are unchanged from
0.4.0.

- Fixed: a bare command name was looked up on the controller's `PATH` even when
  the worker's `env` set its own, so a launch could run a different executable
  than the worker environment named. Lookup now uses the worker's `PATH`.
- Fixed: a worker problem or evidence file nested too deeply to parse raised
  `RecursionError` during collection, leaving an exited worker recorded as
  `running` and the controller uncertain. Such a problem file is now an invalid
  problem (`protocol_error`) and such an evidence file is ignored, as for other
  malformed metadata.

## 0.4.0 - 2026-11-16

Read-only inspection of run records; see ADR 005. The run-record format is
unchanged, but loading is stricter in one case.

- Added `read_run(record_dir)`, a lock-free, read-only and validated view of a
  run's committed records, returning `RunView` (with `RecordedCall` entries and
  `launch_files(launch)` returning `LaunchFiles`) or `None` without
  `run.json`. See `docs/usage.md`. Reading beside a live controller is
  exercised on Linux only; on Windows it may make the controller's commit
  fail, which is neither reproduced nor ruled out.
- A `run.json` nested too deeply to parse now raises `RecordError` instead of
  `RecursionError`.
- Loading a run record, by `Run` or `read_run`, now rejects a call entry with no
  launches with `RecordError`. The controller never writes one. The run-record
  format is otherwise unchanged.

## 0.3.1 - 2026-11-03

Fixed: on Windows the run lock made `.lock` unreadable through other handles
while a `Run` or `hold()` owned it, because `msvcrt.locking` takes mandatory
byte-range locks and the lock covered byte 0. The lock now covers one byte
1 GiB past the start, so the hold marker and interrupted-open bytes stay
readable as recovery evidence. Exclusion between holders is unchanged. The
public API and run-record format are unchanged.

## 0.3.0 - 2026-10-30

Limited Windows support. Workers on Windows are created suspended inside a
dedicated job object and resumed once owned, so descendants that outlive their
leader are detected and terminated as on POSIX; see ADR 004 for the platform
clauses (no cooperative termination phase, controller death kills the job,
Windows 8 or Server 2012 or later). The deterministic suite runs on Windows in
CI; no real harness has been probed there. The public API and run-record format
are unchanged. Internally the platform seam is now `start_process` returning
an `OwnedProcess`; `process_creation_options` and the Windows branch of
`cleanup_process_group` are gone. Also fixed: the Windows run lock positioned
the buffered file rather than the descriptor before `msvcrt.locking`. The
Windows CI job now runs the deterministic subprocess suite as well as the unit
tests.

## 0.2.1 - 2026-10-27

Identical to 0.2.0 except that `uv.lock`, which the sdist includes, no longer
carries machine-local `exclude-newer` options that made `uv sync --locked`
fail on other machines. Installing 0.2.0 was unaffected; prefer 0.2.1 when
working from the sdist.

## 0.2.0 - 2026-10-27

First release with the changes below. Version 0.1.0, published on 2026-10-20,
was an earlier development snapshot without a changelog.

- Execution contract: caller-written Python workflows launch subprocess
  workers with fresh per-launch paths, durable launch reservations, bounded
  retries, explicit recovery of unresolved launches and atomic result records.
  See `docs/design.md`.
- Adapters for headless harness CLIs: Codex 0.162.1, Claude Code 2.1.303 and
  Pi 1.3.0, with terminal-failure classification of their JSON output. Generic
  commands use exit status and the file protocol only. See
  `docs/harness-evidence.md` for what was tested against real binaries.
- Run record format version 2. Older formats are rejected.
- `LaunchStatus` string enum exported; `CallResult.status` and
  `LaunchRecord.status` are its members and still compare equal to the plain
  status strings.
- `WorkjigError` is the common base of `RecordError`, `UncertainLaunchError`
  and `RunBusyError`.
- Work identity is derived from every `Request` field that is not a declared
  execution control, so new fields cannot be omitted from the request hash.
  Hashes of existing requests are unchanged.
- `LaunchRecord.argv` is required; records without it were never valid.
- Internal: typed run state in `workjig.records`, lifecycle invariants checked
  on `LaunchRecord` construction, one lock acquisition path for `Run`, `hold`
  and `busy`.
