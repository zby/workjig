# Handoff from the Commonplace project: theory that bears on Workjig

Written 2026-10-20 by a Commonplace session after reading Workjig at commit
`1b38846` plus the uncommitted `failed_at_launch_limit` rename. Commonplace is
Workjig's primary consumer and maintains a public knowledge base of claims
about how LLM-driven workflows execute and fail
(<https://zby.github.io/commonplace/index.html>). This note maps those claims
onto Workjig's contract. Nothing here is an approved contract change; it is
review input for the maintainers to accept, defer or reject under AGENTS.md.

Three kinds of item follow. **Mechanism** items explain a choice Workjig
already makes, so the design doc can state the reason rather than assert the
rule. **Limit** items predict a failure Workjig does not yet handle or
document. **Change** items propose a concrete API, record or docs edit.

## Mechanisms behind existing choices

- **Host code is the scheduler; the record is a results journal.**
  [The practical scheduler is the host language](https://zby.github.io/commonplace/notes/the-practical-scheduler-is-the-host-language.html)
  argues that a library should hand back a returning call and let ordinary
  code hold control flow and state, reifying state only when a run must
  outlive its process. Workjig does the minimal version: `run.json` stores
  call results, not control position, and the workflow re-derives position by
  replaying its own code. The condition this rests on is stated in
  [any symbolic program with LLM calls is a select/call program](https://zby.github.io/commonplace/notes/any-symbolic-program-with-llm-calls-is-a-select-call-program.html):
  replay needs the code between calls to be deterministic given replayed
  results, and effects on a mutable environment are not replayed. The README
  says only that changing workflow logic may require a new run. Worth stating
  as a requirement: call ids and request material must derive from fixed
  inputs and prior results. A workflow that folds wall-clock, randomness or
  completion order into a request silently creates a second version under the
  same call id and spends a fresh allowance.
- **Bookkeeping lives in code because code can enforce transitions.**
  [Scheduler-LLM separation exploits an error-correction asymmetry](https://zby.github.io/commonplace/notes/scheduler-llm-separation-exploits-an-error-correction-asymmetry.html)
  is the reason identity, budgets and completion state are in `run.py` and no
  agent coordinates. One sentence in the design doc's opening would carry it.
- **Execution retries and content corrections are different remedies.**
  [LLM debugging separates specification gaps, instruction violations and run-to-run variation](https://zby.github.io/commonplace/notes/llm-debugging-starts-with-retry-versus-rewrite-triage.html)
  and the underlying
  [three-way diagnosis](https://zby.github.io/commonplace/notes/llm-output-deviation-requires-three-way-diagnosis.html)
  give the mechanism behind the two-budget rule: a relaunch is a sampling
  remedy and addresses only run-to-run variation. A worker-reported `unable`
  may be a specification gap, so putting `worker_problem` into a `RetryPolicy`
  turns a possible spec defect into blind resampling. A docs warning would
  prevent that misuse.
- **Launch directories are checkpoints, with a known weakness.**
  [LLM-code boundaries are natural checkpoints](https://zby.github.io/commonplace/notes/llm-code-boundaries-are-natural-checkpoints.html)
  explains why keeping prompt, inputs and outputs per launch pays off, and
  notes that hidden state weakens the checkpoint. The shared workdir below is
  that hidden state.
- **Delivery is not acceptance.**
  [Final task success does not establish intended-path health](https://zby.github.io/commonplace/notes/final-task-success-does-not-establish-intended-path-health.html)
  underwrites the rule and the retained attempts list.
  [Error correction works with above-chance oracles and decorrelated checks](https://zby.github.io/commonplace/notes/error-correction-works-above-chance-oracles-with-decorrelated-checks.html)
  adds one README point: `max_launches` is not resampling. Identical prompts
  under one call id replay the cached answer, so voting or self-consistency
  needs caller-built distinct call ids.

## Limits the theory predicts

1. **Parallel calls share one writable workdir.** The
   [bounded-context orchestration model](https://zby.github.io/commonplace/notes/bounded-context-orchestration-model.html)
   requires that calls in one batch not interact before the barrier.
   `run.parallel` launches every worker with the same `cwd`, and the Codex and
   Claude adapters allow workspace writes, so siblings can read each other's
   in-progress files. Neither README nor design doc mentions this. The
   consumer is affected directly: it runs several report writers in parallel
   in one working tree. See also
   [agent orchestration needs coordination guarantees, not just channels](https://zby.github.io/commonplace/notes/agent-orchestration-needs-coordination-guarantees-not-just.html).
2. **A control signal is readable from content.**
   [LLM contexts interpret instructions and content through one medium](https://zby.github.io/commonplace/notes/llm-context-interprets-instructions-and-content-through-one-medium.html)
   predicts that the `usage limit` substring scan over stdout misfires: stdout
   is the harness event stream, including tool output and model text. A
   worker that reads a document mentioning usage limits and then fails for an
   unrelated reason becomes `usage_limit` and is never relaunched. An injected
   input can likewise steer a worker to write a `usage_limit` problem file.
   Commit `1b38846` aligned the precedence of the two reports; the remaining
   point is which evidence each report may rest on. The
   [oracle-strength spectrum](https://zby.github.io/commonplace/notes/oracle-strength-spectrum.html)
   suggests the ordering: prefer the harness's structured final error record,
   then stderr, and treat a substring match in stdout and a worker-written
   `usage_limit` as untrusted hints.
3. **Records sit inside the worker's writable root.** The README tells callers
   to put run records where the worker can write.
   [A consumption channel delivers force without the history that earned it](https://zby.github.io/commonplace/notes/a-consumption-channel-delivers-force-without-the-history-that.html)
   says whatever reaches a high-force path gets that force unless the path
   checks authorization. A worker can alter a sibling launch's outputs before
   collection, or `run.json` between the last write and a resume, and replay
   then grants that content committed status. At minimum a documented trust
   limit; better, a launch root separate from the record root.
4. **Harness-reported errors vanish into `delivered`.** `parse_evidence`
   extracts error flags, served model and session id, but nothing calls it
   during a run and nothing persists its output. A Claude launch with
   `is_error: true` that still left files reads as plain delivery. The
   consumer records the model the harness reports as the provenance of what
   it publishes, with an explicit unknown when the harness reports nothing,
   and never infers the served model from the requested one. Today that needs
   a re-parse of `stdout.log` after the run.
5. **Ambient instruction files are an undeclared input.** Fresh sessions are
   right, per
   [session history should not be the default next context](https://zby.github.io/commonplace/notes/session-history-should-not-be-the-default-next-context.html)
   and
   [context contamination operates below an agent's compliance reasoning](https://zby.github.io/commonplace/notes/context-contamination-operates-below-an-agents-compliance-reasoning.html).
   The remaining leak is that harnesses load `AGENTS.md` and similar files
   from the workdir. Those files are outside request material, so a changed
   instruction file replays a stale answer. The consumer pins its instruction
   files on its own side for this reason. Callers can already fold a
   fingerprint into `inputs`; the README does not say so.
6. **No aggregate bound.**
   [Compiling a coordination strategy preserves primitive but not aggregate authority](https://zby.github.io/commonplace/notes/compiling-coordination-preserves-primitive-not-aggregate-authority.html):
   compiled workflows keep per-call limits but lose the envelope that bounded
   total volume. Allowances are per call, so a loop minting new call ids is
   unbounded in the runtime.
7. **No channel for "finished, but assumed X".**
   [Silent disambiguation is the semantic analogue of tool fallback](https://zby.github.io/commonplace/notes/silent-disambiguation-is-the-semantic-analogue-of-tool-fallback.html)
   and
   [unattended work needs the functions that interaction supplied](https://zby.github.io/commonplace/notes/unattended-work-needs-the-functions-that-interaction-supplied.html):
   the prompt frame offers only `unable`. A worker that resolves an ambiguity
   its own way delivers silently.
   [Intent-framed delegation is a control regime](https://zby.github.io/commonplace/notes/intent-framed-delegation-is-a-control-regime-not-a-short-prompt.html)
   adds that the frame has no slot for purpose or acceptance criteria, so
   caller-owned checks stay invisible to the worker unless written into
   `task`.

## One vocabulary point from the consumer side

The consumer uses "attempt" for a larger unit than a launch: one spend of a
job's budget, which includes a check rejecting delivered content and a
reviewer asking for a correction. Workjig's `Attempt` is one launch record.
The architecture review's `LaunchRecord` rename resolves this on its own
terms; doing it before integration avoids two meanings of one word at the
boundary.

## Suggested changes, in order

1. Decide the `Attempt` rename; it shapes the integration surface.
2. Narrow usage-limit matching to the harness's final error record or stderr
   and mark worker-reported `usage_limit` untrusted in the docs.
3. Attach parsed harness evidence to the launch record at commit, with a
   harness-error flag beside `status` and explicit nulls.
4. Document the shared-workdir, writable-records and ambient-file limits in
   `docs/design.md`; add a per-request workdir override and the adapters'
   no-context-files switches if the consumer's parallel rounds need them.
5. Decide on a durable run-level launch cap and an assumptions field in the
   worker evidence protocol. Both are small, and both can wait for a consumer
   need under the YAGNI rule.

The reviewer also found the following already satisfied and recommends no
change: separate fresh launch directories, the fixed sibling snapshot rule,
distinct call ids for identical prompts, retained raw logs per launch, declared
material only in the request hash, and non-retryable `usage_limit` and
`protocol_error`.
