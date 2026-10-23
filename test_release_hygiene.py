"""Unit tests for release hygiene, licensing, allowlist scope, metadata boundaries, and fail-closed resealing."""

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

from scripts.check_release_hygiene import BASE_DIR, run_hygiene_checks
from scripts.reseal_readonly_artifacts import (
    ResealVerificationError,
    ensure_readonly_artifact_modes,
)


class TestReleaseHygiene(unittest.TestCase):
    def test_release_hygiene_passes_on_canonical_checkout(self):
        ok, errors = run_hygiene_checks(BASE_DIR)
        self.assertTrue(ok, f"Release hygiene checks failed: {errors}")
        self.assertEqual(errors, [])

    def test_license_and_third_party_notices_present(self):
        license_path = os.path.join(BASE_DIR, "LICENSE")
        notices_path = os.path.join(BASE_DIR, "THIRD_PARTY_NOTICES.md")
        contributing_path = os.path.join(BASE_DIR, "CONTRIBUTING.md")
        security_path = os.path.join(BASE_DIR, "SECURITY.md")
        self.assertTrue(os.path.isfile(license_path))
        self.assertTrue(os.path.isfile(notices_path))
        self.assertTrue(os.path.isfile(contributing_path))
        self.assertTrue(os.path.isfile(security_path))
        with open(license_path, "r", encoding="utf-8") as f:
            self.assertIn("MIT License", f.read())
        with open(notices_path, "r", encoding="utf-8") as f:
            notices = f.read()
            self.assertIn("First-Party Code", notices)
            self.assertIn("numpy", notices)
        with open(security_path, "r", encoding="utf-8") as f:
            self.assertIn("security/advisories/new", f.read())

    def test_release_allowlist_binds_base_commit_and_sidecar(self):
        allowlist_path = os.path.join(BASE_DIR, "RELEASE_ALLOWLIST.json")
        with open(allowlist_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["base_commit_sha"], "e6d5630adfdb91085a70e13ddf627757edbfaa98")
        self.assertIn("discovery_probe", data["allowed_top_level_directories"])
        self.assertIn("test_discovery_probe.py", data["allowed_root_files"])

    def test_reseal_restores_0444_on_fresh_clone_and_rejects_tampered_claim_or_gitfile_governance(self):
        """
        R1 regression test:
        1. In a clean git clone (where files start at 0o644), ensure_readonly_artifact_modes restores 0o444.
        2. Replacing sanctioned_claim in campaign_claim_record.json raises ResealVerificationError BEFORE changing permissions.
        3. Modifying a governance artifact in a checkout using a `.git` file (`gitdir: ...`) raises ResealVerificationError BEFORE changing permissions.
        4. Running without Git metadata raises ResealVerificationError BEFORE changing permissions.
        """
        with tempfile.TemporaryDirectory() as tmp:
            clone_dir = os.path.join(tmp, "clone")
            subprocess.run(["git", "clone", "--quiet", BASE_DIR, clone_dir], check=True)

            claim_p = os.path.join(clone_dir, "campaign_claim_record.json")
            gov_p = os.path.join(clone_dir, "campaign3", "p0_operator_decision.json")
            preflight_p = os.path.join(clone_dir, "campaign2_preflight_hashes.json")

            # Case 1: Tamper with campaign_claim_record.json while files are 0o644
            with open(claim_p, "rb") as f:
                orig_claim_bytes = f.read()
            tampered_claim = json.loads(orig_claim_bytes.decode("utf-8"))
            tampered_claim["sanctioned_claim"] = "Synthetic unapproved claim text."
            os.chmod(claim_p, 0o644)
            os.chmod(preflight_p, 0o644)
            with open(claim_p, "w", encoding="utf-8") as f:
                json.dump(tampered_claim, f, indent=2, sort_keys=True)
                f.write("\n")

            with self.assertRaises(ResealVerificationError) as ctx_claim:
                ensure_readonly_artifact_modes(clone_dir)
            self.assertIn("sanctioned_claim", str(ctx_claim.exception))
            # Verify neither the tampered claim file nor preflight manifest was sealed to 0o444
            self.assertEqual(stat.S_IMODE(os.lstat(claim_p).st_mode), 0o644)
            self.assertEqual(stat.S_IMODE(os.lstat(preflight_p).st_mode), 0o644)

            # Restore authentic claim record
            with open(claim_p, "wb") as f:
                f.write(orig_claim_bytes)

            # Case 2: Convert .git directory into a .git file (`gitdir: ...`) and tamper with P0 governance record
            real_git_dir = os.path.join(tmp, "separate_git_dir")
            shutil.move(os.path.join(clone_dir, ".git"), real_git_dir)
            dot_git_file = os.path.join(clone_dir, ".git")
            with open(dot_git_file, "w", encoding="utf-8") as f:
                f.write(f"gitdir: {real_git_dir}\n")

            with open(gov_p, "rb") as f:
                orig_gov_bytes = f.read()
            os.chmod(gov_p, 0o644)
            with open(gov_p, "wb") as f:
                f.write(orig_gov_bytes.replace(b"ACCEPT_P0", b"TAMPER_P0"))

            with self.assertRaises(ResealVerificationError) as ctx_gov:
                ensure_readonly_artifact_modes(clone_dir)
            self.assertIn("campaign3/p0_operator_decision.json", str(ctx_gov.exception))
            self.assertEqual(stat.S_IMODE(os.lstat(gov_p).st_mode), 0o644)
            self.assertEqual(stat.S_IMODE(os.lstat(preflight_p).st_mode), 0o644)

            # Restore authentic governance record; verify `.git` file checkout succeeds and seals to 0o444
            with open(gov_p, "wb") as f:
                f.write(orig_gov_bytes)
            verified_count, updated_count = ensure_readonly_artifact_modes(clone_dir)
            self.assertGreaterEqual(verified_count, 193)
            self.assertGreater(updated_count, 0)
            self.assertEqual(stat.S_IMODE(os.lstat(claim_p).st_mode), 0o444)
            self.assertEqual(stat.S_IMODE(os.lstat(gov_p).st_mode), 0o444)
            self.assertEqual(stat.S_IMODE(os.lstat(preflight_p).st_mode), 0o444)

            # Case 3: Remove .git file -> must fail closed without changing permissions
            os.chmod(preflight_p, 0o644)
            os.remove(dot_git_file)
            with self.assertRaises(ResealVerificationError) as ctx_nogit:
                ensure_readonly_artifact_modes(clone_dir)
            self.assertIn("not a valid Git work tree", str(ctx_nogit.exception))
            self.assertEqual(stat.S_IMODE(os.lstat(preflight_p).st_mode), 0o644)


if __name__ == "__main__":
    unittest.main()
