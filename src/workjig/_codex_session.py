"""Query Codex app-server metadata for one thread; loaded only when a reader resolves."""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import ExitStack
from pathlib import Path
from typing import BinaryIO

from . import _session_guard, _windows
from ._session_observer import _CHUNK, _UUID, _object
from .platform import OwnershipError, resolve_command

_REPLY_LIMIT = 256 * 1024
_LOOKUP_SECONDS = 2.0

# Reasons a lookup reports through NativeSession.reason. Unconfirmed cleanup
# raises OwnershipError instead, which takes precedence over all of these.
NOT_FOUND = "codex executable not found on PATH"
NOT_STARTED = "could not start codex app-server"
NO_ANSWER = "codex app-server did not answer in time"
EXITED = "codex app-server exited without answering"
TOO_LARGE = "codex app-server reply exceeded the size limit"
ERROR_REPLY = "codex app-server returned an error"
NO_PATH = "codex app-server reported no usable transcript path"


def _messages(reader: BinaryIO, deadline: float) -> Iterator[dict]:
    """Read only available bytes. Even an inherited, silent pipe has a deadline.

    Ending without a further message raises ValueError naming why.
    """
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
                raise ValueError(EXITED)  # Includes broken pipe.
            size = min(size, available.value)
        if size:
            try:
                chunk = os.read(fd, size)
            except BlockingIOError:
                chunk = None
            if chunk == b"":
                raise ValueError(EXITED)
            if chunk:
                remaining -= len(chunk)
                parts = chunk.split(b"\n")
                pending.extend(parts[0])
                for part in parts[1:]:
                    if time.monotonic() >= deadline:
                        raise ValueError(NO_ANSWER)
                    yield _object(bytes(pending))
                    pending = bytearray(part)
                continue
        time.sleep(min(0.01, max(0, deadline - time.monotonic())))
    raise ValueError(NO_ANSWER if remaining else TOO_LARGE)


def _reply(messages: Iterator[dict], request_id: int) -> dict:
    for message in messages:
        if type(message.get("id")) is int and message["id"] == request_id:
            if "error" in message:
                raise ValueError(ERROR_REPLY)
            result = message.get("result")
            if "method" not in message and isinstance(result, dict):
                return result
            raise ValueError(NO_PATH)
    raise ValueError(NO_ANSWER)  # Not reached: _messages raises when it ends.


def _exchange(reader: BinaryIO, writer: BinaryIO, native_id: str, deadline: float) -> Path:
    def send(message: dict) -> None:
        data = json.dumps(message).encode("ascii") + b"\n"
        while data:  # Unbuffered, so closing a broken pipe has nothing left to flush.
            data = data[writer.write(data) :]

    send(
        {
            "id": 1,
            "method": "initialize",
            "params": {"clientInfo": {"name": "workjig", "version": "1"}},
        }
    )
    messages = _messages(reader, deadline)
    _reply(messages, 1)
    send({"method": "initialized", "params": {}})
    send(
        {"id": 2, "method": "thread/read", "params": {"threadId": native_id, "includeTurns": False}}
    )
    thread = _reply(messages, 2).get("thread")
    if not isinstance(thread, dict):
        raise ValueError(NO_PATH)
    path = thread.get("path")
    if (
        thread.get("id") == native_id
        and isinstance(path, str)
        and "\0" not in path
        and Path(path).is_absolute()
        and len(json.dumps(path)) <= 60 * 1024  # Bound what a reply can hand back.
    ):
        try:
            path.encode("utf-8")  # Reject lone surrogates.
        except UnicodeEncodeError:
            raise ValueError(NO_PATH) from None
        return Path(path)
    raise ValueError(NO_PATH)


def _pipe(files: ExitStack) -> tuple[BinaryIO, BinaryIO]:
    read, write = os.pipe()
    return (
        files.enter_context(os.fdopen(read, "rb")),
        files.enter_context(os.fdopen(write, "wb", buffering=0)),
    )


def lookup_codex_path(
    native_id: str, argv: tuple[str, ...], cwd: Path, env: Mapping[str, str]
) -> Path:
    """Query one exact thread through a short-lived, separately owned stdio server.

    Raises ValueError with a short reason when no path is reported, or
    OwnershipError when the helper's stop cannot be confirmed.
    """
    if not isinstance(native_id, str) or not _UUID.fullmatch(native_id):
        raise ValueError("invalid native session ID")
    command = resolve_command((argv[0], "app-server"), env)
    if shutil.which(command[0], path=env.get("PATH")) is None:
        raise ValueError(NOT_FOUND)  # Checked before any pipe or process exists.
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
            raise ValueError(NOT_STARTED) from None
        stdin.close()
        stdout.close()
        try:
            return _exchange(reader, writer, native_id, deadline)
        except OSError:  # A write to a helper that has already gone.
            raise ValueError(EXITED) from None


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


def resolve_codex_path(root: Path, native_id: str) -> Path:
    """Query the reader's Codex installation against the launch's pinned store."""
    if root.name != "sessions" or not root.is_dir():
        raise ValueError("pinned Codex sessions directory unavailable")
    env = os.environ | {"CODEX_HOME": str(root.parent)}
    with tempfile.TemporaryDirectory() as directory:
        return lookup_codex_path(native_id, ("codex",), Path(directory), env)
