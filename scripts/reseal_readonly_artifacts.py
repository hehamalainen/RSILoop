#!/usr/bin/env python3
"""
Content-verifying setup script for read-only (0o444) artifact modes after a fresh Git clone.

Git tracks only the executable bit (100644 vs 100755), so a fresh `git clone` checks out
read-only sealed artifacts with mode `0o644`. Because RSILoop's preflight, archival, and
governance verifiers enforce `0o444` on sealed artifacts, this module verifies the SHA-256
content digest and cryptographic/Git anchors of every sealed artifact BEFORE restoring
required file modes.

Never overwrites or modifies file contents. Fails closed without altering any file permissions
if any file is missing, is a symlink, fails SHA-256 or claim verification, or if required
Git commit anchors are unavailable.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from typing import Dict, Set, Tuple

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

CAMPAIGN2_ARCHIVAL_ANCHOR_COMMIT = "f42a895a99fc5f3e7ccbfbfd9669ae4ce6ea0d3a"
CAMPAIGN3_P0_ANCHOR_COMMIT = "96e344e1c7f6c2fcb1242d159aea481e2b1defcf"
CAMPAIGN3_P1_ANCHOR_COMMIT = "8cdd4021aa9287a52b6f0eef8b21c375921a6d89"


EXPECTED_EVIDENCE_MANIFEST_KEYS = frozenset(
    {
        "campaign2_parity_matrix.json",
        "campaign2_tier_assignment.json",
        "campaign2_archive/proxy_calibration_qwen2_5_coder_0_5b_rejected.json",
        "campaign2_archive/proxy_config_gpt_oss_unexecuted.json",
    }
)


class ResealVerificationError(RuntimeError):
    """Raised when an artifact fails content or anchor verification prior to mode restoration."""


def _compute_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _check_regular_nonsymlink(workspace_root: str, rel_path: str) -> str:
    if not isinstance(rel_path, str) or not rel_path:
        raise ResealVerificationError(f"Invalid empty or non-string relative path: {rel_path!r}")
    if (
        os.path.isabs(rel_path)
        or rel_path.startswith(("/", "\\"))
        or os.path.splitdrive(rel_path)[0] != ""
        or "\\" in rel_path
    ):
        raise ResealVerificationError(
            f"Absolute, drive-prefixed, or backslash path forbidden in manifest: {rel_path!r}"
        )
    parts = rel_path.split("/")
    if any(comp in ("", ".", "..") for comp in parts):
        raise ResealVerificationError(
            f"Non-normalized or traversal path component forbidden in manifest: {rel_path!r}"
        )

    abs_root = os.path.abspath(workspace_root)
    curr = abs_root
    if os.path.islink(curr):
        raise ResealVerificationError(f"Workspace root is a symlink: {curr}")
    for comp in parts:
        curr = os.path.join(curr, comp)
        if os.path.lexists(curr) and os.path.islink(curr):
            raise ResealVerificationError(f"Symlink component forbidden: {curr}")
    curr = os.path.abspath(curr)
    if os.path.commonpath([abs_root, curr]) != abs_root or curr == abs_root:
        raise ResealVerificationError(f"Resolved path escapes workspace root: {rel_path!r}")
    if not os.path.exists(curr):
        raise ResealVerificationError(f"Missing required artifact: {rel_path}")
    if not os.path.isfile(curr):
        raise ResealVerificationError(f"Artifact is not a regular file: {rel_path}")
    return curr


def _require_git_worktree(workspace_root: str) -> None:
    """
    Detect whether workspace_root is inside a Git repository using `git rev-parse`
    (supporting both `.git` directories and `.git` gitdir files used by worktrees/submodules).
    Fails closed when Git metadata is absent or invalid.
    """
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=workspace_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except OSError as exc:
        raise ResealVerificationError(f"Git executable unavailable during reseal verification: {exc}") from exc
    if res.returncode != 0 or res.stdout.strip() != "true":
        raise ResealVerificationError(
            f"Workspace is not a valid Git work tree; required governance anchors cannot be verified: {workspace_root}"
        )


def _verify_git_blob_match(workspace_root: str, commit_sha: str, rel_path: str, abs_path: str) -> None:
    res = subprocess.run(
        ["git", "cat-file", "-p", f"{commit_sha}:{rel_path}"],
        cwd=workspace_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if res.returncode != 0:
        raise ResealVerificationError(
            f"Cannot resolve Git blob {commit_sha}:{rel_path} during reseal verification"
        )
    expected_sha = hashlib.sha256(res.stdout).hexdigest()
    actual_sha = _compute_sha256(abs_path)
    if actual_sha != expected_sha:
        raise ResealVerificationError(
            f"SHA-256 mismatch for {rel_path} against {commit_sha}: expected {expected_sha}, got {actual_sha}"
        )


def _verify_claim_record(
    claim_abs: str,
    expected_manifest_sha: str,
    expected_report_sha: str,
) -> None:
    from campaign2.archival import (
        CANONICAL_REPORT_SHA256,
        FORBIDDEN_CLAIM_PHRASES,
        SANCTIONED_CLAIM_TEXT,
    )

    with open(claim_abs, "rb") as f:
        claim_bytes = f.read()
    try:
        claim_data = json.loads(claim_bytes.decode("utf-8"))
    except Exception as exc:
        raise ResealVerificationError(f"Invalid JSON in campaign_claim_record.json: {exc}") from exc

    if not isinstance(claim_data, dict):
        raise ResealVerificationError("campaign_claim_record.json must be a JSON object")
    if claim_data.get("schema_version") != "3":
        raise ResealVerificationError(
            f"Invalid claim record schema_version: {claim_data.get('schema_version')!r}"
        )
    if claim_data.get("campaign_id") != "campaign_2_gpt5_6_sol":
        raise ResealVerificationError(
            f"Invalid claim record campaign_id: {claim_data.get('campaign_id')!r}"
        )
    sanctioned_claim = claim_data.get("sanctioned_claim", "")
    if sanctioned_claim != SANCTIONED_CLAIM_TEXT:
        raise ResealVerificationError(
            f"Claim record sanctioned_claim mismatch: got {sanctioned_claim!r}"
        )
    for phrase in FORBIDDEN_CLAIM_PHRASES:
        if phrase.lower() in sanctioned_claim.lower():
            raise ResealVerificationError(
                f"Claim record contains forbidden phrase: {phrase!r}"
            )
    if expected_report_sha != CANONICAL_REPORT_SHA256:
        raise ResealVerificationError(
            f"Archive manifest canonical_report_sha256 mismatch: {expected_report_sha!r}"
        )
    if claim_data.get("canonical_report_sha256") != CANONICAL_REPORT_SHA256:
        raise ResealVerificationError(
            "Claim record canonical_report_sha256 mismatch against CANONICAL_REPORT_SHA256"
        )
    if claim_data.get("manifest_sha256") != expected_manifest_sha:
        raise ResealVerificationError(
            f"Claim record manifest_sha256 mismatch: expected {expected_manifest_sha}, "
            f"got {claim_data.get('manifest_sha256')}"
        )
    expected_claim_bytes = (json.dumps(claim_data, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if claim_bytes != expected_claim_bytes:
        raise ResealVerificationError("campaign_claim_record.json is not canonically formatted")


def ensure_readonly_artifact_modes(workspace_root: str = BASE_DIR, verbose: bool = False) -> Tuple[int, int]:
    """
    Verify SHA-256 content hashes, exact manifest inventory membership, claim record invariants,
    and Git commit anchors across all sealed artifacts BEFORE restoring required file modes
    (`0o444` for sealed artifacts and exact recorded `st_mode` values for the 32 frozen
    Campaign 2 archive inventory artifacts).

    Returns (verified_count, chmod_updated_count).
    """
    abs_root = os.path.abspath(workspace_root)
    _require_git_worktree(abs_root)
    desired_modes: Dict[str, Tuple[str, int]] = {}

    def _record_desired_mode(rel_p: str, validated_abs_p: str, mode: int) -> None:
        norm_abs = os.path.abspath(validated_abs_p)
        if os.path.commonpath([abs_root, norm_abs]) != abs_root or norm_abs == abs_root:
            raise ResealVerificationError(
                f"Refusing to record target mode for path outside checkout: {rel_p!r} -> {norm_abs!r}"
            )
        desired_modes[rel_p] = (norm_abs, mode)

    # 1. Verify release_preflight_hashes.json (145 live Category 1 files) and historical campaign2_preflight_hashes.json
    from campaign2.manifest import get_expected_frozen_inventory

    release_preflight_rel = "release_preflight_hashes.json"
    hist_preflight_rel = "campaign2_preflight_hashes.json"
    release_preflight_abs = _check_regular_nonsymlink(abs_root, release_preflight_rel)
    hist_preflight_abs = _check_regular_nonsymlink(abs_root, hist_preflight_rel)

    _verify_git_blob_match(
        abs_root,
        CAMPAIGN2_ARCHIVAL_ANCHOR_COMMIT,
        hist_preflight_rel,
        hist_preflight_abs,
    )

    try:
        with open(release_preflight_abs, "r", encoding="utf-8") as f:
            preflight_map = json.load(f)
    except Exception as exc:
        raise ResealVerificationError(f"Invalid JSON in {release_preflight_rel}: {exc}") from exc

    if not isinstance(preflight_map, dict):
        raise ResealVerificationError(f"{release_preflight_rel} must be a JSON object")

    expected_preflight_keys = set(get_expected_frozen_inventory(abs_root))
    for rel_path in preflight_map.keys():
        _check_regular_nonsymlink(abs_root, rel_path)
    actual_preflight_keys = set(preflight_map.keys())
    if actual_preflight_keys != expected_preflight_keys:
        unexpected = sorted(actual_preflight_keys - expected_preflight_keys)
        missing = sorted(expected_preflight_keys - actual_preflight_keys)
        raise ResealVerificationError(
            f"Inventory mismatch in {release_preflight_rel}: unexpected={unexpected}, missing={missing}"
        )

    for rel_path, expected_sha in sorted(preflight_map.items()):
        abs_p = _check_regular_nonsymlink(abs_root, rel_path)
        actual_sha = _compute_sha256(abs_p)
        if actual_sha != expected_sha:
            raise ResealVerificationError(
                f"Release preflight manifest SHA-256 mismatch for {rel_path}: expected {expected_sha}, got {actual_sha}"
            )
        if rel_path != "README.md":
            _record_desired_mode(rel_path, abs_p, 0o444)
    _record_desired_mode(release_preflight_rel, release_preflight_abs, 0o444)
    _record_desired_mode(hist_preflight_rel, hist_preflight_abs, 0o444)

    # 2. Verify campaign2_evidence_hashes.json entries (4 files)
    evidence_rel = "campaign2_evidence_hashes.json"
    evidence_abs = _check_regular_nonsymlink(abs_root, evidence_rel)
    try:
        with open(evidence_abs, "r", encoding="utf-8") as f:
            evidence_map = json.load(f)
    except Exception as exc:
        raise ResealVerificationError(f"Invalid JSON in {evidence_rel}: {exc}") from exc

    if not isinstance(evidence_map, dict):
        raise ResealVerificationError(f"{evidence_rel} must be a JSON object")

    for rel_path in evidence_map.keys():
        _check_regular_nonsymlink(abs_root, rel_path)
    actual_evidence_keys = set(evidence_map.keys())
    if actual_evidence_keys != EXPECTED_EVIDENCE_MANIFEST_KEYS:
        unexpected = sorted(actual_evidence_keys - EXPECTED_EVIDENCE_MANIFEST_KEYS)
        missing = sorted(EXPECTED_EVIDENCE_MANIFEST_KEYS - actual_evidence_keys)
        raise ResealVerificationError(
            f"Inventory mismatch in {evidence_rel}: unexpected={unexpected}, missing={missing}"
        )

    for rel_path, expected_sha in sorted(evidence_map.items()):
        abs_p = _check_regular_nonsymlink(abs_root, rel_path)
        actual_sha = _compute_sha256(abs_p)
        if actual_sha != expected_sha:
            raise ResealVerificationError(
                f"Evidence manifest SHA-256 mismatch for {rel_path}: expected {expected_sha}, got {actual_sha}"
            )
        _record_desired_mode(rel_path, abs_p, 0o444)
    _record_desired_mode(evidence_rel, evidence_abs, 0o444)

    # 3. Verify historical campaign_archive_manifest.json entries (32 files), anchor, and claim record
    from campaign2.archival import FROZEN_CAMPAIGN_ARCHIVE_INVENTORY

    archive_manifest_rel = "campaign_archive_manifest.json"
    archive_anchor_rel = "campaign_archive_anchor.hash"
    claim_record_rel = "campaign_claim_record.json"

    archive_manifest_abs = _check_regular_nonsymlink(abs_root, archive_manifest_rel)
    archive_anchor_abs = _check_regular_nonsymlink(abs_root, archive_anchor_rel)
    claim_record_abs = _check_regular_nonsymlink(abs_root, claim_record_rel)

    with open(archive_anchor_abs, "r", encoding="utf-8") as f:
        expected_manifest_sha = f.read().strip()
    actual_manifest_sha = _compute_sha256(archive_manifest_abs)
    if actual_manifest_sha != expected_manifest_sha:
        raise ResealVerificationError(
            f"Archive anchor mismatch for {archive_manifest_rel}: expected {expected_manifest_sha}, got {actual_manifest_sha}"
        )

    try:
        with open(archive_manifest_abs, "r", encoding="utf-8") as f:
            archive_data = json.load(f)
    except Exception as exc:
        raise ResealVerificationError(f"Invalid JSON in {archive_manifest_rel}: {exc}") from exc

    if not isinstance(archive_data, dict):
        raise ResealVerificationError(f"{archive_manifest_rel} must be a JSON object")

    _verify_claim_record(
        claim_record_abs,
        expected_manifest_sha=actual_manifest_sha,
        expected_report_sha=archive_data.get("canonical_report_sha256", ""),
    )

    for rel_meta, abs_meta in (
        (archive_manifest_rel, archive_manifest_abs),
        (archive_anchor_rel, archive_anchor_abs),
        (claim_record_rel, claim_record_abs),
    ):
        _verify_git_blob_match(
            abs_root,
            CAMPAIGN2_ARCHIVAL_ANCHOR_COMMIT,
            rel_meta,
            abs_meta,
        )

    archive_artifacts = archive_data.get("artifacts")
    if not isinstance(archive_artifacts, list):
        raise ResealVerificationError(f"{archive_manifest_rel} 'artifacts' field must be a list")

    seen_archive_paths: Set[str] = set()
    for item in archive_artifacts:
        if not isinstance(item, dict) or "relative_path" not in item:
            raise ResealVerificationError(f"Malformed artifact entry in {archive_manifest_rel}")
        rel_p = item["relative_path"]
        _check_regular_nonsymlink(abs_root, rel_p)
        if rel_p in seen_archive_paths:
            raise ResealVerificationError(f"Duplicate artifact path in {archive_manifest_rel}: {rel_p}")
        seen_archive_paths.add(rel_p)

    expected_archive_keys = set(FROZEN_CAMPAIGN_ARCHIVE_INVENTORY)
    if seen_archive_paths != expected_archive_keys:
        unexpected = sorted(seen_archive_paths - expected_archive_keys)
        missing = sorted(expected_archive_keys - seen_archive_paths)
        raise ResealVerificationError(
            f"Inventory mismatch in {archive_manifest_rel}: unexpected={unexpected}, missing={missing}"
        )

    for item in archive_artifacts:
        rel_p = item["relative_path"]
        expected_sha = item["sha256"]
        expected_size = item["size_bytes"]
        expected_mode = int(item["st_mode"])
        abs_p = _check_regular_nonsymlink(abs_root, rel_p)
        actual_sha = _compute_sha256(abs_p)
        if actual_sha != expected_sha:
            raise ResealVerificationError(
                f"Campaign archive SHA-256 mismatch for {rel_p}: expected {expected_sha}, got {actual_sha}"
            )
        actual_size = os.lstat(abs_p).st_size
        if actual_size != expected_size:
            raise ResealVerificationError(
                f"Campaign archive size mismatch for {rel_p}: expected {expected_size}, got {actual_size}"
            )
        _record_desired_mode(rel_p, abs_p, expected_mode)

    _record_desired_mode(archive_manifest_rel, archive_manifest_abs, 0o444)
    _record_desired_mode(archive_anchor_rel, archive_anchor_abs, 0o444)
    _record_desired_mode(claim_record_rel, claim_record_abs, 0o444)

    # 4. Verify Campaign 3 P0 and P1 sealed governance artifacts against Git anchors (fail-closed)
    from campaign3.verifier import P0_ARTIFACTS
    from campaign3.p1_verifier import P1_ARTIFACTS

    for rel_p in P0_ARTIFACTS:
        abs_p = _check_regular_nonsymlink(abs_root, rel_p)
        _verify_git_blob_match(abs_root, CAMPAIGN3_P0_ANCHOR_COMMIT, rel_p, abs_p)
        _record_desired_mode(rel_p, abs_p, 0o444)

    for rel_p in P1_ARTIFACTS:
        abs_p = _check_regular_nonsymlink(abs_root, rel_p)
        _verify_git_blob_match(abs_root, CAMPAIGN3_P1_ANCHOR_COMMIT, rel_p, abs_p)
        _record_desired_mode(rel_p, abs_p, 0o444)

    # Apply target modes ONLY after all verifications have succeeded, using the validated in-checkout paths
    updated_count = 0
    for rel_p, (abs_p, target_mode) in sorted(desired_modes.items()):
        if os.path.commonpath([abs_root, abs_p]) != abs_root or abs_p == abs_root:
            raise ResealVerificationError(f"Refusing to chmod path outside checkout: {abs_p}")
        st = os.lstat(abs_p)
        if stat.S_ISLNK(st.st_mode):
            raise ResealVerificationError(f"Refusing to chmod symlink: {abs_p}")
        current_mode = stat.S_IMODE(st.st_mode)
        if current_mode != target_mode:
            os.chmod(abs_p, target_mode)
            updated_count += 1
            if verbose:
                sys.stdout.write(f"Restored mode {oct(target_mode)}: {rel_p} (was {oct(current_mode)})\n")

    return len(desired_modes), updated_count


def main() -> int:
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    regenerate = "--regenerate-manifests" in sys.argv
    if regenerate:
        from campaign2.manifest import generate_release_manifest, verify_release_manifest_relationship

        generate_release_manifest(BASE_DIR)
        verify_release_manifest_relationship(BASE_DIR)
        if verbose:
            sys.stdout.write(
                "Regenerated release_preflight_hashes.json (historical campaign2_preflight_hashes.json "
                "and campaign_archive_manifest.json preserved untouched).\n"
            )

    try:
        verified_count, updated_count = ensure_readonly_artifact_modes(BASE_DIR, verbose=verbose)
    except ResealVerificationError as exc:
        sys.stderr.write(f"FATAL: Artifact reseal verification failed: {exc}\n")
        return 1
    sys.stdout.write(
        f"Verified {verified_count} sealed artifacts; restored 0o444 mode on {updated_count} file(s).\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
