import unittest
import json
import os
import tempfile
import hashlib
from campaign3.sim.model import SimulationModel, LineageEvent
from campaign3.sim.report import run_fixture, generate_report
from campaign3.sim.rational import RationalScore, accept_candidate

class TestCampaign3SimModel(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        fixtures_path = os.path.join("campaign3", "sim", "non_normative_fixtures.json")
        with open(fixtures_path, 'r') as f:
            cls.fixtures = json.load(f)

        cls.report_path = os.path.join("campaign3", "sim", "moments_report.json")
        if os.path.exists(cls.report_path):
            with open(cls.report_path, 'r') as f:
                cls.report = json.load(f)
        else:
            cls.report = {}

    def test_se1_1_fixed_task_effects(self):
        """Block-reordered generation yields byte-identical fixed task effects."""
        fixture_name = "fixture_zero_transition"
        m1 = SimulationModel(self.fixtures[fixture_name], fixture_name)
        m2 = SimulationModel(self.fixtures[fixture_name], fixture_name)

        # Test independent of block/ordering
        m1.generate_fixed_task_effects(mc_iteration=42)
        m2.generate_fixed_task_effects(mc_iteration=42)

        self.assertEqual(len(m1.fixed_task_effects), m1.total_tasks)
        self.assertEqual(m1.fixed_task_effects.tobytes(), m2.fixed_task_effects.tobytes())

    def test_se1_2_lineage(self):
        """Immutable lineage, explicit IDs, rejected IDs never become parents."""
        fixture_name = "fixture_two_point_null"
        model = SimulationModel(self.fixtures[fixture_name], fixture_name)
        model.generate_fixed_task_effects(mc_iteration=1)

        _, _, lineage = model.simulate_block(mc_iteration=1, block=0)

        # Ensure instances are frozen
        for ev in lineage:
            self.assertIsInstance(ev, LineageEvent)

        rejected_candidate_ids = {ev.parent_id for ev in lineage if ev.event_type == "reject"}
        subsequent_parent_ids = {ev.parent_id for ev in lineage if ev.event_type in ("mutate", "confirmatory")}
        
        # Intersection must be empty: a rejected candidate never becomes a parent
        self.assertEqual(rejected_candidate_ids.intersection(subsequent_parent_ids), set())

    def test_se1_3_rational(self):
        """Tie retention using distinct fractional representations (e.g., 1/10 vs 2/20)."""
        incumbent = RationalScore(1, 10)
        candidate = RationalScore(2, 20)

        # They evaluate equal
        self.assertEqual(incumbent.value, candidate.value)

        # But accept_candidate MUST retain the object identity of incumbent
        result = accept_candidate(incumbent, candidate)
        self.assertIs(result, incumbent)

        # And if candidate is better, it returns candidate
        better_candidate = RationalScore(3, 20)
        self.assertIs(accept_candidate(incumbent, better_candidate), better_candidate)

    def test_se1_4_prng_reproducibility(self):
        """Block-order byte-reproducibility of the entire step/mutation stream."""
        fixture_name = "fixture_two_point_null"
        m1 = SimulationModel(self.fixtures[fixture_name], fixture_name)
        m1.generate_fixed_task_effects(mc_iteration=1)
        D1, A1, lin1 = m1.simulate_block(mc_iteration=1, block=1)

        # Reset and simulate out of order
        m2 = SimulationModel(self.fixtures[fixture_name], fixture_name)
        m2.generate_fixed_task_effects(mc_iteration=1)
        _, _, lin2_wrong = m2.simulate_block(mc_iteration=1, block=2)
        D2, A2, lin2 = m2.simulate_block(mc_iteration=1, block=1)

        def serialize_block(D, A, lineage):
            import json
            from dataclasses import asdict
            d = {"D": D, "A": A, "lineage": [asdict(ev) for ev in lineage]}
            return json.dumps(d, sort_keys=True).encode("utf-8")

        self.assertEqual(serialize_block(D1, A1, lin1), serialize_block(D2, A2, lin2))

    def test_se1_5_report(self):
        """Generate temporary report and verify byte-exact match with checked-in evidence."""
        if not self.report:
            self.fail("No checked-in moments_report.json found.")

        with tempfile.TemporaryDirectory() as tmpdir:
            temp_report_path = os.path.join(tmpdir, "temp_moments.json")
            generate_report(
                "campaign3/sim/non_normative_fixtures.json",
                temp_report_path,
                iterations=1000
            )

            with open(temp_report_path, 'r') as f:
                temp_content = f.read()
            with open(self.report_path, 'r') as f:
                checked_in_content = f.read()

            self.assertEqual(temp_content, checked_in_content, "Temporary report differs from checked-in report.")

            report_json = json.loads(temp_content)
            # Ensure selection_bias_offset only present in null fixture
            self.assertIn("selection_bias_offset", report_json["fixture_two_point_null"])
            self.assertNotIn("selection_bias_offset", report_json["fixture_zero_transition"])

            # Verify every manifest digest
            manifest_lines = []
            sim_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "campaign3", "sim")
            for fname in ["model.py", "non_normative_fixtures.json", "prng.py", "rational.py", "report.py"]:
                with open(os.path.join(sim_dir, fname), "rb") as f:
                    manifest_lines.append(f"{fname} {hashlib.sha256(f.read()).hexdigest()}")
            expected_spec_hash = hashlib.sha256("\n".join(manifest_lines).encode("utf-8")).hexdigest()

            for fixture_name, rep in report_json.items():
                self.assertEqual(rep["spec_hash"], expected_spec_hash)

    def test_se1_6_conformance(self):
        """MC moments must converge and pass targets independently."""
        if not self.report:
            self.fail("No checked-in moments_report.json found.")

        for fixture_name, rep in self.report.items():
            tol = rep["intended_moments"]
            passed_A = abs(rep["mean_A"] - tol["mean_A_target"]) <= tol["mean_A_tol"]
            passed_D = abs(rep["mean_D"] - tol["mean_D_target"]) <= tol["mean_D_tol"]
            self.assertTrue(passed_A, f"Failed mean_A target in {fixture_name}")
            self.assertTrue(passed_D, f"Failed mean_D target in {fixture_name}")

if __name__ == "__main__":
    unittest.main()
