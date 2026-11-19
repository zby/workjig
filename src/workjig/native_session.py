"""Launch-bound native transcript pointers. Readers never open a transcript."""

from __future__ import annotations

import json
import os
import shlex
import stat
import sys
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import AbstractContextManager, contextmanager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from threading import Event
from typing import BinaryIO, Literal
from uuid import UUID

from . import _session_receipt
from ._session_observer import _UUID, observe_stdout
from .models import CommandWorker, LaunchRecord

_AGY_TRANSCRIPT = (".system_generated", "logs", "transcript.jsonl")
_RECEIPT_LIMIT = 65536


@dataclass(frozen=True)
class NativeSession:
    status: Literal["unassociated", "unavailable", "located"]
    harness: str | None
    session_id: str | None
    source: Literal["assigned", "observed"] | None
    path: Path | None
    reason: str | None


# -- external stores -----------------------------------------------------------
# Each takes the worker environment and a function expanding a leading "~".


def _pi_store(env: Mapping[str, str], expand: Callable[[str], Path]) -> Path:
    if env.get("PI_CODING_AGENT_SESSION_DIR"):
        return expand(env["PI_CODING_AGENT_SESSION_DIR"])
    return expand(env.get("PI_CODING_AGENT_DIR", "~/.pi/agent")) / "sessions"


def _claude_store(env: Mapping[str, str], expand: Callable[[str], Path]) -> Path:
    return expand(env.get("CLAUDE_CONFIG_DIR", "~/.claude")) / "projects"


def _codex_store(env: Mapping[str, str], expand: Callable[[str], Path]) -> Path:
    return expand(env.get("CODEX_HOME", "~/.codex")) / "sessions"


def _agy_store(env: Mapping[str, str], expand: Callable[[str], Path]) -> Path:
    return expand("~/.gemini/antigravity-cli/brain")


# -- binding -------------------------------------------------------------------
# Each validates the adapter command, may assign a session, and returns the
# final command. Assigning publishes the receipt before reservation.

_SESSION_OVERRIDES = frozenset(
    {
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
)


def _assign(
    harness: str, argv: tuple[str, ...], launch: Path, flags: tuple[str, ...], location: Path | None
) -> tuple[str, ...]:
    """Insert the session flags before the trailing prompt and publish the receipt."""
    identifier = str(UUID(launch.name))
    _session_receipt.publish(launch, harness, identifier, location)
    return (*argv[:-1], "--session-id", identifier, *flags, argv[-1])


def _reject_session_overrides(argv: tuple[str, ...]) -> None:
    if any(arg.partition("=")[0] in _SESSION_OVERRIDES for arg in argv[1:-1]):
        raise ValueError("tracked worker overrides session selection or settings")


def _bind_pi(argv: tuple[str, ...], launch: Path, root: Path) -> tuple[str, ...]:
    identifier = str(UUID(launch.name))
    _reject_session_overrides(argv)
    location = root / f"workjig-{identifier}"
    location.mkdir(mode=0o700, parents=True, exist_ok=False)
    return _assign("pi", argv, launch, ("--session-dir", str(location)), location)


def _bind_claude(argv: tuple[str, ...], launch: Path, root: Path) -> tuple[str, ...]:
    identifier = str(UUID(launch.name))
    _reject_session_overrides(argv)
    command = shlex.join([sys.executable, "-I", _session_receipt.__file__, str(launch), identifier])
    settings = {"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": command}]}]}}
    return _assign("claude", argv, launch, ("--settings", json.dumps(settings)), None)


def _bind_codex(argv: tuple[str, ...], launch: Path, root: Path) -> tuple[str, ...]:
    options = argv[1 : argv.index("--")] if "--" in argv else argv[1:]
    if any(arg.partition("=")[0] in {"resume", "--last", "fork"} for arg in options):
        raise ValueError("tracked Codex exec must start a new thread")
    return argv


def _bind_agy(argv: tuple[str, ...], launch: Path, root: Path) -> tuple[str, ...]:
    skip_value = False
    for arg in argv[1:]:
        if skip_value:
            skip_value = False
            continue
        if arg == "--":
            break
        if arg in {"-p", "--print", "--prompt"}:
            skip_value = True
            continue
        if arg.partition("=")[0] in {"-c", "--continue", "--conversation", "--remote-agent"}:
            raise ValueError("tracked agy must start a new local conversation")
    return argv


# -- transcript location -------------------------------------------------------
# Each checks the reported location against the harness layout under the pinned
# store and returns the transcript path, or raises ValueError.


def _pi_transcript(root: Path, sid: str, path: Path) -> Path:
    if path != root / f"workjig-{sid}":
        raise ValueError("unexpected Pi session directory")
    matches = list(path.glob(f"*_{sid}.jsonl"))
    if len(matches) != 1:
        raise ValueError("missing or ambiguous native transcript")
    return matches[0]


def _claude_transcript(root: Path, sid: str, path: Path) -> Path:
    if path.name != sid + ".jsonl":
        raise ValueError("unexpected Claude transcript name")
    return path


def _codex_transcript(root: Path, sid: str, path: Path) -> Path:
    if not (path.name == sid + ".jsonl" or path.name.endswith("-" + sid + ".jsonl")):
        raise ValueError("unexpected Codex transcript name")
    return path


def _agy_transcript(root: Path, sid: str, path: Path) -> Path:
    if path != root.joinpath(sid, *_AGY_TRANSCRIPT):
        raise ValueError("unexpected agy transcript path")
    return path


# -- observation while the worker runs -----------------------------------------


def _agy_id(event: dict) -> str | None:
    native_id = event.get("conversation_id")
    if event.get("event") == "init" and isinstance(native_id, str) and _UUID.fullmatch(native_id):
        return native_id
    return None


@contextmanager
def capture_agy(stdout: Path | BinaryIO, launch_dir: Path, root: Path) -> Iterator[None]:
    """Observe agy's init event alongside the worker; join only after settlement."""
    stopped = Event()

    def publish(native_id: str) -> None:
        try:
            _session_receipt.publish(
                launch_dir,
                "agy",
                native_id,
                root.joinpath(native_id, *_AGY_TRANSCRIPT),
            )
        except OSError:
            pass  # Optional evidence; never change the worker outcome.

    with ThreadPoolExecutor(max_workers=1) as pool:
        observation = pool.submit(observe_stdout, stdout, stopped, _agy_id, publish)
        try:
            yield
        finally:
            stopped.set()
        observation.result()


def _capture_codex(
    stdout: Path | BinaryIO,
    launch_dir: Path,
    command: tuple[str, ...],
    cwd: Path,
    env: Mapping[str, str],
    root: Path,
) -> AbstractContextManager[None]:
    from ._codex_session import capture_codex

    return capture_codex(stdout, launch_dir, command, cwd, env)


def _capture_agy(
    stdout: Path | BinaryIO,
    launch_dir: Path,
    command: tuple[str, ...],
    cwd: Path,
    env: Mapping[str, str],
    root: Path,
) -> AbstractContextManager[None]:
    return capture_agy(stdout, launch_dir, root)


# -- the per-harness table -----------------------------------------------------


@dataclass(frozen=True)
class _Harness:
    """Everything session tracking knows about one harness.

    ``source`` says whether the runtime assigns the session ID before
    reservation or observes it on stdout; ``capture`` is set only for the
    latter.
    """

    name: str
    source: Literal["assigned", "observed"]
    store: Callable[[Mapping[str, str], Callable[[str], Path]], Path]
    bind: Callable[[tuple[str, ...], Path, Path], tuple[str, ...]]
    transcript: Callable[[Path, str, Path], Path]
    capture: (
        Callable[
            [Path | BinaryIO, Path, tuple[str, ...], Path, Mapping[str, str], Path],
            AbstractContextManager[None],
        ]
        | None
    ) = None


# Keyed by output protocol; one entry per protocol in models.OutputProtocol.
_HARNESS = {
    "pi-json": _Harness("pi", "assigned", _pi_store, _bind_pi, _pi_transcript),
    "claude-stream-json": _Harness(
        "claude", "assigned", _claude_store, _bind_claude, _claude_transcript
    ),
    "codex-json": _Harness(
        "codex", "observed", _codex_store, _bind_codex, _codex_transcript, _capture_codex
    ),
    "agy-stream-json": _Harness(
        "agy", "observed", _agy_store, _bind_agy, _agy_transcript, _capture_agy
    ),
}
_BY_NAME = {harness.name: harness for harness in _HARNESS.values()}


def _store(worker: CommandWorker, harness: str, cwd: Path) -> Path:
    env = os.environ | dict(worker.env)
    home = Path(env.get("USERPROFILE" if os.name == "nt" else "HOME") or Path.home())

    def expand(value: str) -> Path:
        parts = Path(value).parts
        return home.joinpath(*parts[1:]) if parts and parts[0] == "~" else Path(value)

    return (cwd / _BY_NAME[harness].store(env, expand)).resolve()


def bind_session(
    worker: CommandWorker, launch: Path, cwd: Path, argv: tuple[str, ...]
) -> tuple[tuple[str, ...], Path | None]:
    """Finalize the adapter command and pin its external store before reservation."""
    if not worker.track_native_session:
        return argv, None
    harness = _HARNESS[worker.output_protocol]
    root = _store(worker, harness.name, cwd)
    if root.is_relative_to(launch.parents[1]):
        raise ValueError("tracked native session store must be outside the run")
    return harness.bind(argv, launch, root), root


def observes_session(worker: CommandWorker) -> bool:
    """Does this worker need a live reader independent of its stdout writer?"""
    return worker.track_native_session and _HARNESS[worker.output_protocol].capture is not None


def capture_session(
    worker: CommandWorker,
    stdout: Path | BinaryIO,
    launch_dir: Path,
    command: tuple[str, ...],
    cwd: Path,
    env: Mapping[str, str],
    root: Path | None,
) -> AbstractContextManager[None]:
    """Observe a tracked worker's session start; leave only after group cleanup.

    Does nothing when tracking is off or the session was assigned at binding.
    """
    if not worker.track_native_session or root is None:
        return nullcontext()
    capture = _HARNESS[worker.output_protocol].capture
    return capture(stdout, launch_dir, command, cwd, env, root) if capture else nullcontext()


def _read_receipt(launch: Path) -> object:
    """Parse the launch's pointer file from a bounded read of a regular file."""
    receipt = launch / "native-session.json"
    if receipt.resolve() != receipt or not receipt.is_file():
        raise ValueError("missing or redirected pointer")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(receipt, flags), "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise ValueError("not a regular pointer file")
        raw = stream.read(_RECEIPT_LIMIT + 1)
    if len(raw) > _RECEIPT_LIMIT:
        raise ValueError("oversized pointer")
    return json.loads(raw)


def locate_session(worker: CommandWorker, recorded: LaunchRecord, launch: Path) -> NativeSession:
    if not worker.track_native_session:
        return NativeSession("unassociated", None, None, None, None, "tracking disabled")
    harness = _HARNESS[worker.output_protocol]
    identifier = None
    try:
        if harness.source == "assigned":
            identifier = str(UUID(launch.name))
        data = _read_receipt(launch)
        if (
            not isinstance(data, dict)
            or data.get("launch") != launch.name
            or data.get("harness") != harness.name
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
        path = harness.transcript(root, sid, path)
        if path.resolve() != path or not path.is_file():
            raise ValueError("missing or redirected native transcript")
    except (OSError, ValueError, RuntimeError, TypeError, KeyError) as error:
        return NativeSession(
            "unavailable" if identifier else "unassociated",
            harness.name,
            identifier,
            harness.source if identifier else None,
            None,
            str(error),
        )
    return NativeSession("located", harness.name, identifier, harness.source, path, None)
