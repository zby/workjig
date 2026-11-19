"""Capture the Codex exec thread ID and query its app-server metadata."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from pathlib import Path
from threading import Event
from typing import BinaryIO

from . import _session_guard, _session_receipt, _windows
from ._session_observer import _CHUNK, _UUID, EventObserver, _object, observe_stdout
from .platform import OwnershipError, resolve_command

_REPLY_LIMIT = 256 * 1024
_LOOKUP_SECONDS = 2.0


def _codex_id(event: dict) -> str | None:
    native_id = event.get("thread_id")
    if (
        event.get("type") == "thread.started"
        and isinstance(native_id, str)
        and _UUID.fullmatch(native_id)
    ):
        return native_id
    return None


class _Events(EventObserver):
    """Frame Codex JSONL records and extract canonical thread.started IDs."""

    def __init__(self) -> None:
        super().__init__(_codex_id)


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
                    (
                        sys.executable,
                        "-I",
                        _session_guard.__file__,
                        str(_LOOKUP_SECONDS + 1),
                        *command,
                    ),
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
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired as exc:
        raise OwnershipError(f"app-server pid {proc.pid} did not stop") from exc


@contextmanager
def capture_codex(stdout_path: Path, argv: tuple[str, ...], cwd: Path, env: Mapping[str, str]):
    """Observe alongside the worker; leave this context only after group cleanup."""
    stopped = Event()

    def publish(native_id: str, path: Path | None) -> None:
        try:
            _session_receipt.publish(stdout_path.parent, "codex", native_id, path)
        except OSError:
            pass  # Optional evidence; never change the worker outcome.

    def locate(native_id: str) -> bool:
        try:
            path = lookup_codex_path(native_id, argv, cwd, env)
        except OSError:
            return False
        if path is None:
            return False
        publish(native_id, path)
        return True

    def observe() -> str | None:
        native_id = observe_stdout(stdout_path, stopped, _codex_id, lambda nid: publish(nid, None))
        if native_id and not stopped.is_set() and locate(native_id):
            return None
        return native_id

    with ThreadPoolExecutor(max_workers=1) as pool:
        observation = pool.submit(observe)
        try:
            yield
        finally:
            stopped.set()
        native_id = observation.result()
    if native_id:
        # An early thread/read can precede persistence. Try once after settlement.
        locate(native_id)
