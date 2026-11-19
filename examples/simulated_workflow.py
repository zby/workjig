"""Run a script-backed simulated harness using the normal Workjig runtime."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from workjig import CommandWorker, Request, Run


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("record_dir", type=Path)
    parser.add_argument("workdir", type=Path)
    parser.add_argument("--outcome", choices=("deliver", "unable"), default="deliver")
    args = parser.parse_args()

    worker = CommandWorker(
        argv=(
            sys.executable,
            str(Path(__file__).with_name("simulate_harness.py").resolve()),
            "--prompt",
            "{prompt}",
            "--inputs",
            "{input_dir}",
            "--outputs",
            "{output_dir}",
            "--problem",
            "{problem_file}",
            "--evidence",
            "{evidence_file}",
            "--launch-dir",
            "{launch_dir}",
            "--outcome",
            args.outcome,
        ),
        name="simulated-harness",
    )
    # Mixed newlines make a text-mode reserialization distinguishable from a copy.
    source = "example source\r\nsecond line\n"
    request = Request(
        call_id="copy",
        task="Copy source.txt to result.txt and uppercase it into edited.txt in the workdir.",
        inputs={"source.txt": source},
        required_outputs=("result.txt",),
        worker=worker,
        timeout_seconds=10,
    )
    with Run(args.record_dir, args.workdir) as run:
        result = run.call(request)
        # Acceptance belongs here, not inside the runtime or its worker.
        accepted = result.status == "delivered" and result.outputs["result.txt"] == source
        print(
            json.dumps(
                {
                    "status": result.status,
                    "accepted": accepted,
                    "reused": result.reused,
                    "launch_dir": result.launches[-1].launch_dir,
                }
            )
        )
    if not accepted:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
