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
    Verify SHA-256 content hashes, claim record invariants, and Git commit anchors across all
    sealed artifacts BEFORE restoring required file modes (`0o444` for sealed artifacts and
    exact recorded `st_mode` values for the 32 frozen Campaign 2 archive inventory artifacts).

    Returns (verified_count, chmod_updated_count).
    """
    _require_git_worktree(workspace_root)
    desired_modes: Dict[str, int] = {}

    # 1. Verify release_preflight_hashes.json (145 live Category 1 files) and historical campaign2_preflight_hashes.json
    release_preflight_rel = "release_preflight_hashes.json"
    hist_preflight_rel = "campaign2_preflight_hashes.json"
    release_preflight_abs = _check_regular_nonsymlink(workspace_root, release_preflight_rel)
    hist_preflight_abs = _check_regular_nonsymlink(workspace_root, hist_preflight_rel)

    _verify_git_blob_match(
        workspace_root,
        CAMPAIGN2_ARCHIVAL_ANCHOR_COMMIT,
        hist_preflight_rel,
        hist_preflight_abs,
    )

    with open(release_preflight_abs, "r", encoding="utf-8") as f:
        preflight_map: Dict[str, str] = json.load(f)

    for rel_path, expected_sha in sorted(preflight_map.items()):
        abs_p = _check_regular_nonsymlink(workspace_root, rel_path)
        actual_sha = _compute_sha256(abs_p)
        if actual_sha != expected_sha:
            raise ResealVerificationError(
                f"Release preflight manifest SHA-256 mismatch for {rel_path}: expected {expected_sha}, got {actual_sha}"
            )
        if rel_path != "README.md":
            desired_modes[rel_path] = 0o444
    desired_modes[release_preflight_rel] = 0o444
    desired_modes[hist_preflight_rel] = 0o444

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
        desired_modes[rel_path] = 0o444
    desired_modes[evidence_rel] = 0o444

    # 3. Verify historical campaign_archive_manifest.json entries (32 files), anchor, and claim record
    archive_manifest_rel = "campaign_archive_manifest.json"
    archive_anchor_rel = "campaign_archive_anchor.hash"
    claim_record_rel = "campaign_claim_record.json"

    archive_manifest_abs = _check_regular_nonsymlink(workspace_root, archive_manifest_rel)
    archive_anchor_abs = _check_regular_nonsymlink(workspace_root, archive_anchor_rel)
    claim_record_abs = _check_regular_nonsymlink(workspace_root, claim_record_rel)

    with open(archive_anchor_abs, "r", encoding="utf-8") as f:
        expected_manifest_sha = f.read().strip()
    actual_manifest_sha = _compute_sha256(archive_manifest_abs)
    if actual_manifest_sha != expected_manifest_sha:
        raise ResealVerificationError(
            f"Archive anchor mismatch for {archive_manifest_rel}: expected {expected_manifest_sha}, got {actual_manifest_sha}"
        )

    with open(archive_manifest_abs, "r", encoding="utf-8") as f:
        archive_data = json.load(f)

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
            workspace_root,
            CAMPAIGN2_ARCHIVAL_ANCHOR_COMMIT,
            rel_meta,
            abs_meta,
        )

    for item in archive_data.get("artifacts", []):
        rel_p = item["relative_path"]
        expected_sha = item["sha256"]
        expected_size = item["size_bytes"]
        expected_mode = int(item["st_mode"])
        abs_p = _check_regular_nonsymlink(workspace_root, rel_p)
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
        desired_modes[rel_p] = expected_mode

    desired_modes[archive_manifest_rel] = 0o444
    desired_modes[archive_anchor_rel] = 0o444
    desired_modes[claim_record_rel] = 0o444

    # 4. Verify Campaign 3 P0 and P1 sealed governance artifacts against Git anchors (fail-closed)
    from campaign3.verifier import P0_ARTIFACTS
    from campaign3.p1_verifier import P1_ARTIFACTS

    for rel_p in P0_ARTIFACTS:
        abs_p = _check_regular_nonsymlink(workspace_root, rel_p)
        _verify_git_blob_match(workspace_root, CAMPAIGN3_P0_ANCHOR_COMMIT, rel_p, abs_p)
        desired_modes[rel_p] = 0o444

    for rel_p in P1_ARTIFACTS:
        abs_p = _check_regular_nonsymlink(workspace_root, rel_p)
        _verify_git_blob_match(workspace_root, CAMPAIGN3_P1_ANCHOR_COMMIT, rel_p, abs_p)
        desired_modes[rel_p] = 0o444

    # Apply target modes ONLY after all verifications have succeeded
    updated_count = 0
    for rel_p, target_mode in sorted(desired_modes.items()):
        abs_p = os.path.join(workspace_root, rel_p)
        current_mode = stat.S_IMODE(os.lstat(abs_p).st_mode)
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
