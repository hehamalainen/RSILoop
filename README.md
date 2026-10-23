# RSILoop: Containment-Hardened Recursive Self-Improvement Research Prototype

**RSILoop** is an experimental evaluation, containment, and statistical-design prototype for studying **Level 1 Recursive Self-Improvement (RSI)** in LLM-based coding agents. It evaluates candidate code inside a deny-default macOS `sandbox-exec` jail with pre-execution `setrlimit` attestation, pairs failure traces with a meta-optimizer to mutate system prompts under an immutable prompt envelope, and archives cryptographically anchored run evidence.

---

## Project Overview & Current Release State

This repository contains four distinct, self-contained components:

1. **Campaign 1 (`rsi_loop.py`, `evaluator.py`, `candidate_runner.py`, `benchmarks/`)**:
   - The foundational bi-level prompt-optimization harness and host-supervised RPC proxy.
2. **Campaign 2 (`campaign2/`, `benchmarks/campaign2/`, `campaign_archive_manifest.json`, `campaign_claim_record.json`)**:
   - A 30-task, 4-family benchmark population (`suite_c`: 22 tasks, `suite_p`: 8 tasks) with 90 calibrated defect rungs (`campaign2_parity_matrix.json`), deterministic SHA-256 rejection-sampled family-blocked tier assignment (`campaign2_tier_assignment.json`), crash-recoverable checkpoint durability (`campaign2/durability.py`), and a 3-record append-only invocation ledger (`campaign2/attempt_lifecycle.py`).
   - **Recorded Campaign 2 Pilot Outcome**: The non-mock Suite P pilot (`history/run_1786134848_cfb514d2/campaign2_pilot_report.json`, sealed under `campaign_archive_manifest.json` and `campaign_claim_record.json`) terminated at the prespecified **`no_headroom`** stop rule (baseline solved 100% of pilot tasks at $1.0$, yielding $\Delta = 0.0$ across optimizer and control arms) and did **not** advance to the confirmatory Suite C run.
3. **Campaign 3 Statistical Design & Cost Algebra (`campaign3/`, `campaign3/sim/`)**:
   - Governance and preregistration drafting (`campaign3/governance.py`, `campaign3/verifier.py`, `campaign3/p1_verifier.py`), deterministic Monte Carlo simulation and two-stage moment calibration (`campaign3/sim/model_v2.py`, `campaign3/sim/calibrator_v2.py`, `campaign3/sim/quantizer.py`), and closed-form call/token cost algebra across all four protocol variants (`campaign3/cost_model.py`, `campaign3/cost_model_report.md`).
   - **Note on Campaign 2 vs. Campaign 3 Conformance**: The `campaign2/` directory retains the historical Campaign 2 archive verifier while defaulting new executions to Campaign 3 strict conformance: `parse_and_classify_jsonl_output(..., require_transport_evidence=True)`, `SolModelProvider(..., require_transport_evidence=True)`, and `is_permitted_local_retry(..., require_transport_evidence=True)` reject post-spawn CLI retries lacking transport-level zero-bytes-sent proof by default, and `Campaign2Orchestrator(..., atomic_parent_feedback=True, require_transport_evidence=True)` updates parent prompts, scores, and failure traces atomically only on accepted improvements (with `False` confined to explicit historical archive verification tests).
4. **Discovery Probe Sidecar (`discovery_probe/`)**:
   - A dependency-free, text-only active rule-discovery benchmark inspired by [DiG-bench](https://arxiv.org/abs/2608.12593). Episodes commit to a hidden composition of string operations via SHA-256 before exploration and score only locked holdout challenge predictions. See [`discovery_probe/README.md`](discovery_probe/README.md).

---

## Requirements & Installation

- **Operating System**: macOS (requires `/usr/bin/sandbox-exec` for Seatbelt containment profiles)
- **Python**: Python `3.11+` (tested on Python 3.11–3.14)
- **Runtime Dependencies**: `requirements.txt` pins `numpy==2.5.1` (the exact tested reproducibility constraint recorded in archived Campaign 3 evidence `campaign3/sim/moments_report.json`); broader NumPy versions (`numpy>=1.26.0` in `pyproject.toml`) are also supported with cross-version numerical verification in `test_campaign3_sim_model.py`.

Create a virtual environment, install dependencies, and run the offline environment & artifact verifier:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python verify_env.py --offline
```

---

## Model-Free Smoke Examples (Zero API Keys or Paid Inference Required)

All verification, simulation, and sidecar tools run completely offline without API keys or external network calls:

### 1. Play or Script the Active Rule-Discovery Sidecar (`discovery_probe`)

Run an interactive hidden-rule discovery episode (or pass `--reveal-rules` for the execution-control condition):

```bash
python3 -m discovery_probe --seed 42 --tier 2
```

Run a non-interactive programmatic smoke check:

```bash
python3 -c '
from discovery_probe import DiscoverySession, verify_result_commitment
session = DiscoverySession(seed=42, tier=2)
print("Commitment:", session.rule_commitment)
print("Probe abc =>", session.probe("abc"))
challenges = session.challenges()
guesses = [session._rule.apply(c) for c in challenges]
res = session.submit(guesses)
print("Solved:", res.solved, "Score:", res.score, "Commitment Valid:", verify_result_commitment(res))
'
```

### 2. Run Campaign 2 Mock Pilot Orchestration (Offline Vertical Slice)

Run the Campaign 2 orchestrator in offline mock mode (`MockModelProvider`) to exercise the full 3-phase trial pipeline (baseline, counterbalanced optimizer/control mutations, and post-selection microblock trios):

```bash
python3 -m unittest test_campaign2_orchestration.TestCampaign2Orchestrator.test_suite_p_pilot_call_arithmetic_86_calls -v
```

### 3. Evaluate Campaign 3 Cost Algebra & Simulation Engine

Inspect closed-form call and token budgets across the four Campaign 3 protocol variants:

```bash
python3 -m unittest test_campaign3_cost_model.py -v
```

---

## Running the Full Regression & Hygiene Suite

Because Git does not preserve read-only (`0444`) file modes across fresh clones, `run_tests.py` automatically invokes [`scripts/reseal_readonly_artifacts.py`](scripts/reseal_readonly_artifacts.py) to verify every sealed artifact's SHA-256 digest against its committed manifest/anchor before restoring `0444` permissions.

Run the complete verification suite (enforces zero skips, zero warnings, and zero unraisable exceptions):

```bash
python3 run_tests.py
```

Run the standalone release hygiene and allowlist verifier:

```bash
python3 scripts/check_release_hygiene.py
```

---

## Threat Model & Host Containment

- **Process Isolation**: In Campaign 1 (`candidate_runner.py`), the candidate solution and runner RPC shim share a Python process; in Campaign 2 (`campaign2/proxy.py`), the host supervisor runs `test_harness.py` in the trusted host process and communicates over pipes with a deny-default sandboxed worker (`worker.py` + `candidate.py`).
- **Filesystem Containment**: Candidate workers are restricted by macOS `sandbox-exec` (`candidate.sb.template` in Campaign 1 and `get_candidate_sandbox_profile()` in [`campaign2/proxy.py`](campaign2/proxy.py)). Candidate code may **write** only to its isolated per-evaluation temporary directory (`cand_tmpdir` / `temp_dir`) and `/dev/null`. **Read** access is permitted to that isolated temporary directory plus required macOS system and Python runtime paths (`/System`, `/usr`, `/Library`, `/opt/homebrew`, `/private/var/db`, `/private/var/select`, active Python prefixes, and in Campaign 1 `candidate_runner.py` and the task candidate directory); candidate workers cannot read the rest of the repository or sibling temporary directories under `/private/var/folders`.
- **Resource Enforcement**: Workers install CPU (`RLIMIT_CPU`), file-descriptor (`RLIMIT_NOFILE`), file-size (`RLIMIT_FSIZE`), and core-dump (`RLIMIT_CORE`) limits before reporting `READY`.
- **Residual Host-Availability Limitation**: macOS XNU does not enforce `RLIMIT_AS` via `setrlimit`. Memory-bound candidate code is bounded by wall-clock supervision (`CandidateTimeoutError`) rather than kernel address-space limits. Do not run untrusted adversarial workloads outside a dedicated VM or container.

---

## Repository Structure

- [`rsi_loop.py`](rsi_loop.py), [`evaluator.py`](evaluator.py), [`candidate_runner.py`](candidate_runner.py), [`sandbox_worker.py`](sandbox_worker.py) — Campaign 1 bi-level loop and sandboxed RPC evaluator.
- [`campaign2/`](campaign2/) — Campaign 2 containment proxy, manifest/archive verifiers, durability checkpoints, attempt lifecycle ledger, model provider, and orchestrator.
- [`campaign3/`](campaign3/) — Campaign 3 governance, preregistration verifiers, cost model, and `campaign3/sim/` Monte Carlo simulation & calibration engine.
- [`discovery_probe/`](discovery_probe/) — Dependency-free active rule-discovery sidecar benchmark.
- [`benchmarks/`](benchmarks/) — Campaign 1 tasks and Campaign 2 30-task Suite C / Suite P population with 90 defect rungs.
- [`scripts/`](scripts/) — Content-verifying read-only mode restoration (`reseal_readonly_artifacts.py`) and release hygiene gate (`check_release_hygiene.py`).
- [`LICENSE`](LICENSE) & [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) — MIT License and first/third-party provenance documentation.
- [`CONTRIBUTING.md`](CONTRIBUTING.md) & [`SECURITY.md`](SECURITY.md) — Contribution guidelines and private vulnerability reporting policy.
