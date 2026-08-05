# RSILoop: Host-Contained Recursive Self-Improvement Experiment

A bi-level prompt-optimization harness that uses the local **Codex CLI** (remote API model inference) as both the candidate solution generator and the meta-optimizer. The harness evaluates candidate solutions in a macOS `sandbox-exec` jail, then feeds failure traces to the optimizer to mutate system prompts across three independent trials.

## Current Status — Campaign 2 Work Order B (Orchestration & Launch Preflight)

**Status: NO-GO pending adversarial review and an operator-issued launch lock. No Sol calls are authorized.**
- **Active Base Commit**: Pinned to base commit `96e04c94ef44b974301bbe9e4fedd2ba4281022d`.
- **CLI Integration**: Wired into `rsi_loop.py`; resume requires both `--resume` and an exact sealed `--resume-attempt-id run_<id>`.
- **Model Interface**: Pluggable `ModelProvider` architecture supporting `MockModelProvider` (vertical slice testing) and `SolModelProvider` (Work Order C production stub).
- **Per-Trial Call Arithmetic**: Fully enforced per-trial call counts (202 for Suite C full-run, 86 for Suite P pilot) across baseline, mutation, winner selection, and post-selection microblock trios.

**Operator Amendment — Legacy Model Deletion**:
The 491 MB Qwen GGUF model (`models/Qwen2.5-Coder-0.5B-Instruct-Q4_K_M.gguf`) and active model calibration paths have been deleted as an explicit operator amendment. Model-based difficulty stratification is completely withdrawn and replaced with deterministic, machine-recomputable, family-blocked tier assignment (`campaign2_tier_assignment.json`). No local or cloud difficulty-proxy model is required.

### Registered Report Language & Principles
- Tier assignment is balanced by structural task FAMILY, not by measured difficulty. Families are not difficulty strata.
- The primary comparison remains paired on the REALIZED confirmatory tasks; the pairing is unaffected by assignment.
- Difficulty imbalance across tiers may reduce statistical power and complicates interpretation of a null result.
- Generalization from the eight confirmatory tasks to the full 16-task population is limited.
- The Qwen 0.5B proxy was inadequate; model proxies in general were not shown to be invalid.

---

## Threat Model & Host Containment

- **Process Isolation**: The candidate solution and the runner communication shim share a Python interpreter and process. Therefore, protocol authenticity is not guaranteed against active exploitation by candidate code (e.g., introspection to locate control descriptors).
- **Correlation**: Request UUIDs provide stale-frame and accidental output pollution protection, not protocol cryptographic authenticity.
- **Boundary**: The achieved boundary is **host containment** (preventing arbitrary filesystem writes, network operations, or process spawning on the host using macOS `sandbox-exec` and `setrlimit`).
- **Resource Enforcement**: The Campaign 2 worker installs CPU (`RLIMIT_CPU`), file-descriptor (`RLIMIT_NOFILE`), file-size (`RLIMIT_FSIZE`), and core-dump (`RLIMIT_CORE`) limits before it reports readiness. The host rejects readiness unless the worker attests expected limits and confirms candidate import has not started.
- **Residual Host-Availability Risk**: Process address-space ceilings (`RLIMIT_AS`) are unconstrained by `setrlimit` at the OS layer on the macOS XNU kernel. A memory-bound candidate process can create host memory pressure or stall before wall-clock supervision terminates the process. This residual availability risk is accepted for the frozen, non-adversarial benchmark population.
- **Phase-Based Timeout & Exit Attribution**: Startup/readiness wall timeouts or process crashes are infrastructure failures. A non-exiting candidate import/execution wall timeout raises `CandidateTimeoutError` and invalidates the campaign attempt because it is not attributable to a scored outcome. Explicit worker exits and signals during candidate phases remain phase-attributed candidate failures.
- **Source Policy**: Campaign 2 applies no AST denylist. Python semantics are not selectively biased by a hidden import/call policy; containment and budgets are enforced at runtime.

---

## Architecture

```
campaign2/proxy.py host supervisor  (trusted)
  └── generated worker.py + candidate.py  (deny-default macOS sandbox)
```

The worker completes its RLIMIT-backed READY handshake before the host sends the explicit `load_candidate` request.

### Key files

```
├── benchmarks/
│   ├── campaign2/                # Campaign 2 30-task benchmark population
│   │   ├── suite_c/              # 22 Suite C tasks (F1, F2, F3, F4)
│   │   └── suite_p/              # 8 Suite P tasks (F1, F2, F3, F4)
│   ├── circular_buffer/          # Legacy optimization task
│   └── huffman_coding/           # Legacy optimization task
├── campaign2/
│   ├── proxy.py                  # Sandboxed candidate worker IPC & host supervisor
│   ├── manifest.py               # Preflight manifest & sealed evidence verifier
│   ├── attempt_identity.py       # Sealed exact-attempt resume identity
│   ├── durability.py             # Crash-recoverable exact-head checkpoints
│   ├── orchestrator.py           # Randomized trials and task-labelled Schema 3 evidence
│   ├── tier_assignment.py        # SHA-256 rejection sampling family-blocked tier assignment
│   ├── scorer.py                 # Task scoring & evaluation utilities
│   └── defects.py                # Benchmark task defect ladder verifier
├── campaign2_archive/            # Archived audit records (read-only 0o444)
├── campaign2_parity_matrix.json  # 120-row sealed parity matrix (0o444)
├── campaign2_tier_assignment.json# Family-blocked tier assignment artifact (0o444)
├── campaign2_evidence_hashes.json# Sealed scientific evidence manifest (0o444)
├── evaluator.py                  # Host supervisor: RunnerProxy, MRO registry, class/instance caching
├── rsi_loop.py                   # Orchestrator: smoke+canary, three trials, promotion gate, RunContext
└── run_tests.py                  # Strict combined test launcher (enforces zero skips, zero warnings)
```

---

## Getting Started

### Requirements

- macOS (sandbox-exec required)
- Python 3.11+

### Branch Topology & Verification

Scientific launch remains blocked until an operator-reviewed launch lock authenticates the exact final Git commit.

### Run the test suite

Run the strict test launcher from outside any sandbox (sandbox-exec calls require host access). The launcher enforces zero skips, zero warnings, and returns nonzero on any warnings, unraisable exceptions, skips, failures, or errors:

```bash
python3 run_tests.py
```
