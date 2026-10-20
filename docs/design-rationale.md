# Design rationale

The [execution contract](design.md) describes what Workjig does. This document
explains why, which costs we accept, and what would justify revisiting a choice.
Commonplace is the primary consumer; its concrete workflow needs guide these
tradeoffs. Focused [decision records](../adr/README.md) capture implemented
choices and their alternatives as decisions are made or revisited.

## Ordinary Python owns workflow control

Branches, checks, parallel groups and correction loops belong in caller-written
Python. Workjig provides returning calls rather than a graph language or an
agent that advances a step program. Code can enforce launch accounting and
state transitions directly, while workers concentrate on the delegated task.

This keeps domain decisions with the workflow and avoids a second programming
model. It also means the caller must bound loops and total work: per-request
launch allowances do not bound a workflow that keeps creating new calls.

**Reevaluate when:** a consumer needs a shared aggregate execution limit that
cannot be adequately enforced by its workflow. That would motivate a specific
budget feature, not necessarily a new scheduler.

## Direct launches avoid an agent transmission layer

In [Treadle](https://github.com/zby/treadle), code determined pending requests
and an orchestrating agent carried prompts to harness workers and answers back.
The coordination and machinery for reporting all ready requests proved complex.
Workjig instead invokes headless harnesses directly.

Agent-steered delegation remains useful for conversational workflows. We are
trying it alongside Workjig in this repository. The practical choice is whether
a task benefits from flexible conversational coordination or explicit execution
rules and retained results.

**Reevaluate when:** actual tasks show that direct invocation lacks a useful
harness capability, or the cost of expressing a changing process in code
outweighs its execution benefits.

## Records retain outcomes, not the workflow's control position

A run records requests, launches and committed results. On restart, the caller
runs its workflow code again and matching calls return recorded outcomes. This
avoids serializing Python execution state or maintaining a durable scheduler.

Reliable replay requires the caller to reconstruct the intended calls. Fixed
inputs and prior results make this straightforward. Wall-clock values,
randomness, or parallel completion order incorporated into request material
can instead create new request versions with fresh allowances. Code between
calls runs again, including its external effects.

The request hash covers declared material, not the whole environment. Changes
to working files, harness versions or ambient instructions such as `AGENTS.md`
are not detected automatically. Callers can include relevant content or a
fingerprint in `inputs`, and must keep the environment suitable for reuse.

**Reevaluate when:** consumer workflows repeatedly need to resume control state
or track environmental dependencies that they cannot reliably reconstruct.

## Launch reservations favor conservative accounting

The runtime commits a reservation before starting a process. A crash between
those actions can spend allowance without starting a worker. Reserving after
process start would risk the opposite: an executed worker whose allowance was
never recorded.

We accept the unused allowance rather than risk duplicate work through refunded
budgets. Unfinished reservations require explicit acknowledgment that the old
worker has stopped. A PID or leftover output cannot establish that safely.

**Reevaluate when:** a concrete deployment supplies stronger worker-ownership
and termination evidence that could support a bounded recovery procedure.
Automatic recovery would need that evidence, not just more retries.

## Delivery and acceptance answer different questions

The runtime checks whether execution delivered the requested files. The
workflow checks whether their contents satisfy its purpose. Keeping those
judgments separate allows the same runtime to serve different domains and
lets resumed workflows rerun acceptance checks against the intended inputs.

Execution retries and content corrections therefore have separate controls.
Relaunching can help with transient execution failures; it does not repair an
ambiguous task or inadequate specification. Retrying `worker_problem` is an
explicit caller choice and may simply repeat the same inability to proceed.
Corrections should carry findings and previous content in a new request.

Likewise, `max_launches` is not a sampling count: delivery ends execution retries,
and a matching call reuses its recorded result. Independent samples require
distinct call IDs and caller-owned comparison logic.

**Reevaluate when:** a recurring consumer need warrants a reusable check or
correction helper. Keep its acceptance policy visible rather than silently
adding a correction loop to the runtime.

## Fresh launch files provide traceability, not isolation

Each launch has separate inputs, outputs and logs. Failed files cannot become
a later launch's result, and retained evidence ties an outcome to its producer.
This makes launch boundaries useful inspection points without building a
sandbox manager.

Workers still share the run's working directory. Parallel workers can observe
or modify each other's ambient files, so callers must arrange independent work
and fixed input snapshots where needed. Separate output paths alone do not
provide that independence.

Permissions also matter for retained records: a worker that can write the
record directory can alter it. Atomic commitment and content hashes protect
against partial writes and detect some corruption; they do not authenticate
records against a writer able to change both content and hashes.

**Reevaluate when:** consumer tasks need enforced sibling isolation or workers
must not be able to modify committed records. Those needs would justify a
concrete directory/permission design or workdir isolation facility.

## A local whole-run record keeps persistence small

One controller owns a run under an exclusive lock. State is written as a
whole JSON document with atomic replacement. Completion and delivered content
are committed together, keeping reuse and recovery reasoning local.

The cost is rewriting accumulated request and result data on transitions.
This favors simple local runs over high write throughput or distributed
controllers.

**Reevaluate when:** measurements on intended workloads show record writes
limiting required throughput or latency, or consumers need multiple controllers.
Choose a storage change from those requirements rather than anticipated scale.

## Harness evidence remains explicit

Generic execution uses process outcomes and the file protocol. Harness-specific
stdout parsing is a separate helper; requested model identity is not treated
as proof of the model actually served. Raw logs remain available for inspection.

This keeps the generic launcher independent of changing CLI event schemas.
The tradeoff is that consumers needing reported model identity or harness error
flags must parse the logs themselves. A `delivered` outcome does not summarize
every event in those logs.

Usage-limit detection currently combines a structured worker problem with a
case-insensitive log phrase on nonzero exit. The phrase can occur in task or
tool content, so it can suppress retries for an unrelated failure. The
worker-written problem is also a report, not independently authenticated
provider evidence.

**Reevaluate when:** consumer provenance requirements or observed
misclassification justify integrating specific structured harness events into
records or outcome classification.

## Launch vocabulary

`LaunchRecord` and `CallResult.launches` describe physical launch reservations.
Commonplace uses “attempt” for a larger unit that includes content checking and
correction. Distinct names keep those two budgets and lifecycles clear at the
integration boundary. The persisted JSON field remains `attempts` so existing
run records keep their format. See [ADR 001](../adr/001-name-physical-launch-records.md).

## Consumer feedback to evaluate

Commonplace's integration review supplied explanations and concerns for this
rationale. Its proposed changes remain choices to evaluate against consumer
needs:

- Narrow usage-limit recognition and attach parsed harness evidence to records.
- Separate writable launch files from committed records and support isolated
  working directories where parallel tasks need them.
- Add a run-level allowance or a convention for reporting assumptions alongside
  delivered work.

For now, callers can express purpose and acceptance criteria in `task`, include
ambient dependencies in `inputs`, and request an assumptions file among their
outputs. Integration experience should establish which of these responsibilities
benefit from library support.
