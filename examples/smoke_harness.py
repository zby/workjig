"""Manual real-model file-copy and cache-reuse probes for one or all harnesses."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from workjig import Request, Run
from workjig.adapters import claude_worker, codex_worker, parse_evidence, pi_worker

DEFAULT_MODELS = {"codex": "gpt-6-luna", "claude": "haiku", "pi": "luna"}


def probe(harness: str, workdir: Path, model: str) -> dict[str, object]:
    workdir.mkdir(parents=True, exist_ok=True)
    if harness == "codex":
        worker = codex_worker(model, sandbox="workspace-write", effort="low")
    elif harness == "claude":
        worker = claude_worker(model, allowed_tools=("Read", "Write"))
    else:
        worker = pi_worker(
            model,
            provider="openai",
            tools=("read", "write"),
            project_trust="no-approve",
        )
    request = Request(
        call_id="copy-input",
        task=(
            "Read source.txt from the listed input path. Copy its contents exactly to "
            "answer.txt at the listed output path. Do not add commentary or formatting "
            "to the file. This is the entire task."
        ),
        inputs={"source.txt": "headless-probe-7391\n"},
        required_outputs=("answer.txt",),
        worker=worker,
        timeout_seconds=180,
    )
    report = {
        "harness": harness,
        "requested_model": model,
        "records": str(workdir / "records"),
    }
    with Run(workdir / "records", workdir) as run:
        result = run.call(request)
        report.update(status=result.status, launches=len(result.launches))
        if result.status != "delivered":
            return report
        if result.outputs["answer.txt"] != request.inputs["source.txt"]:
            raise ValueError("delivered output does not match source.txt")
        repeated = run.call(request)
        if not (
            repeated.reused
            and len(repeated.launches) == len(result.launches)
            and repeated.launches[-1].launch_id == result.launches[-1].launch_id
        ):
            raise ValueError("repeat call did not reuse the committed launch")
    attempt = result.launches[-1]
    report.update(
        reused=repeated.reused,
        evidence=parse_evidence(
            harness,
            (Path(attempt.launch_dir) / "stdout.log").read_text(encoding="utf-8"),
        ),
    )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("harness", choices=(*DEFAULT_MODELS, "all"))
    parser.add_argument("workdir", type=Path)
    parser.add_argument(
        "--model",
        help="override a single harness's default (codex: gpt-6-luna; claude: haiku; pi: luna)",
    )
    args = parser.parse_args()
    if args.harness == "all" and args.model is not None:
        parser.error("--model requires a single harness, not 'all'")
    root = args.workdir.resolve()
    harnesses = tuple(DEFAULT_MODELS) if args.harness == "all" else (args.harness,)
    failed = False
    for harness in harnesses:
        workdir = root / harness if args.harness == "all" else root
        model = args.model if args.model is not None else DEFAULT_MODELS[harness]
        try:
            report = probe(harness, workdir, model)
        except Exception as exc:
            report = {
                "harness": harness,
                "requested_model": model,
                "records": str(workdir / "records"),
                "status": "probe_error",
                "error": f"{type(exc).__name__}: {exc}",
            }
        print(json.dumps(report, sort_keys=True), flush=True)
        failed |= report["status"] != "delivered"
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
