"""
Campaign 2 Launch Lock Module.

B1 — CANONICAL ROOT LAUNCH LOCK:
- Operator-sealed launch lock (0o444) containing:
  canonical repository REALPATH, approved commit SHA, registration hash,
  configuration hash.
- Phase 0 computes its own root realpath and REFUSES execution if it differs.
- The lock is NOT committed in source — it is created by the operator after
  the implementation commit has been reviewed.
- Verification reads `git rev-parse HEAD` to authenticate the running commit.

Rationale: /Users/.../Documents/RSILoop holds an independent clone plus a dirty
builder_worktree at 5d5af00 and is an operator-identity hazard.
"""

import os
import sys
import json
import stat
import hashlib
import subprocess

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK_FILE_REL = "campaign2_launch_lock.json"


class LaunchLockError(RuntimeError):
    """Raised when launch lock validation fails."""
    pass


def _git_head_sha(workspace_root: str) -> str:
    """Read the current HEAD commit SHA from git with direct filesystem fallback."""
    # Attempt 1: git rev-parse HEAD subprocess
    try:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace_root,
            capture_output=True,
            text=True,
            timeout=10,
            env=env,
        )
        if result.returncode == 0:
            sha = result.stdout.strip()
            if len(sha) == 40 and all(c in '0123456789abcdef' for c in sha):
                return sha
    except Exception:
        pass

    # Attempt 2: Direct .git file parsing fallback
    try:
        git_dir = os.path.join(workspace_root, ".git")
        if os.path.isfile(git_dir):
            with open(git_dir, "r", encoding="utf-8") as f:
                content = f.read().strip()
            if content.startswith("gitdir:"):
                git_dir = content.split("gitdir:", 1)[1].strip()
                if not os.path.isabs(git_dir):
                    git_dir = os.path.realpath(os.path.join(workspace_root, git_dir))

        head_file = os.path.join(git_dir, "HEAD")
        if os.path.isfile(head_file):
            with open(head_file, "r", encoding="utf-8") as f:
                head_content = f.read().strip()
            if head_content.startswith("ref:"):
                ref_path = head_content.split("ref:", 1)[1].strip()
                ref_file = os.path.join(git_dir, ref_path)
                if os.path.isfile(ref_file):
                    with open(ref_file, "r", encoding="utf-8") as f:
                        sha = f.read().strip()
                    if len(sha) == 40 and all(c in '0123456789abcdef' for c in sha):
                        return sha
            elif len(head_content) == 40 and all(c in '0123456789abcdef' for c in head_content):
                return head_content
    except Exception:
        pass

    raise LaunchLockError(f"Could not determine git HEAD SHA for workspace: {workspace_root}")


def _compute_file_hash(path: str) -> str:
    """Compute SHA-256 hash of a file's contents."""
    if not os.path.isfile(path):
        raise LaunchLockError(f"File not found for hashing: {path}")
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def generate_launch_lock(
    workspace_root: str = BASE_DIR,
    lock_rationale: str = "",
) -> str:
    """
    Generate the campaign2_launch_lock.json file.

    MUST be called only AFTER the final implementation commit has been reviewed.
    Reads git HEAD to capture the actual running commit SHA.
    Requires non-empty registration and configuration hashes.
    """
    canonical_realpath = os.path.realpath(workspace_root)
    head_sha = _git_head_sha(workspace_root)

    # Compute registration hash
    reg_path = os.path.join(workspace_root, "campaign2_master_registration.json")
    registration_hash = _compute_file_hash(reg_path)
    if not registration_hash:
        raise LaunchLockError("Registration hash is empty")

    # Compute configuration hash (tier assignment)
    config_path = os.path.join(workspace_root, "campaign2_tier_assignment.json")
    configuration_hash = _compute_file_hash(config_path)
    if not configuration_hash:
        raise LaunchLockError("Configuration hash is empty")

    lock_data = {
        "schema_version": "3",
        "canonical_realpath": canonical_realpath,
        "approved_commit_sha": head_sha,
        "registration_hash": registration_hash,
        "configuration_hash": configuration_hash,
        "lock_rationale": lock_rationale,
    }

    lock_path = os.path.join(workspace_root, LOCK_FILE_REL)
    if os.path.exists(lock_path):
        os.chmod(lock_path, 0o644)

    lock_content = json.dumps(lock_data, indent=2, sort_keys=True) + "\n"
    with open(lock_path, "w", encoding="utf-8") as f:
        f.write(lock_content)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(lock_path, 0o444)

    dir_fd = os.open(workspace_root, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)

    return lock_path


def verify_launch_lock(workspace_root: str = BASE_DIR) -> dict:
    """
    Strict verification of the Campaign 2 Launch Lock.

    1. Lock file exists, is regular, is not a symlink, has mode 0o444.
    2. Lock JSON is valid and schema_version == "3".
    3. canonical_realpath matches os.path.realpath(workspace_root).
    4. approved_commit_sha matches `git rev-parse HEAD`.
    5. registration_hash is non-empty and matches disk content.
    6. configuration_hash is non-empty and matches disk content.

    Returns the parsed lock data on success.
    """
    lock_path = os.path.join(workspace_root, LOCK_FILE_REL)

    if not os.path.exists(lock_path):
        raise LaunchLockError(f"Launch lock file not found: {lock_path}")
    if os.path.islink(lock_path):
        raise LaunchLockError(f"Launch lock must not be a symlink: {lock_path}")
    if not os.path.isfile(lock_path):
        raise LaunchLockError(f"Launch lock must be a regular file: {lock_path}")

    st_mode = stat.S_IMODE(os.stat(lock_path).st_mode)
    if st_mode != 0o444:
        raise LaunchLockError(
            f"Launch lock permission mode must be 0o444, got {oct(st_mode)}"
        )

    with open(lock_path, "r", encoding="utf-8") as f:
        lock_data = json.load(f)

    if lock_data.get("schema_version") != "3":
        raise LaunchLockError(
            f"Launch lock schema_version must be '3', got '{lock_data.get('schema_version')}'"
        )

    # Verify canonical realpath
    expected_realpath = os.path.realpath(workspace_root)
    if lock_data.get("canonical_realpath") != expected_realpath:
        raise LaunchLockError(
            f"Launch lock canonical_realpath mismatch: "
            f"lock has '{lock_data.get('canonical_realpath')}', "
            f"expected '{expected_realpath}'"
        )

    # Verify running commit matches lock's approved SHA
    head_sha = _git_head_sha(workspace_root)
    if lock_data.get("approved_commit_sha") != head_sha:
        raise LaunchLockError(
            f"Launch lock commit mismatch: lock approves "
            f"'{lock_data.get('approved_commit_sha')}', "
            f"but HEAD is '{head_sha}'"
        )

    # Verify non-empty registration hash
    reg_hash = lock_data.get("registration_hash", "")
    if not reg_hash:
        raise LaunchLockError("Launch lock registration_hash is empty")
    reg_path = os.path.join(workspace_root, "campaign2_master_registration.json")
    actual_reg_hash = _compute_file_hash(reg_path)
    if reg_hash != actual_reg_hash:
        raise LaunchLockError(
            f"Launch lock registration_hash mismatch: "
            f"lock has '{reg_hash}', disk has '{actual_reg_hash}'"
        )

    # Verify non-empty configuration hash
    config_hash = lock_data.get("configuration_hash", "")
    if not config_hash:
        raise LaunchLockError("Launch lock configuration_hash is empty")
    config_path = os.path.join(workspace_root, "campaign2_tier_assignment.json")
    actual_config_hash = _compute_file_hash(config_path)
    if config_hash != actual_config_hash:
        raise LaunchLockError(
            f"Launch lock configuration_hash mismatch: "
            f"lock has '{config_hash}', disk has '{actual_config_hash}'"
        )

    return lock_data
