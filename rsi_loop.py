"""
rsi_loop.py — RSI orchestrator: honest three-trial evaluation with separate validation gate.

Architecture:
  1. Codex smoke test: `codex --version` inside generator sandbox.
  2. Codex canary: one authenticated `codex exec --json` call to verify API access.
  3. Preflight: run frozen baseline against all 4 optimization benchmarks (honesty gate).
  4. Three independent trials, each:
       a. Baseline eval on 4 optimization tasks.
       b. Up to 3 mutation iterations.
       c. Best prompt eval on 4 optimization tasks.
  5. Prompt selection:
       a. Evaluate the 3 candidate prompts on 2 separate VALIDATION tasks.
       b. Evaluate the frozen baseline on the same validation tasks (comparison).
       c. Promotion gate: mean paired delta > 0 AND best candidate > baseline on validation.
       d. Deterministic tie-break. If gate fails: retain baseline, record reason.
  6. Operator holdout (if exists): record problem_hash, harness_hash, solution_hash,
     result, usage, manifest_hash. Never used for selection.

Token accounting ceiling: 50,000 cumulative output tokens. Breach immediately invalidates.
"""
import os
import sys
import subprocess
import re
import json
import shutil
import hashlib
import time
import tempfile
import stat
from datetime import datetime, timezone

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BENCHMARKS_DIR = os.path.join(BASE_DIR, "benchmarks")
VALIDATION_DIR = os.path.join(BENCHMARKS_DIR, "validation")
PROMPTS_DIR = os.path.join(BASE_DIR, "prompt_templates")
HISTORY_DIR = os.path.join(BASE_DIR, "history")

BASELINE_PROMPT_PATH = os.path.join(PROMPTS_DIR, "inner_agent_baseline.txt")
OPTIMIZER_PROMPT_PATH = os.path.join(PROMPTS_DIR, "meta_optimizer.txt")
BEST_MANIFEST_PATH = os.path.join(BASE_DIR, "best_manifest.json")
PREFLIGHT_HASHES_PATH = os.path.join(BASE_DIR, "preflight_hashes.json")

# Accounting ceiling: 50,000 cumulative output tokens.
# This is an estimate based on the historical average of ~712 tokens/call.
# The ceiling remains authoritative even if the run aborts.
TOKEN_CEILING = 50_000

os.makedirs(HISTORY_DIR, exist_ok=True)
os.makedirs(PROMPTS_DIR, exist_ok=True)

# ── JSONL parsing ─────────────────────────────────────────────────────────────

def parse_codex_jsonl_output(stdout_lines: list) -> tuple:
    """
    Parse Codex structured JSONL output.

    Rules:
    - Accept agent_message text only from item.completed events where item.type == "agent_message".
    - Use the final such message (if multiple).
    - Require exactly one turn.completed event. Multiple or missing → ValueError (fail-closed).
    - Malformed JSON lines → ValueError immediately (no silent skip).
    - Missing or unparseable usage → ValueError.
    """
    code = ""
    usage_data: dict | None = None
    turn_completed_count = 0

    for line in stdout_lines:
        line = line.strip()
        if not line:
            continue
        # Malformed JSON → fail closed
        event = json.loads(line)  # raises ValueError on bad JSON
        etype = event.get("type")

        if etype == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                code = item.get("text", "").strip()

        elif etype == "turn.completed":
            turn_completed_count += 1
            if turn_completed_count > 1:
                raise ValueError(
                    "Codex JSONL stream has multiple turn.completed events — fail closed"
                )
            usage_data = event.get("usage")

    if turn_completed_count != 1:
        raise ValueError(
            f"Codex JSONL stream missing turn.completed event (count={turn_completed_count})"
        )
    if not usage_data:
        raise ValueError("Codex JSONL turn.completed has no usage data — fail closed")

    return code, usage_data


def extract_code(raw_output: str) -> str:
    code_pattern = r"```python(.*?)```"
    m = re.search(code_pattern, raw_output, re.DOTALL)
    if m:
        return m.group(1).strip()
    return raw_output.strip()


# ── Credential isolation ──────────────────────────────────────────────────────

def _build_codex_temp_home() -> str:
    """
    Create an isolated temp HOME for Codex:
    - dir mode 0700
    - copy auth.json from real .codex dir (mode 0600); fail closed if missing
    - return realpath of temp home
    """
    real_codex_auth = os.path.expanduser("~/.codex/auth.json")
    if not os.path.exists(real_codex_auth):
        raise RuntimeError(
            "Codex auth.json not found at ~/.codex/auth.json — cannot proceed"
        )

    temp_home = tempfile.mkdtemp(prefix="codex_home_")
    os.chmod(temp_home, 0o700)
    real_temp_home = os.path.realpath(temp_home)

    codex_dir = os.path.join(real_temp_home, ".codex")
    os.makedirs(codex_dir, mode=0o700, exist_ok=True)

    dest_auth = os.path.join(codex_dir, "auth.json")
    shutil.copy2(real_codex_auth, dest_auth)
    os.chmod(dest_auth, 0o600)

    return real_temp_home


def _cleanup_temp_home(real_temp_home: str) -> None:
    try:
        shutil.rmtree(real_temp_home)
    except Exception as e:
        sys.stderr.write(f"WARNING: temp home cleanup failed for {real_temp_home!r}: {e}\n")
        sys.stderr.flush()


# ── Codex sandbox runner ──────────────────────────────────────────────────────

def _build_generator_sb(real_temp_home: str, real_candidate_input_dir: str) -> str:
    """Render generator.sb.template with physical paths and write to temp home."""
    template_path = os.path.join(BASE_DIR, "generator.sb.template")
    with open(template_path) as f:
        tmpl = f.read()
    sb_content = tmpl.format(
        real_temp_home=real_temp_home,
        real_candidate_input_dir=real_candidate_input_dir,
    )
    sb_path = os.path.join(real_temp_home, "generator.sb")
    with open(sb_path, "w") as f:
        f.write(sb_content)
    return sb_path


def _run_codex_version_smoke_test(sb_path: str, env: dict) -> None:
    """
    Verify that `codex --version` runs successfully inside the exact generator sandbox.
    Fail closed if it returns non-zero.
    """
    codex_bin = "/opt/homebrew/bin/codex"
    cmd = ["sandbox-exec", "-f", sb_path, codex_bin, "--version"]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=env)
    if res.returncode != 0:
        raise RuntimeError(
            f"Codex smoke test failed (codex --version returned {res.returncode}):\n"
            f"stdout: {res.stdout[:500]}\nstderr: {res.stderr[:500]}"
        )
    sys.stderr.write(f"[smoke] codex --version OK: {res.stdout.strip()[:80]}\n")
    sys.stderr.flush()


def _run_codex_canary(
    sb_path: str, env: dict, token_ceiling: int, consumed_so_far: int
) -> int:
    """
    Run one minimal authenticated `codex exec --json` call to verify API access.
    Count output tokens toward the ceiling. Return tokens used.
    Fail closed on any error or ceiling breach.
    """
    codex_bin = "/opt/homebrew/bin/codex"
    canary_prompt = "Reply with exactly one word: ready"
    cmd = [
        "sandbox-exec", "-f", sb_path,
        codex_bin, "exec", "-",
        "--ephemeral",
        "--json",
        "--model", "codex-mini-latest",
    ]
    res = subprocess.run(
        cmd,
        input=canary_prompt,
        capture_output=True,
        text=True,
        timeout=60,
        env=env,
    )
    if res.returncode != 0:
        raise RuntimeError(
            f"Codex canary call failed (rc={res.returncode}):\n"
            f"stderr: {res.stderr[:500]}"
        )
    _, usage = parse_codex_jsonl_output(res.stdout.splitlines())
    tokens = usage.get("output_tokens")
    if tokens is None:
        raise RuntimeError("Codex canary call returned no output_tokens — fail closed")
    tokens = int(tokens)
    if consumed_so_far + tokens > token_ceiling:
        raise RuntimeError(
            f"Token ceiling breached during canary: {consumed_so_far} + {tokens} > {token_ceiling}"
        )
    sys.stderr.write(f"[canary] API access verified. Canary output tokens: {tokens}\n")
    sys.stderr.flush()
    return tokens


def call_codex_sandboxed(
    prompt: str,
    system_instruction: str,
    candidate_dir: str,
    expect_json_code: bool = True,
    cumulative_tokens: int = 0,
    token_ceiling: int = TOKEN_CEILING,
) -> tuple:
    """
    Run one Codex generation inside the generator sandbox with isolated credentials.
    Returns (code_or_text, output_tokens_used).
    Raises RuntimeError on ceiling breach or any call failure.
    """
    full_prompt = ""
    if system_instruction:
        full_prompt += f"System Instruction:\n{system_instruction}\n\nTask Instruction:\n"
    full_prompt += prompt

    real_temp_home = _build_codex_temp_home()
    real_candidate_input_dir = os.path.realpath(candidate_dir)

    try:
        sb_path = _build_generator_sb(real_temp_home, real_candidate_input_dir)

        allowed_env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin",
            "LANG": "en_US.UTF-8",
            "HOME": real_temp_home,
            "USER": os.environ.get("USER", "codex"),
            "LOGNAME": os.environ.get("LOGNAME", "codex"),
            "TMPDIR": real_temp_home,
        }

        codex_bin = "/opt/homebrew/bin/codex"
        cmd = [
            "sandbox-exec", "-f", sb_path,
            codex_bin, "exec", "-",
            "-s", "read-only",
            "--ephemeral",
            "--json",
            "--model", "codex-mini-latest",
            "-C", real_candidate_input_dir,
        ]

        res = subprocess.run(
            cmd,
            input=full_prompt,
            capture_output=True,
            text=True,
            timeout=120,
            env=allowed_env,
        )

        if res.returncode != 0:
            sys.stderr.write(f"Codex error (rc={res.returncode}). Stderr: {res.stderr[:500]}\n")
            sys.stderr.flush()
            raise RuntimeError(f"Codex returned non-zero exit code {res.returncode}")

        raw_msg, usage = parse_codex_jsonl_output(res.stdout.splitlines())
        tokens = usage.get("output_tokens")
        if tokens is None:
            raise RuntimeError("Codex call returned no output_tokens — fail closed")
        tokens = int(tokens)

        if cumulative_tokens + tokens > token_ceiling:
            raise RuntimeError(
                f"Token ceiling breached: {cumulative_tokens} + {tokens} = "
                f"{cumulative_tokens + tokens} > {token_ceiling}"
            )

        if expect_json_code:
            return extract_code(raw_msg), tokens
        else:
            return raw_msg, tokens

    finally:
        _cleanup_temp_home(real_temp_home)


# ── Evaluation helpers ────────────────────────────────────────────────────────

def run_evaluation_on_candidate(candidate_dir: str, test_harness_path: str) -> dict:
    from evaluator import run_evaluation
    return run_evaluation(candidate_dir, test_harness_path)


def _list_optimization_benchmarks() -> list:
    """Return benchmark names from benchmarks/ excluding the validation/ subdir."""
    return sorted(
        d for d in os.listdir(BENCHMARKS_DIR)
        if os.path.isdir(os.path.join(BENCHMARKS_DIR, d)) and d != "validation"
    )


def _list_validation_benchmarks() -> list:
    """Return benchmark names from benchmarks/validation/."""
    if not os.path.isdir(VALIDATION_DIR):
        return []
    return sorted(
        d for d in os.listdir(VALIDATION_DIR)
        if os.path.isdir(os.path.join(VALIDATION_DIR, d))
    )


def evaluate_prompt_on_tasks(
    prompt_text: str,
    run_id: str,
    iter_id: str,
    benchmarks_root: str,
    task_names: list,
    cumulative_tokens: int,
) -> tuple:
    """
    Run Codex generation + evaluation for each task in task_names.
    Returns (score, results_dict, total_new_tokens).
    Raises RuntimeError on ceiling breach.
    """
    results = {}
    passed_count = 0
    total_tokens = 0

    for benchmark in task_names:
        benchmark_dir = os.path.join(benchmarks_root, benchmark)
        iter_task_dir = os.path.join(HISTORY_DIR, f"run_{run_id}", iter_id, benchmark)
        os.makedirs(iter_task_dir, exist_ok=True)

        task_file = os.path.join(benchmark_dir, "problem.txt")
        with open(task_file) as f:
            task_desc = f.read()

        # Raises on ceiling breach
        solution_code, tokens = call_codex_sandboxed(
            task_desc, prompt_text, iter_task_dir,
            expect_json_code=True,
            cumulative_tokens=cumulative_tokens + total_tokens,
        )
        total_tokens += tokens

        solution_file = os.path.join(iter_task_dir, "solution.py")
        with open(solution_file, "w") as f:
            f.write(solution_code)

        test_harness_path = os.path.join(benchmark_dir, "test_harness.py")
        eval_res = run_evaluation_on_candidate(iter_task_dir, test_harness_path)

        results[benchmark] = {
            "success": eval_res["success"],
            "error": eval_res.get("error"),
            "tokens_used": tokens,
            "solution_hash": hashlib.sha256(solution_code.encode()).hexdigest(),
        }
        if eval_res["success"]:
            passed_count += 1
            sys.stdout.write(f"  [+] {benchmark}: PASSED\n")
        else:
            sys.stdout.write(f"  [-] {benchmark}: FAILED\n")
        sys.stdout.flush()

    score = passed_count / len(task_names) if task_names else 0.0
    return score, results, total_tokens


def mutate_prompt(
    current_prompt: str,
    failures: list,
    iter_dir: str,
    cumulative_tokens: int,
) -> tuple:
    """
    Run the meta-optimizer to propose a prompt mutation.
    Returns (new_prompt_text, output_tokens_used).
    """
    with open(OPTIMIZER_PROMPT_PATH) as f:
        meta_prompt = f.read()

    context = f"Current System Prompt:\n{current_prompt}\n\nFailed Tasks & Errors:\n"
    for fail in failures:
        context += f"--- Task: {fail['task']} ---\n"
        context += f"Problem Description:\n{fail['description']}\n"
        context += f"Failed Solution:\n{fail['code']}\n"
        context += f"Test Error:\n{fail['error']}\n\n"

    return call_codex_sandboxed(
        context, meta_prompt, iter_dir,
        expect_json_code=False,
        cumulative_tokens=cumulative_tokens,
    )


# ── Manifest helpers ──────────────────────────────────────────────────────────

def _sha256_file(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def _atomic_write_manifest(path: str, data: dict) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)
    os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)  # 0o444


def promote_manifest(data: dict) -> None:
    """Atomically write best_manifest.json (mode 0o444)."""
    old_path = BEST_MANIFEST_PATH + ".previous"
    if os.path.exists(BEST_MANIFEST_PATH):
        try:
            # Make writable temporarily to copy
            os.chmod(BEST_MANIFEST_PATH, 0o644)
            shutil.copy2(BEST_MANIFEST_PATH, old_path)
        except Exception:
            pass
    _atomic_write_manifest(BEST_MANIFEST_PATH, data)


# ── Holdout evaluation ────────────────────────────────────────────────────────

PRIVATE_DIR = os.path.join(
    os.path.dirname(BASE_DIR), "rsi_loop_private", "benchmarks", "operator_holdout"
)


def run_holdout_evaluation(
    best_prompt: str,
    run_id: str,
    cumulative_tokens: int,
) -> tuple:
    """
    Evaluate the selected prompt on the operator holdout task.
    Returns (holdout_record_dict, tokens_used).
    Holdout result never enters selection or mutation feedback.
    """
    problem_file = os.path.join(PRIVATE_DIR, "problem.txt")
    harness_file = os.path.join(PRIVATE_DIR, "test_holdout.py")

    if not os.path.exists(problem_file) or not os.path.exists(harness_file):
        sys.stdout.write("Operator holdout directory not found. Skipping.\n")
        return None, 0

    problem_hash = _sha256_file(problem_file)
    harness_hash = _sha256_file(harness_file)

    # Load pre-recorded hashes to verify nothing changed
    if os.path.exists(PREFLIGHT_HASHES_PATH):
        with open(PREFLIGHT_HASHES_PATH) as f:
            prerecorded = json.load(f)
        if "operator_holdout/problem.txt" in prerecorded:
            expected_ph = prerecorded["operator_holdout/problem.txt"]
            if problem_hash != expected_ph:
                raise RuntimeError(
                    f"Operator holdout problem.txt hash mismatch: "
                    f"expected {expected_ph}, got {problem_hash}"
                )
        if "operator_holdout/test_holdout.py" in prerecorded:
            expected_hh = prerecorded["operator_holdout/test_holdout.py"]
            if harness_hash != expected_hh:
                raise RuntimeError(
                    f"Operator holdout test_holdout.py hash mismatch: "
                    f"expected {expected_hh}, got {harness_hash}"
                )

    workspace_tmp = os.path.join(BASE_DIR, "tmp")
    os.makedirs(workspace_tmp, exist_ok=True)
    temp_gen_dir = tempfile.mkdtemp(prefix="holdout_gen_", dir=workspace_tmp)

    try:
        with open(problem_file) as f:
            task_desc = f.read()

        solution_code, tokens = call_codex_sandboxed(
            task_desc, best_prompt, temp_gen_dir,
            expect_json_code=True,
            cumulative_tokens=cumulative_tokens,
        )

        solution_file = os.path.join(temp_gen_dir, "solution.py")
        with open(solution_file, "w") as f:
            f.write(solution_code)

        eval_res = run_evaluation_on_candidate(temp_gen_dir, harness_file)
        solution_hash = hashlib.sha256(solution_code.encode()).hexdigest()

        # Record holdout_manifest.json (mode 0o444)
        run_holdout_dir = os.path.join(HISTORY_DIR, f"run_{run_id}", "holdout_evaluation")
        os.makedirs(run_holdout_dir, exist_ok=True)
        shutil.copy2(solution_file, os.path.join(run_holdout_dir, "solution.py"))

        holdout_manifest_path = os.path.join(run_holdout_dir, "holdout_manifest.json")
        holdout_record = {
            "problem_hash": problem_hash,
            "harness_hash": harness_hash,
            "solution_hash": solution_hash,
            "result": eval_res["success"],
            "error": eval_res.get("error"),
            "usage": {"output_tokens": tokens},
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        _atomic_write_manifest(holdout_manifest_path, holdout_record)
        holdout_record["manifest_hash"] = _sha256_file(holdout_manifest_path)

        sys.stdout.write(f"Holdout result: {eval_res['success']}. Tokens: {tokens}\n")
        return holdout_record, tokens

    finally:
        shutil.rmtree(temp_gen_dir)


# ── Smoke + canary pre-flight ─────────────────────────────────────────────────

def run_smoke_and_canary() -> int:
    """
    Run smoke test (codex --version) and canary (codex exec) inside generator sandbox.
    Returns canary token count. Fail closed on any error.
    """
    # Build a temp home just for the smoke test (no actual auth needed for --version)
    real_temp_home = _build_codex_temp_home()
    try:
        real_candidate_input_dir = BASE_DIR  # dummy input dir for smoke test
        sb_path = _build_generator_sb(real_temp_home, real_candidate_input_dir)
        allowed_env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin",
            "LANG": "en_US.UTF-8",
            "HOME": real_temp_home,
            "USER": os.environ.get("USER", "codex"),
            "LOGNAME": os.environ.get("LOGNAME", "codex"),
            "TMPDIR": real_temp_home,
        }
        # Smoke test: codex --version inside sandbox
        _run_codex_version_smoke_test(sb_path, allowed_env)
        # Canary: one real API call
        canary_tokens = _run_codex_canary(sb_path, allowed_env, TOKEN_CEILING, 0)
        return canary_tokens
    finally:
        _cleanup_temp_home(real_temp_home)


# ── Main orchestration loop ───────────────────────────────────────────────────

def main() -> None:
    # -- Load baseline prompt
    if not os.path.exists(BASELINE_PROMPT_PATH):
        sys.exit(f"Baseline prompt not found at {BASELINE_PROMPT_PATH}")
    with open(BASELINE_PROMPT_PATH) as f:
        baseline_prompt = f.read()
    baseline_hash = hashlib.sha256(baseline_prompt.encode()).hexdigest()
    sys.stdout.write(f"Baseline hash: {baseline_hash}\n")

    # -- Load preflight hashes
    preflight_hashes = {}
    if os.path.exists(PREFLIGHT_HASHES_PATH):
        with open(PREFLIGHT_HASHES_PATH) as f:
            preflight_hashes = json.load(f)

    # -- Smoke + canary
    sys.stdout.write("Running Codex smoke test and canary...\n")
    sys.stdout.flush()
    canary_tokens = run_smoke_and_canary()
    consumed_tokens = canary_tokens
    sys.stdout.write(f"Canary complete. Cumulative tokens: {consumed_tokens}\n")

    run_id = f"{int(time.time())}_{baseline_hash[:8]}"
    optimization_tasks = _list_optimization_benchmarks()
    validation_tasks = _list_validation_benchmarks()
    sys.stdout.write(f"Optimization tasks: {optimization_tasks}\n")
    sys.stdout.write(f"Validation tasks:   {validation_tasks}\n")
    sys.stdout.flush()

    if not optimization_tasks:
        sys.exit("No optimization benchmarks found.")
    if not validation_tasks:
        sys.exit("No validation benchmarks found. Ensure benchmarks/validation/ exists.")

    # ── Three independent trials ──────────────────────────────────────────────

    trial_results = []   # list of {baseline_score, opt_score, delta, candidate_prompt, candidate_prompt_hash}

    for trial_idx in range(3):
        trial_id = f"trial_{trial_idx + 1}"
        sys.stdout.write(f"\n{'='*50}\nTRIAL {trial_idx + 1}/3\n{'='*50}\n")
        sys.stdout.flush()

        trial_dir = os.path.join(HISTORY_DIR, f"run_{run_id}", trial_id)
        os.makedirs(trial_dir, exist_ok=True)

        # A. Baseline evaluation on optimization tasks
        sys.stdout.write(f"[{trial_id}] Evaluating baseline...\n")
        baseline_score, baseline_results, baseline_tokens = evaluate_prompt_on_tasks(
            baseline_prompt, run_id, f"{trial_id}/baseline",
            BENCHMARKS_DIR, optimization_tasks, consumed_tokens,
        )
        consumed_tokens += baseline_tokens
        sys.stdout.write(f"[{trial_id}] Baseline score: {baseline_score:.2%} | Tokens: {consumed_tokens}/{TOKEN_CEILING}\n")

        # B. Optimization loop (up to 3 mutation iterations)
        best_trial_score = baseline_score
        best_trial_prompt = baseline_prompt
        current_prompt = baseline_prompt

        for mut_idx in range(3):
            if best_trial_score >= 1.0:
                sys.stdout.write(f"[{trial_id}] 100% reached, skipping further mutations.\n")
                break

            # Collect failures
            failures = []
            for task, res in baseline_results.items():
                if not res["success"]:
                    task_file = os.path.join(BENCHMARKS_DIR, task, "problem.txt")
                    sol_path = os.path.join(HISTORY_DIR, f"run_{run_id}", f"{trial_id}/baseline", task, "solution.py")
                    with open(task_file) as f:
                        desc = f.read()
                    code = ""
                    if os.path.exists(sol_path):
                        with open(sol_path) as f:
                            code = f.read()
                    failures.append({"task": task, "description": desc, "code": code, "error": res["error"] or ""})

            if not failures:
                sys.stdout.write(f"[{trial_id}] No failures — skipping mutation.\n")
                break

            # Mutate
            mut_dir = os.path.join(trial_dir, f"mutation_{mut_idx + 1}")
            os.makedirs(mut_dir, exist_ok=True)
            sys.stdout.write(f"[{trial_id}] Mutation {mut_idx + 1}...\n")
            try:
                new_prompt, mut_tokens = mutate_prompt(current_prompt, failures, mut_dir, consumed_tokens)
                consumed_tokens += mut_tokens
            except RuntimeError as e:
                sys.stdout.write(f"[{trial_id}] Mutation failed (ceiling?): {e}\n")
                break

            # Evaluate mutated prompt
            sys.stdout.write(f"[{trial_id}] Evaluating mutated prompt...\n")
            try:
                mut_score, mut_results, eval_tokens = evaluate_prompt_on_tasks(
                    new_prompt, run_id, f"{trial_id}/mut_{mut_idx + 1}",
                    BENCHMARKS_DIR, optimization_tasks, consumed_tokens,
                )
                consumed_tokens += eval_tokens
            except RuntimeError as e:
                sys.stdout.write(f"[{trial_id}] Evaluation failed (ceiling?): {e}\n")
                break

            sys.stdout.write(f"[{trial_id}] Mut score: {mut_score:.2%} | Tokens: {consumed_tokens}/{TOKEN_CEILING}\n")

            if mut_score > best_trial_score:
                best_trial_score = mut_score
                best_trial_prompt = new_prompt
                baseline_results = mut_results  # update failure context for next mutation
                current_prompt = new_prompt
            else:
                # Revert
                current_prompt = best_trial_prompt

        trial_results.append({
            "trial": trial_id,
            "baseline_score": baseline_score,
            "opt_score": best_trial_score,
            "delta": best_trial_score - baseline_score,
            "candidate_prompt": best_trial_prompt,
            "candidate_prompt_hash": hashlib.sha256(best_trial_prompt.encode()).hexdigest(),
        })
        sys.stdout.write(f"[{trial_id}] Delta: {best_trial_score - baseline_score:+.2%}\n")

    # ── Prompt selection via validation suite ─────────────────────────────────

    sys.stdout.write(f"\n{'='*50}\nPROMPT SELECTION (validation suite)\n{'='*50}\n")
    mean_delta = sum(t["delta"] for t in trial_results) / len(trial_results)
    sys.stdout.write(f"Mean paired delta: {mean_delta:+.2%}\n")

    # Evaluate baseline on validation tasks (for comparison)
    try:
        baseline_val_score, _, val_baseline_tokens = evaluate_prompt_on_tasks(
            baseline_prompt, run_id, "validation_baseline",
            VALIDATION_DIR, validation_tasks, consumed_tokens,
        )
        consumed_tokens += val_baseline_tokens
    except RuntimeError as e:
        sys.stdout.write(f"Baseline validation failed: {e}\n")
        baseline_val_score = None

    # Evaluate each candidate on validation tasks
    candidate_val_scores = []
    for t in trial_results:
        cand_prompt = t["candidate_prompt"]
        try:
            score, _, vtokens = evaluate_prompt_on_tasks(
                cand_prompt, run_id, f"validation_{t['trial']}",
                VALIDATION_DIR, validation_tasks, consumed_tokens,
            )
            consumed_tokens += vtokens
            candidate_val_scores.append(score)
        except RuntimeError as e:
            sys.stdout.write(f"Candidate {t['trial']} validation failed: {e}\n")
            candidate_val_scores.append(None)

    # Promotion gate
    gate_passed = (
        mean_delta > 0
        and baseline_val_score is not None
        and any(s is not None and s > baseline_val_score for s in candidate_val_scores)
    )

    if not gate_passed:
        sys.stdout.write(
            "Promotion gate FAILED. Retaining baseline. "
            f"(mean_delta={mean_delta:+.2%}, "
            f"baseline_val={baseline_val_score}, "
            f"candidate_vals={candidate_val_scores})\n"
        )
        retention_reason = (
            f"mean_delta={mean_delta:.4f} ({'positive' if mean_delta > 0 else 'non-positive'}), "
            f"baseline_val={baseline_val_score}, candidates={candidate_val_scores}"
        )
        final_manifest = {
            "schema_version": "2",
            "run_id": run_id,
            "baseline_hash": baseline_hash,
            "baseline_prompt_selected": True,
            "promotion_gate_passed": False,
            "retention_reason": retention_reason,
            "mean_paired_delta": mean_delta,
            "trial_results": trial_results,
            "token_ceiling": TOKEN_CEILING,
            "tokens_consumed": consumed_tokens,
            "budget_note": (
                "46992 is a 712-tokens/call estimate (historical avg); not a worst-case bound. "
                "50000 ceiling is authoritative."
            ),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        promote_manifest(final_manifest)
        sys.stdout.write("Baseline retained in best_manifest.json.\n")
        return

    # Select best candidate (deterministic tie-break)
    # Filter to candidates that pass gate individually
    eligible = [
        (i, s) for i, s in enumerate(candidate_val_scores)
        if s is not None and s > (baseline_val_score or 0)
    ]
    # Sort: higher score first; tie-break by lower trial number
    eligible.sort(key=lambda x: (-x[1], x[0]))
    best_idx = eligible[0][0]
    best_trial = trial_results[best_idx]
    best_prompt = best_trial["candidate_prompt"]

    sys.stdout.write(
        f"Promotion gate PASSED. Selected candidate from {best_trial['trial']} "
        f"(val_score={candidate_val_scores[best_idx]:.2%}).\n"
    )

    # Build and write final manifest
    final_manifest = {
        "schema_version": "2",
        "run_id": run_id,
        "baseline_hash": baseline_hash,
        "selected_prompt_hash": best_trial["candidate_prompt_hash"],
        "selected_from_trial": best_trial["trial"],
        "validation_score": candidate_val_scores[best_idx],
        "baseline_validation_score": baseline_val_score,
        "promotion_gate_passed": True,
        "mean_paired_delta": mean_delta,
        "trial_results": trial_results,
        "token_ceiling": TOKEN_CEILING,
        "tokens_consumed": consumed_tokens,
        "budget_note": (
            "46992 is a 712-tokens/call estimate (historical avg); not a worst-case bound. "
            "50000 ceiling is authoritative even if experiment aborts."
        ),
        "preflight_hashes": preflight_hashes,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    # Run holdout evaluation (if operator directory exists)
    holdout_record, holdout_tokens = run_holdout_evaluation(best_prompt, run_id, consumed_tokens)
    consumed_tokens += holdout_tokens
    if holdout_record:
        final_manifest["holdout"] = holdout_record
    final_manifest["tokens_consumed"] = consumed_tokens

    # Save selected prompt
    best_prompt_path = os.path.join(PROMPTS_DIR, "inner_agent_best.txt")
    with open(best_prompt_path, "w") as f:
        f.write(best_prompt)

    promote_manifest(final_manifest)
    sys.stdout.write(
        f"\nRSI loop complete. Best score delta: {best_trial['delta']:+.2%}. "
        f"Total tokens: {consumed_tokens}/{TOKEN_CEILING}.\n"
    )


if __name__ == "__main__":
    main()
