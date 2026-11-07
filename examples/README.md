# Examples

## Ordinary-file workflow

`ordinary_files.py` runs two independent producers in parallel, freezes their
returned contents, then checks a combined file against that snapshot. The
deterministic worker makes the first combined file wrong so the workflow sends
one bounded correction with the prior candidate and feedback. All decisions
about acceptance, correction order, and stopping live in this example.

From an environment with `workjig` installed, create an ordinary
working directory and source files, then run:

```sh
mkdir -p /tmp/ordinary-work
printf 'alpha\n' > /tmp/ordinary-work/left-source.txt
printf 'beta\n' > /tmp/ordinary-work/right-source.txt
python examples/ordinary_files.py /tmp/ordinary-records /tmp/ordinary-work --stop-after-producers
python examples/ordinary_files.py /tmp/ordinary-records /tmp/ordinary-work
```

The second command resumes with the two delivered producer results. A third
identical command rechecks their contents and reuses every delivered result.
`invocations.log` in the working directory shows each actual subprocess
launch. Set `--max-corrections 0` to stop after the rejected first combination.
Changing a declared source file causes its producer to launch again; callers
must ensure any undeclared environmental dependencies remain valid for reuse.

## Script-backed simulated harness

Use a normal `CommandWorker` to replace model execution with a deterministic
script. No harness installation, credentials, callback API or alternate runner
is needed:

```sh
mkdir -p /tmp/simulated-work
uv run python examples/simulated_workflow.py /tmp/simulated-records /tmp/simulated-work
uv run python examples/simulated_workflow.py /tmp/simulated-records /tmp/simulated-work
```

The first command launches `simulate_harness.py`; the second reuses the committed
delivery. Use your own fresh paths for independent tests. The driver supplies
the script with the actual prepared prompt and per-launch paths through command
arguments. It runs in the supplied working directory, with normal process
ownership, launch accounting, timeout handling and result collection.

The script demonstrates:
- Reading the prepared prompt and declared `source.txt` input.
- Delivering `result.txt` through the ordinary output-file protocol.
- Editing `edited.txt` in the working directory.
- Emitting captured `stdout.log` and `stderr.log` content.
- Writing a synthetic session fixture and a separate `evidence.json` object
  pointing to it.

The session fixture is deliberately **not** a Claude, Codex or Pi session schema.
It demonstrates where a test can write such an artifact, not native-session
compatibility. Workjig collects the evidence object but does not interpret or
automatically collect arbitrary session files as returned outputs. Declare a
session file as an output if its contents must be delivered.

To demonstrate an ordinary worker problem, use fresh records:

```sh
uv run python examples/simulated_workflow.py /tmp/simulated-unable-records /tmp/simulated-work --outcome unable
```

This prints `worker_problem` and exits 1 because the caller did not accept a
result. The simulator itself exits zero after writing `problem.json`; Workjig
classifies the problem normally. There are no implicit retries or corrections.

Adapt the script to your workflow's declared inputs and outputs. Keep scenario
selection explicit in arguments, environment overrides or declared input
contents. Script contents and ambient fixture files are not automatically
fingerprinted: use fresh run directories when changing them, or include their
relevant contents/fingerprints in declared inputs. Reuse does not rerun the
script or restore its working-directory edits. External effects are neither
sandboxed nor transactional; keep tests in private temporary directories.

### Substituting a concrete harness executable

The universal script above uses a generic command protocol; it does not accept
the actual CLI arguments of the three built-in adapters. For an adapter-specific
test, replace only its executable prefix and retain its remaining arguments
and output protocol:

```python
import sys
from dataclasses import replace
from pathlib import Path

from workjig.adapters import codex_worker

real = codex_worker("test-model", sandbox="workspace-write")
fake = replace(
    real,
    argv=(sys.executable, str(Path("fake_codex.py").resolve()), *real.argv[1:]),
)
```

`fake_codex.py` is a test-owned script you supply, not `simulate_harness.py`.
It must accept the adapter's real argument shape (including its task-file
pointer), implement the needed file effects, and emit representative Codex
events on stdout. It should reject unexpected arguments so invocation changes
are visible. The same replacement technique works for Claude and Pi factories;
their arguments and terminal-event schemas differ. No executable-override API
is required.

Such tests exercise invocation construction and classification against an
emulation. They do not establish current real-harness compatibility, sandbox
enforcement, or provider/model behavior. Native session fixtures must match the
consumer being tested and should be labeled with their source/schema.
See [harness evidence](../docs/harness-evidence.md) for actual probes.
