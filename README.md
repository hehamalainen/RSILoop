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
│   └── walkthrough.md       # Exhaustive architecture notes, traces, and roadmap
├── prompt_templates/        # System prompts mutated by the optimizer
│   ├── inner_agent_baseline.txt
│   ├── meta_optimizer.txt
│   └── best_prompt.txt      # Best prompt output by the latest run
├── evaluator.py             # Isolated execution harness with strict timeouts
├── rsi_loop.py              # Main bi-level optimization driver
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
2. Diagnostic check:
   ```bash
   .venv/bin/python verify_env.py
   ```
3. Run the automated optimization loop:
   ```bash
   .venv/bin/python rsi_loop.py
   ```

Upon completion, the best performing system prompt will be saved to `prompt_templates/best_prompt.txt`.

---

## 3. How the Loop Works (The Code is the Truth)
1. **Generate**: `rsi_loop.py` compiles the current system prompt with the task details from `benchmarks/<task>/problem.txt` and queries the `codex exec` compiler.
2. **Write**: The response is parsed and written to `benchmarks/<task>/solution.py`.
3. **Sandbox Run**: `evaluator.py` runs the task's `test_harness.py` inside a subprocess with a strict 3.0s timeout to prevent execution locks.
4. **Mutate**: If a solution fails, the error traces are collected, and Codex is called with the `meta_optimizer.txt` prompt to rewrite the coder's system instructions to prevent that failure mode in the next iteration.

Read [docs/walkthrough.md](docs/walkthrough.md) for full design, code trace reviews, and the next-phase roadmap.
