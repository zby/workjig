# 006 — Retain per-launch pointers to native session logs

Status: accepted, with Codex lookup timing superseded by [ADR 008](008-resolve-native-sessions-on-demand.md)
Decision date: 2026-11-19
Recorded: 2026-11-19

Commonplace's advisory audit needs the trace of a particular attempt, including
failed attempts. A call can launch more than once. Neither a successful result
nor a recent transcript establishes that association.

Make binding explicit on the worker. For Pi and Claude, assign the native UUID
before reserving; for Codex and Antigravity CLI (`agy`), observe the JSON start
event. Store only an atomic pointer beside the launch. Reading an external log
is a caller decision; audit content, hash pinning and coverage policy are not
runtime responsibilities. Never search personal transcripts, reconstruct
Claude's internal project key, or copy a transcript into a tracked run. A
missing receipt cannot invalidate a committed execution result, nor authorize
adoption of an uncertain result. A reader does not need the harness, stdout, or
the original working directory.

Pin the effective external native store in the reservation, not in the mutable
receipt alone. Codex uses a bounded metadata-only app-server process. Antigravity
CLI needs no helper process: its `init` event supplies the canonical
`conversation_id`, and the local transcript sits at
`<home>/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/transcript.jsonl`.
The worker group is settled before the controller joins the observer. On POSIX a
separate watchdog also bounds the Codex helper group if the controller
disappears; on Windows the helper uses a kill-on-close job. Neither helper is a
worker retry.

The small receipt can become visible while a launch is running, independent of
run-state transitions. The lookup is deliberately not a transactional snapshot
of an external file. The caller owns storage permissions and retention. If a
harness stops reporting a locator or changes its CLI, degrade to a known ID
without a path, and revisit that adapter using evidence from its public API.

## Evidence and limits

- In a bounded headless Claude Code 2.1.326 probe, `SessionStart` reported the
  preassigned ID and an absolute `transcript_path`; that file existed after exit.
- Codex 0.166.0's generated app-server schema exposes `thread/read` with
  `includeTurns: false` and nullable, **unstable** `thread.path`. A live probe
  killed `exec` immediately after `thread.started`; the separate app server
  returned `thread not loaded`. Receiving the event proves identity, not that
  the rollout was persisted. No session search is warranted on error.
- In a bounded headless Antigravity CLI 1.5.12 probe, `stream-json` emitted
  `event: init` with canonical UUID `conversation_id`, and the local transcript
  existed at `~/.gemini/antigravity-cli/brain/<id>/.system_generated/logs/transcript.jsonl`
  (beside `transcript_full.jsonl`). Checking startup directory behavior showed
  `agy 1.5.12` respects `HOME` and ignores `GEMINI_DIR`; `--continue`, `-c`,
  `--conversation`, and `--remote-agent` are rejected on tracked launches.
- [Adapter evidence](../docs/harness-evidence.md) describes the JSON protocols;
  deterministic worker tests exercise retry, copy and missing-log behavior.
  These are not live compatibility claims for all harness versions or platforms.
