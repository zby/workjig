"""Windows process ownership through job objects.

Each launch is created suspended, assigned to a fresh job object and only then
resumed, so a worker cannot spawn anything before it is owned. Settlement
confirms the whole job has stopped within a bounded deadline. Only documented
kernel32 APIs are used, through ctypes. They are bound lazily so this module
imports on any platform and the policy can be tested against a fake API.
"""

from __future__ import annotations

import ctypes
import time
from collections.abc import Mapping
from ctypes import wintypes
from pathlib import Path
from typing import Any, BinaryIO

_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
_JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
_PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x20002
_STARTF_USESTDHANDLES = 0x100
_CREATE_SUSPENDED = 0x4
_CREATE_NEW_PROCESS_GROUP = 0x200
_CREATE_UNICODE_ENVIRONMENT = 0x400
_EXTENDED_STARTUPINFO_PRESENT = 0x80000
_CREATION_FLAGS = (
    _CREATE_SUSPENDED
    | _CREATE_NEW_PROCESS_GROUP
    | _CREATE_UNICODE_ENVIRONMENT
    | _EXTENDED_STARTUPINFO_PRESENT
)
_DUPLICATE_SAME_ACCESS = 0x2
_WAIT_OBJECT_0 = 0
_INFINITE = 0xFFFFFFFF
_RESUME_FAILED = 0xFFFFFFFF
_TERMINATED_EXIT_CODE = 1
_LINGER_GRACE_SECONDS = 0.25
_TERMINATION_DEADLINE_SECONDS = 1.0


class _IoCounters(ctypes.Structure):
    _fields_ = [
        (name, ctypes.c_ulonglong)
        for name in (
            "ReadOperationCount",
            "WriteOperationCount",
            "OtherOperationCount",
            "ReadTransferCount",
            "WriteTransferCount",
            "OtherTransferCount",
        )
    ]


class _BasicLimitInformation(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", wintypes.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", wintypes.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", wintypes.DWORD),
        ("SchedulingClass", wintypes.DWORD),
    ]


class ExtendedLimitInformation(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", _BasicLimitInformation),
        ("IoInfo", _IoCounters),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class BasicAccountingInformation(ctypes.Structure):
    _fields_ = [
        ("TotalUserTime", ctypes.c_longlong),
        ("TotalKernelTime", ctypes.c_longlong),
        ("ThisPeriodTotalUserTime", ctypes.c_longlong),
        ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
        ("TotalPageFaultCount", wintypes.DWORD),
        ("TotalProcesses", wintypes.DWORD),
        ("ActiveProcesses", wintypes.DWORD),
        ("TotalTerminatedProcesses", wintypes.DWORD),
    ]


class _StartupInfo(ctypes.Structure):
    _fields_ = [
        ("cb", wintypes.DWORD),
        ("lpReserved", wintypes.LPWSTR),
        ("lpDesktop", wintypes.LPWSTR),
        ("lpTitle", wintypes.LPWSTR),
        ("dwX", wintypes.DWORD),
        ("dwY", wintypes.DWORD),
        ("dwXSize", wintypes.DWORD),
        ("dwYSize", wintypes.DWORD),
        ("dwXCountChars", wintypes.DWORD),
        ("dwYCountChars", wintypes.DWORD),
        ("dwFillAttribute", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("wShowWindow", wintypes.WORD),
        ("cbReserved2", wintypes.WORD),
        ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
        ("hStdInput", wintypes.HANDLE),
        ("hStdOutput", wintypes.HANDLE),
        ("hStdError", wintypes.HANDLE),
    ]


class StartupInfoEx(ctypes.Structure):
    _fields_ = [("StartupInfo", _StartupInfo), ("lpAttributeList", wintypes.LPVOID)]


class ProcessInformation(ctypes.Structure):
    _fields_ = [
        ("hProcess", wintypes.HANDLE),
        ("hThread", wintypes.HANDLE),
        ("dwProcessId", wintypes.DWORD),
        ("dwThreadId", wintypes.DWORD),
    ]


_SIGNATURES: dict[str, tuple[list[Any], Any]] = {
    "CreateJobObjectW": ([wintypes.LPVOID, wintypes.LPCWSTR], wintypes.HANDLE),
    "SetInformationJobObject": (
        [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD],
        wintypes.BOOL,
    ),
    "QueryInformationJobObject": (
        [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD, wintypes.LPVOID],
        wintypes.BOOL,
    ),
    "AssignProcessToJobObject": ([wintypes.HANDLE, wintypes.HANDLE], wintypes.BOOL),
    "TerminateJobObject": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
    "CreateProcessW": (
        [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            wintypes.LPVOID,
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.LPCWSTR,
            wintypes.LPVOID,
            wintypes.LPVOID,
        ],
        wintypes.BOOL,
    ),
    "InitializeProcThreadAttributeList": (
        [wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID],
        wintypes.BOOL,
    ),
    "UpdateProcThreadAttribute": (
        [
            wintypes.LPVOID,
            wintypes.DWORD,
            ctypes.c_size_t,
            wintypes.LPVOID,
            ctypes.c_size_t,
            wintypes.LPVOID,
            wintypes.LPVOID,
        ],
        wintypes.BOOL,
    ),
    "DeleteProcThreadAttributeList": ([wintypes.LPVOID], None),
    "DuplicateHandle": (
        [
            wintypes.HANDLE,
            wintypes.HANDLE,
            wintypes.HANDLE,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ],
        wintypes.BOOL,
    ),
    "GetCurrentProcess": ([], wintypes.HANDLE),
    "ResumeThread": ([wintypes.HANDLE], wintypes.DWORD),
    "WaitForSingleObject": ([wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
    "GetExitCodeProcess": ([wintypes.HANDLE, wintypes.LPVOID], wintypes.BOOL),
    "TerminateProcess": ([wintypes.HANDLE, wintypes.UINT], wintypes.BOOL),
    "CloseHandle": ([wintypes.HANDLE], wintypes.BOOL),
}


class Kernel32:
    """The kernel32 entry points the policy uses, plus two host helpers.

    `error` must be called before any further API call, because ctypes keeps
    only the most recent last-error value.
    """

    def __init__(self) -> None:
        import msvcrt

        self._msvcrt = msvcrt
        library = ctypes.WinDLL("kernel32", use_last_error=True)
        for name, (argtypes, restype) in _SIGNATURES.items():
            function = getattr(library, name)
            function.argtypes = argtypes
            function.restype = restype
            setattr(self, name, function)

    def os_handle(self, fd: int) -> int:
        return self._msvcrt.get_osfhandle(fd)

    def error(self, what: str) -> OSError:
        code = ctypes.get_last_error()
        failure = ctypes.WinError(code)
        return OSError(failure.errno, f"{what}: {failure.strerror}", None, code)


_api: Any = None


def api() -> Any:
    global _api
    if _api is None:
        _api = Kernel32()
    return _api


def command_line(argv: tuple[str, ...]) -> str:
    """Quote arguments the way the Microsoft C runtime parses them."""

    parts = []
    for argument in argv:
        if argument and not any(character in argument for character in ' \t"'):
            parts.append(argument)
            continue
        quoted = ['"']
        backslashes = 0
        for character in argument:
            if character == "\\":
                backslashes += 1
            elif character == '"':
                quoted.append("\\" * (backslashes * 2 + 1) + '"')
                backslashes = 0
            else:
                quoted.append("\\" * backslashes + character)
                backslashes = 0
        quoted.append("\\" * (backslashes * 2) + '"')
        parts.append("".join(quoted))
    return " ".join(parts)


def environment_block(env: Mapping[str, str]) -> str:
    """Build a double-NUL terminated Unicode environment block."""

    for key, value in env.items():
        if not key or "\0" in key or "\0" in value or "=" in key[1:]:
            raise ValueError(f"invalid environment variable {key!r}")
    entries = sorted(env.items(), key=lambda item: item[0].upper())
    return "".join(f"{key}={value}\0" for key, value in entries) + "\0"


class WindowsProcess:
    """A worker that is a member of its own job object from its first instruction."""

    def __init__(self, api: Any, job: int, process: int, pid: int) -> None:
        self._api = api
        self._job = job
        self._process = process
        self.pid = pid
        self.returncode: int | None = None

    @classmethod
    def start(
        cls,
        api: Any,
        argv: tuple[str, ...],
        cwd: Path,
        env: Mapping[str, str],
        stdin: BinaryIO,
        stdout: BinaryIO,
        stderr: BinaryIO,
    ) -> WindowsProcess:
        job = api.CreateJobObjectW(None, None)
        if not job:
            raise api.error("CreateJobObjectW")
        try:
            limits = ExtendedLimitInformation()
            limits.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not api.SetInformationJobObject(
                job,
                _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
                ctypes.byref(limits),
                ctypes.sizeof(limits),
            ):
                raise api.error("SetInformationJobObject")
            information = cls._create_suspended(api, argv, cwd, env, (stdin, stdout, stderr))
        except BaseException:
            api.CloseHandle(job)
            raise
        if not api.AssignProcessToJobObject(job, information.hProcess):
            error = api.error("AssignProcessToJobObject")
            api.TerminateProcess(information.hProcess, _TERMINATED_EXIT_CODE)
            cls._close_all(api, information.hThread, information.hProcess, job)
            raise error
        if api.ResumeThread(information.hThread) == _RESUME_FAILED:
            error = api.error("ResumeThread")
            api.TerminateJobObject(job, _TERMINATED_EXIT_CODE)
            cls._close_all(api, information.hThread, information.hProcess, job)
            raise error
        api.CloseHandle(information.hThread)
        return cls(api, job, information.hProcess, information.dwProcessId)

    @classmethod
    def _create_suspended(
        cls,
        api: Any,
        argv: tuple[str, ...],
        cwd: Path,
        env: Mapping[str, str],
        standard: tuple[BinaryIO, BinaryIO, BinaryIO],
    ) -> ProcessInformation:
        # Inheritable duplicates exist only for the duration of CreateProcessW,
        # and the handle list restricts inheritance to exactly these three, so
        # a concurrent launch on another thread cannot capture them.
        current = api.GetCurrentProcess()
        inherited: list[int] = []
        attributes = None
        try:
            for file in standard:
                duplicate = wintypes.HANDLE()
                if not api.DuplicateHandle(
                    current,
                    api.os_handle(file.fileno()),
                    current,
                    ctypes.byref(duplicate),
                    0,
                    True,
                    _DUPLICATE_SAME_ACCESS,
                ):
                    raise api.error("DuplicateHandle")
                inherited.append(duplicate.value)
            size = ctypes.c_size_t(0)
            api.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))
            attributes = ctypes.create_string_buffer(size.value)
            if not api.InitializeProcThreadAttributeList(attributes, 1, 0, ctypes.byref(size)):
                attributes = None
                raise api.error("InitializeProcThreadAttributeList")
            handles = (wintypes.HANDLE * len(inherited))(*inherited)
            if not api.UpdateProcThreadAttribute(
                attributes,
                0,
                _PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                handles,
                ctypes.sizeof(handles),
                None,
                None,
            ):
                raise api.error("UpdateProcThreadAttribute")
            startup = StartupInfoEx()
            startup.StartupInfo.cb = ctypes.sizeof(startup)
            startup.StartupInfo.dwFlags = _STARTF_USESTDHANDLES
            startup.StartupInfo.hStdInput = inherited[0]
            startup.StartupInfo.hStdOutput = inherited[1]
            startup.StartupInfo.hStdError = inherited[2]
            startup.lpAttributeList = ctypes.cast(attributes, wintypes.LPVOID)
            information = ProcessInformation()
            command = ctypes.create_unicode_buffer(command_line(argv))
            block = ctypes.create_unicode_buffer(environment_block(env))
            if not api.CreateProcessW(
                None,
                command,
                None,
                None,
                True,
                _CREATION_FLAGS,
                ctypes.cast(block, wintypes.LPVOID),
                str(cwd),
                ctypes.byref(startup),
                ctypes.byref(information),
            ):
                raise api.error("CreateProcessW")
            return information
        finally:
            if attributes is not None:
                api.DeleteProcThreadAttributeList(attributes)
            cls._close_all(api, *inherited)

    @staticmethod
    def _close_all(api: Any, *handles: int) -> None:
        for handle in handles:
            api.CloseHandle(handle)

    def poll(self) -> int | None:
        if self.returncode is None and self._api.WaitForSingleObject(self._process, 0) == (
            _WAIT_OBJECT_0
        ):
            code = wintypes.DWORD()
            if not self._api.GetExitCodeProcess(self._process, ctypes.byref(code)):
                raise self._api.error("GetExitCodeProcess")
            self.returncode = code.value
        return self.returncode

    def wait(self, timeout: float | None) -> bool:
        milliseconds = _INFINITE if timeout is None else min(int(timeout * 1000), _INFINITE - 1)
        self._api.WaitForSingleObject(self._process, max(milliseconds, 0))
        return self.poll() is not None

    def active_processes(self) -> int:
        accounting = BasicAccountingInformation()
        if not self._api.QueryInformationJobObject(
            self._job,
            _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(accounting),
            ctypes.sizeof(accounting),
            None,
        ):
            raise self._api.error("QueryInformationJobObject")
        return accounting.ActiveProcesses

    def _drained(self, seconds: float) -> bool:
        deadline = time.monotonic() + seconds
        while True:
            if self.active_processes() == 0:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)

    def cleanup(self) -> bool:
        """Stop every member of the job and report whether anything lingered.

        Raises when termination cannot be confirmed within the deadline, so
        the caller leaves the reservation unresolved instead of recording an
        outcome for processes that may still run.
        """

        # The leader's handle and the job's accounting are not updated
        # atomically, so an exited leader is given a moment to leave the count.
        if self.poll() is not None and self._drained(_LINGER_GRACE_SECONDS):
            return False
        if not self._api.TerminateJobObject(self._job, _TERMINATED_EXIT_CODE):
            raise self._api.error("TerminateJobObject")
        if not self._drained(_TERMINATION_DEADLINE_SECONDS) or not self.wait(
            _TERMINATION_DEADLINE_SECONDS
        ):
            raise OSError(f"worker job for pid {self.pid} did not stop after termination")
        return True

    def close(self) -> None:
        self._close_all(self._api, self._process, self._job)
