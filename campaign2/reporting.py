"""
Campaign 2 Schema 3 Reporting Module.

P1-8 — COMPLETE SCHEMA 3 REPORTING:
Populates Schema 3 experiment report with:
- arm assignment vectors
- prompt hashes (immutable_contract_hash, delimiter_hash)
- master registration hash & anchor hash
- launch lock hash (if available)
- attempt ledger path
- active RLIMIT ceilings
- pilot outcome ladder decision
- per-tier and per-trial delta metrics
"""

import os
import json
import time
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from fractions import Fraction

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def generate_schema3_report(
    outcome: Dict[str, Any],
    registration: Dict[str, Any],
    tier_assignment: Dict[str, Any],
    ledger_path: str = "",
    ladder_outcome: Optional[Dict[str, Any]] = None,
    launch_lock_hash: str = "",
    workspace_root: str = BASE_DIR,
) -> Dict[str, Any]:
    """
    Generate a complete Schema 3 experiment report populated with all required fields.
    """
    per_trial = outcome.get("per_trial", [])
    if not per_trial:
        raise ValueError("Cannot generate report from empty trial data")

    # Compute per-trial deltas
    per_trial_deltas = []
    for t in per_trial:
        t_id = t["trial_id"]
        # Calculate per-trial opt vs ctrl mean
        opt_sum = 0
        opt_count = 0
        for task_pairs in t.get("optimizer_scores", {}).values():
            for e, m in task_pairs:
                opt_sum += e / m
                opt_count += 1
        opt_mean = opt_sum / opt_count if opt_count > 0 else 0

        ctrl_sum = 0
        ctrl_count = 0
        for task_pairs in t.get("control_scores", {}).values():
            for e, m in task_pairs:
                ctrl_sum += e / m
                ctrl_count += 1
        ctrl_mean = ctrl_sum / ctrl_count if ctrl_count > 0 else 0

        per_trial_deltas.append({
            "trial_id": t_id,
            "arm_sign": t.get("arm_sign", +1),
            "optimizer_mean": round(opt_mean, 4),
            "control_mean": round(ctrl_mean, 4),
            "delta": round(opt_mean - ctrl_mean, 4),
        })

    master_reg_hash = ""
    reg_path = os.path.join(workspace_root, "campaign2_master_registration.json")
    if os.path.exists(reg_path):
        with open(reg_path, "rb") as f:
            master_reg_hash = hashlib.sha256(f.read()).hexdigest()

    report = {
        "schema_version": "3",
        "suite": outcome["suite"],
        "num_trials": outcome["num_trials"],
        "timestamp_iso": datetime.now(timezone.utc).isoformat(),
        "base_commit_sha": registration.get("base_commit_sha", ""),
        "campaign_id": registration.get("campaign_id", ""),
        "arm_assignment_vectors": registration.get("arm_assignment_vectors", []),
        "prompt_envelope_hashes": {
            "immutable_contract_hash": outcome.get("immutable_contract_hash", ""),
            "delimiter_hash": outcome.get("delimiter_hash", ""),
        },
        "registration_hashes": {
            "master_registration_sha256": master_reg_hash,
            "launch_lock_sha256": launch_lock_hash,
        },
        "ledger_file": ledger_path,
        "active_rlimits": {
            "cpu_seconds": 15,
            "nofile": 64,
            "fsize_bytes": 1048576,
            "core_bytes": 0,
        },
        "primary_outcome": {
            "baseline_aggregate": outcome["baseline_aggregate"],
            "optimizer_winner_aggregate": outcome["optimizer_winner_aggregate"],
            "control_winner_aggregate": outcome["control_winner_aggregate"],
            "delta_opt_minus_ctrl": outcome["delta_opt_minus_ctrl"],
            "delta_positive": outcome["delta_positive"],
        },
        "per_trial_deltas": per_trial_deltas,
        "per_trial": per_trial,
        "ladder_outcome": ladder_outcome if ladder_outcome else {},
        "exploratory": outcome.get("exploratory", {}),
        "known_limitations": registration.get("known_limitations", []),
    }
    return report


def publish_schema3_report(
    report: Dict[str, Any],
    output_path: str,
) -> str:
    """Write the Schema 3 report to disk with os.fsync."""
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
    dir_fd = os.open(os.path.dirname(output_path), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)
    return output_path
