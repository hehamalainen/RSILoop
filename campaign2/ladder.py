"""
Campaign 2 Pilot Outcome Ladder Module.

Evaluates pilot trial outcomes against pre-specified decision criteria.
Called by the orchestrator after all pilot trials complete.

Outcome Ladder:
1. opt/sel aggregate < 1/5 for EITHER arm → STOP (floor)
2. confirmatory aggregate < 1/5 for EITHER arm → STOP (floor)
3. delta(opt-ctrl) > 0 on confirmatory+holdout → ADVANCE to full run
4. delta(opt-ctrl) <= 0 → STOP (no effect detected)
"""

from fractions import Fraction
from typing import Dict, Any, List, Tuple


FLOOR_THRESHOLD = Fraction(1, 5)  # 0.2


class LadderOutcome:
    """Result of evaluating the pilot outcome ladder."""
    __slots__ = ('decision', 'reason', 'detail')

    def __init__(self, decision: str, reason: str, detail: str = ""):
        self.decision = decision  # "stop" or "advance"
        self.reason = reason
        self.detail = detail

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}


def evaluate_pilot_outcome_ladder(
    opt_sel_opt_score: Fraction,
    opt_sel_ctrl_score: Fraction,
    conf_opt_score: Fraction,
    conf_ctrl_score: Fraction,
    holdout_opt_score: Fraction,
    holdout_ctrl_score: Fraction,
    delta: Fraction,
) -> LadderOutcome:
    """
    Evaluate the pilot outcome ladder.

    Args:
        opt_sel_opt_score: optimizer arm aggregate on optimization+selection tasks
        opt_sel_ctrl_score: control arm aggregate on optimization+selection tasks
        conf_opt_score: optimizer aggregate on confirmatory tasks
        conf_ctrl_score: control aggregate on confirmatory tasks
        holdout_opt_score: optimizer aggregate on holdout tasks
        holdout_ctrl_score: control aggregate on holdout tasks
        delta: optimizer_winner - control_winner on confirmatory+holdout
    Returns:
        LadderOutcome with decision and reason
    """
    # Step 1: Floor check on opt/sel
    if opt_sel_opt_score < FLOOR_THRESHOLD:
        return LadderOutcome(
            "stop", "opt_sel_floor",
            f"Optimizer opt/sel aggregate {opt_sel_opt_score} < floor {FLOOR_THRESHOLD}"
        )
    if opt_sel_ctrl_score < FLOOR_THRESHOLD:
        return LadderOutcome(
            "stop", "opt_sel_floor",
            f"Control opt/sel aggregate {opt_sel_ctrl_score} < floor {FLOOR_THRESHOLD}"
        )

    # Step 2: Floor check on confirmatory
    if conf_opt_score < FLOOR_THRESHOLD:
        return LadderOutcome(
            "stop", "conf_floor",
            f"Optimizer confirmatory aggregate {conf_opt_score} < floor {FLOOR_THRESHOLD}"
        )
    if conf_ctrl_score < FLOOR_THRESHOLD:
        return LadderOutcome(
            "stop", "conf_floor",
            f"Control confirmatory aggregate {conf_ctrl_score} < floor {FLOOR_THRESHOLD}"
        )

    # Step 3: Delta check
    if delta > 0:
        return LadderOutcome(
            "advance", "positive_delta",
            f"Delta (opt-ctrl) = {delta} > 0; advance to full run"
        )

    # Step 4: No effect
    return LadderOutcome(
        "stop", "no_effect",
        f"Delta (opt-ctrl) = {delta} <= 0; no effect detected"
    )
