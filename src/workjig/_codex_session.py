"""Capture the Codex exec thread ID and query its app-server metadata."""

from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import time
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path
from threading import Event
from typing import BinaryIO

from . import _windows
from .platform import OwnedProcess, OwnershipError, cleanup_process_group, resolve_command

_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_CHUNK = 64 * 1024
_REPLY_LIMIT = 256 * 1024
_LOOKUP_SECONDS = 2.0
_POLL_SECONDS = 0.025


def _object(line: bytes) -> dict:
    if not line.lstrip().startswith(b"{"):
        return {}
    try:
        value = json.loads(line.decode("utf-8"))
    except (ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


class _Events:
    """Frame complete JSONL records across reads and discard oversized lines whole."""

    def __init__(self) -> None:
        self.pending = b""
        self.skipping = False

    def feed(self, chunk: bytes) -> str | None:
        parts = chunk.split(b"\n")
        for part in parts[:-1]:
            if not self.skipping and len(self.pending) + len(part) <= _CHUNK:
                event = _object(self.pending + part)
                native_id = event.get("thread_id")
                if (
                    event.get("type") == "thread.started"
                    and isinstance(native_id, str)
                    and _UUID.fullmatch(native_id)
                ):
                    return native_id
            self.skipping = False
            self.pending = b""
        self.skipping |= len(self.pending) + len(parts[-1]) > _CHUNK
        self.pending = b"" if self.skipping else self.pending + parts[-1]
        return None


def _messages(reader: BinaryIO, deadline: float) -> Iterator[dict]:
    """Read only available bytes. Even an inherited, silent pipe has a deadline."""
    fd = reader.fileno()
    if os.name == "nt":
        import ctypes
        import msvcrt
        from ctypes import wintypes

        peek = ctypes.WinDLL("kernel32", use_last_error=True).PeekNamedPipe
        peek.argtypes = [
            wintypes.HANDLE,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        peek.restype = wintypes.BOOL
        handle = msvcrt.get_osfhandle(fd)
        available = wintypes.DWORD()
    else:
        os.set_blocking(fd, False)
    pending = bytearray()
    remaining = _REPLY_LIMIT
    while remaining and time.monotonic() < deadline:
        size = min(remaining, _CHUNK)
        if os.name == "nt":
            if not peek(handle, None, 0, None, ctypes.byref(available), None):
                return  # Includes broken pipe.
            size = min(size, available.value)
        if size:
            try:
                chunk = os.read(fd, size)
            except BlockingIOError:
                chunk = None
            if chunk == b"":
                return
            if chunk:
                remaining -= len(chunk)
                parts = chunk.split(b"\n")
                pending.extend(parts[0])
                for part in parts[1:]:
                    if time.monotonic() >= deadline:
                        return
                    yield _object(bytes(pending))
                    pending = bytearray(part)
                continue
        time.sleep(min(0.01, max(0, deadline - time.monotonic())))


def _reply(messages: Iterator[dict], request_id: int) -> dict:
    for message in messages:
        if type(message.get("id")) is int and message["id"] == request_id:
            result = message.get("result")
            if "method" not in message and "error" not in message and isinstance(result, dict):
                return result
            break
    return {}


def _exchange(reader: BinaryIO, writer: BinaryIO, native_id: str, deadline: float) -> Path | None:
    def send(message: dict) -> None:
        writer.write(json.dumps(message).encode("ascii") + b"\n")
        writer.flush()

    send(
        {
            "id": 1,
            "method": "initialize",
            "params": {"clientInfo": {"name": "workjig", "version": "1"}},
        }
    )
    messages = _messages(reader, deadline)
    if not _reply(messages, 1):
        return None
    send({"method": "initialized", "params": {}})
    send(
        {"id": 2, "method": "thread/read", "params": {"threadId": native_id, "includeTurns": False}}
    )
    thread = _reply(messages, 2).get("thread")
    if not isinstance(thread, dict) or thread.get("id") != native_id:
        return None
    path = thread.get("path")
    if (
        isinstance(path, str)
        and "\0" not in path
        and Path(path).is_absolute()
        and len(json.dumps(path)) <= 60 * 1024  # Keep the escaped path inside the receipt limit.
    ):
        path.encode("utf-8")  # Reject lone surrogates before passing to record persistence.
        return Path(path)
    return None


def _pipe(files: ExitStack) -> tuple[BinaryIO, BinaryIO]:
    read, write = os.pipe()
    return files.enter_context(os.fdopen(read, "rb")), files.enter_context(os.fdopen(write, "wb"))


def lookup_codex_path(
    native_id: str, argv: tuple[str, ...], cwd: Path, env: Mapping[str, str]
) -> Path | None:
    """Query one exact thread through a short-lived, separately owned stdio server."""
    if not isinstance(native_id, str) or not _UUID.fullmatch(native_id):
        return None
    command = resolve_command((argv[0], "app-server"), env)
    deadline = time.monotonic() + _LOOKUP_SECONDS
    with ExitStack() as files:
        stdin, writer = _pipe(files)
        reader, stdout = _pipe(files)
        stderr = files.enter_context(open(os.devnull, "wb"))

        try:
            if os.name == "nt":
                job = _windows.WindowsProcess.start(
                    _windows.api(), command, cwd, env, stdin, stdout, stderr
                )
                files.callback(job.close)
                files.callback(_cleanup_helper, job.cleanup)
            else:
                proc = subprocess.Popen(
                    command,
                    cwd=cwd,
                    env=env,
                    stdin=stdin,
                    stdout=stdout,
                    stderr=stderr,
                    start_new_session=True,
                )
                files.callback(_cleanup_helper, lambda: _stop_group(proc))
        except OSError:
            return None
        stdin.close()
        stdout.close()
        try:
            return _exchange(reader, writer, native_id, deadline)
        except (OSError, ValueError):
            return None


def _cleanup_helper(cleanup: Callable[[], object]) -> None:
    try:
        cleanup()
    except OSError as exc:
        raise OwnershipError("could not confirm app-server cleanup") from exc


def _stop_group(proc: subprocess.Popen) -> None:
    cleanup_process_group(proc)
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired as exc:
        raise OwnershipError(f"app-server pid {proc.pid} did not stop") from exc


def wait_for_codex(
    process: OwnedProcess,
    timeout: float,
    stdout_path: Path,
    publish: Callable[[str, Path | None], None],
    argv: tuple[str, ...],
    cwd: Path,
    env: Mapping[str, str],
) -> bool:
    """Return the owned leader's timed wait result. The caller still settles the group.

    The observer publishes the ID. After it joins, the caller thread can publish a path.
    Timed-out workers and lingering descendants need cleanup before lookup_codex_path.
    """
    deadline = time.monotonic() + timeout
    stopped = Event()

    def publish_pointer(native_id: str, path: Path | None) -> None:
        try:
            publish(native_id, path)
        except OSError:
            # Receipt storage is advisory to the worker's execution result.
            pass

    # Keep the authoritative wait independent of JSON parsing and durable publication.
    with ThreadPoolExecutor(max_workers=1) as observer:
        observation = observer.submit(_observe, stdout_path, stopped, publish_pointer)
        try:
            exited = process.wait(max(0.0, deadline - time.monotonic()))
        finally:
            stopped.set()
        native_id = observation.result()
    if exited and native_id is not None:
        try:
            path = lookup_codex_path(native_id, argv, cwd, env) if _group_empty(process) else None
        except OSError:
            path = None
        if path is not None:
            publish_pointer(native_id, path)
    return exited


def _observe(
    stdout_path: Path, stopped: Event, publish: Callable[[str, Path | None], None]
) -> str | None:
    events = _Events()
    final_bytes = 1024 * 1024
    # Read only a regular launch log. A replaced FIFO must not stall settlement.
    try:
        if stdout_path.is_symlink() or not stdout_path.is_file():
            return None
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        stream = os.fdopen(os.open(stdout_path, flags), "rb")
    except OSError:
        return None
    with stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            return None
        while True:
            final = stopped.is_set()
            try:
                chunk = stream.read(_CHUNK)
            except OSError:
                return None
            native_id = events.feed(chunk)
            if native_id is not None:
                publish(native_id, None)
                return native_id
            if final:
                # A descendant may still be appending. Never chase its writes.
                final_bytes -= len(chunk)
                if len(chunk) < _CHUNK or final_bytes == 0:
                    return None
            elif len(chunk) < _CHUNK:
                stopped.wait(_POLL_SECONDS)


def _group_empty(process: OwnedProcess) -> bool:
    """Only spend time on metadata when there is nothing left for the caller to stop."""
    if os.name == "nt":
        return isinstance(process, _windows.WindowsProcess) and not process.descendants_present()
    try:
        os.killpg(process.pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        pass
    return False
