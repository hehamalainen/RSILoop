"""
Campaign 2 Defect Ladder Verifier (A3).

Verifies strict monotonicity of defect ladders (reference > rung1 > rung2 > rung3) across all 30 tasks.
"""

import os
import sys
import importlib.util
from typing import Dict, Any, List, Tuple

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCHMARKS_DIR = os.path.join(BASE_DIR, "benchmarks", "campaign2")


def load_task_module(task_dir: str, module_name: str):
    fpath = os.path.join(task_dir, f"{module_name}.py")
    if not os.path.exists(fpath):
        raise FileNotFoundError(f"Missing required task file: {fpath}")
    mod_key = f"task_{os.path.basename(task_dir)}_{module_name}"
    spec = importlib.util.spec_from_file_location(mod_key, fpath)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def verify_task_defect_ladder(suite: str, task_id: str) -> Dict[str, Any]:
    task_dir = os.path.join(BENCHMARKS_DIR, f"suite_{suite}", task_id)
    sol_mod = load_task_module(task_dir, "solution")
    harness_mod = load_task_module(task_dir, "test_harness")
    defects_mod = load_task_module(task_dir, "defects")

    run_eval = getattr(harness_mod, "run_evaluation")
    ref_earned, ref_max = run_eval(sol_mod)

    if ref_earned < ref_max:
        raise ValueError(f"Task {task_id} reference implementation failed maximum score: earned {ref_earned}/{ref_max}")

    defect_ladder = getattr(defects_mod, "DEFECT_LADDER", [])
    if len(defect_ladder) < 3:
        raise ValueError(f"Task {task_id} defect ladder must contain at least 3 rungs, got {len(defect_ladder)}")

    rung_scores = []
    prev_score = ref_earned
    strictly_decreasing = True

    for idx, rung_fn in enumerate(defect_ladder, 1):
        class DummyModule:
            pass
        d_mod = DummyModule()
        for attr_name in dir(sol_mod):
            if not attr_name.startswith("__"):
                val = getattr(sol_mod, attr_name)
                if callable(val) and hasattr(val, "__name__"):
                    setattr(d_mod, attr_name, rung_fn)
                else:
                    setattr(d_mod, attr_name, val)

        earned, max_s = run_eval(d_mod)
        rung_scores.append((earned, max_s))

        if earned >= prev_score:
            strictly_decreasing = False
            raise ValueError(
                f"Task {task_id} defect ladder failed strict decrease requirement at rung {idx}: "
                f"score {earned} >= previous score {prev_score}"
            )
        prev_score = earned

    return {
        "task_id": task_id,
        "reference_score": (ref_earned, ref_max),
        "rung_scores": rung_scores,
        "strictly_decreasing": strictly_decreasing
    }


def verify_all_defects() -> List[Dict[str, Any]]:
    results = []
    for suite, prefix in [("c", "c_"), ("p", "p_")]:
        s_dir = os.path.join(BENCHMARKS_DIR, f"suite_{suite}")
        if not os.path.exists(s_dir):
            raise FileNotFoundError(f"Missing suite directory: {s_dir}")
        tasks = sorted([d for d in os.listdir(s_dir) if os.path.isdir(os.path.join(s_dir, d))])
        for task_id in tasks:
            res = verify_task_defect_ladder(suite, task_id)
            results.append(res)
    return results
