"""Per-call execution controls and stop-specific continuation across restarts."""

import importlib
import json
import sys
from dataclasses import replace

import pytest

from workjig import CommandWorker, RecordError, Request, RetryPolicy, Run, UncertainLaunchError

pytestmark = pytest.mark.subprocess


@pytest.fixture
def job(tmp_path):
    script = tmp_path / "worker.py"
    script.write_text(
        "import json, sys, time\nfrom pathlib import Path\n"
        "with Path('starts').open('a') as f: f.write('start\\n')\n"
        "mode = Path('mode').read_text()\n"
        "if mode == 'timeout': time.sleep(10)\n"
        "if mode == 'usage':\n"
        " Path(sys.argv[2]).write_text(json.dumps({'kind': 'usage_limit', 'message': 'stop'}))\n"
        " sys.exit(0)\n"
        "if mode == 'fail': sys.exit(1)\n"
        "Path(sys.argv[1]).write_text('answer')\n"
    )
    request = Request(
        "job",
        "Write answer",
        {},
        ("answer.txt",),
        CommandWorker((sys.executable, str(script), "{output_dir}/answer.txt", "{problem_file}")),
    )
    (tmp_path / "mode").write_text("deliver")
    return tmp_path / "records", tmp_path, request


def mode(workdir, value):
    (workdir / "mode").write_text(value)


def test_changed_controls_replay_delivered_and_preserve_history(job):
    records, workdir, request = job
    with Run(records, workdir) as run:
        first = run.call(request)
    original = (records / "run.json").read_bytes()
    changed = replace(
        request, timeout_seconds=900, max_launches=4, retry=RetryPolicy(("timeout",), 0.1)
    )
    with Run(records, workdir) as run:
        cached = run.call(changed)
        different = run.call(replace(changed, inputs={"new.txt": "new work"}))
    assert cached.reused and cached.request_hash == first.request_hash
    assert cached.launches == first.launches
    assert different.request_hash != first.request_hash
    assert len((workdir / "starts").read_text().splitlines()) == 2
    # Replay alone does not mutate the record.
    with Run(records, workdir) as run:
        before = (records / "run.json").read_bytes()
        assert run.call(changed).reused
        assert (records / "run.json").read_bytes() == before
    assert original != before  # Only the changed work added a history.


def test_extend_ceiling_and_policy_without_refunding(job):
    records, workdir, request = job
    mode(workdir, "fail")
    with Run(records, workdir) as run:
        first = run.call(request)
        assert first.failed_at_launch_limit
        assert run.call(replace(request, timeout_seconds=10)).reused
        # Capacity alone doesn't override the old failure's retry gate.
        assert run.call(replace(request, max_launches=2)).reused
    sleeps = []
    extended = replace(
        request, timeout_seconds=10, max_launches=2, retry=RetryPolicy(("process_error",), 0.2)
    )
    with Run(records, workdir, sleep=sleeps.append) as run:
        second = run.call(extended)
        assert len(second.launches) == 2
        assert second.launches[0] == first.launches[0]
        assert second.launches[-1].timeout_seconds == 10
        assert second.launches[-1].max_launches == 2
        assert second.launches[-1].retry == extended.retry
        assert second.launches[0].launch_dir != second.launches[1].launch_dir
        assert run.call(extended).reused
    assert sleeps == [0.2]
    # Lower current controls do not invalidate historical ceilings.
    with Run(records, workdir) as run:
        lower = run.call(request)
        assert lower.reused and lower.failed_at_launch_limit
        capacity_only = run.call(replace(request, max_launches=3))
        assert not capacity_only.failed_at_launch_limit
        assert len(capacity_only.launches) == 2
    mode(workdir, "deliver")
    with Run(records, workdir) as run:
        result = run.call(replace(extended, max_launches=3))
    assert result.status == "delivered" and len(result.launches) == 3
    assert result.request_hash == first.request_hash


def test_longer_timeout_only_applies_to_new_launch(job):
    records, workdir, request = job
    mode(workdir, "timeout")
    short = replace(request, timeout_seconds=0.2)
    with Run(records, workdir) as run:
        first = run.call(short)
        assert first.status == "timeout"
        assert run.call(replace(short, timeout_seconds=2)).reused
    mode(workdir, "deliver")
    with Run(records, workdir) as run:
        result = run.call(
            replace(short, timeout_seconds=2, max_launches=2, retry=RetryPolicy(("timeout",)))
        )
    assert result.status == "delivered"
    assert [a.timeout_seconds for a in result.launches] == [0.2, 2]


def test_usage_approval_is_stop_specific_and_replay_safe(job):
    records, workdir, request = job
    request = replace(request, max_launches=5, retry=RetryPolicy(("process_error",)))
    mode(workdir, "usage")
    with Run(records, workdir) as run:
        first = run.call(request)
        stop = first.launches[-1].launch_id
        assert first.status == "usage_limit" and len(first.launches) == 1
        assert run.call(request).reused
        second = run.call(request, resume_usage_stop=stop)
        assert second.status == "usage_limit" and len(second.launches) == 2
        assert second.launches[-1].resumed_usage_stop == stop
        assert run.call(request, resume_usage_stop=stop).reused
    mode(workdir, "deliver")
    second_stop = second.launches[-1].launch_id
    with Run(records, workdir) as run:
        assert run.call(request, resume_usage_stop=stop).status == "usage_limit"
        result = run.call(request, resume_usage_stop=second_stop)
        assert result.status == "delivered" and len(result.launches) == 3
        assert run.call(request, resume_usage_stop=second_stop).reused
        assert run.call(request, resume_usage_stop=stop).reused
        with pytest.raises(ValueError, match="this call history"):
            run.call(replace(request, inputs={"x": "different"}), resume_usage_stop=stop)


def test_usage_approval_needs_remaining_allowance(job):
    records, workdir, request = job
    mode(workdir, "usage")
    with Run(records, workdir) as run:
        first = run.call(request)
        stop = first.launches[-1].launch_id
        assert run.call(request, resume_usage_stop=stop).reused
        mode(workdir, "deliver")
        result = run.call(replace(request, max_launches=2), resume_usage_stop=stop)
    assert result.status == "delivered" and len(result.launches) == 2


def test_approved_continuation_uses_ordinary_retries(job):
    records, workdir, request = job
    request = replace(request, max_launches=3, retry=RetryPolicy(("process_error",), 0.01))
    mode(workdir, "usage")
    with Run(records, workdir) as run:
        stop = run.call(request).launches[-1].launch_id
    mode(workdir, "fail")
    sleeps = []

    def backoff(delay):
        sleeps.append(delay)
        if len(sleeps) == 2:
            mode(workdir, "deliver")

    with Run(records, workdir, sleep=backoff) as run:
        result = run.call(request, resume_usage_stop=stop)
    assert [a.status for a in result.launches] == ["usage_limit", "process_error", "delivered"]
    assert [a.resumed_usage_stop for a in result.launches] == [None, stop, None]
    assert sleeps == [0.01, 0.01]
    with Run(records, workdir) as run:
        assert run.call(request, resume_usage_stop=stop).reused


def test_crash_after_approval_reservation_requires_recovery(job, monkeypatch):
    records, workdir, request = job
    mode(workdir, "usage")
    with Run(records, workdir) as run:
        stop = run.call(request).launches[-1].launch_id
    request = replace(request, max_launches=3, retry=RetryPolicy(("interrupted",)))
    module = importlib.import_module("workjig.run")
    original = module.launch

    def interrupt(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(module, "launch", interrupt)
    with Run(records, workdir) as run:
        with pytest.raises(KeyboardInterrupt):
            run.call(request, resume_usage_stop=stop)
    monkeypatch.setattr(module, "launch", original)
    mode(workdir, "deliver")
    with Run(records, workdir) as run:
        with pytest.raises(UncertainLaunchError):
            run.call(request, resume_usage_stop=stop)
        (unresolved,) = run.unresolved_launches()
        assert unresolved.resumed_usage_stop == stop
        # The injected interruption happened before process start: no worker exists.
        run.acknowledge_stopped(unresolved.launch_id)
        result = run.call(request, resume_usage_stop=stop)
    assert [a.status for a in result.launches] == ["usage_limit", "interrupted", "delivered"]
    assert result.launches[-1].resumed_usage_stop is None
    assert len((workdir / "starts").read_text().splitlines()) == 2


@pytest.mark.parametrize("approval", ["", 12, "unknown"])
def test_invalid_approval_never_launches(job, approval):
    records, workdir, request = job
    with Run(records, workdir) as run:
        with pytest.raises(ValueError):
            run.call(request, resume_usage_stop=approval)
    assert not (workdir / "starts").exists()


def test_rejected_approval_does_not_create_history(job):
    records, workdir, request = job
    mode(workdir, "usage")
    with Run(records, workdir) as run:
        stop = run.call(request).launches[-1].launch_id
        for rejected in (
            replace(request, call_id="rejected"),
            replace(request, inputs={"new": "different"}),
        ):
            with pytest.raises(ValueError):
                run.call(rejected, resume_usage_stop=stop)
        mode(workdir, "deliver")
        run.call(replace(request, call_id="other"))
    state = json.loads((records / "run.json").read_text())
    assert set(state["calls"]) == {"job", "other"}
    assert len(state["calls"]["job"]) == 1


def test_approval_reservation_write_failure_does_not_consume_permission(job, monkeypatch):
    records, workdir, request = job
    mode(workdir, "usage")
    with Run(records, workdir) as run:
        stop = run.call(request).launches[-1].launch_id
    request = replace(request, max_launches=2)
    original_record = (records / "run.json").read_bytes()
    with Run(records, workdir) as run:

        def fail_write():
            raise OSError("injected before durable reservation")

        monkeypatch.setattr(run, "_write_state", fail_write)
        with pytest.raises(OSError):
            run.call(request, resume_usage_stop=stop)
        with pytest.raises(RecordError, match="uncertain"):
            run.call(request, resume_usage_stop=stop)
    assert (records / "run.json").read_bytes() == original_record
    assert len((workdir / "starts").read_text().splitlines()) == 1
    mode(workdir, "deliver")
    with Run(records, workdir) as run:
        assert not run.unresolved_launches()
        result = run.call(request, resume_usage_stop=stop)
    assert result.status == "delivered" and len(result.launches) == 2


def test_parallel_replays_siblings_and_continues_timeout(job):
    records, workdir, request = job
    a, b = replace(request, call_id="a"), replace(request, call_id="b")
    c = replace(request, call_id="c", timeout_seconds=0.2)
    with Run(records, workdir) as run:
        originals = run.parallel([a, b], max_workers=2)
        mode(workdir, "timeout")
        assert run.call(c).status == "timeout"
    mode(workdir, "deliver")
    with Run(records, workdir) as run:
        results = run.parallel(
            [
                replace(a, timeout_seconds=2),
                replace(b, timeout_seconds=2),
                replace(c, timeout_seconds=2, max_launches=2, retry=RetryPolicy(("timeout",))),
            ],
            max_workers=3,
        )
    assert [r.status for r in results] == ["delivered"] * 3
    assert all(r.reused for r in results[:2])
    assert [r.launches for r in results[:2]] == [r.launches for r in originals]
    assert len(results[2].launches) == 2


def test_old_record_version_rejected(job):
    records, workdir, request = job
    with Run(records, workdir) as run:
        run.call(request)
    path = records / "run.json"
    state = json.loads(path.read_text())
    state["version"] = 1
    path.write_text(json.dumps(state))
    with pytest.raises(RecordError, match="version"):
        with Run(records, workdir):
            pass
