"""Cross-platform non-blocking locks for run records."""

from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO


class RunBusyError(RuntimeError):
    """Another controller already owns that record directory."""


if os.name == "nt":
    import msvcrt
else:
    import fcntl


def acquire(file: BinaryIO) -> None:
    if os.name == "nt":
        file.seek(0)
        try:
            msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as error:
            raise BlockingIOError(error.errno, error.strerror) from error
    else:
        fcntl.flock(file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def release(file: BinaryIO) -> None:
    if os.name == "nt":
        file.seek(0)
        msvcrt.locking(file.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(file.fileno(), fcntl.LOCK_UN)


class HeldLock:
    def __init__(self, record_dir: Path) -> None:
        self.path = Path(record_dir)
        self.file: BinaryIO | None = None

    def __enter__(self) -> Path:
        self.path.mkdir(parents=True, exist_ok=True)
        self.file = (self.path / ".lock").open("a+b")
        try:
            acquire(self.file)
        except BlockingIOError as error:
            self.file.close()
            self.file = None
            raise RunBusyError(f"run records are already owned: {self.path}") from error
        return self.path

    def __exit__(self, *_: object) -> None:
        if self.file is not None:
            release(self.file)
            self.file.close()
            self.file = None


def hold(record_dir: Path) -> HeldLock:
    return HeldLock(Path(record_dir))


def busy(record_dir: Path) -> bool:
    lock_path = Path(record_dir) / ".lock"
    if not lock_path.exists():
        return False
    with lock_path.open("a+b") as file:
        try:
            acquire(file)
        except BlockingIOError:
            return True
        release(file)
    return False
