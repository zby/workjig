"""Select terminal harness failures, never quota phrases from ordinary output.

This is separate from descriptive evidence extraction: an intermediate error
may be useful evidence even when the harness subsequently succeeds.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from pathlib import Path

# Observed terminal messages and provider error codes; not a universal taxonomy.
_QUOTA = re.compile(
    r"\busage limit\b|\bquota exceeded\b|\binsufficient_quota\b|"
    r"\brate[_ -]limit(?:[_ -]exceeded)?\b|\bcredit balance is too low\b|"
    r"(?:^|\bstatus[: ]+|\bhttp[: ]+)429\b",
    re.IGNORECASE,
)


def _failure(message: object) -> str:
    if isinstance(message, str) and _QUOTA.search(message):
        return "usage_limit"
    return "process_error"


def _reject_constant(value: str) -> None:
    raise ValueError(f"invalid JSON constant: {value}")


def _records(path: Path) -> Iterator[dict[str, object]]:
    if path.is_symlink() or not path.is_file():
        raise ValueError("harness log is not a regular file")
    # JSONL is LF-delimited; Unicode paragraph separators are valid string data.
    with path.open(encoding="utf-8", newline="\n") as stream:
        for line in stream:
            if not line.strip():
                continue
            value = json.loads(line, parse_constant=_reject_constant)
            if not isinstance(value, dict) or not isinstance(value.get("type"), str):
                raise ValueError("invalid harness event")
            yield value


def terminal_failure(protocol: str, stdout: Path) -> str | None:
    """Return usage_limit/process_error, or None after a successful terminal event.

    Missing or malformed completion evidence is process_error. The caller still
    checks process exit, cleanup, worker problems and delivered files.
    """
    parser = {
        "codex-json": _codex,
        "claude-stream-json": _claude,
        "pi-json": _pi,
    }[protocol]
    try:
        return parser(_records(stdout))
    except (OSError, UnicodeError, ValueError, RecursionError):
        return "process_error"


def _codex(records: Iterator[dict[str, object]]) -> str | None:
    outcome: str | None = "process_error"
    for record in records:
        event = record["type"]
        if event in {"thread.started", "turn.started"}:
            outcome = "process_error"
        elif event == "turn.completed":
            outcome = None
        elif event == "turn.failed":
            error = record.get("error")
            outcome = _failure(error.get("message") if isinstance(error, dict) else None)
        # Top-level error events can precede retry; item errors can be warnings.
    return outcome


def _claude(records: Iterator[dict[str, object]]) -> str | None:
    outcome: str | None = "process_error"
    category: object = None
    for record in records:
        if record.get("parent_tool_use_id") is not None or record.get("agent_id") is not None:
            continue
        event = record["type"]
        if event == "assistant":
            category = record.get("error")
            outcome = "process_error"
        elif event == "result":
            subtype = record.get("subtype")
            if subtype == "success" and record.get("is_error") is False:
                outcome = None
            elif subtype == "success" and record.get("is_error") is True:
                status = record.get("api_error_status")
                if category in ("rate_limit", "billing_error") or (
                    type(status) is int and status == 429
                ):
                    outcome = "usage_limit"
                else:
                    outcome = _failure(record.get("result"))
            else:
                # Local budgets, startup errors and unknown result variants are
                # not provider quota evidence merely because an earlier retry was.
                outcome = "process_error"
            category = None
        # api_retry and subscription limit notifications are not terminal outcomes.
    return outcome


def _pi(records: Iterator[dict[str, object]]) -> str | None:
    message: dict[str, object] | None = None
    settled = False
    aborted: object = None
    for record in records:
        event = record["type"]
        if event in {"session", "agent_start", "turn_start", "auto_retry_start"}:
            message = None
            settled = False
        elif event in {"message_start", "message_end"}:
            candidate = record.get("message")
            if not isinstance(candidate, dict) or not isinstance(candidate.get("role"), str):
                raise ValueError("invalid Pi message")
            if candidate["role"] == "assistant":
                message = candidate if event == "message_end" else None
                settled = False
        elif event == "agent_settled":
            settled = True
            aborted = record.get("aborted")
    if not settled or aborted is not False or message is None:
        return "process_error"
    if message.get("stopReason") == "stop":
        return None
    if message.get("stopReason") == "error":
        return _failure(message.get("errorMessage"))
    return "process_error"
