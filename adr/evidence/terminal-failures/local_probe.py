"""Bounded CLI failure probes. Run inside a network namespace with only loopback."""

import argparse
import json
import os
import signal
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

VERSIONS = {"codex": "codex-cli 0.162.1", "claude": "2.1.303 (Claude Code)", "pi": "1.3.0"}


def success_events(harness):
    text = "Fixture succeeded. Quoted phrase: usage limit."
    if harness == "claude":
        return [
            (
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": "msg_fixture",
                        "type": "message",
                        "role": "assistant",
                        "model": "claude-haiku-4-5-20251001",
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 16, "output_tokens": 0},
                    },
                },
            ),
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": text},
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            (
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 12},
                },
            ),
            ("message_stop", {"type": "message_stop"}),
        ]
    if harness == "pi":
        return [
            (
                None,
                {
                    "id": "chat_fixture",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "fixture",
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"role": "assistant", "content": text},
                            "finish_reason": None,
                        }
                    ],
                },
            ),
            (
                None,
                {
                    "id": "chat_fixture",
                    "object": "chat.completion.chunk",
                    "created": 1,
                    "model": "fixture",
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 16, "completion_tokens": 12, "total_tokens": 28},
                },
            ),
            (None, "[DONE]"),
        ]
    item = {
        "id": "msg_fixture",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text, "annotations": []}],
    }
    return [
        (name, data)
        for name, data in [
            (
                "response.created",
                {
                    "type": "response.created",
                    "response": {
                        "id": "resp_fixture",
                        "object": "response",
                        "status": "in_progress",
                        "output": [],
                    },
                },
            ),
            (
                "response.output_item.added",
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {**item, "status": "in_progress", "content": []},
                },
            ),
            (
                "response.output_text.delta",
                {
                    "type": "response.output_text.delta",
                    "item_id": "msg_fixture",
                    "output_index": 0,
                    "content_index": 0,
                    "delta": text,
                },
            ),
            (
                "response.output_item.done",
                {"type": "response.output_item.done", "output_index": 0, "item": item},
            ),
            (
                "response.completed",
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_fixture",
                        "object": "response",
                        "status": "completed",
                        "output": [item],
                        "usage": {
                            "input_tokens": 16,
                            "output_tokens": 12,
                            "total_tokens": 28,
                            "input_tokens_details": {"cached_tokens": 0},
                            "output_tokens_details": {"reasoning_tokens": 0},
                        },
                    },
                },
            ),
        ]
    ]


def probe(harness, scenario, destination, cli):
    if {name for _, name in socket.if_nameindex()} != {"lo"}:
        raise RuntimeError("Run in an isolated network namespace with only loopback")
    cli = cli.resolve(strict=True)
    retry_enabled = scenario in {"quota", "recover", "exhausted"}
    transient = scenario in {"recover", "exhausted"}
    root = destination / f"{harness}-{scenario}"
    root.mkdir(parents=True)
    home = root / "home"
    work = root / "work"
    home.mkdir()
    work.mkdir()
    calls = []
    sequence = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers.get("content-length", "0")))
            if "count_tokens" in self.path:
                self.reply(200, {"input_tokens": 16})
                return
            calls.append(self.path)
            if len(calls) > 12:
                self.reply(
                    400,
                    {
                        "error": {
                            "type": "invalid_request_error",
                            "message": "fixture request bound reached",
                        }
                    },
                )
                return
            success = scenario == "success" or (scenario == "recover" and len(calls) > 1)
            sequence.append({"path": self.path, "response": "success" if success else scenario})
            if success:
                body = "".join(
                    (f"event: {name}\n" if name else "")
                    + "data: "
                    + (data if isinstance(data, str) else json.dumps(data))
                    + "\n\n"
                    for name, data in success_events(harness)
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            status = 401 if scenario == "auth" else 400 if scenario == "billing" else 429
            message = (
                "invalid fixture key"
                if scenario == "auth"
                else "Your credit balance is too low to access the Anthropic API."
                if scenario == "billing"
                else "rate limit exceeded"
                if transient
                else "fixture quota exhausted"
            )
            typ = (
                "authentication_error"
                if scenario == "auth"
                else ("invalid_request_error" if scenario == "billing" else "rate_limit_error")
                if harness == "claude"
                else "rate_limit_exceeded"
                if transient
                else "insufficient_quota"
            )
            payload = {
                "type": "error",
                "error": {"type": typ, "code": typ, "message": message},
                "request_id": "req_fixture",
            }
            sequence[-1].update(status=status, body=payload, should_retry=transient)
            self.reply(status, payload, retry=transient)

        def reply(self, status, payload, retry=False):
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("retry-after", "0")
            self.send_header("x-should-retry", "true" if retry else "false")
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin:/usr/sbin",
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / "config"),
        "XDG_CACHE_HOME": str(home / "cache"),
        "LANG": "C.UTF-8",
        "TERM": "dumb",
        "TMPDIR": str(root),
        "CI": "1",
    }
    prompt = "Return the fixture response. Do not use tools."
    if harness == "claude":
        env.update(
            ANTHROPIC_API_KEY="fixture-not-a-secret",
            ANTHROPIC_BASE_URL=url,
            CLAUDE_CONFIG_DIR=str(home / "claude"),
            CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
            DISABLE_AUTOUPDATER="1",
            DISABLE_TELEMETRY="1",
            DISABLE_ERROR_REPORTING="1",
        )
        argv = [
            str(cli),
            "--bare",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            "claude-haiku-4-5-20251001",
            "--tools",
            "",
            "--strict-mcp-config",
            "--setting-sources",
            "",
            "--no-session-persistence",
            "--permission-mode",
            "dontAsk",
            "--permission-prompts",
            "none",
            "--disable-slash-commands",
            "--system-prompt",
            "You are a fixture client.",
            prompt,
        ]
    elif harness == "pi":
        config = home / "pi"
        config.mkdir()
        (config / "models.json").write_text(
            json.dumps(
                {
                    "providers": {
                        "fixture": {
                            "baseUrl": url + "/v1",
                            "api": "openai-completions",
                            "apiKey": "fixture-not-a-secret",
                            "models": [
                                {"id": "fixture", "contextWindow": 32000, "maxTokens": 1000}
                            ],
                        }
                    }
                }
            )
        )
        (config / "settings.json").write_text(
            json.dumps(
                {
                    "retry": {
                        "enabled": retry_enabled,
                        "maxRetries": 1,
                        "baseDelayMs": 10,
                        "maxAgentDelayMs": 100,
                        "provider": {"maxRetries": 0},
                    },
                    "compaction": {"enabled": False},
                    "cacheWarming": "off",
                    "enableInstallTelemetry": False,
                }
            )
        )
        env.update(
            PI_CODING_AGENT_DIR=str(config),
            PI_OFFLINE="1",
            PI_TELEMETRY="0",
            PI_SKIP_VERSION_CHECK="1",
        )
        argv = [
            "/usr/bin/node",
            str(cli),
            "--mode",
            "json",
            "--provider",
            "fixture",
            "--model",
            "fixture",
            "--thinking",
            "off",
            "--no-tools",
            "--no-extensions",
            "--no-mcp",
            "--no-skills",
            "--no-prompt-templates",
            "--no-context-files",
            "--no-session",
            "--no-approve",
            "--offline",
            "--system-prompt",
            "You are a fixture client.",
            prompt,
        ]
    else:
        config = home / "codex"
        config.mkdir()
        env["CODEX_HOME"] = str(config)
        (config / "config.toml").write_text(
            'model = "fixture"\nmodel_provider = "fixture"\n'
            '[model_providers.fixture]\nname = "Fixture"\n'
            f'base_url = "{url}/v1"\nwire_api = "responses"\n'
            "requires_openai_auth = false\n"
            f"request_max_retries = {1 if retry_enabled else 0}\n"
            f"stream_max_retries = {1 if retry_enabled else 0}\n"
            "supports_websockets = false\n"
        )
        argv = [
            str(cli),
            "exec",
            "--json",
            "--ephemeral",
            "--skip-git-repo-check",
            "-s",
            "read-only",
            "-c",
            'approval_policy="never"',
            prompt,
        ]
    version_argv = (
        ["/usr/bin/node", str(cli), "--version"] if harness == "pi" else [str(cli), "--version"]
    )
    version = subprocess.run(
        version_argv, env=env, cwd=work, capture_output=True, text=True, timeout=10, check=True
    ).stdout.strip()
    if version != VERSIONS[harness]:
        raise RuntimeError(f"Expected {VERSIONS[harness]!r}, got {version!r}")
    timed_out = False
    with (root / "stdout.jsonl").open("wb") as out, (root / "stderr.log").open("wb") as err:
        process = subprocess.Popen(
            argv,
            cwd=work,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=err,
            start_new_session=True,
        )
        try:
            process.wait(timeout=35)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)
    result = {
        "harness": harness,
        "version": version,
        "scenario": scenario,
        "returncode": process.returncode,
        "timed_out": timed_out,
        "requests": sequence,
        "argv": argv,
    }
    (root / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps({k: v for k, v in result.items() if k != "argv"}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("destination", type=Path)
    parser.add_argument("--harness", choices=["codex", "claude", "pi"], required=True)
    parser.add_argument(
        "--scenario",
        choices=["success", "quota", "billing", "recover", "auth", "exhausted"],
        required=True,
    )
    parser.add_argument("--cli", type=Path, required=True)
    args = parser.parse_args()
    probe(args.harness, args.scenario, args.destination.resolve(), args.cli)
