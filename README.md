# RSILoop: Sandbox-Safe Recursive Self-Improvement Experiment

A bi-level prompt-optimization harness that uses the local **Codex CLI** as both the candidate solution generator and the meta-optimizer. The harness evaluates candidate solutions in a macOS `sandbox-exec` jail, then feeds failure traces to the optimizer to mutate system prompts across three independent trials.

**Status: research prototype, not production software.** The experiment has not yet produced a promotable result. The code is a containment harness first; any observed self-improvement is a hypothesis until the preflight run is complete and independently verified.

---

## What this actually does

1. **Generates** candidate Python solutions with `codex exec --json` inside `generator.sb`.
2. **Evaluates** solutions in a subprocess (`candidate_runner.py`) under `candidate.sb` (macOS deny-default sandbox). The host evaluator validates every IPC frame by UUID, drains output with non-daemon threads, and reconstructs exception class hierarchies using `(module, qualname)` identity pairs.
3. **Mutates** the system prompt up to 3 times per trial using a meta-optimizer prompt, reverting to the best accepted prompt on regression.
4. **Selects** the winning prompt across three independent trials using a separate validation suite that is never used in mutation feedback. Promotion requires `mean_delta > 0` and the candidate must outperform the baseline on validation tasks.
5. **Evaluates** the winning prompt against an operator-controlled holdout (if present). Holdout results are recorded but never used for selection.

## What this does NOT do

- It does not improve itself autonomously (no code modifies `rsi_loop.py` or `evaluator.py`).
- It does not run unsupervised. Every promotion gate must pass; if not, the baseline is retained.
- The three-trial method demonstrates optimizer performance on a fixed task distribution. Generalization beyond those tasks is not claimed.

---

## Architecture

```
evaluator.py  (host, unsandboxed — trusted supervisor)
  └── candidate_runner.py  (under candidate.sb — deny-default macOS sandbox)
```

> **Note on nested `sandbox-exec`**: macOS prohibits a sandboxed process from applying a new sandbox profile. An intermediate trusted worker subprocess (planned) was therefore impossible. The evaluator spawns `candidate_runner.py` directly.

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
├── sandbox_worker.py             # IPC boundary test utility (not a process in the main loop)
├── rsi_loop.py                   # Orchestrator: smoke+canary, three trials, promotion gate
├── candidate.sb.template         # macOS sandbox profile for candidate_runner.py
├── generator.sb.template         # macOS sandbox profile for Codex generator
├── worker.sb                     # Static profile used by boundary tests
├── preflight_hashes.json         # SHA-256 hashes of all task files (frozen before optimization)
├── test_evaluator.py             # 20 containment and IPC tests
└── test_rsi_loop.py              # 13 orchestrator and parser tests
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
.venv/bin/python -m unittest test_evaluator test_rsi_loop -v
```

Expected: **33/33 tests pass** in ~35s.

### Run the optimization loop

```bash
.venv/bin/python rsi_loop.py
```

The loop will:
1. Run a `codex --version` smoke test and one canary API call inside `generator.sb`.
2. Evaluate the frozen baseline against all four optimization tasks.
3. Run three independent trials (baseline eval → up to 3 mutations → best eval).
4. Evaluate the three candidate prompts on two validation tasks.
5. Apply the promotion gate; retain baseline if it fails.
6. Write `best_manifest.json` (mode 0o444) with all hashes, scores, and usage.

Token ceiling: **50,000 output tokens** across all calls. Any breach immediately invalidates the run.

---

## Containment guarantees

| Property | Mechanism |
|----------|-----------|
| Candidate cannot write outside its dir | `candidate.sb`: `file-write*` limited to `{temp_dir}` |
| Candidate cannot fork/exec | `candidate.sb`: `(deny process-fork)` |
| Candidate cannot reach network | `candidate.sb`: `(deny network-outbound)` |
| Candidate cannot read repo or private benchmarks | `candidate.sb`: no allowlist entry for those paths |
| Candidate cannot spoofing control channel | UUID generated by host; runner must echo it back |
| Candidate output flood cannot deadlock host | Non-daemon drain thread; 10 KB retained, excess discarded |
| Stranded drain thread invalidates evaluation | `assert_no_threads()` called in `finally` |
| Shutdown trailing frames invalidate evaluation | Host reads runner stdout to EOF after shutdown_ok |
| Exception class identity is stable across evals | `(module, qualname)` keyed registry cleared per evaluation |
| Generator cannot read private holdout | `generator.sb`: no path entry for `rsi_loop_private/` |
| Codex credentials are isolated | `_build_codex_temp_home()`: 0700 dir, auth.json 0600 copy, `try/finally` cleanup |

---

## Promotion gate

A candidate prompt is promoted only when **all** of the following hold:

1. Mean paired delta across three trials is positive (`mean_delta > 0`).
2. At least one candidate prompt outperforms the baseline on the validation suite.
3. A deterministic tie-break (highest validation score, then lowest trial index) selects one prompt.
4. The selected prompt's `best_manifest.json` is written atomically (`.tmp` → `os.replace`) with mode `0o444`.

If the gate fails, `best_manifest.json` is updated with `baseline_prompt_selected: true` and a `retention_reason` field. The baseline prompt is never replaced.

---

## What has not happened yet

- **No preflight run has been executed.** The frozen baseline has not been measured against `huffman_coding`. If it scores 4/4, the task has no headroom and the loop will halt immediately.
- **No optimization trial has been run.** No self-improvement claim is made.
- **No promotion has occurred.** `best_manifest.json` does not yet exist.

---

## Credits

Inspired by Weco AI's research on [AIDE²](https://weco.ai/blog): experimental evidence of consistent Level 1 recursive self-improvement in an AI research agent. The framing and bi-level structure draw from that work; this implementation does not reproduce or validate their findings.
