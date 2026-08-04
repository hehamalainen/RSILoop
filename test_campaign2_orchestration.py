"""
Unit and Integration Tests for Campaign 2 Orchestration System (Work Order B Rework).

Tests:
1. MockModelProvider & MockTaskEvaluator behavior
2. Campaign2Orchestrator per-trial call arithmetic (202 for Suite C, 86 for Suite P)
3. Microblock trio adjacency verification
4. Launch lock commit authentication & empty hash rejection
5. Attempt lifecycle marker publishing, resume path, and invalidation
6. Call fault classification from actual exception types
7. Attempt ledger intent-before-completion tracking
8. Durability checkpointing and resume index calculation
9. Pilot outcome ladder floor checks and advance/stop logic
10. Schema 3 report generation
"""

import os
import sys
import json
import stat
import tempfile
import unittest
from fractions import Fraction
import subprocess

from campaign2.model_provider import (
    ModelProvider,
    MockModelProvider,
    MockTaskEvaluator,
    SolModelProvider,
)
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
from campaign2.ladder import evaluate_pilot_outcome_ladder, FLOOR_THRESHOLD
from campaign2.reporting import generate_schema3_report, publish_schema3_report
from campaign2.proxy import InfrastructureError

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


class TestMockModelProvider(unittest.TestCase):
    """Test MockModelProvider and MockTaskEvaluator."""

    def test_generate_and_mutate_hash_predictability(self):
        provider = MockModelProvider()
        code1, t1 = provider.generate_candidate("task_a", "prompt1", "c1")
        code2, t2 = provider.generate_candidate("task_a", "prompt1", "c2")

        self.assertIn("solve()", code1)
        self.assertEqual(t1, 100)
        self.assertEqual(len(provider.call_log), 2)

        mut_body, mt = provider.mutate_prompt("body1", "error trace", "m1")
        self.assertIn("guided=True", mut_body)
        self.assertEqual(mt, 200)

    def test_mock_evaluator_score_override(self):
        evaluator = MockTaskEvaluator(default_earned=5, default_max=10)
        e1, m1 = evaluator.evaluate("task_a", "def solve(): pass")
        self.assertEqual((e1, m1), (5, 10))

        evaluator.set_score("task_a", 9, 10)
        e2, m2 = evaluator.evaluate("task_a", "def solve(): pass")
        self.assertEqual((e2, m2), (9, 10))

    def test_sol_provider_raises_not_implemented(self):
        sol = SolModelProvider()
        with self.assertRaises(NotImplementedError):
            sol.generate_candidate("task_a", "prompt", "c1")
        with self.assertRaises(NotImplementedError):
            sol.mutate_prompt("body", "fail", "c2")


class TestCampaign2Orchestrator(unittest.TestCase):
    """Test Campaign2Orchestrator execution, call arithmetic, and trio adjacency."""

    @classmethod
    def setUpClass(cls):
        generate_master_registration(BASE_DIR)
        cls.ta_data = generate_family_blocked_assignment()
        cls.reg_data = verify_master_registration(BASE_DIR)

    def test_suite_p_pilot_call_arithmetic_and_trio_adjacency(self):
        provider = MockModelProvider()
        evaluator = MockTaskEvaluator()

        orch = Campaign2Orchestrator(
            model_provider=provider,
            task_evaluator=evaluator,
            tier_assignment=self.ta_data,
            registration=self.reg_data,
            suite="suite_p",
            workspace_root=BASE_DIR,
        )

        results = orch.run_all_trials()
        self.assertEqual(len(results), 2)

        # 86 calls per trial in Suite P (8 P1 + 54 P2 + 24 P3 = 86)
        for trial_res in results:
            self.assertEqual(trial_res.model_call_count(), 86)

        # Trio adjacency
        self.assertTrue(orch.verify_trio_adjacency())

        outcome = orch.compute_outcome()
        self.assertEqual(outcome["schema_version"], "3")
        self.assertEqual(outcome["suite"], "suite_p")
        self.assertEqual(outcome["num_trials"], 2)

    def test_suite_c_full_run_single_trial_call_arithmetic(self):
        provider = MockModelProvider()
        evaluator = MockTaskEvaluator()

        orch = Campaign2Orchestrator(
            model_provider=provider,
            task_evaluator=evaluator,
            tier_assignment=self.ta_data,
            registration=self.reg_data,
            suite="suite_c",
            workspace_root=BASE_DIR,
        )

        trial_res = orch.run_trial(0)
        # 202 calls in Suite C Phase 1 (16) + Phase 2 (102) + Phase 3 (84)
        self.assertEqual(trial_res.model_call_count(), 202)
        self.assertTrue(orch.verify_trio_adjacency())

    def test_trio_adjacency_error_detection(self):
        provider = MockModelProvider()
        evaluator = MockTaskEvaluator()

        orch = Campaign2Orchestrator(
            model_provider=provider,
            task_evaluator=evaluator,
            tier_assignment=self.ta_data,
            registration=self.reg_data,
            suite="suite_p",
            workspace_root=BASE_DIR,
        )

        orch.run_trial(0)
        # Corrupt one trio in Phase 3
        records = orch.trial_results[0].call_records
        phase3_idx = [i for i, r in enumerate(records) if r.phase == 3]
        if phase3_idx:
            records[phase3_idx[0]].trio_position = 99  # Corrupt position sequence

        with self.assertRaises(TrioAdjacencyError):
            orch.verify_trio_adjacency()


class TestLaunchLock(unittest.TestCase):
    """Test launch lock generation and strict verification."""

    @classmethod
    def setUpClass(cls):
        generate_master_registration(BASE_DIR)

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="rsi_lock_test_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_launch_lock_git_head_authentication(self):
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
        lock_file = os.path.join(self.tmp_dir, "campaign2_launch_lock.json")
        bad_lock = {
            "schema_version": "3",
            "canonical_realpath": os.path.realpath(self.tmp_dir),
            "approved_commit_sha": "0" * 40,
            "registration_hash": "",  # EMPTY
            "configuration_hash": "a" * 64,
        }
        with open(lock_file, "w") as f:
            json.dump(bad_lock, f)
        os.chmod(lock_file, 0o444)

        with self.assertRaises(LaunchLockError):
            verify_launch_lock(self.tmp_dir)


class TestAttemptLifecycleAndDurability(unittest.TestCase):
    """Test attempt lifecycle, checkpointing, and fault classification."""

    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp(prefix="rsi_lifecycle_test_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_scientific_start_publishing_and_resume(self):
        ckpt_path = os.path.join(self.tmp_dir, "checkpoint.json")

        # Initial check: no marker exists
        res = check_and_resume("pilot", ckpt_path, workspace_root=self.tmp_dir)
        self.assertIsNone(res)

        # Publish marker
        mpath = publish_scientific_start("pilot", workspace_root=self.tmp_dir)
        self.assertTrue(os.path.exists(mpath))

        # Re-check without checkpoint -> should raise AttemptInvalidatedError
        with self.assertRaises(AttemptInvalidatedError):
            check_and_resume("pilot", ckpt_path, workspace_root=self.tmp_dir)

        # Create valid checkpoint
        ckpt_mgr = CheckpointManager(ckpt_path, "pilot")
        ckpt_mgr.seal_checkpoint_intent(0)
        ckpt_mgr.commit_checkpoint_head(0, {"earned": 5})

        # Re-check with valid checkpoint -> returns checkpoint data
        res2 = check_and_resume("pilot", ckpt_path, workspace_root=self.tmp_dir)
        self.assertIsNotNone(res2)
        self.assertEqual(res2["stage"], "pilot")

    def test_fault_classification(self):
        f1 = classify_call_fault(ConnectionRefusedError("Connection refused"))
        self.assertTrue(f1.retryable)
        self.assertEqual(f1.fault_type, "connection_refused")

        f2 = classify_call_fault(json.JSONDecodeError("Expecting value", "", 0))
        self.assertFalse(f2.retryable)
        self.assertEqual(f2.fault_type, "parse_error")

        f3 = classify_call_fault(InfrastructureError("Sandbox failed"))
        self.assertFalse(f3.retryable)
        self.assertEqual(f3.fault_type, "infrastructure")

    def test_ledger_intent_before_completion(self):
        ledger_path = os.path.join(self.tmp_dir, "ledger.json")
        ledger = AttemptLedger(ledger_path)

        ledger.record_intent("call_001", "generate", "optimizer", 1, "task_a")
        ledger.record_completion("call_001", True, tokens=100)

        self.assertTrue(ledger.verify_intent_before_completion())

        # Record completion without prior intent
        ledger.record_completion("call_002_orphan", True)
        self.assertFalse(ledger.verify_intent_before_completion())

    def test_durability_checkpoint_manager(self):
        ckpt_path = os.path.join(self.tmp_dir, "checkpoint.json")
        mgr = CheckpointManager(ckpt_path, "pilot")

        self.assertEqual(mgr.get_resume_trial_index(), 0)

        mgr.seal_checkpoint_intent(0)
        mgr.commit_checkpoint_head(0, {"summary": "trial_0_ok"})

        self.assertEqual(mgr.get_resume_trial_index(), 1)
        results = mgr.get_completed_results()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["summary"], "trial_0_ok")


class TestPilotOutcomeLadderAndReporting(unittest.TestCase):
    """Test outcome ladder floor checks, delta logic, and Schema 3 reporting."""

    def test_ladder_floor_checks_and_advance(self):
        # 1. Opt/sel floor failure for optimizer arm
        out1 = evaluate_pilot_outcome_ladder(
            opt_sel_opt_score=Fraction(1, 10),  # 0.1 < 0.2
            opt_sel_ctrl_score=Fraction(5, 10),
            conf_opt_score=Fraction(5, 10),
            conf_ctrl_score=Fraction(5, 10),
            holdout_opt_score=Fraction(5, 10),
            holdout_ctrl_score=Fraction(5, 10),
            delta=Fraction(0),
        )
        self.assertEqual(out1.decision, "stop")
        self.assertEqual(out1.reason, "opt_sel_floor")

        # 2. Confirmatory floor failure for control arm
        out2 = evaluate_pilot_outcome_ladder(
            opt_sel_opt_score=Fraction(5, 10),
            opt_sel_ctrl_score=Fraction(5, 10),
            conf_opt_score=Fraction(5, 10),
            conf_ctrl_score=Fraction(1, 10),  # 0.1 < 0.2
            holdout_opt_score=Fraction(5, 10),
            holdout_ctrl_score=Fraction(5, 10),
            delta=Fraction(0),
        )
        self.assertEqual(out2.decision, "stop")
        self.assertEqual(out2.reason, "conf_floor")

        # 3. Positive delta -> advance
        out3 = evaluate_pilot_outcome_ladder(
            opt_sel_opt_score=Fraction(5, 10),
            opt_sel_ctrl_score=Fraction(5, 10),
            conf_opt_score=Fraction(6, 10),
            conf_ctrl_score=Fraction(4, 10),
            holdout_opt_score=Fraction(6, 10),
            holdout_ctrl_score=Fraction(4, 10),
            delta=Fraction(2, 10),  # > 0
        )
        self.assertEqual(out3.decision, "advance")
        self.assertEqual(out3.reason, "positive_delta")

        # 4. Zero delta -> stop (no effect)
        out4 = evaluate_pilot_outcome_ladder(
            opt_sel_opt_score=Fraction(5, 10),
            opt_sel_ctrl_score=Fraction(5, 10),
            conf_opt_score=Fraction(5, 10),
            conf_ctrl_score=Fraction(5, 10),
            holdout_opt_score=Fraction(5, 10),
            holdout_ctrl_score=Fraction(5, 10),
            delta=Fraction(0),
        )
        self.assertEqual(out4.decision, "stop")
        self.assertEqual(out4.reason, "no_effect")

    def test_schema3_reporting(self):
        generate_master_registration(BASE_DIR)
        ta = generate_family_blocked_assignment()
        reg = verify_master_registration(BASE_DIR)

        orch = Campaign2Orchestrator(
            model_provider=MockModelProvider(),
            task_evaluator=MockTaskEvaluator(),
            tier_assignment=ta,
            registration=reg,
            suite="suite_p",
            workspace_root=BASE_DIR,
        )
        orch.run_all_trials()
        outcome = orch.compute_outcome()

        report = generate_schema3_report(outcome, reg, ta, BASE_DIR)
        self.assertEqual(report["schema_version"], "3")
        self.assertEqual(report["suite"], "suite_p")
        self.assertIn("primary_outcome", report)
        self.assertEqual(len(report["per_trial"]), 2)
        self.assertEqual(len(report["known_limitations"]), 11)


if __name__ == "__main__":
    unittest.main()
