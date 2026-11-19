"""Protect the live-suite opt-in boundary without invoking a harness."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def collect(*args):
    env = os.environ.copy()
    env.pop("PYTEST_ADDOPTS", None)
    return subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_default_collection_excludes_live_tests():
    completed = collect()
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "tests/examples/" in completed.stdout
    assert "tests/live/" not in completed.stdout


def test_explicit_live_file_requires_optin():
    completed = collect("tests/live/test_readme.py")
    assert completed.returncode != 0
    assert "require explicit --live-smoke" in completed.stdout + completed.stderr


def test_live_optin_collects_without_credentials():
    completed = collect("tests/live", "--live-smoke")
    assert completed.returncode == 0, completed.stdout + completed.stderr
    nodes = [line for line in completed.stdout.splitlines() if line.startswith("tests/live/")]
    assert len(nodes) == 8
    assert all(
        any(f"[{harness}]" in node for harness in ("claude", "codex", "pi", "agy"))
        for node in nodes
    )
