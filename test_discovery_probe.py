"""Protocol and scoring tests for the experimental Discovery Probe."""

import unittest

from discovery_probe import (
    DiscoveryError,
    DiscoverySession,
    score_conditions,
    verify_result_commitment,
)


class TestDiscoverySession(unittest.TestCase):
    def test_seed_is_reproducible(self):
        first = DiscoverySession(seed=17, tier=3)
        second = DiscoverySession(seed=17, tier=3)
        self.assertEqual(first.rule_commitment, second.rule_commitment)
        self.assertEqual(first.probe("aB9xy"), second.probe("aB9xy"))
        self.assertEqual(first.challenges(), second.challenges())

    def test_tier_changes_committed_rule(self):
        easy = DiscoverySession(seed=17, tier=1)
        hard = DiscoverySession(seed=17, tier=3)
        self.assertNotEqual(easy.rule_commitment, hard.rule_commitment)
        self.assertNotEqual(easy.revealed_rule(), hard.revealed_rule())

    def test_probe_budget_is_fail_closed(self):
        session = DiscoverySession(seed=2, max_probes=1)
        session.probe("abc")
        with self.assertRaisesRegex(DiscoveryError, "budget exhausted"):
            session.probe("xyz")

    def test_challenge_lock_prevents_answer_queries(self):
        session = DiscoverySession(seed=3)
        challenge = session.challenges()[0]
        with self.assertRaisesRegex(DiscoveryError, "only before challenges"):
            session.probe(challenge)

    def test_exact_predictions_produce_auditable_perfect_result(self):
        session = DiscoverySession(seed=21, tier=2)
        challenges = session.challenges()
        specification = session._rule.specification
        guesses = tuple(session._rule.apply(value) for value in challenges)
        result = session.submit(guesses)
        self.assertTrue(result.solved)
        self.assertEqual(result.score, 1.0)
        self.assertEqual(result.rule_specification, specification)
        self.assertEqual(result.rule_commitment, session.rule_commitment)
        self.assertTrue(verify_result_commitment(result))
        self.assertTrue(result.as_dict()["commitment_valid"])
        self.assertEqual(result.as_dict()["expected"], result.as_dict()["guesses"])

    def test_submit_requires_locked_challenges_and_exact_arity(self):
        session = DiscoverySession(seed=5, challenge_count=3)
        with self.assertRaisesRegex(DiscoveryError, "request challenges"):
            session.submit(("a", "b", "c"))
        session.challenges()
        with self.assertRaisesRegex(DiscoveryError, "expected 3 guesses"):
            session.submit(("a",))

    def test_rules_given_scorecard_is_a_discovery_diagnostic(self):
        failed = DiscoverySession(seed=7, challenge_count=1)
        failed.challenges()
        hidden_result = failed.submit(("definitely-wrong",))

        solved = DiscoverySession(seed=7, challenge_count=1)
        challenge = solved.challenges()[0]
        revealed_result = solved.submit((solved._rule.apply(challenge),))

        scorecard = score_conditions((hidden_result,), (revealed_result,))
        self.assertEqual(scorecard.hidden_rate, 0.0)
        self.assertEqual(scorecard.revealed_rate, 1.0)
        self.assertEqual(scorecard.discovery_fraction, 0.0)
        self.assertEqual(scorecard.rule_information_gain, 1.0)

    def test_scorecard_requires_paired_committed_episodes(self):
        hidden_session = DiscoverySession(seed=7, challenge_count=1)
        hidden_session.challenges()
        hidden = hidden_session.submit(("wrong",))

        revealed_session = DiscoverySession(seed=8, challenge_count=1)
        challenge = revealed_session.challenges()[0]
        revealed = revealed_session.submit((revealed_session._rule.apply(challenge),))

        with self.assertRaisesRegex(DiscoveryError, "same committed episodes"):
            score_conditions((hidden,), (revealed,))


if __name__ == "__main__":
    unittest.main()
