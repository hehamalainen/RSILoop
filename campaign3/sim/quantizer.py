import fractions
import numpy as np
from typing import Optional
from campaign3.sim.rational import RationalScore

QUANTIZER_CONTRACT = "IEEE-754-HALF-EVEN-DENOM-GE-8-V1"
QUANTIZER_VERSION = "1.0.0"

def validate_denominator(denom: int) -> None:
    """Validate that a scalar denominator is a non-boolean integer >= 8."""
    if isinstance(denom, bool) or not isinstance(denom, int):
        raise TypeError(f"Denominator must be integer, got {type(denom).__name__} ({denom})")
    if denom < 8:
        raise ValueError(f"Denominator must be integer >= 8, got {denom}")

def validate_denominators_array(denoms: np.ndarray) -> None:
    """Validate that an array of denominators contains integer values >= 8."""
    if not np.issubdtype(denoms.dtype, np.integer):
        raise TypeError(f"Denominators array must have integer dtype, got {denoms.dtype}")
    if np.any(denoms < 8):
        min_val = int(np.min(denoms))
        raise ValueError(f"Denominators array must have all values >= 8, minimum was {min_val}")

def quantize_earned_points(value: float, denom: int) -> int:
    """
    IEEE 754 round-half-to-even quantizer for discrete rational scoring on [0, 1].
    Rounds `value * denom` to the nearest integer, breaking ties (halfways) to the even integer.
    Clips output to the closed integer interval [0, denom].
    """
    validate_denominator(denom)
    earned = int(round(value * denom))
    if earned < 0:
        return 0
    if earned > denom:
        return denom
    return earned

def clip_to_rational(value: float, denom: int) -> RationalScore:
    """
    Quantize and clip continuous score `value` to a RationalScore on [0, 1] with denominator `denom`.
    """
    earned = quantize_earned_points(value, denom)
    return RationalScore(earned, denom)

def clip_to_fraction(value: float, denom: int) -> fractions.Fraction:
    """
    Quantize and clip continuous score `value` to an exact fractions.Fraction on [0, 1].
    """
    earned = quantize_earned_points(value, denom)
    return fractions.Fraction(earned, denom)

def quantize_earned_points_array_inplace(
    val_inout: np.ndarray,
    denoms: np.ndarray,
    out_earned: np.ndarray
) -> None:
    """
    In-place vectorized IEEE 754 round-half-to-even quantizer with zero heap temporaries.
    Computes val_inout * denoms in-place, rounds, clips, and copies to out_earned using np.copyto.
    """
    validate_denominators_array(denoms)
    val_inout *= denoms
    np.round(val_inout, out=val_inout)
    np.clip(val_inout, 0, denoms, out=val_inout)
    np.copyto(out_earned, val_inout, casting="unsafe")

def quantize_earned_points_array(
    values: np.ndarray,
    denoms: np.ndarray,
    out: Optional[np.ndarray] = None
) -> np.ndarray:
    """
    Vectorized IEEE 754 round-half-to-even quantizer for arrays of values and denominators.
    Broadcasts `values` and `denoms`.
    Returns integer earned points clipped to [0, denoms].
    """
    validate_denominators_array(denoms)
    if out is None:
        scaled = values * denoms
        np.round(scaled, out=scaled)
        np.clip(scaled, 0, denoms, out=scaled)
        res = np.zeros_like(scaled, dtype=np.int64)
        np.copyto(res, scaled, casting="unsafe")
        return res
    else:
        np.multiply(values, denoms, out=out)
        np.round(out, out=out)
        np.clip(out, 0, denoms, out=out)
        res = np.zeros_like(out, dtype=np.int64)
        np.copyto(res, out, casting="unsafe")
        return res

def quantize_scores_array(
    values: np.ndarray,
    denoms: np.ndarray,
    out: Optional[np.ndarray] = None
) -> np.ndarray:
    """
    Vectorized quantizer returning continuous rational float scores (earned / denom) on [0, 1].
    """
    earned = quantize_earned_points_array(values, denoms, out=out)
    return earned.astype(np.float64) / denoms
