"""Cross-platform non-blocking ownership of a run record directory."""

from __future__ import annotations

import os
from pathlib import Path
from typing import BinaryIO

from .models import WorkjigError
from .platform import file_lock


class RunBusyError(WorkjigError):
    """Another controller already owns that record directory."""


_HOLD_MARKER = b"WJH"


class RunLock:
    """The ``.lock`` file of a record directory, held exclusively while open.

    A controller that opens records and a plain ``hold`` share this one
    acquisition path. The hold marker written by ``mark_held`` lets a later
    controller distinguish a directory that was only held from one whose
    first open was interrupted before any run record was written.
    """

    def __init__(self, record_dir: Path) -> None:
        self.record_dir = Path(record_dir)
        self.path = self.record_dir / ".lock"
        self.preexisted = False
        self._file: BinaryIO | None = None

    @property
    def held(self) -> bool:
        return self._file is not None

    def acquire(self) -> None:
        """Create the directory if needed and take the lock without blocking."""
        if self._file is not None:
            raise RuntimeError("lock already held")
        self.record_dir.mkdir(parents=True, exist_ok=True)
        self.preexisted = self.path.exists()
        # Read-write without truncation, creating the file on first use.
        file = os.fdopen(os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644), "r+b")
        try:
            file_lock().acquire(file)
        except BlockingIOError as error:
            file.close()
            raise RunBusyError(f"run records are already owned: {self.record_dir}") from error
        self._file = file

    def release(self) -> None:
        file, self._file = self._file, None
        if file is not None:
            try:
                file_lock().release(file)
            finally:
                file.close()

    def mark_held(self) -> None:
        file = self._require()
        file.seek(0)
        file.write(_HOLD_MARKER)
        file.flush()

    def marked_held(self) -> bool:
        file = self._require()
        file.seek(0)
        return file.read(len(_HOLD_MARKER)) == _HOLD_MARKER

    def _require(self) -> BinaryIO:
        if self._file is None:
            raise RuntimeError("lock is not held")
        return self._file


class HeldLock:
    """Context manager that holds the run lock and marks the directory as held."""

    def __init__(self, record_dir: Path) -> None:
        self.path = Path(record_dir)
        self._lock = RunLock(self.path)

    def __enter__(self) -> Path:
        self._lock.acquire()
        try:
            self._lock.mark_held()
        except BaseException:
            self._lock.release()
            raise
        return self.path

    def __exit__(self, *_: object) -> None:
        self._lock.release()


def hold(record_dir: Path) -> HeldLock:
    """Hold the shared run lock without opening or validating run records."""
    return HeldLock(Path(record_dir))


def busy(record_dir: Path) -> bool:
    """Return whether another process currently owns the shared run lock."""
    lock = RunLock(Path(record_dir))
    if not lock.path.exists():
        return False
    try:
        lock.acquire()
    except RunBusyError:
        return True
    lock.release()
    return False
