"""Credential-free smoke coverage for README snippets and example entry points."""

import json
import re
import runpy
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from workjig import Run, adapters

ROOT = Path(__file__).resolve().parents[1]
EXAMPLES = ROOT / "examples"

# A deliberately small executable substitute: read the adapter's actual prompt
# pointer and copy declared source content to required outputs. This is not a
# harness emulator or an alternate runner; real Run owns every worker process.
COPY_WORKER = r"""
import json
import re
import sys
from pathlib import Path

harness = sys.argv[1]
args = sys.argv[2:]
if harness == "codex":
    assert args[0] == "exec" and "--json" in args
    assert Path(args[args.index("-C") + 1]) == Path.cwd()
elif harness == "claude":
    assert "-p" in args and args[args.index("--output-format") + 1] == "stream-json"
else:
    assert harness == "pi" and args[args.index("--mode") + 1] == "json"
assert args[-1].startswith("Read the complete task and file paths in `")
pointer = args[-1]
prompt = Path(re.search(r"`([^`]+)`", pointer).group(1)).read_text(encoding="utf-8")
source = re.search(r"^- source.txt: (.+)$", prompt, re.M).group(1)
required = prompt.split("Required outputs (write these exact files):\n", 1)[1]
for line in required.splitlines():
    if not line.startswith("- "):
        break
    _, path = line[2:].split(": ", 1)
    Path(path).write_text(Path(source).read_text(encoding="utf-8"), encoding="utf-8")
if harness == "codex":
    events = [{"type": "turn.completed"}]
elif harness == "claude":
    events = [{"type": "result", "subtype": "success", "is_error": False}]
else:
    events = [
        {"type": "message_end", "message": {"role": "assistant", "stopReason": "stop"}},
        {"type": "agent_settled", "aborted": False},
    ]
for event in events:
    print(json.dumps(event))
"""


@pytest.fixture
def substitute_harnesses(tmp_path, monkeypatch):
    script = tmp_path / "copy_worker.py"
    script.write_text(COPY_WORKER, encoding="utf-8")
    constructed = []

    def substitute(harness, factory):
        def build(*args, **kwargs):
            worker = factory(*args, **kwargs)
            worker = replace(
                worker,
                argv=(sys.executable, str(script), harness, *worker.argv[1:]),
            )
            constructed.append(worker)
            return worker

        return build

    for harness in ("claude", "codex", "pi"):
        name = f"{harness}_worker"
        monkeypatch.setattr(adapters, name, substitute(harness, getattr(adapters, name)))
    return constructed


@pytest.mark.unit
@pytest.mark.parametrize("script", sorted(EXAMPLES.glob("*.py")), ids=lambda path: path.name)
def test_example_help(script, tmp_path):
    completed = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr
    assert "usage:" in completed.stdout
    assert list(tmp_path.iterdir()) == []


@pytest.mark.subprocess
def test_readme_python_examples(tmp_path, monkeypatch, capsys, substitute_harnesses):
    monkeypatch.chdir(tmp_path)
    blocks = re.findall(r"```python\n(.*?)```", (ROOT / "README.md").read_text(), re.S)
    assert len(blocks) == 2, "Update smoke coverage when README Python examples change"
    namespace = {}
    exec(compile(blocks[0], "README.md:main-example", "exec"), namespace)
    assert namespace["result"].status == "delivered"
    assert capsys.readouterr().out == "Material to summarize.\n"

    exec(compile(blocks[1], "README.md:alternatives", "exec"), namespace)
    assert [worker.name for worker in substitute_harnesses] == ["claude", "codex", "pi"]
    # Both alternatives must execute, not just construct successfully. The
    # allowlists, prompt pointers and protocols remain those of the real factories.
    for worker in substitute_harnesses[1:]:
        request = replace(namespace["request"], worker=worker)
        with Run(tmp_path / f"records-{worker.name}", tmp_path) as run:
            result = run.call(request)
        assert result.status == "delivered"
        assert result.outputs == {"summary.txt": "Material to summarize."}


@pytest.mark.subprocess
@pytest.mark.parametrize("harness", ["all", "claude", "codex", "pi"])
def test_smoke_probe_example_with_substitutes(
    tmp_path, monkeypatch, capsys, substitute_harnesses, harness
):
    root = tmp_path / "probe"
    argv = ["smoke_harness.py", harness, str(root)]
    if harness != "all":
        argv.extend(["--model", "fixture-model"])
    monkeypatch.setattr(sys, "argv", argv)
    runpy.run_path(str(EXAMPLES / "smoke_harness.py"), run_name="__main__")
    reports = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    expected = ["codex", "claude", "pi"] if harness == "all" else [harness]
    assert [report["harness"] for report in reports] == expected
    assert len(substitute_harnesses) == len(expected)
    for report in reports:
        assert report["status"] == "delivered"
        if harness != "all":
            assert report["requested_model"] == "fixture-model"
        assert report["launches"] == 1
        assert report["reused"] is True
        assert report["evidence"]["parse_errors"] == 0
        workdir = root / report["harness"] if harness == "all" else root
        assert report["records"] == str(workdir / "records")
        assert (workdir / "records" / "run.json").is_file()


@pytest.mark.unit
def test_readme_substitution_example(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    blocks = re.findall(r"```python\n(.*?)```", (EXAMPLES / "README.md").read_text(), re.S)
    assert len(blocks) == 1, "Update smoke coverage when example snippets change"
    namespace = {}
    exec(compile(blocks[0], "examples/README.md:substitution", "exec"), namespace)
    real, fake = namespace["real"], namespace["fake"]
    assert fake.argv[:2] == (sys.executable, str(tmp_path / "fake_codex.py"))
    assert fake.argv[2:] == real.argv[1:]
    assert fake.output_protocol == real.output_protocol
    assert fake.env == real.env
