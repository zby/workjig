# Execution-control implementation evidence

Collected: 2026-10-20. Baseline revision:
`8f607a0c068a21385cdabbff27ced4d3566ec713`.

Supports [ADR 003](../../003-separate-execution-controls-from-work-identity.md).
The [proposal](../execution-policy-and-continuation-proposal.md) preserves the
reviewed design. This evidence concerns deterministic runtime behavior, not new
real-harness support or model quality.

## Source size

Generated using David A. Wheeler's SLOCCount 2.26 for `src`:

| Measurement | Python physical SLOC |
| --- | ---: |
| Before | 1,288 |
| After | 1,356 |
| Change | +68 (+5.3%) |

Raw output: [before](sloccount-before.txt), [after](sloccount-after.txt).
These are source-size measurements, not effort/cost estimates; ignore the
COCOMO projections emitted by the tool.

The initial pre-change command failed because its private `--datadir` did not
exist. The baseline was reconstructed from `git show HEAD:src/workjig/*.py` into
a private temporary directory after creating the data directory. `src` had no
pre-existing changes, so this is the pre-implementation source. The post count
used the working `src` directory; generated egg-info contributes zero SLOC.

Reproduction (use private scratch paths and create data directories first):

```sh
scratch=$(mktemp -d)
mkdir -p "$scratch/before/src/workjig" "$scratch/data-before" "$scratch/data-after"
for path in src/workjig/*.py; do
    git show "8f607a0c068a21385cdabbff27ced4d3566ec713:$path" \
        > "$scratch/before/$path"
done
sloccount --datadir "$scratch/data-before" "$scratch/before/src"
sloccount --datadir "$scratch/data-after" src
```

## Deterministic verification

`tests/test_continuation.py` exercises replay under changed controls, timeout
continuation, total-ceiling extension, retry selection/backoff, usage-stop
approval, ordinary retries after approval, and interrupted approval reservation
recovery with deterministic workers and isolated files. The injected interruption
occurs before worker start; the recovery acknowledgment therefore does not assert
termination of an unobserved process.

`tests/test_continuation_validation.py` validates controls and authorization
relationships directly without subprocesses. Existing tests continue to cover
process ownership, parallel settlement, persistence failures, and file delivery.

Final checks passed: `uv sync --locked`, `uv run pytest -q` (277 passed),
`uv run ruff check .`, `uv run ruff format --check .`, and `uv build`.
The build emitted a setuptools deprecation warning for the existing license
metadata format but produced both distribution artifacts successfully.

Independent Claude Code 2.1.304 sessions, requested model alias `opus`, performed
an [implementation review](implementation-review.txt) and a
[follow-up verification](followup-review.txt). The first ran the then-current
full suite (272 passed) and found no blockers, but recommended avoiding empty
history creation on invalid approval. That issue was fixed and regression tests
were added. The follow-up found no remaining concrete defects and independently
ran the continuation, validation, and outcome tests (94 passed). Reviewer lint
commands were permission-denied; the implementing session ran all project checks
as listed above. The served model was not verified.

Review outputs are retained verbatim; their pending-status observations refer
to the documents at review time, not the final acceptance state.
