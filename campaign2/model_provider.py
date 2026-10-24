"""
Campaign 2 Model Provider Interface & Production Sol Implementation (C1-1..C1-13).

Provides pluggable model provider abstraction and production Sol model provider for gpt-5.6-sol via Codex CLI.
- ModelProvider ABC: defines generate_candidate() and mutate_prompt().
- ProductionTaskEvaluator: wraps evaluate_candidate_code_sandboxed.
- MockModelProvider: deterministic partial-scoring mock provider for testing.
- SolModelProvider: production provider for gpt-5.6-sol via Codex CLI with three-record ledger logging,
  executable boundary protection via Seatbelt sandbox-exec, allowlisted environment with auth.json setup,
  task problem.txt specification binding, stream bounds (16 MiB / 1 MiB), process group kill/reap,
  narrowed OS-spawn-only retries, strict JSONL output classifications, and PreflightRegistry limit accounting.
"""

import os
import sys
import json
import stat
import time
import signal
import hashlib
import shutil
import tempfile
import subprocess
from abc import ABC, abstractmethod
from typing import Tuple, Dict, Any, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_STDOUT_BYTES = 16 * 1024 * 1024  # 16 MiB
MAX_STDERR_BYTES = 1 * 1024 * 1024   # 1 MiB
CALL_WALL_LIMIT_SECONDS = 120.0


class ExecutableBoundaryError(RuntimeError):
    """Base exception for executable boundary failures."""
    pass


class ModelProvider(ABC):
    """Abstract base class for Campaign 2 model providers."""

    @abstractmethod
    def generate_candidate(self, task_id: str, effective_prompt: str,
                           call_id: str) -> Tuple[str, int]:
        """
        Generate candidate Python code for a task.
        Returns (candidate_code: str, tokens_used: int).
        """

    @abstractmethod
    def mutate_prompt(self, mutable_body: str, failure_context: str,
                      call_id: str) -> Tuple[str, int]:
        """
        Mutate the mutable strategy body of the prompt.
        Returns (new_mutable_body: str, tokens_used: int).
        """


class ProductionTaskEvaluator:
    """Production task evaluator using evaluate_candidate_code_sandboxed (C1-1)."""

    def evaluate(self, task_id: str, candidate_code: str) -> Tuple[int, int, bool, str]:
        from campaign2.proxy import evaluate_candidate_code_sandboxed
        return evaluate_candidate_code_sandboxed(task_id, candidate_code, timeout_seconds=15)


class MockModelProvider(ModelProvider):
    """
    Deterministic mock model provider for testing the orchestration vertical slice.
    """

    def __init__(self, base_score_numerator: int = 5, max_score: int = 10):
        self.is_mock = True
        self.base_score_numerator = base_score_numerator
        self.max_score = max_score
        self.call_log = []

    def generate_candidate(self, task_id: str, effective_prompt: str,
                           call_id: str) -> Tuple[str, int]:
        if task_id in ("canary", "") or "CANARY_OK" in effective_prompt:
            tokens = 10
            self.call_log.append({
                'type': 'generate_candidate',
                'task_id': task_id,
                'call_id': call_id,
                'prompt_hash': 'canary',
                'tokens': tokens
            })
            return "CANARY_OK", tokens

        prompt_hash = hashlib.sha256(effective_prompt.encode('utf-8')).hexdigest()[:16]
        code = (
            f"```python\n"
            f"# Mock candidate for {task_id}\n"
            f"# prompt_hash={prompt_hash}\n"
            f"# call_id={call_id}\n"
            f"def solve():\n"
            f"    return '{task_id}_{prompt_hash[:8]}'\n"
            f"```\n"
        )
        tokens = 100
        self.call_log.append({
            'type': 'generate_candidate',
            'task_id': task_id,
            'call_id': call_id,
            'prompt_hash': prompt_hash,
            'tokens': tokens
        })
        return code, tokens

    def mutate_prompt(self, mutable_body: str, failure_context: str,
                      call_id: str) -> Tuple[str, int]:
        from campaign2.envelope import sanitize_failure_context_for_optimizer
        sanitized_context = sanitize_failure_context_for_optimizer(failure_context)
        body_hash = hashlib.sha256(mutable_body.encode('utf-8')).hexdigest()[:16]
        has_failures = len(sanitized_context) > 0
        new_body = (
            f"# Mutation {call_id} (guided={has_failures})\n"
            f"# parent_hash={body_hash}\n"
            f"{mutable_body}\n"
        )
        tokens = 200
        self.call_log.append({
            'type': 'mutate_prompt',
            'call_id': call_id,
            'guided': has_failures,
            'failure_context': sanitized_context,
            'body_hash': body_hash,
            'tokens': tokens
        })
        return new_body, tokens


class MockTaskEvaluator:
    """
    Deterministic mock task evaluator for testing.
    """

    def __init__(self, default_earned: int = 5, default_max: int = 10):
        self.default_earned = default_earned
        self.default_max = default_max
        self.eval_log = []
        self._score_overrides = {}

    def set_score(self, task_id: str, earned: int, max_score: int) -> None:
        self._score_overrides[task_id] = (earned, max_score)

    def evaluate(self, task_id: str, candidate_code: str) -> Tuple[int, int, bool, str]:
        code_hash = hashlib.sha256(candidate_code.encode('utf-8')).hexdigest()[:16]
        if task_id in self._score_overrides:
            earned, max_s = self._score_overrides[task_id]
        else:
            earned, max_s = self.default_earned, self.default_max
        is_valid = True
        error_trace = ""
        self.eval_log.append({
            'task_id': task_id,
            'code_hash': code_hash,
            'earned': earned,
            'max': max_s,
            'is_valid': is_valid,
            'error_trace': error_trace,
        })
        return earned, max_s, is_valid, error_trace


def _get_codex_bin_path() -> str:
    for path in ("/opt/homebrew/bin/codex", "/usr/local/bin/codex"):
        if os.path.exists(path):
            return path
    return "/opt/homebrew/bin/codex"


def verify_codex_cli_version(binary: str = "codex") -> str:
    """Enforces exact CLI version 0.144.5; any other version aborts (C1-2 b)."""
    codex_bin = _get_codex_bin_path() if binary == "codex" else binary
    try:
        res = subprocess.run([codex_bin, "--version"], capture_output=True, text=True, timeout=5)
        out = (res.stdout or "") + (res.stderr or "")
    except Exception as exc:
        raise ExecutableBoundaryError(f"Failed to check {binary} --version: {exc}") from exc

    if "0.144.5" not in out:
        raise ExecutableBoundaryError(
            f"Exact CLI version 0.144.5 required, got: '{out.strip()}'"
        )
    return "0.144.5"


def render_seatbelt_profile(scratch_dir: str, workspace_root: str = BASE_DIR) -> str:
    """Renders generator.sb.template into a rendered profile inside scratch_dir (C1-2)."""
    template_p = os.path.join(workspace_root, "generator.sb.template")
    if not os.path.exists(template_p):
        template_p = os.path.join(BASE_DIR, "generator.sb.template")
    if not os.path.exists(template_p):
        raise ExecutableBoundaryError(f"Missing generator.sb.template: {template_p}")

    with open(template_p, "r", encoding="utf-8") as f:
        tmpl = f.read()

    codex_bin = _get_codex_bin_path()
    codex_prefix = os.path.dirname(os.path.dirname(codex_bin))

    sb_content = tmpl.format(
        codex_base_dir=codex_prefix,
        codex_prefix=codex_prefix,
        user_home=os.path.expanduser("~"),
        real_temp_home=os.path.realpath(scratch_dir),
        real_candidate_input_dir=os.path.realpath(scratch_dir),
    )

    sb_path = os.path.join(scratch_dir, "generator.sb")
    os.makedirs(scratch_dir, mode=0o700, exist_ok=True)
    with open(sb_path, "w", encoding="utf-8") as f:
        f.write(sb_content)
    return sb_path


def build_codex_cli_command(model: str, scratch_dir: str, sb_path: Optional[str] = None) -> List[str]:
    """
    ONE shared command builder for canary, generation and mutation using sandbox-exec (C1-2 a).
    """
    codex_bin = _get_codex_bin_path()
    base_cmd = [
        codex_bin, "exec",
        "-s", "read-only",
        "--ephemeral",
        "--json",
        "--model", model,
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ignore-rules",
        "-C", scratch_dir,
        "-"
    ]
    if sb_path:
        return ["sandbox-exec", "-f", sb_path] + base_cmd
    return base_cmd


def build_allowlisted_env(invocation_root: str) -> Dict[str, str]:
    """
    Child environment, allowlisted, with NO inherited API keys or proxy credentials (C1-7).
    """
    codex_home = os.path.join(invocation_root, ".codex")
    env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin",
        "HOME": invocation_root,
        "CODEX_HOME": codex_home,
        "TMPDIR": os.path.join(invocation_root, "tmp"),
    }
    return env


def setup_credential_home(scratch_dir: str, is_mock: bool = False, auth_source_path: Optional[str] = None) -> str:
    """Sets up scratch_dir/.codex/auth.json from verified source credential file (C1-7, P1-4)."""
    codex_dir = os.path.join(scratch_dir, ".codex")
    tmp_dir = os.path.join(scratch_dir, "tmp")
    os.makedirs(codex_dir, mode=0o700, exist_ok=True)
    os.makedirs(tmp_dir, mode=0o700, exist_ok=True)

    src_p = auth_source_path or os.environ.get("CODEX_AUTH_PATH") or os.path.expanduser("~/.codex/auth.json")
    dest_auth = os.path.join(codex_dir, "auth.json")

    if src_p and os.path.exists(src_p):
        if os.path.islink(src_p):
            raise ExecutableBoundaryError(f"Credential source cannot be a symlink: {src_p}")
        try:
            st = os.stat(src_p)
            if st.st_uid != os.getuid():
                raise ExecutableBoundaryError(f"Auth source file owned by unexpected UID: {st.st_uid}")
            shutil.copy2(src_p, dest_auth)
            os.chmod(dest_auth, 0o600)
            return dest_auth
        except Exception as exc:
            if not is_mock:
                raise ExecutableBoundaryError(f"Failed to copy production auth.json from {src_p}: {exc}") from exc

    if not is_mock:
        raise ExecutableBoundaryError(f"Production authentication requires valid auth.json at {src_p}")

    # Fallback to dummy auth.json ONLY for mock/test environments (is_mock=True)
    with open(dest_auth, "w", encoding="utf-8") as f:
        f.write('{"auth_mode": "test_credentials"}\n')
    os.chmod(dest_auth, 0o600)

    return dest_auth


def create_fresh_invocation_scratch_dir(workspace_root: str = BASE_DIR, is_mock: bool = False, attempt_id: str = "") -> str:
    """
    Scratch dir is fresh PER INVOCATION, empty, current-UID owned, non-symlink, outside repository (C1-2 c).
    """
    prefix = f"rsi_codex_scratch_{attempt_id}_" if attempt_id else "rsi_codex_scratch_"
    scratch_dir = tempfile.mkdtemp(prefix=prefix)
    try:
        scratch_real = os.path.realpath(scratch_dir)

        if os.path.islink(scratch_dir):
            raise ExecutableBoundaryError(f"Scratch dir must not be a symlink: {scratch_dir}")

        st = os.stat(scratch_real)
        if st.st_uid != os.getuid():
            raise ExecutableBoundaryError(f"Scratch dir must be owned by current UID {os.getuid()}, got {st.st_uid}")

        repo_real = os.path.realpath(workspace_root)
        try:
            rel = os.path.relpath(scratch_real, repo_real)
            if not rel.startswith(".."):
                raise ExecutableBoundaryError(f"Scratch dir must be OUTSIDE Git repository, got: {scratch_real}")
        except ValueError:
            pass

        setup_credential_home(scratch_real, is_mock=is_mock)
        return scratch_real
    except Exception:
        if os.path.exists(scratch_dir):
            shutil.rmtree(scratch_dir, ignore_errors=True)
        raise


def parse_and_classify_jsonl_output(
    stdout_bytes: bytes,
    stderr_bytes: bytes,
    return_code: int = 0,
    *,
    require_transport_evidence: bool = True,
) -> Dict[str, Any]:
    """
    Parses CLI JSONL output and classifies response strictly (C1-11, P0-3, P0-4).
    Raw bytes hashed BEFORE strict UTF-8 decoding.
    Enforces load-bearing zero-event retry invariant: if ANY JSONL event exists,
    classification is invalidated and retries are strictly forbidden.
    Requires exactly one turn.completed event as final line and non-empty final agent message.

    Note on Campaign 2 vs Campaign 3 retry conformance (Finding 7, R2):
    By default (`require_transport_evidence=True`), any non-zero CLI exit lacks
    transport-level proof that zero request bytes were transmitted or processed
    (`usage_known=False`), and is therefore classified as `"invalidated"` (non-retryable)
    regardless of stderr keywords such as HTTP 429/503. Passing `require_transport_evidence=False`
    is explicitly confined to historical Campaign 2 archive verification tests.
    """
    stdout_hash = hashlib.sha256(stdout_bytes).hexdigest()
    stderr_hash = hashlib.sha256(stderr_bytes).hexdigest()

    text = ""
    lines = []
    events = []
    try:
        text = stdout_bytes.decode("utf-8")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        for idx, l in enumerate(lines):
            try:
                ev = json.loads(l)
                if isinstance(ev, dict):
                    events.append(ev)
                else:
                    break
            except Exception:
                break
    except Exception:
        pass

    events_parsed = len(events)
    has_stdout = len(stdout_bytes.strip()) > 0

    if return_code != 0:
        stderr_text = stderr_bytes.decode("utf-8", errors="replace")
        transient_kw = (
            "HTTP 429", "429 Too Many Requests", "503 Service Unavailable",
            "DNS lookup failed", "TLS handshake failed", "connection refused",
            "ConnectionRefusedError", "connect ETIMEDOUT", "rate_limit_exceeded"
        )
        is_transient = any(kw in stderr_text for kw in transient_kw)

        # Zero-Stdout Load-Bearing Invariant: If ANY raw stdout was emitted (has_stdout=True),
        # if stderr does not contain transport failure keywords, or if strict transport evidence
        # is required (Campaign 3 conformance), retry is strictly forbidden!
        if require_transport_evidence or has_stdout or not is_transient:
            classification = "invalidated"
        else:
            classification = "transient_infrastructure_error"

        return {
            "parse_state": "nonzero_exit",
            "classification": classification,
            "usage_known": False,
            "tokens": 0,
            "text": "",
            "stdout_hash": stdout_hash,
            "stderr_hash": stderr_hash,
            "events_parsed": events_parsed,
            "error": f"Process exited with non-zero return code {return_code} (has_stdout={has_stdout}, events_parsed={events_parsed})",
        }

    if not lines or len(lines) != len(events):
        return {
            "parse_state": "malformed_jsonl",
            "classification": "invalidated",
            "usage_known": False,
            "tokens": 0,
            "text": "",
            "stdout_hash": stdout_hash,
            "stderr_hash": stderr_hash,
            "events_parsed": events_parsed,
            "error": "Malformed JSONL or unparseable lines present",
        }

    # Verify EXACTLY ONE turn.completed event exists and is the absolute LAST event
    turn_completed_events = [ev for ev in events if ev.get("type") == "turn.completed"]
    if len(turn_completed_events) != 1 or events[-1].get("type") != "turn.completed":
        return {
            "parse_state": "malformed_jsonl",
            "classification": "invalidated",
            "usage_known": False,
            "tokens": 0,
            "text": "",
            "stdout_hash": stdout_hash,
            "stderr_hash": stderr_hash,
            "events_parsed": events_parsed,
            "error": f"Expected exactly 1 final turn.completed event, got count={len(turn_completed_events)}",
        }

    agent_messages = []
    for ev in events[:-1]:
        ev_type = ev.get("type", "")
        if ev_type == "turn.completed":
            return {
                "parse_state": "malformed_jsonl",
                "classification": "invalidated",
                "usage_known": False,
                "tokens": 0,
                "text": "",
                "stdout_hash": stdout_hash,
                "stderr_hash": stderr_hash,
                "events_parsed": events_parsed,
                "error": "Duplicate turn.completed event before final event",
            }
        if ev_type == "item.completed":
            item = ev.get("item", {})
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text_content = item.get("text", "") or item.get("content", "")
                agent_messages.append(text_content)

    turn_ev = events[-1]
    usage = turn_ev.get("usage")
    if not isinstance(usage, dict) or "output_tokens" not in usage:
        return {
            "parse_state": "malformed_jsonl",
            "classification": "invalidated",
            "usage_known": False,
            "tokens": 0,
            "text": "",
            "stdout_hash": stdout_hash,
            "stderr_hash": stderr_hash,
            "events_parsed": events_parsed,
            "error": "turn.completed usage dictionary missing explicit output_tokens key",
        }

    output_tokens = usage.get("output_tokens")
    if type(output_tokens) is bool or not isinstance(output_tokens, int) or output_tokens < 0:
        return {
            "parse_state": "malformed_jsonl",
            "classification": "invalidated",
            "usage_known": False,
            "tokens": 0,
            "text": "",
            "stdout_hash": stdout_hash,
            "stderr_hash": stderr_hash,
            "events_parsed": events_parsed,
            "error": f"Invalid output_tokens value: {output_tokens}",
        }

    if not agent_messages:
        return {
            "parse_state": "malformed_jsonl",
            "classification": "invalidated",
            "usage_known": True,
            "tokens": output_tokens,
            "text": "",
            "stdout_hash": stdout_hash,
            "stderr_hash": stderr_hash,
            "events_parsed": events_parsed,
            "error": "No agent_message items found in stdout JSONL",
        }

    # Extract ONLY the final agent message (not concatenation of all messages)
    final_text = agent_messages[-1]
    if not final_text or len(final_text.strip()) == 0:
        return {
            "parse_state": "malformed_jsonl",
            "classification": "invalidated",
            "usage_known": True,
            "tokens": output_tokens,
            "text": "",
            "stdout_hash": stdout_hash,
            "stderr_hash": stderr_hash,
            "events_parsed": events_parsed,
            "error": "Final agent_message is empty or whitespace-only",
        }

    return {
        "parse_state": "valid",
        "classification": "valid",
        "usage_known": True,
        "tokens": output_tokens,
        "text": final_text,
        "stdout_hash": stdout_hash,
        "stderr_hash": stderr_hash,
        "events_parsed": events_parsed,
        "error": "",
    }


class SolModelProvider(ModelProvider):
    """
    Production model provider for gpt-5.6-sol via Codex CLI (C1-2, C1-3, C1-6, C1-7, C1-11).
    """

    def __init__(
        self,
        model_name: str = "gpt-5.6-sol",
        workspace_root: str = BASE_DIR,
        artifact_root: Optional[str] = None,
        state_root: Optional[str] = None,
        ledger: Optional[Any] = None,
        mock_subprocess: bool = False,
        attempt_id: Optional[str] = None,
        require_transport_evidence: bool = True,
    ):
        self.model_name = model_name
        self.workspace_root = workspace_root
        self.artifact_root = artifact_root or BASE_DIR
        self.state_root = state_root or workspace_root
        self.ledger = ledger
        self.mock_subprocess = mock_subprocess
        self.is_mock = mock_subprocess
        self.require_transport_evidence = require_transport_evidence
        if attempt_id:
            self.attempt_id = attempt_id
        elif ledger and hasattr(ledger, "ledger_path") and ledger.ledger_path:
            self.attempt_id = os.path.basename(os.path.dirname(os.path.abspath(ledger.ledger_path)))
        else:
            self.attempt_id = ""

    def _execute_cli_call(
        self,
        logical_call_id: str,
        stdin_bytes: bytes,
        task_id: str = "",
        problem_bytes: bytes = b"",
    ) -> Tuple[str, int, Dict[str, Any]]:
        """
        Executes a Codex CLI model invocation with three-record ledger logging,
        Seatbelt sandbox-exec profile rendering, process group kill/reap, stream bounds, and narrowed retries.
        """
        template_p = os.path.join(self.artifact_root, "generator.sb.template")
        if not os.path.exists(template_p):
            template_p = os.path.join(BASE_DIR, "generator.sb.template")
        if not os.path.exists(template_p):
            raise ExecutableBoundaryError(f"Missing generator.sb.template: {template_p}")

        task_file_hash = hashlib.sha256(problem_bytes).hexdigest()
        prompt_hash = hashlib.sha256(stdin_bytes).hexdigest()
        input_hash = prompt_hash

        max_attempts = 3
        reg_file = os.path.join(self.state_root, "campaign2_master_registration.json")
        if os.path.exists(reg_file):
            from campaign2.registration import verify_master_registration
            reg_data = verify_master_registration(self.state_root)
            max_attempts = reg_data.get("execution_limits", {}).get("max_invocation_attempts", 3)

        last_exception = None

        for invocation_ordinal in range(1, max_attempts + 1):
            scratch_dir = None
            proc = None
            t0 = time.monotonic()
            t0_wall = time.time()

            try:
                scratch_dir = create_fresh_invocation_scratch_dir(workspace_root=self.state_root, is_mock=self.mock_subprocess, attempt_id=self.attempt_id)
                sb_path = render_seatbelt_profile(scratch_dir, self.artifact_root)

                # Bind sandbox_profile_hash to rendered generator.sb bytes (P1-5)
                with open(sb_path, "rb") as f:
                    rendered_sb_bytes = f.read()
                sandbox_profile_hash = hashlib.sha256(rendered_sb_bytes).hexdigest()

                cmd = build_codex_cli_command(self.model_name, scratch_dir, sb_path)
                env = build_allowlisted_env(scratch_dir)

                argv_hash = hashlib.sha256(json.dumps(cmd).encode("utf-8")).hexdigest()
                env_hash = hashlib.sha256(json.dumps(env, sort_keys=True).encode("utf-8")).hexdigest()
                cwd_hash = hashlib.sha256(scratch_dir.encode("utf-8")).hexdigest()

                limits = {
                    "call_wall_limit_seconds": CALL_WALL_LIMIT_SECONDS,
                    "max_stdout_bytes": MAX_STDOUT_BYTES,
                    "max_stderr_bytes": MAX_STDERR_BYTES,
                }

                # 1. Record invocation_intent BEFORE spawn (fsynced)
                intent_record = {
                    "record_type": "invocation_intent",
                    "logical_call_id": logical_call_id,
                    "invocation_ordinal": invocation_ordinal,
                    "schedule_coordinate": {"logical_call_id": logical_call_id, "ordinal": invocation_ordinal},
                    "argv_hash": argv_hash,
                    "env_hash": env_hash,
                    "cwd_hash": cwd_hash,
                    "scratch_dir": scratch_dir,
                    "cwd": scratch_dir,
                    "prompt_hash": prompt_hash,
                    "task_file_hash": task_file_hash,
                    "sandbox_profile_hash": sandbox_profile_hash,
                    "input_hash": input_hash,
                    "applicable_limits": limits,
                    "timestamp": time.time(),
                }
                intent_bytes = (json.dumps(intent_record, sort_keys=True) + "\n").encode("utf-8")
                intent_hash = hashlib.sha256(intent_bytes).hexdigest()

                if self.ledger:
                    self.ledger._append_record(intent_record)

                # 2. Attempt spawn
                pid = None

                try:
                    if self.mock_subprocess:
                        pid = 99999 + invocation_ordinal
                    else:
                        proc = subprocess.Popen(
                            cmd,
                            stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            start_new_session=True,
                            text=False,
                            cwd=scratch_dir,
                            env=env,
                        )
                        pid = proc.pid

                except OSError as spawn_err:
                    completion_record = {
                        "record_type": "invocation_completion",
                        "logical_call_id": logical_call_id,
                        "invocation_ordinal": invocation_ordinal,
                        "intent_record_hash": intent_hash,
                        "started_record_hash": "",
                        "return_code": -1,
                        "raw_stdout_digest": hashlib.sha256(b"").hexdigest(),
                        "raw_stderr_digest": hashlib.sha256(b"").hexdigest(),
                        "text_hash": "",
                        "parse_state": "spawn_failure",
                        "usage_known": True,
                        "tokens": 0,
                        "duration": 0.0,
                        "classification": "spawn_failure",
                        "error": str(spawn_err),
                    }
                    comp_bytes = (json.dumps(completion_record, sort_keys=True) + "\n").encode("utf-8")
                    comp_hash = hashlib.sha256(comp_bytes).hexdigest()

                    if self.ledger:
                        self.ledger._append_record(completion_record)

                    from campaign2.preflight_registry import PreflightRegistry
                    reg = PreflightRegistry(workspace_root=self.state_root)
                    reg.record_usage(
                        tokens_used=0,
                        active_time=0.0,
                        logical_calls=1,
                        local_attempts=1,
                        logical_call_id=logical_call_id,
                        invocation_ordinal=invocation_ordinal,
                        completion_hash=comp_hash,
                    )

                    shutil.rmtree(scratch_dir, ignore_errors=True)
                    last_exception = spawn_err
                    if invocation_ordinal < max_attempts:
                        time.sleep(0.1 * (2 ** (invocation_ordinal - 1)))
                        continue
                    raise ExecutableBoundaryError(f"Spawn failed after {max_attempts} retries: {spawn_err}") from spawn_err

                # 3. Record process_started AFTER successful Popen spawn
                started_record = {
                    "record_type": "process_started",
                    "logical_call_id": logical_call_id,
                    "invocation_ordinal": invocation_ordinal,
                    "pid": pid,
                    "monotonic_ts": t0,
                    "wall_ts": t0_wall,
                    "intent_record_hash": intent_hash,
                }
                started_bytes = (json.dumps(started_record, sort_keys=True) + "\n").encode("utf-8")
                started_hash = hashlib.sha256(started_bytes).hexdigest()

                if self.ledger:
                    self.ledger._append_record(started_record)

                stdout_bytes = b""
                stderr_bytes = b""
                return_code = 0
                if self.mock_subprocess:
                    is_canary = "CANARY_OK" in stdin_bytes.decode("utf-8", errors="ignore") or task_id in ("canary", "")
                    is_mut = "MUTATION REQUEST" in stdin_bytes.decode("utf-8", errors="ignore")
                    if is_canary:
                        text_val = "CANARY_OK"
                    elif is_mut:
                        text_val = (
                            "# Strategy mutation\n"
                            "Focus on edge-case error recovery and stream framing.\n"
                        )
                    else:
                        # In mock_subprocess mode (offline integration testing without live Codex CLI),
                        # load reference benchmark solution if available to allow full-pipeline candidate evaluation.
                        sol_code = ""
                        for suite in ("suite_c", "suite_p"):
                            cand_sol = os.path.join(self.state_root, "benchmarks", "campaign2", suite, task_id, "solution.py")
                            if os.path.exists(cand_sol):
                                with open(cand_sol, "r", encoding="utf-8") as sf:
                                    sol_code = sf.read()
                                break
                        if sol_code:
                            text_val = f"```python\n{sol_code}\n```"
                        else:
                            text_val = (
                                "```python\n"
                                f"# Mock candidate for {task_id}\n"
                                "def solve():\n"
                                f"    return '{task_id}_mock'\n"
                                "```\n"
                            )
                    mock_resp = {
                        "type": "turn.completed",
                        "usage": {"output_tokens": 100}
                    }
                    mock_msg = {
                        "type": "item.completed",
                        "item": {
                            "type": "agent_message",
                            "text": text_val
                        }
                    }
                    stdout_bytes = (json.dumps(mock_msg) + "\n" + json.dumps(mock_resp) + "\n").encode("utf-8")
                    stderr_bytes = b""
                    return_code = 0
                else:
                    try:
                        stdout_bytes, stderr_bytes = proc.communicate(input=stdin_bytes, timeout=CALL_WALL_LIMIT_SECONDS)
                        return_code = proc.returncode
                    except subprocess.TimeoutExpired:
                        return_code = -9
                    except Exception:
                        return_code = -1

                    if len(stdout_bytes) > MAX_STDOUT_BYTES or len(stderr_bytes) > MAX_STDERR_BYTES:
                        return_code = -8

            finally:
                if proc:
                    if proc.poll() is None:
                        try:
                            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                        except Exception:
                            try:
                                proc.kill()
                            except Exception:
                                pass
                    try:
                        proc.wait(timeout=2.0)
                    except Exception:
                        pass
                t1 = time.monotonic()
                duration_sec = t1 - t0
                if scratch_dir and os.path.exists(scratch_dir):
                    try:
                        shutil.rmtree(scratch_dir, ignore_errors=False)
                    except Exception:
                        pass
                    if os.path.exists(scratch_dir):
                        raise ExecutableBoundaryError(f"Scratch directory cleanup failed or residue remains: {scratch_dir}")

            parsed = parse_and_classify_jsonl_output(
                stdout_bytes,
                stderr_bytes,
                return_code,
                require_transport_evidence=self.require_transport_evidence,
            )

            text_val = parsed.get("text", "")
            text_hash = hashlib.sha256(text_val.encode("utf-8")).hexdigest() if text_val else ""

            completion_record = {
                "record_type": "invocation_completion",
                "logical_call_id": logical_call_id,
                "invocation_ordinal": invocation_ordinal,
                "intent_record_hash": intent_hash,
                "started_record_hash": started_hash,
                "return_code": return_code,
                "raw_stdout_digest": parsed["stdout_hash"],
                "raw_stderr_digest": parsed["stderr_hash"],
                "text_hash": text_hash,
                "parse_state": parsed["parse_state"],
                "usage_known": parsed["usage_known"],
                "tokens": parsed["tokens"],
                "duration": duration_sec,
                "classification": parsed["classification"],
            }
            comp_bytes = (json.dumps(completion_record, sort_keys=True) + "\n").encode("utf-8")
            comp_hash = hashlib.sha256(comp_bytes).hexdigest()

            if self.ledger:
                self.ledger._append_record(completion_record)

            # Record usage in PreflightRegistry with exact completion identity (P0-1)
            from campaign2.preflight_registry import PreflightRegistry
            reg = PreflightRegistry(workspace_root=self.state_root)
            reg.record_usage(
                tokens_used=parsed["tokens"],
                active_time=duration_sec,
                logical_calls=1,
                local_attempts=1,
                logical_call_id=logical_call_id,
                invocation_ordinal=invocation_ordinal,
                completion_hash=comp_hash,
            )

            if parsed["classification"] == "valid" and return_code == 0:
                return parsed["text"], parsed["tokens"], parsed

            # Failure post-spawn or transport failure
            err_msg = parsed.get("error", "Invalid response classification")
            last_exception = ExecutableBoundaryError(
                f"Invocation {logical_call_id} ordinal {invocation_ordinal} failed: {err_msg}"
            )
            setattr(last_exception, "usage_known", parsed["usage_known"])
            setattr(last_exception, "tokens_used", parsed["tokens"])

            from campaign2.attempt_lifecycle import is_permitted_local_retry
            has_started = (started_hash != "")
            if (
                is_permitted_local_retry(
                    completion_record,
                    has_started,
                    require_transport_evidence=self.require_transport_evidence,
                )
                and invocation_ordinal < max_attempts
            ):
                time.sleep(0.1 * (2 ** (invocation_ordinal - 1)))
                continue
            raise last_exception

        raise last_exception or ExecutableBoundaryError(f"Invocation {logical_call_id} exhausted max attempts")

    def generate_candidate(self, task_id: str, effective_prompt: str,
                           call_id: str) -> Tuple[str, int]:
        from campaign2.envelope import IMMUTABLE_CONTRACT, DELIMITER

        if task_id in ("canary", ""):
            stdin_bytes = effective_prompt.encode("utf-8")
            problem_bytes = b""
        else:
            bench_dir = os.path.join(self.artifact_root, "benchmarks", "campaign2")
            if not os.path.exists(bench_dir):
                bench_dir = os.path.join(BASE_DIR, "benchmarks", "campaign2")
            suite_sub = "suite_c" if task_id.startswith("c_") else "suite_p"
            problem_path = os.path.join(bench_dir, suite_sub, task_id, "problem.txt")

            if not os.path.exists(problem_path):
                raise ExecutableBoundaryError(f"Missing problem.txt specification for task {task_id}: {problem_path}")

            with open(problem_path, "rb") as f:
                problem_bytes = f.read()

            problem_text = problem_bytes.decode("utf-8")

            full_prompt = (
                f"{IMMUTABLE_CONTRACT}\n\n"
                f"---PROBLEM SPECIFICATION---\n\n"
                f"{problem_text}"
                f"{DELIMITER}"
                f"{effective_prompt}"
            )
            stdin_bytes = full_prompt.encode("utf-8")

        text, tokens, _ = self._execute_cli_call(call_id, stdin_bytes, task_id=task_id, problem_bytes=problem_bytes)

        if task_id in ("canary", ""):
            return text, tokens

        stripped = text.strip()
        lines = stripped.splitlines()

        # Strict code fence extraction: exact ```python opening line, exact ``` closing line
        if not (len(lines) >= 2 and lines[0].strip() == "```python" and lines[-1].strip() == "```"):
            return "# malformed fence\n", tokens

        candidate_body = "\n".join(lines[1:-1]) + "\n"
        return candidate_body, tokens

    def mutate_prompt(self, mutable_body: str, failure_context: str,
                      call_id: str) -> Tuple[str, int]:
        from campaign2.envelope import PromptEnvelope, sanitize_failure_context_for_optimizer
        sanitized_context = sanitize_failure_context_for_optimizer(failure_context)
        prompt = (
            "# MUTATION REQUEST\n"
            f"# Failure Context:\n{sanitized_context}\n\n"
            f"# Mutable Strategy Body:\n{mutable_body}\n"
        )
        stdin_bytes = prompt.encode("utf-8")

        # Transport error raises ExecutableBoundaryError and fails closed
        text, tokens, _ = self._execute_cli_call(call_id, stdin_bytes)

        env = PromptEnvelope()
        try:
            clean_body = env.validate_mutation_output(text)
            return clean_body, tokens
        except Exception:
            # Valid model response containing invalid strategy retains parent strategy body
            return mutable_body, tokens
