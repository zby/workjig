# 003 — Separate execution controls from work identity

Status: accepted
Decision date: 2026-10-20
Recorded: 2026-10-20

## Context

Commonplace needs to increase timeouts without relaunching delivered work, extend
failed calls' launch ceilings, and explicitly continue after resolving usage
limits. Previously, timeout, ceiling, and retry policy were part of request
identity. Changing any of them created a separate history and fresh allowance,
rather than continuing the existing work.

The [review proposal](evidence/execution-policy-and-continuation-proposal.md)
explored these changes. The maintainer confirmed there are no saved runs to
preserve, backward compatibility is not required, and run migration across code
changes is out of scope. Resume supports recovery with unchanged workflow/runtime
code and a compatible environment, not upgrades.

## Decision

Exclude `timeout_seconds`, `max_launches`, and `retry` from work identity. Keep
all other declared material identity-bearing. Matching delivered results replay
under changed execution controls, with workflow checks still caller-owned.
`CallResult.request_hash` now hashes work material, not execution controls.

Evaluate the current call's controls against its existing history. The ceiling
is total reservations, not additional launches: increasing two to three permits
one further launch after two were spent. Retry eligibility and backoff are
independent of the ceiling. Increasing timeout alone authorizes no retry. Lower
ceilings stop further work when reached but do not invalidate historical counts.
`failed_at_launch_limit` uses the currently supplied ceiling.

Persist effective timeout, ceiling, and retry policy with every reservation
before process start. Validate each launch against its recorded ceiling and
continuation permission rather than the controls supplied today.

Keep usage stops outside ordinary retry policy. `Run.call(request,
resume_usage_stop=launch_id)` explicitly approves the named usage stop in that
exact history. Approval overrides only the latest stop, while allowance remains.
The next reservation records `resumed_usage_stop`; once reserved, the approved
stop is no longer latest. Repeating approval after continuation or delivery is
harmless. A new usage stop requires new approval. Other subsequent failures
follow ordinary bounded retry policy. Current backoff also applies before an
approved continuation. Invalid, unknown, wrong-entry, and non-usage references
raise `ValueError` once unresolved-launch recovery permits inspecting the call.

Unresolved reservations still require establishing worker termination and
explicit acknowledgment. No allowance is refunded and no unrecorded output is
adopted. Fresh launch paths, process cleanup, and parallel settlement are
unchanged. `protocol_error` remains nonretryable.

Use record format version 2 and reject older formats. Do not provide migration
or a legacy mode. Keep the stored `attempts` field name; renaming it is unrelated
to this change. ADR 001's historical format-preservation rationale does not imply
a continuing compatibility commitment.

## Considered alternatives

- **Keep execution controls in identity:** avoids changing the record contract,
  but cannot preserve delivered work while updating controls and gives policy
  variants separate budgets. Rejected for Commonplace's use case.
- **Reset the budget or treat an increased ceiling as extra launches:** could
  replenish allowance on repeated calls or restart. Rejected; spent reservations
  always count against the supplied total.
- **Separate durable budget grants or pending approval ledger:** adds state and
  crash/replay accounting. Unnecessary: the supplied total ceiling plus durable
  reservations tracks budget, and the next reservation records stop approval.
- **Let a longer timeout imply retry permission:** conflates execution settings
  with authorization. Require explicit ceiling and eligible retry outcome.
- **Permanent usage-resume boolean or `usage_limit` in retry policy:** could
  relaunch repeatedly while usage remains blocked. Bind permission to one stop.
- **Exactly one launch after approval, even for other retryable failures:** adds
  special retry semantics. Clear only the named usage stop, then use ordinary
  bounded retries; any later usage stop blocks again.
- **Separate continuation operation:** not needed for current callers. Use
  per-call controls and one stop-ID argument rather than a second lifecycle API.
- **Legacy mode or merging policy-variant histories:** adds unsupported migration
  work and ambiguous result/budget selection. No saved runs require it.

## Consequences

Execution authorization can evolve without discarding delivered answers or
launch evidence. Changing work material still creates a separate history.
An answer delivered under old controls is not a promise about the hypothetical
answer under new controls; callers wanting new work choose a new identity/run.
External effects can repeat, and their recovery remains caller-owned.

The record format and public hash meaning change without compatibility shims.
Supporting [evidence](evidence/execution-controls/README.md) records deterministic
checks, independent verification, and pre/post source-size measurements. These
checks do not establish new real-harness support claims.

## Reevaluate when

Commonplace demonstrates a need not covered by per-call controls and stop-ID
approval, or deterministic recovery tests reveal ambiguous authorization.
