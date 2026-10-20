"""Terminal classification through real deterministic subprocesses, never models."""

import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from workjig import CommandWorker, RecordError, Request, RetryPolicy, Run
from workjig.adapters import claude_worker, codex_worker, pi_worker

CAPTURES = Path(__file__).resolve().parents[1] / "adr/evidence/terminal-failures/captures"
PROTOCOLS = {"codex": "codex-json", "claude": "claude-stream-json", "pi": "pi-json"}
CASES = [
    f"{harness}-{scenario}"
    for harness in PROTOCOLS
    for scenario in ("success", "quota", "recover", "auth")
] + ["codex-exhausted", "pi-exhausted", "claude-billing"]

WORKER = """\
import subprocess, sys, time
from pathlib import Path
inputs, outputs, problem = map(Path, sys.argv[1:4])
with Path('starts').open('a') as log: log.write('start\\n')
(outputs / 'answer.txt').write_text('candidate')
if (inputs / 'problem.txt').exists():
    problem.write_text((inputs / 'problem.txt').read_text())
sys.stdout.buffer.write((inputs / 'stream.txt').read_bytes())
sys.stdout.flush()
sys.stderr.buffer.write((inputs / 'stderr.txt').read_bytes())
if sys.argv[5] == 'timeout': time.sleep(30)
if sys.argv[5] == 'linger':
    subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
sys.exit(int(sys.argv[4]))
"""


def request(protocol, stream, *, code=0, behavior="exit", problem=None):
    inputs = {"stream.txt": stream, "stderr.txt": "Quoted content: usage limit\n"}
    if problem is not None:
        inputs["problem.txt"] = problem
    return Request(
        "job",
        "Write answer",
        inputs,
        ("answer.txt",),
        CommandWorker(
            (
                sys.executable,
                "-c",
                WORKER,
                "{input_dir}",
                "{output_dir}",
                "{problem_file}",
                str(code),
                behavior,
            ),
            name="descriptive-only",
            output_protocol=protocol,
        ),
        max_launches=2,
        retry=RetryPolicy(on=("process_error",)),
        timeout_seconds=0.5,
    )


def check(tmp_path, req, status):
    records = tmp_path / "records"
    with Run(records, tmp_path) as run:
        result = run.call(req)
    count = 2 if status == "process_error" else 1
    assert result.status == status
    assert len(result.launches) == count
    assert dict(result.outputs) == ({"answer.txt": "candidate"} if status == "delivered" else {})
    assert result.failed_at_launch_limit == (status == "process_error")
    with Run(records, tmp_path) as run:
        replay = run.call(req)
    assert replay.reused
    assert replay.launches == result.launches
    assert replay.status == result.status
    assert dict(replay.outputs) == dict(result.outputs)
    assert (tmp_path / "starts").read_text().splitlines() == ["start"] * count
    return result


@pytest.mark.parametrize("case", CASES)
def test_retained_cli_streams_classify_and_replay(tmp_path, case):
    fixture = CAPTURES / case
    observed = json.loads((fixture / "result.json").read_text())
    harness = observed["harness"]
    req = request(
        PROTOCOLS[harness], (fixture / "stdout.jsonl").read_text(), code=observed["returncode"]
    )
    factories = {
        "codex": lambda: codex_worker("fixture", sandbox="read-only"),
        "claude": lambda: claude_worker("fixture", allowed_tools=("Read",)),
        "pi": lambda: pi_worker(
            "fixture", provider="fixture", tools=("read",), project_trust="no-approve"
        ),
    }
    # Preserve the factory's actual protocol selection while substituting a
    # deterministic executable. No harness installation is needed by the suite.
    worker = replace(factories[harness](), argv=req.worker.argv, name="renamed-display")
    req = replace(req, worker=worker)
    scenario = observed["scenario"]
    expected = (
        "delivered"
        if scenario in {"success", "recover"}
        else ("process_error" if scenario == "auth" else "usage_limit")
    )
    result = check(tmp_path, req, expected)
    assert all(launch.returncode == observed["returncode"] for launch in result.launches)


def jsonl(*records):
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in records) + "\n"


CLAUDE_FAILURE = {
    "type": "result",
    "subtype": "success",
    "is_error": True,
    "result": "unrecognized failure",
}
CLAUDE_SUCCESS = {
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "result": "Quoted usage limit\u2028is ordinary text",
}
PI_ERROR = {
    "type": "message_end",
    "message": {
        "role": "assistant",
        "stopReason": "error",
        "errorMessage": "429: rate limit exceeded",
    },
}
PI_STOP = {"type": "message_end", "message": {"role": "assistant", "stopReason": "stop"}}
SETTLED = {"type": "agent_settled", "aborted": False}


@pytest.mark.parametrize(
    "protocol,records,expected",
    [
        ("codex-json", [{"type": "error", "message": "usage limit"}], "process_error"),
        (
            "codex-json",
            [{"type": "turn.failed", "error": {"message": "Usage limit reached"}}],
            "usage_limit",
        ),
        ("codex-json", [{"type": "turn.completed"}, {"type": "turn.started"}], "process_error"),
        ("codex-json", [{"type": "turn.failed", "error": []}], "process_error"),
        (
            "claude-stream-json",
            [{"type": "system", "subtype": "api_retry", "error": "rate_limit"}, CLAUDE_FAILURE],
            "process_error",
        ),
        (
            "claude-stream-json",
            [
                {"type": "assistant", "parent_tool_use_id": "nested", "error": "rate_limit"},
                CLAUDE_FAILURE,
            ],
            "process_error",
        ),
        (
            "claude-stream-json",
            [{"type": "assistant", "agent_id": "nested", "error": "rate_limit"}, CLAUDE_FAILURE],
            "process_error",
        ),
        (
            "claude-stream-json",
            [{"type": "assistant", "error": "rate_limit"}, {"type": "assistant"}, CLAUDE_FAILURE],
            "process_error",
        ),
        (
            "claude-stream-json",
            [{"type": "assistant", "error": "rate_limit"}, CLAUDE_SUCCESS],
            "delivered",
        ),
        (
            "claude-stream-json",
            [CLAUDE_SUCCESS, {**CLAUDE_FAILURE, "parent_tool_use_id": "nested"}],
            "delivered",
        ),
        (
            "claude-stream-json",
            [
                {
                    "type": "result",
                    "subtype": "error_max_budget_usd",
                    "is_error": True,
                    "errors": ["usage limit"],
                }
            ],
            "process_error",
        ),
        (
            "claude-stream-json",
            [
                {
                    "type": "rate_limit_event",
                    "rate_limit_info": {"status": "rejected", "errorCode": "credits_required"},
                },
                CLAUDE_SUCCESS,
            ],
            "delivered",
        ),
        (
            "claude-stream-json",
            [CLAUDE_SUCCESS, {"type": "system", "subtype": "ui_invalidate"}],
            "delivered",
        ),
        ("claude-stream-json", [{**CLAUDE_FAILURE, "api_error_status": 429}], "usage_limit"),
        ("claude-stream-json", [{**CLAUDE_FAILURE, "is_error": "false"}], "process_error"),
        ("claude-stream-json", [{**CLAUDE_FAILURE, "subtype": "unknown"}], "process_error"),
        ("pi-json", [PI_ERROR], "process_error"),
        ("pi-json", [PI_ERROR, {"type": "agent_end", "willRetry": False}], "process_error"),
        ("pi-json", [PI_ERROR, SETTLED], "usage_limit"),
        ("pi-json", [PI_ERROR, {"type": "auto_retry_start"}, SETTLED], "process_error"),
        (
            "pi-json",
            [PI_ERROR, {"type": "message_start", "message": {"role": "assistant"}}, SETTLED],
            "process_error",
        ),
        ("pi-json", [PI_ERROR, {**SETTLED, "aborted": True}], "process_error"),
        ("pi-json", [PI_STOP, {**SETTLED, "aborted": "false"}], "process_error"),
        ("pi-json", [PI_ERROR, {"type": "auto_retry_start"}, PI_STOP, SETTLED], "delivered"),
        (
            "pi-json",
            [
                {"type": "tool_execution_end", "isError": True, "result": "usage limit"},
                PI_STOP,
                SETTLED,
            ],
            "delivered",
        ),
        ("pi-json", [PI_STOP, SETTLED, {"type": "agent_start"}], "process_error"),
    ],
)
def test_terminal_selection_and_scoping(tmp_path, protocol, records, expected):
    check(tmp_path, request(protocol, jsonl(*records)), expected)


@pytest.mark.parametrize("protocol", PROTOCOLS.values())
@pytest.mark.parametrize("stream", ["", "not json\n", "[]\n", '{"type":[]}\n', '{"type":'])
def test_unknown_or_malformed_stream_is_bounded_process_error(tmp_path, protocol, stream):
    check(tmp_path, request(protocol, stream), "process_error")


@pytest.mark.parametrize("first", [PI_STOP, PI_ERROR])
@pytest.mark.parametrize("message", [None, [], {}, {"role": []}])
def test_malformed_pi_message_cannot_preserve_prior_outcome(tmp_path, first, message):
    stream = jsonl(first, {"type": "message_end", "message": message}, SETTLED)
    check(tmp_path, request("pi-json", stream), "process_error")


@pytest.mark.parametrize(
    "protocol,records",
    [
        ("codex-json", [{"type": "turn.completed"}]),
        ("claude-stream-json", [CLAUDE_SUCCESS]),
        ("pi-json", [PI_STOP, SETTLED]),
    ],
)
@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_invalid_json_constants_cannot_establish_completion(tmp_path, protocol, records, constant):
    stream = jsonl(*records).rstrip()[:-1] + ', "extra": ' + constant + "}\n"
    check(tmp_path, request(protocol, stream), "process_error")


@pytest.mark.parametrize("protocol", PROTOCOLS.values())
def test_malformed_line_cannot_adopt_quota_event(tmp_path, protocol):
    harness = next(h for h, p in PROTOCOLS.items() if p == protocol)
    stream = (CAPTURES / f"{harness}-quota" / "stdout.jsonl").read_text()
    check(tmp_path, request(protocol, stream + "broken\n"), "process_error")


@pytest.mark.parametrize("behavior,expected", [("timeout", "timeout"), ("linger", "usage_limit")])
def test_terminal_quota_precedence(tmp_path, behavior, expected):
    check(tmp_path, request("pi-json", jsonl(PI_ERROR, SETTLED), behavior=behavior), expected)


def test_structured_worker_stop_overrides_unknown_harness_stream(tmp_path):
    check(
        tmp_path,
        request(
            "pi-json",
            "broken",
            problem=json.dumps({"kind": "usage_limit", "message": "operator action needed"}),
        ),
        "usage_limit",
    )


def test_success_event_does_not_override_failed_exit(tmp_path):
    check(tmp_path, request("claude-stream-json", jsonl(CLAUDE_SUCCESS), code=7), "process_error")


def test_display_name_does_not_select_protocol(tmp_path):
    req = request(None, jsonl(PI_ERROR, SETTLED))
    check(tmp_path, replace(req, worker=replace(req.worker, name="pi")), "delivered")


@pytest.mark.parametrize("value", ["unknown", "", [], {}, True, 3])
def test_invalid_protocol_rejected(value):
    with pytest.raises(ValueError, match="output protocol"):
        CommandWorker(("worker",), output_protocol=value)


def test_output_protocol_is_declared_identity(tmp_path):
    req = request(None, jsonl(PI_ERROR, SETTLED))
    with Run(tmp_path / "records", tmp_path) as run:
        first = run.call(req)
        second = run.call(replace(req, worker=replace(req.worker, output_protocol="pi-json")))
    assert first.status == "delivered" and second.status == "usage_limit"
    assert first.request_hash != second.request_hash
    assert first.launches[0].launch_id != second.launches[0].launch_id
    state = json.loads((tmp_path / "records/run.json").read_text())
    entries = state["calls"]["job"]
    assert "output_protocol" not in entries[first.request_hash]["request"]["worker"]
    assert entries[second.request_hash]["request"]["worker"]["output_protocol"] == "pi-json"
    with Run(tmp_path / "records", tmp_path) as run:
        assert run.call(req).reused
        assert run.call(replace(req, worker=replace(req.worker, output_protocol="pi-json"))).reused


def test_old_record_outcome_is_not_reclassified(tmp_path):
    req = replace(request(None, "usage limit", code=7), retry=RetryPolicy())
    records = tmp_path / "records"
    with Run(records, tmp_path) as run:
        first = run.call(req)
    path = records / "run.json"
    state = json.loads(path.read_text())
    entry = state["calls"][req.call_id][first.request_hash]
    assert "output_protocol" not in entry["request"]["worker"]
    # This models a valid v1 record emitted under the old raw-log policy.
    entry["attempts"][0]["status"] = "usage_limit"
    path.write_text(json.dumps(state))
    original = path.read_bytes()
    with Run(records, tmp_path) as run:
        replay = run.call(req)
    assert replay.reused and replay.status == "usage_limit"
    assert len(replay.launches) == 1
    assert path.read_bytes() == original
    assert (tmp_path / "starts").read_text().splitlines() == ["start"]


def test_corrupt_persisted_protocol_raises_record_error(tmp_path):
    req = request("pi-json", jsonl(PI_STOP, SETTLED))
    with Run(tmp_path / "records", tmp_path) as run:
        result = run.call(req)
    path = tmp_path / "records/run.json"
    state = json.loads(path.read_text())
    state["calls"][req.call_id][result.request_hash]["request"]["worker"]["output_protocol"] = []
    path.write_text(json.dumps(state))
    with pytest.raises(RecordError):
        with Run(path.parent, tmp_path):
            pytest.fail("corrupt protocol was loaded")
