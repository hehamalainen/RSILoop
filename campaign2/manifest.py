"""
Campaign 2 Manifest & Evidence Integrity Verification Module.

Provides preflight manifest generation and verification for 128 Git-tracked Category 1 files,
sealed evidence manifest verification (4 required files), and strict read-only parity matrix verification.
"""

import os
import sys
import json
import stat
import hashlib
from typing import Dict, List, Tuple, Any

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREFLIGHT_MANIFEST_PATH = os.path.join(BASE_DIR, "campaign2_preflight_hashes.json")
EVIDENCE_MANIFEST_PATH = os.path.join(BASE_DIR, "campaign2_evidence_hashes.json")
PARITY_MATRIX_PATH = os.path.join(BASE_DIR, "campaign2_parity_matrix.json")

EXPECTED_SUITE_C_TASK_IDS = [
    "c_f1_01_log_stream_parser", "c_f1_02_json_tokenizer_recovery", "c_f1_03_csv_record_recovery",
    "c_f1_04_http_chunked_decoder", "c_f1_05_protobuf_varint_decoder",
    "c_f2_01_dag_task_scheduler", "c_f2_02_resource_limit_planner", "c_f2_03_build_dependency_graph",
    "c_f2_04_pipeline_stage_scheduler", "c_f2_05_package_version_resolver",
    "c_f3_01_lru_ttl_cache", "c_f3_02_lfu_serialized_cache", "c_f3_03_write_through_cache",
    "c_f3_04_segmented_2tier_cache", "c_f3_05_two_queue_cache", "c_f3_06_tiered_storage_store",
    "c_f4_01_raft_leader_election", "c_f4_02_tcp_state_machine", "c_f4_03_oauth2_flow_simulator",
    "c_f4_04_websocket_frame_fsm", "c_f4_05_saga_orchestrator_fsm", "c_f4_06_dtls_handshake_fsm"
]

EXPECTED_SUITE_P_TASK_IDS = [
    "p_f1_01_ndjson_stream_parser", "p_f1_02_syslog_framing_decoder",
    "p_f2_01_workflow_execution_engine", "p_f2_02_k8s_pod_scheduler",
    "p_f3_01_arc_adaptive_cache", "p_f3_02_ring_buffer_cache",
    "p_f4_01_bgp_peer_fsm", "p_f4_02_sip_call_flow_fsm"
]

ALL_30_TASK_IDS = sorted(EXPECTED_SUITE_C_TASK_IDS + EXPECTED_SUITE_P_TASK_IDS)


def compute_file_sha256(filepath: str) -> str:
    """Computes SHA256 digest of a file in 64KB chunks."""
    sha = hashlib.sha256()
    with open(filepath, "rb") as f:
        while chunk := f.read(65536):
            sha.update(chunk)
    return sha.hexdigest()


def _verify_exact_task_directories(workspace_root: str = BASE_DIR) -> None:
    """Verifies that actual benchmark task directories on disk match expected task IDs exactly."""
    bench_dir = os.path.join(workspace_root, "benchmarks", "campaign2")

    suite_c_dir = os.path.join(bench_dir, "suite_c")
    actual_c = set()
    if os.path.exists(suite_c_dir):
        actual_c = {d for d in os.listdir(suite_c_dir) if os.path.isdir(os.path.join(suite_c_dir, d))}
    expected_c = set(EXPECTED_SUITE_C_TASK_IDS)
    if actual_c != expected_c:
        extra = sorted(actual_c - expected_c)
        missing = sorted(expected_c - actual_c)
        if extra:
            raise ValueError(f"Unauthorized extra task directory found in suite_c: {extra}")
        if missing:
            raise ValueError(f"Missing expected task directory in suite_c: {missing}")

    suite_p_dir = os.path.join(bench_dir, "suite_p")
    actual_p = set()
    if os.path.exists(suite_p_dir):
        actual_p = {d for d in os.listdir(suite_p_dir) if os.path.isdir(os.path.join(suite_p_dir, d))}
    expected_p = set(EXPECTED_SUITE_P_TASK_IDS)
    if actual_p != expected_p:
        extra = sorted(actual_p - expected_p)
        missing = sorted(expected_p - actual_p)
        if extra:
            raise ValueError(f"Unauthorized extra task directory found in suite_p: {extra}")
        if missing:
            raise ValueError(f"Missing expected task directory in suite_p: {missing}")


def get_expected_frozen_inventory(workspace_root: str = BASE_DIR) -> List[str]:
    """
    Returns list of exact 128 Category 1 Git-tracked files comprising frozen Campaign 2 benchmark & harness.
    Derived exclusively from hardcoded task IDs — no filesystem discovery.
    """
    inventory = [
        "README.md",
        "run_tests.py",
        "docs/walkthrough.md",
        "campaign2/proxy.py",
        "campaign2/manifest.py",
        "campaign2/tier_assignment.py",
        "campaign2/scorer.py",
        "campaign2/defects.py"
    ]

    # Derive benchmark file inventory exclusively from hardcoded task IDs
    for task_id in ALL_30_TASK_IDS:
        if task_id in EXPECTED_SUITE_C_TASK_IDS:
            suite = "suite_c"
        elif task_id in EXPECTED_SUITE_P_TASK_IDS:
            suite = "suite_p"
        else:
            raise ValueError(f"Task ID '{task_id}' not found in any expected suite")
        for fname in ["problem.txt", "solution.py", "test_harness.py", "defects.py"]:
            inventory.append(os.path.join("benchmarks", "campaign2", suite, task_id, fname))

    return sorted(inventory)


def generate_campaign2_manifest(workspace_root: str = BASE_DIR) -> str:
    """Generates and writes sealed campaign2_preflight_hashes.json with mode 0o444."""
    _verify_exact_task_directories(workspace_root)
    inventory = get_expected_frozen_inventory(workspace_root)

    manifest_data = {}
    for rel_path in inventory:
        abs_path = os.path.join(workspace_root, rel_path)
        if not os.path.exists(abs_path):
            raise FileNotFoundError(f"Missing required Category 1 file for manifest: {rel_path}")
        manifest_data[rel_path] = compute_file_sha256(abs_path)

    out_path = os.path.join(workspace_root, "campaign2_preflight_hashes.json")
    if os.path.exists(out_path):
        os.chmod(out_path, 0o644)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest_data, f, indent=2, sort_keys=True)

    os.chmod(out_path, 0o444)
    return out_path


def _check_no_symlink_components(workspace_root: str, target_path: str) -> None:
    """
    Verifies that target_path and every parent directory component from workspace_root down to target_path is not a symlink.
    """
    if os.path.islink(target_path):
        raise ValueError(f"Symlink rejected at target path: {target_path}")

    try:
        rel = os.path.relpath(target_path, workspace_root)
    except ValueError:
        return

    if rel.startswith(".."):
        return

    curr = workspace_root
    for part in rel.split(os.sep):
        if not part or part == ".":
            continue
        curr = os.path.join(curr, part)
        if os.path.islink(curr):
            raise ValueError(f"Symlink rejected in parent component path: {curr}")


def verify_campaign2_manifest(workspace_root: str = BASE_DIR) -> Tuple[bool, int]:
    """
    STRICTLY READ-ONLY Verification of Preflight Manifest.
    Verifies that campaign2_preflight_hashes.json exists, is a regular file with mode 0o444,
    no path component is a symlink, contains all 128 Category 1 files, and every file SHA256 matches.
    """
    _verify_exact_task_directories(workspace_root)

    manifest_path = os.path.join(workspace_root, "campaign2_preflight_hashes.json")
    if not os.path.exists(manifest_path):
        raise FileNotFoundError(f"Missing preflight manifest: {manifest_path}")

    _check_no_symlink_components(workspace_root, manifest_path)

    if not os.path.isfile(manifest_path):
        raise ValueError(f"Preflight manifest must be a regular file: {manifest_path}")

    st_mode = stat.S_IMODE(os.stat(manifest_path).st_mode)
    if st_mode != 0o444:
        raise ValueError(f"Preflight manifest permission mode must be 0o444, got {oct(st_mode)}")

    with open(manifest_path, "r", encoding="utf-8") as f:
        try:
            manifest_data = json.load(f)
        except Exception as e:
            raise ValueError(f"Invalid JSON in preflight manifest: {e}")

    expected_inventory = get_expected_frozen_inventory(workspace_root)
    if len(manifest_data) != len(expected_inventory):
        raise ValueError(f"Manifest entry count mismatch: expected {len(expected_inventory)}, got {len(manifest_data)}")

    for rel_path in expected_inventory:
        if rel_path not in manifest_data:
            raise ValueError(f"Missing entry in preflight manifest: {rel_path}")

        abs_path = os.path.join(workspace_root, rel_path)

        # Check symlink components for EVERY authenticated file path
        _check_no_symlink_components(workspace_root, abs_path)

        if not os.path.exists(abs_path):
            raise FileNotFoundError(f"Missing file on disk: {rel_path}")

        expected_hash = manifest_data[rel_path]
        actual_hash = compute_file_sha256(abs_path)
        if actual_hash != expected_hash:
            raise ValueError(f"Hash mismatch for {rel_path}: expected {expected_hash}, got {actual_hash}")

    return True, len(manifest_data)


def seal_campaign2_evidence_files(workspace_root: str = BASE_DIR):
    """
    Sets 0o444 read-only permissions on evidence files. Mode setting ONLY.
    Does NOT write or overwrite content.
    """
    target_files = [
        "campaign2_parity_matrix.json",
        "campaign2_tier_assignment.json",
        "campaign2_archive/proxy_calibration_qwen2_5_coder_0_5b_rejected.json",
        "campaign2_archive/proxy_config_gpt_oss_unexecuted.json"
    ]
    for rel in target_files:
        abs_p = os.path.join(workspace_root, rel)
        if os.path.exists(abs_p) and not os.path.islink(abs_p):
            os.chmod(abs_p, 0o444)


def generate_campaign2_evidence_manifest(workspace_root: str = BASE_DIR) -> str:
    """Generates and writes sealed campaign2_evidence_hashes.json with mode 0o444."""
    target_files = [
        "campaign2_parity_matrix.json",
        "campaign2_tier_assignment.json",
        "campaign2_archive/proxy_calibration_qwen2_5_coder_0_5b_rejected.json",
        "campaign2_archive/proxy_config_gpt_oss_unexecuted.json"
    ]

    manifest_data = {}
    for rel in sorted(target_files):
        abs_p = os.path.join(workspace_root, rel)
        if not os.path.exists(abs_p):
            raise FileNotFoundError(f"Missing required evidence file: {rel}")
        manifest_data[rel] = compute_file_sha256(abs_p)

    out_path = os.path.join(workspace_root, "campaign2_evidence_hashes.json")
    if os.path.exists(out_path):
        os.chmod(out_path, 0o644)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(manifest_data, f, indent=2, sort_keys=True)

    os.chmod(out_path, 0o444)
    return out_path


def verify_campaign2_evidence_manifest(workspace_root: str = BASE_DIR) -> bool:
    """
    STRICTLY READ-ONLY Verification of Evidence Manifest.
    Checks file permissions (0o444), regular file type, parent symlinks, and verifies hashes of all 4 evidence files.
    """
    manifest_path = os.path.join(workspace_root, "campaign2_evidence_hashes.json")
    if not os.path.exists(manifest_path):
        raise FileNotFoundError(f"Missing evidence manifest: {manifest_path}")

    _check_no_symlink_components(workspace_root, manifest_path)

    if not os.path.isfile(manifest_path):
        raise ValueError(f"Evidence manifest must be a regular file: {manifest_path}")

    st_mode = stat.S_IMODE(os.stat(manifest_path).st_mode)
    if st_mode != 0o444:
        raise ValueError(f"Evidence manifest permission mode must be 0o444, got {oct(st_mode)}")

    with open(manifest_path, "r", encoding="utf-8") as f:
        try:
            manifest_data = json.load(f)
        except Exception as e:
            raise ValueError(f"Invalid JSON in evidence manifest: {e}")

    expected_files = [
        "campaign2_parity_matrix.json",
        "campaign2_tier_assignment.json",
        "campaign2_archive/proxy_calibration_qwen2_5_coder_0_5b_rejected.json",
        "campaign2_archive/proxy_config_gpt_oss_unexecuted.json"
    ]

    if len(manifest_data) != len(expected_files):
        raise ValueError(f"Evidence manifest count mismatch: expected {len(expected_files)}, got {len(manifest_data)}")

    for rel in expected_files:
        if rel not in manifest_data:
            raise ValueError(f"Missing entry in evidence manifest: {rel}")

        abs_p = os.path.join(workspace_root, rel)
        if not os.path.exists(abs_p):
            raise FileNotFoundError(f"Missing evidence file on disk: {rel}")

        _check_no_symlink_components(workspace_root, abs_p)

        file_mode = stat.S_IMODE(os.stat(abs_p).st_mode)
        if file_mode != 0o444:
            raise ValueError(f"Evidence file {rel} permission mode must be 0o444, got {oct(file_mode)}")

        expected_hash = manifest_data[rel]
        actual_hash = compute_file_sha256(abs_p)
        if actual_hash != expected_hash:
            raise ValueError(f"Evidence hash mismatch for {rel}: expected {expected_hash}, got {actual_hash}")

    return True


def compute_campaign2_parity_matrix(workspace_root: str = BASE_DIR) -> Dict[str, Any]:
    """
    Pure, deterministic function to compute the complete 120-row Campaign 2 Parity Matrix.
    Recomputes SHA256 hashes, direct evaluation scores, and sandboxed IPC evaluation scores for all 120 rows.
    """
    from campaign2.defects import load_task_module
    from campaign2.proxy import evaluate_candidate_code_sandboxed

    bench_dir = os.path.join(workspace_root, "benchmarks", "campaign2")
    matrix = {}

    all_tasks = []
    for suite in ["suite_c", "suite_p"]:
        s_dir = os.path.join(bench_dir, suite)
        if os.path.exists(s_dir):
            for t_id in sorted(os.listdir(s_dir)):
                if os.path.isdir(os.path.join(s_dir, t_id)):
                    all_tasks.append((suite, t_id, os.path.join(s_dir, t_id)))

    for suite, task_id, task_dir in all_tasks:
        sol_path = os.path.join(task_dir, "solution.py")
        harness_path = os.path.join(task_dir, "test_harness.py")
        defects_path = os.path.join(task_dir, "defects.py")

        with open(sol_path, "r", encoding="utf-8") as f:
            sol_code = f.read()
        with open(harness_path, "r", encoding="utf-8") as f:
            harness_code = f.read()
        with open(defects_path, "r", encoding="utf-8") as f:
            def_code = f.read()

        harness_hash = hashlib.sha256(harness_code.encode("utf-8")).hexdigest()
        ref_code_hash = hashlib.sha256(sol_code.encode("utf-8")).hexdigest()

        sol_mod = load_task_module(task_dir, "solution")
        harness_mod = load_task_module(task_dir, "test_harness")
        defects_mod = load_task_module(task_dir, "defects")

        # 1. Reference solution
        direct_ref_earned, direct_ref_max = harness_mod.run_evaluation(sol_mod)
        ipc_ref_earned, ipc_ref_max = evaluate_candidate_code_sandboxed(task_id, sol_code)
        if (direct_ref_earned, direct_ref_max) != (10, 10) or (ipc_ref_earned, ipc_ref_max) != (10, 10):
            raise RuntimeError(f"Reference solution for {task_id} failed 10/10 check")

        matrix[f"ref_{task_id}"] = {
            "task_id": task_id,
            "type": "reference",
            "rung": 0,
            "variant": "reference_solution",
            "code_hash": ref_code_hash,
            "harness_hash": harness_hash,
            "direct_earned_score": direct_ref_earned,
            "direct_max_score": direct_ref_max,
            "ipc_earned_score": ipc_ref_earned,
            "ipc_max_score": ipc_ref_max,
            "parity_verified": True
        }

        # 2. Defect rungs 1, 2, 3
        callable_names = [attr for attr in dir(sol_mod) if not attr.startswith("__") and callable(getattr(sol_mod, attr)) and hasattr(getattr(sol_mod, attr), "__name__")]
        defect_ladder = getattr(defects_mod, "DEFECT_LADDER", [])

        for idx, rung_fn in enumerate(defect_ladder[:3], start=1):
            class DummyModule: pass
            d_mod = DummyModule()
            for attr_name in dir(sol_mod):
                if not attr_name.startswith("__"):
                    val = getattr(sol_mod, attr_name)
                    if callable(val) and hasattr(val, "__name__"):
                        setattr(d_mod, attr_name, rung_fn)
                    else:
                        setattr(d_mod, attr_name, val)

            direct_def_earned, direct_def_max = harness_mod.run_evaluation(d_mod)

            aliases = "\n".join([f"{name} = {rung_fn.__name__}" for name in callable_names])
            cand_code = sol_code + "\n\n" + def_code + "\n\n" + aliases + "\n"
            def_code_hash = hashlib.sha256(cand_code.encode("utf-8")).hexdigest()

            ipc_def_earned, ipc_def_max = evaluate_candidate_code_sandboxed(task_id, cand_code)
            if (direct_def_earned, direct_def_max) != (ipc_def_earned, ipc_def_max):
                raise RuntimeError(f"Defect rung {idx} for {task_id} score mismatch: direct={direct_def_earned}, ipc={ipc_def_earned}")

            matrix[f"defect_{task_id}_rung_{idx}"] = {
                "task_id": task_id,
                "type": "defect",
                "rung": idx,
                "variant": f"defect_rung_{idx}",
                "code_hash": def_code_hash,
                "harness_hash": harness_hash,
                "direct_earned_score": direct_def_earned,
                "direct_max_score": direct_def_max,
                "ipc_earned_score": ipc_def_earned,
                "ipc_max_score": ipc_def_max,
                "parity_verified": True
            }

    return matrix


def generate_campaign2_parity_matrix(workspace_root: str = BASE_DIR) -> str:
    """
    Generates sealed campaign2_parity_matrix.json with mode 0o444 using pure compute_campaign2_parity_matrix.
    """
    matrix = compute_campaign2_parity_matrix(workspace_root)

    out_path = os.path.join(workspace_root, "campaign2_parity_matrix.json")
    if os.path.exists(out_path):
        os.chmod(out_path, 0o644)

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(matrix, f, indent=2, sort_keys=True)

    os.chmod(out_path, 0o444)
    return out_path


def verify_campaign2_parity_matrix(workspace_root: str = BASE_DIR) -> bool:
    """
    STRICTLY READ-ONLY Verification of Sealed Parity Matrix (campaign2_parity_matrix.json).
    Independently recomputes the complete 120-row expected matrix using compute_campaign2_parity_matrix,
    and requires exact dictionary equality with the sealed artifact on disk (verifying all keys, order,
    SHA256 hashes, direct scores, IPC scores, task identity, and permissions 0o444).
    """
    matrix_path = os.path.join(workspace_root, "campaign2_parity_matrix.json")
    if not os.path.exists(matrix_path):
        raise FileNotFoundError(f"Missing parity matrix artifact: {matrix_path}")

    _check_no_symlink_components(workspace_root, matrix_path)

    if not os.path.isfile(matrix_path):
        raise ValueError(f"Parity matrix artifact must be a regular file: {matrix_path}")

    st_mode = stat.S_IMODE(os.stat(matrix_path).st_mode)
    if st_mode != 0o444:
        raise ValueError(f"Parity matrix permission mode must be 0o444, got {oct(st_mode)}")

    with open(matrix_path, "r", encoding="utf-8") as f:
        try:
            sealed_matrix = json.load(f)
        except Exception as e:
            raise ValueError(f"Invalid JSON in parity matrix: {e}")

    if not isinstance(sealed_matrix, dict):
        raise ValueError("Parity matrix JSON must be a dictionary")

    if len(sealed_matrix) != 120:
        raise ValueError(f"Parity matrix must contain EXACTLY 120 rows, got {len(sealed_matrix)}")

    # Independently recompute complete expected structure
    expected_matrix = compute_campaign2_parity_matrix(workspace_root)

    if sealed_matrix != expected_matrix:
        # Pinpoint discrepancy for clear error diagnostic
        for k in expected_matrix:
            if k not in sealed_matrix:
                raise ValueError(f"Sealed parity matrix missing key: '{k}'")
            if sealed_matrix[k] != expected_matrix[k]:
                raise ValueError(f"Sealed parity matrix mismatch for key '{k}': expected {expected_matrix[k]}, got {sealed_matrix[k]}")
        for k in sealed_matrix:
            if k not in expected_matrix:
                raise ValueError(f"Sealed parity matrix contains unauthorized extra key: '{k}'")
        raise ValueError("Sealed parity matrix does not equal independently recomputed matrix")

    return True


if __name__ == "__main__":
    generate_campaign2_parity_matrix()
    generate_campaign2_manifest()
    generate_campaign2_evidence_manifest()
    verify_campaign2_manifest()
    verify_campaign2_evidence_manifest()
    verify_campaign2_parity_matrix()
    print("Campaign 2 preflight and evidence manifests verified successfully.")
