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
the requested file result. Command arguments are passed without a shell.

The generic command adapter also accepts `{input_dir}`, `{output_dir}`,
`{problem_file}`, `{launch_dir}` and `{workdir}` path tokens. Named inputs and
outputs are single filenames. Each physical launch has fresh paths, including
when a call retries.

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
from workjig.adapters import codex_worker, pi_worker

codex = codex_worker("gpt-6.1-sol", sandbox="workspace-write", effort="low")
pi = pi_worker(
    "gpt-6.1-sol",
    provider="openai",
    tools=("read", "write"),
    project_trust="no-approve",
)
```

Pass one of these as the request's `worker`. Put the run records inside a
directory the worker can write; the adapter does not widen the sandbox for
record paths. Pi's project-trust option controls resource loading, not OS
permissions. A `claude_worker` command factory is also available; Claude Code
2.1.303 passed the bounded file-copy and reuse probe with requested model
`haiku`, reporting `claude-haiku-5-5` (see the harness evidence).

Codex and Pi have each passed a bounded real file-copy probe and reuse check.
Their raw event streams remain in each launch's `stdout.log`. The standalone
`parse_evidence(harness, stdout_text)` helper extracts available session and
model fields; it does not infer the served model from the requested model.
For Claude result records, it preserves the reported `modelUsage` mapping as
`model_usage`, including entries for multiple models when present.
`LaunchRecord.evidence` is a separate optional worker-file protocol, not an
automatic interpretation of those CLI streams.

The factories set `CommandWorker.output_protocol` to `codex-json`,
`claude-stream-json`, or `pi-json` for terminal-outcome classification. A generic
`CommandWorker` defaults to `None` (exit status and the file protocol only);
its display `name` never selects a parser. Protocol selection is part of request
identity, so adding it to an existing request creates a different identity,
not a continuation of the old launch allowance.

## Outcomes and recovery

`CallResult.launches` contains immutable `LaunchRecord` values, one per launch
reservation. A record can exist even when the subprocess did not start.

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

Corrupt records raise `RecordError`. A persistence failure can poison the
current controller: close and reopen it before inspection or recovery. Never
delete a reservation merely to suppress uncertainty.
An exception or keyboard interruption after reservation also requires explicit
recovery, even when process cleanup ran: cleanup alone does not commit a
terminal record. If `run.json` is missing from an existing record directory,
preserve that directory, confirm any old workers have stopped, and choose a
fresh directory and launch allowances deliberately.

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
and resume. To run all three real, credential-using probes deliberately:

```sh
uv run python examples/smoke_harness.py all /tmp/my-harness-probes
```

Defaults: Codex **gpt-6-luna**, Claude **haiku**, Pi **luna** (provider `openai`).
Install and authenticate each CLI first; models must be available through your
configured account. These are requested model names, not verified served-model
identities. The command runs sequentially, with separate `codex`, `claude` and
`pi` subdirectories. Each probe allows one launch with a 180-second timeout,
checks an exact file copy and verifies cached reuse. It prints one JSON report
per harness, attempts all three even after a probe fails, and exits nonzero if
any probe fails. Keyboard interruption still stops the command.

Use a fresh root directory for a fresh set of model calls. Repeating a successful
command with the same directory reuses its committed results. Records and raw
logs remain under each harness's `records` directory. Probes stay manual, outside
pytest and GitHub CI.

To run just one harness, or override its model:

```sh
uv run python examples/smoke_harness.py codex /tmp/my-codex-probe
uv run python examples/smoke_harness.py pi /tmp/my-pi-probe
uv run python examples/smoke_harness.py claude /tmp/my-claude-probe --model haiku
```

`--model` applies only to single-harness commands, not `all`.

### Shared run lock

Use `workjig.hold(record_dir)` to hold the run ownership lock while updating files beside the records; it creates the records directory when needed. `workjig.busy(record_dir)` probes whether another process currently holds it. Both use the same non-blocking lock as `Run`; `RunBusyError` is raised on contention. A probe briefly acquires the lock, so a concurrent opener may observe transient contention. The lock is released automatically when the context exits or the process terminates. Callers should take this run lock before a separate publication lock and never take the run lock while holding the publication lock.
