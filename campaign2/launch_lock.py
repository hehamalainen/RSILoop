"""
Campaign 2 Launch Lock Module.

P0-1 — MANDATORY LAUNCH LOCK & GIT AUTHENTICATION:
- Generates campaign2_launch_lock.json capturing git HEAD commit SHA.
- Strictly authenticates that workspace_root is a valid Git repository root.
- Requires non-empty registration and configuration hashes.
- Sets file mode to read-only 0o444 and prevents overwriting sealed lock files.
"""

import os
import sys
import json
import stat
import subprocess
import hashlib
from typing import Dict, Any

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOCK_FILE_REL = "campaign2_launch_lock.json"


class LaunchLockError(ValueError):
    """Raised when launch lock generation, verification, or authentication fails."""
    pass


def _git_head_sha(workspace_root: str = BASE_DIR) -> str:
    """Retrieve git HEAD commit SHA for workspace_root. Strictly requires workspace_root to be a Git root."""
    workspace_realpath = os.path.realpath(workspace_root)
    try:
        res_top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=workspace_root,
            capture_output=True,
            text=True,
            check=False
        )
        if res_top.returncode == 0:
            top_dir = os.path.realpath(res_top.stdout.strip())
            if top_dir != workspace_realpath:
                raise LaunchLockError(
                    f"Workspace root '{workspace_root}' is not the root of a Git repository "
                    f"(found parent git root '{top_dir}')"
                )
        else:
            raise LaunchLockError(f"Workspace root is not a Git repository: {workspace_root}")
    except Exception as e:
        if isinstance(e, LaunchLockError):
            raise
        raise LaunchLockError(f"Workspace root is not a Git repository: {workspace_root}") from e

    try:
        res = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace_root,
            capture_output=True,
            text=True,
            check=False
        )
        if res.returncode == 0:
            sha = res.stdout.strip()
            if len(sha) == 40 and all(c in '0123456789abcdef' for c in sha):
                return sha
    except Exception:
        pass

    raise LaunchLockError(f"Cannot determine git HEAD SHA for workspace: {workspace_root}")


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
        st_mode = stat.S_IMODE(os.stat(lock_path).st_mode)
        if st_mode == 0o444:
            raise LaunchLockError("Sealed launch lock already exists (0o444). Unseal or remove before regenerating.")
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
        raise LaunchLockError(f"Missing launch lock file: {lock_path}")
    if os.path.islink(lock_path):
        raise LaunchLockError(f"Launch lock file must not be a symlink: {lock_path}")
    if not os.path.isfile(lock_path):
        raise LaunchLockError(f"Launch lock file must be a regular file: {lock_path}")

    st_mode = stat.S_IMODE(os.stat(lock_path).st_mode)
    if st_mode != 0o444:
        raise LaunchLockError(f"Launch lock permission mode must be 0o444, got {oct(st_mode)}")

    try:
        with open(lock_path, "r", encoding="utf-8") as f:
            lock_data = json.load(f)
    except Exception as e:
        raise LaunchLockError(f"Failed to parse launch lock JSON: {e}")

    if lock_data.get("schema_version") != "3":
        raise LaunchLockError(f"Launch lock schema_version must be '3', got {lock_data.get('schema_version')}")

    # Check canonical realpath
    expected_realpath = os.path.realpath(workspace_root)
    actual_realpath = lock_data.get("canonical_realpath", "")
    if actual_realpath != expected_realpath:
        raise LaunchLockError(
            f"Launch lock canonical realpath mismatch: expected '{expected_realpath}', "
            f"got '{actual_realpath}'"
        )

    # Check running commit SHA against lock
    head_sha = _git_head_sha(workspace_root)
    approved_sha = lock_data.get("approved_commit_sha", "")
    if not approved_sha:
        raise LaunchLockError("Launch lock approved_commit_sha is empty")
    if head_sha != approved_sha:
        raise LaunchLockError(
            f"Launch lock commit SHA mismatch: approved commit is '{approved_sha}', "
            f"currently running git HEAD is '{head_sha}'"
        )

    # Verify registration hash
    reg_path = os.path.join(workspace_root, "campaign2_master_registration.json")
    actual_reg_hash = _compute_file_hash(reg_path)
    expected_reg_hash = lock_data.get("registration_hash", "")
    if not expected_reg_hash:
        raise LaunchLockError("Launch lock registration_hash is empty")
    if actual_reg_hash != expected_reg_hash:
        raise LaunchLockError(
            f"Master Registration hash mismatch: lock has '{expected_reg_hash}', "
            f"disk has '{actual_reg_hash}'"
        )

    # Verify configuration hash
    config_path = os.path.join(workspace_root, "campaign2_tier_assignment.json")
    actual_config_hash = _compute_file_hash(config_path)
    expected_config_hash = lock_data.get("configuration_hash", "")
    if not expected_config_hash:
        raise LaunchLockError("Launch lock configuration_hash is empty")
    if actual_config_hash != expected_config_hash:
        raise LaunchLockError(
            f"Tier Assignment hash mismatch: lock has '{expected_config_hash}', "
            f"disk has '{actual_config_hash}'"
        )

    return lock_data
