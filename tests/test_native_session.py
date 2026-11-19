"""Public reader and tracked adapters, using only isolated fake native stores."""

import json
import shutil
import sys
from dataclasses import replace

import pytest

from workjig import CommandWorker, Request, RetryPolicy, Run, read_run
from workjig.adapters import claude_worker, codex_worker, pi_worker

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
    store = tmp_path / "external sessions"
    store.mkdir()
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
        env={"PI_CODING_AGENT_SESSION_DIR": str(store), "STORE": str(store)},
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
    assert view.native_session(second) == b
    with pytest.raises(ValueError, match="not in this run"):
        view.native_session(replace(first, launch_id="other"))


@pytest.mark.subprocess
def test_codex_timeout_and_retry_resolve_distinct_native_ids(tmp_path):
    external = tmp_path / "native"
    external.mkdir()
    script = tmp_path / "exec.py"
    script.write_text("""\
import json, os, sys, time, uuid
from pathlib import Path
sid = str(uuid.uuid4())
(Path(os.environ['STORE']) / (sid + '.jsonl')).touch()
print(json.dumps({'type': 'thread.started', 'thread_id': sid}), flush=True)
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
path = Path(os.environ['STORE']) / (sid + '.jsonl')
thread = {'id': sid, 'path': str(path)}
print(json.dumps({'id': req['id'], 'result': {'thread': thread}}), flush=True)
""")
    worker = replace(
        codex_worker("test", sandbox="read-only", track_native_session=True),
        argv=(sys.executable, str(script), "{output_dir}"),
        env={"STORE": str(external)},
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
    with Run(tmp_path / "run", tmp_path) as run:
        result = run.call(req)
    assert [x.status for x in result.launches] == ["timeout", "delivered"]
    archive = tmp_path / "archive"
    shutil.copytree(tmp_path / "run", archive, ignore=shutil.ignore_patterns("*.log"))
    view = read_run(archive)
    a, b = (view.native_session(attempt) for attempt in view.calls[0].launches)
    assert a.status == b.status == "located"
    assert a.session_id != b.session_id and a.source == b.source == "observed"
    assert all(s.path == external / (s.session_id + ".jsonl") for s in (a, b))
    receipt = archive / "launches" / result.launches[0].launch_id / "native-session.json"
    data = json.loads(receipt.read_text())
    data["launch"] = result.launches[1].launch_id
    receipt.write_text(json.dumps(data))
    assert view.native_session(view.calls[0].launches[0]).status == "unassociated"


@pytest.mark.unit
def test_generic_command_cannot_enable_tracking_without_protocol():
    with pytest.raises(ValueError, match="protocol"):
        CommandWorker(argv=("some-command",), track_native_session=True)
