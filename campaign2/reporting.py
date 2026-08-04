"""
Campaign 2 Schema 3 Reporting Module.

Generates the Schema 3 experiment report from orchestrator trial results.
Called by the orchestrator after all trials complete.
"""

import os
import json
import time
from datetime import datetime, timezone
from typing import Dict, Any, List
from fractions import Fraction

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def generate_schema3_report(
    outcome: Dict[str, Any],
    registration: Dict[str, Any],
    tier_assignment: Dict[str, Any],
    workspace_root: str = BASE_DIR,
) -> Dict[str, Any]:
    """
    Generate a Schema 3 experiment report from orchestrator outcome.
    Called with real trial data — never with empty lists or placeholders.
    """
    per_trial = outcome.get("per_trial", [])
    if not per_trial:
        raise ValueError("Cannot generate report from empty trial data")

    report = {
        "schema_version": "3",
        "suite": outcome["suite"],
        "num_trials": outcome["num_trials"],
        "timestamp_iso": datetime.now(timezone.utc).isoformat(),
        "base_commit_sha": registration.get("base_commit_sha", ""),
        "campaign_id": registration.get("campaign_id", ""),
        "primary_outcome": {
            "baseline_aggregate": outcome["baseline_aggregate"],
            "optimizer_winner_aggregate": outcome["optimizer_winner_aggregate"],
            "control_winner_aggregate": outcome["control_winner_aggregate"],
            "delta_opt_minus_ctrl": outcome["delta_opt_minus_ctrl"],
            "delta_positive": outcome["delta_positive"],
        },
        "per_trial": per_trial,
        "exploratory": outcome.get("exploratory", {}),
        "known_limitations": registration.get("known_limitations", []),
    }
    return report


def publish_schema3_report(
    report: Dict[str, Any],
    output_path: str,
) -> str:
    """
    Write the Schema 3 report to disk with fsync.
    """
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
