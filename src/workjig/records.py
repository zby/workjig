"""Run record schema: canonical request material, typed state and its JSON form.

The persisted layout is ``run.json`` version 2. Loading validates every field
and every cross-record rule before the controller trusts the state; in memory
the state is typed and is serialized again only when written.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field, fields

from .models import (
    UNSETTLED_STATUSES,
    CommandWorker,
    LaunchRecord,
    LaunchStatus,
    Request,
    RetryPolicy,
    WorkjigError,
    thaw_json,
)

RECORD_VERSION = 2
_LAUNCH_FIELDS = frozenset(item.name for item in fields(LaunchRecord))
# Work material is every Request field that is neither the call key nor a
# declared execution control, so a new field cannot be left out by mistake.
_WORK_FIELDS = tuple(
    item.name
    for item in fields(Request)
    if item.name != "call_id" and not item.metadata.get("execution_control")
)
_WORKER_FIELDS = tuple(item.name for item in fields(CommandWorker))


class RecordError(WorkjigError):
    """A run record is absent, corrupt, or internally inconsistent."""


def _plain(value: object) -> object:
    """JSON-shaped copy of a frozen value, with the same spelling run.json uses."""
    if isinstance(value, CommandWorker):
        return {name: _plain(getattr(value, name)) for name in _WORKER_FIELDS}
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    return value


def request_material(request: Request) -> dict[str, object]:
    """Identity-bearing work material; execution controls are excluded."""
    return {name: _plain(getattr(request, name)) for name in _WORK_FIELDS}


def material_hash(material: Mapping[str, object]) -> str:
    encoded = json.dumps(
        material, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _request_from_material(call_id: str, material: object) -> Request:
    if not isinstance(material, dict) or set(material) != set(_WORK_FIELDS):
        raise RecordError("invalid request material")
    worker = material["worker"]
    if not isinstance(worker, dict) or set(worker) != set(_WORKER_FIELDS):
        raise RecordError("invalid worker material")
    try:
        request = Request(
            call_id=call_id,
            **{name: material[name] for name in _WORK_FIELDS if name != "worker"},
            worker=CommandWorker(**worker),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise RecordError(f"invalid request material: {error}") from error
    if request_material(request) != material:
        raise RecordError("request material is not canonical")
    return request


def launch_to_json(record: LaunchRecord) -> dict[str, object]:
    return {
        "launch_id": record.launch_id,
        "status": record.status.value,
        "launch_dir": record.launch_dir,
        "argv": list(record.argv),
        "pid": record.pid,
        "returncode": record.returncode,
        "started_at": record.started_at,
        "finished_at": record.finished_at,
        "problem": dict(record.problem) if record.problem is not None else None,
        "evidence": thaw_json(record.evidence),
        "missing_outputs": list(record.missing_outputs),
        "timeout_seconds": record.timeout_seconds,
        "max_launches": record.max_launches,
        "retry": {"on": list(record.retry.on), "backoff_seconds": record.retry.backoff_seconds},
        "resumed_usage_stop": record.resumed_usage_stop,
    }


def launch_from_json(value: object) -> LaunchRecord:
    """Rebuild one launch record; LaunchRecord itself enforces the lifecycle rules."""
    if not isinstance(value, dict):
        raise RecordError("launch record is not an object")
    if set(value) != _LAUNCH_FIELDS:
        raise RecordError("invalid launch record fields")
    retry = value["retry"]
    if (
        not isinstance(retry, dict)
        or set(retry) != {"on", "backoff_seconds"}
        or not isinstance(retry["on"], list)
    ):
        raise RecordError("invalid launch retry controls")
    for name in ("argv", "missing_outputs"):
        if not isinstance(value[name], list):
            raise RecordError(f"invalid launch {name}")
    for name in ("problem", "evidence"):
        if value[name] is not None and not isinstance(value[name], dict):
            raise RecordError(f"invalid launch {name}")
    try:
        return LaunchRecord(
            **{
                **value,
                "argv": tuple(value["argv"]),
                "missing_outputs": tuple(value["missing_outputs"]),
                "retry": RetryPolicy(tuple(retry["on"]), retry["backoff_seconds"]),
            }
        )
    except (TypeError, ValueError) as error:
        raise RecordError(f"invalid launch record: {error}") from error


@dataclass(frozen=True)
class Delivery:
    """The committed outputs of a delivered launch."""

    outputs: Mapping[str, str]
    launch_id: str
    content_hash: str

    def to_json(self) -> dict[str, object]:
        return {
            "status": LaunchStatus.DELIVERED.value,
            "outputs": dict(self.outputs),
            "launch_id": self.launch_id,
            "content_hash": self.content_hash,
        }


@dataclass
class CallEntry:
    """One call identity's launch history under one work-material hash."""

    request: Request
    request_hash: str
    launches: list[LaunchRecord] = field(default_factory=list)
    delivery: Delivery | None = None

    def to_json(self) -> dict[str, object]:
        return {
            "request": request_material(self.request),
            "attempts": [launch_to_json(launch) for launch in self.launches],
            "result": self.delivery.to_json() if self.delivery is not None else None,
        }

    @classmethod
    def from_json(cls, call_id: str, request_hash: str, value: object) -> CallEntry:
        if not isinstance(value, dict) or set(value) != {"request", "attempts", "result"}:
            raise RecordError("invalid request record")
        material = value["request"]
        request = _request_from_material(call_id, material)
        if material_hash(material) != request_hash:
            raise RecordError("request hash mismatch")
        attempts = value["attempts"]
        if not isinstance(attempts, list):
            raise RecordError("attempts must be a list")
        launches = [launch_from_json(attempt) for attempt in attempts]
        result = value["result"]
        delivery = None
        if result is not None:
            if (
                not isinstance(result, dict)
                or set(result) != {"status", "outputs", "launch_id", "content_hash"}
                or result["status"] != LaunchStatus.DELIVERED
                or not isinstance(result["outputs"], dict)
                or not all(
                    isinstance(k, str) and isinstance(v, str) for k, v in result["outputs"].items()
                )
                or result["content_hash"] != material_hash(result["outputs"])
            ):
                raise RecordError("invalid delivered result record")
            delivery = Delivery(result["outputs"], result["launch_id"], result["content_hash"])
        entry = cls(request, request_hash, launches, delivery)
        entry.check_history()
        return entry

    def check_history(self) -> None:
        """Reject histories that no sequence of reservations could have produced."""
        launches = self.launches
        if any(
            launch.status in UNSETTLED_STATUSES or launch.status == LaunchStatus.DELIVERED
            for launch in launches[:-1]
        ):
            raise RecordError("invalid launch order")
        required = set(self.request.required_outputs)
        for index, launch in enumerate(launches):
            if index >= launch.max_launches:
                raise RecordError("launch allowance exceeded in record")
            previous = launches[index - 1] if index else None
            if launch.resumed_usage_stop is not None and (
                previous is None
                or previous.status != LaunchStatus.USAGE_LIMIT
                or launch.resumed_usage_stop != previous.launch_id
            ):
                raise RecordError("invalid usage continuation")
            if previous is not None and (
                (previous.status == LaunchStatus.USAGE_LIMIT and launch.resumed_usage_stop is None)
                or (
                    previous.status != LaunchStatus.USAGE_LIMIT
                    and previous.status not in launch.retry.on
                )
            ):
                raise RecordError("unauthorized continuation")
            if not set(launch.missing_outputs) <= required:
                raise RecordError("launch names undeclared missing outputs")
        last = launches[-1] if launches else None
        if self.delivery is not None:
            declared = required | set(self.request.optional_outputs)
            if (
                not required <= set(self.delivery.outputs)
                or not set(self.delivery.outputs) <= declared
                or last is None
                or last.status != LaunchStatus.DELIVERED
                or self.delivery.launch_id != last.launch_id
            ):
                raise RecordError("invalid delivered result record")
        elif last is not None and last.status == LaunchStatus.DELIVERED:
            raise RecordError("delivered launch has no result record")


@dataclass
class RunState:
    """Everything ``run.json`` holds, keyed by call identity then work material."""

    workdir: str
    calls: dict[str, dict[str, CallEntry]] = field(default_factory=dict)

    def find(self, call_id: str, request_hash: str) -> CallEntry | None:
        return self.calls.get(call_id, {}).get(request_hash)

    def entry(self, request: Request, request_hash: str) -> CallEntry:
        """Return the history for this work material, creating an empty one."""
        versions = self.calls.setdefault(request.call_id, {})
        existing = versions.get(request_hash)
        if existing is None:
            existing = versions[request_hash] = CallEntry(request, request_hash)
        return existing

    def launches(self) -> Iterator[tuple[CallEntry, int]]:
        for versions in self.calls.values():
            for entry in versions.values():
                for index in range(len(entry.launches)):
                    yield entry, index

    def unresolved(self) -> tuple[LaunchRecord, ...]:
        return tuple(
            entry.launches[index]
            for entry, index in self.launches()
            if entry.launches[index].status in UNSETTLED_STATUSES
        )

    def to_json(self) -> dict[str, object]:
        return {
            "version": RECORD_VERSION,
            "workdir": self.workdir,
            "calls": {
                call_id: {request_hash: entry.to_json() for request_hash, entry in versions.items()}
                for call_id, versions in self.calls.items()
            },
        }

    @classmethod
    def from_json(cls, value: object) -> RunState:
        if (
            not isinstance(value, dict)
            or set(value) != {"version", "workdir", "calls"}
            or type(value["version"]) is not int
            or value["version"] != RECORD_VERSION
            or not isinstance(value["workdir"], str)
            or not isinstance(value["calls"], dict)
        ):
            raise RecordError("invalid run record structure or version")
        calls: dict[str, dict[str, CallEntry]] = {}
        seen: set[str] = set()
        for call_id, versions in value["calls"].items():
            if not isinstance(call_id, str) or not isinstance(versions, dict):
                raise RecordError("invalid call record")
            entries: dict[str, CallEntry] = {}
            for request_hash, raw in versions.items():
                entry = CallEntry.from_json(call_id, request_hash, raw)
                for launch in entry.launches:
                    if launch.launch_id in seen:
                        raise RecordError("duplicate launch id")
                    seen.add(launch.launch_id)
                entries[request_hash] = entry
            calls[call_id] = entries
        return cls(workdir=value["workdir"], calls=calls)
