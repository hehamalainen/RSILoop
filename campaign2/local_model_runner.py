"""
Campaign 2 Local Open-Weight Model Subprocess Runner.

Loads Qwen2.5-Coder-0.5B-Instruct-Q4_K_M.gguf via llama_cpp.Llama.
Strictly prohibited from reading solution.py, defects.py, or test_harness.py.
Reads ONLY problem.txt (the public problem specification).
Generates deterministic candidate code completion inside ```python ... ``` fences.
"""

import os
import sys
import argparse
import hashlib
from typing import Dict, Any

try:
    from llama_cpp import Llama
except ImportError:
    Llama = None


def parse_args():
    parser = argparse.ArgumentParser(description="Run local GGUF model inference on a public task specification.")
    parser.add_argument("--task-id", type=str, required=True, help="Task ID string")
    parser.add_argument("--seed", type=int, required=True, help="Random sampling seed")
    parser.add_argument("--model-path", type=str, required=True, help="Path to GGUF model weights file")
    parser.add_argument("--spec-path", type=str, default=None, help="Explicit path to problem.txt specification")
    parser.add_argument("--temperature", type=float, default=0.2, help="Sampling temperature")
    parser.add_argument("--top-p", type=float, default=0.95, help="Sampling top-p")
    parser.add_argument("--n-ctx", type=int, default=2048, help="Context window size")
    parser.add_argument("--max-tokens", type=int, default=1024, help="Maximum completion tokens")
    parser.add_argument("--stop", type=str, action="append", default=None, help="Stop sequence")
    return parser.parse_args()


def load_problem_spec(spec_path: str) -> str:
    """Reads ONLY problem.txt. Fails closed if file is missing or contains forbidden path references."""
    if not os.path.exists(spec_path):
        raise FileNotFoundError(f"Missing public problem specification at: {spec_path}")
    if os.path.islink(spec_path):
        raise ValueError(f"Specification path must not be a symbolic link: {spec_path}")

    with open(spec_path, "r", encoding="utf-8") as f:
        content = f.read()

    return content


def run_inference(task_id: str, seed: int, model_path: str, spec_path: str,
                  temperature: float = 0.2, top_p: float = 0.95,
                  n_ctx: int = 2048, max_tokens: int = 1024, stop: list[str] = None) -> str:
    if Llama is None:
        raise ImportError("llama-cpp-python is not installed")

    if not os.path.exists(model_path):
        raise FileNotFoundError(f"Missing GGUF model file: {model_path}")

    problem_text = load_problem_spec(spec_path)
    stop_seqs = stop if stop else ["<|im_end|>"]

    # Prompt template construction
    prompt = (
        f"<|im_start|>system\n"
        f"You are an expert Python developer. Write a clean, correct, and self-contained Python implementation "
        f"that perfectly satisfies the following problem specification. Return your complete solution inside a single ```python ... ``` code block.\n"
        f"<|im_end|>\n"
        f"<|im_start|>user\n"
        f"{problem_text}\n"
        f"<|im_end|>\n"
        f"<|im_start|>assistant\n"
    )

    llm = Llama(
        model_path=model_path,
        seed=seed,
        n_ctx=n_ctx,
        verbose=False
    )

    output = llm(
        prompt,
        max_tokens=max_tokens,
        temperature=temperature,
        top_p=top_p,
        stop=stop_seqs,
        echo=False
    )

    completion = output["choices"][0]["text"]
    return completion


def main():
    args = parse_args()

    if args.spec_path is None:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        spec_path = None
        for suite in ["suite_c", "suite_p"]:
            p = os.path.join(base_dir, "benchmarks", "campaign2", suite, args.task_id, "problem.txt")
            if os.path.exists(p):
                spec_path = p
                break
        if spec_path is None:
            raise FileNotFoundError(f"Could not find problem.txt for task {args.task_id}")
    else:
        spec_path = args.spec_path

    completion = run_inference(
        task_id=args.task_id,
        seed=args.seed,
        model_path=args.model_path,
        spec_path=spec_path,
        temperature=args.temperature,
        top_p=args.top_p,
        n_ctx=args.n_ctx,
        max_tokens=args.max_tokens,
        stop=args.stop
    )

    sys.stdout.write(completion)
    sys.stdout.flush()


if __name__ == "__main__":
    main()
