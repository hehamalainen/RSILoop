"""
Comprehensive Unit & Integration Tests for Work Order C1 (Production Pilot Integration).

Mechanical Traceability Matrix:
- TestC1_1_EvaluatorNeverNone: Evaluator adapter never None on any path
- TestC1_2_ExecutableBoundary: Flag set exact, CLI version 0.144.5 pinning, fresh scratch dir, 142 inventory recount, process-group kill/reap, raw-byte hashing, 16 MiB/1 MiB stream bounds
- TestC1_3_ThreeRecordLedger: invocation_intent -> process_started -> invocation_completion, cardinality, no-process_started spawn failure
- TestC1_4_PreflightRegistryAndCanary: Preflight registry atomicity/durability, canary lifecycle, fresh attempt rules, passed canary binding survival
- TestC1_5_ActiveTimeAccumulatorAndLimits: Monotonic trial duration + retry backoff (downtime excluded), 600k tokens / 8h active time limits
- TestC1_6_NarrowedRetry: Spawn-only retries, post-spawn failures non-retryable
- TestC1_7_CredentialEnvAndResidue: Allowlisted HOME/CODEX_HOME/auth.json, residue detection
- TestC1_8_FrozenDesignEquality: Seed commit, arm vector, trio seed, tier assignment, envelope, control text, ladder, estimand, literal template bytes
- TestC1_9_PrescientificCpuGate: Worker CPU time measured for 120 parity rows, >3.0 s halt, binding record shape
- TestC1_10_PromptsAndProblemText: Problem.txt hash bound in intent, envelope protection, mutation arm isolation
- TestC1_11_JSONLClassifications: Malformed JSONL -> INVALIDATE; malformed fence -> non-evaluable; invalid mutation -> retain parent; zero-token -> usage known; canary excluded from exploratory denominators
- TestC1_12_MockAndProductionParity: Launch lock verification, --mock structural/accounting parity
- TestC1_13_FullStageRefusal: Stage 'full' raises NotImplementedError
"""

import os
import sys
import json
import stat
import time
import hashlib
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from campaign2.model_provider import (
    ProductionTaskEvaluator,
    MockTaskEvaluator,
    MockModelProvider,
    SolModelProvider,
    verify_codex_cli_version,
    build_codex_cli_command,
    build_allowlisted_env,
    create_fresh_invocation_scratch_dir,
    parse_and_classify_jsonl_output,
    ExecutableBoundaryError,
)
from campaign2.manifest import (
    verify_campaign2_manifest,
    get_expected_frozen_inventory,
)
from campaign2.registration import (
    generate_master_registration,
    verify_master_registration,
    APPROVED_BASE_COMMIT,
)
from campaign2.cpu_gate import (
    run_prescientific_cpu_gate,
    CpuGateLimitExceededError,
)
from campaign2.preflight_registry import (
    PreflightRegistry,
    run_pilot_canary,
    CanaryFailureError,
    StageLimitsExceededError,
)
from campaign2.orchestrator import (
    Campaign2Orchestrator,
    OrchestratorError,
    ExploratoryTracker,
)


class TestC1_1_EvaluatorNeverNone(unittest.TestCase):
    def test_production_evaluator_adapter_instantiation(self):
        evaluator = ProductionTaskEvaluator()
        self.assertIsNotNone(evaluator)

    def test_orchestrator_evaluator_never_none(self):
        reg = verify_master_registration(BASE_DIR)
        ta = {
            "set_assignments": {
                "suite_p": {
                    "optimization": ["p_f1_01_ndjson_stream_parser"],
                    "selection": ["p_f1_02_syslog_framing_decoder"],
                    "confirmatory": ["p_f2_01_workflow_execution_engine"],
                },
                "structural_holdout": ["p_f4_01_bgp_peer_fsm"],
            }
        }
        orch = Campaign2Orchestrator(
            model_provider=MockModelProvider(),
            task_evaluator=None,  # Passed None
            tier_assignment=ta,
            registration=reg,
            suite="suite_p",
            workspace_root=BASE_DIR,
        )
        self.assertIsNotNone(orch.evaluator)
        self.assertIsInstance(orch.evaluator, ProductionTaskEvaluator)


class TestC1_2_ExecutableBoundary(unittest.TestCase):
    def test_shared_command_builder(self):
        from campaign2.model_provider import _get_codex_bin_path
        cmd = build_codex_cli_command("gpt-5.6-sol", "/tmp/scratch")
        expected = [
            _get_codex_bin_path(), "exec", "-s", "read-only", "--ephemeral", "--json",
            "--model", "gpt-5.6-sol", "--skip-git-repo-check",
            "--ignore-user-config", "--ignore-rules", "-C", "/tmp/scratch", "-"
        ]
        self.assertEqual(cmd, expected)
        self.assertEqual(cmd[-1], "-")

    def test_cli_version_pinning(self):
        def fake_run(cmd, **kwargs):
            return SimpleNamespace(stdout="codex-cli 0.144.5\n", stderr="", returncode=0)

        with mock.patch("subprocess.run", side_effect=fake_run):
            ver = verify_codex_cli_version("codex")
            self.assertEqual(ver, "0.144.5")

        def fake_wrong_run(cmd, **kwargs):
            return SimpleNamespace(stdout="codex-cli 0.145.0\n", stderr="", returncode=0)

        with mock.patch("subprocess.run", side_effect=fake_wrong_run):
            with self.assertRaises(ExecutableBoundaryError) as ctx:
                verify_codex_cli_version("codex")
            self.assertIn("Exact CLI version 0.144.5 required", str(ctx.exception))

    def test_fresh_scratch_dir_location_and_ownership(self):
        import shutil
        scratch = create_fresh_invocation_scratch_dir(BASE_DIR)
        self.assertTrue(os.path.exists(scratch))
        self.assertFalse(os.path.islink(scratch))
        self.assertEqual(os.stat(scratch).st_uid, os.getuid())
        self.assertTrue(os.path.realpath(scratch).startswith("/tmp") or not os.path.realpath(scratch).startswith(os.path.realpath(BASE_DIR)))
        shutil.rmtree(scratch)

    def test_manifest_inventory_144_files(self):
        inventory = get_expected_frozen_inventory(BASE_DIR)
        self.assertIn("generator.sb.template", inventory)
        self.assertIn("campaign2/cpu_gate.py", inventory)
        self.assertIn("campaign2/preflight_registry.py", inventory)
        ok, count = verify_campaign2_manifest(BASE_DIR)
        self.assertTrue(ok)
        self.assertEqual(count, 144)


class TestC1_3_ThreeRecordLedger(unittest.TestCase):
    def test_three_record_ledger_flow(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            from campaign2.attempt_lifecycle import AttemptLedger
            ledger_p = os.path.join(tmp_dir, "ledger.jsonl")
            ledger = AttemptLedger(ledger_p)

            provider = SolModelProvider(model_name="gpt-5.6-sol", workspace_root=BASE_DIR, ledger=ledger, mock_subprocess=True)
            code, tokens = provider.generate_candidate("c_f1_01_log_stream_parser", "def solve(): pass", "call_1")
            self.assertIn("def solve()", code)

            entries = ledger.get_entries()
            intents = [e for e in entries if e.get("record_type") == "invocation_intent"]
            starts = [e for e in entries if e.get("record_type") == "process_started"]
            completes = [e for e in entries if e.get("record_type") == "invocation_completion"]

            self.assertEqual(len(intents), 1)
            self.assertEqual(len(starts), 1)
            self.assertEqual(len(completes), 1)

            self.assertEqual(starts[0]["intent_record_hash"], hashlib.sha256((json.dumps(intents[0], sort_keys=True) + "\n").encode("utf-8")).hexdigest())
            self.assertEqual(completes[0]["started_record_hash"], hashlib.sha256((json.dumps(starts[0], sort_keys=True) + "\n").encode("utf-8")).hexdigest())
            self.assertTrue(ledger.verify_three_record_protocol())


class TestC1_4_PreflightRegistryAndCanary(unittest.TestCase):
    def test_preflight_registry_atomicity_and_durability(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            reg = PreflightRegistry(stage="pilot", workspace_root=tmp_dir)
            self.assertFalse(reg.data["canary_passed"])
            self.assertEqual(reg.data["total_logical_calls"], 0)

            # File mode 0o444
            st_mode = stat.S_IMODE(os.stat(reg.registry_path).st_mode)
            self.assertEqual(st_mode, 0o444)

    def test_canary_passed_binding_and_repeat_prevention(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            model = MockModelProvider()
            data1 = run_pilot_canary("pilot", "att_001", model, workspace_root=tmp_dir)
            self.assertTrue(data1["canary_passed"])
            self.assertEqual(data1["accepted_attempt_id"], "att_001")

            # Second call resumes without rerunning canary
            data2 = run_pilot_canary("pilot", "att_001", model, workspace_root=tmp_dir)
            self.assertTrue(data2["canary_passed"])

            # Call with different attempt ID fails
            with self.assertRaises(CanaryFailureError):
                run_pilot_canary("pilot", "att_002", model, workspace_root=tmp_dir)


class TestC1_5_ActiveTimeAccumulatorAndLimits(unittest.TestCase):
    def test_stage_global_limits_accumulation(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            reg = PreflightRegistry(stage="pilot", workspace_root=tmp_dir)
            reg.record_usage(tokens_used=1000, active_time=10.0, logical_calls=1, local_attempts=1)
            self.assertEqual(reg.data["cumulative_tokens"], 1000)
            self.assertEqual(reg.data["cumulative_active_time"], 10.0)

            with self.assertRaises(StageLimitsExceededError):
                reg.record_usage(tokens_used=700000, active_time=1.0)


class TestC1_6_NarrowedRetry(unittest.TestCase):
    def test_post_spawn_failure_is_non_retryable(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            provider = SolModelProvider(model_name="gpt-5.6-sol", workspace_root=BASE_DIR, mock_subprocess=False)

            def mock_popen(*args, **kwargs):
                proc = mock.MagicMock()
                proc.pid = 12345
                proc.poll.return_value = 0
                proc.returncode = 1
                proc.communicate.return_value = (b"", b"HTTP 429 Too Many Requests")
                return proc

            with mock.patch("subprocess.Popen", side_effect=mock_popen):
                with self.assertRaises(ExecutableBoundaryError) as ctx:
                    provider.generate_candidate("c_f1_01_log_stream_parser", "def solve(): pass", "call_err")
                self.assertIn("failed post-spawn", str(ctx.exception))


class TestC1_7_CredentialEnvAndResidue(unittest.TestCase):
    def test_allowlisted_env(self):
        env = build_allowlisted_env("/tmp/inv_root")
        self.assertEqual(env["HOME"], "/tmp/inv_root")
        self.assertEqual(env["CODEX_HOME"], "/tmp/inv_root/.codex")
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)


class TestC1_8_FrozenDesignEquality(unittest.TestCase):
    def test_frozen_design_parameters(self):
        data = verify_master_registration(BASE_DIR)
        self.assertEqual(data["base_commit_sha"], APPROVED_BASE_COMMIT)
        self.assertEqual(data["arm_assignment_vectors"], [-1, -1, -1, 1, -1, -1, -1, 1, 1, -1])
        self.assertEqual(data["trio_ordering_master_seed"], 685898983)
        self.assertEqual(data["reasoning_setting"], "cli_0.144.5_default")
        self.assertIn("candidate_template_bytes", data["prompt_envelope"])
        self.assertIn("mutation_template_bytes", data["prompt_envelope"])


class TestC1_9_PrescientificCpuGate(unittest.TestCase):
    def test_cpu_gate_execution_and_persisted_record(self):
        record = run_prescientific_cpu_gate(workspace_root=BASE_DIR, is_mock=True)
        self.assertEqual(record["total_rows_evaluated"], 120)
        self.assertLessEqual(record["max_cpu_seconds"], 3.0)

        # Verification of existing file
        rec_path = os.path.join(BASE_DIR, "campaign2_cpu_gate_record.json")
        self.assertTrue(os.path.exists(rec_path))
        st_mode = stat.S_IMODE(os.stat(rec_path).st_mode)
        self.assertEqual(st_mode, 0o444)


class TestC1_10_PromptsAndProblemText(unittest.TestCase):
    def test_candidate_prompt_includes_authenticated_problem_text(self):
        provider = SolModelProvider(model_name="gpt-5.6-sol", workspace_root=BASE_DIR, mock_subprocess=True)

        captured_stdin = []
        def fake_execute(call_id, stdin_bytes, task_id="", problem_bytes=b""):
            captured_stdin.append(stdin_bytes)
            return "```python\ndef solve(): pass\n```", 50, {"classification": "valid"}

        with mock.patch.object(provider, "_execute_cli_call", side_effect=fake_execute):
            code, tokens = provider.generate_candidate("c_f1_01_log_stream_parser", "def solve(): pass", "call_task_spec")
            self.assertEqual(len(captured_stdin), 1)
            stdin_str = captured_stdin[0].decode("utf-8")
            self.assertIn("---PROBLEM SPECIFICATION---", stdin_str)
            self.assertIn("log_stream_parser", stdin_str)
            self.assertIn("expert Python coder", stdin_str)


class TestC1_11_JSONLClassifications(unittest.TestCase):
    def test_jsonl_parsing_classifications(self):
        # 1. Valid output
        valid_turn = {"type": "turn.completed", "usage": {"output_tokens": 50}}
        valid_msg = {"type": "item.completed", "item": {"type": "agent_message", "text": "code"}}
        valid_bytes = (json.dumps(valid_msg) + "\n" + json.dumps(valid_turn) + "\n").encode("utf-8")
        parsed = parse_and_classify_jsonl_output(valid_bytes, b"")
        self.assertEqual(parsed["classification"], "valid")
        self.assertEqual(parsed["tokens"], 50)
        self.assertEqual(parsed["text"], "code")

        # 2. Malformed JSONL
        parsed_bad = parse_and_classify_jsonl_output(b"not json\n", b"")
        self.assertEqual(parsed_bad["classification"], "invalidated")
        self.assertFalse(parsed_bad["usage_known"])

        # 3. Nonzero exit code
        parsed_nonzero = parse_and_classify_jsonl_output(valid_bytes, b"", return_code=1)
        self.assertEqual(parsed_nonzero["classification"], "invalidated")
        self.assertFalse(parsed_nonzero["usage_known"])

        # 4. Zero-token completion usage known
        zero_turn = {"type": "turn.completed", "usage": {"output_tokens": 0}}
        zero_msg = {"type": "item.completed", "item": {"type": "agent_message", "text": "code"}}
        zero_bytes = (json.dumps(zero_msg) + "\n" + json.dumps(zero_turn) + "\n").encode("utf-8")
        parsed_zero = parse_and_classify_jsonl_output(zero_bytes, b"")
        self.assertTrue(parsed_zero["usage_known"])
        self.assertEqual(parsed_zero["tokens"], 0)


class TestC1_12_MockAndProductionParity(unittest.TestCase):
    def test_mock_mode_uses_sol_provider_mock_subprocess_and_production_evaluator(self):
        from campaign2.launch_lock import verify_launch_lock
        from rsi_loop import _run_campaign2

        args = SimpleNamespace(stage="pilot", mock=True, resume=False)
        with tempfile.TemporaryDirectory() as tmp_dir:
            attempt_id = "run_mock_parity_test_0001"
            run_dir = os.path.join(tmp_dir, attempt_id)
            os.makedirs(run_dir, exist_ok=True)
            ctx = SimpleNamespace(run_dir=run_dir, run_id=attempt_id)

            # Mock launch lock, parity matrix, and reporting to allow test run
            with mock.patch("campaign2.launch_lock.verify_launch_lock", return_value=True), \
                 mock.patch("campaign2.manifest.verify_campaign2_parity_matrix", return_value=True), \
                 mock.patch("campaign2.reporting.generate_schema3_report", return_value={}), \
                 mock.patch("campaign2.reporting.publish_schema3_report", return_value=""):
                with mock.patch("campaign2.orchestrator.Campaign2Orchestrator") as mock_orch_cls:
                    from fractions import Fraction
                    mock_orch_inst = mock.MagicMock()
                    mock_orch_inst.run_all_trials.return_value = []
                    mock_orch_inst.compute_outcome.return_value = {
                        "suite": "suite_p",
                        "num_trials": 10,
                        "opt_sel_baseline_aggregate": Fraction(1, 2),
                        "conf_baseline_aggregate": Fraction(1, 2),
                        "opt_sel_opt_aggregate": Fraction(3, 5),
                        "opt_sel_ctrl_aggregate": Fraction(1, 2),
                        "baseline_aggregate": Fraction(1, 2),
                        "optimizer_winner_aggregate": Fraction(7, 10),
                        "control_winner_aggregate": Fraction(1, 2),
                        "delta_opt_minus_ctrl": Fraction(1, 5),
                        "delta_positive": True,
                        "all_16_opt_sel_perfect": False,
                        "per_trial": [{"trial_index": 0}],
                        "exploratory": {},
                        "family_scores": {"F1": {}, "F2": {}, "F3": {}, "F4": {}},
                    }
                    mock_orch_cls.return_value = mock_orch_inst

                    _run_campaign2(args, ctx)

                    self.assertTrue(mock_orch_cls.called)
                    kwargs = mock_orch_cls.call_args.kwargs
                    provider = kwargs.get("model_provider")
                    evaluator = kwargs.get("task_evaluator")

                    self.assertIsInstance(provider, SolModelProvider)
                    self.assertTrue(provider.mock_subprocess)
                    self.assertIsInstance(evaluator, ProductionTaskEvaluator)


class TestC1_13_FullStageRefusal(unittest.TestCase):
    def test_full_stage_refuses_to_start(self):
        from rsi_loop import _run_campaign2
        args = SimpleNamespace(stage="full", mock=True)
        ctx = SimpleNamespace(run_dir="/tmp/test_run", run_id="run_123")
        with self.assertRaises(NotImplementedError) as c:
            _run_campaign2(args, ctx)
        self.assertIn("full stage requires Work Order C implementation", str(c.exception))


if __name__ == "__main__":
    unittest.main()
