"""Physical worker lifecycle: group cleanup, termination and timeout precedence."""

from __future__ import annotations

import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest
from worker_cases import _kill, _request, _worker

from workjig import (
    RetryPolicy,
    Run,
)
from workjig.platform import cleanup_process_group

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX signal handling")


def _alive(pid: int) -> bool:
    if os.name == "nt":
        listed = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True, check=False
        )
        return str(pid) in listed.stdout.split()
    try:
        state = Path(f"/proc/{pid}/stat").read_text().split(") ", 1)[1]
    except FileNotFoundError:
        return False
    return not state.startswith("Z")


@pytest.mark.parametrize("ignore_term", [False, True])
@pytest.mark.subprocess
@posix_only
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

    monkeypatch.setattr("workjig.platform.os.killpg", observed_killpg)
    try:
        assert select.select([process.stdout], [], [], 5)[0], "worker did not become ready"
        assert process.stdout.readline() == b"ready\n"
        assert cleanup_process_group(process)
        process.wait(timeout=5)
        assert process.returncode == (-signal.SIGKILL if ignore_term else 0)
        assert signals == ([signal.SIGTERM, signal.SIGKILL] if ignore_term else [signal.SIGTERM])
    finally:
        if process.poll() is None:
            killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)
        process.stdout.close()


@pytest.mark.parametrize("report", ["none", "raw", "problem"])
@pytest.mark.subprocess
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
    assert result.status == ("usage_limit" if report == "problem" else "process_error")
    assert result.outputs == {}
    assert len(result.launches) == 1
    with Run(records, tmp_path) as run:
        replay = run.call(request)
    assert replay.reused
    assert replay.status == result.status
    assert replay.launches == result.launches


@pytest.mark.subprocess
@posix_only
@pytest.mark.slow
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


# Each value of each dimension appears with each value of the others at least
# once; ownership does not depend on the full product.
@pytest.mark.parametrize(
    ("leader_exits", "grandchild", "retain_logs"),
    [
        pytest.param(False, False, True, marks=pytest.mark.slow),
        pytest.param(False, True, False, marks=pytest.mark.slow),
        (True, False, False),
        (True, True, True),
    ],
)
@pytest.mark.subprocess
def test_process_group_children_are_stopped(
    tmp_path: Path, leader_exits: bool, grandchild: bool, retain_logs: bool
) -> None:
    script = tmp_path / "worker.py"
    script.write_text(
        """\
import os
import subprocess
import sys
import time
from pathlib import Path

_, inputs, outputs, problem, marker, mode, depth = sys.argv[1:8]
sleeper = ('import os,signal,sys,time; from pathlib import Path; '
    'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
    'Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)')
if depth == 'grandchild':
    # The intermediate exits at once, so only ownership can reach the sleeper.
    subprocess.Popen([sys.executable, '-c',
        'import subprocess, sys; subprocess.Popen([sys.executable, "-c", *sys.argv[1:]])',
        sleeper, marker])
else:
    subprocess.Popen([sys.executable, '-c', sleeper, marker])
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
    worker = _worker(
        script,
        str(marker),
        "exit" if leader_exits else "wait",
        "grandchild" if grandchild else "child",
    )
    with Run(tmp_path / "records", tmp_path, retain_logs=retain_logs) as run:
        result = run.call(_request(worker, timeout=2))
    assert result.status == ("process_error" if leader_exits else "timeout")
    assert result.outputs == {}
    child_pid = int(marker.read_text())
    try:
        deadline = time.monotonic() + 5
        while _alive(child_pid):
            if time.monotonic() > deadline:
                pytest.fail("a child survived the leader's completion/timeout")
            time.sleep(0.01)
    finally:
        _kill(child_pid)
