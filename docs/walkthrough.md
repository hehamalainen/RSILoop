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
| Candidate import | Unattributed non-exiting wait; campaign attempt is invalid | Phase-attributed candidate failure (`CandidateProcessError`: non-`SIGXCPU` signal/exit like `SIGABRT` or `SIGSEGV`), scored 0/10 |
| Candidate execution | Unattributed non-exiting wait; campaign attempt is invalid | Phase-attributed candidate failure (`CandidateProcessError`: non-`SIGXCPU` signal/exit like `SIGABRT` or `SIGSEGV`), scored 0/10 |

Phase-based exit attribution classifies any non-`SIGXCPU` worker process exit occurring after `load_candidate` as a candidate process failure (`CandidateProcessError`) scoring 0/10.
It does not attribute a worker that remains alive but stops producing protocol output. Consistent with registered Limitation 10, that wall-clock timeout invalidates the campaign attempt.

## Orchestration recovery and evidence

Every trial is preceded by a sealed pre-trial intent. After evaluation, the commit intent contains the exact canonical bytes and SHA-256 hash of the next checkpoint. Startup rolls forward only a transition proven by that intent; an unexplained pre-trial intent, missing checkpoint, missing anchor, fork, or rollback invalidates the attempt rather than resampling work.

CLI resume requires an explicit `--resume-attempt-id`. The named run directory contains a sealed, hash-anchored identity binding the attempt ID, canonical directory, stage, mock mode, registration hash, checkpoint, and ledger. Checkpoint recovery and intent/completion ledger admission both finish before provider construction.

Schema 3 evidence is emitted by the orchestrator with real task IDs. Each trial carries tier deltas, baseline/optimizer/control k-sample arrays, and control call records. Structural family F1–F4 membership is parsed from task IDs rather than inferred from optimization tiers. Evaluator error traces are preserved in call records and supplied to optimizer mutations for failed tasks.

## Residual Host Memory Availability Risk

Process address-space ceilings (`RLIMIT_AS`) cannot be lowered below `RLIM_INFINITY` via `setrlimit` on macOS XNU kernel. While candidate CPU time, descriptors, file sizes, and core dumps are strictly constrained at the OS layer, address space remains unconstrained by `setrlimit`. A memory-bound candidate process can create host memory pressure or stall before wall-clock supervision terminates the worker. This residual host-availability risk is explicitly registered and accepted for the frozen, non-adversarial benchmark population.

## Containment boundary

The macOS sandbox denies process forking and network access. Read access is limited to the candidate directory, Python/runtime locations, required system libraries, and `/private/var` runtime data. The repository and `/private/etc` are not in the read allowlist. Writes are limited to the candidate directory and `/dev/null`.

The candidate and worker shim still share one Python interpreter, so the IPC protocol is not an authenticity boundary against deliberate in-process introspection. The supported security claim is host containment, not isolation of the worker shim from candidate code.

## Candidate semantics

Campaign 2 no longer applies a binding-aware AST denylist. The former abstract interpreter was incomplete and introduced a hidden language-policy bias while providing no reliable containment. Imports and ordinary Python semantics are allowed; resource consumption and prohibited host effects are enforced at runtime.

## Branch Topology & Preflight Manifest Authentication Scope

- **Branch Topology**: Scientific launch remains blocked until an operator-reviewed launch lock authenticates the exact final Git commit.
- **Preflight Manifest (`campaign2_preflight_hashes.json`)**: Authenticates all 141 Category 1 campaign runtime files, including `rsi_loop.py`, attempt identity/recovery code, worker code, documentation, and all 30 benchmark task file sets.
- **Git & Evidence Authentication**: Test launcher `test_campaign2.py` and evaluation evidence files are authenticated via Git commit hash and `campaign2_evidence_hashes.json`.

## Verification

The regression suite directly checks that:

- a candidate top-level marker does not exist at `ready` and appears only after `load_candidate`;
- a real CPU-bound worker exits via `SIGXCPU` under `RLIMIT_CPU`;
- candidate process signal terminations (e.g. `SIGABRT`) explicitly raise `CandidateProcessError` with returncode `-6` and score 0/10 as candidate failures;
- repository reads, `/etc/passwd` reads, and loopback socket binding are denied;
- startup waits are infrastructure failures, while unattributed import/execution wall waits invalidate the campaign attempt;
- the previously demonstrated `typing.sys.modules`, dynamic `__import__`, and `asyncio` waits all score 0/10;
- all 30 reference solutions retain 10/10 scores.

The sealed preflight manifest authenticates the current implementation and this documentation. It is regenerated only as part of an operator-reviewed source change.
