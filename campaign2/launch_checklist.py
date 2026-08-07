"""
Campaign 2 Launch Checklist Generator and Verifier Module.

Generates and verifies history/campaign2_launch_checklist.json (0o444).
Captures approved HEAD, tracked-tree status, artifact hashes, preflight inventory count,
frozen design parameters, accounting ceilings, and ordered launch steps.
"""

import os
import sys
import json
import stat
import hashlib
import tempfile
import subprocess
from typing import Dict, Any, List, Tuple

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHECKLIST_RELATIVE_PATH = os.path.join("history", "campaign2_launch_checklist.json")

from campaign2.registration import FROZEN_ORDERED_LAUNCH_STEPS
ORDERED_LAUNCH_STEPS = list(FROZEN_ORDERED_LAUNCH_STEPS)


class LaunchChecklistError(RuntimeError):
    """Raised when launch checklist generation or verification fails."""
    pass


def get_git_head_sha(workspace_root: str = BASE_DIR) -> str:
    """Retrieve live HEAD commit SHA from Git."""
    try:
        out = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=workspace_root)
        return out.decode("utf-8").strip()
    except Exception as exc:
        raise LaunchChecklistError(f"Failed to retrieve Git HEAD SHA: {exc}") from exc


def get_git_tracked_status(workspace_root: str = BASE_DIR) -> bool:
    """Verify Git tracked working tree status is clean (porcelain output is empty excluding untracked files)."""
    try:
        out = subprocess.check_output(["git", "status", "--porcelain", "--untracked-files=no"], cwd=workspace_root)
        return len(out.strip()) == 0
    except Exception as exc:
        raise LaunchChecklistError(f"Failed to check Git status: {exc}") from exc


def compute_file_sha256(path: str) -> str:
    """Compute SHA-256 digest of a file."""
    if not os.path.exists(path) or os.path.islink(path) or not os.path.isfile(path):
        raise LaunchChecklistError(f"File missing or invalid for digest computation: {path}")
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def generate_launch_checklist_data(workspace_root: str = BASE_DIR) -> Dict[str, Any]:
    """
    Generate machine-derived launch checklist data from verified live sources.
    No values are retyped or hardcoded where live sources exist.
    """
    from campaign2.manifest import verify_campaign2_manifest
    from campaign2.registration import verify_master_registration
    from campaign2.tier_assignment import verify_family_blocked_assignment

    real_repo_path = os.path.realpath(workspace_root)
    head_sha = get_git_head_sha(workspace_root)
    is_clean = get_git_tracked_status(workspace_root)

    if not is_clean:
        raise LaunchChecklistError("Cannot generate launch checklist: Git tracked working tree contains uncommitted changes")

    # Preflight manifest verification
    manifest_ok, preflight_file_count = verify_campaign2_manifest(workspace_root)
    if not manifest_ok:
        raise LaunchChecklistError("Campaign 2 preflight manifest verification failed")

    # Master registration verification
    reg_path = os.path.join(workspace_root, "campaign2_master_registration.json")
    reg_data = verify_master_registration(workspace_root)
    master_reg_hash = compute_file_sha256(reg_path)

    # Evidence manifest verification and hashing
    from campaign2.manifest import verify_campaign2_evidence_manifest
    verify_campaign2_evidence_manifest(workspace_root)
    evidence_path = os.path.join(workspace_root, "campaign2_evidence_hashes.json")
    evidence_manifest_hash = compute_file_sha256(evidence_path)

    # Tier assignment hash
    tier_path = os.path.join(workspace_root, "campaign2_tier_assignment.json")
    verify_family_blocked_assignment(workspace_root)
    tier_hash = compute_file_sha256(tier_path)

    arm_vectors = reg_data.get("arm_assignment_vectors", [])
    if len(arm_vectors) != 10:
        raise LaunchChecklistError(f"Master registration must contain 10 arm assignment vectors, got {len(arm_vectors)}")

    # Derived accounting ceilings directly from verified master registration
    if "call_arithmetic" not in reg_data or "execution_limits" not in reg_data or "pilot_limits" not in reg_data:
        raise LaunchChecklistError("Master registration missing required call_arithmetic, execution_limits, or pilot_limits")

    call_arith = reg_data["call_arithmetic"]
    exec_limits = reg_data["execution_limits"]
    pilot_lims = reg_data["pilot_limits"]

    try:
        per_trial = call_arith["per_trial_benchmark_logical_calls"]
        num_trials = pilot_lims["num_trials"]
        benchmark_workload = call_arith["pilot_total_benchmark_logical_calls"]
        canary_calls = call_arith["max_canary_calls"]
        total_ceiling = call_arith["pilot_max_logical_calls"]
        max_attempts = exec_limits["max_invocation_attempts"]
        attempt_ceiling = call_arith["max_local_invocation_attempts"]

        per_call_wall_cap = int(exec_limits["call_wall_limit_seconds"])
        cand_cpu_limit = int(exec_limits["candidate_cpu_limit_seconds"])
        cand_wall_limit = int(exec_limits["candidate_wall_limit_seconds"])
        output_token_ceiling = int(exec_limits["max_output_tokens"])
        active_time_hours = float(exec_limits["max_active_seconds"]) / 3600.0
    except KeyError as ke:
        raise LaunchChecklistError(f"Master registration missing required limit key: {ke}") from ke

    from campaign2.cpu_gate import MAX_ALLOWED_CPU_SECONDS
    cpu_threshold = MAX_ALLOWED_CPU_SECONDS

    # Assert mathematical relationships
    if per_trial * num_trials != benchmark_workload:
        raise LaunchChecklistError(f"Per-trial call arithmetic mismatch: {per_trial} * {num_trials} != {benchmark_workload}")
    if benchmark_workload + canary_calls != total_ceiling:
        raise LaunchChecklistError(f"Total logical call ceiling arithmetic mismatch: {benchmark_workload} + {canary_calls} != {total_ceiling}")
    if total_ceiling * max_attempts != attempt_ceiling:
        raise LaunchChecklistError(f"Attempt ceiling arithmetic mismatch: {total_ceiling} * {max_attempts} != {attempt_ceiling}")
    if total_ceiling != exec_limits.get("max_logical_calls"):
        raise LaunchChecklistError(f"Registration total logical calls mismatch: {total_ceiling} != {exec_limits.get('max_logical_calls')}")
    if attempt_ceiling != exec_limits.get("max_local_invocation_attempts"):
        raise LaunchChecklistError(f"Registration max invocation attempts mismatch: {attempt_ceiling} != {exec_limits.get('max_local_invocation_attempts')}")

    checklist_data = {
        "schema_version": "1",
        "canonical_repository_realpath": real_repo_path,
        "approved_commit_sha": head_sha,
        "clean_tracked_tree": is_clean,
        "master_registration_hash": master_reg_hash,
        "evidence_manifest_hash": evidence_manifest_hash,
        "tier_assignment_hash": tier_hash,
        "tier_configuration_hash_alias": tier_hash,
        "verified_preflight_file_count": preflight_file_count,
        "frozen_design_values": {
            "design_seed_commit": reg_data.get("base_commit_sha", ""),
            "arm_assignment_vectors": arm_vectors,
            "trio_ordering_master_seed": reg_data.get("trio_ordering_master_seed", 0),
        },
        "accounting": {
            "benchmark_workload": benchmark_workload,
            "canary_logical_calls": canary_calls,
            "total_logical_call_ceiling": total_ceiling,
            "invocation_attempt_ceiling": attempt_ceiling,
            "output_token_ceiling": output_token_ceiling,
            "cumulative_active_model_invocation_time_ceiling_hours": active_time_hours,
            "per_logical_call_wall_cap_seconds": per_call_wall_cap,
            "candidate_cpu_limit_seconds": cand_cpu_limit,
            "candidate_wall_limit_seconds": cand_wall_limit,
            "cpu_gate_threshold_seconds": cpu_threshold,
        },
        "ordered_launch_steps": ORDERED_LAUNCH_STEPS,
    }
    return checklist_data


def write_sealed_launch_checklist(workspace_root: str = BASE_DIR) -> str:
    """
    Generate and write sealed history/campaign2_launch_checklist.json (0o444).
    Durability order: write -> fchmod(0o444) -> fsync -> close -> replace -> parent fsync.
    """
    data = generate_launch_checklist_data(workspace_root)
    checklist_path = os.path.join(workspace_root, CHECKLIST_RELATIVE_PATH)
    history_dir = os.path.dirname(checklist_path)
    os.makedirs(history_dir, exist_ok=True)

    content_bytes = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")

    if os.path.lexists(checklist_path):
        if os.path.islink(checklist_path) or not os.path.isfile(checklist_path):
            raise LaunchChecklistError(f"Checklist path is a symlink or non-regular file: {checklist_path}")
        st_mode = stat.S_IMODE(os.stat(checklist_path).st_mode)
        if st_mode != 0o444:
            raise LaunchChecklistError(f"Existing checklist file permission mode must be 0o444, got {oct(st_mode)}")
        with open(checklist_path, "rb") as f:
            existing_bytes = f.read()
        if existing_bytes == content_bytes:
            return checklist_path
        raise LaunchChecklistError(f"Sealed launch checklist already exists at '{checklist_path}' with differing content; refusing overwrite")

    tmp_fd, tmp_path = tempfile.mkstemp(dir=history_dir, prefix=".checklist_tmp_")
    try:
        written = 0
        while written < len(content_bytes):
            n = os.write(tmp_fd, content_bytes[written:])
            if n <= 0:
                raise LaunchChecklistError(f"Short write while saving launch checklist to {tmp_path}")
            written += n
        os.fchmod(tmp_fd, 0o444)
        os.fsync(tmp_fd)
        os.close(tmp_fd)
        tmp_fd = None
        os.replace(tmp_path, checklist_path)
    except Exception:
        if tmp_fd is not None:
            try:
                os.close(tmp_fd)
            except Exception:
                pass
        if os.path.lexists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        raise
    finally:
        dir_fd = os.open(history_dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    return checklist_path


def verify_launch_checklist(workspace_root: str = BASE_DIR) -> Dict[str, Any]:
    """
    Strictly verify history/campaign2_launch_checklist.json against live repo and files.
    Fails closed on any mismatch.
    """
    checklist_path = os.path.join(workspace_root, CHECKLIST_RELATIVE_PATH)
    if not os.path.lexists(checklist_path):
        raise LaunchChecklistError(f"Missing launch checklist file: {checklist_path}")
    if os.path.islink(checklist_path) or not os.path.isfile(checklist_path):
        raise LaunchChecklistError(f"Launch checklist must be a regular non-symlink file: {checklist_path}")
    st_mode = stat.S_IMODE(os.stat(checklist_path).st_mode)
    if st_mode != 0o444:
        raise LaunchChecklistError(f"Launch checklist permission mode must be 0o444, got {oct(st_mode)}")

    with open(checklist_path, "rb") as f:
        content_bytes = f.read()
    try:
        data = json.loads(content_bytes.decode("utf-8"))
    except Exception as exc:
        raise LaunchChecklistError(f"Unparseable launch checklist JSON: {exc}") from exc

    live_data = generate_launch_checklist_data(workspace_root)
    if data != live_data:
        raise LaunchChecklistError("Launch checklist verification failed: content mismatch against live repository state")

    return data
