"""Command rendering, worker files, opaque logs and request validation."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from worker_cases import _request, _worker

from workjig import (
    CommandWorker,
    Request,
    RetryPolicy,
    Run,
)

posix_only = pytest.mark.skipif(os.name == "nt", reason="POSIX executable script")


@pytest.mark.subprocess
def test_protocol_names_are_safe_inside_input_and_output_directories(tmp_path: Path) -> None:
    names = (
        "prompt.txt",
        "problem.json",
        "evidence.json",
        "stdout.log",
        "stderr.log",
        "inputs",
        "outputs",
    )
    worker = CommandWorker(
        (
            sys.executable,
            "-c",
            "import pathlib, shutil, sys; "
            "shutil.copytree(sys.argv[1], sys.argv[2], dirs_exist_ok=True)",
            "{input_dir}",
            "{output_dir}",
        )
    )
    request = Request("names", "Copy inputs.", dict.fromkeys(names, "payload"), names, worker)
    with Run(tmp_path / "records", tmp_path) as run:
        result = run.call(request)
    assert result.status == "delivered"
    assert result.outputs == dict(request.inputs)
    with Run(tmp_path / "records", tmp_path) as run:
        assert run.call(request).reused


@pytest.mark.unit
def test_worker_argv_preserves_literal_braces(tmp_path: Path) -> None:
    worker = CommandWorker((sys.executable, "-c", 'print({"x": 1})', "{prompt}"))
    prompt = tmp_path / "{input_dir}" / "prompt.txt"
    command = worker.command({"prompt": prompt, "input_dir": tmp_path / "other"})
    assert command == (sys.executable, "-c", 'print({"x": 1})', str(prompt))


@pytest.mark.parametrize("exit_code, expected", [(0, "delivered"), (7, "process_error")])
@pytest.mark.subprocess
def test_generic_log_text_does_not_classify_usage_limits(
    tmp_path: Path, exit_code: int, expected: str
) -> None:
    # Generic streams are opaque bytes. Exercise both channels and all the
    # old phrase probes (including the chunk boundary) in the same launch.
    payload = (
        b"\xff" + b"x" * 65532 + b"Usage limit reached\nYou've hit your usage limit\n"
        b"Provider: USAGE LIMIT exceeded\nProvider: rate limit exceeded"
    )
    script = tmp_path / "worker.py"
    script.write_text(
        "import sys\n"
        "from pathlib import Path\n"
        "with Path('launches').open('a') as log: log.write('launch\\n')\n"
        "(Path(sys.argv[3]) / 'result.txt').write_text('candidate')\n"
        f"payload = {payload!r}\n"
        "sys.stdout.buffer.write(payload + b'out')\n"
        "sys.stderr.buffer.write(payload + b'err')\n"
        f"sys.exit({exit_code})\n"
    )
    request = _request(_worker(script), max_launches=2, retry=RetryPolicy(on=("process_error",)))
    records = tmp_path / "records"
    with Run(records, tmp_path) as run:
        result = run.call(request)
    count = 2 if expected == "process_error" else 1
    assert result.status == expected
    assert len(result.launches) == count
    assert result.outputs == ({"result.txt": "candidate"} if expected == "delivered" else {})
    for attempt in result.launches:
        assert attempt.returncode == exit_code
        directory = Path(attempt.launch_dir)
        assert (directory / "stdout.log").read_bytes() == payload + b"out"
        assert (directory / "stderr.log").read_bytes() == payload + b"err"
    with Run(records, tmp_path) as run:
        resumed = run.call(request)
    assert resumed.status == expected
    assert len(resumed.launches) == count
    assert (tmp_path / "launches").read_text().splitlines() == ["launch"] * count


@pytest.mark.subprocess
def test_timeout_is_not_classified_as_usage_limit(tmp_path: Path) -> None:
    message = "Usage limit reached"
    script = tmp_path / "worker.py"
    script.write_text(f"import time\nprint({message!r}, flush=True)\ntime.sleep(30)\n")
    with Run(tmp_path / "records", tmp_path) as run:
        result = run.call(_request(_worker(script), timeout=0.5))
    assert result.status == "timeout"
    assert len(result.launches) == 1
    assert result.launches[0].returncode != 0
    assert (Path(result.launches[0].launch_dir) / "stdout.log").read_text() == message + "\n"


@pytest.mark.parametrize("value", [0, -1, True, float("nan"), float("inf")])
@pytest.mark.unit
def test_invalid_timeout_rejected(value: object) -> None:
    with pytest.raises(ValueError):
        _request(CommandWorker(("echo",)), timeout=value)


@pytest.mark.parametrize("value", [0, -1, True, 1.5])
@pytest.mark.unit
def test_invalid_launch_limit_rejected(value: object) -> None:
    with pytest.raises(ValueError):
        _request(CommandWorker(("echo",)), max_launches=value)


@posix_only
@pytest.mark.subprocess
def test_worker_path_selects_the_executable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for owner in ("controller", "worker"):
        tool = tmp_path / owner / "tool"
        tool.parent.mkdir()
        tool.write_text(
            f"#!{sys.executable}\n"
            "import sys\nfrom pathlib import Path\n"
            f"Path(sys.argv[1], 'answer.txt').write_text({owner!r})\n"
        )
        tool.chmod(0o755)
    monkeypatch.setenv("PATH", f"{tmp_path / 'controller'}{os.pathsep}{os.environ['PATH']}")
    worker = CommandWorker(("tool", "{output_dir}"), env={"PATH": str(tmp_path / "worker")})
    with Run(tmp_path / "records", tmp_path) as run:
        result = run.call(Request("job", "Write answer", {}, ("answer.txt",), worker))

    assert result.status == "delivered"
    assert dict(result.outputs) == {"answer.txt": "worker"}
