# Usage and recovery

Detailed usage for the [Workjig runtime](../README.md). For guarantees and
limits, see the [execution contract](design.md).

## A call

```python
import sys
from pathlib import Path

from workjig import CommandWorker, Request, Run

worker = CommandWorker(
    argv=(
        sys.executable,
        str(Path("worker.py").resolve()),
        "{prompt}",
    )
)
request = Request(
    call_id="summarize-v1",
    task="Read the supplied source and write the requested summary file.",
    inputs={"source.txt": "Material to summarize."},
    required_outputs=("summary.txt",),
    worker=worker,
    timeout_seconds=60,
)

with Run(Path("run-records"), Path.cwd()) as run:
    result = run.call(request)
    if result.status == "delivered":
        print(result.outputs["summary.txt"])
    else:
        print(result.status)
```

`worker.py` represents your worker executable. It receives the path of a
generated prompt that lists absolute input and output paths. It must read the
inputs and write the named files. Standard output is execution evidence, not
the requested file result. Command arguments are passed without a shell. A
bare command name is looked up on the worker's `PATH`: the controller's,
unless the worker's `env` sets its own.

The generic command adapter also accepts `{input_dir}`, `{output_dir}`,
`{problem_file}`, `{launch_dir}` and `{workdir}` path tokens. Named inputs and
outputs are single filenames. Each physical launch has fresh paths, including
when a call retries.

## Testing workflows without a model

A simulator can be an ordinary script launched through `CommandWorker`.
It receives the real prepared launch files and produces outputs, filesystem
effects and fixture logs through the same subprocess boundary as any worker.
See the [runnable simulator example](../examples/README.md#script-backed-simulated-harness),
including how to substitute a concrete adapter's executable without changing
its argument construction or protocol selection.

## Resume and corrections

Reopen the same run directory and repeat the same call to reuse a committed
result. Both the stable `call_id` and declared work material must match: task,
inputs, required and optional outputs, and worker configuration. Timeout, launch
ceiling, and retry policy are execution controls, not work identity. Changing
them replays delivered answers and applies the new controls only to future
launches. `CallResult.request_hash` hashes the work material, excluding these
controls. Different identities request independent work even when prompts match.

Resume is an exceptional recovery path for workflow and runtime code
compatible with the run, not a way to carry old runs across incompatible
changes. Changing execution controls or adding later calls is compatible;
changing what an existing call's result means without changing its identity is
not. After an incompatible change, start a fresh run directory and rerun the
whole workflow. Migration of old runs is out of scope; the runtime rejects
record formats it does not support.

The caller must also keep the surrounding environment compatible with reuse.
Changing ambient files or tools may require a new run. The runtime does not
infer those dependencies, pin code or manage environments. Starting fresh does
not undo earlier worker effects; the caller still owns their recovery.

Check delivered content in your workflow. For a correction, create a new call
with a stable identity for that correction, previous content and findings as
inputs. Bound that loop in workflow code. Replay a cached result through its
check; delivery does not certify acceptance. Use fixed sibling snapshots for
checks whose answer depends on other parallel results.

The default is one physical launch. Larger limits and execution retry policy
must be explicit. `max_launches` is a total ceiling for this call history: raising
it from two to three after two spent launches permits at most one more launch.
The latest failure must also be selected by the currently supplied retry policy;
its backoff applies before resumed retries too. Raising timeout alone grants
neither allowance nor retry permission. A lower ceiling prevents launches once
reached, but does not invalidate history. Each launch reservation spends an
allowance before process start. Restarting does not refund consumed allowances.
`LaunchRecord.timeout_seconds`, `max_launches`, and `retry` retain the effective
controls recorded before process start. An unresolved launch
reservation blocks automatic work: establish that the old worker has stopped
before acknowledging it with `acknowledge_stopped`. This operation records
your assertion; it cannot prove that the old process is gone.

`run.parallel(requests, max_workers=...)` preserves result order and waits for
all submitted work to settle before reporting an exception. It does not create
a dependency scheduler. The limit applies to that group; callers creating other
threads or parallel groups remain responsible for their combined concurrency.

## Harness adapters

```python
from workjig.adapters import agy_worker, codex_worker, pi_worker

codex = codex_worker("gpt-6.1-sol", sandbox="workspace-write", effort="low")
pi = pi_worker(
    "gpt-6.1-sol",
    provider="openai",
    tools=("read", "write"),
    project_trust="no-approve",
)
agy = agy_worker("gemini-3.8-flash-low")
```

Pass one of these as the request's `worker`. Put the run records inside a
directory the worker can write; the adapter does not widen the sandbox for
record paths. Pi's project-trust option controls resource loading, not OS
permissions. A `claude_worker` command factory is also available; Claude Code
2.1.303 passed the bounded file-copy and reuse probe with requested model
`haiku`, reporting `claude-haiku-5-5` (see the harness evidence). `agy_worker`
configures Antigravity CLI (`agy`) 1.5.12 with `--output-format stream-json`,
`--disable-slash-commands`, optional `--mode` (`accept-edits`, `auto`, `plan`),
optional `--effort`, and `--sandbox` enabled by default (`sandbox=True`),
without `--dangerously-skip-permissions`.

Codex, Pi, Claude and Antigravity CLI (`agy`) have each passed a bounded real
file-copy probe and reuse check.
By default their raw event streams remain in each launch's `stdout.log`. The standalone
`parse_evidence(harness, stdout_text)` helper extracts available session and
model fields; it does not infer the served model from the requested model.
For Claude result records, it preserves the reported `modelUsage` mapping as
`model_usage`, including entries for multiple models when present.
`LaunchRecord.evidence` is a separate optional worker-file protocol, not an
automatic interpretation of those CLI streams.

The factories set `CommandWorker.output_protocol` to `codex-json`,
`claude-stream-json`, `pi-json`, or `agy-stream-json` for terminal-outcome
classification. A generic
`CommandWorker` defaults to `None` (exit status and the file protocol only);
its display `name` never selects a parser. Protocol selection is part of request
identity, so adding it to an existing request creates a different identity,
not a continuation of the old launch allowance.

### Optional raw launch logs

`Run(records, workdir, retain_logs=True)` is the default. Set `retain_logs=False`
to omit **both** `stdout.log` and `stderr.log` on new launches, including
failed or timed-out ones. Generic command streams are discarded; when a harness
protocol needs stdout, Workjig can spool it to a temporary file for terminal
classification and session-ID observation. Off means *not retained*, not *never
written to disk*. File outputs, execution outcomes, and native transcript policy
do not change. Consumers that parse a raw log after delivery must opt to retain
it. The setting is not work identity or allowance: replay neither relaunches a
worker nor creates or deletes prior logs. `examples/smoke_harness.py` exposes
`-v` / `--verbose` (default on) and `--no-verbose`; it controls file retention,
not streaming progress to the terminal.

## Outcomes and recovery

`CallResult.launches` contains immutable `LaunchRecord` values, one per launch
reservation. A record can exist even when the subprocess did not start.
`CallResult.status` and `LaunchRecord.status` are `LaunchStatus` members, a
string enum, so comparisons with plain strings such as `"delivered"` work.

`CallResult.failed_at_launch_limit` is true only when delivery failed and all
launch allowance was spent. False does not imply retryability: delivery may
have succeeded on the last launch, or a failure may be nonretryable despite
remaining allowance.

`CallResult.status` distinguishes `delivered`, `incomplete`, `worker_problem`,
`usage_limit`, `process_error`, `timeout`, `launch_error`, `protocol_error`,
and an explicitly acknowledged `interrupted` launch. `RetryPolicy(on=(...),
backoff_seconds=...)` selects which eligible execution outcomes to retry up
to `Request.max_launches`. `usage_limit` and `protocol_error` cannot be
selected for automatic retry.
A structured `usage_limit` problem or a recognized terminal harness quota,
billing or unrecovered rate-limit failure stops automatic relaunches. This also
handles Pi's exit-zero failures. Earlier errors followed by successful recovery
and ordinary stdout/stderr quotations do not trigger usage stops. The harness
owns short retries; after it gives up, the workflow decides what happens next.

To explicitly continue after a usage stop, supply its launch ID:

```python
from dataclasses import replace

continued = run.call(
    replace(request, max_launches=3),  # Total ceiling, not three extra launches.
    resume_usage_stop=stopped_result.launches[-1].launch_id,
)
```

Approval must name a committed `usage_limit` launch in this exact call history;
malformed, unknown, wrong-entry, or non-usage references raise `ValueError`.
Approval only overrides that stop when it is the latest launch and allowance
remains. The next reservation records the stop ID as
`LaunchRecord.resumed_usage_stop`. Repeating approval after continuation or
delivery is harmless; a later usage stop requires new approval. Other subsequent
failures follow ordinary retry policy. This does not enable `usage_limit` in
`RetryPolicy` or bypass unresolved-launch recovery. The current backoff also
applies before an approved usage continuation. Stop approval is available on
`run.call`, not `run.parallel`; continue usage-stopped calls individually.

For a selected output protocol, other terminal failures and malformed or missing
completion evidence become `process_error`, subject to explicit bounded retry.
Unknown provider errors are not universally recognized as quota stops. A generic
command has no log classifier. Existing committed outcomes replay unchanged.
See [ADR 002](../adr/002-classify-terminal-harness-failures.md) for supported signals
and the evidence boundaries.

If children outlive the worker leader, the runtime terminates the remaining
process group and returns `process_error`, even after exit zero with complete
outputs. A valid `usage_limit` problem or recognized terminal usage stop takes
precedence over that error; a timeout takes precedence over both.
Cleanup sends SIGTERM, waits up to 0.25 seconds while reaping the leader, then
sends SIGKILL if the group still exists.

`LaunchRecord.problem` holds either a worker's `unable` or `usage_limit` report,
or runtime details with kind `launch_error` or `operator_acknowledgment`.

An uncertain launch raises `UncertainLaunchError` before new work starts.
Inspect `run.unresolved_launches()`, establish that the old worker and its
children have stopped, then call `run.acknowledge_stopped(launch_id)`. To allow
a subsequent launch, the request supplied when continuing must have remaining
allowance and an explicit retry policy including `interrupted`. The caller may
increase the total ceiling and update retry policy to authorize continuation,
but already spent launches remain spent. Otherwise start a new run deliberately.

Corrupt records raise `RecordError`. `RecordError`, `UncertainLaunchError` and
`RunBusyError` all derive from `WorkjigError`, as does `UnresolvedSessionError`. A persistence failure can poison the
current controller: close and reopen it before inspection or recovery. Never
delete a reservation merely to suppress uncertainty.
An exception or keyboard interruption after reservation also requires explicit
recovery, even when process cleanup ran: cleanup alone does not commit a
terminal record. If `run.json` is missing from an existing record directory,
preserve that directory, confirm any old workers have stopped, and choose a
fresh directory and launch allowances deliberately.

## Inspecting records

Copy the manifest and its referenced `payloads/` together. For an active run,
read or copy the manifest **first**, then its payloads: once published they are
not replaced or collected by Workjig. `read_run()` verifies them under the
supplied record directory and returns the original strings. Missing, invalid
or hash-mismatched content raises `RecordError`; there is no fallback to launch
files or to a store in the original location. Include the launch tree separately
when retaining physical execution evidence. This format is for fresh runs.

`read_run(record_dir)` returns a validated, immutable `RunView` of the
committed `run.json` without taking the run lock, so status commands and
audits can read while a controller works. It writes nothing and creates no
file or directory. It returns `None` when there is no `run.json` (absent,
empty or only partly initialised directory) and raises `RecordError` for a
record `Run` would reject. It does not check the working directory;
`view.workdir` reports the recorded one.

```python
from pathlib import Path

from workjig import read_run

view = read_run(Path("run-records"))
if view is not None:
    for call in view.calls:
        print(call.call_id, call.request_hash, call.status, call.worker.name)
        for launch in call.launches:
            print(view.launch_files(launch).stdout)
```

`view.calls` holds one `RecordedCall` per call ID and work-material version,
in no meaningful order; select a version by `request_hash`. Each exposes the
recorded work material (`task`, `inputs`, `required_outputs`,
`optional_outputs`, `worker`), `status` by the same rule as
`CallResult.status`, committed `outputs` (empty unless delivered) and its
`launches`. Execution controls are on each `LaunchRecord`, not the call.

`view.launch_files(launch)` gives the `prompt`, `stdout`, `stderr`, `problem`,
`inputs` and `outputs` locations under the directory passed to `read_run`, so
they follow a copied or moved run. A location may not exist.

The view is one committed state and may be stale when used; files of a
`reserved` or `running` launch may still be changing.
`view.unresolved_launches()` returns those launches, including a live
controller's own active ones. `busy(record_dir)` asks separately whether a
controller holds the lock; the two answers can change between reads.
Acknowledging a stopped launch still requires an open `Run`.

Reading beside a live controller is exercised on Linux only. On Windows a
reader that has `run.json` open while the controller replaces it may make that
commit fail, which leaves the controller uncertain and its launch unresolved;
this has not been reproduced or ruled out. Until it is, read on Windows only
when no controller is working.

### Locate a native session

Pass `track_native_session=True` to `pi_worker`, `claude_worker`,
`codex_worker`, or `agy_worker` (default: false). A custom `CommandWorker` can
set the same flag and matching `output_protocol` when it runs that native CLI
with the same invocation shape: Pi and Claude take the prompt last; Codex runs
its own executable in JSON exec mode; Antigravity CLI (`agy`) runs in
`stream-json` print mode (`-p`). Do not supply competing session selection
flags. Tracking is part of the declared worker configuration, not inferred
from its display name. The assigned IDs themselves are per-launch, not work
identity. No Commonplace wrapper is needed.

```python
for launch in call.launches:
    session = view.native_session(launch, resolve=True)
    if session.status == "located":
        audit(session.path)  # caller policy; the file can still change or disappear
    else:
        print(session.status, session.session_id, session.reason)
```

`unassociated` means tracking was disabled or no trustworthy ID was captured.
`unavailable` means the ID is known but there is no safely resolved file.
`source` is `assigned` (Pi/Claude) or `observed` (Codex/Antigravity CLI) for a
known ID.
Without `resolve=True` the lookup starts no process. If the receipt holds an
ID whose transcript only the harness can report, it raises
`UnresolvedSessionError`, which carries `harness` and `session_id` and says
to pass `resolve=True`. Today that applies to Codex. Every lookup checks
current files, so a repeat call can see a new receipt or a deleted
transcript. It never writes to the run or takes its lock. Only inspect
trusted runs and stores. The caller decides whether to open the returned
path.

Tracked Pi uses a fresh directory under `PI_CODING_AGENT_SESSION_DIR`
(default: the agent's sessions root), outside the run. Claude reports its
path via a per-launch `SessionStart` hook; disabling hooks can leave a known
ID without a path. Workjig supplies the hook settings; combining with a
second explicit `--settings` is not supported. Codex supplies its own ID
in `thread.started`. The controller records that ID during execution.
For an ID-only Codex receipt, `resolve=True` makes one bounded
metadata query for the exact ID using the reader's `codex` on PATH, with
`CODEX_HOME` set to the parent of the pinned sessions directory. The helper
runs in a temporary working directory. The reader needs access to the
original store and a compatible CLI. With `resolve=True`, a redirected sessions directory is
unavailable. A missing rollout, failed lookup or
unconfirmed helper cleanup yields `unavailable` and preserves the known ID.
Antigravity CLI (`agy`) supplies its own `conversation_id` in the `init` event,
and its transcript path is pinned to
`<home>/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/transcript.jsonl`
(no helper subprocess is required; `--continue`, `-c`, `--conversation`, and
`--remote-agent` are rejected on tracked launches). A crash before `init`
leaves no ID; an early stop before `transcript.jsonl` exists leaves a known ID
with `status="unavailable"`.

The reservation pins the native store from the effective worker environment:
Pi’s sessions root, `CLAUDE_CONFIG_DIR/projects`, `CODEX_HOME/sessions`, or
`<home>/.gemini/antigravity-cli/brain`, with the harness defaults under the
worker home. A receipt cannot redirect
lookup outside that store, into the original or copied run, or to a file named
for a different ID. A harness using an unsupported storage layout returns
`unavailable`; the reader does not discover a replacement by scanning.

For shallow copies retain `run.json`, its sibling `payloads/` directory, and
`launches/<launch_id>/native-session.json` when present. Keep the native
session store where it is; copying this receipt does not copy the log.
Native-session lookup does not require retained stdout and does not change recovery policy.

## Boundaries

Separate output directories prevent accidental reuse of another launch's
files. They do not restrict a worker's filesystem or network access. The caller
configures permissions, credentials and the harness environment. External
effects are not transactional and may need caller-owned recovery.

See [the execution contract](design.md) for guarantees and limits,
[design rationale](design-rationale.md) and
[decision records](../adr/README.md) for the choices behind them, and
[harness evidence](harness-evidence.md) for adapter evidence and tested
versions. Support claims must distinguish deterministic tests from actual
model invocations.

## Examples and real-harness probes

The [ordinary-file example](../examples/README.md) demonstrates checks, corrections
and resume. To run all four real, credential-using probes deliberately:

```sh
uv run python examples/smoke_harness.py all /tmp/my-harness-probes
```

Defaults: Codex **gpt-6-luna**, Claude **haiku**, Pi **luna** (provider `openai`),
Antigravity CLI **gemini-3.8-flash-low**.
Install and authenticate each CLI first; models must be available through your
configured account. These are requested model names, not verified served-model
identities. The command runs sequentially, with separate `codex`, `claude`, `pi`
and `agy` subdirectories. Each probe allows one launch with a 180-second timeout,
checks an exact file copy and verifies cached reuse. It prints one JSON report
per harness, attempts all four even after a probe fails, and exits nonzero if
any probe fails. Keyboard interruption still stops the command.

Use a fresh root directory for a fresh set of model calls. Repeating a successful
command with the same directory reuses its committed results. Records and raw
logs remain under each harness's `records` directory. Probes stay manual, outside
normal pytest runs and GitHub CI. An explicitly opted-in
[real-harness test suite](../tests/README.md#real-harness-smoke-tests-manual-opt-in)
also exercises the README examples and these single-harness commands.

To run just one harness, or override its model:

```sh
uv run python examples/smoke_harness.py codex /tmp/my-codex-probe
uv run python examples/smoke_harness.py pi /tmp/my-pi-probe
uv run python examples/smoke_harness.py claude /tmp/my-claude-probe --model haiku
uv run python examples/smoke_harness.py agy /tmp/my-agy-probe --model gemini-3.8-flash-low
```

`--model` applies only to single-harness commands, not `all`.

### Shared run lock

Use `workjig.hold(record_dir)` to hold the run ownership lock while updating files beside the records; it creates the records directory when needed. `workjig.busy(record_dir)` probes whether another process currently holds it. Both use the same non-blocking lock as `Run`; `RunBusyError` is raised on contention. A probe briefly acquires the lock, so a concurrent opener may observe transient contention. The lock is released automatically when the context exits or the process terminates. Callers should take this run lock before a separate publication lock and never take the run lock while holding the publication lock.
