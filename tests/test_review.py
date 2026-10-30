"""Parent-review probes for output attribution and the advertised group limit."""

import json
import sys
from pathlib import Path

import pytest

from workjig import CommandWorker, Request, RetryPolicy, Run

pytestmark = pytest.mark.subprocess


def test_successful_retry_without_output_cannot_adopt_failed_output(tmp_path: Path):
    script = tmp_path / "worker.py"
    script.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "output, marker = map(Path, sys.argv[1:])\n"
        "if not marker.exists():\n"
        "    marker.write_text('started')\n"
        "    output.write_text('belongs to failed launch')\n"
        "    sys.exit(7)\n"
        "sys.exit(0)\n"
    )
    request = Request(
        "retry",
        "Write the output.",
        {},
        ("answer.txt",),
        CommandWorker(
            (
                sys.executable,
                str(script),
                "{output_dir}/answer.txt",
                str(tmp_path / "marker"),
            )
        ),
        max_launches=2,
        retry=RetryPolicy(on=("process_error",)),
    )
    with Run(tmp_path / "records", tmp_path) as run:
        result = run.call(request)
    assert result.status == "incomplete"
    assert not result.outputs
    assert [launch.status for launch in result.launches] == ["process_error", "incomplete"]
    assert result.failed_at_launch_limit


def test_parallel_limit_bounds_observed_live_worker_count(tmp_path: Path):
    script = tmp_path / "worker.py"
    script.write_text(
        "import json, sys, time\n"
        "from pathlib import Path\n"
        "output, state, lock = map(Path, sys.argv[1:])\n"
        "def change(delta):\n"
        "    while True:\n"
        "        try:\n"
        "            lock.mkdir()\n"
        "            break\n"
        "        except FileExistsError:\n"
        "            time.sleep(0.001)\n"
        "    value = json.loads(state.read_text()) if state.exists() else {'live':0,'peak':0}\n"
        "    value['live'] += delta\n"
        "    value['peak'] = max(value['peak'], value['live'])\n"
        "    state.write_text(json.dumps(value))\n"
        "    lock.rmdir()\n"
        "change(1)\n"
        "time.sleep(0.15)\n"
        "output.write_text('done')\n"
        "change(-1)\n"
    )
    worker = CommandWorker(
        (
            sys.executable,
            str(script),
            "{output_dir}/answer.txt",
            str(tmp_path / "counts.json"),
            str(tmp_path / "counts.lock"),
        )
    )
    requests = [
        Request(str(index), "Write output.", {}, ("answer.txt",), worker) for index in range(5)
    ]
    with Run(tmp_path / "records", tmp_path) as run:
        results = run.parallel(requests, max_workers=2)
    assert all(result.status == "delivered" for result in results)
    counts = json.loads((tmp_path / "counts.json").read_text())
    assert counts["live"] == 0
    assert 1 <= counts["peak"] <= 2
