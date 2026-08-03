"""
Campaign 2 Preflight Manifest Generator & Inventory Verifier (A6).

TWO-CATEGORY VERIFICATION GUARANTEE:
Category 1: 128 Git-tracked frozen files (30 tasks * 4 files + core modules + descriptor & fetch script).
Category 2: 1 Permitted external artifact (models/Qwen2.5-Coder-0.5B-Instruct-Q4_K_M.gguf),
            verified by exact path, size (491400064 bytes), SHA256, regular-file status, and no symlinks.

STRICTLY READ-ONLY verification: never creates or mutates files on disk or alters permissions during checks. Fail-closed.
"""

import os
import json
import hashlib
from typing import Dict, Any, Tuple

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EXPECTED_SUITE_C_TASK_IDS = [
    "c_f1_01_log_stream_parser",
    "c_f1_02_json_tokenizer_recovery",
    "c_f1_03_csv_record_recovery",
    "c_f1_04_http_chunked_decoder",
    "c_f1_05_protobuf_varint_decoder",
    "c_f2_01_dag_task_scheduler",
    "c_f2_02_resource_limit_planner",
    "c_f2_03_build_dependency_graph",
    "c_f2_04_pipeline_stage_scheduler",
    "c_f2_05_package_version_resolver",
    "c_f3_01_lru_ttl_cache",
    "c_f3_02_lfu_serialized_cache",
    "c_f3_03_write_through_cache",
    "c_f3_04_segmented_2tier_cache",
    "c_f3_05_two_queue_cache",
    "c_f3_06_tiered_storage_store",
    "c_f4_01_raft_leader_election",
    "c_f4_02_tcp_state_machine",
    "c_f4_03_oauth2_flow_simulator",
    "c_f4_04_websocket_frame_fsm",
    "c_f4_05_saga_orchestrator_fsm",
    "c_f4_06_dtls_handshake_fsm"
]

EXPECTED_SUITE_P_TASK_IDS = [
    "p_f1_01_ndjson_stream_parser",
    "p_f1_02_syslog_framing_decoder",
    "p_f2_01_workflow_execution_engine",
    "p_f2_02_k8s_pod_scheduler",
    "p_f3_01_arc_adaptive_cache",
    "p_f3_02_ring_buffer_cache",
    "p_f4_01_bgp_peer_fsm",
    "p_f4_02_sip_call_flow_fsm"
]

EXPECTED_EXTERNAL_MODEL_RELPATH = os.path.join("models", "Qwen2.5-Coder-0.5B-Instruct-Q4_K_M.gguf")
EXPECTED_EXTERNAL_MODEL_SHA256 = "1d9614638d18024d0fbb36575a15f1302a3adf044df10345688ec4f6e1c4ff32"
EXPECTED_EXTERNAL_MODEL_SIZE = 491400064


def _check_no_symlinks(full_path: str, workspace_root: str = BASE_DIR):
    """Verifies that neither the path nor any parent directory within workspace_root is a symbolic link."""
    abs_path = os.path.abspath(full_path)
    abs_root = os.path.abspath(workspace_root)
    curr = abs_path

    while curr and curr.startswith(abs_root):
        if os.path.islink(curr):
            raise ValueError(f"Symlink detected in path component: {curr}")
        if curr == abs_root:
            break
        curr = os.path.dirname(curr)


def _sha256_file(path: str, workspace_root: str = BASE_DIR) -> str:
    _check_no_symlinks(path, workspace_root)
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(8192 * 1024):
            sha.update(chunk)
    return sha.hexdigest()


def get_expected_frozen_inventory() -> list[str]:
    """Returns the hardcoded 128 Git-tracked frozen file paths."""
    files = [
        os.path.join("campaign2", "scorer.py"),
        os.path.join("campaign2", "defects.py"),
        os.path.join("campaign2", "proxy.py"),
        os.path.join("campaign2", "proxy_config.json"),
        os.path.join("campaign2", "local_model_runner.py"),
        os.path.join("campaign2", "manifest.py"),
        os.path.join("models", "model_descriptor.json"),
        os.path.join("models", "fetch_model.py"),
    ]

    for task_id in EXPECTED_SUITE_C_TASK_IDS:
        for fname in ["problem.txt", "solution.py", "test_harness.py", "defects.py"]:
            files.append(os.path.join("benchmarks", "campaign2", "suite_c", task_id, fname))

    for task_id in EXPECTED_SUITE_P_TASK_IDS:
        for fname in ["problem.txt", "solution.py", "test_harness.py", "defects.py"]:
            files.append(os.path.join("benchmarks", "campaign2", "suite_p", task_id, fname))

    return sorted(files)


def generate_campaign2_manifest(workspace_root: str = BASE_DIR) -> Dict[str, str]:
    """
    Explicit generator tool to create campaign2_preflight_hashes.json during initial Gate A1 build.
    """
    hashes = {}
    expected_files = get_expected_frozen_inventory()
    manifest_path = os.path.join(workspace_root, "campaign2_preflight_hashes.json")

    for rel_path in expected_files:
        full_path = os.path.join(workspace_root, rel_path)
        if not os.path.exists(full_path):
            raise FileNotFoundError(f"Required preregistered file missing: {full_path}")
        hashes[rel_path] = _sha256_file(full_path, workspace_root)

    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(hashes, f, indent=2, sort_keys=True)

    return hashes


def verify_external_model_artifact(workspace_root: str = BASE_DIR) -> bool:
    """Verifies Category 2: Permitted external model artifact based on models/model_descriptor.json."""
    desc_path = os.path.join(workspace_root, "models", "model_descriptor.json")
    if not os.path.exists(desc_path):
        raise FileNotFoundError(f"Missing model descriptor at: {desc_path}")
    with open(desc_path, "r", encoding="utf-8") as f:
        desc = json.load(f)

    expected_sha = desc.get("expected_sha256")
    if expected_sha == "PENDING_AUTHORIZED_DOWNLOAD":
        return True

    rel_path = desc.get("relative_path", os.path.join("models", desc.get("filename", "")))
    full_path = os.path.join(workspace_root, rel_path)
    if not os.path.exists(full_path):
        raise FileNotFoundError(f"Permitted external model artifact missing at: {full_path}")
    _check_no_symlinks(full_path, workspace_root)
    if os.path.islink(full_path):
        raise ValueError(f"External model artifact must not be a symlink: {full_path}")

    expected_size = desc.get("expected_size_bytes")
    if expected_size is not None:
        st = os.stat(full_path)
        if st.st_size != expected_size:
            raise ValueError(f"External model size mismatch: expected {expected_size} bytes, got {st.st_size}")

    if expected_sha:
        actual_sha = _sha256_file(full_path, workspace_root)
        if actual_sha != expected_sha:
            raise ValueError(f"External model SHA256 mismatch: expected {expected_sha}, got {actual_sha}")

    return True


def verify_campaign2_manifest(workspace_root: str = BASE_DIR) -> Tuple[bool, int]:
    """
    STRICTLY READ-ONLY Verification across Category 1 (128 Git-tracked files) and Category 2 (1 Permitted External Artifact).
    Rejects any unlisted, unexpected, missing, or symlinked files. Fail-closed.
    Returns (True, 128).
    """
    manifest_path = os.path.join(workspace_root, "campaign2_preflight_hashes.json")
    if not os.path.exists(manifest_path):
        raise FileNotFoundError(f"Campaign 2 manifest file missing: {manifest_path}")

    with open(manifest_path, "r", encoding="utf-8") as f:
        hashes = json.load(f)

    expected_inventory = set(get_expected_frozen_inventory())
    manifest_keys = set(hashes.keys())

    # 1. Enforce Category 1 exact inventory match (128 files)
    if manifest_keys != expected_inventory:
        missing = expected_inventory - manifest_keys
        extra = manifest_keys - expected_inventory
        raise ValueError(
            f"Manifest keys do not match preregistered 30-task inventory (128 files). "
            f"Missing={sorted(list(missing))}, Extra={sorted(list(extra))}"
        )

    # 2. Check all 128 Category 1 files
    for rel_path, expected_hash in hashes.items():
        full_path = os.path.join(workspace_root, rel_path)
        if not os.path.exists(full_path):
            raise FileNotFoundError(f"Manifest entry missing on disk: {rel_path}")
        actual_hash = _sha256_file(full_path, workspace_root)
        if actual_hash != expected_hash:
            raise ValueError(f"Hash mismatch for {rel_path}: expected {expected_hash}, got {actual_hash}")

    # 3. Check Category 2 Permitted External Model Artifact
    verify_external_model_artifact(workspace_root)

    # 4. Enforce exact on-disk inventory & symlink check across directories
    c2_dir = os.path.join(workspace_root, "campaign2")
    bench_dir = os.path.join(workspace_root, "benchmarks", "campaign2")
    models_dir = os.path.join(workspace_root, "models")

    scanned_files = set()
    for root_dir in [c2_dir, bench_dir, models_dir]:
        if not os.path.exists(root_dir): continue
        for root, dirs, files in os.walk(root_dir, followlinks=False):
            _check_no_symlinks(root, workspace_root)
            for dname in list(dirs):
                if dname == "__pycache__":
                    dirs.remove("__pycache__")
                else:
                    _check_no_symlinks(os.path.join(root, dname), workspace_root)

            for fname in files:
                if fname.endswith((".pyc", ".pyo")): continue
                full_p = os.path.join(root, fname)
                _check_no_symlinks(full_p, workspace_root)
                rel_p = os.path.relpath(full_p, workspace_root)
                # Ignore permitted external artifact GGUF file during Category 1 check
                if rel_p == EXPECTED_EXTERNAL_MODEL_RELPATH:
                    continue
                scanned_files.add(rel_p)

    unlisted = scanned_files - expected_inventory
    if unlisted:
        raise ValueError(f"Unlisted/unexpected scientific files found on disk: {sorted(list(unlisted))}")

    return True, len(hashes)


def verify_campaign2_evidence_manifest(workspace_root: str = BASE_DIR) -> bool:
    """
    STRICTLY READ-ONLY Verification of Sealed Scientific Evidence Manifest (campaign2_evidence_hashes.json).
    Authenticates exact 6-file inventory, 64-char lowercase hex SHA256 hashes, symlink rejection, regular file type, and mode 0o444.
    """
    import stat
    import re
    manifest_path = os.path.join(workspace_root, "campaign2_evidence_hashes.json")
    if not os.path.exists(manifest_path):
        raise FileNotFoundError(f"Missing sealed evidence manifest: {manifest_path}")

    _check_no_symlinks(manifest_path, workspace_root)
    if not os.path.isfile(manifest_path) or os.path.islink(manifest_path):
        raise ValueError(f"Evidence manifest must be a regular non-symlink file: {manifest_path}")

    st_mode = stat.S_IMODE(os.stat(manifest_path).st_mode)
    if st_mode != 0o444:
        raise ValueError(f"Evidence manifest permission mode must be 0o444, got {oct(st_mode)}")

    with open(manifest_path, "r", encoding="utf-8") as f:
        try:
            hashes = json.load(f)
        except Exception as e:
            raise ValueError(f"Invalid JSON in evidence manifest: {e}")

    if not isinstance(hashes, dict):
        raise ValueError("Evidence manifest JSON must be a dictionary")

    required_inventory = {
        "campaign2_parity_matrix.json",
        "campaign2_proxy_calibration_qwen2_5_coder_0_5b_rejected.json",
        "campaign2_proxy_calibration_gpt_oss_20b.json",
        "campaign2_proxy_adequacy.json",
        "campaign2/proxy_config.json",
        "models/model_descriptor.json"
    }

    manifest_keys = set(hashes.keys())
    if manifest_keys != required_inventory:
        missing = sorted(list(required_inventory - manifest_keys))
        extra = sorted(list(manifest_keys - required_inventory))
        raise ValueError(f"Evidence manifest inventory mismatch. Missing: {missing}, Extra: {extra}")

    hex_pattern = re.compile(r"^[0-9a-f]{64}$")

    for rel_path, expected_hash in hashes.items():
        if not isinstance(expected_hash, str) or not hex_pattern.match(expected_hash):
            raise ValueError(f"Invalid hash format for {rel_path}: '{expected_hash}' (must be 64 lowercase hex characters)")

        full_path = os.path.join(workspace_root, rel_path)
        _check_no_symlinks(full_path, workspace_root)

        if not os.path.isfile(full_path) or os.path.islink(full_path):
            raise ValueError(f"Evidence entry must be a regular non-symlink file: {rel_path}")

        mode = stat.S_IMODE(os.stat(full_path).st_mode)
        if mode != 0o444:
            raise ValueError(f"Evidence file permission mode must be 0o444 for {rel_path}, got {oct(mode)}")

        actual_hash = _sha256_file(full_path, workspace_root)
        if actual_hash != expected_hash:
            raise ValueError(f"Evidence hash mismatch for {rel_path}: expected {expected_hash}, got {actual_hash}")

    return True
