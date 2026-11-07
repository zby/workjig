"""Exercise the documented simulator through its executable example."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.subprocess

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "simulated_workflow.py"


def invoke(records: Path, workdir: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(EXAMPLE), str(records), str(workdir), *extra],
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )


def test_simulator_outputs_effects_logs_and_replay(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    records = tmp_path / "records"
    first = invoke(records, workdir)
    assert first.returncode == 0, first.stderr
    report = json.loads(first.stdout)
    assert report["status"] == "delivered"
    assert report["accepted"] is True
    assert report["reused"] is False
    launch = Path(report["launch_dir"])
    assert (launch / "outputs" / "result.txt").read_text() == "example source\n"
    assert (workdir / "edited.txt").read_text() == "EXAMPLE SOURCE\n"
    prompt = (launch / "prompt.txt").read_text()
    assert str(launch / "inputs" / "source.txt") in prompt
    event = json.loads((launch / "stdout.log").read_text())
    assert event == {
        "type": "simulation",
        "outcome": "deliver",
        "prompt": str(launch / "prompt.txt"),
    }
    assert (launch / "stderr.log").read_text() == "simulated diagnostic\n"
    session = launch / "synthetic-session.jsonl"
    assert json.loads(session.read_text()) == event
    assert json.loads((launch / "evidence.json").read_text()) == {
        "simulated": True,
        "session_file": str(session),
    }

    # Replay delivers recorded content; it must not repeat or repair side effects.
    (workdir / "edited.txt").write_text("caller changed this")
    second = invoke(records, workdir)
    assert second.returncode == 0, second.stderr
    replay = json.loads(second.stdout)
    assert replay == {**report, "reused": True}
    assert (workdir / "edited.txt").read_text() == "caller changed this"


def test_simulator_problem_uses_normal_classification(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    completed = invoke(tmp_path / "records", workdir, "--outcome", "unable")
    assert completed.returncode == 1, completed.stderr
    report = json.loads(completed.stdout)
    assert report["status"] == "worker_problem"
    assert report["accepted"] is False
    launch = Path(report["launch_dir"])
    assert json.loads((launch / "problem.json").read_text()) == {
        "kind": "unable",
        "message": "Simulated inability to complete",
    }
    assert not (launch / "outputs" / "result.txt").exists()
    assert not (workdir / "edited.txt").exists()
