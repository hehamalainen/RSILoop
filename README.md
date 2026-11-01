# RSILoop

RSILoop studies whether a coding agent can improve when a second agent revises its system prompt. The agent can propose a change. The host decides how it scores.

A coding agent writes a candidate solution. The host runs the scoring harness and interacts with the candidate through a sandboxed worker. A second agent, the meta-optimizer, uses evaluation feedback to propose a revised system prompt. The next round tests what that revision changes.

## What the pilot found

**The Campaign 2 pilot hit its prespecified ceiling.** Across two trials on Suite P, all 16 baseline evaluations on the optimization and selection tasks earned full credit. The optimizer and control arms also earned full credit on those tiers. The `no_headroom` rule fired, and the project stopped before running the confirmatory Suite C experiment.

The ceiling was specific to those tiers. Across the full pilot, the observed optimizer-minus-control difference was `+1/160`. The pilot did not establish that revising the prompt improves performance. It did establish something practical about this benchmark and baseline pairing: on the tasks used to choose a better prompt, the original prompt already earned every available point. [Pilot report](history/run_1786134848_cfb514d2/campaign2_pilot_report.json) · [Stop rule](campaign2/ladder.py) · [Recorded research claim](campaign_claim_record.json)

## Try it without an API key

From the repository root, play a hidden-rule discovery episode with Python’s standard library. No package installation, network connection, or model call is required.

```bash
python3 -m discovery_probe --seed 42 --tier 2
```

Enter a string such as `abc` to see how the hidden rule transforms it. Try another string to test your hypothesis. When you type `:solve`, exploration closes and you predict the outputs for a fixed set of challenges. The game reveals the rule afterward so you can check it against the SHA-256 commitment printed before your first move.

## What’s in the repository

| Component | What you can inspect or run |
|---|---|
| **Campaign 1** | The original prompt-optimization loop, sandboxed candidate runner, and host-supervised evaluator. |
| **Campaign 2** | A 30-task benchmark across four task families, plus the archived eight-task Suite P pilot, its invocation ledger, and a manifest for checking the evidence hashes. |
| **Campaign 3** | Protocol verifiers, Monte Carlo design simulations, and call and token cost estimates. The confirmatory experiment remains unexecuted. |
| **Discovery Probe** | The standalone hidden-rule game above. It demonstrates how to commit to a task before exploration and score predictions afterward. |

The live coding-agent campaigns and the full environment verifier require macOS. The pinned reproduction environment uses Python 3.12 or newer. RSILoop is released under the MIT License.

---

## Requirements & Installation

- **Operating System**: macOS (requires `/usr/bin/sandbox-exec` for Seatbelt containment profiles)
- **Pinned Reproduction Environment (`requirements.txt`)**: **Python `3.12+`** (tested on Python 3.12–3.14) with `numpy==2.5.1` (upstream `numpy==2.5.1` requires `Python >=3.12`), matching the exact environment identity recorded in archived Campaign 3 evidence (`campaign3/sim/moments_report.json`) and verified by `verify_env.py --offline`.
- **Broader Package Compatibility (`pyproject.toml`)**: Supports `Python >=3.11` with `numpy>=1.26.0` (via `.venv/bin/pip install -e .`), separating historical environment identity from cross-version numerical verification in `test_campaign3_sim_model.py`.

Create a **Python 3.12+** virtual environment, install the pinned reproduction dependencies, and run the offline environment & artifact verifier:

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

Run a non-interactive commitment-verification self-test (reads `session._rule` in-process to verify end-to-end scoring and SHA-256 commitment verification; the sidecar itself is an in-process benchmark library and does not isolate an external solver):

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
