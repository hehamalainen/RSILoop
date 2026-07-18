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

class RunContext:
    def __init__(self):
        self.run_id = None
        self.run_dir = None
        self.approved_commit_sha = None
        self.lock_file_hash = None
        self.codex_version = None
        self.codex_model = "codex-mini-latest"
        self.consumed_tokens = 0
        self.unknown_token_state = False
        self.current_stage = "pre-init"
        self.allow_no_holdout = os.environ.get("ALLOW_NO_HOLDOUT") == "1"

# ── Containment Helpers ───────────────────────────────────────────────────────

def is_contained(target_path: str, base_dir: str) -> bool:
    real_target = os.path.realpath(target_path)
    real_base = os.path.realpath(base_dir)
    try:
        common = os.path.commonpath([real_target, real_base])
        return common == real_base
    except ValueError:
        return False

def check_parent_symlinks(path: str):
    components = []
    head, tail = os.path.split(path)
    while tail:
        components.append(head)
        head, tail = os.path.split(head)
    for comp in reversed(components):
        if comp and comp != "/" and os.path.islink(comp):
            raise ExperimentInvalidError(f"Symlink detected in path component: {comp}")

# ── JSONL parsing ─────────────────────────────────────────────────────────────

def parse_codex_jsonl_output(stdout_lines: list) -> tuple:
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
                raise ExperimentInvalidError("Codex JSONL stream has multiple turn.completed events — fail closed")
            usage_data = event.get("usage")

    if turn_completed_count != 1:
        raise ExperimentInvalidError(f"Codex JSONL stream missing turn.completed event (count={turn_completed_count})")
    if not usage_data:
        raise ExperimentInvalidError("Codex JSONL turn.completed has no usage data — fail closed")

    output_tokens = usage_data.get("output_tokens")
    if output_tokens is None:
        raise ExperimentInvalidError("Codex JSONL usage has no output_tokens")
    if not isinstance(output_tokens, int) or isinstance(output_tokens, bool):
        raise ExperimentInvalidError(f"output_tokens must be an integer, got: {type(output_tokens).__name__} ({output_tokens!r})")
    if output_tokens < 0:
        raise ExperimentInvalidError(f"output_tokens must be non-negative, got: {output_tokens}")

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
        raise ExperimentInvalidError("Codex auth.json not found at ~/.codex/auth.json — cannot proceed")

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

    user_home = os.path.realpath(os.path.expanduser("~"))
    codex_real_path = os.path.realpath("/opt/homebrew/bin/codex")
    if os.path.exists(codex_real_path):
        codex_prefix = os.path.dirname(os.path.dirname(codex_real_path))
        codex_base_dir = "/opt/homebrew"
    else:
        codex_real_path = os.path.realpath("/usr/local/bin/codex")
        if os.path.exists(codex_real_path):
            codex_prefix = os.path.dirname(os.path.dirname(codex_real_path))
            codex_base_dir = "/usr/local"
        else:
            codex_prefix = "/opt/homebrew"
            codex_base_dir = "/opt/homebrew"

    sb_content = tmpl.format(
        real_temp_home=real_temp_home,
        real_candidate_input_dir=real_candidate_input_dir,
        user_home=user_home,
        codex_prefix=codex_prefix,
        codex_base_dir=codex_base_dir,
    )
    sb_path = os.path.join(real_temp_home, "generator.sb")
    with open(sb_path, "w") as f:
        f.write(sb_content)
    return sb_path

def _get_codex_bin_path() -> str:
    for path in ("/opt/homebrew/bin/codex", "/usr/local/bin/codex"):
        if os.path.exists(path):
            return path
    return "/opt/homebrew/bin/codex"

def _run_codex_version_smoke_test(sb_path: str, env: dict) -> str:
    codex_bin = _get_codex_bin_path()
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
    sb_path: str, env: dict, ctx: RunContext
) -> int:
    codex_bin = _get_codex_bin_path()
    canary_prompt = "Reply with exactly one word: ready"
    cmd = [
        "sandbox-exec", "-f", sb_path,
        codex_bin, "exec", "-",
        "--ephemeral",
        "--json",
        "--model", ctx.codex_model,
    ]
    try:
        res = subprocess.run(
            cmd,
            input=canary_prompt,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )
    except Exception as e:
        ctx.unknown_token_state = True
        raise ExperimentInvalidError(f"Canary process execution failed: {e}")

    if res.returncode != 0:
        ctx.unknown_token_state = True
        raise ExperimentInvalidError(
            f"Codex canary call failed (rc={res.returncode}):\nstderr: {res.stderr[:500]}"
        )

    try:
        _, usage = parse_codex_jsonl_output(res.stdout.splitlines())
        tokens_val = usage.get("output_tokens")
        if tokens_val is None:
            raise ValueError("Codex canary call returned no output_tokens")
        tokens = int(tokens_val)
        if tokens < 0:
            raise ValueError(f"Negative token count: {tokens}")
    except Exception as e:
        ctx.unknown_token_state = True
        raise ExperimentInvalidError(f"Canary token parsing failed: {e}")

    ctx.consumed_tokens += tokens
    if ctx.consumed_tokens > TOKEN_CEILING:
        raise ExperimentInvalidError(
            f"Token ceiling breached during canary: {ctx.consumed_tokens} > {TOKEN_CEILING}"
        )
    sys.stderr.write(f"[canary] API access verified. Canary output tokens: {tokens}\n")
    sys.stderr.flush()
    return tokens

def call_codex_sandboxed(
    prompt: str,
    system_instruction: str,
    candidate_dir: str,
    ctx: RunContext,
    expect_json_code: bool = True,
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

        codex_bin = _get_codex_bin_path()
        cmd = [
            "sandbox-exec", "-f", sb_path,
            codex_bin, "exec", "-",
            "-s", "read-only",
            "--ephemeral",
            "--json",
            "--model", ctx.codex_model,
            "-C", real_candidate_input_dir,
        ]

        try:
            res = subprocess.run(
                cmd,
                input=full_prompt,
                capture_output=True,
                text=True,
                timeout=120,
                env=allowed_env,
            )
        except Exception as e:
            ctx.unknown_token_state = True
            raise ExperimentInvalidError(f"Codex process execution failed: {e}")

        if res.returncode != 0:
            ctx.unknown_token_state = True
            raise ExperimentInvalidError(f"Codex returned non-zero exit code {res.returncode}. Stderr: {res.stderr[:500]}")

        try:
            raw_msg, usage = parse_codex_jsonl_output(res.stdout.splitlines())
            tokens_val = usage.get("output_tokens")
            if tokens_val is None:
                raise ValueError("Codex call returned no output_tokens")
            tokens = int(tokens_val)
            if tokens < 0:
                raise ValueError(f"Negative token count: {tokens}")
        except Exception as e:
            ctx.unknown_token_state = True
            raise ExperimentInvalidError(f"Codex call token parsing failed: {e}")

        ctx.consumed_tokens += tokens
        if ctx.consumed_tokens > TOKEN_CEILING:
            raise ExperimentInvalidError(
                f"Token ceiling breached: {ctx.consumed_tokens} > {TOKEN_CEILING}"
            )

        if expect_json_code:
            return extract_code(raw_msg), tokens
        else:
            return raw_msg, tokens

    finally:
        _cleanup_temp_home(real_temp_home)

# ── Evaluation helpers ────────────────────────────────────────────────────────

def run_evaluation_on_candidate(candidate_dir: str, test_harness_path: str) -> dict:
    from evaluator import run_evaluation, HarnessInfrastructureError
    try:
        return run_evaluation(candidate_dir, test_harness_path)
    except HarnessInfrastructureError as e:
        raise ExperimentInvalidError(f"Harness infrastructure failure: {e}") from e

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
    run_dir: str,
    iter_id: str,
    benchmarks_root: str,
    task_names: list,
    cumulative_tokens: int,
    ctx: RunContext,
) -> tuple:
    results = {}
    passed_count = 0
    total_tokens = 0

    for benchmark in task_names:
        benchmark_dir = os.path.join(benchmarks_root, benchmark)
        iter_task_dir = os.path.realpath(os.path.join(run_dir, iter_id, benchmark))
        if not is_contained(iter_task_dir, run_dir):
            raise ExperimentInvalidError(f"Directory traversal attempt: {iter_task_dir} is outside {run_dir}")
        os.makedirs(iter_task_dir, exist_ok=True)

        task_file = os.path.join(benchmark_dir, "problem.txt")
        with open(task_file) as f:
            task_desc = f.read()

        solution_code, tokens = call_codex_sandboxed(
            task_desc, prompt_text, iter_task_dir, ctx,
            expect_json_code=True,
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
    ctx: RunContext,
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
        context, meta_prompt, iter_dir, ctx,
        expect_json_code=False,
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
            raise RuntimeError(f"Unknown or legacy pointer schema version: {ptr.get('pointer_schema_version')}")
        path = os.path.realpath(os.path.join(BASE_DIR, ptr["run_manifest_path"]))
        expected_hash = ptr["run_manifest_sha256"]

        # Validate pointer target resolves beneath history/ and filename is run_manifest.json
        if not is_contained(path, os.path.realpath(HISTORY_DIR)):
            raise RuntimeError(f"Pointer target {path} is not contained within HISTORY_DIR")
        if os.path.basename(path) != "run_manifest.json":
            raise RuntimeError(f"Pointer target filename {os.path.basename(path)} is not run_manifest.json")

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

def verify_existing_best_manifest_startup() -> None:
    if not os.path.exists(BEST_MANIFEST_PATH):
        return
    try:
        with open(BEST_MANIFEST_PATH) as f:
            ptr = json.load(f)
        if ptr.get("pointer_schema_version") != "2":
            raise RuntimeError(f"Unknown or legacy pointer schema version: {ptr.get('pointer_schema_version')}")

        target_path = ptr.get("run_manifest_path")
        if not target_path:
            raise RuntimeError("run_manifest_path missing from pointer")

        full_path = os.path.realpath(os.path.join(BASE_DIR, target_path))
        if not is_contained(full_path, os.path.realpath(HISTORY_DIR)):
            raise RuntimeError(f"Pointer target {full_path} is not contained within HISTORY_DIR")
        if os.path.basename(full_path) != "run_manifest.json":
            raise RuntimeError(f"Pointer target filename {os.path.basename(full_path)} is not run_manifest.json")

        if not os.path.exists(full_path):
            raise RuntimeError(f"Target run manifest missing: {full_path}")

        expected_hash = ptr.get("run_manifest_sha256")
        actual_hash = _sha256_file(full_path)
        if actual_hash != expected_hash:
            raise RuntimeError(f"Manifest hash mismatch: expected {expected_hash}, got {actual_hash}")

        with open(full_path) as f:
            target_manifest = json.load(f)

        status = target_manifest.get("status")
        if status not in ["no_headroom", "completed_no_improvement", "completed_promoted"]:
            raise RuntimeError(f"Invalid target manifest status: {status}")

    except Exception as e:
        import shutil
        legacy_dir = os.path.join(HISTORY_DIR, "legacy")
        os.makedirs(legacy_dir, exist_ok=True)
        backup_name = f"best_manifest.json.archive_{uuid.uuid4().hex}.backup"
        backup_path = os.path.join(legacy_dir, backup_name)
        try:
            shutil.move(BEST_MANIFEST_PATH, backup_path)
            sys.stdout.write(f"Archived legacy/invalid manifest to: {backup_path}\n")
            sys.stdout.flush()
        except Exception as move_err:
            raise ExperimentInvalidError(f"Existing best_manifest.json is invalid ({e}), and failed to archive: {move_err}")

        raise ExperimentInvalidError(
            f"Existing best_manifest.json was invalid/legacy ({e}). It has been archived to history/legacy/{backup_name}. "
            "The run is aborted to prevent running on top of unverifiable state."
        )

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

def verify_experiment_lock(ctx: RunContext) -> str:
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

    ctx.lock_file_hash = _sha256_file(lock_path)

    try:
        with open(lock_path) as f:
            lock_data = json.load(f)
    except Exception as e:
        raise ExperimentInvalidError(f"Failed to read experiment.lock: {e}")

    approved_sha = lock_data.get("approved_commit_sha")
    if not approved_sha:
        raise ExperimentInvalidError("approved_commit_sha missing from experiment.lock")

    lock_allow_no_holdout = lock_data.get("allow_no_holdout", False)
    if not isinstance(lock_allow_no_holdout, bool):
        raise ExperimentInvalidError(f"allow_no_holdout in experiment.lock must be a boolean, got: {type(lock_allow_no_holdout).__name__}")

    env_allow_no_holdout = os.environ.get("ALLOW_NO_HOLDOUT") == "1"
    if env_allow_no_holdout != lock_allow_no_holdout:
        raise ExperimentInvalidError(
            f"Environment ALLOW_NO_HOLDOUT={os.environ.get('ALLOW_NO_HOLDOUT')} "
            f"disagrees with experiment.lock allow_no_holdout={lock_allow_no_holdout}"
        )
    ctx.allow_no_holdout = lock_allow_no_holdout

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
    ctx.approved_commit_sha = approved_sha
    return approved_sha

EXPECTED_PREFLIGHT_KEYS = {
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
    "generator.sb.template"
}

def verify_preflight_hashes():
    if not os.path.exists(PREFLIGHT_HASHES_PATH):
        raise ExperimentInvalidError("preflight_hashes.json missing")
    with open(PREFLIGHT_HASHES_PATH) as f:
        preflight_hashes = json.load(f)

    actual_keys = set(preflight_hashes.keys())
    if EXPECTED_PREFLIGHT_KEYS != actual_keys:
        raise ExperimentInvalidError(f"Preflight keys mismatch. Missing: {EXPECTED_PREFLIGHT_KEYS - actual_keys}, Extra: {actual_keys - EXPECTED_PREFLIGHT_KEYS}")

    for rel_path, expected_hash in preflight_hashes.items():
        unresolved_path = os.path.join(BASE_DIR, rel_path)
        if os.path.islink(unresolved_path):
            raise ExperimentInvalidError(f"Symlink detected in preflight file: {unresolved_path}")

        full_path = os.path.realpath(unresolved_path)
        if not is_contained(full_path, BASE_DIR):
            raise ExperimentInvalidError(f"Path traversal detected in preflight hash key: {rel_path}")

        check_parent_symlinks(full_path)

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
        unresolved_path = os.path.join(private_dir, rel_path)
        if os.path.islink(unresolved_path):
            raise ExperimentInvalidError(f"Symlink detected in holdout file: {unresolved_path}")

        full_path = os.path.realpath(unresolved_path)
        if not is_contained(full_path, private_dir):
            raise ExperimentInvalidError(f"Path traversal detected in holdout hash key: {rel_path}")

        check_parent_symlinks(full_path)

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
    run_dir: str,
    ctx: RunContext,
) -> tuple:
    problem_file = os.path.join(PRIVATE_DIR, "simple_regex_matcher", "problem.txt")
    harness_file = os.path.join(PRIVATE_DIR, "simple_regex_matcher", "test_holdout.py")

    if not os.path.exists(PRIVATE_DIR):
        if not getattr(ctx, "allow_no_holdout", False):
            raise ExperimentInvalidError("Private holdout directory is missing and no-holdout mode is not authorized")
        return None, 0

    verify_holdout_hashes(PRIVATE_DIR)

    if not os.path.exists(problem_file) or not os.path.exists(harness_file):
        if not getattr(ctx, "allow_no_holdout", False):
            raise ExperimentInvalidError("Private holdout files simple_regex_matcher are missing and no-holdout mode is not authorized")
        sys.stdout.write("Holdout simple_regex_matcher files not found. Skipping.\n")
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
            task_desc, best_prompt, temp_gen_dir, ctx,
            expect_json_code=True,
        )

        solution_file = os.path.join(temp_gen_dir, "solution.py")
        with open(solution_file, "w") as f:
            f.write(solution_code)

        eval_res = run_evaluation_on_candidate(temp_gen_dir, harness_file)
        solution_hash = hashlib.sha256(solution_code.encode()).hexdigest()

        run_holdout_dir = os.path.realpath(os.path.join(run_dir, "holdout_evaluation"))
        if not is_contained(run_holdout_dir, run_dir):
            raise ExperimentInvalidError("Path traversal in holdout evaluation run directory")
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

def run_smoke_and_canary(ctx: RunContext) -> tuple:
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
        ctx.codex_version = version
        canary_tokens = _run_codex_canary(sb_path, allowed_env, ctx)
        return version, canary_tokens
    finally:
        _cleanup_temp_home(real_temp_home)

# ── Main execution entry point ────────────────────────────────────────────────

def _main_exec(ctx: RunContext) -> None:
    ctx.current_stage = "preflight_verifications"
    approved_sha = verify_experiment_lock(ctx)
    verify_existing_best_manifest_startup()
    verify_preflight_hashes()

    # Verify holdout if no-holdout mode is not authorized
    if not ctx.allow_no_holdout:
        if not os.path.exists(PRIVATE_DIR):
            raise ExperimentInvalidError("Private holdout directory is missing and no-holdout mode is not authorized")
        problem_file = os.path.join(PRIVATE_DIR, "simple_regex_matcher", "problem.txt")
        harness_file = os.path.join(PRIVATE_DIR, "simple_regex_matcher", "test_holdout.py")
        if not os.path.exists(problem_file) or not os.path.exists(harness_file):
            raise ExperimentInvalidError("Private holdout files simple_regex_matcher are missing and no-holdout mode is not authorized")
        verify_holdout_hashes(PRIVATE_DIR)

    if not os.path.exists(BASELINE_PROMPT_PATH):
        raise ExperimentInvalidError(f"Baseline prompt not found at {BASELINE_PROMPT_PATH}")
    with open(BASELINE_PROMPT_PATH) as f:
        baseline_prompt = f.read()
    baseline_hash = hashlib.sha256(baseline_prompt.encode()).hexdigest()
    sys.stdout.write(f"Baseline hash: {baseline_hash}\n")

    ctx.current_stage = "smoke_and_canary"
    sys.stdout.write("Running smoke test and canary...\n")
    sys.stdout.flush()
    codex_version, canary_tokens = run_smoke_and_canary(ctx)

    # Isolated Huffman baseline preflight check
    ctx.current_stage = "huffman_preflight"
    sys.stdout.write("Running isolated Huffman preflight check...\n")
    sys.stdout.flush()
    huffman_iter_dir = os.path.realpath(os.path.join(ctx.run_dir, "huffman_preflight"))
    if not is_contained(huffman_iter_dir, ctx.run_dir):
         raise ExperimentInvalidError("Path traversal in huffman preflight run directory")
    os.makedirs(huffman_iter_dir, exist_ok=True)

    huffman_problem = os.path.join(BENCHMARKS_DIR, "huffman_coding", "problem.txt")
    with open(huffman_problem) as f:
        huffman_desc = f.read()

    huffman_code, huff_tokens = call_codex_sandboxed(
        huffman_desc, baseline_prompt, huffman_iter_dir, ctx,
        expect_json_code=True,
    )

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

    preflight_huffman_path = os.path.join(ctx.run_dir, "preflight_huffman.json")
    preflight_huffman_data = {
        "huffman_preflight_score": 1.0 if huffman_success else 0.0,
        "huffman_preflight_success": huffman_success,
        "huffman_log": huffman_log,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "solution_hash": hashlib.sha256(huffman_code.encode()).hexdigest(),
        "tokens_consumed": huff_tokens,
    }
    _write_once_fsynced_manifest(preflight_huffman_path, preflight_huffman_data)

    if huffman_success:
        sys.stdout.write("Huffman preflight scored 5/5 (no headroom). Trials never invoked.\n")
        no_headroom_manifest_path = os.path.join(ctx.run_dir, "run_manifest.json")
        no_headroom_manifest = {
            "schema_version": "2",
            "run_id": ctx.run_id,
            "approved_commit_sha": approved_sha,
            "lock_file_hash": ctx.lock_file_hash,
            "status": "no_headroom",
            "baseline_hash": baseline_hash,
            "codex_version": ctx.codex_version,
            "model_name": ctx.codex_model,
            "canary_tokens_consumed": canary_tokens,
            "preflight_hashes": preflight_hashes_global(),
            "preflight_huffman": preflight_huffman_data,
            "token_ceiling": TOKEN_CEILING,
            "tokens_consumed": ctx.consumed_tokens,
            "unknown_token_state": ctx.unknown_token_state,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        _write_once_fsynced_manifest(no_headroom_manifest_path, no_headroom_manifest)
        if not os.path.exists(BEST_MANIFEST_PATH):
            update_best_manifest_pointer(no_headroom_manifest_path)
        return

    # Trial Execution
    ctx.current_stage = "trials"
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

        trial_dir = os.path.realpath(os.path.join(ctx.run_dir, trial_id))
        if not is_contained(trial_dir, ctx.run_dir):
            raise ExperimentInvalidError(f"Path traversal in trial run directory: {trial_dir}")
        os.makedirs(trial_dir, exist_ok=True)

        baseline_score, baseline_results, baseline_tokens = evaluate_prompt_on_tasks(
            baseline_prompt, ctx.run_dir, f"{trial_id}/baseline",
            BENCHMARKS_DIR, optimization_tasks, ctx.consumed_tokens, ctx
        )

        best_trial_score = baseline_score
        best_trial_prompt = baseline_prompt
        current_prompt = baseline_prompt
        current_results = baseline_results
        current_source_subdir = f"{trial_id}/baseline"
        mutations_history = []

        for mut_idx in range(3):
            if best_trial_score >= 1.0:
                break

            failures = []
            for task, res in current_results.items():
                if not res["success"]:
                    task_file = os.path.join(BENCHMARKS_DIR, task, "problem.txt")
                    sol_path = os.path.realpath(os.path.join(ctx.run_dir, current_source_subdir, task, "solution.py"))
                    with open(task_file) as f:
                        desc = f.read()
                    code = ""
                    if os.path.exists(sol_path):
                        with open(sol_path) as f:
                            code = f.read()
                    failures.append({"task": task, "description": desc, "code": code, "error": res["error"] or ""})

            if not failures:
                break

            mut_dir = os.path.realpath(os.path.join(trial_dir, f"mutation_{mut_idx + 1}"))
            if not is_contained(mut_dir, ctx.run_dir):
                raise ExperimentInvalidError(f"Path traversal in mutation run directory: {mut_dir}")
            os.makedirs(mut_dir, exist_ok=True)
            sys.stdout.write(f"[{trial_id}] Mutation {mut_idx + 1}...\n")

            new_prompt, mut_tokens = mutate_prompt(current_prompt, failures, mut_dir, ctx)

            sys.stdout.write(f"[{trial_id}] Evaluating mutated prompt...\n")
            mut_score, mut_results, eval_tokens = evaluate_prompt_on_tasks(
                new_prompt, ctx.run_dir, f"{trial_id}/mut_{mut_idx + 1}",
                BENCHMARKS_DIR, optimization_tasks, ctx.consumed_tokens, ctx
            )

            mutation_record = {
                "mutation_index": mut_idx + 1,
                "prompt_hash": hashlib.sha256(new_prompt.encode()).hexdigest(),
                "prompt": new_prompt,
                "optimizer_tokens_consumed": mut_tokens,
                "evaluation_run": {
                    "benchmarks": mut_results,
                    "tokens_consumed": eval_tokens
                }
            }
            mutations_history.append(mutation_record)

            if mut_score > best_trial_score:
                best_trial_score = mut_score
                best_trial_prompt = new_prompt
                current_results = mut_results
                current_source_subdir = f"{trial_id}/mut_{mut_idx + 1}"
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
            "baseline_run": {
                "benchmarks": baseline_results,
                "tokens_consumed": baseline_tokens
            },
            "mutations": mutations_history
        })

    mean_delta = sum(t["delta"] for t in trial_results) / len(trial_results)

    # Evaluate baseline validation
    ctx.current_stage = "validation"
    baseline_val_score, baseline_val_results, val_baseline_tokens = evaluate_prompt_on_tasks(
        baseline_prompt, ctx.run_dir, "validation_baseline",
        VALIDATION_DIR, validation_tasks, ctx.consumed_tokens, ctx
    )

    # Evaluate candidates validation
    candidate_val_scores = []
    candidate_val_results_list = []
    candidate_val_tokens_list = []
    for t in trial_results:
        score, val_res, vtokens = evaluate_prompt_on_tasks(
            t["candidate_prompt"], ctx.run_dir, f"validation_{t['trial']}",
            VALIDATION_DIR, validation_tasks, ctx.consumed_tokens, ctx
        )
        candidate_val_scores.append(score)
        candidate_val_results_list.append(val_res)
        candidate_val_tokens_list.append(vtokens)

    eligible = []
    for i, t in enumerate(trial_results):
        val_score = candidate_val_scores[i]
        is_different = t["candidate_prompt_hash"] != baseline_hash
        is_positive_delta = t["delta"] > 0
        is_better_val = val_score > baseline_val_score
        if is_different and is_positive_delta and is_better_val:
            eligible.append((i, val_score))

    gate_passed = (mean_delta > 0 and len(eligible) > 0)

    candidate_profile_hash = _sha256_file(os.path.join(BASE_DIR, "candidate.sb.template"))
    generator_profile_hash = _sha256_file(os.path.join(BASE_DIR, "generator.sb.template"))
    preflight_hashes = preflight_hashes_global()

    validation_run_record = {
        "baseline": {
            "score": baseline_val_score,
            "benchmarks": baseline_val_results,
            "tokens_consumed": val_baseline_tokens
        },
        "candidates": {
            t["trial"]: {
                "score": candidate_val_scores[idx],
                "benchmarks": candidate_val_results_list[idx],
                "tokens_consumed": candidate_val_tokens_list[idx]
            }
            for idx, t in enumerate(trial_results)
        }
    }

    if not gate_passed:
        # State: completed_no_improvement
        retention_reason = (
            f"mean_delta={mean_delta:.4f}, "
            f"baseline_val={baseline_val_score}, candidate_vals={candidate_val_scores}"
        )
        run_manifest_path = os.path.join(ctx.run_dir, "run_manifest.json")
        no_improvement_manifest = {
            "schema_version": "2",
            "run_id": ctx.run_id,
            "approved_commit_sha": approved_sha,
            "lock_file_hash": ctx.lock_file_hash,
            "status": "completed_no_improvement",
            "baseline_hash": baseline_hash,
            "codex_version": ctx.codex_version,
            "model_name": ctx.codex_model,
            "canary_tokens_consumed": canary_tokens,
            "promotion_gate_passed": False,
            "retention_reason": retention_reason,
            "mean_paired_delta": mean_delta,
            "trial_results": trial_results,
            "validation_run": validation_run_record,
            "candidate_profile_hash": candidate_profile_hash,
            "generator_profile_hash": generator_profile_hash,
            "preflight_hashes": preflight_hashes,
            "preflight_huffman": preflight_huffman_data,
            "token_ceiling": TOKEN_CEILING,
            "tokens_consumed": ctx.consumed_tokens,
            "unknown_token_state": ctx.unknown_token_state,
            "budget_note": (
                "46992 is a 712-tokens/call estimate (historical avg); not a worst-case bound. "
                "50000 ceiling is authoritative."
            ),
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "note": "Statistical significance is not established by validation gate sampling."
        }
        _write_once_fsynced_manifest(run_manifest_path, no_improvement_manifest)

        if not os.path.exists(BEST_MANIFEST_PATH):
            update_best_manifest_pointer(run_manifest_path)

        sys.stdout.write("Gate failed. Baseline retained.\n")
        return

    # Select best candidate
    eligible.sort(key=lambda x: (-x[1], x[0]))
    best_idx = eligible[0][0]
    best_trial = trial_results[best_idx]
    best_prompt = best_trial["candidate_prompt"]

    ctx.current_stage = "holdout"
    holdout_record, holdout_tokens = run_holdout_evaluation(best_prompt, ctx.run_dir, ctx)

    # State: completed_promoted
    run_manifest_path = os.path.join(ctx.run_dir, "run_manifest.json")
    promoted_manifest = {
        "schema_version": "2",
        "run_id": ctx.run_id,
        "approved_commit_sha": approved_sha,
        "lock_file_hash": ctx.lock_file_hash,
        "status": "completed_promoted",
        "baseline_hash": baseline_hash,
        "codex_version": ctx.codex_version,
        "model_name": ctx.codex_model,
        "canary_tokens_consumed": canary_tokens,
        "selected_prompt_hash": best_trial["candidate_prompt_hash"],
        "selected_from_trial": best_trial["trial"],
        "validation_score": candidate_val_scores[best_idx],
        "baseline_validation_score": baseline_val_score,
        "promotion_gate_passed": True,
        "mean_paired_delta": mean_delta,
        "trial_results": trial_results,
        "validation_run": validation_run_record,
        "candidate_profile_hash": candidate_profile_hash,
        "generator_profile_hash": generator_profile_hash,
        "preflight_hashes": preflight_hashes,
        "preflight_huffman": preflight_huffman_data,
        "token_ceiling": TOKEN_CEILING,
        "tokens_consumed": ctx.consumed_tokens,
        "unknown_token_state": ctx.unknown_token_state,
        "budget_note": (
            "46992 is a 712-tokens/call estimate (historical avg); not a worst-case bound. "
            "50000 ceiling is authoritative."
        ),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "note": "Statistical significance is not established by validation gate sampling.",
        "no_holdout_mode_active": ctx.allow_no_holdout,
    }
    if holdout_record:
        promoted_manifest["holdout"] = holdout_record

    _write_once_fsynced_manifest(run_manifest_path, promoted_manifest)

    # Save selected prompt
    best_prompt_path = os.path.join(PROMPTS_DIR, "inner_agent_best.txt")
    with open(best_prompt_path, "w") as f:
        f.write(best_prompt)

    # Update atomic pointer
    update_best_manifest_pointer(run_manifest_path)
    sys.stdout.write(f"Promotion gate passed. Manifest promoted to best_manifest.json.\n")

def preflight_hashes_global() -> dict:
    if os.path.exists(PREFLIGHT_HASHES_PATH):
        with open(PREFLIGHT_HASHES_PATH) as f:
            return json.load(f)
    return {}

def main() -> None:
    ctx = RunContext()
    ctx.run_id = f"run_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    ctx.run_dir = os.path.realpath(os.path.join(HISTORY_DIR, ctx.run_id))
    try:
        os.makedirs(ctx.run_dir, exist_ok=True)
    except Exception as e:
        sys.stderr.write(f"FATAL: failed to create run directory: {e}\n")
        sys.exit(1)

    try:
        _main_exec(ctx)
        sys.exit(0)
    except Exception as e:
        sys.stderr.write(f"FATAL: Experiment invalidated: {e}\n")
        invalid_manifest_path = os.path.join(ctx.run_dir, "invalid_manifest.json")
        invalid_data = {
            "schema_version": "2",
            "run_id": ctx.run_id,
            "status": "invalid",
            "approved_commit_sha": ctx.approved_commit_sha,
            "lock_file_hash": ctx.lock_file_hash,
            "codex_version": ctx.codex_version,
            "model_name": ctx.codex_model,
            "error_message": f"{type(e).__name__}: {e}",
            "tokens_consumed": ctx.consumed_tokens,
            "unknown_token_state": ctx.unknown_token_state,
            "current_stage": ctx.current_stage,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        try:
            _write_once_fsynced_manifest(invalid_manifest_path, invalid_data)
        except Exception:
            pass
        sys.exit(1)

if __name__ == "__main__":
    main()
