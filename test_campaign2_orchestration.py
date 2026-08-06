"""
Unit and Integration Tests for Campaign 2 Orchestration System (Round 7 Remediation).

Mechanical Traceability Matrix:
- TestPilotOutcomeLadder: Literal Frozen B8 6-outcome ladder
- TestCheckpointManagerDurability: True B5 exact-head durability with seal_pre_trial_intent and state anchor rollback rejection
- TestCLIResumeNamespace: CLI --resume argument and pre-execution ledger admission verification
- TestWinnerSelectionEqualTaskWeighting: Equal-task weighted winner selection across valid task IDs with unequal maxima
- TestArmSchedulingCounterbalance: Counterbalanced arm scheduling (arm_sign -1 runs Control 1st, Optimizer 2nd)
- TestRetryTaxonomyAndLedger: Complete pre-request and HTTP 429 zero response-body retry taxonomy
- TestFullReportEquivalence: Canonical Schema 3 report equality between uninterrupted and resumed runs
- TestReportingStructure: Task-derived per-family deltas without aliasing
- TestMasterRegistrationHashVerification: Exact Limitation 10 campaign-fatal text verification
"""

import os
import sys
import json
import stat
import hashlib
import tempfile
import unittest
import subprocess
from fractions import Fraction
from unittest import mock
from types import SimpleNamespace

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
from campaign2.tier_assignment import (
    generate_family_blocked_assignment,
    write_sealed_family_blocked_assignment,
)
from campaign2.orchestrator import (
    Campaign2Orchestrator,
    CallArithmeticError,
    TrioAdjacencyError,
    OrchestratorError,
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
    verify_stage_scientific_start,
    publish_sealed_invalidation,
    is_sealed_invalidated,
    check_and_resume,
    classify_call_fault,
    AttemptLedger,
    CallFault,
    AttemptLifecycleError,
    AttemptInvalidatedError,
)
from campaign2.durability import CheckpointManager, DurabilityError, GENESIS_HEAD_HASH
from campaign2.attempt_identity import (
    create_attempt_identity,
    resolve_resume_attempt,
    verify_attempt_identity,
    AttemptIdentityError,
)
from campaign2.ladder import (
    evaluate_pilot_outcome_ladder,
    ENGAGEMENT_FLOOR,
    OPT_SEL_HEADROOM_CEILING,
    CONF_HEADROOM_CEILING,
)
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

        clean = envelope.validate_mutation_output("  Focus on boundary conditions.  ")
        self.assertEqual(clean, "Focus on boundary conditions.")

        with self.assertRaises(PromptEnvelopeError):
            envelope.validate_mutation_output("Strategy text ---STRATEGY--- escape attempt")

        with self.assertRaises(PromptEnvelopeError):
            envelope.validate_mutation_output("```python\ndef solve(): pass\n```")

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
        with self.assertRaises(NotImplementedError):
            SolModelProvider()


class TestCampaign2Orchestrator(unittest.TestCase):
    """Test Campaign2Orchestrator execution, 202/86 call arithmetic, arm vectors, and trios."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.workspace = self.tmp_dir.name
        generate_master_registration(self.workspace)
        self.reg_data = verify_master_registration(self.workspace)
        self.ta_data = generate_family_blocked_assignment(base_commit_sha=self.reg_data["base_commit_sha"])

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_suite_p_pilot_call_arithmetic_86_calls(self):
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
        self.assertTrue(ledger.verify_all_intents_completed())

        outcome = orch.compute_outcome()
        self.assertEqual(outcome["suite"], "suite_p")

    def test_suite_c_full_call_arithmetic_202_calls(self):
        ledger_path = os.path.join(self.workspace, "ledger_c.jsonl")
        ledger = AttemptLedger(ledger_path)

        orch = Campaign2Orchestrator(
            model_provider=MockModelProvider(),
            task_evaluator=MockTaskEvaluator(),
            tier_assignment=self.ta_data,
            registration=self.reg_data,
            suite="suite_c",
            ledger=ledger,
            workspace_root=self.workspace,
        )

        res0 = orch.run_trial(0)
        self.assertEqual(res0.model_call_count(), 202)
        self.assertTrue(orch.verify_trio_adjacency())


class TestArmSchedulingCounterbalance(unittest.TestCase):
    """Test counterbalanced arm scheduling under +1 and -1 arm assignment signs."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.workspace = self.tmp_dir.name
        generate_master_registration(self.workspace)
        self.reg_data = verify_master_registration(self.workspace)
        self.ta_data = generate_family_blocked_assignment(base_commit_sha=self.reg_data["base_commit_sha"])

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_arm_scheduling_counterbalance(self):
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

        res0 = orch.run_trial(0)  # arm_sign +1 -> slot_a = Optimizer (1st), slot_b = Control (2nd)
        res1 = orch.run_trial(1)  # arm_sign -1 -> slot_a = Control (1st), slot_b = Optimizer (2nd)

        p2_calls_0 = [r for r in res0.call_records if r.phase == 2]
        p2_calls_1 = [r for r in res1.call_records if r.phase == 2]

        self.assertEqual(p2_calls_0[0].arm, "slot_a")
        self.assertEqual(p2_calls_1[0].arm, "slot_a")
        outcome = orch.compute_outcome()
        self.assertEqual(outcome["per_trial"][0]["optimizer_slot"], "slot_a")
        self.assertEqual(outcome["per_trial"][0]["control_slot"], "slot_b")
        self.assertEqual(outcome["per_trial"][1]["optimizer_slot"], "slot_b")
        self.assertEqual(outcome["per_trial"][1]["control_slot"], "slot_a")


class TestWinnerSelectionEqualTaskWeighting(unittest.TestCase):
    """Test equal-task weighted winner selection using valid task IDs with unequal maxima."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.workspace = self.tmp_dir.name
        generate_master_registration(self.workspace)
        self.reg_data = verify_master_registration(self.workspace)
        self.ta_data = generate_family_blocked_assignment(base_commit_sha=self.reg_data["base_commit_sha"])

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_equal_task_weighting_valid_task_ids(self):
        class SlotProvider(MockModelProvider):
            def generate_candidate(self, task_id, effective_prompt, call_id):
                return call_id, 1

        task_ids = sorted(
            self.ta_data["set_assignments"]["suite_p"]["optimization"]
            + self.ta_data["set_assignments"]["suite_p"]["selection"]
        )

        class UnequalMaxEvaluator:
            def evaluate(self, task_id, candidate_code):
                if "gen_baseline_" in candidate_code:
                    return 0, 1, True, ""
                if "gen_slot_a_" in candidate_code:
                    return (1000, 1000, True, "") if task_id == task_ids[0] else (0, 1, True, "")
                if "gen_slot_b_" in candidate_code:
                    return (0, 1000, True, "") if task_id == task_ids[0] else (1, 1, True, "")
                return 0, 1, True, ""

        reg_mod = dict(self.reg_data)
        reg_mod["arm_assignment_vectors"] = [+1, -1]

        orch = Campaign2Orchestrator(
            model_provider=SlotProvider(),
            task_evaluator=UnequalMaxEvaluator(),
            tier_assignment=self.ta_data,
            registration=reg_mod,
            suite="suite_p",
            workspace_root=self.workspace,
        )

        res = orch.run_trial(0)
        self.assertEqual(res.phase2_winner, "slot_b")
        self.assertTrue(all(task_id.startswith("p_f") for task_id in task_ids))

    def test_optimizer_feedback_contains_evaluator_error_traces(self):
        class TraceProvider(MockModelProvider):
            def __init__(self):
                super().__init__()
                self.feedback = []
            def mutate_prompt(self, mutable_body, failure_context, call_id):
                self.feedback.append(failure_context)
                return super().mutate_prompt(mutable_body, failure_context, call_id)

        class TraceEvaluator:
            def evaluate(self, task_id, candidate_code):
                return 0, 10, False, f"AssertionError: {task_id} boundary mismatch"

        provider = TraceProvider()
        orch = Campaign2Orchestrator(
            model_provider=provider,
            task_evaluator=TraceEvaluator(),
            tier_assignment=self.ta_data,
            registration=self.reg_data,
            suite="suite_p",
            workspace_root=self.workspace,
        )
        result = orch.run_trial(0)
        self.assertTrue(any("AssertionError:" in feedback for feedback in provider.feedback))
        self.assertTrue(any(record.error_trace for record in result.call_records if record.call_type == "generate"))


class TestCheckpointManagerDurability(unittest.TestCase):
    """Test true B5 exact-head durability with seal_pre_trial_intent, commit_intent, and state anchor rollback rejection."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.workspace = self.tmp_dir.name
        self.ckpt_path = os.path.join(self.workspace, "ckpt.json")

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_exact_head_commit_and_rollback_rejection(self):
        mgr = CheckpointManager(self.ckpt_path, "pilot")

        # 1. Seal pre-trial intent
        intent_p = mgr.seal_pre_trial_intent(0)
        self.assertTrue(os.path.exists(intent_p))
        self.assertEqual(stat.S_IMODE(os.stat(intent_p).st_mode), 0o444)

        # 2. Commit trial 0
        h0 = mgr.commit_checkpoint_head(0, {"trial_index": 0, "score": "1/2"})
        self.assertEqual(stat.S_IMODE(os.stat(self.ckpt_path).st_mode), 0o444)
        self.assertTrue(mgr.verify_checkpoint_head_chain())

        # Save copy of head 0 checkpoint
        with open(self.ckpt_path, "r", encoding="utf-8") as f:
            h0_ckpt_data = f.read()

        # 3. Commit trial 1
        mgr.seal_pre_trial_intent(1)
        h1 = mgr.commit_checkpoint_head(1, {"trial_index": 1, "score": "1/2"})
        self.assertNotEqual(h0, h1)

        # 4. Attempt to restore head 0 checkpoint over current head 1
        os.chmod(self.ckpt_path, 0o644)
        with open(self.ckpt_path, "w", encoding="utf-8") as f:
            f.write(h0_ckpt_data)
        os.chmod(self.ckpt_path, 0o444)

        # Fresh CheckpointManager rejects restored older head during admission.
        with self.assertRaises(DurabilityError):
            CheckpointManager(self.ckpt_path, "pilot")

    def test_commit_intent_recovers_every_post_execution_boundary_without_resampling(self):
        class InjectedCrash(RuntimeError):
            pass

        class FaultingManager(CheckpointManager):
            boundary = ""
            def _fault_hook(self, boundary):
                if boundary == self.boundary:
                    raise InjectedCrash(boundary)

        for boundary in (
            "after_commit_intent",
            "after_checkpoint_replace",
            "after_anchor_replace",
            "after_intent_cleanup",
        ):
            with self.subTest(boundary=boundary):
                run_dir = os.path.join(self.workspace, boundary)
                os.makedirs(run_dir)
                path = os.path.join(run_dir, "checkpoint.json")
                manager = FaultingManager(path, "pilot")
                manager.seal_pre_trial_intent(0)
                manager.boundary = boundary
                with self.assertRaises(InjectedCrash):
                    manager.commit_checkpoint_head(0, {"trial_index": 0, "score": "1/2"})
                if boundary == "after_commit_intent":
                    with self.assertRaises(DurabilityError):
                        CheckpointManager(path, "pilot")
                else:
                    recovered = CheckpointManager(path, "pilot")
                    self.assertEqual(recovered.read_checkpoint()["head_index"], 0)
                    self.assertEqual(recovered.get_resume_trial_index(), 1)
                    self.assertFalse(os.path.exists(path + ".commit_intent"))

    def test_residual_pre_trial_intent_is_ambiguous_and_never_rescheduled(self):
        manager = CheckpointManager(self.ckpt_path, "pilot")
        manager.seal_pre_trial_intent(0)
        with self.assertRaisesRegex(DurabilityError, "must be invalidated, not resampled"):
            CheckpointManager(self.ckpt_path, "pilot")

    def test_missing_checkpoint_with_anchor_refuses_genesis(self):
        CheckpointManager(self.ckpt_path, "pilot")
        os.chmod(self.ckpt_path, 0o600)
        os.unlink(self.ckpt_path)
        with self.assertRaisesRegex(DurabilityError, "refusing genesis"):
            CheckpointManager(self.ckpt_path, "pilot")

    def test_commit_intent_contains_exact_checkpoint_bytes_and_hash(self):
        class InjectedCrash(RuntimeError):
            pass
        class FaultingManager(CheckpointManager):
            def _fault_hook(self, boundary):
                if boundary == "after_commit_intent":
                    raise InjectedCrash(boundary)
        manager = FaultingManager(self.ckpt_path, "pilot")
        manager.seal_pre_trial_intent(0)
        with self.assertRaises(InjectedCrash):
            manager.commit_checkpoint_head(0, {"trial_index": 0})
        with open(self.ckpt_path + ".commit_intent", encoding="utf-8") as handle:
            intent = json.load(handle)
        self.assertIn("expected_checkpoint_bytes_b64", intent)
        self.assertEqual(len(intent["expected_checkpoint_sha256"]), 64)
        self.assertIsInstance(intent["expected_checkpoint_size"], int)

    def test_sealed_write_orders_fchmod_before_fsync(self):
        events = []
        real_fchmod = os.fchmod
        real_fsync = os.fsync
        with mock.patch("campaign2.durability.os.fchmod", side_effect=lambda fd, mode: (events.append("fchmod"), real_fchmod(fd, mode))[1]), \
             mock.patch("campaign2.durability.os.fsync", side_effect=lambda fd: (events.append("fsync"), real_fsync(fd))[1]):
            CheckpointManager(self.ckpt_path, "pilot")
        self.assertLess(events.index("fchmod"), events.index("fsync"))


class TestCLIResumeNamespace(unittest.TestCase):
    """Test CLI --resume argument registration and pre-execution ledger admission verification."""

    def test_cli_resume_argument_registered(self):
        from rsi_loop import build_arg_parser
        parser = build_arg_parser()
        args, unknown = parser.parse_known_args([
            "--campaign", "2", "--resume", "--resume-attempt-id", "run_exact_123"
        ])
        self.assertTrue(args.resume)
        self.assertEqual(args.resume_attempt_id, "run_exact_123")

    def test_resume_resolves_named_sealed_identity_not_newest_mtime(self):
        with tempfile.TemporaryDirectory() as history:
            first = os.path.join(history, "run_first")
            second = os.path.join(history, "run_second")
            os.makedirs(first)
            os.makedirs(second)
            create_attempt_identity(first, "run_first", "pilot", "suite_p", True, "a" * 64)
            create_attempt_identity(second, "run_second", "pilot", "suite_p", True, "a" * 64)
            os.utime(second, None)
            resolved, identity = resolve_resume_attempt(history, "run_first", "pilot", True)
            self.assertEqual(resolved, os.path.realpath(first))
            self.assertEqual(identity["attempt_id"], "run_first")

    def test_resume_identity_tamper_is_rejected(self):
        with tempfile.TemporaryDirectory() as history:
            run_dir = os.path.join(history, "run_tampered")
            os.makedirs(run_dir)
            create_attempt_identity(run_dir, "run_tampered", "pilot", "suite_p", True, "a" * 64)
            identity_path = os.path.join(run_dir, "campaign2_attempt_identity.json")
            os.chmod(identity_path, 0o600)
            with open(identity_path, "ab") as handle:
                handle.write(b" ")
            os.chmod(identity_path, 0o444)
            with self.assertRaises(AttemptIdentityError):
                verify_attempt_identity(run_dir)

    def test_ledger_admission_rejects_unmatched_intent_and_orphan_completion(self):
        with tempfile.TemporaryDirectory() as run_dir:
            ledger = AttemptLedger(os.path.join(run_dir, "ledger.jsonl"))
            ledger.record_intent("call_1", "generate", "slot_a", 2)
            with self.assertRaises(AttemptLifecycleError):
                ledger.verify_resume_admission()

    def test_real_campaign_entry_admits_ledger_before_provider_construction(self):
        import rsi_loop
        registration = verify_master_registration(BASE_DIR)
        assignment = generate_family_blocked_assignment(registration["base_commit_sha"])
        with open(os.path.join(BASE_DIR, "campaign2_master_registration.json"), "rb") as handle:
            registration_hash = hashlib.sha256(handle.read()).hexdigest()
        with tempfile.TemporaryDirectory() as history:
            run_id = "run_admission_order"
            run_dir = os.path.join(history, run_id)
            os.makedirs(run_dir)
            identity = create_attempt_identity(
                run_dir, run_id, "pilot", "suite_p", True, registration_hash
            )
            CheckpointManager(os.path.join(run_dir, identity["checkpoint_file"]), "pilot")
            ledger = AttemptLedger(os.path.join(run_dir, identity["ledger_file"]))
            ledger.record_intent("ambiguous_1", "generate", "slot_a", 2)
            ctx = rsi_loop.RunContext()
            ctx.run_id = run_id
            ctx.run_dir = run_dir
            args = SimpleNamespace(stage="pilot", mock=True, resume=True)
            patches = (
                mock.patch("campaign2.manifest.verify_campaign2_manifest"),
                mock.patch("campaign2.manifest.verify_campaign2_evidence_manifest"),
                mock.patch("campaign2.manifest.verify_campaign2_parity_matrix"),
                mock.patch("campaign2.tier_assignment.verify_family_blocked_assignment"),
                mock.patch("campaign2.tier_assignment.generate_family_blocked_assignment", return_value=assignment),
                mock.patch("campaign2.registration.verify_master_registration", return_value=registration),
                mock.patch("campaign2.model_provider.MockModelProvider"),
            )
            entered = [patcher.start() for patcher in patches]
            try:
                provider_constructor = entered[-1]
                with self.assertRaises(AttemptLifecycleError):
                    rsi_loop._run_campaign2(args, ctx)
                provider_constructor.assert_not_called()
            finally:
                for patcher in reversed(patches):
                    patcher.stop()
        with tempfile.TemporaryDirectory() as run_dir:
            ledger = AttemptLedger(os.path.join(run_dir, "ledger.jsonl"))
            ledger.record_completion("call_2", True)
            with self.assertRaises(AttemptLifecycleError):
                ledger.verify_resume_admission()

    def test_terminal_attempt_resume_admission_rejected(self):
        with tempfile.TemporaryDirectory() as run_dir:
            ledger = AttemptLedger(os.path.join(run_dir, "ledger.jsonl"))
            ledger.record_attempt_start("att_1", "pilot")
            ledger.record_terminal_classification("att_1", "completed", "Finished pilot")
            with self.assertRaises(AttemptLifecycleError):
                ledger.verify_resume_admission()


class TestAttemptAccounting(unittest.TestCase):
    """Test attempt start and terminal classification ledger accounting."""

    def test_attempt_start_terminal_coverage(self):
        with tempfile.TemporaryDirectory() as base_tmp:
            run_dir = os.path.realpath(os.path.join(base_tmp, "run_test_acc_cov"))
            os.makedirs(run_dir)
            run_id = os.path.basename(run_dir)
            ctx = SimpleNamespace(run_dir=run_dir, run_id=run_id)
            args = SimpleNamespace(stage="pilot", mock=True, resume=False)
            reg_p, anchor_p = generate_master_registration(run_dir)

            ledger_path = os.path.join(run_dir, "campaign2_pilot_ledger.jsonl")
            ledger = AttemptLedger(ledger_path)
            ledger.record_attempt_start(run_id, "pilot")
            ledger.record_intent("call_unmatched_1", "mutate", "slot_a", 2)

            import rsi_loop
            with mock.patch("campaign2.durability.CheckpointManager", side_effect=DurabilityError("Injected checkpoint recovery failure")):
                with self.assertRaises(DurabilityError):
                    rsi_loop._run_campaign2(args, ctx)

            ledger_reload = AttemptLedger(ledger_path)
            entries = ledger_reload.get_entries()

            types = [e.get("record_type") for e in entries]
            self.assertIn("attempt_start", types)
            self.assertIn("terminal_classification", types)
            term_entry = [e for e in entries if e.get("record_type") == "terminal_classification"][0]
            self.assertEqual(term_entry["classification"], "invalidated")
            self.assertGreater(ledger_reload.unknown_usage_count, 0)

    def test_terminal_attempt_resume_immutable(self):
        with tempfile.TemporaryDirectory() as base_tmp:
            run_dir = os.path.realpath(os.path.join(base_tmp, "run_test_term_immut"))
            os.makedirs(run_dir)
            run_id = os.path.basename(run_dir)
            ctx = SimpleNamespace(run_dir=run_dir, run_id=run_id)
            args = SimpleNamespace(stage="pilot", mock=True, resume=True)
            reg_p, anchor_p = generate_master_registration(run_dir)

            with open(reg_p, "rb") as f:
                reg_sha = hashlib.sha256(f.read()).hexdigest()
            create_attempt_identity(run_dir, run_id, "pilot", "suite_p", True, reg_sha)

            ledger_path = os.path.join(run_dir, "campaign2_pilot_ledger.jsonl")
            ledger = AttemptLedger(ledger_path)
            ledger.record_attempt_start(run_id, "pilot")
            ledger.record_terminal_classification(run_id, "completed", "Ladder decision: advance")

            initial_entries = ledger.get_entries()
            initial_count = len(initial_entries)

            import rsi_loop
            with self.assertRaises(AttemptLifecycleError):
                rsi_loop._run_campaign2(args, ctx)

            after_entries = ledger.get_entries()
            self.assertEqual(len(after_entries), initial_count)
            term_entries = [e for e in after_entries if e.get("record_type") == "terminal_classification"]
            self.assertEqual(len(term_entries), 1)
            self.assertEqual(term_entries[0]["classification"], "completed")

    def test_malformed_ledger_unmutated_on_repeated_launches(self):
        with tempfile.TemporaryDirectory() as base_tmp:
            run_dir = os.path.realpath(os.path.join(base_tmp, "run_test_corrupt_ledger"))
            os.makedirs(run_dir)
            run_id = os.path.basename(run_dir)
            ctx = SimpleNamespace(run_dir=run_dir, run_id=run_id)
            args = SimpleNamespace(stage="pilot", mock=True, resume=True)
            reg_p, anchor_p = generate_master_registration(run_dir)

            with open(reg_p, "rb") as f:
                reg_sha = hashlib.sha256(f.read()).hexdigest()
            create_attempt_identity(run_dir, run_id, "pilot", "suite_p", True, reg_sha)

            ledger_path = os.path.join(run_dir, "campaign2_pilot_ledger.jsonl")
            corrupt_bytes = b'{"record_type": "attempt_start", "attempt_id": "' + run_id.encode("utf-8") + b'", "stage": "pilot"}\n{"record_type": "intent", "call_id": "bad_json_line\n'
            with open(ledger_path, "wb") as f:
                f.write(corrupt_bytes)
            initial_sha = hashlib.sha256(corrupt_bytes).hexdigest()

            import rsi_loop
            # First launch attempt on corrupt ledger -> raises AttemptLifecycleError and publishes sealed invalidation
            with self.assertRaises(AttemptLifecycleError):
                rsi_loop._run_campaign2(args, ctx)

            with open(ledger_path, "rb") as f:
                after_launch1_bytes = f.read()
            self.assertEqual(hashlib.sha256(after_launch1_bytes).hexdigest(), initial_sha, "First launch must NOT mutate corrupted ledger")

            invalidated_marker = os.path.join(run_dir, "campaign2_pilot_invalidated.json")
            self.assertTrue(os.path.exists(invalidated_marker))
            st_mode = stat.S_IMODE(os.stat(invalidated_marker).st_mode)
            self.assertEqual(st_mode, 0o444)

            # Second launch attempt -> rejects immediately without touching ledger
            with self.assertRaises(AttemptLifecycleError):
                rsi_loop._run_campaign2(args, ctx)

            with open(ledger_path, "rb") as f:
                after_launch2_bytes = f.read()
            self.assertEqual(hashlib.sha256(after_launch2_bytes).hexdigest(), initial_sha, "Second launch must NOT mutate corrupted ledger")


class ImprovingMockModelProvider(MockModelProvider):
    """Mock model provider that marks candidates generated from mutated prompts."""
    def generate_candidate(self, task_id: str, effective_prompt: str, call_id: str) -> Tuple[str, int]:
        code, tokens = super().generate_candidate(task_id, effective_prompt, call_id)
        if "Mutation" in effective_prompt or "guided=" in effective_prompt:
            code += "\n# Mutation Candidate"
        return code, tokens


class DynamicMockTaskEvaluator:
    """Mock evaluator that returns higher score for mutated candidate code."""
    def __init__(self, base_earned: int = 2, mutated_earned: int = 8, max_score: int = 10):
        self.base_earned = base_earned
        self.mutated_earned = mutated_earned
        self.max_score = max_score

    def evaluate(self, task_id: str, candidate_code: str) -> Tuple[int, int, bool, str]:
        if "Mutation Candidate" in candidate_code:
            return self.mutated_earned, self.max_score, True, ""
        return self.base_earned, self.max_score, True, ""


class TestFullReportEquivalence(unittest.TestCase):
    """Test canonical Schema 3 report equality between uninterrupted and resumed runs."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.workspace = self.tmp_dir.name
        generate_master_registration(self.workspace)
        self.reg_data = verify_master_registration(self.workspace)
        self.ta_data = generate_family_blocked_assignment(base_commit_sha=self.reg_data["base_commit_sha"])

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_canonical_report_equality(self):
        u_dir = os.path.join(self.workspace, "uninterrupted")
        os.makedirs(u_dir)
        u_ledger = AttemptLedger(os.path.join(u_dir, "ledger.jsonl"))
        u_ckpt = CheckpointManager(os.path.join(u_dir, "ckpt.json"), "pilot")

        u_orch = Campaign2Orchestrator(
            model_provider=MockModelProvider(),
            task_evaluator=MockTaskEvaluator(),
            tier_assignment=self.ta_data,
            registration=self.reg_data,
            suite="suite_p",
            ledger=u_ledger,
            checkpoint_mgr=u_ckpt,
            workspace_root=self.workspace,
        )
        u_orch.run_all_trials()
        u_outcome = u_orch.compute_outcome()

        u_report = generate_schema3_report(
            u_outcome, self.reg_data, self.ta_data,
            ledger_path=os.path.join(u_dir, "ledger.jsonl"),
            workspace_root=self.workspace,
            is_mock=True,
        )

        r_dir = os.path.join(self.workspace, "resumed")
        os.makedirs(r_dir)
        r_ledger1 = AttemptLedger(os.path.join(r_dir, "ledger.jsonl"))
        r_ckpt1 = CheckpointManager(os.path.join(r_dir, "ckpt.json"), "pilot")

        r_orch1 = Campaign2Orchestrator(
            model_provider=MockModelProvider(),
            task_evaluator=MockTaskEvaluator(),
            tier_assignment=self.ta_data,
            registration=self.reg_data,
            suite="suite_p",
            ledger=r_ledger1,
            checkpoint_mgr=r_ckpt1,
            workspace_root=self.workspace,
        )
        t0_res = r_orch1.run_trial(0)
        r_ckpt1.seal_pre_trial_intent(0)
        r_ckpt1.commit_checkpoint_head(0, t0_res.to_dict())

        r_ledger2 = AttemptLedger(os.path.join(r_dir, "ledger.jsonl"))
        r_ckpt2 = CheckpointManager(os.path.join(r_dir, "ckpt.json"), "pilot")

        r_orch2 = Campaign2Orchestrator(
            model_provider=MockModelProvider(),
            task_evaluator=MockTaskEvaluator(),
            tier_assignment=self.ta_data,
            registration=self.reg_data,
            suite="suite_p",
            ledger=r_ledger2,
            checkpoint_mgr=r_ckpt2,
            workspace_root=self.workspace,
        )
        r_orch2.run_all_trials()
        r_outcome = r_orch2.compute_outcome()

        r_report = generate_schema3_report(
            r_outcome, self.reg_data, self.ta_data,
            ledger_path=os.path.join(r_dir, "ledger.jsonl"),
            workspace_root=self.workspace,
            is_mock=True,
        )

        def sanitize_report(rep):
            r = dict(rep)
            r["timestamp_iso"] = "SANITIZED"
            r["ledger_file"] = "SANITIZED"
            return r

        u_canonical = sanitize_report(u_report)
        r_canonical = sanitize_report(r_report)

        self.assertEqual(u_canonical, r_canonical)

    def test_canonical_report_equality_with_improving_mutations(self):
        u_dir = os.path.join(self.workspace, "u_imp")
        os.makedirs(u_dir)
        u_ledger = AttemptLedger(os.path.join(u_dir, "ledger.jsonl"))
        u_ckpt = CheckpointManager(os.path.join(u_dir, "ckpt.json"), "pilot")

        u_orch = Campaign2Orchestrator(
            model_provider=ImprovingMockModelProvider(),
            task_evaluator=DynamicMockTaskEvaluator(base_earned=2, mutated_earned=8),
            tier_assignment=self.ta_data,
            registration=self.reg_data,
            suite="suite_p",
            ledger=u_ledger,
            checkpoint_mgr=u_ckpt,
            workspace_root=self.workspace,
        )
        u_orch.run_all_trials()
        u_exploratory = u_orch.exploratory.summary()

        self.assertNotEqual(u_exploratory["score_improving_mutations_per_call"], "0/6")
        self.assertEqual(u_exploratory["score_improving_mutations_per_call"], "2/6")
        self.assertEqual(u_exploratory["improvement_per_1000_tokens"], "0.1087")
        self.assertEqual(u_orch.exploratory.total_output_tokens, 18400)
        self.assertEqual(u_orch.exploratory.optimizer_mutations_total, 6)

        r_dir = os.path.join(self.workspace, "r_imp")
        os.makedirs(r_dir)
        r_ledger1 = AttemptLedger(os.path.join(r_dir, "ledger.jsonl"))
        r_ckpt1 = CheckpointManager(os.path.join(r_dir, "ckpt.json"), "pilot")

        r_orch1 = Campaign2Orchestrator(
            model_provider=ImprovingMockModelProvider(),
            task_evaluator=DynamicMockTaskEvaluator(base_earned=2, mutated_earned=8),
            tier_assignment=self.ta_data,
            registration=self.reg_data,
            suite="suite_p",
            ledger=r_ledger1,
            checkpoint_mgr=r_ckpt1,
            workspace_root=self.workspace,
        )
        r_res0 = r_orch1.run_trial(0)
        r_ckpt1.seal_pre_trial_intent(0)
        r_ckpt1.commit_checkpoint_head(0, r_res0.to_dict())

        r_ledger2 = AttemptLedger(os.path.join(r_dir, "ledger.jsonl"))
        r_ckpt2 = CheckpointManager(os.path.join(r_dir, "ckpt.json"), "pilot")

        r_orch2 = Campaign2Orchestrator(
            model_provider=ImprovingMockModelProvider(),
            task_evaluator=DynamicMockTaskEvaluator(base_earned=2, mutated_earned=8),
            tier_assignment=self.ta_data,
            registration=self.reg_data,
            suite="suite_p",
            ledger=r_ledger2,
            checkpoint_mgr=r_ckpt2,
            workspace_root=self.workspace,
        )
        r_orch2.run_all_trials()
        r_exploratory = r_orch2.exploratory.summary()

        self.assertEqual(u_exploratory, r_exploratory)


class TestMasterRegistrationHashVerification(unittest.TestCase):
    """Test verify_master_registration text recomputation and Known Limitation 10 text."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.workspace = self.tmp_dir.name
        generate_master_registration(self.workspace)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_known_limitation_10_exact_text(self):
        reg = verify_master_registration(self.workspace)
        lim10 = reg["known_limitations"][9]
        self.assertEqual(
            lim10,
            "A candidate that blocks without consuming CPU produces an unattributed wall-clock timeout "
            "and invalidates the attempt; phase-based exit attribution does not cover non-exiting processes.",
        )

    def test_registration_content_tamper_is_rejected(self):
        path = os.path.join(self.workspace, "campaign2_master_registration.json")
        os.chmod(path, 0o600)
        with open(path, "ab") as handle:
            handle.write(b" ")
        os.chmod(path, 0o444)
        with self.assertRaises(ValueError):
            verify_master_registration(self.workspace)


class TestIsolatedLaunchLock(unittest.TestCase):
    """Test launch lock generation and strict git repository authentication."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.workspace = self.tmp_dir.name
        subprocess.run(["git", "init"], cwd=self.workspace, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.workspace, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=self.workspace, capture_output=True, check=True)
        generate_master_registration(self.workspace)
        write_sealed_family_blocked_assignment(workspace_root=self.workspace)
        subprocess.run(["git", "add", "."], cwd=self.workspace, capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.workspace, capture_output=True, check=True)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_launch_lock_verification_in_git_workspace(self):
        lock_path = generate_launch_lock(self.workspace, lock_rationale="test lock")
        self.assertTrue(os.path.exists(lock_path))
        lock_data = verify_launch_lock(self.workspace)
        expected_sha = _git_head_sha(self.workspace)
        self.assertEqual(lock_data["approved_commit_sha"], expected_sha)

    def test_launch_lock_rejects_non_git_workspace(self):
        with tempfile.TemporaryDirectory() as non_git:
            generate_master_registration(non_git)
            write_sealed_family_blocked_assignment(workspace_root=non_git)
            with self.assertRaises(LaunchLockError):
                generate_launch_lock(non_git)


class TestPilotOutcomeLadder(unittest.TestCase):
    """Test pre-registered verbatim B8 6-outcome pilot ladder."""

    def test_literal_b8_ladder_6_outcomes(self):
        out1 = evaluate_pilot_outcome_ladder(
            opt_sel_baseline=Fraction(5, 10), conf_baseline=Fraction(5, 10),
            is_invalid=True, invalid_reason="Plumbing fault"
        )
        self.assertEqual(out1.decision, "invalid")

        out2 = evaluate_pilot_outcome_ladder(
            opt_sel_baseline=Fraction(1), conf_baseline=Fraction(5, 10),
            all_16_opt_sel_perfect=True
        )
        self.assertEqual(out2.decision, "no_headroom")

        out3 = evaluate_pilot_outcome_ladder(
            opt_sel_baseline=Fraction(91, 100), conf_baseline=Fraction(1, 2)
        )
        self.assertEqual(out3.decision, "insufficient_headroom")

        out4 = evaluate_pilot_outcome_ladder(
            opt_sel_baseline=Fraction(1, 2), conf_baseline=Fraction(24, 25)
        )
        self.assertEqual(out4.decision, "insufficient_confirmatory_headroom")

        out5 = evaluate_pilot_outcome_ladder(
            opt_sel_baseline=Fraction(19, 100), conf_baseline=Fraction(1, 2)
        )
        self.assertEqual(out5.decision, "insufficient_baseline_engagement")

        out6 = evaluate_pilot_outcome_ladder(
            opt_sel_baseline=Fraction(5, 10), conf_baseline=Fraction(5, 10)
        )
        self.assertEqual(out6.decision, "go")
        self.assertTrue(out6.is_go)


class TestRetryTaxonomyAndLedger(unittest.TestCase):
    """Test pre-request and HTTP 429 zero response-body retry taxonomy."""

    def test_http_retry_evidence_zero_response_body(self):
        # HTTP 429 with zero response body -> RETRYABLE
        f1 = classify_call_fault(RuntimeError("HTTP 429 Too Many Requests"), bytes_sent=100, bytes_received=0)
        self.assertTrue(f1.retryable)
        self.assertEqual(f1.fault_type, "rate_limit_pre_request")

        # HTTP 429 with received response body bytes -> NON-RETRYABLE
        f2 = classify_call_fault(RuntimeError("HTTP 429 Too Many Requests"), bytes_sent=100, bytes_received=256)
        self.assertFalse(f2.retryable)
        self.assertEqual(f2.fault_type, "rate_limit_with_body")

        # Unknown body evidence must never be manufactured as zero.
        unknown = classify_call_fault(RuntimeError("HTTP 429 Too Many Requests"), bytes_sent=100)
        self.assertFalse(unknown.retryable)

        retry_503 = classify_call_fault(RuntimeError("HTTP 503 Service Unavailable"), bytes_sent=100, bytes_received=0)
        self.assertTrue(retry_503.retryable)
        unknown_503 = classify_call_fault(RuntimeError("HTTP 503 Service Unavailable"), bytes_sent=100)
        self.assertFalse(unknown_503.retryable)

    def test_pre_request_dns_tls_spawn_require_explicit_zero_evidence(self):
        for exc in (
            RuntimeError("DNS name resolution failed"),
            RuntimeError("TLS handshake failed"),
            FileNotFoundError("spawn executable missing"),
        ):
            with self.subTest(exc=exc):
                explicit = classify_call_fault(exc, bytes_sent=0, bytes_received=0)
                self.assertTrue(explicit.retryable)
                unknown = classify_call_fault(exc)
                self.assertFalse(unknown.retryable)

    def test_orchestrator_does_not_retry_plain_http_text_without_body_evidence(self):
        class UnknownEvidenceProvider(MockModelProvider):
            def __init__(self):
                super().__init__()
                self.attempts = 0
            def generate_candidate(self, task_id, effective_prompt, call_id):
                self.attempts += 1
                raise RuntimeError("HTTP 429 Too Many Requests")

        with tempfile.TemporaryDirectory() as workspace:
            generate_master_registration(workspace)
            registration = verify_master_registration(workspace)
            assignment = generate_family_blocked_assignment(registration["base_commit_sha"])
            provider = UnknownEvidenceProvider()
            orch = Campaign2Orchestrator(
                provider, MockTaskEvaluator(), assignment, registration,
                suite="suite_p", workspace_root=workspace,
            )
            with self.assertRaises(OrchestratorError):
                orch._generate_and_evaluate(
                    "p_f1_02_syslog_framing_decoder", "body", "slot_a", 2, 0
                )
            self.assertEqual(provider.attempts, 1)

    def test_orchestrator_retries_only_explicit_zero_body_http_evidence(self):
        class ExplicitZeroError(RuntimeError):
            bytes_sent = 100
            bytes_received = 0

        class ExplicitProvider(MockModelProvider):
            def __init__(self):
                super().__init__()
                self.attempts = 0
            def generate_candidate(self, task_id, effective_prompt, call_id):
                self.attempts += 1
                if self.attempts == 1:
                    raise ExplicitZeroError("HTTP 503 Service Unavailable")
                return super().generate_candidate(task_id, effective_prompt, call_id)

        with tempfile.TemporaryDirectory() as workspace:
            generate_master_registration(workspace)
            registration = verify_master_registration(workspace)
            assignment = generate_family_blocked_assignment(registration["base_commit_sha"])
            provider = ExplicitProvider()
            orch = Campaign2Orchestrator(
                provider, MockTaskEvaluator(), assignment, registration,
                suite="suite_p", workspace_root=workspace,
            )
            earned, maximum, _tokens, _record = orch._generate_and_evaluate(
                "p_f1_02_syslog_framing_decoder", "body", "slot_a", 2, 0
            )
            self.assertEqual((earned, maximum), (5, 10))
            self.assertEqual(provider.attempts, 2)


class TestReportingStructure(unittest.TestCase):
    """Test task-derived per-family deltas labeled strictly by structural task families (F1-F4)."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.workspace = self.tmp_dir.name
        generate_master_registration(self.workspace)
        self.reg_data = verify_master_registration(self.workspace)
        self.ta_data = generate_family_blocked_assignment(base_commit_sha=self.reg_data["base_commit_sha"])

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_task_derived_family_deltas_no_aliasing(self):
        orch = Campaign2Orchestrator(
            model_provider=MockModelProvider(),
            task_evaluator=MockTaskEvaluator(),
            tier_assignment=self.ta_data,
            registration=self.reg_data,
            suite="suite_p",
            workspace_root=self.workspace,
        )
        orch.run_all_trials()
        outcome = orch.compute_outcome()

        report = generate_schema3_report(
            outcome, self.reg_data, self.ta_data, workspace_root=self.workspace, is_mock=True
        )

        per_family = report.get("per_family_deltas", [])
        self.assertEqual(len(per_family), 4)
        families = [f["family"] for f in per_family]
        self.assertEqual(families, ["F1", "F2", "F3", "F4"])
        self.assertEqual(per_family[0]["family_name"], "Structural Task Family 1")
        self.assertEqual(
            per_family[0]["task_ids"],
            ["p_f1_01_ndjson_stream_parser", "p_f1_02_syslog_framing_decoder"],
        )
        self.assertIn("p_f3_01_arc_adaptive_cache", per_family[2]["task_ids"])
        self.assertTrue(per_family[0]["optimizer_k_samples"])
        self.assertEqual(len(report["per_tier_deltas"]), 4)
        first_trial = report["per_trial"][0]
        self.assertEqual(set(first_trial["tiers"]), {
            "optimization", "selection", "confirmatory", "structural_holdout"
        })
        first_task = next(iter(first_trial["tiers"]["optimization"]["tasks"].values()))
        self.assertEqual(len(first_task["baseline_k_samples"]), 2)
        self.assertEqual(len(first_task["optimizer_k_samples"]), 2)
        self.assertEqual(len(first_task["control_k_samples"]), 2)
        self.assertTrue(first_trial["control_records"])

    def test_reporting_rejects_missing_family_evidence_instead_of_aliasing_tiers(self):
        outcome = {
            "suite": "suite_p",
            "num_trials": 1,
            "per_trial": [{
                "tiers": {
                    name: {"delta_opt_minus_ctrl": "0"}
                    for name in ("optimization", "selection", "confirmatory", "structural_holdout")
                }
            }],
        }
        with self.assertRaisesRegex(ValueError, "family_scores"):
            generate_schema3_report(outcome, self.reg_data, self.ta_data, workspace_root=self.workspace)


class TestStageGlobalOneShot(unittest.TestCase):
    """Test stage-global scientific start claim marker and anti-fishing duplicate attempt rejection."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.workspace = self.tmp_dir.name

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_duplicate_stage_start_rejected(self):
        p1 = publish_scientific_start("pilot", "attempt_1", workspace_root=self.workspace)
        self.assertTrue(os.path.exists(p1))
        self.assertTrue(verify_stage_scientific_start("pilot", "attempt_1", workspace_root=self.workspace))

        # Re-publishing with same attempt ID is idempotent
        p1_again = publish_scientific_start("pilot", "attempt_1", workspace_root=self.workspace)
        self.assertEqual(p1, p1_again)

        # Second attempt claiming same stage raises AttemptInvalidatedError under B4(b)
        with self.assertRaises(AttemptInvalidatedError):
            publish_scientific_start("pilot", "attempt_2", workspace_root=self.workspace)

        with self.assertRaises(AttemptInvalidatedError):
            verify_stage_scientific_start("pilot", "attempt_2", workspace_root=self.workspace)

    def test_scientific_start_strict_verification(self):
        p1 = publish_scientific_start("pilot", "attempt_1", workspace_root=self.workspace)

        # 1. Symlink rejection
        sym_path = p1 + "_sym"
        os.symlink(p1, sym_path)
        with mock.patch("os.path.join", return_value=sym_path):
            with self.assertRaises(AttemptInvalidatedError):
                verify_stage_scientific_start("pilot", "attempt_1", workspace_root=self.workspace)

        # 2. Mode 0o444 vs 0o644 rejection
        os.chmod(p1, 0o644)
        with self.assertRaises(AttemptInvalidatedError):
            verify_stage_scientific_start("pilot", "attempt_1", workspace_root=self.workspace)
        os.chmod(p1, 0o444)

        # 3. Noncanonical JSON byte formatting rejection
        data = {"stage": "pilot", "attempt_id": "attempt_1", "status": "started", "timestamp": 123}
        noncanonical_bytes = json.dumps(data).encode("utf-8")
        os.chmod(p1, 0o644)
        with open(p1, "wb") as f:
            f.write(noncanonical_bytes)
        os.chmod(p1, 0o444)

        with self.assertRaises(AttemptInvalidatedError):
            verify_stage_scientific_start("pilot", "attempt_1", workspace_root=self.workspace)

        with self.assertRaises(AttemptInvalidatedError):
            publish_scientific_start("pilot", "attempt_1", workspace_root=self.workspace)

        # 4. Stage mismatch rejection
        with self.assertRaises(AttemptInvalidatedError):
            verify_stage_scientific_start("full", "attempt_1", workspace_root=self.workspace)

        # 5. Status mismatch rejection
        os.chmod(p1, 0o644)
        mismatch_data = {"stage": "pilot", "attempt_id": "attempt_1", "status": "completed", "timestamp": 123}
        with open(p1, "w", encoding="utf-8") as f:
            json.dump(mismatch_data, f, indent=2, sort_keys=True)
            f.write("\n")
        os.chmod(p1, 0o444)

        with self.assertRaises(AttemptInvalidatedError):
            verify_stage_scientific_start("pilot", "attempt_1", workspace_root=self.workspace)


class TestUncommittedPreviousHeadDurability(unittest.TestCase):
    """Test fail-closed B5 recovery invalidation on uncommitted commit_intent pointing to previous head."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.workspace = self.tmp_dir.name
        self.ckpt_path = os.path.join(self.workspace, "ckpt.json")

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_uncommitted_previous_head_raises_durability_error(self):
        import base64
        mgr = CheckpointManager(self.ckpt_path, "pilot")
        mgr.seal_pre_trial_intent(0)

        trial_rec = {"trial_index": 0, "score": "1/2"}
        trial_bytes = json.dumps(trial_rec, sort_keys=True).encode("utf-8")
        trial_hash = hashlib.sha256(trial_bytes).hexdigest()
        new_head = hashlib.sha256(f"{GENESIS_HEAD_HASH}|0|{trial_hash}".encode("utf-8")).hexdigest()
        new_data = {
            "stage": "pilot",
            "version": 1,
            "head_index": 0,
            "previous_head_hash": GENESIS_HEAD_HASH,
            "trial_record_hash": trial_hash,
            "head_hash": new_head,
            "completed_trials": [trial_rec],
            "last_intent": None,
        }
        exp_bytes = (json.dumps(new_data, indent=2, sort_keys=True) + "\n").encode("utf-8")
        exp_sha = hashlib.sha256(exp_bytes).hexdigest()
        exp_b64 = base64.b64encode(exp_bytes).decode("ascii")

        c_intent_path = self.ckpt_path + ".commit_intent"
        c_intent_data = {
            "trial_index": 0,
            "previous_head_hash": GENESIS_HEAD_HASH,
            "expected_new_head_hash": new_head,
            "expected_checkpoint_sha256": exp_sha,
            "expected_checkpoint_size": len(exp_bytes),
            "expected_checkpoint_bytes_b64": exp_b64,
            "trial_record_hash": trial_hash,
            "status": "commit_intent_sealed",
        }
        with open(c_intent_path, "w", encoding="utf-8") as f:
            json.dump(c_intent_data, f)
        os.chmod(c_intent_path, 0o444)

        # Opening fresh CheckpointManager must raise DurabilityError under frozen B5 rule
        with self.assertRaises(DurabilityError):
            CheckpointManager(self.ckpt_path, "pilot")


class TestSigxcpuWorkerExit(unittest.TestCase):
    """Test SIGXCPU worker exit is scored as attributable candidate process error (0, max)."""

    def test_sigxcpu_raises_candidate_process_error(self):
        from campaign2.proxy import WorkerIPCClient, CandidateProcessError
        client = WorkerIPCClient.__new__(WorkerIPCClient)
        client.phase = "candidate_execution"
        with self.assertRaises(CandidateProcessError):
            client._raise_worker_exit(-24)


class TestB4AttemptAccounting(unittest.TestCase):
    """Test B4 attempt accounting methods in AttemptLedger."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.ledger_path = os.path.join(self.tmp_dir.name, "ledger.jsonl")

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_b4_attempt_accounting(self):
        ledger = AttemptLedger(self.ledger_path)
        ledger.record_attempt_start("att_1", "pilot")
        ledger.record_intent("call_1", "generate", "slot_a", 1)
        ledger.record_completion("call_1", True, tokens=150)
        ledger.record_terminal_classification("att_1", "completed", "All trials executed successfully")

        entries = ledger.get_entries()
        types = [e["record_type"] for e in entries]
        self.assertEqual(types, ["attempt_start", "intent", "completion", "terminal_classification"])
        self.assertEqual(ledger.cumulative_calls, 1)
        self.assertEqual(ledger.cumulative_tokens, 150)


class TestCandidateRlimits(unittest.TestCase):
    """Test candidate worker limits reporting in Schema 3 report."""

    def test_candidate_rlimits_schema3(self):
        from campaign2.reporting import get_candidate_rlimits
        rlimits = get_candidate_rlimits()
        self.assertEqual(rlimits["nofile"], 64)
        self.assertEqual(rlimits["fsize_bytes"], 1048576)
        self.assertEqual(rlimits["core_bytes"], 0)
        self.assertEqual(rlimits["cpu_seconds"], 15)


class TestStrictEvaluatorContract(unittest.TestCase):
    """Test strict 4-tuple requirement with exact types in orchestrator."""

    def test_evaluator_strict_contract_rejections(self):
        class InvalidEvaluator:
            def __init__(self, return_val):
                self.return_val = return_val
            def evaluate(self, task_id, code):
                return self.return_val

        with tempfile.TemporaryDirectory() as workspace:
            generate_master_registration(workspace)
            reg = verify_master_registration(workspace)
            assignment = generate_family_blocked_assignment(reg["base_commit_sha"])

            invalid_returns = [
                (5, 10),  # 2-tuple
                (5, 10, True),  # 3-tuple
                (5, 10, True, "", "extra"),  # 5-tuple
                (5.5, 10, True, ""),  # float score
                ("5", 10, True, ""),  # string score
                (True, 10, True, ""),  # bool as int score
                (5, 10.0, True, ""),  # float max_s
                (5, 10, "True", ""),  # non-bool validity
                (5, 10, True, 123),  # non-string error trace
                (15, 10, True, ""),  # score > max_s
                (-1, 10, True, ""),  # score < 0
            ]

            for ret in invalid_returns:
                orch = Campaign2Orchestrator(
                    MockModelProvider(), InvalidEvaluator(ret), assignment, reg,
                    suite="suite_p", workspace_root=workspace,
                )
                with self.assertRaises(OrchestratorError):
                    orch._generate_and_evaluate("p_f1_02_syslog_framing_decoder", "body", "slot_a", 2, 0)

            # Valid 4-tuple with exact types
            valid_eval = InvalidEvaluator((5, 10, True, ""))
            orch_valid = Campaign2Orchestrator(
                MockModelProvider(), valid_eval, assignment, reg,
                suite="suite_p", workspace_root=workspace,
            )
            earned, max_s, _toks, _rec = orch_valid._generate_and_evaluate(
                "p_f1_02_syslog_framing_decoder", "body", "slot_a", 2, 0
            )
            self.assertEqual((earned, max_s), (5, 10))


class TestSealedInvalidationSecurity(unittest.TestCase):
    """Test sealed invalidation marker publication, mode 0o444, non-symlink path containment, and identity authentication."""

    def test_sealed_invalidation_authentication_and_containment(self):
        with tempfile.TemporaryDirectory() as base_tmp:
            run_dir = os.path.realpath(os.path.join(base_tmp, "run_inv_test"))
            os.makedirs(run_dir)

            marker_path = publish_sealed_invalidation(run_dir, "pilot", "att_123", "Corrupt ledger")
            self.assertTrue(os.path.exists(marker_path))
            self.assertTrue(is_sealed_invalidated(run_dir, "pilot", "att_123"))

            # Attempt ID mismatch -> raises AttemptLifecycleError
            with self.assertRaises(AttemptLifecycleError):
                is_sealed_invalidated(run_dir, "pilot", "att_WRONG")

            # Exact mode 0o444
            mode = stat.S_IMODE(os.stat(marker_path).st_mode)
            self.assertEqual(mode, 0o444)

            # Symlink marker rejection -> raises AttemptLifecycleError
            sym_dir = os.path.realpath(os.path.join(base_tmp, "run_sym_test"))
            os.makedirs(sym_dir)
            target = os.path.join(sym_dir, "campaign2_pilot_invalidated.json")
            real_file = os.path.join(sym_dir, "real_target.json")
            with open(real_file, "w") as f:
                f.write('{\n  "attempt_id": "att_123",\n  "classification": "invalidated",\n  "record_type": "terminal_classification",\n  "stage": "pilot"\n}\n')
            os.chmod(real_file, 0o444)
            os.symlink(real_file, target)
            with self.assertRaises(AttemptLifecycleError):
                is_sealed_invalidated(sym_dir, "pilot", "att_123")

            # Missing or empty attempt_id -> raises ValueError
            with self.assertRaises(ValueError):
                is_sealed_invalidated(sym_dir, "pilot", "")

            # Tampered marker (wrong mode) -> raises AttemptLifecycleError
            tampered_dir = os.path.realpath(os.path.join(base_tmp, "run_tamp_mode"))
            os.makedirs(tampered_dir)
            t_path = publish_sealed_invalidation(tampered_dir, "pilot", "att_123", "reason")
            os.chmod(t_path, 0o644)
            with self.assertRaises(AttemptLifecycleError):
                is_sealed_invalidated(tampered_dir, "pilot", "att_123")

            # Tampered marker (non-dict JSON) -> raises AttemptLifecycleError
            tamp_dict_dir = os.path.realpath(os.path.join(base_tmp, "run_tamp_dict"))
            os.makedirs(tamp_dict_dir)
            td_path = os.path.join(tamp_dict_dir, "campaign2_pilot_invalidated.json")
            with open(td_path, "w") as f:
                f.write("[]\n")
            os.chmod(td_path, 0o444)
            with self.assertRaises(AttemptLifecycleError):
                is_sealed_invalidated(tamp_dict_dir, "pilot", "att_123")

            # Minified / noncanonical JSON -> raises AttemptLifecycleError
            noncanon_dir = os.path.realpath(os.path.join(base_tmp, "run_noncanon"))
            os.makedirs(noncanon_dir)
            nc_path = os.path.join(noncanon_dir, "campaign2_pilot_invalidated.json")
            with open(nc_path, "w") as f:
                f.write('{"record_type":"terminal_classification","stage":"pilot","attempt_id":"att_123","classification":"invalidated"}\n')
            os.chmod(nc_path, 0o444)
            with self.assertRaises(AttemptLifecycleError):
                is_sealed_invalidated(noncanon_dir, "pilot", "att_123")

            # Existing malformed marker cannot be overwritten -> publish_sealed_invalidation raises AttemptLifecycleError
            with open(nc_path, "rb") as f:
                raw_before = f.read()
            with self.assertRaises(AttemptLifecycleError):
                publish_sealed_invalidation(noncanon_dir, "pilot", "att_123", "Attempting overwrite")
            with open(nc_path, "rb") as f:
                raw_after = f.read()
            self.assertEqual(raw_before, raw_after)

            # Deterministic check-then-replace atomic link race test:
            # Inject an existing valid marker for attempt-existing immediately before publish call
            race_dir = os.path.realpath(os.path.join(base_tmp, "run_race_dir"))
            os.makedirs(race_dir)
            existing_marker_path = publish_sealed_invalidation(race_dir, "pilot", "attempt-existing", "Existing marker")
            with open(existing_marker_path, "r", encoding="utf-8") as f:
                content_before = f.read()

            # Attempting publish with attempt-new must trigger FileExistsError handling and raise AttemptLifecycleError without overwriting
            with self.assertRaises(AttemptLifecycleError):
                publish_sealed_invalidation(race_dir, "pilot", "attempt-new", "New marker attempt")

            with open(existing_marker_path, "r", encoding="utf-8") as f:
                content_after = f.read()

            self.assertEqual(content_before, content_after)
            existing_data = json.loads(content_after)
            self.assertEqual(existing_data["attempt_id"], "attempt-existing")

    def test_non_dict_json_ledger_lines_raise_lifecycle_error(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            ledger_p = os.path.join(tmp_dir, "ledger.jsonl")
            with open(ledger_p, "w") as f:
                f.write("[]\n")
            with self.assertRaises(AttemptLifecycleError):
                AttemptLedger(ledger_p)

    def test_zero_max_score_rejected_by_orchestrator(self):
        """P2 FIX: Verifies evaluator returning max_score <= 0 is rejected by orchestrator."""
        class ZeroMaxEvaluator:
            def evaluate(self, task_id, code):
                return 0, 0, True, ""

        class MockProvider:
            def generate_candidate(self, task_id, effective_prompt, call_id):
                return "# code", 100
            def mutate_prompt(self, mutable_body, failure_context, call_id):
                return "# mutated", 200

        with tempfile.TemporaryDirectory() as tmp_dir:
            ta_data = generate_family_blocked_assignment()
            reg_data = verify_master_registration(BASE_DIR)
            orch = Campaign2Orchestrator(
                model_provider=MockProvider(),
                task_evaluator=ZeroMaxEvaluator(),
                tier_assignment=ta_data,
                registration=reg_data,
                suite="suite_p",
                workspace_root=tmp_dir,
            )
            with self.assertRaises(OrchestratorError) as ctx:
                orch.run_trial(0)
            self.assertIn("max_s", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
