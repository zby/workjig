# Workjig

A standalone Python runtime for workflows that run headless LLM coding
harnesses such as Claude Code, Codex and Pi as subprocess workers.
You write the workflow in Python; Workjig handles launches, separate output
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
Initial platform: Linux, Python 3.11 or newer.

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

```python
import sys
from pathlib import Path

from workjig import CommandWorker, Request, Run

worker = CommandWorker(argv=(sys.executable, str(Path("worker.py").resolve()), "{prompt}"))
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

Supply your own `worker.py`: it receives a generated prompt with absolute input
and output paths, reads the inputs and writes the named files. Standard output
is execution evidence, not the file result. For real harnesses, use the
`claude_worker`, `codex_worker` or `pi_worker` factories instead of a generic
`CommandWorker`; see [usage and adapter configuration](docs/usage.md).
The [ordinary-file example](examples/README.md) provides a runnable workflow
with checks, corrections and resume.

## Guarantees and boundaries

- **Reuse:** reopening a run reuses committed results only when the stable call
  identity and declared request match. Ambient environment changes are not
  detected automatically.
- **Bounded execution:** one launch by default; retries and larger allowances
  are explicit. Launches are reserved durably, and restart does not refund them.
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
they do not call model APIs or require credentials. Real-harness probes are
[separate, deliberate operations](docs/usage.md#examples-and-real-harness-probes).
Building creates local artifacts; publishing is a separate step.
