"""
Campaign 3 Verifier Module for Work Order P1
"""

import os
import stat
import hashlib
import json
import subprocess
from typing import Any
from campaign3.governance import jcs_canonicalize

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

class P1VerifierError(Exception):
    pass

P1_ARTIFACTS = [
    "campaign3/p1_c2_review_decision.json",
    "campaign3/rfc_1_v3.md",
    "campaign3/rfc_1_v3_redline_ledger.json",
    "campaign3/p1_open_items.json",
]

def _check_no_self_referential_fields(data: Any, path: str) -> None:
    forbidden_keys = {"commit_sha", "commit_hash", "self_hash", "artifact_hash", "self_sha256"}
    if isinstance(data, dict):
        for k, v in data.items():
            if k in forbidden_keys:
                raise P1VerifierError(f"Forbidden self-referential field '{k}' found in {path}")
            _check_no_self_referential_fields(v, path)
    elif isinstance(data, list):
        for item in data:
            _check_no_self_referential_fields(item, path)

def verify_campaign3_p1(workspace_root: str = BASE_DIR, trusted_commit_sha: str = "") -> bool:
    if not trusted_commit_sha or not isinstance(trusted_commit_sha, str):
        raise ValueError("trusted_commit_sha is required for verification; unanchored verification is forbidden")

    trusted_commit_sha = trusted_commit_sha.strip()
    import re
    if not re.match(r"^[0-9a-fA-F]{40}$", trusted_commit_sha):
        raise ValueError(f"trusted_commit_sha must be exactly 40 hex characters, got '{trusted_commit_sha}'")

    real_root = os.path.realpath(workspace_root)

    res_commit = subprocess.run(
        ["git", "cat-file", "-t", trusted_commit_sha],
        cwd=real_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )
    if res_commit.returncode != 0 or res_commit.stdout.strip() != "commit":
        raise P1VerifierError(f"Trusted commit SHA '{trusted_commit_sha}' does not resolve to a valid commit object")

    for rel_path in P1_ARTIFACTS:
        abs_path = os.path.join(real_root, rel_path)
        if not os.path.exists(abs_path):
            raise P1VerifierError(f"Missing artifact: {rel_path}")

        if not os.path.isfile(abs_path) or os.path.islink(abs_path):
            raise P1VerifierError(f"Artifact {rel_path} must be a regular file, not a directory or symlink")

        st_mode = stat.S_IMODE(os.stat(abs_path).st_mode)
        if st_mode != 0o444:
            raise P1VerifierError(f"Artifact {rel_path} mode must be 0o444, got {oct(st_mode)}")

        with open(abs_path, "rb") as f:
            disk_bytes = f.read()

        try:
            git_blob = subprocess.check_output(
                ["git", "cat-file", "-p", f"{trusted_commit_sha}:{rel_path}"],
                cwd=real_root,
                stderr=subprocess.PIPE
            )
        except subprocess.CalledProcessError as e:
            raise P1VerifierError(f"Failed to retrieve git blob for {rel_path} at commit {trusted_commit_sha}: {e}")

        disk_hash = hashlib.sha256(disk_bytes).hexdigest()
        git_hash = hashlib.sha256(git_blob).hexdigest()

        if disk_hash != git_hash:
            raise P1VerifierError(
                f"Byte mismatch for {rel_path}! Disk hash: {disk_hash}, Git commit {trusted_commit_sha} blob hash: {git_hash}"
            )

        if rel_path.endswith(".json"):
            try:
                parsed = json.loads(disk_bytes.decode('utf-8'))
                _check_no_self_referential_fields(parsed, rel_path)
                canonical = jcs_canonicalize(parsed)
                if disk_bytes.strip() != canonical:
                    raise P1VerifierError(f"Artifact {rel_path} is not JCS canonicalized!")

                # Timestamp check
                for key, val in parsed.items():
                    if isinstance(key, str) and key.endswith("timestamp_utc") and isinstance(val, str):
                        import datetime
                        try:
                            ts = datetime.datetime.strptime(val, "%Y-%m-%dT%H:%M:%SZ")
                            ts = ts.replace(tzinfo=datetime.timezone.utc)
                            # Get commit timestamp
                            commit_ts_str = subprocess.check_output(
                                ["git", "show", "-s", "--format=%cI", trusted_commit_sha],
                                cwd=real_root,
                                text=True
                            ).strip()
                            commit_ts = datetime.datetime.fromisoformat(commit_ts_str)
                            if ts > commit_ts:
                                raise P1VerifierError(f"Timestamp {val} in {rel_path} is later than commit timestamp {commit_ts_str}")
                        except ValueError as ve:
                            raise P1VerifierError(f"Invalid UTC timestamp format in {rel_path}: {val}")
            except P1VerifierError:
                raise
            except Exception as ex:
                raise P1VerifierError(f"Failed JCS verification for {rel_path}: {ex}")

    return True
