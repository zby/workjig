"""Bounded public-API probes of untrusted worker metadata."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from workjig.harness import _read_evidence, _read_problem

fifo_only = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFOs are POSIX-only")


@pytest.mark.unit
@pytest.mark.parametrize("kind", ["[]", "{}"])
def test_bad_problem_kind_is_invalid(tmp_path, kind):
    path = tmp_path / "problem.json"
    path.write_text('{"kind":' + kind + ',"message":"oops"}')
    assert _read_problem(path) == (None, True)


@pytest.mark.unit
@pytest.mark.parametrize(
    "raw", ['{"nested":[NaN]}', '{"x":Infinity}', '{"x":-Infinity}', '{"x":1e400}']
)
def test_nonfinite_evidence_is_discarded(tmp_path, raw):
    path = tmp_path / "evidence.json"
    path.write_text(raw)
    assert _read_evidence(path) is None


@pytest.mark.unit
def test_deep_metadata_does_not_escape_parser(tmp_path):
    path = tmp_path / "deep.json"
    path.write_text("[" * 200_000)
    assert _read_problem(path) == (None, True)
    assert _read_evidence(path) is None


# Exercise collection, precedence, allowance and replay together for distinct
# outcomes, not for every parser input at both ceilings. Keep the enclosing
# controller timeout: accepting a FIFO as a regular file could block forever.
@pytest.mark.subprocess
@pytest.mark.parametrize(
    ("problem", "evidence", "outcome", "expected", "max_launches"),
    [
        ('{"kind":[],"message":"oops"}', "{}", "log_limit", "protocol_error", 1),
        ('{"kind":"usage_limit","message":"quota"}', '{"x":NaN}', "exit", "usage_limit", 2),
        pytest.param(
            '{"kind":"usage_limit","message":"quota"}',
            "deep",
            "timeout",
            "timeout",
            1,
            marks=pytest.mark.slow,
        ),
        ("absent", '{"x":1e400}', "log_limit", "process_error", 2),
        ("absent", "deep", "exit", "delivered", 1),
        pytest.param("fifo", "fifo", "exit", "protocol_error", 2, marks=fifo_only),
        pytest.param("deep", "deep", "timeout", "timeout", 2, marks=pytest.mark.slow),
        pytest.param("absent", "fifo", "exit", "delivered", 2, marks=fifo_only),
        pytest.param("absent", "{}", "log_fifo", "process_error", 1, marks=fifo_only),
    ],
)
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
    elif content == 'deep':
        path.write_text('[' * 200_000)
    elif content != 'absent':
        path.write_text(content)
(outputs / 'answer.txt').write_text('done')
if sys.argv[6] == 'timeout':
    time.sleep(30)
if sys.argv[6] == 'log_limit':
    print('USAGE LIMIT', flush=True)
    sys.exit(7)
if sys.argv[6] == 'log_fifo':
    log = outputs.parent / 'stdout.log'
    log.unlink()
    os.mkfifo(log)
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
     problem, evidence, outcome),
    output_protocol='pi-json' if outcome == 'log_fifo' else None),
    timeout_seconds=1 if outcome == 'timeout' else 5, max_launches=max_launches)
with Run(root / 'records', root) as run:
    result = run.call(request)
assert result.status == expected, result
assert len(result.launches) == 1
if outcome == 'timeout':
    # This file is written after both metadata files, before sleeping. A timeout
    # during Python startup would not actually exercise metadata precedence.
    assert (Path(result.launches[0].launch_dir) / 'outputs/answer.txt').read_text() == 'done'
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
