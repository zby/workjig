"""Codex metadata protocol and ownership; no native install or personal sessions."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from workjig import CommandWorker, Request, Run, _codex_session, _session_receipt, read_run

SID = "01901234-abcd-7000-8000-123456789abc"
EVENT = json.dumps({"type": "thread.started", "thread_id": SID}).encode()


@pytest.mark.unit
def test_only_complete_top_level_canonical_id():
    events = _codex_session._Events()
    for invalid in [
        b"not json",
        b"[" * 2000,
        b"\xff",
        b"[" + EVENT + b"]",
        b'{"item":' + EVENT + b"}",
        EVENT.replace(SID.encode(), SID.upper().encode()),
        EVENT.replace(SID.encode(), b"../../target"),
        EVENT.replace(b"thread.started", b"item.started"),
    ]:
        assert events.feed(invalid + b"\n") is None
    assert events.feed(b"x" * _codex_session._CHUNK) is None
    assert events.feed(EVENT + b"\n" + EVENT[:13]) is None
    assert events.feed(EVENT[13:]) is None
    assert events.feed(b"\r\n") == SID


SERVER = """\
import json, os, sys
from pathlib import Path
init = json.loads(input()); assert init['method'] == 'initialize'
print(json.dumps({'id': init['id'], 'result': {'userAgent':'fake'}}), flush=True)
assert json.loads(input())['method'] == 'initialized'
req = json.loads(input()); rid = req['id']; sid = os.environ['EXPECTED_ID']
assert req['method'] == 'thread/read' and req['params'] == {'threadId': sid, 'includeTurns': False}
"""


@pytest.mark.subprocess
@pytest.mark.parametrize(
    "reply",
    [
        "{'error': {'message':'thread not loaded'}}",
        "{'result': {'thread': {'id':'wrong', 'path': str(Path.cwd())}}}",
        "{'result': {'thread': {'id':sid, 'path':'../relative'}}}",
        "{'result': {'thread': {'id':sid, 'path':'/' + chr(0)}}}",
        "{'result': {'thread': None}}",
    ],
)
def test_metadata_lookup_fails_closed(tmp_path, reply):
    (tmp_path / "app-server").write_text(
        SERVER + f"print(json.dumps({{'id':rid, **{reply}}}), flush=True)"
    )
    assert (
        _codex_session.lookup_codex_path(
            SID, (sys.executable,), tmp_path, os.environ | {"EXPECTED_ID": SID}
        )
        is None
    )


@pytest.mark.subprocess
@pytest.mark.slow
def test_slow_receipt_does_not_extend_worker_lifetime(tmp_path, monkeypatch):
    # Synchronize the worker with the receipt writer, not with interpreter startup.
    def slow(*_):
        (tmp_path / "observed").touch()
        time.sleep(1.5)

    monkeypatch.setattr(_session_receipt, "publish", slow)
    script = tmp_path / "exec.py"
    script.write_text(f"""\
import time
from pathlib import Path
print({EVENT.decode()!r}, flush=True)
while not Path('observed').exists(): time.sleep(.01)
time.sleep(1)
Path('late-effect').touch()
""")
    req = Request(
        "timeout",
        "test",
        {},
        ("done",),
        CommandWorker(
            argv=(sys.executable, str(script)),
            track_native_session=True,
            output_protocol="codex-json",
        ),
        timeout_seconds=0.75,
    )
    with Run(tmp_path / "records", tmp_path) as run:
        assert run.call(req).status == "timeout"
    assert (tmp_path / "observed").exists() and not (tmp_path / "late-effect").exists()


@pytest.mark.subprocess
def test_partial_start_event_is_not_an_association(tmp_path):
    req = Request(
        "partial",
        "test",
        {},
        ("done",),
        CommandWorker(
            argv=(sys.executable, "-c", f"import sys; sys.stdout.buffer.write({EVENT!r})"),
            track_native_session=True,
            output_protocol="codex-json",
        ),
    )
    with Run(tmp_path / "run", tmp_path) as run:
        run.call(req)
    view = read_run(tmp_path / "run")
    assert view.native_session(view.calls[0].launches[0]).status == "unassociated"


@pytest.mark.subprocess
@pytest.mark.skipif(os.name == "nt", reason="POSIX supervisor; Windows uses a kill-on-close job")
@pytest.mark.slow
def test_controller_death_does_not_orphan_metadata_helper(tmp_path):
    # Both leader and child ignore a closed stdin; the child even ignores SIGTERM.
    (tmp_path / "app-server").write_text("""\
import os, signal, subprocess, sys, time
from pathlib import Path
code = ("import signal,time; from pathlib import Path;"
        "signal.signal(signal.SIGTERM,signal.SIG_IGN);"
        "Path('ready').touch(); time.sleep(2.5); Path('escaped').touch()")
subprocess.Popen([sys.executable, '-c', code]); time.sleep(4)
""")
    probe = f"""from pathlib import Path
import os, sys
from workjig import _codex_session as b
b._LOOKUP_SECONDS = .75
b.lookup_codex_path({SID!r}, (sys.executable,), Path.cwd(), os.environ)
"""
    source = Path(__file__).resolve().parents[1] / "src"
    proc = subprocess.Popen(
        [sys.executable, "-c", probe], cwd=tmp_path, env=os.environ | {"PYTHONPATH": str(source)}
    )
    try:
        end = time.monotonic() + 5
        while not (tmp_path / "ready").exists() and time.monotonic() < end:
            time.sleep(0.02)
        assert (tmp_path / "ready").exists() and proc.poll() is None
        proc.kill()
        proc.wait(timeout=2)
        time.sleep(2.7)
        assert not (tmp_path / "escaped").exists()
    finally:
        proc.kill()
        proc.wait(timeout=2)
