"""Unit tests for release hygiene, licensing, allowlist scope, metadata boundaries, and fail-closed resealing."""

import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest

from scripts.check_release_hygiene import (
    BASE_DIR,
    run_hygiene_checks,
    verify_superseded_merge_bases,
)
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
        self.assertIn("release_preflight_hashes.json", data["allowed_root_files"])

        lineage = data["source_repository_lineage"]
        direct_shas = [a["commit_sha"] for a in lineage["direct_git_ancestors"]]
        self.assertEqual(
            direct_shas,
            [
                "5d5af00c564c600efde9394c94e9c39499257699",
                "f42a895a99fc5f3e7ccbfbfd9669ae4ce6ea0d3a",
            ],
        )
        non_ancestor_shas = [a["commit_sha"] for a in lineage["superseded_non_ancestor_checkouts"]]
        self.assertEqual(
            non_ancestor_shas,
            [
                "b54d6a61715ef5dd22bb054fa5783aaa4cffdac8",
                "0297eac73a82383d1146d834aab52a2a56cdcd06",
            ],
        )
        non_ancestor_mbs = {
            a["commit_sha"]: a["merge_base_with_candidate"]
            for a in lineage["superseded_non_ancestor_checkouts"]
        }
        self.assertEqual(
            non_ancestor_mbs,
            {
                "b54d6a61715ef5dd22bb054fa5783aaa4cffdac8": "96e04c94ef44b974301bbe9e4fedd2ba4281022d",
                "0297eac73a82383d1146d834aab52a2a56cdcd06": "dbe1f8f0d2198573e45822c8af8c0c7e77e1f4f0",
            },
        )
        self.assertEqual(data["allowed_publication_refs"], ["refs/heads/release/public-prototype-rc1"])
        self.assertNotIn("refs/heads/main", data["allowed_publication_refs"])
        self.assertIn("builder_worktree", lineage["dirty_working_tree_dispositions"])
        self.assertIn("round7_fix_worktree", lineage["dirty_working_tree_dispositions"])

    def test_superseded_merge_base_verification_exact_and_public_clone_unavailable_reporting(self):
        """
        Regression test for Round 2 Finding 3 & Round 3 Finding 1:
        1. Using a self-contained temporary Git repository with diverging sibling commits,
           verify_superseded_merge_bases mechanically verifies exact merge bases, rejects an
           ancestor commit that is not the actual merge base, and rejects linear ancestors
           listed as non-ancestors.
        2. Separately verify unavailable-object reporting (not_performed_historical_object_unavailable)
           when historical sibling commit objects are absent (e.g. in a public single-branch clone).
        """
        import sys

        def _git(cwd: str, *args: str) -> str:
            return subprocess.check_output(["git", *args], cwd=cwd, text=True).strip()

        def _commit_file(cwd: str, filename: str, content: str, msg: str) -> str:
            with open(os.path.join(cwd, filename), "w", encoding="utf-8") as f:
                f.write(content)
            _git(cwd, "add", filename)
            _git(cwd, "commit", "-q", "-m", msg)
            return _git(cwd, "rev-parse", "HEAD")

        # 1. Self-contained temporary Git repository with sibling branches and distinct merge bases
        with tempfile.TemporaryDirectory() as tmp_git:
            _git(tmp_git, "init", "-q")
            _git(tmp_git, "config", "user.name", "Test User")
            _git(tmp_git, "config", "user.email", "test@example.com")
            _git(tmp_git, "checkout", "-q", "-b", "release/public-prototype-rc1")

            c_init = _commit_file(tmp_git, "base.txt", "init\n", "Initial commit")

            # Sibling branch 1 diverges from c_init
            _git(tmp_git, "checkout", "-q", "-b", "sibling-branch-1", c_init)
            c_sib1 = _commit_file(tmp_git, "sib1.txt", "sib1\n", "Sibling 1 commit")

            # Advance release branch to c_mid
            _git(tmp_git, "checkout", "-q", "release/public-prototype-rc1")
            c_mid = _commit_file(tmp_git, "mid.txt", "mid\n", "Mid ancestor commit")

            # Sibling branch 2 diverges from c_mid
            _git(tmp_git, "checkout", "-q", "-b", "sibling-branch-2", c_mid)
            c_sib2 = _commit_file(tmp_git, "sib2.txt", "sib2\n", "Sibling 2 commit")

            # Advance release branch to HEAD (c_head)
            _git(tmp_git, "checkout", "-q", "release/public-prototype-rc1")
            c_head = _commit_file(tmp_git, "head.txt", "head\n", "Candidate HEAD commit")
            self.assertNotEqual(c_head, c_mid)

            # Positive case: exact merge bases (c_init for c_sib1, c_mid for c_sib2)
            valid_superseded = [
                {
                    "checkout": "sibling_1_worktree",
                    "commit_sha": c_sib1,
                    "merge_base_with_candidate": c_init,
                },
                {
                    "checkout": "sibling_2_worktree",
                    "commit_sha": c_sib2,
                    "merge_base_with_candidate": c_mid,
                },
            ]
            errs, statuses = verify_superseded_merge_bases(tmp_git, valid_superseded)
            self.assertEqual(errs, [])
            self.assertEqual(len(statuses), 2)
            self.assertEqual(statuses[0]["verification_status"], "verified_exact_merge_base")
            self.assertEqual(statuses[0]["actual_merge_base"], c_init)
            self.assertEqual(statuses[1]["verification_status"], "verified_exact_merge_base")
            self.assertEqual(statuses[1]["actual_merge_base"], c_mid)

            # Negative case A: wrong merge base that is still an ancestor of HEAD (c_mid instead of c_init for c_sib1)
            wrong_mb_superseded = [
                {
                    "checkout": "sibling_1_worktree",
                    "commit_sha": c_sib1,
                    "merge_base_with_candidate": c_mid,
                }
            ]
            wrong_errs, _ = verify_superseded_merge_bases(tmp_git, wrong_mb_superseded)
            self.assertEqual(len(wrong_errs), 1)
            self.assertIn(f"expected {c_init}, got {c_mid}", wrong_errs[0])

            # Negative case B: linear ancestor (c_mid) falsely listed as non-ancestor
            ancestor_superseded = [
                {
                    "checkout": "linear_ancestor_worktree",
                    "commit_sha": c_mid,
                    "merge_base_with_candidate": c_mid,
                }
            ]
            anc_errs, _ = verify_superseded_merge_bases(tmp_git, ancestor_superseded)
            self.assertEqual(len(anc_errs), 1)
            self.assertIn("listed as non-ancestor but is an ancestor of HEAD", anc_errs[0])

        # 2. Separate test for unavailable historical objects in a single-branch public clone
        allowlist_path = os.path.join(BASE_DIR, "RELEASE_ALLOWLIST.json")
        with open(allowlist_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        superseded = data["source_repository_lineage"]["superseded_non_ancestor_checkouts"]

        with tempfile.TemporaryDirectory() as tmp:
            clone_dir = os.path.join(tmp, "public_clone")
            subprocess.run(
                [
                    "git",
                    "clone",
                    "--quiet",
                    "--single-branch",
                    "--branch",
                    "release/public-prototype-rc1",
                    f"file://{BASE_DIR}",
                    clone_dir,
                ],
                check=True,
            )
            shutil.copy2(
                os.path.join(BASE_DIR, "RELEASE_ALLOWLIST.json"),
                os.path.join(clone_dir, "RELEASE_ALLOWLIST.json"),
            )
            shutil.copy2(
                os.path.join(BASE_DIR, "scripts", "check_release_hygiene.py"),
                os.path.join(clone_dir, "scripts", "check_release_hygiene.py"),
            )
            clone_errs, clone_statuses = verify_superseded_merge_bases(clone_dir, superseded)
            self.assertEqual(clone_errs, [])
            self.assertEqual(len(clone_statuses), 2)
            for st in clone_statuses:
                self.assertEqual(
                    st["verification_status"],
                    "not_performed_historical_object_unavailable",
                )

            proc = subprocess.run(
                [sys.executable, os.path.join(clone_dir, "scripts", "check_release_hygiene.py")],
                cwd=clone_dir,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("not_performed_historical_object_unavailable", proc.stdout)

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

    def test_reseal_rejects_absolute_paths_and_unexpected_inventory_without_changing_permissions(self):
        """
        Regression test for Round 2 Finding 1 (out-of-checkout permission change):
        1. Adding an absolute path key to release_preflight_hashes.json (or campaign2_evidence_hashes.json)
           along with a matching stripped-path file inside the checkout must raise ResealVerificationError
           and leave both the external sentinel (0o600) and all internal checkout files (0o644) untouched.
        2. Adding an unexpected relative path key to release_preflight_hashes.json or campaign2_evidence_hashes.json
           must raise ResealVerificationError before any permission change.
        """
        import hashlib

        with tempfile.TemporaryDirectory() as tmp:
            clone_dir = os.path.join(tmp, "clone")
            ext_dir = os.path.join(tmp, "external_secret")
            os.makedirs(ext_dir, exist_ok=True)
            ext_sentinel = os.path.join(ext_dir, "sentinel.txt")
            sentinel_bytes = b"external-private-sentinel\n"
            sentinel_sha = hashlib.sha256(sentinel_bytes).hexdigest()
            with open(ext_sentinel, "wb") as f:
                f.write(sentinel_bytes)
            os.chmod(ext_sentinel, 0o600)

            subprocess.run(["git", "clone", "--quiet", BASE_DIR, clone_dir], check=True)

            # Matching stripped path inside the checkout (reproducing the exact reviewer exploit)
            internal_mirror = os.path.join(clone_dir, ext_sentinel.lstrip(os.sep))
            os.makedirs(os.path.dirname(internal_mirror), exist_ok=True)
            with open(internal_mirror, "wb") as f:
                f.write(sentinel_bytes)
            os.chmod(internal_mirror, 0o644)

            release_preflight_p = os.path.join(clone_dir, "release_preflight_hashes.json")
            claim_p = os.path.join(clone_dir, "campaign_claim_record.json")
            hist_preflight_p = os.path.join(clone_dir, "campaign2_preflight_hashes.json")

            with open(release_preflight_p, "r", encoding="utf-8") as f:
                orig_preflight = json.load(f)

            # Subcase A: Absolute path key injected into release_preflight_hashes.json
            malformed_abs = dict(orig_preflight)
            malformed_abs[ext_sentinel] = sentinel_sha
            with open(release_preflight_p, "w", encoding="utf-8") as f:
                json.dump(malformed_abs, f, indent=2, sort_keys=True)

            with self.assertRaises(ResealVerificationError) as ctx_abs:
                ensure_readonly_artifact_modes(clone_dir)
            self.assertIn("Absolute", str(ctx_abs.exception))
            self.assertEqual(stat.S_IMODE(os.lstat(ext_sentinel).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.lstat(internal_mirror).st_mode), 0o644)
            self.assertEqual(stat.S_IMODE(os.lstat(claim_p).st_mode), 0o644)
            self.assertEqual(stat.S_IMODE(os.lstat(hist_preflight_p).st_mode), 0o644)

            # Subcase B: Unexpected in-checkout relative path key injected into release_preflight_hashes.json
            unexpected_rel = "unexpected_extra_artifact.txt"
            unexpected_abs = os.path.join(clone_dir, unexpected_rel)
            with open(unexpected_abs, "wb") as f:
                f.write(sentinel_bytes)
            os.chmod(unexpected_abs, 0o644)

            malformed_rel = dict(orig_preflight)
            malformed_rel[unexpected_rel] = sentinel_sha
            with open(release_preflight_p, "w", encoding="utf-8") as f:
                json.dump(malformed_rel, f, indent=2, sort_keys=True)

            with self.assertRaises(ResealVerificationError) as ctx_rel:
                ensure_readonly_artifact_modes(clone_dir)
            self.assertIn("Inventory mismatch", str(ctx_rel.exception))
            self.assertEqual(stat.S_IMODE(os.lstat(ext_sentinel).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.lstat(unexpected_abs).st_mode), 0o644)
            self.assertEqual(stat.S_IMODE(os.lstat(claim_p).st_mode), 0o644)
            self.assertEqual(stat.S_IMODE(os.lstat(hist_preflight_p).st_mode), 0o644)

            # Restore clean manifest and remove temporary injected files; verify resealing then succeeds
            with open(release_preflight_p, "w", encoding="utf-8") as f:
                json.dump(orig_preflight, f, indent=2, sort_keys=True)
                f.write("\n")
            os.remove(unexpected_abs)
            shutil.rmtree(os.path.join(clone_dir, ext_sentinel.lstrip(os.sep).split(os.sep)[0]))

            verified_count, _ = ensure_readonly_artifact_modes(clone_dir)
            self.assertEqual(verified_count, 194)
            self.assertEqual(stat.S_IMODE(os.lstat(ext_sentinel).st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(os.lstat(claim_p).st_mode), 0o444)

    def test_verify_env_offline_mode_succeeds_without_dotenv_or_api_keys(self):
        """
        R6 & Round 3 Finding 2 regression test:
        1. verify_env.py --offline succeeds without ~/.env or optional SDKs on Python >= 3.12.
        2. verify_env.check_offline_environment rejects Python < 3.12 for the pinned
           numpy==2.5.1 reproduction environment.
        """
        import sys
        from unittest import mock
        import verify_env

        ok, details = verify_env.check_offline_environment(BASE_DIR)
        self.assertTrue(ok, f"Offline environment check failed: {details}")
        self.assertEqual(details["mode"], "offline")
        self.assertTrue(details["python_ok"])
        self.assertTrue(details["numpy_ok"])
        self.assertTrue(details["sandbox_exec_ok"])
        self.assertTrue(details["sealed_artifacts_ok"])
        self.assertTrue(details["manifest_ok"])

        class _MockVersionInfo(tuple):
            major = 3
            minor = 11
            micro = 9

        with mock.patch.object(
            verify_env.sys, "version_info", _MockVersionInfo((3, 11, 9, "final", 0))
        ):
            ok_311, details_311 = verify_env.check_offline_environment(BASE_DIR)
            self.assertFalse(ok_311)
            self.assertFalse(details_311["python_ok"])
            self.assertTrue(
                any("Python 3.12+ required" in e for e in details_311["errors"])
            )

        with tempfile.TemporaryDirectory() as empty_home:
            env = dict(os.environ)
            env["HOME"] = empty_home
            for k in ("OPENAI_API_KEY", "GEMINI_API_KEY", "ANTHROPIC_API_KEY"):
                env.pop(k, None)
            proc = subprocess.run(
                [sys.executable, os.path.join(BASE_DIR, "verify_env.py"), "--offline"],
                cwd=BASE_DIR,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(proc.returncode, 0, f"verify_env.py --offline failed: {proc.stderr}\n{proc.stdout}")
            self.assertIn("Offline Environment & Integrity Check", proc.stdout)
            self.assertIn(">= 3.12 reproduction environment", proc.stdout)
            self.assertNotIn("~/.env", proc.stdout)


if __name__ == "__main__":
    unittest.main()
