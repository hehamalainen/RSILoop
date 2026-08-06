"""
Campaign 2 Sealed Master Registration & Known Limitations Module.

B2 — SEALED MASTER REGISTRATION:
- Sealed master registration (0o444, canonically serialized JSON)
- Hash stored in a separate anchor file (campaign2_registration_anchor.hash, 0o444)
- Blocks first scientific model call until verified.
- Carries 11 explicit KNOWN LIMITATIONS entries and 4 PRE-REGISTERED EXPLORATORY SECONDARIES.
- Assignment vectors and trio orderings are PRNG-derived with recorded transcripts.
"""

import os
import sys
import json
import stat
import struct
import hashlib
from typing import Dict, Any, List, Tuple

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REGISTRATION_FILE_REL = "campaign2_master_registration.json"
ANCHOR_FILE_REL = "campaign2_registration_anchor.hash"

APPROVED_BASE_COMMIT = "96e04c94ef44b974301bbe9e4fedd2ba4281022d"

KNOWN_LIMITATIONS = [
    "Modest power; ten randomized trial blocks; narrow guided-vs-unguided claim only.",
    "Estimand is the service presented as gpt-5.6-sol during the campaign window; silent provider changes undetectable.",
    "CLI JSONL is strictly parsed and correlation-bound, not cryptographically authenticated.",
    "One-shot enforcement is local, cooperative-operator threat model; the remote anchors definition only.",
    "No hard token or cost bound; post-call ceiling with possible one-call overshoot.",
    "Trial timeouts are accepted-risk thresholds, not worst-case.",
    "Claims cover the eight fixed preregistered confirmatory tasks, not unseen tasks generally.",
    "Tiers balanced by structural task FAMILY, not measured difficulty; families are not difficulty strata.",
    "MacBook memory availability is not a production containment guarantee; accepted for this non-adversarial research campaign.",
    "A candidate that blocks without consuming CPU produces an unattributed wall-clock timeout and invalidates the attempt; phase-based exit attribution does not cover non-exiting processes.",
    "A >= 0.05 is an observed practical gate, not inferential proof of absolute improvement."
]

EXPLORATORY_SECONDARIES = [
    "score-improving mutations per optimizer mutation call",
    "improvement per 1,000 output tokens",
    "evaluable-candidate rate",
    "mutation depth producing each new best"
]

EVALUABLE_CANDIDATE_DEFINITION = {
    "metric_name": "evaluable-candidate rate",
    "numerator": "Number of candidate generation calls where candidate imported cleanly and completed a scored test harness evaluation without process death, worker timeout, or evaluation-aborting infrastructure failure",
    "denominator": "Total candidate generation calls (baseline + optimizer + control)",
    "classification": "Harness-contained candidate runtime exceptions during evaluation remain evaluable (is_valid=True) with bounded error traces recorded for optimizer feedback. Candidate top-level import/syntax failures and attributable process termination (CandidateProcessError) score (0, 10) with is_valid=False. Any live-worker CandidateTimeoutError, unattributed wall-clock timeout, or infrastructure fault invalidates the attempt, not a scored candidate."
}

FROZEN_TRIO_ORDERINGS = [
    ("baseline", "slot_a_winner", "slot_b_winner"),
    ("baseline", "slot_b_winner", "slot_a_winner"),
    ("slot_a_winner", "baseline", "slot_b_winner"),
    ("slot_a_winner", "slot_b_winner", "baseline"),
    ("slot_b_winner", "baseline", "slot_a_winner"),
    ("slot_b_winner", "slot_a_winner", "baseline"),
]


class RegistrationPRNG:
    """Rejection-sampling PRNG for deriving registration parameters deterministically."""

    def __init__(self, base_commit_sha: str, domain: str):
        self.base_commit_sha = base_commit_sha
        self.domain = domain
        self.stream_index = 0
        self.transcript = []

    def _hash_block(self, block_index: int) -> bytes:
        seed = f"{self.base_commit_sha}|{self.domain}|{block_index}"
        return hashlib.sha256(seed.encode('utf-8')).digest()

    def next_uint32(self) -> Tuple[int, int]:
        block_idx = self.stream_index // 8
        byte_offset = (self.stream_index % 8) * 4
        digest = self._hash_block(block_idx)
        val = struct.unpack_from('>I', digest, byte_offset)[0]
        offset = self.stream_index
        self.stream_index += 1
        return val, offset

    def rejection_sample(self, n: int) -> int:
        if n <= 0:
            raise ValueError(f"rejection_sample requires n > 0, got {n}")
        limit = (1 << 32) - ((1 << 32) % n)
        while True:
            val, offset = self.next_uint32()
            if val < limit:
                selected = val % n
                self.transcript.append({
                    "action": "accept",
                    "stream_index": offset,
                    "uint32_val": val,
                    "rejection_limit": limit,
                    "range_n": n,
                    "chosen": selected
                })
                return selected
            else:
                self.transcript.append({
                    "action": "reject",
                    "stream_index": offset,
                    "uint32_val": val,
                    "rejection_limit": limit,
                    "range_n": n,
                    "chosen": None
                })


def derive_assignment_vectors(base_commit: str = APPROVED_BASE_COMMIT, num_trials: int = 10) -> Tuple[List[int], List[dict]]:
    """
    Derive +1/-1 arm assignment vectors from PRNG.
    One value per trial. +1 = optimizer is slot A; -1 = optimizer is slot B.
    Returns (vectors, prng_transcript).
    """
    prng = RegistrationPRNG(base_commit, "rsi_loop/campaign2/arm_assignment")
    vectors = []
    for _ in range(num_trials):
        bit = prng.rejection_sample(2)  # 0 or 1
        vectors.append(+1 if bit == 0 else -1)
    return vectors, prng.transcript


def derive_trio_ordering_seed(base_commit: str = APPROVED_BASE_COMMIT) -> Tuple[int, List[dict]]:
    """
    Derive a master seed for trio ordering from PRNG.
    The orchestrator uses this seed + (task_index, replicate_index) to select
    which of the 6 frozen orderings to use for each microblock trio.
    Returns (master_seed, prng_transcript).
    """
    prng = RegistrationPRNG(base_commit, "rsi_loop/campaign2/trio_ordering")
    seed = prng.rejection_sample(1 << 30)  # Large range for seed
    return seed, prng.transcript


def get_trio_ordering_index(master_seed: int, task_index: int, replicate_index: int) -> int:
    """Deterministic trio ordering selection from master seed + position."""
    h = hashlib.sha256(f"{master_seed}|{task_index}|{replicate_index}".encode('utf-8')).digest()
    val = struct.unpack_from('>I', h, 0)[0]
    return val % 6


def generate_master_registration(workspace_root: str = BASE_DIR) -> Tuple[str, str]:
    """
    Generate and seal campaign2_master_registration.json (0o444) and
    campaign2_registration_anchor.hash (0o444).
    """
    arm_vectors, arm_transcript = derive_assignment_vectors()
    trio_seed, trio_transcript = derive_trio_ordering_seed()

    from campaign2.envelope import (
        IMMUTABLE_CONTRACT,
        IMMUTABLE_CONTRACT_HASH,
        DELIMITER,
        DELIMITER_HASH,
        CONTROL_REFORMULATION_TEXT,
        CONTROL_REFORMULATION_HASH,
    )

    reg_data = {
        "schema_version": "3",
        "campaign_id": "campaign_2_gpt5_6_sol",
        "base_commit_sha": APPROVED_BASE_COMMIT,
        "model_name": "gpt-5.6-sol",
        "cli_version": "0.144.5",
        "reasoning_setting": "cli_0.144.5_default",
        "known_limitations": KNOWN_LIMITATIONS,
        "exploratory_secondaries": EXPLORATORY_SECONDARIES,
        "evaluable_candidate_definition": EVALUABLE_CANDIDATE_DEFINITION,
        "frozen_trio_orderings": [list(t) for t in FROZEN_TRIO_ORDERINGS],
        "arm_assignment_vectors": arm_vectors,
        "execution_limits": {
            "max_output_tokens": 600000,
            "max_active_seconds": 28800.0,
            "max_logical_calls": 175,
            "max_local_invocation_attempts": 525,
            "call_wall_limit_seconds": 120.0,
            "candidate_cpu_limit_seconds": 15.0,
            "candidate_wall_limit_seconds": 15.0,
            "max_canary_attempts": 3,
            "max_invocation_attempts": 3,
        },
        "prompt_envelope": {
            "immutable_contract": IMMUTABLE_CONTRACT,
            "immutable_contract_hash": IMMUTABLE_CONTRACT_HASH,
            "delimiter": DELIMITER,
            "delimiter_hash": DELIMITER_HASH,
            "control_reformulation_text": CONTROL_REFORMULATION_TEXT,
            "control_reformulation_hash": CONTROL_REFORMULATION_HASH,
            "candidate_template_bytes": IMMUTABLE_CONTRACT,
            "candidate_template_bytes_hash": IMMUTABLE_CONTRACT_HASH,
            "mutation_template_bytes": CONTROL_REFORMULATION_TEXT,
            "mutation_template_bytes_hash": CONTROL_REFORMULATION_HASH,
            "candidate_wrapper": "{immutable_contract}\n\n---PROBLEM SPECIFICATION---\n\n{problem_text}{delimiter}{mutable_body}",
            "mutation_wrapper": "# MUTATION REQUEST\n# Failure Context:\n{failure_context}\n\n# Mutable Strategy Body:\n{mutable_body}\n",
        },
        "arm_assignment_derivation": {
            "domain": "rsi_loop/campaign2/arm_assignment",
            "base_commit": APPROVED_BASE_COMMIT,
            "algorithm": "SHA-256 rejection sampling uint32, bit -> +1/-1",
            "transcript": arm_transcript
        },
        "trio_ordering_master_seed": trio_seed,
        "trio_ordering_derivation": {
            "domain": "rsi_loop/campaign2/trio_ordering",
            "base_commit": APPROVED_BASE_COMMIT,
            "algorithm": "SHA-256 rejection sampling uint32",
            "transcript": trio_transcript
        }
    }

    reg_path = os.path.join(workspace_root, REGISTRATION_FILE_REL)
    anchor_path = os.path.join(workspace_root, ANCHOR_FILE_REL)

    for p in (reg_path, anchor_path):
        if os.path.exists(p):
            os.chmod(p, 0o644)

    reg_content = json.dumps(reg_data, indent=2, sort_keys=True) + "\n"
    with open(reg_path, "w", encoding="utf-8") as f:
        f.write(reg_content)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(reg_path, 0o444)

    reg_hash = hashlib.sha256(reg_content.encode("utf-8")).hexdigest()
    with open(anchor_path, "w", encoding="utf-8") as f:
        f.write(reg_hash + "\n")
        f.flush()
        os.fsync(f.fileno())
    os.chmod(anchor_path, 0o444)

    dir_fd = os.open(workspace_root, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)

    return reg_path, anchor_path


def verify_master_registration(workspace_root: str = BASE_DIR) -> Dict[str, Any]:
    """
    Strict read-only verification of Master Registration and Anchor Hash.
    MUST be invoked before any scientific model call.
    Returns the parsed registration data on success.
    """
    reg_path = os.path.join(workspace_root, REGISTRATION_FILE_REL)
    anchor_path = os.path.join(workspace_root, ANCHOR_FILE_REL)

    for p in (reg_path, anchor_path):
        if not os.path.exists(p):
            raise FileNotFoundError(f"Missing master registration file: {p}")
        if os.path.islink(p):
            raise ValueError(f"Master registration file must not be a symlink: {p}")
        if not os.path.isfile(p):
            raise ValueError(f"Master registration file must be a regular file: {p}")
        st_mode = stat.S_IMODE(os.stat(p).st_mode)
        if st_mode != 0o444:
            raise ValueError(f"Master registration permission mode must be 0o444, got {oct(st_mode)} for {p}")

    with open(anchor_path, "r", encoding="utf-8") as f:
        expected_hash = f.read().strip()

    with open(reg_path, "rb") as f:
        reg_bytes = f.read()
    actual_hash = hashlib.sha256(reg_bytes).hexdigest()
    if actual_hash != expected_hash:
        raise ValueError(
            f"Master Registration Anchor Hash Mismatch: anchor has '{expected_hash}', "
            f"disk content produces '{actual_hash}'"
        )

    data = json.loads(reg_bytes.decode("utf-8"))

    lims = data.get("known_limitations", [])
    if len(lims) != 11:
        raise ValueError(f"Master registration must contain exactly 11 known limitations, got {len(lims)}")

    for idx, expected_text in enumerate(KNOWN_LIMITATIONS):
        if idx >= len(lims) or lims[idx] != expected_text:
            raise ValueError(f"Master registration limitation #{idx+1} mismatch")

    sec = data.get("exploratory_secondaries", [])
    if len(sec) != 4 or sec != EXPLORATORY_SECONDARIES:
        raise ValueError(f"Master registration exploratory secondaries mismatch: expected {EXPLORATORY_SECONDARIES}, got {sec}")

    ecd = data.get("evaluable_candidate_definition")
    if not isinstance(ecd, dict) or ecd != EVALUABLE_CANDIDATE_DEFINITION:
        raise ValueError(f"Master registration evaluable_candidate_definition mismatch: expected {EVALUABLE_CANDIDATE_DEFINITION}, got {ecd}")

    orderings = data.get("frozen_trio_orderings", [])
    if len(orderings) != 6:
        raise ValueError(f"Master registration must contain exactly 6 trio orderings, got {len(orderings)}")

    vectors = data.get("arm_assignment_vectors", [])
    if len(vectors) != 10:
        raise ValueError(f"Master registration must contain exactly 10 arm assignment vectors, got {len(vectors)}")
    for v in vectors:
        if v not in (+1, -1):
            raise ValueError(f"Arm assignment vector element must be +1 or -1, got {v}")

    if data.get("base_commit_sha") != APPROVED_BASE_COMMIT:
        raise ValueError(f"Base commit SHA mismatch: expected {APPROVED_BASE_COMMIT}, got {data.get('base_commit_sha')}")

    if data.get("arm_assignment_vectors") != [-1, -1, -1, 1, -1, -1, -1, 1, 1, -1]:
        raise ValueError(f"Arm assignment vectors mismatch: got {data.get('arm_assignment_vectors')}")

    if data.get("trio_ordering_master_seed") != 685898983:
        raise ValueError(f"Trio ordering master seed mismatch: expected 685898983, got {data.get('trio_ordering_master_seed')}")

    if data.get("model_name") != "gpt-5.6-sol":
        raise ValueError(f"Model name mismatch: expected 'gpt-5.6-sol', got {data.get('model_name')}")

    if data.get("cli_version") != "0.144.5":
        raise ValueError(f"CLI version mismatch: expected '0.144.5', got {data.get('cli_version')}")

    if data.get("reasoning_setting") != "cli_0.144.5_default":
        raise ValueError(f"Reasoning setting mismatch: expected 'cli_0.144.5_default', got {data.get('reasoning_setting')}")

    limits = data.get("execution_limits", {})
    if limits.get("max_output_tokens") != 600000 or limits.get("max_active_seconds") != 28800.0:
        raise ValueError(f"Execution limits mismatch in master registration: {limits}")

    if "arm_assignment_derivation" not in data:
        raise ValueError("Master registration missing arm_assignment_derivation")
    if "trio_ordering_derivation" not in data:
        raise ValueError("Master registration missing trio_ordering_derivation")

    pe = data.get("prompt_envelope")
    if not isinstance(pe, dict):
        raise ValueError("Master registration missing or invalid prompt_envelope")

    required_pe_keys = (
        "immutable_contract", "immutable_contract_hash",
        "delimiter", "delimiter_hash",
        "control_reformulation_text", "control_reformulation_hash"
    )
    for k in required_pe_keys:
        val = pe.get(k)
        if not val or not isinstance(val, str):
            raise ValueError(f"Master registration prompt_envelope missing non-empty string '{k}'")

    # Recompute and verify SHA-256 hashes of text components
    calc_contract_hash = hashlib.sha256(pe["immutable_contract"].encode("utf-8")).hexdigest()
    if calc_contract_hash != pe["immutable_contract_hash"]:
        raise ValueError(
            f"Master registration immutable_contract_hash mismatch: text produces '{calc_contract_hash}', "
            f"registered hash is '{pe['immutable_contract_hash']}'"
        )

    calc_delimiter_hash = hashlib.sha256(pe["delimiter"].encode("utf-8")).hexdigest()
    if calc_delimiter_hash != pe["delimiter_hash"]:
        raise ValueError(
            f"Master registration delimiter_hash mismatch: text produces '{calc_delimiter_hash}', "
            f"registered hash is '{pe['delimiter_hash']}'"
        )

    calc_ctrl_hash = hashlib.sha256(pe["control_reformulation_text"].encode("utf-8")).hexdigest()
    if calc_ctrl_hash != pe["control_reformulation_hash"]:
        raise ValueError(
            f"Master registration control_reformulation_hash mismatch: text produces '{calc_ctrl_hash}', "
            f"registered hash is '{pe['control_reformulation_hash']}'"
        )

    return data
