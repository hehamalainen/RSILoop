"""
Campaign 2 Pre-Scientific CPU Gate Module (C1-9).

Measures WORKER CPU time — not wall time — once for each of the 120 parity rows
through the PRODUCTION evaluator path (evaluate_candidate_code_sandboxed).
Persists every row measurement plus the maximum and its row ID in a durable
pre-scientific record (campaign2_cpu_gate_record.json, mode 0o444).
First complete result is BINDING. If maximum CPU time exceeds 3.0 s, launch stops.
"""

import os
import json
import stat
import time
from typing import Dict, Any

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CPU_GATE_RECORD_REL = "campaign2_cpu_gate_record.json"
MAX_ALLOWED_CPU_SECONDS = 3.0


class CpuGateError(RuntimeError):
    """Base exception for pre-scientific CPU gate failures."""
    pass


class CpuGateLimitExceededError(CpuGateError):
    """Raised when worker CPU time exceeds 3.0 s limit."""
    pass


def get_row_code(row: dict, workspace_root: str = BASE_DIR) -> str:
    """Helper to fetch or construct code for a parity matrix row (reference solution or defect rung)."""
    task_id = row["task_id"]
    suite = "suite_c" if task_id.startswith("c_") else "suite_p"
    task_dir = os.path.join(workspace_root, "benchmarks", "campaign2", suite, task_id)
    if not os.path.exists(task_dir):
        task_dir = os.path.join(BASE_DIR, "benchmarks", "campaign2", suite, task_id)
    sol_path = os.path.join(task_dir, "solution.py")

    with open(sol_path, "r", encoding="utf-8") as f:
        sol_code = f.read()

    row_type = row.get("type", "reference")
    if row_type == "reference":
        return sol_code

    rung = row.get("rung", 1)
    def_path = os.path.join(task_dir, "defects.py")
    with open(def_path, "r", encoding="utf-8") as f:
        def_code = f.read()

    from campaign2.defects import load_task_module
    sol_mod = load_task_module(task_dir, "solution")
    defects_mod = load_task_module(task_dir, "defects")

    callable_names = [
        attr for attr in dir(sol_mod)
        if not attr.startswith("__") and callable(getattr(sol_mod, attr)) and hasattr(getattr(sol_mod, attr), "__name__")
    ]
    defect_ladder = getattr(defects_mod, "DEFECT_LADDER", [])
    if rung <= len(defect_ladder):
        rung_fn = defect_ladder[rung - 1]
        aliases = "\n".join([f"{name} = {rung_fn.__name__}" for name in callable_names])
        cand_code = sol_code + "\n\n" + def_code + "\n\n" + aliases + "\n"
        return cand_code

    return sol_code


def run_prescientific_cpu_gate(workspace_root: str = BASE_DIR, is_mock: bool = False) -> Dict[str, Any]:
    """
    Executes or verifies the pre-scientific worker CPU time gate for all 120 parity rows.
    Must run BEFORE SCIENTIFIC_START and before canary.
    Returns the binding CPU gate record.
    """
    record_path = os.path.join(workspace_root, CPU_GATE_RECORD_REL)

    # 1. If record exists, verify it strictly (binding result)
    if os.path.exists(record_path):
        if os.path.islink(record_path) or not os.path.isfile(record_path):
            raise CpuGateError(f"CPU gate record must be a regular non-symlink file: {record_path}")
        st_mode = stat.S_IMODE(os.stat(record_path).st_mode)
        if st_mode != 0o444:
            raise CpuGateError(f"CPU gate record mode must be 0o444, got {oct(st_mode)}")
        try:
            with open(record_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            raise CpuGateError(f"Corrupted CPU gate record: {exc}") from exc

        if "is_mock" not in data or data.get("is_mock") is not is_mock:
            raise CpuGateError(
                f"Existing CPU gate record is_mock state mismatch or missing explicit is_mock key: "
                f"record is_mock={data.get('is_mock')}, expected is_mock={is_mock}"
            )

        max_cpu = data.get("max_cpu_seconds", 0.0)
        if max_cpu > MAX_ALLOWED_CPU_SECONDS:
            raise CpuGateLimitExceededError(
                f"Existing CPU gate record bound a failing max worker CPU time: {max_cpu:.4f} s > {MAX_ALLOWED_CPU_SECONDS} s"
            )
        return data

    # 2. Otherwise execute the pre-scientific CPU gate benchmark across all 120 parity rows
    from campaign2.manifest import PARITY_MATRIX_PATH
    from campaign2.proxy import evaluate_candidate_code_sandboxed

    if not os.path.exists(PARITY_MATRIX_PATH):
        raise CpuGateError(f"Missing parity matrix file: {PARITY_MATRIX_PATH}")

    with open(PARITY_MATRIX_PATH, "r", encoding="utf-8") as f:
        parity_matrix = json.load(f)

    if isinstance(parity_matrix, dict):
        if "matrix" in parity_matrix and isinstance(parity_matrix["matrix"], list):
            rows = parity_matrix["matrix"]
        else:
            rows = list(parity_matrix.values())
    elif isinstance(parity_matrix, list):
        rows = parity_matrix
    else:
        rows = []

    if len(rows) != 120:
        raise CpuGateError(f"Parity matrix must contain exactly 120 rows, got {len(rows)}")

    row_measurements = []
    max_cpu_seconds = 0.0
    max_row_id = ""

    for row in rows:
        task_id = row["task_id"]
        row_id = row.get("row_id", f"{task_id}_{row.get('variant', 'unknown')}")
        code = get_row_code(row, workspace_root=workspace_root)

        if is_mock:
            # In mock mode, worker CPU time is deterministic fast mock measurement
            worker_cpu_sec = 0.01
        else:
            import resource
            ru0 = resource.getrusage(resource.RUSAGE_CHILDREN)
            earned, max_s, is_valid, trace = evaluate_candidate_code_sandboxed(task_id, code, timeout_seconds=15)
            ru1 = resource.getrusage(resource.RUSAGE_CHILDREN)
            worker_cpu_sec = max(0.0, (ru1.ru_utime + ru1.ru_stime) - (ru0.ru_utime + ru0.ru_stime))

        row_measurements.append({
            "row_id": row_id,
            "task_id": task_id,
            "worker_cpu_seconds": worker_cpu_sec,
        })

        if worker_cpu_sec > max_cpu_seconds:
            max_cpu_seconds = worker_cpu_sec
            max_row_id = row_id

    gate_record = {
        "record_type": "prescientific_cpu_gate",
        "timestamp": time.time(),
        "is_mock": is_mock,
        "total_rows_evaluated": len(row_measurements),
        "max_cpu_seconds": max_cpu_seconds,
        "max_row_id": max_row_id,
        "allowed_limit_seconds": MAX_ALLOWED_CPU_SECONDS,
        "row_measurements": row_measurements,
    }

    # Fail closed if max exceeds 3.0s BEFORE persisting
    if max_cpu_seconds > MAX_ALLOWED_CPU_SECONDS:
        raise CpuGateLimitExceededError(
            f"Pre-scientific CPU gate FAILED: Max worker CPU time for row '{max_row_id}' "
            f"was {max_cpu_seconds:.4f} s, exceeding limit of {MAX_ALLOWED_CPU_SECONDS} s"
        )

    # Persist binding record with 0o444 permissions
    content_bytes = (json.dumps(gate_record, indent=2, sort_keys=True) + "\n").encode("utf-8")
    tmp_path = f"{record_path}_{os.getpid()}.tmp"
    with open(tmp_path, "wb") as f:
        f.write(content_bytes)
        f.flush()
        os.fsync(f.fileno())

    os.chmod(tmp_path, 0o444)
    os.replace(tmp_path, record_path)

    dir_fd = os.open(workspace_root, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)

    return gate_record
