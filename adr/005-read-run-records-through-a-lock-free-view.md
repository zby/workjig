# 005 — Read run records through a validated, lock-free view

Status: accepted
Decision date: 2026-11-16
Recorded: 2026-11-16

## Context

Opening a `Run` was the only way to read a run's records. It takes the
exclusive controller lock, requires the recorded working directory and can
create a record. Status commands, audits and provenance reports need none of
that and often run while a controller is working. Commonplace therefore parsed
`run.json` itself in three places and spelled launch file names in several
more, skipping Workjig's validation and depending on a format that carries no
compatibility promise. The
[proposal](evidence/read-only-run-inspection-proposal.md) lists those readers
and the choices considered.

## Decision

Add `read_run(record_dir)`, which returns an immutable `RunView` of the
committed `run.json`, or `None` when there is no `run.json`.

- It takes no lock and writes nothing. It never consults the lock either:
  `busy` takes the lock for an instant and could make a controller that is
  just opening fail.
- It loads through the same validation as `Run` and raises `RecordError` for a
  record `Run` would reject. There is no lenient mode.
- It does not check the working directory; the view reports the recorded one.
- `RecordedCall` exposes the recorded work material as fields. It does not
  rebuild a `Request`, whose execution controls would be defaults that no
  launch used; each `LaunchRecord` carries the controls it ran under.
- `view.calls` holds one entry per call ID and work-material version, in no
  meaningful order. The record keeps no order between versions, and callers
  select by the request hash they already hold.
- `view.launch_files(launch)` derives locations from the directory given to
  `read_run`, not from the recorded absolute `launch_dir`, so they follow a
  copied or moved run. The evidence file is not included: its name depends on
  the worker, and its parsed content is already on the launch record.
- `view.unresolved_launches()` reports reserved and running launches without
  saying whether a controller is alive. Recovery still requires an open `Run`.

`CallResult.status` and `RecordedCall.status` share one rule. Loading now
rejects a call entry with no launches, for `Run` as well: a controller creates
an entry together with its first reservation, so no sequence of reservations
produces one.

## Considered alternatives

- **Keep parsing in the consumer and declare the format stable:** freezes a
  format the project chose not to freeze and leaves validation skipped.
- **A read-only mode on `Run`:** `Run` is built around lock ownership and a
  working directory; a mode without both would make every method conditional.
- **Take the lock to read:** a status command could not report on a run in
  progress, which is its main use.
- **Return the recorded `launch_dir` paths:** Commonplace's audit already
  refuses a recorded path outside the run's own tree, and its observation
  reader already builds paths from the run root.
- **Report lock state from the view:** needs a second, racy read and touches
  the lock. Callers that want it ask `busy` themselves.
- **Record an order between versions:** a record-format change no reader
  needs.

## Consequences

Consumers can drop their own parsers and file-name spellings, and inspection
tools see only records Workjig accepts. A corrupt record shows nothing through
this API; it can still be opened as a file. The view may be stale by the time
it is used, and files of an unsettled launch may still be changing.

Unresolved limitations:

- **Windows.** The controller commits by replacing `run.json`. A reader that
  has the file open at that moment may make the replacement fail, which leaves
  the controller uncertain and its launch unresolved. This is reasoned from
  documented platform behavior and is neither reproduced nor ruled out.
  Reading beside a live controller is exercised on Linux only, and the usage
  guide says so.
- **Launch IDs.** Loading accepts any nonempty launch ID. `launch_files`
  raises `ValueError` for one that is not a plain directory name, so a view of
  a damaged record can hold a launch it cannot locate. Whether loading should
  reject such IDs is undecided.

Deterministic tests cover the view, the no-lock property and reading from
another process beside a live controller. Commonplace has not yet replaced its
readers with this API.

## Reevaluate when

The Windows collision is reproduced or ruled out; a consumer needs lenient
loading, version order, the evidence file or lock state from the view; or the
persisted `attempts` field is renamed once no consumer parses the file.
