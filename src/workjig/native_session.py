"""Launch-bound native transcript pointers. Readers never open a transcript."""

from __future__ import annotations

import json
import os
import shlex
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import UUID

from . import _session_receipt
from .models import CommandWorker, LaunchRecord

_HARNESS = {"pi-json": "pi", "claude-stream-json": "claude", "codex-json": "codex"}


@dataclass(frozen=True)
class NativeSession:
    status: Literal["unassociated", "unavailable", "located"]
    harness: str | None
    session_id: str | None
    source: Literal["assigned", "observed"] | None
    path: Path | None
    reason: str | None


def _store(worker: CommandWorker, harness: str, cwd: Path) -> Path:
    env = os.environ | dict(worker.env)
    home = Path(env.get("USERPROFILE" if os.name == "nt" else "HOME") or Path.home())

    def expand(value: str) -> Path:
        parts = Path(value).parts
        return home.joinpath(*parts[1:]) if parts and parts[0] == "~" else Path(value)

    if harness == "pi":
        root = (
            expand(env["PI_CODING_AGENT_SESSION_DIR"])
            if env.get("PI_CODING_AGENT_SESSION_DIR")
            else expand(env.get("PI_CODING_AGENT_DIR", "~/.pi/agent")) / "sessions"
        )
    elif harness == "claude":
        root = expand(env.get("CLAUDE_CONFIG_DIR", "~/.claude")) / "projects"
    else:
        root = expand(env.get("CODEX_HOME", "~/.codex")) / "sessions"
    return (cwd / root).resolve()


def bind_session(
    worker: CommandWorker, launch: Path, cwd: Path, argv: tuple[str, ...]
) -> tuple[tuple[str, ...], Path | None]:
    """Finalize the adapter command and pin its external store before reservation."""
    if not worker.track_native_session:
        return argv, None
    harness = _HARNESS[worker.output_protocol]
    root = _store(worker, harness, cwd)
    if root.is_relative_to(launch.parents[1]):
        raise ValueError("tracked native session store must be outside the run")
    if harness == "codex":
        if any(arg.partition("=")[0] in {"resume", "--last", "fork"} for arg in argv[1:]):
            raise ValueError("tracked Codex exec must start a new thread")
        return argv, root
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
        location = root / f"workjig-{identifier}"
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
    return (*argv[:-1], *flags, argv[-1]), root


def locate_session(worker: CommandWorker, recorded: LaunchRecord, launch: Path) -> NativeSession:
    if not worker.track_native_session:
        return NativeSession("unassociated", None, None, None, None, "tracking disabled")
    harness = _HARNESS[worker.output_protocol]
    source = "observed" if harness == "codex" else "assigned"
    identifier = None
    try:
        if harness != "codex":
            identifier = str(UUID(launch.name))
        receipt = launch / "native-session.json"
        if receipt.resolve() != receipt or not receipt.is_file():
            raise ValueError("missing or redirected pointer")
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(receipt, flags), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("not a regular pointer file")
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
        root = Path(recorded.native_session_root or "")
        original = Path(recorded.launch_dir)
        if (
            not root.is_absolute()
            or not path.is_absolute()
            or path.resolve() != path
            or not path.is_relative_to(root)
            or path.is_relative_to(launch.parents[1])
            or (len(original.parents) >= 2 and path.is_relative_to(original.parents[1]))
        ):
            raise ValueError("transcript outside the pinned native store or redirected")
        if harness == "pi":
            if path != root / f"workjig-{sid}":
                raise ValueError("unexpected Pi session directory")
            matches = list(path.glob(f"*_{sid}.jsonl"))
            if len(matches) != 1:
                raise ValueError("missing or ambiguous native transcript")
            path = matches[0]
        elif harness == "claude" and path.name != sid + ".jsonl":
            raise ValueError("unexpected Claude transcript name")
        elif harness == "codex" and not (
            path.name == sid + ".jsonl" or path.name.endswith("-" + sid + ".jsonl")
        ):
            raise ValueError("unexpected Codex transcript name")
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
