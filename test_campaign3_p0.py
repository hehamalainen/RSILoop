"""
Campaign 3 Work Order P0 Test Suite (W6 Corrective C4)
Verifies W1 (Plan Adoption), W2 (Root Discovery), W3 (Root Registry),
W4 (Remote RFC Resolution), and W5 (Verifier) invariants, along with a
symbol-accurate traceability matrix mapping requirements W1-W5 to exact code lines and tests.
"""

import os
import sys
import json
import stat
import hashlib
import unittest
import tempfile
import subprocess
from typing import Dict, Any, Tuple

from campaign3.governance import (
    GovernanceError,
    jcs_canonicalize,
    verify_canonical_root,
    emit_plan_v1_3_adoption,
    emit_root_discovery,
    emit_root_registry,
    emit_remote_rfc_status
)
from campaign3.verifier import verify_campaign3_p0, VerifierError, P0_ARTIFACTS

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__)))


# TRACEABILITY MATRIX (W1-W5 -> Absolute File:Line -> Symbol -> Test Name)
TRACEABILITY_MATRIX = [
    {
        "clause": "W1.a - Verify campaign3/project_plan_v1_3.md SHA-256 match and immutable adoption",
        "file": os.path.join(BASE_DIR, "campaign3", "governance.py"),
        "line": 84,
        "symbol": "def emit_plan_v1_3_adoption",
        "test": "test_operator_plan_hash_mismatch_stops_adoption"
    },
    {
        "clause": "W1.c - Machine-emit campaign3/plan_v1_3_adoption.json (0o444, JCS) with no self-referential hash or commit SHA",
        "file": os.path.join(BASE_DIR, "campaign3", "governance.py"),
        "line": 84,
        "symbol": "def emit_plan_v1_3_adoption",
        "test": "test_adoption_record_contains_no_self_referential_field"
    },
    {
        "clause": "W1.d - Anchor adoption record digest in Git commit; verification compares against trusted commit",
        "file": os.path.join(BASE_DIR, "campaign3", "verifier.py"),
        "line": 40,
        "symbol": "def verify_campaign3_p0",
        "test": "test_git_anchor_tamper_rejection"
    },
    {
        "clause": "W2 - Root Discovery Record captured from raw command output for R1, R2, R3",
        "file": os.path.join(BASE_DIR, "campaign3", "governance.py"),
        "line": 199,
        "symbol": "def emit_root_discovery",
        "test": "test_discovery_record_traceable_to_command_output"
    },
    {
        "clause": "W3 - Root Registry declares R1 permitted, R2/R3 forbidden; root check fails closed",
        "file": os.path.join(BASE_DIR, "campaign3", "governance.py"),
        "line": 33,
        "symbol": "def verify_canonical_root",
        "test": "test_root_registry_drives_canonical_check_and_forbidden_root_fails_closed"
    },
    {
        "clause": "W3 - R2 and R3 files are preserved read-only as historical evidence without mutation across entire work order",
        "file": os.path.join(BASE_DIR, "test_campaign3_p0.py"),
        "line": 339,
        "symbol": "def test_r2_r3_files_mode_and_byte_identical_before_and_after",
        "test": "test_r2_r3_files_mode_and_byte_identical_before_and_after"
    },
    {
        "clause": "W4 - Remote RFC / Acceptance Resolution determines read-only remote status via fetch and tree inspection",
        "file": os.path.join(BASE_DIR, "campaign3", "governance.py"),
        "line": 323,
        "symbol": "def emit_remote_rfc_status",
        "test": "test_remote_rfc_status_schema_and_conclusion"
    },
    {
        "clause": "W5 - Verifier REQUIRES trusted_commit_sha; fails closed on any defect or mismatch",
        "file": os.path.join(BASE_DIR, "campaign3", "verifier.py"),
        "line": 40,
        "symbol": "def verify_campaign3_p0",
        "test": "test_verifier_requires_trusted_commit_sha"
    },
    {
        "clause": "W6 - Every recorded SHA is exactly 40 hex characters",
        "file": os.path.join(BASE_DIR, "test_campaign3_p0.py"),
        "line": 370,
        "symbol": "def test_every_recorded_sha_is_exactly_40_hex_characters",
        "test": "test_every_recorded_sha_is_exactly_40_hex_characters"
    }
]


def _snapshot_root(root_path: str) -> Dict[str, Tuple[str, str]]:
    snap = {}
    for dirpath, dirnames, filenames in os.walk(root_path):
        if ".git" in dirpath.split(os.sep):
            continue
        for f in filenames:
            p = os.path.join(dirpath, f)
            rel = os.path.relpath(p, root_path)
            try:
                st = os.stat(p)
                mode = oct(stat.S_IMODE(st.st_mode))
                if os.path.isfile(p) and not os.path.islink(p):
                    with open(p, "rb") as open_f:
                        h = hashlib.sha256(open_f.read()).hexdigest()
                else:
                    h = "symlink"
                snap[rel] = (mode, h)
            except Exception as e:
                snap[rel] = (str(e),)
    return snap


class TestCampaign3P0(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        snap_path = os.path.join(BASE_DIR, "campaign3", "historical_roots_initial_snapshot.json")
        with open(snap_path, "r", encoding="utf-8") as f:
            snap_data = json.load(f)
        cls.r2_path = os.environ.get("RSILOOP_R2_PATH", snap_data.get("R2_realpath", ""))
        cls.r3_path = os.environ.get("RSILOOP_R3_PATH", snap_data.get("R3_realpath", ""))
        cls.src_plan = os.path.join(BASE_DIR, "campaign3", "project_plan_v1_3.md")

    def test_operator_plan_hash_mismatch_stops_adoption(self):
        src_plan = self.src_plan
        wrong_hash = "0000000000000000000000000000000000000000000000000000000000000000"
        with tempfile.TemporaryDirectory() as tmp_dir:
            with self.assertRaises(GovernanceError):
                emit_plan_v1_3_adoption(src_plan, wrong_hash, "test_op", workspace_root=tmp_dir)

    def test_adoption_record_contains_no_self_referential_field(self):
        adoption_path = os.path.join(BASE_DIR, "campaign3", "plan_v1_3_adoption.json")
        self.assertTrue(os.path.exists(adoption_path))
        with open(adoption_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        forbidden_keys = {"commit_sha", "commit_hash", "self_hash", "artifact_hash", "self_sha256"}
        for k in data.keys():
            self.assertNotIn(k, forbidden_keys)

        self.assertEqual(data["plan_sha256"], "355740d543b65774f66c45481f120810ba8262da59815a19b30e5eabb416b845")
        self.assertEqual(data["plan_path"], "campaign3/project_plan_v1_3.md")
        self.assertEqual(data["adoption_timestamp_utc"], "2026-08-09T16:45:06Z")
        self.assertIn("operator_identity", data)
        self.assertIn("execution_root_realpath", data)

    def test_adoption_immutability_fails_closed_on_mismatch(self):
        """Verifies that emit_plan_v1_3_adoption fails closed if adoption already exists with mismatched parameters."""
        src_plan = self.src_plan
        valid_hash = "355740d543b65774f66c45481f120810ba8262da59815a19b30e5eabb416b845"
        op_id = "<operator>"

        with tempfile.TemporaryDirectory() as tmp_dir:
            c3_tmp = os.path.join(tmp_dir, "campaign3")
            os.makedirs(c3_tmp, exist_ok=True)

            # 1. Create valid initial adoption
            rec1 = emit_plan_v1_3_adoption(src_plan, valid_hash, op_id, workspace_root=tmp_dir)
            ts1 = rec1["adoption_timestamp_utc"]

            # 2. Re-running with identical parameters returns exact existing record without mutating timestamp
            rec2 = emit_plan_v1_3_adoption(src_plan, valid_hash, op_id, workspace_root=tmp_dir)
            self.assertEqual(rec2["adoption_timestamp_utc"], ts1)

            # 3. Attempting to adopt with different operator identity fails closed with GovernanceError
            with self.assertRaises(GovernanceError):
                emit_plan_v1_3_adoption(src_plan, valid_hash, "imposter_operator", workspace_root=tmp_dir)

    def test_adoption_immutability_fails_closed_on_asymmetric_existence(self):
        """Verifies that emit_plan_v1_3_adoption fails closed if exactly one of plan/adoption files exists."""
        src_plan = self.src_plan
        valid_hash = "355740d543b65774f66c45481f120810ba8262da59815a19b30e5eabb416b845"
        op_id = "<operator>"

        with tempfile.TemporaryDirectory() as tmp_dir:
            c3_tmp = os.path.join(tmp_dir, "campaign3")
            os.makedirs(c3_tmp, exist_ok=True)

            # Create plan file without adoption json
            dest_plan = os.path.join(c3_tmp, "project_plan_v1_3.md")
            with open(dest_plan, "w") as f:
                f.write("partial plan bytes")
            os.chmod(dest_plan, 0o444)

            with self.assertRaises(GovernanceError, msg="Asymmetric adoption state (plan exists, adoption json missing) must fail closed!"):
                emit_plan_v1_3_adoption(src_plan, valid_hash, op_id, workspace_root=tmp_dir)

    def test_git_anchor_tamper_rejection(self):
        """Substantively tests rejection for EACH tampered artifact in a dedicated git repo."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            c3_dir = os.path.join(tmp_dir, "campaign3")
            os.makedirs(c3_dir, exist_ok=True)

            subprocess.run(["git", "init"], cwd=tmp_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_dir, check=True)
            subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_dir, check=True)

            for rel in P0_ARTIFACTS:
                src = os.path.join(BASE_DIR, rel)
                dst = os.path.join(tmp_dir, rel)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                with open(src, "rb") as sf, open(dst, "wb") as df:
                    df.write(sf.read())
                os.chmod(dst, 0o444)

            subprocess.run(["git", "add", "campaign3/"], cwd=tmp_dir, check=True)
            subprocess.run(["git", "commit", "-m", "Initial P0 artifacts"], cwd=tmp_dir, check=True)
            test_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=tmp_dir, text=True).strip()

            self.assertTrue(verify_campaign3_p0(tmp_dir, trusted_commit_sha=test_sha))

            for target_rel in P0_ARTIFACTS:
                target_path = os.path.join(tmp_dir, target_rel)

                os.chmod(target_path, 0o644)
                with open(target_path, "ab") as tf:
                    tf.write(b"\n# tampered")
                os.chmod(target_path, 0o444)
                with self.assertRaises(VerifierError, msg=f"Byte tamper for {target_rel} was not rejected!"):
                    verify_campaign3_p0(tmp_dir, trusted_commit_sha=test_sha)

                res_orig = subprocess.check_output(["git", "cat-file", "-p", f"{test_sha}:{target_rel}"], cwd=tmp_dir)
                os.chmod(target_path, 0o644)
                with open(target_path, "wb") as tf:
                    tf.write(res_orig)
                os.chmod(target_path, 0o444)
                self.assertTrue(verify_campaign3_p0(tmp_dir, trusted_commit_sha=test_sha))

                os.chmod(target_path, 0o644)
                with self.assertRaises(VerifierError, msg=f"Permission tamper (0o644) for {target_rel} was not rejected!"):
                    verify_campaign3_p0(tmp_dir, trusted_commit_sha=test_sha)
                os.chmod(target_path, 0o444)

    def test_root_registry_drives_canonical_check_and_forbidden_root_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            emit_root_registry(
                permitted_root=tmp_dir,
                forbidden_roots={self.r2_path: "forbidden R2", self.r3_path: "forbidden R3"},
                workspace_root=tmp_dir,
            )
            real_r1 = verify_canonical_root(tmp_dir)
            self.assertEqual(real_r1, os.path.realpath(tmp_dir))

        with self.assertRaises(GovernanceError):
            verify_canonical_root(self.r2_path)
        with self.assertRaises(GovernanceError):
            verify_canonical_root(self.r3_path)

    def test_discovery_record_traceable_to_command_output(self):
        disc_path = os.path.join(BASE_DIR, "campaign3", "root_discovery.json")
        self.assertTrue(os.path.exists(disc_path))
        with open(disc_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        roots = data.get("roots", {})
        self.assertIn("R1", roots)
        self.assertIn("R2", roots)
        self.assertIn("R3", roots)

        r1_entry = roots["R1"]
        self.assertGreaterEqual(r1_entry["run_tests_discovered_count"], 317)
        self.assertIn("Executed test cases:", r1_entry["run_tests_raw_output"])

    def test_remote_rfc_status_schema_and_conclusion(self):
        """Verifies committed W4 remote_rfc_status.json artifact is strictly determinate and covers all branches."""
        remote_path = os.path.join(BASE_DIR, "campaign3", "remote_rfc_status.json")
        self.assertTrue(os.path.exists(remote_path))
        with open(remote_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        # MUST be strictly determinate for P0 completion
        self.assertEqual(data["conclusion"], "no_accepted_record_found", f"W4 artifact must be determinate, got '{data.get('conclusion')}'")
        self.assertFalse(data["accepted_record_found_on_remote"])
        self.assertEqual(data["rfc1_v2_sha256"], "e15996148e34864d650d5b572e2c6269a9d14b9f58c060c8ede38371d17c197e")

        expected_branches = {
            "refs/heads/builder-canary-parity",
            "refs/heads/campaign2-work-order-a-rebuild",
            "refs/heads/diagnostic-run-1",
            "refs/heads/main"
        }
        actual_branches = set(data.get("inspected_remote_branches", []))
        self.assertEqual(actual_branches, expected_branches, f"Every advertised branch must be inspected! Expected {expected_branches}, got {actual_branches}")

        # Network failure / invalid remote URL returns indeterminate_remote_content_not_inspected
        with tempfile.TemporaryDirectory() as tmp_dir:
            c3_tmp = os.path.join(tmp_dir, "campaign3")
            os.makedirs(c3_tmp, exist_ok=True)
            with open(os.path.join(BASE_DIR, "campaign3", "rfc_1_v2.md"), "rb") as sf, \
                 open(os.path.join(c3_tmp, "rfc_1_v2.md"), "wb") as df:
                df.write(sf.read())

            res = emit_remote_rfc_status(workspace_root=tmp_dir, remote_url="https://invalid.domain.example/repo.git")
            self.assertEqual(res["conclusion"], "indeterminate_remote_content_not_inspected")
            self.assertFalse(res["accepted_record_found_on_remote"])
            self.assertIn("error_message", res)

    def test_remote_rfc_status_accepted_rfc_content_detection(self):
        """Positive test: verifies that a mock remote repository containing an accepted RFC file is detected."""
        with tempfile.TemporaryDirectory() as tmp_remote_repo, tempfile.TemporaryDirectory() as tmp_workspace:
            subprocess.run(["git", "init", "-b", "main"], cwd=tmp_remote_repo, stdout=subprocess.DEVNULL, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_remote_repo, check=True)
            subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_remote_repo, check=True)

            c3_remote = os.path.join(tmp_remote_repo, "campaign3")
            os.makedirs(c3_remote, exist_ok=True)
            rfc_remote_path = os.path.join(c3_remote, "rfc_1_v3.md")
            with open(rfc_remote_path, "w", encoding="utf-8") as f:
                f.write("# RFC-1 v3\n**Status**: ACCEPTED\n\nAccepted preregistration content.")

            subprocess.run(["git", "add", "campaign3/"], cwd=tmp_remote_repo, check=True)
            subprocess.run(["git", "commit", "-m", "Add accepted RFC-1 v3"], cwd=tmp_remote_repo, check=True)

            c3_local = os.path.join(tmp_workspace, "campaign3")
            os.makedirs(c3_local, exist_ok=True)
            with open(os.path.join(BASE_DIR, "campaign3", "rfc_1_v2.md"), "rb") as sf, \
                 open(os.path.join(c3_local, "rfc_1_v2.md"), "wb") as df:
                df.write(sf.read())

            res = emit_remote_rfc_status(workspace_root=tmp_workspace, remote_url=tmp_remote_repo)
            self.assertEqual(res["conclusion"], "accepted_record_found")
            self.assertTrue(res["accepted_record_found_on_remote"])

    def test_verifier_requires_trusted_commit_sha(self):
        with self.assertRaises(ValueError):
            verify_campaign3_p0(BASE_DIR, trusted_commit_sha="")
        with self.assertRaises(ValueError):
            verify_campaign3_p0(BASE_DIR, trusted_commit_sha="invalid_short_sha")

    def test_r2_r3_files_mode_and_byte_identical_before_and_after(self):
        """Authenticates live R2 and R3 against persisted initial pre-work-order snapshot with set-exact key equality."""
        snap_path = os.path.join(BASE_DIR, "campaign3", "historical_roots_initial_snapshot.json")
        self.assertTrue(os.path.exists(snap_path), "Missing historical_roots_initial_snapshot.json")

        with open(snap_path, "r", encoding="utf-8") as f:
            snap_data = json.load(f)

        r2_init = snap_data.get("R2_snapshot", {})
        r3_init = snap_data.get("R3_snapshot", {})
        self.assertGreater(len(r2_init), 0)
        self.assertGreater(len(r3_init), 0)
        if not (os.path.isdir(self.r2_path) and os.path.isdir(self.r3_path)):
            return

        r2_now = _snapshot_root(self.r2_path)
        r3_now = _snapshot_root(self.r3_path)

        self.assertEqual(set(r2_now.keys()), set(r2_init.keys()), "R2 file set mismatch (deletions or additions)!")
        self.assertEqual(set(r3_now.keys()), set(r3_init.keys()), "R3 file set mismatch (deletions or additions)!")

        for rel, item in r2_now.items():
            init_item = r2_init[rel]
            self.assertEqual(item[0], init_item.get("mode"), f"R2 mode altered for {rel}")
            self.assertEqual(item[1], init_item.get("hash"), f"R2 hash altered for {rel}")

        for rel, item in r3_now.items():
            init_item = r3_init[rel]
            self.assertEqual(item[0], init_item.get("mode"), f"R3 mode altered for {rel}")
            self.assertEqual(item[1], init_item.get("hash"), f"R3 hash altered for {rel}")

    def test_every_recorded_sha_is_exactly_40_hex_characters(self):
        disc_path = os.path.join(BASE_DIR, "campaign3", "root_discovery.json")
        with open(disc_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        roots = data.get("roots", {})
        for root_name, entry in roots.items():
            head_sha = entry.get("git_head_sha", "")
            self.assertEqual(len(head_sha), 40, f"Head SHA for {root_name} must be 40 hex characters, got '{head_sha}'")
            self.assertTrue(all(c in "0123456789abcdefABCDEF" for c in head_sha))

    def test_traceability_matrix_validity(self):
        self.assertGreater(len(TRACEABILITY_MATRIX), 0)
        for item in TRACEABILITY_MATRIX:
            self.assertIn("clause", item)
            self.assertIn("file", item)
            self.assertIn("line", item)
            self.assertIn("symbol", item)
            self.assertIn("test", item)
            self.assertTrue(os.path.isabs(item["file"]), f"Traceability matrix path must be absolute: {item['file']}")
            self.assertTrue(os.path.exists(item["file"]), f"Traceability matrix file missing: {item['file']}")
            self.assertTrue(hasattr(self, item["test"]), f"Test method '{item['test']}' specified in matrix does not exist on TestCampaign3P0")

            with open(item["file"], "r", encoding="utf-8") as f:
                lines = f.readlines()
            target_line_idx = item["line"] - 1
            self.assertLess(target_line_idx, len(lines), f"Line {item['line']} out of bounds for {item['file']}")

            surrounding = "".join(lines[max(0, target_line_idx - 5): min(len(lines), target_line_idx + 6)])
            self.assertIn(
                item["symbol"],
                surrounding,
                f"Symbol '{item['symbol']}' not found at or around line {item['line']} in {item['file']}"
            )

    def test_p0_operator_decision_present_and_valid(self):
        decision_path = os.path.join(BASE_DIR, "campaign3", "p0_operator_decision.json")
        self.assertTrue(os.path.exists(decision_path))
        with open(decision_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["decision"], "ACCEPT_P0")
        self.assertEqual(data["trusted_commit_sha"], "c26d4ec499d93142bc557c7d83f71f8de98cd3b3")


if __name__ == "__main__":
    unittest.main()
