"""Reject corrupt persisted launch evidence before reuse or new execution."""

import json
import sys
from pathlib import Path

import pytest

from workjig import CommandWorker, RecordError, Request, Run


@pytest.fixture
def completed_record(tmp_path: Path):
    script = tmp_path / "worker.py"
    script.write_text(
        "import sys\nfrom pathlib import Path\n"
        "with Path('starts').open('a') as log: log.write('start\\n')\n"
        "Path(sys.argv[1]).write_text('done')\n"
    )
    request = Request(
        "job",
        "Write output",
        {},
        ("answer.txt",),
        CommandWorker((sys.executable, str(script), "{output_dir}/answer.txt")),
    )
    records = tmp_path / "records"
    with Run(records, tmp_path) as run:
        assert run.call(request).status == "delivered"
    path = records / "run.json"
    original = path.read_text()
    state = json.loads(original)
    entry = next(iter(state["calls"]["job"].values()))
    return path, original, state, entry, request


@pytest.mark.parametrize(
    "field, bad",
    [
        ("launch_id", []),
        ("launch_id", ""),
        ("launch_dir", 42),
        ("status", []),
        ("status", "unknown"),
        ("argv", "command"),
        ("argv", None),
        ("argv", []),
        ("argv", [42]),
        ("argv", [""]),
        ("pid", "not-a-pid"),
        ("pid", True),
        ("pid", 0),
        ("pid", -1),
        ("pid", 1.5),
        ("returncode", {}),
        ("returncode", False),
        ("returncode", 0.0),
        ("started_at", []),
        ("started_at", "yesterday"),
        ("started_at", "2026-01-01"),
        ("finished_at", 123),
        ("finished_at", "2026-01-01T00:00:00"),
        ("problem", []),
        ("problem", {"kind": "unable", "message": 5}),
        ("problem", {"kind": [], "message": "bad"}),
        ("evidence", []),
        ("evidence", "model"),
        ("evidence", {"nested": [float("nan")]}),
        ("missing_outputs", "answer.txt"),
        ("missing_outputs", [None]),
        ("missing_outputs", ["answer.txt", "answer.txt"]),
        ("pid", None),
        ("returncode", None),
        ("started_at", None),
        ("finished_at", None),
        ("returncode", 1),
        ("missing_outputs", ["answer.txt"]),
    ],
)
def test_corrupt_attempt_rejected_without_changing_record(completed_record, field, bad):
    path, original, state, entry, request = completed_record
    entry["attempts"][0][field] = bad
    damaged = json.dumps(state)
    path.write_text(damaged)
    with pytest.raises(RecordError):
        with Run(path.parent, path.parent.parent):
            pytest.fail("corrupt record was opened")
    assert path.read_text() == damaged
    assert (path.parent.parent / "starts").read_text() == "start\n"
    # Failed loading must release the lock and leave the controller reusable.
    path.write_text(original)
    with Run(path.parent, path.parent.parent) as run:
        assert run.call(request).reused
    assert (path.parent.parent / "starts").read_text() == "start\n"


@pytest.mark.parametrize("damage", ["missing-field", "extra-field", "not-object"])
def test_attempt_schema_is_checked(completed_record, damage):
    path, _, state, entry, _ = completed_record
    if damage == "missing-field":
        del entry["attempts"][0]["pid"]
    elif damage == "extra-field":
        entry["attempts"][0]["unexpected"] = "value"
    else:
        entry["attempts"][0] = []
    path.write_text(json.dumps(state))
    with pytest.raises(RecordError):
        with Run(path.parent, path.parent.parent):
            pytest.fail("corrupt record was opened")


@pytest.mark.parametrize(
    "status, updates",
    [
        ("reserved", {}),
        ("running", {}),
        ("launch_error", {}),
        ("interrupted", {}),
        ("incomplete", {}),
        ("incomplete", {"missing_outputs": ["undeclared.txt"]}),
        ("worker_problem", {}),
        ("timeout", {"problem": {"kind": "operator_acknowledgment", "message": "stopped"}}),
        ("process_error", {"problem": {"kind": "launch_error", "message": "failed"}}),
        ("timeout", {"missing_outputs": ["answer.txt"]}),
    ],
)
def test_inconsistent_lifecycle_record_is_rejected(completed_record, status, updates):
    path, _, state, entry, _ = completed_record
    entry["result"] = None
    entry["attempts"][0].update(status=status, **updates)
    path.write_text(json.dumps(state))
    with pytest.raises(RecordError):
        with Run(path.parent, path.parent.parent):
            pytest.fail("inconsistent record was opened")


@pytest.mark.parametrize(
    "status, body",
    [
        ("delivered", "output.write_text('done')"),
        ("incomplete", "pass"),
        ("worker_problem", "problem.write_text(json.dumps({'kind': 'unable', 'message': 'no'}))"),
        ("usage_limit", "print('Usage limit reached'); sys.exit(7)"),
        ("process_error", "sys.exit(3)"),
        ("protocol_error", "problem.write_text('broken JSON')"),
        ("timeout", "time.sleep(30)"),
        ("launch_error", "pass"),
    ],
)
def test_emitted_terminal_records_reopen_without_another_launch(tmp_path: Path, status, body):
    script = tmp_path / "worker.py"
    evidence = {"model": "synthetic", "details": [None, True, 3, 0.5, {"items": []}]}
    script.write_text(
        "import json, sys, time\nfrom pathlib import Path\n"
        "output, problem, evidence = map(Path, sys.argv[1:])\n"
        "with Path('starts').open('a') as log: log.write('start\\n')\n"
        f"evidence.write_text({json.dumps(evidence)!r})\n"
        f"{body}\n"
    )
    worker = CommandWorker(
        (
            sys.executable,
            str(script),
            "{output_dir}/answer.txt",
            "{problem_file}",
            "{evidence_file}",
        )
        if status != "launch_error"
        else (str(tmp_path / "missing-executable"),)
    )
    request = Request("job", "Write output", {}, ("answer.txt",), worker, timeout_seconds=0.5)
    records = tmp_path / "records"
    with Run(records, tmp_path) as run:
        result = run.call(request)
    assert result.status == status
    with Run(records, tmp_path) as run:
        replayed = run.call(request)
    assert replayed.reused
    assert replayed.status == status
    assert replayed.attempts == result.attempts
    if status != "launch_error":
        assert (tmp_path / "starts").read_text() == "start\n"
        assert replayed.attempts[0].evidence["model"] == "synthetic"
    if status == "timeout":
        assert replayed.attempts[0].returncode < 0
