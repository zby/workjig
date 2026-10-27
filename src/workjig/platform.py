"""Operating-system policies used by the runtime.

The rest of Workjig deals in runtime concepts such as a held run lock and a
durable record. This module is the small adapter where those concepts are
translated into platform APIs.
"""

from __future__ import annotations

import os
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
