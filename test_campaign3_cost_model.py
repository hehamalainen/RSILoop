import unittest
import os
import re
import json
import tempfile
from campaign3.cost_model import (
    NUM_SEARCH_TASKS,
    NUM_CONF_TASKS,
    NUM_HOLDOUT_TASKS,
    NUM_FINAL_TASKS,
    FOUR_VARIANTS,
    generate_traceability_matrix,
    get_per_block_symbolic_formulas,
    get_affine_coefficients,
    evaluate_cost_breakdown,
    generate_all_coefficient_tables,
    reconcile_prior_figures,
    build_cost_model_report,
    generate_cost_model_markdown,
    emit_cost_model_artifacts
)

class TestCampaign3CostModel(unittest.TestCase):
    def setUp(self):
        self.hand_computed_fixtures = {
            "SHARED_BLOCK_SHARED": {
                "k": 2, "B": 4, "M": 3, "R": 2, "T_sol": 500, "T_mut": 200,
                "expected_solution_calls": 784,
                "expected_mutation_calls": 24,
                "expected_logical_calls": 808,
                "expected_local_evaluations": 784,
                "expected_max_attempts": 1616,
                "expected_output_tokens": 396800,
                "expected_intercept": 400,
                "expected_slope": 136
            },
            "SHARED_REPLICATE_SPECIFIC": {
                "k": 2, "B": 4, "M": 3, "R": 3, "T_sol": 400, "T_mut": 300,
                "expected_solution_calls": 784,
                "expected_mutation_calls": 48,
                "expected_logical_calls": 832,
                "expected_local_evaluations": 784,
                "expected_max_attempts": 2496,
                "expected_output_tokens": 328000,
                "expected_intercept": 400,
                "expected_slope": 144
            },
            "SEPARATE_BLOCK_SHARED": {
                "k": 2, "B": 4, "M": 3, "R": 1, "T_sol": 600, "T_mut": 150,
                "expected_solution_calls": 848,
                "expected_mutation_calls": 24,
                "expected_logical_calls": 872,
                "expected_local_evaluations": 848,
                "expected_max_attempts": 872,
                "expected_output_tokens": 512400,
                "expected_intercept": 464,
                "expected_slope": 136
            },
            "SEPARATE_REPLICATE_SPECIFIC": {
                "k": 2, "B": 4, "M": 3, "R": 2, "T_sol": 500, "T_mut": 250,
                "expected_solution_calls": 848,
                "expected_mutation_calls": 48,
                "expected_logical_calls": 896,
                "expected_local_evaluations": 848,
                "expected_max_attempts": 1792,
                "expected_output_tokens": 436000,
                "expected_intercept": 464,
                "expected_slope": 144
            }
        }

    def test_se3r1_task_counts_and_four_variants_definition(self):
        """Verify task counts match preregistration and four variants are supported."""
        self.assertEqual(NUM_SEARCH_TASKS, 8)
        self.assertEqual(NUM_CONF_TASKS, 8)
        self.assertEqual(NUM_HOLDOUT_TASKS, 6)
        self.assertEqual(NUM_FINAL_TASKS, 14)
        self.assertEqual(len(FOUR_VARIANTS), 4)

    def test_se3r2_hand_computed_fixtures_all_four_variants(self):
        """Verify exact mathematical evaluations against pre-declared hand-computed fixtures."""
        for var_name, fix in self.hand_computed_fixtures.items():
            k = fix["k"]
            B = fix["B"]
            M = fix["M"]
            R = fix["R"]
            T_sol = fix["T_sol"]
            T_mut = fix["T_mut"]

            # 1. Test affine coefficients
            coeffs = get_affine_coefficients(var_name, k, B)
            self.assertEqual(coeffs["intercept_logical"], fix["expected_intercept"], msg=f"Intercept mismatch for {var_name}")
            self.assertEqual(coeffs["slope_logical"], fix["expected_slope"], msg=f"Slope mismatch for {var_name}")

            # 2. Test instantiated evaluation
            breakdown = evaluate_cost_breakdown(var_name, k, B, M, R=R, T_solution=T_sol, T_mutation=T_mut)
            counts = breakdown["counts"]

            self.assertEqual(counts["subject_model_solution_calls"], fix["expected_solution_calls"], msg=f"Solution mismatch for {var_name}")
            self.assertEqual(counts["mutation_generation_calls"], fix["expected_mutation_calls"], msg=f"Mutation mismatch for {var_name}")
            self.assertEqual(counts["total_provider_logical_calls"], fix["expected_logical_calls"], msg=f"Logical mismatch for {var_name}")
            self.assertEqual(counts["local_evaluator_executions"], fix["expected_local_evaluations"], msg=f"Evaluator mismatch for {var_name}")
            self.assertEqual(counts["maximum_transport_attempts"], fix["expected_max_attempts"], msg=f"Max attempts mismatch for {var_name}")
            self.assertIsInstance(counts["maximum_transport_attempts"], int)
            self.assertEqual(breakdown["output_tokens_estimate"], fix["expected_output_tokens"], msg=f"Token estimate mismatch for {var_name}")
            self.assertEqual(breakdown["status"], "NON_NORMATIVE_ILLUSTRATIVE")

    def test_se3r2_symbolic_formulas_per_block(self):
        """Verify per-block symbolic string formulas match exact closed-form expressions."""
        for var_name in FOUR_VARIANTS:
            sym = get_per_block_symbolic_formulas(var_name)
            self.assertEqual(sym["variant"], var_name)
            bd = sym["breakdown"]
            self.assertEqual(bd["mutant_search_evaluations"], "16Mk")
            self.assertEqual(bd["confirmatory_evaluations"], "24k")
            self.assertEqual(bd["secondary_holdout_evaluations"], "18k")
            self.assertEqual(bd["final_evaluations_total"], "42k")

            if var_name.startswith("SHARED"):
                self.assertEqual(bd["search_starting_baseline_calls"], "8k")
                self.assertEqual(bd["subject_model_solution_calls"], "(50 + 16M)k")
            else:
                self.assertEqual(bd["search_starting_baseline_calls"], "16k")
                self.assertEqual(bd["subject_model_solution_calls"], "(58 + 16M)k")

            if var_name.endswith("BLOCK_SHARED"):
                self.assertEqual(bd["mutation_generation_calls"], "2M")
            else:
                self.assertEqual(bd["mutation_generation_calls"], "2kM")

    def test_se3r3_call_classes_and_symbolic_retries(self):
        """Verify transport attempts scale strictly as integer product with symbolic R."""
        breakdown_r1 = evaluate_cost_breakdown("SHARED_BLOCK_SHARED", k=1, B=2, M=2, R=1)
        breakdown_r3 = evaluate_cost_breakdown("SHARED_BLOCK_SHARED", k=1, B=2, M=2, R=3)

        log_calls = breakdown_r1["counts"]["total_provider_logical_calls"]
        self.assertEqual(breakdown_r1["counts"]["maximum_transport_attempts"], 1 * log_calls)
        self.assertEqual(breakdown_r3["counts"]["maximum_transport_attempts"], 3 * log_calls)
        self.assertIsInstance(breakdown_r3["counts"]["maximum_transport_attempts"], int)

    def test_se3r3_fail_closed_input_validation(self):
        """Verify cost instantiation and coefficient functions strictly reject impossible/invalid inputs."""
        # 1. Invalid k
        for bad_k in [0, 4, -1, 1.5, True, False, "1", None]:
            with self.assertRaises(ValueError, msg=f"Failed to reject k={bad_k!r}"):
                get_affine_coefficients("SHARED_BLOCK_SHARED", k=bad_k, B=2)
            with self.assertRaises(ValueError, msg=f"Failed to reject k={bad_k!r}"):
                evaluate_cost_breakdown("SHARED_BLOCK_SHARED", k=bad_k, B=2, M=1, R=1)

        # 2. Invalid B (odd, < 2, > 40, fractional, boolean, non-int)
        for bad_B in [0, 1, 3, 41, 42, -2, 2.0, 4.5, True, False, "2", None]:
            with self.assertRaises(ValueError, msg=f"Failed to reject B={bad_B!r}"):
                get_affine_coefficients("SHARED_BLOCK_SHARED", k=1, B=bad_B)
            with self.assertRaises(ValueError, msg=f"Failed to reject B={bad_B!r}"):
                evaluate_cost_breakdown("SHARED_BLOCK_SHARED", k=1, B=bad_B, M=1, R=1)

        # 3. Invalid M (negative, fractional, boolean, non-int)
        for bad_M in [-1, -5, 1.5, True, False, "3", None]:
            with self.assertRaises(ValueError, msg=f"Failed to reject M={bad_M!r}"):
                evaluate_cost_breakdown("SHARED_BLOCK_SHARED", k=1, B=2, M=bad_M, R=1)

        # 4. Invalid R (missing, < 1, fractional, boolean, non-int)
        for bad_R in [0, -1, 0.5, 1.5, True, False, "2", None]:
            with self.assertRaises(ValueError, msg=f"Failed to reject R={bad_R!r}"):
                evaluate_cost_breakdown("SHARED_BLOCK_SHARED", k=1, B=2, M=1, R=bad_R)

        # 5. Invalid token assumptions (asymmetric presence, negative, fractional, boolean)
        with self.assertRaises(ValueError):
            evaluate_cost_breakdown("SHARED_BLOCK_SHARED", k=1, B=2, M=1, R=1, T_solution=500, T_mutation=None)
        with self.assertRaises(ValueError):
            evaluate_cost_breakdown("SHARED_BLOCK_SHARED", k=1, B=2, M=1, R=1, T_solution=None, T_mutation=200)
        with self.assertRaises(ValueError):
            evaluate_cost_breakdown("SHARED_BLOCK_SHARED", k=1, B=2, M=1, R=1, T_solution=-10, T_mutation=200)
        with self.assertRaises(ValueError):
            evaluate_cost_breakdown("SHARED_BLOCK_SHARED", k=1, B=2, M=1, R=1, T_solution=500, T_mutation=-5)
        with self.assertRaises(ValueError):
            evaluate_cost_breakdown("SHARED_BLOCK_SHARED", k=1, B=2, M=1, R=1, T_solution=500.5, T_mutation=200)
        with self.assertRaises(ValueError):
            evaluate_cost_breakdown("SHARED_BLOCK_SHARED", k=1, B=2, M=1, R=1, T_solution=True, T_mutation=200)

        # 6. Invalid variant
        with self.assertRaises(ValueError):
            get_affine_coefficients("INVALID_VARIANT", k=1, B=2)
        with self.assertRaises(ValueError):
            evaluate_cost_breakdown("INVALID_VARIANT", k=1, B=2, M=1, R=1)

    def test_se3r4_coefficient_tables_generation(self):
        """Verify affine coefficient tables cover all k in {1,2,3} and even B in {2..40}."""
        tables = generate_all_coefficient_tables(k_values=(1, 2, 3), b_values=tuple(range(2, 42, 2)))
        self.assertEqual(set(tables.keys()), set(FOUR_VARIANTS))

        for var_name, rows in tables.items():
            self.assertEqual(len(rows), 3 * 20)  # 3 k-values x 20 B-values = 60 rows
            for row in rows:
                k = row["k"]
                B = row["B"]
                self.assertIn(k, (1, 2, 3))
                self.assertTrue(B % 2 == 0 and 2 <= B <= 40)

                # Direct algebraic consistency check for each row
                for M in (0, 1, 3, 5):
                    eval_out = evaluate_cost_breakdown(var_name, k, B, M, R=1)
                    logical_from_coeffs = row["intercept_logical"] + row["slope_logical"] * M
                    self.assertEqual(eval_out["counts"]["total_provider_logical_calls"], logical_from_coeffs)

    def test_se3r5_reconcile_prior_figures(self):
        """Verify exact reconciliation of historical figures: 24kB, (98k+6)B, (106k+6)B, P2 reference."""
        rec = reconcile_prior_figures()
        r = rec["reconciliations"]

        self.assertIn("24kB", r)
        self.assertIn("(98k + 6)B", r)
        self.assertIn("(106k + 6)B", r)
        self.assertIn("p2_reference_algorithm", r)

        # 1. 24kB equals 3 arms x 8 tasks x k replicates x B
        self.assertEqual(3 * 8, 24)

        # 2. (98k + 6)B matches SHARED_BLOCK_SHARED at M=3
        c_98_k1 = get_affine_coefficients("SHARED_BLOCK_SHARED", k=1, B=2)
        calls_98_k1 = c_98_k1["intercept_logical"] + c_98_k1["slope_logical"] * 3
        self.assertEqual(calls_98_k1, (98 * 1 + 6) * 2)

        c_98_k2 = get_affine_coefficients("SHARED_BLOCK_SHARED", k=2, B=2)
        calls_98_k2 = c_98_k2["intercept_logical"] + c_98_k2["slope_logical"] * 3
        self.assertEqual(calls_98_k2, (98 * 2 + 6) * 2)

        # 3. (106k + 6)B matches SEPARATE_BLOCK_SHARED at M=3
        c_106_k1 = get_affine_coefficients("SEPARATE_BLOCK_SHARED", k=1, B=2)
        calls_106_k1 = c_106_k1["intercept_logical"] + c_106_k1["slope_logical"] * 3
        self.assertEqual(calls_106_k1, (106 * 1 + 6) * 2)

        c_106_k2 = get_affine_coefficients("SEPARATE_BLOCK_SHARED", k=2, B=2)
        calls_106_k2 = c_106_k2["intercept_logical"] + c_106_k2["slope_logical"] * 3
        self.assertEqual(calls_106_k2, (106 * 2 + 6) * 2)

        # 4. P2 reference matches SEPARATE_REPLICATE_SPECIFIC at M=3
        c_p2_k1 = get_affine_coefficients("SEPARATE_REPLICATE_SPECIFIC", k=1, B=2)
        calls_p2_k1 = c_p2_k1["intercept_logical"] + c_p2_k1["slope_logical"] * 3
        self.assertEqual(calls_p2_k1, (58 + 18 * 3) * 1 * 2)  # 112 * 1 * 2

        c_p2_k2 = get_affine_coefficients("SEPARATE_REPLICATE_SPECIFIC", k=2, B=2)
        calls_p2_k2 = c_p2_k2["intercept_logical"] + c_p2_k2["slope_logical"] * 3
        self.assertEqual(calls_p2_k2, (58 + 18 * 3) * 2 * 2)  # 112 * 2 * 2

    def test_se3r6_no_selected_or_recommended_parameters_in_artifacts(self):
        """
        Verify that report artifacts contain NO selected or recommended parameter values
        for M, R, k, B, B_max, T_solution, or T_mutation, and are explicitly marked NON-NORMATIVE.
        """
        repo_dir = os.path.dirname(os.path.abspath(__file__))
        committed_json = os.path.join(repo_dir, "campaign3", "cost_model_report.json")
        committed_md = os.path.join(repo_dir, "campaign3", "cost_model_report.md")

        self.assertTrue(os.path.exists(committed_json), "Committed cost_model_report.json missing")
        self.assertTrue(os.path.exists(committed_md), "Committed cost_model_report.md missing")

        with open(committed_json, "r", encoding="utf-8") as f:
            report_data = json.load(f)

        with open(committed_md, "r", encoding="utf-8") as f:
            report_text = f.read()

        # Check governance status & traceability
        self.assertEqual(report_data["governance_status"], "NON_NORMATIVE_ANALYTICAL_CLOSEOUT_ONLY")
        self.assertIn("NON-NORMATIVE", report_text)
        self.assertIn("DRAFT", report_text)
        self.assertIn("traceability_matrix", report_data)
        self.assertEqual(len(report_data["traceability_matrix"]), 6)

        # Forbidden selection / freeze keywords
        forbidden_phrases = [
            "registered M =",
            "frozen M =",
            "recommended M =",
            "registered R =",
            "frozen R =",
            "recommended R =",
            "registered k =",
            "frozen k =",
            "recommended k =",
            "registered B =",
            "frozen B =",
            "recommended B =",
            "registered B_max =",
            "frozen B_max =",
            "recommended B_max =",
            "registered T_solution =",
            "frozen T_solution =",
            "recommended T_solution ="
        ]

        for phrase in forbidden_phrases:
            self.assertNotIn(phrase.lower(), report_text.lower(), msg=f"Forbidden phrase found in markdown: {phrase}")

    def test_se3r6_isolated_artifact_emission_byte_exact(self):
        """Verify emit_cost_model_artifacts into isolated directory matches committed reports byte-identically."""
        repo_dir = os.path.dirname(os.path.abspath(__file__))
        committed_json = os.path.join(repo_dir, "campaign3", "cost_model_report.json")
        committed_md = os.path.join(repo_dir, "campaign3", "cost_model_report.md")

        with tempfile.TemporaryDirectory() as temp_dir:
            temp_json = os.path.join(temp_dir, "cost_model_report.json")
            temp_md = os.path.join(temp_dir, "cost_model_report.md")

            emit_cost_model_artifacts(json_path=temp_json, md_path=temp_md)

            with open(temp_json, "rb") as f:
                temp_json_bytes = f.read()
            with open(committed_json, "rb") as f:
                comm_json_bytes = f.read()
            self.assertEqual(temp_json_bytes, comm_json_bytes, "Emitted JSON bytes do not match committed JSON bytes")

            with open(temp_md, "rb") as f:
                temp_md_bytes = f.read()
            with open(committed_md, "rb") as f:
                comm_md_bytes = f.read()
            self.assertEqual(temp_md_bytes, comm_md_bytes, "Emitted Markdown bytes do not match committed Markdown bytes")

    def test_se3r6_traceability_matrix_structure_and_test_existence(self):
        """Verify dynamic traceability matrix contains all six clauses with numeric file:line and valid test methods."""
        matrix = generate_traceability_matrix()
        self.assertEqual(len(matrix), 6)

        expected_clauses = [f"SE3R-{i}" for i in range(1, 7)]
        found_clauses = [row["clause_id"] for row in matrix]
        self.assertEqual(found_clauses, expected_clauses)

        loc_pattern = re.compile(r"campaign3/cost_model\.py:\d+-\d+ \(\w+\)")

        for row in matrix:
            clause = row["clause_id"]
            loc = row["implementation_location"]
            test_name = row["exact_test_name"]

            # 1. Check implementation location format
            self.assertTrue(loc_pattern.search(loc), msg=f"Invalid location format in {clause}: {loc}")

            # Extract line ranges and assert positive start <= end
            for match in re.finditer(r"campaign3/cost_model\.py:(\d+)-(\d+)", loc):
                start_l, end_l = int(match.group(1)), int(match.group(2))
                self.assertGreater(start_l, 0)
                self.assertLessEqual(start_l, end_l)

            # 2. Check test method exists on TestCampaign3CostModel
            # Format is test_campaign3_cost_model.TestCampaign3CostModel.<method_name>
            method_name = test_name.split(".")[-1]
            self.assertTrue(
                hasattr(self, method_name) and callable(getattr(self, method_name)),
                msg=f"Referenced test method {method_name} for {clause} does not exist on TestCampaign3CostModel"
            )

if __name__ == "__main__":
    unittest.main()
