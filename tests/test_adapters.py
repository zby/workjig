"""Command construction and synthetic harness-event parsing.

These tests do not invoke a model or establish live adapter support.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from workjig.adapters import (
    agy_worker,
    claude_worker,
    codex_worker,
    parse_evidence,
    pi_worker,
)
from workjig.models import CommandWorker


def _render(worker):
    launch_dir = Path("launch-1")
    paths = {
        "prompt": launch_dir / "prompt.txt",
        "input_dir": launch_dir / "inputs",
        "output_dir": launch_dir / "outputs",
        "problem_file": launch_dir / "problem.json",
        "evidence_file": launch_dir / "evidence.json",
        "launch_dir": launch_dir,
        "workdir": Path("project"),
    }
    return worker.command(paths)


@pytest.mark.unit
def test_codex_command_has_explicit_model_sandbox_and_unattended_policy():
    command = _render(codex_worker("gpt-6.1-sol", sandbox="workspace-write", effort="medium"))
    assert command[:2] == ("codex", "exec")
    assert ("-m", "gpt-6.1-sol") == command[command.index("-m") : command.index("-m") + 2]
    assert ("-s", "workspace-write") == command[command.index("-s") : command.index("-s") + 2]
    assert "approval_policy=never" in command
    assert "model_reasoning_effort=medium" in command
    assert "--skip-git-repo-check" in command
    assert str(Path("launch-1") / "prompt.txt") in command[-1]
    assert "--dangerously-bypass-approvals-and-sandbox" not in command
    assert "--ignore-rules" not in command


@pytest.mark.unit
def test_claude_command_requires_tool_choice_and_denies_unanswered_prompts():
    command = _render(claude_worker("sonnet", allowed_tools=("Read", "Write")))
    assert command[:2] == ("claude", "-p")
    assert command[command.index("--model") + 1] == "sonnet"
    assert command[command.index("--permission-mode") + 1] == "dontAsk"
    assert command[command.index("--permission-prompts") + 1] == "none"
    assert "--allowedTools=Read,Write" in command
    assert "--verbose" in command
    assert str(Path("launch-1") / "prompt.txt") in command[-1]
    assert "--dangerously-skip-permissions" not in command
    assert "--bare" not in command


@pytest.mark.unit
def test_pi_command_requires_provider_tools_and_trust_choice():
    command = _render(
        pi_worker(
            "gpt-6.1-sol", provider="openai", tools=("read", "write"), project_trust="no-approve"
        )
    )
    assert command[:3] == ("pi", "--mode", "json")
    assert command[command.index("--provider") + 1] == "openai"
    assert command[command.index("--model") + 1] == "gpt-6.1-sol"
    assert command[command.index("--tools") + 1] == "read,write"
    assert "--no-approve" in command
    assert command[command.index("--session-dir") + 1] == str(Path("launch-1"))
    assert str(Path("launch-1") / "prompt.txt") in command[-1]


@pytest.mark.unit
def test_agy_command_uses_stream_json_sandbox_and_omits_permission_bypass():
    command = _render(agy_worker("gemini-3.8-flash-low"))
    assert command[0] == "agy"
    assert command[command.index("--output-format") + 1] == "stream-json"
    assert command[command.index("--model") + 1] == "gemini-3.8-flash-low"
    assert "--disable-slash-commands" in command
    assert "--sandbox" in command
    assert "--mode" not in command
    assert "--effort" not in command
    assert "--dangerously-skip-permissions" not in command
    assert command[-2] == "-p"
    assert str(Path("launch-1") / "prompt.txt") in command[-1]


@pytest.mark.unit
def test_agy_command_accepts_explicit_mode_effort_and_sandbox_toggle():
    worker = agy_worker(
        "gemini-4-neon-high",
        mode="accept-edits",
        sandbox=False,
        effort="high",
        track_native_session=True,
    )
    command = _render(worker)
    assert command[command.index("--mode") + 1] == "accept-edits"
    assert command[command.index("--effort") + 1] == "high"
    assert "--sandbox" not in command
    assert "--dangerously-skip-permissions" not in command
    assert command[-2] == "-p"
    assert worker.track_native_session is True


@pytest.mark.unit
def test_command_rendering_keeps_literal_braces_while_replacing_path_tokens():
    command = _render(CommandWorker(argv=("worker", '{"literal": 1}', "{prompt}")))
    assert command == ("worker", '{"literal": 1}', str(Path("launch-1") / "prompt.txt"))


@pytest.mark.parametrize(
    "build",
    [
        lambda: codex_worker("", sandbox="workspace-write"),
        lambda: codex_worker("m", sandbox="invalid"),
        lambda: codex_worker("{prompt}", sandbox="workspace-write"),
        lambda: claude_worker("m", allowed_tools=()),
        lambda: claude_worker("m", allowed_tools=("Read,Bash",)),
        lambda: pi_worker("m", provider="p", tools=("read",), project_trust="unknown"),
        lambda: agy_worker(""),
        lambda: agy_worker("{prompt}"),
        lambda: agy_worker("m", mode="unknown"),
        lambda: agy_worker("m", sandbox="true"),
        lambda: agy_worker("m", effort="extreme"),
    ],
)
@pytest.mark.unit
def test_invalid_adapter_configuration_is_rejected(build):
    with pytest.raises(ValueError):
        build()


def _jsonl(*records):
    return "\n".join(json.dumps(record) for record in records)


@pytest.mark.unit
def test_codex_evidence_keeps_session_and_terminal_failure_without_model_guess():
    evidence = parse_evidence(
        "codex",
        _jsonl(
            {"type": "thread.started", "thread_id": "thread-1"},
            {"type": "turn.started"},
            {"type": "turn.failed", "error": {"message": "failure"}},
        ),
    )
    assert evidence["session_id"] == "thread-1"
    assert evidence["turn_outcome"] == "turn.failed"
    assert "served_model" not in evidence


@pytest.mark.unit
def test_claude_retry_rate_limit_is_not_a_final_usage_limit_claim():
    evidence = parse_evidence(
        "claude",
        _jsonl(
            {"type": "system", "subtype": "api_retry", "error": "rate_limit"},
            {"type": "assistant", "message": {"model": "claude-sonnet-example"}},
            {"type": "result", "subtype": "success", "session_id": "session-1", "is_error": False},
        ),
    )
    assert evidence["retry_error_categories"] == ["rate_limit"]
    assert evidence["is_error"] is False
    assert evidence["session_id"] == "session-1"
    assert evidence["reported_model"] == "claude-sonnet-example"
    assert "served_model" not in evidence


@pytest.mark.unit
def test_pi_evidence_distinguishes_requested_and_concrete_response_model():
    evidence = parse_evidence(
        "pi",
        _jsonl(
            {"type": "session", "id": "pi-1", "cwd": "/tmp/project"},
            {
                "type": "message_end",
                "message": {
                    "role": "assistant",
                    "provider": "openai",
                    "model": "model-alias",
                    "responseModel": "concrete-model",
                    "responseId": "resp-1",
                    "stopReason": "stop",
                },
            },
            {"type": "agent_settled", "aborted": False},
        ),
    )
    assert evidence["session_id"] == "pi-1"
    assert evidence["reported_model"] == "model-alias"
    assert evidence["served_model"] == "concrete-model"
    assert evidence["response_id"] == "resp-1"
    assert evidence["settled_aborted"] is False


@pytest.mark.unit
def test_claude_result_preserves_usage_for_multiple_models_without_guessing():
    usage = {
        "claude-haiku-example": {"inputTokens": 12, "outputTokens": 8},
        "claude-other-example": {"inputTokens": 3, "outputTokens": 1},
    }
    evidence = parse_evidence("claude", _jsonl({"type": "result", "modelUsage": usage}))
    assert evidence["model_usage"] == usage
    assert "served_model" not in evidence
    assert "reported_model" not in evidence


@pytest.mark.parametrize("usage", [None, "haiku", ["haiku"]])
@pytest.mark.unit
def test_claude_invalid_model_usage_is_not_model_evidence(usage):
    evidence = parse_evidence("claude", _jsonl({"type": "result", "modelUsage": usage}))
    assert "model_usage" not in evidence


@pytest.mark.unit
def test_partial_and_malformed_stream_does_not_invent_completion():
    evidence = parse_evidence("pi", '{"type":"session","id":"pi-2"}\nnot json\n')
    assert evidence["session_id"] == "pi-2"
    assert evidence["parse_errors"] == 1
    assert "settled_aborted" not in evidence
    assert "served_model" not in evidence


@pytest.mark.unit
def test_jsonl_parser_does_not_split_unicode_line_separator_inside_text():
    raw = json.dumps(
        {"type": "message_end", "message": {"role": "assistant", "model": "m\u2028v"}},
        ensure_ascii=False,
    )
    evidence = parse_evidence("pi", raw + "\n")
    assert evidence["records"] == 1
    assert evidence["parse_errors"] == 0
    assert evidence["reported_model"] == "m\u2028v"


@pytest.mark.unit
def test_agy_evidence_keeps_session_model_status_and_usage_without_served_guess():
    usage = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}
    evidence = parse_evidence(
        "agy",
        _jsonl(
            {
                "event": "init",
                "conversation_id": "conv-1",
                "init": {"model": "gemini-3.8-flash-low", "permission_mode": "request-review"},
            },
            {
                "event": "result",
                "result": {
                    "conversation_id": "conv-1",
                    "status": "SUCCESS",
                    "response": "Done.",
                    "usage": usage,
                },
            },
        ),
    )
    assert evidence["session_id"] == "conv-1"
    assert evidence["reported_model"] == "gemini-3.8-flash-low"
    assert evidence["permission_mode"] == "request-review"
    assert evidence["result_status"] == "SUCCESS"
    assert evidence["usage"] == usage
    assert "served_model" not in evidence
    assert "error_message" not in evidence
    assert "denied_actions" not in evidence


@pytest.mark.unit
def test_agy_evidence_records_errors_and_denied_actions_without_empty_session():
    error_evidence = parse_evidence(
        "agy",
        _jsonl(
            {
                "event": "result",
                "result": {
                    "conversation_id": "",
                    "status": "ERROR",
                    "error": "invalid model selection",
                    "finished_reason": "error",
                },
            }
        ),
    )
    assert "session_id" not in error_evidence
    assert error_evidence["result_status"] == "ERROR"
    assert error_evidence["finished_reason"] == "error"
    assert error_evidence["error_message"] == "invalid model selection"

    denied_evidence = parse_evidence(
        "agy",
        _jsonl(
            {
                "event": "result",
                "result": {
                    "conversation_id": "conv-2",
                    "status": "SUCCESS",
                    "denied_actions": [{"action": "command", "display_name": "Bash"}],
                },
            }
        ),
    )
    assert denied_evidence["session_id"] == "conv-2"
    assert denied_evidence["denied_actions"] == [{"action": "command", "display_name": "Bash"}]
