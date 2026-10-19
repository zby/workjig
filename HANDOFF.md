# Handoff: workjig

Written 2026-10-19 for the first agent working in this repository.
`AGENTS.md` (contributor instructions adapted from Treadle, commit
`947252d`) is the inherited rule set; this file adds the state, the ordered
work and the acceptance for it.

## Purpose

workjig is a standalone Python runtime for workflows that invoke subprocess
workers, usually headless coding harnesses (`codex exec`, `claude -p`,
`pi -p`). Workflow code written by the caller owns branches, checks and
correction loops; workjig owns launches, per-launch paths, bounded retries,
run records and resumption. It must stay usable without Commonplace, Git or
any knowledge base.

## State (2026-10-19)

- Repository: `main`, first commit `3b8adae`, 55 tests passing with
  `uv run pytest -q` from the repository root; `uv run ruff check .` clean.
- The package was built the same day inside the Commonplace repository as
  `treadle-headless`, reviewed, and renamed to `workjig`. Distribution,
  import and prose name are all `workjig`; do not rename anything.
- Codex and Pi have passed real probes with `gpt-6.1-sol`. Claude is
  command-tested and parser-tested only, never run with a real model.
- No CI, no PyPI publication. `workjig` was free on PyPI on 2026-10-19 but
  is not registered.
- Read before changing anything: `README.md` (API and support claims),
  `docs/design.md` (the execution contract), `docs/harness-evidence.md`
  (what each adapter has actually demonstrated), `examples/README.md`.

## The boundary, which you must keep

- No scheduler, no dependency graph, no orchestrator-agent protocol.
- No check helper that understands a document type, a refusal grammar or a
  correction cascade; checks, snapshots for parallel checks and correction
  loops are the caller's.
- No environment preparation, worktree management, code pinning or sandbox
  enforcement; the caller supplies the working directory and permissions.
- Durable results never imply exactly-once execution or that a current check
  passes. A launch reservation spends an allowance before the process starts;
  an unresolved reservation blocks resumption until the operator acknowledges
  it with `acknowledge_stopped`. Keep that contract; do not soften it.
- Execution-retry budgets and content-correction budgets stay separate.

If a change needs any of these to bend, stop and report the conflict instead.

## Work in order

1. Read `AGENTS.md`, `README.md`, `docs/design.md` and
   `docs/harness-evidence.md`; run the checks in `AGENTS.md` from a fresh
   `uv sync --locked` and record the result before changing anything.
2. Usage-limit classification. Today a provider quota error in a harness's
   raw output is a `process_error` and is retried under the retry policy. The
   messages observed in the installed harnesses are "Usage limit reached"
   (Claude Code) and "You've hit your usage limit" (Codex); Pi passes the
   provider's error through. Classify a nonzero exit whose stdout or stderr
   contains "usage limit" (case-insensitive) as `usage_limit`, never a
   timeout (a kill reports nothing), and keep it out of automatic retry.
   Record what each harness was observed to emit in
   `docs/harness-evidence.md`; do not claim an exit status you did not see.
3. A real Claude probe through `examples/smoke_harness.py`, bounded like the
   Codex and Pi ones, after the operator confirms the model and account to
   use. Record versions, command, record location and limits in the evidence
   file. Claude's JSON result reports the models used under `modelUsage`;
   `parse_evidence` should surface that without inferring the served model
   from the requested one.
4. Only if the operator asks: escalating backoff between relaunches (the
   Commonplace consumer used 30 s then 120 s), and a `--help`-level check that
   each adapter's flags still exist in the installed harness version.

## Acceptance

- The checks `AGENTS.md` lists pass from the root: `uv sync --locked`,
  `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv build`.
- Every new behaviour has a deterministic subprocess test; a real-harness
  probe is evidence, not a test, and is recorded as such.
- `README.md` and `docs/design.md` say what the code does, no more. A support
  claim for a harness names the version and the probe that backs it.
- No import of Commonplace, Treadle or any other sibling project.

## Coordination

- Commonplace (`/home/zby/llm/commonplace`, ADR 118) runs its own analysis
  workflow over its own headless primitives in `src/commonplace/headless/`;
  it is the candidate first consumer of workjig and will be ported onto it
  as a separate project. Do not edit Commonplace, Treadle or anything outside
  this repository. The Commonplace workshop
  `kb/work/headless-workflow-extraction/` holds the plan, the responsibility
  matrix and the implementation record; treat it as history, not as a
  specification that overrides this repository's docs.
- Commit on `main` with explicit paths; subjects are one imperative sentence;
  never force-push or rewrite history. Report the model you ran as in a
  `Model:` trailer.

## Stop and ask

- Any change to the public API (`Run`, `Request`, `CallResult`,
  `RetryPolicy`, `CommandWorker`, the adapters' signatures) or to the record
  format.
- Anything that needs credentials, a real model run, PyPI registration or a
  version bump.
- A test that can only pass by weakening isolation or an assertion.

Say what you verified and what you did not; a passing suite is not a claim
about real harness behaviour.
