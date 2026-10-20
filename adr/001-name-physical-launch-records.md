# 001 — Name physical launch records explicitly

Status: accepted
Decision date: 2026-10-20
Recorded: 2026-10-20

## Context

Workjig records each physical launch reservation, including reservations for
which no process started. Commonplace uses “attempt” for a larger unit of work
that includes content checking and correction. Using the same word at both
levels obscures which lifecycle and budget a caller is discussing. The
[consumer handoff](../docs/handoff-from-commonplace-2026-10-20.md#one-vocabulary-point-from-the-consumer-side)
identified this integration problem.

## Decision

Expose `LaunchRecord` and `CallResult.launches` in the Python API. Callers use
these names when inspecting execution evidence and spent launch reservations.
They align with `launch_id`, `launch_dir`, `max_launches`, and
`unresolved_launches()`.

Keep the version-1 persisted JSON field `attempts`. The public vocabulary change
does not require a storage migration. This records the implemented choice
retrospectively.

## Considered alternatives

**Keep `Attempt` and `CallResult.attempts`.** “Attempt” can reasonably include an
effort to start a process. However, the consumer's different meaning creates
avoidable ambiguity at the integration boundary.

**Use `result.launch_records`.** This is explicit but unnecessarily repeats the
record representation. `result.launches` reads naturally and matches the other
launch-oriented API names.

**Rename stored fields too.** Consistent spelling across Python and JSON would
be tidier, but would introduce migration work unrelated to the consumer-facing
problem. Existing records should remain readable without rewriting them.

## Consequences

The runtime and consumer can describe physical launches and workflow attempts
without conflating their budgets. `LaunchRecord` also accommodates a reservation
that never reached process start.

Python consumers must use the new names; no compatibility aliases were added.
Persisted records retain their format and existing execution/recovery behavior.
Internal storage helpers can continue to use the old field terminology.

The rename was checked by the functional suite and a separate verification
session that compared old and new codecs across all 11 lifecycle statuses.
This supports storage compatibility; it does not establish new harness behavior.

## Reevaluate when

Consumer integration reveals another concrete ambiguity in the launch lifecycle.
Revisit the stored field name only if a separately justified record-format change
makes migration worthwhile.
