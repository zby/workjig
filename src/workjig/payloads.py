"""Run-local UTF-8 payloads. Only the exclusive controller publishes files."""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from pathlib import Path

from .models import WorkjigError
from .platform import sync_directory


class RecordError(WorkjigError):
    """A run record or its referenced content is corrupt or unavailable."""


class PayloadStore:
    def __init__(self, record_dir: Path) -> None:
        self.root = record_dir / "payloads"
        self._known: set[str] = set()

    def _read(self, digest: object) -> str:
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise RecordError("invalid payload reference")
        path = self.root / digest
        try:
            if self.root.is_symlink() or path.is_symlink():
                raise RecordError(f"redirected payload: {digest}")
            flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0)
            with os.fdopen(os.open(path, flags | getattr(os, "O_NOFOLLOW", 0)), "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise RecordError(f"not a regular payload: {digest}")
                data = stream.read()
            if hashlib.sha256(data).hexdigest() != digest:
                raise RecordError(f"payload digest mismatch: {digest}")
            value = data.decode("utf-8")
        except (OSError, UnicodeError) as error:
            raise RecordError(f"cannot read payload {digest}: {error}") from error
        return value

    def get(self, digest: str) -> str:
        """Resolve an already-committed reference."""
        value = self._read(digest)
        self._known.add(digest)
        return value

    def put(self, text: str) -> str:
        data = text.encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        path = self.root / digest
        if self.root.is_symlink():
            raise RecordError("redirected payload directory")
        if digest in self._known:
            self.get(digest)
            return digest
        self.root.mkdir(exist_ok=True)
        if not self._known:
            # The directory entry itself must survive before a manifest refers into it.
            sync_directory(self.root.parent)
        if os.path.lexists(path):
            # Do not mark a verified orphan known until its directory entry is durable.
            self._read(digest)
        else:
            fd, tmp = tempfile.mkstemp(prefix=".payload-", dir=self.root)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(tmp, path)
            finally:
                Path(tmp).unlink(missing_ok=True)
        # Also flush a verified orphan left by a previously interrupted writer.
        sync_directory(self.root)
        self._known.add(digest)
        return digest
