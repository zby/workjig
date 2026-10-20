# Project-local Pi subagents

`index.ts` and `agents.ts` are copied unchanged from Pi 1.3.0's
`examples/extensions/subagent/` in `@earendil-works/pi-coding-agent`.
They use Pi's public extension API and launch isolated Pi subprocesses, not
Workjig. No npm installation is needed: Pi supplies the imported packages.

## Load

Trust this project's extensions and run `/reload` in Pi (or restart Pi).
The extension registers the `subagent` tool. Project agent definitions live in
`.pi/agents/`; this project provides `review-reader` without a pinned model, so
it inherits the dispatching session's model and thinking level.

For example, ask the parent agent to call:

```json
{
  "agent": "review-reader",
  "agentScope": "project",
  "task": "Review <exact candidate and scope> through the architecture lens. Read AGENTS.md and docs/design.md; return findings directly without repository edits or saved reports."
}
```

Use `tasks: [{"agent": "review-reader", "task": "..."}, ...]` instead of
`agent` and `task` for independent parallel reviews. The example accepts at most
eight tasks and runs at most four concurrently. Supply the complete assignment,
shared constraints, and exact candidate to each reviewer. The parent must keep
the candidate stable and coordinate expensive checks.

## Review response round

The upstream example uses ephemeral child sessions (`--no-session`); calls do
not continue earlier conversations. For the response round required by
`instructions/lightweight-review.md`, send each replacement reviewer its role,
original assignment, original review, and the same consolidation explicitly.
Do not use files as review handoffs or save review artifacts.

## Boundaries and limitations

The reviewer definition omits edit/write tools, but bash and inherited
extensions can still have side effects: this is instruction-based read-only
behavior, not a sandbox. Review all loaded extensions before use. Children
inherit the environment, credentials, and normal Pi resource discovery.

The copied example has no execution deadline or launch budget. Cancellation
signals the direct child, not an owned process group; do not treat it as
Workjig-grade process cleanup. Parallel summaries are capped at 50 KB per task
(full results remain in tool details). Project agent scope is opt-in and the
example retains its project-trust confirmation behavior.

This is development tooling only; it adds no Python runtime dependency or
Workjig adapter. Keep review outputs conversational. Changes to the tooling
still require separate-session verification under AGENTS.md before acceptance.
