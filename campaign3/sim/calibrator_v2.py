import json
import math
import os
import sys
import re
import resource
import hashlib
import fractions
import numpy as np
from typing import Dict, Any, List, Tuple, Optional
from campaign3.sim.model_v2 import SimulationModelV2
from campaign3.sim.quantizer import (
    quantize_earned_points_array_inplace,
    validate_denominator,
    validate_denominators_array,
    QUANTIZER_CONTRACT,
    QUANTIZER_VERSION
)

PLAN_HASH_HEX = "355740d543b65774f66c45481f120810ba8262da59815a19b30e5eabb416b845"
PRNG_ALGORITHM = "HMAC-SHA256-PCG64-V2"
HEX_64_PATTERN = re.compile(r"^[0-9a-f]{64}$")

def get_rfc_sha256(rfc_path: Optional[str] = None) -> str:
    """Compute SHA-256 hex digest of se2_model_rfc.md (fail-closed)."""
    if rfc_path is None:
        rfc_path = os.path.join(os.path.dirname(__file__), "se2_model_rfc.md")
    if not os.path.exists(rfc_path):
        raise FileNotFoundError(f"SE-2 Model RFC not found at: {rfc_path}")
    with open(rfc_path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()

class ScenarioCalibrationError(Exception):
    """Raised when deterministic calibration fails to find parameters within tolerance."""
    pass

class ScenarioValidationError(Exception):
    """Raised when independent validation fails parameter checks or the simultaneous 5-moment gate."""
    pass

def compute_calibration_record_digest(rec: Dict[str, Any]) -> str:
    """
    Compute authoritative SHA-256 cryptographic digest over the complete canonical payload
    of an SE-2 calibration record. Hashes the entire canonical JSON record excluding only 'record_digest'.
    """
    payload = {k: v for k, v in rec.items() if k != "record_digest"}
    canonical_bytes = json.dumps(payload, sort_keys=True).encode("utf-8")
    return hashlib.sha256(canonical_bytes).hexdigest()

def validate_scenario_spec(scenario: Dict[str, Any]) -> None:
    """
    Validate scenario specification and enforce registered boundary invariants fail-closed.
    - N_delta: requires mean_D == 0.10 (exact).
    - N_A: requires mean_A == 0.00 (exact).
    - N_intersection_zero_cov: requires mean_D == 0.10, mean_A == 0.00, rho == 0.0 (exact).
    - N_intersection_pos_cov: requires mean_D == 0.10, mean_A == 0.00, rho > 0.0 (exact).
    - H1_target: requires mean_D > 0.10 and mean_A > 0.00.
    """
    if not isinstance(scenario, dict):
        raise ScenarioValidationError("Scenario must be a dictionary")

    scenario_id = scenario.get("scenario_id")
    if not scenario_id or not isinstance(scenario_id, str):
        raise ScenarioValidationError("Scenario must have a non-empty string 'scenario_id'")

    s_class = scenario.get("scenario_class")
    valid_classes = {"N_delta", "N_A", "N_intersection_zero_cov", "N_intersection_pos_cov", "H1_target"}
    if s_class not in valid_classes:
        raise ScenarioValidationError(f"Invalid scenario_class '{s_class}'. Must be one of {valid_classes}")

    targets = scenario.get("target_moments")
    if not isinstance(targets, dict):
        raise ScenarioValidationError("Scenario must contain 'target_moments' dictionary")

    for k in ["mean_D", "mean_A", "sd_D", "sd_A"]:
        if k not in targets:
            raise ScenarioValidationError(f"Missing required target moment '{k}'")

    mean_D_frac = fractions.Fraction(str(targets["mean_D"]))
    mean_A_frac = fractions.Fraction(str(targets["mean_A"]))

    raw_rho = targets.get("rho_D_A", targets.get("rho"))
    if raw_rho is None:
        raise ScenarioValidationError("Missing required correlation target 'rho_D_A' (or 'rho')")
    try:
        target_rho = float(raw_rho)
    except (ValueError, TypeError):
        raise ScenarioValidationError(f"Correlation target '{raw_rho}' must be numeric")

    if not math.isfinite(target_rho) or not (-1.0 <= target_rho <= 1.0):
        raise ScenarioValidationError(f"Correlation target '{target_rho}' must be finite and in [-1, 1]")

    if s_class == "N_delta":
        if mean_D_frac != fractions.Fraction(1, 10):
            raise ScenarioValidationError(f"Scenario class 'N_delta' invariant violated: target mean_D must be 0.10, got {targets['mean_D']}")
    elif s_class == "N_A":
        if mean_A_frac != fractions.Fraction(0, 1):
            raise ScenarioValidationError(f"Scenario class 'N_A' invariant violated: target mean_A must be 0.00, got {targets['mean_A']}")
    elif s_class == "N_intersection_zero_cov":
        if mean_D_frac != fractions.Fraction(1, 10) or mean_A_frac != fractions.Fraction(0, 1):
            raise ScenarioValidationError(f"Scenario class '{s_class}' invariant violated: target (mean_D, mean_A) must be (0.10, 0.00), got ({targets['mean_D']}, {targets['mean_A']})")
        if target_rho != 0.0:
            raise ScenarioValidationError(f"Scenario class 'N_intersection_zero_cov' requires rho==0.0, got {target_rho}")
    elif s_class == "N_intersection_pos_cov":
        if mean_D_frac != fractions.Fraction(1, 10) or mean_A_frac != fractions.Fraction(0, 1):
            raise ScenarioValidationError(f"Scenario class '{s_class}' invariant violated: target (mean_D, mean_A) must be (0.10, 0.00), got ({targets['mean_D']}, {targets['mean_A']})")
        if target_rho <= 0.0:
            raise ScenarioValidationError(f"Scenario class 'N_intersection_pos_cov' requires rho > 0.0, got {target_rho}")
    elif s_class == "H1_target":
        if float(mean_D_frac) <= 0.10 or float(mean_A_frac) <= 0.00:
            raise ScenarioValidationError(f"Scenario class 'H1_target' requires mean_D > 0.10 and mean_A > 0.00, got ({targets['mean_D']}, {targets['mean_A']})")

def compute_sample_moments(data: np.ndarray) -> Tuple[float, float, float]:
    """
    Compute sample mean, sample standard deviation (ddof=1), and sample excess kurtosis.
    SD uses ddof=1 divisor (N - 1).
    m2 and m4 use divisor N (population moments about sample mean):
      m_r = (1/N) * sum((x_i - mean)^r)
      excess_kurtosis = m_4 / (m_2^2) - 3 (NO bias correction).
      If and ONLY if m2 == 0.0, excess_kurtosis is 0.0.
    """
    n = len(data)
    if n < 2:
        return float(data[0]) if n == 1 else 0.0, 0.0, 0.0

    mean = float(np.mean(data))
    sd = float(np.std(data, ddof=1))

    diff = data - mean
    m2 = float(np.mean(diff ** 2))
    m4 = float(np.mean(diff ** 4))

    if m2 == 0.0:
        gamma_2 = 0.0
    else:
        gamma_2 = (m4 / (m2 ** 2)) - 3.0

    return mean, sd, gamma_2

def compute_sd_standard_error(sd: float, gamma_2: float, n: int) -> float:
    """
    Compute fourth-moment delta-method standard error for sample standard deviation.
    SE_sd = sd * sqrt(max(0, gamma_2 + 2) / (4 * n))
    """
    if n < 2 or sd <= 0:
        return 0.0
    var_factor = max(0.0, gamma_2 + 2.0)
    return sd * math.sqrt(var_factor / (4.0 * n))

def evaluate_moments_decision(
    mean_D: float,
    mean_A: float,
    sd_D: float,
    sd_A: float,
    gamma2_D: float,
    gamma2_A: float,
    rho: float,
    scenario: Dict[str, Any],
    n_val: int = 10000,
    z_crit: float = 3.719016,
    tol_mu_D: float = 0.010,
    tol_mu_A: float = 0.010,
    tol_sd_D: float = 0.015,
    tol_sd_A: float = 0.015,
    tol_rho: float = 0.050
) -> Dict[str, Any]:
    """
    Pure mathematical decision function for evaluating simultaneous 5-moment equivalence bounds.
    """
    validate_scenario_spec(scenario)
    targets = scenario["target_moments"]
    s_class = scenario.get("scenario_class", "unknown")

    target_mu_D = float(targets["mean_D"])
    target_mu_A = float(targets["mean_A"])
    target_sd_D = float(targets["sd_D"])
    target_sd_A = float(targets["sd_A"])
    target_rho = float(targets.get("rho_D_A", targets.get("rho", 0.0)))

    se_mean_D = sd_D / math.sqrt(n_val) if sd_D > 0 else 0.0
    se_mean_A = sd_A / math.sqrt(n_val) if sd_A > 0 else 0.0
    margin_mean_D = z_crit * se_mean_D
    margin_mean_A = z_crit * se_mean_A

    lcb_D = mean_D - margin_mean_D
    ucb_D = mean_D + margin_mean_D
    lcb_A = mean_A - margin_mean_A
    ucb_A = mean_A + margin_mean_A

    # 1. Gate 1: Check mu_D
    if s_class in ["N_delta", "N_intersection_zero_cov", "N_intersection_pos_cov"]:
        pass_mu_D = bool(lcb_D >= (target_mu_D - tol_mu_D) and ucb_D <= target_mu_D)
    else:
        pass_mu_D = bool(abs(mean_D - target_mu_D) + margin_mean_D <= tol_mu_D)

    # 2. Gate 2: Check mu_A
    if s_class in ["N_A", "N_intersection_zero_cov", "N_intersection_pos_cov"]:
        pass_mu_A = bool(lcb_A >= (target_mu_A - tol_mu_A) and ucb_A <= target_mu_A)
    else:
        pass_mu_A = bool(abs(mean_A - target_mu_A) + margin_mean_A <= tol_mu_A)

    # 3. Gate 3: Check sd_D (fourth-moment delta method)
    se_sd_D = compute_sd_standard_error(sd_D, gamma2_D, n_val)
    margin_sd_D = z_crit * se_sd_D
    if target_sd_D == 0.0:
        pass_sd_D = bool(sd_D == 0.0)
    else:
        pass_sd_D = bool(abs(sd_D - target_sd_D) + margin_sd_D <= tol_sd_D)

    # 4. Gate 4: Check sd_A (fourth-moment delta method)
    se_sd_A = compute_sd_standard_error(sd_A, gamma2_A, n_val)
    margin_sd_A = z_crit * se_sd_A
    if target_sd_A == 0.0:
        pass_sd_A = bool(sd_A == 0.0)
    else:
        pass_sd_A = bool(abs(sd_A - target_sd_A) + margin_sd_A <= tol_sd_A)

    # 5. Gate 5: Check rho_DA (nonlinear Fisher z transform)
    if sd_D == 0.0 or sd_A == 0.0:
        pass_rho = bool(target_rho == 0.0)
    else:
        z_obs = math.atanh(np.clip(rho, -0.999, 0.999))
        se_z = 1.0 / math.sqrt(n_val - 3)
        margin_z = z_crit * se_z

        z_low = math.atanh(np.clip(target_rho - tol_rho, -0.999, 0.999))
        z_high = math.atanh(np.clip(target_rho + tol_rho, -0.999, 0.999))

        pass_rho = bool((z_obs - margin_z >= z_low) and (z_obs + margin_z <= z_high))

    all_passed = bool(pass_mu_D and pass_mu_A and pass_sd_D and pass_sd_A and pass_rho)

    return {
        "validation_passed": all_passed,
        "pass_details": {
            "pass_mu_D": pass_mu_D,
            "pass_mu_A": pass_mu_A,
            "pass_sd_D": pass_sd_D,
            "pass_sd_A": pass_sd_A,
            "pass_rho": pass_rho
        },
        "moments": {
            "mean_D": mean_D,
            "mean_A": mean_A,
            "sd_D": sd_D,
            "sd_A": sd_A,
            "gamma2_D": gamma2_D,
            "gamma2_A": gamma2_A,
            "rho": rho
        },
        "bounds": {
            "ci_09998_mean_D": [lcb_D, ucb_D],
            "ci_09998_mean_A": [lcb_A, ucb_A],
            "se_sd_D": se_sd_D,
            "se_sd_A": se_sd_A
        }
    }

class DeterministicCalibratorV2:
    """
    SE-2 Chunked Deterministic Calibrator.
    Evaluates 4001 candidate kappa values represented as integer millisteps in [-2000, +2000].
    Routes all scoring through centralized quantizer.py and computes exact rational aggregations.
    Employs 2D chunking over both kappa millisteps and Monte Carlo iteration blocks
    to strictly guarantee simultaneously live NumPy data buffers <= 64,000,000 bytes.
    """
    K_MIN_MILLI = -2000
    K_MAX_MILLI = 2000
    K_STEP_MILLI = 1
    KAPPA_CHUNK_SIZE = 100
    ITER_CHUNK_SIZE = 500
    BUFFER_CEILING_BYTES = 64_000_000
    EPSILON_CAL = fractions.Fraction(25, 10000)  # 0.0025 exact rational

    def __init__(
        self,
        fixture: Dict[str, Any],
        scenario_id: str,
        plan_hash_hex: Optional[str] = None,
        execution_mode: str = "SCIENTIFIC"
    ):
        if execution_mode not in ["SCIENTIFIC", "NON_SCIENTIFIC_ENGINE_TEST"]:
            raise ValueError(f"Invalid execution_mode '{execution_mode}'. Must be 'SCIENTIFIC' or 'NON_SCIENTIFIC_ENGINE_TEST'")

        self.fixture = fixture
        self.scenario_id = scenario_id
        self.plan_hash_hex = plan_hash_hex or PLAN_HASH_HEX
        self.execution_mode = execution_mode
        self.millisteps = list(range(self.K_MIN_MILLI, self.K_MAX_MILLI + 1, self.K_STEP_MILLI))
        self.all_chunk_milli = np.arange(self.K_MIN_MILLI, self.K_MAX_MILLI + 1, self.K_STEP_MILLI, dtype=np.float64) * 0.001
        self.measured_peak_buffer_bytes = 0
        self.rss_diagnostic_bytes: Optional[int] = None
        self.rss_diagnostic_status = "NOT_MEASURED"

    def calibrate(
        self,
        scenario_spec: Dict[str, Any],
        n_cal: Optional[int] = None,
        record_dir: Optional[str] = None
    ) -> Tuple[float, float, Dict[str, Any]]:
        """
        Run deterministic 2-stage decoupled calibration over integer millisteps.
        Enforces scenario spec invariants, exact rational aggregations, and strict mode sample sizes.
        Returns (kappa_G_star, kappa_U_star, calibration_record).
        """
        validate_scenario_spec(scenario_spec)
        if scenario_spec["scenario_id"] != self.scenario_id:
            raise ScenarioValidationError(f"Scenario ID mismatch: {scenario_spec['scenario_id']} vs {self.scenario_id}")

        if self.execution_mode == "SCIENTIFIC":
            if n_cal is not None and n_cal != 10000:
                raise ScenarioValidationError(f"SCIENTIFIC mode requires n_cal=10,000, got {n_cal}")
            n_cal_eff = 10000
        else:
            n_cal_eff = n_cal if n_cal is not None else 1000

        targets = scenario_spec["target_moments"]
        target_mu_D_str = str(targets["mean_D"])
        target_mu_A_str = str(targets["mean_A"])
        target_mu_D_exact = fractions.Fraction(target_mu_D_str)
        target_mu_A_exact = fractions.Fraction(target_mu_A_str)

        model = SimulationModelV2(self.fixture, self.scenario_id, plan_hash_hex=self.plan_hash_hex)

        num_conf = self.fixture["num_conf_tasks"]
        num_rep = self.fixture["num_replicates"]
        conf_denoms = np.asarray(self.fixture["conf_denominators"], dtype=np.int64)
        validate_denominators_array(conf_denoms)

        # 1. Pre-generate N_cal latent trajectories across all k replicates
        fixed_bases = np.zeros((n_cal_eff, num_conf), dtype=np.float64)
        thetas_G = np.zeros((n_cal_eff, num_rep), dtype=np.float64)
        thetas_U = np.zeros((n_cal_eff, num_rep), dtype=np.float64)

        for i in range(n_cal_eff):
            model.generate_fixed_task_effects(mc_iteration=i, domain="calibration")
            fixed_bases[i, :] = model.fixed_task_effects[model.num_search_tasks:]
            _, _, lineage = model.simulate_block(mc_iteration=i, block=0, domain="calibration")
            for rep in range(num_rep):
                g_thetas_rep = [ev.theta for ev in lineage if ev.arm == "guided" and ev.replicate == rep and ev.event_type in ["baseline", "accept"]]
                u_thetas_rep = [ev.theta for ev in lineage if ev.arm == "unguided" and ev.replicate == rep and ev.event_type in ["baseline", "accept"]]
                thetas_G[i, rep] = g_thetas_rep[-1] if g_thetas_rep else 0.0
                thetas_U[i, rep] = u_thetas_rep[-1] if u_thetas_rep else 0.0

        # Baseline evaluation routed through in-place quantizer
        base_val_buf = fixed_bases.copy()
        base_earned = np.zeros((n_cal_eff, num_conf), dtype=np.int64)
        quantize_earned_points_array_inplace(base_val_buf, conf_denoms, base_earned)

        base_earned_per_task = np.sum(base_earned, axis=0)
        base_task_fracs = [fractions.Fraction(int(base_earned_per_task[t]) * num_rep, int(conf_denoms[t])) for t in range(num_conf)]
        total_base_frac_sum = sum(base_task_fracs)
        mean_base_score_frac = total_base_frac_sum / fractions.Fraction(n_cal_eff * num_rep * num_conf)

        # Preallocated material data buffers for 2D chunking (zero-temporary loop)
        val_chunk_buf = np.zeros((self.KAPPA_CHUNK_SIZE, self.ITER_CHUNK_SIZE, num_rep, num_conf), dtype=np.float64)
        thetas_scaled_buf = np.zeros((self.KAPPA_CHUNK_SIZE, self.ITER_CHUNK_SIZE, num_rep, 1), dtype=np.float64)
        earned_chunk_buf = np.zeros((self.KAPPA_CHUNK_SIZE, self.ITER_CHUNK_SIZE, num_rep, num_conf), dtype=np.int64)
        sum_rep_buf = np.zeros((self.KAPPA_CHUNK_SIZE, self.ITER_CHUNK_SIZE, num_conf), dtype=np.int64)
        chunk_reduction_buf = np.zeros((self.KAPPA_CHUNK_SIZE, num_conf), dtype=np.int64)
        chunk_task_earned_buf = np.zeros((self.KAPPA_CHUNK_SIZE, num_conf), dtype=np.int64)

        conf_denoms_4d = conf_denoms[np.newaxis, np.newaxis, np.newaxis, :]

        total_live_pre = (
            fixed_bases.nbytes + thetas_G.nbytes + thetas_U.nbytes + conf_denoms.nbytes +
            base_val_buf.nbytes + base_earned.nbytes + base_earned_per_task.nbytes +
            self.all_chunk_milli.nbytes + val_chunk_buf.nbytes + thetas_scaled_buf.nbytes +
            earned_chunk_buf.nbytes + sum_rep_buf.nbytes + chunk_reduction_buf.nbytes +
            chunk_task_earned_buf.nbytes + conf_denoms_4d.nbytes
        )
        self.measured_peak_buffer_bytes = max(self.measured_peak_buffer_bytes, total_live_pre)
        if self.measured_peak_buffer_bytes > self.BUFFER_CEILING_BYTES:
            raise RuntimeError(f"Buffer ceiling exceeded: {self.measured_peak_buffer_bytes} > {self.BUFFER_CEILING_BYTES}")

        def evaluate_arm_grid_exact(thetas_all_rep: np.ndarray) -> List[fractions.Fraction]:
            """
            Evaluate mean confirmatory score as exact Fractions over all replicates and tasks
            across candidate millisteps using in-place quantizer and 2D chunking.
            """
            n_milli = len(self.millisteps)
            mean_scores_frac: List[fractions.Fraction] = [fractions.Fraction(0)] * n_milli

            for m_start in range(0, n_milli, self.KAPPA_CHUNK_SIZE):
                m_end = min(n_milli, m_start + self.KAPPA_CHUNK_SIZE)
                chunk_milli = self.all_chunk_milli[m_start:m_end]
                n_kc = len(chunk_milli)

                chunk_task_earned_buf[:n_kc, :].fill(0)

                for iter_start in range(0, n_cal_eff, self.ITER_CHUNK_SIZE):
                    iter_end = min(n_cal_eff, iter_start + self.ITER_CHUNK_SIZE)
                    n_ic = iter_end - iter_start

                    sub_bases = fixed_bases[iter_start:iter_end]
                    sub_thetas = thetas_all_rep[iter_start:iter_end]

                    # Pre-allocated sub-array slices
                    sub_val = val_chunk_buf[:n_kc, :n_ic, :, :]
                    sub_scaled = thetas_scaled_buf[:n_kc, :n_ic, :, :]
                    sub_earned = earned_chunk_buf[:n_kc, :n_ic, :, :]

                    # Compute broadcast value in-place without heap temporaries
                    np.multiply(
                        chunk_milli[:n_kc, np.newaxis, np.newaxis, np.newaxis],
                        sub_thetas[np.newaxis, :n_ic, :, np.newaxis],
                        out=sub_scaled
                    )
                    np.add(
                        sub_bases[np.newaxis, :n_ic, np.newaxis, :],
                        sub_scaled,
                        out=sub_val
                    )

                    # In-place quantization
                    quantize_earned_points_array_inplace(sub_val, conf_denoms_4d, sub_earned)

                    # In-place reduction into preallocated buffers
                    np.sum(sub_earned, axis=2, out=sum_rep_buf[:n_kc, :n_ic, :])
                    np.sum(sum_rep_buf[:n_kc, :n_ic, :], axis=1, out=chunk_reduction_buf[:n_kc, :])
                    np.add(chunk_task_earned_buf[:n_kc, :], chunk_reduction_buf[:n_kc, :], out=chunk_task_earned_buf[:n_kc, :])

                # Exact rational score per candidate kappa in chunk
                for k_idx in range(n_kc):
                    global_k_idx = m_start + k_idx
                    task_fracs = [fractions.Fraction(int(chunk_task_earned_buf[k_idx, t]), int(conf_denoms[t])) for t in range(num_conf)]
                    total_frac = sum(task_fracs)
                    mean_scores_frac[global_k_idx] = total_frac / fractions.Fraction(n_cal_eff * num_rep * num_conf)

            return mean_scores_frac

        # Stage 1: Calibrate kappa_G on target_mu_A using exact Fractions
        mean_G_fracs = evaluate_arm_grid_exact(thetas_G)
        mu_A_fracs = [g_frac - mean_base_score_frac for g_frac in mean_G_fracs]

        diffs_A = [abs(mu_A - target_mu_A_exact) for mu_A in mu_A_fracs]
        min_diff_A = min(diffs_A)

        best_indices_A = [i for i, diff in enumerate(diffs_A) if diff == min_diff_A]
        best_milli_A = [self.millisteps[i] for i in best_indices_A]
        best_milli_A = sorted(best_milli_A, key=lambda m: (abs(m), m))
        milli_G_star = best_milli_A[0]
        idx_G_star = self.millisteps.index(milli_G_star)
        kappa_G_star = milli_G_star / 1000.0
        realized_mu_A_frac = mu_A_fracs[idx_G_star]

        # Stage 2: Calibrate kappa_U on target_mu_D using exact Fractions
        mean_U_fracs = evaluate_arm_grid_exact(thetas_U)
        mu_AU_fracs = [u_frac - mean_base_score_frac for u_frac in mean_U_fracs]
        mu_D_fracs = [realized_mu_A_frac - mu_AU for mu_AU in mu_AU_fracs]

        diffs_D = [abs(mu_D - target_mu_D_exact) for mu_D in mu_D_fracs]
        min_diff_D = min(diffs_D)

        best_indices_D = [i for i, diff in enumerate(diffs_D) if diff == min_diff_D]
        best_milli_D = [self.millisteps[i] for i in best_indices_D]
        best_milli_D = sorted(best_milli_D, key=lambda m: (abs(m), m))
        milli_U_star = best_milli_D[0]
        idx_U_star = self.millisteps.index(milli_U_star)
        kappa_U_star = milli_U_star / 1000.0
        realized_mu_D_frac = mu_D_fracs[idx_U_star]

        err_A = abs(realized_mu_A_frac - target_mu_A_exact)
        err_D = abs(realized_mu_D_frac - target_mu_D_exact)

        # Fresh-process honest RSS diagnostic (never fabricate observations)
        try:
            rusage = resource.getrusage(resource.RUSAGE_SELF)
            raw_rss = int(rusage.ru_maxrss)
            if raw_rss > 0:
                self.rss_diagnostic_bytes = raw_rss if sys.platform == "darwin" else raw_rss * 1024
                self.rss_diagnostic_status = "AVAILABLE"
            else:
                self.rss_diagnostic_bytes = None
                self.rss_diagnostic_status = "UNAVAILABLE_ZERO_RAW_RSS"
        except Exception as e:
            self.rss_diagnostic_bytes = None
            self.rss_diagnostic_status = f"ERROR_{type(e).__name__}"

        fixture_canonical_bytes = json.dumps(self.fixture, sort_keys=True).encode('utf-8')
        fixture_hash = hashlib.sha256(fixture_canonical_bytes).hexdigest()

        scenario_canonical_bytes = json.dumps(scenario_spec, sort_keys=True).encode('utf-8')
        scenario_hash = hashlib.sha256(scenario_canonical_bytes).hexdigest()

        target_canonical_bytes = json.dumps({
            "target_mu_D_frac": str(target_mu_D_exact),
            "target_mu_A_frac": str(target_mu_A_exact)
        }, sort_keys=True).encode('utf-8')
        target_hash = hashlib.sha256(target_canonical_bytes).hexdigest()

        rfc_sha = get_rfc_sha256()

        record = {
            "record_type": "SE2_CALIBRATION_RECORD",
            "execution_mode": self.execution_mode,
            "scenario_id": self.scenario_id,
            "scenario_class": scenario_spec["scenario_class"],
            "controlling_plan_hash": self.plan_hash_hex,
            "rfc_hash": rfc_sha,
            "scenario_hash": scenario_hash,
            "scenario_spec": scenario_spec,
            "fixture_hash": fixture_hash,
            "target_hashes": {
                "targets_sha256": target_hash,
                "target_mu_D_frac": str(target_mu_D_exact),
                "target_mu_A_frac": str(target_mu_A_exact),
                "target_mu_D_str": target_mu_D_str,
                "target_mu_A_str": target_mu_A_str
            },
            "prng_algorithm": PRNG_ALGORITHM,
            "prng_version": f"numpy={np.__version__}",
            "quantizer_contract": QUANTIZER_CONTRACT,
            "quantizer_version": QUANTIZER_VERSION,
            "n_cal": n_cal_eff,
            "lattice_parameters": {
                "k_min_milli": self.K_MIN_MILLI,
                "k_max_milli": self.K_MAX_MILLI,
                "k_step_milli": self.K_STEP_MILLI,
                "total_points": len(self.millisteps)
            },
            "chunk_parameters": {
                "kappa_chunk_size": self.KAPPA_CHUNK_SIZE,
                "iter_chunk_size": self.ITER_CHUNK_SIZE,
                "measured_peak_buffer_bytes": self.measured_peak_buffer_bytes,
                "rss_diagnostic": {
                    "status": self.rss_diagnostic_status,
                    "rss_bytes": self.rss_diagnostic_bytes
                }
            },
            "replicate_count": num_rep,
            "task_counts": {
                "num_search_tasks": self.fixture["num_search_tasks"],
                "num_conf_tasks": num_conf
            },
            "denominators": {
                "search_denominators": self.fixture["search_denominators"],
                "conf_denominators": self.fixture["conf_denominators"]
            },
            "calibrated_parameters": {
                "kappa_G_star": kappa_G_star,
                "kappa_U_star": kappa_U_star,
                "milli_G_star": milli_G_star,
                "milli_U_star": milli_U_star
            },
            "realized_calibration_moments": {
                "target_mu_D_str": target_mu_D_str,
                "target_mu_A_str": target_mu_A_str,
                "realized_mu_D_frac": str(realized_mu_D_frac),
                "realized_mu_A_frac": str(realized_mu_A_frac),
                "realized_mu_D_float": float(realized_mu_D_frac),
                "realized_mu_A_float": float(realized_mu_A_frac),
                "error_D_frac": str(err_D),
                "error_A_frac": str(err_A)
            }
        }

        # Cryptographically bind complete canonical record payload against tampering
        record["record_digest"] = compute_calibration_record_digest(record)

        if err_A > self.EPSILON_CAL or err_D > self.EPSILON_CAL:
            if record_dir is not None:
                os.makedirs(record_dir, exist_ok=True)
                fail_rec_path = os.path.join(record_dir, "se2_calibration_failure_record.json")
                with open(fail_rec_path, "w", encoding="utf-8") as f:
                    json.dump(record, f, indent=2)
            raise ScenarioCalibrationError(
                f"Scenario '{self.scenario_id}' calibration error exceeded tolerance {float(self.EPSILON_CAL)}: "
                f"err_A={float(err_A):.5f} (target {target_mu_A_str}, realized {float(realized_mu_A_frac):.5f}), "
                f"err_D={float(err_D):.5f} (target {target_mu_D_str}, realized {float(realized_mu_D_frac):.5f})"
            )

        if record_dir is not None:
            os.makedirs(record_dir, exist_ok=True)
            record_path = os.path.join(record_dir, "se2_calibration_record.json")
            with open(record_path, "w", encoding="utf-8") as f:
                json.dump(record, f, indent=2)

        return kappa_G_star, kappa_U_star, record

class IndependentValidatorV2:
    """
    SE-2 Independent Validator.
    Consumes and validates se2_calibration_record.json before running Monte Carlo draws.
    Enforces mandatory cryptographic record digest integrity against trusted 64-char hex anchor,
    simultaneous 5-moment confidence bounds, and near-boundary null bands.
    Rejects any innovation_adapter in SCIENTIFIC mode fail-closed.
    """
    Z_CRIT = 3.719016  # Simultaneous Bonferroni critical value for alpha_comp = 0.0002
    TOL_MU_D = 0.010
    TOL_MU_A = 0.010
    TOL_SD_D = 0.015
    TOL_SD_A = 0.015
    TOL_RHO = 0.050

    def __init__(
        self,
        fixture: Dict[str, Any],
        scenario: Dict[str, Any],
        calibration_record: Dict[str, Any],
        expected_record_digest: str,
        plan_hash_hex: Optional[str] = None,
        execution_mode: str = "SCIENTIFIC",
        innovation_adapter: Optional[Any] = None
    ):
        if not isinstance(expected_record_digest, str) or not HEX_64_PATTERN.match(expected_record_digest):
            raise ScenarioValidationError(
                f"expected_record_digest is mandatory and must be exactly 64 lowercase hexadecimal characters, got {expected_record_digest!r}"
            )

        if execution_mode not in ["SCIENTIFIC", "NON_SCIENTIFIC_ENGINE_TEST"]:
            raise ValueError(f"Invalid execution_mode '{execution_mode}'. Must be 'SCIENTIFIC' or 'NON_SCIENTIFIC_ENGINE_TEST'")

        if execution_mode == "SCIENTIFIC" and innovation_adapter is not None:
            raise ScenarioValidationError(
                "innovation_adapter is strictly forbidden in SCIENTIFIC execution mode; "
                "test manipulation seams are permitted only in NON_SCIENTIFIC_ENGINE_TEST mode."
            )

        self.fixture = fixture
        self.scenario = scenario
        self.scenario_id = scenario["scenario_id"]
        self.scenario_class = scenario.get("scenario_class", "unknown")
        self.calibration_record = calibration_record
        self.expected_record_digest = expected_record_digest
        self.plan_hash_hex = plan_hash_hex or PLAN_HASH_HEX
        self.execution_mode = execution_mode
        self.innovation_adapter = innovation_adapter

        # Enforce scenario spec boundary invariants
        validate_scenario_spec(self.scenario)

        # Strict validation of bound calibration record BEFORE any draws
        self._verify_calibration_record()

    def _verify_calibration_record(self) -> None:
        """Validate every bound field and cryptographic record digest before any execution."""
        rec = self.calibration_record
        if rec.get("record_type") != "SE2_CALIBRATION_RECORD":
            raise ScenarioValidationError("Invalid calibration record type")

        rec_mode = rec.get("execution_mode")
        if rec_mode not in ["SCIENTIFIC", "NON_SCIENTIFIC_ENGINE_TEST"]:
            raise ScenarioValidationError(f"Invalid execution_mode '{rec_mode}' in calibration record")

        if self.execution_mode != rec_mode:
            raise ScenarioValidationError(
                f"Execution mode mismatch: validator mode '{self.execution_mode}' != record mode '{rec_mode}'"
            )

        if rec.get("scenario_id") != self.scenario_id:
            raise ScenarioValidationError(f"Scenario ID mismatch: expected {self.scenario_id}, got {rec.get('scenario_id')}")
        if rec.get("scenario_class") != self.scenario_class:
            raise ScenarioValidationError(f"Scenario class mismatch: expected {self.scenario_class}, got {rec.get('scenario_class')}")
        if rec.get("controlling_plan_hash") != self.plan_hash_hex:
            raise ScenarioValidationError("Plan hash mismatch in calibration record")

        # RFC hash validation (fail-closed)
        expected_rfc_sha = get_rfc_sha256()
        if rec.get("rfc_hash") != expected_rfc_sha:
            raise ScenarioValidationError(f"RFC hash mismatch: expected {expected_rfc_sha}, got {rec.get('rfc_hash')}")

        if rec.get("prng_algorithm") != PRNG_ALGORITHM:
            raise ScenarioValidationError("PRNG algorithm mismatch in calibration record")
        if rec.get("prng_version") != f"numpy={np.__version__}":
            raise ScenarioValidationError("PRNG version mismatch in calibration record")
        if rec.get("quantizer_contract") != QUANTIZER_CONTRACT:
            raise ScenarioValidationError("Quantizer contract mismatch in calibration record")
        if rec.get("quantizer_version") != QUANTIZER_VERSION:
            raise ScenarioValidationError("Quantizer version mismatch in calibration record")

        if self.execution_mode == "SCIENTIFIC":
            if rec.get("n_cal") != 10000:
                raise ScenarioValidationError(f"SCIENTIFIC mode requires n_cal=10,000, got {rec.get('n_cal')}")

        # Lattice parameters check
        lat = rec.get("lattice_parameters", {})
        if lat.get("k_min_milli") != -2000 or lat.get("k_max_milli") != 2000 or lat.get("k_step_milli") != 1 or lat.get("total_points") != 4001:
            raise ScenarioValidationError("Lattice parameters mismatch in calibration record")

        # Chunk parameters check
        chunk_p = rec.get("chunk_parameters", {})
        peak_bytes = chunk_p.get("measured_peak_buffer_bytes")
        if peak_bytes is None or not isinstance(peak_bytes, int) or peak_bytes <= 0 or peak_bytes > 64_000_000:
            raise ScenarioValidationError(f"Invalid measured_peak_buffer_bytes: {peak_bytes}")

        # Honest RSS diagnostic validation
        rss_diag = chunk_p.get("rss_diagnostic")
        if not isinstance(rss_diag, dict):
            raise ScenarioValidationError("Missing 'rss_diagnostic' object in calibration record chunk_parameters")

        rss_status = rss_diag.get("status")
        rss_bytes = rss_diag.get("rss_bytes")
        if rss_status == "AVAILABLE":
            if not isinstance(rss_bytes, int) or rss_bytes <= 0:
                raise ScenarioValidationError(f"rss_bytes must be a positive integer when status is AVAILABLE, got {rss_bytes}")
        elif isinstance(rss_status, str) and (rss_status.startswith("UNAVAILABLE") or rss_status.startswith("ERROR")):
            if rss_bytes is not None:
                raise ScenarioValidationError(f"rss_bytes must be None when status is {rss_status}, got {rss_bytes}")
        else:
            raise ScenarioValidationError(f"Invalid rss_diagnostic status: {rss_status}")

        if chunk_p.get("kappa_chunk_size") != 100:
            raise ScenarioValidationError(f"Invalid kappa_chunk_size in record: {chunk_p.get('kappa_chunk_size')}")
        if chunk_p.get("iter_chunk_size") != 500:
            raise ScenarioValidationError(f"Invalid iter_chunk_size in record: {chunk_p.get('iter_chunk_size')}")

        # Fixture hash validation
        expected_fixture_hash = hashlib.sha256(json.dumps(self.fixture, sort_keys=True).encode('utf-8')).hexdigest()
        if rec.get("fixture_hash") != expected_fixture_hash:
            raise ScenarioValidationError("Fixture hash mismatch in calibration record")

        # Complete scenario spec & spec hash validation
        expected_scenario_hash = hashlib.sha256(json.dumps(self.scenario, sort_keys=True).encode('utf-8')).hexdigest()
        if rec.get("scenario_hash") != expected_scenario_hash:
            raise ScenarioValidationError("Scenario spec hash mismatch in calibration record")
        if rec.get("scenario_spec") != self.scenario:
            raise ScenarioValidationError("Embedded scenario_spec does not match current scenario")

        # Target moment validation
        targets = self.scenario.get("target_moments", {})
        scenario_mu_D_exact = fractions.Fraction(str(targets.get("mean_D")))
        scenario_mu_A_exact = fractions.Fraction(str(targets.get("mean_A")))

        expected_target_hash = hashlib.sha256(json.dumps({
            "target_mu_D_frac": str(scenario_mu_D_exact),
            "target_mu_A_frac": str(scenario_mu_A_exact)
        }, sort_keys=True).encode('utf-8')).hexdigest()

        rec_target_hashes = rec.get("target_hashes", {})
        if rec_target_hashes.get("targets_sha256") != expected_target_hash:
            raise ScenarioValidationError(
                f"Target hash mismatch: scenario targets (D={scenario_mu_D_exact}, A={scenario_mu_A_exact}) "
                f"do not match calibration targets ({rec_target_hashes})"
            )
        if rec_target_hashes.get("target_mu_D_frac") != str(scenario_mu_D_exact) or rec_target_hashes.get("target_mu_A_frac") != str(scenario_mu_A_exact):
            raise ScenarioValidationError("Stored target fraction string mismatch in calibration record")
        if rec_target_hashes.get("target_mu_D_str") != str(targets.get("mean_D")) or rec_target_hashes.get("target_mu_A_str") != str(targets.get("mean_A")):
            raise ScenarioValidationError("Stored target string mismatch in calibration record")

        # Parameter presence & millistep consistency validation
        cal_params = rec.get("calibrated_parameters", {})
        if "kappa_G_star" not in cal_params or "kappa_U_star" not in cal_params:
            raise ScenarioValidationError("Missing calibrated kappa parameters in calibration record")
        if "milli_G_star" not in cal_params or "milli_U_star" not in cal_params:
            raise ScenarioValidationError("Missing calibrated millistep parameters in calibration record")

        k_G = float(cal_params["kappa_G_star"])
        k_U = float(cal_params["kappa_U_star"])
        m_G = int(cal_params["milli_G_star"])
        m_U = int(cal_params["milli_U_star"])
        if int(round(k_G * 1000)) != m_G or int(round(k_U * 1000)) != m_U:
            raise ScenarioValidationError("Inconsistent kappa and millistep values in calibration record")

        if rec.get("replicate_count") != self.fixture["num_replicates"]:
            raise ScenarioValidationError("Replicate count mismatch between fixture and calibration record")

        task_c = rec.get("task_counts", {})
        if task_c.get("num_search_tasks") != self.fixture["num_search_tasks"]:
            raise ScenarioValidationError("Search task count mismatch in calibration record")
        if task_c.get("num_conf_tasks") != self.fixture["num_conf_tasks"]:
            raise ScenarioValidationError("Confirmatory task count mismatch in calibration record")

        denoms = rec.get("denominators", {})
        if denoms.get("search_denominators") != self.fixture["search_denominators"]:
            raise ScenarioValidationError("Search denominators mismatch in calibration record")
        if denoms.get("conf_denominators") != self.fixture["conf_denominators"]:
            raise ScenarioValidationError("Confirmatory denominators mismatch in calibration record")

        # Cryptographic Record Digest Integrity Check
        if "record_digest" not in rec or not isinstance(rec["record_digest"], str) or not HEX_64_PATTERN.match(rec["record_digest"]):
            raise ScenarioValidationError("Missing or invalid 'record_digest' in calibration record (must be 64 lowercase hex chars)")

        if rec["record_digest"] != self.expected_record_digest:
            raise ScenarioValidationError(
                f"Trusted calibration record digest mismatch: expected trusted digest {self.expected_record_digest}, "
                f"got record digest {rec['record_digest']}"
            )

        computed_digest = compute_calibration_record_digest(rec)
        if rec["record_digest"] != computed_digest:
            raise ScenarioValidationError(
                f"Calibration record cryptographic self-digest mismatch: record has been tampered with or modified "
                f"(expected {computed_digest}, got {rec['record_digest']})"
            )

    def validate(self, n_val: Optional[int] = None, failure_record_dir: Optional[str] = None) -> Dict[str, Any]:
        """
        Run independent validation on disjoint seeds and evaluate all 5 moments.
        In SCIENTIFIC mode, requires exactly n_val=10,000 and forbids innovation_adapter.
        Raises ScenarioValidationError on any failure and writes se2_validation_failure_record.json.
        """
        if self.execution_mode == "SCIENTIFIC":
            if n_val is not None and n_val != 10000:
                raise ScenarioValidationError(f"SCIENTIFIC mode requires n_val=10,000, got {n_val}")
            n_val_eff = 10000
        else:
            n_val_eff = n_val if n_val is not None else 1000

        cal_params = self.calibration_record["calibrated_parameters"]
        fixture_with_kappas = dict(self.fixture)
        fixture_with_kappas["kappa_G"] = float(cal_params["kappa_G_star"])
        fixture_with_kappas["kappa_U"] = float(cal_params["kappa_U_star"])

        model = SimulationModelV2(
            fixture_with_kappas,
            self.scenario_id,
            plan_hash_hex=self.plan_hash_hex,
            innovation_adapter=self.innovation_adapter
        )

        D_vals = np.zeros(n_val_eff, dtype=np.float64)
        A_vals = np.zeros(n_val_eff, dtype=np.float64)

        for i in range(n_val_eff):
            model.generate_fixed_task_effects(mc_iteration=i, domain="conformance_validation")
            d, a, _ = model.simulate_block(mc_iteration=i, block=0, domain="conformance_validation")
            D_vals[i] = d
            A_vals[i] = a

        mean_D, sd_D, gamma2_D = compute_sample_moments(D_vals)
        mean_A, sd_A, gamma2_A = compute_sample_moments(A_vals)

        if sd_D > 0 and sd_A > 0:
            rho = float(np.corrcoef(D_vals, A_vals)[0, 1])
        else:
            rho = 0.0

        decision = evaluate_moments_decision(
            mean_D=mean_D,
            mean_A=mean_A,
            sd_D=sd_D,
            sd_A=sd_A,
            gamma2_D=gamma2_D,
            gamma2_A=gamma2_A,
            rho=rho,
            scenario=self.scenario,
            n_val=n_val_eff,
            z_crit=self.Z_CRIT,
            tol_mu_D=self.TOL_MU_D,
            tol_mu_A=self.TOL_MU_A,
            tol_sd_D=self.TOL_SD_D,
            tol_sd_A=self.TOL_SD_A,
            tol_rho=self.TOL_RHO
        )

        result = {
            "record_type": "SE2_VALIDATION_RECORD",
            "execution_mode": self.execution_mode,
            "scenario_id": self.scenario_id,
            "scenario_class": self.scenario_class,
            "validation_passed": decision["validation_passed"],
            "n_val": n_val_eff,
            "moments": decision["moments"],
            "bounds": decision["bounds"],
            "pass_details": decision["pass_details"]
        }

        if not decision["validation_passed"]:
            if failure_record_dir is not None:
                os.makedirs(failure_record_dir, exist_ok=True)
                fail_path = os.path.join(failure_record_dir, "se2_validation_failure_record.json")
                with open(fail_path, "w", encoding="utf-8") as f:
                    json.dump(result, f, indent=2)
            raise ScenarioValidationError(
                f"Scenario '{self.scenario_id}' failed independent validation gate: {result['pass_details']}"
            )

        return result
