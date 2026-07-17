import os
import sys
import subprocess
import re
import json
import shutil
import hashlib
import time
import tempfile
from datetime import datetime

# Path setup
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BENCHMARKS_DIR = os.path.join(BASE_DIR, "benchmarks")
PROMPTS_DIR = os.path.join(BASE_DIR, "prompt_templates")
HISTORY_DIR = os.path.join(BASE_DIR, "history")

BASELINE_PROMPT_PATH = os.path.join(PROMPTS_DIR, "inner_agent_baseline.txt")
OPTIMIZER_PROMPT_PATH = os.path.join(PROMPTS_DIR, "meta_optimizer.txt")

# Ensure required directories exist
os.makedirs(HISTORY_DIR, exist_ok=True)
os.makedirs(PROMPTS_DIR, exist_ok=True)

# Parse structured JSONL events from Codex execution
def parse_codex_jsonl_output(stdout_lines: list) -> tuple:
    code = ""
    usage_data = {}
    completed_event_seen = False
    
    for line in stdout_lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
            etype = event.get("type")
            
            # Precise JSONL item filtering condition
            if etype == "item.completed":
                item = event.get("item", {})
                if item.get("type") == "agent_message":
                    code = item.get("text", "").strip()
                    
            elif etype == "turn.completed":
                usage_data = event.get("usage", {})
                completed_event_seen = True
        except json.JSONDecodeError:
            pass
            
    # Fail closed on missing turn.completed usage records
    if not completed_event_seen or not usage_data:
        raise ValueError("Codex JSONL stream parsing error: missing turn.completed event or usage records")
        
    return code, usage_data

def extract_code(raw_output: str) -> str:
    code_pattern = r"```python(.*?)```"
    code_match = re.search(code_pattern, raw_output, re.DOTALL)
    if code_match:
        return code_match.group(1).strip()
    return raw_output.strip()

# Call Codex non-interactively inside dynamic generator.sb allowlist sandbox
def call_codex_sandboxed(prompt: str, system_instruction: str, candidate_dir: str, expect_json_code: bool = True) -> tuple:
    full_prompt = ""
    if system_instruction:
        full_prompt += f"System Instruction:\n{system_instruction}\n\nTask Instruction:\n"
    full_prompt += prompt

    # Create unpredictable per-run secure temp directory with mode 0700
    temp_dir = tempfile.mkdtemp(prefix="codex_run_", dir="/tmp")
    os.chmod(temp_dir, 0o700)
    real_temp_dir = os.path.realpath(temp_dir)

    # Generate generator.sb dynamically in the secure isolated tmp folder
    template_path = os.path.join(BASE_DIR, "generator.sb.template")
    with open(template_path, "r") as f:
        template = f.read()
    
    sb_content = template.format(
        candidate_dir=os.path.abspath(candidate_dir),
        temp_dir=real_temp_dir
    )
    
    sb_path = os.path.join(real_temp_dir, "generator.sb")
    with open(sb_path, "w") as f:
        f.write(sb_content)

    # Run Codex inside sandbox-exec with strict allowlist
    cmd = [
        "sandbox-exec", "-f", sb_path,
        "/opt/homebrew/bin/codex", "exec", "-", 
        "-s", "read-only",
        "--ephemeral",
        "--json",  # Structured JSONL event stream output
        "-C", os.path.abspath(candidate_dir)
    ]
    
    allowed_env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin",
        "LANG": "en_US.UTF-8",
        "HOME": "/Users/heikkihamalainen",
        "USER": "heikkihamalainen",
        "LOGNAME": "heikkihamalainen",
        "TMPDIR": real_temp_dir
    }

    try:
        res = subprocess.run(
            cmd,
            input=full_prompt,
            capture_output=True,
            text=True,
            timeout=120,
            env=allowed_env
        )
        
        # Clean up temp directory
        shutil.rmtree(real_temp_dir, ignore_errors=True)

        if res.returncode != 0:
            sys.stderr.write(f"Codex error. Stderr: {res.stderr}\n")
            sys.stderr.flush()
            return "", 0

        stdout_lines = res.stdout.splitlines()
        raw_msg, usage = parse_codex_jsonl_output(stdout_lines)
        
        # Enforce output tokens billable budget limit
        tokens = usage.get("output_tokens", 0)
        
        if expect_json_code:
            code = extract_code(raw_msg)
            return code, tokens
        else:
            return raw_msg, tokens
            
    except Exception as e:
        if os.path.exists(sb_path):
            os.remove(sb_path)
        sys.stderr.write(f"Generator subprocess execution error: {e}\n")
        sys.stderr.flush()
        return "", 0

# Import evaluator dynamically to execute Zero-Trust evaluation
def run_evaluation_on_candidate(candidate_dir, test_harness_path):
    from evaluator import run_evaluation
    return run_evaluation(candidate_dir, test_harness_path)

def evaluate_prompt(prompt_text: str, run_id: str, iter_id: str) -> tuple:
    print(f"\n--- Evaluating Candidate Prompt ({iter_id}) ---")
    
    benchmarks = [d for d in os.listdir(BENCHMARKS_DIR) if os.path.isdir(os.path.join(BENCHMARKS_DIR, d))]
    
    results = {}
    passed_count = 0
    total_tokens = 0
    
    for benchmark in benchmarks:
        print(f"Running task: {benchmark}...")
        
        iter_task_dir = os.path.join(HISTORY_DIR, f"run_{run_id}", f"iteration_{iter_id}", "benchmarks", benchmark)
        os.makedirs(iter_task_dir, exist_ok=True)
        
        task_file = os.path.join(BENCHMARKS_DIR, benchmark, "problem.txt")
        with open(task_file, "r") as f:
            task_desc = f.read()
            
        solution_code, tokens = call_codex_sandboxed(task_desc, prompt_text, iter_task_dir, expect_json_code=True)
        total_tokens += tokens
        
        solution_file = os.path.join(iter_task_dir, "solution.py")
        with open(solution_file, "w") as f:
            f.write(solution_code)
            
        test_harness_path = os.path.join(BENCHMARKS_DIR, benchmark, "test_harness.py")
        eval_res = run_evaluation_on_candidate(iter_task_dir, test_harness_path)
        
        results[benchmark] = {
            "success": eval_res["success"],
            "error": eval_res["error"],
            "code_len": len(solution_code),
            "tokens_used": tokens,
            "hash": hashlib.sha256(solution_code.encode()).hexdigest()
        }
        
        if eval_res["success"]:
            print(f"  [+] PASSED")
            passed_count += 1
        else:
            print(f"  [-] FAILED: {str(eval_res['error'])[:120]}...")
            
    score = passed_count / len(benchmarks) if benchmarks else 0.0
    print(f"Final Score: {passed_count}/{len(benchmarks)} ({score*100:.1f}%)")
    return score, results, total_tokens

def mutate_prompt(current_prompt: str, failures: list, run_id: str, next_iter_dir: str) -> tuple:
    print("\n--- Proposing Prompt Mutation (Outer Loop) ---")
    
    with open(OPTIMIZER_PROMPT_PATH, "r") as f:
        meta_prompt = f.read()
        
    context = f"Current System Prompt:\n{current_prompt}\n\n"
    context += "Failed Tasks & Errors:\n"
    for fail in failures:
        context += f"--- Task: {fail['task']} ---\n"
        context += f"Problem Description:\n{fail['description']}\n"
        context += f"Failed Solution:\n{fail['code']}\n"
        context += f"Test Error:\n{fail['error']}\n\n"
        
    # Meta optimizer returns plain text prompt instructions (not python code blocks)
    new_prompt, tokens = call_codex_sandboxed(context, meta_prompt, next_iter_dir, expect_json_code=False)
    return new_prompt, tokens

def promote_manifest(run_id: str, iter_id: str, score: float, prompt_text: str, holdout_eval: dict = None):
    best_manifest_path = os.path.join(BASE_DIR, "best_manifest.json")
    temp_manifest_path = os.path.join(BASE_DIR, "best_manifest.json.tmp")
    
    # Save old best_manifest.json if exists as invalid_old
    if os.path.exists(best_manifest_path):
        try:
            shutil.copy2(best_manifest_path, os.path.join(BASE_DIR, "best_manifest.json.invalid_old"))
        except Exception:
            pass
            
    manifest_data = {
        "run_id": run_id,
        "iteration_id": iter_id,
        "score": score,
        "prompt_text": prompt_text,
        "timestamp": str(datetime.now()),
        "holdout_evaluation": holdout_eval
    }
    
    with open(temp_manifest_path, "w") as f:
        json.dump(manifest_data, f, indent=2)
        
    os.replace(temp_manifest_path, best_manifest_path)

def seal_directory(path):
    for root, dirs, files in os.walk(path):
        for momo in dirs + files:
            os.chmod(os.path.join(root, momo), 0o555)
    os.chmod(path, 0o555)

def run_holdout_evaluation(best_prompt: str, run_id: str) -> dict:
    print(f"\n==========================================")
    print(f"Executing Unseen Private Holdout Evaluation Task")
    print(f"==========================================")
    
    private_task_dir = "/Users/heikkihamalainen/.gemini/antigravity/scratch/rsi_loop_private/benchmarks/state_machine"
    problem_desc_file = os.path.join(private_task_dir, "problem.txt")
    private_harness_file = os.path.join(private_task_dir, "test_holdout.py")
    
    if not os.path.exists(problem_desc_file) or not os.path.exists(private_harness_file):
        print("Private holdout files missing. Skipping holdout check.")
        return None
        
    # Copy problem description to separate temp directory inside trusted workspace to avoid exposure
    workspace_tmp = os.path.join(BASE_DIR, "tmp")
    os.makedirs(workspace_tmp, exist_ok=True)
    temp_gen_dir = tempfile.mkdtemp(prefix="holdout_gen_", dir=workspace_tmp)
    shutil.copy2(problem_desc_file, os.path.join(temp_gen_dir, "problem.txt"))
    
    with open(problem_desc_file, "r") as f:
        task_desc = f.read()
        
    # Generate solution cleanly from prompt
    solution_code, tokens = call_codex_sandboxed(task_desc, best_prompt, temp_gen_dir, expect_json_code=True)
    
    solution_file = os.path.join(temp_gen_dir, "solution.py")
    with open(solution_file, "w") as f:
        f.write(solution_code)
        
    # Evaluate securely on the host using sandboxed worker
    eval_res = run_evaluation_on_candidate(temp_gen_dir, private_harness_file)
    
    # Read public/private hashes
    with open(problem_desc_file, "rb") as f:
        prob_hash = hashlib.sha256(f.read()).hexdigest()
    with open(private_harness_file, "rb") as f:
        harness_hash = hashlib.sha256(f.read()).hexdigest()
        
    holdout_record = {
        "success": eval_res["success"],
        "error": eval_res["error"],
        "solution_hash": hashlib.sha256(solution_code.encode()).hexdigest(),
        "problem_hash": prob_hash,
        "private_harness_hash": harness_hash,
        "tokens_used": tokens,
        "timestamp": str(datetime.now())
    }
    
    # Save solution in the history folder
    run_holdout_dir = os.path.join(HISTORY_DIR, f"run_{run_id}", "holdout_evaluation")
    os.makedirs(run_holdout_dir, exist_ok=True)
    shutil.copy2(solution_file, os.path.join(run_holdout_dir, "solution.py"))
    
    with open(os.path.join(run_holdout_dir, "holdout_manifest.json"), "w") as f:
        json.dump(holdout_record, f, indent=2)
        
    # Clean temporary generation directory
    shutil.rmtree(temp_gen_dir, ignore_errors=True)
    
    print(f"Holdout Task Success: {eval_res['success']}")
    return holdout_record

def main():
    if not os.path.exists(BASELINE_PROMPT_PATH):
        print(f"Baseline prompt not found at {BASELINE_PROMPT_PATH}")
        sys.exit(1)
        
    with open(BASELINE_PROMPT_PATH, "r") as f:
        baseline_prompt = f.read()
        
    baseline_hash = hashlib.sha256(baseline_prompt.encode()).hexdigest()
    print(f"Starting Self-Improvement Loop. Baseline Hash: {baseline_hash}")
    
    run_id = f"{int(time.time())}_{baseline_hash[:8]}"
    iterations = 3
    best_score = -1.0
    best_prompt = baseline_prompt
    current_prompt = baseline_prompt
    
    # Greedy Policy: Causal feedback failures are tied strictly to best accepted prompt
    best_failures = []
    
    # Output Tokens limit budget
    total_token_budget = 15000
    consumed_tokens = 0
    
    for idx in range(iterations):
        iter_id = f"iter_{idx+1}"
        iter_dir = os.path.join(HISTORY_DIR, f"run_{run_id}", f"iteration_{iter_id}")
        os.makedirs(iter_dir, exist_ok=True)
        
        print(f"\n==========================================")
        print(f"ITERATION {idx+1} / {iterations} (Run ID: {run_id})")
        print(f"==========================================")
        
        score, results, eval_tokens = evaluate_prompt(current_prompt, run_id, iter_id)
        consumed_tokens += eval_tokens
        
        # Collect iteration failures
        iter_failures = []
        for task, res in results.items():
            if not res["success"]:
                task_file = os.path.join(BENCHMARKS_DIR, task, "problem.txt")
                solution_file = os.path.join(iter_dir, "benchmarks", task, "solution.py")
                with open(task_file, "r") as f:
                    desc = f.read()
                with open(solution_file, "r") as f:
                    code = f.read()
                iter_failures.append({
                    "task": task,
                    "description": desc,
                    "code": code,
                    "error": res["error"]
                })
        
        # Greedy Optimization Check
        if score > best_score:
            print(f"[+] Found improved prompt! (Old Best: {best_score*100:.1f}%, New: {score*100:.1f}%)")
            best_score = score
            best_prompt = current_prompt
            best_failures = iter_failures
            
            # Atomic promotion setup
            promote_manifest(run_id, iter_id, best_score, best_prompt)
        else:
            print(f"[-] No improvement. Retaining prior best prompt.")
            # Revert mutation search to current best prompt
            current_prompt = best_prompt
            
        manifest_data = {
            "run_id": run_id,
            "iteration_id": iter_id,
            "score": score,
            "tokens_consumed": eval_tokens,
            "results": results,
            "prompt": current_prompt
        }
        with open(os.path.join(iter_dir, "manifest.json"), "w") as f:
            json.dump(manifest_data, f, indent=2)
            
        # Seal iteration directory
        seal_directory(iter_dir)
        
        if best_score == 1.0:
            print("Achieved 100% success rate on benchmarks! Stopping loop.")
            break
            
        if consumed_tokens >= total_token_budget:
            print("Exceeded total token budget limit! Stopping loop.")
            break
            
        # Do not run mutation or create empty folders on the final iteration
        if idx < iterations - 1:
            next_iter_id = f"iter_{idx+2}"
            next_iter_dir = os.path.join(HISTORY_DIR, f"run_{run_id}", f"iteration_{next_iter_id}")
            os.makedirs(next_iter_dir, exist_ok=True)
            
            # Causal correction: Meta-Optimizer always mutates from best_prompt and its failure context
            mutation_prompt, mut_tokens = mutate_prompt(best_prompt, best_failures, run_id, next_iter_dir)
            consumed_tokens += mut_tokens
            current_prompt = mutation_prompt
            print(f"\nNew Prompt Proposed:\n{current_prompt[:200]}...")
            
    # Executing Private Unseen Task Evaluation post-selection on final best promoted prompt
    holdout_res = run_holdout_evaluation(best_prompt, run_id)
    if holdout_res:
        promote_manifest(run_id, iter_id, best_score, best_prompt, holdout_res)
        
    print(f"\n==========================================")
    print(f"Self-Improvement Loop Finished!")
    print(f"Best Score: {best_score*100:.1f}%")
    print(f"Total Tokens Consumed: {consumed_tokens}")
    print(f"==========================================")

if __name__ == "__main__":
    main()
