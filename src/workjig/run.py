"""Exclusive run controller: launch accounting, recovery, and parallel calls."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import cast

from .harness import launch, prepare
from .locking import RunLock
from .models import UNSETTLED_STATUSES, CallResult, LaunchRecord, LaunchStatus, Request
from .platform import sync_directory
from .records import (
    CallEntry,
    Delivery,
    RecordError,
    RunState,
    material_hash,
    request_material,
)

__all__ = ["RecordError", "Run", "UncertainLaunchError"]


class UncertainLaunchError(RuntimeError):
    """A prior launch may still be running; explicit recovery is required."""

    def __init__(self, launches: Iterable[LaunchRecord]) -> None:
        self.launches = tuple(launches)
        super().__init__(
            "unresolved launch reservation(s): " + ", ".join(a.launch_id for a in self.launches)
        )


@dataclass(frozen=True)
class _LaunchPlan:
    """One more launch is permitted, after an optional backoff."""

    approved_stop: str | None
    backoff_seconds: float


def _plan_launch(
    entry: CallEntry | None, request: Request, resume_usage_stop: str | None
) -> _LaunchPlan | None:
    """Decide from the recorded history whether this call may launch again.

    None means the history is final under the current execution controls and
    must be replayed. Raises ValueError when resume_usage_stop names no usage
    stop in this exact history.
    """
    prior = tuple(entry.launches) if entry is not None else ()
    if resume_usage_stop is not None and not any(
        a.launch_id == resume_usage_stop and a.status == LaunchStatus.USAGE_LIMIT for a in prior
    ):
        raise ValueError("resume_usage_stop must name a usage stop in this call history")
    if entry is not None and entry.delivery is not None:
        return None
    if not prior:
        return _LaunchPlan(approved_stop=None, backoff_seconds=0.0)
    previous = prior[-1]
    approved_stop = None
    if previous.status == LaunchStatus.USAGE_LIMIT and previous.launch_id == resume_usage_stop:
        approved_stop = resume_usage_stop
    if len(prior) >= request.max_launches or (
        previous.status not in request.retry.on and approved_stop is None
    ):
        return None
    return _LaunchPlan(approved_stop=approved_stop, backoff_seconds=request.retry.backoff_seconds)


def _history_final(entry: CallEntry, request: Request) -> bool:
    """True once the latest launch settles the call under the current controls."""
    latest = entry.launches[-1]
    return (
        latest.status == LaunchStatus.DELIVERED
        or len(entry.launches) >= request.max_launches
        or latest.status not in request.retry.on
    )


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
        self._lock: RunLock | None = None
        self._state: RunState | None = None
        self._poisoned = False
        self._mutex = threading.RLock()
        self._owned_launches: set[str] = set()
        self._active_calls: set[str] = set()

    # -- lifecycle -----------------------------------------------------------

    def __enter__(self) -> Run:
        if self._lock is not None:
            raise RuntimeError("run already open")
        if not self.workdir.is_dir():
            raise ValueError("workdir must be an existing directory")
        lock = RunLock(self.record_dir)
        lock.acquire()
        try:
            self._state = self._load_or_create(lock)
            (self.record_dir / "launches").mkdir(exist_ok=True)
            self._lock = lock
            self._poisoned = False
            return self
        except BaseException:
            lock.release()
            self._state = None
            raise

    def __exit__(self, *_: object) -> None:
        with self._mutex:
            if self._active_calls:
                raise RuntimeError("cannot close run while calls are active")
            if self._lock is not None:
                self._lock.release()
            self._lock = None
            self._state = None
            self._poisoned = False
            self._owned_launches.clear()

    def _load_or_create(self, lock: RunLock) -> RunState:
        state_path = self.record_dir / "run.json"
        if state_path.exists():
            try:
                state = RunState.from_json(json.loads(state_path.read_text(encoding="utf-8")))
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError) as error:
                raise RecordError(f"cannot read run record: {error}") from error
            if state.workdir != str(self.workdir):
                raise RecordError("run workdir differs from recorded workdir")
            return state
        if (lock.preexisted and not lock.marked_held()) or any(
            path.name != ".lock" for path in self.record_dir.iterdir()
        ):
            raise RecordError(
                "run record is missing; preserve this directory for inspection, "
                "confirm any old workers have stopped, and start a new run in a "
                "fresh directory with deliberately chosen launch allowances"
            )
        self._state = RunState(workdir=str(self.workdir))
        self._write_state()
        return self._state

    def _ensure_open(self) -> RunState:
        if self._lock is None or self._state is None:
            raise RuntimeError("run is not open")
        if self._poisoned:
            raise RecordError("controller state is uncertain; close and reopen the run")
        return self._state

    def _write_state(self) -> None:
        if self._state is None:
            raise RuntimeError("run has no state")
        encoded = json.dumps(
            self._state.to_json(),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        fd, name = tempfile.mkstemp(prefix=".run-", suffix=".json", dir=self.record_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(encoded)
                file.flush()
                os.fsync(file.fileno())
            os.replace(name, self.record_dir / "run.json")
            sync_directory(self.record_dir)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    # -- recovery ------------------------------------------------------------

    def unresolved_launches(self) -> tuple[LaunchRecord, ...]:
        """Return reservations not settled by a durable completion record."""
        with self._mutex:
            return self._ensure_open().unresolved()

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
            state = self._ensure_open()
            if launch_id in self._owned_launches:
                raise ValueError("cannot acknowledge an active launch owned by this controller")
            for entry, index in state.launches():
                record = entry.launches[index]
                if record.launch_id != launch_id:
                    continue
                if record.status not in UNSETTLED_STATUSES:
                    raise ValueError("launch is already settled")
                entry.launches[index] = replace(
                    record,
                    status=LaunchStatus.INTERRUPTED,
                    finished_at=datetime.now(UTC).isoformat(),
                    problem={
                        "kind": "operator_acknowledgment",
                        "message": "operator confirmed worker stopped",
                    },
                )
                try:
                    self._write_state()
                except BaseException:
                    self._poisoned = True
                    raise
                return
            raise KeyError(launch_id)

    # -- calls ---------------------------------------------------------------

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
        request_hash = material_hash(request_material(request))
        with self._mutex:
            self._ensure_open()
            self._assert_no_stale_launches()
            if request.call_id in self._active_calls:
                raise ValueError(f"call is already active: {request.call_id}")
            self._active_calls.add(request.call_id)
        # Once a reservation may be durable, any exception leaves this
        # controller uncertain until it is closed and reopened.
        may_have_reserved = False
        try:
            while True:
                with self._mutex:
                    state = self._ensure_open()
                    self._assert_no_stale_launches()
                    entry = state.find(request.call_id, request_hash)
                    plan = _plan_launch(entry, request, resume_usage_stop)
                    if plan is None:
                        assert entry is not None
                        return self._result(entry, request, reused=True)
                if plan.backoff_seconds:
                    self._sleep(plan.backoff_seconds)
                reserved, paths, argv = self._prepare_launch(request, plan.approved_stop)
                with self._mutex:
                    self._assert_no_stale_launches()
                    entry = self._ensure_open().entry(request, request_hash)
                    may_have_reserved = True
                    self._reserve(entry, reserved)
                attempt, outputs = launch(
                    request,
                    self.workdir,
                    paths,
                    argv,
                    reserved,
                    partial(self._mark_running, entry, reserved),
                )
                with self._mutex:
                    self._commit(entry, attempt, outputs)
                    may_have_reserved = False
                    if _history_final(entry, request):
                        return self._result(entry, request, reused=False)
        except BaseException:
            if may_have_reserved:
                with self._mutex:
                    self._poisoned = True
            raise
        finally:
            with self._mutex:
                self._active_calls.discard(request.call_id)

    def _prepare_launch(
        self, request: Request, approved_stop: str | None
    ) -> tuple[LaunchRecord, Mapping[str, Path], tuple[str, ...]]:
        """Give the launch fresh paths and build its reservation; nothing is durable yet."""
        launch_id = uuid.uuid4().hex
        launch_dir = self.record_dir / "launches" / launch_id
        launch_dir.mkdir()
        paths, argv = prepare(request, launch_dir, self.workdir)
        reserved = LaunchRecord(
            launch_id=launch_id,
            status=LaunchStatus.RESERVED,
            launch_dir=str(launch_dir),
            argv=argv,
            timeout_seconds=request.timeout_seconds,
            max_launches=request.max_launches,
            retry=request.retry,
            resumed_usage_stop=approved_stop,
        )
        return reserved, paths, argv

    def _reserve(self, entry: CallEntry, reserved: LaunchRecord) -> None:
        """Spend one launch allowance durably before any process starts."""
        entry.launches.append(reserved)
        self._write_state()
        self._owned_launches.add(reserved.launch_id)

    def _mark_running(
        self,
        entry: CallEntry,
        reserved: LaunchRecord,
        pid: int,
        command: tuple[str, ...],
        started_at: str,
    ) -> None:
        with self._mutex:
            entry.launches[-1] = replace(
                reserved, status=LaunchStatus.RUNNING, pid=pid, argv=command, started_at=started_at
            )
            self._write_state()

    def _commit(self, entry: CallEntry, attempt: LaunchRecord, outputs: dict[str, str]) -> None:
        """Settle the owned launch; a delivered outcome commits its outputs atomically."""
        entry.launches[-1] = attempt
        if attempt.status == LaunchStatus.DELIVERED:
            entry.delivery = Delivery(outputs, attempt.launch_id, material_hash(outputs))
        self._write_state()
        self._owned_launches.remove(attempt.launch_id)

    def _result(self, entry: CallEntry, request: Request, reused: bool) -> CallResult:
        launches = tuple(entry.launches)
        if entry.delivery is not None:
            status: str = LaunchStatus.DELIVERED
            outputs: Mapping[str, str] = entry.delivery.outputs
        elif launches:
            status = launches[-1].status
            outputs = {}
        else:
            raise RecordError("call has neither result nor attempt")
        return CallResult(
            call_id=request.call_id,
            request_hash=entry.request_hash,
            status=status,
            outputs=outputs,
            launches=launches,
            failed_at_launch_limit=status != LaunchStatus.DELIVERED
            and len(launches) >= request.max_launches,
            reused=reused,
        )

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
