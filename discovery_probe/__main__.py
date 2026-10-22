"""Interactive terminal for the Discovery Probe."""

from __future__ import annotations

import argparse
import json

from .engine import DiscoveryError, DiscoverySession


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Play a hidden string-transformation discovery episode.")
    parser.add_argument("--seed", type=int, default=1, help="Deterministic episode seed (default: 1).")
    parser.add_argument("--tier", type=int, choices=(1, 2, 3), default=1, help="Number of composed rules.")
    parser.add_argument("--max-probes", type=int, default=12, help="Exploration query budget.")
    parser.add_argument(
        "--reveal-rules",
        action="store_true",
        help="Show the true rule up front for the execution-control condition.",
    )
    return parser


def main() -> int:
    arguments = _parser().parse_args()
    session = DiscoverySession(seed=arguments.seed, tier=arguments.tier, max_probes=arguments.max_probes)
    print("RSILoop Discovery Probe")
    print(f"tier={arguments.tier}  probes={arguments.max_probes}  commitment={session.rule_commitment}")
    if arguments.reveal_rules:
        print(f"RULES GIVEN: {session.revealed_rule()}")
    print("Enter a test string to probe the hidden machine. Enter :solve when ready.")

    while session.phase == "explore":
        try:
            value = input(f"probe[{session.remaining_probes}]> ")
        except EOFError:
            print("\nNo submission; episode abandoned.")
            return 2
        if value == ":solve":
            break
        try:
            print(f"=> {session.probe(value)}")
        except DiscoveryError as error:
            print(f"error: {error}")

    challenges = session.challenges()
    print("Exploration is now locked. Predict each output exactly.")
    guesses = []
    for challenge in challenges:
        while True:
            try:
                guess = input(f"{challenge} => ")
                session.validate_text(guess, label="guess")
            except (DiscoveryError, EOFError) as error:
                if isinstance(error, EOFError):
                    print("\nNo submission; episode abandoned.")
                    return 2
                print(f"error: {error}")
                continue
            guesses.append(guess)
            break

    result = session.submit(guesses)
    print(json.dumps(result.as_dict(), indent=2, sort_keys=True))
    return 0 if result.solved else 1


if __name__ == "__main__":
    raise SystemExit(main())
