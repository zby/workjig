# Terminal failure signals in supported harnesses

Inspected 2026-10-20 against Workjig `4a6cb21`. This investigation asks which
parts of a harness stream identify a final failure, and which can identify a
usage/quota stop without scanning task content. The resulting implemented policy
is recorded separately in [ADR 002](../../002-classify-terminal-harness-failures.md).

## Scope and evidence

Inspected the adapter-supported versions: Codex 0.162.1, Claude Code 2.1.303,
and Pi 1.3.0. The local executables now report Codex 0.163.0 and Claude Code
2.1.304; those newer binaries were not used to claim behavior for the supported
versions. The managed Pi installation remains 1.3.0.

Sources are tagged upstream code describing Codex's public JSONL interface,
version-pinned Claude SDK declarations, and the installed Pi documentation and
print-mode implementation. No live provider quota-exhaustion probes were performed. Synthetic checks
below test Workjig's handling. The subsequent [local-provider probes](local-probes.md)
exercise the real CLIs against controlled HTTP responses, with raw streams
retained alongside the reproduction script.

## Findings at a glance

| Harness | Completion/failure evidence | Quota information | Important trap |
|---|---|---|---|
| Codex | `turn.failed.error.message`; successful turn ends in `turn.completed` | Error text; no quota code in this JSONL structure | A top-level `error` event alone need not be terminal |
| Claude | Final `result`, including `is_error`, `subtype`, and optional `terminal_reason` | `api_error_status`, top-level assistant `error` category, subscription `rate_limit_event` fields | `subtype: success` can carry `is_error: true` |
| Pi | Final assistant `message_end`, read through `agent_settled` | `errorMessage`; failed `auto_retry_end.finalError`; no dedicated quota code in the inspected message contract | JSON mode may exit 0 after an assistant failure |

## Codex 0.162.1

### Terminal evidence

The [version-tagged event types][codex-events] define:

```rust
pub struct TurnFailedEvent {
    pub error: ThreadErrorEvent,
}
pub struct ThreadErrorEvent {
    pub message: String,
}
```

The [JSONL emitter][codex-emitter] maps a failed turn to `turn.failed`, using the
turn error message plus additional details. It falls back to the last error
or the text `turn failed` if necessary. A completed turn emits `turn.completed`.
An interrupted turn initiates shutdown without emitting either terminal event
in that branch.

The [execution loop][codex-loop] exits 1 for a failed or interrupted primary
turn. It also tracks non-retrying error notifications as failures. Startup and
other exceptional paths may exit without a complete event stream.

### Not every error is terminal

The emitter converts error notifications to top-level `error` records but does
not include the notification's `will_retry` flag. The execution loop separately
checks that flag before treating the notification as fatal. Consequently, an
`error` record alone is insufficient to classify the final outcome.

An `item.completed` whose item type is `error` is also insufficient: the upstream
[tests][codex-tests] explicitly cover a runtime warning represented this way.
Those tests were inspected, not executed here.

### Usage-limit implications

`turn.failed.error.message` is a much narrower place to match a known quota
phrase than the whole log. It is still text matching: the JSONL error structure
does not retain a structured quota category. A top-level `error` fallback
without a terminal event would need a separate incomplete-stream policy.

## Claude Code 2.1.303

### Version-pinned public types

Downloaded `@anthropic-ai/claude-agent-sdk@0.3.303` from the [npm registry][claude-package].
Its package metadata has `claudeCodeVersion: 2.1.303`, and its manifest names the
same CLI version. The archive matched the registry's SHA-512 integrity value.
No package installation or CLI execution was needed.

The declarations in `package/sdk.d.ts` describe the CLI's stream-json messages.
The current [TypeScript reference][claude-types] and [headless guide][claude-headless]
provide supplementary explanations; the pinned declarations establish field
availability for this inspection.

### Terminal evidence

`SDKResultMessage` is the turn-complete signal. Informational events may follow,
so do not require `result` to be the final JSONL line. In a single-prompt run,
the process exits after the turn.

The declarations explicitly say that `subtype: success` carries either final
assistant text or, when `is_error` is true, error text for an API failure.
Relevant fields on that variant are:

```text
type: result
subtype: success
is_error: boolean
api_error_status?: number | null
result: string
terminal_reason?: TerminalReason
```

The error-result variants include `error_during_execution`, `error_max_turns`,
`error_max_budget_usd`, and `error_max_structured_output_retries`, with an
`errors: string[]` field. These are not all provider quota failures. A caller's
max-turn or cost limit is a distinct cause.

The headless guide documents nonzero exit for failed runs and in-run failure
results on stdout. That exit behavior was not independently exercised here.

### Structured quota-related signals

- Top-level `assistant.error` is a typed category including `rate_limit`,
  `billing_error`, `overloaded`, and authentication/model errors. It is a sibling
  of `message`, not assistant text inside `message.content`.
- `parent_tool_use_id` and `agent_id` distinguish subagent messages. Do not use a
  nested worker's failure as the outer run's terminal outcome.
- `api_error_status` on the final result identifies the HTTP status of the API
  error that ended the conversation. A 429 is more specific than a generic
  failure but does not by itself distinguish brief throttling from exhausted
  subscription usage.
- `rate_limit_event.rate_limit_info` has `status` values `allowed`,
  `allowed_warning`, and `rejected`, plus optional reset and overage information.
  `errorCode: credits_required` specifically denotes exhausted included usage
  requiring purchased credits according to the public reference. Events describe
  changes in subscription limit information, not necessarily a failed turn.
- `terminal_reason` includes `blocking_limit`, `api_error`, and
  `budget_exhausted`, among other reasons. The type union alone does not establish
  that these should all map to Workjig's `usage_limit`.

Use the final result to establish failure, then use related structured fields
to identify its cause. An earlier `system/api_retry` category or a rate-limit
warning cannot override a successful final result. The [local-provider probes](local-probes.md) now establish the assistant-category
to final-result sequence for API-key rate-limit, credit-balance, and authentication
errors, plus a retry that succeeds. Subscription-event correlation still needs
representative fixtures.

## Pi 1.3.0

### Terminal evidence

The installed `docs/json.md` and `docs/message-types.md` define:

- `message_end.message` as the authoritative completed message. Select the
  assistant role; user and tool-result messages also use message events.
- Assistant `stopReason` values including `stop`, `length`, `toolUse`, `error`,
  `aborted`, and `deferred`, with optional `errorMessage`.
- `agent_end` as the end of one low-level run. Retries, compaction recovery, or
  queued work may continue afterward.
- `agent_settled` as the end of automatic work. Its `aborted` flag is not a
  success flag: `false` can accompany an error.
- `auto_retry_end` with `success: false` and `finalError` after failed automatic
  retries. Read it in the context of the eventual settled run.

Do not confuse `tool_execution_end.isError` with a failed agent run. Likewise,
`length` and `deferred` are not evidence of provider quota exhaustion.

### Exit status differs between text and JSON modes

The installed `docs/cli-integration.md` explicitly states:

> A failed or aborted assistant response appears in the event stream but does
> not by itself produce a nonzero exit status. Inspect the events when success
> or failure matters. Pi still exits nonzero if the invocation throws an error.

Inspection of `dist/modes/print-mode.js` confirms that final assistant
error/abort/length handling sets exit code 1 only in text mode. JSON mode emits
the events and otherwise retains exit code 0 unless the invocation throws.

This matters because Workjig's Pi adapter selects JSON mode. Restricting future
quota classification to nonzero exits would miss an ordinary reported assistant
failure in this mode.

### Usage-limit implications

Select error text from the final failed assistant message after automatic work
settles, rather than from arbitrary streamed text. Failed retry details may
provide additional context. There is no quota-specific code in the inspected
shared assistant message type, so text matching remains provider-dependent.
Neither `agent_settled.aborted: false` nor process exit 0 establishes success.

## Checks against current Workjig

Constructed minimal synthetic event streams and called `parse_evidence`:

- Codex: `error` followed by `turn.completed` yields `error_event_count: 1` and
  `turn_outcome: turn.completed`; the count is not a terminal failure flag.
- Claude: a top-level assistant `error: rate_limit` followed by a
  `result/subtype: success`, `is_error: true`, `api_error_status: 429` yields
  `last_result_subtype: success` and `is_error: true`. The helper omits the
  assistant category, HTTP status, and terminal reason.
- Pi: an assistant `message_end` with `stopReason: error`, followed by
  `agent_settled` with `aborted: false`, yields both the error stop reason and
  `settled_aborted: false`.

A deterministic Python subprocess then wrote a required output file, printed
that synthetic Pi failure stream, and exited 0. Workjig returned `delivered`.
This confirms the current execution path does not interpret harness events; it
is not a live Pi failure reproduction. No implementation was changed.

## What an implementation proposal must settle

1. Select the protocol explicitly; do not make a worker's display name silently
   determine classification policy.
2. Separate terminal outcome selection from quota classification. Each harness
   has a different completion boundary.
3. Interpret supported terminal events even at exit 0 where the protocol
   requires it, particularly Pi. Decide whether this affects only quota stops
   or other harness-reported failures as well.
4. Decide how to handle missing, malformed, or contradictory terminal evidence.
5. Define which Claude categories count as a stop: transient rate limiting,
   exhausted credits, and a locally configured budget are different conditions.

The [local-provider probes](local-probes.md) exercise actual CLI emission without
consuming model quota. They narrow the remaining questions to classification
policy and untested paths such as subscription OAuth, other Pi transports,
subagents, and incomplete streams.

## Source provenance

Pi files were read from:
`/home/zby/.pi/agent/install/releases/1.3.0/node_modules/@earendil-works/pi-coding-agent/`.
The source files and package archives remain temporary research downloads;
this document retains the findings and retrieval references.

SHA-256 of inspected material:

```text
Codex rust-v0.162.1 exec_events.rs
  dafa872d7e86a099e56e28a329dcb9c03db90ed768c3b88cca8c91d46dc1d0e5
Codex rust-v0.162.1 event_processor_with_jsonl_output.rs
  bc296541b385f9b4ca2c28cc638e25a8d6b1332b2cd67f5a03eaa97a3d612b19
Codex rust-v0.162.1 exec/src/lib.rs
  51b62cef4893c6d07b51753fc2716fc05985cdc9de6673f2fa2d974779454d4b
Claude Agent SDK 0.3.303 npm archive
  86dc29477a33792a85088236c2d64a58881daca14fc98afbefa44c73799371fc
Pi 1.3.0 docs/json.md
  f103191816fdcf3627dfc98b89acad8a28eee737697130ddaeb2581131213543
Pi 1.3.0 docs/message-types.md
  b832893fb762f87fdbbfd694241d484592942823791f3e81a6c4d915421ff5db
Pi 1.3.0 docs/cli-integration.md
  edf8e723a70b4c3c40275db0391219e55d30597936212a9cfdef33963dd37f93
```

[codex-events]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/exec/src/exec_events.rs
[codex-emitter]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/exec/src/event_processor_with_jsonl_output.rs
[codex-loop]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/exec/src/lib.rs
[codex-tests]: https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/exec/src/event_processor_with_jsonl_output_tests.rs
[claude-package]: https://registry.npmjs.org/@anthropic-ai/claude-agent-sdk/0.3.303
[claude-types]: https://code.claude.com/docs/en/agent-sdk/typescript
[claude-headless]: https://code.claude.com/docs/en/headless
