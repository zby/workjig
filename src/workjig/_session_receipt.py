"""Small atomic pointer writer, also invoked directly by Claude's SessionStart hook."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path


def publish(launch: Path, harness: str, session_id: str, location: Path | None) -> None:
    record = {
        "launch": launch.name,
        "harness": harness,
        "id": session_id,
        "location": str(location) if location is not None else None,
    }
    fd, name = tempfile.mkstemp(prefix=".session-", dir=launch)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(record, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, launch / "native-session.json")
        if os.name != "nt":
            directory = os.open(launch, os.O_RDONLY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


def claude_hook(launch: Path, expected: str) -> None:
    raw = sys.stdin.buffer.read(65537)
    if len(raw) > 65536:
        return
    event = json.loads(raw)
    if (
        not isinstance(event, dict)
        or event.get("hook_event_name") != "SessionStart"
        or event.get("session_id") != expected
    ):
        return
    path = Path(event["transcript_path"])
    if path.is_absolute() and path.name == expected + ".jsonl":
        publish(launch, "claude", expected, path)


if __name__ == "__main__":
    # Advisory hook: missing metadata must not turn into a Claude startup failure.
    try:
        claude_hook(Path(sys.argv[1]), sys.argv[2])
    except (ValueError, OSError, TypeError, KeyError, RecursionError):
        pass
