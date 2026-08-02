"""
Campaign 2 Rational Arithmetic Scorer Module (A2).

Enforces exact rational arithmetic scoring via Python fractions.Fraction.
Rejects floating point rounding, negative scores, earned > max_score, and resolution < 8.
All functions fail-closed with ScorerValidationError on any anomaly.
"""

from fractions import Fraction
from typing import Tuple, List


class ScorerValidationError(ValueError):
    """Raised when score pair validation fails or arithmetic boundaries are violated."""
    pass


def validate_score_pair(earned: int, max_score: int) -> Tuple[int, int]:
    """Validates that earned and max_score are non-negative integers with max_score >= 8."""
    if not isinstance(earned, int) or isinstance(earned, bool):
        raise ScorerValidationError(f"earned_score must be an integer, got {type(earned)}")
    if not isinstance(max_score, int) or isinstance(max_score, bool):
        raise ScorerValidationError(f"max_score must be an integer, got {type(max_score)}")
    if max_score < 8:
        raise ScorerValidationError(f"max_score resolution must be >= 8, got {max_score}")
    if earned < 0 or earned > max_score:
        raise ScorerValidationError(f"earned_score {earned} out of bounds [0, {max_score}]")
    return earned, max_score


def task_score_fraction(earned: int, max_score: int) -> Fraction:
    """Computes exact rational fraction earned / max_score."""
    e, m = validate_score_pair(earned, max_score)
    return Fraction(e, m)


def task_mean_over_k(pairs: List[Tuple[int, int]]) -> Fraction:
    """Computes mean score fraction over K trials: (1/K) * sum(earned_i / max_i)."""
    if not pairs:
        raise ScorerValidationError("Trial list cannot be empty")
    fractions = [task_score_fraction(e, m) for e, m in pairs]
    return sum(fractions) / Fraction(len(fractions), 1)


def aggregate_tasks_equal_weight(task_means: List[Fraction]) -> Fraction:
    """Computes equal-weighted aggregate score across N tasks: (1/N) * sum(task_mean_i)."""
    if not task_means:
        raise ScorerValidationError("Task list cannot be empty")
    for f in task_means:
        if not isinstance(f, Fraction):
            raise ScorerValidationError(f"Expected Fraction object, got {type(f)}")
    return sum(task_means) / Fraction(len(task_means), 1)


def aggregate_trials_equal_weight(trials: List[List[Tuple[int, int]]]) -> Fraction:
    """Computes equal-weighted aggregate across M trials: (1/M) * sum(task_mean_over_k(trial_i))."""
    if not trials:
        raise ScorerValidationError("Trial list cannot be empty")
    trial_means = [aggregate_tasks_equal_weight([task_score_fraction(e, m) for e, m in t]) for t in trials]
    return sum(trial_means) / Fraction(len(trial_means), 1)
