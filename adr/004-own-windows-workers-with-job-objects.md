# 004 — Own Windows workers with job objects

Status: accepted
Decision date: 2026-10-30
Recorded: 2026-10-30

## Context

The execution contract requires handled timeouts to terminate the owned
process group, including children whose leader has exited, and requires a
child that outlives a normally exited leader to fail the launch with
`process_error`. POSIX meets this with a fresh session per launch, `killpg`,
and a group-existence check after the leader is reaped.

Windows has no kill-by-group primitive. The first Windows adapter ran
`taskkill /T` while the leader lived, which cannot reach descendants once the
leader exits and cannot detect them, so a launch whose child outlived it was
reported `delivered`. The first Windows CI run of the subprocess suite showed
exactly that. Commonplace needs to run on Windows, and the maintainer chose to
close the ownership gap rather than carry a Windows exception.

The [proposal](evidence/windows-process-cleanup-proposal.md) records the
analysis, the reviewer's comments, the agreed direction, and what the CI runs
observed.

## Decision

On Windows the owned group is a job object, implemented in
`src/workjig/_windows.py` behind the `OwnedProcess` seam in
`src/workjig/platform.py`:

- Each worker is created with `CreateProcessW` and `CREATE_SUSPENDED`,
  assigned to a fresh job with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, and
  resumed only after assignment succeeds. The worker executes no instruction
  before it is owned. Standard handles are passed through a
  `PROC_THREAD_ATTRIBUTE_HANDLE_LIST` naming exactly the launch's three
  duplicated handles.
- If creation, assignment or resume fails, the suspended worker is terminated
  and its exit confirmed before `launch_error` is recorded. When the exit
  cannot be confirmed, `OwnershipError` is raised instead of an `OSError`, the
  harness does not settle the launch, and the reservation stays unresolved.
- There is no cooperative termination phase. Handled timeouts and lingering
  descendants call `TerminateJobObject`.
- A descendant lingers when the job's pid list contains any member other than
  the leader at the moment the leader is reaped. A stale entry for the leader
  itself is not lingering. There is no grace period.
- Settlement waits a bounded time for the job to report no active processes
  and for the leader's handle to signal. If either cannot be confirmed,
  `OwnershipError` leaves the reservation unresolved.
- `close()` is called after settlement, including after an unconfirmed one.
  Closing the controller's last job handle, there or on controller death,
  kills the job. The runtime cannot observe that, so the explicit recovery
  rule for unresolved reservations is unchanged and does not rely on it.
- Only documented `kernel32` entry points are used, through `ctypes`, bound
  lazily so the module imports on any host. No dependency is added.

Public APIs and the persisted record format do not change. The POSIX path is
unchanged behind the seam.

## Considered alternatives

- **Assign to the job after `subprocess.Popen` returns.** Rejected in review:
  the controller can be descheduled between `CreateProcess` and assignment, so
  the window is unbounded, and `CommandWorker` accepts arbitrary commands.
  It also depended on the private `Popen._handle`.
- **`_winapi.CreateProcess`**, which returns the thread handle. About twenty
  lines, but a private standard-library module.
- **Process-tree snapshots with `tasklist` or WMI.** Racy and PID-based, which
  the contract treats as insufficient evidence.
- **A trampoline process that creates the job and starts the worker.** Closes
  the window with public APIs but adds a second owned process per launch and
  needs the same guarantees itself.
- **`pywin32`.** Estimated to save 100–275 lines of binding code depending on
  how much of the extended startup path it replaces, at the cost of a
  platform-conditional dependency; it does not remove the ownership or
  error-handling logic. Not adopted; recorded as an option.
- **A 250 ms grace period for job accounting after the leader exits.** The
  first implementation did this; two reviews showed it could report
  `delivered` for a descendant that outlived the leader briefly. Replaced by
  the pid-list check.

## Consequences

- Windows meets the lingering-descendant rule with the same outcomes as POSIX:
  `timeout` on handled timeouts, `process_error` when a child outlives a
  normally exited leader. Observed on `windows-latest` in CI runs 41092350683
  and 41097420349, including a grandchild whose parent exited at once.
- A Windows worker cannot flush state when stopped; it is killed. The POSIX
  SIGTERM-then-SIGKILL handshake test is POSIX-only permanently.
- Controller death on Windows kills the job. This is stronger than POSIX and
  deliberately not relied upon.
- Assigning a worker whose controller already runs inside a job, as CI
  runners do, needs nested job support: Windows 8 or Server 2012 or later.
- `_windows.py` is about 520 lines, above the 300–400 estimate for the whole
  change, roughly tripling the platform-specific code. The policy is tested
  against a fake `kernel32` on every host and exercised for real only by the
  Windows CI job.
- Windows support is limited: the deterministic suite passes in CI; no real
  harness has been probed on Windows, no test demonstrates concurrent-job
  isolation or detects a leaked handle, and several failure entry points are
  exercised only against the fake. The README states the limitation.

## Reevaluate when

- A real-harness probe on Windows fails in a way the deterministic suite does
  not predict, or Commonplace reports Windows behaviour that differs from the
  contract.
- A Windows harness needs to flush state on timeout, which would require a
  cooperative phase this design does not offer.
- A harness is shown to spawn before its runtime loads, which the suspended
  launch already covers but which would make any regression to post-start
  assignment unacceptable.
- `pywin32` becomes a dependency for another reason, or the binding code
  grows enough that its estimated saving changes the tradeoff.
- CPython exposes a public way to start a process suspended.
