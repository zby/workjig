"""Read-only inspection of run records: validation, snapshots and lock freedom."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import FrozenInstanceError, fields
from pathlib import Path

import pytest

from workjig import (
    CommandWorker,
    LaunchRecord,
    RecordedCall,
    RecordError,
    Request,
    read_run,
)
from workjig.records import Delivery, RunState, launch_to_json, material_hash, request_material

SRC = Path(__file__).resolve().parents[1] / "src"
STAMP = "2026-01-01T00:00:00+00:00"


def _request(call_id: str, task: str = "Write answer") -> Request:
    return Request(call_id, task, {"in.txt": "data"}, ("answer.txt",), CommandWorker(("worker",)))


def _launch(launch_id: str, status: str, **updates: object) -> LaunchRecord:
    settled = {"pid": 7, "returncode": 0, "started_at": STAMP, "finished_at": STAMP}
    fields_for = {
        "reserved": {},
        "running": {"pid": 7, "started_at": STAMP},
        "delivered": settled,
        "process_error": {**settled, "returncode": 1},
    }
    return LaunchRecord(
        launch_id,
        status,
        f"/original/records/launches/{launch_id}",
        argv=("worker",),
        **{**fields_for[status], **updates},
    )


def _entry(request: Request, launches: list[LaunchRecord], outputs: dict | None = None) -> dict:
    result = None
    if outputs is not None:
        result = Delivery(outputs, launches[-1].launch_id, material_hash(outputs)).to_json()
    return {
        "request": request_material(request),
        "attempts": [launch_to_json(launch) for launch in launches],
        "result": result,
    }


def _write(records: Path, calls: dict[str, list[tuple[Request, dict]]]) -> None:
    state = {
        "version": 2,
        "workdir": "/original/workdir",
        "calls": {
            call_id: {
                material_hash(request_material(request)): entry for request, entry in versions
            }
            for call_id, versions in calls.items()
        },
    }
    records.mkdir(parents=True, exist_ok=True)
    (records / "run.json").write_text(json.dumps(state), encoding="utf-8")


@pytest.fixture
def records(tmp_path: Path) -> Path:
    delivered, failed, pending = _request("delivered"), _request("failed"), _request("pending")
    old, new = _request("versions", "Old task"), _request("versions", "New task")
    path = tmp_path / "records"
    _write(
        path,
        {
            "delivered": [
                (delivered, _entry(delivered, [_launch("d1", "delivered")], {"answer.txt": "42"}))
            ],
            "failed": [(failed, _entry(failed, [_launch("f1", "process_error")]))],
            "pending": [(pending, _entry(pending, [_launch("p1", "running")]))],
            "versions": [
                (old, _entry(old, [_launch("v1", "process_error")])),
                (new, _entry(new, [_launch("v2", "reserved")])),
            ],
        },
    )
    return path


@pytest.mark.unit
def test_view_reports_recorded_calls(records: Path) -> None:
    view = read_run(records)
    assert view is not None
    assert view.workdir == "/original/workdir"
    assert view.record_dir == records.resolve()
    by_id: dict[str, list[RecordedCall]] = {}
    for call in view.calls:
        by_id.setdefault(call.call_id, []).append(call)

    (delivered,) = by_id["delivered"]
    assert delivered.status == "delivered"
    assert dict(delivered.outputs) == {"answer.txt": "42"}
    assert delivered.request_hash == material_hash(request_material(_request("delivered")))
    assert delivered.task == "Write answer"
    assert dict(delivered.inputs) == {"in.txt": "data"}
    assert delivered.required_outputs == ("answer.txt",)
    assert delivered.optional_outputs == ()
    assert delivered.worker == CommandWorker(("worker",))
    assert [launch.launch_id for launch in delivered.launches] == ["d1"]

    (failed,) = by_id["failed"]
    assert failed.status == "process_error"
    assert dict(failed.outputs) == {}

    (pending,) = by_id["pending"]
    assert pending.status == "running"

    versions = {call.task: call for call in by_id["versions"]}
    assert set(versions) == {"Old task", "New task"}
    assert versions["Old task"].status == "process_error"
    assert versions["New task"].status == "reserved"
    assert versions["Old task"].request_hash != versions["New task"].request_hash

    assert {launch.launch_id for launch in view.unresolved_launches()} == {"p1", "v2"}


@pytest.mark.unit
def test_view_is_immutable(records: Path) -> None:
    view = read_run(records)
    assert view is not None
    call = view.calls[0]
    with pytest.raises(FrozenInstanceError):
        view.workdir = "/elsewhere"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        call.status = "delivered"  # type: ignore[misc]
    with pytest.raises(TypeError):
        call.outputs["answer.txt"] = "changed"  # type: ignore[index]
    with pytest.raises(TypeError):
        call.inputs["in.txt"] = "changed"  # type: ignore[index]
    assert isinstance(view.calls, tuple) and isinstance(call.launches, tuple)


@pytest.mark.unit
def test_recorded_call_exposes_all_work_material() -> None:
    work = {f.name for f in fields(Request) if not f.metadata.get("execution_control")}
    assert work <= {f.name for f in fields(RecordedCall)}


@pytest.mark.unit
def test_launch_files_follow_a_copied_record_directory(records: Path, tmp_path: Path) -> None:
    copy = tmp_path / "moved" / "copy"
    shutil.copytree(records, copy)
    view = read_run(copy)
    assert view is not None
    launch = next(call for call in view.calls if call.call_id == "delivered").launches[0]
    assert launch.launch_dir == "/original/records/launches/d1"
    files = view.launch_files(launch)
    base = copy.resolve() / "launches" / "d1"
    assert files.prompt == base / "prompt.txt"
    assert files.stdout == base / "stdout.log"
    assert files.stderr == base / "stderr.log"
    assert files.problem == base / "problem.json"
    assert files.inputs == base / "inputs"
    assert files.outputs == base / "outputs"
    with pytest.raises(ValueError):
        view.launch_files(_launch("unknown", "reserved"))


@pytest.mark.unit
def test_reading_creates_nothing(records: Path, tmp_path: Path) -> None:
    def snapshot() -> list[tuple[str, int]]:
        return sorted((str(p), p.stat().st_mtime_ns) for p in tmp_path.rglob("*"))

    before = snapshot()
    assert read_run(records) is not None
    assert read_run(tmp_path / "absent") is None
    assert snapshot() == before
    assert not (records / ".lock").exists()


@pytest.mark.unit
def test_absent_record_is_none(tmp_path: Path) -> None:
    assert read_run(tmp_path / "absent") is None
    empty = tmp_path / "empty"
    empty.mkdir()
    assert read_run(empty) is None
    assert list(empty.iterdir()) == []


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        json.dumps({"version": 1, "workdir": "/w", "calls": {}}),
        json.dumps({"version": 3, "workdir": "/w", "calls": {}}),
        b"\xff\xfe",
    ],
)
@pytest.mark.unit
def test_corrupt_or_unsupported_record_raises(tmp_path: Path, content: str | bytes) -> None:
    (tmp_path / "run.json").write_bytes(content if isinstance(content, bytes) else content.encode())
    with pytest.raises(RecordError):
        read_run(tmp_path)


@pytest.mark.unit
def test_entry_without_launches_is_rejected(tmp_path: Path) -> None:
    request = _request("job")
    _write(tmp_path, {"job": [(request, _entry(request, []))]})
    with pytest.raises(RecordError, match="no launches"):
        read_run(tmp_path)
    with pytest.raises(RecordError, match="no launches"):
        RunState.from_json(json.loads((tmp_path / "run.json").read_text()))


@pytest.mark.subprocess
def test_read_while_another_process_holds_the_run(tmp_path: Path) -> None:
    records, gate = tmp_path / "records", tmp_path / "gate"
    script = tmp_path / "worker.py"
    script.write_text(
        "import sys, time\nfrom pathlib import Path\n"
        "while not Path(sys.argv[1]).exists(): time.sleep(0.02)\n"
        "Path(sys.argv[2], 'answer.txt').write_text('done')\n",
        encoding="utf-8",
    )
    controller = tmp_path / "controller.py"
    controller.write_text(
        "from workjig import CommandWorker, Request, Run\n"
        f"worker = CommandWorker(({sys.executable!r}, {str(script)!r}, {str(gate)!r}, "
        "'{output_dir}'))\n"
        "call = Request('job', 'Write answer', {}, ('answer.txt',), worker, timeout_seconds=60)\n"
        f"with Run({str(records)!r}, {str(tmp_path)!r}) as run:\n"
        "    assert run.call(call).status == 'delivered'\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [sys.executable, str(controller)], cwd=tmp_path, env=dict(os.environ, PYTHONPATH=str(SRC))
    )
    try:
        deadline = time.monotonic() + 30
        view = None
        while time.monotonic() < deadline and process.poll() is None:
            view = read_run(records)
            if view is not None and any(
                launch.status == "running" for launch in view.unresolved_launches()
            ):
                break
            time.sleep(0.02)
        assert view is not None, "controller never wrote a record"
        (running,) = view.unresolved_launches()
        assert running.status == "running"
        assert view.launch_files(running).stdout.parent == Path(running.launch_dir)
        gate.write_text("go", encoding="utf-8")
        assert process.wait(timeout=30) == 0
    finally:
        gate.touch()
        if process.poll() is None:
            process.kill()
        process.wait(timeout=30)
    final = read_run(records)
    assert final is not None
    (call,) = final.calls
    assert call.status == "delivered"
    assert dict(call.outputs) == {"answer.txt": "done"}
    assert final.unresolved_launches() == ()
    assert final.launch_files(call.launches[0]).stdout.exists()
