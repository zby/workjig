"""Durability and process ownership cases at the local subprocess boundary."""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from workjig import (
    CommandWorker,
    RecordError,
    Request,
    RetryPolicy,
    Run,
    RunBusyError,
    UncertainLaunchError,
)
from workjig.harness import _cleanup_group

SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.mark.parametrize("ignore_term", [False, True])
def test_cleanup_reaps_cooperative_leader_and_escalates_uncooperative_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ignore_term: bool
) -> None:
    script = tmp_path / "term.py"
    handler = "signal.SIG_IGN" if ignore_term else "lambda *_: sys.exit(0)"
    script.write_text(
        "import signal, sys, time\n"
        f"signal.signal(signal.SIGTERM, {handler})\n"
        "print('ready', flush=True)\n"
        "while True: time.sleep(1)\n"
    )
    process = subprocess.Popen(
        [sys.executable, str(script)], stdout=subprocess.PIPE, start_new_session=True
    )
    signals = []
    killpg = os.killpg

    def observed_killpg(pgid: int, sig: int) -> None:
        if sig:
            signals.append(sig)
        killpg(pgid, sig)

    monkeypatch.setattr("workjig.harness.os.killpg", observed_killpg)
    try:
        assert select.select([process.stdout], [], [], 5)[0], "worker did not become ready"
        assert process.stdout.readline() == b"ready\n"
        assert _cleanup_group(process)
        process.wait(timeout=5)
        assert process.returncode == (-signal.SIGKILL if ignore_term else 0)
        assert signals == ([signal.SIGTERM, signal.SIGKILL] if ignore_term else [signal.SIGTERM])
    finally:
        if process.poll() is None:
            killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        process.stdout.close()


@pytest.mark.parametrize("report", ["none", "raw", "problem"])
def test_lingering_child_usage_precedence_and_replay(tmp_path: Path, report: str) -> None:
    script = tmp_path / "linger.py"
    script.write_text(
        "import json, subprocess, sys\n"
        "from pathlib import Path\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])\n"
        "Path(sys.argv[3], 'result.txt').write_text('premature')\n"
        + (
            "print('Usage limit reached')\nsys.exit(7)\n"
            if report == "raw"
            else 'Path(sys.argv[4]).write_text(\'{"kind":"usage_limit","message":"quota"}\')\n'
            if report == "problem"
            else ""
        )
    )
    request = _request(_worker(script), max_launches=2, retry=RetryPolicy(on=("worker_problem",)))
    records = tmp_path / "records"
    with Run(records, tmp_path) as run:
        result = run.call(request)
    assert result.status == ("process_error" if report == "none" else "usage_limit")
    assert result.outputs == {}
    assert len(result.launches) == 1
    with Run(records, tmp_path) as run:
        replay = run.call(request)
    assert replay.reused
    assert replay.status == result.status
    assert replay.launches == result.launches


def test_protocol_names_are_safe_inside_input_and_output_directories(tmp_path: Path) -> None:
    names = (
        "prompt.txt",
        "problem.json",
        "evidence.json",
        "stdout.log",
        "stderr.log",
        "inputs",
        "outputs",
    )
    worker = CommandWorker(
        (
            sys.executable,
            "-c",
            "import pathlib, shutil, sys; "
            "shutil.copytree(sys.argv[1], sys.argv[2], dirs_exist_ok=True)",
            "{input_dir}",
            "{output_dir}",
        )
    )
    request = Request("names", "Copy inputs.", dict.fromkeys(names, "payload"), names, worker)
    with Run(tmp_path / "records", tmp_path) as run:
        result = run.call(request)
    assert result.status == "delivered"
    assert result.outputs == dict(request.inputs)
    with Run(tmp_path / "records", tmp_path) as run:
        assert run.call(request).reused


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


def test_cooperative_timeout_commits_timeout_even_after_exit_zero(tmp_path: Path) -> None:
    script = tmp_path / "worker.py"
    stopped = tmp_path / "stopped"
    script.write_text(
        "import signal, sys, time\n"
        "from pathlib import Path\n"
        "def stop(*_):\n"
        f"    Path({str(stopped)!r}).write_text('clean shutdown')\n"
        "    sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "Path(sys.argv[3], 'result.txt').write_text('unfinished')\n"
        "while True: time.sleep(1)\n"
    )
    request = _request(_worker(script), timeout=2)
    with Run(tmp_path / "records", tmp_path) as run:
        result = run.call(request)
    assert result.status == "timeout"
    assert result.launches[0].returncode == 0
    assert result.outputs == {}
    assert stopped.read_text() == "clean shutdown"
    with Run(tmp_path / "records", tmp_path) as run:
        replay = run.call(request)
    assert replay.reused
    assert replay.status == "timeout"


def _worker(script: Path, *args: str) -> CommandWorker:
    return CommandWorker(
        (
            sys.executable,
            str(script),
            "{prompt}",
            "{input_dir}",
            "{output_dir}",
            "{problem_file}",
            *args,
        )
    )


def _request(
    worker: CommandWorker,
    *,
    timeout: float = 2,
    max_launches: int = 1,
    retry: RetryPolicy | None = None,
) -> Request:
    return Request(
        "job",
        "Read input and write output.",
        {"input.txt": "payload"},
        ("result.txt",),
        worker,
        timeout_seconds=timeout,
        max_launches=max_launches,
        retry=retry or RetryPolicy(),
    )


def test_worker_argv_preserves_literal_braces(tmp_path: Path) -> None:
    worker = CommandWorker((sys.executable, "-c", 'print({"x": 1})', "{prompt}"))
    prompt = tmp_path / "{input_dir}" / "prompt.txt"
    command = worker.command({"prompt": prompt})
    assert command[2] == 'print({"x": 1})'
    assert command[3] == str(prompt)


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
@pytest.mark.parametrize(
    "message, exit_code, expected",
    [
        ("Usage limit reached", 1, "usage_limit"),
        ("You've hit your usage limit", 7, "usage_limit"),
        ("Provider: USAGE LIMIT exceeded", 2, "usage_limit"),
        ("x" * 65532 + "Usage limit reached", 1, "usage_limit"),
        ("Usage limit reached", 0, "delivered"),
        ("Provider: rate limit exceeded", 1, "process_error"),
    ],
    ids=["claude", "codex", "provider", "chunk-boundary", "success", "other-error"],
)
def test_raw_usage_limit_classification_and_resume(
    tmp_path: Path, stream: str, message: str, exit_code: int, expected: str
) -> None:
    script = tmp_path / "worker.py"
    script.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "with Path('launches').open('a') as log: log.write('launch\\n')\n"
        "(Path(sys.argv[3]) / 'result.txt').write_text('candidate')\n"
        f"sys.{stream}.buffer.write(b'\\xff' + {message.encode()!r})\n"
        f"sys.exit({exit_code})\n"
    )
    request = _request(_worker(script), max_launches=2, retry=RetryPolicy(on=("process_error",)))
    records = tmp_path / "records"
    with Run(records, tmp_path) as run:
        result = run.call(request)
    count = 2 if expected == "process_error" else 1
    assert result.status == expected
    assert len(result.launches) == count
    assert result.outputs == ({"result.txt": "candidate"} if expected == "delivered" else {})
    for attempt in result.launches:
        assert attempt.returncode == exit_code
        assert (Path(attempt.launch_dir) / f"{stream}.log").read_bytes() == (
            b"\xff" + message.encode()
        )
    with Run(records, tmp_path) as run:
        resumed = run.call(request)
    assert resumed.status == expected
    assert len(resumed.launches) == count
    assert (tmp_path / "launches").read_text().splitlines() == ["launch"] * count


@pytest.mark.parametrize("message", ["", "Usage limit reached"])
def test_timeout_is_not_classified_as_usage_limit(tmp_path: Path, message: str) -> None:
    script = tmp_path / "worker.py"
    script.write_text(f"import time\nprint({message!r}, flush=True)\ntime.sleep(30)\n")
    with Run(tmp_path / "records", tmp_path) as run:
        result = run.call(_request(_worker(script), timeout=0.5))
    assert result.status == "timeout"
    assert len(result.launches) == 1
    assert result.launches[0].returncode != 0
    assert (Path(result.launches[0].launch_dir) / "stdout.log").read_text() == message + "\n"


@pytest.mark.parametrize("leader_exits", [False, True])
def test_process_group_children_are_stopped(tmp_path: Path, leader_exits: bool) -> None:
    script = tmp_path / "worker.py"
    script.write_text(
        """\
import os
import subprocess
import sys
import time
from pathlib import Path

_, inputs, outputs, problem, marker, mode = sys.argv[1:7]
subprocess.Popen([sys.executable, '-c',
    'import os,signal,sys,time; from pathlib import Path; '
    'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
    'Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)',
    marker])
deadline = time.monotonic() + 5
while not Path(marker).exists():
    if time.monotonic() > deadline: raise RuntimeError('child did not start')
    time.sleep(0.01)
(Path(outputs) / 'result.txt').write_text('premature')
if mode == 'wait':
    time.sleep(10)
""",
        encoding="utf-8",
    )
    marker = tmp_path / "child.pid"
    worker = _worker(script, str(marker), "exit" if leader_exits else "wait")
    with Run(tmp_path / "records", tmp_path) as run:
        result = run.call(_request(worker, timeout=2))
    assert result.status == ("process_error" if leader_exits else "timeout")
    assert result.outputs == {}
    child_pid = int(marker.read_text())
    try:
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            try:
                state = Path(f"/proc/{child_pid}/stat").read_text().split(") ", 1)[1]
            except FileNotFoundError:
                break
            if state.startswith("Z"):
                break
            time.sleep(0.01)
        else:
            pytest.fail("a child survived the leader's completion/timeout")
    finally:
        try:
            os.kill(child_pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


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


def test_cleanup_error_leaves_reservation_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records = tmp_path / "records"
    script = tmp_path / "sleep.py"
    script.write_text("import time\ntime.sleep(5)\n", encoding="utf-8")
    call = _request(_worker(script), timeout=0.05)

    def fail_cleanup(_: subprocess.Popen[bytes]) -> bool:
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
                    try:
                        os.killpg(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass

    with Run(records, tmp_path) as run:
        unresolved = run.unresolved_launches()
        assert len(unresolved) == 1
        assert unresolved[0].status == "running"
        run.acknowledge_stopped(unresolved[0].launch_id)


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
        "    if any(e['result'] is not None for v in state['calls'].values() "
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


@pytest.mark.parametrize("value", [0, -1, True, float("nan"), float("inf")])
def test_invalid_timeout_rejected(value: object) -> None:
    with pytest.raises(ValueError):
        _request(CommandWorker(("echo",)), timeout=value)


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
def test_invalid_launch_limit_rejected(value: object) -> None:
    with pytest.raises(ValueError):
        _request(CommandWorker(("echo",)), max_launches=value)
