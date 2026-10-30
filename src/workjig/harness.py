"""Per-launch file protocol and local process ownership."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from .models import LaunchRecord, LaunchStatus, Request
from .outcomes import terminal_failure
from .platform import OwnedProcess, resolve_command, start_process


def _now() -> str:
    return datetime.now(UTC).isoformat()


def prepare(
    request: Request, launch_dir: Path, workdir: Path
) -> tuple[Mapping[str, Path], tuple[str, ...]]:
    """Write fresh inputs and a human-readable task frame before reservation."""
    input_dir = launch_dir / "inputs"
    output_dir = launch_dir / "outputs"
    input_dir.mkdir()
    output_dir.mkdir()
    for name, content in request.inputs.items():
        (input_dir / name).write_text(content, encoding="utf-8")
    prompt = launch_dir / "prompt.txt"
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
            str(launch_dir / "problem.json"),
            'Use {"kind":"unable","message":"..."} or {"kind":"usage_limit","message":"..."}.',
        ]
    )
    prompt.write_text("\n".join(lines) + "\n", encoding="utf-8")
    paths = {
        "prompt": prompt,
        "input_dir": input_dir,
        "output_dir": output_dir,
        "problem_file": launch_dir / "problem.json",
        "evidence_file": launch_dir / (request.worker.evidence_file or "evidence.json"),
        "launch_dir": launch_dir,
        "workdir": workdir,
    }
    return paths, request.worker.command(paths)


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
    except (OSError, UnicodeError, json.JSONDecodeError):
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
    except (OSError, UnicodeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def launch(
    request: Request,
    workdir: Path,
    paths: Mapping[str, Path],
    argv: tuple[str, ...],
    reserved: LaunchRecord,
    on_started: Callable[[int, tuple[str, ...], str], None],
) -> tuple[LaunchRecord, dict[str, str]]:
    """Invoke once, terminate its process group, then collect only its outputs."""
    started_at = _now()
    stdout_path = Path(reserved.launch_dir) / "stdout.log"
    stderr_path = Path(reserved.launch_dir) / "stderr.log"
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

    files = ExitStack()
    try:
        stdout = files.enter_context(stdout_path.open("wb"))
        stderr = files.enter_context(stderr_path.open("wb"))
    except OSError as error:
        files.close()
        return launch_error(error)
    try:
        try:
            process = start_process(resolve_command(argv), workdir, env, stdout, stderr)
        except OSError as error:
            # Only a failure that left nothing running settles as launch_error.
            # An OwnershipError propagates and leaves the reservation unresolved.
            return launch_error(error)
        try:
            timed_out = False
            try:
                on_started(process.pid, argv, started_at)
                timed_out = not process.wait(request.timeout_seconds)
            finally:
                # Cleanup settles the whole owned group or raises, in which
                # case the reservation stays unresolved.
                group_lingered = _cleanup_group(process)
        finally:
            process.close()
    finally:
        files.close()

    status = LaunchStatus.DELIVERED
    problem, invalid_problem = _read_problem(paths["problem_file"])
    evidence = _read_evidence(paths["evidence_file"])
    outputs: dict[str, str] = {}
    missing: list[str] = []
    # LaunchRecord construction below enforces the lifecycle invariants.
    harness_failure = (
        terminal_failure(request.worker.output_protocol, stdout_path)
        if request.worker.output_protocol is not None and not timed_out
        else None
    )
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
                outputs[name] = path.read_text(encoding="utf-8")
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
