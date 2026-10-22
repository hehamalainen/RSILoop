"""A small, text-only benchmark for active rule discovery."""

from .engine import (
    DiscoveryError,
    DiscoveryScorecard,
    DiscoverySession,
    EpisodeResult,
    score_conditions,
    verify_result_commitment,
)

__all__ = [
    "DiscoveryError",
    "DiscoveryScorecard",
    "DiscoverySession",
    "EpisodeResult",
    "score_conditions",
    "verify_result_commitment",
]
