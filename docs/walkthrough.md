# RSILoop Campaign 2 Worker Remediation

This walkthrough describes the implemented candidate-worker boundary. It does not claim that source inspection is a security boundary: candidate Python is treated as untrusted and controlled by operating-system containment, irreversible resource limits, and a phase-aware supervisor.

## Worker lifecycle

1. The trusted host imports the task harness.
2. The host writes `candidate.py`, the fixed worker shim, and a deny-default `sandbox-exec` profile into a private temporary directory.
3. The worker starts with a sanitized environment and installs `RLIMIT_CPU`, `RLIMIT_NOFILE`, `RLIMIT_FSIZE`, and `RLIMIT_CORE`. (Note: process address-space ceilings are unconstrained by `setrlimit` at the OS layer on macOS XNU kernel).
4. The worker emits `ready` with the applied limits and `candidate_imported: false`.
5. The host verifies that attestation. Only then does it send `load_candidate`.
6. Candidate import and harness-driven calls run under one candidate wall-clock budget and the worker's CPU limit.

The protocol deliberately separates three phases:

| Phase | Wall timeout classification | Unexpected exit / Signal classification |
|---|---|---|
| Worker startup/readiness | Infrastructure failure; calibration is invalid | Infrastructure failure |
| Candidate import | Candidate failure (`CandidateTimeoutError`), scored 0/10 | Phase-attributed candidate failure (`CandidateProcessError`: non-`SIGXCPU` signal/exit like `SIGABRT` or `SIGSEGV`), scored 0/10 |
| Candidate execution | Candidate failure (`CandidateTimeoutError`), scored 0/10 | Phase-attributed candidate failure (`CandidateProcessError`: non-`SIGXCPU` signal/exit like `SIGABRT` or `SIGSEGV`), scored 0/10 |

Phase-based exit attribution classifies any non-`SIGXCPU` worker process exit occurring after `load_candidate` as a candidate process failure (`CandidateProcessError`) scoring 0/10.

## Residual Host Memory Availability Risk

Process address-space ceilings (`RLIMIT_AS`) cannot be lowered below `RLIM_INFINITY` via `setrlimit` on macOS XNU kernel. While candidate CPU time, descriptors, file sizes, and core dumps are strictly constrained at the OS layer, address space remains unconstrained by `setrlimit`. A memory-bound candidate process can create host memory pressure or stall before wall-clock supervision terminates the worker. This residual host-availability risk is explicitly registered and accepted for the frozen, non-adversarial benchmark population.

## Containment boundary

The macOS sandbox denies process forking and network access. Read access is limited to the candidate directory, Python/runtime locations, required system libraries, and `/private/var` runtime data. The repository and `/private/etc` are not in the read allowlist. Writes are limited to the candidate directory and `/dev/null`.

The candidate and worker shim still share one Python interpreter, so the IPC protocol is not an authenticity boundary against deliberate in-process introspection. The supported security claim is host containment, not isolation of the worker shim from candidate code.

## Candidate semantics

Campaign 2 no longer applies a binding-aware AST denylist. The former abstract interpreter was incomplete and introduced a hidden language-policy bias while providing no reliable containment. Imports and ordinary Python semantics are allowed; resource consumption and prohibited host effects are enforced at runtime.

## Branch Topology & Preflight Manifest Authentication Scope

- **Branch Topology**: Development is maintained on branch `campaign2-work-order-a-rebuild` directly above pinned base commit `1abb708afc8dae8c90a2aa57e9f6d8a81a8a0bae`.
- **Preflight Manifest (`campaign2_preflight_hashes.json`)**: Authenticates all 128 Category 1 campaign runtime files (including `campaign2/proxy.py`, `campaign2/manifest.py`, `campaign2/tier_assignment.py`, `campaign2/scorer.py`, `campaign2/defects.py`, `docs/walkthrough.md`, `README.md`, and all 30 benchmark task files).
- **Git & Evidence Authentication**: Test launcher `test_campaign2.py` and evaluation evidence files are authenticated via Git commit hash and `campaign2_evidence_hashes.json`.

## Verification

The regression suite directly checks that:

- a candidate top-level marker does not exist at `ready` and appears only after `load_candidate`;
- a real CPU-bound worker exits via `SIGXCPU` under `RLIMIT_CPU`;
- candidate process signal terminations (e.g. `SIGABRT`) explicitly raise `CandidateProcessError` with returncode `-6` and score 0/10 as candidate failures;
- repository reads, `/etc/passwd` reads, and loopback socket binding are denied;
- startup waits are infrastructure failures, while import/execution waits are candidate failures;
- the previously demonstrated `typing.sys.modules`, dynamic `__import__`, and `asyncio` waits all score 0/10;
- all 30 reference solutions retain 10/10 scores.

The sealed preflight manifest authenticates the current implementation and this documentation. It is regenerated only as part of an operator-reviewed source change.
