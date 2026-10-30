"""Windows job-object ownership policy, checked against a fake kernel32 on any host."""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import workjig._windows as windows
import workjig.platform as platform
from workjig.platform import OwnershipError

pytestmark = pytest.mark.unit

JOB, PROCESS, THREAD, PID, CHILD = 11, 21, 22, 4242, 7777


class FakeKernel32:
    """Records calls and lets a test fail one entry point or shape job membership.

    `members` are the pids the job reports until it is terminated; afterwards
    it reports `active_after_terminate` processes and no pids.
    """

    def __init__(
        self,
        *,
        fail: str | None = None,
        members: tuple[int, ...] = (),
        active_after_terminate: int = 0,
        exited: bool = True,
        exit_code: int = 0,
        wait_result: int | None = None,
        more_data: bool = False,
    ) -> None:
        self.fail = fail
        self.members = members
        self.active_after = active_after_terminate
        self.exited = exited
        self.exit_code = exit_code
        self.wait_result = wait_result
        self.more_data = more_data
        self.terminated_job = False
        self.calls: list[str] = []
        self.closed: list[int] = []
        self.duplicates: list[int] = []
        self.limit_flags: int | None = None
        self.handle_list: list[int] | None = None
        self.create: dict[str, object] = {}

    def _ok(self, name: str) -> bool:
        self.calls.append(name)
        return name != self.fail

    def CreateJobObjectW(self, attributes, name):
        return JOB if self._ok("CreateJobObjectW") else None

    def SetInformationJobObject(self, job, kind, info, size):
        self.limit_flags = info._obj.BasicLimitInformation.LimitFlags
        return self._ok("SetInformationJobObject")

    def QueryInformationJobObject(self, job, kind, info, size, returned):
        assert job == JOB
        members = () if self.terminated_job else self.members
        if kind == windows._JOB_OBJECT_BASIC_PROCESS_ID_LIST:
            if self.more_data:
                self.calls.append("QueryInformationJobObject")
                return False
            info._obj.NumberOfAssignedProcesses = len(members)
            info._obj.NumberOfProcessIdsInList = len(members)
            info._obj.ProcessIdList[: len(members)] = members
        else:
            info._obj.ActiveProcesses = self.active_after if self.terminated_job else len(members)
        return self._ok("QueryInformationJobObject")

    def AssignProcessToJobObject(self, job, process):
        assert (job, process) == (JOB, PROCESS)
        return self._ok("AssignProcessToJobObject")

    def TerminateJobObject(self, job, code):
        assert job == JOB
        if not self._ok("TerminateJobObject"):
            return False
        self.terminated_job = True
        self.exited = True
        self.exit_code = code
        return True

    def CreateProcessW(self, app, command, psa, tsa, inherit, flags, env, cwd, startup, info):
        startup_info = startup._obj.StartupInfo
        self.create = {
            "command": command.value,
            "inherit": inherit,
            "flags": flags,
            "cwd": cwd,
            "std": [startup_info.hStdInput, startup_info.hStdOutput, startup_info.hStdError],
            "std_flags": startup_info.dwFlags,
            "attributes": startup._obj.lpAttributeList,
        }
        info._obj.hProcess, info._obj.hThread, info._obj.dwProcessId = PROCESS, THREAD, PID
        return self._ok("CreateProcessW")

    def InitializeProcThreadAttributeList(self, attributes, count, flags, size):
        if attributes is None:
            size._obj.value = 64
            return False
        return self._ok("InitializeProcThreadAttributeList")

    def UpdateProcThreadAttribute(self, attributes, flags, attribute, value, size, prev, ret):
        assert attribute == windows._PROC_THREAD_ATTRIBUTE_HANDLE_LIST
        self.handle_list = list(value)
        return self._ok("UpdateProcThreadAttribute")

    def DeleteProcThreadAttributeList(self, attributes):
        self.calls.append("DeleteProcThreadAttributeList")

    def DuplicateHandle(self, source, handle, target, out, access, inherit, options):
        assert inherit is True
        out._obj.value = 1000 + handle
        self.duplicates.append(out._obj.value)
        return self._ok("DuplicateHandle")

    def GetCurrentProcess(self):
        return 7

    def ResumeThread(self, thread):
        assert thread == THREAD
        return windows._RESUME_FAILED if not self._ok("ResumeThread") else 1

    def WaitForSingleObject(self, handle, milliseconds):
        assert handle == PROCESS
        self.calls.append("WaitForSingleObject")
        if self.wait_result is not None:
            return self.wait_result
        return windows._WAIT_OBJECT_0 if self.exited else windows._WAIT_TIMEOUT

    def GetExitCodeProcess(self, handle, code):
        code._obj.value = self.exit_code
        return self._ok("GetExitCodeProcess")

    def TerminateProcess(self, handle, code):
        assert handle == PROCESS
        if not self._ok("TerminateProcess"):
            return False
        self.exited = True
        return True

    def CloseHandle(self, handle):
        self.closed.append(handle)
        return True

    def os_handle(self, fd):
        return fd

    def last_error(self):
        return windows._ERROR_MORE_DATA if self.more_data else 0

    def error(self, what):
        return OSError(f"{what} failed")


def _files():
    return tuple(SimpleNamespace(fileno=lambda fd=fd: fd) for fd in (3, 4, 5))


def _start(api: FakeKernel32) -> windows.WindowsProcess:
    return windows.WindowsProcess.start(
        api, ("worker.exe", "--flag"), Path("C:/work"), {"A": "1"}, *_files()
    )


@pytest.fixture(autouse=True)
def fast_deadlines(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(windows, "_TERMINATION_DEADLINE_SECONDS", 0.02)


def test_worker_is_owned_before_it_runs_and_inherits_only_its_handles() -> None:
    api = FakeKernel32()
    process = _start(api)

    assert process.pid == PID
    assert api.limit_flags == windows._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    assert api.create["flags"] & windows._CREATE_SUSPENDED
    assert api.create["flags"] & windows._EXTENDED_STARTUPINFO_PRESENT
    assert api.create["inherit"] is True
    assert api.create["command"] == "worker.exe --flag"
    assert api.create["cwd"] == str(Path("C:/work"))
    assert api.create["std_flags"] == windows._STARTF_USESTDHANDLES
    assert api.create["std"] == api.duplicates == api.handle_list == [1003, 1004, 1005]
    assert api.create["attributes"] is not None
    assert api.calls.index("AssignProcessToJobObject") < api.calls.index("ResumeThread")
    assert api.calls.index("CreateProcessW") < api.calls.index("AssignProcessToJobObject")
    # Inheritable duplicates and the thread handle do not outlive the launch.
    assert sorted(api.closed) == [THREAD, 1003, 1004, 1005]


@pytest.mark.parametrize(
    ("failure", "expect_terminate_process", "expect_terminate_job"),
    [
        ("CreateProcessW", False, False),
        ("AssignProcessToJobObject", True, False),
        ("ResumeThread", False, True),
    ],
)
def test_launch_failures_stop_the_suspended_worker_and_release_handles(
    failure: str, expect_terminate_process: bool, expect_terminate_job: bool
) -> None:
    api = FakeKernel32(fail=failure)
    with pytest.raises(OSError, match=failure):
        _start(api)

    assert ("TerminateProcess" in api.calls) is expect_terminate_process
    assert ("TerminateJobObject" in api.calls) is expect_terminate_job
    assert "ResumeThread" not in api.calls or failure == "ResumeThread"
    assert JOB in api.closed
    assert {1003, 1004, 1005} <= set(api.closed)
    if failure != "CreateProcessW":
        # The suspended worker's exit was confirmed before the error settled.
        assert "WaitForSingleObject" in api.calls
        assert {PROCESS, THREAD} <= set(api.closed)


def test_setup_failure_whose_termination_fails_does_not_settle() -> None:
    class TerminationFails(FakeKernel32):
        def TerminateProcess(self, handle, code):
            self.calls.append("TerminateProcess")
            return False

    api = TerminationFails(fail="AssignProcessToJobObject", exited=False)
    with pytest.raises(OwnershipError, match=f"pid {PID}") as raised:
        _start(api)

    assert not isinstance(raised.value, OSError)
    assert "AssignProcessToJobObject" in str(raised.value.__cause__)
    assert {PROCESS, THREAD, JOB} <= set(api.closed)


def test_setup_failure_whose_worker_does_not_exit_does_not_settle() -> None:
    class StaysAlive(FakeKernel32):
        def TerminateProcess(self, handle, code):
            self.calls.append("TerminateProcess")
            return True  # Reported success, but the process never signals.

    api = StaysAlive(fail="AssignProcessToJobObject", exited=False)
    with pytest.raises(OwnershipError):
        _start(api)


def test_resume_failure_falls_back_to_terminating_the_process() -> None:
    class JobTerminationFails(FakeKernel32):
        def TerminateJobObject(self, job, code):
            self.calls.append("TerminateJobObject")
            return False

    api = JobTerminationFails(fail="ResumeThread", exited=False)
    with pytest.raises(OSError, match="ResumeThread"):
        _start(api)

    assert "TerminateProcess" in api.calls


def test_exited_leader_with_empty_job_did_not_linger() -> None:
    api = FakeKernel32(exit_code=3)
    process = _start(api)

    assert process.wait(1.0) is True
    assert process.cleanup() is False
    assert process.returncode == 3
    assert "TerminateJobObject" not in api.calls


def test_stale_leader_entry_is_not_a_lingering_descendant() -> None:
    api = FakeKernel32(members=(PID,))
    process = _start(api)

    assert process.wait(1.0) is True
    assert process.cleanup() is False
    assert "TerminateJobObject" not in api.calls


def test_descendant_present_at_leader_exit_is_terminated_and_reported() -> None:
    # No grace period: a child alive when the leader is reaped lingers, even if
    # it would have exited a moment later.
    api = FakeKernel32(members=(PID, CHILD))
    process = _start(api)

    assert process.wait(1.0) is True
    assert process.cleanup() is True
    assert "TerminateJobObject" in api.calls


def test_more_members_than_the_buffer_holds_counts_as_lingering() -> None:
    api = FakeKernel32(more_data=True)
    process = _start(api)

    assert process.wait(1.0) is True
    assert process.cleanup() is True
    assert "TerminateJobObject" in api.calls


def test_timeout_terminates_job_and_settles_leader() -> None:
    api = FakeKernel32(exited=False, members=(PID, CHILD))
    process = _start(api)

    assert process.wait(0.01) is False
    assert process.cleanup() is True
    assert process.returncode == windows._TERMINATED_EXIT_CODE


def test_unconfirmed_termination_raises_instead_of_settling() -> None:
    api = FakeKernel32(members=(PID, CHILD), active_after_terminate=1)
    process = _start(api)
    process.wait(1.0)

    with pytest.raises(OwnershipError, match="did not stop"):
        process.cleanup()


def test_wait_failure_is_an_error_not_a_timeout() -> None:
    api = FakeKernel32(exited=False)
    process = _start(api)
    api.wait_result = 0xFFFFFFFF

    with pytest.raises(OSError, match="WaitForSingleObject"):
        process.wait(0.01)
    with pytest.raises(OSError, match="WaitForSingleObject"):
        process.poll()


def test_close_releases_process_and_job() -> None:
    api = FakeKernel32()
    process = _start(api)
    process.close()

    assert api.closed[-2:] == [PROCESS, JOB]


@pytest.mark.parametrize(
    ("argv", "expected"),
    [
        (("plain", "two words"), 'plain "two words"'),
        (("", "a"), '"" a'),
        (('say "hi"',), '"say \\"hi\\""'),
        (("C:\\dir\\",), "C:\\dir\\"),
        (("C:\\with space\\",), '"C:\\with space\\\\"'),
        (('back\\\\"slash',), '"back\\\\\\\\\\"slash"'),
    ],
)
def test_command_line_follows_c_runtime_rules(argv: tuple[str, ...], expected: str) -> None:
    assert windows.command_line(argv) == expected


def test_environment_block_is_sorted_and_double_terminated() -> None:
    assert windows.environment_block({"b": "2", "A": "1", "=C:": "C:\\"}) == (
        "=C:=C:\\\0A=1\0b=2\0\0"
    )
    for key, value in (("a=b", "x"), ("", "x"), ("a\0", "x"), ("a", "\0")):
        with pytest.raises(ValueError):
            windows.environment_block({key: value})


def test_start_process_on_windows_uses_a_job_owned_launch(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    api = FakeKernel32()
    monkeypatch.setattr(platform, "os", SimpleNamespace(name="nt", devnull=os.devnull))
    monkeypatch.setattr(windows, "api", lambda: api)
    with (tmp_path / "out").open("wb") as stdout, (tmp_path / "err").open("wb") as stderr:
        process = platform.start_process(("worker",), tmp_path, {}, stdout, stderr)
        assert api.create["std"][1:] == [1000 + stdout.fileno(), 1000 + stderr.fileno()]

    assert isinstance(process, windows.WindowsProcess)
    assert process.pid == PID
