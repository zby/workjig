# Headless harness adapter evidence

Inspected 2026-10-19 for the independent workflow package. The command research
below was performed without model calls or configuration changes. Subsequent
bounded real probes are recorded separately at the end of this document; do
not treat help inspection or synthetic parsing tests as live execution evidence.

## Codex CLI

- Installed executable: `/home/zby/.npm-global/bin/codex`; `codex-cli 0.162.1`. `codex --version` and `codex exec --help` completed, with a nonfatal warning that PATH aliases could not be created on the read-only filesystem.
- Candidate invocation: `codex exec --json --skip-git-repo-check -C <working-dir> -m <model> -s <sandbox> -c approval_policy=never 'Read the complete task and file paths in <prompt-path>...'`. The adapter passes the per-launch prompt path as a positional instruction; the worker must open it. `-c approval_policy=never` is the installed version's per-run configuration override. `--ask-for-approval never` appears in the [current command reference](https://developers.openai.com/codex/cli/reference) and an [OpenAI Cookbook example](https://developers.openai.com/cookbook/examples/codex/build_iterative_repair_loops_with_codex), but **is absent from this installed `codex exec --help`**; do not put it in this version's adapter command before a direct compatibility probe. `--sandbox` accepts `read-only`, `workspace-write`, and `danger-full-access`. `--skip-git-repo-check` is needed for the package's no-Git acceptance case. The caller must choose the model and sandbox. If the run record directory lies outside the Codex working root, the caller must choose a location and host permissions that allow the requested output writes; the adapter does not widen access automatically. [Official non-interactive guide](https://developers.openai.com/codex/noninteractive).
- `--json` sends JSONL events to stdout. Documented events include `thread.started` with `thread_id`, `turn.completed` with usage, `turn.failed`, `error`, and `item.*`. `-o` could write the final agent message separately; the first adapter implementation relies on retained stdout. Retain raw stdout/stderr, exit status, selected model, CLI version, and `thread_id`; a selected model is not proof of the actual served model. The cited JSONL example does not show an actual model identifier. [Official non-interactive guide](https://developers.openai.com/codex/noninteractive).
- `--ignore-user-config` and `--ignore-rules` can remove some ambient configuration; authentication still uses `CODEX_HOME`. These switches change worker behavior and should be explicit caller choices. `--ephemeral` suppresses session rollout files, so retain the JSONL stream if chosen. The adapter should not read or overwrite credentials or project settings. [Official command reference](https://developers.openai.com/codex/cli/reference).
- Failure evidence: process exit, `turn.failed` and `error` are documented. Their full payload and rate-limit taxonomy are **not established** by the inspected source, so preserve raw events and classify quota/usage stops only from observed or separately documented stable signals. Do not infer success from exit zero when required files are absent.

## Claude Code

- Installed executable: `/home/zby/.local/bin/claude`; version `2.1.303 (Claude Code)`. `claude --help` completed.
- Candidate invocation: run in the selected working directory with `claude -p --output-format stream-json --verbose --model <model> --permission-mode dontAsk --permission-prompts none --allowedTools=<explicit-list> 'Read ... <prompt-path>...'`. The adapter uses `--allowedTools=<list>` so the variadic option cannot consume the positional prompt. `-p` is the documented one-shot interface; `stream-json` yields a final `result` record with response and session metadata. `--permission-prompts none` denies anything that still asks. `dontAsk` denies calls that lack a prior allow rule. For file-producing work, the caller must explicitly choose a sufficient `--allowedTools` set or a different permission mode; `--allowedTools` is a permission rule, not a complete sandbox. `--permission-mode acceptEdits` is another documented caller choice for edits, though other shell commands may still need allow rules. [Programmatic guide](https://code.claude.com/docs/en/headless), [CLI reference](https://code.claude.com/docs/en/cli-reference).
- In version 2.1.303, print runs default to Manual mode if no mode is set. The CLI `--help` warns that `-p` skips the workspace-trust dialog and invalid settings files may be silently ignored. `--bare` reduces startup customization but needs an `ANTHROPIC_API_KEY` or explicit API-key helper and does not use subscription OAuth; do not silently add it to an adapter using the existing login. For tighter configuration control, `--setting-sources` and `--settings` are exposed locally, but exact policy needs a real probe. [Programmatic guide](https://code.claude.com/docs/en/headless).
- JSON output documents `session_id`, result, usage/cost metadata; streaming `assistant` messages can provide model information, but exact observed model fields and fallback behavior remain unverified here. Retain raw stream, stderr, exit code, requested model, and session ID. `--no-session-persistence` is available if the caller wants an ephemeral session; then the stream is the durable evidence. [Programmatic guide](https://code.claude.com/docs/en/headless), [CLI reference](https://code.claude.com/docs/en/cli-reference).
- Failure evidence: nonzero exit on run failure; invalid options go to stderr, while in-run failures such as missing authentication can appear as the result on stdout. Streamed `system/api_retry` records contain `error_status` and a named `error` category including `rate_limit`, `overloaded`, and `billing_error`; an internal retry event alone does not establish the final outcome. Treat explicit `rate_limit` as a stop candidate and require the final result/exit to determine whether it remained unresolved. `SIGTERM` exits 143 and leaves the active turn incomplete. [Programmatic guide](https://code.claude.com/docs/en/headless).

## Pi

- Installed executable: `/home/zby/.local/bin/pi`; `pi --version` reports `1.3.0`. `pi --help` could **not** be inspected in this sandbox: startup tries to create `/home/zby/.pi/agent/trust.json.lock` and receives `EROFS`. The installed package's own `docs/cli.md`, `docs/json.md`, `docs/security.md`, and `docs/message-types.md` under `/home/zby/.pi/agent/install/releases/1.3.0/node_modules/@earendil-works/pi-coding-agent/` establish the version-specific command contract. Equivalent upstream [usage](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/usage.md), [JSON](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/json.md), and [security](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/security.md) docs were checked, but may advance independently of installed 1.3.0.
- Candidate invocation from the installed docs: run in the selected working directory with `pi --mode json --provider <provider> --model <model> --tools <explicit-list> --<approve|no-approve> --session-dir <launch-session-dir> 'Read ... <prompt-path>...'`. `--mode json` is one-shot JSONL. `--provider` with `--model` avoids ambiguous provider selection. The caller explicitly selects project trust: `--no-approve` ignores trust-gated project-local resources for this process, while `--approve` loads them. This is **project-resource trust, not tool-call permission**. Context files such as `AGENTS.md` still load unless `--no-context-files` is used. An alternative `--no-session` avoids persistence; the documented JSON mode still emits a session header. Installed `docs/cli.md` and [upstream security](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/security.md).
- Pi has no built-in sandbox or per-tool approval. Built-in tools run with the Pi process's OS privileges. `--tools` limits declared tools but may retain MCP tools unless `--no-mcp` or an MCP exclusion is applied; loaded extensions can affect behavior. A production adapter must rely on caller-provided host isolation for access boundaries. [Upstream security](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/security.md); installed `docs/cli.md`.
- JSON mode starts with a `session` header containing ID and cwd. `message_end.message` is the authoritative completed message; assistant messages contain `provider`, requested `model`, optional actual `responseModel` and `responseId`, usage, `stopReason`, and optional `errorMessage`. `agent_end` may precede automatic retry; `agent_settled` closes automatic work. Retain the raw stream, stderr, exit code, session ID, and completed assistant model fields. Installed `docs/json.md` and `docs/message-types.md`; [upstream JSON docs](https://github.com/earendil-works/pi/blob/main/packages/coding-agent/docs/json.md).
- Failure evidence: completed assistant `stopReason` can be `error`, `aborted`, `length`, or `deferred`; `errorMessage` is optional. `tool_execution_end.isError` concerns a tool call, not necessarily the whole run. `auto_retry_start` and `auto_retry_end` disclose internal attempts, with `finalError` on final retry failure. Do not classify all errors as retriable or treat `agent_end` alone as completion. No version-specific stable usage-limit marker was established. Installed `docs/json.md` and `docs/message-types.md`.

## Adapter gate

Help inspection establishes command forms, not successful execution. The bounded
real probes below establish file delivery and cached reuse for the listed Codex,
Pi and Claude versions. Their scope does not establish every failure mode or
permission configuration. Failure parsing also has synthetic test coverage,
which is distinct from those probes. Future probes need a controlled location
and suitable permissions, including for Pi's trust lock; do not change existing
trust or credentials to make a probe pass.

## Real probes, 2026-10-19

The parent ran `examples/smoke_harness.py` with the adapter factories against
Codex 0.162.1 and Pi 1.3.0, requesting `gpt-6.1-sol`. Both read a supplied
`source.txt`, wrote an exact copy to `answer.txt`, returned a delivered result
after one launch, and reused the same launch record on the repeated call.
These runs used existing logins and approval to execute outside the parent
tool sandbox. The Codex child selected `workspace-write`; Pi selected only
read/write tools and `no-approve` for project resource loading. No settings or
credentials were rewritten by the probe; normal harness state was allowed.

| Harness | Retained local run records | Observed evidence |
|---|---|---|
| Codex | `/tmp/treadle-headless-codex-20261019-01/records` | Session `01a15545-8934-7752-92d8-493d18a78f62`, `turn.completed`, no JSONL parse errors; stream did not establish served model |
| Pi | `/tmp/treadle-headless-pi-20261019-01/records` | Session `01a15545-d25b-74b0-b116-9cb140097eda`, reported model `gpt-6.1-sol`, final stop reason `stop`, settled without abort; no separate served-model field observed |

Temporary raw records are local evidence, not distributed fixtures. The
committed probe script reproduces the checks with a new chosen directory.
No real quota exhaustion, authentication failure or permission denial was
induced in these Codex and Pi probes. Claude was initially command-tested and
synthetically tested only; its subsequent real probe is recorded below.

## Usage-limit classification, 2026-10-19

The repository handoff reports these messages observed in installed harnesses:
Claude Code: `Usage limit reached`; Codex: `You've hit your usage limit`.
It reports that Pi passes the provider's error through, without establishing
a stable Pi-specific phrase. These are inherited observations, not new real
probes. The handoff supplies no captured exit statuses or raw record locations
for these messages, so no particular exit code is claimed here.

The initial classifier treated a nonzero exit containing `usage limit` in either
raw log, ignoring case, as nonretryable `usage_limit`. This rule is superseded by
[terminal-outcome classification (ADR 002)](../adr/002-classify-terminal-harness-failures.md).
Timeouts retain their own status; exit-zero logs alone do not trigger this
classification. This deliberately narrow text rule does not recognize every
provider's quota error. Deterministic subprocess tests use synthetic messages
and exits; they do not establish real harness exit behavior or quota exhaustion.

## Claude Haiku probe, 2026-10-19

The operator approved `haiku` and the existing Claude Code login. Claude Code
`2.1.303` passed this bounded probe:

```sh
uv run python examples/smoke_harness.py claude /tmp/workjig-claude-haiku-20261019-01 --model haiku
```

The script used the existing adapter with `-p --output-format stream-json
--verbose --model haiku --permission-mode dontAsk --permission-prompts none
--allowedTools=Read,Write` and the generated prompt pointer. It used one launch,
a 180-second timeout, and no retries. The parent authorized execution outside
its tool sandbox; normal Claude settings and login remained active. The tool
allowlist is not an OS sandbox.

The process ran from `18:59:07` to `18:59:17` UTC and exited **0**. It read
`source.txt`, wrote the exact contents `headless-probe-7391` plus a newline to
`answer.txt`, and returned `delivered`. The second call reused the same launch
without another model invocation.

- Records: `/tmp/workjig-claude-haiku-20261019-01/records`.
- Launch: `788e1445f263423f991ada6fe035c1b2` (raw `stdout.log` and `stderr.log`
  are under `records/launches/<launch-id>/`).
- Session: `e77d96c7-f93b-4781-bdbe-eaca618aec31`.
- Parsed stream: 15 records, zero parse errors, final result subtype `success`,
  `is_error: false`.
- Assistant-reported model: `claude-haiku-5-5`. The final `modelUsage` mapping
  also contained `claude-haiku-5-5`, with `canonicalModel: claude-haiku-5-5`
  and `provider: firstParty`. This is reported evidence, not an inference from
  the requested `haiku` alias.

`parse_evidence` preserves the result's `modelUsage` mapping as `model_usage`;
it does not collapse multiple models into an inferred served-model identity.
Synthetic parser tests and a deterministic fake-CLI subprocess test cover
this behavior separately from the real probe. No real quota exhaustion,
authentication failure, or permission-denial behavior was tested here.

## Terminal-failure protocol inspection, 2026-10-20

The [detailed inspection](../adr/evidence/terminal-failures/README.md) establishes
the distinctions behind [ADR 002](../adr/002-classify-terminal-harness-failures.md):

- Codex's `turn.failed` is terminal, but a top-level `error` can precede retry.
- Claude's final `result` can have `subtype: success` and `is_error: true`;
  its pinned public types also expose structured rate-limit information.
- Pi JSON mode can exit 0 after an assistant failure. Read the final assistant
  message through `agent_settled`; neither exit 0 nor `aborted: false` means
  the assistant succeeded.

These findings come from version-scoped interfaces and synthetic Workjig checks.
Subsequent [local-provider probes](../adr/evidence/terminal-failures/local-probes.md)
ran 15 cases through the exact supported CLI versions in loopback-only network
namespaces. They confirmed terminal quota/authentication errors, successful
retries, and Pi's exit-zero failure behavior. Raw streams and a reproduction
script are retained in the evidence bundle. These are real CLI executions
against fixture responses, not live provider quota exhaustion. The ordinary test
suite replays all 15 retained streams through deterministic subprocesses to check
classification, retry allowances and reopen/reuse; it invokes no real harnesses.

## Usage-limit investigation, 2026-10-20

Investigated at `71cdf30`, without changing classification policy. Six bounded
Python subprocess scenarios used isolated temporary records, a three-second
per-launch timeout, `max_launches=2`, and retries only on `process_error`.
Each scenario was reopened to check replay without further execution.

| Synthetic worker behavior | Observed status | Launches |
|---|---|---|
| Print `Quoted document: usage limit is a pricing term` to stdout, exit 3 | `usage_limit` | 1 |
| Print the same quotation to stderr, exit 3 | `usage_limit` | 1 |
| Print `disk error` to stderr, exit 3 | `process_error` | 2 |
| Print JSON with `type: result`, `is_error: true`, `errors: [insufficient_quota]`, exit 3 | `process_error` | 2 |
| Write a valid `usage_limit` problem file, exit 0 | `usage_limit` | 1 |
| Deliver the required output, print `usage limit`, exit 0 | `delivered` | 1 |

All six outcomes replayed unchanged. The JSON quota example is a constructed
input to test the classifier, not an observed harness failure schema. These
results establish the classifier's false-positive mechanism and dependence on
wording, not how frequently those cases occur in real use.

### What public harness interfaces establish

- **Codex 0.162.1:** the version-tagged
  [JSONL event definitions](https://github.com/openai/codex/blob/rust-v0.162.1/codex-rs/exec/src/exec_events.rs)
  expose `turn.failed.error.message` and top-level `error.message` as strings.
  These error structures do not carry a quota-specific code. Restricting phrase
  matching to those fields would avoid scanning ordinary item content, but
  would still be a text heuristic. The
  [noninteractive guide](https://developers.openai.com/codex/noninteractive)
  confirms stdout includes agent messages and command-execution events.
- **Claude:** the current
  [headless guide](https://code.claude.com/docs/en/headless)
  documents in-run failures on stdout and startup flag errors on stderr.
  Stream output ends with a `result` message. Its `system/api_retry` events
  include `rate_limit` and `billing_error` categories, but describe attempts
  that the harness is retrying, not necessarily terminal failure. Stderr-only
  detection would therefore discard a documented failure channel; an earlier
  retry event alone cannot establish quota exhaustion.
- **Pi:** the previously recorded interface inspection above establishes error
  messages and retry/settlement events, but no stable quota-specific marker.
  No additional Pi failure-protocol verification was performed in this pass.

The three retained real probe logs listed above were also re-parsed. All were
successful exit-zero deliveries: Codex ended with `turn.completed`, Claude with
`success`, and Pi with `stop`. They provide no quota-failure specimens. No live
quota exhaustion was induced during this investigation.

### Recommendation and decisions still needed

Do not replace the current rule with stderr-only scanning: it retains the same
quotation problem and loses documented stdout failures. The useful direction
is harness-specific selection of terminal error fields, with fixtures tied to
supported versions. Existing `parse_evidence` output is not sufficient for all
of this: for example, it counts Codex error events but omits their messages.

Keep worker-written `usage_limit` reports conceptually separate from provider
confirmation. They can remain an explicit request to stop automatic retries
without claiming the provider's quota was independently verified.

Before implementation, decide what unknown or malformed harness streams should
do and whether generic command workers retain raw-log matching. Falling back
to whole-log scanning would retain its false positives; falling back to
`process_error` could retry an unrecognized quota stop. Current evidence does
not settle that tradeoff. Existing recorded outcomes also replay as recorded;
a new classifier would not automatically reinterpret them.
