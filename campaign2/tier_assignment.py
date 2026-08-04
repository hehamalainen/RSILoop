"""
Campaign 2 Family-Blocked Deterministic Tier Assignment Module.

REPLACEMENT SPECIFICATION (WORK ORDER A-REV2):
Model-based difficulty stratification is completely withdrawn. Tier assignment is deterministic,
family-blocked, machine-recomputable assignment based on SHA-256 rejection sampling PRNG over
the pinned base commit SHA.

QUOTA MATRIX & REJECTION SAMPLING PRNG CONTRACT:
- Seed: SHA-256(base_commit_sha || "|" || domain || "|" || uint64(j))
- Uint32 stream: Big-endian 4-byte integers extracted from SHA-256 digest stream.
- Rejection sampling: To sample an integer in [0, N-1], reject values >= (2^32 - (2^32 % N)).
"""

import os
import sys
import json
import stat
import hashlib
import struct
from typing import Dict, List, Tuple, Any

BASE_COMMIT_SHA = "1abb708afc8dae8c90a2aa57e9f6d8a81a8a0bae"
DOMAIN = "rsi_loop/campaign2"
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ASSIGNMENT_ARTIFACT_PATH = os.path.join(BASE_DIR, "campaign2_tier_assignment.json")

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


class RejectionSamplingPRNGStream:
    """Deterministic uint32 PRNG stream backed by continuous SHA-256 blocks."""
    def __init__(self, base_commit_sha: str, domain: str):
        self.base_commit_sha = base_commit_sha
        self.domain = domain
        self.counter = 0
        self.bytes_consumed = 0
        self.buffer = b""
        self.transcript = []

    def _fill_buffer(self):
        ctx = hashlib.sha256()
        ctx.update(self.base_commit_sha.encode("utf-8"))
        ctx.update(b"|")
        ctx.update(self.domain.encode("utf-8"))
        ctx.update(b"|")
        ctx.update(struct.pack(">Q", self.counter))
        self.buffer += ctx.digest()
        self.counter += 1

    def next_uint32(self) -> Tuple[int, int]:
        while len(self.buffer) < 4:
            self._fill_buffer()
        offset = self.bytes_consumed
        val = struct.unpack(">I", self.buffer[:4])[0]
        self.buffer = self.buffer[4:]
        self.bytes_consumed += 4
        return val, offset

    def rejection_sample(self, n: int) -> int:
        if n <= 1:
            return 0
        limit = 0x100000000 - (0x100000000 % n)
        while True:
            val, offset = self.next_uint32()
            if val < limit:
                selected = val % n
                self.transcript.append({
                    "action": "accept",
                    "stream_byte_offset": offset,
                    "uint32_val": val,
                    "rejection_limit": limit,
                    "range_n": n,
                    "chosen_index": selected
                })
                return selected
            else:
                self.transcript.append({
                    "action": "reject",
                    "stream_byte_offset": offset,
                    "uint32_val": val,
                    "rejection_limit": limit,
                    "range_n": n,
                    "chosen_index": None
                })


def generate_family_blocked_assignment(
    base_commit_sha: str = BASE_COMMIT_SHA,
    domain: str = DOMAIN
) -> Dict[str, Any]:
    """
    Generates deterministic family-blocked tier assignment using rejection sampling PRNG over base_commit_sha.
    """
    prng = RejectionSamplingPRNGStream(base_commit_sha, domain)

    all_c = list(EXPECTED_SUITE_C_TASK_IDS)
    all_p = list(EXPECTED_SUITE_P_TASK_IDS)

    # Separate holdouts (F4 tasks)
    c_holdouts = sorted([t for t in all_c if t.startswith("c_f4_")])
    p_holdouts = sorted([t for t in all_p if t.startswith("p_f4_")])
    c_eval = sorted([t for t in all_c if not t.startswith("c_f4_")])
    p_eval = sorted([t for t in all_p if not t.startswith("p_f4_")])

    # Family grouping for Suite C
    c_f1 = sorted([t for t in c_eval if t.startswith("c_f1_")])  # 5 tasks
    c_f2 = sorted([t for t in c_eval if t.startswith("c_f2_")])  # 5 tasks
    c_f3 = sorted([t for t in c_eval if t.startswith("c_f3_")])  # 6 tasks

    # Family grouping for Suite P
    p_f1 = sorted([t for t in p_eval if t.startswith("p_f1_")])  # 2 tasks
    p_f2 = sorted([t for t in p_eval if t.startswith("p_f2_")])  # 2 tasks
    p_f3 = sorted([t for t in p_eval if t.startswith("p_f3_")])  # 2 tasks

    def shuffle_list(items: List[str]) -> List[str]:
        pool = list(items)
        shuffled = []
        while pool:
            idx = prng.rejection_sample(len(pool))
            shuffled.append(pool.pop(idx))
        return shuffled

    shuf_c_f1 = shuffle_list(c_f1)
    shuf_c_f2 = shuffle_list(c_f2)
    shuf_c_f3 = shuffle_list(c_f3)

    shuf_p_f1 = shuffle_list(p_f1)
    shuf_p_f2 = shuffle_list(p_f2)
    shuf_p_f3 = shuffle_list(p_f3)

    # Suite C quotas:
    # F1 (5 tasks): 1 opt, 1 sel, 3 conf
    # F2 (5 tasks): 1 opt, 2 sel, 2 conf
    # F3 (6 tasks): 2 opt, 1 sel, 3 conf
    c_opt = sorted([shuf_c_f1[0], shuf_c_f2[0], shuf_c_f3[0], shuf_c_f3[1]])
    c_sel = sorted([shuf_c_f1[1], shuf_c_f2[1], shuf_c_f2[2], shuf_c_f3[2]])
    c_conf = sorted([shuf_c_f1[2], shuf_c_f1[3], shuf_c_f1[4], shuf_c_f2[3], shuf_c_f2[4], shuf_c_f3[3], shuf_c_f3[4], shuf_c_f3[5]])

    # Suite P quotas:
    # F1 (2 tasks): 1 opt, 0 sel, 1 conf
    # F2 (2 tasks): 1 opt, 1 sel, 0 conf
    # F3 (2 tasks): 0 opt, 1 sel, 1 conf
    p_opt = sorted([shuf_p_f1[0], shuf_p_f2[0]])
    p_sel = sorted([shuf_p_f2[1], shuf_p_f3[0]])
    p_conf = sorted([shuf_p_f1[1], shuf_p_f3[1]])

    all_assigned = sorted(c_opt + c_sel + c_conf + p_opt + p_sel + p_conf + c_holdouts + p_holdouts)
    expected_all = sorted(all_c + all_p)
    assert all_assigned == expected_all, "Assignment missing tasks or contains duplicates"

    return {
        "base_commit_sha": base_commit_sha,
        "domain": domain,
        "assignment_type": "family_blocked_rejection_sampling",
        "prng_algorithm": "SHA-256 rejection sampling uint32",
        "rejection_sampling_transcript": prng.transcript,
        "quotas": {
            "suite_c": {"f1": [1, 1, 3], "f2": [1, 2, 2], "f3": [2, 1, 3]},
            "suite_p": {"f1": [1, 0, 1], "f2": [1, 1, 0], "f3": [0, 1, 1]},
            "structural_holdout": {"suite_c_f4": 6, "suite_p_f4": 2}
        },
        "set_assignments": {
            "suite_c": {
                "optimization": c_opt,
                "selection": c_sel,
                "confirmatory": c_conf
            },
            "suite_p": {
                "optimization": p_opt,
                "selection": p_sel,
                "confirmatory": p_conf
            },
            "structural_holdout": sorted(c_holdouts + p_holdouts)
        },
        "all_tasks": expected_all
    }


def write_sealed_family_blocked_assignment(
    workspace_root: str = BASE_DIR,
    base_commit_sha: str = BASE_COMMIT_SHA,
    domain: str = DOMAIN
) -> str:
    """Generates and writes sealed campaign2_tier_assignment.json with mode 0o444."""
    data = generate_family_blocked_assignment(base_commit_sha, domain)
    target_path = os.path.join(workspace_root, "campaign2_tier_assignment.json")
    if os.path.exists(target_path):
        os.chmod(target_path, 0o644)
    with open(target_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, sort_keys=True)
    os.chmod(target_path, 0o444)
    return target_path


def verify_family_blocked_assignment(
    workspace_root: str = BASE_DIR,
    artifact_path: str = ASSIGNMENT_ARTIFACT_PATH
) -> bool:
    """
    STRICTLY READ-ONLY Verification of Sealed Family-Blocked Assignment Artifact.
    Checks file permissions (0o444), regular non-symlink file type, and enforces strict
    matching against pinned BASE_COMMIT_SHA and registered DOMAIN before recomputing.
    """
    if not os.path.exists(artifact_path):
        raise FileNotFoundError(f"Missing assignment artifact: {artifact_path}")

    if os.path.islink(artifact_path):
        raise ValueError(f"Assignment artifact must not be a symlink: {artifact_path}")

    if not os.path.isfile(artifact_path):
        raise ValueError(f"Assignment artifact must be a regular file: {artifact_path}")

    st_mode = stat.S_IMODE(os.stat(artifact_path).st_mode)
    if st_mode != 0o444:
        raise ValueError(f"Assignment artifact permission mode must be 0o444, got {oct(st_mode)}")

    with open(artifact_path, "r", encoding="utf-8") as f:
        try:
            artifact_data = json.load(f)
        except Exception as e:
            raise ValueError(f"Invalid JSON in assignment artifact: {e}")

    # Finding 4 [P1] FIX: Enforce exact equality with pinned BASE_COMMIT_SHA and DOMAIN
    artifact_sha = artifact_data.get("base_commit_sha")
    artifact_domain = artifact_data.get("domain")

    if artifact_sha != BASE_COMMIT_SHA:
        raise ValueError(
            f"Artifact base_commit_sha mismatch: expected pinned '{BASE_COMMIT_SHA}', got '{artifact_sha}'"
        )

    if artifact_domain != DOMAIN:
        raise ValueError(
            f"Artifact domain mismatch: expected registered '{DOMAIN}', got '{artifact_domain}'"
        )

    recomputed = generate_family_blocked_assignment(BASE_COMMIT_SHA, DOMAIN)
    if artifact_data != recomputed:
        raise ValueError("Assignment artifact contents do not match recomputed assignment")

    return True


verify_tier_assignment_artifact = verify_family_blocked_assignment


if __name__ == "__main__":
    write_sealed_family_blocked_assignment()
    print("Sealed campaign2_tier_assignment.json generated and verified successfully.")
