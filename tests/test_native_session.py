"""Public reader and tracked adapters, using only isolated fake native stores."""

import json
import os
import shutil
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from workjig import (
    CommandWorker,
    Request,
    RetryPolicy,
    Run,
    UnresolvedSessionError,
    _session_receipt,
    read_run,
)
from workjig.adapters import agy_worker, claude_worker, codex_worker, pi_worker

FAKE = """\
import json, os, shlex, subprocess, sys, time
from pathlib import Path
args = sys.argv
sid = args[args.index('--session-id') + 1]
if '--settings' in args:
    settings = json.loads(args[args.index('--settings') + 1])
    command = settings['hooks']['SessionStart'][0]['hooks'][0]['command']
    path = Path(os.environ['STORE']) / (sid + '.jsonl')
    hook = {'hook_event_name': 'SessionStart', 'session_id': sid, 'transcript_path': str(path)}
    subprocess.run(shlex.split(command), input=json.dumps(hook).encode(), check=True)
    terminal = [{'type': 'result', 'is_error': False, 'subtype': 'success'}]
else:
    path = Path(args[args.index('--session-dir') + 1]) / ('now_' + sid + '.jsonl')
    terminal = [{'type': 'message_end', 'message': {'role': 'assistant', 'stopReason': 'stop'}},
                {'type': 'agent_settled', 'aborted': False}]
path.write_bytes(b'unique evidence')
flag = Path('first')
if not flag.exists():
    flag.touch()
    time.sleep(30)
for event in terminal: print(json.dumps(event))
(Path(args[-1]) / 'done').touch()
"""


@pytest.mark.subprocess
@pytest.mark.parametrize("adapter", ["pi", "claude"])
def test_lookup_after_timeout_retry_and_shallow_copy(tmp_path, adapter):
    store = tmp_path / "external sessions" / ("projects" if adapter == "claude" else "sessions")
    store.mkdir(parents=True)
    script = tmp_path / "fake harness.py"
    script.write_text(FAKE)
    base = (
        claude_worker("test", allowed_tools=("Read",), track_native_session=True)
        if adapter == "claude"
        else pi_worker(
            "test",
            provider="test",
            tools=("read",),
            project_trust="no-approve",
            track_native_session=True,
        )
    )
    worker = replace(
        base,
        argv=(sys.executable, str(script), *base.argv[1:-1], "{output_dir}"),
        env={
            "PI_CODING_AGENT_SESSION_DIR": str(store),
            "CLAUDE_CONFIG_DIR": str(store.parent),
            "STORE": str(store),
        },
    )
    req = Request(
        call_id="test",
        task="test",
        inputs={},
        required_outputs=("done",),
        worker=worker,
        max_launches=2,
        retry=RetryPolicy(on=("timeout",)),
        timeout_seconds=1.5,
    )
    with Run(tmp_path / "run", tmp_path) as run:
        result = run.call(req)
    assert [x.status for x in result.launches] == ["timeout", "delivered"]
    archive = tmp_path / "copy"
    shutil.copytree(tmp_path / "run", archive, ignore=shutil.ignore_patterns("*.log"))
    shutil.rmtree(tmp_path / "run")
    view = read_run(archive)
    first, second = view.calls[0].launches
    a, b = view.native_session(first), view.native_session(second)
    assert a.status == b.status == "located" and a.source == b.source == "assigned"
    assert a.session_id != b.session_id and a.path != b.path
    assert a.path.is_relative_to(store) and b.path.is_relative_to(store)
    assert not list(archive.rglob("*.jsonl"))
    a.path.unlink()
    missing = view.native_session(first)
    assert missing.status == "unavailable" and missing.session_id == a.session_id
    assert view.native_session(second, resolve=True) == b
    with pytest.raises(ValueError, match="not in this run"):
        view.native_session(replace(first, launch_id="other"))


@pytest.mark.subprocess
@pytest.mark.parametrize("retain_logs", [True, False])
def test_codex_timeout_and_retry_resolve_distinct_native_ids(tmp_path, retain_logs, monkeypatch):
    from workjig import _codex_session
    from workjig.platform import OwnershipError

    external = tmp_path / "native" / "sessions"
    external.mkdir(parents=True)
    script = tmp_path / "exec.py"
    script.write_text("""\
import json, os, sys, time, uuid
from pathlib import Path
sid = str(uuid.uuid4())
launch = Path(sys.argv[-1]).parent
for name in ('stdout.log', 'stderr.log'):
    assert (launch / name).is_file() == (os.environ['RETAIN_LOGS'] == 'True')
(Path(os.environ['STORE']) / (sid + '.jsonl')).touch()
print(json.dumps({'type': 'thread.started', 'thread_id': sid}), flush=True)
while not (launch / 'native-session.json').exists():
    time.sleep(.01)
print('diagnostic', file=sys.stderr, flush=True)
if not Path('first').exists():
    Path('first').touch()
    time.sleep(30)
(Path(sys.argv[-1]) / 'done').touch()
print(json.dumps({'type': 'turn.completed'}))
""")
    (tmp_path / "app-server").write_text("""\
import json, sys, os
from pathlib import Path
init = json.loads(input())
print(json.dumps({'id': init['id'], 'result': {'userAgent': 'test'}}), flush=True)
assert json.loads(input())['method'] == 'initialized'
req = json.loads(input()); sid = req['params']['threadId']
assert req['method'] == 'thread/read' and not req['params']['includeTurns']
path = Path(os.environ['CODEX_HOME']) / 'sessions' / (sid + '.jsonl')
thread = {'id': sid, 'path': str(path)}
print(json.dumps({'id': req['id'], 'result': {'thread': thread}}), flush=True)
""")
    queries = []

    def reader_command(argv, env):
        assert argv == ("codex", "app-server")
        assert env["CODEX_HOME"] == str(external.parent)
        queries.append(argv)
        return (sys.executable, str(tmp_path / "app-server"))

    monkeypatch.setattr(_codex_session, "resolve_command", reader_command)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "reader-home"))
    worker = replace(
        codex_worker("test", sandbox="read-only", track_native_session=True),
        argv=(sys.executable, str(script), "{output_dir}"),
        env={
            "STORE": str(external),
            "CODEX_HOME": str(external.parent),
            "RETAIN_LOGS": str(retain_logs),
        },
    )
    req = Request(
        call_id="copy",
        task="test",
        inputs={},
        required_outputs=("done",),
        worker=worker,
        max_launches=2,
        retry=RetryPolicy(on=("timeout",)),
        timeout_seconds=1.5,
    )
    with Run(tmp_path / "run", tmp_path, retain_logs=retain_logs) as run:
        result = run.call(req)
    assert [x.status for x in result.launches] == ["timeout", "delivered"]
    archive = tmp_path / "archive"
    shutil.copytree(tmp_path / "run", archive, ignore=shutil.ignore_patterns("*.log"))
    view = read_run(archive)
    first, second = view.calls[0].launches
    with pytest.raises(UnresolvedSessionError, match="resolve=True") as unresolved:
        view.native_session(first)
    assert unresolved.value.harness == "codex" and unresolved.value.session_id
    assert not queries
    a, b = (view.native_session(attempt, resolve=True) for attempt in (first, second))
    assert a.session_id == unresolved.value.session_id
    assert a.status == b.status == "located"
    assert a.session_id != b.session_id and a.source == b.source == "observed"
    assert all(s.path == external / (s.session_id + ".jsonl") for s in (a, b))
    assert len(queries) == 2
    # Resolution writes nothing back: the passive lookup is still unresolved.
    with pytest.raises(UnresolvedSessionError):
        view.native_session(first)
    assert len(queries) == 2

    def uncertain(*args):
        raise OwnershipError("could not confirm app-server cleanup")

    monkeypatch.setattr(_codex_session, "lookup_codex_path", uncertain)
    failure = view.native_session(second, resolve=True)
    assert failure.status == "unavailable" and failure.session_id == b.session_id
    assert failure.reason == "could not confirm app-server cleanup"
    assert read_run(archive).calls[0].status == "delivered"
    receipt = archive / "launches" / result.launches[0].launch_id / "native-session.json"
    data = json.loads(receipt.read_text())
    # Neither a plausible name outside the reserved store, nor another thread's
    # file within that store, is an authorized locator for A.
    other = tmp_path / (a.session_id + ".jsonl")
    other.touch()
    for target in (other, b.path, view.launch_files(view.calls[0].launches[0]).prompt):
        receipt.write_text(json.dumps({**data, "location": str(target)}))
        invalid = view.native_session(view.calls[0].launches[0])
        assert invalid.status == "unavailable" and invalid.session_id == a.session_id
    data["launch"] = result.launches[1].launch_id
    receipt.write_text(json.dumps(data))
    assert view.native_session(view.calls[0].launches[0]).status == "unassociated"


@pytest.mark.unit
@pytest.mark.skipif(
    os.name == "nt", reason="creating directory symlinks requires Windows privileges"
)
def test_codex_redirected_sessions_does_not_select_another_home(tmp_path, monkeypatch):
    from types import SimpleNamespace

    from workjig import _codex_session
    from workjig.native_session import bind_session, locate_session

    home = tmp_path / "home"
    home.mkdir()
    target = tmp_path / "other-home" / "sessions"
    target.mkdir(parents=True)
    (home / "sessions").symlink_to(target, target_is_directory=True)
    worker = replace(
        codex_worker("test", sandbox="read-only", track_native_session=True),
        env={"CODEX_HOME": str(home)},
    )
    launch = tmp_path / "run" / "launches" / "example"
    launch.mkdir(parents=True)
    _, root = bind_session(worker, launch, tmp_path, worker.argv)
    assert root == home / "sessions"
    sid = "01901234-abcd-7000-8000-123456789abc"
    _session_receipt.publish(launch, "codex", sid, None)
    monkeypatch.setattr(
        _codex_session, "lookup_codex_path", lambda *args: pytest.fail("unexpected query")
    )
    recorded = SimpleNamespace(launch_dir=str(launch), native_session_root=str(root))
    session = locate_session(worker, recorded, launch, resolve=True)
    assert session.status == "unavailable" and session.session_id == sid
    assert session.reason == "invalid pinned native store"


@pytest.mark.unit
def test_tracked_pi_expands_tilde_from_worker_home(tmp_path):
    import os

    from workjig.native_session import _store

    worker = pi_worker(
        "example",
        provider="example",
        tools=("read",),
        project_trust="no-approve",
        track_native_session=True,
    )
    home = tmp_path / "worker-home"
    worker = replace(
        worker,
        env={
            ("USERPROFILE" if os.name == "nt" else "HOME"): str(home),
            "PI_CODING_AGENT_SESSION_DIR": "~/sessions",
        },
    )
    assert _store(worker, "pi", tmp_path) == home / "sessions"


@pytest.mark.unit
def test_resume_last_rejected_before_reservation(tmp_path):
    worker = CommandWorker(
        argv=("codex", "exec", "resume", "--last", "--json", "prompt"),
        track_native_session=True,
        output_protocol="codex-json",
    )
    with Run(tmp_path / "run", tmp_path) as run, pytest.raises(ValueError, match="new thread"):
        run.call(Request("no-resume", "task", {}, ("done",), worker))
    assert not read_run(tmp_path / "run").calls


@pytest.mark.unit
def test_codex_literal_prompt_is_not_a_resume(tmp_path):
    from workjig.native_session import bind_session

    argv = ("codex", "exec", "--json", "--", "resume")
    worker = CommandWorker(
        argv=argv,
        output_protocol="codex-json",
        track_native_session=True,
        env={"CODEX_HOME": str(tmp_path / "external")},
    )
    command, _ = bind_session(worker, tmp_path / "run/launches/test", tmp_path, argv)
    assert command == argv


@pytest.mark.unit
@pytest.mark.parametrize("name", ["Native-Session.JSON", "native-session.json.."])
def test_receipt_filename_reserved_case_insensitively(name):
    with pytest.raises(ValueError, match="conflicts"):
        CommandWorker(
            argv=("pi",),
            output_protocol="pi-json",
            track_native_session=True,
            evidence_file=name,
        )


@pytest.mark.unit
def test_generic_command_cannot_enable_tracking_without_protocol():
    with pytest.raises(ValueError, match="protocol"):
        CommandWorker(argv=("some-command",), track_native_session=True)


@pytest.mark.subprocess
@pytest.mark.parametrize("retain_logs", [True, False])
def test_agy_timeout_and_retry_resolve_distinct_native_ids(tmp_path, retain_logs):
    home = tmp_path / "agy-home"
    brain = home / ".gemini" / "antigravity-cli" / "brain"
    brain.mkdir(parents=True)
    script = tmp_path / "fake_agy.py"
    script.write_text("""\
import json, os, sys, time, uuid
from pathlib import Path
sid = str(uuid.uuid4())
home = Path(os.environ['USERPROFILE' if os.name == 'nt' else 'HOME'])
for name in ('stdout.log', 'stderr.log'):
    assert (Path(sys.argv[-1]).parent / name).exists() == (os.environ['RETAIN_LOGS'] == 'True')
logs = home / '.gemini' / 'antigravity-cli' / 'brain' / sid / '.system_generated' / 'logs'
transcript = logs / 'transcript.jsonl'
transcript.parent.mkdir(parents=True, exist_ok=True)
transcript.write_bytes(b'{"step_index":0}\\n')
print(json.dumps({'event': 'init', 'conversation_id': sid, 'init': {'model': 'test'}}), flush=True)
if not Path('first').exists():
    Path('first').touch()
    time.sleep(30)
(Path(sys.argv[-1]) / 'done').touch()
print(json.dumps({'event': 'result', 'result': {'conversation_id': sid, 'status': 'SUCCESS'}}))
""")
    home_var = "USERPROFILE" if os.name == "nt" else "HOME"
    worker = replace(
        agy_worker("test", track_native_session=True),
        argv=(sys.executable, str(script), "{output_dir}"),
        env={home_var: str(home), "RETAIN_LOGS": str(retain_logs)},
    )
    req = Request(
        call_id="copy",
        task="test",
        inputs={},
        required_outputs=("done",),
        worker=worker,
        max_launches=2,
        retry=RetryPolicy(on=("timeout",)),
        timeout_seconds=1.5,
    )
    with Run(tmp_path / "run", tmp_path, retain_logs=retain_logs) as run:
        result = run.call(req)
    assert [x.status for x in result.launches] == ["timeout", "delivered"]
    for attempt in result.launches:
        for name in ("stdout.log", "stderr.log"):
            assert (Path(attempt.launch_dir) / name).is_file() == retain_logs
    archive = tmp_path / "archive"
    shutil.copytree(tmp_path / "run", archive, ignore=shutil.ignore_patterns("*.log"))
    shutil.rmtree(tmp_path / "run")
    view = read_run(archive)
    first, second = view.calls[0].launches
    a, b = view.native_session(first), view.native_session(second)
    assert a.status == b.status == "located"
    assert a.source == b.source == "observed"
    assert a.harness == b.harness == "agy"
    assert a.session_id != b.session_id and a.path != b.path
    assert all(
        s.path == brain / s.session_id / ".system_generated" / "logs" / "transcript.jsonl"
        for s in (a, b)
    )
    assert not list(archive.rglob("*.jsonl"))

    # Tampering or missing transcript must fail closed without losing the known ID.
    receipt = archive / "launches" / first.launch_id / "native-session.json"
    data = json.loads(receipt.read_text())
    wrong_name = a.path.with_name("transcript_full.jsonl")
    wrong_name.write_bytes(b"{}\n")
    outside = tmp_path / "transcript.jsonl"
    outside.write_bytes(b"{}\n")
    for target in (outside, b.path, wrong_name, view.launch_files(first).prompt):
        receipt.write_text(json.dumps({**data, "location": str(target)}))
        invalid = view.native_session(first)
        assert invalid.status == "unavailable" and invalid.session_id == a.session_id
    receipt.write_text(json.dumps(data))
    a.path.unlink()
    missing = view.native_session(first)
    assert missing.status == "unavailable" and missing.session_id == a.session_id
    assert view.native_session(second, resolve=True) == b

    data["launch"] = second.launch_id
    receipt.write_text(json.dumps(data))
    assert view.native_session(first).status == "unassociated"


@pytest.mark.unit
def test_agy_init_event_validation():
    from workjig._session_observer import _CHUNK, EventObserver
    from workjig.native_session import _agy_id

    sid = "01901234-abcd-7000-8000-123456789abc"
    event = json.dumps({"event": "init", "conversation_id": sid}).encode()
    observer = EventObserver(_agy_id)
    for invalid in [
        b"not json",
        b"[" * 2000,
        b"\xff",
        b"[" + event + b"]",
        b'{"nested":' + event + b"}",
        event.replace(sid.encode(), sid.upper().encode()),
        event.replace(sid.encode(), b"../../target"),
        event.replace(sid.encode(), b""),
        event.replace(b'"init"', b'"result"'),
    ]:
        assert observer.feed(invalid + b"\n") is None
    assert observer.feed(b"x" * _CHUNK) is None
    assert observer.feed(event + b"\n" + event[:13]) is None
    assert observer.feed(event[13:]) is None
    assert observer.feed(b"\r\n") == sid


@pytest.mark.subprocess
def test_agy_partial_init_and_missing_transcript(tmp_path):
    home = tmp_path / "agy-home"
    home.mkdir()
    home_var = "USERPROFILE" if os.name == "nt" else "HOME"
    sid = "01901234-abcd-7000-8000-123456789abc"
    partial = json.dumps({"event": "init", "conversation_id": sid})
    script = tmp_path / "emit.py"
    script.write_text(
        "import sys\nsys.stdout.write(sys.argv[1] + ('\\n' if sys.argv[2] == 'nl' else ''))\n"
    )
    req_partial = Request(
        "partial",
        "test",
        {},
        ("done",),
        CommandWorker(
            argv=(sys.executable, str(script), partial, "none"),
            output_protocol="agy-stream-json",
            track_native_session=True,
            env={home_var: str(home)},
        ),
    )
    req_no_file = Request(
        "no-file",
        "test",
        {},
        ("done",),
        CommandWorker(
            argv=(sys.executable, str(script), partial, "nl"),
            output_protocol="agy-stream-json",
            track_native_session=True,
            env={home_var: str(home)},
        ),
    )
    with Run(tmp_path / "run", tmp_path) as run:
        run.call(req_partial)
        run.call(req_no_file)
    view = read_run(tmp_path / "run")
    by_call = {call.call_id: view.native_session(call.launches[0]) for call in view.calls}
    assert by_call["partial"].status == "unassociated"
    assert by_call["no-file"].status == "unavailable"
    assert by_call["no-file"].session_id == sid
    assert by_call["no-file"].source == "observed"


@pytest.mark.subprocess
def test_agy_slow_receipt_does_not_extend_worker_lifetime(tmp_path, monkeypatch):
    home = tmp_path / "agy-home"
    home.mkdir()
    home_var = "USERPROFILE" if os.name == "nt" else "HOME"
    sid = "01901234-abcd-7000-8000-123456789abc"
    event = json.dumps({"event": "init", "conversation_id": sid})

    def slow(*_):
        (tmp_path / "observed").touch()
        time.sleep(1.5)

    monkeypatch.setattr(_session_receipt, "publish", slow)
    script = tmp_path / "agy_slow.py"
    script.write_text(f"""\
import time
from pathlib import Path
print({event!r}, flush=True)
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
            output_protocol="agy-stream-json",
            track_native_session=True,
            env={home_var: str(home)},
        ),
        timeout_seconds=0.75,
    )
    with Run(tmp_path / "records", tmp_path) as run:
        assert run.call(req).status == "timeout"
    assert (tmp_path / "observed").exists() and not (tmp_path / "late-effect").exists()


@pytest.mark.unit
@pytest.mark.parametrize(
    "flag",
    [
        ("-c",),
        ("--continue",),
        ("--conversation", "abc"),
        ("--conversation=abc",),
        ("--remote-agent",),
    ],
)
def test_agy_tracked_rejects_resume_and_remote_flags_before_reservation(tmp_path, flag):
    home = tmp_path / "agy-home"
    home.mkdir()
    home_var = "USERPROFILE" if os.name == "nt" else "HOME"
    worker = CommandWorker(
        argv=("agy", "--output-format", "stream-json", *flag, "-p", "prompt"),
        output_protocol="agy-stream-json",
        track_native_session=True,
        env={home_var: str(home)},
    )
    with Run(tmp_path / "run", tmp_path) as run, pytest.raises(ValueError, match="local"):
        run.call(Request("no-resume", "task", {}, ("done",), worker))
    assert not read_run(tmp_path / "run").calls


@pytest.mark.unit
@pytest.mark.parametrize(
    "argv",
    [
        ("agy", "--output-format", "stream-json", "-p", "--continue"),
        ("agy", "--output-format", "stream-json", "--print", "-c"),
        ("agy", "--output-format", "stream-json", "--prompt", "--remote-agent"),
        ("agy", "--output-format", "stream-json", "-p=--continue"),
        ("agy", "--output-format", "stream-json", "--", "--conversation"),
    ],
)
def test_agy_literal_prompt_is_not_a_resume_flag(tmp_path, argv):
    from workjig.native_session import bind_session

    home = tmp_path / "agy-home"
    home_var = "USERPROFILE" if os.name == "nt" else "HOME"
    worker = CommandWorker(
        argv=argv,
        output_protocol="agy-stream-json",
        track_native_session=True,
        env={home_var: str(home)},
    )
    command, root = bind_session(worker, tmp_path / "run/launches/test", tmp_path, argv)
    assert command == argv
    assert root == (home / ".gemini" / "antigravity-cli" / "brain").resolve()


@pytest.mark.unit
def test_agy_rejects_native_store_inside_run(tmp_path):
    from workjig.native_session import bind_session

    run_dir = tmp_path / "run"
    home_var = "USERPROFILE" if os.name == "nt" else "HOME"
    worker = replace(
        agy_worker("test", track_native_session=True),
        env={home_var: str(run_dir)},
    )
    with pytest.raises(ValueError, match="outside the run"):
        bind_session(worker, run_dir / "launches" / "test", tmp_path, worker.argv)
