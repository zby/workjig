# Proposal: Windows process cleanup with job objects

Status: direction agreed, implementation in progress. Written 2026-10-30
against revision `675d891`; revised the same day after the review comment at
the end, which settled the open decisions as recorded below. An ADR follows
verified implementation. The [execution contract](../../docs/design.md)
remains authoritative until then.

## Problem

The contract requires handled timeouts to terminate the owned process group,
including children whose leader has already exited, and requires a child that
outlives a normally exited leader to fail the launch with `process_error`.
The POSIX adapter meets this with a fresh session per launch, `killpg`, and a
group-existence check after the leader is reaped.

The Windows adapter in `src/workjig/platform.py` cannot. It runs
`taskkill /PID <leader> /T /F` only while the leader is still alive, because
sending a recycled PID to `taskkill` would kill an unrelated process. Two gaps
follow:

- Descendants become unreachable once the leader exits. `taskkill /T` walks the
  parent-PID tree at kill time, so a grandchild whose parent has exited is
  missed even while the leader lives. `CREATE_NEW_PROCESS_GROUP` only scopes
  console control events; it is not a kill target.
- Lingering cannot be detected, so `group_lingered` is always false after a
  normal exit and the launch is reported `delivered`.

Observed: the first Windows CI run of the subprocess suite (GitHub Actions run
41084921845, 2026-10-30) failed
`test_lingering_child_usage_precedence_and_replay[none]` and
`test_process_group_children_are_stopped[True]` with status `delivered` where
`process_error` was expected. Both tests start a worker that spawns a sleeping
child and then exits. The remaining Windows failures in that run were
POSIX-only test code, fixed in `675d891`.

No snapshot-based workaround is acceptable. Enumerating the tree with
`tasklist` or WMI before the leader exits races with the exit itself and relies
on PIDs, which the contract treats as insufficient evidence.

## Proposed design

Own each Windows launch with a job object: the kernel tracks membership, so
descendants stay reachable after the leader exits and the member count gives a
direct lingering check.

Per launch:

1. `CreateJobObjectW` with no name and a non-inheritable handle.
2. `SetInformationJobObject(JobObjectExtendedLimitInformation)` with
   `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`.
3. Create the worker with `CreateProcessW` and `CREATE_SUSPENDED |
   CREATE_NEW_PROCESS_GROUP | CREATE_UNICODE_ENVIRONMENT`, redirecting stdin to
   `NUL` and stdout and stderr to the launch's log files through
   `STARTF_USESTDHANDLES` with a `PROC_THREAD_ATTRIBUTE_HANDLE_LIST` naming
   exactly those handles, so a concurrent launch on another thread cannot
   inherit them.
4. `AssignProcessToJobObject`, then `ResumeThread` on the primary thread. The
   worker executes no instruction before it is owned. If creation, assignment,
   or resume fails, terminate the suspended process, close every handle, and
   report `launch_error`.
5. Wait on the process handle within the timeout.
6. On timeout: `TerminateJobObject`.
7. After the leader exits or the job is terminated, poll
   `JobObjectBasicAccountingInformation.ActiveProcesses` under a bounded
   deadline. For a normal exit, a count above zero means descendants linger:
   terminate the job and report `group_lingered = True`, which the harness
   already maps to `process_error` under the usage-limit precedence rule. If
   the count does not reach zero before the deadline, cleanup raises and the
   reservation stays unresolved, as it does today when cleanup fails.
8. Close the job, process, and thread handles after settlement.

Bindings use `ctypes` against `kernel32` only, isolated in the platform module.
The package keeps `dependencies = []`; `pywin32` would shorten the code but
adds a dependency for one platform. `subprocess.Popen` is not used on Windows
because it closes the primary-thread handle before returning, so a suspended
worker cannot be resumed through it, and because its process handle is a
private attribute.

### Interface change in the platform seam

`process_creation_options()` and `cleanup_process_group(process)` are stateless
functions. A job handle must live from launch to settlement, so the seam
becomes a small owned-process object, for example
`start_process(argv, cwd, env, stdout, stderr) -> OwnedProcess` with `wait()`,
`cleanup() -> bool`, `pid`, and `returncode`. The POSIX implementation wraps the
existing `Popen`, session, and `killpg` logic unchanged. `harness.py` changes
only at the launch and cleanup call sites and stops touching `Popen` directly.
`_cleanup_group` in `harness.py` is a test seam and moves with it. Public APIs
and the persisted record format do not change.

### Expected size

| Piece | Lines |
|---|---|
| `ctypes` bindings and structures: job, process, startup info, attribute list | 90–120 |
| Native launch: suspended create, handle list, environment block, argument quoting, assign, resume, failure cleanup | 120–160 |
| Job wrapper and bounded whole-job settlement | 50–60 |
| Owned-process seam, both platforms, and `harness.py` call sites | 50–70 |
| Tests: mocked-binding policy tests; subprocess cases for surviving grandchildren, assignment and resume failures, cleanup failure, concurrent-launch isolation, and ownership before execution | 120–180 |

Roughly 300–400 source lines plus tests: a native launch path for Windows, not
an adapter tweak. An earlier draft estimated half this by assigning after
`Popen` returned; the review comment explains why that was insufficient.

## Decisions

The review comment settled these on 2026-10-30. The reasoning for each is kept
so the choice can be revisited.

### 1. The assignment window: closed with a suspended launch

The leader runs between `CreateProcess` returning and
`AssignProcessToJobObject`. A child spawned in that window is outside the job.
The textbook fix is `CREATE_SUSPENDED`, assign, then `ResumeThread`, but
CPython's `Popen` closes the primary-thread handle before returning, so the
thread cannot be resumed through the public API.

Options considered:

- **(a) Assign immediately after `Popen` returns and document the window.**
  Rejected. The first draft argued the window was microseconds because the
  targeted CLIs cannot spawn before their runtime loads. That was wrong twice:
  the controller can be descheduled between `CreateProcess` returning and
  assignment, so the window is unbounded, and `CommandWorker` accepts
  arbitrary commands. It also depended on the private `Popen._handle`.
- **(b) Call `_winapi.CreateProcess` directly**, which returns the thread
  handle. About 20 lines, but `_winapi` is a private standard-library module.
- **(c) Call `CreateProcessW` through `ctypes`** with `CREATE_SUSPENDED`,
  assign, then resume. Documented Win32 APIs only; reimplements standard-handle
  inheritance, environment-block construction, and argument quoting.

Decision: (c). Ownership is established before the worker executes any
instruction, matching the POSIX guarantee that `start_new_session` takes
effect before `exec`. The extra native code is accepted.

### 2. Immediate termination on Windows

The contract specifies SIGTERM, up to 0.25 seconds for the group to exit, then
SIGKILL. A job has no cooperative termination; `TerminateJobObject` is
immediate. `GenerateConsoleCtrlEvent(CTRL_BREAK_EVENT)` exists but requires a
shared console and is unreliable under CI runners and services.

Decision: on Windows, handled timeouts and lingering descendants terminate the
job without a cooperative phase. A Windows worker cannot flush state on
timeout. `test_cooperative_timeout_commits_timeout_even_after_exit_zero`
remains POSIX-only permanently. The contract paragraph on cleanup gains a
Windows clause.

### 3. Abrupt controller death: kernel cleanup, unchanged recovery rule

`KILL_ON_JOB_CLOSE` means the kernel terminates the job when the controller's
last handle to it closes, including when the controller crashes. POSIX cannot
promise this. The record-level rule is unchanged: an unresolved reservation
still requires establishing that the worker stopped and explicit
acknowledgement, because the runtime cannot observe the kill. Decision: state
this in the contract so the Windows guarantee is neither relied on nor removed
as accidental.

### 4. Platform floor

If the controller already runs inside a job, which CI runners and some
services do, assignment requires nested job support, available from Windows 8
and Server 2012. The contract should state this floor. `windows-latest` on
GitHub Actions is within it and is the real-Windows exercise of the
controller-inside-a-job case the review asks for. Not raised in the review;
proposed as recorded.

## Considered alternatives

- **Keep `taskkill /T` and mark the two tests expected failures on Windows.**
  Leaves the contract unmet on Windows. Acceptable only if Windows remains
  explicitly unsupported; the README still names Linux as the initial platform.
- **Process-tree snapshots via `tasklist` or WMI.** Racy and PID-based; rejected
  above.
- **A trampoline process that creates the job and starts the worker inside it.**
  Closes the assignment window with public APIs, but adds a second owned
  process per launch, complicates PID and return-code recording, and the
  trampoline itself needs the same cleanup guarantees.
- **`pywin32` (`win32job`).** Fewer lines than `ctypes`, at the cost of a
  platform-conditional dependency. Not preferred while the binding count is
  this small.

## Consequences if adopted

- Windows meets the lingering-descendant rule with the same observable outcomes
  as POSIX: `timeout` on handled timeouts, `process_error` when a child
  outlives a normally exited leader.
- Windows loses the cooperative phase and gains kernel-level cleanup on
  controller death. Both are platform clauses in the contract, not new
  configuration.
- The platform seam becomes stateful. Tests that monkeypatch
  `workjig.harness._cleanup_group` or `os.killpg` move to the new seam.
- `platform.py` roughly doubles. The Windows policy is unit-tested through
  mocked bindings on every platform and exercised for real only by the Windows
  CI job.

## Observed and unverified

Implemented in `c637a8f` through `82c13b4` (`src/workjig/_windows.py`,
`platform.py`, `harness.py`). The first green run of the full subprocess suite
on `windows-latest` (Python 3.14.9) was GitHub Actions run 41092350683 on
2026-10-30: 159 passed, 13 skipped as POSIX-only. Observed there:

- Suspended create, assign and resume succeed for every launch in the suite.
- A child, and a grandchild whose parent exited at once, are detected after a
  normally exited leader and terminated; the launch reports `process_error`
  (`test_process_group_children_are_stopped`,
  `test_lingering_child_usage_precedence_and_replay`).
- Handled timeouts terminate the job and settle with a non-zero return code.
- Killing the controller terminates its worker: the orphan never logs
  completion (`test_unresolved_reservation_blocks_resume_until_old_worker_stops`).
- Final source size: `_windows.py` about 330 lines, within the revised estimate.

Still inferred from documentation or source reading, not observed:

- `Popen` closing the thread handle before returning; the implementation does
  not depend on it since it does not use `Popen` on Windows.
- `PROC_THREAD_ATTRIBUTE_HANDLE_LIST` preventing inheritance of other launches'
  log handles. Parallel tests pass, but no test detects a leaked handle.
- Nested-job assignment. The run passed, but whether the runner process was
  itself inside a job was not checked.

Running the suite on Windows also exposed three pre-existing portability bugs,
fixed in the same series: the Windows file lock positioned the buffered file
rather than the descriptor before `msvcrt.locking`; a recovery test assumed an
orphan outlives a killed controller; and the example worker relied on
`O_APPEND` being atomic across processes.

Independent verification in a separate session, as AGENTS.md requires, has not
yet happened. The ADR follows it.

## Reevaluate when

- Windows becomes a supported platform in the README rather than an exercised
  one, which would make the assignment window and the missing cooperative phase
  user-facing limitations.
- A harness CLI is shown to spawn before its runtime loads, which would make
  option (a) above insufficient.
- CPython exposes a public way to start a process suspended, or `pywin32`
  becomes a dependency for another reason.

## Review comment — 2026-10-30

Commonplace needs to run on Windows. Although workflows currently see light
use, the maintainer agreed that closing this ownership gap is preferable to
carrying a Windows execution exception. The agreed direction is race-free job
ownership before worker execution, rather than option (a)'s assignment window.
This comment records the review and direction; it is not implementation evidence
or an accepted ADR.

The job-object design and stateful owned-process seam are appropriate. However,
assignment after `Popen` returns cannot establish the execution guarantee: the
controller can be descheduled while the worker continues running, so the window
is not bounded to microseconds. Python or Node startup time provides no safety
guarantee, and generic commands are also supported. A child spawned before
assignment can escape ownership. The proposed use of `process._handle` also
already depends on a private CPython interface.

Recommend creating the worker suspended, assigning it to the job, and resuming
it only after assignment succeeds, using documented Win32 APIs isolated behind
the owned-process seam. Accept the additional native-launch code and an explicit
Windows policy of immediate termination. Keep public APIs and persisted record
formats unchanged.

Settlement must confirm that the entire job has stopped within a bounded
deadline; waiting only for the leader after `TerminateJobObject` is insufficient.
If cleanup cannot establish termination, leave the reservation unresolved.
Creation, assignment, and resume failures need explicit handle and process
cleanup, including termination of any suspended worker. Kill-on-close protection
does not remove the existing explicit recovery requirement.

The two existing failing tests establish the reported symptoms, but are not
sufficient acceptance coverage. Add focused coverage for surviving grandchildren,
assignment and resume failures, cleanup failure, and concurrent-job isolation;
verify ownership is established before worker execution. Exercise the resulting
implementation on real Windows, including a controller already inside a job,
and obtain independent verification in a separate session as AGENTS.md requires.
Update the proposal's recommendation and contract wording when implementation is
taken up, and record the implemented decision in an ADR after verification.
