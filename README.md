# RSILoop: Host-Contained Recursive Self-Improvement Experiment

A bi-level prompt-optimization harness that uses the local **Codex CLI** as both the candidate solution generator and the meta-optimizer. The harness evaluates candidate solutions in a macOS `sandbox-exec` jail, then feeds failure traces to the optimizer to mutate system prompts across three independent trials.

**Status: research prototype, not production software.** The experiment has not yet produced a promoted result. The code is a containment harness first; any observed self-improvement is a hypothesis until the preflight run is complete and independently verified.

---

## Threat Model & Host Containment

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
│   ├── huffman_coding/           # Optimization task (frozen spec, deterministic)
│   └── validation/
│       ├── string_calculator/    # Validation only — never used in mutation feedback
│       └── matrix_operations/    # Validation only — never used in mutation feedback
├── prompt_templates/
│   ├── inner_agent_baseline.txt  # Frozen baseline (hashed before optimization)
│   └── meta_optimizer.txt        # Meta-optimizer system prompt
├── evaluator.py                  # Host supervisor: RunnerProxy, drain threads, MRO registry
├── candidate_runner.py           # Untrusted process: fd1→/dev/null, RLIMIT_*, UUID framing
├── rsi_loop.py                   # Orchestrator: smoke+canary, three trials, promotion gate
├── candidate.sb.template         # macOS sandbox profile for candidate_runner.py
├── generator.sb.template         # macOS sandbox profile for Codex generator
├── worker.sb                     # Static profile used by boundary tests
├── preflight_hashes.json         # SHA-256 hashes of all task files (frozen before optimization)
├── test_evaluator.py             # 20+ containment, IPC, and reference solution tests
└── test_rsi_loop.py              # 13 orchestrator, parser, and state validation tests
```

---

## Getting Started

### Requirements

- macOS (sandbox-exec required)
- Python 3.11+
- [Codex CLI](https://github.com/openai/codex) installed and authenticated: `codex login`

### Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install google-genai openai anthropic python-dotenv
```

### Run the test suite

```bash
# Run from outside any sandbox (sandbox-exec calls require host access)
python3 -W error::ResourceWarning -m unittest test_evaluator test_rsi_loop -v
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
2. Evaluate the frozen baseline against all four optimization tasks.
3. Run three independent trials (baseline eval → up to 3 mutations → best eval).
4. Evaluate the three candidate prompts on two validation tasks.
5. Apply the promotion gate; retain baseline if it fails.
6. Write run-scoped manifests under `history/run_<id>/` (fsync + read-only permissions 0444).
7. Root `best_manifest.json` acts as a verified pointer to the run manifest.

Token ceiling: **50,000 output tokens** across all calls. Any breach immediately invalidates the run.

---

## Containment limits

| Property | Mechanism |
|----------|-----------|
| Candidate cannot write outside its dir | `candidate.sb`: `file-write*` limited to `{temp_dir}` |
| Candidate cannot fork/exec | `candidate.sb`: `(deny process-fork)` |
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

1. Mean paired delta across three trials is positive (`mean_delta > 0`).
2. At least one candidate prompt outperforms the baseline on the validation suite.
3. A deterministic tie-break selects one prompt.

If the gate fails, baseline is retained, and pointer is updated only if no manifest exists.

---

## Credits

Inspired by Weco AI's research on [AIDE²](https://weco.ai/blog): experimental evidence of consistent Level 1 recursive self-improvement in an AI research agent. The framing and bi-level structure draw from that work; this implementation does not reproduce or validate their findings.
