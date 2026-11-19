"""Run the real probe CLI, including exact file checks and committed reuse."""

import json
import runpy
import shutil
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.subprocess, pytest.mark.live]
EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "smoke_harness.py"


@pytest.mark.parametrize("harness", ["claude", "codex", "pi", "agy"])
def test_real_probe(harness, tmp_path, monkeypatch, capsys):
    assert shutil.which(harness), f"Install and authenticate {harness}; its CLI must be on PATH"
    monkeypatch.setattr(sys, "argv", [str(EXAMPLE), harness, str(tmp_path)])
    # No outer controller subprocess: normal Run owns and bounds each harness.
    # The command itself verifies exact output contents and repeat-call reuse.
    runpy.run_path(str(EXAMPLE), run_name="__main__")
    report = json.loads(capsys.readouterr().out)
    assert report["harness"] == harness
    assert report["status"] == "delivered"
    assert report["launches"] == 1
    assert report["reused"] is True
    assert report["evidence"]["parse_errors"] == 0
    assert (Path(report["records"]) / "run.json").is_file()
