# Proposal: read-only inspection of a run's records

Status: open proposal, not approved. It asks for a public API addition; the
[execution contract](../../docs/design.md) and [usage](../../docs/usage.md)
remain authoritative.

Written 2026-11-16 against Workjig `cecc730` (0.3.1 plus example tests) and
Commonplace `794a81126`. The consumer evidence was read from source, not run.
The Windows concern in section 4 is reasoned from documented platform behavior
and has not been reproduced.

## 1. Problem

Workjig's only way to read a run's records is to open a `Run`. Opening takes
the exclusive controller lock, requires the recorded working directory, and can
create a record. A status command, an audit or a provenance report needs none
of that: it wants to see what calls and launches a run holds, often while a
controller is still working.

Commonplace therefore parses the record files itself. Three readers depend on
the layout of `run.json` and on the names of the per-launch files:

| Reader (Commonplace) | Reads | For |
|---|---|---|
| `headless/jobs.py` `recorded_calls` | `calls`, each entry's `attempts`, `result`, the last attempt's `status`, `problem`, `launch_id` | run status, dismissing worker problems, listing unsettled launches |
| `lib/agentic_analysis/audit.py` `collect` | `request.worker.output_protocol` and `name`, each attempt's `launch_dir`, `launch_id`, `status`; then `stdout.log` and `prompt.txt` | inventory of every launch and transcript scanning |
| `lib/review_workflows/observations.py` `refresh` | `request.inputs`; then `calls/launches/<id>/stdout.log` | matching calls to a review request and extracting harness evidence |

Commonplace also spells launch file names where it already holds a
`LaunchRecord`: `stdout.log` in `headless/harness.py`, `stderr.log` in
`headless/jobs.py`, and both logs in `lib/agentic_analysis/audit.py`.

Consequences:

- `run.json` is undocumented for callers and carries no compatibility promise
  (“do not maintain backward compatibility … for persisted run records”), yet
  a format change breaks Commonplace silently. The persisted field is still `attempts` while
  the API says `launches` (ADR 001), so readers already carry both vocabularies.
- The readers skip Workjig's validation. `RunState.from_json` checks the
  version, hashes and launch history; the readers index into raw JSON and would
  misreport a record that Workjig itself rejects.
- The rule “delivered if a result is committed, else the latest launch's
  status” is written twice: in `Run._result` and in `recorded_calls`.

## 2. Proposal

Add one function that returns a validated, immutable snapshot of a run's
records without taking the lock.

```python
from workjig import read_run

view = read_run(record_dir)          # RunView | None
for call in view.calls:
    call.call_id, call.request_hash  # identity
    call.request                     # Request rebuilt from recorded work material
    call.status                      # LaunchStatus, same rule as CallResult.status
    call.outputs                     # committed outputs, empty unless delivered
    call.launches                    # tuple[LaunchRecord, ...]
view.unresolved_launches()           # tuple[LaunchRecord, ...]
```

Behavior:

- **No lock, no writes.** It never creates the directory, the lock file or a
  record, and it does not contend with a running controller.
- **Same validation as opening.** It loads through `RunState.from_json`. A
  corrupt, inconsistent or unsupported-version record raises `RecordError`.
- **Absent record.** A directory with no `run.json` returns `None`. That covers
  both “never started” and the interrupted-first-open case; the caller cannot
  tell them apart from records alone, and inspection should not guess.
- **No working-directory check.** `RunView.workdir` reports the recorded value.
  The reader may be running elsewhere, for example over a copied run; see
  open choice 6 for what that means for launch paths.
- **A snapshot of one file.** `run.json` is replaced atomically, so the view is
  one committed state. It can be stale by the time it is used. Launch files of
  a `reserved` or `running` launch may still be changing.

Types:

- `RunView`: `workdir`, `calls`, `unresolved_launches()`.
- `RecordedCall`: the fields above. It is not `CallResult`:
  `failed_at_launch_limit` and `reused` describe one call against a currently
  supplied ceiling, which a reader does not have.
- `LaunchRecord` and `Request` are reused unchanged.

### Launch file names

Readers also need the files beside each launch. Publish the names instead of
leaving consumers to spell them:

```python
from workjig import launch_files

files = launch_files(launch)   # paths under launch.launch_dir
files.prompt, files.stdout, files.stderr, files.problem, files.inputs, files.outputs
```

These paths are evidence locations. A path may not exist, and nothing here
interprets its content.

## 3. What stays with the caller

- Interpreting call identifiers. Commonplace's `job#n` convention and its
  per-job grouping are its own.
- Dismissals, the event log and accepted artifacts. These are Commonplace files
  beside the records, not Workjig state.
- Transcript scanning and harness evidence. `parse_evidence` is unchanged;
  attaching parsed evidence to records is a separate question already listed in
  the design rationale.
- Recovery. Acknowledging a stopped launch still requires an open `Run`.
- A command-line status tool. It could be built on this function later; it is
  not part of this proposal.

## 4. Open choices

1. **Reader and writer on Windows.** The controller commits with `os.replace`
   onto `run.json`. A reader that has `run.json` open at that moment can make
   the replacement fail on Windows, because Python opens files without delete
   sharing. A failed commit poisons the controller. Commonplace's current
   readers already carry this risk. Options: read with a share mode that
   permits replacement; have the writer retry a bounded number of times; or
   document lock-free reading as unsupported on Windows. Needs a reproduction
   before choosing.
2. **Strict or lenient loading.** Strict loading means an inspection tool
   cannot show anything about a record Workjig would refuse. That matches the
   project rule to reject clearly. A lenient mode for forensic use is not
   proposed; a corrupt record can still be opened as a file.
3. **`None` or an exception for an absent record.** `None` suits status
   commands that poll many run directories. An exception would match `Run`'s
   treatment of a non-empty directory without `run.json`.
4. **Exposing `Request`.** The view rebuilds the full request, including
   inputs and worker environment configuration. This is what
   `observations.refresh` and `audit.collect` use. A narrower view would hide
   it but force another private read.
5. **Renaming the persisted `attempts` field.** Once no consumer parses the
   file, the format can follow ADR 001's vocabulary. That is a record-format
   change and a separate approval.
6. **Launch paths over a copied run.** `LaunchRecord.launch_dir` is recorded
   as an absolute path, so `launch_files(launch)` over a copied or moved run
   points at the original location. The controller always creates
   `<record_dir>/launches/<launch_id>`. Options: derive the paths from the
   directory given to `read_run`, for example `view.launch_files(launch)`; or
   keep the recorded path and drop the copied-run claim in section 2.
7. **Execution controls on the rebuilt `Request`.** Only work material is
   persisted per call, so a rebuilt `Request` would carry the class defaults
   for `timeout_seconds`, `max_launches` and `retry`, not the values used. The
   effective values are on each `LaunchRecord`. Options: expose the work
   material without a `Request`; or return a `Request` and document those
   three fields as meaningless in a view.
8. **A call with no launches.** `RunState.from_json` accepts an entry with
   empty `attempts` and no result, but the status rule in `Run._result`
   raises on it. The controller does not write such an entry today, and
   Commonplace's `recorded_calls` skips one. Options: reject it on load, which
   also tightens what `Run` accepts; omit it from the view; or give
   `RecordedCall.status` a `None` value.
9. **Order of a call's versions.** `run.json` is written with sorted keys, so
   several work-material versions of one call ID load in hash order, not in
   the order they were made, and a reserved launch has no timestamp. The view
   cannot say which version is current. Options: document `view.calls` as
   unordered and leave selection to the caller, which holds the request hash
   from its `CallResult`; or record an order, which is a record-format change.
10. **Unsettled is not stale.** While a controller is working,
    `view.unresolved_launches()` includes that controller's own live launches.
    A reader can separate the cases only by also asking `busy(record_dir)`,
    and that answer can change between the two reads. Decide whether the view
    only documents this or reports the lock state itself.
11. **The evidence file.** `launch_files` as sketched omits it. Its name comes
    from `request.worker.evidence_file`, default `evidence.json`, so it cannot
    be derived from a `LaunchRecord` alone. The parsed content is already in
    `LaunchRecord.evidence`. Options: leave the file out; or build the paths
    from the call as well as the launch.

## 5. Acceptance

- Unit tests: a view over a record with delivered, failed, unsettled and
  multi-version calls; `RecordError` on a corrupt record and on an unsupported
  version; `None` for an empty or absent directory; no file created by reading.
- A subprocess test: `read_run` succeeds while another process holds the run
  open, and the controller's next commit succeeds afterward.
- `RecordedCall.status` and `CallResult.status` come from one shared rule.
- Commonplace replaces its three readers and every launch file path it builds
  by name with this API, with no behavior change in its status, audit and
  observation outputs. That change is made and verified in Commonplace.

## 6. Alternatives considered

- **Keep parsing in the consumer and document the format as stable.** This
  freezes a format the project has chosen not to freeze, and leaves validation
  duplicated or skipped.
- **A read-only mode on `Run`.** `Run` is a context manager built around lock
  ownership and a working directory; a mode that skips both would make every
  method conditional. A separate function keeps the controller unchanged.
- **Take the lock to read.** A status command could not report on a run that
  is in progress, which is its main use.
