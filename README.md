# RSILoop: Sandbox-Safe Recursive Self-Improvement Loop

A bi-level optimization prototype designed to automate the evaluation, benchmarking, and recursive self-improvement of LLM-based coding prompts. The harness uses a local sandboxed runner to verify candidate solutions against strict unit test parameters, feeding execution trace errors back into a meta-optimizer to mutate system prompts.

Built to run autonomously using the local **Codex CLI (`codex`)** as the reasoning engine.

---

## 1. Project Directory Structure

```
├── benchmarks/              # Target programming challenges
│   ├── circular_buffer/     # Fixed capacity circular buffer task + tests
│   ├── json_parser/         # Manual JSON string parser task + tests
│   └── run_length_encoding/ # Run-length compression task + tests
├── docs/
│   └── walkthrough.md       # Architectural notes, zero-trust profiles, and roadmap
├── prompt_templates/        # System prompts mutated by the optimizer
│   ├── inner_agent_baseline.txt
│   ├── meta_optimizer.txt
│   └── best_prompt.txt      # Convenience copy of the best performing prompt
├── evaluator.py             # Sandbox execution evaluator with dynamic MRO exception registry
├── sandbox_worker.py        # Zero-trust worker enforcing irreversible resource limits
├── rsi_loop.py              # Main bi-level optimization driver (JSONL parser + Greedy Policy)
├── test_evaluator.py        # 14-test regression verification suite
├── test_rsi_loop.py         # Orchestrator and parser validation tests
├── verify_env.py            # Diagnostic script to check CLI dependencies
└── README.md
```

---

## 2. Getting Started

### Prerequisites
1. **Python 3.10+**
2. **Node.js** (for running the Codex CLI wrapper)
3. **Codex CLI**: Reinstall/verify it is globally installed and authenticated:
   ```bash
   npm install -g @openai/codex@latest
   codex login
   ```

### Setup & Run
1. Create a virtual environment and install the required tooling:
   ```bash
   python3 -m venv .venv
   .venv/bin/pip install google-genai openai anthropic python-dotenv
   ```
2. Run diagnostic checks & regression verification suite:
   ```bash
   .venv/bin/python test_evaluator.py
   .venv/bin/python test_rsi_loop.py
   ```
3. Run the automated optimization loop:
   ```bash
   .venv/bin/python rsi_loop.py
   ```

Upon completion, the best performing manifest will be saved atomically to `best_manifest.json`, referencing the best prompt and solutions.

---

## 3. How the Loop Works (The Code is the Truth)
1. **Generate**: `rsi_loop.py` executes Codex using `codex exec --json` under `generator.sb`. It parses the structured JSONL events (`turn.completed` and `item.completed`) to extract the candidate solution and token usage.
2. **Evaluate**: `evaluator.py` spawns a sandboxed subprocess (`sandbox_worker.py`) under `candidate.sb` with a strict `3.0s` timeout and resource limits. Ordinary classes are proxied, and custom exception class hierarchies are reconstructed dynamically on the host matching their exact MRO names.
3. **Mutate**: If a candidate fails, the meta-optimizer mutates the best accepted prompt using the greedy search policy based on the best prompt's own failure traces.

---

## 4. Credits & Original Research

This project is a localized prototype inspired by Weco AI's pioneering research on **AIDE²** (the first documented experimental evidence of consistent Level 1 recursive self-improvement in an AI research agent).

Key References:
* **Research & Blog**: [AIDE²: Autoresearch on Autoresearch](https://weco.ai/blog) by Zhengyao Jiang and the Weco AI team.
* **Core Agent Framework**: [WecoAI/aideml](https://github.com/WecoAI/aideml) on GitHub.
