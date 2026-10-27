"""Operating-system policies used by the runtime.

The rest of Workjig deals in runtime concepts such as a held run lock and a
durable record. This module is the small adapter where those concepts are
translated into platform APIs.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from pathlib import Path
from typing import Any, BinaryIO, Protocol


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
        file.seek(0)
        try:
            self._msvcrt.locking(file.fileno(), self._msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise BlockingIOError(error.errno, error.strerror) from error

    def release(self, file: BinaryIO) -> None:
        file.seek(0)
        self._msvcrt.locking(file.fileno(), self._msvcrt.LK_UNLCK, 1)


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
    """Resolve a console executable on Windows without changing the command contract."""

    if os.name != "nt":
        return argv
    executable = shutil.which(argv[0])
    return (executable, *argv[1:]) if executable is not None else argv


def process_creation_options() -> dict[str, Any]:
    """Return subprocess options that give each worker its own process group."""

    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


def cleanup_process_group(process: subprocess.Popen[bytes]) -> bool:
    """Terminate the worker group and report whether descendants lingered."""

    if os.name == "nt":
        # Never send a recycled PID to taskkill after the leader has already
        # exited. A job-object implementation can later replace this fallback
        # to cover descendants that outlive their leader.
        if process.poll() is not None:
            return False
        completed = subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return completed.returncode == 0

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
