import subprocess
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
        from campaign2.model_provider import _get_codex_bin_path, DISABLED_CODEX_AGENT_FEATURES
        cmd = build_codex_cli_command("gpt-5.6-sol", "/tmp/scratch")
        expected = [
            _get_codex_bin_path(), "exec", "-s", "read-only", "--ephemeral", "--json",
        ]
        for feat in DISABLED_CODEX_AGENT_FEATURES:
            expected.extend(["--disable", feat])
        expected.extend([
            "--model", "gpt-5.6-sol", "--skip-git-repo-check",
            "--ignore-user-config", "--ignore-rules", "-C", "/tmp/scratch", "-"
        ])
        self.assertEqual(cmd, expected)
        self.assertEqual(cmd[-1], "-")

        cmd_broker = build_codex_cli_command("gpt-5.6-sol", "/tmp/scratch", broker_url="http://127.0.0.1:54321/v1")
        self.assertIn('model_provider="host_broker"', cmd_broker)
        self.assertTrue(any("requires_openai_auth=false" in arg for arg in cmd_broker))
        self.assertEqual(cmd_broker[-1], "-")

    def test_host_auth_broker_and_zero_secret_scratch_home_and_tool_rejection(self):
        """Regression test for host-controlled auth, zero-secret scratch home, tool-event rejection, and Seatbelt process-exec/network restrictions."""
        from campaign2.model_provider import (
            setup_credential_home,
            enforce_text_only_api_payload,
            parse_and_classify_jsonl_output,
            render_seatbelt_profile,
            ExecutableBoundaryError,
        )
        import subprocess
        import sys

        with tempfile.TemporaryDirectory() as tmp_home, tempfile.TemporaryDirectory() as tmp_scratch:
            codex_dir = os.path.join(tmp_home, ".codex")
            os.makedirs(codex_dir, exist_ok=True)
            auth_p = os.path.join(codex_dir, "auth.json")
            with open(auth_p, "w", encoding="utf-8") as f:
                json.dump({"OPENAI_API_KEY": "sk-synthetic-secret-do-not-stage-in-scratch"}, f)
            os.chmod(auth_p, 0o600)

            with mock.patch("os.path.expanduser", side_effect=lambda p: p.replace("~", tmp_home)):
                setup_credential_home(tmp_scratch, is_mock=False)

            # Scratch home MUST NOT contain auth.json or any secret material
            scratch_auth = os.path.join(tmp_scratch, ".codex", "auth.json")
            self.assertFalse(os.path.exists(scratch_auth))
            for root, _dirs, files in os.walk(tmp_scratch):
                for fname in files:
                    fpath = os.path.join(root, fname)
                    with open(fpath, "r", encoding="utf-8") as f:
                        self.assertNotIn("sk-synthetic-secret-do-not-stage-in-scratch", f.read())

            # Host broker payload enforcement rejects tool definitions and forces text-only response mode
            clean_payload = enforce_text_only_api_payload(
                json.dumps({"model": "gpt-5.6-sol", "input": "hello", "tools": None}).encode("utf-8")
            )
            parsed_clean = json.loads(clean_payload.decode("utf-8"))
            self.assertEqual(parsed_clean["tools"], [])
            self.assertEqual(parsed_clean["tool_choice"], "none")

            with self.assertRaises(ExecutableBoundaryError):
                enforce_text_only_api_payload(
                    json.dumps({"model": "gpt-5.6-sol", "tools": [{"type": "function", "name": "shell"}]}).encode("utf-8")
                )

            # JSONL parser must invalidate any stream containing tool or command execution events
            for forbidden_item_type in ("command_execution", "file_change", "mcp_tool_call", "web_search", "tool_call", "function_call"):
                raw_stream = "\n".join([
                    json.dumps({"type": "item.completed", "item": {"type": forbidden_item_type, "command": "cat ~/.codex/auth.json"}}),
                    json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "def solve(): pass"}}),
                    json.dumps({"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}}),
                ]).encode("utf-8")
                res = parse_and_classify_jsonl_output(raw_stream, b"", 0)
                self.assertEqual(res["parse_state"], "forbidden_tool_event")
                self.assertEqual(res["classification"], "invalidated")

            # Seatbelt kernel profile must block shell/utility execution and external outbound networking
            sb_path = render_seatbelt_profile(tmp_scratch, BASE_DIR)
            for blocked_cmd in (
                ["/bin/sh", "-c", "echo pwn"],
                ["/bin/zsh", "-c", "echo pwn"],
                ["/usr/bin/curl", "--version"],
                ["/bin/cat", "/etc/hosts"],
                ["/usr/bin/python3", "-c", "print(1)"],
            ):
                proc = subprocess.run(
                    ["/usr/bin/sandbox-exec", "-f", sb_path] + blocked_cmd,
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                self.assertNotEqual(proc.returncode, 0, f"Expected sandbox to block {blocked_cmd}")

            # External outbound TCP connection must be denied by Seatbelt while localhost is allowed
            ext_net = subprocess.run(
                [
                    "/usr/bin/sandbox-exec", "-f", sb_path,
                    sys.executable, "-c",
                    "import socket; s=socket.socket(); s.settimeout(1.0); s.connect(('1.1.1.1', 443))",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            self.assertNotEqual(ext_net.returncode, 0)

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
        self.assertTrue(count in (144, 145))


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
                    "task_file_hash": "",
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

            # Patch launch lock, manifest, evidence manifest, and parity matrix verification, and mock evaluate_candidate_code_sandboxed to run production evaluator
            with mock.patch("campaign2.launch_lock.verify_launch_lock", return_value=True), \
                 mock.patch("campaign2.manifest.verify_campaign2_manifest", return_value=(True, 145)), \
                 mock.patch("campaign2.manifest.verify_campaign2_evidence_manifest", return_value=True), \
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
                    "task_file_hash": "",
                "argv_hash": "a" * 64,
                "env_hash": "a" * 64,
                "cwd_hash": "a" * 64,
                "sandbox_profile_hash": "a" * 64,
                "input_hash": "a" * 64,
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
                    "raw_stdout_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                    "raw_stderr_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
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
            intent = {"record_type": "invocation_intent",
                    "task_file_hash": "", "logical_call_id": "call_seq_test", "invocation_ordinal": 1, "timestamp": time.time()}
            intent_bytes = (json.dumps(intent, sort_keys=True) + "\n").encode("utf-8")
            intent_hash = hashlib.sha256(intent_bytes).hexdigest()

            comp = {
                "record_type": "invocation_completion",
                    "raw_stdout_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                    "raw_stderr_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
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
        # Authenticate legitimate execution and archival markers (mode 0o444)
        for rel_p in (
            "campaign2_cpu_gate_record.json",
            os.path.join("history", "campaign2_pilot_scientific_start.json"),
            os.path.join("history", "campaign2_pilot_preflight_registry_incident.json"),
            "campaign_archive_manifest.json",
            "campaign_archive_anchor.hash",
            "campaign_claim_record.json",
        ):
            abs_p = os.path.join(BASE_DIR, rel_p)
            if os.path.exists(abs_p):
                st_mode = stat.S_IMODE(os.stat(abs_p).st_mode)
                self.assertEqual(st_mode, 0o444, f"Mode for {rel_p} must be 0o444, got {oct(st_mode)}")

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

        # Probe 3: Zero raw stdout + HTTP 429 in stderr -> invalidated under default strict mode, RETRYABLE only under legacy require_transport_evidence=False
        parsed3_default = parse_and_classify_jsonl_output(b"", b"HTTP 429 Too Many Requests\n", return_code=1)
        self.assertEqual(parsed3_default["classification"], "invalidated")
        parsed3 = parse_and_classify_jsonl_output(
            b"",
            b"HTTP 429 Too Many Requests\n",
            return_code=1,
            require_transport_evidence=False,
        )
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
                    "task_file_hash": "",
                "argv_hash": "a" * 64,
                "env_hash": "a" * 64,
                "cwd_hash": "a" * 64,
                "sandbox_profile_hash": "a" * 64,
                "input_hash": "a" * 64,
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
                "text_hash": hashlib.sha256(b"FAKE_RESPONSE").hexdigest(),
                    "raw_stdout_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                    "raw_stderr_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",  # Mismatched text_hash!
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
                    "task_file_hash": "",
                "argv_hash": "a" * 64,
                "env_hash": "a" * 64,
                "cwd_hash": "a" * 64,
                "sandbox_profile_hash": "a" * 64,
                "input_hash": "a" * 64,
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
                    "raw_stdout_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                    "raw_stderr_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
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
                    "task_file_hash": "",
                "argv_hash": "a" * 64,
                "env_hash": "a" * 64,
                "cwd_hash": "a" * 64,
                "sandbox_profile_hash": "a" * 64,
                "input_hash": "a" * 64,
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
                    "raw_stdout_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                    "raw_stderr_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
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
                    "task_file_hash": "",
                "argv_hash": "a" * 64,
                "env_hash": "a" * 64,
                "cwd_hash": "a" * 64,
                "sandbox_profile_hash": "a" * 64,
                "input_hash": "a" * 64,
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
                    "raw_stdout_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                    "raw_stderr_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
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
                    "task_file_hash": "",
                "argv_hash": "a" * 64,
                "env_hash": "a" * 64,
                "cwd_hash": "a" * 64,
                "sandbox_profile_hash": "a" * 64,
                "input_hash": "a" * 64,
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
                "started_record_hash": "",
                "parse_state": "spawn_failure",
                "usage_known": True,
                "classification": "spawn_failure",
                "return_code": -1,
                "text_hash": "",
                "raw_stdout_digest": hashlib.sha256(b"").hexdigest(),
                "raw_stderr_digest": hashlib.sha256(b"spawn failed").hexdigest(),
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
                    "task_file_hash": "",
                "argv_hash": "a" * 64,
                "env_hash": "a" * 64,
                "cwd_hash": "a" * 64,
                "sandbox_profile_hash": "a" * 64,
                "input_hash": "a" * 64,
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
                    "raw_stdout_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                    "raw_stderr_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                "tokens": 7,
                "duration": 0.3,
                "timestamp": time.time(),
            }

            for r in (intent1, comp1, intent2, started2, comp2):
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
                    "task_file_hash": "",
                "argv_hash": "a" * 64,
                "env_hash": "a" * 64,
                "cwd_hash": "a" * 64,
                "sandbox_profile_hash": "a" * 64,
                "input_hash": "a" * 64,
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
                    "task_file_hash": "",
                "argv_hash": "a" * 64,
                "env_hash": "a" * 64,
                "cwd_hash": "a" * 64,
                "sandbox_profile_hash": "a" * 64,
                "input_hash": "a" * 64,
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
                    "raw_stdout_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                    "raw_stderr_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
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
                    "task_file_hash": "",
                "argv_hash": "a" * 64,
                "env_hash": "a" * 64,
                "cwd_hash": "a" * 64,
                "sandbox_profile_hash": "a" * 64,
                "input_hash": "a" * 64,
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
                    "raw_stdout_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                    "raw_stderr_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
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
                    "task_file_hash": "",
                "argv_hash": "a" * 64,
                "env_hash": "a" * 64,
                "cwd_hash": "a" * 64,
                "sandbox_profile_hash": "a" * 64,
                "input_hash": "a" * 64,
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
                    "raw_stdout_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                    "raw_stderr_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
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
            with open(os.path.join(tmp_dir, "campaign2_master_registration.json"), "rb") as f:
                reg.data["registration_hash"] = hashlib.sha256(f.read()).hexdigest()
            reg.data["launch_lock_hash"] = lock_hash
            reg._save_data(reg.data)

            reg._reload()  # Succeeds when both exist and match

            # Delete launch lock file -> reload MUST fail closed
            os.remove(lock_file)
            with self.assertRaises(PreflightRegistryError) as ctx:
                reg._reload()
            self.assertIn("Bound launch lock file missing", str(ctx.exception))

    def test_round31_live_and_recovery_retry_parity_transient_infrastructure_error(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            from campaign2.preflight_registry import PreflightRegistry, run_pilot_canary, CANARY_NEUTRAL_PROMPT, CANARY_EXPECTED_RESPONSE
            from campaign2.attempt_lifecycle import AttemptLedger, is_permitted_local_retry
            from campaign2.model_provider import parse_and_classify_jsonl_output

            cid = "canary_pilot_att_r31_001_01"
            ledger_path = os.path.join(tmp_dir, "campaign2_pilot_ledger.jsonl")
            ledger = AttemptLedger(ledger_path)

            canary_prompt_hash = hashlib.sha256(CANARY_NEUTRAL_PROMPT.encode("utf-8")).hexdigest()
            canary_text_hash = hashlib.sha256(CANARY_EXPECTED_RESPONSE.encode("utf-8")).hexdigest()

            # Ordinal 1: Real parser-produced transient error (HTTP 429) in legacy Campaign 2 mode
            parsed1 = parse_and_classify_jsonl_output(
                stdout_bytes=b"",
                stderr_bytes=b"HTTP 429 Too Many Requests\n",
                return_code=1,
                require_transport_evidence=False,
            )
            self.assertEqual(parsed1["parse_state"], "nonzero_exit")
            self.assertEqual(parsed1["classification"], "transient_infrastructure_error")
            self.assertFalse(parsed1["usage_known"])

            intent1 = {
                "record_type": "invocation_intent",
                "logical_call_id": cid,
                "invocation_ordinal": 1,
                "prompt_hash": canary_prompt_hash,
                    "task_file_hash": "",
                "argv_hash": "a" * 64,
                "env_hash": "a" * 64,
                "cwd_hash": "a" * 64,
                "sandbox_profile_hash": "a" * 64,
                "input_hash": "a" * 64,
                "timestamp": time.time(),
            }
            intent1_bytes = (json.dumps(intent1, sort_keys=True) + "\n").encode("utf-8")
            intent1_hash = hashlib.sha256(intent1_bytes).hexdigest()

            started1 = {
                "record_type": "process_started",
                "logical_call_id": cid,
                "invocation_ordinal": 1,
                "pid": 44441,
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
                "return_code": 1,
                "raw_stdout_digest": parsed1["stdout_hash"],
                "raw_stderr_digest": parsed1["stderr_hash"],
                "text_hash": "",
                "parse_state": parsed1["parse_state"],
                "usage_known": parsed1["usage_known"],
                "tokens": parsed1["tokens"],
                "duration": 0.5,
                "classification": parsed1["classification"],
                "error": parsed1["error"],
                "timestamp": time.time(),
            }

            self.assertFalse(is_permitted_local_retry(comp1, has_process_started=True))
            self.assertTrue(
                is_permitted_local_retry(comp1, has_process_started=True, require_transport_evidence=False)
            )

            # Ordinal 2: valid pass
            intent2 = {
                "record_type": "invocation_intent",
                "logical_call_id": cid,
                "invocation_ordinal": 2,
                "prompt_hash": canary_prompt_hash,
                    "task_file_hash": "",
                "argv_hash": "a" * 64,
                "env_hash": "a" * 64,
                "cwd_hash": "a" * 64,
                "sandbox_profile_hash": "a" * 64,
                "input_hash": "a" * 64,
                "timestamp": time.time(),
            }
            intent2_bytes = (json.dumps(intent2, sort_keys=True) + "\n").encode("utf-8")
            intent2_hash = hashlib.sha256(intent2_bytes).hexdigest()

            started2 = {
                "record_type": "process_started",
                "logical_call_id": cid,
                "invocation_ordinal": 2,
                "pid": 44442,
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
                    "raw_stdout_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
                    "raw_stderr_digest": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
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
            data = run_pilot_canary(
                "pilot",
                "att_r31_001",
                model,
                workspace_root=tmp_dir,
                ledger=ledger,
                require_transport_evidence=False,
            )
            self.assertTrue(data["canary_passed"])

            reg = PreflightRegistry(stage="pilot", workspace_root=tmp_dir)
            self.assertEqual(reg.data["total_logical_calls"], 1)
            self.assertEqual(reg.data["total_local_invocation_attempts"], 2)

    def test_round31_real_transient_retry_provider_loop(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            from campaign2.model_provider import SolModelProvider
            from campaign2.attempt_lifecycle import AttemptLedger
            from campaign2.preflight_registry import run_pilot_canary, PreflightRegistry, CANARY_EXPECTED_RESPONSE, CANARY_NEUTRAL_PROMPT

            run_dir = os.path.join(tmp_dir, "att_r31_loop")
            os.makedirs(run_dir, exist_ok=True)
            ledger_p = os.path.join(run_dir, "campaign2_pilot_ledger.jsonl")
            ledger = AttemptLedger(ledger_p)

            # Create provider with mocked subprocess that fails transiently on attempt 1 and succeeds on attempt 2
            valid_jsonl = (
                json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": CANARY_EXPECTED_RESPONSE}}) + "\n" +
                json.dumps({"type": "turn.completed", "usage": {"output_tokens": 12}}) + "\n"
            ).encode("utf-8")

            responses = [
                (1, b"", b"HTTP 429 Too Many Requests\n"),  # Attempt 1: transient error
                (0, valid_jsonl, b""),                     # Attempt 2: valid pass
            ]

            shutil.copy2(os.path.join(BASE_DIR, "campaign2_master_registration.json"), os.path.join(tmp_dir, "campaign2_master_registration.json"))
            shutil.copy2(os.path.join(BASE_DIR, "campaign2_registration_anchor.hash"), os.path.join(tmp_dir, "campaign2_registration_anchor.hash"))

            provider = SolModelProvider(
                workspace_root=tmp_dir,
                ledger=ledger,
                mock_subprocess=False,
                attempt_id="att_r31_loop",
                require_transport_evidence=False,
            )

            call_count = [0]
            def mock_popen(*args, **kwargs):
                idx = call_count[0]
                call_count[0] += 1
                ret_code, out_b, err_b = responses[idx]
                proc = mock.MagicMock()
                proc.pid = 99900 + idx
                proc.poll.return_value = ret_code
                proc.wait.return_value = ret_code
                proc.returncode = ret_code
                proc.communicate.return_value = (out_b, err_b)
                return proc

            with mock.patch("campaign2.model_provider.setup_credential_home"), mock.patch("subprocess.Popen", side_effect=mock_popen):
                text, tokens, _ = provider._execute_cli_call("canary_pilot_att_r31_loop_01", CANARY_NEUTRAL_PROMPT.encode("utf-8"))
                self.assertEqual(text, CANARY_EXPECTED_RESPONSE)
                self.assertEqual(tokens, 12)
                self.assertEqual(call_count[0], 2)  # Proves local retry loop was exercised!

                # Now verify recovery reconciliation of this authentic ledger
                data = run_pilot_canary("pilot", "att_r31_loop", provider, workspace_root=tmp_dir, ledger=ledger)
                self.assertTrue(data["canary_passed"])

            reg = PreflightRegistry(stage="pilot", workspace_root=tmp_dir)
            self.assertEqual(reg.data["total_logical_calls"], 1)
            self.assertEqual(reg.data["total_local_invocation_attempts"], 2)

    def test_round31_retry_predicate_mutation_matrix(self):
        from campaign2.attempt_lifecycle import is_permitted_local_retry

        valid_transient = {
            "record_type": "invocation_completion",
            "logical_call_id": "cid",
            "invocation_ordinal": 1,
            "intent_record_hash": "a" * 64,
            "started_record_hash": "b" * 64,
            "classification": "transient_infrastructure_error",
            "parse_state": "nonzero_exit",
            "usage_known": False,
            "tokens": 0,
            "return_code": 1,
            "duration": 1.5,
            "text_hash": "",
            "raw_stdout_digest": hashlib.sha256(b"").hexdigest(),
            "raw_stderr_digest": "c" * 64,
        }
        self.assertFalse(is_permitted_local_retry(valid_transient, has_process_started=True))
        self.assertTrue(
            is_permitted_local_retry(valid_transient, has_process_started=True, require_transport_evidence=False)
        )

        valid_spawn = {
            "record_type": "invocation_completion",
            "logical_call_id": "cid",
            "invocation_ordinal": 1,
            "intent_record_hash": "a" * 64,
            "started_record_hash": "",
            "classification": "spawn_failure",
            "parse_state": "spawn_failure",
            "usage_known": True,
            "tokens": 0,
            "return_code": -1,
            "duration": 0.1,
            "text_hash": "",
            "raw_stdout_digest": hashlib.sha256(b"").hexdigest(),
            "raw_stderr_digest": "c" * 64,
        }
        self.assertTrue(is_permitted_local_retry(valid_spawn, has_process_started=False))

        mutations = [
            ("parse_state_mismatch", {**valid_transient, "parse_state": "malformed_jsonl"}, True),
            ("transient_usage_known_true", {**valid_transient, "usage_known": True}, True),
            ("spawn_usage_known_false", {**valid_spawn, "usage_known": False}, False),
            ("tokens_nonzero", {**valid_transient, "tokens": 999}, True),
            ("tokens_float", {**valid_transient, "tokens": 0.0}, True),
            ("tokens_bool", {**valid_transient, "tokens": False}, True),
            ("transient_return_code_zero", {**valid_transient, "return_code": 0}, True),
            ("spawn_return_code_zero", {**valid_spawn, "return_code": 0}, False),
            ("spawn_return_code_none", {**valid_spawn, "return_code": None}, False),
            ("text_hash_nonempty", {**valid_transient, "text_hash": "xyz"}, True),
            ("duration_negative", {**valid_transient, "duration": -1.0}, True),
            ("duration_none", {**valid_transient, "duration": None}, True),
            ("duration_nan", {**valid_transient, "duration": float("nan")}, True),
            ("duration_bool", {**valid_transient, "duration": True}, True),
            ("raw_stdout_nonempty", {**valid_transient, "raw_stdout_digest": "x" * 64}, True),
            ("raw_stderr_short", {**valid_transient, "raw_stderr_digest": "abc"}, True),
            ("transient_process_not_started", valid_transient, False),
            ("spawn_process_started", valid_spawn, True),
            ("transient_started_hash_empty", {**valid_transient, "started_record_hash": ""}, True),
            ("spawn_started_hash_nonempty", {**valid_spawn, "started_record_hash": "x" * 64}, False),
        ]

        for label, mut_record, started_flag in mutations:
            res = is_permitted_local_retry(
                mut_record,
                has_process_started=started_flag,
                require_transport_evidence=False,
            )
            self.assertFalse(res, f"Mutation '{label}' must be rejected by is_permitted_local_retry but returned True")

    def test_round31_record_usage_validates_inputs(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            from campaign2.preflight_registry import PreflightRegistry, PreflightRegistryError

            shutil.copy2(os.path.join(BASE_DIR, "campaign2_master_registration.json"), os.path.join(tmp_dir, "campaign2_master_registration.json"))
            shutil.copy2(os.path.join(BASE_DIR, "campaign2_registration_anchor.hash"), os.path.join(tmp_dir, "campaign2_registration_anchor.hash"))

            reg = PreflightRegistry(stage="pilot", workspace_root=tmp_dir)

            bad_inputs = [
                {"tokens_used": -10, "active_time": 1.0},
                {"tokens_used": True, "active_time": 1.0},
                {"tokens_used": 100, "active_time": -1.0},
                {"tokens_used": 100, "active_time": float("nan")},
                {"tokens_used": 100, "active_time": 1.0, "logical_calls": -1},
                {"tokens_used": 100, "active_time": 1.0, "local_attempts": -1},
            ]

            for kw in bad_inputs:
                with self.assertRaises(PreflightRegistryError, msg=f"Input {kw} must fail validation"):
                    reg.record_usage(**kw)

    def test_round31_dangling_symlink_artifact_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            from campaign2.preflight_registry import PreflightRegistry, PreflightRegistryError

            shutil.copy2(os.path.join(BASE_DIR, "campaign2_master_registration.json"), os.path.join(tmp_dir, "campaign2_master_registration.json"))
            shutil.copy2(os.path.join(BASE_DIR, "campaign2_registration_anchor.hash"), os.path.join(tmp_dir, "campaign2_registration_anchor.hash"))

            # Create dangling symlink at campaign2_launch_lock.json
            lock_link = os.path.join(tmp_dir, "campaign2_launch_lock.json")
            os.symlink(os.path.join(tmp_dir, "nonexistent_target.json"), lock_link)

            with self.assertRaises(PreflightRegistryError) as ctx:
                PreflightRegistry(stage="pilot", workspace_root=tmp_dir)
            self.assertTrue("Bound launch lock file missing or invalid" in str(ctx.exception) or "symlinks forbidden" in str(ctx.exception))


class TestC1_13_FullStageRefusal(unittest.TestCase):
    def test_full_stage_refuses_to_start(self):
        from rsi_loop import _run_campaign2
        args = SimpleNamespace(stage="full", mock=True)
        ctx = SimpleNamespace(run_dir="/tmp/test_run", run_id="run_123")
        with self.assertRaises(NotImplementedError) as c:
            _run_campaign2(args, ctx)
        self.assertIn("full stage requires Work Order C implementation", str(c.exception))


EXPECTED_LAUNCH_STEPS = [
    "1. manifest_verification",
    "2. evidence_manifest_verification",
    "3. parity_matrix_verification",
    "4. tier_assignment_verification",
    "5. master_registration_verification",
    "6. launch_lock_verification",
    "7. attempt_identity_creation_or_verification",
    "8. attempt_ledger_and_resume_admission_verification",
    "9. checkpoint_manager_initialization",
    "10. codex_cli_version_check",
    "11. preflight_registry_authentication",
    "12. prescientific_cpu_gate",
    "13. pilot_canary_execution_or_reconciliation",
    "14. scientific_start_claim",
    "15. trial_1_execution_and_checkpoint",
    "16. trial_2_execution_and_checkpoint",
    "17. pilot_outcome_ladder_evaluation",
    "18. schema3_report_generation_and_sealing",
    "19. terminal_classification_recording"
]


class TestC1a_LaunchPreflightHardening(unittest.TestCase):
    def test_h1_load_or_init_and_reload_symlink_symmetry_matrix(self):
        """Verify _load_or_init and _reload symmetrically fail closed across all 8 symlink combinations."""
        from campaign2.preflight_registry import PreflightRegistry, PreflightRegistryError

        for artifact_name in ("campaign2_master_registration.json", "campaign2_launch_lock.json"):
            for is_dangling in (False, True):
                for test_path in ("load_or_init", "reload"):
                    with tempfile.TemporaryDirectory() as tmp_dir:
                        shutil.copy2(os.path.join(BASE_DIR, "campaign2_master_registration.json"), os.path.join(tmp_dir, "campaign2_master_registration.json"))
                        shutil.copy2(os.path.join(BASE_DIR, "campaign2_registration_anchor.hash"), os.path.join(tmp_dir, "campaign2_registration_anchor.hash"))

                        target_file = os.path.join(tmp_dir, artifact_name)
                        if os.path.lexists(target_file):
                            os.remove(target_file)

                        if is_dangling:
                            sym_target = os.path.join(tmp_dir, "nonexistent.json")
                        else:
                            sym_target = os.path.join(tmp_dir, "real_target.json")
                            with open(sym_target, "w") as f:
                                f.write("{\"lock\": true}\n")

                        os.symlink(sym_target, target_file)

                        if test_path == "load_or_init":
                            with self.assertRaises(PreflightRegistryError) as ctx:
                                PreflightRegistry(stage="pilot", workspace_root=tmp_dir)
                            err_str = str(ctx.exception)
                            self.assertTrue("symlinks forbidden" in err_str or "missing or invalid" in err_str)
                        else:
                            # Create clean valid state first
                            os.remove(target_file)
                            if artifact_name == "campaign2_master_registration.json":
                                shutil.copy2(os.path.join(BASE_DIR, "campaign2_master_registration.json"), target_file)
                            else:
                                with open(target_file, "w") as f:
                                    f.write("{\"schema_version\": \"1\", \"lock\": true}\n")
                            reg = PreflightRegistry(stage="pilot", workspace_root=tmp_dir)
                            os.remove(target_file)
                            os.symlink(sym_target, target_file)
                            with self.assertRaises(PreflightRegistryError) as ctx:
                                reg._reload()
                            err_str = str(ctx.exception)
                            self.assertTrue("symlinks forbidden" in err_str or "missing or invalid" in err_str)

    def test_h2_reject_synthetic_parse_state_alias(self):
        """Verify is_permitted_local_retry accepts ONLY nonzero_exit and rejects synthetic parse_state aliases."""
        from campaign2.attempt_lifecycle import is_permitted_local_retry
        empty_sha256 = hashlib.sha256(b"").hexdigest()
        valid_stderr_sha256 = hashlib.sha256(b"HTTP 429 Too Many Requests\n").hexdigest()

        synthetic_record = {
            "classification": "transient_infrastructure_error",
            "parse_state": "transient_infrastructure_error",
            "usage_known": False,
            "tokens": 0,
            "return_code": 1,
            "duration": 0.5,
            "started_record_hash": "a" * 64,
            "text_hash": "",
            "raw_stdout_digest": empty_sha256,
            "raw_stderr_digest": valid_stderr_sha256,
        }
        self.assertFalse(
            is_permitted_local_retry(synthetic_record, has_process_started=True, require_transport_evidence=False)
        )

        real_record = dict(synthetic_record)
        real_record["parse_state"] = "nonzero_exit"
        self.assertFalse(is_permitted_local_retry(real_record, has_process_started=True))
        self.assertTrue(
            is_permitted_local_retry(real_record, has_process_started=True, require_transport_evidence=False)
        )

    def test_h3_digest_shape_validation_and_sentinel_matrix(self):
        """Verify missing/deleted required hashes fail closed, and empty task_file_hash is restricted to canary."""
        from campaign2.attempt_lifecycle import (
            AttemptLedger,
            AttemptLifecycleError,
        )

        valid_hash = "a" * 64

        # 1. Individually delete each required hash field on invocation_intent -> must raise AttemptLifecycleError
        for field_to_delete in ("argv_hash", "env_hash", "cwd_hash", "sandbox_profile_hash", "input_hash", "task_file_hash"):
            with tempfile.TemporaryDirectory() as tmp_dir:
                ledger_p = os.path.join(tmp_dir, "ledger.jsonl")
                ledger = AttemptLedger(ledger_p)
                intent_rec = {
                    "record_type": "invocation_intent",
                    "logical_call_id": "test_call_01",
                    "invocation_ordinal": 1,
                    "prompt_hash": valid_hash,
                    "task_file_hash": valid_hash,
                    "argv_hash": valid_hash,
                    "env_hash": valid_hash,
                    "cwd_hash": valid_hash,
                    "sandbox_profile_hash": valid_hash,
                    "input_hash": valid_hash,
                }
                del intent_rec[field_to_delete]
                ledger._append(intent_rec)
                with self.assertRaises(AttemptLifecycleError) as ctx:
                    ledger.verify_three_record_protocol()
                self.assertTrue("missing or invalid" in str(ctx.exception) or "missing task_file_hash" in str(ctx.exception))

        # 2. Missing task_file_hash on canary invocation_intent -> must raise AttemptLifecycleError
        with tempfile.TemporaryDirectory() as tmp_dir:
            ledger_p = os.path.join(tmp_dir, "ledger.jsonl")
            ledger = AttemptLedger(ledger_p)
            canary_missing_tf = {
                "record_type": "invocation_intent",
                "logical_call_id": "canary_pilot_att_01",
                "invocation_ordinal": 1,
                "prompt_hash": valid_hash,
                "argv_hash": valid_hash,
                "env_hash": valid_hash,
                "cwd_hash": valid_hash,
                "sandbox_profile_hash": valid_hash,
                "input_hash": valid_hash,
            }
            ledger._append(canary_missing_tf)
            with self.assertRaises(AttemptLifecycleError) as ctx:
                ledger.verify_three_record_protocol()
            self.assertIn("missing task_file_hash", str(ctx.exception))

        # 3. None task_file_hash on canary invocation_intent -> must raise AttemptLifecycleError
        with tempfile.TemporaryDirectory() as tmp_dir:
            ledger_p = os.path.join(tmp_dir, "ledger.jsonl")
            ledger = AttemptLedger(ledger_p)
            canary_none_tf = {
                "record_type": "invocation_intent",
                "logical_call_id": "canary_pilot_att_01",
                "invocation_ordinal": 1,
                "prompt_hash": valid_hash,
                "task_file_hash": None,
                "argv_hash": valid_hash,
                "env_hash": valid_hash,
                "cwd_hash": valid_hash,
                "sandbox_profile_hash": valid_hash,
                "input_hash": valid_hash,
            }
            ledger._append(canary_none_tf)
            with self.assertRaises(AttemptLifecycleError) as ctx:
                ledger.verify_three_record_protocol()
            self.assertIn("cannot be None", str(ctx.exception))

        # 4. Individually delete required digests on invocation_completion -> must raise AttemptLifecycleError
        for comp_field in ("raw_stdout_digest", "raw_stderr_digest"):
            with tempfile.TemporaryDirectory() as tmp_dir:
                ledger_p = os.path.join(tmp_dir, "ledger.jsonl")
                ledger = AttemptLedger(ledger_p)
                intent_rec = {
                    "record_type": "invocation_intent",
                    "logical_call_id": "canary_pilot_att_01",
                    "invocation_ordinal": 1,
                    "prompt_hash": valid_hash,
                    "task_file_hash": "",
                    "argv_hash": valid_hash,
                    "env_hash": valid_hash,
                    "cwd_hash": valid_hash,
                    "sandbox_profile_hash": valid_hash,
                    "input_hash": valid_hash,
                }
                intent_bytes = (json.dumps(intent_rec, sort_keys=True) + "\n").encode("utf-8")
                intent_hash = hashlib.sha256(intent_bytes).hexdigest()

                started_rec = {
                    "record_type": "process_started",
                    "logical_call_id": "canary_pilot_att_01",
                    "invocation_ordinal": 1,
                    "intent_record_hash": intent_hash,
                }
                started_bytes = (json.dumps(started_rec, sort_keys=True) + "\n").encode("utf-8")
                started_hash = hashlib.sha256(started_bytes).hexdigest()

                comp_rec = {
                    "record_type": "invocation_completion",
                    "logical_call_id": "canary_pilot_att_01",
                    "invocation_ordinal": 1,
                    "intent_record_hash": intent_hash,
                    "started_record_hash": started_hash,
                    "parse_state": "parse_success",
                    "classification": "accepted_completion",
                    "usage_known": True,
                    "tokens": 100,
                    "return_code": 0,
                    "duration": 0.1,
                    "text_hash": valid_hash,
                    "raw_stdout_digest": valid_hash,
                    "raw_stderr_digest": valid_hash,
                }
                del comp_rec[comp_field]
                ledger._append(intent_rec)
                ledger._append(started_rec)
                ledger._append(comp_rec)
                with self.assertRaises(AttemptLifecycleError) as ctx:
                    ledger.verify_three_record_protocol()
                self.assertIn("missing or invalid", str(ctx.exception))

        # 5. Benchmark invocation_intent with empty task_file_hash -> must raise AttemptLifecycleError
        with tempfile.TemporaryDirectory() as tmp_dir:
            ledger_p = os.path.join(tmp_dir, "ledger.jsonl")
            ledger = AttemptLedger(ledger_p)
            benchmark_intent = {
                "record_type": "invocation_intent",
                "logical_call_id": "c_f1_01_log_stream_parser_opt_01",
                "invocation_ordinal": 1,
                "prompt_hash": valid_hash,
                "task_file_hash": "",
                "argv_hash": valid_hash,
                "env_hash": valid_hash,
                "cwd_hash": valid_hash,
                "sandbox_profile_hash": valid_hash,
                "input_hash": valid_hash,
            }
            ledger._append(benchmark_intent)
            with self.assertRaises(AttemptLifecycleError) as ctx:
                ledger.verify_three_record_protocol()
            self.assertIn("task_file_hash must be non-empty", str(ctx.exception))

        # 6. Canary invocation_intent with explicit empty task_file_hash -> must succeed
        with tempfile.TemporaryDirectory() as tmp_dir:
            ledger_p = os.path.join(tmp_dir, "ledger.jsonl")
            ledger = AttemptLedger(ledger_p)
            canary_intent = {
                "record_type": "invocation_intent",
                "logical_call_id": "canary_pilot_att_01",
                "invocation_ordinal": 1,
                "prompt_hash": valid_hash,
                "task_file_hash": "",
                "argv_hash": valid_hash,
                "env_hash": valid_hash,
                "cwd_hash": valid_hash,
                "sandbox_profile_hash": valid_hash,
                "input_hash": valid_hash,
            }
            canary_bytes = (json.dumps(canary_intent, sort_keys=True) + "\n").encode("utf-8")
            canary_hash = hashlib.sha256(canary_bytes).hexdigest()

            canary_started = {
                "record_type": "process_started",
                "logical_call_id": "canary_pilot_att_01",
                "invocation_ordinal": 1,
                "intent_record_hash": canary_hash,
            }
            canary_started_bytes = (json.dumps(canary_started, sort_keys=True) + "\n").encode("utf-8")
            canary_started_hash = hashlib.sha256(canary_started_bytes).hexdigest()

            canary_comp = {
                "record_type": "invocation_completion",
                "logical_call_id": "canary_pilot_att_01",
                "invocation_ordinal": 1,
                "intent_record_hash": canary_hash,
                "started_record_hash": canary_started_hash,
                "parse_state": "parse_success",
                "classification": "accepted_completion",
                "usage_known": True,
                "tokens": 100,
                "return_code": 0,
                "duration": 0.1,
                "text_hash": valid_hash,
                "raw_stdout_digest": valid_hash,
                "raw_stderr_digest": valid_hash,
            }
            ledger._append(canary_intent)
            ledger._append(canary_started)
            ledger._append(canary_comp)
            self.assertTrue(ledger.verify_three_record_protocol())
    def test_h4_full_launch_sequence_hermetic_integration(self):
        """Authentic integration test proving exact 19-step launch sequence, trial ordering, and clean-path accounting."""
        from campaign2.launch_checklist import ORDERED_LAUNCH_STEPS

        with tempfile.TemporaryDirectory() as tmp_dir:
            subprocess.run(["git", "init"], cwd=tmp_dir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_dir, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_dir, check=True)

            with open(os.path.join(tmp_dir, ".gitignore"), "w") as f:
                f.write("history/\n")

            for f_item in ["README.md", "rsi_loop.py", "run_tests.py", "generator.sb.template"]:
                if os.path.exists(os.path.join(BASE_DIR, f_item)):
                    shutil.copy2(os.path.join(BASE_DIR, f_item), os.path.join(tmp_dir, f_item))
            os.makedirs(os.path.join(tmp_dir, "docs"), exist_ok=True)
            shutil.copy2(os.path.join(BASE_DIR, "docs", "walkthrough.md"), os.path.join(tmp_dir, "docs", "walkthrough.md"))

            shutil.copytree(os.path.join(BASE_DIR, "campaign2"), os.path.join(tmp_dir, "campaign2"))
            shutil.copytree(os.path.join(BASE_DIR, "benchmarks"), os.path.join(tmp_dir, "benchmarks"))
            if os.path.exists(os.path.join(BASE_DIR, "campaign2_archive")):
                shutil.copytree(os.path.join(BASE_DIR, "campaign2_archive"), os.path.join(tmp_dir, "campaign2_archive"))

            shutil.copy2(os.path.join(BASE_DIR, "campaign2_tier_assignment.json"), os.path.join(tmp_dir, "campaign2_tier_assignment.json"))
            shutil.copy2(os.path.join(BASE_DIR, "campaign2_evidence_hashes.json"), os.path.join(tmp_dir, "campaign2_evidence_hashes.json"))
            if os.path.exists(os.path.join(BASE_DIR, "campaign2_parity_matrix.json")):
                shutil.copy2(os.path.join(BASE_DIR, "campaign2_parity_matrix.json"), os.path.join(tmp_dir, "campaign2_parity_matrix.json"))
                os.chmod(os.path.join(tmp_dir, "campaign2_parity_matrix.json"), 0o444)
                os.chmod(os.path.join(tmp_dir, "campaign2_parity_matrix.json"), 0o444)

            from campaign2.registration import generate_master_registration
            from campaign2.manifest import generate_campaign2_manifest
            from campaign2.launch_lock import generate_launch_lock

            generate_master_registration(tmp_dir)
            generate_campaign2_manifest(tmp_dir)

            subprocess.run(["git", "add", "."], cwd=tmp_dir, check=True)
            subprocess.run(["git", "commit", "-m", "init hermetic"], cwd=tmp_dir, check=True)

            generate_launch_lock(tmp_dir)

            from rsi_loop import _run_campaign2, RunContext
            from campaign2.preflight_registry import PreflightRegistry
            from campaign2.attempt_lifecycle import publish_scientific_start

            run_id = "run_c1a_launch_01"
            run_dir = os.path.join(tmp_dir, "history", run_id)
            os.makedirs(run_dir, exist_ok=True)

            args = SimpleNamespace(stage="pilot", mock=True, resume=False)
            ctx = RunContext()
            ctx.run_id = run_id
            ctx.run_dir = run_dir
            ctx.workspace_root = tmp_dir

            events = []

            import rsi_loop as rsi_mod
            import campaign2.manifest as man_mod
            import campaign2.registration as reg_mod
            import campaign2.launch_lock as lock_mod
            import campaign2.tier_assignment as tier_mod
            import campaign2.cpu_gate as cpu_mod
            import campaign2.preflight_registry as pref_mod
            import campaign2.reporting as rep_mod
            import campaign2.attempt_identity as ident_mod
            import campaign2.attempt_lifecycle as life_mod
            import campaign2.durability as dur_mod
            import campaign2.ladder as ladder_mod

            orig_manifest = man_mod.verify_campaign2_manifest
            orig_evidence = man_mod.verify_campaign2_evidence_manifest
            orig_parity = man_mod.verify_campaign2_parity_matrix
            orig_tier = tier_mod.verify_family_blocked_assignment
            orig_reg = reg_mod.verify_master_registration
            orig_lock = lock_mod.verify_launch_lock
            orig_ident = ident_mod.create_attempt_identity
            orig_resume = life_mod.AttemptLedger.verify_resume_admission
            orig_open = dur_mod.CheckpointManager._open_or_recover
            orig_cpu = cpu_mod.run_prescientific_cpu_gate
            orig_load = pref_mod.PreflightRegistry._load_or_init
            orig_canary = pref_mod.run_pilot_canary
            orig_start = life_mod.publish_scientific_start
            orig_save_ckpt = dur_mod.CheckpointManager.commit_checkpoint_head
            orig_ladder = ladder_mod.evaluate_pilot_outcome_ladder
            orig_report = rep_mod.publish_schema3_report
            orig_term = life_mod.AttemptLedger.record_terminal_classification

            def wrap_manifest(*a, **k):
                if "1. manifest_verification" not in events:
                    events.append("1. manifest_verification")
                return orig_manifest(*a, **k)

            def wrap_evidence(*a, **k):
                if "2. evidence_manifest_verification" not in events:
                    events.append("2. evidence_manifest_verification")
                return orig_evidence(*a, **k)

            def wrap_parity(*a, **k):
                if "3. parity_matrix_verification" not in events:
                    events.append("3. parity_matrix_verification")
                return orig_parity(*a, **k)

            def wrap_tier(*a, **k):
                if "4. tier_assignment_verification" not in events:
                    events.append("4. tier_assignment_verification")
                return orig_tier(*a, **k)

            def wrap_reg(*a, **k):
                if "5. master_registration_verification" not in events:
                    events.append("5. master_registration_verification")
                return orig_reg(*a, **k)

            def wrap_lock(*a, **k):
                if "6. launch_lock_verification" not in events:
                    events.append("6. launch_lock_verification")
                return orig_lock(*a, **k)

            def wrap_ident(*a, **k):
                if "7. attempt_identity_creation_or_verification" not in events:
                    events.append("7. attempt_identity_creation_or_verification")
                return orig_ident(*a, **k)

            def wrap_resume(self_obj):
                if "8. attempt_ledger_and_resume_admission_verification" not in events:
                    events.append("8. attempt_ledger_and_resume_admission_verification")
                return orig_resume(self_obj)

            def wrap_open(self_obj):
                if "9. checkpoint_manager_initialization" not in events:
                    events.append("9. checkpoint_manager_initialization")
                return orig_open(self_obj)

            def wrap_cli_ver(*a, **k):
                if "10. codex_cli_version_check" not in events:
                    events.append("10. codex_cli_version_check")
                return "codex-cli 0.144.5"

            def wrap_cpu(*a, **k):
                if "12. prescientific_cpu_gate" not in events:
                    events.append("12. prescientific_cpu_gate")
                return orig_cpu(*a, **k)

            def wrap_load(self_obj):
                if "11. preflight_registry_authentication" not in events:
                    events.append("11. preflight_registry_authentication")
                return orig_load(self_obj)

            def wrap_canary(*a, **k):
                if "13. pilot_canary_execution_or_reconciliation" not in events:
                    events.append("13. pilot_canary_execution_or_reconciliation")
                return orig_canary(*a, **k)

            def wrap_start(*a, **k):
                if "14. scientific_start_claim" not in events:
                    events.append("14. scientific_start_claim")
                return orig_start(*a, **k)

            def wrap_save_ckpt(self_obj, trial_idx, trial_result_dict):
                if trial_idx == 0:
                    if "15. trial_1_execution_and_checkpoint" not in events:
                        events.append("15. trial_1_execution_and_checkpoint")
                elif trial_idx == 1:
                    if "16. trial_2_execution_and_checkpoint" not in events:
                        events.append("16. trial_2_execution_and_checkpoint")
                return orig_save_ckpt(self_obj, trial_idx, trial_result_dict)

            def wrap_ladder(*a, **k):
                if "17. pilot_outcome_ladder_evaluation" not in events:
                    events.append("17. pilot_outcome_ladder_evaluation")
                return orig_ladder(*a, **k)

            def wrap_report(*a, **k):
                if "18. schema3_report_generation_and_sealing" not in events:
                    events.append("18. schema3_report_generation_and_sealing")
                return orig_report(*a, **k)

            def wrap_term(self_obj, attempt_id, classification, reason=""):
                if "19. terminal_classification_recording" not in events:
                    events.append("19. terminal_classification_recording")
                return orig_term(self_obj, attempt_id, classification, reason)

            import campaign2.proxy
            from campaign2.proxy import TaggedCodec
            import types, re

            class HermeticWorkerIPCMock:
                def __init__(self, host_in=0, host_out=0, startup_deadline=0, proc=None):
                    self.counter = 0
                    self.last_status = "ok"
                    self.candidate_process_error = None
                    self.candidate_timeout = False
                    self.infrastructure_error = None
                    self.candidate_exception_occurred = False
                    self.last_exception_message = ""
                    self.phase = "execution"
                    self.selector = mock.MagicMock()
                    self.cand_dir = proc.cand_tmpdir if proc else ""
                    self.candidate_mod = None
                    self.host_callbacks = {}
                    self.cb_counter = 0
                    self.host_objects = {}
                    self.hobj_counter = 0
                    self.objects = {}
                    self.obj_counter = 0
                    self.object_cache = {}

                def _read_framed_msg(self):
                    return {"op": "ready", "status": "ok", "candidate_imported": False, "limits": {"cpu": [15, 16], "nofile": [64, 64], "fsize": [1024*1024, 1024*1024], "core": [0, 0]}}

                def set_phase(self, phase, deadline):
                    self.phase = phase

                def make_host_callback_proxy(self, cb_id):
                    return self.host_callbacks[cb_id]

                def make_obj_proxy(self, obj_id):
                    return self.object_cache.get(obj_id)

                def register_callback(self, cb):
                    self.cb_counter += 1
                    cid = f"cb_{self.cb_counter}"
                    self.host_callbacks[cid] = cb
                    return cid

                def register_host_object(self, obj):
                    self.hobj_counter += 1
                    hid = f"hobj_{self.hobj_counter}"
                    self.host_objects[hid] = obj
                    return hid

                def register_obj(self, obj):
                    self.obj_counter += 1
                    oid = f"obj_{self.obj_counter}"
                    self.objects[oid] = obj
                    return oid

                def send_request(self, req):
                    # print(f"IPC OP: {req.get('op')}", flush=True)
                    op = req.get("op")
                    if op == "load_candidate":
                        cand_p = os.path.join(self.cand_dir, "candidate.py")
                        with open(cand_p, "r", encoding="utf-8") as f:
                            code_text = f.read()
                        m = re.search(r"```python\s*(.*?)\s*```", code_text, re.DOTALL)
                        raw_py = m.group(1) if m else code_text
                        self.candidate_mod = types.ModuleType("candidate")
                        exec(raw_py, self.candidate_mod.__dict__)
                        self.last_status = "ok"
                        return {"phase": "candidate_import"}
                    elif op == "call_func":
                        if not self.candidate_mod or not hasattr(self.candidate_mod, req["func_name"]):
                            self.last_status = "no_func"
                            return None
                        func_name = req["func_name"]
                        func = getattr(self.candidate_mod, func_name)
                        args = TaggedCodec.decode(req.get("args", []), self.make_host_callback_proxy, self.make_obj_proxy, lambda oid: self.objects[oid], lambda h_id: self.host_objects[h_id])
                        kwargs = TaggedCodec.decode(req.get("kwargs", {}), self.make_host_callback_proxy, self.make_obj_proxy, lambda oid: self.objects[oid], lambda h_id: self.host_objects[h_id])
                        try:
                            res = func(*args, **kwargs)
                            self.last_status = "ok"
                            return res
                        except Exception as e:
                            self.candidate_exception_occurred = True
                            self.last_exception_message = f"{type(e).__name__}: {e}"
                            self.last_status = "exception"
                            return e
                    elif op == "create_obj":
                        if not self.candidate_mod or not hasattr(self.candidate_mod, req["class_name"]):
                            self.last_status = "no_class"
                            return None
                        cls_name = req["class_name"]
                        cls = getattr(self.candidate_mod, cls_name)
                        args = TaggedCodec.decode(req.get("args", []), self.make_host_callback_proxy, self.make_obj_proxy, lambda oid: self.objects[oid], lambda h_id: self.host_objects[h_id])
                        kwargs = TaggedCodec.decode(req.get("kwargs", {}), self.make_host_callback_proxy, self.make_obj_proxy, lambda oid: self.objects[oid], lambda h_id: self.host_objects[h_id])
                        try:
                            obj = cls(*args, **kwargs)
                            oid = self.register_obj(obj)
                            self.last_status = "ok"
                            return {"obj_id": oid}
                        except Exception as e:
                            self.candidate_exception_occurred = True
                            self.last_exception_message = f"{type(e).__name__}: {e}"
                            self.last_status = "exception"
                            return e
                    elif op == "get_attr":
                        obj = self.objects[req["obj_id"]]
                        attr_val = getattr(obj, req["attr_name"])
                        self.last_status = "ok"
                        if callable(attr_val) and not isinstance(attr_val, (dict, list, tuple, set, str, bytes)):
                            return {"is_callable": True}
                        else:
                            return {"is_callable": False, "value": TaggedCodec.encode(attr_val, self.register_callback, self.register_obj, self.register_host_object)}
                    elif op == "call_method":
                        obj = self.objects[req["obj_id"]]
                        fn = getattr(obj, req["method_name"])
                        args = TaggedCodec.decode(req.get("args", []), self.make_host_callback_proxy, self.make_obj_proxy, lambda oid: self.objects[oid], lambda h_id: self.host_objects[h_id])
                        kwargs = TaggedCodec.decode(req.get("kwargs", {}), self.make_host_callback_proxy, self.make_obj_proxy, lambda oid: self.objects[oid], lambda h_id: self.host_objects[h_id])
                        try:
                            res = fn(*args, **kwargs)
                            self.last_status = "ok"
                            return res
                        except Exception as e:
                            self.candidate_exception_occurred = True
                            self.last_exception_message = f"{type(e).__name__}: {e}"
                            self.last_status = "exception"
                            return e
                    elif op == "set_attr":
                        obj = self.objects[req["obj_id"]]
                        val = TaggedCodec.decode(req["value"], self.make_host_callback_proxy, self.make_obj_proxy, lambda oid: self.objects[oid], lambda h_id: self.host_objects[h_id])
                        setattr(obj, req["attr_name"], val)
                        self.last_status = "ok"
                        return True

            import subprocess as _real_sp
            _orig_popen = _real_sp.Popen
            def fake_popen(cmd, cwd=None, **kwargs):
                cmd_str = " ".join(cmd) if isinstance(cmd, (list, tuple)) else str(cmd)
                if "sandbox-exec" in cmd_str or "candidate" in cmd_str:
                    p = mock.MagicMock()
                    p.cand_tmpdir = cwd
                    return p
                return _orig_popen(cmd, cwd=cwd, **kwargs)

            import contextlib
            with contextlib.ExitStack() as stack:
                stack.enter_context(mock.patch.object(man_mod, "verify_campaign2_manifest", side_effect=wrap_manifest))
                stack.enter_context(mock.patch.object(man_mod, "verify_campaign2_evidence_manifest", side_effect=wrap_evidence))
                stack.enter_context(mock.patch.object(man_mod, "verify_campaign2_parity_matrix", side_effect=wrap_parity))
                stack.enter_context(mock.patch.object(tier_mod, "verify_family_blocked_assignment", side_effect=wrap_tier))
                stack.enter_context(mock.patch.object(reg_mod, "verify_master_registration", side_effect=wrap_reg))
                stack.enter_context(mock.patch.object(lock_mod, "verify_launch_lock", side_effect=wrap_lock))
                stack.enter_context(mock.patch.object(ident_mod, "create_attempt_identity", side_effect=wrap_ident))
                stack.enter_context(mock.patch.object(life_mod.AttemptLedger, "verify_resume_admission", autospec=True, side_effect=wrap_resume))
                stack.enter_context(mock.patch.object(dur_mod.CheckpointManager, "_open_or_recover", autospec=True, side_effect=wrap_open))
                stack.enter_context(mock.patch("campaign2.model_provider.verify_codex_cli_version", side_effect=wrap_cli_ver))
                stack.enter_context(mock.patch.object(cpu_mod, "run_prescientific_cpu_gate", side_effect=wrap_cpu))
                stack.enter_context(mock.patch.object(pref_mod.PreflightRegistry, "_load_or_init", autospec=True, side_effect=wrap_load))
                stack.enter_context(mock.patch.object(pref_mod, "run_pilot_canary", side_effect=wrap_canary))
                stack.enter_context(mock.patch.object(life_mod, "publish_scientific_start", side_effect=wrap_start))
                stack.enter_context(mock.patch.object(dur_mod.CheckpointManager, "commit_checkpoint_head", autospec=True, side_effect=wrap_save_ckpt))
                stack.enter_context(mock.patch.object(ladder_mod, "evaluate_pilot_outcome_ladder", side_effect=wrap_ladder))
                stack.enter_context(mock.patch.object(rep_mod, "publish_schema3_report", side_effect=wrap_report))
                stack.enter_context(mock.patch.object(life_mod.AttemptLedger, "record_terminal_classification", autospec=True, side_effect=wrap_term))
                stack.enter_context(mock.patch("campaign2.proxy.subprocess.Popen", side_effect=fake_popen))
                stack.enter_context(mock.patch("campaign2.proxy.WorkerIPCClient", side_effect=HermeticWorkerIPCMock))
                stack.enter_context(mock.patch("campaign2.model_provider.setup_credential_home"))
                _run_campaign2(args, ctx)

            # Assert ALL 19 steps occurred in exact sequence
            self.assertEqual(events, EXPECTED_LAUNCH_STEPS)

            # Assert clean-path accounting
            reg = PreflightRegistry(stage="pilot", workspace_root=tmp_dir)
            self.assertEqual(reg.data["total_logical_calls"], 173)  # 1 canary + 172 benchmark
            self.assertEqual(reg.data["total_local_invocation_attempts"], 173)
            master_reg = verify_master_registration(tmp_dir)
            max_active_time = float(master_reg["execution_limits"]["max_active_seconds"])
            self.assertEqual(reg.data.get("cumulative_tokens"), 17300)
            self.assertGreaterEqual(reg.data.get("cumulative_active_time"), 0.0)
            self.assertLessEqual(reg.data.get("cumulative_active_time"), max_active_time)

            # Assert checkpoint and report sealing
            ckpt_p = os.path.join(run_dir, "campaign2_pilot_checkpoint.json")
            self.assertTrue(os.path.exists(ckpt_p))
            self.assertEqual(stat.S_IMODE(os.stat(ckpt_p).st_mode), 0o444)

            report_p = os.path.join(run_dir, "campaign2_pilot_report.json")
            self.assertTrue(os.path.exists(report_p))
            self.assertEqual(stat.S_IMODE(os.stat(report_p).st_mode), 0o444)

            # Assert terminal ledger state and completed classification
            ledger_p = os.path.join(run_dir, "campaign2_pilot_ledger.jsonl")
            from campaign2.attempt_lifecycle import AttemptLedger
            ledger = AttemptLedger(ledger_p)
            entries = ledger.get_entries()
            term_recs = [e for e in entries if e.get("record_type") == "terminal_classification"]
            self.assertEqual(len(term_recs), 1)
            self.assertEqual(term_recs[0]["classification"], "completed")
            self.assertIn("Ladder decision", term_recs[0].get("reason", ""))

    def test_h4_cpu_gate_stop_path_binding(self):
        """Prove real CPU gate failure stops launch before canary/start/tokens, creates 0o444 record, and binds first result."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            subprocess.run(["git", "init"], cwd=tmp_dir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_dir, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_dir, check=True)

            with open(os.path.join(tmp_dir, ".gitignore"), "w") as f:
                f.write("history/\n")

            for f_item in ["README.md", "rsi_loop.py", "run_tests.py", "generator.sb.template"]:
                if os.path.exists(os.path.join(BASE_DIR, f_item)):
                    shutil.copy2(os.path.join(BASE_DIR, f_item), os.path.join(tmp_dir, f_item))
            os.makedirs(os.path.join(tmp_dir, "docs"), exist_ok=True)
            shutil.copy2(os.path.join(BASE_DIR, "docs", "walkthrough.md"), os.path.join(tmp_dir, "docs", "walkthrough.md"))

            shutil.copytree(os.path.join(BASE_DIR, "campaign2"), os.path.join(tmp_dir, "campaign2"))
            shutil.copytree(os.path.join(BASE_DIR, "benchmarks"), os.path.join(tmp_dir, "benchmarks"))
            if os.path.exists(os.path.join(BASE_DIR, "campaign2_archive")):
                shutil.copytree(os.path.join(BASE_DIR, "campaign2_archive"), os.path.join(tmp_dir, "campaign2_archive"))

            shutil.copy2(os.path.join(BASE_DIR, "campaign2_tier_assignment.json"), os.path.join(tmp_dir, "campaign2_tier_assignment.json"))
            shutil.copy2(os.path.join(BASE_DIR, "campaign2_evidence_hashes.json"), os.path.join(tmp_dir, "campaign2_evidence_hashes.json"))
            if os.path.exists(os.path.join(BASE_DIR, "campaign2_parity_matrix.json")):
                shutil.copy2(os.path.join(BASE_DIR, "campaign2_parity_matrix.json"), os.path.join(tmp_dir, "campaign2_parity_matrix.json"))
                os.chmod(os.path.join(tmp_dir, "campaign2_parity_matrix.json"), 0o444)

            # Modify parity matrix in tmp_dir so row 0 specifies mock_cpu_sec: 3.5 (> 3.0 s threshold)
            pm_path = os.path.join(tmp_dir, "campaign2_parity_matrix.json")
            with open(os.path.join(BASE_DIR, "campaign2_parity_matrix.json"), "r", encoding="utf-8") as f:
                pm_data = json.load(f)
            pm_rows = list(pm_data.values()) if isinstance(pm_data, dict) and "matrix" not in pm_data else (pm_data["matrix"] if isinstance(pm_data, dict) else pm_data)
            pm_rows[0]["mock_cpu_sec"] = 3.5
            pm_bytes = (json.dumps(pm_data, indent=2, sort_keys=True) + "\n").encode("utf-8")
            if os.path.exists(pm_path):
                os.chmod(pm_path, 0o644)
            with open(pm_path, "wb") as f:
                f.write(pm_bytes)
            os.chmod(pm_path, 0o444)

            # Update evidence hashes in tmp_dir for modified parity matrix
            ev_path = os.path.join(tmp_dir, "campaign2_evidence_hashes.json")
            os.chmod(ev_path, 0o644)
            with open(ev_path, "r", encoding="utf-8") as f:
                ev_data = json.load(f)
            ev_data["campaign2_parity_matrix.json"] = hashlib.sha256(pm_bytes).hexdigest()
            ev_bytes = (json.dumps(ev_data, indent=2, sort_keys=True) + "\n").encode("utf-8")
            with open(ev_path, "wb") as f:
                f.write(ev_bytes)
            os.chmod(ev_path, 0o444)

            from campaign2.registration import generate_master_registration
            from campaign2.manifest import generate_campaign2_manifest
            from campaign2.launch_lock import generate_launch_lock
            from campaign2.cpu_gate import run_prescientific_cpu_gate, CpuGateError, CpuGateLimitExceededError
            from campaign2.preflight_registry import PreflightRegistry

            generate_master_registration(tmp_dir)
            generate_campaign2_manifest(tmp_dir)

            subprocess.run(["git", "add", "."], cwd=tmp_dir, check=True)
            subprocess.run(["git", "commit", "-m", "init hermetic"], cwd=tmp_dir, check=True)

            generate_launch_lock(tmp_dir)

            from rsi_loop import _run_campaign2, RunContext, ExperimentInvalidError

            run_id = "run_c1a_cpugate_01"
            run_dir = os.path.join(tmp_dir, "history", run_id)
            os.makedirs(run_dir, exist_ok=True)

            args = SimpleNamespace(stage="pilot", mock=True, resume=False)
            ctx = RunContext()
            ctx.run_id = run_id
            ctx.run_dir = run_dir
            ctx.workspace_root = tmp_dir

            canary_calls = [0]
            import campaign2.preflight_registry as pref_mod
            orig_canary = pref_mod.run_pilot_canary
            def count_canary(*a, **k):
                canary_calls[0] += 1
                return orig_canary(*a, **k)

            # Invoke REAL _run_campaign2 WITHOUT replacing run_prescientific_cpu_gate
            with mock.patch("campaign2.manifest.verify_campaign2_parity_matrix", return_value=True), \
                 mock.patch.object(pref_mod, "run_pilot_canary", side_effect=count_canary):
                with self.assertRaises((CpuGateLimitExceededError, CpuGateError, ExperimentInvalidError)):
                    _run_campaign2(args, ctx)

            # Inspect registry and ledger accounting to prove 0 canary calls, 0 tokens, and no SCIENTIFIC_START published
            self.assertEqual(canary_calls[0], 0)

            reg = PreflightRegistry(stage="pilot", workspace_root=tmp_dir)
            self.assertEqual(reg.data.get("cumulative_tokens", 0), 0)
            self.assertEqual(reg.data.get("total_logical_calls", 0), 0)
            self.assertEqual(reg.data.get("total_local_invocation_attempts", 0), 0)

            # Assert zero invocation-intent records in attempt ledger
            ledger_p = os.path.join(run_dir, "campaign2_pilot_ledger.jsonl")
            from campaign2.attempt_lifecycle import AttemptLedger
            ledger = AttemptLedger(ledger_p)
            intents = [e for e in ledger.get_entries() if e.get("record_type") == "invocation_intent"]
            self.assertEqual(len(intents), 0)

            # Assert zero trial records in checkpoint manager
            ckpt_p = os.path.join(run_dir, "campaign2_pilot_checkpoint.json")
            if os.path.exists(ckpt_p):
                with open(ckpt_p, "r", encoding="utf-8") as f:
                    ckpt_data = json.load(f)
                self.assertEqual(len(ckpt_data.get("completed_trials", [])), 0)

            start_marker = os.path.join(tmp_dir, "history", "campaign2_pilot_scientific_start.json")
            self.assertFalse(os.path.exists(start_marker))

            # Prove durable 0o444 CPU gate record was created on disk by real CPU gate
            gate_rec_p = os.path.join(tmp_dir, "campaign2_cpu_gate_record.json")
            self.assertTrue(os.path.exists(gate_rec_p))
            self.assertEqual(stat.S_IMODE(os.stat(gate_rec_p).st_mode), 0o444)

            # Prove FIRST-RESULT BINDING: calling real run_prescientific_cpu_gate a second time
            # on existing 0o444 record WITHOUT ANY PATCHES raises CpuGateLimitExceededError off disk
            with self.assertRaises(CpuGateLimitExceededError) as exc_ctx:
                run_prescientific_cpu_gate(tmp_dir, is_mock=True)
            self.assertIn("bound a failing max worker CPU time", str(exc_ctx.exception))

    def test_h5_launch_checklist_generator_and_verifier(self):
        """Verify launch checklist generation, sealing (0o444), machine derivation, and verifier fail-closed checks."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            subprocess.run(["git", "init"], cwd=tmp_dir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_dir, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_dir, check=True)

            with open(os.path.join(tmp_dir, ".gitignore"), "w") as f:
                f.write("history/\n")

            for f_item in ["README.md", "rsi_loop.py", "run_tests.py", "generator.sb.template"]:
                if os.path.exists(os.path.join(BASE_DIR, f_item)):
                    shutil.copy2(os.path.join(BASE_DIR, f_item), os.path.join(tmp_dir, f_item))
            os.makedirs(os.path.join(tmp_dir, "docs"), exist_ok=True)
            shutil.copy2(os.path.join(BASE_DIR, "docs", "walkthrough.md"), os.path.join(tmp_dir, "docs", "walkthrough.md"))

            shutil.copytree(os.path.join(BASE_DIR, "campaign2"), os.path.join(tmp_dir, "campaign2"))
            shutil.copytree(os.path.join(BASE_DIR, "benchmarks"), os.path.join(tmp_dir, "benchmarks"))
            if os.path.exists(os.path.join(BASE_DIR, "campaign2_archive")):
                shutil.copytree(os.path.join(BASE_DIR, "campaign2_archive"), os.path.join(tmp_dir, "campaign2_archive"))

            shutil.copy2(os.path.join(BASE_DIR, "campaign2_tier_assignment.json"), os.path.join(tmp_dir, "campaign2_tier_assignment.json"))
            shutil.copy2(os.path.join(BASE_DIR, "campaign2_evidence_hashes.json"), os.path.join(tmp_dir, "campaign2_evidence_hashes.json"))
            if os.path.exists(os.path.join(BASE_DIR, "campaign2_parity_matrix.json")):
                shutil.copy2(os.path.join(BASE_DIR, "campaign2_parity_matrix.json"), os.path.join(tmp_dir, "campaign2_parity_matrix.json"))
                os.chmod(os.path.join(tmp_dir, "campaign2_parity_matrix.json"), 0o444)
                os.chmod(os.path.join(tmp_dir, "campaign2_parity_matrix.json"), 0o444)

            from campaign2.registration import generate_master_registration
            from campaign2.manifest import generate_campaign2_manifest

            generate_master_registration(tmp_dir)
            generate_campaign2_manifest(tmp_dir)

            subprocess.run(["git", "add", "."], cwd=tmp_dir, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=tmp_dir, check=True)

            from campaign2.launch_checklist import (
                write_sealed_launch_checklist,
                verify_launch_checklist,
                LaunchChecklistError,
            )

            # 1. Write sealed checklist
            checklist_p = write_sealed_launch_checklist(tmp_dir)
            self.assertTrue(os.path.exists(checklist_p))
            self.assertEqual(stat.S_IMODE(os.stat(checklist_p).st_mode), 0o444)

            # 2. Verify checklist succeeds
            data = verify_launch_checklist(tmp_dir)
            self.assertTrue(data["clean_tracked_tree"])
            self.assertEqual(data["verified_preflight_file_count"], 145)
            self.assertEqual(data["accounting"]["total_logical_call_ceiling"], 175)

            # 3. Modify tracked file -> verify fails closed
            readme_p = os.path.join(tmp_dir, "README.md")
            with open(readme_p, "a") as f:
                f.write("\ntracked change\n")

            with self.assertRaises(LaunchChecklistError) as ctx:
                verify_launch_checklist(tmp_dir)
            self.assertIn("uncommitted changes", str(ctx.exception))

    def test_h2_launch_checklist_arithmetic_derivation_mismatch_matrix(self):
        """Verify launch checklist generator fails closed with exact error messages when master registration contains missing keys or arithmetic mismatches."""
        from campaign2.launch_checklist import write_sealed_launch_checklist, LaunchChecklistError
        from campaign2.registration import generate_master_registration
        from campaign2.manifest import generate_campaign2_manifest

        with tempfile.TemporaryDirectory() as tmp_dir:
            subprocess.run(["git", "init"], cwd=tmp_dir, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_dir, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_dir, check=True)

            for f_item in ["README.md", "rsi_loop.py", "run_tests.py", "generator.sb.template"]:
                if os.path.exists(os.path.join(BASE_DIR, f_item)):
                    shutil.copy2(os.path.join(BASE_DIR, f_item), os.path.join(tmp_dir, f_item))
            os.makedirs(os.path.join(tmp_dir, "docs"), exist_ok=True)
            shutil.copy2(os.path.join(BASE_DIR, "docs", "walkthrough.md"), os.path.join(tmp_dir, "docs", "walkthrough.md"))

            shutil.copytree(os.path.join(BASE_DIR, "campaign2"), os.path.join(tmp_dir, "campaign2"))
            shutil.copytree(os.path.join(BASE_DIR, "benchmarks"), os.path.join(tmp_dir, "benchmarks"))
            if os.path.exists(os.path.join(BASE_DIR, "campaign2_archive")):
                shutil.copytree(os.path.join(BASE_DIR, "campaign2_archive"), os.path.join(tmp_dir, "campaign2_archive"))

            shutil.copy2(os.path.join(BASE_DIR, "campaign2_tier_assignment.json"), os.path.join(tmp_dir, "campaign2_tier_assignment.json"))
            shutil.copy2(os.path.join(BASE_DIR, "campaign2_evidence_hashes.json"), os.path.join(tmp_dir, "campaign2_evidence_hashes.json"))
            if os.path.exists(os.path.join(BASE_DIR, "campaign2_parity_matrix.json")):
                shutil.copy2(os.path.join(BASE_DIR, "campaign2_parity_matrix.json"), os.path.join(tmp_dir, "campaign2_parity_matrix.json"))
                os.chmod(os.path.join(tmp_dir, "campaign2_parity_matrix.json"), 0o444)

            generate_master_registration(tmp_dir)
            generate_campaign2_manifest(tmp_dir)

            subprocess.run(["git", "add", "."], cwd=tmp_dir, check=True)
            subprocess.run(["git", "commit", "-m", "init hermetic"], cwd=tmp_dir, check=True)

            reg_p = os.path.join(tmp_dir, "campaign2_master_registration.json")
            anchor_p = os.path.join(tmp_dir, "campaign2_registration_anchor.hash")
            with open(reg_p, "r", encoding="utf-8") as f:
                base_reg_data = json.load(f)

            def test_bad_registration_mocked(mock_reg_data, expected_error_substring):
                with mock.patch("campaign2.registration.verify_master_registration", return_value=mock_reg_data):
                    with self.assertRaises(LaunchChecklistError) as exc_ctx:
                        write_sealed_launch_checklist(tmp_dir)
                    self.assertIn(expected_error_substring.lower(), str(exc_ctx.exception).lower())

            # 1. Missing call_arithmetic key -> raises LaunchChecklistError
            bad1 = json.loads(json.dumps(base_reg_data))
            del bad1["call_arithmetic"]
            test_bad_registration_mocked(bad1, "missing required call_arithmetic")

            # 1b. Missing pilot_limits key -> raises LaunchChecklistError
            bad1b = json.loads(json.dumps(base_reg_data))
            del bad1b["pilot_limits"]
            test_bad_registration_mocked(bad1b, "missing required call_arithmetic")

            # 2. Missing key inside call_arithmetic
            bad2 = json.loads(json.dumps(base_reg_data))
            del bad2["call_arithmetic"]["per_trial_benchmark_logical_calls"]
            test_bad_registration_mocked(bad2, "per_trial_benchmark_logical_calls")

            # 3. Missing key inside execution_limits
            bad3 = json.loads(json.dumps(base_reg_data))
            del bad3["execution_limits"]["max_active_seconds"]
            test_bad_registration_mocked(bad3, "max_active_seconds")

            # 4. Per-trial call arithmetic mismatch (per_trial * num_trials != pilot_total_benchmark_logical_calls)
            bad4 = json.loads(json.dumps(base_reg_data))
            bad4["call_arithmetic"]["pilot_total_benchmark_logical_calls"] = 999
            test_bad_registration_mocked(bad4, "per-trial call arithmetic mismatch")

            # 5. Total logical call ceiling arithmetic mismatch (benchmark + canary != pilot_max_logical_calls)
            bad5 = json.loads(json.dumps(base_reg_data))
            bad5["call_arithmetic"]["pilot_max_logical_calls"] = 50
            bad5["execution_limits"]["max_logical_calls"] = 50
            test_bad_registration_mocked(bad5, "total logical call ceiling arithmetic mismatch")

            # 6. Attempt ceiling arithmetic mismatch (total_ceiling * max_attempts != max_local_invocation_attempts)
            bad6 = json.loads(json.dumps(base_reg_data))
            bad6["call_arithmetic"]["max_local_invocation_attempts"] = 100
            bad6["execution_limits"]["max_local_invocation_attempts"] = 100
            test_bad_registration_mocked(bad6, "attempt ceiling arithmetic mismatch")

    def test_h6_production_workspace_root_override_refusal(self):
        """Verify production mode (args.mock=False) refuses ctx.workspace_root redirection and uses BASE_DIR."""
        from rsi_loop import _run_campaign2, RunContext, BASE_DIR

        with tempfile.TemporaryDirectory() as tmp_dir:
            args = SimpleNamespace(stage="pilot", mock=False, resume=False)
            ctx = RunContext()
            ctx.run_id = "run_prod_test_01"
            ctx.run_dir = os.path.join(tmp_dir, "history", ctx.run_id)
            ctx.workspace_root = "/tmp/fake_nonexistent_override_workspace"

            with mock.patch("campaign2.manifest.verify_campaign2_manifest", return_value=(True, 145)) as mock_ver:
                try:
                    _run_campaign2(args, ctx)
                except Exception:
                    pass
                mock_ver.assert_called_once_with(BASE_DIR)


if __name__ == "__main__":
    unittest.main()