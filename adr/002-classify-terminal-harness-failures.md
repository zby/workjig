# 002 — Classify terminal harness failures, not whole logs

Status: accepted
Decision date: 2026-10-20
Recorded: 2026-10-20

## Context

The original `usage limit` scan could mistake quoted task content for a quota
stop and suppress a configured retry. It also missed every quota failure in
our local fixtures. Pi can report an assistant failure while exiting zero, and
all three harnesses can recover after an intermediate error.

[Protocol inspection](evidence/terminal-failures/README.md) and
[15 real-CLI fixture probes](evidence/terminal-failures/local-probes.md) establish
these distinctions for Codex 0.162.1, Claude Code 2.1.303 and Pi 1.3.0. The probes
used isolated loopback providers, not live quota exhaustion. Subscription OAuth
and other Pi provider transports remain outside the observed evidence.

## Decision

Select the final harness outcome first, then classify its cause. Adapter
factories declare `CommandWorker.output_protocol`; worker names remain
labels. Generic commands default to no stream interpretation. Protocol selection
belongs to declared request identity, with the absent default preserving the
canonical material of existing records. Committed outcomes are not reinterpreted.

| Protocol | Terminal selection | Usage-stop signals |
|---|---|---|
| `codex-json` | Last `turn.completed` or `turn.failed` in the current turn | Known text in `turn.failed.error.message` |
| `claude-stream-json` | Main-agent `result`; `subtype: success` still requires `is_error: false` | On an error result of that variant: last main-agent assistant `rate_limit`/`billing_error`, final HTTP 429, or known terminal error text |
| `pi-json` | Last completed assistant message, read through non-aborted `agent_settled`; `stopReason: stop` succeeds | Known text in the final assistant's `errorMessage` when `stopReason: error` |

Text matching is limited to selected error fields: usage-limit and quota-exceeded
phrases, `insufficient_quota`, rate-limit wording/codes, credit-balance failure
wording and terminal HTTP 429 forms. Ordinary messages, tool errors, retry
notifications and stderr do not establish quota failure. Descriptive
`parse_evidence` remains separate from outcome classification.

Recognized quota, billing and **unrecovered transient rate limits** become
nonretryable `usage_limit`. Harnesses already handle short retries; after they
give up, workflow code decides whether and when to resume. Workjig does not add
a second implicit backoff loop. Worker-written `usage_limit` problems remain
independent explicit stop signals, not claims of verified provider exhaustion.

Handled timeout wins. Either usage-stop signal wins over other completed-process
failures, including lingering children, after process-group cleanup. Otherwise,
existing cleanup and worker-problem precedence remains. Other terminal failures
and missing or malformed completion evidence become `process_error`; a caller
may explicitly retry that outcome within the launch allowance. A successful
terminal event does not override a nonzero exit or certify delivered content:
file collection and workflow checks still apply.

## Considered alternatives

- **Keep whole-log matching, or scan only stderr:** both retain quotation false
  positives; stderr-only also misses the observed stdout failure channels.
- **Use exit status alone:** sufficient for generic commands, but would accept
  Pi's exit-zero errors if required files were already present.
- **Treat any error event as terminal:** would stop successfully recovered runs.
- **Automatically retry exhausted throttling:** duplicates harness retry policy
  and can spend launches while an account or provider remains blocked. Keep the
  resume decision in the workflow instead.
- **Stop on every unknown harness failure:** would suppress intentional bounded
  retries of ordinary process failures. Unknown failures use `process_error`.

## Consequences

The small version-scoped parsers cover retained failure/recovery streams without
requiring harness installations in the normal tests. They are not universal
provider-error classifiers. Unknown quota wording can still become a retryable
`process_error`; missing completion cannot become delivery merely because the
process exited zero. Authentication and local harness budget failures also use
that generic outcome rather than new runtime statuses.

Subscription limit notifications and Pi retry summaries are not independently
used as quota evidence. The supported final outcome must establish the stop.
Raw streams remain available for diagnosis; adding new protocol signals needs
evidence rather than broadening a whole-log keyword list.

## Reevaluate when

- A supported CLI changes terminal framing, or retained real failures show a
  different final-message sequence or quota representation.
- Subscription OAuth or another provider transport needs a reliably classified
  stop that these signals miss.
- Actual workflows show that delegating exhausted throttling to callers is
  impractical, with evidence for a bounded runtime scheduling policy.
