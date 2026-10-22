"""Unit tests for release hygiene, licensing, allowlist scope, and metadata boundaries."""

import json
import os
import unittest

from scripts.check_release_hygiene import BASE_DIR, run_hygiene_checks


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


if __name__ == "__main__":
    unittest.main()
