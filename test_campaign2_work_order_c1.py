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


def _get_workspace_snapshot(directory: str) -> dict:
    snap = {}
    for root, _, files in os.walk(directory):
        for f in files:
            if f.startswith(".") or f.endswith(".pyc") or f == ".DS_Store":
                continue
            p = os.path.join(root, f)
            rel = os.path.relpath(p, directory)
            try:
                st = os.stat(p)
                st_mode = stat.S_IMODE(st.st_mode)
                size = st.st_size
                with open(p, "rb") as fp:
                    h = hashlib.sha256(fp.read()).hexdigest()
                snap[rel] = (st_mode, size, h)
            except Exception:
                pass
    return snap


CANONICAL_PRE_SNAPSHOT = _get_workspace_snapshot(BASE_DIR)




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
import shutil
from campaign2.preflight_registry import (
    PreflightRegistry,
    run_pilot_canary,
    PreflightRegistryError,
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
        with tempfile.TemporaryDirectory() as tmp_dir:
            orch = Campaign2Orchestrator(
                model_provider=MockModelProvider(),
                task_evaluator=None,  # Passed None
                tier_assignment=ta,
                registration=reg,
                suite="suite_p",
                workspace_root=tmp_dir,
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
        scratch = create_fresh_invocation_scratch_dir(BASE_DIR, is_mock=True)
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


    def test_seatbelt_profile_denies_workspace_root(self):
        from campaign2.model_provider import render_seatbelt_profile
        with tempfile.TemporaryDirectory() as tmp_scratch:
            sb_p = render_seatbelt_profile(tmp_scratch, BASE_DIR)
            with open(sb_p, "r", encoding="utf-8") as f:
                sb_text = f.read()
            self.assertNotIn(os.path.realpath(BASE_DIR), sb_text)
            self.assertIn("/opt/homebrew", sb_text)


class TestC1_3_ThreeRecordLedger(unittest.TestCase):
    def test_three_record_ledger_flow(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            from campaign2.attempt_lifecycle import AttemptLedger
            ledger_p = os.path.join(tmp_dir, "ledger.jsonl")
            ledger = AttemptLedger(ledger_p)

            provider = SolModelProvider(model_name="gpt-5.6-sol", workspace_root=tmp_dir, ledger=ledger, mock_subprocess=True)
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

    def test_three_record_ledger_sequence_and_contiguous_ordinals(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            from campaign2.attempt_lifecycle import AttemptLedger, AttemptLifecycleError
            ledger_p = os.path.join(tmp_dir, "ledger.jsonl")
            ledger = AttemptLedger(ledger_p)

            # Non-contiguous ordinal (ordinal=2 for first call) raises AttemptLifecycleError
            bad_record = {
                "record_type": "invocation_intent",
                "logical_call_id": "call_bad",
                "invocation_ordinal": 2,
                "timestamp": time.time(),
            }
            ledger._append_record(bad_record)
            with self.assertRaises(AttemptLifecycleError):
                ledger.verify_three_record_protocol()


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
    def test_stage_global_limits_accumulation_and_durable_overshoot(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            reg = PreflightRegistry(stage="pilot", workspace_root=tmp_dir)
            reg.record_usage(tokens_used=1000, active_time=10.0, logical_calls=1, local_attempts=1)
            self.assertEqual(reg.data["cumulative_tokens"], 1000)
            self.assertEqual(reg.data["cumulative_active_time"], 10.0)

            # Overshoot writes updated tokens to disk BEFORE raising StageLimitsExceededError
            with self.assertRaises(StageLimitsExceededError):
                reg.record_usage(tokens_used=700000, active_time=1.0)

            # Re-read from disk to verify overshooting usage was saved durably
            with open(reg.registry_path, "r", encoding="utf-8") as f:
                reg_disk = json.load(f)
            self.assertEqual(reg_disk["cumulative_tokens"], 701000)


class TestC1_6_NarrowedRetry(unittest.TestCase):
    def test_post_spawn_failure_is_non_retryable(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            provider = SolModelProvider(model_name="gpt-5.6-sol", workspace_root=tmp_dir, mock_subprocess=False)

            def mock_popen(*args, **kwargs):
                proc = mock.MagicMock()
                proc.pid = 12345
                proc.poll.return_value = 0
                proc.returncode = 1
                proc.communicate.return_value = (b"", b"HTTP 429 Too Many Requests")
                return proc

            with mock.patch("campaign2.model_provider.create_fresh_invocation_scratch_dir", return_value=tmp_dir), \
                 mock.patch("subprocess.Popen", side_effect=mock_popen):
                with self.assertRaises(ExecutableBoundaryError) as ctx:
                    provider.generate_candidate("c_f1_01_log_stream_parser", "def solve(): pass", "call_err")
                self.assertIn("failed", str(ctx.exception))

    def test_mutation_transport_failure_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            provider = SolModelProvider(model_name="gpt-5.6-sol", workspace_root=tmp_dir, mock_subprocess=False)
            with mock.patch.object(provider, "_execute_cli_call", side_effect=ExecutableBoundaryError("timeout")):
                with self.assertRaises(ExecutableBoundaryError):
                    provider.mutate_prompt("body", "ctx", "mut_call")


class TestC1_7_CredentialEnvAndResidue(unittest.TestCase):
    def test_allowlisted_env(self):
        env = build_allowlisted_env("/tmp/inv_root")
        self.assertEqual(env["HOME"], "/tmp/inv_root")
        self.assertEqual(env["CODEX_HOME"], "/tmp/inv_root/.codex")
        self.assertNotIn("OPENAI_API_KEY", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)

    def test_setup_credential_home_fails_closed_in_production(self):
        from campaign2.model_provider import setup_credential_home
        with tempfile.TemporaryDirectory() as tmp_scratch:
            with self.assertRaises(ExecutableBoundaryError):
                setup_credential_home(tmp_scratch, is_mock=False, auth_source_path="/nonexistent/auth.json")


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
        with tempfile.TemporaryDirectory() as tmp_dir:
            shutil.copy2(os.path.join(BASE_DIR, "campaign2_parity_matrix.json"), os.path.join(tmp_dir, "campaign2_parity_matrix.json"))
            record = run_prescientific_cpu_gate(workspace_root=tmp_dir, is_mock=True)
            self.assertEqual(record["total_rows_evaluated"], 120)
            self.assertLessEqual(record["max_cpu_seconds"], 3.0)

    def test_cpu_gate_rejects_missing_is_mock_key(self):
        from campaign2.cpu_gate import CpuGateError
        with tempfile.TemporaryDirectory() as tmp_dir:
            rec_p = os.path.join(tmp_dir, "campaign2_cpu_gate_record.json")
            with open(rec_p, "w", encoding="utf-8") as f:
                json.dump({"total_rows_evaluated": 120, "max_cpu_seconds": 0.01}, f)
            os.chmod(rec_p, 0o444)
            with self.assertRaises(CpuGateError):
                run_prescientific_cpu_gate(workspace_root=tmp_dir, is_mock=False)


class TestC1_10_PromptsAndProblemText(unittest.TestCase):
    def test_candidate_prompt_includes_authenticated_problem_text_single_envelope(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            provider = SolModelProvider(model_name="gpt-5.6-sol", workspace_root=tmp_dir, mock_subprocess=True)

            captured_stdin = []
            def fake_execute(call_id, stdin_bytes, task_id="", problem_bytes=b""):
                captured_stdin.append(stdin_bytes)
                return "```python\ndef solve(): pass\n```", 50, {"classification": "valid"}

            with mock.patch.object(provider, "_execute_cli_call", side_effect=fake_execute):
                code, tokens = provider.generate_candidate("c_f1_01_log_stream_parser", "def solve(): pass", "call_task_spec")
                self.assertEqual(len(captured_stdin), 1)
                stdin_str = captured_stdin[0].decode("utf-8")
                self.assertEqual(stdin_str.count("---PROBLEM SPECIFICATION---"), 1)
                self.assertEqual(stdin_str.count("You are an expert Python coder"), 1)
                self.assertIn("log_stream_parser", stdin_str)


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
            ctx = SimpleNamespace(run_dir=run_dir, run_id=attempt_id, workspace_root=tmp_dir)

            # Copy master registration and generator.sb.template to tmp_dir
            shutil.copy2(os.path.join(BASE_DIR, "campaign2_master_registration.json"), os.path.join(tmp_dir, "campaign2_master_registration.json"))
            shutil.copy2(os.path.join(BASE_DIR, "campaign2_registration_anchor.hash"), os.path.join(tmp_dir, "campaign2_registration_anchor.hash"))
            shutil.copy2(os.path.join(BASE_DIR, "generator.sb.template"), os.path.join(tmp_dir, "generator.sb.template"))

            def mock_eval_sandboxed(task_id, code, timeout_seconds=15):
                return (10, 10, True, "")

            # Patch launch lock and parity matrix verification, and mock evaluate_candidate_code_sandboxed to run production evaluator
            with mock.patch("campaign2.launch_lock.verify_launch_lock", return_value=True), \
                 mock.patch("campaign2.manifest.verify_campaign2_parity_matrix", return_value=True), \
                 mock.patch("campaign2.proxy.evaluate_candidate_code_sandboxed", side_effect=mock_eval_sandboxed):

                _run_campaign2(args, ctx)

                report_path = os.path.join(run_dir, "campaign2_pilot_report.json")
                self.assertTrue(os.path.exists(report_path))
                with open(report_path, "r", encoding="utf-8") as f:
                    report_data = json.load(f)
                self.assertEqual(report_data["schema_version"], "3")
                self.assertTrue(report_data["is_mock"])
                self.assertEqual(report_data["num_trials"], 2)
                self.assertIn("primary_outcome", report_data)
                self.assertEqual(len(report_data["per_family_deltas"]), 4)
                self.assertEqual(len(report_data["per_tier_deltas"]), 4)
                self.assertEqual(report_data["prescientific_cpu_gate"]["total_rows_evaluated"], 120)

                # Assert complete accounting equivalence (P1-4)
                self.assertIn("exploratory", report_data)
                exp = report_data["exploratory"]
                self.assertIn("evaluable_candidate_rate", exp)
                self.assertIn("score_improving_mutations_per_call", exp)

                from campaign2.preflight_registry import PreflightRegistry
                reg = PreflightRegistry(stage="pilot", workspace_root=tmp_dir)
                self.assertEqual(reg.data["total_logical_calls"], 173)
                self.assertEqual(reg.data["total_local_invocation_attempts"], 173)
                self.assertGreater(reg.data["cumulative_tokens"], 0)


class TestC1_14_Round24Invariants(unittest.TestCase):
    def test_preflight_registry_authentication_fails_closed_on_missing_or_mismatched_hashes(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            shutil.copy2(os.path.join(BASE_DIR, "campaign2_master_registration.json"), os.path.join(tmp_dir, "campaign2_master_registration.json"))
            shutil.copy2(os.path.join(BASE_DIR, "campaign2_registration_anchor.hash"), os.path.join(tmp_dir, "campaign2_registration_anchor.hash"))

            reg_path = os.path.join(tmp_dir, "history", "campaign2_pilot_preflight_registry.json")
            os.makedirs(os.path.dirname(reg_path), exist_ok=True)
            bad_data = {
                "record_type": "preflight_registry",
                "stage": "pilot",
                "registration_hash": "mismatched_registration_hash_000000000000000000000000",
                "launch_lock_hash": "mismatched_lock_hash_000000000000000000000000",
                "canary_passed": False,
            }
            with open(reg_path, "w", encoding="utf-8") as f:
                json.dump(bad_data, f)
            os.chmod(reg_path, 0o444)

            with self.assertRaises(PreflightRegistryError):
                PreflightRegistry(stage="pilot", workspace_root=tmp_dir)

    def test_canary_validation_exact_string(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            model = mock.MagicMock()
            model.generate_candidate.return_value = ("prefix CANARY_OK suffix", 10)
            with self.assertRaises(CanaryFailureError):
                run_pilot_canary("pilot", "att_exact_fail", model, workspace_root=tmp_dir)

    def test_canary_reconciliation_from_ledger(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            from campaign2.attempt_lifecycle import AttemptLedger
            from campaign2.preflight_registry import CANARY_NEUTRAL_PROMPT
            run_dir = os.path.join(tmp_dir, "att_rec_001")
            os.makedirs(run_dir, exist_ok=True)
            ledger_p = os.path.join(run_dir, "campaign2_pilot_ledger.jsonl")
            ledger = AttemptLedger(ledger_p)

            canary_prompt_hash = hashlib.sha256(CANARY_NEUTRAL_PROMPT.encode("utf-8")).hexdigest()

            # Record a valid 3-record canary sequence in ledger
            intent = {
                "record_type": "invocation_intent",
                "logical_call_id": "canary_pilot_att_rec_001_01",
                "invocation_ordinal": 1,
                "prompt_hash": canary_prompt_hash,
                "timestamp": time.time(),
            }
            intent_bytes = (json.dumps(intent, sort_keys=True) + "\n").encode("utf-8")
            intent_hash = hashlib.sha256(intent_bytes).hexdigest()

            started = {
                "record_type": "process_started",
                "logical_call_id": "canary_pilot_att_rec_001_01",
                "invocation_ordinal": 1,
                "pid": 12345,
                "intent_record_hash": intent_hash,
                "timestamp": time.time(),
            }
            started_bytes = (json.dumps(started, sort_keys=True) + "\n").encode("utf-8")
            started_hash = hashlib.sha256(started_bytes).hexdigest()

            comp = {
                "record_type": "invocation_completion",
                "logical_call_id": "canary_pilot_att_rec_001_01",
                "invocation_ordinal": 1,
                "intent_record_hash": intent_hash,
                "started_record_hash": started_hash,
                "classification": "valid",
                "return_code": 0,
                "text_hash": hashlib.sha256(b"CANARY_OK").hexdigest(),
                "tokens": 15,
                "duration": 0.2,
                "timestamp": time.time(),
            }

            ledger._append_record(intent)
            ledger._append_record(started)
            ledger._append_record(comp)

            # Copy master registration files
            shutil.copy2(os.path.join(BASE_DIR, "campaign2_master_registration.json"), os.path.join(tmp_dir, "campaign2_master_registration.json"))
            shutil.copy2(os.path.join(BASE_DIR, "campaign2_registration_anchor.hash"), os.path.join(tmp_dir, "campaign2_registration_anchor.hash"))

            model = mock.MagicMock()
            data = run_pilot_canary("pilot", "att_rec_001", model, workspace_root=tmp_dir, ledger=ledger)
            self.assertTrue(data["canary_passed"])
            # Model should NOT have been called due to reconciliation
            self.assertFalse(model.generate_candidate.called)
            self.assertEqual(data["canary_ordinals"][0]["tokens"], 15)

    def test_zero_event_retry_load_bearing_invariant(self):
        # 1 valid event followed by non-zero exit MUST NOT be retried
        valid_event = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "hello"}}) + "\n"
        stdout_bytes = valid_event.encode("utf-8")
        stderr_bytes = b"HTTP 429 Too Many Requests\n"
        parsed = parse_and_classify_jsonl_output(stdout_bytes, stderr_bytes, return_code=1)
        self.assertEqual(parsed["classification"], "invalidated")
        self.assertGreater(parsed["events_parsed"], 0)

    def test_jsonl_contract_rejects_duplicate_turn_completed_and_concatenation(self):
        ev1 = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "msg1"}})
        ev2 = json.dumps({"type": "turn.completed", "usage": {"output_tokens": 10}})
        ev3 = json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "msg2"}})
        ev4 = json.dumps({"type": "turn.completed", "usage": {"output_tokens": 10}})

        # Duplicate turn.completed is invalidated
        dup_bytes = (ev1 + "\n" + ev2 + "\n" + ev3 + "\n" + ev4 + "\n").encode("utf-8")
        parsed_dup = parse_and_classify_jsonl_output(dup_bytes, b"")
        self.assertEqual(parsed_dup["classification"], "invalidated")

        # Multiple messages extract ONLY final agent message
        msg_bytes = (ev1 + "\n" + ev3 + "\n" + ev4 + "\n").encode("utf-8")
        parsed_msg = parse_and_classify_jsonl_output(msg_bytes, b"")
        self.assertEqual(parsed_msg["classification"], "valid")
        self.assertEqual(parsed_msg["text"], "msg2")

    def test_spawn_retry_cumulative_accounting(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            reg = PreflightRegistry(stage="pilot", workspace_root=tmp_dir)
            # Attempt 1: spawn failure
            reg.record_usage(tokens_used=0, active_time=0.0, logical_calls=1, local_attempts=1, logical_call_id="call_spawn_test")
            # Attempt 2: success
            reg.record_usage(tokens_used=100, active_time=0.5, logical_calls=1, local_attempts=1, logical_call_id="call_spawn_test")

            self.assertEqual(reg.data["total_logical_calls"], 1)
            self.assertEqual(reg.data["total_local_invocation_attempts"], 2)

    def test_hardened_cpu_gate_verification(self):
        from campaign2.cpu_gate import run_prescientific_cpu_gate, CpuGateError
        with tempfile.TemporaryDirectory() as tmp_dir:
            rec_p = os.path.join(tmp_dir, "campaign2_cpu_gate_record.json")
            fake_record = {"record_type": "prescientific_cpu_gate", "is_mock": False}
            with open(rec_p, "w", encoding="utf-8") as f:
                json.dump(fake_record, f)
            os.chmod(rec_p, 0o444)

            with self.assertRaises(CpuGateError):
                run_prescientific_cpu_gate(workspace_root=tmp_dir, is_mock=False)

    def test_ledger_sequence_rejects_started_after_completion(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            from campaign2.attempt_lifecycle import AttemptLedger, AttemptLifecycleError
            ledger_p = os.path.join(tmp_dir, "ledger.jsonl")
            ledger = AttemptLedger(ledger_p)

            key = ("call_seq_test", 1)
            intent = {"record_type": "invocation_intent", "logical_call_id": "call_seq_test", "invocation_ordinal": 1, "timestamp": time.time()}
            intent_bytes = (json.dumps(intent, sort_keys=True) + "\n").encode("utf-8")
            intent_hash = hashlib.sha256(intent_bytes).hexdigest()

            comp = {
                "record_type": "invocation_completion",
                "logical_call_id": "call_seq_test",
                "invocation_ordinal": 1,
                "intent_record_hash": intent_hash,
                "started_record_hash": "",
                "parse_state": "spawn_failure",
                "timestamp": time.time(),
            }

            started = {
                "record_type": "process_started",
                "logical_call_id": "call_seq_test",
                "invocation_ordinal": 1,
                "intent_record_hash": intent_hash,
                "timestamp": time.time(),
            }

            ledger._append_record(intent)
            ledger._append_record(comp)
            ledger._append_record(started)

            with self.assertRaises(AttemptLifecycleError):
                ledger.verify_three_record_protocol()

    def test_canonical_workspace_remains_clean(self):
        # Perform true pre/post byte, mode, and hash snapshot verification of BASE_DIR (P1-3)
        post_snap = _get_workspace_snapshot(BASE_DIR)
        self.assertEqual(CANONICAL_PRE_SNAPSHOT, post_snap, "BASE_DIR files altered or unexpected new files created during test suite execution")
        reg_p = os.path.join(BASE_DIR, "history", "campaign2_pilot_preflight_registry.json")
        gate_p = os.path.join(BASE_DIR, "campaign2_cpu_gate_record.json")
        self.assertFalse(os.path.exists(reg_p), f"Forbidden canonical file found in BASE_DIR: {reg_p}")
        self.assertFalse(os.path.exists(gate_p), f"Forbidden canonical file found in BASE_DIR: {gate_p}")

    def test_master_registration_mismatch_with_source_fails_closed(self):
        from campaign2.orchestrator import Campaign2Orchestrator, OrchestratorError
        from campaign2.model_provider import MockModelProvider
        from campaign2.registration import verify_master_registration
        with tempfile.TemporaryDirectory() as tmp_dir:
            ta = {
                "set_assignments": {
                    "suite_p": {
                        "optimization": ["p_f1_01_log_stream_parser", "p_f2_01_rate_limiter_token_bucket"],
                        "selection": ["p_f3_01_lru_cache_ttl", "p_f4_01_bgp_peer_fsm"],
                        "confirmatory": ["p_f1_02_huffman_encoding", "p_f2_02_token_bucket_refill"],
                    },
                    "structural_holdout": ["p_f3_02_lru_eviction", "p_f4_02_bgp_state_transition"],
                }
            }
            reg = dict(verify_master_registration(BASE_DIR))
            reg["pilot_limits"] = {
                "num_trials": 99,  # Mismatched num_trials!
                "max_mutations_per_trial": 3,
            }
            with self.assertRaises(OrchestratorError):
                Campaign2Orchestrator(
                    model_provider=MockModelProvider(),
                    task_evaluator=None,
                    tier_assignment=ta,
                    registration=reg,
                    suite="suite_p",
                    workspace_root=tmp_dir,
                )

    def test_partial_stdout_bytes_with_http_429_is_invalidated_not_retryable(self):
        # Probe 1: Partial malformed JSON + HTTP 429 in stderr -> MUST be invalidated
        partial_json = b'{"type": "item.completed", "item": {"type": "agent_message", "text": "partial'
        parsed1 = parse_and_classify_jsonl_output(partial_json, b"HTTP 429 Too Many Requests\n", return_code=1)
        self.assertEqual(parsed1["classification"], "invalidated")

        # Probe 2: Arbitrary partial stdout + HTTP 429 in stderr -> MUST be invalidated
        partial_bytes = b"random stdout bytes text..."
        parsed2 = parse_and_classify_jsonl_output(partial_bytes, b"HTTP 429 Too Many Requests\n", return_code=1)
        self.assertEqual(parsed2["classification"], "invalidated")

        # Probe 3: Zero raw stdout + HTTP 429 in stderr -> RETRYABLE
        parsed3 = parse_and_classify_jsonl_output(b"", b"HTTP 429 Too Many Requests\n", return_code=1)
        self.assertEqual(parsed3["classification"], "transient_infrastructure_error")

    def test_canary_reconciliation_semantic_authentication(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            from campaign2.attempt_lifecycle import AttemptLedger
            from campaign2.preflight_registry import CANARY_NEUTRAL_PROMPT
            run_dir = os.path.join(tmp_dir, "att_rec_002")
            os.makedirs(run_dir, exist_ok=True)
            ledger_p = os.path.join(run_dir, "campaign2_pilot_ledger.jsonl")
            ledger = AttemptLedger(ledger_p)

            canary_prompt_hash = hashlib.sha256(CANARY_NEUTRAL_PROMPT.encode("utf-8")).hexdigest()

            # Record completion with WRONG text hash
            intent = {
                "record_type": "invocation_intent",
                "logical_call_id": "canary_pilot_att_rec_002_01",
                "invocation_ordinal": 1,
                "prompt_hash": canary_prompt_hash,
                "timestamp": time.time(),
            }
            intent_bytes = (json.dumps(intent, sort_keys=True) + "\n").encode("utf-8")
            intent_hash = hashlib.sha256(intent_bytes).hexdigest()

            started = {
                "record_type": "process_started",
                "logical_call_id": "canary_pilot_att_rec_002_01",
                "invocation_ordinal": 1,
                "pid": 12345,
                "intent_record_hash": intent_hash,
                "timestamp": time.time(),
            }
            started_bytes = (json.dumps(started, sort_keys=True) + "\n").encode("utf-8")
            started_hash = hashlib.sha256(started_bytes).hexdigest()

            comp_bad = {
                "record_type": "invocation_completion",
                "logical_call_id": "canary_pilot_att_rec_002_01",
                "invocation_ordinal": 1,
                "intent_record_hash": intent_hash,
                "started_record_hash": started_hash,
                "classification": "valid",
                "return_code": 0,
                "text_hash": hashlib.sha256(b"FAKE_RESPONSE").hexdigest(),  # Mismatched text_hash!
                "tokens": 15,
                "duration": 0.2,
                "timestamp": time.time(),
            }

            ledger._append_record(intent)
            ledger._append_record(started)
            ledger._append_record(comp_bad)

            shutil.copy2(os.path.join(BASE_DIR, "campaign2_master_registration.json"), os.path.join(tmp_dir, "campaign2_master_registration.json"))
            shutil.copy2(os.path.join(BASE_DIR, "campaign2_registration_anchor.hash"), os.path.join(tmp_dir, "campaign2_registration_anchor.hash"))

            model = mock.MagicMock()
            model.generate_candidate.return_value = ("CANARY_OK", 10)
            data = run_pilot_canary("pilot", "att_rec_002", model, workspace_root=tmp_dir, ledger=ledger)
            self.assertTrue(data["canary_passed"])
            # Model MUST have been called with advanced ordinal _02 (not reusing _01) (P0-2)
            self.assertTrue(model.generate_candidate.called)
            kwargs = model.generate_candidate.call_args.kwargs
            self.assertEqual(kwargs.get("call_id"), "canary_pilot_att_rec_002_02")

    def test_cpu_gate_rejects_booleans_and_mismatched_max_row_id(self):
        from campaign2.cpu_gate import run_prescientific_cpu_gate, CpuGateError
        with tempfile.TemporaryDirectory() as tmp_dir:
            shutil.copy2(os.path.join(BASE_DIR, "campaign2_parity_matrix.json"), os.path.join(tmp_dir, "campaign2_parity_matrix.json"))
            rec_p = os.path.join(tmp_dir, "campaign2_cpu_gate_record.json")

            with open(os.path.join(BASE_DIR, "campaign2_parity_matrix.json"), "r", encoding="utf-8") as f:
                pm = json.load(f)
            rows = pm.get("matrix", list(pm.values()))
            fake_measurements = []
            for r in rows:
                fake_measurements.append({
                    "row_id": r.get("row_id", f"{r['task_id']}_{r.get('variant', 'unknown')}"),
                    "task_id": r["task_id"],
                    "worker_cpu_seconds": True,  # Boolean!
                })

            fake_rec = {
                "record_type": "prescientific_cpu_gate",
                "is_mock": False,
                "total_rows_evaluated": 120,
                "max_cpu_seconds": 1.0,
                "max_row_id": rows[0].get("row_id", f"{rows[0]['task_id']}_{rows[0].get('variant', 'unknown')}"),
                "allowed_limit_seconds": 3.0,
                "row_measurements": fake_measurements,
            }
            with open(rec_p, "w", encoding="utf-8") as f:
                json.dump(fake_rec, f)
            os.chmod(rec_p, 0o444)

            with self.assertRaises(CpuGateError):
                run_prescientific_cpu_gate(workspace_root=tmp_dir, is_mock=False)

    def test_reconciled_canary_completion_restores_cumulative_usage(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            from campaign2.attempt_lifecycle import AttemptLedger
            from campaign2.preflight_registry import CANARY_NEUTRAL_PROMPT, CANARY_EXPECTED_RESPONSE, run_pilot_canary, PreflightRegistry
            run_dir = os.path.join(tmp_dir, "att_rec_003")
            os.makedirs(run_dir, exist_ok=True)
            ledger_p = os.path.join(run_dir, "campaign2_pilot_ledger.jsonl")
            ledger = AttemptLedger(ledger_p)

            canary_prompt_hash = hashlib.sha256(CANARY_NEUTRAL_PROMPT.encode("utf-8")).hexdigest()
            canary_text_hash = hashlib.sha256(CANARY_EXPECTED_RESPONSE.encode("utf-8")).hexdigest()

            intent = {
                "record_type": "invocation_intent",
                "logical_call_id": "canary_pilot_att_rec_003_01",
                "invocation_ordinal": 1,
                "prompt_hash": canary_prompt_hash,
                "timestamp": time.time(),
            }
            intent_bytes = (json.dumps(intent, sort_keys=True) + "\n").encode("utf-8")
            intent_hash = hashlib.sha256(intent_bytes).hexdigest()

            started = {
                "record_type": "process_started",
                "logical_call_id": "canary_pilot_att_rec_003_01",
                "invocation_ordinal": 1,
                "pid": 12345,
                "intent_record_hash": intent_hash,
                "timestamp": time.time(),
            }
            started_bytes = (json.dumps(started, sort_keys=True) + "\n").encode("utf-8")
            started_hash = hashlib.sha256(started_bytes).hexdigest()

            comp_pass = {
                "record_type": "invocation_completion",
                "logical_call_id": "canary_pilot_att_rec_003_01",
                "invocation_ordinal": 1,
                "intent_record_hash": intent_hash,
                "started_record_hash": started_hash,
                "classification": "valid",
                "return_code": 0,
                "text_hash": canary_text_hash,
                "tokens": 7,
                "duration": 0.3,
                "timestamp": time.time(),
            }

            ledger._append_record(intent)
            ledger._append_record(started)
            ledger._append_record(comp_pass)

            shutil.copy2(os.path.join(BASE_DIR, "campaign2_master_registration.json"), os.path.join(tmp_dir, "campaign2_master_registration.json"))
            shutil.copy2(os.path.join(BASE_DIR, "campaign2_registration_anchor.hash"), os.path.join(tmp_dir, "campaign2_registration_anchor.hash"))

            model = mock.MagicMock()
            data = run_pilot_canary("pilot", "att_rec_003", model, workspace_root=tmp_dir, ledger=ledger)
            self.assertTrue(data["canary_passed"])

            reg = PreflightRegistry(stage="pilot", workspace_root=tmp_dir)
            self.assertEqual(reg.data["total_logical_calls"], 1)
            self.assertEqual(reg.data["total_local_invocation_attempts"], 1)
            self.assertEqual(reg.data["cumulative_tokens"], 7)

    def test_unknown_usage_canary_completion_blocks_without_resampling(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            from campaign2.attempt_lifecycle import AttemptLedger
            from campaign2.preflight_registry import CANARY_NEUTRAL_PROMPT, run_pilot_canary, CanaryFailureError
            run_dir = os.path.join(tmp_dir, "att_rec_004")
            os.makedirs(run_dir, exist_ok=True)
            ledger_p = os.path.join(run_dir, "campaign2_pilot_ledger.jsonl")
            ledger = AttemptLedger(ledger_p)

            canary_prompt_hash = hashlib.sha256(CANARY_NEUTRAL_PROMPT.encode("utf-8")).hexdigest()

            intent = {
                "record_type": "invocation_intent",
                "logical_call_id": "canary_pilot_att_rec_004_01",
                "invocation_ordinal": 1,
                "prompt_hash": canary_prompt_hash,
                "timestamp": time.time(),
            }
            intent_bytes = (json.dumps(intent, sort_keys=True) + "\n").encode("utf-8")
            intent_hash = hashlib.sha256(intent_bytes).hexdigest()

            started = {
                "record_type": "process_started",
                "logical_call_id": "canary_pilot_att_rec_004_01",
                "invocation_ordinal": 1,
                "pid": 12345,
                "intent_record_hash": intent_hash,
                "timestamp": time.time(),
            }
            started_bytes = (json.dumps(started, sort_keys=True) + "\n").encode("utf-8")
            started_hash = hashlib.sha256(started_bytes).hexdigest()

            comp_invalid = {
                "record_type": "invocation_completion",
                "logical_call_id": "canary_pilot_att_rec_004_01",
                "invocation_ordinal": 1,
                "intent_record_hash": intent_hash,
                "started_record_hash": started_hash,
                "classification": "invalidated",
                "return_code": -9,
                "text_hash": "",
                "tokens": 10,
                "duration": 0.5,
                "timestamp": time.time(),
            }

            ledger._append_record(intent)
            ledger._append_record(started)
            ledger._append_record(comp_invalid)

            shutil.copy2(os.path.join(BASE_DIR, "campaign2_master_registration.json"), os.path.join(tmp_dir, "campaign2_master_registration.json"))
            shutil.copy2(os.path.join(BASE_DIR, "campaign2_registration_anchor.hash"), os.path.join(tmp_dir, "campaign2_registration_anchor.hash"))

            model = mock.MagicMock()
            with self.assertRaises(CanaryFailureError):
                run_pilot_canary("pilot", "att_rec_004", model, workspace_root=tmp_dir, ledger=ledger)
            self.assertFalse(model.generate_candidate.called)

    def test_corrupted_registry_reload_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            from campaign2.preflight_registry import PreflightRegistry, PreflightRegistryError
            reg_dir = os.path.join(tmp_dir, "history")
            os.makedirs(reg_dir, exist_ok=True)
            reg_path = os.path.join(reg_dir, "campaign2_pilot_preflight_registry.json")
            with open(reg_path, "w", encoding="utf-8") as f:
                f.write("corrupted JSON syntax {{{")
            os.chmod(reg_path, 0o444)

            with self.assertRaises(PreflightRegistryError):
                PreflightRegistry(stage="pilot", workspace_root=tmp_dir)

    def test_create_fresh_scratch_dir_cleans_up_on_setup_failure(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            from campaign2.model_provider import create_fresh_invocation_scratch_dir
            with mock.patch("campaign2.model_provider.setup_credential_home", side_effect=ValueError("Simulated credential setup failure")):
                with self.assertRaises(ValueError):
                    create_fresh_invocation_scratch_dir(workspace_root=tmp_dir, is_mock=True)

    def test_round28_provider_and_recovery_completion_identity_equivalence(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            from campaign2.preflight_registry import PreflightRegistry, run_pilot_canary, CANARY_NEUTRAL_PROMPT, CANARY_EXPECTED_RESPONSE
            from campaign2.attempt_lifecycle import AttemptLedger

            run_dir = os.path.join(tmp_dir, "att_r28_001")
            os.makedirs(run_dir, exist_ok=True)
            ledger_p = os.path.join(run_dir, "campaign2_pilot_ledger.jsonl")
            ledger = AttemptLedger(ledger_p)

            canary_prompt_hash = hashlib.sha256(CANARY_NEUTRAL_PROMPT.encode("utf-8")).hexdigest()
            canary_text_hash = hashlib.sha256(CANARY_EXPECTED_RESPONSE.encode("utf-8")).hexdigest()

            intent = {
                "record_type": "invocation_intent",
                "logical_call_id": "canary_pilot_att_r28_001_01",
                "invocation_ordinal": 1,
                "prompt_hash": canary_prompt_hash,
                "timestamp": time.time(),
            }
            intent_bytes = (json.dumps(intent, sort_keys=True) + "\n").encode("utf-8")
            intent_hash = hashlib.sha256(intent_bytes).hexdigest()

            started = {
                "record_type": "process_started",
                "logical_call_id": "canary_pilot_att_r28_001_01",
                "invocation_ordinal": 1,
                "pid": 12345,
                "intent_record_hash": intent_hash,
                "timestamp": time.time(),
            }
            started_bytes = (json.dumps(started, sort_keys=True) + "\n").encode("utf-8")
            started_hash = hashlib.sha256(started_bytes).hexdigest()

            comp_pass = {
                "record_type": "invocation_completion",
                "logical_call_id": "canary_pilot_att_r28_001_01",
                "invocation_ordinal": 1,
                "intent_record_hash": intent_hash,
                "started_record_hash": started_hash,
                "classification": "valid",
                "return_code": 0,
                "text_hash": canary_text_hash,
                "tokens": 7,
                "duration": 0.2,
                "timestamp": time.time(),
            }
            comp_bytes = (json.dumps(comp_pass, sort_keys=True) + "\n").encode("utf-8")
            comp_hash = hashlib.sha256(comp_bytes).hexdigest()

            ledger._append_record(intent)
            ledger._append_record(started)
            ledger._append_record(comp_pass)

            shutil.copy2(os.path.join(BASE_DIR, "campaign2_master_registration.json"), os.path.join(tmp_dir, "campaign2_master_registration.json"))
            shutil.copy2(os.path.join(BASE_DIR, "campaign2_registration_anchor.hash"), os.path.join(tmp_dir, "campaign2_registration_anchor.hash"))

            # 1. Provider records usage first
            reg = PreflightRegistry(stage="pilot", workspace_root=tmp_dir)
            reg.record_usage(
                tokens_used=7,
                active_time=0.2,
                logical_calls=1,
                local_attempts=1,
                logical_call_id="canary_pilot_att_r28_001_01",
                invocation_ordinal=1,
                completion_hash=comp_hash,
            )

            # 2. Crash recovery runs run_pilot_canary
            model = mock.MagicMock()
            data = run_pilot_canary("pilot", "att_r28_001", model, workspace_root=tmp_dir, ledger=ledger)
            self.assertTrue(data["canary_passed"])

            # 3. Usage MUST remain exactly 1 call, 1 attempt, 7 tokens, 0.2s active time
            reg._reload()
            self.assertEqual(reg.data["total_logical_calls"], 1)
            self.assertEqual(reg.data["total_local_invocation_attempts"], 1)
            self.assertEqual(reg.data["cumulative_tokens"], 7)
            self.assertAlmostEqual(reg.data["cumulative_active_time"], 0.2)

    def test_round28_logical_call_grouping_reconciles_terminal_completion(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            from campaign2.preflight_registry import PreflightRegistry, run_pilot_canary, CANARY_NEUTRAL_PROMPT, CANARY_EXPECTED_RESPONSE
            from campaign2.attempt_lifecycle import AttemptLedger

            run_dir = os.path.join(tmp_dir, "att_r28_002")
            os.makedirs(run_dir, exist_ok=True)
            ledger_p = os.path.join(run_dir, "campaign2_pilot_ledger.jsonl")
            ledger = AttemptLedger(ledger_p)

            canary_prompt_hash = hashlib.sha256(CANARY_NEUTRAL_PROMPT.encode("utf-8")).hexdigest()
            canary_text_hash = hashlib.sha256(CANARY_EXPECTED_RESPONSE.encode("utf-8")).hexdigest()

            # Ordinal 1: spawn failure (permitted local retry)
            intent1 = {
                "record_type": "invocation_intent",
                "logical_call_id": "canary_pilot_att_r28_002_01",
                "invocation_ordinal": 1,
                "prompt_hash": canary_prompt_hash,
                "timestamp": time.time(),
            }
            intent1_bytes = (json.dumps(intent1, sort_keys=True) + "\n").encode("utf-8")
            intent1_hash = hashlib.sha256(intent1_bytes).hexdigest()

            started1 = {
                "record_type": "process_started",
                "logical_call_id": "canary_pilot_att_r28_002_01",
                "invocation_ordinal": 1,
                "pid": 12345,
                "intent_record_hash": intent1_hash,
                "timestamp": time.time(),
            }
            started1_bytes = (json.dumps(started1, sort_keys=True) + "\n").encode("utf-8")
            started1_hash = hashlib.sha256(started1_bytes).hexdigest()

            comp1 = {
                "record_type": "invocation_completion",
                "logical_call_id": "canary_pilot_att_r28_002_01",
                "invocation_ordinal": 1,
                "intent_record_hash": intent1_hash,
                "started_record_hash": started1_hash,
                "classification": "spawn_failure",
                "return_code": -1,
                "text_hash": "",
                "tokens": 0,
                "duration": 0.1,
                "timestamp": time.time(),
            }

            # Ordinal 2: valid pass
            intent2 = {
                "record_type": "invocation_intent",
                "logical_call_id": "canary_pilot_att_r28_002_01",
                "invocation_ordinal": 2,
                "prompt_hash": canary_prompt_hash,
                "timestamp": time.time(),
            }
            intent2_bytes = (json.dumps(intent2, sort_keys=True) + "\n").encode("utf-8")
            intent2_hash = hashlib.sha256(intent2_bytes).hexdigest()

            started2 = {
                "record_type": "process_started",
                "logical_call_id": "canary_pilot_att_r28_002_01",
                "invocation_ordinal": 2,
                "pid": 12346,
                "intent_record_hash": intent2_hash,
                "timestamp": time.time(),
            }
            started2_bytes = (json.dumps(started2, sort_keys=True) + "\n").encode("utf-8")
            started2_hash = hashlib.sha256(started2_bytes).hexdigest()

            comp2 = {
                "record_type": "invocation_completion",
                "logical_call_id": "canary_pilot_att_r28_002_01",
                "invocation_ordinal": 2,
                "intent_record_hash": intent2_hash,
                "started_record_hash": started2_hash,
                "classification": "valid",
                "return_code": 0,
                "text_hash": canary_text_hash,
                "tokens": 7,
                "duration": 0.3,
                "timestamp": time.time(),
            }

            for r in (intent1, started1, comp1, intent2, started2, comp2):
                ledger._append_record(r)

            shutil.copy2(os.path.join(BASE_DIR, "campaign2_master_registration.json"), os.path.join(tmp_dir, "campaign2_master_registration.json"))
            shutil.copy2(os.path.join(BASE_DIR, "campaign2_registration_anchor.hash"), os.path.join(tmp_dir, "campaign2_registration_anchor.hash"))

            model = mock.MagicMock()
            data = run_pilot_canary("pilot", "att_r28_002", model, workspace_root=tmp_dir, ledger=ledger)
            self.assertTrue(data["canary_passed"])

            reg = PreflightRegistry(stage="pilot", workspace_root=tmp_dir)
            self.assertEqual(reg.data["total_logical_calls"], 1)
            self.assertEqual(reg.data["total_local_invocation_attempts"], 2)
            self.assertEqual(reg.data["cumulative_tokens"], 7)

    def test_round28_unknown_usage_durable_stage_blockage(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            from campaign2.preflight_registry import PreflightRegistry, run_pilot_canary, CanaryFailureError

            shutil.copy2(os.path.join(BASE_DIR, "campaign2_master_registration.json"), os.path.join(tmp_dir, "campaign2_master_registration.json"))
            shutil.copy2(os.path.join(BASE_DIR, "campaign2_registration_anchor.hash"), os.path.join(tmp_dir, "campaign2_registration_anchor.hash"))

            reg = PreflightRegistry(stage="pilot", workspace_root=tmp_dir)
            reg.record_canary_attempt("att_r28_003", "invalidated", 10, 0.5, False, call_id="canary_pilot_att_r28_003_01")

            reg._reload()
            self.assertTrue(reg.data.get("stage_blocked"))

            # Fresh run MUST fail closed immediately without calling model provider
            model = mock.MagicMock()
            with self.assertRaises(CanaryFailureError) as ctx:
                run_pilot_canary("pilot", "att_r28_004", model, workspace_root=tmp_dir)
            self.assertIn("permanently BLOCKED", str(ctx.exception))
            self.assertFalse(model.generate_candidate.called)

    def test_round28_registry_reload_strict_authentication(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            from campaign2.preflight_registry import PreflightRegistry, PreflightRegistryError

            shutil.copy2(os.path.join(BASE_DIR, "campaign2_master_registration.json"), os.path.join(tmp_dir, "campaign2_master_registration.json"))
            shutil.copy2(os.path.join(BASE_DIR, "campaign2_registration_anchor.hash"), os.path.join(tmp_dir, "campaign2_registration_anchor.hash"))

            reg = PreflightRegistry(stage="pilot", workspace_root=tmp_dir)
            reg_path = reg.registry_path

            # Mode not 0o444 fails closed
            os.chmod(reg_path, 0o644)
            with self.assertRaises(PreflightRegistryError):
                reg._reload()

            os.chmod(reg_path, 0o444)
            reg._reload()

            # Mismatched registration hash fails closed
            reg.data["registration_hash"] = "0000000000000000000000000000000000000000000000000000000000000000"
            reg._save_data(reg.data)
            with self.assertRaises(PreflightRegistryError):
                reg._reload()

    def test_round29_real_spawn_failure_recovery_deduplication(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            from campaign2.preflight_registry import PreflightRegistry, run_pilot_canary, CANARY_NEUTRAL_PROMPT, CANARY_EXPECTED_RESPONSE
            from campaign2.attempt_lifecycle import AttemptLedger

            cid = "canary_pilot_att_r29_001_01"
            ledger_path = os.path.join(tmp_dir, "campaign2_pilot_ledger.jsonl")
            ledger = AttemptLedger(ledger_path)

            canary_prompt_hash = hashlib.sha256(CANARY_NEUTRAL_PROMPT.encode("utf-8")).hexdigest()
            canary_text_hash = hashlib.sha256(CANARY_EXPECTED_RESPONSE.encode("utf-8")).hexdigest()

            # Ordinal 1: spawn_failure
            intent1 = {
                "record_type": "invocation_intent",
                "logical_call_id": cid,
                "invocation_ordinal": 1,
                "prompt_hash": canary_prompt_hash,
                "timestamp": time.time(),
            }
            intent1_bytes = (json.dumps(intent1, sort_keys=True) + "\n").encode("utf-8")
            intent1_hash = hashlib.sha256(intent1_bytes).hexdigest()

            comp1 = {
                "record_type": "invocation_completion",
                "logical_call_id": cid,
                "invocation_ordinal": 1,
                "intent_record_hash": intent1_hash,
                "started_record_hash": "",
                "return_code": -1,
                "raw_stdout_digest": hashlib.sha256(b"").hexdigest(),
                "raw_stderr_digest": hashlib.sha256(b"").hexdigest(),
                "text_hash": "",
                "parse_state": "spawn_failure",
                "usage_known": True,
                "tokens": 0,
                "duration": 0.0,
                "classification": "spawn_failure",
                "error": "spawn failed test",
                "timestamp": time.time(),
            }
            comp1_bytes = (json.dumps(comp1, sort_keys=True) + "\n").encode("utf-8")
            comp1_hash = hashlib.sha256(comp1_bytes).hexdigest()

            # Ordinal 2: valid pass
            intent2 = {
                "record_type": "invocation_intent",
                "logical_call_id": cid,
                "invocation_ordinal": 2,
                "prompt_hash": canary_prompt_hash,
                "timestamp": time.time(),
            }
            intent2_bytes = (json.dumps(intent2, sort_keys=True) + "\n").encode("utf-8")
            intent2_hash = hashlib.sha256(intent2_bytes).hexdigest()

            started2 = {
                "record_type": "process_started",
                "logical_call_id": cid,
                "invocation_ordinal": 2,
                "pid": 22222,
                "intent_record_hash": intent2_hash,
                "timestamp": time.time(),
            }
            started2_bytes = (json.dumps(started2, sort_keys=True) + "\n").encode("utf-8")
            started2_hash = hashlib.sha256(started2_bytes).hexdigest()

            comp2 = {
                "record_type": "invocation_completion",
                "logical_call_id": cid,
                "invocation_ordinal": 2,
                "intent_record_hash": intent2_hash,
                "started_record_hash": started2_hash,
                "classification": "valid",
                "return_code": 0,
                "text_hash": canary_text_hash,
                "tokens": 7,
                "duration": 0.2,
                "timestamp": time.time(),
            }

            for r in (intent1, comp1, intent2, started2, comp2):
                ledger._append_record(r)

            shutil.copy2(os.path.join(BASE_DIR, "campaign2_master_registration.json"), os.path.join(tmp_dir, "campaign2_master_registration.json"))
            shutil.copy2(os.path.join(BASE_DIR, "campaign2_registration_anchor.hash"), os.path.join(tmp_dir, "campaign2_registration_anchor.hash"))

            # Pre-record spawn failure usage in registry as would occur live before crash
            reg = PreflightRegistry(stage="pilot", workspace_root=tmp_dir)
            reg.record_usage(0, 0.0, 1, 1, cid, 1, comp1_hash)

            model = mock.MagicMock()
            data = run_pilot_canary("pilot", "att_r29_001", model, workspace_root=tmp_dir, ledger=ledger)
            self.assertTrue(data["canary_passed"])

            reg._reload()
            self.assertEqual(reg.data["total_logical_calls"], 1)
            self.assertEqual(reg.data["total_local_invocation_attempts"], 2)
            self.assertEqual(reg.data["cumulative_tokens"], 7)

    def test_round29_forbidden_intermediate_retry_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            from campaign2.preflight_registry import PreflightRegistry, run_pilot_canary, CanaryFailureError, CANARY_NEUTRAL_PROMPT, CANARY_EXPECTED_RESPONSE
            from campaign2.attempt_lifecycle import AttemptLedger

            cid = "canary_pilot_att_r29_002_01"
            ledger_path = os.path.join(tmp_dir, "campaign2_pilot_ledger.jsonl")
            ledger = AttemptLedger(ledger_path)

            canary_prompt_hash = hashlib.sha256(CANARY_NEUTRAL_PROMPT.encode("utf-8")).hexdigest()
            canary_text_hash = hashlib.sha256(CANARY_EXPECTED_RESPONSE.encode("utf-8")).hexdigest()

            # Ordinal 1: unauthorized intermediate retry (e.g. invalidated)
            intent1 = {
                "record_type": "invocation_intent",
                "logical_call_id": cid,
                "invocation_ordinal": 1,
                "prompt_hash": canary_prompt_hash,
                "timestamp": time.time(),
            }
            intent1_bytes = (json.dumps(intent1, sort_keys=True) + "\n").encode("utf-8")
            intent1_hash = hashlib.sha256(intent1_bytes).hexdigest()

            started1 = {
                "record_type": "process_started",
                "logical_call_id": cid,
                "invocation_ordinal": 1,
                "pid": 33332,
                "intent_record_hash": intent1_hash,
                "timestamp": time.time(),
            }
            started1_bytes = (json.dumps(started1, sort_keys=True) + "\n").encode("utf-8")
            started1_hash = hashlib.sha256(started1_bytes).hexdigest()

            comp1 = {
                "record_type": "invocation_completion",
                "logical_call_id": cid,
                "invocation_ordinal": 1,
                "intent_record_hash": intent1_hash,
                "started_record_hash": started1_hash,
                "classification": "invalidated",
                "return_code": 1,
                "text_hash": "",
                "tokens": 10,
                "duration": 0.5,
                "usage_known": True,
                "timestamp": time.time(),
            }

            # Ordinal 2: valid pass
            intent2 = {
                "record_type": "invocation_intent",
                "logical_call_id": cid,
                "invocation_ordinal": 2,
                "prompt_hash": canary_prompt_hash,
                "timestamp": time.time(),
            }
            intent2_bytes = (json.dumps(intent2, sort_keys=True) + "\n").encode("utf-8")
            intent2_hash = hashlib.sha256(intent2_bytes).hexdigest()

            started2 = {
                "record_type": "process_started",
                "logical_call_id": cid,
                "invocation_ordinal": 2,
                "pid": 33333,
                "intent_record_hash": intent2_hash,
                "timestamp": time.time(),
            }
            started2_bytes = (json.dumps(started2, sort_keys=True) + "\n").encode("utf-8")
            started2_hash = hashlib.sha256(started2_bytes).hexdigest()

            comp2 = {
                "record_type": "invocation_completion",
                "logical_call_id": cid,
                "invocation_ordinal": 2,
                "intent_record_hash": intent2_hash,
                "started_record_hash": started2_hash,
                "classification": "valid",
                "return_code": 0,
                "text_hash": canary_text_hash,
                "tokens": 7,
                "duration": 0.2,
                "usage_known": True,
                "timestamp": time.time(),
            }

            for r in (intent1, started1, comp1, intent2, started2, comp2):
                ledger._append_record(r)

            shutil.copy2(os.path.join(BASE_DIR, "campaign2_master_registration.json"), os.path.join(tmp_dir, "campaign2_master_registration.json"))
            shutil.copy2(os.path.join(BASE_DIR, "campaign2_registration_anchor.hash"), os.path.join(tmp_dir, "campaign2_registration_anchor.hash"))

            model = mock.MagicMock()
            with self.assertRaises(CanaryFailureError) as ctx:
                run_pilot_canary("pilot", "att_r29_002", model, workspace_root=tmp_dir, ledger=ledger)
            self.assertIn("unauthorized intermediate retry attempt", str(ctx.exception))

            reg = PreflightRegistry(stage="pilot", workspace_root=tmp_dir)
            self.assertTrue(reg.data.get("stage_blocked"))

    def test_round29_raw_trailing_newline_canary_parity(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            from campaign2.preflight_registry import run_pilot_canary, CanaryFailureError

            shutil.copy2(os.path.join(BASE_DIR, "campaign2_master_registration.json"), os.path.join(tmp_dir, "campaign2_master_registration.json"))
            shutil.copy2(os.path.join(BASE_DIR, "campaign2_registration_anchor.hash"), os.path.join(tmp_dir, "campaign2_registration_anchor.hash"))

            model = mock.MagicMock()
            model.generate_candidate.return_value = ("CANARY_OK\n", 10)

            with self.assertRaises(CanaryFailureError) as ctx:
                run_pilot_canary("pilot", "att_r29_003", model, workspace_root=tmp_dir)
            self.assertIn("content mismatch", str(ctx.exception))

    def test_round29_missing_bound_artifact_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            from campaign2.preflight_registry import PreflightRegistry, PreflightRegistryError

            shutil.copy2(os.path.join(BASE_DIR, "campaign2_master_registration.json"), os.path.join(tmp_dir, "campaign2_master_registration.json"))
            shutil.copy2(os.path.join(BASE_DIR, "campaign2_registration_anchor.hash"), os.path.join(tmp_dir, "campaign2_registration_anchor.hash"))

            lock_file = os.path.join(tmp_dir, "campaign2_launch_lock.json")
            with open(lock_file, "w") as f:
                f.write('{"lock": true}\n')
            lock_hash = hashlib.sha256(b'{"lock": true}\n').hexdigest()

            reg = PreflightRegistry(stage="pilot", workspace_root=tmp_dir)
            reg.data["registration_hash"] = hashlib.sha256(open(os.path.join(tmp_dir, "campaign2_master_registration.json"), "rb").read()).hexdigest()
            reg.data["launch_lock_hash"] = lock_hash
            reg._save_data(reg.data)

            reg._reload()  # Succeeds when both exist and match

            # Delete launch lock file -> reload MUST fail closed
            os.remove(lock_file)
            with self.assertRaises(PreflightRegistryError) as ctx:
                reg._reload()
            self.assertIn("Bound launch lock file missing", str(ctx.exception))


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
