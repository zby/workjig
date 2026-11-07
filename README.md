# Workjig

A Python workflow runtime that orchestrates headless LLM harnesses.

Run Claude Code, Codex and Pi as subprocess workers. You write the workflow
in Python; Workjig handles launches, separate output
paths, bounded execution and durable run records.

## Why Workjig?

**Use existing harnesses, not another model API integration.** Workjig delegates
work to coding harnesses through their command-line interfaces, using their
tools, configuration and authentication. An important motivation is letting
people use Claude Code with an eligible Anthropic subscription rather than
requiring separate API billing. Availability depends on the harness, account,
provider terms and usage limits; Workjig does not supply or bypass access.

**Keep the whole workflow in Python.** Branches, parallel work, checks and
correction loops are ordinary Python, not a separate graph or workflow language.
Unlike libraries centered on direct model API calls, Workjig treats the harness
as the worker. [mdflow](https://github.com/johnlindquist/mdflow) is another
harness-based approach: it turns Markdown prompts into executable CLI commands
that can be composed through pipes. For Commonplace, where most of the system
is already Python, keeping workflow control in Python lets us reuse its code
and tests without introducing another way to express control flow.

**Keep the runtime small and independent.** Workjig's primary purpose is to
support [Commonplace](https://github.com/zby/commonplace) as the foundation of
its workflow engine. A separate package gives agents a focused codebase and a
clear execution contract. Commonplace drives priorities, but its domain rules
stay outside the runtime. Workjig requires neither Commonplace nor Git.
Platforms: Linux is the primary platform, Python 3.11 or newer. Windows
support is limited: workers are owned through job objects
([ADR 004](adr/004-own-windows-workers-with-job-objects.md)), the
deterministic test suite runs on Windows in CI, and no real harness has yet
been probed there. Windows kills a timed-out worker without a cooperative
phase, and controller death kills the worker's job. See the
[execution contract](docs/design.md) for the platform clauses.

### Compatibility

Workjig is in early development. We do not maintain backward
compatibility for the public API or persisted run-record format. Changes may
require updating callers and starting fresh run directories; no migration of
older records is promised.

### Code-steered or agent-steered?

Agent-led delegation is useful for exploratory work where the process changes
as we learn; our [lightweight review workflow](instructions/lightweight-review.md)
uses that approach. Workjig is for code-steered work: execution rules live in
Python, completed calls can be reused after restart, and interruptions require
explicit recovery. We are trying both, not claiming one is always better.

In [Treadle](https://github.com/zby/treadle), code controlled the workflow but an
agent carried prompts and answers between it and workers. Workjig launches
harnesses directly to remove that transmission and coordination layer.

The supporting research lives in the [Commonplace KB](https://zby.github.io/commonplace/),
including notes on [orchestration design dimensions](https://github.com/zby/commonplace/blob/main/kb/notes/agent-orchestration-occupies-a-multi-dimensional-design-space.md)
and [coordination guarantees versus channels](https://github.com/zby/commonplace/blob/main/kb/notes/agent-orchestration-needs-coordination-guarantees-not-just.md).
These inform the design; they are not evidence of Workjig's tested behavior.
Our [design rationale](docs/design-rationale.md) explains the local tradeoffs.

## A call

Install from PyPI with `pip install workjig` or `uv add workjig`.
Install and authenticate [Claude Code](https://code.claude.com/docs/en/overview)
separately; the `claude` command must be available on your `PATH`. Running this
example makes a real model call and consumes your account's usage allowance.

```python
from pathlib import Path

from workjig import Request, Run
from workjig.adapters import claude_worker

worker = claude_worker(
    "haiku",
    allowed_tools=("Read", "Write"),
    permission_mode="acceptEdits",
)
request = Request(
    call_id="summarize-v1",
    task="Read the supplied source and write the requested summary file.",
    inputs={"source.txt": "Material to summarize."},
    required_outputs=("summary.txt",),
    worker=worker,
    timeout_seconds=180,
)

with Run(Path("run-records"), Path.cwd()) as run:
    result = run.call(request)
    if result.status == "delivered":
        print(result.outputs["summary.txt"])
    else:
        print(result.status)
```

`claude_worker` configures a noninteractive Claude Code command; it does not
launch anything until `run.call(request)`. Workjig writes the task and absolute
input/output paths into a prompt file, then asks Claude Code to read it and
perform the task. The returned `summary.txt` is the file Claude Code writes;
standard output is retained as execution evidence, not used as the answer.
The tool allowlist and permission mode permit file edits; they are not a sandbox.
Run this example in a working directory you are comfortable letting the harness
access. Repeating it with the same records and declared work reuses the delivery
rather than making another model call.

To use Codex or Pi instead, replace the `worker` definition before constructing
the request (install and authenticate the corresponding CLI first):

```python
from workjig.adapters import codex_worker, pi_worker

# Codex CLI: choose a model available to your account.
worker = codex_worker("gpt-6.1-sol", sandbox="workspace-write", effort="low")

# Or Pi CLI: choose a provider and model available to your account.
worker = pi_worker(
    "gpt-6.1-sol",
    provider="openai",
    tools=("read", "write"),
    project_trust="no-approve",
)
```

These factories construct command configurations, not model clients. See
[usage and adapter configuration](docs/usage.md) for details. For credential-free
tests, a generic `CommandWorker` can launch a deterministic Python script;
see the [simulated harness example](examples/README.md#script-backed-simulated-harness).
The [ordinary-file example](examples/README.md#ordinary-file-workflow) demonstrates
checks, corrections and resume without a model.

## Guarantees and boundaries

- **Reuse:** reopening a run reuses committed results only when the stable call
  identity and declared work match. Timeout, launch ceiling, and retry policy
  are not part of work identity. Ambient environment changes are not detected
  automatically.
- **Bounded execution:** one launch by default; retries and larger allowances
  are explicit. Launches are reserved durably, and restart does not refund them.
  A caller may raise the total ceiling to continue eligible failed work; usage
  stops require explicit approval of the particular stop.
- **Explicit recovery:** unresolved launches block automatic work until you
  establish that the old worker has stopped and acknowledge it.
- **Delivery is not acceptance:** your workflow checks content and bounds
  correction loops. Reused results still need those checks.
- **Separate files are not a sandbox:** callers own permissions, credentials,
  environment compatibility and recovery of external effects.

## Documentation

- [Usage and recovery](docs/usage.md) — adapters, retries, outcomes, parallel calls
  and recovery procedures.
- [Execution contract](docs/design.md) — guarantees and limits.
- [Design rationale](docs/design-rationale.md) and [decision records](adr/README.md)
  — reasons, alternatives and tradeoffs.
- [Harness evidence](docs/harness-evidence.md) — tested adapters and versions,
  distinguishing deterministic tests from actual model invocations.

## Development

Run from this package directory:

```sh
uv sync --locked
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv build
```

Normal tests use direct parser checks and deterministic subprocess workers;
they do not call model APIs or require credentials. See [test organization](tests/README.md)
for selective runs and GitHub CI. Real-harness probes remain
[manual operations](docs/usage.md#examples-and-real-harness-probes), outside CI.
Building creates local artifacts; publishing is a separate step.

## License

[MIT](LICENSE).
