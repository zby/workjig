# Workjig — contributor instructions

## Project

[Workjig](README.md) is a standalone Python runtime whose primary goal is to
support Commonplace as the foundation of its workflow engine. Extraction into
an independently maintained package aims to help agents focus on a smaller
codebase and improve its quality. Commonplace's needs drive priorities; reuse
in other projects is a secondary opportunity.

Workflow code owns branches, checks and correction loops; the runtime owns
launches, separate output paths, bounded execution and run records. Keep
Commonplace's domain rules in Commonplace and the runtime independently usable
and testable. It does not require Git or Commonplace.

Read the [execution contract](docs/design.md) and relevant
[decision records](adr/README.md) before implementation or contract changes.
Record consequential implemented choices using the ADR guide, with supporting
material in `adr/evidence/`. See [harness evidence](docs/harness-evidence.md) for tested adapters
and versions. Distinguish deterministic test coverage from actual model
invocations when making support claims.

## Rules that are easy to break

- Preserve stable call identity and matching declared request material for
  reuse. Only complete, atomically committed results are reusable. Delivery
  does not certify acceptance: workflows rerun checks against the intended
  input snapshot, using fixed sibling snapshots for parallel checks.
- Reserve each launch durably before process start. Restart and recovery must
  not refund spent allowances. Unresolved reservations block automatic work
  until explicit acknowledgement that the old worker has stopped; never delete
  a reservation to suppress uncertainty or adopt its unrecorded outputs.
- Give every physical launch fresh paths. Separate output directories are not
  a sandbox. Durable records do not make external effects transactional; the
  caller owns permissions, idempotency and recovery policy.
- Settle all submitted parallel work before reporting group failure. Handled
  timeouts must terminate the owned process group, including children whose
  leader has exited. Abrupt controller death requires explicit recovery.
- Keep execution retries bounded and explicit. Content corrections and their
  budgets belong to workflow code, not an implicit runtime correction loop.
- Build on harness public interfaces; do not copy or patch their internals.
- Keep the implementation small; add only what current work needs.
- Ask before settling open choices in the design, changing the public API or
  record format, using credentials for real model probes, publishing, or
  bumping the version. Do not promote experimental findings into approved
  policy.
- Keep the package standalone. Tests and examples must not depend on Treadle
  or Commonplace. Test support must not become a hidden second runner.

## Checks

Use uv: `uv sync --locked`, `uv run pytest`, `uv run ruff check .`,
`uv run ruff format --check .`, `uv build`. All must pass, without blanket
skips. The [README](README.md#development) documents development commands;
the initial platform is Linux with Python 3.11 or newer. Missing prerequisites
are failures, not automatic skips. Use the existing development environment for
routine checks; an editable install is fine. Use a fresh environment when
testing built distributions, installing the artifact rather than the source tree.

Test parsing, classification and validation directly with unit or file-based
functional tests. Use real subprocesses with deterministic worker fixtures for
execution guarantees: delivered files, errors, effects, restart behavior, budgets
and process cleanup. Keep representative integration cases rather than repeating
the full lifecycle for every parser input. Use owned processes and isolated
private state; never call a real model API in the normal test suite. Real-harness
probes are separate, deliberate, bounded operations that may use credentials.

Other agents may be working in this repository at the same time. Use your own
temporary directories for stores and scratch files.

## Delegation

Delegate by intent: give the worker the purpose, binding constraints, write
scope and acceptance, including what its result will be compared against, and
let it choose the means from what it finds while working. Workers see neither
this conversation nor, unless their runtime loads it, this file, so state any
rule they need. A worker returns choices it cannot settle within those bounds
instead of guessing. Implementation is accepted only after a separate session
verifies it.

## Git

- Review status and diff. Preserve others' changes: no reverting, stashing,
  hard resets or force-pushes without permission.
- Stage explicit files, not `git add -A`. Prefer atomic stage and commit:
  `git add <files> && git commit ...`.
- Prefer complete artifact commits. Do not partially stage shared docs just to
  expose an artifact; handle unrelated changes separately.
- Write imperative subjects under about 72 characters, without `feat:` prefixes.
  State the intended result in the body when the subject and diff leave it unclear.
  For sweeps, state what moved, how many, and what was kept, cut or deferred.
  Keep change history in commits, not docs.
- Commit renames separately from content edits so `git log --follow` survives.
