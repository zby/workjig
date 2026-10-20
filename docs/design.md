# Execution contract

Workjig runs caller-written Python workflows through subprocess
workers. It does not schedule a declarative graph or ask an agent to advance
a step program. Calls return delivered files and execution evidence; the
workflow decides whether their contents are acceptable and what to do next.

See [design rationale](design-rationale.md) for the reasons behind these
boundaries, accepted tradeoffs and conditions for revisiting them.

## Purpose

The primary use case is the runtime foundation for Commonplace's workflow
engine. Maintaining Workjig as a separate package gives agents a focused
codebase and an explicit execution contract, with the aim of improving package
quality. Commonplace's needs guide priorities while its workflow and domain
policy remain in Commonplace.

Independent maintenance requires that Workjig can be used and tested without
Commonplace. That boundary also permits reuse in other projects; speculative
generality is not a goal.

## Boundaries

The caller supplies an existing working directory, task inputs, output names,
worker configuration and limits. The runtime creates separate launch files,
collects results, bounds launches and retains records. It needs neither Git
nor Commonplace.

The caller owns environment preparation, sandbox permissions, credentials,
external effects and domain validation. A fresh output directory is not a
sandbox. A retained result is not evidence that the environment is unchanged
or that the output passed a current check.

## Identity and persistence

A run owns its record directory under an exclusive controller lock. Within a
run, the caller gives each logical call a stable identity. Reuse also requires
matching declared work material: task, inputs, required and optional outputs,
and worker configuration. Timeout, total launch ceiling, and retry policy are
execution controls, excluded from identity. Changing them replays delivered
results and affects only future launches. Two independent calls with identical
prompts have different identities. Changes to supplied content must not reuse
the earlier result.

Every physical launch receives a fresh directory and its own prompt, inputs
and output paths. Files left by a failed launch are evidence of that launch;
they cannot become another launch's result. The runtime records returned
content with the invocation that produced it. Model or session identity that
the harness does not establish remains unknown.

Only complete, atomically committed result records are reusable. Workflow
acceptance is separate: a resumed workflow reruns its checks against the
intended input snapshot. It must not use changing sibling output state as an
implicit input to a parallel check.

Resume is an exceptional recovery path, not a way to carry runs across code
changes. Reuse assumes unchanged workflow and runtime code and a compatible
surrounding environment. If either codebase changes, start a fresh run directory
and rerun the whole workflow. Run migration across code changes is out of scope,
not a deferred compatibility feature. The caller is responsible for deciding
whether reuse is appropriate; Workjig does not detect all incompatible changes.
A fresh run does not undo earlier external effects.

## Budgets and interruption

A durable reservation precedes each process start and spends one launch
allowance. A reservation can therefore count a process that did not actually
start if the controller stopped between reservation and launch. This is a
conservative budget, not an exact process census. Retrying cannot reset spent
allowances by restarting the controller. The caller may explicitly raise the
total ceiling to permit more launches; all prior reservations remain spent.
Current retry selection and backoff apply to the latest recorded failure.
A lower ceiling does not invalidate history. Each reservation records its
effective timeout, ceiling, retry policy, and any approved usage-stop ID.

An unfinished reservation makes prior execution uncertain. Automatic resume
must stop until the operator establishes that the old worker is no longer
running and explicitly acknowledges that uncertainty. A PID alone is not
sufficient evidence: it may have been reused. Recovery does not refund the
reservation or adopt unrecorded output files.

Handled timeouts terminate the owned process group, including children whose
leader has already exited. Cleanup sends SIGTERM and gives the group up to
0.25 seconds to exit, reaping the leader while checking for remaining members,
then sends SIGKILL if needed. A child outliving a normally exited leader causes
`process_error`, even if that leader exited zero and wrote complete outputs.
Usage-limit classification takes precedence over this error as described below.
Independent parallel calls settle before group
failure is reported. Abrupt process death cannot promise that children have
stopped; that case follows the explicit recovery rule above.

An exception or keyboard interruption after reservation leaves the launch
unresolved even if cleanup ran. Explicit acknowledgment is still required;
cleanup without a committed completion record does not establish settlement.
An existing record directory without `run.json` is rejected, including after
an interrupted first open. Preserve it for inspection, confirm any old workers
have stopped, and deliberately choose a fresh run directory and allowances.

Retries of execution failures are bounded and explicit. Content corrections
are caller-owned calls carrying feedback and previous results. A bounded
workflow loop controls their allowance. These two budgets must not be
conflated.

Adapter factories explicitly declare their stdout protocol. The runtime selects
the terminal outcome before classifying its cause: intermediate retry errors
and ordinary task text do not establish failure. A recognized terminal quota,
billing or unrecovered rate-limit failure becomes nonretryable `usage_limit`,
including at exit zero. The harness handles short retries; after it gives up,
the workflow decides whether and when to try again. To continue the same history,
the caller explicitly approves the latest usage-stop launch ID and supplies
remaining allowance. The next reservation durably records that approval.
Repeating it after continuation or delivery is harmless, but cannot approve a
later usage stop. Ordinary retry rules govern subsequent non-usage failures.
Approval never bypasses unresolved-launch recovery.

A valid structured `usage_limit` problem remains a separate explicit stop signal.
Either kind of usage stop takes precedence over other completed-process failures,
including lingering children; a handled timeout remains `timeout`.
Other recognized terminal failures, malformed or missing completion evidence, and
nonzero exits become `process_error` unless a worker problem, invalid problem
file or cleanup failure already determines the result. A successful terminal
event still requires exit zero, clean process ownership and the declared files.

Generic commands declare no output protocol and use exit status and the file
protocol only. Stdout/stderr phrase scanning is not used. Protocol selection is
declared request material, not inferred from the worker's display name. Existing
committed outcomes are not reclassified. See [ADR 002](../adr/002-classify-terminal-harness-failures.md)
for the evidence, supported signals and accepted fallback limits.

## Limits

No exactly-once guarantee covers worker side effects. The caller must make
external effects repeatable or supply an appropriate recovery policy.
Cross-run cache reuse, process migration, distributed controllers, environment
pinning and general automatic recovery are outside the initial scope.

The public API and tested support claims are recorded in the package README
and harness evidence. This document states the intended contract; acceptance
tests and release evidence establish which parts the implementation meets.
