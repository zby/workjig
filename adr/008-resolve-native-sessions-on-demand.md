# 008 — Resolve native transcript paths on demand through RunView

Status: accepted  
Decision date: 2026-11-19

[ADR 006](006-locate-native-sessions.md) bound each receipt to a physical
launch. Its Codex observer also started a metadata helper. An unconfirmed
helper stop could prevent the controller from committing a finished worker's
outcome. Ignoring that exception would obscure process ownership. Requiring
each audit caller to locate a transcript would spread harness-specific
instructions across workflows.

Keep the observed thread ID in the Codex receipt. The controller can settle
the worker independently of transcript availability. The common audit entry
point is `RunView.native_session(launch, resolve=True)`. A recorded location goes
through the existing store and file checks. For an ID-only Codex receipt,
the reader queries the exact thread under the pinned store and applies the
same checks to the reply. The helper uses the reader's Codex installation
and a temporary working directory so a copied run can be inspected after
the original workspace disappears. The run record supplies neither a program
to execute nor an environment to replay.

Resolution returns the existing session status and reason on lookup or
cleanup failure. Each call can observe changes to the external store.
There is no writeback. Without `resolve=True` the same method requires only
the records and filesystem and starts nothing. When it finds an ID whose
transcript only the harness can report, it raises `UnresolvedSessionError`
naming the parameter, so an undetermined answer is never mistaken for an
unavailable transcript. Revisit the helper if Codex adds
a transcript path to the worker's public output.

## Evidence

- In one bounded live probe on Linux with `codex-cli 0.170.0` (2026-11-19), a
  tracked `codex exec` launch was delivered with a receipt holding the thread
  ID and no location. Read from a copy of the run, the lookup without
  `resolve=True` raised `UnresolvedSessionError`; with it, each of two calls
  returned `located` in about 0.3 seconds, naming an existing
  `sessions/2026/11/19/rollout-<start time>-<id>.jsonl` under the pinned
  store. The receipt was unchanged afterwards and no helper process remained.
- This is one version on one platform. The helper's `thread/read` reply path is
  still the unstable field described in ADR 006. Windows was not probed.

## Accepted limits

- The recorded store path selects the `CODEX_HOME` the reader's Codex runs
  with, so resolving executes Codex under configuration the run record points
  to. What `codex app-server` loads from that directory at startup has not been
  established. Resolve only runs and stores the reader trusts.
- A Codex `sessions` directory that is a symlink is reported as unavailable,
  because its target's parent would otherwise be taken for the Codex home.
- Without `resolve=True`, a Codex launch with a known ID raises instead of
  yielding a path.
