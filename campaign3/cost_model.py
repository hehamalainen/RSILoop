"""
Campaign 3 Cost Algebra & Affordability Inputs Module (Work Order SE-3R).
NON-NORMATIVE / ANALYTICAL CLOSEOUT ONLY.

Provides exact parametric cost algebra, call categorization, retry models,
affine coefficient generators, and historical figure reconciliation across
all four combinations of the two unresolved design axes:
  Axis A: Search baseline evaluation (SHARED vs SEPARATE)
  Axis B: Mutation-lineage topology (BLOCK_SHARED vs REPLICATE_SPECIFIC)

No parameter values (M, R, k, B, B_max, T_solution, T_mutation) are selected,
frozen, or registered by this module.
"""

import inspect
import json
import os
from typing import Dict, Any, List, Tuple, Optional

NUM_SEARCH_TASKS = 8
NUM_CONF_TASKS = 8
NUM_HOLDOUT_TASKS = 6
NUM_FINAL_TASKS = NUM_CONF_TASKS + NUM_HOLDOUT_TASKS  # 14

VALID_SEARCH_BASELINE_VARIANTS = ["SHARED", "SEPARATE"]
VALID_MUTATION_TOPOLOGIES = ["BLOCK_SHARED", "REPLICATE_SPECIFIC"]
FOUR_VARIANTS = [
    "SHARED_BLOCK_SHARED",
    "SHARED_REPLICATE_SPECIFIC",
    "SEPARATE_BLOCK_SHARED",
    "SEPARATE_REPLICATE_SPECIFIC"
]

def validate_k(k: Any) -> int:
    """Validate k is a non-boolean integer in {1, 2, 3}."""
    if type(k) is not int or isinstance(k, bool) or k not in {1, 2, 3}:
        raise ValueError(f"k must be a non-boolean integer in {{1, 2, 3}}, got {k!r}")
    return k

def validate_b(B: Any) -> int:
    """Validate B is a non-boolean even integer in [2, 40]."""
    if type(B) is not int or isinstance(B, bool) or B < 2 or B > 40 or B % 2 != 0:
        raise ValueError(f"B must be a non-boolean even integer in [2, 40], got {B!r}")
    return B

def validate_m(M: Any) -> int:
    """Validate M is a non-boolean, non-negative integer."""
    if type(M) is not int or isinstance(M, bool) or M < 0:
        raise ValueError(f"M must be a non-boolean, non-negative integer, got {M!r}")
    return M

def validate_r(R: Any) -> int:
    """Validate R is a mandatory non-boolean integer >= 1."""
    if type(R) is not int or isinstance(R, bool) or R < 1:
        raise ValueError(f"R is mandatory and must be a non-boolean integer >= 1, got {R!r}")
    return R

def validate_tokens(T_solution: Any, T_mutation: Any) -> Tuple[Optional[int], Optional[int]]:
    """Validate token parameters: either both None, or both non-boolean, non-negative integers."""
    if (T_solution is None) != (T_mutation is None):
        raise ValueError("T_solution and T_mutation must either both be None or both be non-negative integers")
    if T_solution is not None:
        if type(T_solution) is not int or isinstance(T_solution, bool) or T_solution < 0:
            raise ValueError(f"T_solution must be a non-boolean, non-negative integer, got {T_solution!r}")
        if type(T_mutation) is not int or isinstance(T_mutation, bool) or T_mutation < 0:
            raise ValueError(f"T_mutation must be a non-boolean, non-negative integer, got {T_mutation!r}")
    return T_solution, T_mutation

def parse_variant(variant: str) -> Tuple[str, str]:
    """Parse variant string into (search_baseline_axis, mutation_topology_axis)."""
    if variant == "SHARED_BLOCK_SHARED":
        return "SHARED", "BLOCK_SHARED"
    elif variant == "SHARED_REPLICATE_SPECIFIC":
        return "SHARED", "REPLICATE_SPECIFIC"
    elif variant == "SEPARATE_BLOCK_SHARED":
        return "SEPARATE", "BLOCK_SHARED"
    elif variant == "SEPARATE_REPLICATE_SPECIFIC":
        return "SEPARATE", "REPLICATE_SPECIFIC"
    else:
        raise ValueError(f"Unknown variant '{variant}'. Must be one of {FOUR_VARIANTS}")

def get_per_block_symbolic_formulas(variant: str) -> Dict[str, Any]:
    """
    Return exact symbolic string and algebraic formulas for a single block under the specified variant.
    """
    base_axis, topo_axis = parse_variant(variant)

    if base_axis == "SHARED":
        search_base_formula = "8k"
        solution_formula = "(50 + 16M)k"
        solution_intercept_k_factor = 50
        solution_slope_k_factor = 16
    else:
        search_base_formula = "16k"
        solution_formula = "(58 + 16M)k"
        solution_intercept_k_factor = 58
        solution_slope_k_factor = 16

    if topo_axis == "BLOCK_SHARED":
        mutation_formula = "2M"
        logical_formula = f"{solution_formula} + 2M"
        logical_intercept_formula = f"{solution_intercept_k_factor}k"
        logical_slope_formula = "16k + 2"
    else:
        mutation_formula = "2kM"
        total_k_factor = solution_intercept_k_factor
        slope_k_factor = solution_slope_k_factor + 2  # 18
        logical_formula = f"({total_k_factor} + {slope_k_factor}M)k"
        logical_intercept_formula = f"{total_k_factor}k"
        logical_slope_formula = f"{slope_k_factor}k"

    return {
        "variant": variant,
        "search_baseline_axis": base_axis,
        "mutation_topology_axis": topo_axis,
        "breakdown": {
            "search_starting_baseline_calls": search_base_formula,
            "mutant_search_evaluations": "16Mk",
            "confirmatory_evaluations": "24k",
            "secondary_holdout_evaluations": "18k",
            "final_evaluations_total": "42k",
            "subject_model_solution_calls": solution_formula,
            "mutation_generation_calls": mutation_formula,
            "local_evaluator_executions": solution_formula,
            "total_provider_logical_calls": logical_formula
        },
        "affine_coefficients_per_block": {
            "solution_intercept": f"{solution_intercept_k_factor}k",
            "solution_slope": f"{solution_slope_k_factor}k",
            "mutation_intercept": "0",
            "mutation_slope": "2" if topo_axis == "BLOCK_SHARED" else "2k",
            "logical_intercept": logical_intercept_formula,
            "logical_slope": logical_slope_formula
        }
    }

def get_affine_coefficients(variant: str, k: int, B: int) -> Dict[str, int]:
    """
    Compute integer affine coefficients (intercept and slope w.r.t. M)
    for total provider logical calls, solution calls, and mutation calls over B blocks.

    logical_calls(M) = intercept_logical + slope_logical * M
    solution_calls(M) = intercept_solution + slope_solution * M
    mutation_calls(M) = intercept_mutation + slope_mutation * M
    """
    k_val = validate_k(k)
    b_val = validate_b(B)
    base_axis, topo_axis = parse_variant(variant)

    if base_axis == "SHARED":
        sol_intercept_per_block = 50 * k_val
        sol_slope_per_block = 16 * k_val
    else:
        sol_intercept_per_block = 58 * k_val
        sol_slope_per_block = 16 * k_val

    if topo_axis == "BLOCK_SHARED":
        mut_intercept_per_block = 0
        mut_slope_per_block = 2
    else:
        mut_intercept_per_block = 0
        mut_slope_per_block = 2 * k_val

    sol_intercept = sol_intercept_per_block * b_val
    sol_slope = sol_slope_per_block * b_val

    mut_intercept = mut_intercept_per_block * b_val
    mut_slope = mut_slope_per_block * b_val

    logical_intercept = sol_intercept + mut_intercept
    logical_slope = sol_slope + mut_slope

    return {
        "variant": variant,
        "k": k_val,
        "B": b_val,
        "intercept_solution": sol_intercept,
        "slope_solution": sol_slope,
        "intercept_mutation": mut_intercept,
        "slope_mutation": mut_slope,
        "intercept_logical": logical_intercept,
        "slope_logical": logical_slope,
        "local_evaluator_intercept": sol_intercept,
        "local_evaluator_slope": sol_slope
    }

def evaluate_cost_breakdown(
    variant: str,
    k: int,
    B: int,
    M: int,
    R: int,
    T_solution: Optional[int] = None,
    T_mutation: Optional[int] = None
) -> Dict[str, Any]:
    """
    Instantiate the exact cost breakdown for a specific candidate configuration.
    Note: Any instantiated output is strictly non-normative / illustrative.
    """
    k_val = validate_k(k)
    b_val = validate_b(B)
    m_val = validate_m(M)
    r_val = validate_r(R)
    t_sol, t_mut = validate_tokens(T_solution, T_mutation)

    coeffs = get_affine_coefficients(variant, k_val, b_val)

    solution_calls = coeffs["intercept_solution"] + coeffs["slope_solution"] * m_val
    mutation_calls = coeffs["intercept_mutation"] + coeffs["slope_mutation"] * m_val
    logical_calls = coeffs["intercept_logical"] + coeffs["slope_logical"] * m_val
    local_evaluations = solution_calls

    max_transport_attempts = r_val * logical_calls

    token_estimate = None
    if t_sol is not None and t_mut is not None:
        token_estimate = (solution_calls * t_sol) + (mutation_calls * t_mut)

    return {
        "status": "NON_NORMATIVE_ILLUSTRATIVE",
        "variant": variant,
        "k": k_val,
        "B": b_val,
        "M": m_val,
        "R": r_val,
        "counts": {
            "subject_model_solution_calls": solution_calls,
            "mutation_generation_calls": mutation_calls,
            "local_evaluator_executions": local_evaluations,
            "total_provider_logical_calls": logical_calls,
            "maximum_transport_attempts": max_transport_attempts
        },
        "output_tokens_estimate": token_estimate,
        "tokens_per_call_inputs": {
            "T_solution": t_sol,
            "T_mutation": t_mut
        }
    }

def generate_all_coefficient_tables(
    k_values: Tuple[int, ...] = (1, 2, 3),
    b_values: Optional[Tuple[int, ...]] = None
) -> Dict[str, List[Dict[str, Any]]]:
    """Generate affine coefficient tables for all variants across candidate k in {1,2,3} and even B in {2..40}."""
    if b_values is None:
        b_values = tuple(range(2, 42, 2))

    tables: Dict[str, List[Dict[str, Any]]] = {}
    for var in FOUR_VARIANTS:
        rows = []
        for k in k_values:
            for B in b_values:
                coeffs = get_affine_coefficients(var, k, B)
                rows.append(coeffs)
        tables[var] = rows

    return tables

def reconcile_prior_figures() -> Dict[str, Any]:
    """
    Mechanically reconcile historical and reference figures against the parametric algebra.
    Proves exact mathematical relationship and clarifies why none is authoritative.
    """
    return {
        "reconciliations": {
            "24kB": {
                "expression": "24kB",
                "derivation": (
                    "Covers confirmatory evaluation tasks only: 3 arms (Baseline, Guided, Unguided) "
                    "x 8 confirmatory tasks x k replicates = 24k calls per block; total 24kB. "
                    "Completely omits search starting baselines, mutant search evaluations, "
                    "secondary holdout evaluations, and mutation generation calls."
                ),
                "scope": "CONFIRMATORY_EVALUATION_SUBSET_ONLY",
                "authoritative_status": "NON_AUTHORITATIVE_PARTIAL_ESTIMATE"
            },
            "(98k + 6)B": {
                "expression": "(98k + 6)B",
                "derivation": (
                    "Assumes M = 3, SHARED search starting baseline (8k), and BLOCK_SHARED mutation topology (2M = 6). "
                    "Solution calls = (8k [search base] + 16(3)k [search mutants] + 24k [confirmatory] + 18k [holdout]) = 98k. "
                    "Mutation generation calls = 2(3) = 6. "
                    "Total provider logical calls per block = 98k + 6; total over B blocks = (98k + 6)B."
                ),
                "assumed_variant": "SHARED_BLOCK_SHARED",
                "assumed_M": 3,
                "authoritative_status": "NON_AUTHORITATIVE_HYPOTHETICAL"
            },
            "(106k + 6)B": {
                "expression": "(106k + 6)B",
                "derivation": (
                    "Assumes M = 3, SEPARATE search starting baselines (16k), and BLOCK_SHARED mutation topology (2M = 6). "
                    "Solution calls = (16k [search base] + 16(3)k [search mutants] + 24k [confirmatory] + 18k [holdout]) = 106k. "
                    "Mutation generation calls = 2(3) = 6. "
                    "Total provider logical calls per block = 106k + 6; total over B blocks = (106k + 6)B."
                ),
                "assumed_variant": "SEPARATE_BLOCK_SHARED",
                "assumed_M": 3,
                "authoritative_status": "NON_AUTHORITATIVE_HYPOTHETICAL"
            },
            "p2_reference_algorithm": {
                "expression": "(58 + 18M)kB",
                "derivation": (
                    "Corresponds to SEPARATE search baselines (16k) and REPLICATE_SPECIFIC mutation topology (2kM). "
                    "Solution / evaluator calls per block = (16 + 16M + 24 + 18)k = (58 + 16M)k. "
                    "Mutation generation calls per block = 2kM. "
                    "Total provider logical calls per block = (58 + 18M)k; total over B blocks = (58 + 18M)kB. "
                    "When M = 3, this evaluates to (58 + 54)kB = 112kB."
                ),
                "assumed_variant": "SEPARATE_REPLICATE_SPECIFIC",
                "authoritative_status": "NON_AUTHORITATIVE_PROPOSAL"
            }
        },
        "conclusion": (
            "None of the prior figures is authoritative because neither the search baseline axis "
            "nor the mutation topology axis nor M nor (k, B) has been frozen by the operator."
        )
    }

def _get_function_location(func) -> str:
    """Return numeric file:line location for a function object."""
    lines, start_line = inspect.getsourcelines(func)
    end_line = start_line + len(lines) - 1
    return f"campaign3/cost_model.py:{start_line}-{end_line} ({func.__name__})"

def generate_traceability_matrix() -> List[Dict[str, str]]:
    """
    Dynamically generate the SE-3R traceability matrix using inspect
    to produce exact numeric file:line locations.
    """
    return [
        {
            "clause_id": "SE3R-1",
            "title": "Cost Four Variants; Select None",
            "implementation_location": f"{_get_function_location(parse_variant)}, {_get_function_location(get_per_block_symbolic_formulas)}",
            "exact_test_name": "test_campaign3_cost_model.TestCampaign3CostModel.test_se3r1_task_counts_and_four_variants_definition"
        },
        {
            "clause_id": "SE3R-2",
            "title": "Exact Derivation Across Two Unresolved Design Axes",
            "implementation_location": f"{_get_function_location(get_per_block_symbolic_formulas)}, {_get_function_location(get_affine_coefficients)}, {_get_function_location(evaluate_cost_breakdown)}",
            "exact_test_name": "test_campaign3_cost_model.TestCampaign3CostModel.test_se3r2_hand_computed_fixtures_all_four_variants"
        },
        {
            "clause_id": "SE3R-3",
            "title": "Call Classes, Fail-Closed Validation & Symbolic Retry Multiplier",
            "implementation_location": f"{_get_function_location(validate_k)}, {_get_function_location(validate_b)}, {_get_function_location(validate_m)}, {_get_function_location(validate_r)}, {_get_function_location(validate_tokens)}, {_get_function_location(evaluate_cost_breakdown)}",
            "exact_test_name": "test_campaign3_cost_model.TestCampaign3CostModel.test_se3r3_fail_closed_input_validation"
        },
        {
            "clause_id": "SE3R-4",
            "title": "Machine-Generated Affine Coefficient Tables (k in {1,2,3}, even B in [2,40])",
            "implementation_location": f"{_get_function_location(generate_all_coefficient_tables)}, {_get_function_location(get_affine_coefficients)}",
            "exact_test_name": "test_campaign3_cost_model.TestCampaign3CostModel.test_se3r4_coefficient_tables_generation"
        },
        {
            "clause_id": "SE3R-5",
            "title": "Reconciliation of Prior Historical and Reference Figures",
            "implementation_location": f"{_get_function_location(reconcile_prior_figures)}",
            "exact_test_name": "test_campaign3_cost_model.TestCampaign3CostModel.test_se3r5_reconcile_prior_figures"
        },
        {
            "clause_id": "SE3R-6",
            "title": "Tests, Isolation, Artifact Emission & Non-Selection Assertions",
            "implementation_location": f"{_get_function_location(build_cost_model_report)}, {_get_function_location(generate_cost_model_markdown)}, {_get_function_location(emit_cost_model_artifacts)}",
            "exact_test_name": "test_campaign3_cost_model.TestCampaign3CostModel.test_se3r6_isolated_artifact_emission_byte_exact"
        }
    ]

def build_cost_model_report() -> Dict[str, Any]:
    """
    Construct the complete, self-contained cost model and affordability analysis report.
    """
    symbolic_formulas = {var: get_per_block_symbolic_formulas(var) for var in FOUR_VARIANTS}
    coefficient_tables = generate_all_coefficient_tables()
    reconciliation = reconcile_prior_figures()
    traceability = generate_traceability_matrix()

    report = {
        "report_id": "SE3R_COST_ALGEBRA_AND_AFFORDABILITY_INPUTS",
        "governance_status": "NON_NORMATIVE_ANALYTICAL_CLOSEOUT_ONLY",
        "notice": (
            "This document is an analytical closeout for Campaign 3 Phase 2 (P2). "
            "It does NOT freeze, select, or recommend any values for M, R, k, B, B_max, "
            "T_solution, or T_mutation. RFC-1 v3 remains in DRAFT state and open items P3-P18 remain blocked."
        ),
        "task_counts": {
            "search_tasks": NUM_SEARCH_TASKS,
            "confirmatory_tasks": NUM_CONF_TASKS,
            "secondary_holdout_tasks": NUM_HOLDOUT_TASKS,
            "final_tasks_total": NUM_FINAL_TASKS
        },
        "unresolved_design_axes": {
            "axis_A_search_starting_baseline": {
                "SHARED": "8k solution calls per block, shared across both guided and unguided arms.",
                "SEPARATE": "16k solution calls per block, matched set of 8k guided and 8k unguided."
            },
            "axis_B_mutation_lineage_topology": {
                "BLOCK_SHARED": "2M mutation generation calls per block (M guided, M unguided shared across replicates).",
                "REPLICATE_SPECIFIC": "2kM mutation generation calls per block (M guided, M unguided per replicate)."
            }
        },
        "symbolic_formulas_per_block": symbolic_formulas,
        "call_classes_and_accounting_semantics": {
            "subject_model_solution_calls": "Solution generation calls to the subject model under evaluation.",
            "mutation_generation_calls": "Calls to the mutation generator LLM to synthesize mutant instructions.",
            "local_evaluator_executions": "Grading runs on the local benchmark harness (zero provider tokens).",
            "total_provider_logical_calls": "Sum of subject_model_solution_calls and mutation_generation_calls.",
            "transport_attempts": "Total transport attempts = R * logical_calls, where R >= 1 is a mandatory symbolic integer multiplier.",
            "token_accounting_notes": (
                "Pre-response and zero-body retries contribute zero output tokens to the body "
                "but may incur provider-dependent input token metering or billing exposure."
            )
        },
        "coefficient_tables": coefficient_tables,
        "prior_figure_reconciliation": reconciliation,
        "traceability_matrix": traceability
    }

    return report

def generate_cost_model_markdown(report: Dict[str, Any]) -> str:
    """Generate GitHub-flavored Markdown report from the structured dictionary."""
    lines = []
    lines.append("# Campaign 3: Cost Algebra & Affordability Inputs (SE-3R)")
    lines.append("")
    lines.append("> [!IMPORTANT]")
    lines.append("> **Governance Status**: NON-NORMATIVE / ANALYTICAL CLOSEOUT ONLY.")
    lines.append("> This report derives exact parametric cost formulas across all four candidate design variants.")
    lines.append("> It does **NOT** freeze, select, or recommend any values for `M`, `R`, `k`, `B`, `B_max`, `T_solution`, or `T_mutation`.")
    lines.append("> RFC-1 v3 remains in `DRAFT` status and items P3–P18 remain blocked.")
    lines.append("")
    lines.append("## 1. Preregistered Task Counts")
    lines.append("- **Search Tasks**: 8 tasks")
    lines.append("- **Confirmatory Tasks**: 8 tasks")
    lines.append("- **Secondary Holdout Tasks**: 6 tasks")
    lines.append("- **Final Tasks Total**: 14 tasks (8 confirmatory + 6 holdout)")
    lines.append("")
    lines.append("## 2. Unresolved Design Axes & Four Candidate Variants")
    lines.append("The total call count depends on two independent, unresolved scientific and engineering design axes:")
    lines.append("1. **Axis A (Search Starting Baseline)**:")
    lines.append("   - `SHARED`: 8k calls per block, shared across both arms.")
    lines.append("   - `SEPARATE`: 16k calls per block, matched 8k guided and 8k unguided.")
    lines.append("2. **Axis B (Mutation Lineage Topology)**:")
    lines.append("   - `BLOCK_SHARED`: 2M mutation generator calls per block (shared across replicates).")
    lines.append("   - `REPLICATE_SPECIFIC`: 2kM mutation generator calls per block (replicate-specific lineages).")
    lines.append("")
    lines.append("### Exact Per-Block and B-Block Formulas")
    lines.append("")
    lines.append("| Variant | Solution Calls (Per Block) | Mutation Calls (Per Block) | Provider Logical Calls (Per Block) | B-Block Logical Calls |")
    lines.append("|---|---|---|---|---|")
    lines.append("| `SHARED_BLOCK_SHARED` | `(50 + 16M)k` | `2M` | `(50 + 16M)k + 2M` | `[50k + (16k + 2)M]B` |")
    lines.append("| `SHARED_REPLICATE_SPECIFIC` | `(50 + 16M)k` | `2kM` | `(50 + 18M)k` | `(50 + 18M)kB` |")
    lines.append("| `SEPARATE_BLOCK_SHARED` | `(58 + 16M)k` | `2M` | `(58 + 16M)k + 2M` | `[58k + (16k + 2)M]B` |")
    lines.append("| `SEPARATE_REPLICATE_SPECIFIC` | `(58 + 16M)k` | `2kM` | `(58 + 18M)k` | `(58 + 18M)kB` |")
    lines.append("")
    lines.append("## 3. Call Classes, Retry Multiplier & Token Semantics")
    lines.append("- **Subject-Model Solution Calls**: Solution attempts submitted to the model under test.")
    lines.append("- **Mutation-Generation Calls**: Prompts to synthesize mutation candidates.")
    lines.append("- **Local Evaluator Executions**: Harness execution of test suites (local compute, 0 provider tokens).")
    lines.append("- **Total Provider Logical Calls**: `Solution Calls + Mutation Calls`.")
    lines.append("- **Maximum Transport Attempts**: `R * Logical Calls`, where `R >= 1` is a mandatory symbolic integer multiplier (no default).")
    lines.append("")
    lines.append("> [!NOTE]")
    lines.append("> Pre-response / zero-body retries contribute zero output tokens but may incur provider-dependent input token billing exposure.")
    lines.append("")
    lines.append("## 4. Affine Coefficient Tables (`logical_calls = intercept + slope * M`)")
    lines.append("For all candidate $k \\in \\{1, 2, 3\\}$ and even $B \\in \\{2, \\dots, 40\\}$, the call count is an affine function of $M$:")
    lines.append("")

    for var in FOUR_VARIANTS:
        lines.append(f"### Variant: `{var}`")
        lines.append("")
        lines.append("| k | B | Logical Intercept | Logical Slope | Solution Intercept | Solution Slope | Mutation Intercept | Mutation Slope |")
        lines.append("|---|---|---|---|---|---|---|---|")
        for k in (1, 2, 3):
            for B in range(2, 42, 2):
                c = get_affine_coefficients(var, k, B)
                lines.append(f"| {k} | {B} | {c['intercept_logical']} | {c['slope_logical']} | {c['intercept_solution']} | {c['slope_solution']} | {c['intercept_mutation']} | {c['slope_mutation']} |")
        lines.append("")

    lines.append("## 5. Prior Figure Reconciliation")
    lines.append("")
    lines.append("1. **`24kB`**:")
    lines.append("   - **Scope**: Confirmatory evaluation subset only ($3 \\text{ arms} \\times 8 \\text{ tasks} \\times k = 24k$ per block).")
    lines.append("   - **Omission**: Omits all search starting baselines ($8k$ or $16k$), mutant evaluations ($16Mk$), holdout evaluations ($18k$), and mutation calls ($2M$ or $2kM$).")
    lines.append("2. **`(98k + 6)B`**:")
    lines.append("   - **Assumption**: Assumes $M = 3$, `SHARED` search baseline ($8k$), and `BLOCK_SHARED` mutation topology ($2M = 6$).")
    lines.append("   - **Evaluation**: $(8k + 16(3)k + 24k + 18k) + 6 = 98k + 6$ per block.")
    lines.append("3. **`(106k + 6)B`**:")
    lines.append("   - **Assumption**: Assumes $M = 3$, `SEPARATE` search baselines ($16k$), and `BLOCK_SHARED` mutation topology ($2M = 6$).")
    lines.append("   - **Evaluation**: $(16k + 16(3)k + 24k + 18k) + 6 = 106k + 6$ per block.")
    lines.append("4. **Existing P2 Reference (`(58 + 18M)kB`)**:")
    lines.append("   - **Assumption**: Corresponds to `SEPARATE` search baselines ($16k$) and `REPLICATE_SPECIFIC` mutation topology ($2kM$).")
    lines.append("   - **Evaluation**: $(16 + 16M + 24 + 18)k + 2kM = (58 + 18M)k$ per block; equals $112kB$ when $M=3$.")
    lines.append("")
    lines.append("> [!IMPORTANT]")
    lines.append("> None of the above figures is authoritative until the operator officially decides the design axes and parameter registers.")
    lines.append("")
    lines.append("## 6. Work Order Traceability Matrix (SE-3R)")
    lines.append("")
    lines.append("| Clause ID | Title | Implementation Location | Exact Test Suite / Verification |")
    lines.append("|---|---|---|---|")
    for t in report.get("traceability_matrix", []):
        lines.append(f"| `{t['clause_id']}` | {t['title']} | `{t['implementation_location']}` | `{t['exact_test_name']}` |")
    lines.append("")

    return "\n".join(lines)

def emit_cost_model_artifacts(
    json_path: Optional[str] = None,
    md_path: Optional[str] = None
) -> Tuple[str, str]:
    """Emit the JSON and Markdown cost model reports."""
    base_dir = os.path.dirname(os.path.abspath(__file__))
    if json_path is None:
        json_path = os.path.join(base_dir, "cost_model_report.json")
    if md_path is None:
        md_path = os.path.join(base_dir, "cost_model_report.md")

    report = build_cost_model_report()

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2)

    md_text = generate_cost_model_markdown(report)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_text)

    return json_path, md_path

if __name__ == "__main__":
    j_p, m_p = emit_cost_model_artifacts()
    print(f"Emitted: {j_p} and {m_p}")
