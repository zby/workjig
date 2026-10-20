"""Manual smoke-command wiring, using fake CLIs instead of real harnesses."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.subprocess
SMOKE = Path(__file__).resolve().parents[1] / "examples" / "smoke_harness.py"


@pytest.mark.parametrize("failure", [None, "process", "content"])
def test_all_smoke_defaults_isolation_and_failure_settlement(tmp_path, failure):
    binaries = tmp_path / "bin"
    binaries.mkdir()
    for harness in ("codex", "claude", "pi"):
        executable = binaries / harness
        executable.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            "from pathlib import Path\n"
            f"harness = {harness!r}\n"
            "flag = '-m' if harness == 'codex' else '--model'\n"
            "model = sys.argv[sys.argv.index(flag) + 1]\n"
            "assert model == ('haiku' if harness == 'claude' else 'luna')\n"
            "with Path('launches').open('a') as log: log.write(model + '\\n')\n"
            f"failure = {failure!r} if harness == 'codex' else None\n"
            "if failure == 'process': sys.exit(7)\n"
            "prompt = Path(sys.argv[-1].split('`')[1]).read_text()\n"
            "paths = [Path(line.split(': ', 1)[1]) for line in prompt.splitlines()\n"
            "    if line.startswith('- ') and ': /' in line]\n"
            "source = next(path for path in paths if path.name == 'source.txt')\n"
            "output = next(path for path in paths if path.name == 'answer.txt')\n"
            "output.write_bytes(b'wrong' if failure == 'content' else source.read_bytes())\n"
            "events = {\n"
            "    'codex': [{'type': 'turn.completed'}],\n"
            "    'claude': [{'type': 'result', 'subtype': 'success', 'is_error': False}],\n"
            "    'pi': [{'type': 'message_end', 'message': {'role': 'assistant',\n"
            "            'stopReason': 'stop'}}, {'type': 'agent_settled', 'aborted': False}],\n"
            "}\n"
            "for event in events[harness]: print(json.dumps(event))\n"
        )
        executable.chmod(0o755)
    root = tmp_path / "probes"
    command = [sys.executable, str(SMOKE), "all", str(root)]
    env = {**os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"]}
    completed = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)
    assert completed.returncode == (1 if failure else 0), completed.stderr
    reports = [json.loads(line) for line in completed.stdout.splitlines()]
    assert [report["harness"] for report in reports] == ["codex", "claude", "pi"]
    for report in reports:
        harness = report["harness"]
        model = "haiku" if harness == "claude" else "luna"
        assert report["requested_model"] == model
        assert report["records"] == str(root / harness / "records")
        assert (root / harness / "launches").read_text() == model + "\n"
        if harness == "codex" and failure:
            assert report["status"] == ("process_error" if failure == "process" else "probe_error")
        else:
            assert report["status"] == "delivered"
            assert report["reused"] is True
            assert report["launches"] == 1
    if failure is None:
        repeated = subprocess.run(command, env=env, capture_output=True, text=True, timeout=15)
        assert repeated.returncode == 0, repeated.stderr
        assert repeated.stdout == completed.stdout
        for report in reports:
            assert (root / report["harness"] / "launches").read_text().splitlines() == [
                report["requested_model"]
            ]


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
