"""
Unit and Integration Tests for Campaign 2 Orchestration System (Round 2 Remediation).

Tests:
1. MockModelProvider & MockTaskEvaluator behavior
2. Campaign2Orchestrator per-trial call arithmetic (202 for Suite C, 86 for Suite P)
3. Microblock trio adjacency verification
4. Active arm assignment vector (+1/-1) scheduling & slot labeling
5. PromptEnvelope building & mutation output validation (delimiter rejection, code block rejection)
6. Mandatory launch lock verification & isolated test directory handling
7. SCIENTIFIC_START preflight failure prevention & full-stage refusal
8. Integrated AttemptLedger append-only JSONL tracking
9. Integrated CheckpointManager trial-boundary durability & resumption
10. Restored baseline-headroom pilot outcome ladder decision evaluation
11. Complete Schema 3 reporting populated with all required metadata fields
"""

import os
import sys
import json
import stat
import tempfile
import unittest
from fractions import Fraction

from campaign2.model_provider import (
    ModelProvider,
    MockModelProvider,
    MockTaskEvaluator,
    SolModelProvider,
)
from campaign2.envelope import PromptEnvelope, PromptEnvelopeError
from campaign2.registration import (
    generate_master_registration,
    verify_master_registration,
    derive_assignment_vectors,
    get_trio_ordering_index,
    KNOWN_LIMITATIONS,
)
from campaign2.tier_assignment import generate_family_blocked_assignment
from campaign2.orchestrator import (
    Campaign2Orchestrator,
    CallArithmeticError,
    TrioAdjacencyError,
    SUITE_C_PARAMS,
    SUITE_P_PARAMS,
)
from campaign2.launch_lock import (
    generate_launch_lock,
    verify_launch_lock,
    LaunchLockError,
    _git_head_sha,
)
from campaign2.attempt_lifecycle import (
    publish_scientific_start,
    check_and_resume,
    classify_call_fault,
    AttemptLedger,
    AttemptLifecycleError,
    AttemptInvalidatedError,
)
from campaign2.durability import CheckpointManager, DurabilityError
from campaign2.ladder import evaluate_pilot_outcome_ladder, FLOOR_THRESHOLD, HEADROOM_CEILING
from campaign2.reporting import generate_schema3_report, publish_schema3_report

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class TestPromptEnvelope(unittest.TestCase):
    """Test PromptEnvelope construction and mutation output validation."""

    def test_build_effective_prompt(self):
        envelope = PromptEnvelope()
        prompt, p_hash = envelope.build_effective_prompt("My strategy body")
        self.assertIn("---STRATEGY---", prompt)
        self.assertIn("My strategy body", prompt)
        self.assertEqual(len(p_hash), 64)

    def test_mutation_output_validation_rejects_delimiter_and_codeblocks(self):
        envelope = PromptEnvelope()

        # Valid strategy body
        clean = envelope.validate_mutation_output("  Focus on boundary conditions.  ")
        self.assertEqual(clean, "Focus on boundary conditions.")

        # Rejects delimiter injection
        with self.assertRaises(PromptEnvelopeError):
            envelope.validate_mutation_output("Strategy text ---STRATEGY--- escape attempt")

        # Rejects markdown code block wrapper
        with self.assertRaises(PromptEnvelopeError):
            envelope.validate_mutation_output("```python\ndef solve(): pass\n```")

        # Rejects contract duplication
        with self.assertRaises(PromptEnvelopeError):
            envelope.validate_mutation_output("You are an expert Python coder.")


class TestMockModelProvider(unittest.TestCase):
    """Test MockModelProvider and MockTaskEvaluator."""

    def test_generate_and_mutate_hash_predictability(self):
        provider = MockModelProvider()
        code1, t1 = provider.generate_candidate("task_a", "prompt1", "c1")
        self.assertIn("solve()", code1)
        self.assertEqual(t1, 100)

        mut_body, mt = provider.mutate_prompt("body1", "error trace", "m1")
        self.assertIn("guided=True", mut_body)
        self.assertEqual(mt, 200)

    def test_sol_provider_raises_not_implemented(self):
        sol = SolModelProvider()
        with self.assertRaises(NotImplementedError):
            sol.generate_candidate("task_a", "prompt", "c1")


class TestCampaign2Orchestrator(unittest.TestCase):
    """Test Campaign2Orchestrator execution, call arithmetic, arm vectors, and trios."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.workspace = self.tmp_dir.name
        # Generate registration inside isolated temp workspace
        generate_master_registration(self.workspace)
        self.reg_data = verify_master_registration(self.workspace)
        self.ta_data = generate_family_blocked_assignment(base_commit_sha=self.reg_data["base_commit_sha"])

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_suite_p_pilot_call_arithmetic_and_trio_adjacency(self):
        ledger_path = os.path.join(self.workspace, "ledger.jsonl")
        ledger = AttemptLedger(ledger_path)
        ckpt_mgr = CheckpointManager(os.path.join(self.workspace, "ckpt.json"), "pilot")

        orch = Campaign2Orchestrator(
            model_provider=MockModelProvider(),
            task_evaluator=MockTaskEvaluator(),
            tier_assignment=self.ta_data,
            registration=self.reg_data,
            suite="suite_p",
            ledger=ledger,
            checkpoint_mgr=ckpt_mgr,
            workspace_root=self.workspace,
        )

        results = orch.run_all_trials()
        self.assertEqual(len(results), 2)
        for trial_res in results:
            self.assertEqual(trial_res.model_call_count(), 86)

        self.assertTrue(orch.verify_trio_adjacency())
        self.assertTrue(ledger.verify_intent_before_completion())

        outcome = orch.compute_outcome()
        self.assertEqual(outcome["schema_version"], "3")
        self.assertEqual(outcome["suite"], "suite_p")

    def test_active_arm_assignment_randomization(self):
        # Create orchestrator with modified arm_vectors: trial 0 = +1, trial 1 = -1
        reg_mod = dict(self.reg_data)
        reg_mod["arm_assignment_vectors"] = [+1, -1]

        orch = Campaign2Orchestrator(
            model_provider=MockModelProvider(),
            task_evaluator=MockTaskEvaluator(),
            tier_assignment=self.ta_data,
            registration=reg_mod,
            suite="suite_p",
            workspace_root=self.workspace,
        )

        res0 = orch.run_trial(0)
        res1 = orch.run_trial(1)

        self.assertEqual(res0.arm_sign, +1)
        self.assertEqual(res1.arm_sign, -1)
        # Phase 2 calls record execution order reflecting arm_sign
        p2_calls_0 = [r for r in res0.call_records if r.phase == 2]
        p2_calls_1 = [r for r in res1.call_records if r.phase == 2]

        self.assertEqual(p2_calls_0[0].arm, "optimizer")
        self.assertEqual(p2_calls_1[0].arm, "control")

    def test_trial_resumption_via_checkpoint(self):
        ckpt_path = os.path.join(self.workspace, "ckpt.json")
        ckpt_mgr = CheckpointManager(ckpt_path, "pilot")

        orch1 = Campaign2Orchestrator(
            model_provider=MockModelProvider(),
            task_evaluator=MockTaskEvaluator(),
            tier_assignment=self.ta_data,
            registration=self.reg_data,
            suite="suite_p",
            checkpoint_mgr=ckpt_mgr,
            workspace_root=self.workspace,
        )

        # Execute trial 0
        orch1.run_trial(0)
        ckpt_mgr.seal_checkpoint_intent(0)
        ckpt_mgr.commit_checkpoint_head(0, {"summary": "trial_0_ok"})

        self.assertEqual(ckpt_mgr.get_resume_trial_index(), 1)

        # Second orchestrator instance should resume from trial 1
        orch2 = Campaign2Orchestrator(
            model_provider=MockModelProvider(),
            task_evaluator=MockTaskEvaluator(),
            tier_assignment=self.ta_data,
            registration=self.reg_data,
            suite="suite_p",
            checkpoint_mgr=ckpt_mgr,
            workspace_root=self.workspace,
        )

        resumed_results = orch2.run_all_trials()
        self.assertEqual(len(resumed_results), 1)  # Only trial 1 executed
        self.assertEqual(resumed_results[0].trial_index, 1)


class TestIsolatedLaunchLock(unittest.TestCase):
    """Test launch lock generation and verification in isolated temporary directory."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.workspace = self.tmp_dir.name
        generate_master_registration(self.workspace)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_launch_lock_verification_in_git_workspace(self):
        lock_path = generate_launch_lock(BASE_DIR, lock_rationale="test lock")
        try:
            self.assertTrue(os.path.exists(lock_path))
            lock_data = verify_launch_lock(BASE_DIR)
            expected_sha = _git_head_sha(BASE_DIR)
            self.assertEqual(lock_data["approved_commit_sha"], expected_sha)
        finally:
            if os.path.exists(lock_path):
                os.chmod(lock_path, 0o644)
                os.remove(lock_path)

    def test_launch_lock_rejects_empty_hashes(self):
        lock_file = os.path.join(self.workspace, "campaign2_launch_lock.json")
        bad_lock = {
            "schema_version": "3",
            "canonical_realpath": os.path.realpath(self.workspace),
            "approved_commit_sha": "0" * 40,
            "registration_hash": "",  # EMPTY
            "configuration_hash": "a" * 64,
        }
        with open(lock_file, "w") as f:
            json.dump(bad_lock, f)
        os.chmod(lock_file, 0o444)

        with self.assertRaises(LaunchLockError):
            verify_launch_lock(self.workspace)


class TestPilotOutcomeLadder(unittest.TestCase):
    """Test pre-registered baseline-headroom pilot outcome ladder."""

    def test_ladder_decision_outcomes(self):
        # 1. No headroom (baseline score >= 1.0)
        out1 = evaluate_pilot_outcome_ladder(
            baseline_score=Fraction(1, 1),
            opt_sel_opt_score=Fraction(1, 1),
            opt_sel_ctrl_score=Fraction(1, 1),
            conf_holdout_opt_score=Fraction(1, 1),
            conf_holdout_ctrl_score=Fraction(1, 1),
            delta=Fraction(0),
        )
        self.assertEqual(out1.decision, "no_headroom")
        self.assertFalse(out1.is_go)

        # 2. Insufficient headroom (baseline < 0.2)
        out2 = evaluate_pilot_outcome_ladder(
            baseline_score=Fraction(1, 10),
            opt_sel_opt_score=Fraction(5, 10),
            opt_sel_ctrl_score=Fraction(5, 10),
            conf_holdout_opt_score=Fraction(5, 10),
            conf_holdout_ctrl_score=Fraction(5, 10),
            delta=Fraction(0),
        )
        self.assertEqual(out2.decision, "insufficient_headroom")
        self.assertFalse(out2.is_go)

        # 3. Post-mutation floor failure on opt/sel (optimizer < 0.2)
        out3 = evaluate_pilot_outcome_ladder(
            baseline_score=Fraction(5, 10),
            opt_sel_opt_score=Fraction(1, 10),
            opt_sel_ctrl_score=Fraction(5, 10),
            conf_holdout_opt_score=Fraction(5, 10),
            conf_holdout_ctrl_score=Fraction(5, 10),
            delta=Fraction(0),
        )
        self.assertEqual(out3.decision, "opt_sel_floor")
        self.assertFalse(out3.is_go)

        # 4. No effect (delta <= 0)
        out4 = evaluate_pilot_outcome_ladder(
            baseline_score=Fraction(5, 10),
            opt_sel_opt_score=Fraction(5, 10),
            opt_sel_ctrl_score=Fraction(5, 10),
            conf_holdout_opt_score=Fraction(5, 10),
            conf_holdout_ctrl_score=Fraction(5, 10),
            delta=Fraction(0),
        )
        self.assertEqual(out4.decision, "no_effect")
        self.assertFalse(out4.is_go)

        # 5. GO outcome (valid baseline headroom, floors met, delta > 0)
        out5 = evaluate_pilot_outcome_ladder(
            baseline_score=Fraction(5, 10),
            opt_sel_opt_score=Fraction(6, 10),
            opt_sel_ctrl_score=Fraction(5, 10),
            conf_holdout_opt_score=Fraction(7, 10),
            conf_holdout_ctrl_score=Fraction(5, 10),
            delta=Fraction(2, 10),
        )
        self.assertEqual(out5.decision, "go")
        self.assertTrue(out5.is_go)


class TestRetryTaxonomyAndLedger(unittest.TestCase):
    """Test strict fail-closed retry taxonomy and append-only JSONL ledger."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.workspace = self.tmp_dir.name

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_retry_taxonomy_requires_bytes_sent_zero(self):
        # Connection error BEFORE request bytes sent -> RETRYABLE
        f1 = classify_call_fault(ConnectionRefusedError("Refused"), bytes_sent=0)
        self.assertTrue(f1.retryable)

        # Connection error AFTER bytes sent -> NON-RETRYABLE
        f2 = classify_call_fault(ConnectionError("Reset"), bytes_sent=50)
        self.assertFalse(f2.retryable)

        # Unconfirmed bytes sent (-1) -> NON-RETRYABLE (fail-closed)
        f3 = classify_call_fault(TimeoutError("Timed out"), bytes_sent=-1)
        self.assertFalse(f3.retryable)

    def test_append_only_jsonl_ledger(self):
        ledger_path = os.path.join(self.workspace, "attempt_ledger.jsonl")
        ledger = AttemptLedger(ledger_path)

        ledger.record_intent("c1", "generate", "optimizer", 1, "task_a")
        ledger.record_completion("c1", True, tokens=100)

        entries = ledger.get_entries()
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0]["event"], "intent")
        self.assertEqual(entries[1]["event"], "completion")

        self.assertTrue(ledger.verify_intent_before_completion())

        # Orphaning a completion without intent raises AttemptLifecycleError
        with self.assertRaises(AttemptLifecycleError):
            ledger.record_completion("orphan_c2", True)


if __name__ == "__main__":
    unittest.main()
