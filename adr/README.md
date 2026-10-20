# Architecture decision records

ADRs record consequential choices and the reasoning needed to revisit them.
Read related records before changing a design. Keep the current behavior in the
[execution contract](../docs/design.md) and public API documentation; use ADRs
to explain why that behavior was chosen.

The [design rationale](../docs/design-rationale.md) is an overview of existing
tradeoffs. Add focused ADRs as decisions are made or revisited rather than
turning every paragraph of that overview into a record.

## Records

| ADR | Decision | Status |
|---|---|---|
| [001](001-name-physical-launch-records.md) | Use launch vocabulary at the public API boundary | Accepted |
| [002](002-classify-terminal-harness-failures.md) | Classify terminal harness failures, not whole logs | Accepted |

## When to write one

Write an ADR for a decision that has been made and implemented, including an
adopted documentation or process convention. Record choices whose alternatives,
accepted limitations, or reasoning would otherwise be easy to lose. Routine
fixes and mechanical changes usually need only a commit message.

Keep investigations and undecided options in [evidence/](evidence/README.md)
until there is a decision. Evidence can support an ADR without becoming policy.

Use `NNN-short-decision-name.md`, starting with the next unused number. Do not
renumber or reuse numbers. Keep the table above current.

## Writing a useful record

- State the problem and constraints, the actual choice, and who or what uses it.
- Describe alternatives actually considered and why they lost. “None developed”
  is useful information; do not invent a debate after the fact.
- Explain the benefit, cost, and accepted limitation. Deliberately omitting a
  feature or guarantee is a decision worth recording when it simplifies the
  system.
- Link evidence beside the claim it supports. Distinguish observed behavior,
  reported experience, expectations, and preferences. Say what remains unknown.
- Name concrete conditions that would justify reconsidering the choice. These
  trigger a fresh decision, not automatic implementation of a deferred feature.
- Keep detail proportional to the decision. Put raw logs, measurements, long
  analyses, and reproductions in `evidence/`; summarize their implications here.
- Leave file inventories, migration steps, and change narratives to Git. Mention
  the ADR number in implementing commit messages so related work can be found.

For retrospective records, say so and use the known decision date, or `unknown`.
Do not present an inferred motivation as something the original decision-maker
stated.

## Template

```markdown
# NNN — <decision>

Status: accepted
Decision date: YYYY-MM-DD
Recorded: YYYY-MM-DD

## Context

What problem or constraint requires a choice?

## Decision

What did we choose, within what scope, and which callers or components use it?

## Considered alternatives

What other options were considered, and what decided between them?

## Consequences

What becomes easier or harder? What cost or limitation do we accept?
State where the choice stops applying.

## Reevaluate when

What change in tasks, operating conditions, or evidence would reopen the choice?
```

## Maintaining records

Clarify a record as evidence improves. For a replacement decision, add a new
ADR, mark the old one `superseded`, and cross-link them. Use `deprecated` when a
choice is abandoned without a replacement. Update the affected contract or
instructions alongside the decision so future callers and agents encounter it.

This guide adapts Commonplace's ADR authoring conventions to a plain Markdown
repository; it requires no Commonplace tooling or schema.
