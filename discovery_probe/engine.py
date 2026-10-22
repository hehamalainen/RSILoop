"""Deterministic, dependency-free active-discovery episodes.

The benchmark deliberately scores only locked challenge predictions. Exploratory
probes are bounded but otherwise unpenalized: a useful experiment is part of the
capability being measured, rather than wasted motion.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import random
import string
from typing import Callable, Iterable, Sequence


class DiscoveryError(ValueError):
    """Raised when an episode command violates the benchmark protocol."""


@dataclass(frozen=True)
class _Operation:
    name: str
    description: str
    apply: Callable[[str], str]


@dataclass(frozen=True)
class _Rule:
    operations: tuple[_Operation, ...]

    def apply(self, value: str) -> str:
        for operation in self.operations:
            value = operation.apply(value)
        return value

    @property
    def description(self) -> str:
        steps = "; then ".join(operation.description for operation in self.operations)
        return f"Starting with the input, {steps}."

    @property
    def specification(self) -> tuple[str, ...]:
        return tuple(operation.name for operation in self.operations)


@dataclass(frozen=True)
class Probe:
    input: str
    output: str


@dataclass(frozen=True)
class EpisodeResult:
    seed: int
    tier: int
    rule_commitment: str
    rule_specification: tuple[str, ...]
    rule_description: str
    probes: tuple[Probe, ...]
    challenges: tuple[str, ...]
    expected: tuple[str, ...]
    guesses: tuple[str, ...]
    correct: int
    total: int

    @property
    def score(self) -> float:
        return self.correct / self.total

    @property
    def solved(self) -> bool:
        return self.correct == self.total

    def as_dict(self) -> dict:
        report = asdict(self)
        report["score"] = self.score
        report["solved"] = self.solved
        report["commitment_valid"] = verify_result_commitment(self)
        return report


@dataclass(frozen=True)
class DiscoveryScorecard:
    hidden_solved: int
    hidden_total: int
    revealed_solved: int
    revealed_total: int

    @property
    def hidden_rate(self) -> float:
        return self.hidden_solved / self.hidden_total

    @property
    def revealed_rate(self) -> float:
        return self.revealed_solved / self.revealed_total

    @property
    def discovery_fraction(self) -> float:
        """Fraction of the rules-given execution ceiling reached without rules."""
        if self.revealed_rate == 0:
            return 0.0
        return self.hidden_rate / self.revealed_rate

    @property
    def rule_information_gain(self) -> float:
        """Absolute solve-rate improvement caused by supplying the true rules."""
        return self.revealed_rate - self.hidden_rate

    def as_dict(self) -> dict:
        return {
            **asdict(self),
            "hidden_rate": self.hidden_rate,
            "revealed_rate": self.revealed_rate,
            "discovery_fraction": self.discovery_fraction,
            "rule_information_gain": self.rule_information_gain,
        }


def _rotate_letters(value: str, delta: int) -> str:
    transformed = []
    for character in value:
        if "a" <= character <= "z":
            transformed.append(chr((ord(character) - ord("a") + delta) % 26 + ord("a")))
        elif "A" <= character <= "Z":
            transformed.append(chr((ord(character) - ord("A") + delta) % 26 + ord("A")))
        else:
            transformed.append(character)
    return "".join(transformed)


def _swap_pairs(value: str) -> str:
    characters = list(value)
    for index in range(0, len(characters) - 1, 2):
        characters[index], characters[index + 1] = characters[index + 1], characters[index]
    return "".join(characters)


def _outside_in(value: str) -> str:
    output = []
    left = 0
    right = len(value) - 1
    while left <= right:
        output.append(value[left])
        if left != right:
            output.append(value[right])
        left += 1
        right -= 1
    return "".join(output)


def _move_vowels(value: str) -> str:
    vowels = set("aeiouAEIOU")
    return "".join(character for character in value if character not in vowels) + "".join(
        character for character in value if character in vowels
    )


def _operation_catalog() -> tuple[_Operation, ...]:
    return (
        _Operation("reverse", "reverse the whole string", lambda value: value[::-1]),
        _Operation("swap_case", "swap the case of every letter", str.swapcase),
        _Operation(
            "rotate_1",
            "advance every letter by one place in the alphabet, wrapping z to a",
            lambda value: _rotate_letters(value, 1),
        ),
        _Operation(
            "rotate_2",
            "advance every letter by two places in the alphabet, wrapping at z",
            lambda value: _rotate_letters(value, 2),
        ),
        _Operation("swap_pairs", "swap each adjacent pair, leaving a final odd character in place", _swap_pairs),
        _Operation("outside_in", "read characters from the outside inward, left then right", _outside_in),
        _Operation("vowels_last", "move all vowels to the end while preserving both groups' order", _move_vowels),
    )


def _make_rule(seed: int, tier: int) -> _Rule:
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise DiscoveryError("seed must be an integer")
    if isinstance(tier, bool) or not isinstance(tier, int) or not 1 <= tier <= 3:
        raise DiscoveryError("tier must be an integer from 1 to 3")
    generator = random.Random(f"rsi-discovery-rule-v1:{seed}:{tier}")
    operations = generator.sample(_operation_catalog(), k=tier)
    return _Rule(tuple(operations))


def _make_challenges(seed: int, count: int) -> tuple[str, ...]:
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise DiscoveryError("challenge_count must be a positive integer")
    generator = random.Random(f"rsi-discovery-challenges-v1:{seed}")
    alphabet = string.ascii_letters + string.digits
    anchors = ["aZ9", "signal", "AeIo7", "wxyz", "mN4qP"]
    generator.shuffle(anchors)
    values = anchors[: min(count, len(anchors))]
    while len(values) < count:
        length = generator.randint(3, 9)
        candidate = "".join(generator.choice(alphabet) for _ in range(length))
        if candidate not in values:
            values.append(candidate)
    return tuple(values)


def _commitment(seed: int, tier: int, specification: Sequence[str], challenges: Sequence[str]) -> str:
    payload = json.dumps(
        {
            "domain": "rsi-discovery-probe-v1",
            "seed": seed,
            "tier": tier,
            "rule": list(specification),
            "challenges": list(challenges),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class DiscoverySession:
    """A stateful hidden-rule episode with exploration and locked scoring phases."""

    def __init__(
        self,
        *,
        seed: int,
        tier: int = 1,
        max_probes: int = 12,
        challenge_count: int = 5,
    ) -> None:
        if isinstance(max_probes, bool) or not isinstance(max_probes, int) or max_probes < 1:
            raise DiscoveryError("max_probes must be a positive integer")
        if isinstance(challenge_count, bool) or not isinstance(challenge_count, int) or challenge_count < 1:
            raise DiscoveryError("challenge_count must be a positive integer")
        self._rule = _make_rule(seed, tier)
        self._challenges = _make_challenges(seed, challenge_count)
        self._seed = seed
        self._tier = tier
        self._max_probes = max_probes
        self._probes: list[Probe] = []
        self._phase = "explore"
        self._result: EpisodeResult | None = None
        self._rule_commitment = _commitment(seed, tier, self._rule.specification, self._challenges)

    @property
    def rule_commitment(self) -> str:
        return self._rule_commitment

    @property
    def remaining_probes(self) -> int:
        return self._max_probes - len(self._probes)

    @property
    def phase(self) -> str:
        return self._phase

    @property
    def probes(self) -> tuple[Probe, ...]:
        return tuple(self._probes)

    def revealed_rule(self) -> str:
        """Return the rules-given control information without exposing code."""
        return self._rule.description

    def probe(self, value: str) -> str:
        if self._phase != "explore":
            raise DiscoveryError("probes are allowed only before challenges are revealed")
        if self.remaining_probes == 0:
            raise DiscoveryError("probe budget exhausted")
        self.validate_text(value, label="probe")
        output = self._rule.apply(value)
        self._probes.append(Probe(value, output))
        return output

    def challenges(self) -> tuple[str, ...]:
        """Lock exploration and reveal the inputs whose outputs must be predicted."""
        if self._phase == "finished":
            raise DiscoveryError("episode is already finished")
        self._phase = "solve"
        return self._challenges

    def submit(self, guesses: Sequence[str]) -> EpisodeResult:
        if self._phase != "solve":
            raise DiscoveryError("request challenges before submitting guesses")
        if isinstance(guesses, (str, bytes)):
            raise DiscoveryError("guesses must be a sequence of strings")
        guesses_tuple = tuple(guesses)
        if len(guesses_tuple) != len(self._challenges):
            raise DiscoveryError(f"expected {len(self._challenges)} guesses, got {len(guesses_tuple)}")
        for guess in guesses_tuple:
            self.validate_text(guess, label="guess")
        expected = tuple(self._rule.apply(value) for value in self._challenges)
        correct = sum(guess == answer for guess, answer in zip(guesses_tuple, expected))
        self._result = EpisodeResult(
            seed=self._seed,
            tier=self._tier,
            rule_commitment=self._rule_commitment,
            rule_specification=self._rule.specification,
            rule_description=self._rule.description,
            probes=tuple(self._probes),
            challenges=self._challenges,
            expected=expected,
            guesses=guesses_tuple,
            correct=correct,
            total=len(expected),
        )
        self._phase = "finished"
        return self._result

    @staticmethod
    def validate_text(value: str, *, label: str = "value") -> None:
        """Validate a string before it consumes a probe or enters a submission."""
        if not isinstance(value, str):
            raise DiscoveryError(f"{label} must be a string")
        if not 1 <= len(value) <= 32:
            raise DiscoveryError(f"{label} length must be between 1 and 32 characters")
        if any(character not in string.printable or character in "\r\n\t\x0b\x0c" for character in value):
            raise DiscoveryError(f"{label} must contain printable single-line ASCII only")


def score_conditions(
    hidden_results: Iterable[EpisodeResult],
    revealed_results: Iterable[EpisodeResult],
) -> DiscoveryScorecard:
    """Compare hidden-rule performance with a rules-given execution control."""
    hidden = tuple(hidden_results)
    revealed = tuple(revealed_results)
    if not hidden or not revealed:
        raise DiscoveryError("both conditions need at least one completed episode")
    hidden_episodes = sorted((result.seed, result.tier, result.rule_commitment) for result in hidden)
    revealed_episodes = sorted((result.seed, result.tier, result.rule_commitment) for result in revealed)
    if hidden_episodes != revealed_episodes:
        raise DiscoveryError("hidden and revealed conditions must use the same committed episodes")
    return DiscoveryScorecard(
        hidden_solved=sum(result.solved for result in hidden),
        hidden_total=len(hidden),
        revealed_solved=sum(result.solved for result in revealed),
        revealed_total=len(revealed),
    )


def verify_result_commitment(result: EpisodeResult) -> bool:
    """Verify that a completed result matches its pre-play SHA-256 commitment."""
    expected = _commitment(
        result.seed,
        result.tier,
        result.rule_specification,
        result.challenges,
    )
    return expected == result.rule_commitment
