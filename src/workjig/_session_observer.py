"""Bounded, non-blocking stdout JSONL observer for native session start events."""

from __future__ import annotations

import json
import os
import re
import stat
from collections.abc import Callable
from pathlib import Path
from threading import Event

_UUID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}")
_CHUNK = 64 * 1024
_POLL_SECONDS = 0.025


def _object(line: bytes) -> dict:
    if not line.lstrip().startswith(b"{"):
        return {}
    try:
        value = json.loads(line.decode("utf-8"))
    except (ValueError, RecursionError):
        return {}
    return value if isinstance(value, dict) else {}


class EventObserver:
    """Frame complete JSONL records across reads and discard oversized lines whole."""

    def __init__(self, extract: Callable[[dict], str | None]) -> None:
        self._extract = extract
        self.pending = b""
        self.skipping = False

    def feed(self, chunk: bytes) -> str | None:
        parts = chunk.split(b"\n")
        for part in parts[:-1]:
            if not self.skipping and len(self.pending) + len(part) <= _CHUNK:
                native_id = self._extract(_object(self.pending + part))
                if native_id is not None:
                    return native_id
            self.skipping = False
            self.pending = b""
        self.skipping |= len(self.pending) + len(parts[-1]) > _CHUNK
        self.pending = b"" if self.skipping else self.pending + parts[-1]
        return None


def observe_stdout(
    stdout_path: Path,
    stopped: Event,
    extract: Callable[[dict], str | None],
    publish: Callable[[str], None],
) -> str | None:
    """Tail a regular stdout file until an ID is extracted or settlement stops."""
    events = EventObserver(extract)
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
                publish(native_id)
                return native_id
            if final:
                # A descendant may still be appending. Never chase its writes.
                final_bytes -= len(chunk)
                if len(chunk) < _CHUNK or final_bytes == 0:
                    return None
            elif len(chunk) < _CHUNK:
                stopped.wait(_POLL_SECONDS)
