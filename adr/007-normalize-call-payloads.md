# 007 — Share exact call payloads within one run

Status: accepted
Decision date: 2026-11-19
Recorded: 2026-11-19

A consumer corpus showed 24.46 MiB of declared input text and 11.36 MiB when
counted once per distinct value in each run. This measures logical repetition,
not Git pack sizes or a throughput improvement. Embedding these strings also
rewrote them on each reservation, start and completion.

Use one local `payloads/<sha256>` per exact UTF-8 byte string used as a task,
input or committed output. The format-4 manifest records named references;
work identity and delivery integrity still cover the original logical material.
Publish and flush payloads before atomically replacing the manifest. Only the
exclusive controller writes; a lock-free reader validates one manifest and the
content it names. A failed transition can leave an orphan, not a reusable call.
No online collection or repair: preserve unresolved reservations and fail on
missing or corrupt evidence. These hashes do not authenticate an untrusted run.

Worker files remain independent, writable per-launch materializations. Failed
launch files, expanded prompts, external native logs, caller acceptance and
publication policy have distinct meanings; none are substitutes for a delivery.
Run copies need their referenced payloads. A cross-run store would entangle
copy, lifetime and cleanup policies; the measured within-run reuse does not
require it. There is no historical migration or runtime dependency on Commonplace.

Reconsider the whole-manifest or eager-validation costs only with workload
evidence. Caller-produced artifacts are outside this decision.
