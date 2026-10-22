import unittest
import os
import json
import re
import datetime
import subprocess

BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__)))

class TestCampaign3P1(unittest.TestCase):
    def setUp(self):
        self.rfc_path = os.path.join(BASE_DIR, "campaign3", "rfc_1_v3.md")
        self.ledger_path = os.path.join(BASE_DIR, "campaign3", "rfc_1_v3_redline_ledger.json")
        self.open_items_path = os.path.join(BASE_DIR, "campaign3", "p1_open_items.json")
        self.decision_path = os.path.join(BASE_DIR, "campaign3", "p1_c2_review_decision.json")

        with open(self.rfc_path, "r", encoding="utf-8") as f:
            self.rfc_text = f.read()

        with open(self.ledger_path, "r", encoding="utf-8") as f:
            self.ledger_data = json.load(f)

        with open(self.open_items_path, "r", encoding="utf-8") as f:
            self.open_items_data = json.load(f)

        with open(self.decision_path, "r", encoding="utf-8") as f:
            self.decision_data = json.load(f)

    def test_clause_inventory_maps_to_rfc(self):
        # We check if 2.1 through 2.9 exist in the RFC
        for i in range(1, 10):
            clause = f"2.{i}"
            self.assertIn(clause, self.rfc_text, f"Clause {clause} missing from RFC")

    def test_ledger_source_text_verbatim_is_substring(self):
        for entry in self.ledger_data["ledger"]:
            source_text = entry["source_text_verbatim"]
            self.assertIn(source_text, self.rfc_text, f"Ledger entry {entry['id']} source text not found in RFC")

    def test_delta_appears_exactly_as_0_10(self):
        self.assertIn("delta = 0.10", self.rfc_text)

    def test_gates_appear_in_exact_form(self):
        self.assertIn("L_delta > 0.10", self.rfc_text)
        self.assertIn("L_A > 0", self.rfc_text)

    def test_decision_rule_is_intersection_union(self):
        self.assertIn("intersection-union", self.rfc_text)

    def test_quota_matrix_sums(self):
        self.assertIn("12 | 11 | 11 | 34", self.rfc_text)

    def test_ledger_contains_exactly_14_entries_and_correct_ids(self):
        ledger_entries = self.ledger_data["ledger"]
        self.assertEqual(len(ledger_entries), 14)
        for i, entry in enumerate(ledger_entries, 1):
            expected_id = f"REDLINE-{i:02d}"
            self.assertEqual(entry["id"], expected_id)

    def test_no_numeric_values_for_b_bmax_k(self):
        # Ensure B=something, B_max=something, k=something are not in RFC
        self.assertFalse(re.search(r'\bB\s*=\s*\d+', self.rfc_text))
        self.assertFalse(re.search(r'\bB_max\s*=\s*\d+', self.rfc_text))
        self.assertFalse(re.search(r'\bk\s*=\s*\d+', self.rfc_text))

        # Check B=24 or 100 directly
        self.assertNotIn("B=24", self.rfc_text.replace(" ", ""))
        self.assertNotIn("B_max=100", self.rfc_text.replace(" ", ""))

    def test_no_draft_to_accepted_transition_text(self):
        self.assertNotIn("ACCEPTED", self.rfc_text)
        self.assertNotIn("DRAFT-to-ACCEPTED", self.rfc_text)
        self.assertIn("DRAFT", self.rfc_text)

    def test_bidirectional_open_item_completeness(self):
        import re
        import collections
        # 1. Extract all placeholders from RFC
        matches = re.findall(r'\[OPEN ITEM: (.*?)\. See p1_open_items\.json\]', self.rfc_text)

        # Check for duplicates in RFC placeholders
        rfc_counts = collections.Counter(matches)
        rfc_duplicates = {k: v for k, v in rfc_counts.items() if v > 1}
        self.assertFalse(rfc_duplicates, f"Duplicate OPEN ITEM placeholders found in RFC: {rfc_duplicates}")

        rfc_placeholders = set(matches)

        # 2. Extract all entries from open_items register
        register_params = [item["parameter"] for item in self.open_items_data["open_items"]]

        # Check for duplicates in register
        register_counts = collections.Counter(register_params)
        register_duplicates = {k: v for k, v in register_counts.items() if v > 1}
        self.assertFalse(register_duplicates, f"Duplicate parameters found in p1_open_items.json: {register_duplicates}")

        register_params_set = set(register_params)

        # 3. Assert bidirectional completeness
        self.assertEqual(rfc_placeholders, register_params_set, f"Mismatch between RFC placeholders and register entries. RFC has: {rfc_placeholders}, Register has: {register_params_set}")

        # 4. Also ensure no numeric assignment for these params in RFC
        for param in register_params_set:
            if param != "k":
                self.assertFalse(re.search(fr'\b{param}\s*=\s*\d+', self.rfc_text), f"Found numeric assignment for {param}")

        # Exception for k: k_admission = 2 is allowed
        self.assertIn("k_admission = 2", self.rfc_text)

    def test_timestamps_valid_utc_and_not_in_future(self):
        now = datetime.datetime.now(datetime.timezone.utc)
        for path, data in [("decision", self.decision_data)]:
            for k, v in data.items():
                if k.endswith("timestamp_utc"):
                    ts = datetime.datetime.strptime(v, "%Y-%m-%dT%H:%M:%SZ")
                    ts = ts.replace(tzinfo=datetime.timezone.utc)
                    self.assertTrue(ts <= now, f"Timestamp {v} is in the future")

    def test_git_diff_name_only_is_subset_of_allowlist(self):
        allowlist = {
            "campaign3/p1_c2_review_decision.json",
            "campaign3/rfc_1_v3.md",
            "campaign3/rfc_1_v3_redline_ledger.json",
            "campaign3/p1_open_items.json",
            "campaign3/p1_verifier.py",
            "test_campaign3_p1.py",
            "campaign3/p0_operator_decision.json",
            "campaign3/verifier.py",
            "test_campaign3_p0.py"
        }
        # Branch point is c26d4ec499d93142bc557c7d83f71f8de98cd3b3; P1 closeout is 8cdd4021aa9287a52b6f0eef8b21c375921a6d89
        diff_output = subprocess.check_output(
            [
                "git",
                "diff",
                "--name-only",
                "c26d4ec499d93142bc557c7d83f71f8de98cd3b3..8cdd4021aa9287a52b6f0eef8b21c375921a6d89",
            ],
            cwd=BASE_DIR,
            text=True
        ).strip()

        if diff_output:
            changed_files = set(diff_output.split("\n"))

            # Strict subset check against allowlist
            invalid_files = changed_files - allowlist
            self.assertEqual(len(invalid_files), 0, f"Modified files outside allowlist: {invalid_files}")

if __name__ == '__main__':
    unittest.main()
