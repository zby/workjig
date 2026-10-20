# Workjig

A standalone Python runtime for workflows that run headless LLM coding
harnesses such as Codex, Claude Code and Pi as subprocess workers.
Workflow code owns branches, checks and correction loops. The runtime owns
subprocess launches, separate output paths, bounded execution and run records.

## Purpose and scope

Workjig's primary goal is to support Commonplace as the runtime foundation
of its workflow engine. It is extracted into a separate package so it can be
maintained independently. The aim is to give agents a smaller, focused codebase
and a clear contract to work against, helping them build a better package.

Commonplace's workflow needs drive development priorities. Workjig owns the
reusable execution machinery; Commonplace owns its workflows, domain rules,
checks and correction policies. Reuse in other projects is a secondary
opportunity: keep the package independently usable without adding abstractions
for hypothetical consumers.

Workjig does not import Commonplace or require a Git checkout. Its tests and
examples run independently of Commonplace. Initial platform: Linux, Python
3.11 or newer.

## When to use Workjig

We are trying two ways of running multi-agent work in this repository:

- **Agent-steered:** an orchestrating agent delegates to sub-agents, interprets
  their replies, and decides the next step. Our
  [lightweight review workflow](instructions/lightweight-review.md) works this
  way, through direct conversations.
- **Code-steered:** Python controls branches, checks and correction loops.
  Workjig launches headless harnesses as workers, retains results, bounds
  execution and accounts for launch allowances across restarts.

Agent steering looks useful for exploratory work where the process changes
as we learn. Workjig is useful when execution rules belong in code, completed
calls should be reused after a restart, and interrupted work needs explicit
recovery. We are dogfooding both to compare setup effort, ease of change,
result quality, execution cost and recovery.

We also tried a third approach in [Treadle](https://github.com/zby/treadle):
code controlled the workflow, while an orchestrating agent carried prompts
to harness workers and submitted answers back. That transmission layer and
the machinery for determining which requests were ready proved complex.
We switched to direct headless harness execution to simplify code-steered
workflows.

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
result. Both the stable call identity and declared request must match.
Different identities request independent work even when their prompts match.

The caller must keep the surrounding environment compatible with reuse.
Changing ambient files, tools or workflow logic may require a new run. The
runtime does not infer those dependencies, pin code or manage environments.

Check delivered content in your workflow. For a correction, create a new call
with a stable identity for that correction, previous content and findings as
inputs. Bound that loop in workflow code. Replay a cached result through its
check; delivery does not certify acceptance. Use fixed sibling snapshots for
checks whose answer depends on other parallel results.

The default is one physical launch. Larger limits and execution retry policy
must be explicit. Each launch reservation spends an allowance before process
start. Restarting does not refund consumed allowances. An unresolved launch
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

For a selected output protocol, other terminal failures and malformed or missing
completion evidence become `process_error`, subject to explicit bounded retry.
Unknown provider errors are not universally recognized as quota stops. A generic
command has no log classifier. Existing committed outcomes replay unchanged.
See [ADR 002](adr/002-classify-terminal-harness-failures.md) for supported signals
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
a subsequent launch, the original request must have remaining allowance and
an explicit retry policy including `interrupted`. Otherwise start a new run
deliberately. Changing execution policy or limits changes declared request
identity; it is not a way to continue the original call's budget.

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

See [the execution contract](docs/design.md) for guarantees and limits,
[design rationale](docs/design-rationale.md) and
[decision records](adr/README.md) for the choices behind them, and
[harness evidence](docs/harness-evidence.md) for adapter evidence and tested
versions. Support claims must distinguish deterministic tests from actual
model invocations.

## Development

Run commands from this package directory:

```sh
uv sync --locked
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv build
```

Parser and classification tests exercise functions and files directly. Execution
and recovery tests use real subprocesses with deterministic worker fixtures.
Neither requires model credentials. Real-harness probes are separate, bounded operations.
Building creates local distribution artifacts; publishing is a separate step.

The [ordinary-file example](examples/README.md) demonstrates checks, corrections
and resume. To run a real, credential-using probe deliberately:

```sh
uv run python examples/smoke_harness.py codex /tmp/my-codex-probe --model gpt-6.1-sol
uv run python examples/smoke_harness.py pi /tmp/my-pi-probe --model gpt-6.1-sol
uv run python examples/smoke_harness.py claude /tmp/my-claude-probe --model haiku
```
