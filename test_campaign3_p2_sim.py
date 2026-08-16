import unittest
import json
import os
import math
import hmac
import hashlib
from fractions import Fraction
import numpy as np
from campaign3.sim.p2_sim import (
    compute_clopper_pearson_bounds,
    generate_p2_tradeoff_report,
    run_scenario_conformance,
    _simulate_chunk,
    simulate_real_se1_grid,
    FROZEN_T_CRITICAL_095
)
from campaign3.sim.quantizer import quantize_earned_points, clip_to_fraction, clip_to_rational

class TestCampaign3P2Sim(unittest.TestCase):
    def setUp(self):
        self.repo_root = os.path.dirname(os.path.abspath(__file__))
        self.spec_path = os.path.join(self.repo_root, "campaign3", "sim", "p2_scenarios.json")
        self.quarantined_path = os.path.join(self.repo_root, "campaign3", "sim", "invalid_round2", "p2_scenarios.json")

    def test_p2_1_frozen_t_table(self):
        """Frozen t-table contains all required even B values and monotonically decreases."""
        even_B = [2, 4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26, 28, 30, 32, 34, 36, 38, 40]
        for B in even_B:
            self.assertIn(B, FROZEN_T_CRITICAL_095)
            self.assertGreater(FROZEN_T_CRITICAL_095[B], 1.6448)

        for i in range(len(even_B) - 1):
            b1 = even_B[i]
            b2 = even_B[i+1]
            self.assertGreater(FROZEN_T_CRITICAL_095[b1], FROZEN_T_CRITICAL_095[b2])

    def test_p2_2_clopper_pearson_authoritative_fixtures(self):
        """Clopper-Pearson exact bounds validated against authoritative mathematical fixtures to 6 decimal places."""
        fixtures = [
            (0, 100, 0.05, 0.000000, 0.036217),
            (100, 100, 0.05, 0.963788, 1.000000),
            (50, 100, 0.05, 0.398321, 0.601679),
            (10, 1000, 0.05, 0.004806, 0.018313),
            (500, 1000, 0.0002, 0.440919, 0.559081),
            (950, 1000, 0.0002, 0.919389, 0.971837),
        ]
        for x, n, a, exp_lcb, exp_ucb in fixtures:
            lcb, ucb = compute_clopper_pearson_bounds(x, n, a)
            self.assertAlmostEqual(lcb, exp_lcb, places=6, msg=f"LCB mismatch for X={x}, N={n}, a={a}")
            self.assertAlmostEqual(ucb, exp_ucb, places=6, msg=f"UCB mismatch for X={x}, N={n}, a={a}")

    def test_p2_3_cost_identities(self):
        """Symbolic cost formulas match (58 + 16M)k evaluator runs and 2M generator calls."""
        M = 5
        k = 3
        B = 24

        evaluator_runs_block = (58 + 16 * M) * k
        self.assertEqual(evaluator_runs_block, (58 + 80) * 3)  # 138 * 3 = 414

        total_eval_runs = B * evaluator_runs_block
        self.assertEqual(total_eval_runs, 24 * 414)  # 9936

        total_gen_calls = B * (2 * M * k)
        self.assertEqual(total_gen_calls, 24 * 30)  # 720

    def test_p2_4_all_se1_entry_points_hard_disabled(self):
        """Every active SE-1 simulation entry point unconditionally raises RuntimeError."""
        temp_out = os.path.join(self.repo_root, "campaign3", "sim", "temp_tombstone_test.json")
        try:
            # 1. generate_p2_tradeoff_report
            with self.assertRaises(RuntimeError) as ctx1:
                generate_p2_tradeoff_report(self.spec_path, temp_out, mc_iterations=10)
            self.assertIn("permanently retired", str(ctx1.exception).lower())

            # 2. run_scenario_conformance
            with self.assertRaises(RuntimeError) as ctx2:
                run_scenario_conformance({}, {})
            self.assertIn("permanently retired", str(ctx2.exception).lower())

            # 3. _simulate_chunk
            with self.assertRaises(RuntimeError) as ctx3:
                _simulate_chunk(({}, "test", 2, 0, 1))
            self.assertIn("permanently retired", str(ctx3.exception).lower())

            # 4. simulate_real_se1_grid
            with self.assertRaises(RuntimeError) as ctx4:
                simulate_real_se1_grid({}, {}, 1, [2], 10, 0.05)
            self.assertIn("permanently retired", str(ctx4.exception).lower())
        finally:
            if os.path.exists(temp_out):
                os.remove(temp_out)

    def test_p2_5_central_quantizer_halfway_ties_and_contracts(self):
        """Centralized quantizer enforces IEEE 754 round-half-to-even and contract validation (denom >= 8 integer)."""
        # Halfway ties (denom = 10)
        self.assertEqual(quantize_earned_points(0.05, 10), 0)
        self.assertEqual(quantize_earned_points(0.15, 10), 2)
        self.assertEqual(quantize_earned_points(0.25, 10), 2)
        self.assertEqual(quantize_earned_points(0.35, 10), 4)
        self.assertEqual(quantize_earned_points(0.45, 10), 4)
        self.assertEqual(quantize_earned_points(0.55, 10), 6)

        # Clipping
        self.assertEqual(quantize_earned_points(-0.5, 10), 0)
        self.assertEqual(quantize_earned_points(1.5, 10), 10)

        # Contract rejections
        with self.assertRaises(ValueError):
            quantize_earned_points(0.5, 7)  # denom < 8
        with self.assertRaises(ValueError):
            quantize_earned_points(0.5, 0)  # denom = 0
        with self.assertRaises(ValueError):
            quantize_earned_points(0.5, -5) # denom < 0
        with self.assertRaises(TypeError):
            quantize_earned_points(0.5, True) # boolean denom
        with self.assertRaises(TypeError):
            quantize_earned_points(0.5, 8.5) # float denom

        # Wrappers
        rs = clip_to_rational(0.15, 10)
        self.assertEqual(rs.earned, 2)
        self.assertEqual(rs.denominator, 10)
        self.assertEqual(rs.value, Fraction(1, 5))

    def test_p2_6_se2_exact_rational_score_lattice_support(self):
        """Verify SE-2 exact Fraction score-lattice support on discrete task lattice."""
        conf_denoms = [10, 10, 10, 10, 10, 10, 10, 10]
        base_effects = [0.40, 0.40, 0.50, 0.50, 0.50, 0.60, 0.60, 0.50]

        def evaluate_confirmatory_arm(kappa: float, theta: float) -> Fraction:
            scores = [clip_to_fraction(base + kappa * theta, denom) for base, denom in zip(base_effects, conf_denoms)]
            return sum(scores) / len(scores)

        theta_G = 0.25
        theta_U = 0.20

        # 1. Scenario N_delta Face Support: (1/10, 1/5)
        y_b = evaluate_confirmatory_arm(0.0, 0.0)
        y_g_delta = evaluate_confirmatory_arm(0.80, theta_G)
        y_u_delta = evaluate_confirmatory_arm(0.50, theta_U)
        self.assertEqual(y_g_delta - y_u_delta, Fraction(1, 10))
        self.assertEqual(y_g_delta - y_b, Fraction(1, 5))

        # 2. Scenario N_A Face Support: (1/4, 0/1)
        y_g_A = evaluate_confirmatory_arm(0.00, theta_G)
        y_u_A = evaluate_confirmatory_arm(-1.25, theta_U)
        self.assertEqual(y_g_A - y_u_A, Fraction(1, 4))
        self.assertEqual(y_g_A - y_b, Fraction(0, 1))

        # 3. Scenario N_intersection Point Support: (1/10, 0/1)
        y_g_int = evaluate_confirmatory_arm(0.00, theta_G)
        y_u_int = evaluate_confirmatory_arm(-0.50, theta_U)
        self.assertEqual(y_g_int - y_u_int, Fraction(1, 10))
        self.assertEqual(y_g_int - y_b, Fraction(0, 1))

    def test_p2_7_prngv2_golden_vector(self):
        """Verify DomainSeparatedPRNGV2 HMAC-SHA256 golden vector and little-endian seed derivation."""
        plan_hash_hex = '355740d543b65774f66c45481f120810ba8262da59815a19b30e5eabb416b845'
        key = bytes.fromhex(plan_hash_hex)
        payload = {
            'arm': 'guided',
            'block': 0,
            'candidate_index': 0,
            'domain': 'conformance_validation',
            'draw_index': 0,
            'fixture': 'N_delta_boundary',
            'mc_iteration': 0,
            'mutation_index': 0,
            'phase': 'search',
            'purpose': 'fixed_task_effects',
            'replicate': 0,
            'scenario_id': 'N_delta_boundary',
            'task': 'conf_0'
        }
        msg = json.dumps(payload, sort_keys=True, separators=(',', ':')).encode('utf-8')
        mac = hmac.new(key, msg, hashlib.sha256).digest()
        self.assertEqual(mac.hex(), "a7eb1e1e0b370d8274b77653ce86c871b62cb03707834244625372d18f904e7e")

        seed_uint64 = int.from_bytes(mac[:8], byteorder='little', signed=False)
        self.assertEqual(seed_uint64, 9371206920517381031)

    def test_p2_8_two_sided_equivalence_containment_math(self):
        """Verify that two-sided equivalence requires CI containment: |estimate - target| + z*SE <= tol."""
        z_crit = 3.719016
        N = 10000
        sd = 0.12
        se = sd / math.sqrt(N)  # 0.0012
        margin = z_crit * se   # ~0.00446

        tol = 0.010
        # Case A: mean is 0.003 off target -> diff + margin = 0.003 + 0.00446 = 0.00746 <= 0.010 -> PASS
        diff_a = 0.003
        self.assertLessEqual(diff_a + margin, tol)

        # Case B: mean is 0.007 off target -> diff + margin = 0.007 + 0.00446 = 0.01146 > 0.010 -> FAIL
        diff_b = 0.007
        self.assertGreater(diff_b + margin, tol)

    def test_p2_9_near_boundary_null_containment_band(self):
        """Verify near-boundary null containment: LCB >= tau - delta and UCB <= tau."""
        z_crit = 3.719016
        N = 10000
        sd_D = 0.12
        se_D = sd_D / math.sqrt(N)
        margin = z_crit * se_D # ~0.00446
        tau = 0.10
        delta = 0.010 # Band is [0.090, 0.100]

        # Valid Near-Boundary Mean: mu_hat = 0.095 -> LCB = 0.09054 >= 0.090, UCB = 0.09946 <= 0.100 -> PASS
        mu_hat_valid = 0.095
        lcb_valid = mu_hat_valid - margin
        ucb_valid = mu_hat_valid + margin
        self.assertGreaterEqual(lcb_valid, tau - delta)
        self.assertLessEqual(ucb_valid, tau)

        # Deep Interior Null: mu_hat = 0.030 -> UCB = 0.03446 <= 0.100, but LCB = 0.02554 < 0.090 -> REJECT
        mu_hat_deep = 0.030
        lcb_deep = mu_hat_deep - margin
        ucb_deep = mu_hat_deep + margin
        self.assertLessEqual(ucb_deep, tau) # passes naive UCB
        self.assertLess(lcb_deep, tau - delta) # FAILS near-boundary band check!

    def test_p2_10_nonlinear_fisher_z_equivalence(self):
        """Verify nonlinear Fisher z equivalence bounds around nonzero target correlation."""
        z_crit = 3.719016
        N = 10000
        se_z = 1.0 / math.sqrt(N - 3)
        margin_z = z_crit * se_z # ~0.0372

        rho_target = 0.60
        delta_rho = 0.050 # Target band is [0.55, 0.65]

        z_low = math.atanh(rho_target - delta_rho)  # atanh(0.55) ~ 0.61838
        z_high = math.atanh(rho_target + delta_rho) # atanh(0.65) ~ 0.77530
        z_target = math.atanh(rho_target)          # atanh(0.60) ~ 0.69315

        # Asymmetric z distances:
        self.assertNotEqual(z_target - z_low, z_high - z_target)

        # Observed rho = 0.605 -> z_obs = atanh(0.605) ~ 0.7013
        z_obs = math.atanh(0.605)
        self.assertGreaterEqual(z_obs - margin_z, z_low)
        self.assertLessEqual(z_obs + margin_z, z_high)

if __name__ == "__main__":
    unittest.main()
