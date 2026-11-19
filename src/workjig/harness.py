"""Per-launch file protocol and local process ownership."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from .models import LaunchRecord, LaunchStatus, Request, launch_files_under
from .native_session import bind_session, capture_session, observes_session
from .outcomes import terminal_failure
from .platform import OwnedProcess, resolve_command, start_process


def _now() -> str:
    return datetime.now(UTC).isoformat()


def prepare(
    request: Request, launch_dir: Path, workdir: Path
) -> tuple[Mapping[str, Path], tuple[str, ...]]:
    """Write fresh inputs and a human-readable task frame before reservation."""
    files = launch_files_under(launch_dir)
    input_dir = files.inputs
    output_dir = files.outputs
    input_dir.mkdir()
    output_dir.mkdir()
    for name, content in request.inputs.items():
        (input_dir / name).write_bytes(content.encode("utf-8"))
    prompt = files.prompt
    lines = ["Task:", request.task, "", "Inputs (read these exact files):"]
    lines.extend(f"- {name}: {input_dir / name}" for name in sorted(request.inputs))
    lines.extend(["", "Required outputs (write these exact files):"])
    lines.extend(f"- {name}: {output_dir / name}" for name in request.required_outputs)
    if request.optional_outputs:
        lines.extend(["", "Optional outputs:"])
        lines.extend(f"- {name}: {output_dir / name}" for name in request.optional_outputs)
    lines.extend(
        [
            "",
            "If unable to finish, write a JSON object to this problem file:",
            str(files.problem),
            'Use {"kind":"unable","message":"..."} or {"kind":"usage_limit","message":"..."}.',
        ]
    )
    prompt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    paths = {
        "prompt": prompt,
        "input_dir": input_dir,
        "output_dir": output_dir,
        "problem_file": files.problem,
        "evidence_file": launch_dir / (request.worker.evidence_file or "evidence.json"),
        "launch_dir": launch_dir,
        "workdir": workdir,
    }
    argv, root = bind_session(request.worker, launch_dir, workdir, request.worker.command(paths))
    if root is not None:
        paths["native_session_root"] = root
    return paths, argv


def _cleanup_group(process: OwnedProcess) -> bool:
    """Seam for tests that inject cleanup failures."""

    return process.cleanup()


def _read_problem(path: Path) -> tuple[dict[str, str] | None, bool]:
    if path.is_symlink():
        return None, True
    if not path.exists():
        return None, False
    if not path.is_file():
        return None, True
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    # Deeply nested JSON exhausts the parser's stack rather than failing to parse.
    except (OSError, UnicodeError, json.JSONDecodeError, RecursionError):
        return None, True
    if (
        not isinstance(value, dict)
        or not isinstance(value.get("kind"), str)
        or value.get("kind") not in {"unable", "usage_limit"}
        or not isinstance(value.get("message"), str)
    ):
        return None, True
    return {"kind": value["kind"], "message": value["message"]}, False


def _read_evidence(path: Path) -> Mapping[str, object] | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        # Optional metadata must fit the same strict JSON format as run records.
        # This also rejects overflowing numeric literals and nested nonfinite values.
        json.dumps(value, allow_nan=False)
    except (OSError, UnicodeError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def _stdout_streams(
    files: ExitStack, path: Path, retain_logs: bool, protocol: str | None, observe: bool
) -> tuple[BinaryIO, Path | BinaryIO | None]:
    if retain_logs:
        return files.enter_context(path.open("wb")), path
    if protocol is None:
        return files.enter_context(open(os.devnull, "wb")), None
    if not observe:
        stream = files.enter_context(tempfile.TemporaryFile())
        return stream, stream

    # Independent opens give the live observer its own offset. A dup would
    # share the worker's offset and could skip or overwrite protocol events.
    windows = os.name == "nt"
    writer = files.enter_context(tempfile.NamedTemporaryFile(mode="wb", delete=windows))

    def open_reader(name: str, flags: int) -> int:
        return os.open(name, flags | getattr(os, "O_TEMPORARY", 0))

    try:
        reader = files.enter_context(open(writer.name, "rb", opener=open_reader))
    finally:
        if not windows:
            os.unlink(writer.name)
    return writer, reader


def launch(
    request: Request,
    workdir: Path,
    paths: Mapping[str, Path],
    argv: tuple[str, ...],
    reserved: LaunchRecord,
    on_started: Callable[[int, tuple[str, ...], str], None],
    *,
    retain_logs: bool,
) -> tuple[LaunchRecord, dict[str, str]]:
    """Invoke once, terminate its process group, then collect only its outputs."""
    started_at = _now()
    located = launch_files_under(Path(reserved.launch_dir))
    protocol = request.worker.output_protocol
    observe = observes_session(request.worker)
    env = os.environ.copy()
    env.update(request.worker.env)

    def launch_error(error: OSError) -> tuple[LaunchRecord, dict[str, str]]:
        return (
            replace(
                reserved,
                status=LaunchStatus.LAUNCH_ERROR,
                argv=argv,
                started_at=started_at,
                finished_at=_now(),
                problem={"kind": "launch_error", "message": str(error)},
            ),
            {},
        )

    with ExitStack() as files:
        try:
            stdout, reader = _stdout_streams(files, located.stdout, retain_logs, protocol, observe)
            stderr = files.enter_context(open(located.stderr if retain_logs else os.devnull, "wb"))
            command = resolve_command(argv, env)
            process = start_process(command, workdir, env, stdout, stderr)
        except OSError as error:
            # Only a failure that left nothing running settles as launch_error.
            # An OwnershipError propagates and leaves the reservation unresolved.
            return launch_error(error)
        with ExitStack() as observers:
            try:
                on_started(process.pid, argv, started_at)
                if observe:
                    assert reader is not None
                    # Join the observer after group cleanup, before parsing terminal evidence.
                    observers.enter_context(
                        capture_session(
                            request.worker,
                            reader,
                            paths["launch_dir"],
                            paths.get("native_session_root"),
                        )
                    )
                timed_out = not process.wait(request.timeout_seconds)
            finally:
                # Cleanup settles the whole owned group or raises, in which
                # case the reservation stays unresolved.
                try:
                    group_lingered = _cleanup_group(process)
                finally:
                    process.close()
        harness_failure = (
            terminal_failure(protocol, reader)
            if protocol is not None and reader is not None and not timed_out
            else None
        )

    status = LaunchStatus.DELIVERED
    problem, invalid_problem = _read_problem(paths["problem_file"])
    evidence = _read_evidence(paths["evidence_file"])
    outputs: dict[str, str] = {}
    missing: list[str] = []
    # LaunchRecord construction below enforces the lifecycle invariants.
    if timed_out:
        status = LaunchStatus.TIMEOUT
    elif harness_failure == "usage_limit" or (
        problem is not None and problem["kind"] == "usage_limit"
    ):
        status = LaunchStatus.USAGE_LIMIT
    elif group_lingered:
        status = LaunchStatus.PROCESS_ERROR
    elif invalid_problem:
        status = LaunchStatus.PROTOCOL_ERROR
    elif problem is not None:
        status = LaunchStatus.WORKER_PROBLEM
    elif process.returncode != 0 or harness_failure is not None:
        status = LaunchStatus.PROCESS_ERROR
    else:
        if paths["output_dir"].is_symlink():
            status = LaunchStatus.PROTOCOL_ERROR
        for name in request.required_outputs + request.optional_outputs:
            if status != LaunchStatus.DELIVERED:
                break
            path = paths["output_dir"] / name
            if path.is_symlink():
                status = LaunchStatus.PROTOCOL_ERROR
                break
            if not path.is_file():
                if name in request.required_outputs:
                    missing.append(name)
                continue
            try:
                outputs[name] = path.read_bytes().decode("utf-8")
            except (OSError, UnicodeError):
                status = LaunchStatus.PROTOCOL_ERROR
                break
        if status == LaunchStatus.DELIVERED and missing:
            status = LaunchStatus.INCOMPLETE
    if status != LaunchStatus.DELIVERED:
        outputs = {}
    return (
        replace(
            reserved,
            status=status,
            argv=argv,
            pid=process.pid,
            returncode=process.returncode,
            started_at=started_at,
            finished_at=_now(),
            problem=problem,
            evidence=evidence,
            missing_outputs=tuple(missing),
        ),
        outputs,
    )
