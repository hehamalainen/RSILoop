import json
import math
import hashlib
import os
import sys
from typing import Dict, Any, List, Tuple
import numpy as np

from .model import SimulationModel
from .prng import DomainSeparatedPRNG

class ScenarioConformanceError(Exception):
    """Raised when a scientific scenario fails simultaneous moment equivalence intervals."""
    pass

# Frozen Student-t 0.95 quantile critical values for even B
FROZEN_T_CRITICAL_095 = {
    2: 6.3137515147,  # df=1
    4: 2.3533634348,  # df=3
    6: 2.0150483727,  # df=5
    8: 1.8945786051,  # df=7
    10: 1.8331129327, # df=9
    12: 1.7958848187, # df=11
    14: 1.7709333960, # df=13
    16: 1.7530503557, # df=15
    18: 1.7396067261, # df=17
    20: 1.7291328115, # df=19
    22: 1.7207429028, # df=21
    24: 1.7138715277, # df=23
    26: 1.7081407613, # df=25
    28: 1.7032884457, # df=27
    30: 1.6991270265, # df=29
    32: 1.6955187825, # df=31
    34: 1.6923603090, # df=33
    36: 1.6895724578, # df=35
    38: 1.6870936196, # df=37
    40: 1.6848751217, # df=39
}

def beta_cdf_stable(x: float, a: float, b: float) -> float:
    """Stably evaluate the regularized incomplete beta integral I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    mode = (a - 1.0) / (a + b - 2.0) if (a > 1.0 and b > 1.0) else (a / (a + b))
    std = math.sqrt(max(1e-12, (a * b) / (((a + b)**2) * (a + b + 1.0))))

    lower = max(1e-12, mode - 10.0 * std)
    if x <= lower:
        return 0.0

    nodes, weights = np.polynomial.legendre.leggauss(120)
    pts = 0.5 * (x - lower) * (nodes + 1.0) + lower
    wts = 0.5 * (x - lower) * weights

    ln_B = math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b)
    log_vals = (a - 1.0) * np.log(pts) + (b - 1.0) * np.log(1.0 - pts) - ln_B
    integrand = np.exp(log_vals)
    integral = float(np.sum(wts * integrand))
    return min(1.0, max(0.0, integral))

def beta_ppf_stable(p: float, a: float, b: float) -> float:
    """Invert regularized incomplete beta integral to find quantile p."""
    if p <= 0.0:
        return 0.0
    if p >= 1.0:
        return 1.0
    mode = (a - 1.0) / (a + b - 2.0) if (a > 1.0 and b > 1.0) else (a / (a + b))
    std = math.sqrt(max(1e-12, (a * b) / (((a + b)**2) * (a + b + 1.0))))
    low = max(0.0, mode - 10.0 * std)
    high = min(1.0, mode + 10.0 * std)
    for _ in range(50):
        mid = (low + high) / 2.0
        val = beta_cdf_stable(mid, a, b)
        if val < p:
            low = mid
        else:
            high = mid
    return (low + high) / 2.0

def compute_clopper_pearson_bounds(rejections: int, total_trials: int, alpha: float) -> Tuple[float, float]:
    """Compute exact Clopper-Pearson binomial lower and upper bounds at significance level alpha."""
    if total_trials <= 0:
        return 0.0, 1.0

    if rejections == 0:
        lcb = 0.0
    else:
        lcb = beta_ppf_stable(alpha / 2.0, float(rejections), float(total_trials - rejections + 1))

    if rejections == total_trials:
        ucb = 1.0
    else:
        ucb = beta_ppf_stable(1.0 - alpha / 2.0, float(rejections + 1), float(total_trials - rejections))

    return lcb, ucb

def run_scenario_conformance(scenario: Dict[str, Any], system_params: Dict[str, Any], iterations: int = 10000) -> Dict[str, Any]:
    """Verify that a scenario realizes its target moments (HARD DISABLED FOR SE-1)."""
    raise RuntimeError(
        "SE-1 simulation model is permanently retired as MODEL_INADEQUATE (invariant mu_Delta <= mu_A). "
        "Scenario conformance execution is disabled until SE-2 Model RFC is implemented."
    )

def _simulate_chunk(args: Tuple[Dict[str, Any], str, int, int, int]) -> Tuple[np.ndarray, np.ndarray]:
    """Top-level worker function for parallel chunk simulation (HARD DISABLED FOR SE-1)."""
    raise RuntimeError(
        "SE-1 simulation model is permanently retired as MODEL_INADEQUATE (invariant mu_Delta <= mu_A)."
    )

def simulate_real_se1_grid(
    scenario: Dict[str, Any],
    system_params: Dict[str, Any],
    k: int,
    B_domain: List[int],
    iterations: int,
    alpha_cell: float
) -> Dict[str, Any]:
    """Execute direct SE-1 SimulationModel iterations (HARD DISABLED FOR SE-1)."""
    raise RuntimeError(
        "SE-1 simulation model is permanently retired as MODEL_INADEQUATE (invariant mu_Delta <= mu_A). "
        "Scientific grid execution is disabled until SE-2 Model RFC is implemented."
    )

def generate_p2_tradeoff_report(
    spec_path: str,
    output_path: str,
    mc_iterations: int = 10000,
    k_domain_override: List[int] = None,
    scenarios_override: List[str] = None
) -> Dict[str, Any]:
    """Execute complete direct SE-1 P2 evaluation grid and compile audit report (HARD DISABLED)."""
    raise RuntimeError(
        "SE-1 simulation model is permanently retired as MODEL_INADEQUATE (invariant mu_Delta <= mu_A). "
        "Scientific grid execution is disabled until the SE-2 Model RFC is formally reviewed and accepted."
    )
