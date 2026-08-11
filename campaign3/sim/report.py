import json
import numpy as np
import hashlib
import os
from typing import Dict, Any
from .model import SimulationModel
from .prng import DomainSeparatedPRNG

def compute_spec_hash(fixture_bytes: bytes) -> str:
    # Documented canonical manifest recipe:
    # SHA256 of concatenated contents of model.py, prng.py, rational.py, report.py, and fixture_bytes
    base_dir = os.path.dirname(__file__)
    files = ["model.py", "prng.py", "rational.py", "report.py"]
    
    h = hashlib.sha256()
    for fname in files:
        fpath = os.path.join(base_dir, fname)
        with open(fpath, "rb") as f:
            h.update(f.read())
            
    h.update(fixture_bytes)
    return h.hexdigest()

def run_fixture(fixture_name: str, fixture: Dict[str, Any], iterations: int = 1000) -> Dict[str, Any]:
    model = SimulationModel(fixture, fixture_name)
    
    D_vals = []
    A_vals = []
    
    model.generate_fixed_task_effects(mc_iteration=0)
    
    for i in range(iterations):
        D, A, _ = model.simulate_block(mc_iteration=i, block=0)
        D_vals.append(D)
        A_vals.append(A)
        
    D_arr = np.array(D_vals)
    A_arr = np.array(A_vals)
    
    mean_D = float(np.mean(D_arr))
    mean_A = float(np.mean(A_arr))
    sd_D = float(np.std(D_arr, ddof=1)) if iterations > 1 else 0.0
    sd_A = float(np.std(A_arr, ddof=1)) if iterations > 1 else 0.0
    se_D = float(sd_D / np.sqrt(iterations)) if iterations > 0 else 0.0
    se_A = float(sd_A / np.sqrt(iterations)) if iterations > 0 else 0.0
    
    fixture_bytes = json.dumps(fixture, sort_keys=True).encode('utf-8')
    fixture_hash = hashlib.sha256(fixture_bytes).hexdigest()
    spec_hash = compute_spec_hash(fixture_bytes)
    
    out = {
        "mean_D": mean_D,
        "mean_A": mean_A,
        "sd_D": sd_D,
        "sd_A": sd_A,
        "se_D": se_D,
        "se_A": se_A,
        "fixed_task_effects_hash": model.fixed_task_effects_hash,
        "fixture_hash": fixture_hash,
        "spec_hash": spec_hash,
        "iteration_count": iterations,
        "prng_identity": DomainSeparatedPRNG.ALGORITHM,
        "prng_version": f"numpy={np.__version__}"
    }
    
    if fixture.get("is_selection_null", False):
        out["selection_bias_offset"] = mean_A
        
    # Check PASS/FAIL vs independent targets
    tol = fixture["moments_tolerance"]
    passed_A = abs(mean_A - tol["mean_A_target"]) <= tol["mean_A_tol"]
    passed_D = abs(mean_D - tol["mean_D_target"]) <= tol["mean_D_tol"]
    out["passed_targets"] = bool(passed_A and passed_D)
    
    return out

def generate_report(fixtures_path: str, output_path: str, iterations: int = 1000):
    with open(fixtures_path, 'r') as f:
        fixtures = json.load(f)
        
    report = {}
    for name, fixture in fixtures.items():
        report[name] = run_fixture(name, fixture, iterations)
        
    with open(output_path, 'w') as f:
        json.dump(report, f, indent=2)

if __name__ == "__main__":
    generate_report("campaign3/sim/non_normative_fixtures.json", "campaign3/sim/moments_report.json")
