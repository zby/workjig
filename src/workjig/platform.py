"""Operating-system policies used by the runtime.

The rest of Workjig deals in runtime concepts such as a held run lock and an
owned worker process. This module is the small adapter where those concepts
are translated into platform APIs.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from collections.abc import Mapping
from pathlib import Path
from typing import BinaryIO, Protocol


class FileLock(Protocol):
    """The platform operations required by the run ownership lock."""

    def acquire(self, file: BinaryIO) -> None: ...

    def release(self, file: BinaryIO) -> None: ...


class _PosixFileLock:
    def __init__(self) -> None:
        import fcntl

        self._fcntl = fcntl

    def acquire(self, file: BinaryIO) -> None:
        self._fcntl.flock(file.fileno(), self._fcntl.LOCK_EX | self._fcntl.LOCK_NB)

    def release(self, file: BinaryIO) -> None:
        self._fcntl.flock(file.fileno(), self._fcntl.LOCK_UN)


class _WindowsFileLock:
    def __init__(self) -> None:
        import msvcrt

        self._msvcrt = msvcrt

    def acquire(self, file: BinaryIO) -> None:
        self._rewind(file)
        try:
            self._msvcrt.locking(file.fileno(), self._msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise BlockingIOError(error.errno, error.strerror) from error

    def release(self, file: BinaryIO) -> None:
        self._rewind(file)
        self._msvcrt.locking(file.fileno(), self._msvcrt.LK_UNLCK, 1)

    @staticmethod
    def _rewind(file: BinaryIO) -> None:
        # msvcrt.locking works at the descriptor's position. A buffered seek
        # may only move the buffer cursor, so position the descriptor itself.
        file.flush()
        os.lseek(file.fileno(), 0, os.SEEK_SET)


def file_lock() -> FileLock:
    """Return the native non-blocking file-lock adapter for this OS."""

    return _WindowsFileLock() if os.name == "nt" else _PosixFileLock()


def sync_directory(path: Path) -> None:
    """Durably flush a directory entry where the platform supports it."""

    if os.name == "nt":
        return
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def resolve_command(argv: tuple[str, ...]) -> tuple[str, ...]:
    """Resolve a console executable through PATH without changing the command contract."""

    executable = shutil.which(argv[0])
    return (executable, *argv[1:]) if executable is not None else argv


class OwnershipError(RuntimeError):
    """The runtime could not confirm that a process it started has stopped.

    Unlike an OSError at launch this must not settle the launch: the harness
    lets it propagate so the reservation stays unresolved for explicit recovery.
    """


class OwnedProcess(Protocol):
    """A worker whose whole process tree the runtime can stop and account for."""

    pid: int
    returncode: int | None

    def wait(self, timeout: float | None) -> bool:
        """Wait for the leader to exit; return False if the timeout elapsed first."""

    def cleanup(self) -> bool:
        """Stop everything owned and report whether anything lingered.

        Returns with the leader reaped and `returncode` set, or raises
        OwnershipError when termination cannot be confirmed so the launch
        stays unresolved.
        """

    def close(self) -> None:
        """Release platform handles after settlement."""


def start_process(
    argv: tuple[str, ...],
    cwd: Path,
    env: Mapping[str, str],
    stdout: BinaryIO,
    stderr: BinaryIO,
) -> OwnedProcess:
    """Start a worker that is owned from its first instruction.

    POSIX gives the worker its own session before `exec`; Windows creates it
    suspended inside a dedicated job object and resumes it once assigned.
    """

    if os.name == "nt":
        from . import _windows

        with open(os.devnull, "rb") as stdin:
            return _windows.WindowsProcess.start(
                _windows.api(), argv, cwd, env, stdin, stdout, stderr
            )
    return _PosixProcess(argv, cwd, env, stdout, stderr)


class _PosixProcess:
    def __init__(
        self,
        argv: tuple[str, ...],
        cwd: Path,
        env: Mapping[str, str],
        stdout: BinaryIO,
        stderr: BinaryIO,
    ) -> None:
        self._popen = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        self.pid = self._popen.pid

    @property
    def returncode(self) -> int | None:
        return self._popen.returncode

    def wait(self, timeout: float | None) -> bool:
        try:
            self._popen.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return False
        return True

    def cleanup(self) -> bool:
        lingered = cleanup_process_group(self._popen)
        if self._popen.poll() is None:
            # Group termination can return before the leader's exit status is
            # collectable. Settle it here rather than letting a TimeoutExpired
            # escape: a launch must reach a terminal outcome or stay unresolved.
            try:
                self._popen.wait(timeout=1)
            except subprocess.TimeoutExpired:
                lingered = True
                self._popen.kill()
                self._popen.wait()
        return lingered

    def close(self) -> None:
        return None


def cleanup_process_group(process: subprocess.Popen[bytes]) -> bool:
    """Terminate a POSIX session and report whether any member still lived."""

    pgid = process.pid
    process.poll()
    if not _posix_group_exists(pgid):
        return False
    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return False
    deadline = time.monotonic() + 0.25
    while time.monotonic() < deadline:
        # Reap the leader so its zombie does not keep an empty group alive.
        # Still inspect the group: surviving children need cleanup too.
        process.poll()
        if not _posix_group_exists(pgid):
            return True
        time.sleep(0.01)
    try:
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    return True


def _posix_group_exists(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    return True
