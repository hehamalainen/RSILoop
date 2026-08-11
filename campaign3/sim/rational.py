import fractions
from typing import Tuple

class RationalScore:
    __slots__ = ('earned', 'denominator', 'value')

    def __init__(self, earned: int, denominator: int):
        if not isinstance(earned, int) or not isinstance(denominator, int):
            raise TypeError("earned and denominator must be integers")
        if denominator <= 0:
            raise ValueError("denominator must be positive")
        if earned < 0 or earned > denominator:
            raise ValueError("earned must be in [0, denominator]")

        self.earned = earned
        self.denominator = denominator
        self.value = fractions.Fraction(earned, denominator)

    def __eq__(self, other):
        if not isinstance(other, RationalScore):
            return NotImplemented
        return self.value == other.value

    def __lt__(self, other):
        if not isinstance(other, RationalScore):
            return NotImplemented
        return self.value < other.value

    def __gt__(self, other):
        if not isinstance(other, RationalScore):
            return NotImplemented
        return self.value > other.value

    def __le__(self, other):
        if not isinstance(other, RationalScore):
            return NotImplemented
        return self.value <= other.value

    def __ge__(self, other):
        if not isinstance(other, RationalScore):
            return NotImplemented
        return self.value >= other.value

def accept_candidate(incumbent: RationalScore, candidate: RationalScore) -> RationalScore:
    """
    SE1-3: Exact rational acceptance.
    Strict `>` with the incumbent retained on ties.
    """
    if candidate > incumbent:
        return candidate
    return incumbent
