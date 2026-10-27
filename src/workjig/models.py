"""Immutable, plain request and result values."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Literal


class WorkjigError(RuntimeError):
    """Base of every error the runtime raises about records, locks or recovery."""


class LaunchStatus(StrEnum):
    """Lifecycle of one launch reservation, from reservation to a settled outcome."""

    RESERVED = "reserved"
    RUNNING = "running"
    DELIVERED = "delivered"
    INCOMPLETE = "incomplete"
    WORKER_PROBLEM = "worker_problem"
    USAGE_LIMIT = "usage_limit"
    PROCESS_ERROR = "process_error"
    TIMEOUT = "timeout"
    LAUNCH_ERROR = "launch_error"
    INTERRUPTED = "interrupted"
    PROTOCOL_ERROR = "protocol_error"


UNSETTLED_STATUSES = frozenset({LaunchStatus.RESERVED, LaunchStatus.RUNNING})
FAILURE_STATUSES = frozenset(
    {
        LaunchStatus.INCOMPLETE,
        LaunchStatus.WORKER_PROBLEM,
        LaunchStatus.USAGE_LIMIT,
        LaunchStatus.PROCESS_ERROR,
        LaunchStatus.TIMEOUT,
        LaunchStatus.LAUNCH_ERROR,
        LaunchStatus.INTERRUPTED,
        LaunchStatus.PROTOCOL_ERROR,
    }
)
RETRYABLE_STATUSES = FAILURE_STATUSES - {LaunchStatus.USAGE_LIMIT, LaunchStatus.PROTOCOL_ERROR}
_WORKER_PROBLEM_KINDS = frozenset({"unable", "usage_limit"})
_PROBLEM_KINDS = _WORKER_PROBLEM_KINDS | {"launch_error", "operator_acknowledgment"}

_SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_PATH_TOKENS = {
    "prompt",
    "input_dir",
    "output_dir",
    "problem_file",
    "evidence_file",
    "launch_dir",
    "workdir",
}
_PATH_TOKEN_RE = re.compile(r"\{(" + "|".join(_PATH_TOKENS) + r")\}")


def _safe_name(value: str) -> str:
    if not isinstance(value, str) or not _SAFE_NAME.fullmatch(value):
        raise ValueError(f"unsafe file name: {value!r}")
    return value


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value


def _positive_number(value: float | int, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a positive finite number")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return value


def _nonempty_text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be nonempty text")
    return value


def _optional_timestamp(value: object, name: str) -> str | None:
    if value is None:
        return None
    try:
        if not isinstance(value, str) or datetime.fromisoformat(value).tzinfo is None:
            raise ValueError("expected timezone-aware timestamp")
    except ValueError as error:
        raise ValueError(f"invalid {name}: {error}") from error
    return value


@dataclass(frozen=True)
class RetryPolicy:
    """Execution outcomes eligible for automatic relaunch, with fixed delay."""

    on: tuple[str, ...] = ()
    backoff_seconds: float = 0.0

    def __post_init__(self) -> None:
        statuses = tuple(self.on)
        if len(statuses) != len(set(statuses)) or not set(statuses) <= RETRYABLE_STATUSES:
            raise ValueError("retry statuses must be unique eligible execution failures")
        if isinstance(self.backoff_seconds, bool) or not isinstance(
            self.backoff_seconds, (int, float)
        ):
            raise ValueError("backoff_seconds must be a nonnegative finite number")
        if not math.isfinite(self.backoff_seconds) or self.backoff_seconds < 0:
            raise ValueError("backoff_seconds must be a nonnegative finite number")
        object.__setattr__(self, "on", statuses)
        object.__setattr__(self, "backoff_seconds", float(self.backoff_seconds))


@dataclass(frozen=True)
class CommandWorker:
    """A command template with path placeholders substituted per launch.

    Supported placeholders are prompt, input_dir, output_dir, problem_file,
    evidence_file, launch_dir, and workdir. The process runs in workdir.
    output_protocol explicitly selects terminal-event classification of stdout;
    None uses process exit and the file protocol only. name is descriptive.
    """

    argv: tuple[str, ...]
    name: str = "command"
    env: Mapping[str, str] = field(default_factory=dict)
    evidence_file: str | None = None
    output_protocol: Literal["codex-json", "claude-stream-json", "pi-json"] | None = None

    def __post_init__(self) -> None:
        argv = tuple(self.argv)
        if not argv or not all(isinstance(x, str) and x for x in argv):
            raise ValueError("argv must contain nonempty strings")
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("worker name must be nonempty")
        env = dict(self.env)
        if not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            raise ValueError("environment overrides must be strings")
        if self.evidence_file is not None:
            if (
                not isinstance(self.evidence_file, str)
                or not _SAFE_NAME.fullmatch(self.evidence_file)
                or self.evidence_file
                in {"inputs", "outputs", "prompt.txt", "problem.json", "stdout.log", "stderr.log"}
            ):
                raise ValueError("unsafe evidence file name")
        if self.output_protocol is not None and (
            not isinstance(self.output_protocol, str)
            or self.output_protocol not in {"codex-json", "claude-stream-json", "pi-json"}
        ):
            raise ValueError("unsupported output protocol")
        object.__setattr__(self, "argv", argv)
        object.__setattr__(self, "env", MappingProxyType(env))

    def command(self, paths: Mapping[str, Path]) -> tuple[str, ...]:
        """Render a subprocess command without invoking it."""
        return tuple(_replace_tokens(arg, paths) for arg in self.argv)


def _replace_tokens(arg: str, paths: Mapping[str, Path]) -> str:
    # Only these exact tokens are special. Literal JSON and shell/code braces survive.
    return _PATH_TOKEN_RE.sub(lambda match: str(paths[match.group(1)]), arg)


# Field metadata marking a Request field as an execution control.
EXECUTION_CONTROL = {"execution_control": True}


@dataclass(frozen=True)
class Request:
    """One logical call: identity-bearing work material plus execution controls.

    Every field except call_id is work material unless its field metadata is
    ``EXECUTION_CONTROL``. Work material determines reuse; execution controls
    only govern future launches of the same work.
    """

    call_id: str
    task: str
    inputs: Mapping[str, str]
    required_outputs: tuple[str, ...]
    worker: CommandWorker
    optional_outputs: tuple[str, ...] = ()
    timeout_seconds: float = field(default=300.0, metadata=EXECUTION_CONTROL)
    max_launches: int = field(default=1, metadata=EXECUTION_CONTROL)
    retry: RetryPolicy = field(default_factory=RetryPolicy, metadata=EXECUTION_CONTROL)

    def __post_init__(self) -> None:
        if not isinstance(self.call_id, str) or not self.call_id or "\x00" in self.call_id:
            raise ValueError("call_id must be a nonempty string")
        if not isinstance(self.task, str) or not self.task:
            raise ValueError("task must be nonempty text")
        inputs = dict(self.inputs)
        if not all(isinstance(v, str) for v in inputs.values()):
            raise ValueError("input contents must be text")
        for name in inputs:
            _safe_name(name)
        required = tuple(self.required_outputs)
        optional = tuple(self.optional_outputs)
        for name in required + optional:
            _safe_name(name)
        if not required or len(set(required + optional)) != len(required + optional):
            raise ValueError("outputs must include a required name and have no duplicates")
        if not isinstance(self.worker, CommandWorker):
            raise ValueError("worker must be a CommandWorker")
        if (
            isinstance(self.max_launches, bool)
            or not isinstance(self.max_launches, int)
            or self.max_launches < 1
        ):
            raise ValueError("max_launches must be a positive integer")
        if not isinstance(self.retry, RetryPolicy):
            raise ValueError("retry must be a RetryPolicy")
        object.__setattr__(
            self, "timeout_seconds", _positive_number(self.timeout_seconds, "timeout_seconds")
        )
        object.__setattr__(self, "inputs", MappingProxyType(inputs))
        object.__setattr__(self, "required_outputs", required)
        object.__setattr__(self, "optional_outputs", optional)


@dataclass(frozen=True)
class LaunchRecord:
    """One launch reservation and its execution evidence, whether or not it started.

    Construction validates field types and the lifecycle invariants of the
    status, so every emitted and every loaded record satisfies the same rules.
    """

    launch_id: str
    status: LaunchStatus
    launch_dir: str
    argv: tuple[str, ...]
    pid: int | None = None
    returncode: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    problem: Mapping[str, str] | None = None
    evidence: Mapping[str, object] | None = None
    missing_outputs: tuple[str, ...] = ()
    timeout_seconds: float = 300.0
    max_launches: int = 1
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    resumed_usage_stop: str | None = None

    def __post_init__(self) -> None:
        _nonempty_text(self.launch_id, "launch_id")
        _nonempty_text(self.launch_dir, "launch_dir")
        try:
            status = LaunchStatus(self.status)
        except ValueError as error:
            raise ValueError(f"invalid launch status: {self.status!r}") from error
        argv = tuple(self.argv)
        if not argv or not all(isinstance(arg, str) and arg for arg in argv):
            raise ValueError("argv must contain nonempty strings")
        for name in ("pid", "returncode"):
            number = getattr(self, name)
            if number is not None and (type(number) is not int or (name == "pid" and number <= 0)):
                raise ValueError(f"invalid {name}")
        _optional_timestamp(self.started_at, "started_at")
        _optional_timestamp(self.finished_at, "finished_at")
        problem = self.problem
        if problem is not None:
            if (
                not isinstance(problem, Mapping)
                or set(problem) != {"kind", "message"}
                or not all(isinstance(item, str) for item in problem.values())
                or problem["kind"] not in _PROBLEM_KINDS
            ):
                raise ValueError("invalid problem")
            problem = _freeze_json(dict(problem))
        evidence = self.evidence
        if evidence is not None:
            if not isinstance(evidence, Mapping):
                raise ValueError("invalid evidence")
            try:
                json.dumps(thaw_json(evidence), allow_nan=False)
            except (TypeError, ValueError) as error:
                raise ValueError("invalid evidence") from error
            evidence = _freeze_json(evidence)
        missing = tuple(self.missing_outputs)
        if not all(isinstance(name, str) and name for name in missing) or len(set(missing)) != len(
            missing
        ):
            raise ValueError("invalid missing_outputs")
        timeout = _positive_number(self.timeout_seconds, "timeout_seconds")
        if type(self.max_launches) is not int or self.max_launches < 1:
            raise ValueError("max_launches must be a positive integer")
        if not isinstance(self.retry, RetryPolicy):
            raise ValueError("retry must be a RetryPolicy")
        if self.resumed_usage_stop is not None:
            _nonempty_text(self.resumed_usage_stop, "resumed_usage_stop")
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "argv", argv)
        object.__setattr__(self, "problem", problem)
        object.__setattr__(self, "evidence", evidence)
        object.__setattr__(self, "missing_outputs", missing)
        object.__setattr__(self, "timeout_seconds", timeout)
        if not self._lifecycle_consistent():
            raise ValueError(f"inconsistent {status.value} launch record")

    def _lifecycle_consistent(self) -> bool:
        """The single statement of which evidence each status must carry.

        harness.launch and Run.acknowledge_stopped emit records that satisfy
        these rules; loading a run record checks them again.
        """
        status = self.status
        started = self.started_at is not None
        finished = self.finished_at is not None
        has_pid = self.pid is not None
        exited = self.returncode is not None
        kind = self.problem["kind"] if self.problem is not None else None
        bare = kind is None and self.evidence is None and not self.missing_outputs
        if status in UNSETTLED_STATUSES:
            running = status == LaunchStatus.RUNNING
            return (
                bare and not exited and not finished and has_pid == running and started == running
            )
        if status == LaunchStatus.INTERRUPTED:
            return (
                finished
                and not exited
                and has_pid == started
                and kind == "operator_acknowledgment"
                and self.evidence is None
                and not self.missing_outputs
            )
        if status == LaunchStatus.LAUNCH_ERROR:
            return (
                started
                and finished
                and not has_pid
                and not exited
                and kind == "launch_error"
                and self.evidence is None
                and not self.missing_outputs
            )
        consistent = (
            has_pid
            and exited
            and started
            and finished
            and (kind is None or kind in _WORKER_PROBLEM_KINDS)
            and (
                not self.missing_outputs
                or status in {LaunchStatus.INCOMPLETE, LaunchStatus.PROTOCOL_ERROR}
            )
        )
        if status in {LaunchStatus.DELIVERED, LaunchStatus.INCOMPLETE}:
            consistent = (
                consistent
                and self.returncode == 0
                and kind is None
                and bool(self.missing_outputs) == (status == LaunchStatus.INCOMPLETE)
            )
        if status == LaunchStatus.WORKER_PROBLEM:
            consistent = consistent and kind == "unable"
        return consistent


def thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw_json(item) for item in value]
    return value


@dataclass(frozen=True)
class CallResult:
    """Call outcome; failed_at_launch_limit means failed with all launches spent.

    request_hash identifies work material, excluding execution controls.
    The limit flag uses the ceiling supplied to the current call.
    A false value does not imply retryability or remaining launch allowance.
    """

    call_id: str
    request_hash: str
    status: LaunchStatus
    outputs: Mapping[str, str]
    launches: tuple[LaunchRecord, ...]
    failed_at_launch_limit: bool
    reused: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", LaunchStatus(self.status))
        object.__setattr__(self, "outputs", MappingProxyType(dict(self.outputs)))
        object.__setattr__(self, "launches", tuple(self.launches))
