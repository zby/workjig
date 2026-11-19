"""Credential-free example worker, not an emulator of a specific harness CLI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prompt", required=True, type=Path)
    parser.add_argument("--inputs", required=True, type=Path)
    parser.add_argument("--outputs", required=True, type=Path)
    parser.add_argument("--problem", required=True, type=Path)
    parser.add_argument("--evidence", required=True, type=Path)
    parser.add_argument("--launch-dir", required=True, type=Path)
    parser.add_argument("--outcome", choices=("deliver", "unable"), default="deliver")
    args = parser.parse_args()

    # Read the real launch files, not a test-only reconstruction of the task.
    prompt = args.prompt.read_text(encoding="utf-8")
    if not prompt.strip():
        raise ValueError("empty task prompt")
    source = (args.inputs / "source.txt").read_bytes()

    # This deliberately synthetic session file is distinct from stdout and the
    # optional evidence object collected by Workjig. It claims no native schema.
    session = args.launch_dir / "synthetic-session.jsonl"
    event = {"type": "simulation", "outcome": args.outcome, "prompt": str(args.prompt)}
    session.write_text(json.dumps(event) + "\n", encoding="utf-8")
    args.evidence.write_text(
        json.dumps({"simulated": True, "session_file": str(session)}), encoding="utf-8"
    )
    print(json.dumps(event))
    print("simulated diagnostic", file=sys.stderr)

    if args.outcome == "unable":
        args.problem.write_text(
            json.dumps({"kind": "unable", "message": "Simulated inability to complete"}),
            encoding="utf-8",
        )
        return

    # The working directory is shared, not sandboxed or rolled back by Workjig.
    Path("edited.txt").write_text(source.decode("utf-8").upper(), encoding="utf-8", newline="")
    (args.outputs / "result.txt").write_bytes(source)


if __name__ == "__main__":
    main()
