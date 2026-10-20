# Workjig contributor handoff

Workjig provides the runtime foundation for Commonplace's workflow engine.
Keep the package independently maintained, usable and testable. Commonplace
owns its workflows and domain policy; changes to Commonplace belong in that
repository.

Read [AGENTS.md](AGENTS.md) for contributor rules, [README.md](README.md) for
purpose and the public API, [the execution contract](docs/design.md) for
runtime guarantees, and [harness evidence](docs/harness-evidence.md) for the
scope of tested adapter support. Run the checks in AGENTS.md in the existing
development environment; use fresh environments for built-distribution tests.

Do not infer current test counts or support from old handoffs or local review
reports. Use the current checks and retained evidence. Change history belongs
in commits.

Ask before changing the public API or record format, settling open design
choices, using credentials for real model probes, publishing, or bumping the
version. Escalating backoff and broader adapter compatibility checks remain
possible follow-up work, not approved requirements.
