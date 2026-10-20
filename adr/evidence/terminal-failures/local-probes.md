# Real CLI probes against a local provider fixture

Collected 2026-10-20, following the [terminal-interface inspection](README.md).
These are actual harness invocations with synthetic HTTP responses, not model
invocations or observations of real account exhaustion. During evidence gathering, no Workjig classifier
was changed.

## Setup

Each case ran in its own Linux user/network namespace created by `unshare -Urn`.
Only loopback was enabled. The fixture server and CLI shared that namespace,
so no request could reach an external provider. Each case also used a fresh
working directory, home, configuration and a deliberately constructed environment
without inherited credentials. All supplied API keys were dummy values.

Versions were checked by the probe:

- Codex CLI 0.162.1, downloaded as `@openai/codex@0.162.1-linux-x64` and checked
  against the npm registry's SHA-512 integrity value.
- Claude Code 2.1.303, using the installed version-specific binary.
- Pi 1.3.0, using the installed version-specific Node entry point.

Claude used `--bare`, no tools, no session persistence, and a loopback
`ANTHROPIC_BASE_URL`. Pi used offline mode, disabled extensions/tools/context
loading, and a custom `openai-completions` endpoint. Codex used a custom
Responses API provider, ephemeral sessions, a read-only sandbox and denied
interactive approvals. Fixture success responses never requested tools.

Each CLI invocation had a 35-second deadline with process-group cleanup. Version
checks had a 10-second deadline. All 15 retained cases completed before their
deadline. The script uses the public CLIs and configuration interfaces; it does
not patch or imitate harness internals.

## Results

“Requests” counts model-endpoint requests, not auxiliary token-count requests.
All three success responses deliberately included the phrase `usage limit` in
ordinary assistant text.

| Harness | Fixture scenario | Requests | Exit | Observed final outcome |
|---|---|---:|---:|---|
| Codex | Success with quoted phrase | 1 | 0 | `turn.completed` |
| Codex | HTTP 429, `insufficient_quota` | 1 | 1 | `turn.failed`: `Quota exceeded. Check your plan and billing details.` |
| Codex | Transient 429, then success | 2 | 0 | Intermediate `error`, then `turn.completed` |
| Codex | HTTP 401 authentication error | 1 | 1 | `turn.failed` with authentication error text |
| Codex | Transient 429 persists through one retry | 2 | 1 | `turn.failed`: `exceeded retry limit, last status: 429 Too Many Requests` |
| Claude | Success with quoted phrase | 1 | 0 | `result`, `is_error: false` |
| Claude | HTTP 429, `rate_limit_error` | 1 | 1 | Assistant `error: rate_limit`; final `is_error: true`, `api_error_status: 429` |
| Claude | HTTP 400, insufficient-credit message | 1 | 1 | Assistant `error: billing_error`; final `is_error: true`, `api_error_status: 400` |
| Claude | Transient 429, then success | 2 | 0 | `system/api_retry` with `rate_limit`, then final `is_error: false` |
| Claude | HTTP 401 authentication error | 1 | 1 | Assistant `error: authentication_failed`; final `is_error: true` |
| Pi | Success with quoted phrase | 1 | 0 | Assistant `stopReason: stop`, then `agent_settled` |
| Pi | HTTP 429, `insufficient_quota` | 1 | 0 | Assistant `stopReason: error`, then `agent_settled` |
| Pi | Transient 429, then success | 2 | 0 | Error message, `agent_end` with `willRetry: true`, successful retry, final assistant `stop` |
| Pi | HTTP 401 authentication error | 1 | 0 | Assistant `stopReason: error`, then `agent_settled` |
| Pi | Transient 429 persists through one retry | 2 | 0 | `auto_retry_end.success: false` with `finalError`, final assistant error, then settlement |

All three Claude failure cases used `subtype: success`, `is_error: true`, and
`terminal_reason: api_error`. Their top-level assistant errors had
`parent_tool_use_id: null`. Stderr was empty. This confirms a usable sequence
for binding the assistant's category to the terminal result in these cases.

Pi's error text retained the provider's JSON, for example:

```text
429: {"type":"insufficient_quota","code":"insufficient_quota","message":"fixture quota exhausted"}
```

Pi emitted `agent_settled.aborted: false` in both success and failure cases.
Codex's quota message did not contain the initial Workjig marker `usage limit`.
Neither did the Claude quota/billing or Pi quota failure streams.

A separate check parsed all 15 retained streams and asserted the observed exit
codes, request counts, terminal outcomes, and Claude error categories against
the table above.

## Inputs and limitations

`quota` supplies HTTP 429. For Codex and Pi its error type/code is
`insufficient_quota`; Claude receives `rate_limit_error`. `billing` supplies
HTTP 400 with `invalid_request_error` and the message
`Your credit balance is too low to access the Anthropic API.` This tests Claude's
normalization of a credit-balance error, not a structured provider billing code.

`recover` and `exhausted` supply transient rate-limit errors. The former returns
a successful SSE response on the second model request; the latter keeps failing.
Pi allows one agent retry and no provider retries. Codex allows one stream retry
and one request retry; observed request counts are recorded. Claude's recovery
uses its built-in retry policy.

The fixture sets `retry-after: 0` and `x-should-retry: false` for terminal-error
cases, and `x-should-retry: true` for transient cases. These headers deliberately
control the test; the runs do not establish real providers' backoff timing.
Claude's full retry-exhaustion sequence was not exercised.

The probes cover API-key-style endpoints, not Claude subscription OAuth or
Pi's OpenAI Codex OAuth transport. No Claude `rate_limit_event` was observed;
its subscription-specific `credits_required` behavior still has schema evidence
only. Pi's error formatting for other provider protocols remains unverified.
No subagents, multiple user prompts, or malformed streams were tested.

Early exploratory runs used an `insufficient_quota` response for a recovery
case, or disabled Codex stream retry; those configurations did not reach the
fixture's success response. The retained suite uses a transient error and an
explicit one-retry configuration. The final source and captures below describe
that suite.

## Retained artifacts

- [local_probe.py](local_probe.py): standalone reproduction for one case.
- [captures/](captures/): 15 case directories. Each contains the unedited
  `stdout.jsonl` and `stderr.log`, plus `result.json` with the command, version,
  exit code, timeout indicator, and fixture response sequence.

Only those three files per case were copied from private temporary directories.
No homes, auth files, or harness session stores were retained. The logs contain
synthetic prompts and outputs, generated session IDs, timestamps, and temporary
paths. Provider request bodies and headers were not retained. Reported token
usage and cost are estimates calculated from fixture-supplied usage; no model
service billed these runs.

## Reproduce

Requires Linux with unprivileged user/network namespaces, `ip`, Python 3.11+,
Node at `/usr/bin/node` for Pi, and the exact CLI versions above. Missing
prerequisites should fail visibly. The script refuses a namespace with network
interfaces other than loopback and rejects a mismatched CLI version.

From the repository root, choose a new private destination:

```sh
OUT="$(mktemp -d /tmp/workjig-terminal-probes.XXXXXX)"
PROBE="$PWD/adr/evidence/terminal-failures/local_probe.py"

# CLI is the version-specific binary (or Pi's dist/cli.js).
CLI=/path/to/codex-0.162.1
unshare -Urn sh -c 'ip link set lo up && exec python3 "$@"' sh \
  "$PROBE" "$OUT" --harness codex --scenario quota --cli "$CLI"
```

Run `success`, `quota`, `recover`, and `auth` for each harness; additionally run
`exhausted` for Codex and Pi, and `billing` for Claude. Each case directory must
be new. This evidence script is separate from the normal test suite and has no
runtime role in Workjig.

The Codex package metadata is available at
<https://registry.npmjs.org/@openai/codex/0.162.1-linux-x64>. Download the tarball
named by `dist.tarball`, verify `dist.integrity`, and extract into a private
directory. Its executable is
`package/vendor/x86_64-unknown-linux-musl/bin/codex`; no global installation is
needed. The inspected archive's SHA-256 is:

```text
7417711678b464eee70664126e39e88bb3c84c2928da0e71f14b56e04ff5c09f
```

## Implication for the classifier proposal

Terminal event selection is now demonstrated for these cases. Whole-log matching
is unnecessary, and matching only `usage limit` would miss the observed errors.
A supported-harness classifier can use Claude's terminal error category,
Codex's terminal normalized quota message, and Pi's final provider-error text.
It must allow Pi error evidence at exit 0 and ignore errors superseded by a
successful retry.

The policy question remains whether exhausted transient throttling should also
stop automatic Workjig relaunches. These fixtures distinguish it from explicit
insufficient quota; they do not decide how the caller should respond.
