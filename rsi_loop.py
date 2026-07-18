"""
rsi_loop.py — RSI orchestrator: honest three-trial evaluation with separate validation gate.
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
import uuid

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BENCHMARKS_DIR = os.path.join(BASE_DIR, "benchmarks")
VALIDATION_DIR = os.path.join(BENCHMARKS_DIR, "validation")
PROMPTS_DIR = os.path.join(BASE_DIR, "prompt_templates")
HISTORY_DIR = os.path.join(BASE_DIR, "history")

BASELINE_PROMPT_PATH = os.path.join(PROMPTS_DIR, "inner_agent_baseline.txt")
OPTIMIZER_PROMPT_PATH = os.path.join(PROMPTS_DIR, "meta_optimizer.txt")
BEST_MANIFEST_PATH = os.path.join(BASE_DIR, "best_manifest.json")
PREFLIGHT_HASHES_PATH = os.path.join(BASE_DIR, "preflight_hashes.json")

TOKEN_CEILING = 50_000

os.makedirs(HISTORY_DIR, exist_ok=True)
os.makedirs(PROMPTS_DIR, exist_ok=True)

class ExperimentInvalidError(Exception):
    """Raised when the experiment is invalidated due to infrastructure failures."""
    pass

# ── JSONL parsing ─────────────────────────────────────────────────────────────

def parse_codex_jsonl_output(stdout_lines: list) -> tuple:
    """
    Parse Codex structured JSONL output.
    """
    code = ""
    usage_data: dict | None = None
    turn_completed_count = 0

    for line in stdout_lines:
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except Exception as e:
            raise ExperimentInvalidError(f"Malformed JSON line: {line!r} — fail closed: {e}")

        etype = event.get("type")

        if etype == "item.completed":
            item = event.get("item", {})
            if item.get("type") == "agent_message":
                code = item.get("text", "").strip()

        elif etype == "turn.completed":
            turn_completed_count += 1
            if turn_completed_count > 1:
                raise ExperimentInvalidError(
                    "Codex JSONL stream has multiple turn.completed events — fail closed"
                )
            usage_data = event.get("usage")

    if turn_completed_count != 1:
        raise ExperimentInvalidError(
            f"Codex JSONL stream missing turn.completed event (count={turn_completed_count})"
        )
    if not usage_data:
        raise ExperimentInvalidError("Codex JSONL turn.completed has no usage data — fail closed")

    return code, usage_data

def extract_code(raw_output: str) -> str:
    code_pattern = r"```python(.*?)```"
    m = re.search(code_pattern, raw_output, re.DOTALL)
    if m:
        return m.group(1).strip()
    return raw_output.strip()

# ── Credential isolation ──────────────────────────────────────────────────────

def _build_codex_temp_home() -> str:
    real_codex_auth = os.path.expanduser("~/.codex/auth.json")
    if not os.path.exists(real_codex_auth):
        raise ExperimentInvalidError(
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

def _run_codex_version_smoke_test(sb_path: str, env: dict) -> str:
    codex_bin = "/opt/homebrew/bin/codex"
    cmd = ["sandbox-exec", "-f", sb_path, codex_bin, "--version"]
    res = subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=env)
    if res.returncode != 0:
        raise ExperimentInvalidError(
            f"Codex smoke test failed (codex --version returned {res.returncode}):\n"
            f"stderr: {res.stderr[:500]}"
        )
    version = res.stdout.strip()
    sys.stderr.write(f"[smoke] codex --version OK: {version}\n")
    sys.stderr.flush()
    return version

def _run_codex_canary(
    sb_path: str, env: dict, token_ceiling: int, consumed_so_far: int
) -> int:
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
        raise ExperimentInvalidError(
            f"Codex canary call failed (rc={res.returncode}):\nstderr: {res.stderr[:500]}"
        )
    _, usage = parse_codex_jsonl_output(res.stdout.splitlines())
    tokens = usage.get("output_tokens")
    if tokens is None:
        raise ExperimentInvalidError("Codex canary call returned no output_tokens — fail closed")
    tokens = int(tokens)
    if consumed_so_far + tokens > token_ceiling:
        raise ExperimentInvalidError(
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
            raise ExperimentInvalidError(f"Codex returned non-zero exit code {res.returncode}. Stderr: {res.stderr[:500]}")

        raw_msg, usage = parse_codex_jsonl_output(res.stdout.splitlines())
        tokens = usage.get("output_tokens")
        if tokens is None:
            raise ExperimentInvalidError("Codex call returned no output_tokens — fail closed")
        tokens = int(tokens)

        if cumulative_tokens + tokens > token_ceiling:
            raise ExperimentInvalidError(
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
    # run_evaluation handles exceptions internally; HarnessInfrastructureError is raised
    return run_evaluation(candidate_dir, test_harness_path)

def _list_optimization_benchmarks() -> list:
    return sorted(
        d for d in os.listdir(BENCHMARKS_DIR)
        if os.path.isdir(os.path.join(BENCHMARKS_DIR, d)) and d != "validation"
    )

def _list_validation_benchmarks() -> list:
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

# ── Write-Once Fsynced Manifest Helpers ───────────────────────────────────────

def _sha256_file(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()

def _write_once_fsynced_manifest(path: str, data: dict) -> None:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ExperimentInvalidError(f"Manifest already exists at: {path}")

    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        raise ExperimentInvalidError(f"Failed to write fsynced manifest: {e}")

    os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)  # 0o444

def load_and_verify_best_manifest() -> dict | None:
    if not os.path.exists(BEST_MANIFEST_PATH):
        return None
    try:
        with open(BEST_MANIFEST_PATH) as f:
            ptr = json.load(f)
        if ptr.get("pointer_schema_version") != "2":
            return ptr
        path = os.path.join(BASE_DIR, ptr["run_manifest_path"])
        expected_hash = ptr["run_manifest_sha256"]
        if not os.path.exists(path):
            raise RuntimeError(f"Manifest file missing: {path}")
        actual_hash = _sha256_file(path)
        if actual_hash != expected_hash:
            raise RuntimeError(f"Manifest hash mismatch: expected {expected_hash}, got {actual_hash}")
        with open(path) as f:
            return json.load(f)
    except Exception as e:
        sys.stderr.write(f"WARNING: load_and_verify_best_manifest error: {e}\n")
        return None

def update_best_manifest_pointer(manifest_path: str) -> None:
    manifest_hash = _sha256_file(manifest_path)
    rel_path = os.path.relpath(manifest_path, BASE_DIR)
    pointer_data = {
        "pointer_schema_version": "2",
        "run_manifest_path": rel_path,
        "run_manifest_sha256": manifest_hash
    }
    tmp = BEST_MANIFEST_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(pointer_data, f, indent=2)
    os.replace(tmp, BEST_MANIFEST_PATH)

# ── Verification Checkers ─────────────────────────────────────────────────────

def verify_experiment_lock() -> str:
    try:
        res = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd=BASE_DIR, check=True
        )
        if res.stdout.strip():
            raise ExperimentInvalidError("Git worktree is not clean — fail closed")
    except Exception as e:
        if isinstance(e, ExperimentInvalidError):
            raise
        raise ExperimentInvalidError(f"Failed to check git status: {e}")

    lock_path = os.path.join(BASE_DIR, "experiment.lock")
    if not os.path.exists(lock_path):
        raise ExperimentInvalidError("experiment.lock file missing — fail closed")
    try:
        with open(lock_path) as f:
            lock_data = json.load(f)
    except Exception as e:
        raise ExperimentInvalidError(f"Failed to read experiment.lock: {e}")

    approved_sha = lock_data.get("approved_commit_sha")
    if not approved_sha:
        raise ExperimentInvalidError("approved_commit_sha missing from experiment.lock")

    try:
        res_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=BASE_DIR, check=True
        )
        current_sha = res_head.stdout.strip()
    except Exception as e:
        raise ExperimentInvalidError(f"Failed to get git HEAD: {e}")

    if current_sha != approved_sha:
        raise ExperimentInvalidError(
            f"Git HEAD mismatch: current HEAD is {current_sha}, "
            f"but approved commit SHA in experiment.lock is {approved_sha}"
        )
    return approved_sha

def verify_preflight_hashes():
    if not os.path.exists(PREFLIGHT_HASHES_PATH):
        raise ExperimentInvalidError("preflight_hashes.json missing")
    with open(PREFLIGHT_HASHES_PATH) as f:
        preflight_hashes = json.load(f)

    expected_keys = {
        "prompt_templates/inner_agent_baseline.txt",
        "prompt_templates/meta_optimizer.txt",
        "benchmarks/circular_buffer/problem.txt",
        "benchmarks/circular_buffer/test_harness.py",
        "benchmarks/json_parser/problem.txt",
        "benchmarks/json_parser/test_harness.py",
        "benchmarks/run_length_encoding/problem.txt",
        "benchmarks/run_length_encoding/test_harness.py",
        "benchmarks/huffman_coding/problem.txt",
        "benchmarks/huffman_coding/test_harness.py",
        "benchmarks/validation/string_calculator/problem.txt",
        "benchmarks/validation/string_calculator/test_harness.py",
        "benchmarks/validation/matrix_operations/problem.txt",
        "benchmarks/validation/matrix_operations/test_harness.py",
        "candidate.sb.template",
        "generator.sb.template",
        "worker.sb"
    }

    actual_keys = set(preflight_hashes.keys())
    if expected_keys != actual_keys:
        raise ExperimentInvalidError(f"Preflight keys mismatch. Missing: {expected_keys - actual_keys}, Extra: {actual_keys - expected_keys}")

    for rel_path, expected_hash in preflight_hashes.items():
        if ".." in rel_path or rel_path.startswith("/"):
            raise ExperimentInvalidError(f"Path traversal detected in preflight hash key: {rel_path}")
        full_path = os.path.join(BASE_DIR, rel_path)
        if os.path.islink(full_path):
            raise ExperimentInvalidError(f"Symlink detected in preflight file: {full_path}")
        if not os.path.exists(full_path):
            raise ExperimentInvalidError(f"Preflight file missing: {full_path}")
        actual_hash = _sha256_file(full_path)
        if actual_hash != expected_hash:
            raise ExperimentInvalidError(f"Hash mismatch for {rel_path}: expected {expected_hash}, got {actual_hash}")

def verify_holdout_hashes(private_dir: str):
    holdout_manifest_path = os.path.join(private_dir, "holdout_hashes.json")
    if not os.path.exists(holdout_manifest_path):
        raise ExperimentInvalidError("Private holdout exists but holdout_hashes.json is missing — fail closed")
    try:
        with open(holdout_manifest_path) as f:
            holdout_hashes = json.load(f)
    except Exception as e:
        raise ExperimentInvalidError(f"Failed to read holdout_hashes.json: {e}")

    expected_holdout_keys = {
        "operator_holdout/problem.txt",
        "operator_holdout/test_holdout.py",
        "simple_regex_matcher/problem.txt",
        "simple_regex_matcher/test_harness.py",
        "simple_regex_matcher/test_holdout.py"
    }
    actual_holdout_keys = set(holdout_hashes.keys())
    if expected_holdout_keys != actual_holdout_keys:
        raise ExperimentInvalidError(
            f"Holdout hashes keys mismatch. Missing: {expected_holdout_keys - actual_holdout_keys}, "
            f"Extra: {actual_holdout_keys - expected_holdout_keys}"
        )

    for rel_path, expected_hash in holdout_hashes.items():
        if ".." in rel_path or rel_path.startswith("/"):
            raise ExperimentInvalidError(f"Path traversal detected in holdout hash key: {rel_path}")
        full_path = os.path.join(private_dir, rel_path)
        if os.path.islink(full_path):
            raise ExperimentInvalidError(f"Symlink detected in holdout file: {full_path}")
        if not os.path.exists(full_path):
            raise ExperimentInvalidError(f"Holdout file missing: {full_path}")
        actual_hash = _sha256_file(full_path)
        if actual_hash != expected_hash:
            raise ExperimentInvalidError(f"Hash mismatch for holdout {rel_path}: expected {expected_hash}, got {actual_hash}")

# ── Private Holdout evaluation ────────────────────────────────────────────────

PRIVATE_DIR = os.path.join(
    os.path.dirname(BASE_DIR), "rsi_loop_private", "benchmarks"
)

def run_holdout_evaluation(
    best_prompt: str,
    run_id: str,
    cumulative_tokens: int,
) -> tuple:
    problem_file = os.path.join(PRIVATE_DIR, "operator_holdout", "problem.txt")
    harness_file = os.path.join(PRIVATE_DIR, "operator_holdout", "test_holdout.py")

    # If simple_regex_matcher exists but not operator_holdout, check hashes of rsi_loop_private
    if not os.path.exists(PRIVATE_DIR):
        return None, 0

    verify_holdout_hashes(PRIVATE_DIR)

    if not os.path.exists(problem_file) or not os.path.exists(harness_file):
        sys.stdout.write("Operator holdout files not found. Skipping.\n")
        return None, 0

    problem_hash = _sha256_file(problem_file)
    harness_hash = _sha256_file(harness_file)

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
        _write_once_fsynced_manifest(holdout_manifest_path, holdout_record)
        holdout_record["manifest_hash"] = _sha256_file(holdout_manifest_path)

        sys.stdout.write(f"Holdout result: {eval_res['success']}. Tokens: {tokens}\n")
        return holdout_record, tokens

    finally:
        shutil.rmtree(temp_gen_dir)

# ── Smoke + canary pre-flight ─────────────────────────────────────────────────

def run_smoke_and_canary() -> tuple:
    real_temp_home = _build_codex_temp_home()
    try:
        real_candidate_input_dir = BASE_DIR
        sb_path = _build_generator_sb(real_temp_home, real_candidate_input_dir)
        allowed_env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin",
            "LANG": "en_US.UTF-8",
            "HOME": real_temp_home,
            "USER": os.environ.get("USER", "codex"),
            "TMPDIR": real_temp_home,
        }
        version = _run_codex_version_smoke_test(sb_path, allowed_env)
        canary_tokens = _run_codex_canary(sb_path, allowed_env, TOKEN_CEILING, 0)
        return version, canary_tokens
    finally:
        _cleanup_temp_home(real_temp_home)

# ── Main execution entry point ────────────────────────────────────────────────

def _main_exec() -> None:
    approved_sha = verify_experiment_lock()
    verify_preflight_hashes()

    if not os.path.exists(BASELINE_PROMPT_PATH):
        raise ExperimentInvalidError(f"Baseline prompt not found at {BASELINE_PROMPT_PATH}")
    with open(BASELINE_PROMPT_PATH) as f:
        baseline_prompt = f.read()
    baseline_hash = hashlib.sha256(baseline_prompt.encode()).hexdigest()
    sys.stdout.write(f"Baseline hash: {baseline_hash}\n")

    sys.stdout.write("Running smoke test and canary...\n")
    sys.stdout.flush()
    codex_version, canary_tokens = run_smoke_and_canary()
    consumed_tokens = canary_tokens

    run_id = f"run_{int(time.time())}_{baseline_hash[:8]}"
    run_dir = os.path.join(HISTORY_DIR, run_id)
    os.makedirs(run_dir, exist_ok=True)

    # Isolated Huffman baseline preflight check
    sys.stdout.write("Running isolated Huffman preflight check...\n")
    sys.stdout.flush()
    huffman_iter_dir = os.path.join(run_dir, "huffman_preflight")
    os.makedirs(huffman_iter_dir, exist_ok=True)

    huffman_problem = os.path.join(BENCHMARKS_DIR, "huffman_coding", "problem.txt")
    with open(huffman_problem) as f:
        huffman_desc = f.read()

    huffman_code, huff_tokens = call_codex_sandboxed(
        huffman_desc, baseline_prompt, huffman_iter_dir,
        expect_json_code=True, cumulative_tokens=consumed_tokens
    )
    consumed_tokens += huff_tokens

    solution_file = os.path.join(huffman_iter_dir, "solution.py")
    with open(solution_file, "w") as f:
        f.write(huffman_code)

    huffman_harness = os.path.join(BENCHMARKS_DIR, "huffman_coding", "test_harness.py")
    eval_res = run_evaluation_on_candidate(huffman_iter_dir, huffman_harness)

    huffman_log = eval_res.get("log", "")
    huffman_success = eval_res.get("success", False)

    # Verify testsRun == 5
    tests_run_match = re.search(r"Ran (\d+) tests", huffman_log)
    if not tests_run_match or int(tests_run_match.group(1)) != 5:
        raise ExperimentInvalidError(f"Huffman preflight failed: expected 5 tests to run, log output:\n{huffman_log}")

    preflight_huffman_path = os.path.join(run_dir, "preflight_huffman.json")
    preflight_huffman_data = {
        "huffman_preflight_score": 1.0 if huffman_success else 0.0,
        "huffman_preflight_success": huffman_success,
        "huffman_log": huffman_log,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    _write_once_fsynced_manifest(preflight_huffman_path, preflight_huffman_data)

    if huffman_success:
        sys.stdout.write("Huffman preflight scored 5/5 (no headroom). Trials never invoked.\n")
        no_headroom_manifest_path = os.path.join(run_dir, "run_manifest.json")
        no_headroom_manifest = {
            "schema_version": "2",
            "run_id": run_id,
            "approved_commit_sha": approved_sha,
            "status": "no_headroom",
            "baseline_hash": baseline_hash,
            "preflight_hashes": preflight_hashes_global(),
            "preflight_huffman": preflight_huffman_data,
            "token_ceiling": TOKEN_CEILING,
            "tokens_consumed": consumed_tokens,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        _write_once_fsynced_manifest(no_headroom_manifest_path, no_headroom_manifest)
        return

    # Trial Execution
    optimization_tasks = _list_optimization_benchmarks()
    validation_tasks = _list_validation_benchmarks()
    sys.stdout.write(f"Optimization tasks: {optimization_tasks}\n")
    sys.stdout.write(f"Validation tasks:   {validation_tasks}\n")
    sys.stdout.flush()

    if not optimization_tasks:
        raise ExperimentInvalidError("No optimization benchmarks found.")
    if not validation_tasks:
        raise ExperimentInvalidError("No validation benchmarks found. Ensure benchmarks/validation/ exists.")

    trial_results = []
    for trial_idx in range(3):
        trial_id = f"trial_{trial_idx + 1}"
        sys.stdout.write(f"\n{'='*50}\nTRIAL {trial_idx + 1}/3\n{'='*50}\n")
        sys.stdout.flush()

        trial_dir = os.path.join(run_dir, trial_id)
        os.makedirs(trial_dir, exist_ok=True)

        baseline_score, baseline_results, baseline_tokens = evaluate_prompt_on_tasks(
            baseline_prompt, run_id, f"{trial_id}/baseline",
            BENCHMARKS_DIR, optimization_tasks, consumed_tokens,
        )
        consumed_tokens += baseline_tokens

        best_trial_score = baseline_score
        best_trial_prompt = baseline_prompt
        current_prompt = baseline_prompt

        for mut_idx in range(3):
            if best_trial_score >= 1.0:
                break

            failures = []
            for task, res in baseline_results.items():
                if not res["success"]:
                    task_file = os.path.join(BENCHMARKS_DIR, task, "problem.txt")
                    sol_path = os.path.join(run_dir, f"{trial_id}/baseline", task, "solution.py")
                    with open(task_file) as f:
                        desc = f.read()
                    code = ""
                    if os.path.exists(sol_path):
                        with open(sol_path) as f:
                            code = f.read()
                    failures.append({"task": task, "description": desc, "code": code, "error": res["error"] or ""})

            if not failures:
                break

            mut_dir = os.path.join(trial_dir, f"mutation_{mut_idx + 1}")
            os.makedirs(mut_dir, exist_ok=True)
            sys.stdout.write(f"[{trial_id}] Mutation {mut_idx + 1}...\n")

            new_prompt, mut_tokens = mutate_prompt(current_prompt, failures, mut_dir, consumed_tokens)
            consumed_tokens += mut_tokens

            sys.stdout.write(f"[{trial_id}] Evaluating mutated prompt...\n")
            mut_score, mut_results, eval_tokens = evaluate_prompt_on_tasks(
                new_prompt, run_id, f"{trial_id}/mut_{mut_idx + 1}",
                BENCHMARKS_DIR, optimization_tasks, consumed_tokens,
            )
            consumed_tokens += eval_tokens

            if mut_score > best_trial_score:
                best_trial_score = mut_score
                best_trial_prompt = new_prompt
                baseline_results = mut_results
                current_prompt = new_prompt
            else:
                current_prompt = best_trial_prompt

        trial_results.append({
            "trial": trial_id,
            "baseline_score": baseline_score,
            "opt_score": best_trial_score,
            "delta": best_trial_score - baseline_score,
            "candidate_prompt": best_trial_prompt,
            "candidate_prompt_hash": hashlib.sha256(best_trial_prompt.encode()).hexdigest(),
        })

    mean_delta = sum(t["delta"] for t in trial_results) / len(trial_results)

    # Evaluate baseline validation
    baseline_val_score, _, val_baseline_tokens = evaluate_prompt_on_tasks(
        baseline_prompt, run_id, "validation_baseline",
        VALIDATION_DIR, validation_tasks, consumed_tokens,
    )
    consumed_tokens += val_baseline_tokens

    # Evaluate candidates validation
    candidate_val_scores = []
    for t in trial_results:
        score, _, vtokens = evaluate_prompt_on_tasks(
            t["candidate_prompt"], run_id, f"validation_{t['trial']}",
            VALIDATION_DIR, validation_tasks, consumed_tokens,
        )
        consumed_tokens += vtokens
        candidate_val_scores.append(score)

    gate_passed = (
        mean_delta > 0
        and baseline_val_score is not None
        and any(s is not None and s > baseline_val_score for s in candidate_val_scores)
    )

    candidate_profile_hash = _sha256_file(os.path.join(BASE_DIR, "candidate.sb.template"))
    generator_profile_hash = _sha256_file(os.path.join(BASE_DIR, "generator.sb.template"))
    preflight_hashes = preflight_hashes_global()

    if not gate_passed:
        # State: completed_no_improvement
        retention_reason = (
            f"mean_delta={mean_delta:.4f}, "
            f"baseline_val={baseline_val_score}, candidate_vals={candidate_val_scores}"
        )
        run_manifest_path = os.path.join(run_dir, "run_manifest.json")
        no_improvement_manifest = {
            "schema_version": "2",
            "run_id": run_id,
            "approved_commit_sha": approved_sha,
            "status": "completed_no_improvement",
            "baseline_hash": baseline_hash,
            "promotion_gate_passed": False,
            "retention_reason": retention_reason,
            "mean_paired_delta": mean_delta,
            "trial_results": trial_results,
            "candidate_profile_hash": candidate_profile_hash,
            "generator_profile_hash": generator_profile_hash,
            "preflight_hashes": preflight_hashes,
            "preflight_huffman": preflight_huffman_data,
            "token_ceiling": TOKEN_CEILING,
            "tokens_consumed": consumed_tokens,
            "budget_note": (
                "46992 is a 712-tokens/call estimate (historical avg); not a worst-case bound. "
                "50000 ceiling is authoritative."
            ),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        _write_once_fsynced_manifest(run_manifest_path, no_improvement_manifest)

        # Pointer check: only initialize baseline if best_manifest.json does not exist
        if not os.path.exists(BEST_MANIFEST_PATH):
            update_best_manifest_pointer(run_manifest_path)

        sys.stdout.write("Gate failed. Baseline retained.\n")
        return

    # Select best candidate
    eligible = [
        (i, s) for i, s in enumerate(candidate_val_scores)
        if s is not None and s > baseline_val_score
    ]
    eligible.sort(key=lambda x: (-x[1], x[0]))
    best_idx = eligible[0][0]
    best_trial = trial_results[best_idx]
    best_prompt = best_trial["candidate_prompt"]

    holdout_record, holdout_tokens = run_holdout_evaluation(best_prompt, run_id, consumed_tokens)
    consumed_tokens += holdout_tokens

    # State: completed_promoted
    run_manifest_path = os.path.join(run_dir, "run_manifest.json")
    promoted_manifest = {
        "schema_version": "2",
        "run_id": run_id,
        "approved_commit_sha": approved_sha,
        "status": "completed_promoted",
        "baseline_hash": baseline_hash,
        "selected_prompt_hash": best_trial["candidate_prompt_hash"],
        "selected_from_trial": best_trial["trial"],
        "validation_score": candidate_val_scores[best_idx],
        "baseline_validation_score": baseline_val_score,
        "promotion_gate_passed": True,
        "mean_paired_delta": mean_delta,
        "trial_results": trial_results,
        "candidate_profile_hash": candidate_profile_hash,
        "generator_profile_hash": generator_profile_hash,
        "preflight_hashes": preflight_hashes,
        "preflight_huffman": preflight_huffman_data,
        "token_ceiling": TOKEN_CEILING,
        "tokens_consumed": consumed_tokens,
        "budget_note": (
            "46992 is a 712-tokens/call estimate (historical avg); not a worst-case bound. "
            "50000 ceiling is authoritative."
        ),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if holdout_record:
        promoted_manifest["holdout"] = holdout_record

    _write_once_fsynced_manifest(run_manifest_path, promoted_manifest)

    # Save selected prompt
    best_prompt_path = os.path.join(PROMPTS_DIR, "inner_agent_best.txt")
    with open(best_prompt_path, "w") as f:
        f.write(best_prompt)

    # Update atomic root pointer
    update_best_manifest_pointer(run_manifest_path)
    sys.stdout.write(f"Promotion gate passed. Manifest promoted to best_manifest.json.\n")

def preflight_hashes_global() -> dict:
    if os.path.exists(PREFLIGHT_HASHES_PATH):
        with open(PREFLIGHT_HASHES_PATH) as f:
            return json.load(f)
    return {}

def main() -> None:
    try:
        _main_exec()
        sys.exit(0)
    except ExperimentInvalidError as e:
        sys.stderr.write(f"FATAL: Experiment invalidated: {e}\n")
        # Write invalid manifest
        run_id = f"invalid_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        run_dir = os.path.join(HISTORY_DIR, f"run_{run_id}")
        os.makedirs(run_dir, exist_ok=True)

        invalid_manifest_path = os.path.join(run_dir, "invalid_manifest.json")
        invalid_data = {
            "schema_version": "2",
            "run_id": run_id,
            "status": "invalid",
            "error_message": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            _write_once_fsynced_manifest(invalid_manifest_path, invalid_data)
        except Exception:
            pass
        sys.exit(1)

if __name__ == "__main__":
    main()
