# Proposal: execution controls and failed-call continuation

Status: historical review proposal; implemented by
[ADR 003](../003-separate-execution-controls-from-work-identity.md).
The text below preserves the proposal reviewed before implementation; current
behavior is documented in the execution contract and usage guide.

Commonplace wants to change execution controls without losing delivered answers,
and explicitly continue failed calls without losing their launch history.
Current [usage](../../docs/usage.md) and the
[execution contract](../../docs/design.md) remain authoritative.

## 1. Separate work identity from execution controls

Remove `timeout_seconds`, `max_launches`, and `retry` from request identity.
Keep all other current request material identity-bearing: task, inputs, required
and optional outputs, and worker configuration (name, arguments, environment,
evidence-file configuration, and output protocol).

For the same `call_id` and work material:

- Replay a committed delivered result, regardless of changed execution controls.
- Apply the supplied controls only to future launches.
- Record effective controls in each durable reservation before process start.
- Preserve previous launch settings and outcomes unchanged.

Changing work material still creates a separate history under that `call_id`,
as today. Changing execution controls no longer creates a fresh budget.
`CallResult.request_hash` would identify work material rather than the full
request; update its documentation and callers accordingly.

Reuse means an answer was delivered, not that it would be identical under new
controls or that its content is acceptable. Workflow checks still rerun.

## 2. Allow explicit continuation of failed calls

Use the controls supplied on each call against its recorded history:

- `max_launches` is a **total ceiling**, not an increment. Two spent launches
  and a new ceiling of three permit at most one more launch.
- The latest failure must be eligible under the supplied retry policy. Current
  backoff applies before a resumed retry too.
- A lower ceiling neither refunds launches nor makes the history invalid; it
  prevents further launches once reached. Compute `failed_at_launch_limit`
  against the currently supplied ceiling.
- No separate budget-grant ledger is needed. Reservations retain the effective
  ceiling, and restarting never resets the spent count.

Example: A and B delivered; C timed out on its only allowed launch with no retry
policy. The caller supplies a longer timeout, a ceiling of two, and `timeout`
in the retry policy. A and B replay; C gets one new launch. Increasing timeout
alone would not permit it.

### Usage-limit continuation

Keep `usage_limit` forbidden in automatic retry policy. Recommended explicit
approval: the caller names the usage-stop `launch_id` it wants to continue.

- Approval permits continuation only if that stop is this call entry's latest
  launch and allowance remains.
- Record the approved stop ID in the next reservation, before process start.
  No separate pending-approval ledger is needed.
- Once reserved, that stop is no longer latest. Repeating its approval cannot
  authorize another usage stop. A new usage stop requires new approval.
- After continuation, ordinary bounded retries apply to other eligible failures.
- Repeated approval for an already-continued stop or after delivery is harmless,
  so workflow replay works. Approval cannot authorize another call entry.

A crash before reservation spends nothing. A crash after reservation uses the
existing unresolved-launch recovery rules.

## Safeguards and scope

- Unresolved reservations still block automatic work until the caller establishes
  worker termination and acknowledges it. Neither extra allowance nor usage
  approval bypasses this requirement or refunds a launch.
- `protocol_error` stays nonretryable; corrupt records remain errors.
- Every launch gets fresh paths. No unrecorded outputs are adopted.
- Keep existing controller locking, same-call concurrency rejection, parallel
  settlement, and process cleanup guarantees.
- External effects remain caller-owned and may repeat on another launch.

Resume is exceptional recovery with unchanged workflow/runtime code and a
compatible environment. Code changes require a fresh run and full workflow
rerun. There are no saved runs to preserve. As stated in the
[README](../../README.md#compatibility), do not add backward-compatibility layers,
legacy modes, or migrations. Reject incompatible record formats clearly.

## Remaining review choices

1. Confirm that timeout, total ceiling, and retry eligibility remain independent:
   increasing timeout does not implicitly authorize retry.
2. Choose the smallest API for usage-stop approval: a per-call stop-ID argument
   is recommended over a separate continuation operation. Define malformed,
   unknown, and wrong-entry reference handling without breaking harmless replay.
3. Confirm that approval clears the named stop and then ordinary retries apply,
   rather than granting an exactly-one-launch exception.
4. Specify reservation fields and validation for effective controls and stop IDs.
   Historical validity must not depend on the controls supplied today.

## Acceptance

Use deterministic tests and owned subprocess fixtures, not real model APIs:

- Changed controls replay delivered answers; changed work cannot reuse them.
- New launches record current controls without rewriting history.
- Ceiling increases, decreases, repeated calls, and restarts preserve spent
  counts; current retry selection and backoff govern continuation.
- Timeout changes alone bypass neither budget nor retry restrictions.
- Usage approval targets one stop; repeating it after continuation or delivery
  is harmless; a later usage stop blocks again. Test crash/restart boundaries
  and ordinary retries after approved continuation.
- Unresolved-launch recovery, fresh paths, immutable outcomes, and existing
  execution guarantees remain intact; incompatible records fail clearly.

Implementation requires normal project checks, independent-session verification,
an ADR, and updated contract/API documentation. This draft authorizes no changes.

## Review provenance

Based on the Commonplace agent's request. Claude Code 2.1.304, requested model
alias `opus`, reviewed the earlier draft against repository files using read-only
tools. Its recommendations informed this proposal; the served model was not
verified. That review is not implementation verification. No worker execution
probes were performed.
