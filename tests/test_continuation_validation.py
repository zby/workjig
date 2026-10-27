"""Direct validation of execution controls and continuation authorization."""

from dataclasses import replace

import pytest

from workjig import CommandWorker, LaunchRecord, RecordError, Request, RetryPolicy
from workjig.records import RunState, launch_to_json, material_hash, request_material

pytestmark = pytest.mark.unit


@pytest.fixture
def state():
    request = Request("job", "Write answer", {}, ("answer.txt",), CommandWorker(("worker",)))
    material = request_material(request)
    reservation = LaunchRecord("launch", "reserved", "/private/launch", argv=("worker",))
    return {
        "version": 2,
        "workdir": "/private",
        "calls": {
            "job": {
                material_hash(material): {
                    "request": material,
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
def test_invalid_recorded_controls_rejected(state, field, bad):
    entry = next(iter(state["calls"]["job"].values()))
    entry["attempts"][0][field] = bad
    with pytest.raises(RecordError):
        RunState.from_json(state)


def test_reservation_records_validate_without_process_start(state):
    assert RunState.from_json(state).to_json() == state


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
    state, status, approval, ceiling, retry, valid
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
        assert RunState.from_json(state).to_json() == state
    else:
        with pytest.raises(RecordError):
            RunState.from_json(state)


def test_third_launch_cannot_use_ceiling_two(state):
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
        RunState.from_json(state)
