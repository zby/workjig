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
point is `RunView.resolve_native_session(launch)`. A recorded location goes
through the existing store and file checks. For an ID-only Codex receipt,
the reader queries the exact thread under the pinned store and applies the
same checks to the reply. The helper uses the reader's Codex installation
and a temporary working directory so a copied run can be inspected after
the original workspace disappears. The run record supplies neither a program
to execute nor an environment to replay.

Resolution returns the existing session status and reason on lookup or
cleanup failure. Each call can observe changes to the external store.
There is no writeback. Passive inspection through `native_session` still
requires only the records and filesystem. Revisit the helper if Codex adds
a transcript path to the worker's public output.
