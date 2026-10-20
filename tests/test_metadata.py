"""Bounded public-API probes of untrusted worker metadata."""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("problem", "evidence", "outcome", "expected"),
    [
        ('{"kind":[],"message":"oops"}', "{}", "exit", "protocol_error"),
        ('{"kind":{},"message":"oops"}', "{}", "exit", "protocol_error"),
        ('{"kind":[],"message":"oops"}', "{}", "timeout", "timeout"),
        ('{"kind":[],"message":"oops"}', "{}", "log_limit", "usage_limit"),
        ('{"kind":"usage_limit","message":"quota"}', '{"x":NaN}', "exit", "usage_limit"),
        ('{"kind":"usage_limit","message":"quota"}', '{"x":Infinity}', "timeout", "timeout"),
        ("absent", '{"nested":[NaN]}', "exit", "delivered"),
        ("absent", '{"x":Infinity}', "exit", "delivered"),
        ("absent", '{"x":-Infinity}', "exit", "delivered"),
        ("absent", '{"x":1e400}', "exit", "delivered"),
        ("absent", '{"x":1e400}', "timeout", "timeout"),
        ("absent", '{"x":1e400}', "log_limit", "usage_limit"),
        ("fifo", "{}", "exit", "protocol_error"),
        ("absent", "fifo", "exit", "delivered"),
        ("fifo", "fifo", "timeout", "timeout"),
        ("fifo", "fifo", "log_limit", "usage_limit"),
    ],
)
@pytest.mark.parametrize("max_launches", [1, 2])
def test_metadata_settles_and_reopens(
    tmp_path: Path, problem: str, evidence: str, outcome: str, expected: str, max_launches: int
) -> None:
    worker = tmp_path / "worker.py"
    worker.write_text(
        """\
import os, sys, time
from pathlib import Path
problem, evidence, outputs = map(Path, sys.argv[1:4])
for path, content in zip((problem, evidence), sys.argv[4:6]):
    if content == 'fifo':
        os.mkfifo(path)
    elif content != 'absent':
        path.write_text(content)
(outputs / 'answer.txt').write_text('done')
if sys.argv[6] == 'timeout':
    time.sleep(30)
if sys.argv[6] == 'log_limit':
    print('USAGE LIMIT', flush=True)
    sys.exit(7)
"""
    )
    controller = tmp_path / "controller.py"
    controller.write_text(
        """\
import sys
from pathlib import Path
from workjig import CommandWorker, Request, Run
root, worker, problem, evidence, outcome, expected, max_launches = sys.argv[1:]
root = Path(root)
max_launches = int(max_launches)
request = Request('metadata', 'Write answer.', {}, ('answer.txt',), CommandWorker(
    (sys.executable, worker, '{problem_file}', '{evidence_file}', '{output_dir}',
     problem, evidence, outcome)), timeout_seconds=0.5, max_launches=max_launches)
with Run(root / 'records', root) as run:
    result = run.call(request)
assert result.status == expected, result
assert len(result.launches) == 1
assert result.launches[0].finished_at is not None
assert result.launches[0].evidence in (None, {}), result
assert result.failed_at_launch_limit == (expected != 'delivered' and max_launches == 1)
assert dict(result.outputs) == ({'answer.txt': 'done'} if expected == 'delivered' else {})
with Run(root / 'records', root) as run:
    again = run.call(request)
assert again.reused
assert again.status == expected
assert again.launches == result.launches
assert dict(again.outputs) == dict(result.outputs)
assert again.failed_at_launch_limit == result.failed_at_launch_limit
"""
    )
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    subprocess.run(
        [
            sys.executable,
            str(controller),
            str(tmp_path),
            str(worker),
            problem,
            evidence,
            outcome,
            expected,
            str(max_launches),
        ],
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=8,
    )
