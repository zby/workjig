"""Cross-platform non-blocking locks for run records."""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO

from .platform import file_lock


class RunBusyError(RuntimeError):
    """Another controller already owns that record directory."""


def acquire(file: BinaryIO) -> None:
    file_lock().acquire(file)


def release(file: BinaryIO) -> None:
    file_lock().release(file)


_HOLD_MARKER = b"WJH"


class HeldLock:
    """Context manager for the shared run ownership lock."""

    def __init__(self, record_dir: Path) -> None:
        self.path = Path(record_dir)
        self.file: BinaryIO | None = None

    def __enter__(self) -> Path:
        self.path.mkdir(parents=True, exist_ok=True)
        lock_path = self.path / ".lock"
        self.file = lock_path.open("r+b") if lock_path.exists() else lock_path.open("w+b")
        try:
            acquire(self.file)
            self.file.seek(0)
            self.file.write(_HOLD_MARKER)
            self.file.flush()
        except BlockingIOError as error:
            self.file.close()
            self.file = None
            raise RunBusyError(f"run records are already owned: {self.path}") from error
        return self.path

    def __exit__(self, *_: object) -> None:
        if self.file is not None:
            try:
                release(self.file)
            finally:
                self.file.close()
                self.file = None


def hold(record_dir: Path) -> HeldLock:
    """Hold the shared run lock without opening or validating run records."""
    return HeldLock(Path(record_dir))


def busy(record_dir: Path) -> bool:
    """Return whether another process currently owns the shared run lock."""
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
