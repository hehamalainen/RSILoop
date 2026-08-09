"""
Campaign 3 Verifier Module (Work Order P0 — W5 Corrective C2)
Provides strict, read-only verification requiring a trusted Git commit SHA.
Recomputes every artifact hash and compares against Git blobs from trusted_commit_sha.
Fails closed on hash mismatch, non-0o444 mode, symlink, non-regular file, missing anchor,
or self-referential fields. Never repairs.
"""

import os
import sys
import json
import stat
import re
import hashlib
import subprocess
from typing import Dict, Any, List
from campaign3.governance import jcs_canonicalize

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


class VerifierError(Exception):
    """Raised when verification fails."""
    pass


P0_ARTIFACTS = [
    "campaign3/rfc_1_v2.md",
    "campaign3/rfc1_draft_anchor.hash",
    "campaign3/project_plan_v1_3.md",
    "campaign3/plan_v1_3_adoption.json",
    "campaign3/root_discovery.json",
    "campaign3/root_registry.json",
    "campaign3/remote_rfc_status.json",
    "campaign3/whitespace_exception.json",
    "campaign3/historical_roots_initial_snapshot.json",
]


def verify_campaign3_p0(workspace_root: str = BASE_DIR, trusted_commit_sha: str = "") -> bool:
    """
    W5 — VERIFIER.
    Strict read-only verification requiring trusted_commit_sha.
    Compares every artifact's disk bytes and hash against Git blob from trusted_commit_sha.
    Fails closed on any defect.
    """
    if not trusted_commit_sha or not isinstance(trusted_commit_sha, str):
        raise ValueError("trusted_commit_sha is required for verification; unanchored verification is forbidden")

    trusted_commit_sha = trusted_commit_sha.strip()
    if not re.match(r"^[0-9a-fA-F]{40}$", trusted_commit_sha):
        raise ValueError(f"trusted_commit_sha must be exactly 40 hex characters, got '{trusted_commit_sha}'")

    # 1. Verify trusted_commit_sha exists in repository git log
    res_commit = subprocess.run(
        ["git", "cat-file", "-t", trusted_commit_sha],
        cwd=workspace_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    if res_commit.returncode != 0 or res_commit.stdout.strip() != "commit":
        raise VerifierError(f"Trusted commit SHA '{trusted_commit_sha}' does not resolve to a valid commit object")

    for rel_path in P0_ARTIFACTS:
        abs_path = os.path.join(workspace_root, rel_path)

        # a. Symlink & path component check
        _check_no_symlink_components(workspace_root, abs_path)

        # b. File existence & regular file check
        if not os.path.exists(abs_path):
            raise VerifierError(f"Missing artifact: {rel_path}")
        if not os.path.isfile(abs_path):
            raise VerifierError(f"Artifact must be a regular file: {rel_path}")

        # c. Permission mode 0o444 check
        st_mode = stat.S_IMODE(os.stat(abs_path).st_mode)
        if st_mode != 0o444:
            raise VerifierError(f"Artifact permission mode must be 0o444, got {oct(st_mode)} for {rel_path}")

        # d. Read disk bytes & compute SHA-256
        with open(abs_path, "rb") as f:
            disk_bytes = f.read()
        disk_sha = hashlib.sha256(disk_bytes).hexdigest()

        # e. Retrieve Git blob content from trusted_commit_sha
        res_blob = subprocess.run(
            ["git", "cat-file", "-p", f"{trusted_commit_sha}:{rel_path}"],
            cwd=workspace_root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
        if res_blob.returncode != 0:
            raise VerifierError(f"Missing artifact blob in commit {trusted_commit_sha}: {rel_path}")

        git_bytes = res_blob.stdout
        git_sha = hashlib.sha256(git_bytes).hexdigest()

        # f. Compare disk hash vs git blob hash
        if disk_sha != git_sha:
            raise VerifierError(f"Hash mismatch for {rel_path}! Disk: {disk_sha}, Git blob: {git_sha}")

        # g. JSON canonicalization & self-referential check
        if rel_path.endswith(".json"):
            try:
                json_data = json.loads(disk_bytes.decode('utf-8'))
            except Exception as e:
                raise VerifierError(f"Invalid JSON in artifact {rel_path}: {e}")

            # Verify JCS formatting
            jcs_bytes = jcs_canonicalize(json_data)
            stripped_disk = disk_bytes.rstrip(b"\r\n")
            if jcs_bytes != stripped_disk:
                raise VerifierError(f"JSON artifact {rel_path} is not canonical JCS (RFC 8785) formatted")

            # Verify no self-referential hash or commit field
            _verify_no_self_referential_fields(json_data, rel_path)

    return True


def _check_no_symlink_components(workspace_root: str, path: str) -> None:
    rel = os.path.relpath(path, workspace_root)
    curr = os.path.realpath(workspace_root)
    if os.path.islink(curr):
        raise VerifierError(f"Workspace root component is a symlink: {curr}")
    for part in rel.split(os.sep):
        if not part or part == ".":
            continue
        curr = os.path.join(curr, part)
        if os.path.islink(curr):
            raise VerifierError(f"Path component is a symlink: {curr}")


def _verify_no_self_referential_fields(data: Any, path: str) -> None:
    forbidden_keys = {"commit_sha", "commit_hash", "self_hash", "artifact_hash", "self_sha256"}
    if isinstance(data, dict):
        for k, v in data.items():
            if k in forbidden_keys:
                raise VerifierError(f"Forbidden self-referential field '{k}' found in {path}")
            _verify_no_self_referential_fields(v, path)
    elif isinstance(data, list):
        for item in data:
            _verify_no_self_referential_fields(item, path)
