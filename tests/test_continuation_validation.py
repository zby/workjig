"""Direct validation of execution controls and continuation authorization."""

from dataclasses import replace

import pytest

from workjig import (
    CommandWorker,
    LaunchRecord,
    RecordError,
    Request,
    RetryPolicy,
    RunBusyError,
    UncertainLaunchError,
    WorkjigError,
)
from workjig.payloads import PayloadStore
from workjig.records import (
    RunState,
    launch_to_json,
    material_hash,
    request_material,
    stored_material,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def store(tmp_path):
    return PayloadStore(tmp_path)


@pytest.fixture
def state(store):
    request = Request("job", "Write answer", {}, ("answer.txt",), CommandWorker(("worker",)))
    material = request_material(request)
    reservation = LaunchRecord("launch", "reserved", "/private/launch", argv=("worker",))
    return {
        "version": 4,
        "workdir": "/private",
        "calls": {
            "job": {
                material_hash(material): {
                    "request": stored_material(request, store),
                    "attempts": [launch_to_json(reservation)],
                    "result": None,
                }
            }
        },
    }


@pytest.mark.parametrize(
    "field,bad",
    [
        ("timeout_seconds", 0),
        ("timeout_seconds", True),
        ("timeout_seconds", float("nan")),
        ("max_launches", 0),
        ("max_launches", True),
        ("retry", {"on": ["usage_limit"], "backoff_seconds": 0}),
        ("retry", {"on": ["protocol_error"], "backoff_seconds": 0}),
        ("retry", {"on": [], "backoff_seconds": -1}),
        ("retry", {"on": "timeout", "backoff_seconds": 0}),
        ("resumed_usage_stop", "unknown"),
        ("resumed_usage_stop", []),
    ],
)
def test_invalid_recorded_controls_rejected(state, store, field, bad):
    entry = next(iter(state["calls"]["job"].values()))
    entry["attempts"][0][field] = bad
    with pytest.raises(RecordError):
        RunState.from_json(state, store)


def test_reservation_records_validate_without_process_start(state, store):
    assert RunState.from_json(state, store).to_json() == state


@pytest.mark.parametrize(
    "status,approval,ceiling,retry,valid",
    [
        ("usage_limit", "old", 2, (), True),
        ("usage_limit", None, 2, (), False),
        ("usage_limit", "wrong", 2, (), False),
        ("usage_limit", "old", 1, (), False),
        ("process_error", "old", 2, (), False),
        ("process_error", None, 2, (), False),
        ("process_error", None, 2, ("process_error",), True),
        ("protocol_error", None, 2, (), False),
    ],
)
def test_history_requires_allowance_and_appropriate_authorization(
    state, store, status, approval, ceiling, retry, valid
):
    entry = next(iter(state["calls"]["job"].values()))
    finished = LaunchRecord(
        "old",
        status,
        "/private/old",
        argv=("worker",),
        pid=123,
        returncode=1,
        started_at="2026-10-20T00:00:00+00:00",
        finished_at="2026-10-20T00:00:01+00:00",
    )
    next_launch = entry["attempts"][0]
    next_launch["max_launches"] = ceiling
    next_launch["resumed_usage_stop"] = approval
    next_launch["retry"] = {"on": list(retry), "backoff_seconds": 0}
    entry["attempts"].insert(0, launch_to_json(finished))
    if valid:
        assert RunState.from_json(state, store).to_json() == state
    else:
        with pytest.raises(RecordError):
            RunState.from_json(state, store)


def test_third_launch_cannot_use_ceiling_two(state, store):
    entry = next(iter(state["calls"]["job"].values()))
    first = LaunchRecord(
        "one",
        "process_error",
        "/private/one",
        argv=("worker",),
        pid=123,
        returncode=1,
        started_at="2026-10-20T00:00:00+00:00",
        finished_at="2026-10-20T00:00:01+00:00",
        retry=RetryPolicy(("process_error",)),
    )
    second = replace(first, launch_id="two", max_launches=2)
    third = entry["attempts"][0]
    third["max_launches"] = 2
    third["retry"] = {"on": ["process_error"], "backoff_seconds": 0}
    entry["attempts"] = [launch_to_json(first), launch_to_json(second), third]
    with pytest.raises(RecordError, match="allowance exceeded"):
        RunState.from_json(state, store)


def test_work_material_is_every_request_field_except_controls():
    request = Request(
        "job",
        "Write answer",
        {"in.txt": "x"},
        ("answer.txt",),
        CommandWorker(("worker",), env={"K": "V"}),
        optional_outputs=("extra.txt",),
        timeout_seconds=5,
        max_launches=3,
        retry=RetryPolicy(("timeout",), 1),
    )
    assert request_material(request) == {
        "task": "Write answer",
        "inputs": {"in.txt": "x"},
        "required_outputs": ["answer.txt"],
        "optional_outputs": ["extra.txt"],
        "worker": {
            "name": "command",
            "argv": ["worker"],
            "env": {"K": "V"},
            "evidence_file": None,
            "output_protocol": None,
            "track_native_session": False,
        },
    }
    assert material_hash(request_material(request)) == material_hash(
        request_material(replace(request, timeout_seconds=1, max_launches=1, retry=RetryPolicy()))
    )


def test_runtime_errors_share_a_base():
    for error in (RecordError, UncertainLaunchError, RunBusyError):
        assert issubclass(error, WorkjigError)
