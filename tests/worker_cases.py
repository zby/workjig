"""Plain request builders and cleanup for owned test processes."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

from workjig import (
    CommandWorker,
    Request,
    RetryPolicy,
)


def _kill(pid: int, *, group: bool = False) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, check=False)
        return
    try:
        (os.killpg if group else os.kill)(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _worker(script: Path, *args: str) -> CommandWorker:
    return CommandWorker(
        (
            sys.executable,
            str(script),
            "{prompt}",
            "{input_dir}",
            "{output_dir}",
            "{problem_file}",
            *args,
        )
    )


def _request(
    worker: CommandWorker,
    *,
    timeout: float = 2,
    max_launches: int = 1,
    retry: RetryPolicy | None = None,
) -> Request:
    return Request(
        "job",
        "Read input and write output.",
        {"input.txt": "payload"},
        ("result.txt",),
        worker,
        timeout_seconds=timeout,
        max_launches=max_launches,
        retry=retry or RetryPolicy(),
    )
