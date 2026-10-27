"""Exclusive run records, launch accounting, recovery, and parallel calls."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

from .harness import launch, prepare
from .locking import _HOLD_MARKER, RunBusyError, acquire, release
from .models import (
    FAILURE_STATUSES,
    CallResult,
    CommandWorker,
    LaunchRecord,
    Request,
    RetryPolicy,
    _positive_number,
)


class RecordError(RuntimeError):
    """A run record is absent, corrupt, or internally inconsistent."""


class UncertainLaunchError(RuntimeError):
    """A prior launch may still be running; explicit recovery is required."""

    def __init__(self, launches: Iterable[LaunchRecord]) -> None:
        self.launches = tuple(launches)
        super().__init__(
            "unresolved launch reservation(s): " + ", ".join(a.launch_id for a in self.launches)
        )


def _request_material(request: Request) -> dict[str, object]:
    return {
        "task": request.task,
        "inputs": dict(request.inputs),
        "required_outputs": list(request.required_outputs),
        "optional_outputs": list(request.optional_outputs),
        "worker": {
            "name": request.worker.name,
            "argv": list(request.worker.argv),
            "env": dict(request.worker.env),
            "evidence_file": request.worker.evidence_file,
            "output_protocol": request.worker.output_protocol,
        },
    }


def _hash(material: Mapping[str, object]) -> str:
    encoded = json.dumps(
        material, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _validated_request(call_id: str, material: object) -> Request:
    if not isinstance(material, dict) or set(material) != {
        "task",
        "inputs",
        "required_outputs",
        "optional_outputs",
        "worker",
    }:
        raise RecordError("invalid request material")
    worker = material["worker"]
    worker_fields = {"name", "argv", "env", "evidence_file", "output_protocol"}
    if not isinstance(worker, dict) or set(worker) != worker_fields:
        raise RecordError("invalid worker material")
    try:
        request = Request(
            call_id=call_id,
            task=material["task"],
            inputs=material["inputs"],
            required_outputs=tuple(material["required_outputs"]),
            optional_outputs=tuple(material["optional_outputs"]),
            worker=CommandWorker(
                argv=tuple(worker["argv"]),
                name=worker["name"],
                env=worker["env"],
                evidence_file=worker["evidence_file"],
                output_protocol=worker["output_protocol"],
            ),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RecordError(f"invalid request material: {error}") from error
    if _request_material(request) != material:
        raise RecordError("request material is not canonical")
    return request


def _attempt_to_dict(attempt: LaunchRecord) -> dict[str, object]:
    return {
        "launch_id": attempt.launch_id,
        "status": attempt.status,
        "launch_dir": attempt.launch_dir,
        "argv": list(attempt.argv) if attempt.argv is not None else None,
        "pid": attempt.pid,
        "returncode": attempt.returncode,
        "started_at": attempt.started_at,
        "finished_at": attempt.finished_at,
        "problem": dict(attempt.problem) if attempt.problem is not None else None,
        "evidence": _thaw(attempt.evidence),
        "missing_outputs": list(attempt.missing_outputs),
        "timeout_seconds": attempt.timeout_seconds,
        "max_launches": attempt.max_launches,
        "retry": {"on": list(attempt.retry.on), "backoff_seconds": attempt.retry.backoff_seconds},
        "resumed_usage_stop": attempt.resumed_usage_stop,
    }


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _attempt_from_dict(value: object) -> LaunchRecord:
    if not isinstance(value, dict):
        raise RecordError("attempt is not an object")
    fields = {
        "launch_id",
        "status",
        "launch_dir",
        "argv",
        "pid",
        "returncode",
        "started_at",
        "finished_at",
        "problem",
        "evidence",
        "missing_outputs",
        "timeout_seconds",
        "max_launches",
        "retry",
        "resumed_usage_stop",
    }
    if set(value) != fields:
        raise RecordError("invalid attempt fields")
    try:
        timeout = _positive_number(value["timeout_seconds"], "timeout_seconds")
        retry = value["retry"]
        if not isinstance(retry, dict) or set(retry) != {"on", "backoff_seconds"}:
            raise ValueError("invalid retry fields")
        if not isinstance(retry["on"], list):
            raise ValueError("invalid retry outcomes")
        policy = RetryPolicy(tuple(retry["on"]), retry["backoff_seconds"])
        if type(value["max_launches"]) is not int or value["max_launches"] < 1:
            raise ValueError("invalid launch ceiling")
        stop = value["resumed_usage_stop"]
        if stop is not None and (not isinstance(stop, str) or not stop):
            raise ValueError("invalid usage stop reference")
    except (TypeError, ValueError) as error:
        raise RecordError(f"invalid launch controls: {error}") from error
    for field in ("launch_id", "launch_dir"):
        if not isinstance(value[field], str) or not value[field]:
            raise RecordError(f"invalid attempt {field}")
    status = value["status"]
    if not isinstance(status, str) or status not in FAILURE_STATUSES | {
        "reserved",
        "running",
        "delivered",
    }:
        raise RecordError("invalid attempt status")
    argv = value["argv"]
    if (
        not isinstance(argv, list)
        or not argv
        or not all(isinstance(arg, str) and arg for arg in argv)
    ):
        raise RecordError("invalid attempt argv")
    for field in ("pid", "returncode"):
        number = value[field]
        if number is not None and (type(number) is not int or (field == "pid" and number <= 0)):
            raise RecordError(f"invalid attempt {field}")
    for field in ("started_at", "finished_at"):
        timestamp = value[field]
        if timestamp is not None:
            try:
                if (
                    not isinstance(timestamp, str)
                    or datetime.fromisoformat(timestamp).tzinfo is None
                ):
                    raise ValueError("expected timezone-aware timestamp")
            except ValueError as error:
                raise RecordError(f"invalid attempt {field}") from error
    problem = value["problem"]
    if problem is not None and (
        not isinstance(problem, dict)
        or set(problem) != {"kind", "message"}
        or not all(isinstance(item, str) for item in problem.values())
        or problem["kind"]
        not in {"unable", "usage_limit", "launch_error", "operator_acknowledgment"}
    ):
        raise RecordError("invalid attempt problem")
    evidence = value["evidence"]
    if evidence is not None:
        if not isinstance(evidence, dict):
            raise RecordError("invalid attempt evidence")
        try:
            json.dumps(evidence, allow_nan=False)
        except (TypeError, ValueError) as error:
            raise RecordError("invalid attempt evidence") from error
    missing = value["missing_outputs"]
    if (
        not isinstance(missing, list)
        or not all(isinstance(name, str) and name for name in missing)
        or len(set(missing)) != len(missing)
    ):
        raise RecordError("invalid attempt missing_outputs")

    # These invariants correspond to harness.launch and acknowledge_stopped.
    pid, returncode = value["pid"], value["returncode"]
    started, finished = value["started_at"], value["finished_at"]
    if status in {"reserved", "running"}:
        valid = (
            returncode is None
            and finished is None
            and problem is None
            and evidence is None
            and not missing
            and (
                (pid is None and started is None)
                if status == "reserved"
                else (pid is not None and started is not None)
            )
        )
    elif status == "interrupted":
        valid = (
            finished is not None
            and returncode is None
            and ((pid is None) == (started is None))
            and problem is not None
            and problem["kind"] == "operator_acknowledgment"
            and evidence is None
            and not missing
        )
    elif status == "launch_error":
        valid = (
            started is not None
            and finished is not None
            and pid is None
            and returncode is None
            and problem is not None
            and problem["kind"] == "launch_error"
            and evidence is None
            and not missing
        )
    else:
        valid = (
            pid is not None
            and returncode is not None
            and started is not None
            and finished is not None
            and (problem is None or problem["kind"] in {"unable", "usage_limit"})
            and (not missing or status in {"incomplete", "protocol_error"})
        )
        if status in {"delivered", "incomplete"}:
            valid = (
                valid
                and returncode == 0
                and problem is None
                and bool(missing) == (status == "incomplete")
            )
        if status == "worker_problem":
            valid = valid and problem is not None and problem["kind"] == "unable"
    if not valid:
        raise RecordError(f"inconsistent {status} attempt")
    return LaunchRecord(
        **{
            **value,
            "argv": tuple(argv),
            "missing_outputs": tuple(missing),
            "timeout_seconds": timeout,
            "retry": policy,
        }
    )


def _validate_state(state: object) -> dict[str, object]:
    if (
        not isinstance(state, dict)
        or set(state) != {"version", "workdir", "calls"}
        or type(state.get("version")) is not int
        or state["version"] != 2
        or not isinstance(state.get("workdir"), str)
        or not isinstance(state.get("calls"), dict)
    ):
        raise RecordError("invalid run record structure or version")
    all_launch_ids: set[str] = set()
    for call_id, versions in state["calls"].items():
        if not isinstance(call_id, str) or not isinstance(versions, dict):
            raise RecordError("invalid call record")
        for request_hash, entry in versions.items():
            if not isinstance(entry, dict) or set(entry) != {"request", "attempts", "result"}:
                raise RecordError("invalid request record")
            material = entry["request"]
            request = _validated_request(call_id, material)
            if _hash(material) != request_hash:
                raise RecordError("request hash mismatch")
            attempts = entry["attempts"]
            if not isinstance(attempts, list):
                raise RecordError("attempts must be a list")
            parsed = [_attempt_from_dict(attempt) for attempt in attempts]
            if any(
                attempt.status in {"reserved", "running", "delivered"} for attempt in parsed[:-1]
            ):
                raise RecordError("invalid attempt order")
            for index, attempt in enumerate(parsed):
                if index >= attempt.max_launches:
                    raise RecordError("launch allowance exceeded in record")
                previous = parsed[index - 1] if index else None
                if attempt.resumed_usage_stop is not None:
                    if (
                        previous is None
                        or previous.status != "usage_limit"
                        or attempt.resumed_usage_stop != previous.launch_id
                    ):
                        raise RecordError("invalid usage continuation")
                if previous is not None and (
                    (previous.status == "usage_limit" and attempt.resumed_usage_stop is None)
                    or (
                        previous.status != "usage_limit" and previous.status not in attempt.retry.on
                    )
                ):
                    raise RecordError("unauthorized continuation")
                if not set(attempt.missing_outputs) <= set(request.required_outputs):
                    raise RecordError("attempt names undeclared missing outputs")
                if attempt.launch_id in all_launch_ids:
                    raise RecordError("duplicate launch id")
                all_launch_ids.add(attempt.launch_id)
            result = entry["result"]
            if result is not None:
                if (
                    not isinstance(result, dict)
                    or set(result) != {"status", "outputs", "launch_id", "content_hash"}
                    or result["status"] != "delivered"
                    or not isinstance(result["outputs"], dict)
                    or not all(
                        isinstance(k, str) and isinstance(v, str)
                        for k, v in result["outputs"].items()
                    )
                    or not set(request.required_outputs) <= set(result["outputs"])
                    or not set(result["outputs"])
                    <= set(request.required_outputs + request.optional_outputs)
                    or result["content_hash"] != _hash(result["outputs"])
                    or not parsed
                    or parsed[-1].status != "delivered"
                    or result["launch_id"] != parsed[-1].launch_id
                ):
                    raise RecordError("invalid delivered result record")
            elif parsed and parsed[-1].status == "delivered":
                raise RecordError("delivered attempt has no result record")
    return state


class Run:
    """One exclusive controller for local, run-scoped worker results.

    Enter the context before calling. The workdir is caller maintained; run
    records do not prove its ambient state stayed suitable for reuse.
    """

    def __init__(
        self,
        record_dir: str | Path,
        workdir: str | Path,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.record_dir = Path(record_dir).resolve()
        self.workdir = Path(workdir).resolve()
        self._sleep = sleep
        self._lock_file: object | None = None
        self._state: dict[str, object] | None = None
        self._poisoned = False
        self._mutex = threading.RLock()
        self._owned_launches: set[str] = set()
        self._active_calls: set[str] = set()

    def __enter__(self) -> Run:
        if self._lock_file is not None:
            raise RuntimeError("run already open")
        if not self.workdir.is_dir():
            raise ValueError("workdir must be an existing directory")
        self.record_dir.mkdir(parents=True, exist_ok=True)
        lock_path = self.record_dir / ".lock"
        had_lock = lock_path.exists()
        lock_file = lock_path.open("a+b")
        try:
            acquire(lock_file)
        except BlockingIOError as error:
            lock_file.close()
            raise RunBusyError(f"run records are already owned: {self.record_dir}") from error
        try:
            state_path = self.record_dir / "run.json"
            if state_path.exists():
                try:
                    self._state = _validate_state(
                        json.loads(state_path.read_text(encoding="utf-8"))
                    )
                except (
                    OSError,
                    UnicodeError,
                    json.JSONDecodeError,
                    TypeError,
                    ValueError,
                ) as error:
                    raise RecordError(f"cannot read run record: {error}") from error
                if self._state["workdir"] != str(self.workdir):
                    raise RecordError("run workdir differs from recorded workdir")
            else:
                lock_file.seek(0)
                lock_marker = lock_file.read(len(_HOLD_MARKER))
                if (had_lock and lock_marker != _HOLD_MARKER) or any(
                    path.name != ".lock" for path in self.record_dir.iterdir()
                ):
                    raise RecordError(
                        "run record is missing; preserve this directory for inspection, "
                        "confirm any old workers have stopped, and start a new run in a "
                        "fresh directory with deliberately chosen launch allowances"
                    )
                self._state = {"version": 2, "workdir": str(self.workdir), "calls": {}}
                self._write_state()
            (self.record_dir / "launches").mkdir(exist_ok=True)
            self._lock_file = lock_file
            self._poisoned = False
            return self
        except BaseException:
            release(lock_file)
            lock_file.close()
            self._state = None
            raise

    def __exit__(self, *_: object) -> None:
        with self._mutex:
            if self._active_calls:
                raise RuntimeError("cannot close run while calls are active")
            if self._lock_file is not None:
                release(self._lock_file)
                self._lock_file.close()
            self._lock_file = None
            self._state = None
            self._poisoned = False
            self._owned_launches.clear()

    def _ensure_open(self) -> None:
        if self._lock_file is None or self._state is None:
            raise RuntimeError("run is not open")
        if self._poisoned:
            raise RecordError("controller state is uncertain; close and reopen the run")

    def _write_state(self) -> None:
        if self._state is None:
            raise RuntimeError("run has no state")
        encoded = json.dumps(
            self._state, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        )
        fd, name = tempfile.mkstemp(prefix=".run-", suffix=".json", dir=self.record_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(encoded)
                file.flush()
                os.fsync(file.fileno())
            os.replace(name, self.record_dir / "run.json")
            if os.name != "nt":
                directory_fd = os.open(self.record_dir, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    def _entry(
        self, call_id: str, request_hash: str, material: dict[str, object]
    ) -> dict[str, object]:
        assert self._state is not None
        calls = self._state["calls"]
        assert isinstance(calls, dict)
        versions = calls.setdefault(call_id, {})
        return versions.setdefault(
            request_hash, {"request": material, "attempts": [], "result": None}
        )

    def unresolved_launches(self) -> tuple[LaunchRecord, ...]:
        """Return reservations not settled by a durable completion record."""
        with self._mutex:
            self._ensure_open()
            assert self._state is not None
            unresolved = []
            for versions in self._state["calls"].values():
                for entry in versions.values():
                    for value in entry["attempts"]:
                        attempt = _attempt_from_dict(value)
                        if attempt.status in {"reserved", "running"}:
                            unresolved.append(attempt)
            return tuple(unresolved)

    def _assert_no_stale_launches(self) -> None:
        stale = [a for a in self.unresolved_launches() if a.launch_id not in self._owned_launches]
        if stale:
            raise UncertainLaunchError(stale)

    def acknowledge_stopped(self, launch_id: str) -> None:
        """Record the operator's confirmation that an uncertain old worker stopped.

        This does not inspect or kill a process. Do not call until external
        evidence establishes the old process and its children have stopped.
        """
        with self._mutex:
            self._ensure_open()
            if launch_id in self._owned_launches:
                raise ValueError("cannot acknowledge an active launch owned by this controller")
            assert self._state is not None
            for versions in self._state["calls"].values():
                for entry in versions.values():
                    for index, value in enumerate(entry["attempts"]):
                        attempt = _attempt_from_dict(value)
                        if attempt.launch_id != launch_id:
                            continue
                        if attempt.status not in {"reserved", "running"}:
                            raise ValueError("launch is already settled")
                        settled = replace(
                            attempt,
                            status="interrupted",
                            finished_at=datetime.now(UTC).isoformat(),
                            problem={
                                "kind": "operator_acknowledgment",
                                "message": "operator confirmed worker stopped",
                            },
                        )
                        entry["attempts"][index] = _attempt_to_dict(settled)
                        try:
                            self._write_state()
                        except BaseException:
                            self._poisoned = True
                            raise
                        return
            raise KeyError(launch_id)

    def _result(
        self, request: Request, request_hash: str, entry: dict[str, object], reused: bool
    ) -> CallResult:
        attempts = tuple(_attempt_from_dict(value) for value in entry["attempts"])
        delivered = entry["result"]
        if delivered is not None:
            status = "delivered"
            outputs = delivered["outputs"]
        elif attempts:
            status = attempts[-1].status
            outputs = {}
        else:
            raise RecordError("call has neither result nor attempt")
        return CallResult(
            call_id=request.call_id,
            request_hash=request_hash,
            status=status,
            outputs=outputs,
            launches=attempts,
            failed_at_launch_limit=status != "delivered" and len(attempts) >= request.max_launches,
            reused=reused,
        )

    def call(self, request: Request, *, resume_usage_stop: str | None = None) -> CallResult:
        """Run or replay work; optionally approve continuation of one usage stop.

        Approval names a usage-limit launch in this exact call history. Repeating
        approval after continuation or delivery is harmless. New usage stops
        require new approval, and all launches still spend the total allowance.
        """
        if not isinstance(request, Request):
            raise TypeError("request must be a Request")
        if resume_usage_stop is not None and (
            not isinstance(resume_usage_stop, str) or not resume_usage_stop
        ):
            raise ValueError("resume_usage_stop must be a nonempty launch ID")
        material = _request_material(request)
        request_hash = _hash(material)
        with self._mutex:
            self._ensure_open()
            self._assert_no_stale_launches()
            if request.call_id in self._active_calls:
                raise ValueError(f"call is already active: {request.call_id}")
            self._active_calls.add(request.call_id)
        may_have_reserved = False
        try:
            while True:
                with self._mutex:
                    self._ensure_open()
                    self._assert_no_stale_launches()
                    assert self._state is not None
                    entry = self._state["calls"].get(request.call_id, {}).get(request_hash)
                    prior = (
                        tuple(_attempt_from_dict(value) for value in entry["attempts"])
                        if entry is not None
                        else ()
                    )
                    if resume_usage_stop is not None and not any(
                        a.launch_id == resume_usage_stop and a.status == "usage_limit"
                        for a in prior
                    ):
                        raise ValueError(
                            "resume_usage_stop must name a usage stop in this call history"
                        )
                    entry = self._entry(request.call_id, request_hash, material)
                    if entry["result"] is not None:
                        return self._result(request, request_hash, entry, reused=True)
                    approved_stop = None
                    if prior:
                        previous = prior[-1]
                        if (
                            previous.status == "usage_limit"
                            and previous.launch_id == resume_usage_stop
                        ):
                            approved_stop = resume_usage_stop
                        if len(prior) >= request.max_launches or (
                            previous.status not in request.retry.on and approved_stop is None
                        ):
                            return self._result(request, request_hash, entry, reused=True)
                if prior and request.retry.backoff_seconds:
                    self._sleep(request.retry.backoff_seconds)
                launch_id = uuid.uuid4().hex
                launch_dir = self.record_dir / "launches" / launch_id
                launch_dir.mkdir()
                paths, argv = prepare(request, launch_dir, self.workdir)
                reserved = LaunchRecord(
                    launch_id=launch_id,
                    status="reserved",
                    launch_dir=str(launch_dir),
                    argv=argv,
                    timeout_seconds=request.timeout_seconds,
                    max_launches=request.max_launches,
                    retry=request.retry,
                    resumed_usage_stop=approved_stop,
                )
                with self._mutex:
                    self._assert_no_stale_launches()
                    entry = self._entry(request.call_id, request_hash, material)
                    may_have_reserved = True
                    entry["attempts"].append(_attempt_to_dict(reserved))
                    self._write_state()
                    self._owned_launches.add(launch_id)

                def on_started(
                    pid: int,
                    command: tuple[str, ...],
                    started_at: str,
                    reserved: LaunchRecord = reserved,
                    entry: dict[str, object] = entry,
                ) -> None:
                    with self._mutex:
                        running = replace(
                            reserved, status="running", pid=pid, argv=command, started_at=started_at
                        )
                        entry["attempts"][-1] = _attempt_to_dict(running)
                        self._write_state()

                attempt, outputs = launch(request, self.workdir, paths, argv, reserved, on_started)
                with self._mutex:
                    entry["attempts"][-1] = _attempt_to_dict(attempt)
                    if attempt.status == "delivered":
                        entry["result"] = {
                            "status": "delivered",
                            "outputs": outputs,
                            "launch_id": launch_id,
                            "content_hash": _hash(outputs),
                        }
                    self._write_state()
                    self._owned_launches.remove(launch_id)
                    may_have_reserved = False
                    if attempt.status == "delivered":
                        return self._result(request, request_hash, entry, reused=False)
                    if (
                        len(entry["attempts"]) >= request.max_launches
                        or attempt.status not in request.retry.on
                    ):
                        return self._result(request, request_hash, entry, reused=False)
        except BaseException:
            if may_have_reserved:
                with self._mutex:
                    self._poisoned = True
            raise
        finally:
            with self._mutex:
                self._active_calls.discard(request.call_id)

    def parallel(self, requests: Iterable[Request], *, max_workers: int) -> tuple[CallResult, ...]:
        """Settle all independent calls before surfacing any controller error."""
        self._ensure_open()
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 1:
            raise ValueError("max_workers must be a positive integer")
        items = tuple(requests)
        if len({request.call_id for request in items}) != len(items):
            raise ValueError("parallel calls need distinct call IDs")
        if not items:
            return ()
        results: list[CallResult | None] = [None] * len(items)
        errors: list[Exception] = []
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(self.call, request): index for index, request in enumerate(items)
            }
            for future in as_completed(futures):
                index = futures[future]
                try:
                    results[index] = future.result()
                except Exception as error:
                    errors.append(error)
        if errors:
            raise ExceptionGroup("parallel calls failed", errors)
        # Every future completed without raising, so every slot has a result.
        return cast(tuple[CallResult, ...], tuple(results))
