# Changelog

Workjig is in early development: releases may change the public API and the
persisted run-record format without migration. Each entry states what changed
so callers can update and decide whether existing run directories remain usable.

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
