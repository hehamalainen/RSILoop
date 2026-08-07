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

# Accounting constants (derived from design specs)
PER_LOGICAL_CALL_WALL_CAP_SECONDS = 120
CANDIDATE_CPU_LIMIT_SECONDS = 15
CANDIDATE_WALL_LIMIT_SECONDS = 15
CPU_GATE_THRESHOLD_SECONDS = 3.0
OUTPUT_TOKEN_CEILING = 600_000
CUMULATIVE_ACTIVE_TIME_CEILING_HOURS = 8.0

ORDERED_LAUNCH_STEPS = [
    "1. manifest_verification",
    "2. evidence_manifest_verification",
    "3. parity_matrix_verification",
    "4. tier_assignment_verification",
    "5. master_registration_verification",
    "6. launch_lock_verification",
    "7. attempt_identity_creation_or_verification",
    "8. attempt_ledger_and_resume_admission_verification",
    "9. checkpoint_manager_initialization",
    "10. codex_cli_version_check",
    "11. prescientific_cpu_gate",
    "12. preflight_registry_authentication",
    "13. pilot_canary_execution_or_reconciliation",
    "14. scientific_start_claim",
    "15. trial_1_execution_and_checkpoint",
    "16. trial_2_execution_and_checkpoint",
    "17. pilot_outcome_ladder_evaluation",
    "18. schema3_report_generation_and_sealing",
    "19. terminal_classification_recording"
]


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

    # Evidence manifest hash
    evidence_path = os.path.join(workspace_root, "campaign2_evidence_hashes.json")
    evidence_manifest_hash = compute_file_sha256(evidence_path)

    # Tier assignment hash
    tier_path = os.path.join(workspace_root, "campaign2_tier_assignment.json")
    verify_family_blocked_assignment(workspace_root)
    tier_hash = compute_file_sha256(tier_path)

    arm_vectors = reg_data.get("arm_assignment_vectors", [])
    if len(arm_vectors) != 10:
        raise LaunchChecklistError(f"Master registration must contain 10 arm assignment vectors, got {len(arm_vectors)}")

    # Derived accounting ceilings
    benchmark_workload = len(arm_vectors) * 86 // 5  # 172
    canary_calls = 3
    total_ceiling = benchmark_workload + canary_calls  # 175
    attempt_ceiling = total_ceiling * 3  # 525

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
            "output_token_ceiling": OUTPUT_TOKEN_CEILING,
            "cumulative_active_model_invocation_time_ceiling_hours": CUMULATIVE_ACTIVE_TIME_CEILING_HOURS,
            "per_logical_call_wall_cap_seconds": PER_LOGICAL_CALL_WALL_CAP_SECONDS,
            "candidate_cpu_limit_seconds": CANDIDATE_CPU_LIMIT_SECONDS,
            "candidate_wall_limit_seconds": CANDIDATE_WALL_LIMIT_SECONDS,
            "cpu_gate_threshold_seconds": CPU_GATE_THRESHOLD_SECONDS,
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
