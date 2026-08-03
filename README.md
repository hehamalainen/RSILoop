# RSILoop: Host-Contained Recursive Self-Improvement Experiment

> [!NOTE]
> **Current status — 3 August 2026**
> This README describes the committed baseline harness on `origin/main` at commit `153c05f9d58302e27189384ebe78859766f15afb`. Campaign 2 is being redesigned and is not yet represented on main. The proposed Campaign 2 design removes the model-based difficulty proxy and instead uses preregistered family-blocked task assignment. No additional local or cloud model is required for that proxy. No confirmatory Campaign 2 run or promoted result exists yet.

**Document Specification Snapshot**: Commit `153c05f9d58302e27189384ebe78859766f15afb` | Date: 3 August 2026.

A bi-level prompt-optimization harness that uses the installed **Codex CLI** (which performs remote model inference) as both the candidate solution generator and the meta-optimizer. The harness evaluates candidate solutions inside a macOS `sandbox-exec` containment jail, then feeds failure traces to the optimizer to mutate system prompts across three separately executed trials.

> **Model Clarification**: The statement "no cloud or local model needed" applies strictly to the abandoned difficulty proxy. The core harness requires the Codex CLI, whose model inference is remote (not a local model).

**Status: Research prototype, not production software.** The experiment has not yet produced a promoted result. The code is a containment harness first; any observed self-improvement is a hypothesis until the preflight run is complete and independently verified.

---

## Threat Model & Host Containment

The harness implements and regression-tests a macOS containment boundary. It is a research prototype, not a hardened security sandbox, and should not be used to execute untrusted third-party code on a sensitive host.

- **Process Isolation**: The candidate solution and the runner communication shim share a Python interpreter and process. Therefore, protocol authenticity is not guaranteed against active exploitation by candidate code (e.g., introspection to locate control descriptors).
- **Correlation**: Request UUIDs provide stale-frame and accidental output pollution protection, not protocol cryptographic authenticity.
- **Boundary**: The achieved boundary is **host containment** (preventing arbitrary filesystem writes, network operations, or process spawning on the host using macOS `sandbox-exec` and `setrlimit`).

---

## Architecture

```
evaluator.py  (host, unsandboxed — trusted supervisor)
  └── candidate_runner.py  (under candidate.sb — deny-default macOS sandbox)
```

> **Note on nested `sandbox-exec`**: macOS prohibits a sandboxed process from applying a new sandbox profile. The evaluator spawns `candidate_runner.py` directly.

### Key files

```
├── benchmarks/
│   ├── circular_buffer/          # Optimization task
│   ├── json_parser/              # Optimization task
│   ├── run_length_encoding/      # Optimization task
│   ├── huffman_coding/           # Optimization task (evaluated first as no-headroom preflight)
│   └── validation/
│       ├── string_calculator/    # Validation only — never used in mutation feedback
│       └── matrix_operations/    # Validation only — never used in mutation feedback
├── prompt_templates/
│   ├── inner_agent_baseline.txt  # Frozen baseline (hashed before optimization)
│   └── meta_optimizer.txt        # Meta-optimizer system prompt
├── evaluator.py                  # Host supervisor: RunnerProxy, MRO registry, class/instance caching
├── candidate_runner.py           # Untrusted process: fd1→/dev/null, RLIMIT_*, UUID framing, object store
├── rsi_loop.py                   # Orchestrator: smoke+canary, three trials, promotion gate, RunContext
├── candidate.sb.template         # macOS sandbox profile for candidate_runner.py (parameterized)
├── generator.sb.template         # macOS sandbox profile for Codex generator (parameterized)
├── preflight_hashes.json         # SHA-256 hashes of all task files (frozen before optimization)
├── test_evaluator.py             # Containment, IPC, and reference solution tests
├── test_rsi_loop.py              # Orchestrator, parser, and state validation tests
└── run_tests.py                  # Combined test launcher (enforces zero skips and zero ResourceWarnings)
```

---

## Getting Started

### Prerequisites

- macOS (sandbox-exec required)
- Python 3.11+
- [Codex CLI](https://github.com/openai/codex) installed and authenticated.

The real prerequisite for running the harness is an installed and authenticated Codex CLI. Authenticate via:
```bash
codex login
```
*Note*: `codex login` supports ChatGPT OAuth, API-key, or access-token authentication according to the [official Codex command reference](https://learn.chatgpt.com/docs/developer-commands#codex-login). Legacy SDK packages (such as `google-genai`, `openai`, `anthropic`, or `python-dotenv`) belong to legacy environment diagnostics and are not required by the committed experiment runtime.

### Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
```

### Run the test suite

Run the test launcher from outside any sandbox (sandbox-exec calls require host access). The launcher enforces zero skips, zero ResourceWarnings, and returns nonzero on any warnings, unraisable exceptions, skips, failures, or errors:

```bash
python3 run_tests.py
```

### Run the optimization loop

A clean Git status is verified, and matching approved Git commit hash is checked against the lock file:
1. Commits are done locally.
2. The approved HEAD SHA is verified by an operator.
3. `experiment.lock` is created with:
   ```json
   { "approved_commit_sha": "<HEAD_SHA>" }
   ```
4. Run:
   ```bash
   python3 rsi_loop.py
   ```

The loop will:
1. Run a `codex --version` smoke test and one canary API call inside `generator.sb`.
2. **Preflight Evaluation**: Evaluate the frozen baseline against `huffman_coding` as a no-headroom preflight check. If the baseline solution already passes `huffman_coding` completely without room for improvement, the experiment terminates early.
3. Evaluate the frozen baseline against the remaining optimization tasks (`circular_buffer`, `json_parser`, `run_length_encoding`).
4. Run **three separately executed trials** (baseline eval → up to 3 mutations → best eval). *Note*: Because trials share the same tasks, baseline, and model provider, statistical independence is not established.
5. Evaluate the three candidate prompts on two validation tasks.
6. Apply the promotion gate; retain baseline if it fails.
7. Write run-scoped manifests under `history/run_<id>/` (fsync + read-only permissions 0444).
8. Root `best_manifest.json` acts as a verified pointer to the run manifest.

Token ceiling: **50,000 output tokens** across all calls. Any breach immediately invalidates the run.

---

## Containment limits

| Property | Mechanism |
|----------|-----------|
| Candidate cannot write outside its dir | `candidate.sb`: `file-write*` limited to `{temp_dir}` |
| Candidate cannot fork | `candidate.sb`: `(deny process-fork)` |
| Candidate process exec limit | `candidate.sb`: `(allow process-exec)` limited strictly to resolved virtualenv, Python interpreter, prefix, base prefix, and Homebrew paths |
| Candidate cannot reach network | `candidate.sb`: `(deny network-outbound)` |
| Candidate cannot read repo or private benchmarks | `candidate.sb`: no allowlist entry for those paths |
| Candidate output flood cannot deadlock host | Non-daemon drain thread; 10 KB retained, excess discarded |
| Stranded drain thread invalidates evaluation | `assert_no_threads()` called in `finally` |
| Shutdown trailing frames invalidate evaluation | Host reads runner stdout to EOF after shutdown_ok (checks shutdown_clean) |
| Exception class identity is stable across evals | `(module, qualname)` keyed registry cleared per evaluation |
| Generator cannot read private holdout | `generator.sb`: no path entry for `rsi_loop_private/` |
| Codex credentials are isolated | `_build_codex_temp_home()`: 0700 dir, auth.json 0600 copy, `try/finally` cleanup |

---

## Promotion gate

A candidate prompt is promoted only when **all** of the following hold:

1. Mean paired delta across three separately executed trials is positive (`mean_delta > 0`).
2. At least one candidate prompt outperforms the baseline on the validation suite.
3. The selected candidate prompt must differ from the baseline prompt, have its own positive optimization delta (`delta > 0`), and beat the baseline validation score.
4. **Note**: Statistical significance is not established by the validation gate sampling.

If the gate fails, baseline is retained, and pointer is updated only if no manifest exists.

---

## No-Holdout Mode & Research Limitations

For environments where the private holdout test suite (`simple_regex_matcher`) is unavailable (such as local testing or non-production environments), the harness supports a **No-Holdout Mode**.

- **Enabling**: Set `ALLOW_NO_HOLDOUT=1` in the environment, and configure `"allow_no_holdout": true` inside the operator-approved `experiment.lock`. The harness enforces strict agreement between the environment and the lock file; any disagreement will abort the run immediately.
- **Provenance Flag**: When No-Holdout Mode is active, any promoted manifest will record `"no_holdout_mode_active": true`.
- **Research Limitations**: Without holdout validation, prompt optimization is susceptible to **overfitting** on the public/validation tasks. Promotion in this mode does not guarantee generalizable improvement and is intended solely for integration verification.

---

## Repository Access & Licensing Note

- **Access Note**: The repository is private. Unauthenticated or anonymous HTTPS requests return `404 Not Found`. This PDF print specification represents a point-in-time snapshot labelled with commit SHA `153c05f9d58302e27189384ebe78859766f15afb` and date `3 August 2026`.
- **Licensing**: Private research prototype. License terms to be resolved prior to any public distribution.

---

## Credits

Inspired by Weco AI's research on [AIDE²](https://weco.ai/blog): experimental evidence of consistent Level 1 recursive self-improvement in an AI research agent. The framing and bi-level structure draw from that work; this implementation does not reproduce or validate their findings.
