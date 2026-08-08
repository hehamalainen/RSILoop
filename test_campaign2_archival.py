"""
Campaign 2 Archival & Sealing Unit Test Suite.

Tests for Work Order A2 requirements (A2-1 through A2-9).
All failure-mode, tampering, torn read, and mutation tests execute strictly inside
isolated temporary directory fixtures (tempfile.TemporaryDirectory) to ensure
canonical workspace files are NEVER mutated or overwritten during test execution.
"""

import os
import sys
import json
import stat
import shutil
import unittest
import tempfile
import hashlib
import subprocess
from unittest import mock

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from campaign2.archival import (
    generate_campaign_archive,
    verify_campaign_archive,
    FROZEN_CAMPAIGN_ARCHIVE_INVENTORY,
    SANCTIONED_CLAIM_TEXT,
    FORBIDDEN_CLAIM_PHRASES,
    CANONICAL_REPORT_SHA256,
    MANIFEST_REL_PATH,
    ANCHOR_REL_PATH,
    CLAIM_REL_PATH,
)


def _populate_temp_workspace(temp_dir: str) -> None:
    """Helper: copy all 32 inventory files from BASE_DIR into temp_dir."""
    for rel_p in FROZEN_CAMPAIGN_ARCHIVE_INVENTORY:
        src = os.path.join(BASE_DIR, rel_p)
        if not os.path.exists(src):
            raise FileNotFoundError(f"Source inventory file missing in BASE_DIR: {src}")
        dst = os.path.join(temp_dir, rel_p)
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        if os.path.exists(dst):
            os.chmod(dst, 0o644)
        shutil.copy2(src, dst)
        st_mode = stat.S_IMODE(os.stat(src).st_mode)
        os.chmod(dst, st_mode)


def _setup_temp_git_repo(temp_dir: str) -> str:
    """Initialize a git repository in temp_dir, commit all files, and return HEAD SHA."""
    subprocess.run(["git", "init"], cwd=temp_dir, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=temp_dir, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=temp_dir, check=True)
    subprocess.run(["git", "add", "."], cwd=temp_dir, check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=temp_dir, check=True)
    return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=temp_dir).decode("utf-8").strip()


class TestCampaignArchival(unittest.TestCase):
    """Unit tests covering Work Order A2 Campaign Archival requirements."""

    def test_a2_1_frozen_inventory_immune_to_new_run_directories(self):
        """A2-1: Test that adding an extra run directory does not change manifest or verification."""
        with tempfile.TemporaryDirectory() as temp_dir:
            _populate_temp_workspace(temp_dir)
            generate_campaign_archive(temp_dir)
            trusted_sha = _setup_temp_git_repo(temp_dir)
            verify_campaign_archive(relocated_root=temp_dir, trusted_commit_sha=trusted_sha)

            # Inject extraneous run directory
            extra_dir = os.path.join(temp_dir, "history", "run_9999999999_extra")
            os.makedirs(extra_dir, exist_ok=True)
            with open(os.path.join(extra_dir, "untracked.json"), "w", encoding="utf-8") as f:
                f.write('{"untracked": true}\n')

            # Verification of relocated archive must still succeed and contain exactly 32 artifacts
            ver = verify_campaign_archive(relocated_root=temp_dir, trusted_commit_sha=trusted_sha)
            self.assertEqual(len(ver["artifacts"]), len(FROZEN_CAMPAIGN_ARCHIVE_INVENTORY))
            art_paths = [a["relative_path"] for a in ver["artifacts"]]
            self.assertNotIn("history/run_9999999999_extra/untracked.json", art_paths)

    def test_a2_2_immutability_of_originals(self):
        """A2-2: Test that all 32 original files remain 100% byte, size, and mode identical around generation."""
        pre_snapshots = {}
        for rel_p in FROZEN_CAMPAIGN_ARCHIVE_INVENTORY:
            abs_p = os.path.join(BASE_DIR, rel_p)
            self.assertTrue(os.path.exists(abs_p), f"Original artifact missing: {rel_p}")
            self.assertFalse(os.path.islink(abs_p), f"Original artifact is a symlink: {rel_p}")
            self.assertTrue(os.path.isfile(abs_p), f"Original artifact is not a regular file: {rel_p}")
            st = os.stat(abs_p)
            with open(abs_p, "rb") as f:
                content = f.read()
            pre_snapshots[rel_p] = {
                "sha256": hashlib.sha256(content).hexdigest(),
                "size": st.st_size,
                "mode": stat.S_IMODE(st.st_mode),
            }

        with tempfile.TemporaryDirectory() as temp_dir:
            _populate_temp_workspace(temp_dir)
            generate_campaign_archive(temp_dir)

        # Assert post-snapshots match pre-snapshots exactly
        for rel_p, pre_meta in pre_snapshots.items():
            abs_p = os.path.join(BASE_DIR, rel_p)
            st = os.stat(abs_p)
            with open(abs_p, "rb") as f:
                content = f.read()
            post_sha = hashlib.sha256(content).hexdigest()
            post_size = st.st_size
            post_mode = stat.S_IMODE(st.st_mode)

            self.assertEqual(pre_meta["sha256"], post_sha, f"SHA mismatch after generation for {rel_p}")
            self.assertEqual(pre_meta["size"], post_size, f"Size mismatch after generation for {rel_p}")
            self.assertEqual(pre_meta["mode"], post_mode, f"Mode mismatch after generation for {rel_p}")

    def test_a2_3_consistent_snapshot_torn_read(self):
        """A2-3: Test that lstat mismatch during snapshot generation triggers torn read exception."""
        with tempfile.TemporaryDirectory() as temp_dir:
            _populate_temp_workspace(temp_dir)
            target_file = os.path.join(temp_dir, FROZEN_CAMPAIGN_ARCHIVE_INVENTORY[0])
            real_lstat = os.lstat
            st_real = real_lstat(target_file)

            class MockStat:
                def __init__(self, real_st, mtime_delta):
                    self.st_ino = real_st.st_ino
                    self.st_size = real_st.st_size
                    self.st_mode = real_st.st_mode
                    self.st_mtime = real_st.st_mtime + mtime_delta

            direct_lstat_count = 0

            def mock_lstat(path):
                nonlocal direct_lstat_count
                if str(path).endswith(FROZEN_CAMPAIGN_ARCHIVE_INVENTORY[0]):
                    direct_lstat_count += 1
                    if direct_lstat_count == 3:
                        return MockStat(st_real, 10.0)
                return real_lstat(path)

            with mock.patch("campaign2.archival.os.lstat", side_effect=mock_lstat):
                with self.assertRaises(ValueError) as exc_ctx:
                    generate_campaign_archive(temp_dir)
                self.assertIn("torn read", str(exc_ctx.exception).lower())

    def test_a2_4_relocated_copy_verification(self):
        """A2-4: Test self-contained verification from a relocated repository copy."""
        with tempfile.TemporaryDirectory() as temp_dir:
            _populate_temp_workspace(temp_dir)
            generate_campaign_archive(temp_dir)
            trusted_sha = _setup_temp_git_repo(temp_dir)
            res = verify_campaign_archive(relocated_root=temp_dir, trusted_commit_sha=trusted_sha)
            self.assertEqual(res["schema_version"], "3")
            self.assertEqual(len(res["artifacts"]), len(FROZEN_CAMPAIGN_ARCHIVE_INVENTORY))
            for art in res["artifacts"]:
                self.assertIn("relative_path", art)
                self.assertIn("absolute_path", art)

    def test_a2_5_independent_report_hash_verification(self):
        """A2-5: Test that mutating canonical report causes generate_campaign_archive to fail."""
        with tempfile.TemporaryDirectory() as temp_dir:
            _populate_temp_workspace(temp_dir)
            rep_path = os.path.join(temp_dir, "history/run_1786134848_cfb514d2/campaign2_pilot_report.json")
            os.chmod(rep_path, 0o644)
            with open(rep_path, "w", encoding="utf-8") as f:
                f.write('{"tampered": true}\n')
            os.chmod(rep_path, 0o444)

            with self.assertRaises(ValueError) as exc_ctx:
                generate_campaign_archive(temp_dir)
            self.assertIn("Canonical Campaign 2 report hash mismatch", str(exc_ctx.exception))

    def test_a2_6_git_anchor_tamper_rejection(self):
        """A2-6: Test real Git repository commit & blob comparison for tamper rejection."""
        with tempfile.TemporaryDirectory() as temp_dir:
            _populate_temp_workspace(temp_dir)
            manifest_p, anchor_p, claim_p = generate_campaign_archive(temp_dir)
            trusted_sha = _setup_temp_git_repo(temp_dir)

            # Verification against valid commit SHA succeeds
            verify_campaign_archive(relocated_root=temp_dir, trusted_commit_sha=trusted_sha)

            # Mutate manifest AND update anchor file to match tampered manifest hash
            os.chmod(manifest_p, 0o644)
            with open(manifest_p, "r", encoding="utf-8") as f:
                data = json.load(f)
            data["tampered_key"] = "tampered_value"
            tampered_bytes = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")
            with open(manifest_p, "wb") as f:
                f.write(tampered_bytes)
            os.chmod(manifest_p, 0o444)

            tampered_hash = hashlib.sha256(tampered_bytes).hexdigest()
            os.chmod(anchor_p, 0o644)
            with open(anchor_p, "w", encoding="utf-8") as f:
                f.write(tampered_hash + "\n")
            os.chmod(anchor_p, 0o444)

            os.chmod(claim_p, 0o644)
            with open(claim_p, "r", encoding="utf-8") as f:
                c_data = json.load(f)
            c_data["manifest_sha256"] = tampered_hash
            with open(claim_p, "w", encoding="utf-8") as f:
                json.dump(c_data, f, indent=2, sort_keys=True)
            os.chmod(claim_p, 0o444)

            # Re-verification against trusted commit SHA must detect disk mismatch vs Git blob
            with self.assertRaises(ValueError) as exc_ctx:
                verify_campaign_archive(relocated_root=temp_dir, trusted_commit_sha=trusted_sha)
            self.assertIn("Git anchor verification failure", str(exc_ctx.exception))

    def test_missing_trusted_commit_sha_rejection(self):
        """Test that verify_campaign_archive rejects calls missing trusted_commit_sha."""
        with tempfile.TemporaryDirectory() as temp_dir:
            _populate_temp_workspace(temp_dir)
            generate_campaign_archive(temp_dir)
            with self.assertRaises(ValueError) as exc_ctx:
                verify_campaign_archive(relocated_root=temp_dir)
            self.assertIn("trusted_commit_sha is required", str(exc_ctx.exception))

    def test_a2_7_claim_record_verbatim_and_forbidden_phrases(self):
        """A2-7: Test claim record string verbatim assertion and forbidden phrase check."""
        with tempfile.TemporaryDirectory() as temp_dir:
            _populate_temp_workspace(temp_dir)
            generate_campaign_archive(temp_dir)
            trusted_sha = _setup_temp_git_repo(temp_dir)
            claim_p = os.path.join(temp_dir, CLAIM_REL_PATH)

            # Case 1: Verbatim text mismatch
            os.chmod(claim_p, 0o644)
            with open(claim_p, "w", encoding="utf-8") as f:
                json.dump({"schema_version": "3", "sanctioned_claim": "Incorrect claim text"}, f)
            os.chmod(claim_p, 0o444)

            with self.assertRaises(ValueError) as exc_ctx:
                verify_campaign_archive(relocated_root=temp_dir, trusted_commit_sha=trusted_sha)
            self.assertIn("sanctioned_claim mismatch", str(exc_ctx.exception))

            # Case 2: Forbidden phrase injection
            for phrase in FORBIDDEN_CLAIM_PHRASES:
                os.chmod(claim_p, 0o644)
                with open(claim_p, "w", encoding="utf-8") as f:
                    json.dump({"schema_version": "3", "sanctioned_claim": f"{SANCTIONED_CLAIM_TEXT} {phrase}"}, f)
                os.chmod(claim_p, 0o444)

                with self.assertRaises(ValueError) as exc_ctx:
                    verify_campaign_archive(relocated_root=temp_dir, trusted_commit_sha=trusted_sha)
                self.assertIn("sanctioned_claim mismatch", str(exc_ctx.exception))

    def test_a2_8_verifier_failure_modes(self):
        """A2-8: Test verifier failure modes (missing, hash, size, mode, non-regular, symlink, component symlink)."""
        target_rel = FROZEN_CAMPAIGN_ARCHIVE_INVENTORY[0]

        # Failure Mode 1: Missing artifact file
        with tempfile.TemporaryDirectory() as temp_dir:
            _populate_temp_workspace(temp_dir)
            generate_campaign_archive(temp_dir)
            trusted_sha = _setup_temp_git_repo(temp_dir)
            target_abs = os.path.join(temp_dir, target_rel)
            os.chmod(target_abs, 0o644)
            os.remove(target_abs)
            with self.assertRaises((ValueError, FileNotFoundError)):
                verify_campaign_archive(relocated_root=temp_dir, trusted_commit_sha=trusted_sha)

        # Failure Mode 2: Hash mismatch
        with tempfile.TemporaryDirectory() as temp_dir:
            _populate_temp_workspace(temp_dir)
            generate_campaign_archive(temp_dir)
            trusted_sha = _setup_temp_git_repo(temp_dir)
            target_abs = os.path.join(temp_dir, target_rel)
            os.chmod(target_abs, 0o644)
            with open(target_abs, "wb") as f:
                f.write(b"tampered content\n")
            os.chmod(target_abs, 0o444)
            with self.assertRaises(ValueError) as exc_ctx:
                verify_campaign_archive(relocated_root=temp_dir, trusted_commit_sha=trusted_sha)
            self.assertIn("SHA-256 mismatch", str(exc_ctx.exception))

        # Failure Mode 3: Symlink artifact file
        with tempfile.TemporaryDirectory() as temp_dir:
            _populate_temp_workspace(temp_dir)
            generate_campaign_archive(temp_dir)
            trusted_sha = _setup_temp_git_repo(temp_dir)
            target_abs = os.path.join(temp_dir, target_rel)
            os.chmod(target_abs, 0o644)
            os.remove(target_abs)
            os.symlink(os.path.join(temp_dir, "best_manifest.json"), target_abs)
            with self.assertRaises(ValueError) as exc_ctx:
                verify_campaign_archive(relocated_root=temp_dir, trusted_commit_sha=trusted_sha)
            self.assertIn("symlink", str(exc_ctx.exception).lower())

        # Failure Mode 4: Mode mismatch
        with tempfile.TemporaryDirectory() as temp_dir:
            _populate_temp_workspace(temp_dir)
            generate_campaign_archive(temp_dir)
            trusted_sha = _setup_temp_git_repo(temp_dir)
            target_abs = os.path.join(temp_dir, target_rel)
            current_mode = stat.S_IMODE(os.stat(target_abs).st_mode)
            new_mode = 0o777 if current_mode != 0o777 else 0o644
            os.chmod(target_abs, new_mode)
            with self.assertRaises(ValueError) as exc_ctx:
                verify_campaign_archive(relocated_root=temp_dir, trusted_commit_sha=trusted_sha)
            self.assertIn("Mode mismatch", str(exc_ctx.exception))

        # Failure Mode 5: Non-regular file (directory at artifact location)
        with tempfile.TemporaryDirectory() as temp_dir:
            _populate_temp_workspace(temp_dir)
            generate_campaign_archive(temp_dir)
            trusted_sha = _setup_temp_git_repo(temp_dir)
            target_abs = os.path.join(temp_dir, target_rel)
            os.chmod(target_abs, 0o644)
            os.remove(target_abs)
            os.mkdir(target_abs)
            with self.assertRaises(ValueError) as exc_ctx:
                verify_campaign_archive(relocated_root=temp_dir, trusted_commit_sha=trusted_sha)
            self.assertIn("not a regular file", str(exc_ctx.exception).lower())

        # Failure Mode 6: Intermediate component symlink (e.g. history directory replaced by symlink)
        with tempfile.TemporaryDirectory() as temp_dir:
            _populate_temp_workspace(temp_dir)
            generate_campaign_archive(temp_dir)
            trusted_sha = _setup_temp_git_repo(temp_dir)
            hist_dir = os.path.join(temp_dir, "history")
            real_hist = os.path.join(temp_dir, "history_real")
            shutil.move(hist_dir, real_hist)
            os.symlink(real_hist, hist_dir)
            with self.assertRaises(ValueError) as exc_ctx:
                verify_campaign_archive(relocated_root=temp_dir, trusted_commit_sha=trusted_sha)
            self.assertIn("symlink", str(exc_ctx.exception).lower())

    def test_a2_9_idempotence_and_differing_content_rejection(self):
        """A2-9: Test idempotence and rejection of differing manifest overwrite without mutating permissions."""
        with tempfile.TemporaryDirectory() as temp_dir:
            _populate_temp_workspace(temp_dir)
            m1, a1, c1 = generate_campaign_archive(temp_dir)
            m2, a2, c2 = generate_campaign_archive(temp_dir)
            self.assertEqual(m1, m2)

            target_abs = os.path.join(temp_dir, FROZEN_CAMPAIGN_ARCHIVE_INVENTORY[0])
            os.chmod(target_abs, 0o644)
            with open(target_abs, "a", encoding="utf-8") as f:
                f.write("\n// appended content\n")
            os.chmod(target_abs, 0o444)

            m_mode_before = stat.S_IMODE(os.stat(m1).st_mode)

            with self.assertRaises(ValueError) as exc_ctx:
                generate_campaign_archive(temp_dir)
            self.assertIn("Refusing to overwrite", str(exc_ctx.exception))

            m_mode_after = stat.S_IMODE(os.stat(m1).st_mode)
            self.assertEqual(m_mode_before, m_mode_after)


if __name__ == "__main__":
    unittest.main()
