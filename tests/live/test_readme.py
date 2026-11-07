"""Execute the README harness examples without substituting their executables."""

import ast
import re
import shutil
from dataclasses import replace
from pathlib import Path

import pytest

from workjig import Run

pytestmark = [pytest.mark.subprocess, pytest.mark.live]
ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("harness", ["claude", "codex", "pi"])
def test_readme_harness(harness, tmp_path, monkeypatch):
    assert shutil.which(harness), f"Install and authenticate {harness}; its CLI must be on PATH"
    monkeypatch.chdir(tmp_path)
    blocks = re.findall(r"```python\n(.*?)```", (ROOT / "README.md").read_text(), re.S)
    assert len(blocks) == 2, "Update live coverage when README Python examples change"
    namespace = {}
    if harness == "claude":
        # Run the main example exactly as printed.
        exec(compile(blocks[0], "README.md:main-example", "exec"), namespace)
        result = namespace["result"]
        request = namespace["request"]
        records = tmp_path / "run-records"
    else:
        # Construct the same request, then apply the documented alternative
        # before execution. Factory calls configure commands; they launch nothing.
        tree = ast.parse(blocks[0])
        tree.body = [
            node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom, ast.Assign))
        ]
        exec(compile(tree, "README.md:request", "exec"), namespace)
        alternatives = ast.parse(blocks[1])
        alternatives.body = [
            node
            for node in alternatives.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
            or (
                isinstance(node, ast.Assign)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Name)
                and node.value.func.id == f"{harness}_worker"
            )
        ]
        exec(compile(alternatives, "README.md:alternative", "exec"), namespace)
        request = replace(namespace["request"], worker=namespace["worker"])
        records = tmp_path / "run-records"
        with Run(records, tmp_path) as run:
            result = run.call(request)

    assert request.worker.name == harness
    assert request.max_launches == 1
    assert request.timeout_seconds <= 180
    assert result.status == "delivered", result
    assert result.outputs["summary.txt"].strip(), "Harness delivered an empty summary"
    assert len(result.launches) == 1
    assert not result.reused
    launch = result.launches[-1]
    assert (Path(launch.launch_dir) / "outputs" / "summary.txt").read_text(
        encoding="utf-8"
    ) == result.outputs["summary.txt"]
    assert (Path(launch.launch_dir) / "stdout.log").is_file()

    with Run(records, tmp_path) as run:
        replay = run.call(request)
    assert replay.reused
    assert replay.outputs == result.outputs
    assert replay.launches == result.launches
