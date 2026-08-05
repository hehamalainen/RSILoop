"""
Campaign 2 Frozen B8 Pilot Outcome Ladder Module.

Exact Frozen B8 Decision Taxonomy (6 Outcomes):
1. "invalid": infrastructure or plumbing fault
2. "no_headroom": all 16 opt/selection baseline task scores equal 1.0 (mean == 1.0)
3. "insufficient_headroom": opt/selection baseline mean score > 9/10 (0.90)
4. "insufficient_confirmatory_headroom": confirmatory baseline mean score > 19/20 (0.95)
5. "insufficient_baseline_engagement": opt/selection baseline mean < 1/5 (0.20) OR confirmatory baseline mean < 1/5 (0.20)
6. "go": otherwise (headroom valid across baselines, baseline engagement met)
"""

from fractions import Fraction
from typing import Dict, Any, Optional

OPT_SEL_HEADROOM_CEILING = Fraction(9, 10)       # > 0.90 -> insufficient_headroom
CONF_HEADROOM_CEILING = Fraction(19, 20)         # > 0.95 -> insufficient_confirmatory_headroom
ENGAGEMENT_FLOOR = Fraction(1, 5)                # < 0.20 -> insufficient_baseline_engagement


class LadderOutcome:
    """Result of evaluating the frozen B8 pilot outcome ladder."""
    __slots__ = ('decision', 'reason', 'detail', 'is_go')

    def __init__(self, decision: str, reason: str, detail: str = "", is_go: bool = False):
        self.decision = decision
        self.reason = reason
        self.detail = detail
        self.is_go = is_go

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}


def evaluate_pilot_outcome_ladder(
    opt_sel_baseline: Fraction,
    conf_baseline: Fraction,
    delta: Fraction = Fraction(0),
    all_16_opt_sel_perfect: bool = False,
    is_invalid: bool = False,
    invalid_reason: str = "",
) -> LadderOutcome:
    """
    Evaluate the verbatim frozen B8 pilot outcome ladder.

    Args:
        opt_sel_baseline: Phase 1 pre-mutation baseline mean aggregate on Opt/Sel tasks
        conf_baseline: Phase 3 pre-mutation baseline mean aggregate on Confirmatory tasks
        delta: Delta value (retained for signature compatibility, unused by B8 logic)
        all_16_opt_sel_perfect: True if all 16 baseline evaluations on Opt/Sel tasks scored 1.0
        is_invalid: True if plumbing/infrastructure fault occurred
        invalid_reason: Detail string if is_invalid is True
    """
    # 1. Plumbing / Infrastructure Fault Check
    if is_invalid:
        return LadderOutcome(
            "invalid", "plumbing_or_execution_fault",
            invalid_reason or "Infrastructure or execution protocol error detected",
            is_go=False
        )

    # 2. All 16 Opt/Selection Baselines Perfect (No Headroom)
    if all_16_opt_sel_perfect or opt_sel_baseline == Fraction(1):
        return LadderOutcome(
            "no_headroom", "all_16_opt_sel_baselines_perfect",
            "All 16 baseline evaluations on optimization/selection tasks scored 1.0 (no headroom)",
            is_go=False
        )

    # 3. Opt/Selection High Baseline Ceiling Check
    if opt_sel_baseline > OPT_SEL_HEADROOM_CEILING:
        return LadderOutcome(
            "insufficient_headroom", "opt_sel_baseline_ceiling_exceeded",
            f"Opt/sel baseline aggregate {opt_sel_baseline} > ceiling {OPT_SEL_HEADROOM_CEILING}",
            is_go=False
        )

    # 4. Confirmatory High Baseline Ceiling Check
    if conf_baseline > CONF_HEADROOM_CEILING:
        return LadderOutcome(
            "insufficient_confirmatory_headroom", "confirmatory_baseline_ceiling_exceeded",
            f"Confirmatory baseline aggregate {conf_baseline} > ceiling {CONF_HEADROOM_CEILING}",
            is_go=False
        )

    # 5. Low Baseline Engagement Check (Opt/Sel or Confirmatory < 1/5)
    if opt_sel_baseline < ENGAGEMENT_FLOOR:
        return LadderOutcome(
            "insufficient_baseline_engagement", "opt_sel_baseline_below_floor",
            f"Opt/sel baseline aggregate {opt_sel_baseline} < engagement floor {ENGAGEMENT_FLOOR}",
            is_go=False
        )
    if conf_baseline < ENGAGEMENT_FLOOR:
        return LadderOutcome(
            "insufficient_baseline_engagement", "confirmatory_baseline_below_floor",
            f"Confirmatory baseline aggregate {conf_baseline} < engagement floor {ENGAGEMENT_FLOOR}",
            is_go=False
        )

    # 6. Otherwise -> GO Outcome
    return LadderOutcome(
        "go", "headroom_and_engagement_valid",
        f"Valid headroom across baselines (opt_sel={opt_sel_baseline}, conf={conf_baseline}), "
        f"engagement floor met",
        is_go=True
    )
