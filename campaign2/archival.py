"""
Campaign 2 Archival, Cryptographic Anchor & Claim Record Module.

Work Order A2 Implementation:
- Frozen evidence inventory (32 exact relative paths)
- Strict mode check (0o444 for frozen artifacts and generated archive metadata)
- Schema 3 compliant campaign_archive_manifest.json with dual-path provenance
- SHA-256 anchor file campaign_archive_anchor.hash (0o444)
- Sanctioned claim record campaign_claim_record.json (0o444)
- Git-backed trust anchor verification against trusted commit SHA
- Self-contained relocated verification using target_root
"""

import os
import sys
import json
import stat
import hashlib
import subprocess
from datetime import datetime, timezone
from typing import Dict, Any, List, Tuple, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

MANIFEST_REL_PATH = "campaign_archive_manifest.json"
ANCHOR_REL_PATH = "campaign_archive_anchor.hash"
CLAIM_REL_PATH = "campaign_claim_record.json"

CANONICAL_REPORT_REL_PATH = "history/run_1786134848_cfb514d2/campaign2_pilot_report.json"
CANONICAL_REPORT_SHA256 = "7d95b7b46902052711939a005cf427b697e58711dc040254665a56bcb9903ad3"

SANCTIONED_CLAIM_TEXT = (
    "For the service presented as gpt-5.6-sol during the campaign window, "
    "Campaign 1's Huffman task and Campaign 2's eight Suite P tasks provided "
    "insufficient baseline headroom under their preregistered gates."
)

FORBIDDEN_CLAIM_PHRASES = [
    "frontier models",
    "hand-authored benchmarks",
    "independent campaigns",
    "independently executed",
]

FROZEN_CAMPAIGN_ARCHIVE_INVENTORY = [
    "history/run_1784409881_d5134082/invalid_manifest.json",
    "history/run_1785357988_aaf6d755/run_manifest.json",
    "history/run_1785357988_aaf6d755/operator_report.json",
    "history/run_1785357988_aaf6d755/inner_agent_best.txt",
    "history/run_1785357988_aaf6d755/preflight_huffman.json",
    "history/run_1785357988_aaf6d755/huffman_preflight/solution.py",
    "history/run_1785624487_6c9c3d1a/run_manifest.json",
    "history/run_1785624487_6c9c3d1a/operator_report.json",
    "history/run_1785624487_6c9c3d1a/inner_agent_best.txt",
    "history/run_1785624487_6c9c3d1a/preflight_huffman.json",
    "history/run_1785624487_6c9c3d1a/huffman_preflight/solution.py",
    "history/run_1786134848_cfb514d2/campaign2_pilot_ledger.jsonl",
    "history/run_1786134848_cfb514d2/campaign2_pilot_checkpoint.json",
    "history/run_1786134848_cfb514d2/campaign2_pilot_report.json",
    "history/run_1786134848_cfb514d2/campaign2_attempt_identity.json",
    "history/run_1786134848_cfb514d2/campaign2_attempt_identity.sha256",
    "history/run_1786134848_cfb514d2/campaign2_pilot_checkpoint.json.state_anchor",
    "history/run_1786134848_cfb514d2/operator_report.json",
    "best_manifest.json",
    "history/legacy/best_manifest.json.archive_4a68892a14e74064998a9cf01b9710c6.backup",
    "history/legacy/best_manifest.json.archive_c3bd39bfab6342d8bbb214615092b0af.backup",
    "campaign2_cpu_gate_record.json",
    "history/campaign2_pilot_scientific_start.json",
    "history/campaign2_pilot_preflight_registry_incident.json",
    "campaign2_master_registration.json",
    "campaign2_tier_assignment.json",
    "campaign2_parity_matrix.json",
    "campaign2_evidence_hashes.json",
    "campaign2_preflight_hashes.json",
    "campaign2_registration_anchor.hash",
    "history/campaign2_launch_checklist.json",
    "campaign2_launch_lock.json",
]


def _safe_lstat_component_check(workspace_root: str, rel_path: str) -> str:
    """
    Safely inspect every path component from workspace_root down to rel_path.
    Rejects any symlinks in path components, '..' escapes, or absolute path escapes.
    Returns absolute path if valid.
    """
    if os.path.isabs(rel_path):
        raise ValueError(f"Path escape attempt: absolute path forbidden '{rel_path}'")

    workspace_abs = os.path.abspath(workspace_root)
    rel_components = rel_path.strip(os.sep).split(os.sep)

    current = workspace_abs
    for comp in rel_components:
        if comp == ".." or comp == ".":
            raise ValueError(f"Path traversal attempt forbidden: '{rel_path}'")
        current = os.path.join(current, comp)
        if os.path.lexists(current) and os.path.islink(current):
            raise ValueError(f"Path component is a symlink: '{current}'")

    if not current.startswith(workspace_abs):
        raise ValueError(f"Path escape attempt: '{current}' outside '{workspace_abs}'")

    return current


def read_file_with_consistency_check(workspace_root: str, rel_path: str) -> Tuple[bytes, int, int]:
    """
    Read file contents with before/after lstat time/inode/size consistency checks.
    Guarantees consistent snapshot (torn read prevention).
    Returns (content_bytes, mode, size_bytes).
    """
    abs_path = _safe_lstat_component_check(workspace_root, rel_path)

    st1 = os.lstat(abs_path)
    if stat.S_ISLNK(st1.st_mode):
        raise ValueError(f"Symlink forbidden: '{abs_path}'")
    if not stat.S_ISREG(st1.st_mode):
        raise ValueError(f"Artifact path is not a regular file: '{abs_path}'")

    with open(abs_path, "rb") as f:
        content = f.read()

    st2 = os.lstat(abs_path)
    if (st1.st_ino != st2.st_ino or
        st1.st_size != st2.st_size or
        st1.st_mode != st2.st_mode or
        st1.st_mtime != st2.st_mtime):
        raise ValueError(f"Consistent-snapshot failure (torn read detected) for '{rel_path}'")

    mode = stat.S_IMODE(st1.st_mode)
    size = st1.st_size
    return content, mode, size


def _get_git_blob_bytes(git_repo_dir: str, commit_sha: str, rel_path: str) -> Optional[bytes]:
    """Retrieve raw bytes of a file from Git object store at specified commit."""
    try:
        cmd = ["git", "show", f"{commit_sha}:{rel_path}"]
        res = subprocess.run(cmd, cwd=git_repo_dir, capture_output=True, check=True)
        return res.stdout
    except Exception:
        return None


def generate_campaign_archive(workspace_root: str = BASE_DIR) -> Tuple[str, str, str]:
    """
    Generate and seal campaign_archive_manifest.json, campaign_archive_anchor.hash,
    and campaign_claim_record.json with mode 0o444.
    """
    workspace_root = os.path.abspath(workspace_root)

    # 1. Independent report verification
    rep_content, _, _ = read_file_with_consistency_check(workspace_root, CANONICAL_REPORT_REL_PATH)
    actual_rep_hash = hashlib.sha256(rep_content).hexdigest()
    if actual_rep_hash != CANONICAL_REPORT_SHA256:
        raise ValueError(
            f"Canonical Campaign 2 report hash mismatch: expected {CANONICAL_REPORT_SHA256}, "
            f"got {actual_rep_hash}"
        )

    # 2. Collect metadata for all 32 evidence artifacts
    artifacts = []
    seen = set()
    for rel_p in FROZEN_CAMPAIGN_ARCHIVE_INVENTORY:
        if rel_p in seen:
            raise ValueError(f"Duplicate path in frozen inventory: {rel_p}")
        seen.add(rel_p)

        content_bytes, mode, size = read_file_with_consistency_check(workspace_root, rel_p)
        sha256_hex = hashlib.sha256(content_bytes).hexdigest()
        abs_p = os.path.abspath(os.path.join(workspace_root, rel_p))

        artifacts.append({
            "relative_path": rel_p,
            "absolute_path": abs_p,
            "sha256": sha256_hex,
            "size_bytes": size,
            "st_mode": mode,
        })

    # Sort artifacts deterministically by relative_path
    artifacts.sort(key=lambda x: x["relative_path"])

    manifest_p = os.path.join(workspace_root, MANIFEST_REL_PATH)
    anchor_p = os.path.join(workspace_root, ANCHOR_REL_PATH)
    claim_p = os.path.join(workspace_root, CLAIM_REL_PATH)

    created_iso = datetime.now(timezone.utc).isoformat()
    if os.path.exists(manifest_p):
        try:
            with open(manifest_p, "r", encoding="utf-8") as f:
                ex_data = json.load(f)
                if "created_at_iso" in ex_data:
                    created_iso = ex_data["created_at_iso"]
        except Exception:
            pass

    manifest_data = {
        "schema_version": "3",
        "campaign_id": "campaign_2_gpt5_6_sol",
        "created_at_iso": created_iso,
        "canonical_report_path": CANONICAL_REPORT_REL_PATH,
        "canonical_report_sha256": CANONICAL_REPORT_SHA256,
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
    }

    manifest_bytes = (json.dumps(manifest_data, indent=2, sort_keys=True) + "\n").encode("utf-8")
    expected_manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()

    claim_data = {
        "schema_version": "3",
        "sanctioned_claim": SANCTIONED_CLAIM_TEXT,
        "campaign_id": "campaign_2_gpt5_6_sol",
        "canonical_report_sha256": CANONICAL_REPORT_SHA256,
        "manifest_sha256": expected_manifest_hash,
    }
    claim_bytes = (json.dumps(claim_data, indent=2, sort_keys=True) + "\n").encode("utf-8")

    meta_files = [manifest_p, anchor_p, claim_p]
    existing = [p for p in meta_files if os.path.exists(p)]

    # Atomic Precheck without permission mutation
    if len(existing) == 3:
        try:
            m_mode = stat.S_IMODE(os.stat(manifest_p).st_mode)
            a_mode = stat.S_IMODE(os.stat(anchor_p).st_mode)
            c_mode = stat.S_IMODE(os.stat(claim_p).st_mode)

            with open(manifest_p, "rb") as f:
                ex_m = f.read()
            with open(anchor_p, "rb") as f:
                ex_a = f.read()
            with open(claim_p, "rb") as f:
                ex_c = f.read()

            if (m_mode == 0o444 and a_mode == 0o444 and c_mode == 0o444 and
                ex_m == manifest_bytes and
                ex_a == (expected_manifest_hash + "\n").encode("utf-8") and
                ex_c == claim_bytes):
                return manifest_p, anchor_p, claim_p
        except Exception:
            pass
        raise ValueError("Refusing to overwrite manifest with differing content or invalid permissions")
    elif len(existing) > 0:
        raise ValueError("Refusing to overwrite partial campaign archive files")

    # Atomic Write & Seal
    for path, data_bytes in ((manifest_p, manifest_bytes),
                             (anchor_p, (expected_manifest_hash + "\n").encode("utf-8")),
                             (claim_p, claim_bytes)):
        with open(path, "wb") as f:
            f.write(data_bytes)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(path, 0o444)

    dir_fd = os.open(workspace_root, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)

    return manifest_p, anchor_p, claim_p


def verify_campaign_archive(workspace_root: str = BASE_DIR,
                            relocated_root: Optional[str] = None,
                            trusted_commit_sha: Optional[str] = None) -> Dict[str, Any]:
    """
    Self-contained verification of campaign archive manifest, anchor hash, claim record,
    and all 32 evidence artifacts.
    Supports relocated repository copies via relocated_root.
    Validates against Git object store when trusted_commit_sha is supplied.
    """
    target_root = os.path.abspath(relocated_root if relocated_root is not None else workspace_root)

    manifest_p = os.path.join(target_root, MANIFEST_REL_PATH)
    anchor_p = os.path.join(target_root, ANCHOR_REL_PATH)
    claim_p = os.path.join(target_root, CLAIM_REL_PATH)

    # 1. Metadata file existence & permission mode checks
    for p in (manifest_p, anchor_p, claim_p):
        if not os.path.exists(p):
            raise FileNotFoundError(f"Missing archive metadata file: '{p}'")
        if os.path.islink(p):
            raise ValueError(f"Archive metadata file must not be a symlink: '{p}'")
        if not os.path.isfile(p):
            raise ValueError(f"Archive metadata file must be a regular file: '{p}'")
        st_mode = stat.S_IMODE(os.stat(p).st_mode)
        if st_mode != 0o444:
            raise ValueError(f"Archive metadata permission mode must be 0o444, got {oct(st_mode)} for '{p}'")

    # 2. Read anchor hash & verify against manifest disk hash
    with open(anchor_p, "r", encoding="utf-8") as f:
        expected_manifest_hash = f.read().strip()

    with open(manifest_p, "rb") as f:
        manifest_bytes = f.read()

    actual_manifest_hash = hashlib.sha256(manifest_bytes).hexdigest()
    if actual_manifest_hash != expected_manifest_hash:
        raise ValueError(
            f"Campaign Archive Anchor Hash Mismatch: anchor has '{expected_manifest_hash}', "
            f"disk content produces '{actual_manifest_hash}'"
        )

    # 3. Read & verify claim record
    with open(claim_p, "rb") as f:
        claim_bytes = f.read()

    claim_data = json.loads(claim_bytes.decode("utf-8"))
    if claim_data.get("schema_version") != "3":
        raise ValueError(f"Invalid claim record schema_version: {claim_data.get('schema_version')}")

    s_claim = claim_data.get("sanctioned_claim", "")
    if s_claim != SANCTIONED_CLAIM_TEXT:
        raise ValueError(f"sanctioned_claim mismatch: got '{s_claim}'")

    for phrase in FORBIDDEN_CLAIM_PHRASES:
        if phrase.lower() in s_claim.lower():
            raise ValueError(f"sanctioned_claim contains forbidden phrase: '{phrase}'")

    if claim_data.get("manifest_sha256") != actual_manifest_hash:
        raise ValueError("Claim record manifest_sha256 mismatch")
    if claim_data.get("canonical_report_sha256") != CANONICAL_REPORT_SHA256:
        raise ValueError("Claim record canonical_report_sha256 mismatch")

    # 4. Parse manifest and verify schema version
    manifest_data = json.loads(manifest_bytes.decode("utf-8"))
    if manifest_data.get("schema_version") != "3":
        raise ValueError(f"Invalid manifest schema_version: {manifest_data.get('schema_version')}")

    if manifest_data.get("canonical_report_sha256") != CANONICAL_REPORT_SHA256:
        raise ValueError("Manifest canonical_report_sha256 mismatch")

    # 5. Git blob verification (MANDATORY: unanchored verification is forbidden)
    if not trusted_commit_sha:
        raise ValueError(
            "trusted_commit_sha is required for campaign archive verification; unanchored verification is forbidden"
        )

    for rel_p, disk_bytes in (
        (MANIFEST_REL_PATH, manifest_bytes),
        (ANCHOR_REL_PATH, (expected_manifest_hash + "\n").encode("utf-8")),
        (CLAIM_REL_PATH, claim_bytes),
    ):
        git_bytes = _get_git_blob_bytes(target_root, trusted_commit_sha, rel_p)
        if git_bytes is None or git_bytes != disk_bytes:
            raise ValueError(
                f"Git anchor verification failure for '{rel_p}': on-disk content differs from "
                f"Git blob at trusted commit {trusted_commit_sha}"
            )

    # 6. Verify evidence artifacts under target_root
    artifacts = manifest_data.get("artifacts", [])
    if len(artifacts) != len(FROZEN_CAMPAIGN_ARCHIVE_INVENTORY):
        raise ValueError(
            f"Artifact count mismatch: manifest has {len(artifacts)}, expected {len(FROZEN_CAMPAIGN_ARCHIVE_INVENTORY)}"
        )

    seen_paths = set()
    for art in artifacts:
        rel_p = art["relative_path"]
        if rel_p in seen_paths:
            raise ValueError(f"Duplicate relative_path in manifest: {rel_p}")
        seen_paths.add(rel_p)

        content_bytes, mode, size = read_file_with_consistency_check(target_root, rel_p)
        actual_sha = hashlib.sha256(content_bytes).hexdigest()

        if actual_sha != art["sha256"]:
            raise ValueError(f"SHA-256 mismatch for artifact '{rel_p}': manifest {art['sha256']}, disk {actual_sha}")

        if size != art["size_bytes"]:
            raise ValueError(f"Size mismatch for artifact '{rel_p}': manifest {art['size_bytes']}, disk {size}")

        if mode != art["st_mode"]:
            raise ValueError(
                f"Mode mismatch for artifact '{rel_p}': manifest recorded {oct(art['st_mode'])}, disk has {oct(mode)}"
            )

    # Enforce exact set equality of inventory membership
    if seen_paths != set(FROZEN_CAMPAIGN_ARCHIVE_INVENTORY):
        raise ValueError(
            f"Artifact inventory membership mismatch: expected exact set of {len(FROZEN_CAMPAIGN_ARCHIVE_INVENTORY)} paths"
        )

    return manifest_data
