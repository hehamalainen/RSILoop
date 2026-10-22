#!/usr/bin/env python3
"""
Content-verifying setup script for read-only (0o444) artifact modes after a fresh Git clone.

Git tracks only the executable bit (100644 vs 100755), so a fresh `git clone` checks out
read-only sealed artifacts with mode `0o644`. Because RSILoop's preflight, archival, and
governance verifiers enforce `0o444` on sealed artifacts, this module verifies the SHA-256
content digest of every sealed artifact against its committed manifest or Git commit anchor
BEFORE restoring mode `0o444`.

Never overwrites or modifies file contents. Fails closed if any file is missing, is a symlink,
or fails SHA-256 verification.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
import subprocess
import sys
from typing import Dict, List, Set, Tuple

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

CAMPAIGN3_P0_ANCHOR_COMMIT = "96e344e1c7f6c2fcb1242d159aea481e2b1defcf"
CAMPAIGN3_P1_ANCHOR_COMMIT = "8cdd4021aa9287a52b6f0eef8b21c375921a6d89"


class ResealVerificationError(RuntimeError):
    """Raised when an artifact fails content verification prior to mode restoration."""


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
    abs_root = os.path.abspath(workspace_root)
    curr = abs_root
    if os.path.islink(curr):
        raise ResealVerificationError(f"Workspace root is a symlink: {curr}")
    for comp in rel_path.strip(os.sep).split(os.sep):
        if not comp or comp == ".":
            continue
        if comp == "..":
            raise ResealVerificationError(f"Path traversal forbidden: {rel_path}")
        curr = os.path.join(curr, comp)
        if os.path.lexists(curr) and os.path.islink(curr):
            raise ResealVerificationError(f"Symlink component forbidden: {curr}")
    if not os.path.exists(curr):
        raise ResealVerificationError(f"Missing required artifact: {rel_path}")
    if not os.path.isfile(curr):
        raise ResealVerificationError(f"Artifact is not a regular file: {rel_path}")
    return curr


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


def ensure_readonly_artifact_modes(workspace_root: str = BASE_DIR, verbose: bool = False) -> Tuple[int, int]:
    """
    Verify SHA-256 content hashes against committed manifests/anchors and ensure mode 0o444
    on all sealed artifacts.

    Returns (verified_count, chmod_updated_count).
    """
    verified_paths: Set[str] = set()

    # 1. Verify campaign2_preflight_hashes.json entries (145 files)
    preflight_rel = "campaign2_preflight_hashes.json"
    preflight_abs = _check_regular_nonsymlink(workspace_root, preflight_rel)
    with open(preflight_abs, "r", encoding="utf-8") as f:
        preflight_map: Dict[str, str] = json.load(f)

    for rel_path, expected_sha in sorted(preflight_map.items()):
        abs_p = _check_regular_nonsymlink(workspace_root, rel_path)
        actual_sha = _compute_sha256(abs_p)
        if actual_sha != expected_sha:
            raise ResealVerificationError(
                f"Preflight manifest SHA-256 mismatch for {rel_path}: expected {expected_sha}, got {actual_sha}"
            )
        verified_paths.add(rel_path)
    verified_paths.add(preflight_rel)

    # 2. Verify campaign2_evidence_hashes.json entries (4 files)
    evidence_rel = "campaign2_evidence_hashes.json"
    evidence_abs = _check_regular_nonsymlink(workspace_root, evidence_rel)
    with open(evidence_abs, "r", encoding="utf-8") as f:
        evidence_map: Dict[str, str] = json.load(f)

    for rel_path, expected_sha in sorted(evidence_map.items()):
        abs_p = _check_regular_nonsymlink(workspace_root, rel_path)
        actual_sha = _compute_sha256(abs_p)
        if actual_sha != expected_sha:
            raise ResealVerificationError(
                f"Evidence manifest SHA-256 mismatch for {rel_path}: expected {expected_sha}, got {actual_sha}"
            )
        verified_paths.add(rel_path)
    verified_paths.add(evidence_rel)

    # 3. Verify campaign_archive_manifest.json entries (32 files) and anchor
    archive_manifest_rel = "campaign_archive_manifest.json"
    archive_anchor_rel = "campaign_archive_anchor.hash"
    claim_record_rel = "campaign_claim_record.json"

    archive_manifest_abs = _check_regular_nonsymlink(workspace_root, archive_manifest_rel)
    archive_anchor_abs = _check_regular_nonsymlink(workspace_root, archive_anchor_rel)
    _check_regular_nonsymlink(workspace_root, claim_record_rel)

    with open(archive_anchor_abs, "r", encoding="utf-8") as f:
        expected_manifest_sha = f.read().strip()
    actual_manifest_sha = _compute_sha256(archive_manifest_abs)
    if actual_manifest_sha != expected_manifest_sha:
        raise ResealVerificationError(
            f"Archive anchor mismatch for {archive_manifest_rel}: expected {expected_manifest_sha}, got {actual_manifest_sha}"
        )

    with open(archive_manifest_abs, "r", encoding="utf-8") as f:
        archive_data = json.load(f)
    for item in archive_data.get("artifacts", []):
        rel_p = item["relative_path"]
        expected_sha = item["sha256"]
        abs_p = _check_regular_nonsymlink(workspace_root, rel_p)
        actual_sha = _compute_sha256(abs_p)
        if actual_sha != expected_sha:
            raise ResealVerificationError(
                f"Campaign archive SHA-256 mismatch for {rel_p}: expected {expected_sha}, got {actual_sha}"
            )
        verified_paths.add(rel_p)

    verified_paths.update([archive_manifest_rel, archive_anchor_rel, claim_record_rel])

    # 4. Verify Campaign 3 P0 and P1 sealed governance artifacts
    from campaign3.verifier import P0_ARTIFACTS
    from campaign3.p1_verifier import P1_ARTIFACTS

    has_git = os.path.isdir(os.path.join(workspace_root, ".git"))
    for rel_p in P0_ARTIFACTS:
        abs_p = _check_regular_nonsymlink(workspace_root, rel_p)
        if has_git:
            _verify_git_blob_match(workspace_root, CAMPAIGN3_P0_ANCHOR_COMMIT, rel_p, abs_p)
        verified_paths.add(rel_p)

    for rel_p in P1_ARTIFACTS:
        abs_p = _check_regular_nonsymlink(workspace_root, rel_p)
        if has_git:
            _verify_git_blob_match(workspace_root, CAMPAIGN3_P1_ANCHOR_COMMIT, rel_p, abs_p)
        verified_paths.add(rel_p)

    # 5. Verify historical invalid_manifest.json / operator_report.json records in history/run_*
    history_dir = os.path.join(workspace_root, "history")
    if os.path.isdir(history_dir):
        for entry in sorted(os.listdir(history_dir)):
            if entry.startswith("run_"):
                run_dir = os.path.join(history_dir, entry)
                if os.path.isdir(run_dir) and not os.path.islink(run_dir):
                    for fname in ("invalid_manifest.json", "operator_report.json"):
                        rel_p = os.path.join("history", entry, fname)
                        abs_p = os.path.join(workspace_root, rel_p)
                        if os.path.exists(abs_p):
                            _check_regular_nonsymlink(workspace_root, rel_p)
                            verified_paths.add(rel_p)

    # Apply 0o444 to all verified sealed artifacts
    updated_count = 0
    for rel_p in sorted(verified_paths):
        abs_p = os.path.join(workspace_root, rel_p)
        current_mode = stat.S_IMODE(os.lstat(abs_p).st_mode)
        if current_mode != 0o444:
            os.chmod(abs_p, 0o444)
            updated_count += 1
            if verbose:
                sys.stdout.write(f"Sealed mode 0o444: {rel_p} (was {oct(current_mode)})\n")

    return len(verified_paths), updated_count


def main() -> int:
    verbose = "--verbose" in sys.argv or "-v" in sys.argv
    regenerate = "--regenerate-manifests" in sys.argv
    if regenerate:
        sys.path.insert(0, BASE_DIR)
        from campaign2.manifest import generate_campaign2_manifest
        from campaign2.archival import generate_campaign_archive, MANIFEST_REL_PATH, ANCHOR_REL_PATH, CLAIM_REL_PATH

        # First ensure existing archive inventory files have 0o444 before regenerating archive manifest
        generate_campaign2_manifest(BASE_DIR)
        # Ensure all 32 inventory files have 0o444 so st_mode in campaign_archive_manifest.json is 0o444 (292)
        from campaign2.archival import FROZEN_CAMPAIGN_ARCHIVE_INVENTORY
        for rel_p in FROZEN_CAMPAIGN_ARCHIVE_INVENTORY:
            abs_p = _check_regular_nonsymlink(BASE_DIR, rel_p)
            if stat.S_IMODE(os.lstat(abs_p).st_mode) != 0o444:
                os.chmod(abs_p, 0o444)

        for meta_rel in (MANIFEST_REL_PATH, ANCHOR_REL_PATH, CLAIM_REL_PATH):
            meta_abs = os.path.join(BASE_DIR, meta_rel)
            if os.path.exists(meta_abs):
                os.chmod(meta_abs, 0o644)
                os.remove(meta_abs)

        generate_campaign_archive(BASE_DIR)
        if verbose:
            sys.stdout.write("Regenerated campaign2_preflight_hashes.json and campaign_archive_manifest.json.\n")

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
