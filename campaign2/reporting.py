"""
Campaign 2 Schema 3 Reporting Module.

P0-5 — COMPLETE RATIONAL SCHEMA 3 REPORTING:
- Uses exact Fraction arithmetic for all aggregates, per-trial deltas, and per-family deltas.
- Groups task samples strictly by structural task families (F1, F2, F3, F4) derived from set assignments.
- Includes prompt-component history (contract hash, delimiter hash, control reformulation hash).
- Sources candidate active RLIMIT ceilings.
"""

import os
import json
import time
import stat
import tempfile
import hashlib
import resource
from datetime import datetime, timezone
from typing import Dict, Any, List, Optional
from fractions import Fraction

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ReportingError(RuntimeError):
    """Raised when report generation or publication fails."""
    pass


def _fraction_mean(fracs: List[Fraction]) -> Fraction:
    if not fracs:
        return Fraction(0)
    return sum(fracs) / Fraction(len(fracs))


def get_candidate_rlimits(suite: str = "suite_p", registration: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Retrieve candidate active worker resource and budget ceilings for a given suite."""
    if registration and isinstance(registration, dict) and "call_arithmetic" in registration:
        max_calls = registration["call_arithmetic"].get("per_trial_benchmark_logical_calls", 86)
    else:
        max_calls = 202 if suite.lower() == "suite_c" else 86
    return {
        "cpu_seconds": 15,
        "nofile": 64,
        "fsize_bytes": 1048576,  # 1 MiB
        "core_bytes": 0,
        "retry_cap": 3,
        "max_calls": max_calls,
    }


get_active_rlimits = get_candidate_rlimits


def generate_schema3_report(
    outcome: Dict[str, Any],
    registration: Dict[str, Any],
    tier_assignment: Dict[str, Any],
    ledger_path: str = "",
    ladder_outcome: Optional[Dict[str, Any]] = None,
    launch_lock_hash: str = "",
    workspace_root: str = BASE_DIR,
    is_mock: bool = False,
    cpu_gate_record: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Generate a complete Schema 3 experiment report populated with exact rational arithmetic.
    Groups per-family deltas strictly by structural task families (F1, F2, F3, F4).
    """
    per_trial = outcome.get("per_trial", [])
    if not per_trial:
        raise ValueError("Cannot generate report from empty trial data")

    pe_reg = registration.get("prompt_envelope", {})
    prompt_history = {
        "immutable_contract_hash": pe_reg.get("immutable_contract_hash", ""),
        "delimiter_hash": pe_reg.get("delimiter_hash", ""),
        "control_reformulation_hash": pe_reg.get("control_reformulation_hash", ""),
        "immutable_contract_text": pe_reg.get("immutable_contract", ""),
        "delimiter_text": pe_reg.get("delimiter", ""),
        "control_reformulation_text": pe_reg.get("control_reformulation_text", ""),
    }

    # Family evidence must be emitted by the orchestrator from task IDs.  A
    # reporting fallback would silently relabel tiers as structural families.
    family_scores_raw = outcome.get("family_scores")
    if not isinstance(family_scores_raw, dict):
        raise ValueError("Schema 3 outcome is missing task-derived family_scores")

    per_family_deltas = []
    for fam_key in ("F1", "F2", "F3", "F4"):
        evidence = family_scores_raw.get(fam_key)
        if not isinstance(evidence, dict):
            raise ValueError(f"Schema 3 outcome is missing structural family {fam_key}")
        opt_samples = [Fraction(value) for value in evidence.get("optimizer_samples", [])]
        ctrl_samples = [Fraction(value) for value in evidence.get("control_samples", [])]
        if not opt_samples or len(opt_samples) != len(ctrl_samples):
            raise ValueError(f"Structural family {fam_key} has missing or asymmetric samples")
        opt_mean = _fraction_mean(opt_samples)
        ctrl_mean = _fraction_mean(ctrl_samples)

        delta = opt_mean - ctrl_mean
        per_family_deltas.append({
            "family": fam_key,
            "family_name": f"Structural Task Family {fam_key[1:]}",
            "task_ids": list(evidence.get("task_ids", [])),
            "optimizer_k_samples": [str(value) for value in opt_samples],
            "control_k_samples": [str(value) for value in ctrl_samples],
            "optimizer_aggregate": str(opt_mean),
            "control_aggregate": str(ctrl_mean),
            "delta_opt_minus_ctrl": str(delta),
            "delta_positive": delta > 0,
        })

    tier_names = ("optimization", "selection", "confirmatory", "structural_holdout")
    per_tier_deltas = []
    for tier_name in tier_names:
        deltas = [
            Fraction(trial["tiers"][tier_name]["delta_opt_minus_ctrl"])
            for trial in per_trial
        ]
        per_tier_deltas.append({
            "tier": tier_name,
            "per_trial_deltas": [str(value) for value in deltas],
            "aggregate_delta_opt_minus_ctrl": str(_fraction_mean(deltas)),
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
        "is_mock": is_mock or outcome.get("is_mock", False),
        "timestamp_iso": datetime.now(timezone.utc).isoformat(),
        "base_commit_sha": registration.get("base_commit_sha", ""),
        "campaign_id": registration.get("campaign_id", ""),
        "arm_assignment_vectors": registration.get("arm_assignment_vectors", []),
        "prompt_history": prompt_history,
        "prompt_envelope_hashes": {
            "immutable_contract_hash": pe_reg.get("immutable_contract_hash", ""),
            "delimiter_hash": pe_reg.get("delimiter_hash", ""),
            "control_reformulation_hash": pe_reg.get("control_reformulation_hash", ""),
        },
        "registration_hashes": {
            "master_registration_sha256": master_reg_hash,
            "launch_lock_sha256": launch_lock_hash,
        },
        "ledger_file": ledger_path,
        "active_rlimits": get_candidate_rlimits(suite=outcome.get("suite", "suite_p"), registration=registration),
        "prescientific_cpu_gate": cpu_gate_record if cpu_gate_record else {},
        "primary_outcome": {
            "opt_sel_baseline_aggregate": outcome.get("opt_sel_baseline_aggregate", "0"),
            "conf_baseline_aggregate": outcome.get("conf_baseline_aggregate", "0"),
            "conf_holdout_baseline_aggregate": outcome.get("conf_holdout_baseline_aggregate", "0"),
            "baseline_aggregate": outcome.get("baseline_aggregate", "0"),
            "opt_sel_opt_aggregate": outcome.get("opt_sel_opt_aggregate", "0"),
            "opt_sel_ctrl_aggregate": outcome.get("opt_sel_ctrl_aggregate", "0"),
            "optimizer_winner_aggregate": outcome.get("optimizer_winner_aggregate", "0"),
            "control_winner_aggregate": outcome.get("control_winner_aggregate", "0"),
            "delta_opt_minus_ctrl": outcome.get("delta_opt_minus_ctrl", "0"),
            "delta_positive": outcome.get("delta_positive", False),
        },
        "per_family_deltas": per_family_deltas,
        "per_tier_deltas": per_tier_deltas,
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
    """
    Publish Schema 3 report with atomic sealed publication (0o444).
    Write temp -> flush -> fchmod(0o444) -> fsync(fd) -> close -> atomic replace -> fsync(parent).

    Fail-closed:
    - If output_path already exists (or is a symlink / dangling symlink / non-regular file / mode != 0o444):
      - If it is a symlink, dangling symlink, non-regular file, or permission mode != 0o444: raise ReportingError
      - If existing file contains identical bytes: accept idempotently (return output_path)
      - If existing file contains DIFFERENT bytes: raise ReportingError (NEVER overwrite differing sealed report)
    """
    new_bytes = (json.dumps(report, indent=2, sort_keys=True) + "\n").encode("utf-8")

    parent_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(parent_dir, exist_ok=True)

    if os.path.lexists(output_path):
        if os.path.islink(output_path) or not os.path.isfile(output_path):
            raise ReportingError(f"Existing report path is a symlink or non-regular file: {output_path}")
        st_mode = stat.S_IMODE(os.stat(output_path).st_mode)
        if st_mode != 0o444:
            raise ReportingError(f"Existing report file permission mode must be 0o444, got {oct(st_mode)} for {output_path}")
        with open(output_path, "rb") as f:
            existing_bytes = f.read()
        if existing_bytes == new_bytes:
            return output_path
        raise ReportingError(f"Sealed report already exists at '{output_path}' with differing content; refusing overwrite")

    tmp_fd, tmp_path = tempfile.mkstemp(dir=parent_dir, prefix=".report_tmp_")
    try:
        written = 0
        while written < len(new_bytes):
            n = os.write(tmp_fd, new_bytes[written:])
            if n <= 0:
                raise ReportingError(f"Short write while saving report to {tmp_path}")
            written += n
        os.fchmod(tmp_fd, 0o444)
        os.fsync(tmp_fd)
        os.close(tmp_fd)
        tmp_fd = None
        os.replace(tmp_path, output_path)
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
        dir_fd = os.open(parent_dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    return output_path
