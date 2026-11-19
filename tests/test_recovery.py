"""Controller interruption, reservations and durable replay."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from worker_cases import _kill, _request, _worker

from workjig import (
    CommandWorker,
    RecordError,
    Request,
    RetryPolicy,
    Run,
    RunBusyError,
    UncertainLaunchError,
)

SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.mark.unit
def test_first_open_missing_record_preserves_directory_and_explains_recovery(
    tmp_path: Path,
) -> None:
    records = tmp_path / "records"
    records.mkdir()
    (records / ".lock").touch()
    with pytest.raises(RecordError, match="confirm any old workers have stopped"):
        with Run(records, tmp_path):
            pass
    assert not (records / "run.json").exists()
    assert (records / ".lock").exists()


@pytest.mark.unit
def test_lock_workdir_binding_and_closed_controller(tmp_path: Path) -> None:
    records = tmp_path / "records"
    other = tmp_path / "other"
    other.mkdir()
    worker = CommandWorker((sys.executable, "-c", "pass"))
    with Run(records, tmp_path) as run:
        with pytest.raises(RunBusyError):
            with Run(records, tmp_path):
                pass
    with pytest.raises(RuntimeError, match="not open"):
        run.call(_request(worker))
    with pytest.raises(RecordError, match="workdir"):
        with Run(records, other):
            pass


@pytest.mark.subprocess
def test_cleanup_error_leaves_reservation_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = tmp_path / "records"
    script = tmp_path / "sleep.py"
    script.write_text("import time\ntime.sleep(5)\n", encoding="utf-8")
    call = _request(_worker(script), timeout=0.05)

    def fail_cleanup(_: object) -> bool:
        raise PermissionError("injected cleanup failure")

    monkeypatch.setattr("workjig.harness._cleanup_group", fail_cleanup)
    try:
        with Run(records, tmp_path) as run:
            with pytest.raises(PermissionError, match="injected cleanup failure"):
                run.call(call)
            with pytest.raises(RecordError, match="controller state is uncertain"):
                run.call(call)
    finally:
        # The injected failure deliberately leaves a real subprocess alive.
        if (records / "run.json").exists():
            state = json.loads((records / "run.json").read_text(encoding="utf-8"))
            attempts = state["calls"].get("job", {}).values()
            pids = [attempt["pid"] for entry in attempts for attempt in entry["attempts"]]
            for pid in pids:
                if pid is not None:
                    _kill(pid, group=True)

    with Run(records, tmp_path) as run:
        unresolved = run.unresolved_launches()
        assert len(unresolved) == 1
        assert unresolved[0].status == "running"
        run.acknowledge_stopped(unresolved[0].launch_id)


@pytest.mark.subprocess
def test_acknowledgment_spends_prelaunch_reservation_and_blocks_other_ids(tmp_path: Path) -> None:
    records = tmp_path / "records"
    script = tmp_path / "worker.py"
    script.write_text(
        "from pathlib import Path\nimport sys\n"
        "Path(sys.argv[3], 'result.txt').write_text('done')\n",
        encoding="utf-8",
    )
    worker = _worker(script)
    call = _request(worker, max_launches=2, retry=RetryPolicy(on=("interrupted",)))
    controller = tmp_path / "controller.py"
    controller.write_text(
        "import os\n"
        "import workjig.run as module\n"
        "from workjig import CommandWorker, Request, RetryPolicy, Run\n"
        "module.launch = lambda *a, **kw: os._exit(17)\n"
        f"worker = CommandWorker({worker.argv!r})\n"
        "call = Request('job', 'Read input and write output.', {'input.txt': 'payload'}, "
        "('result.txt',), worker, timeout_seconds=2, max_launches=2, "
        "retry=RetryPolicy(on=('interrupted',)))\n"
        f"with Run({str(records)!r}, {str(tmp_path)!r}) as run: run.call(call)\n",
        encoding="utf-8",
    )
    env = dict(os.environ, PYTHONPATH=str(SRC))
    ended = subprocess.run([sys.executable, str(controller)], cwd=tmp_path, env=env, check=False)
    assert ended.returncode == 17
    with Run(records, tmp_path) as run:
        unresolved = run.unresolved_launches()
        assert len(unresolved) == 1
        other = Request("other", call.task, call.inputs, call.required_outputs, worker)
        with pytest.raises(UncertainLaunchError):
            run.call(other)
        run.acknowledge_stopped(unresolved[0].launch_id)
        delivered = run.call(call)
    assert delivered.status == "delivered"
    assert len(delivered.launches) == 2
    assert delivered.launches[0].status == "interrupted"


@pytest.mark.subprocess
def test_completed_record_survives_exit_after_atomic_commit(tmp_path: Path) -> None:
    records = tmp_path / "records"
    marker = tmp_path / "launches.txt"
    script = tmp_path / "worker.py"
    script.write_text(
        "from pathlib import Path\nimport sys\n"
        "with Path(sys.argv[5]).open('a') as f: f.write('launch\\n')\n"
        "Path(sys.argv[3], 'result.txt').write_text('done')\n",
        encoding="utf-8",
    )
    worker = _worker(script, str(marker))
    call = _request(worker)
    controller = tmp_path / "controller.py"
    controller.write_text(
        "import os\n"
        "from workjig import CommandWorker, Request, Run\n"
        "original = Run._write_state\n"
        "def after_commit(self):\n"
        "    original(self)\n"
        "    state = self._state\n"
        "    if any(e.delivery is not None for v in state.calls.values() "
        "for e in v.values()):\n"
        "        os._exit(23)\n"
        "Run._write_state = after_commit\n"
        f"worker = CommandWorker({worker.argv!r})\n"
        "call = Request('job', 'Read input and write output.', {'input.txt': 'payload'}, "
        "('result.txt',), worker, timeout_seconds=2)\n"
        f"with Run({str(records)!r}, {str(tmp_path)!r}) as run: run.call(call)\n",
        encoding="utf-8",
    )
    ended = subprocess.run(
        [sys.executable, str(controller)],
        cwd=tmp_path,
        env=dict(os.environ, PYTHONPATH=str(SRC)),
        check=False,
    )
    assert ended.returncode == 23
    with Run(records, tmp_path) as run:
        result = run.call(call)
    assert result.status == "delivered"
    assert result.reused
    assert marker.read_text(encoding="utf-8").splitlines() == ["launch"]
