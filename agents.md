# Agent Onboarding Guide: RSILoop Project

Welcome! This document provides the necessary context and entry points for any autonomous agent joining this workspace.

---

## 🎯 Project Overview
**RSILoop** is a bi-level optimization harness designed for **Recursive Self-Improvement (RSI)** of LLM-based coding prompts. The loop automatically evaluates a coding agent, runs generated solutions in a zero-trust sandbox, feeds traceback errors into a meta-optimizer, and mutates system prompts to iteratively improve performance.

```mermaid
flowchart TD
    subgraph Host [Host System / Outer Loop]
        ORCH[rsi_loop.py] -->|1. Prompts & Tasks| CX[Codex CLI --json]
        CX -->|2. Captures JSONL| ORCH
        ORCH -->|3. Writes solution.py| SOL[solution.py]
        ORCH -->|4. Launches| EV[evaluator.py]
    end
    
    subgraph Sandbox [Sandboxed Subprocess / Inner Loop]
        EV -->|5. Spawns Worker| WRK[sandbox_worker.py]
        WRK -->|6. Sets resource limits| WRK
        WRK -->|7. Imports| SOL
        EV -->|8. Maps Proxies| TEST[test_harness.py]
        TEST -->|9. Runs assertions| EV
    end
    
    EV -->|10. Outputs Result| ORCH
```

---

## 📂 Codebase Navigation
- **[rsi_loop.py](file:///Users/heikkihamalainen/.gemini/antigravity/scratch/rsi_loop/rsi_loop.py)**: Main bi-level optimization driver (JSONL parser + Greedy Policy).
- **[evaluator.py](file:///Users/heikkihamalainen/.gemini/antigravity/scratch/rsi_loop/evaluator.py)**: Sandbox execution evaluator with dynamic MRO exception registry.
- **[sandbox_worker.py](file:///Users/heikkihamalainen/.gemini/antigravity/scratch/rsi_loop/sandbox_worker.py)**: Zero-trust worker enforcing irreversible resource limits.
- **[test_evaluator.py](file:///Users/heikkihamalainen/.gemini/antigravity/scratch/rsi_loop/test_evaluator.py)**: 14-test regression verification suite.
- **[test_rsi_loop.py](file:///Users/heikkihamalainen/.gemini/antigravity/scratch/rsi_loop/test_rsi_loop.py)**: Orchestrator and parser validation tests.
- **[prompt_templates/](file:///Users/heikkihamalainen/.gemini/antigravity/scratch/rsi_loop/prompt_templates/)**:
  - `inner_agent_baseline.txt`: Initial prompt for the generator.
  - `meta_optimizer.txt`: System prompt guiding the prompt mutation logic.
  - `best_prompt.txt`: Convenience copy of the best performing prompt.
- **[benchmarks/](file:///Users/heikkihamalainen/.gemini/antigravity/scratch/rsi_loop/benchmarks/)**: target problems and verification files.
- **[docs/walkthrough.md](file:///Users/heikkihamalainen/.gemini/antigravity/scratch/rsi_loop/docs/walkthrough.md)**: Details structural design, results, and long-term directions.

---

## 🛠️ Getting Started for Agents
1. **Verify Environment**: Run the environmental diagnostic check.
   ```bash
   .venv/bin/python verify_env.py
   ```
2. **Run All Regression Tests**:
   ```bash
   .venv/bin/python test_evaluator.py
   .venv/bin/python test_rsi_loop.py
   ```
3. **Run Optimization Loop**: Spin up the automated prompt-mutation iteration.
   ```bash
   .venv/bin/python rsi_loop.py
   ```

---

## 📈 Current Project State
- All baseline benchmarks pass under the zero-trust sandbox containment framework.
- Active verification suite contains 14 regression and behavioral resource limit tests.
- The authoritative promoted run state is recorded atomically inside `best_manifest.json`.
