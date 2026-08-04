"""
Campaign 2 Pilot Outcome Ladder Module.

P0-5 — RESTORATION OF FROZEN BASELINE-HEADROOM PILOT LADDER:
Evaluates pilot trial outcomes against pre-specified baseline-headroom decision criteria:

Decision Outcomes:
1. "invalid": infrastructure error or execution protocol failure
2. "no_headroom": baseline score >= 1.0 (no headroom for optimization)
3. "insufficient_headroom": baseline score < 0.2 (below floor)
4. "opt_sel_floor": post-mutation arm aggregate < 0.2
5. "no_effect": delta (opt_winner - ctrl_winner) <= 0 on confirmatory+holdout
6. "go": baseline headroom valid, floors met, positive delta (opt_winner - ctrl_winner > 0)
"""

from fractions import Fraction
from typing import Dict, Any, List, Tuple


FLOOR_THRESHOLD = Fraction(1, 5)  # 0.2 (20%)
HEADROOM_CEILING = Fraction(1, 1)  # 1.0 (100%)


class LadderOutcome:
    """Result of evaluating the pilot outcome ladder."""
    __slots__ = ('decision', 'reason', 'detail', 'is_go')

    def __init__(self, decision: str, reason: str, detail: str = "", is_go: bool = False):
        self.decision = decision  # "go", "no_headroom", "insufficient_headroom", "opt_sel_floor", "no_effect", "invalid"
        self.reason = reason
        self.detail = detail
        self.is_go = is_go

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}


def evaluate_pilot_outcome_ladder(
    baseline_score: Fraction,
    opt_sel_opt_score: Fraction,
    opt_sel_ctrl_score: Fraction,
    conf_holdout_opt_score: Fraction,
    conf_holdout_ctrl_score: Fraction,
    delta: Fraction,
) -> LadderOutcome:
    """
    Evaluate the pilot outcome ladder against pre-specified criteria.

    Args:
        baseline_score: pre-mutation baseline aggregate score on opt+sel
        opt_sel_opt_score: optimizer arm aggregate on opt+sel
        opt_sel_ctrl_score: control arm aggregate on opt+sel
        conf_holdout_opt_score: optimizer winner aggregate on confirmatory+holdout
        conf_holdout_ctrl_score: control winner aggregate on confirmatory+holdout
        delta: optimizer_winner - control_winner on confirmatory+holdout
    """
    # Step 1: Baseline Headroom Check (no headroom if baseline >= 1.0)
    if baseline_score >= HEADROOM_CEILING:
        return LadderOutcome(
            "no_headroom", "baseline_score_at_ceiling",
            f"Baseline aggregate {baseline_score} >= 1.0; no headroom for optimization",
            is_go=False
        )

    # Step 2: Baseline Floor Check (insufficient headroom if baseline < 0.2)
    if baseline_score < FLOOR_THRESHOLD:
        return LadderOutcome(
            "insufficient_headroom", "baseline_below_floor",
            f"Baseline aggregate {baseline_score} < floor {FLOOR_THRESHOLD}",
            is_go=False
        )

    # Step 3: Post-mutation Arm Floor Checks on opt/sel
    if opt_sel_opt_score < FLOOR_THRESHOLD:
        return LadderOutcome(
            "opt_sel_floor", "optimizer_below_floor",
            f"Optimizer opt/sel aggregate {opt_sel_opt_score} < floor {FLOOR_THRESHOLD}",
            is_go=False
        )
    if opt_sel_ctrl_score < FLOOR_THRESHOLD:
        return LadderOutcome(
            "opt_sel_floor", "control_below_floor",
            f"Control opt/sel aggregate {opt_sel_ctrl_score} < floor {FLOOR_THRESHOLD}",
            is_go=False
        )

    # Step 4: Delta Check on Confirmatory+Holdout
    if delta <= 0:
        return LadderOutcome(
            "no_effect", "delta_non_positive",
            f"Delta (opt_winner - ctrl_winner) = {delta} <= 0; no effect detected",
            is_go=False
        )

    # Step 5: All criteria met -> GO
    return LadderOutcome(
        "go", "positive_delta_with_valid_headroom",
        f"Baseline headroom valid ({baseline_score}), floors met, positive delta = {delta} > 0",
        is_go=True
    )
