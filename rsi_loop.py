import os
import sys
import subprocess
import re
import json
import shutil

# Path setup
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BENCHMARKS_DIR = os.path.join(BASE_DIR, "benchmarks")
PROMPTS_DIR = os.path.join(BASE_DIR, "prompt_templates")
EVALUATOR_PATH = os.path.join(BASE_DIR, "evaluator.py")

BASELINE_PROMPT_PATH = os.path.join(PROMPTS_DIR, "inner_agent_baseline.txt")
OPTIMIZER_PROMPT_PATH = os.path.join(PROMPTS_DIR, "meta_optimizer.txt")

# Helper to call codex
def call_codex(prompt: str, system_instruction: str = None) -> str:
    full_prompt = ""
    if system_instruction:
        full_prompt += f"System Instruction:\n{system_instruction}\n\nTask Instruction:\n"
    full_prompt += prompt
    
    output_file = os.path.join(BASE_DIR, "codex_output.txt")
    if os.path.exists(output_file):
        os.remove(output_file)
        
    cmd = [
        "codex", "exec", "-", 
        "--dangerously-bypass-approvals-and-sandbox",
        "-o", output_file
    ]
    
    try:
        res = subprocess.run(
            cmd,
            input=full_prompt,
            capture_output=True,
            text=True,
            timeout=120
        )
        if not os.path.exists(output_file):
            print(f"Codex error (no output file). Stderr:\n{res.stderr}\nStdout:\n{res.stdout}")
            return ""
        
        with open(output_file, "r") as f:
            content = f.read()
            
        os.remove(output_file)
        return content
    except Exception as e:
        print(f"Subprocess error: {e}")
        return ""

# Helper to extract Python code block from markdown
def extract_code(text: str) -> str:
    pattern = r"```python(.*?)```"
    match = re.search(pattern, text, re.DOTALL)
    if match:
        return match.group(1).strip()
    
    # Fallback to see if code is returned without markdown block
    if "def " in text or "class " in text:
        # Strip other lines if possible, but return text as is
        return text.strip()
    return ""

def run_evaluation(benchmark_name: str) -> dict:
    benchmark_path = os.path.join(BENCHMARKS_DIR, benchmark_name)
    # Run evaluator.py via python
    cmd = [sys.executable, EVALUATOR_PATH, benchmark_path]
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=10
        )
        
        # Parse output
        lines = res.stdout.strip().splitlines()
        success = False
        error = ""
        for line in lines:
            if line.startswith("Success: "):
                success = line.split("Success: ")[1].strip() == "True"
        
        if not success:
            # Gather error
            error_index = res.stdout.find("Error:\n")
            if error_index != -1:
                error = res.stdout[error_index + 7:].strip()
            else:
                error = res.stderr.strip() or res.stdout.strip()
                
        return {"success": success, "error": error}
    except Exception as e:
        return {"success": False, "error": str(e)}

def evaluate_prompt(prompt_text: str) -> tuple:
    print(f"\n--- Evaluating Prompt ---")
    benchmarks = [d for d in os.listdir(BENCHMARKS_DIR) if os.path.isdir(os.path.join(BENCHMARKS_DIR, d))]
    
    results = {}
    passed_count = 0
    
    for benchmark in benchmarks:
        print(f"Running task: {benchmark}...")
        
        # Load task description
        task_file = os.path.join(BENCHMARKS_DIR, benchmark, "problem.txt")
        with open(task_file, "r") as f:
            task_desc = f.read()
            
        # Get solution from Codex
        raw_output = call_codex(task_desc, system_instruction=prompt_text)
        solution_code = extract_code(raw_output)
        
        # Write solution.py
        solution_file = os.path.join(BENCHMARKS_DIR, benchmark, "solution.py")
        with open(solution_file, "w") as f:
            f.write(solution_code)
            
        # Run evaluator
        eval_res = run_evaluation(benchmark)
        results[benchmark] = {
            "success": eval_res["success"],
            "error": eval_res["error"],
            "code_len": len(solution_code)
        }
        
        if eval_res["success"]:
            print(f"  [+] PASSED")
            passed_count += 1
        else:
            print(f"  [-] FAILED: {eval_res['error'][:100]}...")
            
    score = passed_count / len(benchmarks) if benchmarks else 0.0
    print(f"Final Score: {passed_count}/{len(benchmarks)} ({score*100:.1f}%)")
    return score, results

def mutate_prompt(current_prompt: str, failures: list) -> str:
    print("\n--- Proposing Prompt Mutation (Outer Loop) ---")
    
    # Load meta optimizer instructions
    with open(OPTIMIZER_PROMPT_PATH, "r") as f:
        meta_prompt = f.read()
        
    # Build history context
    context = f"Current System Prompt:\n{current_prompt}\n\n"
    context += "Failed Tasks & Errors:\n"
    for fail in failures:
        context += f"--- Task: {fail['task']} ---\n"
        context += f"Problem Description:\n{fail['description']}\n"
        context += f"Failed Solution:\n{fail['code']}\n"
        context += f"Test Error:\n{fail['error']}\n\n"
        
    raw_new_prompt = call_codex(context, system_instruction=meta_prompt)
    
    # Simple clean up of prompts in case they wrap in markdown
    new_prompt = raw_new_prompt.replace("```", "").strip()
    return new_prompt

def main():
    if not os.path.exists(BASELINE_PROMPT_PATH):
        print(f"Baseline prompt not found at {BASELINE_PROMPT_PATH}")
        sys.exit(1)
        
    with open(BASELINE_PROMPT_PATH, "r") as f:
        current_prompt = f.read()
        
    print("Starting Self-Improvement Loop...")
    
    iterations = 3
    best_score = -1.0
    best_prompt = current_prompt
    
    for i in range(iterations):
        print(f"\n==========================================")
        print(f"ITERATION {i+1} / {iterations}")
        print(f"==========================================")
        
        score, results = evaluate_prompt(current_prompt)
        
        if score > best_score:
            print(f"[+] Found improved prompt! (Old Best: {best_score*100:.1f}%, New: {score*100:.1f}%)")
            best_score = score
            best_prompt = current_prompt
            # Save the best prompt
            with open(os.path.join(PROMPTS_DIR, "best_prompt.txt"), "w") as f:
                f.write(best_prompt)
        else:
            print(f"[-] No improvement. Retaining prior best.")
            
        # Stop early if 100% success
        if best_score == 1.0:
            print("Achieved 100% success rate on benchmarks! Stopping loop.")
            break
            
        # Collect failures to pass to meta-optimizer
        failures = []
        for task, res in results.items():
            if not res["success"]:
                # read files for details
                with open(os.path.join(BENCHMARKS_DIR, task, "problem.txt"), "r") as f:
                    desc = f.read()
                with open(os.path.join(BENCHMARKS_DIR, task, "solution.py"), "r") as f:
                    code = f.read()
                failures.append({
                    "task": task,
                    "description": desc,
                    "code": code,
                    "error": res["error"]
                })
                
        if failures:
            current_prompt = mutate_prompt(best_prompt, failures)
            print(f"\nNew Prompt Proposed:\n{current_prompt[:200]}...")
            
    print(f"\n==========================================")
    print(f"Self-Improvement Loop Finished!")
    print(f"Best Score achieved: {best_score*100:.1f}%")
    print(f"Best prompt saved to: {os.path.join(PROMPTS_DIR, 'best_prompt.txt')}")
    print(f"==========================================")

if __name__ == "__main__":
    main()
