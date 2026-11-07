"""Manual smoke-command argument validation; no harness is launched."""

import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
SMOKE = Path(__file__).resolve().parents[1] / "examples" / "smoke_harness.py"


def test_all_smoke_rejects_ambiguous_model_override(tmp_path):
    completed = subprocess.run(
        [sys.executable, str(SMOKE), "all", str(tmp_path / "unused"), "--model", "override"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 2
    assert "--model requires a single harness" in completed.stderr
    assert not (tmp_path / "unused").exists()
