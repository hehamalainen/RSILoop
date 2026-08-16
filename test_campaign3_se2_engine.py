import unittest
import math
import os
import sys
import subprocess
import tempfile
import json
import fractions
import numpy as np
from typing import List, Tuple, Dict, Any, Optional
from campaign3.sim.prng_v2 import DomainSeparatedPRNGV2, PLAN_HASH_HEX
from campaign3.sim.model import SimulationModel
from campaign3.sim.model_v2 import SimulationModelV2
from campaign3.sim.calibrator_v2 import (
    DeterministicCalibratorV2,
    IndependentValidatorV2,
    ScenarioCalibrationError,
    ScenarioValidationError,
    compute_sample_moments,
    compute_sd_standard_error,
    evaluate_moments_decision,
    validate_scenario_spec,
    compute_calibration_record_digest,
    get_rfc_sha256,
    PLAN_HASH_HEX as CAL_PLAN_HASH_HEX,
    PRNG_ALGORITHM,
    QUANTIZER_CONTRACT,
    QUANTIZER_VERSION
)
from campaign3.sim.quantizer import (
    quantize_earned_points,
    clip_to_rational,
    clip_to_fraction,
    quantize_earned_points_array,
    quantize_earned_points_array_inplace,
    quantize_scores_array
)
from campaign3.sim.generate_se2_walkthrough import emit_reconciliation_record, generate_walkthrough

class InnovationTapeAdapter:
    """Mock adapter for recording and replaying exact innovation and failure tapes between SE-1 and SE-2."""
    def __init__(self, task_effects: np.ndarray, z_vecs: List[np.ndarray], fail_draws: Dict[Tuple[int, int, int, str, int], float]):
        self.task_effects = task_effects
        self.z_vecs = z_vecs
        self.fail_draws = fail_draws

    def get_task_effects(self, mc_iteration: int) -> np.ndarray:
        return self.task_effects

    def get_z_vec(self, mc_iteration: int, block: int, mutation_idx: int) -> np.ndarray:
        return self.z_vecs[mutation_idx]

    def get_fail_draw(self, mc_iteration: int, block: int, mutation_idx: int, arm: str, rep: int) -> float:
        return self.fail_draws.get((mc_iteration, block, mutation_idx, arm, rep), 0.5)

class Gate1UnguidedShiftAdapter:
    """Validation adapter shifting unguided innovations to isolate mu_D gate failure."""
    def __init__(self, prng_v2: DomainSeparatedPRNGV2, scenario_id: str, fixture_name: str, shift: float = 0.50):
        self.prng_v2 = prng_v2
        self.scenario_id = scenario_id
        self.fixture_name = fixture_name
        self.shift = shift

    def get_z_vec(self, mc_iteration: int, block: int, mutation_index: int) -> np.ndarray:
        z = self.prng_v2.draw_multivariate_normal(
            mean=np.zeros(4),
            cov=np.eye(4),
            scenario_id=self.scenario_id,
            fixture=self.fixture_name,
            domain="conformance_validation",
            purpose="step_z",
            mc_iteration=mc_iteration,
            block=block,
            mutation_index=mutation_index
        )
        z_mod = z.copy()
        z_mod[2:] += self.shift  # shift unguided arm innovations
        return z_mod

class Gate2BaselineShiftAdapter:
    """Validation adapter shifting confirmatory baseline evaluation to isolate mu_A gate failure."""
    def __init__(self, shift: float = -0.05):
        self.shift = shift

    def get_confirmatory_baseline_shift(self) -> float:
        return self.shift

class TestCampaign3SE2Engine(unittest.TestCase):
    def setUp(self):
        self.fixture = {
            "fixture_name": "test_fixture",
            "num_search_tasks": 8,
            "num_conf_tasks": 8,
            "num_replicates": 2,
            "mutation_count": 5,
            "search_denominators": [8, 10, 10, 12, 12, 16, 16, 20],
            "conf_denominators": [8, 10, 10, 12, 12, 16, 16, 20],
            "task_effect_mean": 0.50,
            "task_effect_cov": (np.eye(16) * 0.01).tolist(),
            "arm_correlation": 0.50,
            "replicate_correlation": 0.30,
            "guided_params": {"step_type": "two_point", "delta": 0.10, "failure_rate": 0.10},
            "unguided_params": {"step_type": "two_point", "delta": 0.10, "failure_rate": 0.10},
            "kappa_G": 0.80,
            "kappa_U": 0.50
        }

    # -------------------------------------------------------------------------
    # R1: Scenario Specification, Boundary & Covariance Invariant Enforcement
    # -------------------------------------------------------------------------
    def test_r1_scenario_spec_boundary_and_covariance_invariants(self):
        """Verify scenario spec validator enforces exact registered null boundary and covariance invariants fail-closed."""
        # 1. Valid N_delta (mean_D == 0.10)
        valid_ndelta = {
            "scenario_id": "ndelta_valid",
            "scenario_class": "N_delta",
            "target_moments": {"mean_D": 0.10, "mean_A": 0.20, "sd_D": 0.12, "sd_A": 0.12, "rho_D_A": 0.50}
        }
        validate_scenario_spec(valid_ndelta)

        # 2. Invalid N_delta (mean_D == 0.02) must raise
        invalid_ndelta = {
            "scenario_id": "ndelta_invalid",
            "scenario_class": "N_delta",
            "target_moments": {"mean_D": 0.02, "mean_A": 0.20, "sd_D": 0.12, "sd_A": 0.12, "rho_D_A": 0.50}
        }
        with self.assertRaises(ScenarioValidationError):
            validate_scenario_spec(invalid_ndelta)

        # 3. Valid N_A (mean_A == 0.00)
        valid_na = {
            "scenario_id": "na_valid",
            "scenario_class": "N_A",
            "target_moments": {"mean_D": 0.25, "mean_A": 0.00, "sd_D": 0.12, "sd_A": 0.12, "rho_D_A": 0.50}
        }
        validate_scenario_spec(valid_na)

        # 4. Invalid N_A (mean_A == 0.05) must raise
        invalid_na = {
            "scenario_id": "na_invalid",
            "scenario_class": "N_A",
            "target_moments": {"mean_D": 0.25, "mean_A": 0.05, "sd_D": 0.12, "sd_A": 0.12, "rho_D_A": 0.50}
        }
        with self.assertRaises(ScenarioValidationError):
            validate_scenario_spec(invalid_na)

        # 5. Valid N_intersection_zero_cov (mean_D=0.10, mean_A=0.00, rho=0.00)
        valid_nint_zero = {
            "scenario_id": "nint_zero_valid",
            "scenario_class": "N_intersection_zero_cov",
            "target_moments": {"mean_D": 0.10, "mean_A": 0.00, "sd_D": 0.12, "sd_A": 0.12, "rho_D_A": 0.00}
        }
        validate_scenario_spec(valid_nint_zero)

        # 6. Invalid N_intersection_zero_cov (rho=0.50) must raise
        invalid_nint_zero = {
            "scenario_id": "nint_zero_invalid",
            "scenario_class": "N_intersection_zero_cov",
            "target_moments": {"mean_D": 0.10, "mean_A": 0.00, "sd_D": 0.12, "sd_A": 0.12, "rho_D_A": 0.50}
        }
        with self.assertRaises(ScenarioValidationError):
            validate_scenario_spec(invalid_nint_zero)

        # 7. Valid N_intersection_pos_cov (mean_D=0.10, mean_A=0.00, rho=0.40)
        valid_nint_pos = {
            "scenario_id": "nint_pos_valid",
            "scenario_class": "N_intersection_pos_cov",
            "target_moments": {"mean_D": 0.10, "mean_A": 0.00, "sd_D": 0.12, "sd_A": 0.12, "rho_D_A": 0.40}
        }
        validate_scenario_spec(valid_nint_pos)

        # 8. Invalid N_intersection_pos_cov (rho=0.00) must raise
        invalid_nint_pos = {
            "scenario_id": "nint_pos_invalid",
            "scenario_class": "N_intersection_pos_cov",
            "target_moments": {"mean_D": 0.10, "mean_A": 0.00, "sd_D": 0.12, "sd_A": 0.12, "rho_D_A": 0.00}
        }
        with self.assertRaises(ScenarioValidationError):
            validate_scenario_spec(invalid_nint_pos)

        # 9. Missing rho or non-finite rho must raise
        no_rho_spec = {
            "scenario_id": "no_rho",
            "scenario_class": "N_delta",
            "target_moments": {"mean_D": 0.10, "mean_A": 0.20, "sd_D": 0.12, "sd_A": 0.12}
        }
        with self.assertRaises(ScenarioValidationError):
            validate_scenario_spec(no_rho_spec)

        nan_rho_spec = {
            "scenario_id": "nan_rho",
            "scenario_class": "N_delta",
            "target_moments": {"mean_D": 0.10, "mean_A": 0.20, "sd_D": 0.12, "sd_A": 0.12, "rho_D_A": float("nan")}
        }
        with self.assertRaises(ScenarioValidationError):
            validate_scenario_spec(nan_rho_spec)

    # -------------------------------------------------------------------------
    # R2: Preserve & Reuse SE-1 Mechanics via Class Inheritance & Adapter
    # -------------------------------------------------------------------------
    def test_r2_se1_se2_state_machine_equivalence_on_tape(self):
        """Verify SE-2 preserves identical transition mechanics across two_point, normal, zero, and failures."""
        step_types = [
            ("two_point", {"step_type": "two_point", "delta": 0.10, "failure_rate": 0.20}),
            ("normal", {"step_type": "normal", "step_mean": 0.05, "step_variance": 0.01, "failure_rate": 0.20}),
            ("zero", {"step_type": "zero", "failure_rate": 0.0})
        ]

        for name, param_dict in step_types:
            np.random.seed(101)
            total_tasks = 16
            task_effects = np.random.normal(0.50, 0.10, total_tasks)

            z_vecs = [np.random.normal(0, 1, 4) for _ in range(5)]
            fail_draws = {}
            for m in range(5):
                for arm in ["guided", "unguided"]:
                    for rep in range(2):
                        fail_draws[(0, 0, m, arm, rep)] = 0.10 if (m + rep) % 3 == 0 else 0.80

            adapter = InnovationTapeAdapter(task_effects, z_vecs, fail_draws)

            fixture_test = dict(self.fixture)
            fixture_test["guided_params"] = param_dict
            fixture_test["unguided_params"] = param_dict
            fixture_test["kappa_G"] = 1.0
            fixture_test["kappa_U"] = 1.0

            # 1. Run SE-2 model with adapter
            model_se2 = SimulationModelV2(fixture_test, f"test_eq_{name}", innovation_adapter=adapter)
            model_se2.generate_fixed_task_effects(mc_iteration=0)
            d_se2, a_se2, lineage_se2 = model_se2.simulate_block(mc_iteration=0, block=0)

            # 2. Run SE-1 with mock PRNG replaying exact tape
            class MockPRNG:
                def __init__(self, z_vecs, fail_draws):
                    self.z_vecs = z_vecs
                    self.fail_draws = fail_draws
                def draw_multivariate_normal(self, **kwargs):
                    m = kwargs["mutation_index"]
                    return self.z_vecs[m]
                def draw_uniform(self, **kwargs):
                    m = kwargs["mutation_index"]
                    arm = kwargs["arm"]
                    rep = kwargs["replicate"]
                    return self.fail_draws.get((0, 0, m, arm, rep), 0.5)

            model_se1 = SimulationModel(fixture_test, f"test_eq_{name}")
            model_se1.prng = MockPRNG(z_vecs, fail_draws)
            model_se1.fixed_task_effects = task_effects.copy()
            d_se1, a_se1, lineage_se1 = model_se1.simulate_block(mc_iteration=0, block=0)

            # Assert bit-identical lineage sequences
            self.assertEqual(len(lineage_se2), len(lineage_se1), msg=f"Lineage length mismatch for step_type={name}")
            for ev_se1, ev_se2 in zip(lineage_se1, lineage_se2):
                self.assertEqual(ev_se1.event_type, ev_se2.event_type)
                self.assertEqual(ev_se1.arm, ev_se2.arm)
                self.assertEqual(ev_se1.replicate, ev_se2.replicate)
                self.assertAlmostEqual(ev_se1.theta, ev_se2.theta, places=12)
                self.assertEqual(ev_se1.score, ev_se2.score)

            # Assert bit-identical block differences
            self.assertEqual(d_se1, d_se2)
            self.assertEqual(a_se1, a_se2)

    def test_r2_prng_fixture_domain_separation_under_same_scenario(self):
        """Verify different fixture identities under the same scenario produce distinct PRNG streams."""
        fixture_A = dict(self.fixture, fixture_name="fixture_alpha")
        fixture_B = dict(self.fixture, fixture_name="fixture_beta")

        model_A = SimulationModelV2(fixture_A, scenario_id="shared_scenario_1")
        model_B = SimulationModelV2(fixture_B, scenario_id="shared_scenario_1")

        self.assertEqual(model_A.fixture_name, "fixture_alpha")
        self.assertEqual(model_B.fixture_name, "fixture_beta")

        model_A.generate_fixed_task_effects(mc_iteration=0)
        model_B.generate_fixed_task_effects(mc_iteration=0)

        # Assert task effects are distinct between different fixtures
        self.assertFalse(
            np.allclose(model_A.fixed_task_effects, model_B.fixed_task_effects),
            msg="Different fixtures produced identical task effects; PRNG fixture domain separation failed."
        )

    def test_r2_fixture_name_required_nonempty(self):
        """Verify SimulationModelV2 strictly requires non-empty fixture_name string."""
        bad_fixtures = [
            dict(self.fixture, fixture_name=""),
            dict(self.fixture, fixture_name="   "),
            dict(self.fixture, fixture_name=None),
            {k: v for k, v in self.fixture.items() if k != "fixture_name"},
        ]
        for bad_fix in bad_fixtures:
            with self.assertRaises(ValueError):
                SimulationModelV2(bad_fix, scenario_id="test_scen")

    # -------------------------------------------------------------------------
    # R3: Canonical Quantizer & Search Scoring Delegation
    # -------------------------------------------------------------------------
    def test_r3_search_and_confirmatory_scoring_delegates_to_quantizer(self):
        """Verify SimulationModelV2 search and confirmatory scoring routes to canonical quantizer."""
        model = SimulationModelV2(self.fixture, "test_quant")
        model.fixed_task_effects = np.array([0.50] * 16)

        # 1. _clip_to_rational uses quantizer clip_to_rational
        score_r = model._clip_to_rational(0.25, 10)
        self.assertEqual(score_r.earned, 2)  # 2.5 rounds to 2 (banker's round-to-even)
        self.assertEqual(score_r.denominator, 10)
        self.assertEqual(score_r.value, fractions.Fraction(1, 5))

        # 2. evaluate_search uses quantizer
        score_s = model.evaluate_search(0.0)
        self.assertEqual(score_s.value, fractions.Fraction(1, 2))

        # 3. Array quantizer kernel checks
        test_vals = np.array([0.15, 0.25, 0.35])
        test_denoms = np.array([10, 10, 10])
        earned_arr = quantize_earned_points_array(test_vals, test_denoms)
        np.testing.assert_array_equal(earned_arr, [2, 2, 4])

        scores_arr = quantize_scores_array(test_vals, test_denoms)
        np.testing.assert_array_almost_equal(scores_arr, [0.2, 0.2, 0.4])

    def test_r3_multi_replicate_calibration_and_validation(self):
        """Verify that when k=3, calibrator evaluates all 3 replicates and uses centralized quantizer."""
        fixture_k3 = dict(self.fixture)
        fixture_k3["num_replicates"] = 3

        scenario = {
            "scenario_id": "test_k3",
            "scenario_class": "H1_target",
            "target_moments": {"mean_D": 0.12, "mean_A": 0.15, "sd_D": 0.10, "sd_A": 0.10, "rho_D_A": 0.50}
        }

        calibrator = DeterministicCalibratorV2(fixture_k3, "test_k3", execution_mode="NON_SCIENTIFIC_ENGINE_TEST")
        k_G, k_U, report = calibrator.calibrate(scenario, n_cal=100)
        self.assertEqual(report["replicate_count"], 3)
        self.assertIn("milli_G_star", report["calibrated_parameters"])

    # -------------------------------------------------------------------------
    # R4: Fully Bound Calibration Record, Mandatory Trusted Digest Anchor & Tamper Rejection
    # -------------------------------------------------------------------------
    def test_r4_calibration_record_tamper_rejection(self):
        """Verify IndependentValidatorV2 strictly requires a 64-hex trusted anchor and rejects each tampered field."""
        with tempfile.TemporaryDirectory() as temp_dir:
            scenario = {
                "scenario_id": "test_tamper",
                "scenario_class": "N_delta",
                "target_moments": {"mean_D": 0.10, "mean_A": 0.20, "sd_D": 0.12, "sd_A": 0.12, "rho_D_A": 0.50}
            }

            calibrator = DeterministicCalibratorV2(self.fixture, "test_tamper", execution_mode="NON_SCIENTIFIC_ENGINE_TEST")
            k_G, k_U, valid_record = calibrator.calibrate(
                scenario,
                n_cal=50,
                record_dir=temp_dir
            )
            trusted_digest = valid_record["record_digest"]

            # Valid constructor passes in matching mode with mandatory trusted anchor
            validator = IndependentValidatorV2(
                self.fixture,
                scenario,
                valid_record,
                expected_record_digest=trusted_digest,
                execution_mode="NON_SCIENTIFIC_ENGINE_TEST"
            )

            # 1. Test omitted / None / non-string expected_record_digest raises ScenarioValidationError
            with self.assertRaises(ScenarioValidationError):
                IndependentValidatorV2(self.fixture, scenario, valid_record, expected_record_digest=None)
            with self.assertRaises(ScenarioValidationError):
                IndependentValidatorV2(self.fixture, scenario, valid_record, expected_record_digest="")
            with self.assertRaises(ScenarioValidationError):
                IndependentValidatorV2(self.fixture, scenario, valid_record, expected_record_digest=123456)

            # 2. Test malformed expected_record_digest (wrong length, uppercase, non-hex) raises ScenarioValidationError
            with self.assertRaises(ScenarioValidationError):
                IndependentValidatorV2(self.fixture, scenario, valid_record, expected_record_digest="a" * 63)
            with self.assertRaises(ScenarioValidationError):
                IndependentValidatorV2(self.fixture, scenario, valid_record, expected_record_digest="a" * 65)
            with self.assertRaises(ScenarioValidationError):
                IndependentValidatorV2(self.fixture, scenario, valid_record, expected_record_digest=trusted_digest.upper())
            with self.assertRaises(ScenarioValidationError):
                IndependentValidatorV2(self.fixture, scenario, valid_record, expected_record_digest="z" * 64)

            # 3. Test wrong (mismatched) valid 64-hex expected_record_digest raises ScenarioValidationError
            wrong_digest = "0" * 64
            with self.assertRaises(ScenarioValidationError):
                IndependentValidatorV2(self.fixture, scenario, valid_record, expected_record_digest=wrong_digest, execution_mode="NON_SCIENTIFIC_ENGINE_TEST")

            # 4. Test typo in execution_mode raises ValueError immediately
            with self.assertRaises(ValueError):
                IndependentValidatorV2(self.fixture, scenario, valid_record, expected_record_digest=trusted_digest, execution_mode="SCIENTIFC")
            with self.assertRaises(ValueError):
                DeterministicCalibratorV2(self.fixture, "test_tamper", execution_mode="SCIENTIFC")

            # 5. Test asymmetric mode mismatch: SCIENTIFIC validator rejecting NON_SCIENTIFIC record
            with self.assertRaises(ScenarioValidationError):
                IndependentValidatorV2(self.fixture, scenario, valid_record, expected_record_digest=trusted_digest, execution_mode="SCIENTIFIC")

            # 6. Test asymmetric mode mismatch: NON_SCIENTIFIC validator rejecting SCIENTIFIC record
            scientific_record = dict(valid_record)
            scientific_record["execution_mode"] = "SCIENTIFIC"
            scientific_record["n_cal"] = 10000
            sci_digest = compute_calibration_record_digest(scientific_record)
            scientific_record["record_digest"] = sci_digest
            with self.assertRaises(ScenarioValidationError):
                IndependentValidatorV2(self.fixture, scenario, scientific_record, expected_record_digest=sci_digest, execution_mode="NON_SCIENTIFIC_ENGINE_TEST")

            # 7. Test strict rejection of innovation_adapter in SCIENTIFIC mode
            class DummyAdapter: pass
            with self.assertRaises(ScenarioValidationError):
                IndependentValidatorV2(self.fixture, scenario, valid_record, expected_record_digest=trusted_digest, execution_mode="SCIENTIFIC", innovation_adapter=DummyAdapter())

            # 8. Test coordinated tampering with recomputed in-record digest rejected by trusted anchor (NON_SCIENTIFIC & SCIENTIFIC)
            tampered_coord_k_G = json.loads(json.dumps(valid_record))
            tampered_coord_k_G["calibrated_parameters"]["kappa_G_star"] = 0.999
            tampered_coord_k_G["calibrated_parameters"]["milli_G_star"] = 999
            tampered_coord_k_G["record_digest"] = compute_calibration_record_digest(tampered_coord_k_G)
            with self.assertRaises(ScenarioValidationError):
                IndependentValidatorV2(
                    self.fixture, scenario, tampered_coord_k_G,
                    expected_record_digest=trusted_digest,
                    execution_mode="NON_SCIENTIFIC_ENGINE_TEST"
                )

            # Forged SCIENTIFIC record with recomputed internal digest tested against trusted anchor
            sci_forged = json.loads(json.dumps(scientific_record))
            sci_forged["calibrated_parameters"]["kappa_G_star"] = 0.999
            sci_forged["calibrated_parameters"]["milli_G_star"] = 999
            sci_forged["record_digest"] = compute_calibration_record_digest(sci_forged)
            with self.assertRaises(ScenarioValidationError):
                IndependentValidatorV2(
                    self.fixture, scenario, sci_forged,
                    expected_record_digest=sci_digest,
                    execution_mode="SCIENTIFIC"
                )

            # 9. Test missing or tampered rss_diagnostic structure
            tampered_no_rss = json.loads(json.dumps(valid_record))
            del tampered_no_rss["chunk_parameters"]["rss_diagnostic"]
            with self.assertRaises(ScenarioValidationError):
                IndependentValidatorV2(self.fixture, scenario, tampered_no_rss, expected_record_digest=trusted_digest, execution_mode="NON_SCIENTIFIC_ENGINE_TEST")

            tampered_synthetic_rss = json.loads(json.dumps(valid_record))
            tampered_synthetic_rss["chunk_parameters"]["rss_diagnostic"] = {
                "status": "UNAVAILABLE_ZERO_RAW_RSS",
                "rss_bytes": 1048576
            }
            with self.assertRaises(ScenarioValidationError):
                IndependentValidatorV2(self.fixture, scenario, tampered_synthetic_rss, expected_record_digest=trusted_digest, execution_mode="NON_SCIENTIFIC_ENGINE_TEST")

            tampered_zero_avail_rss = json.loads(json.dumps(valid_record))
            tampered_zero_avail_rss["chunk_parameters"]["rss_diagnostic"] = {
                "status": "AVAILABLE",
                "rss_bytes": 0
            }
            with self.assertRaises(ScenarioValidationError):
                IndependentValidatorV2(self.fixture, scenario, tampered_zero_avail_rss, expected_record_digest=trusted_digest, execution_mode="NON_SCIENTIFIC_ENGINE_TEST")

            # 10. Test tampering individual fields
            tamper_cases = [
                ("scenario_id", "wrong_id"),
                ("scenario_class", "wrong_class"),
                ("controlling_plan_hash", "wrong_hash"),
                ("rfc_hash", "wrong_rfc"),
                ("prng_algorithm", "wrong_algo"),
                ("prng_version", "wrong_prng_ver"),
                ("quantizer_contract", "wrong_contract"),
                ("quantizer_version", "wrong_quant_ver"),
                ("fixture_hash", "tampered_fixture_hash"),
                ("scenario_hash", "tampered_scenario_hash"),
                ("replicate_count", 99),
                ("n_cal", 25),
            ]

            for field, bad_val in tamper_cases:
                tampered = json.loads(json.dumps(valid_record))
                tampered[field] = bad_val
                with self.assertRaises(ScenarioValidationError, msg=f"Failed to reject tampered {field}"):
                    IndependentValidatorV2(self.fixture, scenario, tampered, expected_record_digest=trusted_digest, execution_mode="NON_SCIENTIFIC_ENGINE_TEST")

    # -------------------------------------------------------------------------
    # R5: Resource Ceiling Measurement at Addendum D Envelope (Child Process)
    # -------------------------------------------------------------------------
    def test_r5_resource_ceiling_at_envelope(self):
        """Verify calibration-owned live buffer bytes stays strictly <= 64,000,000 bytes at maximum shape."""
        child_script = """
import json
import sys
import resource
import numpy as np
from campaign3.sim.calibrator_v2 import DeterministicCalibratorV2

fixture = {
    "fixture_name": "envelope_fixture",
    "num_search_tasks": 8,
    "num_conf_tasks": 8,
    "num_replicates": 5,
    "mutation_count": 5,
    "search_denominators": [8, 10, 10, 12, 12, 16, 16, 20],
    "conf_denominators": [8, 10, 10, 12, 12, 16, 16, 20],
    "task_effect_mean": 0.50,
    "task_effect_cov": (np.eye(16) * 0.01).tolist(),
    "arm_correlation": 0.50,
    "replicate_correlation": 0.30,
    "guided_params": {"step_type": "two_point", "delta": 0.10, "failure_rate": 0.10},
    "unguided_params": {"step_type": "two_point", "delta": 0.10, "failure_rate": 0.10},
    "kappa_G": 0.80,
    "kappa_U": 0.50
}

scenario = {
    "scenario_id": "envelope_test",
    "scenario_class": "N_delta",
    "target_moments": {"mean_D": 0.10, "mean_A": 0.20, "sd_D": 0.10, "sd_A": 0.10, "rho_D_A": 0.50}
}

calibrator = DeterministicCalibratorV2(fixture, "envelope_test", execution_mode="NON_SCIENTIFIC_ENGINE_TEST")
calibrator.KAPPA_CHUNK_SIZE = 100
calibrator.ITER_CHUNK_SIZE = 500

_, _, report = calibrator.calibrate(scenario, n_cal=10000)
peak_bytes = calibrator.measured_peak_buffer_bytes
rusage = resource.getrusage(resource.RUSAGE_SELF)
rss_bytes = rusage.ru_maxrss if sys.platform == "darwin" else rusage.ru_maxrss * 1024

print(json.dumps({"peak_buffer_bytes": peak_bytes, "rss_bytes": rss_bytes}))
"""
        proc = subprocess.run([sys.executable, "-c", child_script], capture_output=True, text=True, check=True)
        out = json.loads(proc.stdout.strip())
        peak_bytes = out["peak_buffer_bytes"]
        rss_bytes = out["rss_bytes"]

        self.assertLessEqual(peak_bytes, 64_000_000)
        self.assertGreater(peak_bytes, 0)
        self.assertGreater(rss_bytes, 0)

    # -------------------------------------------------------------------------
    # R6: Isolated 5-Gate Tests, End-to-End Validation, and Failure Records
    # -------------------------------------------------------------------------
    def test_r6_pure_decision_function_isolated_gates(self):
        """Verify each of the 5 moment failure gates is independently isolated in the pure decision helper."""
        scenario_ndelta = {
            "scenario_id": "ndelta_gate_test",
            "scenario_class": "N_delta",
            "target_moments": {"mean_D": 0.10, "mean_A": 0.20, "sd_D": 0.080, "sd_A": 0.080, "rho_D_A": 0.50}
        }
        scenario_na = {
            "scenario_id": "na_gate_test",
            "scenario_class": "N_A",
            "target_moments": {"mean_D": 0.25, "mean_A": 0.00, "sd_D": 0.080, "sd_A": 0.080, "rho_D_A": 0.50}
        }

        # Case 0: All 5 Gates Pass
        dec_0 = evaluate_moments_decision(
            mean_D=0.095, mean_A=0.200, sd_D=0.080, sd_A=0.080,
            gamma2_D=0.0, gamma2_A=0.0, rho=0.50,
            scenario=scenario_ndelta, n_val=10000
        )
        self.assertTrue(dec_0["validation_passed"])
        self.assertEqual(dec_0["pass_details"], {
            "pass_mu_D": True, "pass_mu_A": True, "pass_sd_D": True, "pass_sd_A": True, "pass_rho": True
        })

        # Gate 1 Fail Isolated (mu_D too low: 0.050 on N_delta band [0.090, 0.100])
        dec_g1_low = evaluate_moments_decision(
            mean_D=0.050, mean_A=0.200, sd_D=0.080, sd_A=0.080,
            gamma2_D=0.0, gamma2_A=0.0, rho=0.50,
            scenario=scenario_ndelta, n_val=10000
        )
        self.assertFalse(dec_g1_low["validation_passed"])
        self.assertEqual(dec_g1_low["pass_details"], {
            "pass_mu_D": False, "pass_mu_A": True, "pass_sd_D": True, "pass_sd_A": True, "pass_rho": True
        })

        # Gate 1 Fail Isolated (mu_D too high: 0.150 on N_delta band [0.090, 0.100])
        dec_g1_high = evaluate_moments_decision(
            mean_D=0.150, mean_A=0.200, sd_D=0.080, sd_A=0.080,
            gamma2_D=0.0, gamma2_A=0.0, rho=0.50,
            scenario=scenario_ndelta, n_val=10000
        )
        self.assertFalse(dec_g1_high["validation_passed"])
        self.assertEqual(dec_g1_high["pass_details"], {
            "pass_mu_D": False, "pass_mu_A": True, "pass_sd_D": True, "pass_sd_A": True, "pass_rho": True
        })

        # Gate 2 Fail Isolated (mu_A too low: -0.050 on N_A band [-0.010, 0.000])
        dec_g2_low = evaluate_moments_decision(
            mean_D=0.250, mean_A=-0.050, sd_D=0.080, sd_A=0.080,
            gamma2_D=0.0, gamma2_A=0.0, rho=0.50,
            scenario=scenario_na, n_val=10000
        )
        self.assertFalse(dec_g2_low["validation_passed"])
        self.assertEqual(dec_g2_low["pass_details"], {
            "pass_mu_D": True, "pass_mu_A": False, "pass_sd_D": True, "pass_sd_A": True, "pass_rho": True
        })

        # Gate 2 Fail Isolated (mu_A too high: 0.050 on N_A band [-0.010, 0.000])
        dec_g2_high = evaluate_moments_decision(
            mean_D=0.250, mean_A=0.050, sd_D=0.080, sd_A=0.080,
            gamma2_D=0.0, gamma2_A=0.0, rho=0.50,
            scenario=scenario_na, n_val=10000
        )
        self.assertFalse(dec_g2_high["validation_passed"])
        self.assertEqual(dec_g2_high["pass_details"], {
            "pass_mu_D": True, "pass_mu_A": False, "pass_sd_D": True, "pass_sd_A": True, "pass_rho": True
        })

        # Gate 3 Fail Isolated (sd_D outside tolerance 0.015: observed 0.040 vs target 0.080)
        dec_g3 = evaluate_moments_decision(
            mean_D=0.095, mean_A=0.200, sd_D=0.040, sd_A=0.080,
            gamma2_D=0.0, gamma2_A=0.0, rho=0.50,
            scenario=scenario_ndelta, n_val=10000
        )
        self.assertFalse(dec_g3["validation_passed"])
        self.assertEqual(dec_g3["pass_details"], {
            "pass_mu_D": True, "pass_mu_A": True, "pass_sd_D": False, "pass_sd_A": True, "pass_rho": True
        })

        # Gate 4 Fail Isolated (sd_A outside tolerance 0.015: observed 0.040 vs target 0.080)
        dec_g4 = evaluate_moments_decision(
            mean_D=0.095, mean_A=0.200, sd_D=0.080, sd_A=0.040,
            gamma2_D=0.0, gamma2_A=0.0, rho=0.50,
            scenario=scenario_ndelta, n_val=10000
        )
        self.assertFalse(dec_g4["validation_passed"])
        self.assertEqual(dec_g4["pass_details"], {
            "pass_mu_D": True, "pass_mu_A": True, "pass_sd_D": True, "pass_sd_A": False, "pass_rho": True
        })

        # Gate 5 Fail Isolated (rho outside Fisher z tolerance 0.050: observed -0.50 vs target 0.50)
        dec_g5 = evaluate_moments_decision(
            mean_D=0.095, mean_A=0.200, sd_D=0.080, sd_A=0.080,
            gamma2_D=0.0, gamma2_A=0.0, rho=-0.50,
            scenario=scenario_ndelta, n_val=10000
        )
        self.assertFalse(dec_g5["validation_passed"])
        self.assertEqual(dec_g5["pass_details"], {
            "pass_mu_D": True, "pass_mu_A": True, "pass_sd_D": True, "pass_sd_A": True, "pass_rho": False
        })

    def test_r6_end_to_end_successful_validation(self):
        """Verify genuine successful calibration and independent validation pass end-to-end."""
        scenario = {
            "scenario_id": "test_r6_pass",
            "scenario_class": "H1_target",
            "target_moments": {"mean_D": 0.12, "mean_A": 0.15, "sd_D": 0.056, "sd_A": 0.058, "rho_D_A": 0.96}
        }
        calibrator = DeterministicCalibratorV2(self.fixture, "test_r6_pass", execution_mode="NON_SCIENTIFIC_ENGINE_TEST")
        k_G, k_U, rec = calibrator.calibrate(scenario, n_cal=1000)

        validator = IndependentValidatorV2(
            self.fixture, scenario, rec,
            expected_record_digest=rec["record_digest"],
            execution_mode="NON_SCIENTIFIC_ENGINE_TEST"
        )
        val_result = validator.validate(n_val=10000)
        self.assertTrue(val_result["validation_passed"])
        self.assertEqual(val_result["pass_details"], {
            "pass_mu_D": True, "pass_mu_A": True, "pass_sd_D": True, "pass_sd_A": True, "pass_rho": True
        })

    def test_r6_end_to_end_per_gate_validation_failures_through_validate(self):
        """
        Verify all 5 failure gates through end-to-end validator.validate() calls,
        without forging or rewriting any calibration records.
        Confirms ScenarioValidationError is raised and se2_validation_failure_record.json
        contains EXACTLY the targeted gate as False and ALL other four gates as True.
        """
        prng_v2 = DomainSeparatedPRNGV2()

        # 1. Base scenario calibration (100% authentic, unmodified record with valid record_digest)
        base_scenario = {
            "scenario_id": "test_base_gate",
            "scenario_class": "H1_target",
            "target_moments": {"mean_D": 0.12, "mean_A": 0.15, "sd_D": 0.056, "sd_A": 0.058, "rho_D_A": 0.96}
        }
        cal = DeterministicCalibratorV2(self.fixture, "test_base_gate", execution_mode="NON_SCIENTIFIC_ENGINE_TEST")
        _, _, base_rec = cal.calibrate(base_scenario, n_cal=1000)

        # Gate 1: mu_D fails isolated via unguided innovation shift adapter
        g1_adapter = Gate1UnguidedShiftAdapter(prng_v2, "test_base_gate", self.fixture["fixture_name"], shift=0.50)
        val1 = IndependentValidatorV2(
            self.fixture, base_scenario, base_rec,
            expected_record_digest=base_rec["record_digest"],
            execution_mode="NON_SCIENTIFIC_ENGINE_TEST",
            innovation_adapter=g1_adapter
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(ScenarioValidationError):
                val1.validate(n_val=10000, failure_record_dir=temp_dir)
            with open(os.path.join(temp_dir, "se2_validation_failure_record.json"), "r", encoding="utf-8") as f:
                r1 = json.load(f)
            self.assertEqual(r1["pass_details"], {"pass_mu_D": False, "pass_mu_A": True, "pass_sd_D": True, "pass_sd_A": True, "pass_rho": True})

        # Gate 2: mu_A fails isolated via confirmatory baseline shift adapter
        g2_adapter = Gate2BaselineShiftAdapter(shift=-0.05)
        val2 = IndependentValidatorV2(
            self.fixture, base_scenario, base_rec,
            expected_record_digest=base_rec["record_digest"],
            execution_mode="NON_SCIENTIFIC_ENGINE_TEST",
            innovation_adapter=g2_adapter
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(ScenarioValidationError):
                val2.validate(n_val=10000, failure_record_dir=temp_dir)
            with open(os.path.join(temp_dir, "se2_validation_failure_record.json"), "r", encoding="utf-8") as f:
                r2 = json.load(f)
            self.assertEqual(r2["pass_details"], {"pass_mu_D": True, "pass_mu_A": False, "pass_sd_D": True, "pass_sd_A": True, "pass_rho": True})

        # Gate 3: sd_D fails isolated via genuine calibration of target scenario with sd_D=0.90
        scen3 = {
            "scenario_id": "test_g3_gate",
            "scenario_class": "H1_target",
            "target_moments": {"mean_D": 0.12, "mean_A": 0.15, "sd_D": 0.90, "sd_A": 0.058, "rho_D_A": 0.96}
        }
        cal3 = DeterministicCalibratorV2(self.fixture, "test_g3_gate", execution_mode="NON_SCIENTIFIC_ENGINE_TEST")
        _, _, rec3 = cal3.calibrate(scen3, n_cal=1000)
        val3 = IndependentValidatorV2(
            self.fixture, scen3, rec3,
            expected_record_digest=rec3["record_digest"],
            execution_mode="NON_SCIENTIFIC_ENGINE_TEST"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(ScenarioValidationError):
                val3.validate(n_val=10000, failure_record_dir=temp_dir)
            with open(os.path.join(temp_dir, "se2_validation_failure_record.json"), "r", encoding="utf-8") as f:
                r3 = json.load(f)
            self.assertEqual(r3["pass_details"], {"pass_mu_D": True, "pass_mu_A": True, "pass_sd_D": False, "pass_sd_A": True, "pass_rho": True})

        # Gate 4: sd_A fails isolated via genuine calibration of target scenario with sd_A=0.90
        scen4 = {
            "scenario_id": "test_g4_gate",
            "scenario_class": "H1_target",
            "target_moments": {"mean_D": 0.12, "mean_A": 0.15, "sd_D": 0.056, "sd_A": 0.90, "rho_D_A": 0.96}
        }
        cal4 = DeterministicCalibratorV2(self.fixture, "test_g4_gate", execution_mode="NON_SCIENTIFIC_ENGINE_TEST")
        _, _, rec4 = cal4.calibrate(scen4, n_cal=1000)
        val4 = IndependentValidatorV2(
            self.fixture, scen4, rec4,
            expected_record_digest=rec4["record_digest"],
            execution_mode="NON_SCIENTIFIC_ENGINE_TEST"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(ScenarioValidationError):
                val4.validate(n_val=10000, failure_record_dir=temp_dir)
            with open(os.path.join(temp_dir, "se2_validation_failure_record.json"), "r", encoding="utf-8") as f:
                r4 = json.load(f)
            self.assertEqual(r4["pass_details"], {"pass_mu_D": True, "pass_mu_A": True, "pass_sd_D": True, "pass_sd_A": False, "pass_rho": True})

        # Gate 5: rho fails isolated via genuine calibration of target scenario with rho_D_A=0.50
        scen5 = {
            "scenario_id": "test_g5_gate",
            "scenario_class": "H1_target",
            "target_moments": {"mean_D": 0.12, "mean_A": 0.15, "sd_D": 0.056, "sd_A": 0.058, "rho_D_A": 0.50}
        }
        cal5 = DeterministicCalibratorV2(self.fixture, "test_g5_gate", execution_mode="NON_SCIENTIFIC_ENGINE_TEST")
        _, _, rec5 = cal5.calibrate(scen5, n_cal=1000)
        val5 = IndependentValidatorV2(
            self.fixture, scen5, rec5,
            expected_record_digest=rec5["record_digest"],
            execution_mode="NON_SCIENTIFIC_ENGINE_TEST"
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            with self.assertRaises(ScenarioValidationError):
                val5.validate(n_val=10000, failure_record_dir=temp_dir)
            with open(os.path.join(temp_dir, "se2_validation_failure_record.json"), "r", encoding="utf-8") as f:
                r5 = json.load(f)
            self.assertEqual(r5["pass_details"], {"pass_mu_D": True, "pass_mu_A": True, "pass_sd_D": True, "pass_sd_A": True, "pass_rho": False})

    def test_r6_calibration_failure_record_emission(self):
        """Verify calibration failure raises ScenarioCalibrationError and writes se2_calibration_failure_record.json."""
        with tempfile.TemporaryDirectory() as temp_dir:
            impossible_scenario = {
                "scenario_id": "test_cal_fail",
                "scenario_class": "H1_target",
                "target_moments": {"mean_D": 0.99, "mean_A": 0.99, "sd_D": 0.080, "sd_A": 0.080, "rho_D_A": 0.50}
            }
            calibrator = DeterministicCalibratorV2(self.fixture, "test_cal_fail", execution_mode="NON_SCIENTIFIC_ENGINE_TEST")
            with self.assertRaises(ScenarioCalibrationError):
                calibrator.calibrate(impossible_scenario, n_cal=50, record_dir=temp_dir)

            fail_path = os.path.join(temp_dir, "se2_calibration_failure_record.json")
            self.assertTrue(os.path.exists(fail_path))
            with open(fail_path, "r", encoding="utf-8") as f:
                fail_data = json.load(f)
            self.assertEqual(fail_data["record_type"], "SE2_CALIBRATION_RECORD")

    # -------------------------------------------------------------------------
    # R7: Numeric Contracts & Hand-Computed Fixtures (Addendum C)
    # -------------------------------------------------------------------------
    def test_r7_numeric_contracts_and_hand_computed_fixtures(self):
        """Verify ddof=1 for SD, divisor N for moments, exact kurtosis, millisteps, and PRNG validation."""
        data = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        mean, sd, gamma2 = compute_sample_moments(data)

        self.assertAlmostEqual(mean, 3.0, places=10)
        self.assertAlmostEqual(sd, math.sqrt(2.5), places=10)
        self.assertAlmostEqual(gamma2, -1.3, places=10)

        # Zero-variance case
        const_data = np.array([3.0, 3.0, 3.0, 3.0])
        mean_c, sd_c, gamma2_c = compute_sample_moments(const_data)
        self.assertEqual(sd_c, 0.0)
        self.assertEqual(gamma2_c, 0.0)

        # Fail-closed RFC hash lookup
        with self.assertRaises(FileNotFoundError):
            get_rfc_sha256(rfc_path="/nonexistent/path/rfc.md")

        # PRNG validation
        prng = DomainSeparatedPRNGV2()
        with self.assertRaises(ValueError):
            DomainSeparatedPRNGV2(plan_hash_hex="0000000000000000000000000000000000000000000000000000000000000000")
        with self.assertRaises(ValueError):
            prng.derive_seed(scenario_id="s", fixture="f", domain="conformance_validation", purpose="p", mc_iteration=0, arm="invalid_arm")
        with self.assertRaises(ValueError):
            prng.derive_seed(scenario_id="s", fixture="f", domain="invalid_domain", purpose="p", mc_iteration=0)

    # -------------------------------------------------------------------------
    # R8: Metamorphic Test for Two-Stage Decoupled Calibration (ALL 4001 Millisteps)
    # -------------------------------------------------------------------------
    def test_r8_metamorphic_two_stage_decoupled_calibration(self):
        """Verify varying kappa_U across ALL 4001 millisteps leaves A and mu_A bit-identical."""
        fixture_meta = dict(self.fixture)
        fixture_meta["kappa_G"] = 0.75

        # Baseline A value
        fixture_meta["kappa_U"] = 0.0
        model_base = SimulationModelV2(fixture_meta, "test_meta")
        model_base.generate_fixed_task_effects(mc_iteration=42)
        _, baseline_A, _ = model_base.simulate_block(mc_iteration=42, block=0)

        # Iterate through all 4001 millisteps from -2000 to +2000
        for milli in range(-2000, 2001, 1):
            fixture_meta["kappa_U"] = milli / 1000.0
            model = SimulationModelV2(fixture_meta, "test_meta")
            model.fixed_task_effects = model_base.fixed_task_effects.copy()
            _, a_val, _ = model.simulate_block(mc_iteration=42, block=0)
            self.assertEqual(a_val, baseline_A, msg=f"Metamorphic divergence at millistep {milli}")

    # -------------------------------------------------------------------------
    # R9: Machine Emission of Reconciliation Record & Walkthrough Generation
    # -------------------------------------------------------------------------
    def test_r9_reconciliation_record_machine_emission_and_walkthrough(self):
        """Verify emit_reconciliation_record dynamically captures environment, test suite, and generates walkthrough."""
        with tempfile.TemporaryDirectory() as temp_dir:
            rec_file = os.path.join(temp_dir, "test_rec.json")
            # 1. Non-executing mode explicitly emits NOT_RUN status and None passed count
            rec_not_run = emit_reconciliation_record(output_path=rec_file, run_tests=False)
            self.assertTrue(os.path.exists(rec_file))
            self.assertEqual(rec_not_run["reconciliation_record_id"], "SE2_RECONCILIATION_RECORD_V2")
            self.assertEqual(rec_not_run["test_discovery"]["targeted_tests_status"], "NOT_RUN")
            self.assertIsNone(rec_not_run["test_discovery"]["targeted_tests_passed"])
            self.assertGreater(rec_not_run["test_discovery"]["master_discovery_test_count"], 300)
            self.assertGreater(rec_not_run["test_discovery"]["targeted_test_count"], 20)

            # 2. Walkthrough formatting from record
            out_walkthrough = os.path.join(temp_dir, "test_walkthrough.md")
            wt = generate_walkthrough(recon_path=rec_file, output_path=out_walkthrough)
            self.assertTrue(os.path.exists(out_walkthrough))
            self.assertIn("SE-2 Scientific Implementation Reconciliation Walkthrough", wt)

if __name__ == "__main__":
    unittest.main()
