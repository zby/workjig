"""Codex metadata protocol and ownership; no native install or personal sessions."""

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from workjig import (
    CommandWorker,
    Request,
    Run,
    UnresolvedSessionError,
    _codex_session,
    _session_receipt,
    native_session,
    read_run,
)
from workjig._session_observer import EventObserver
from workjig.adapters import codex_worker

SID = "01901234-abcd-7000-8000-123456789abc"
EVENT = json.dumps({"type": "thread.started", "thread_id": SID}).encode()


@pytest.mark.unit
def test_only_complete_top_level_canonical_id():
    events = EventObserver(native_session._codex_id)
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
    "reply, reason",
    [
        ("{'error': {'message':'thread not loaded'}}", _codex_session.ERROR_REPLY),
        ("{'result': {'thread': {'id':'wrong', 'path': str(Path.cwd())}}}", _codex_session.NO_PATH),
        ("{'result': {'thread': {'id':sid, 'path':'../relative'}}}", _codex_session.NO_PATH),
        ("{'result': {'thread': {'id':sid, 'path':'/' + chr(0)}}}", _codex_session.NO_PATH),
        ("{'result': {'thread': None}}", _codex_session.NO_PATH),
        ("{'result': None}", _codex_session.NO_PATH),
    ],
)
def test_metadata_lookup_fails_closed(tmp_path, reply, reason):
    (tmp_path / "app-server").write_text(
        SERVER + f"print(json.dumps({{'id':rid, **{reply}}}), flush=True)"
    )
    with pytest.raises(ValueError) as failure:
        _codex_session.lookup_codex_path(
            SID, (sys.executable,), tmp_path, os.environ | {"EXPECTED_ID": SID}
        )
    assert str(failure.value) == reason


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


@pytest.mark.subprocess
@pytest.mark.parametrize("failure", ["publish", "thread"])
def test_observer_failure_does_not_change_the_worker_outcome(tmp_path, monkeypatch, failure):
    def broken(*_args, **_kwargs):
        raise RuntimeError("observer failure")

    if failure == "publish":
        monkeypatch.setattr(_session_receipt, "publish", broken)
    else:
        monkeypatch.setattr(native_session.ThreadPoolExecutor, "submit", broken)
    done = json.dumps({"type": "turn.completed"})
    script = tmp_path / "exec.py"
    script.write_text(
        "import sys\nfrom pathlib import Path\n"
        f"print({EVENT.decode()!r}, flush=True)\n"
        "(Path(sys.argv[1]) / 'done').write_text('ok')\n"
        f"print({done!r}, flush=True)\n"
    )
    req = Request(
        "observed",
        "test",
        {},
        ("done",),
        CommandWorker(
            argv=(sys.executable, str(script), "{output_dir}"),
            track_native_session=True,
            output_protocol="codex-json",
        ),
    )
    with Run(tmp_path / "records", tmp_path) as run:
        result = run.call(req)
        assert result.status == "delivered"
        assert run.unresolved_launches() == ()
        assert run.call(req).reused


# -- resolving through the public reader --------------------------------------
# One recorded launch serves every case; each case starts its own fake helper,
# found on the reader's PATH and steered by the reader's environment.

HELPER = """\
import json, os, subprocess, sys, time
from pathlib import Path
out = Path(os.environ['FAKE_DIR'])
seen = {'pid': os.getpid(), 'argv': sys.argv[1:], 'cwd': os.getcwd(),
        'CODEX_HOME': os.environ.get('CODEX_HOME'), 'RECORD_ONLY': os.environ.get('RECORD_ONLY')}
(out / 'helper.json').write_text(json.dumps(seen))
mode = os.environ['FAKE_MODE']
if mode == 'hang':
    child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
    (out / 'child.pid').write_text(str(child.pid))
    time.sleep(30)
if mode == 'exit':
    sys.exit(3)
if mode == 'close':
    os.close(1)
    time.sleep(30)
init = json.loads(input())
print(json.dumps({'id': init['id'], 'result': {}}), flush=True)
input()
req = json.loads(input())
thread = {'id': req['params']['threadId']}
if os.environ['FAKE_PATH']:
    thread['path'] = os.environ['FAKE_PATH']
reply = {'result': {'thread': thread}}
if mode == 'error':
    reply = {'error': {'message': 'no such thread'}}
print(json.dumps({'id': req['id'], **reply}), flush=True)
"""

POSIX_PATH = pytest.mark.skipif(
    os.name == "nt", reason="the fake codex on PATH is a POSIX shell script"
)


@pytest.fixture(scope="module")
def recorded(tmp_path_factory):
    """One delivered Codex launch whose receipt holds only the thread ID."""
    base = tmp_path_factory.mktemp("codex-resolve")
    store = base / "home" / "sessions"
    store.mkdir(parents=True)
    script = base / "exec.py"
    script.write_text(f"""\
import json, sys, time
from pathlib import Path
(Path({str(store)!r}) / {SID + ".jsonl"!r}).touch()
print({EVENT.decode()!r}, flush=True)
while not (Path(sys.argv[1]).parent / 'native-session.json').exists():
    time.sleep(.01)
(Path(sys.argv[1]) / 'done').write_text('ok')
print(json.dumps({{'type': 'turn.completed'}}), flush=True)
""")
    worker = CommandWorker(
        argv=(sys.executable, str(script), "{output_dir}"),
        track_native_session=True,
        output_protocol="codex-json",
        env={"CODEX_HOME": str(store.parent), "RECORD_ONLY": "from-record"},
    )
    with Run(base / "run", base) as run:
        assert run.call(Request("resolve", "test", {}, ("done",), worker)).status == "delivered"
    bin_dir = base / "bin"
    bin_dir.mkdir()
    (base / "app_server.py").write_text(HELPER)
    codex = bin_dir / "codex"
    codex.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{base / "app_server.py"}" "$@"\n')
    codex.chmod(0o755)
    return SimpleNamespace(base=base, store=store, bin=bin_dir, view=read_run(base / "run"))


@pytest.fixture
def resolve(recorded, tmp_path, monkeypatch):
    """Resolve the recorded launch with the fake codex first on the reader's PATH."""
    monkeypatch.setenv("PATH", f"{recorded.bin}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("FAKE_DIR", str(tmp_path))
    monkeypatch.delenv("RECORD_ONLY", raising=False)

    def run(mode, path=None):
        monkeypatch.setenv("FAKE_MODE", mode)
        monkeypatch.setenv("FAKE_PATH", str(path or ""))
        session = recorded.view.native_session(recorded.view.calls[0].launches[0], resolve=True)
        assert session.session_id == SID and session.harness == "codex"
        return session

    return run


def _gone(pid):
    """Has this process stopped and been reaped (allowing init a moment)?"""
    end = time.monotonic() + 2
    while time.monotonic() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)
    return False


@pytest.mark.subprocess
@pytest.mark.slow
@POSIX_PATH
def test_resolve_uses_reader_codex_without_record_environment(recorded, resolve, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(recorded.base / "reader-home"))
    session = resolve("reply", recorded.store / (SID + ".jsonl"))
    assert session.status == "located" and session.reason is None
    assert session.path == recorded.store / (SID + ".jsonl")
    seen = json.loads((Path(os.environ["FAKE_DIR"]) / "helper.json").read_text())
    assert seen["argv"] == ["app-server"]
    assert seen["CODEX_HOME"] == str(recorded.store.parent)
    assert seen["RECORD_ONLY"] is None
    assert not Path(seen["cwd"]).exists()  # A temporary directory, removed afterwards.
    assert _gone(seen["pid"])


@pytest.mark.subprocess
def test_resolve_without_codex_on_path_starts_nothing(recorded, tmp_path, monkeypatch):
    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("started a process"))
    session = recorded.view.native_session(recorded.view.calls[0].launches[0], resolve=True)
    assert session.status == "unavailable" and session.session_id == SID
    assert session.reason == _codex_session.NOT_FOUND


@pytest.mark.subprocess
@POSIX_PATH
def test_resolve_reports_a_helper_that_cannot_start(resolve, monkeypatch):
    def refuse(*args, **kwargs):
        raise PermissionError("not allowed")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    session = resolve("reply")
    assert session.status == "unavailable"
    assert session.reason == _codex_session.NOT_STARTED


@pytest.mark.subprocess
@pytest.mark.slow
@POSIX_PATH
def test_hanging_helper_times_out_and_leaves_no_process(resolve, tmp_path):
    start = time.monotonic()
    session = resolve("hang")
    elapsed = time.monotonic() - start
    assert session.status == "unavailable"
    assert session.reason == _codex_session.NO_ANSWER
    assert _codex_session._LOOKUP_SECONDS <= elapsed < _codex_session._LOOKUP_SECONDS + 1.5
    helper = json.loads((tmp_path / "helper.json").read_text())["pid"]
    child = int((tmp_path / "child.pid").read_text())
    assert _gone(helper) and _gone(child)


@pytest.mark.subprocess
@pytest.mark.slow
@POSIX_PATH
@pytest.mark.parametrize(
    "mode, reason",
    [
        ("exit", _codex_session.EXITED),
        ("close", _codex_session.EXITED),
        ("error", _codex_session.ERROR_REPLY),
        ("reply", _codex_session.NO_PATH),  # The thread is known but has no path.
    ],
)
def test_resolve_reports_each_helper_failure(resolve, tmp_path, mode, reason):
    session = resolve(mode)
    assert session.status == "unavailable" and session.reason == reason
    assert _gone(json.loads((tmp_path / "helper.json").read_text())["pid"])


@pytest.mark.subprocess
@pytest.mark.slow
@POSIX_PATH
@pytest.mark.parametrize("target", ["outside", "other thread", "symlink"])
def test_reply_outside_the_pinned_transcript_is_unavailable(recorded, resolve, tmp_path, target):
    if target == "outside":
        path = tmp_path / (SID + ".jsonl")
        path.touch()
        reason = "transcript outside the pinned native store or redirected"
    elif target == "other thread":
        path = recorded.store / "01901234-abcd-7000-8000-000000000000.jsonl"
        path.touch()
        reason = "unexpected Codex transcript name"
    else:
        path = recorded.store / f"rollout-{tmp_path.name}-{SID}.jsonl"
        path.symlink_to(recorded.store / (SID + ".jsonl"))
        reason = "transcript outside the pinned native store or redirected"
    session = resolve("reply", path)
    assert session.status == "unavailable" and session.path is None
    assert session.reason == reason


@pytest.mark.subprocess
@pytest.mark.slow
@POSIX_PATH
def test_unconfirmed_helper_stop_outranks_the_lookup_failure(resolve, tmp_path, monkeypatch):
    real_killpg = os.killpg
    monkeypatch.setattr(os, "killpg", lambda *args: None)  # The group stop takes no effect.
    try:
        session = resolve("close")
        assert session.status == "unavailable"
        assert re.fullmatch(r"app-server pid \d+ did not stop", session.reason)
    finally:
        group = os.getpgid(json.loads((tmp_path / "helper.json").read_text())["pid"])
        real_killpg(group, signal.SIGKILL)


@pytest.mark.unit
def test_passive_lookup_with_invalid_store_still_raises_unresolved(tmp_path, monkeypatch):
    def started(*args, **kwargs):
        pytest.fail("passive lookup started a process")

    monkeypatch.setattr(subprocess, "Popen", started)
    monkeypatch.setattr(_codex_session, "lookup_codex_path", started)
    launch = tmp_path / "run" / "launches" / "example"
    launch.mkdir(parents=True)
    _session_receipt.publish(launch, "codex", SID, None)
    worker = codex_worker("test", sandbox="read-only", track_native_session=True)
    for root in ("relative/sessions", str(tmp_path / "run" / "sessions")):
        record = SimpleNamespace(launch_dir=str(launch), native_session_root=root)
        with pytest.raises(UnresolvedSessionError) as unresolved:
            native_session.locate_session(worker, record, launch)
        assert unresolved.value.session_id == SID


@pytest.mark.subprocess
def test_reader_imports_do_not_load_the_codex_helper(tmp_path):
    probe = (
        "import sys, workjig, workjig.view, workjig.harness, workjig.adapters\n"
        "print(sorted(m for m in ('workjig._codex_session', 'workjig._windows')"
        " if m in sys.modules))"
    )
    source = Path(__file__).resolve().parents[1] / "src"
    out = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        env=os.environ | {"PYTHONPATH": str(source)},
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout.strip() == "[]"
