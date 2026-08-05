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
import argparse
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
_EXPLICIT_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_CODEX_VERSION_RE = re.compile(
    r"^codex-cli\s+\d+\.\d+\.\d+(?:[-+][A-Za-z0-9._-]+)?$"
)

os.makedirs(HISTORY_DIR, exist_ok=True)
os.makedirs(PROMPTS_DIR, exist_ok=True)

class ExperimentInvalidError(Exception):
    """Raised when the experiment is invalidated due to infrastructure failures."""

    def __init__(
        self,
        message: str,
        *,
        stdout: str | bytes | None = None,
        stderr: str | bytes | None = None,
        stderr_summary: str | None = None,
        unresolved_codex_home: str | None = None,
        cleanup_error: BaseException | str | None = None,
    ):
        super().__init__(message)
        self.stdout = normalize_and_truncate_stream(stdout)
        self.stderr = normalize_and_truncate_stream(stderr)
        self.stderr_summary = (
            stderr_summary
            if stderr_summary is not None
            else get_stderr_summary(stderr)
        )
        self.unresolved_codex_home = unresolved_codex_home
        self.cleanup_error = (
            None
            if cleanup_error is None
            else f"{type(cleanup_error).__name__}: {cleanup_error}"
            if isinstance(cleanup_error, BaseException)
            else str(cleanup_error)
        )

class PointerCommitIndeterminateError(ExperimentInvalidError):
    """Raised when the pointer has already been mutated but a post-commit action (such as directory fsync) fails."""

class RunContext:
    def __init__(self):
        self.run_id = None
        self.run_dir = None
        self.approved_commit_sha = None
        self.lock_file_hash = None
        self.codex_version = None
        self.codex_cli_version = "unavailable"
        self.lock_model = None
        self.codex_model = None
        self.model_resolved = "unavailable"
        self.sw_vers = "unavailable"
        self.python_version = sys.version
        self.created_codex_home = None
        self.unresolved_codex_home = None
        self.cleanup_error = None
        self.consumed_tokens = 0
        self.unknown_token_state = False
        self.current_stage = "pre-init"
        self.allow_no_holdout = None
        self.created_canary_dir = None
        self.canary_candidate_relpath = "unknown"
        self.lock_remote_url = "unknown"
        self.configured_origin_url = "unknown"
        self.local_head_sha = "unknown"
        self.remote_main_sha = "unknown"
        self.canary_tokens_consumed = "unknown"
        self.preflight_huffman_data = None
        self.trial_results = None
        self.validation_run_record = None
        self.holdout_record = None
        self.terminal_state = "not_started"
        self.operator_report = {}


def normalize_and_truncate_stream(
    stream: str | bytes | None,
    max_bytes: int = 65536,
) -> str:
    """Return bounded UTF-8 text, including the truncation marker in the bound."""
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")
    value = _normalize_stream_unbounded(stream)
    if not value:
        return value
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= max_bytes:
        return value
    marker = b"\n[TRUNCATED]"
    if max_bytes <= len(marker):
        return marker[:max_bytes].decode("utf-8", errors="ignore")
    prefix = encoded[: max_bytes - len(marker)].decode("utf-8", errors="ignore")
    return prefix + marker.decode("ascii")


def _normalize_stream_unbounded(stream: str | bytes | None) -> str:
    if stream is None:
        return ""
    return (
        stream.decode("utf-8", errors="replace")
        if isinstance(stream, bytes)
        else str(stream)
    )


def get_stderr_summary(stderr: str | bytes | None) -> str:
    normalized = _normalize_stream_unbounded(stderr)
    lines = [line for line in normalized.splitlines() if line.strip()]
    if not lines:
        return "stderr was empty"
    non_warnings = [
        line for line in lines if not line.lstrip().startswith("WARNING")
    ]
    if not non_warnings:
        return lines[-1]
    return non_warnings[-1]

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

def parse_codex_jsonl_output(
    stdout_lines: list,
) -> tuple:
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

        if not isinstance(event, dict):
            raise ExperimentInvalidError("Codex JSONL event must be an object")

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

_SAFE_RUN_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def _mode_bits(st_result: os.stat_result) -> int:
    return stat.S_IMODE(st_result.st_mode)


def _assert_owned_directory(path: str, mode: int) -> None:
    st_result = os.lstat(path)
    if stat.S_ISLNK(st_result.st_mode) or not stat.S_ISDIR(st_result.st_mode):
        raise ExperimentInvalidError(f"Expected real directory at {path}")
    if st_result.st_uid != os.getuid():
        raise ExperimentInvalidError(f"Directory is not owned by current uid: {path}")
    if _mode_bits(st_result) != mode:
        raise ExperimentInvalidError(
            f"Directory mode mismatch for {path}: expected {oct(mode)}, got {oct(_mode_bits(st_result))}"
        )


def _assert_components_without_symlinks(path: str, parent_dir: str) -> None:
    absolute_path = os.path.abspath(path)
    absolute_parent = os.path.abspath(parent_dir)
    if os.path.commonpath([absolute_path, absolute_parent]) != absolute_parent:
        raise ExperimentInvalidError(f"Path escapes credential parent: {path}")
    current = absolute_parent
    if os.path.lexists(current) and stat.S_ISLNK(os.lstat(current).st_mode):
        raise ExperimentInvalidError(f"Symlink detected in credential path: {current}")
    relative = os.path.relpath(absolute_path, absolute_parent)
    if relative == ".":
        return
    for component in relative.split(os.sep):
        current = os.path.join(current, component)
        if not os.path.lexists(current):
            continue
        st_result = os.lstat(current)
        if stat.S_ISLNK(st_result.st_mode):
            raise ExperimentInvalidError(f"Symlink detected in credential path: {current}")
        if current != absolute_path and not stat.S_ISDIR(st_result.st_mode):
            raise ExperimentInvalidError(f"Non-directory credential path component: {current}")


def _remove_owned_codex_home(codex_home: str, parent_dir: str) -> None:
    lexical_parent = os.path.abspath(parent_dir)
    lexical_home = os.path.abspath(codex_home)
    expected_parent = os.path.realpath(parent_dir)
    if os.path.dirname(lexical_home) == lexical_parent:
        component_parent = lexical_parent
    elif os.path.dirname(lexical_home) == expected_parent:
        component_parent = expected_parent
    else:
        raise ExperimentInvalidError(f"Refusing cleanup outside credential parent: {codex_home}")
    _assert_components_without_symlinks(lexical_home, component_parent)
    absolute_home = os.path.realpath(lexical_home)
    if os.path.dirname(absolute_home) != expected_parent:
        raise ExperimentInvalidError(f"Refusing cleanup outside credential parent: {codex_home}")
    if not os.path.basename(absolute_home).startswith("codex_home_"):
        raise ExperimentInvalidError(f"Refusing cleanup of unexpected path: {codex_home}")
    if os.path.realpath(absolute_home) != os.path.join(
        expected_parent, os.path.basename(absolute_home)
    ):
        raise ExperimentInvalidError(f"Refusing cleanup of resolved path mismatch: {codex_home}")
    _assert_owned_directory(absolute_home, 0o700)
    shutil.rmtree(absolute_home)


def _build_codex_run_scoped_home(
    ctx: RunContext,
    run_id: str,
    auth_source_path: str | None = None,
) -> str:
    if not isinstance(run_id, str) or not _SAFE_RUN_ID_RE.fullmatch(run_id):
        raise ExperimentInvalidError(f"Unsafe credential run id: {run_id!r}")
    if ctx.created_codex_home is not None:
        raise ExperimentInvalidError("Run context already owns a Codex home")

    parent_dir = os.path.dirname(os.path.abspath(BASE_DIR))
    codex_home = os.path.join(parent_dir, f"codex_home_{run_id}")
    absolute_parent = os.path.abspath(parent_dir)
    absolute_home = os.path.abspath(codex_home)
    private_root = os.path.abspath(
        os.path.join(os.path.dirname(BASE_DIR), "rsi_loop_private")
    )
    if os.path.commonpath([absolute_home, absolute_parent]) != absolute_parent:
        raise ExperimentInvalidError("Credential home escapes its parent")
    if is_contained(absolute_home, os.path.abspath(BASE_DIR)):
        raise ExperimentInvalidError("Credential home must be outside BASE_DIR")
    if is_contained(absolute_home, private_root):
        raise ExperimentInvalidError("Credential home must be outside the private holdout")
    _assert_components_without_symlinks(absolute_home, absolute_parent)

    created_by_this_call = False
    try:
        os.mkdir(absolute_home, mode=0o700)
        created_by_this_call = True

        real_parent = os.path.realpath(absolute_parent)
        real_home = os.path.realpath(absolute_home)
        if os.path.commonpath([real_home, real_parent]) != real_parent:
            raise ExperimentInvalidError("Resolved credential home escapes its parent")
        if is_contained(real_home, os.path.realpath(BASE_DIR)):
            raise ExperimentInvalidError("Resolved credential home is inside BASE_DIR")
        if is_contained(real_home, os.path.realpath(private_root)):
            raise ExperimentInvalidError("Resolved credential home is inside private holdout")
        _assert_components_without_symlinks(real_home, real_parent)
        _assert_owned_directory(real_home, 0o700)

        codex_dir = os.path.join(real_home, ".codex")
        tmp_dir = os.path.join(real_home, "tmp")
        os.mkdir(codex_dir, mode=0o700)
        os.mkdir(tmp_dir, mode=0o700)
        _assert_owned_directory(codex_dir, 0o700)
        _assert_owned_directory(tmp_dir, 0o700)

        source_path = auth_source_path or os.path.expanduser("~/.codex/auth.json")
        source_fd = None
        dest_fd = None
        dest_auth = os.path.join(codex_dir, "auth.json")
        try:
            source_fd = os.open(
                source_path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            )
            source_stat = os.fstat(source_fd)
            if not stat.S_ISREG(source_stat.st_mode):
                raise ExperimentInvalidError("Codex auth source is not a regular file")
            if source_stat.st_uid != os.getuid():
                raise ExperimentInvalidError("Codex auth source has unexpected owner")
            if _mode_bits(source_stat) & 0o077:
                raise ExperimentInvalidError("Codex auth source is accessible by group or others")

            dest_fd = os.open(
                dest_auth,
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            while True:
                chunk = os.read(source_fd, 65536)
                if not chunk:
                    break
                view = memoryview(chunk)
                while view:
                    written = os.write(dest_fd, view)
                    if written <= 0:
                        raise ExperimentInvalidError("Short write while copying Codex auth")
                    view = view[written:]
            os.fsync(dest_fd)
            dest_stat = os.fstat(dest_fd)
            if not stat.S_ISREG(dest_stat.st_mode):
                raise ExperimentInvalidError("Codex auth destination is not regular")
            if dest_stat.st_uid != os.getuid() or dest_stat.st_nlink != 1:
                raise ExperimentInvalidError("Codex auth destination ownership/link check failed")
            if _mode_bits(dest_stat) != 0o600:
                raise ExperimentInvalidError("Codex auth destination mode is not 0600")
        finally:
            if dest_fd is not None:
                os.close(dest_fd)
            if source_fd is not None:
                os.close(source_fd)

        final_stat = os.lstat(dest_auth)
        if (
            stat.S_ISLNK(final_stat.st_mode)
            or not stat.S_ISREG(final_stat.st_mode)
            or final_stat.st_uid != os.getuid()
            or final_stat.st_nlink != 1
            or _mode_bits(final_stat) != 0o600
        ):
            raise ExperimentInvalidError("Codex auth destination post-check failed")

        ctx.created_codex_home = real_home
        created_by_this_call = False
        return real_home
    except BaseException as error:
        if created_by_this_call:
            try:
                _remove_owned_codex_home(absolute_home, absolute_parent)
            except BaseException as cleanup_error:
                ctx.created_codex_home = absolute_home
                ctx.unresolved_codex_home = absolute_home
                ctx.cleanup_error = f"{type(cleanup_error).__name__}: {cleanup_error}"
                primary_stdout = getattr(error, "stdout", None)
                primary_stderr = getattr(error, "stderr", None)
                raise ExperimentInvalidError(
                    f"{type(error).__name__}: {error}; cleanup of Codex home "
                    f"{absolute_home!r} failed: {type(cleanup_error).__name__}: {cleanup_error}",
                    stdout=primary_stdout,
                    stderr=primary_stderr,
                    stderr_summary=getattr(error, "stderr_summary", None),
                    unresolved_codex_home=absolute_home,
                    cleanup_error=cleanup_error,
                ) from error
            else:
                created_by_this_call = False
        ctx.created_codex_home = None
        ctx.unresolved_codex_home = None
        ctx.cleanup_error = None
        if isinstance(error, ExperimentInvalidError):
            raise
        raise ExperimentInvalidError(
            f"Failed to build run-scoped Codex home: {type(error).__name__}: {error}"
        ) from error


def _cleanup_codex_run_scoped_home(ctx: RunContext, expected_home: str) -> None:
    owned_home = ctx.created_codex_home
    if owned_home is None:
        return
    try:
        if os.path.abspath(owned_home) != os.path.abspath(expected_home):
            raise ExperimentInvalidError("Codex home cleanup sentinel mismatch")
        _remove_owned_codex_home(
            owned_home,
            os.path.dirname(os.path.abspath(BASE_DIR)),
        )
    except BaseException as cleanup_error:
        ctx.unresolved_codex_home = owned_home
        ctx.cleanup_error = f"{type(cleanup_error).__name__}: {cleanup_error}"
        raise ExperimentInvalidError(
            f"Codex home cleanup failed for {owned_home!r}: "
            f"{type(cleanup_error).__name__}: {cleanup_error}",
            unresolved_codex_home=owned_home,
            cleanup_error=cleanup_error,
        ) from cleanup_error
    else:
        ctx.created_codex_home = None
        ctx.unresolved_codex_home = None
        ctx.cleanup_error = None


def _cleanup_after_call(
    ctx: RunContext,
    expected_home: str,
    primary_error: BaseException | None,
) -> None:
    try:
        _cleanup_codex_run_scoped_home(ctx, expected_home)
    except ExperimentInvalidError as cleanup_failure:
        if primary_error is None:
            raise
        unresolved_home = (
            cleanup_failure.unresolved_codex_home
            or ctx.unresolved_codex_home
            or expected_home
        )
        cleanup_diagnostic = (
            cleanup_failure.cleanup_error or str(cleanup_failure)
        )
        raise ExperimentInvalidError(
            f"{type(primary_error).__name__}: {primary_error}; "
            f"{cleanup_failure}",
            stdout=getattr(primary_error, "stdout", None),
            stderr=getattr(primary_error, "stderr", None),
            stderr_summary=getattr(primary_error, "stderr_summary", None),
            unresolved_codex_home=unresolved_home,
            cleanup_error=cleanup_diagnostic,
        ) from primary_error


# Compatibility for the existing generator startup regression. Production
# callers use the context-aware API above; these wrappers retain ownership so
# the legacy test still receives the same safe lifecycle.
_LEGACY_CODEX_HOME_CONTEXTS: dict[str, RunContext] = {}


def _build_codex_temp_home() -> str:
    legacy_ctx = RunContext()
    run_id = f"legacy_{uuid.uuid4().hex}"
    home = _build_codex_run_scoped_home(legacy_ctx, run_id)
    _LEGACY_CODEX_HOME_CONTEXTS[home] = legacy_ctx
    return home


def _cleanup_temp_home(real_temp_home: str) -> None:
    legacy_ctx = _LEGACY_CODEX_HOME_CONTEXTS.pop(real_temp_home, None)
    if legacy_ctx is None:
        sys.stderr.write(
            f"WARNING: no ownership context for legacy Codex home {real_temp_home!r}\n"
        )
        sys.stderr.flush()
        return
    _cleanup_codex_run_scoped_home(legacy_ctx, real_temp_home)

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


def _codex_environment(codex_home: str) -> dict:
    return {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin",
        "LANG": "en_US.UTF-8",
        "HOME": codex_home,
        "USER": os.environ.get("USER", "codex"),
        "LOGNAME": os.environ.get("LOGNAME", "codex"),
        "TMPDIR": os.path.join(codex_home, "tmp"),
    }


def _build_codex_command(
    sb_path: str,
    model: str,
    candidate_dir: str,
) -> list[str]:
    return [
        "sandbox-exec", "-f", sb_path,
        _get_codex_bin_path(), "exec", "-",
        "-s", "read-only",
        "--ephemeral",
        "--json",
        "--model", model,
        "-C", candidate_dir,
    ]


def _validate_canary_dir(path: str, expected_parent: str, temp_home: str) -> None:
    if not expected_parent:
        raise ExperimentInvalidError("Expected parent directory is not set")
    if not temp_home:
        raise ExperimentInvalidError("Credential temp home is not set")

    # 1. lstat checks on path itself (first, before resolving realpath)
    try:
        stat_info = os.lstat(path)
    except OSError as e:
        raise ExperimentInvalidError(f"Canary candidate directory {path} does not exist: {e}")

    import stat as stat_mod
    if stat_mod.S_ISLNK(stat_info.st_mode):
        raise ExperimentInvalidError(f"Canary candidate directory {path} is a symlink")
    if not stat_mod.S_ISDIR(stat_info.st_mode):
        raise ExperimentInvalidError(f"Canary candidate directory {path} is not a directory")

    # Owner check on path
    if stat_info.st_uid != os.getuid():
        raise ExperimentInvalidError(f"Canary candidate directory {path} st_uid {stat_info.st_uid} does not match current UID {os.getuid()}")

    # Mode check on path (exactly 0o700)
    if (stat_info.st_mode & 0o777) != 0o700:
        raise ExperimentInvalidError(f"Canary candidate directory {path} mode {oct(stat_info.st_mode & 0o777)} is not 0o700")

    # 2. lstat checks on expected_parent
    try:
        parent_stat = os.lstat(expected_parent)
    except OSError as e:
        raise ExperimentInvalidError(f"Expected parent directory {expected_parent} does not exist: {e}")

    if stat_mod.S_ISLNK(parent_stat.st_mode):
        raise ExperimentInvalidError(f"Expected parent directory {expected_parent} is a symlink")
    if not stat_mod.S_ISDIR(parent_stat.st_mode):
        raise ExperimentInvalidError(f"Expected parent directory {expected_parent} is not a directory")
    if parent_stat.st_uid != os.getuid():
        raise ExperimentInvalidError(f"Expected parent directory {expected_parent} st_uid {parent_stat.st_uid} does not match current UID {os.getuid()}")

    # 3. Containment check of expected_parent under HISTORY_DIR
    real_parent = os.path.realpath(expected_parent)
    real_history = os.path.realpath(HISTORY_DIR)
    if os.path.commonpath([real_parent, real_history]) != real_history:
        raise ExperimentInvalidError("Expected parent directory is not under HISTORY_DIR")

    real_path = os.path.realpath(path)
    real_temp_home = os.path.realpath(temp_home)

    # 4. Exact basename check
    if os.path.basename(path) != "canary_candidate":
        raise ExperimentInvalidError(f"Canary candidate directory name is not 'canary_candidate', got: {os.path.basename(path)}")

    # 5. Exact parent check
    if os.path.dirname(real_path) != real_parent:
        raise ExperimentInvalidError("Canary candidate directory parent is not expected_parent")

    # 6. Containment checks
    # Lexical:
    lex_common = os.path.commonpath([os.path.abspath(path), os.path.abspath(expected_parent)])
    if lex_common != os.path.abspath(expected_parent):
        raise ExperimentInvalidError("Canary candidate directory lexical containment check failed")
    # Resolved:
    res_common = os.path.commonpath([real_path, real_parent])
    if res_common != real_parent:
        raise ExperimentInvalidError("Canary candidate directory resolved containment check failed")

    # 7. Must be outside temp_home
    home_common = os.path.commonpath([real_path, real_temp_home])
    if home_common == real_temp_home:
        raise ExperimentInvalidError("Canary candidate directory must be outside the credential home directory")

    # Emptiness check
    if os.listdir(real_path) != []:
        raise ExperimentInvalidError(f"Canary candidate directory {path} is not empty")


def _cleanup_canary_dir(path: str, expected_parent: str, temp_home: str) -> None:
    _validate_canary_dir(path, expected_parent, temp_home)
    shutil.rmtree(os.path.realpath(path))


def _raise_launch_failure(label: str, error: BaseException) -> None:
    stdout = getattr(error, "stdout", None)
    stderr = getattr(error, "stderr", None)
    summary = (
        get_stderr_summary(stderr)
        if _normalize_stream_unbounded(stderr).strip()
        else str(error)
    )
    raise ExperimentInvalidError(
        f"{label} process execution failed: {summary}",
        stdout=stdout,
        stderr=stderr,
    ) from error


def _run_codex_version_smoke_test(sb_path: str, env: dict, candidate_dir: str) -> str:
    codex_bin = _get_codex_bin_path()
    cmd = ["sandbox-exec", "-f", sb_path, codex_bin, "--version"]
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
            cwd=candidate_dir,
        )
    except (subprocess.SubprocessError, OSError) as error:
        _raise_launch_failure("Codex smoke test", error)
    raw_stdout = res.stdout
    raw_stderr = res.stderr
    stdout = normalize_and_truncate_stream(raw_stdout)
    stderr_summary = get_stderr_summary(raw_stderr)
    if res.returncode != 0:
        raise ExperimentInvalidError(
            f"Codex smoke test failed (codex --version returned {res.returncode}):\n"
            f"stderr: {stderr_summary}",
            stdout=raw_stdout,
            stderr=raw_stderr,
            stderr_summary=stderr_summary,
        )
    version = stdout.strip()
    if not version or not _CODEX_VERSION_RE.fullmatch(version):
        raise ExperimentInvalidError(
            f"Codex smoke test returned invalid version output: {version!r}",
            stdout=raw_stdout,
            stderr=raw_stderr,
            stderr_summary=stderr_summary,
        )
    sys.stderr.write(f"[smoke] codex --version OK: {version}\n")
    sys.stderr.flush()
    return version

def _run_codex_canary(
    sb_path: str, env: dict, ctx: RunContext, canary_candidate_dir: str
) -> int:
    canary_prompt = "Reply with exactly one word: ready"
    cmd = _build_codex_command(sb_path, ctx.codex_model, canary_candidate_dir)
    try:
        res = subprocess.run(
            cmd,
            input=canary_prompt,
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            cwd=canary_candidate_dir,
        )
    except (subprocess.SubprocessError, OSError) as error:
        ctx.unknown_token_state = True
        _raise_launch_failure("Canary", error)

    raw_stdout = res.stdout
    raw_stderr = res.stderr
    stdout = normalize_and_truncate_stream(raw_stdout)
    stderr_summary = get_stderr_summary(raw_stderr)

    if res.returncode != 0:
        ctx.unknown_token_state = True
        raise ExperimentInvalidError(
            f"Codex canary call failed (rc={res.returncode}): "
            f"{stderr_summary}",
            stdout=raw_stdout,
            stderr=raw_stderr,
            stderr_summary=stderr_summary,
        )

    try:
        _, usage = parse_codex_jsonl_output(stdout.splitlines())
        tokens_val = usage.get("output_tokens")
        if tokens_val is None:
            raise ValueError("Codex canary call returned no output_tokens")
        tokens = int(tokens_val)
        if tokens < 0:
            raise ValueError(f"Negative token count: {tokens}")
    except Exception as e:
        ctx.unknown_token_state = True
        raise ExperimentInvalidError(
            f"Canary token parsing failed: {e}",
            stdout=raw_stdout,
            stderr=raw_stderr,
            stderr_summary=stderr_summary,
        ) from e

    ctx.consumed_tokens += tokens
    ctx.model_resolved = "unavailable"
    if ctx.consumed_tokens > TOKEN_CEILING:
        raise ExperimentInvalidError(
            f"Token ceiling breached during canary: {ctx.consumed_tokens} > {TOKEN_CEILING}",
            stdout=raw_stdout,
            stderr=raw_stderr,
            stderr_summary=stderr_summary,
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

    call_run_id = ctx.run_id or f"adhoc_{uuid.uuid4().hex}"
    real_temp_home = _build_codex_run_scoped_home(ctx, call_run_id)
    real_candidate_input_dir = os.path.realpath(candidate_dir)
    primary_error = None

    try:
        sb_path = _build_generator_sb(real_temp_home, real_candidate_input_dir)

        allowed_env = _codex_environment(real_temp_home)

        cmd = _build_codex_command(sb_path, ctx.codex_model, real_candidate_input_dir)

        try:
            res = subprocess.run(
                cmd,
                input=full_prompt,
                capture_output=True,
                text=True,
                timeout=120,
                env=allowed_env,
                cwd=real_candidate_input_dir,
            )
        except (subprocess.SubprocessError, OSError) as error:
            ctx.unknown_token_state = True
            _raise_launch_failure("Codex", error)

        raw_stdout = res.stdout
        raw_stderr = res.stderr
        stdout = normalize_and_truncate_stream(raw_stdout)
        stderr_summary = get_stderr_summary(raw_stderr)

        if res.returncode != 0:
            ctx.unknown_token_state = True
            raise ExperimentInvalidError(
                f"Codex returned non-zero exit code {res.returncode}. "
                f"Stderr: {stderr_summary}",
                stdout=raw_stdout,
                stderr=raw_stderr,
                stderr_summary=stderr_summary,
            )

        try:
            raw_msg, usage = parse_codex_jsonl_output(stdout.splitlines())
            tokens_val = usage.get("output_tokens")
            if tokens_val is None:
                raise ValueError("Codex call returned no output_tokens")
            tokens = int(tokens_val)
            if tokens < 0:
                raise ValueError(f"Negative token count: {tokens}")
        except Exception as e:
            ctx.unknown_token_state = True
            raise ExperimentInvalidError(
                f"Codex call token parsing failed: {e}",
                stdout=raw_stdout,
                stderr=raw_stderr,
                stderr_summary=stderr_summary,
            ) from e

        ctx.consumed_tokens += tokens
        if ctx.consumed_tokens > TOKEN_CEILING:
            raise ExperimentInvalidError(
                f"Token ceiling breached: {ctx.consumed_tokens} > {TOKEN_CEILING}",
                stdout=raw_stdout,
                stderr=raw_stderr,
                stderr_summary=stderr_summary,
            )

        if expect_json_code:
            return extract_code(raw_msg), tokens
        else:
            return raw_msg, tokens

    except BaseException as error:
        primary_error = error
        raise
    finally:
        _cleanup_after_call(
            ctx,
            real_temp_home,
            primary_error,
        )

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
            f.flush()
            os.fchmod(f.fileno(), 0o444)
            os.fsync(f.fileno())

        test_harness_path = os.path.join(benchmark_dir, "test_harness.py")
        eval_res = run_evaluation_on_candidate(iter_task_dir, test_harness_path)

        solution_rel_path = os.path.relpath(os.path.realpath(solution_file), os.path.realpath(ctx.run_dir))
        results[benchmark] = {
            "success": eval_res["success"],
            "error": eval_res.get("error"),
            "tokens_consumed": tokens,
            "solution_path": solution_rel_path,
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


def publish_terminal_manifest_and_report(ctx: RunContext, manifest_path: str, manifest_data: dict, pointer_commit_status: str = "not_applicable") -> None:
    if ctx.terminal_state != "not_started":
        raise ExperimentInvalidError("Terminal publication already in progress or completed — competing write blocked")

    sync_context_to_report(ctx)
    ctx.terminal_state = "staging"

    manifest_bytes = json.dumps(manifest_data, indent=2).encode('utf-8')
    manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()

    ctx.operator_report["schema_version"] = "2"
    ctx.operator_report["status"] = manifest_data["status"]
    ctx.operator_report["manifest_sha256"] = manifest_sha
    ctx.operator_report["pointer_commit_status"] = pointer_commit_status
    ctx.operator_report["model_requested"] = manifest_data.get("model_requested", "unknown")
    ctx.operator_report["model_resolved"] = manifest_data.get("model_resolved", "unknown")
    ctx.operator_report["tokens"]["cumulative_consumed"] = ctx.consumed_tokens

    common_keys = ["run_id", "approved_commit_sha", "lock_file_hash"]
    for key in common_keys:
        val_m = manifest_data.get(key)
        if key == "approved_commit_sha":
            val_r = ctx.operator_report["lock_contents"]["approved_commit_sha"]
        elif key == "lock_file_hash":
            val_r = ctx.lock_file_hash or "unknown"
        else:
            val_r = ctx.operator_report.get(key)
        if val_m != val_r:
            raise ExperimentInvalidError(f"Field mismatch between manifest and report for key '{key}': {val_m} vs {val_r}")

    report_path = os.path.join(ctx.run_dir, "operator_report.json")
    tmp_manifest_path = manifest_path + ".tmp"
    tmp_report_path = report_path + ".tmp"

    if os.path.exists(tmp_manifest_path):
        ctx.terminal_state = "not_started"
        raise ExperimentInvalidError(f"Staging manifest file already exists: {tmp_manifest_path}")
    if os.path.exists(tmp_report_path):
        ctx.terminal_state = "not_started"
        raise ExperimentInvalidError(f"Staging report file already exists: {tmp_report_path}")

    # Stage manifest, report
    try:
        m_fd = os.open(tmp_manifest_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(m_fd, 'wb') as mf:
            mf.write(manifest_bytes)
            mf.flush()
            os.fchmod(mf.fileno(), stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            os.fsync(mf.fileno())

        r_fd = os.open(tmp_report_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(r_fd, 'w') as rf:
            json.dump(ctx.operator_report, rf, indent=2)
            rf.flush()
            os.fchmod(rf.fileno(), stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            os.fsync(rf.fileno())

    except Exception as e:
        for p in [tmp_manifest_path, tmp_report_path]:
            if os.path.exists(p):
                try:
                    os.remove(p)
                except Exception:
                    pass
        ctx.terminal_state = "not_started"
        raise ExperimentInvalidError(f"Failed to stage terminal files: {e}")

    # atomically publish manifest
    try:
        os.replace(tmp_manifest_path, manifest_path)
    except Exception as e:
        if os.path.exists(tmp_manifest_path):
            try:
                os.remove(tmp_manifest_path)
            except Exception:
                pass
        if os.path.exists(tmp_report_path):
            try:
                os.remove(tmp_report_path)
            except Exception:
                pass
        ctx.terminal_state = "not_started"
        raise ExperimentInvalidError(f"Failed to atomically publish manifest: {e}")

    ctx.terminal_state = "manifest_published"

    # atomically publish report
    try:
        os.replace(tmp_report_path, report_path)
    except Exception as e:
        if os.path.exists(tmp_report_path):
            try:
                os.remove(tmp_report_path)
            except Exception:
                pass
        raise ExperimentInvalidError(f"Failed to atomically publish report: {e}")

    # fsync on parent dir
    parent_dir = os.path.dirname(os.path.realpath(manifest_path))
    dir_fd = None
    try:
        dir_fd = os.open(parent_dir, os.O_RDONLY)
        os.fsync(dir_fd)
    except Exception as e:
        raise ExperimentInvalidError(f"Failed to fsync manifest parent directory: {e}")
    finally:
        if dir_fd is not None:
            try:
                os.close(dir_fd)
            except Exception:
                pass

    try:
        os.chmod(manifest_path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        os.chmod(report_path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    except Exception as e:
        raise ExperimentInvalidError(f"Failed to seal terminal files: {e}")

    try:
        with open(manifest_path, 'rb') as mf:
            read_manifest_bytes = mf.read()
        with open(report_path, 'r') as rf:
            read_report_data = json.load(rf)

        read_manifest_sha = hashlib.sha256(read_manifest_bytes).hexdigest()
        if read_manifest_sha != manifest_sha:
            raise ExperimentInvalidError("Re-read manifest SHA mismatch")

        if read_report_data.get("manifest_sha256") != manifest_sha:
            raise ExperimentInvalidError("Re-read report does not contain the correct manifest hash")

        if read_report_data.get("status") != manifest_data["status"]:
            raise ExperimentInvalidError("Re-read report status mismatch")

        if (os.stat(manifest_path).st_mode & 0o777) != 0o444:
            raise ExperimentInvalidError("Manifest is not sealed to 0o444")
        if (os.stat(report_path).st_mode & 0o777) != 0o444:
            raise ExperimentInvalidError("Report is not sealed to 0o444")
    except Exception as e:
        raise ExperimentInvalidError(f"Verification of published terminal pair failed: {e}")

    ctx.terminal_state = "pair_verified"


def _update_operator_report_commit_status(ctx: RunContext, status: str, error_msg: str | None = None) -> None:
    unresolved_report_path = os.path.join(ctx.run_dir, "operator_report.json")
    try:
        st = os.lstat(unresolved_report_path)
        if stat.S_ISLNK(st.st_mode):
            raise ExperimentInvalidError(f"Symlink detected at operator report path: {unresolved_report_path}")
    except FileNotFoundError:
        pass
    except Exception as e:
        raise ExperimentInvalidError(f"Failed to lstat operator report path: {e}")

    report_path = os.path.realpath(unresolved_report_path)
    real_run_dir = os.path.realpath(ctx.run_dir)
    if not is_contained(report_path, real_run_dir):
        raise ExperimentInvalidError(f"Operator report path {report_path} is not contained within run directory {real_run_dir}")

    if not os.path.exists(report_path):
        raise ExperimentInvalidError(f"Operator report missing at {report_path}")

    # 1. Do NOT unseal or modify the existing report in-place.
    try:
        with open(report_path, "r") as f:
            data = json.load(f)
    except Exception as e:
        raise ExperimentInvalidError(f"Failed to read existing operator report: {e}")

    data["pointer_commit_status"] = status
    if error_msg:
        data["pointer_commit_error"] = error_msg
    else:
        data.pop("pointer_commit_error", None)

    # Keep memory in sync
    ctx.operator_report = data

    # 2. Exclusive temporary-file creation
    tmp_path = report_path + ".tmp"
    if os.path.exists(tmp_path):
        try:
            os.remove(tmp_path)
        except Exception:
            pass

    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except Exception as e:
        raise ExperimentInvalidError(f"Failed to create exclusive temporary report file: {e}")

    try:
        serialized = json.dumps(data, indent=2)
        if len(serialized) > 10 * 1024 * 1024:
            raise ValueError("Operator report serialization size exceeds 10MB limit")
        with os.fdopen(fd, 'w') as f:
            f.write(serialized)
            f.flush()
            os.fchmod(f.fileno(), stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
            os.fsync(f.fileno())
    except Exception as e:
        try:
            os.close(fd)
        except Exception:
            pass
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        raise ExperimentInvalidError(f"Failed to serialize and write temporary report: {e}")

    # 4. Atomic replacement
    try:
        os.replace(tmp_path, report_path)
    except Exception as e:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        raise ExperimentInvalidError(f"Failed to atomically replace operator report: {e}")

    # 5. fsync parent directory
    parent_dir = os.path.dirname(report_path)
    dir_fd = None
    try:
        dir_fd = os.open(parent_dir, os.O_RDONLY)
        os.fsync(dir_fd)
    except Exception as e:
        raise ExperimentInvalidError(f"Failed to fsync operator report parent directory: {e}")
    finally:
        if dir_fd is not None:
            try:
                os.close(dir_fd)
            except Exception:
                pass

    # 6. Reread and state verification
    try:
        with open(report_path, "r") as f:
            reread_data = json.load(f)
        if reread_data.get("pointer_commit_status") != status:
            raise ExperimentInvalidError("Re-read check failed: pointer_commit_status mismatch")
        if error_msg and reread_data.get("pointer_commit_error") != error_msg:
            raise ExperimentInvalidError("Re-read check failed: pointer_commit_error mismatch")
        if not error_msg and "pointer_commit_error" in reread_data:
            raise ExperimentInvalidError("Re-read check failed: pointer_commit_error should be absent")
    except Exception as e:
        raise ExperimentInvalidError(f"Verification of updated operator report failed: {e}")


def _write_atomic_report(path: str, data: dict, seal: bool = False) -> None:
    tmp_path = path + ".tmp"
    if os.path.exists(tmp_path):
        try:
            os.remove(tmp_path)
        except Exception:
            pass

    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except Exception as e:
        raise ExperimentInvalidError(f"Failed to open temporary report file: {e}")

    try:
        serialized = json.dumps(data, indent=2)
        if len(serialized) > 10 * 1024 * 1024:
            raise ValueError("Operator report serialization size exceeds 10MB limit")
        with os.fdopen(fd, 'w') as f:
            f.write(serialized)
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        try:
            os.close(fd)
        except Exception:
            pass
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        raise ExperimentInvalidError(f"Failed to serialize and write temporary report: {e}")

    try:
        os.replace(tmp_path, path)
    except Exception as e:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        raise ExperimentInvalidError(f"Failed to atomically replace operator report: {e}")

    # fsync on parent directory descriptor
    parent_dir = os.path.dirname(os.path.realpath(path))
    dir_fd = None
    try:
        dir_fd = os.open(parent_dir, os.O_RDONLY)
        os.fsync(dir_fd)
    except Exception as e:
        raise ExperimentInvalidError(f"Failed to fsync operator report parent directory: {e}")
    finally:
        if dir_fd is not None:
            try:
                os.close(dir_fd)
            except Exception:
                pass
                pass

    if seal:
        try:
            os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
        except Exception as e:
            raise ExperimentInvalidError(f"Failed to seal operator report: {e}")


def _write_atomic_run_prompt(path: str, prompt: str) -> None:
    tmp_path = path + ".tmp"
    if os.path.exists(tmp_path):
        try:
            os.remove(tmp_path)
        except Exception:
            pass

    try:
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except Exception as e:
        raise ExperimentInvalidError(f"Failed to open temporary prompt file: {e}")

    try:
        with os.fdopen(fd, 'w') as f:
            f.write(prompt)
            f.flush()
            os.fsync(f.fileno())
    except Exception as e:
        try:
            os.close(fd)
        except Exception:
            pass
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        raise ExperimentInvalidError(f"Failed to write temporary prompt file: {e}")

    try:
        os.replace(tmp_path, path)
    except Exception as e:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        raise ExperimentInvalidError(f"Failed to atomically replace prompt file: {e}")

    # fsync on parent directory descriptor
    parent_dir = os.path.dirname(os.path.realpath(path))
    dir_fd = None
    try:
        dir_fd = os.open(parent_dir, os.O_RDONLY)
        os.fsync(dir_fd)
    except Exception as e:
        raise ExperimentInvalidError(f"Failed to fsync prompt parent directory: {e}")
    finally:
        if dir_fd is not None:
            try:
                os.close(dir_fd)
            except Exception:
                pass

    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    except Exception as e:
        raise ExperimentInvalidError(f"Failed to seal prompt file: {e}")


def _filter_benchmarks(benchmarks: dict) -> dict:
    if not isinstance(benchmarks, dict):
        return {}
    res = {}
    for task_name, task_data in benchmarks.items():
        if isinstance(task_data, dict):
            allowed_fields = ["success", "tokens_consumed", "score", "status", "solution_path", "solution_hash"]
            filtered_task = {}
            for k in allowed_fields:
                if k in task_data:
                    filtered_task[k] = task_data[k]
            res[task_name] = filtered_task
        else:
            raise ExperimentInvalidError(f"Unexpected benchmark data shape: expected dict, got {type(task_data).__name__}")
    return res


def sync_context_to_report(ctx: RunContext) -> None:
    if not ctx.operator_report:
        ctx.operator_report = {}

    lock_contents = ctx.operator_report.setdefault("lock_contents", {})
    if ctx.approved_commit_sha is not None:
        lock_contents["approved_commit_sha"] = ctx.approved_commit_sha
    elif lock_contents.get("approved_commit_sha") is None:
        lock_contents["approved_commit_sha"] = "unknown"

    if ctx.lock_remote_url != "unknown":
        lock_contents["remote_url"] = ctx.lock_remote_url
    elif lock_contents.get("remote_url") is None:
        lock_contents["remote_url"] = "unknown"

    if ctx.lock_model is not None:
        lock_contents["model"] = ctx.lock_model
    elif lock_contents.get("model") is None:
        lock_contents["model"] = "unknown"

    if ctx.allow_no_holdout is not None:
        lock_contents["allow_no_holdout"] = ctx.allow_no_holdout
    elif lock_contents.get("allow_no_holdout") is None:
        lock_contents["allow_no_holdout"] = "unknown"

    env = ctx.operator_report.setdefault("environment", {})
    if ctx.sw_vers != "unavailable":
        env["sw_vers"] = ctx.sw_vers
    elif env.get("sw_vers") is None:
        env["sw_vers"] = "unknown"

    env["python_version"] = sys.version

    if ctx.codex_cli_version != "unavailable":
        env["codex_cli_version"] = ctx.codex_cli_version
    elif ctx.codex_version is not None:
        env["codex_cli_version"] = ctx.codex_version
    elif env.get("codex_cli_version") is None:
        env["codex_cli_version"] = "unknown"

    toks = ctx.operator_report.setdefault("tokens", {})
    toks["canary_consumed"] = ctx.canary_tokens_consumed
    toks["cumulative_consumed"] = ctx.consumed_tokens
    toks.setdefault("ceiling", 50000)

    if ctx.preflight_huffman_data is not None:
        ph = ctx.operator_report.setdefault("preflight_huffman", {})
        ph["score"] = ctx.preflight_huffman_data.get("huffman_preflight_score", "unknown")
        ph["success"] = ctx.preflight_huffman_data.get("huffman_preflight_success", "unknown")
        ph["tests_run"] = 5
        ph["tokens"] = ctx.preflight_huffman_data.get("tokens_consumed", "unknown")
        ph["solution_path"] = ctx.preflight_huffman_data.get("solution_path")
        ph["solution_hash"] = ctx.preflight_huffman_data.get("solution_hash")

    if ctx.trial_results is not None:
        filtered_trials = []
        for t in ctx.trial_results:
            brun = t.get("baseline_run", {})
            mutations = t.get("mutations", [])

            filtered_muts = []
            for m in mutations:
                meval = m.get("evaluation_run", {})
                filtered_muts.append({
                    "mutation_index": m.get("mutation_index"),
                    "prompt_hash": m.get("prompt_hash"),
                    "optimizer_tokens_consumed": m.get("optimizer_tokens_consumed"),
                    "evaluation_run": {
                        "benchmarks": _filter_benchmarks(meval.get("benchmarks", {})),
                        "tokens_consumed": meval.get("tokens_consumed")
                    }
                })

            filtered_trials.append({
                "trial": t.get("trial"),
                "baseline_score": t.get("baseline_score"),
                "opt_score": t.get("opt_score"),
                "delta": t.get("delta"),
                "candidate_prompt_hash": t.get("candidate_prompt_hash"),
                "baseline_run": {
                    "benchmarks": _filter_benchmarks(brun.get("benchmarks", {})),
                    "tokens_consumed": brun.get("tokens_consumed")
                },
                "mutations": filtered_muts
            })
        ctx.operator_report["trials"] = filtered_trials

        ctx.operator_report["candidates"] = [
            {
                "trial": t.get("trial"),
                "candidate_prompt_hash": t.get("candidate_prompt_hash"),
                "opt_score": t.get("opt_score"),
                "baseline_score": t.get("baseline_score"),
                "delta": t.get("delta")
            }
            for t in ctx.trial_results
        ]

    if ctx.validation_run_record is not None:
        ctx.operator_report["validation_run"] = {
            "baseline": {
                "score": ctx.validation_run_record.get("baseline", {}).get("score"),
                "benchmarks": _filter_benchmarks(ctx.validation_run_record.get("baseline", {}).get("benchmarks", {})),
                "tokens_consumed": ctx.validation_run_record.get("baseline", {}).get("tokens_consumed")
            },
            "candidates": {
                t_name: {
                    "score": c_data.get("score"),
                    "benchmarks": _filter_benchmarks(c_data.get("benchmarks", {})),
                    "tokens_consumed": c_data.get("tokens_consumed")
                }
                for t_name, c_data in ctx.validation_run_record.get("candidates", {}).items()
            }
        }

    if ctx.holdout_record is not None:
        ho = ctx.operator_report.setdefault("holdout", {})
        ho["executed"] = True
        ho["success"] = ctx.holdout_record.get("result", False)
        ho["tokens"] = ctx.holdout_record.get("usage", {}).get("output_tokens", 0)
        if ctx.holdout_record.get("solution_path") is not None:
            ho["solution_path"] = ctx.holdout_record.get("solution_path")
        if ctx.holdout_record.get("solution_hash") is not None:
            ho["solution_hash"] = ctx.holdout_record.get("solution_hash")
    else:
        ho = ctx.operator_report.setdefault("holdout", {})
        ho["executed"] = "not_run"
        ho["success"] = False
        ho["tokens"] = 0

    if ctx.lock_remote_url != "unknown":
        ctx.operator_report["lock_remote_url"] = ctx.lock_remote_url
    elif ctx.operator_report.get("lock_remote_url") is None:
        ctx.operator_report["lock_remote_url"] = "unknown"

    if ctx.configured_origin_url != "unknown":
        ctx.operator_report["configured_origin_url"] = ctx.configured_origin_url
    elif ctx.operator_report.get("configured_origin_url") is None:
        ctx.operator_report["configured_origin_url"] = "unknown"

    if ctx.local_head_sha != "unknown":
        ctx.operator_report["local_head_sha"] = ctx.local_head_sha
    elif ctx.operator_report.get("local_head_sha") is None:
        ctx.operator_report["local_head_sha"] = "unknown"

    if ctx.remote_main_sha != "unknown":
        ctx.operator_report["remote_main_sha"] = ctx.remote_main_sha
    elif ctx.operator_report.get("remote_main_sha") is None:
        ctx.operator_report["remote_main_sha"] = "unknown"

    model_val = ctx.lock_model or ctx.codex_model
    if model_val is not None:
        ctx.operator_report["model_requested"] = model_val
    elif ctx.operator_report.get("model_requested") is None:
        ctx.operator_report["model_requested"] = "unknown"

    if ctx.model_resolved != "unavailable":
        ctx.operator_report["model_resolved"] = ctx.model_resolved
    elif ctx.operator_report.get("model_resolved") is None:
        ctx.operator_report["model_resolved"] = "unavailable"


def save_operator_report(ctx: RunContext, seal: bool = False) -> None:
    if not ctx.run_dir:
        return
    sync_context_to_report(ctx)
    path = os.path.join(ctx.run_dir, "operator_report.json")
    _write_atomic_report(path, ctx.operator_report, seal=seal)


def log_phase_transition(ctx: RunContext, phase: str, new_state: str, metadata: dict | None = None) -> None:
    if "phase_records" not in ctx.operator_report:
        ctx.operator_report["phase_records"] = []

    summaries = ctx.operator_report.setdefault("phase_summaries", {
        "phase_0_lock_and_git": "not_run",
        "smoke_and_canary": "not_run",
        "huffman_preflight": "not_run",
        "trial_loop": "not_run",
        "validation_loop": "not_run",
        "holdout_evaluation": "not_run"
    })

    previous_state = summaries.get(phase, "not_run")
    allowed = False
    if previous_state == "not_run" and new_state in ("running", "failed"):
        allowed = True
    elif previous_state == "running" and new_state in ("completed", "failed"):
        allowed = True
    elif previous_state == new_state:
        allowed = True

    if not allowed:
        raise ExperimentInvalidError(f"Invalid phase transition for {phase}: {previous_state} -> {new_state}")

    seq = len(ctx.operator_report["phase_records"]) + 1
    record = {
        "sequence": seq,
        "phase": phase,
        "previous_state": previous_state,
        "new_state": new_state,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "metadata": metadata or {}
    }
    ctx.operator_report["phase_records"].append(record)
    summaries[phase] = new_state
    save_operator_report(ctx)


def init_operator_report(ctx: RunContext) -> None:
    ctx.operator_report = {
        "schema_version": "2",
        "run_id": ctx.run_id or "unknown",
        "status": "running",
        "invalidation_reason": "none",
        "lock_contents": {
            "approved_commit_sha": "unknown",
            "remote_url": "unknown",
            "model": "unknown",
            "allow_no_holdout": "unknown"
        },
        "environment": {
            "sw_vers": "unknown",
            "python_version": sys.version,
            "codex_cli_version": "unknown"
        },
        "phase_summaries": {
            "phase_0_lock_and_git": "not_run",
            "smoke_and_canary": "not_run",
            "huffman_preflight": "not_run",
            "trial_loop": "not_run",
            "validation_loop": "not_run",
            "holdout_evaluation": "not_run"
        },
        "phase_records": [],
        "tokens": {
            "canary_consumed": "unknown",
            "cumulative_consumed": 0,
            "ceiling": 50000
        },
        "trials": [],
        "candidates": [],
        "holdout": {
            "executed": "not_run",
            "success": False,
            "tokens": 0
        },
        "model_requested": "unknown",
        "model_resolved": "unknown",
        "manifest_sha256": "unknown",
        "lock_remote_url": "unknown",
        "configured_origin_url": "unknown",
        "local_head_sha": "unknown",
        "remote_main_sha": "unknown"
    }


def _validate_solution_file(run_dir: str, sol_rel: str, sol_hash: str, error_cls=RuntimeError) -> None:
    if not sol_rel or not isinstance(sol_rel, str):
        raise error_cls("solution_path is missing or not a string")
    if not sol_hash or not isinstance(sol_hash, str):
        raise error_cls("solution_hash is missing or not a string")

    # 1. Symlink / path component rejection (check unresolved components from run_dir down using lstat)
    parts = []
    for part in sol_rel.split(os.sep):
        if not part or part == ".":
            continue
        if part == "..":
            raise error_cls(f"Parent directory traversal (..) is not allowed in solution_path: {sol_rel}")
        parts.append(part)

    current_path = os.path.realpath(run_dir)
    for part in parts:
        current_path = os.path.join(current_path, part)
        try:
            st = os.lstat(current_path)
            if stat.S_ISLNK(st.st_mode):
                raise error_cls(f"Symlink detected in solution path: {current_path}")
        except OSError as oe:
            if isinstance(oe, FileNotFoundError):
                raise error_cls(f"Solution file missing: {current_path}")
            raise error_cls(f"Failed to stat component {current_path}: {oe}")

    sol_full_path = os.path.realpath(current_path)

    # 2. Containment validation
    if not is_contained(sol_full_path, run_dir):
        raise error_cls(f"Solution path {sol_full_path} is not contained within run directory {run_dir}")

    # 3. Regular-file type check
    if not os.path.exists(sol_full_path):
        raise error_cls(f"Solution file missing: {sol_full_path}")
    if os.path.islink(sol_full_path) or not os.path.isfile(sol_full_path):
        raise error_cls(f"Solution path is not a regular file: {sol_full_path}")

    # 4. Mode 0o444 validation
    st = os.stat(sol_full_path)
    if (st.st_mode & 0o777) != 0o444:
        raise error_cls(f"Solution file mode is not 0o444: {oct(st.st_mode & 0o777)} for {sol_full_path}")

    # 5. Hash validation
    actual_hash = _sha256_file(sol_full_path)
    if actual_hash != sol_hash:
        raise error_cls(f"Solution hash mismatch for {sol_full_path}: expected {sol_hash}, got {actual_hash}")


def _extract_and_validate_all_solutions(run_dir: str, manifest_data: dict, report_data: dict, error_cls=RuntimeError) -> None:
    def _collect_solutions(container, out_dict):
        if isinstance(container, dict):
            rel = container.get("solution_path")
            hsh = container.get("solution_hash")
            if rel or hsh:
                if not rel or not hsh:
                    raise error_cls("Benchmark result contains incomplete solution_path / solution_hash pair")
                out_dict[rel] = hsh
            for v in container.values():
                _collect_solutions(v, out_dict)
        elif isinstance(container, list):
            for item in container:
                _collect_solutions(item, out_dict)

    m_sols = {}
    r_sols = {}
    _collect_solutions(manifest_data, m_sols)
    _collect_solutions(report_data, r_sols)

    for rel, hsh in m_sols.items():
        if rel not in r_sols:
            raise error_cls(f"Solution {rel} in manifest is missing from operator report")
        if r_sols[rel] != hsh:
            raise error_cls(f"Solution {rel} hash mismatch between manifest ({hsh}) and report ({r_sols[rel]})")
        _validate_solution_file(run_dir, rel, hsh, error_cls=error_cls)

    for rel, hsh in r_sols.items():
        if rel not in m_sols:
            raise error_cls(f"Solution {rel} in report is missing from manifest")


def validate_manifest_pointer_and_target(pointer_data: dict, allow_pending: bool = False) -> dict:
    """
    Validates a pointer dict and its target manifest.
    Raises RuntimeError/ExperimentInvalidError if validation fails.
    Returns the loaded target manifest dict.
    """
    if pointer_data.get("pointer_schema_version") != "2":
        raise RuntimeError(f"Unknown or legacy pointer schema version: {pointer_data.get('pointer_schema_version')}")

    target_path = pointer_data.get("run_manifest_path")
    if not target_path:
        raise RuntimeError("run_manifest_path missing from pointer")

    full_path = os.path.realpath(os.path.join(BASE_DIR, target_path))
    if not is_contained(full_path, os.path.realpath(HISTORY_DIR)):
        raise RuntimeError(f"Pointer target {full_path} is not contained within HISTORY_DIR")
    if os.path.basename(full_path) != "run_manifest.json":
        raise RuntimeError(f"Pointer target filename {os.path.basename(full_path)} is not run_manifest.json")

    if not os.path.exists(full_path):
        raise RuntimeError(f"Target run manifest missing: {full_path}")

    expected_hash = pointer_data.get("run_manifest_sha256")
    actual_hash = _sha256_file(full_path)
    if actual_hash != expected_hash:
        raise RuntimeError(f"Manifest hash mismatch: expected {expected_hash}, got {actual_hash}")

    with open(full_path) as f:
        target_manifest = json.load(f)

    if target_manifest.get("schema_version") != "2":
        raise RuntimeError(f"Invalid target manifest schema version: {target_manifest.get('schema_version')}")

    status = target_manifest.get("status")
    if status not in ["no_headroom", "completed_no_improvement", "completed_promoted"]:
        raise RuntimeError(f"Invalid target manifest status: {status}")

    # Check for matching operator report
    report_path = os.path.join(os.path.dirname(full_path), "operator_report.json")
    if not os.path.exists(report_path):
        raise RuntimeError(f"Target operator report missing: {report_path}")

    # Symlink and mode verification
    try:
        rst = os.lstat(report_path)
        if stat.S_ISLNK(rst.st_mode):
            raise RuntimeError(f"Symlink detected at operator report path: {report_path}")
        if (rst.st_mode & 0o777) != 0o444:
            raise RuntimeError(f"Operator report file mode is not 0o444: {oct(rst.st_mode & 0o777)}")
    except OSError as oe:
        raise RuntimeError(f"Failed to stat operator report: {oe}")

    try:
        with open(report_path, "r") as rf:
            report_data = json.load(rf)
    except Exception as ree:
        raise RuntimeError(f"Failed to read operator report: {ree}")

    if report_data.get("schema_version") != "2":
        raise RuntimeError(f"Invalid operator report schema version: expected '2', got {report_data.get('schema_version')}")

    if report_data.get("manifest_sha256") != actual_hash:
        raise RuntimeError(f"Operator report manifest hash mismatch: expected {actual_hash}, got {report_data.get('manifest_sha256')}")

    if report_data.get("status") != status:
        raise RuntimeError(f"Operator report status mismatch: expected {status}, got {report_data.get('status')}")

    commit_status = report_data.get("pointer_commit_status")
    allowed_statuses = ["committed", "indeterminate", "pending"] if allow_pending else ["committed", "indeterminate"]
    if commit_status not in allowed_statuses:
        raise RuntimeError(f"Invalid pointer commit status in operator report: {commit_status}")

    # Validate selected prompt (required for every target manifest of the active pointer)
    selected_prompt_path = target_manifest.get("selected_prompt_path")
    selected_prompt_hash = target_manifest.get("selected_prompt_hash")

    if not selected_prompt_path or not selected_prompt_hash:
        raise RuntimeError("selected_prompt_path or selected_prompt_hash missing from target manifest")

    run_dir = os.path.dirname(full_path)

    # 1. Symlink Rejection (check unresolved components from run_dir down using lstat)
    parts = []
    for part in selected_prompt_path.split(os.sep):
        if not part or part == ".":
            continue
        if part == "..":
            raise RuntimeError("Parent directory traversal (..) is not allowed in selected_prompt_path")
        parts.append(part)

    current_path = os.path.realpath(run_dir)
    for part in parts:
        current_path = os.path.join(current_path, part)
        try:
            st = os.lstat(current_path)
            if stat.S_ISLNK(st.st_mode):
                raise RuntimeError(f"Symlink detected in selected prompt path: {current_path}")
        except OSError as oe:
            if isinstance(oe, FileNotFoundError):
                raise RuntimeError(f"Selected prompt file missing: {current_path}")
            raise RuntimeError(f"Failed to stat component {current_path}: {oe}")

    prompt_full_path = os.path.realpath(current_path)

    # 2. Containment validation
    if not is_contained(prompt_full_path, run_dir):
        raise RuntimeError(f"Selected prompt path {prompt_full_path} is not contained within run directory {run_dir}")

    # 3. Regular-file type
    if not os.path.exists(prompt_full_path):
        raise RuntimeError(f"Selected prompt file missing: {prompt_full_path}")
    if os.path.islink(prompt_full_path) or not os.path.isfile(prompt_full_path):
        raise RuntimeError(f"Selected prompt path is not a regular file: {prompt_full_path}")

    # 4. Mode 0o444 validation
    st = os.stat(prompt_full_path)
    if (st.st_mode & 0o777) != 0o444:
        raise RuntimeError(f"Selected prompt file mode is not 0o444: {oct(st.st_mode & 0o777)}")

    # 5. Hash validation
    actual_prompt_hash = _sha256_file(prompt_full_path)
    if actual_prompt_hash != selected_prompt_hash:
        raise RuntimeError(f"Selected prompt hash mismatch: expected {selected_prompt_hash}, got {actual_prompt_hash}")

    # 6. Solution file validation across all benchmarks (preflight, trials, candidates)
    _extract_and_validate_all_solutions(run_dir, target_manifest, report_data, RuntimeError)

    return target_manifest

def load_and_verify_best_manifest() -> dict | None:
    if not os.path.exists(BEST_MANIFEST_PATH):
        return None
    try:
        with open(BEST_MANIFEST_PATH) as f:
            ptr = json.load(f)
        return validate_manifest_pointer_and_target(ptr)
    except Exception as e:
        sys.stderr.write(f"WARNING: load_and_verify_best_manifest error: {e}\n")
        return None

def verify_existing_best_manifest_startup() -> None:
    if not os.path.exists(BEST_MANIFEST_PATH):
        return
    try:
        st = os.lstat(BEST_MANIFEST_PATH)
        if stat.S_ISLNK(st.st_mode):
            raise ExperimentInvalidError(f"Symlink detected at pointer path: {BEST_MANIFEST_PATH}")
        if (st.st_mode & 0o777) != 0o444:
            raise ExperimentInvalidError(f"Authoritative pointer file mode is not 0o444: {oct(st.st_mode & 0o777)}")
        with open(BEST_MANIFEST_PATH) as f:
            ptr = json.load(f)
        validate_manifest_pointer_and_target(ptr)
    except Exception as e:
        import shutil
        legacy_dir = os.path.join(HISTORY_DIR, "legacy")
        backup_name = f"best_manifest.json.archive_{uuid.uuid4().hex}.backup"
        backup_path = os.path.join(legacy_dir, backup_name)

        # Check unresolved path components of the destination for symlinks
        curr = backup_path
        real_base = os.path.realpath(BASE_DIR)
        while curr:
            if curr == BASE_DIR or os.path.realpath(curr) == real_base:
                break
            if os.path.islink(curr):
                raise ExperimentInvalidError(f"Symlink detected in archive path component: {curr}")
            parent = os.path.dirname(curr)
            if parent == curr:
                break
            curr = parent

        # Check containment beneath HISTORY_DIR before moving
        if not is_contained(backup_path, HISTORY_DIR):
            raise ExperimentInvalidError(f"Archive destination {backup_path} is not contained within HISTORY_DIR")

        os.makedirs(legacy_dir, exist_ok=True)
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
    if os.path.exists(tmp):
        try:
            os.remove(tmp)
        except Exception:
            pass

    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except Exception as e:
        raise ExperimentInvalidError(f"Failed to open temporary pointer file: {e}")

    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(pointer_data, f, indent=2)
            f.flush()
            os.fchmod(f.fileno(), 0o444)
            os.fsync(f.fileno())
    except Exception as e:
        try:
            os.close(fd)
        except Exception:
            pass
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass
        raise ExperimentInvalidError(f"Failed to write temporary pointer file: {e}")

    try:
        os.replace(tmp, BEST_MANIFEST_PATH)
    except Exception as e:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass
        raise ExperimentInvalidError(f"Failed to atomically replace pointer file: {e}")

    try:
        st = os.lstat(BEST_MANIFEST_PATH)
        if stat.S_ISLNK(st.st_mode):
            raise ExperimentInvalidError(f"Symlink detected at pointer path: {BEST_MANIFEST_PATH}")
        if (st.st_mode & 0o777) != 0o444:
            raise ExperimentInvalidError(f"Authoritative pointer file mode is not 0o444: {oct(st.st_mode & 0o777)}")
    except Exception as e:
        raise ExperimentInvalidError(f"Failed to verify authoritative pointer mode: {e}")

    # fsync on parent directory descriptor of BEST_MANIFEST_PATH
    parent_dir = os.path.dirname(os.path.realpath(BEST_MANIFEST_PATH))
    dir_fd = None
    try:
        dir_fd = os.open(parent_dir, os.O_RDONLY)
        os.fsync(dir_fd)
    except Exception as e:
        raise PointerCommitIndeterminateError(f"Failed to fsync pointer parent directory: {e}")
    finally:
        if dir_fd is not None:
            try:
                os.close(dir_fd)
            except Exception:
                pass

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
    ctx.approved_commit_sha = approved_sha

    remote_url = lock_data.get("remote_url")
    if not remote_url:
        raise ExperimentInvalidError("remote_url missing from experiment.lock")
    ctx.lock_remote_url = remote_url

    model = lock_data.get("model")
    if not isinstance(model, str) or not _EXPLICIT_MODEL_RE.fullmatch(model) or model == "resolve-at-canary":
        raise ExperimentInvalidError(
            "model in experiment.lock must be a valid explicit model name"
        )
    ctx.lock_model = model
    ctx.codex_model = model

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

    # C3: Local remote URL check (byte-for-byte matching of configured URL vs lock URL)
    try:
        res_url = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True, text=True, cwd=BASE_DIR, check=True
        )
        configured_origin = res_url.stdout.strip()
    except Exception as e:
        raise ExperimentInvalidError(f"Failed to get configured git remote URL: {e}")
    ctx.configured_origin_url = configured_origin

    if configured_origin != remote_url:
        raise ExperimentInvalidError(
            f"Git origin URL mismatch: configured remote origin is {configured_origin!r}, "
            f"but lock file requires {remote_url!r}"
        )

    # C3: Query git ls-remote directly
    try:
        env_git = os.environ.copy()
        env_git["GIT_TERMINAL_PROMPT"] = "0"
        res_ls = subprocess.run(
            ["git", "ls-remote", "--refs", "--exit-code", remote_url, "refs/heads/main"],
            capture_output=True, text=True, cwd=BASE_DIR, check=True, timeout=15, env=env_git
        )
        ls_output = res_ls.stdout
    except Exception as e:
        raise ExperimentInvalidError(f"Failed to verify main branch ref via git ls-remote: {e}")

    ls_lines = ls_output.split('\n')
    is_valid_lines = (
        (len(ls_lines) == 2 and ls_lines[1] == "") or
        (len(ls_lines) == 1 and ls_lines[0] != "")
    )
    if not is_valid_lines:
        raise ExperimentInvalidError("git ls-remote returned invalid number of lines or empty records")

    ls_line = ls_lines[0]
    if ls_line.endswith('\r'):
        ls_line = ls_line[:-1]

    import re
    if not re.match(r"^[0-9a-f]{40}\trefs/heads/main$", ls_line):
        raise ExperimentInvalidError(f"git ls-remote output format mismatch or invalid whitespace: {ls_output!r}")

    match = re.match(r"^([0-9a-f]{40})\trefs/heads/main$", ls_line)
    remote_sha = match.group(1)
    ctx.remote_main_sha = remote_sha

    try:
        res_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=BASE_DIR, check=True
        )
        current_sha = res_head.stdout.strip()
    except Exception as e:
        raise ExperimentInvalidError(f"Failed to get git HEAD: {e}")
    ctx.local_head_sha = current_sha

    # Strict consistency verification
    if remote_sha != approved_sha:
        raise ExperimentInvalidError(
            f"Remote main SHA mismatch: remote is {remote_sha}, but approved commit SHA is {approved_sha}"
        )
    if current_sha != approved_sha:
        raise ExperimentInvalidError(
            f"Git HEAD mismatch: local HEAD is {current_sha}, but approved commit SHA is {approved_sha}"
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
            f.flush()
            os.fchmod(f.fileno(), 0o444)
            os.fsync(f.fileno())

        eval_res = run_evaluation_on_candidate(temp_gen_dir, harness_file)
        solution_hash = hashlib.sha256(solution_code.encode()).hexdigest()

        run_holdout_dir = os.path.realpath(os.path.join(run_dir, "holdout_evaluation"))
        if not is_contained(run_holdout_dir, run_dir):
            raise ExperimentInvalidError("Path traversal in holdout evaluation run directory")
        os.makedirs(run_holdout_dir, exist_ok=True)

        dest_holdout_solution = os.path.join(run_holdout_dir, "solution.py")
        with open(dest_holdout_solution, "w") as f:
            f.write(solution_code)
            f.flush()
            os.fchmod(f.fileno(), 0o444)
            os.fsync(f.fileno())

        sol_rel_path = os.path.relpath(os.path.realpath(dest_holdout_solution), os.path.realpath(run_dir))

        holdout_manifest_path = os.path.join(run_holdout_dir, "holdout_manifest.json")
        holdout_record = {
            "problem_hash": problem_hash,
            "harness_hash": harness_hash,
            "solution_path": sol_rel_path,
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
    if not ctx.run_dir:
        raise ExperimentInvalidError("ctx.run_dir is not set")
    real_run_dir = os.path.realpath(ctx.run_dir)
    real_history_dir = os.path.realpath(HISTORY_DIR)
    if os.path.commonpath([real_run_dir, real_history_dir]) != real_history_dir:
        raise ExperimentInvalidError("ctx.run_dir is not inside HISTORY_DIR")
    stat_run = os.lstat(ctx.run_dir)
    import stat as stat_mod
    if stat_mod.S_ISLNK(stat_run.st_mode):
        raise ExperimentInvalidError("ctx.run_dir is a symlink")
    if not stat_mod.S_ISDIR(stat_run.st_mode):
        raise ExperimentInvalidError("ctx.run_dir is not a directory")
    if stat_run.st_uid != os.getuid():
        raise ExperimentInvalidError("ctx.run_dir is not owned by current user")

    call_run_id = ctx.run_id or f"adhoc_{uuid.uuid4().hex}"
    real_temp_home = _build_codex_run_scoped_home(ctx, call_run_id)
    ctx.created_codex_home = real_temp_home

    primary_error = None
    canary_cleanup_error = None
    home_cleanup_error = None

    try:
        canary_candidate_dir = os.path.realpath(os.path.join(ctx.run_dir, "canary_candidate"))
        try:
            os.mkdir(canary_candidate_dir, 0o700)
        except FileExistsError:
            raise ExperimentInvalidError(f"Canary candidate directory already exists: {canary_candidate_dir}")

        ctx.created_canary_dir = canary_candidate_dir

        _validate_canary_dir(canary_candidate_dir, ctx.run_dir, real_temp_home)
        ctx.canary_candidate_relpath = os.path.relpath(canary_candidate_dir, BASE_DIR)

        sb_path = _build_generator_sb(real_temp_home, canary_candidate_dir)
        allowed_env = _codex_environment(real_temp_home)
        version = _run_codex_version_smoke_test(sb_path, allowed_env, canary_candidate_dir)
        ctx.codex_version = version
        ctx.codex_cli_version = version
        canary_tokens = _run_codex_canary(sb_path, allowed_env, ctx, canary_candidate_dir)
        return version, canary_tokens
    except BaseException as error:
        primary_error = error
        raise
    finally:
        if ctx.created_canary_dir and os.path.lexists(ctx.created_canary_dir):
            try:
                _cleanup_canary_dir(ctx.created_canary_dir, ctx.run_dir, real_temp_home)
            except Exception as ce:
                canary_cleanup_error = ce

        try:
            _cleanup_after_call(ctx, real_temp_home, primary_error)
        except Exception as he:
            home_cleanup_error = he

        if canary_cleanup_error or home_cleanup_error:
            msgs = []
            if canary_cleanup_error:
                msgs.append(f"Canary cleanup failed: {canary_cleanup_error}")
            if home_cleanup_error:
                msgs.append(f"Home cleanup failed: {home_cleanup_error}")
            raise ExperimentInvalidError(
                f"Cleanup failure: {'; '.join(msgs)}",
                unresolved_codex_home=getattr(home_cleanup_error, "unresolved_codex_home", real_temp_home),
                cleanup_error=f"canary_err={canary_cleanup_error}, home_err={getattr(home_cleanup_error, 'cleanup_error', home_cleanup_error)}"
            ) from (primary_error or canary_cleanup_error or home_cleanup_error)

# ── Main execution entry point ────────────────────────────────────────────────

def _main_exec(ctx: RunContext) -> None:
    ctx.current_stage = "preflight_verifications"
    log_phase_transition(ctx, "phase_0_lock_and_git", "running")
    approved_sha = verify_experiment_lock(ctx)
    ctx.approved_commit_sha = approved_sha
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

    recover_pending_runs()
    verify_existing_best_manifest_startup()

    if not os.path.exists(BASELINE_PROMPT_PATH):
        raise ExperimentInvalidError(f"Baseline prompt not found at {BASELINE_PROMPT_PATH}")
    with open(BASELINE_PROMPT_PATH) as f:
        baseline_prompt = f.read()
    baseline_hash = hashlib.sha256(baseline_prompt.encode()).hexdigest()
    sys.stdout.write(f"Baseline hash: {baseline_hash}\n")

    log_phase_transition(ctx, "phase_0_lock_and_git", "completed")

    ctx.current_stage = "smoke_and_canary"
    log_phase_transition(ctx, "smoke_and_canary", "running")
    sys.stdout.write("Running smoke test and canary...\n")
    sys.stdout.flush()
    codex_version, canary_tokens = run_smoke_and_canary(ctx)
    ctx.canary_tokens_consumed = canary_tokens
    log_phase_transition(ctx, "smoke_and_canary", "completed", {"tokens": canary_tokens})

    # Isolated Huffman baseline preflight check
    ctx.current_stage = "huffman_preflight"
    log_phase_transition(ctx, "huffman_preflight", "running")
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
        f.flush()
        os.fchmod(f.fileno(), 0o444)
        os.fsync(f.fileno())

    huffman_harness = os.path.join(BENCHMARKS_DIR, "huffman_coding", "test_harness.py")
    eval_res = run_evaluation_on_candidate(huffman_iter_dir, huffman_harness)

    huffman_log = eval_res.get("log", "")
    huffman_success = eval_res.get("success", False)

    # Verify testsRun == 5
    tests_run_match = re.search(r"Ran (\d+) tests", huffman_log)
    if not tests_run_match or int(tests_run_match.group(1)) != 5:
        raise ExperimentInvalidError(f"Huffman preflight failed: expected 5 tests to run, log output:\n{huffman_log}")

    solution_rel_path = os.path.relpath(solution_file, ctx.run_dir)
    preflight_huffman_path = os.path.join(ctx.run_dir, "preflight_huffman.json")
    preflight_huffman_data = {
        "huffman_preflight_score": 1.0 if huffman_success else 0.0,
        "huffman_preflight_success": huffman_success,
        "huffman_log": huffman_log,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "solution_path": solution_rel_path,
        "solution_hash": hashlib.sha256(huffman_code.encode()).hexdigest(),
        "tokens_consumed": huff_tokens,
    }
    _write_once_fsynced_manifest(preflight_huffman_path, preflight_huffman_data)
    ctx.preflight_huffman_data = preflight_huffman_data

    if huffman_success:
        log_phase_transition(ctx, "huffman_preflight", "completed", {
            "score": 1.0,
            "success": True,
            "tokens": huff_tokens
        })
        sys.stdout.write("Huffman preflight scored 5/5 (no headroom). Trials never invoked.\n")
        no_headroom_manifest_path = os.path.join(ctx.run_dir, "run_manifest.json")
        no_headroom_manifest = {
            "schema_version": "2",
            "run_id": ctx.run_id,
            **_terminal_environment_metadata(ctx),
            "approved_commit_sha": approved_sha,
            "lock_file_hash": ctx.lock_file_hash,
            "status": "no_headroom",
            "baseline_hash": baseline_hash,
            "codex_version": ctx.codex_version,
            **_model_manifest_fields(ctx),
            "canary_tokens_consumed": canary_tokens,
            "preflight_hashes": preflight_hashes_global(),
            "preflight_huffman": preflight_huffman_data,
            "token_ceiling": TOKEN_CEILING,
            "tokens_consumed": ctx.consumed_tokens,
            "unknown_token_state": ctx.unknown_token_state,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        promote = (not os.path.exists(BEST_MANIFEST_PATH))
        if promote:
            run_best_prompt_path = os.path.join(ctx.run_dir, "inner_agent_best.txt")
            _write_atomic_run_prompt(run_best_prompt_path, baseline_prompt)
            no_headroom_manifest["selected_prompt_path"] = "inner_agent_best.txt"
            no_headroom_manifest["selected_prompt_hash"] = hashlib.sha256(baseline_prompt.encode('utf-8')).hexdigest()

        publish_terminal_manifest_and_report(ctx, no_headroom_manifest_path, no_headroom_manifest, pointer_commit_status="pending" if promote else "not_applicable")

        if promote:
            try:
                update_best_manifest_pointer(no_headroom_manifest_path)
            except PointerCommitIndeterminateError as pcie:
                try:
                    _update_operator_report_commit_status(ctx, "indeterminate", str(pcie))
                except Exception:
                    pass
                raise
            except Exception as e:
                deleted_all = True
                for p in [run_best_prompt_path, no_headroom_manifest_path, os.path.join(ctx.run_dir, "operator_report.json")]:
                    if os.path.exists(p):
                        try:
                            os.chmod(p, 0o600)
                            os.remove(p)
                        except Exception as de:
                            deleted_all = False
                            sys.stderr.write(f"WARNING: rollback cleanup failed for {p}: {de}\n")
                if deleted_all:
                    ctx.terminal_state = "not_started"
                    raise e
                else:
                    raise ExperimentInvalidError(f"Pre-commit failure occurred, and rollback cleanup failed to remove all promotion artifacts: {e}")

            try:
                _update_operator_report_commit_status(ctx, "committed")
            except PointerCommitIndeterminateError as pcie:
                try:
                    _update_operator_report_commit_status(ctx, "indeterminate", str(pcie))
                except Exception:
                    pass
                raise
            except Exception as e:
                sys.stderr.write(f"WARNING: Post-commit report status update failed: {e}\n")
                raise
        return
    else:
        log_phase_transition(ctx, "huffman_preflight", "completed", {
            "score": 0.0,
            "success": False,
            "tokens": huff_tokens
        })

    # Trial Execution
    ctx.current_stage = "trials"
    log_phase_transition(ctx, "trial_loop", "running")
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
    ctx.trial_results = trial_results
    log_phase_transition(ctx, "trial_loop", "completed", {"trials": len(trial_results), "mean_delta": mean_delta})

    # Evaluate baseline validation
    ctx.current_stage = "validation"
    log_phase_transition(ctx, "validation_loop", "running")
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
            "benchmarks": _filter_benchmarks(baseline_val_results),
            "tokens_consumed": val_baseline_tokens
        },
        "candidates": {
            t["trial"]: {
                "score": candidate_val_scores[idx],
                "benchmarks": _filter_benchmarks(candidate_val_results_list[idx]),
                "tokens_consumed": candidate_val_tokens_list[idx]
            }
            for idx, t in enumerate(trial_results)
        }
    }
    ctx.validation_run_record = validation_run_record

    if not gate_passed:
        log_phase_transition(ctx, "validation_loop", "completed", {"baseline_val_score": baseline_val_score, "candidate_val_scores": candidate_val_scores, "gate_passed": False})
        # State: completed_no_improvement
        retention_reason = (
            f"mean_delta={mean_delta:.4f}, "
            f"baseline_val={baseline_val_score}, candidate_vals={candidate_val_scores}"
        )
        run_manifest_path = os.path.join(ctx.run_dir, "run_manifest.json")
        no_improvement_manifest = {
            "schema_version": "2",
            "run_id": ctx.run_id,
            **_terminal_environment_metadata(ctx),
            "approved_commit_sha": approved_sha,
            "lock_file_hash": ctx.lock_file_hash,
            "status": "completed_no_improvement",
            "baseline_hash": baseline_hash,
            "codex_version": ctx.codex_version,
            **_model_manifest_fields(ctx),
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
        promote = (not os.path.exists(BEST_MANIFEST_PATH))
        if promote:
            run_best_prompt_path = os.path.join(ctx.run_dir, "inner_agent_best.txt")
            _write_atomic_run_prompt(run_best_prompt_path, baseline_prompt)
            no_improvement_manifest["selected_prompt_path"] = "inner_agent_best.txt"
            no_improvement_manifest["selected_prompt_hash"] = hashlib.sha256(baseline_prompt.encode('utf-8')).hexdigest()

        publish_terminal_manifest_and_report(ctx, run_manifest_path, no_improvement_manifest, pointer_commit_status="pending" if promote else "not_applicable")

        if promote:
            try:
                update_best_manifest_pointer(run_manifest_path)
            except PointerCommitIndeterminateError as pcie:
                try:
                    _update_operator_report_commit_status(ctx, "indeterminate", str(pcie))
                except Exception:
                    pass
                raise
            except Exception as e:
                deleted_all = True
                for p in [run_best_prompt_path, run_manifest_path, os.path.join(ctx.run_dir, "operator_report.json")]:
                    if os.path.exists(p):
                        try:
                            os.chmod(p, 0o600)
                            os.remove(p)
                        except Exception as de:
                            deleted_all = False
                            sys.stderr.write(f"WARNING: rollback cleanup failed for {p}: {de}\n")
                if deleted_all:
                    ctx.terminal_state = "not_started"
                    raise e
                else:
                    raise ExperimentInvalidError(f"Pre-commit failure occurred, and rollback cleanup failed to remove all promotion artifacts: {e}")

            try:
                _update_operator_report_commit_status(ctx, "committed")
            except PointerCommitIndeterminateError as pcie:
                try:
                    _update_operator_report_commit_status(ctx, "indeterminate", str(pcie))
                except Exception:
                    pass
                raise
            except Exception as e:
                sys.stderr.write(f"WARNING: Post-commit report status update failed: {e}\n")
                raise
        sys.stdout.write("Gate failed. Baseline retained.\n")
        return

    log_phase_transition(ctx, "validation_loop", "completed", {"baseline_val_score": baseline_val_score, "candidate_val_scores": candidate_val_scores, "gate_passed": True})

    # Select best candidate
    eligible.sort(key=lambda x: (-x[1], x[0]))
    best_idx = eligible[0][0]
    best_trial = trial_results[best_idx]
    best_prompt = best_trial["candidate_prompt"]

    ctx.current_stage = "holdout"
    log_phase_transition(ctx, "holdout_evaluation", "running")
    holdout_record, holdout_tokens = run_holdout_evaluation(best_prompt, ctx.run_dir, ctx)
    ctx.holdout_record = holdout_record
    log_phase_transition(ctx, "holdout_evaluation", "completed", {"holdout_success": holdout_record["result"]})

    # State: completed_promoted
    run_manifest_path = os.path.join(ctx.run_dir, "run_manifest.json")
    promoted_manifest = {
        "schema_version": "2",
        "run_id": ctx.run_id,
        **_terminal_environment_metadata(ctx),
        "approved_commit_sha": approved_sha,
        "lock_file_hash": ctx.lock_file_hash,
        "status": "completed_promoted",
        "baseline_hash": baseline_hash,
        "codex_version": ctx.codex_version,
        **_model_manifest_fields(ctx),
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

    run_best_prompt_path = os.path.join(ctx.run_dir, "inner_agent_best.txt")
    _write_atomic_run_prompt(run_best_prompt_path, best_prompt)
    promoted_manifest["selected_prompt_path"] = "inner_agent_best.txt"

    publish_terminal_manifest_and_report(ctx, run_manifest_path, promoted_manifest, pointer_commit_status="pending")
    try:
        update_best_manifest_pointer(run_manifest_path)
    except PointerCommitIndeterminateError as pcie:
        try:
            _update_operator_report_commit_status(ctx, "indeterminate", str(pcie))
        except Exception:
            pass
        raise
    except Exception as e:
        deleted_all = True
        for p in [run_best_prompt_path, run_manifest_path, os.path.join(ctx.run_dir, "operator_report.json")]:
            if os.path.exists(p):
                try:
                    os.chmod(p, 0o600)
                    os.remove(p)
                except Exception as de:
                    deleted_all = False
                    sys.stderr.write(f"WARNING: rollback cleanup failed for {p}: {de}\n")
        if deleted_all:
            ctx.terminal_state = "not_started"
            raise e
        else:
            raise ExperimentInvalidError(f"Pre-commit failure occurred, and rollback cleanup failed to remove all promotion artifacts: {e}")

    try:
        _update_operator_report_commit_status(ctx, "committed")
    except PointerCommitIndeterminateError as pcie:
        try:
            _update_operator_report_commit_status(ctx, "indeterminate", str(pcie))
        except Exception:
            pass
        raise
    except Exception as e:
        sys.stderr.write(f"WARNING: Post-commit report status update failed: {e}\n")
        raise
    sys.stdout.write(f"Promotion gate passed. Manifest promoted to best_manifest.json.\n")

def preflight_hashes_global() -> dict:
    if os.path.exists(PREFLIGHT_HASHES_PATH):
        with open(PREFLIGHT_HASHES_PATH) as f:
            return json.load(f)
    return {}


def _capture_environment_metadata(ctx: RunContext) -> None:
    ctx.python_version = sys.version
    try:
        result = subprocess.run(
            ["/usr/bin/sw_vers"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=BASE_DIR,
        )
        stdout = normalize_and_truncate_stream(result.stdout)
        stderr = normalize_and_truncate_stream(result.stderr)
        if result.returncode == 0 and stdout.strip():
            ctx.sw_vers = stdout.strip()
        else:
            ctx.sw_vers = f"unavailable: {get_stderr_summary(stderr) or 'sw_vers failed'}"
    except (subprocess.SubprocessError, OSError) as error:
        ctx.sw_vers = f"unavailable: {type(error).__name__}"


def _terminal_environment_metadata(ctx: RunContext) -> dict:
    codex_cli_version = (
        ctx.codex_cli_version
        if ctx.codex_cli_version != "unavailable"
        else (ctx.codex_version or "unavailable")
    )
    return {
        "sw_vers": ctx.sw_vers,
        "python_version": ctx.python_version,
        "codex_cli_version": codex_cli_version,
    }


def _model_manifest_fields(ctx: RunContext) -> dict:
    model_val = ctx.lock_model or ctx.codex_model or "unavailable"
    return {
        "model_requested": model_val,
        "model_resolved": "unavailable",
        "model_name": model_val,
    }


KNOWN_HISTORICAL_POINTER_HASH = "d880b66ff7c0587087d09e0df0480847ba5a025b73d857f610b737f959546d3b"

def _is_known_historical_legacy_pointer(pointer_path: str) -> bool:
    try:
        st = os.lstat(pointer_path)
        if stat.S_ISLNK(st.st_mode):
            return False
        return _sha256_file(pointer_path) == KNOWN_HISTORICAL_POINTER_HASH
    except Exception:
        return False

def _read_authoritative_pointer() -> dict | None:
    if not os.path.lexists(BEST_MANIFEST_PATH):
        return None
    try:
        # Check that BEST_MANIFEST_PATH itself is not a symlink (lstat first)
        st = os.lstat(BEST_MANIFEST_PATH)
        if stat.S_ISLNK(st.st_mode):
            raise ExperimentInvalidError(f"Symlink detected at pointer path: {BEST_MANIFEST_PATH}")
        if not os.path.exists(BEST_MANIFEST_PATH):
            raise ExperimentInvalidError(f"Authoritative pointer path exists but cannot be accessed: {BEST_MANIFEST_PATH}")
        with open(BEST_MANIFEST_PATH, "r") as f:
            data = json.load(f)
    except ExperimentInvalidError:
        raise
    except Exception as e:
        if _is_known_historical_legacy_pointer(BEST_MANIFEST_PATH):
            return None
        raise ExperimentInvalidError(f"Authoritative pointer (best_manifest.json) is corrupted, malformed, or unsafe: {e}")

    if not isinstance(data, dict):
        raise ExperimentInvalidError("Authoritative pointer (best_manifest.json) is not a JSON object/dictionary")

    if (st.st_mode & 0o777) != 0o444:
        if _is_known_historical_legacy_pointer(BEST_MANIFEST_PATH):
            return None
        raise ExperimentInvalidError(f"Authoritative pointer file mode is not 0o444: {oct(st.st_mode & 0o777)}")

    if data.get("pointer_schema_version") != "2":
        if _is_known_historical_legacy_pointer(BEST_MANIFEST_PATH):
            return None
        raise ExperimentInvalidError(f"Authoritative pointer (best_manifest.json) schema version mismatch (expected '2', got {data.get('pointer_schema_version')!r})")
    if "run_manifest_path" not in data or "run_manifest_sha256" not in data:
        raise ExperimentInvalidError("Authoritative pointer (best_manifest.json) is incomplete: missing run_manifest_path or run_manifest_sha256")

    # Fully validate the pointer target BEFORE any classification or recovery action
    try:
        validate_manifest_pointer_and_target(data, allow_pending=True)
        return data
    except Exception as e:
        if _is_known_historical_legacy_pointer(BEST_MANIFEST_PATH):
            return None
        raise ExperimentInvalidError(f"Authoritative pointer targets a corrupted, malformed, or missing run manifest/report: {e}")


def _validate_invalid_marker_and_report(run_dir: str) -> bool:
    invalid_path = os.path.join(run_dir, "invalid_manifest.json")
    report_path = os.path.join(run_dir, "operator_report.json")

    # If the invalid manifest does not exist, there's no cleanup validation needed
    if not os.path.exists(invalid_path):
        return False

    try:
        # Check invalid_manifest.json symlink and mode
        ist = os.lstat(invalid_path)
        if stat.S_ISLNK(ist.st_mode):
            raise ExperimentInvalidError(f"Symlink detected at invalid manifest path: {invalid_path}")
        if (ist.st_mode & 0o777) != 0o444:
            raise ExperimentInvalidError(f"Invalid manifest is not sealed to 0o444: {invalid_path}")

        # Check operator_report.json symlink and mode
        if not os.path.exists(report_path):
            raise ExperimentInvalidError(f"Operator report missing for invalid manifest: {report_path}")
        rst = os.lstat(report_path)
        if stat.S_ISLNK(rst.st_mode):
            raise ExperimentInvalidError(f"Symlink detected at operator report path: {report_path}")
        if (rst.st_mode & 0o777) != 0o444:
            raise ExperimentInvalidError(f"Operator report is not sealed to 0o444: {report_path}")

        # Parse invalid_manifest.json
        with open(invalid_path, "r") as f:
            manifest_data = json.load(f)
        if not isinstance(manifest_data, dict):
            raise ExperimentInvalidError(f"Invalid manifest is not a dictionary: {invalid_path}")
        if manifest_data.get("schema_version") != "2" or manifest_data.get("status") != "invalid":
            raise ExperimentInvalidError(f"Invalid manifest schema/status mismatch: {invalid_path}")

        # Parse operator_report.json
        with open(report_path, "r") as f:
            report_data = json.load(f)
        if not isinstance(report_data, dict):
            raise ExperimentInvalidError(f"Operator report is not a dictionary: {report_path}")
        if report_data.get("schema_version") != "2" or report_data.get("status") != "invalid":
            raise ExperimentInvalidError(f"Operator report schema/status mismatch: {report_path}")

        # Verify hash pairing
        with open(invalid_path, "rb") as f:
            manifest_bytes = f.read()
        manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()
        if report_data.get("manifest_sha256") != manifest_sha:
            raise ExperimentInvalidError(f"Hash pairing mismatch between invalid manifest and report: {report_path}")

        return True
    except Exception as e:
        raise ExperimentInvalidError(f"Untrusted or malformed invalid marker pairing: {e}")


def _validate_pending_run_tuple(run_dir: str, entry: str, report_data: dict, manifest_data: dict) -> None:
    # 1. Report mode verification
    report_path = os.path.join(run_dir, "operator_report.json")
    try:
        rst = os.lstat(report_path)
        if stat.S_ISLNK(rst.st_mode) or not stat.S_ISREG(rst.st_mode):
            raise ExperimentInvalidError(f"Operator report is not a regular file or is a symlink: {report_path}")
        if (rst.st_mode & 0o777) != 0o444:
            raise ExperimentInvalidError(f"Operator report file mode is not 0o444: {report_path}")
    except OSError as oe:
        raise ExperimentInvalidError(f"Failed to stat operator report: {oe}")

    # 2. Report schema and structure verification
    if report_data.get("schema_version") != "2":
        raise ExperimentInvalidError(f"Invalid report schema version: {report_data.get('schema_version')}")
    status = report_data.get("status")
    if status not in ["no_headroom", "completed_no_improvement", "completed_promoted"]:
        raise ExperimentInvalidError(f"Invalid report status: {status}")
    if report_data.get("run_id") != entry:
        raise ExperimentInvalidError(f"Report run ID mismatch: expected {entry}, got {report_data.get('run_id')}")

    # 3. Manifest mode verification
    manifest_path = os.path.join(run_dir, "run_manifest.json")
    try:
        mst = os.lstat(manifest_path)
        if stat.S_ISLNK(mst.st_mode) or not stat.S_ISREG(mst.st_mode):
            raise ExperimentInvalidError(f"Run manifest is not a regular file or is a symlink: {manifest_path}")
        if (mst.st_mode & 0o777) != 0o444:
            raise ExperimentInvalidError(f"Run manifest file mode is not 0o444: {manifest_path}")
    except OSError as oe:
        raise ExperimentInvalidError(f"Failed to stat run manifest: {oe}")

    # 4. Manifest schema and pairing verification
    if manifest_data.get("schema_version") != "2":
        raise ExperimentInvalidError(f"Invalid manifest schema version: {manifest_data.get('schema_version')}")
    if manifest_data.get("status") != status:
        raise ExperimentInvalidError(f"Manifest status mismatch: expected {status}, got {manifest_data.get('status')}")
    if manifest_data.get("run_id") != entry:
        raise ExperimentInvalidError(f"Manifest run ID mismatch: expected {entry}, got {manifest_data.get('run_id')}")

    actual_manifest_sha = _sha256_file(manifest_path)
    if report_data.get("manifest_sha256") != actual_manifest_sha:
        raise ExperimentInvalidError(f"Report manifest hash mismatch: expected {actual_manifest_sha}, got {report_data.get('manifest_sha256')}")

    # 5. Prompt authentication
    selected_prompt_path = manifest_data.get("selected_prompt_path")
    selected_prompt_hash = manifest_data.get("selected_prompt_hash")
    if not selected_prompt_path or not selected_prompt_hash:
        raise ExperimentInvalidError("selected_prompt_path or selected_prompt_hash missing from run manifest")

    # Reject parent directory traversals
    parts = []
    for part in selected_prompt_path.split(os.sep):
        if not part or part == ".":
            continue
        if part == "..":
            raise ExperimentInvalidError("Parent directory traversal (..) is not allowed in selected_prompt_path")
        parts.append(part)

    current_path = os.path.realpath(run_dir)
    for part in parts:
        current_path = os.path.join(current_path, part)
        try:
            st = os.lstat(current_path)
            if stat.S_ISLNK(st.st_mode):
                raise ExperimentInvalidError(f"Symlink detected in selected prompt path: {current_path}")
        except OSError as oe:
            raise ExperimentInvalidError(f"Failed to stat component {current_path}: {oe}")

    prompt_full_path = os.path.realpath(current_path)
    if not is_contained(prompt_full_path, run_dir):
        raise ExperimentInvalidError(f"Selected prompt path {prompt_full_path} is not contained within run directory {run_dir}")
    if not os.path.exists(prompt_full_path):
        raise ExperimentInvalidError(f"Selected prompt file missing: {prompt_full_path}")
    if os.path.islink(prompt_full_path) or not os.path.isfile(prompt_full_path):
        raise ExperimentInvalidError(f"Selected prompt path is not a regular file: {prompt_full_path}")

    # Mode 0o444 validation
    pst = os.stat(prompt_full_path)
    if (pst.st_mode & 0o777) != 0o444:
        raise ExperimentInvalidError(f"Selected prompt file mode is not 0o444: {prompt_full_path}")

    # Hash validation
    actual_prompt_hash = _sha256_file(prompt_full_path)
    if actual_prompt_hash != selected_prompt_hash:
        raise ExperimentInvalidError(f"Selected prompt hash mismatch: expected {selected_prompt_hash}, got {actual_prompt_hash}")

    # Solution file validation across all benchmarks (preflight, trials, candidates)
    _extract_and_validate_all_solutions(run_dir, manifest_data, report_data, ExperimentInvalidError)


def recover_pending_runs() -> None:
    """
    Scans the history directory for any run directories that have terminal artifacts
    but whose operator report is still marked as 'pending'.
    Reconciles them atomically according to the Recovery State Decision Table.
    """
    # Pass 1: Read-only classification and validation
    pointer_data = _read_authoritative_pointer()

    if not os.path.exists(HISTORY_DIR):
        return

    # Check if HISTORY_DIR is a symlink
    hst = os.lstat(HISTORY_DIR)
    if stat.S_ISLNK(hst.st_mode):
        raise ExperimentInvalidError(f"Symlink detected at history directory: {HISTORY_DIR}")

    classified_pending_runs = []
    cleanup_marker_runs = []

    for entry in os.listdir(HISTORY_DIR):
        if entry == "legacy":
            continue

        run_dir = os.path.join(HISTORY_DIR, entry)
        try:
            st = os.lstat(run_dir)
            if stat.S_ISLNK(st.st_mode):
                raise ExperimentInvalidError(f"Symlink detected in history directory: {run_dir}")
            if not stat.S_ISDIR(st.st_mode):
                continue
        except OSError as oe:
            raise ExperimentInvalidError(f"Failed to lstat history entry {run_dir}: {oe}")

        # If it doesn't start with run_, it is an unknown non-run directory. We skip it explicitly.
        if not entry.startswith("run_"):
            continue

        # Realpath containment check
        real_run_dir = os.path.realpath(run_dir)
        real_history_dir = os.path.realpath(HISTORY_DIR)
        if not is_contained(real_run_dir, real_history_dir):
            raise ExperimentInvalidError(f"Run directory {real_run_dir} is not contained within HISTORY_DIR")

        report_path = os.path.join(run_dir, "operator_report.json")
        manifest_path = os.path.join(run_dir, "run_manifest.json")
        invalid_path = os.path.join(run_dir, "invalid_manifest.json")
        prompt_path = os.path.join(run_dir, "inner_agent_best.txt")

        # Scan all items inside run_dir to intercept any symlinks or dangling symlinks
        try:
            for item in os.listdir(run_dir):
                item_path = os.path.join(run_dir, item)
                ist = os.lstat(item_path)
                if stat.S_ISLNK(ist.st_mode):
                    raise ExperimentInvalidError(f"Symlink detected in run directory {run_dir}: {item_path}")
                if not os.path.exists(item_path):
                    raise ExperimentInvalidError(f"Dangling symlink or inaccessible file detected in run directory {run_dir}: {item_path}")
        except OSError as oe:
            raise ExperimentInvalidError(f"Failed to scan run directory {run_dir}: {oe}")

        invalid_exists = os.path.lexists(invalid_path)
        report_exists = os.path.lexists(report_path)
        manifest_exists = os.path.lexists(manifest_path)
        prompt_exists = os.path.lexists(prompt_path)

        # Check for invalid marker cleanup validation first
        if invalid_exists:
            has_evidence = False
            if prompt_exists or manifest_exists:
                has_evidence = True
            if has_evidence:
                _validate_invalid_marker_and_report(run_dir)
                cleanup_marker_runs.append((entry, run_dir))

        if report_exists:
            try:
                with open(report_path, "r") as f:
                    report_data = json.load(f)
            except Exception as e:
                raise ExperimentInvalidError(f"Operator report under {run_dir} is corrupted or unreadable: {e}")

            status = report_data.get("pointer_commit_status")
            if status == "pending":
                if not manifest_exists:
                    raise ExperimentInvalidError(f"Run manifest missing for pending run in {run_dir}")

                try:
                    with open(manifest_path, "r") as f:
                        manifest_data = json.load(f)
                    if not isinstance(manifest_data, dict):
                        raise ExperimentInvalidError("Run manifest is not a dictionary")
                except Exception as e:
                    raise ExperimentInvalidError(f"Run manifest under {run_dir} is corrupted or unreadable: {e}")

                # Authenticate the complete pending run tuple
                _validate_pending_run_tuple(run_dir, entry, report_data, manifest_data)

                # Classify commit status
                commit_occurred = False
                if pointer_data:
                    ptr_manifest_path = pointer_data.get("run_manifest_path")
                    ptr_manifest_sha = pointer_data.get("run_manifest_sha256")
                    if ptr_manifest_path and ptr_manifest_sha:
                        resolved_ptr_path = os.path.realpath(os.path.join(BASE_DIR, ptr_manifest_path))
                        expected_manifest_path = os.path.realpath(manifest_path)
                        if resolved_ptr_path == expected_manifest_path:
                            actual_sha = _sha256_file(expected_manifest_path)
                            if actual_sha == ptr_manifest_sha:
                                commit_occurred = True

                if commit_occurred:
                    validate_manifest_pointer_and_target(pointer_data, allow_pending=True)
                    classified_pending_runs.append((entry, run_dir, report_data, manifest_data, True))
                else:
                    classified_pending_runs.append((entry, run_dir, report_data, manifest_data, False))

    # Pass 2: Authorized mutations (Only reached if Pass 1 succeeds with zero errors)
    for entry, run_dir, report_data, manifest_data, commit_occurred in classified_pending_runs:
        manifest_path = os.path.join(run_dir, "run_manifest.json")
        if commit_occurred:
            rec_ctx = RunContext()
            rec_ctx.run_dir = run_dir
            _update_operator_report_commit_status(rec_ctx, "committed")
        else:
            rec_ctx = RunContext()
            rec_ctx.run_id = entry
            rec_ctx.run_dir = run_dir
            rec_ctx.terminal_state = "not_started"
            rec_ctx.consumed_tokens = manifest_data.get("tokens_consumed", 0)
            rec_ctx.unknown_token_state = manifest_data.get("unknown_token_state", False)
            rec_ctx.approved_commit_sha = manifest_data.get("approved_commit_sha", "unknown")
            rec_ctx.lock_file_hash = manifest_data.get("lock_file_hash", "unknown")

            rec_ctx.operator_report = {
                "schema_version": "2",
                "run_id": entry,
                "status": "invalid",
                "lock_contents": {
                    "approved_commit_sha": rec_ctx.approved_commit_sha
                },
                "lock_file_hash": rec_ctx.lock_file_hash,
                "tokens": {
                    "cumulative_consumed": rec_ctx.consumed_tokens
                }
            }

            invalid_manifest_path = os.path.join(run_dir, "invalid_manifest.json")
            invalid_data = {
                "schema_version": "2",
                "run_id": entry,
                "status": "invalid",
                "invalidation_reason": "Interrupted/crashed during pointer commit (left pending)",
                "approved_commit_sha": rec_ctx.approved_commit_sha,
                "lock_file_hash": rec_ctx.lock_file_hash,
                "timestamp": datetime.now(timezone.utc).isoformat()
            }

            # Step 1: publish invalid manifest and report (durable decision)
            publish_terminal_manifest_and_report(rec_ctx, invalid_manifest_path, invalid_data, pointer_commit_status="not_applicable")

            # Step 2: delete original false promotion files (after decision is durable)
            for p in [os.path.join(run_dir, "inner_agent_best.txt"), manifest_path]:
                if os.path.exists(p):
                    try:
                        os.chmod(p, 0o600)
                        os.remove(p)
                    except Exception as de:
                        raise ExperimentInvalidError(f"Failed to delete false promotion artifact {p}: {de}")

    # Idempotent cleanup of residual promotion files for runs that are already invalid
    for entry, run_dir in cleanup_marker_runs:
        for p in [os.path.join(run_dir, "inner_agent_best.txt"), os.path.join(run_dir, "run_manifest.json")]:
            if os.path.exists(p):
                try:
                    os.chmod(p, 0o600)
                    os.remove(p)
                except Exception as de:
                    raise ExperimentInvalidError(f"Failed to delete residual promotion artifact {p}: {de}")


def _run_campaign2(args: argparse.Namespace, ctx: RunContext) -> None:
    """Execute Campaign 2 pilot or full run orchestration."""
    from campaign2.manifest import (
        verify_campaign2_manifest,
        verify_campaign2_evidence_manifest,
        verify_campaign2_parity_matrix,
    )
    from campaign2.tier_assignment import (
        verify_family_blocked_assignment,
        generate_family_blocked_assignment,
    )
    from campaign2.registration import verify_master_registration
    from campaign2.launch_lock import verify_launch_lock, LaunchLockError
    from campaign2.attempt_lifecycle import publish_scientific_start, AttemptLedger
    from campaign2.attempt_identity import create_attempt_identity, verify_attempt_identity
    from campaign2.durability import CheckpointManager
    from campaign2.attempt_lifecycle import AttemptLifecycleError, publish_scientific_start, verify_stage_scientific_start
    from campaign2.model_provider import (
        MockModelProvider,
        MockTaskEvaluator,
        SolModelProvider,
    )
    from campaign2.orchestrator import Campaign2Orchestrator
    from campaign2.ladder import evaluate_pilot_outcome_ladder
    from campaign2.reporting import generate_schema3_report, publish_schema3_report
    from fractions import Fraction

    stage = args.stage
    # P0-2: Hard-refuse full stage without Work Order C implementation
    if stage == "full":
        raise NotImplementedError("Campaign 2 full stage requires Work Order C implementation")

    # 1. Preflight verifications
    verify_campaign2_manifest(BASE_DIR)
    verify_campaign2_evidence_manifest(BASE_DIR)
    verify_campaign2_parity_matrix(BASE_DIR)
    verify_family_blocked_assignment(BASE_DIR)
    reg_data = verify_master_registration(BASE_DIR)

    # Launch lock check
    lock_path = os.path.join(BASE_DIR, "campaign2_launch_lock.json")
    lock_hash = ""
    if not args.mock:
        # MANDATORY launch lock for scientific launch
        lock_data = verify_launch_lock(BASE_DIR)
        with open(lock_path, "rb") as f:
            lock_hash = hashlib.sha256(f.read()).hexdigest()
    else:
        # In mock mode: verify lock if it exists, otherwise note mock testing mode
        if os.path.exists(lock_path):
            lock_data = verify_launch_lock(BASE_DIR)
            with open(lock_path, "rb") as f:
                lock_hash = hashlib.sha256(f.read()).hexdigest()
        else:
            sys.stdout.write("[mock mode] Proceeding without operator launch lock for vertical slice testing.\n")

    ta_data = generate_family_blocked_assignment()
    suite = "suite_p" if stage == "pilot" else "suite_c"
    with open(os.path.join(BASE_DIR, "campaign2_master_registration.json"), "rb") as reg_file:
        registration_sha256 = hashlib.sha256(reg_file.read()).hexdigest()

    # 2. Bind all resume state to one explicit, sealed attempt identity.
    is_resume = getattr(args, "resume", False)
    if is_resume:
        identity = verify_attempt_identity(
            ctx.run_dir,
            expected_attempt_id=ctx.run_id,
            expected_stage=stage,
            expected_mock=args.mock,
            expected_registration_sha256=registration_sha256,
        )
    else:
        identity = create_attempt_identity(
            ctx.run_dir, ctx.run_id, stage, suite, args.mock, registration_sha256
        )

    # 3. Construct ledger and wrap all admission, recovery, provider init, and execution in terminal try block.
    ledger_path = os.path.join(ctx.run_dir, identity["ledger_file"])
    ledger = AttemptLedger(ledger_path)

    try:
        entries = ledger.get_entries()
        already_terminal = any(e.get("record_type") == "terminal_classification" for e in entries)
        if already_terminal:
            term_rec = [e for e in entries if e.get("record_type") == "terminal_classification"][0]
            raise AttemptLifecycleError(
                f"Attempt '{identity['attempt_id']}' is already terminal ({term_rec.get('classification')}) and cannot be resumed."
            )

        existing_starts = [
            e for e in entries
            if e.get("record_type") == "attempt_start" and e.get("attempt_id") == identity["attempt_id"]
        ]
        if not existing_starts:
            ledger.record_attempt_start(identity["attempt_id"], stage)

        ckpt_path = os.path.join(ctx.run_dir, identity["checkpoint_file"])
        ckpt_mgr = CheckpointManager(ckpt_path, stage)
        ledger.verify_resume_admission()

        if args.mock:
            model = MockModelProvider()
            evaluator = MockTaskEvaluator()
        else:
            model = SolModelProvider()
            evaluator = None
            from campaign2.attempt_lifecycle import verify_stage_scientific_start
            if is_resume:
                verify_stage_scientific_start(stage, identity["attempt_id"], BASE_DIR)
            else:
                publish_scientific_start(stage, identity["attempt_id"], BASE_DIR)

        # 5. Instantiate & Run Orchestrator
        orch = Campaign2Orchestrator(
            model_provider=model,
            task_evaluator=evaluator,
            tier_assignment=ta_data,
            registration=reg_data,
            suite=suite,
            ledger=ledger,
            checkpoint_mgr=ckpt_mgr,
            workspace_root=BASE_DIR,
        )

        results = orch.run_all_trials()
        orch.verify_trio_adjacency()
        ledger.verify_resume_admission()
        outcome = orch.compute_outcome()

        opt_sel_baseline = Fraction(outcome["opt_sel_baseline_aggregate"])
        conf_baseline = Fraction(outcome["conf_baseline_aggregate"])
        opt_sel_opt_score = Fraction(outcome["opt_sel_opt_aggregate"])
        opt_sel_ctrl_score = Fraction(outcome["opt_sel_ctrl_aggregate"])
        conf_opt_score = Fraction(outcome["optimizer_winner_aggregate"])
        conf_ctrl_score = Fraction(outcome["control_winner_aggregate"])
        delta_val = Fraction(outcome["delta_opt_minus_ctrl"])
        all_16_opt_sel_perfect = outcome.get("all_16_opt_sel_perfect", False)

        ladder = evaluate_pilot_outcome_ladder(
            opt_sel_baseline=opt_sel_baseline,
            conf_baseline=conf_baseline,
            delta=delta_val,
            all_16_opt_sel_perfect=all_16_opt_sel_perfect,
        )

        # 7. Generate & Publish Schema 3 Report
        report = generate_schema3_report(
            outcome, reg_data, ta_data,
            ledger_path=ledger_path,
            ladder_outcome=ladder.to_dict(),
            launch_lock_hash=lock_hash,
            workspace_root=BASE_DIR,
            is_mock=args.mock,
        )
        report_path = os.path.join(ctx.run_dir, f"campaign2_{stage}_report.json")
        publish_schema3_report(report, report_path)

        ledger.record_terminal_classification(
            identity["attempt_id"],
            "completed",
            f"Ladder decision: {ladder.decision} ({ladder.reason})"
        )
        sys.stdout.write(f"Campaign 2 {stage} execution complete. Ladder decision: {ladder.decision}. Report written to {report_path}\n")
    except Exception as exc:
        is_already_terminal = False
        try:
            cur_entries = ledger.get_entries()
            if any(e.get("record_type") == "terminal_classification" for e in cur_entries):
                is_already_terminal = True
        except Exception:
            pass

        if not is_already_terminal:
            intents = {}
            completions = {}
            try:
                cur_entries = ledger.get_entries()
            except Exception:
                cur_entries = []
            for entry in cur_entries:
                cid = entry.get("call_id")
                if cid:
                    if entry.get("record_type") == "intent":
                        intents[cid] = intents.get(cid, 0) + 1
                    elif entry.get("record_type") == "completion":
                        completions[cid] = completions.get(cid, 0) + 1
            unmatched_count = sum(max(0, intents[cid] - completions.get(cid, 0)) for cid in intents)
            if unmatched_count > 0:
                ledger.unknown_usage_count += unmatched_count

            ledger.record_terminal_classification(
                identity["attempt_id"],
                "invalidated",
                f"Terminal execution failure: {exc}"
            )
        raise


def build_arg_parser() -> argparse.ArgumentParser:
    """Build the real CLI parser so parser behavior is directly testable."""
    parser = argparse.ArgumentParser(description="RSI Loop Orchestrator")
    parser.add_argument("--campaign", type=int, default=1, choices=[1, 2], help="Campaign number (1 or 2)")
    parser.add_argument("--stage", type=str, default="pilot", choices=["pilot", "full"], help="Stage for Campaign 2 (pilot or full)")
    parser.add_argument("--mock", action="store_true", help="Use mock model provider and evaluator for verification")
    parser.add_argument("--resume", action="store_true", help="Resume an in-progress or interrupted campaign run")
    parser.add_argument(
        "--resume-attempt-id",
        default="",
        help="Exact sealed run_<id> to resume; required with --resume",
    )
    return parser


def main() -> None:
    parser = build_arg_parser()
    args, unknown = parser.parse_known_args()

    if args.resume and args.campaign != 2:
        parser.error("--resume is supported only for --campaign 2")
    if args.resume and not args.resume_attempt_id:
        parser.error("--resume requires --resume-attempt-id")
    if args.resume_attempt_id and not args.resume:
        parser.error("--resume-attempt-id requires --resume")

    ctx = RunContext()
    _capture_environment_metadata(ctx)
    if args.resume:
        from campaign2.attempt_identity import resolve_resume_attempt
        try:
            ctx.run_dir, _identity = resolve_resume_attempt(
                HISTORY_DIR, args.resume_attempt_id, args.stage, args.mock
            )
            ctx.run_id = args.resume_attempt_id
            operator_report_path = os.path.join(ctx.run_dir, "operator_report.json")
            with open(operator_report_path, "r", encoding="utf-8") as report_file:
                ctx.operator_report = json.load(report_file)
            if ctx.operator_report.get("run_id") != ctx.run_id:
                raise ValueError("operator report does not match sealed resume attempt")
        except Exception as e:
            sys.stderr.write(f"FATAL: resume admission failed: {e}\n")
            sys.exit(1)
    else:
        ctx.run_id = f"run_{int(time.time())}_{uuid.uuid4().hex[:8]}"
        ctx.run_dir = os.path.realpath(os.path.join(HISTORY_DIR, ctx.run_id))
        try:
            os.makedirs(ctx.run_dir, exist_ok=False)
        except Exception as e:
            sys.stderr.write(f"FATAL: failed to create run directory: {e}\n")
            sys.exit(1)

        init_operator_report(ctx)
        try:
            save_operator_report(ctx)
        except Exception as e:
            sys.stderr.write(f"FATAL: failed to write initial operator report: {e}\n")
            sys.exit(1)

    try:
        if args.campaign == 2:
            _run_campaign2(args, ctx)
        else:
            _main_exec(ctx)
        sys.exit(0)
    except Exception as e:
        sys.stderr.write(f"FATAL: Experiment invalidated: {e}\n")

        if ctx.terminal_state != "not_started":
            sys.stderr.write("Aborting: terminal publication already occurred, no competing invalid manifest will be written.\n")
            sys.exit(1)

        ctx.operator_report["status"] = "invalid"
        ctx.operator_report["invalidation_reason"] = f"{type(e).__name__}: {e}"
        phase_map = {
            "preflight_verifications": "phase_0_lock_and_git",
            "smoke_and_canary": "smoke_and_canary",
            "huffman_preflight": "huffman_preflight",
            "trials": "trial_loop",
            "validation": "validation_loop",
            "holdout": "holdout_evaluation"
        }
        active_phase = phase_map.get(ctx.current_stage)
        if active_phase:
            try:
                log_phase_transition(ctx, active_phase, "failed", {"error": f"{type(e).__name__}: {e}"})
            except Exception:
                pass

        invalid_manifest_path = os.path.join(ctx.run_dir, "invalid_manifest.json")
        invalid_data = {
            "schema_version": "2",
            "run_id": ctx.run_id or "unknown",
            "status": "invalid",
            **_terminal_environment_metadata(ctx),
            "approved_commit_sha": ctx.approved_commit_sha or "unknown",
            "lock_file_hash": ctx.lock_file_hash or "unknown",
            "codex_version": ctx.codex_version,
            **_model_manifest_fields(ctx),
            "error_message": f"{type(e).__name__}: {e}",
            "error_stdout": normalize_and_truncate_stream(getattr(e, "stdout", None)),
            "error_stderr": normalize_and_truncate_stream(getattr(e, "stderr", None)),
            "stderr_summary": getattr(
                e,
                "stderr_summary",
                get_stderr_summary(getattr(e, "stderr", None)),
            ),
            "unresolved_codex_home": (
                getattr(e, "unresolved_codex_home", None)
                or ctx.unresolved_codex_home
            ),
            "cleanup_error": (
                getattr(e, "cleanup_error", None)
                or ctx.cleanup_error
            ),
            "tokens_consumed": ctx.consumed_tokens,
            "unknown_token_state": ctx.unknown_token_state,
            "current_stage": ctx.current_stage,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        publish_terminal_manifest_and_report(ctx, invalid_manifest_path, invalid_data, pointer_commit_status="not_applicable")
        sys.exit(1)

if __name__ == "__main__":
    main()
