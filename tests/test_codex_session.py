"""Codex session association against local exec and app-server stand-ins."""

import json
import os
import sys
import time
from contextlib import contextmanager

import pytest

from workjig import _codex_session as backend
from workjig.platform import start_process

SID = "01901234-abcd-7000-8000-123456789abc"
EVENT = json.dumps({"type": "thread.started", "thread_id": SID}).encode()
# sys.executable is the launch executable. "app-server" is a Python file in the
# worker cwd, which exercises the fixed helper argv on both Linux and Windows.
SERVER = """
import json, os, sys
from pathlib import Path

def recv():
    return json.loads(sys.stdin.buffer.readline())

def emit(msg):
    print(json.dumps(msg), flush=True)

assert sys.argv == ['app-server']
init = recv()
assert init['method'] == 'initialize' and init['params']['clientInfo']['name'] == 'workjig'
emit({'id': init['id'], 'result': {'userAgent': 'fake'}})
assert recv()['method'] == 'initialized'
req = recv()
sid = os.environ['EXPECTED_ID']
assert req['method'] == 'thread/read' and req['params'] == {'threadId': sid, 'includeTurns': False}
rid = req['id']
emit({'method': 'notification'})
"""


@pytest.mark.unit
def test_only_first_complete_top_level_canonical_id():
    events = backend._Events()
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
    assert events.feed(b"x" * backend._CHUNK) is None
    assert events.feed(EVENT + b"\n" + EVENT[:13]) is None
    assert events.feed(EVENT[13:]) is None
    assert events.feed(b"\r\n" + EVENT.replace(b"abc", b"def") + b"\n") == SID


def server(cwd, body):
    (cwd / "app-server").write_text(SERVER + body, encoding="utf-8")


@contextmanager
def worker(cwd, code):
    env = os.environ | {"EXPECTED_ID": SID}
    argv = (sys.executable, "-c", code)
    stdout = cwd / "stdout"
    with stdout.open("wb") as out, (cwd / "stderr").open("wb") as err:
        process = start_process(argv, cwd, env, out, err)
        try:
            yield process, stdout, argv, env
        finally:
            try:
                process.cleanup()
            finally:
                process.close()


@pytest.mark.subprocess
def test_live_id_and_exact_path_on_failed_exit(tmp_path):
    path = tmp_path / "untouched.jsonl"
    server(
        tmp_path,
        f"emit({{'id': rid, 'result': {{'thread': {{'id': sid, 'path': {str(path)!r}}}}}}})",
    )
    raw = b"\xff\n" + EVENT + b"\n"
    code = f"""
import sys, time
from pathlib import Path
sys.stdout.buffer.write({raw!r})
sys.stdout.buffer.flush()
while not Path('published').exists(): time.sleep(.01)
sys.stdout.buffer.write(b'tail\\x00')
sys.exit(7)
"""
    seen = []
    with worker(tmp_path, code) as (proc, stdout, argv, env):

        def publish(sid, location):
            seen.append((sid, location))
            if location is None:
                assert not proc.wait(0)
                (tmp_path / "published").touch()

        assert backend.wait_for_codex(proc, 5, stdout, publish, argv, tmp_path, env)
        assert proc.returncode == 7
    assert seen == [(SID, None), (SID, path)]
    assert stdout.read_bytes() == raw + b"tail\x00"
    assert not path.exists()


@pytest.mark.subprocess
@pytest.mark.parametrize(
    "response",
    [
        "{'error': {'message': 'thread not loaded'}}",
        "{'result': {'thread': {'id': 'wrong', 'path': str(Path.cwd())}}}",
        "{'result': {'thread': {'id': sid, 'path': '../relative'}}}",
        "{'result': {'thread': {'id': sid, 'path': '/' + chr(0)}}}",
        "{'result': {'thread': None}}",
    ],
)
def test_fast_exit_advisory_metadata(tmp_path, response):
    server(tmp_path, f"emit({{'id': rid, **{response}}})")
    with worker(tmp_path, f"print({EVENT.decode()!r})") as (proc, stdout, argv, env):
        assert proc.wait(5)
        seen = []
        assert backend.wait_for_codex(
            proc, 5, stdout, lambda *x: seen.append(x), argv, tmp_path, env
        )
    assert seen == [(SID, None)]


@pytest.mark.subprocess
def test_timeout_with_unterminated_event(tmp_path):
    with worker(
        tmp_path, f"import time; print({EVENT.decode()!r}, end='', flush=True); time.sleep(30)"
    ) as (proc, stdout, argv, env):
        assert not backend.wait_for_codex(
            proc, 0.25, stdout, lambda *_: pytest.fail(), argv, tmp_path, env
        )
        assert proc.returncode is None


@pytest.mark.subprocess
def test_stalled_helper_descendant_is_stopped(tmp_path, monkeypatch):
    server(
        tmp_path,
        """
import subprocess
child = ("from pathlib import Path; import time; Path('ready').touch();"
         "time.sleep(1.5); Path('escaped').touch()")
subprocess.Popen([sys.executable, '-c', child])
""",
    )
    monkeypatch.setattr(backend, "_LOOKUP_SECONDS", 1)
    env = os.environ | {"EXPECTED_ID": SID}
    before = time.monotonic()
    assert backend.lookup_codex_path(SID, (sys.executable, "ignored"), tmp_path, env) is None
    assert (tmp_path / "ready").exists()
    assert time.monotonic() - before < 3
    time.sleep(1.6)
    assert not (tmp_path / "escaped").exists()


@pytest.mark.subprocess
def test_timeout_retains_known_id_and_does_not_wait_for_helper(tmp_path):
    server(tmp_path, "import time; time.sleep(30)")
    with worker(
        tmp_path, f"import time; print({EVENT.decode()!r}, flush=True); time.sleep(30)"
    ) as (proc, stdout, argv, env):
        seen = []
        started = time.monotonic()
        assert not backend.wait_for_codex(
            proc, 0.5, stdout, lambda *p: seen.append(p), argv, tmp_path, env
        )
        assert time.monotonic() - started < 1.5
        assert proc.returncode is None
        assert seen == [(SID, None)]


@pytest.mark.subprocess
def test_worker_descendant_is_left_for_caller_cleanup(tmp_path):
    server(tmp_path, "Path('looked-up').touch()")
    code = f"""
import subprocess, sys
subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
print({EVENT.decode()!r})
"""
    with worker(tmp_path, code) as (proc, stdout, argv, env):
        seen = []
        assert backend.wait_for_codex(
            proc, 5, stdout, lambda *p: seen.append(p), argv, tmp_path, env
        )
        assert seen == [(SID, None)]
        assert proc.cleanup()
        assert not (tmp_path / "looked-up").exists()


@pytest.mark.subprocess
def test_slow_publication_cannot_turn_timeout_into_exit(tmp_path):
    code = f"""
import time
from pathlib import Path
print({EVENT.decode()!r}, flush=True)
while not Path('finish').exists(): time.sleep(.01)
"""
    with worker(tmp_path, code) as (proc, stdout, argv, env):

        def persist(sid, location):
            assert (sid, location) == (SID, None)
            time.sleep(0.6)
            (tmp_path / "finish").touch()
            # Simulate an fsync which returns after the late worker has exited.
            time.sleep(0.2)

        assert not backend.wait_for_codex(proc, 0.4, stdout, persist, argv, tmp_path, env)
        assert proc.wait(2)


@pytest.mark.subprocess
def test_oversized_helper_reply_keeps_id(tmp_path):
    server(tmp_path, "print('x' * 300000); print(json.dumps({'id': rid, 'result': {}}))")
    with worker(tmp_path, f"print({EVENT.decode()!r})") as (proc, stdout, argv, env):
        seen = []
        assert backend.wait_for_codex(
            proc, 5, stdout, lambda *p: seen.append(p), argv, tmp_path, env
        )
        assert seen == [(SID, None)]


@pytest.mark.subprocess
def test_receipt_storage_failure_preserves_worker_result(tmp_path):
    server(
        tmp_path, "emit({'id': rid, 'result': {'thread': {'id': sid, 'path': str(Path.cwd())}}})"
    )
    seen = []

    def unavailable(sid, path):
        seen.append((sid, path))
        raise OSError("receipt unavailable")

    with worker(tmp_path, f"import sys; print({EVENT.decode()!r}); sys.exit(7)") as (
        proc,
        stdout,
        argv,
        env,
    ):
        assert backend.wait_for_codex(proc, 5, stdout, unavailable, argv, tmp_path, env)
        assert proc.returncode == 7
    assert seen == [(SID, None), (SID, tmp_path)]
