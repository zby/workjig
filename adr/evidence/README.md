# Decision evidence

Store raw material and longer analyses here when they help explain or revisit
an architectural choice. Keep the short decision and its implications in the
[ADR](../README.md).

Use descriptive topic directories, for example:

```text
adr/evidence/<topic>/
  README.md
  <logs, measurements, analysis, or reproduction files>
```

A small investigation can be a single Markdown file. Evidence for an open
question does not need an ADR number; link it from the decision if one follows.

For each bundle, record enough context to interpret it later:

- The question, collection date, and relevant code revision or tool versions.
- Where the material came from, and whether it is raw, excerpted, sanitized,
  synthetic, or a summary.
- What was actually checked, the result, and what remains unverified.
- Reproduction commands when useful, including limits and prerequisites for
  any credential-using probe.

Keep credentials, tokens, private prompts, and personal data out of committed
evidence. Preserve useful context when sanitizing and describe the omissions.
Link large external artifacts with a stable identifier or checksum rather than
adding unrelated dumps. A reproduction script is supporting evidence, not a
second runtime or a substitute for maintained tests.

## Investigations

- [Terminal failures](terminal-failures/README.md): version-scoped Codex,
  Claude Code, and Pi completion signals, quota-related fields, and exit behavior.
  Includes [local-provider probes](terminal-failures/local-probes.md), raw CLI
  captures, and a bounded reproduction script.

## Existing evidence

These materials remain at their current locations; link rather than duplicate:

- [Harness evidence](../../docs/harness-evidence.md): version-scoped interface
  research, bounded real probes, and the usage-limit investigation.
