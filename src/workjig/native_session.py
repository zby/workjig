"""Launch-bound native transcript pointers. Readers never open a transcript."""

from __future__ import annotations

import json
import os
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

from . import _session_receipt
from .models import CommandWorker

_HARNESS = {"pi-json": "pi", "claude-stream-json": "claude", "codex-json": "codex"}


@dataclass(frozen=True)
class NativeSession:
    status: Literal["unassociated", "unavailable", "located"]
    harness: str | None
    session_id: str | None
    source: Literal["assigned", "observed"] | None
    path: Path | None
    reason: str | None


def bind_session(
    worker: CommandWorker, launch: Path, cwd: Path, argv: tuple[str, ...]
) -> tuple[str, ...]:
    """Finalize the adapter command and any assigned pointer before reservation."""
    if not worker.track_native_session:
        return argv
    harness = _HARNESS[worker.output_protocol]
    if harness == "codex":
        return argv
    identifier = str(UUID(launch.name))
    conflicts = {
        "--session-id",
        "--session",
        "--session-dir",
        "--fork",
        "--continue",
        "-c",
        "--resume",
        "-r",
        "--no-session",
        "--settings",
        "--no-session-persistence",
    }
    if any(arg.partition("=")[0] in conflicts for arg in argv[1:-1]):
        raise ValueError("tracked worker overrides session selection or settings")
    flags = ["--session-id", identifier]
    location = None
    if harness == "pi":
        env = os.environ | dict(worker.env)
        agent = Path(env.get("PI_CODING_AGENT_DIR", "~/.pi/agent")).expanduser()
        root = Path(env.get("PI_CODING_AGENT_SESSION_DIR", agent / "sessions")).expanduser()
        location = (cwd / root).resolve() / f"workjig-{identifier}"
        if location.is_relative_to(launch.parents[1]):
            raise ValueError("tracked Pi session directory must be outside the run")
        location.mkdir(mode=0o700, parents=True, exist_ok=False)
        flags.extend(("--session-dir", str(location)))
    else:
        command = shlex.join(
            [sys.executable, "-I", _session_receipt.__file__, str(launch), identifier]
        )
        settings = {
            "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}
        }
        flags.extend(("--settings", json.dumps(settings)))
    _session_receipt.publish(launch, harness, identifier, location)
    return (*argv[:-1], *flags, argv[-1])


def locate_session(worker: CommandWorker, launch: Path) -> NativeSession:
    if not worker.track_native_session:
        return NativeSession("unassociated", None, None, None, None, "tracking disabled")
    harness = _HARNESS[worker.output_protocol]
    source = "observed" if harness == "codex" else "assigned"
    identifier = None if harness == "codex" else str(UUID(launch.name))
    try:
        receipt = launch / "native-session.json"
        if receipt.resolve() != receipt or not receipt.is_file():
            raise ValueError("missing or redirected pointer")
        with receipt.open("rb") as stream:
            raw = stream.read(65537)
        if len(raw) > 65536:
            raise ValueError("oversized pointer")
        data = json.loads(raw)
        if (
            not isinstance(data, dict)
            or data.get("launch") != launch.name
            or data.get("harness") != harness
        ):
            raise ValueError("pointer does not match this launch")
        sid = data["id"]
        if (
            not isinstance(sid, str)
            or str(UUID(sid)) != sid
            or (identifier is not None and sid != identifier)
        ):
            raise ValueError("invalid native session ID")
        identifier = sid
        if data["location"] is None:
            raise ValueError("no transcript location reported")
        path = Path(data["location"])
        if not path.is_absolute() or path.resolve() != path:
            raise ValueError("invalid or redirected transcript location")
        if harness == "pi":
            if path.name != f"workjig-{sid}":
                raise ValueError("unexpected Pi session directory")
            matches = list(path.glob(f"*_{sid}.jsonl"))
            if len(matches) != 1:
                raise ValueError("missing or ambiguous native transcript")
            path = matches[0]
        elif harness == "claude" and path.name != sid + ".jsonl":
            raise ValueError("unexpected Claude transcript name")
        if path.resolve() != path or not path.is_file():
            raise ValueError("missing or redirected native transcript")
    except (OSError, ValueError, RuntimeError, TypeError, KeyError) as error:
        return NativeSession(
            "unavailable" if identifier else "unassociated",
            harness,
            identifier,
            source if identifier else None,
            None,
            str(error),
        )
    return NativeSession("located", harness, identifier, source, path, None)
