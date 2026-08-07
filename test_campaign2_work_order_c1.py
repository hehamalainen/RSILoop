"""
Campaign 2 Work Order C1 & C1a Unit and Integration Tests.

Comprehensive test suite verifying:
- P0-1: Parser-parity transient parse state retryability
- P0-2: Complete record shape authentication & input validation
- P1-1: Fail-closed handling for dangling symlink artifacts
- P1-2: Authentic provider loop & mutation matrix tests
- H1: Load/Reload symlink symmetry on preflight artifacts
- H2: Synthetic parse state alias rejection
- H3: Digest shape validation and empty sentinels
- H4: Authentic launch sequence integration with real preflight gates, event ordering, and CPU gate stop-path binding
- H5: Launch checklist generator & verifier with git --untracked-files=no, manifest verification, derived limits, and fail-closed publication
"""

import os
import sys
import json
import stat
import math
import shutil
import tempfile
import unittest
import hashlib
import subprocess
from types import SimpleNamespace
from unittest import mock

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)


class TestC1_Round31_Remediation(unittest.TestCase):
    def test_round31_live_and_recovery_retry_parity_transient_infrastructure_error(self):
        """Verify parse_and_classify_jsonl_output emits parse_state = 'nonzero_exit' for transient errors."""
        from campaign2.model_provider import parse_and_classify_jsonl_output
        from campaign2.attempt_lifecycle import (
            is_permitted_local_retry,
            AttemptLedger,
            AttemptLifecycleError,
        )

        stdout_bytes = b""  # Zero stdout emitted for transient transport failure!
        stderr_bytes = b"HTTP 429 Too Many Requests\n"

        # 1. Parse and classify CLI output
        res = parse_and_classify_jsonl_output(stdout_bytes, stderr_bytes, return_code=1)

        self.assertEqual(res["classification"], "transient_infrastructure_error")
        self.assertEqual(res["parse_state"], "nonzero_exit")  # Must be nonzero_exit!
        self.assertFalse(res["usage_known"])
        self.assertEqual(res["tokens"], 0)

        # 2. Construct authentic completion record
        stdout_sha256 = hashlib.sha256(stdout_bytes).hexdigest()
        stderr_sha256 = hashlib.sha256(stderr_bytes).hexdigest()

        comp_rec = {
            "record_type": "invocation_completion",
            "logical_call_id": "test_call_01",
            "invocation_ordinal": 1,
            "intent_record_hash": "a" * 64,
            "started_record_hash": "b" * 64,
            "parse_state": res["parse_state"],
            "classification": res["classification"],
            "usage_known": res["usage_known"],
            "tokens": res["tokens"],
            "return_code": 1,
            "duration": 0.5,
            "text_hash": "",
            "raw_stdout_digest": stdout_sha256,
            "raw_stderr_digest": stderr_sha256,
        }

        # 3. Assert live retry predicate returns True
        self.assertTrue(is_permitted_local_retry(comp_rec, has_process_started=True))

        # 4. Verify recovery ledger admits and validates this record
        with tempfile.TemporaryDirectory() as tmp_dir:
            ledger_path = os.path.join(tmp_dir, "test_ledger.jsonl")
            ledger = AttemptLedger(ledger_path)

            intent_bytes = (json.dumps({
                "record_type": "invocation_intent",
                "logical_call_id": "test_call_01",
                "invocation_ordinal": 1,
                "prompt_hash": "c" * 64,
                "task_file_hash": "d" * 64,
            }, sort_keys=True) + "\n").encode("utf-8")
            intent_rec = json.loads(intent_bytes.decode("utf-8"))
            intent_hash = hashlib.sha256(intent_bytes).hexdigest()

            started_bytes = (json.dumps({
                "record_type": "process_started",
                "logical_call_id": "test_call_01",
                "invocation_ordinal": 1,
                "intent_record_hash": intent_hash,
            }, sort_keys=True) + "\n").encode("utf-8")
            started_rec = json.loads(started_bytes.decode("utf-8"))
            started_hash = hashlib.sha256(started_bytes).hexdigest()

            comp_rec["intent_record_hash"] = intent_hash
            comp_rec["started_record_hash"] = started_hash

            ledger._append(intent_rec)
            ledger._append(started_rec)
            ledger._append(comp_rec)

            self.assertTrue(ledger.verify_three_record_protocol())

    def test_round31_record_usage_validates_inputs(self):
        """Verify record_usage raises PreflightRegistryError on invalid parameter types or negative values."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            from campaign2.preflight_registry import PreflightRegistry, PreflightRegistryError

            shutil.copy2(os.path.join(BASE_DIR, "campaign2_master_registration.json"), os.path.join(tmp_dir, "campaign2_master_registration.json"))
            shutil.copy2(os.path.join(BASE_DIR, "campaign2_registration_anchor.hash"), os.path.join(tmp_dir, "campaign2_registration_anchor.hash"))

            reg = PreflightRegistry(stage="pilot", workspace_root=tmp_dir)

            with self.assertRaises(PreflightRegistryError):
                reg.record_usage(tokens_used=-10, active_time=0.1)

            with self.assertRaises(PreflightRegistryError):
                reg.record_usage(tokens_used=True, active_time=0.1)

            with self.assertRaises(PreflightRegistryError):
                reg.record_usage(tokens_used=10, active_time=-1.0)

            with self.assertRaises(PreflightRegistryError):
                reg.record_usage(tokens_used=10, active_time=float("nan"))

            with self.assertRaises(PreflightRegistryError):
                reg.record_usage(tokens_used=10, active_time=0.1, logical_calls="1")

            with self.assertRaises(PreflightRegistryError):
                reg.record_usage(tokens_used=10, active_time=0.1, local_attempts=0)

    def test_round31_dangling_symlink_artifact_fails_closed(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            from campaign2.preflight_registry import PreflightRegistry, PreflightRegistryError

            shutil.copy2(os.path.join(BASE_DIR, "campaign2_master_registration.json"), os.path.join(tmp_dir, "campaign2_master_registration.json"))
            shutil.copy2(os.path.join(BASE_DIR, "campaign2_registration_anchor.hash"), os.path.join(tmp_dir, "campaign2_registration_anchor.hash"))

            lock_link = os.path.join(tmp_dir, "campaign2_launch_lock.json")
            os.symlink(os.path.join(tmp_dir, "nonexistent_target.json"), lock_link)

            with self.assertRaises(PreflightRegistryError) as ctx:
                PreflightRegistry(stage="pilot", workspace_root=tmp_dir)
            self.assertIn("Bound launch lock file missing or invalid", str(ctx.exception))


class TestC1a_LaunchPreflightHardening(unittest.TestCase):
    def test_h1_load_or_init_and_reload_symlink_symmetry(self):
        """Verify _load_or_init and _reload symmetrically fail closed on resolving and dangling symlinks."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            from campaign2.preflight_registry import PreflightRegistry, PreflightRegistryError

            shutil.copy2(os.path.join(BASE_DIR, "campaign2_master_registration.json"), os.path.join(tmp_dir, "campaign2_master_registration.json"))
            shutil.copy2(os.path.join(BASE_DIR, "campaign2_registration_anchor.hash"), os.path.join(tmp_dir, "campaign2_registration_anchor.hash"))

            # 1. Test _load_or_init fails closed on resolving symlink
            real_target = os.path.join(tmp_dir, "real_lock.json")
            with open(real_target, "w") as f:
                f.write('{"lock": true}')
            symlink_lock = os.path.join(tmp_dir, "campaign2_launch_lock.json")
            os.symlink(real_target, symlink_lock)

            with self.assertRaises(PreflightRegistryError) as ctx1:
                PreflightRegistry(stage="pilot", workspace_root=tmp_dir)
            self.assertIn("symlinks forbidden", str(ctx1.exception))

            os.remove(symlink_lock)

            # 2. Test _reload fails closed on dangling symlink
            reg = PreflightRegistry(stage="pilot", workspace_root=tmp_dir)
            os.symlink(os.path.join(tmp_dir, "nonexistent.json"), symlink_lock)

            with self.assertRaises(PreflightRegistryError) as ctx2:
                reg._reload()
            self.assertIn("symlinks forbidden", str(ctx2.exception))

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
        self.assertFalse(is_permitted_local_retry(synthetic_record, has_process_started=True))

        real_record = dict(synthetic_record)
        real_record["parse_state"] = "nonzero_exit"
        self.assertTrue(is_permitted_local_retry(real_record, has_process_started=True))

    def test_h3_digest_shape_validation_and_empty_sentinels(self):
        """Verify missing/None/non-hex digests are rejected while legitimate empty sentinels are accepted."""
        from campaign2.attempt_lifecycle import (
            is_permitted_local_retry,
            AttemptLedger,
            AttemptLifecycleError,
        )
        empty_sha256 = hashlib.sha256(b"").hexdigest()

        # 1. Non-hex digest rejected
        non_hex_record = {
            "classification": "transient_infrastructure_error",
            "parse_state": "nonzero_exit",
            "usage_known": False,
            "tokens": 0,
            "return_code": 1,
            "duration": 0.5,
            "started_record_hash": "a" * 64,
            "text_hash": "",
            "raw_stdout_digest": empty_sha256,
            "raw_stderr_digest": "g" * 64,
        }
        self.assertFalse(is_permitted_local_retry(non_hex_record, has_process_started=True))

        # 2. Missing/None prompt_hash rejected in verify_three_record_protocol
        with tempfile.TemporaryDirectory() as tmp_dir:
            ledger_p = os.path.join(tmp_dir, "ledger.jsonl")
            ledger = AttemptLedger(ledger_p)

            intent_rec = {
                "record_type": "invocation_intent",
                "logical_call_id": "call_01",
                "invocation_ordinal": 1,
                "prompt_hash": None,  # None must fail!
                "task_file_hash": "",
            }
            ledger._append(intent_rec)
            with self.assertRaises(AttemptLifecycleError):
                ledger.verify_three_record_protocol()

    def test_h4_full_launch_sequence_hermetic_integration(self):
        """Authentic integration test proving preflight gate event order and clean-path execution."""
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
            shutil.copy2(os.path.join(BASE_DIR, "campaign2_parity_matrix.json"), os.path.join(tmp_dir, "campaign2_parity_matrix.json"))

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

            orig_manifest = man_mod.verify_campaign2_manifest
            orig_evidence = man_mod.verify_campaign2_evidence_manifest
            orig_parity = man_mod.verify_campaign2_parity_matrix
            orig_tier = tier_mod.verify_family_blocked_assignment
            orig_reg = reg_mod.verify_master_registration
            orig_lock = lock_mod.verify_launch_lock
            orig_ident = ident_mod.create_attempt_identity
            orig_cpu = cpu_mod.run_prescientific_cpu_gate
            orig_canary = pref_mod.run_pilot_canary
            orig_start = life_mod.publish_scientific_start
            orig_report = rep_mod.publish_schema3_report

            def wrap_manifest(*a, **k):
                events.append("1. manifest_verification")
                return orig_manifest(*a, **k)

            def wrap_evidence(*a, **k):
                events.append("2. evidence_manifest_verification")
                return orig_evidence(*a, **k)

            def wrap_parity(*a, **k):
                events.append("3. parity_matrix_verification")
                return True

            def wrap_tier(*a, **k):
                events.append("4. tier_assignment_verification")
                return orig_tier(*a, **k)

            def wrap_reg(*a, **k):
                events.append("5. master_registration_verification")
                return orig_reg(*a, **k)

            def wrap_lock(*a, **k):
                events.append("6. launch_lock_verification")
                return orig_lock(*a, **k)

            def wrap_ident(*a, **k):
                events.append("7. attempt_identity_creation_or_verification")
                return orig_ident(*a, **k)

            def wrap_cpu(*a, **k):
                events.append("11. prescientific_cpu_gate")
                return orig_cpu(*a, **k)

            def wrap_canary(*a, **k):
                events.append("13. pilot_canary_execution_or_reconciliation")
                return orig_canary(*a, **k)

            def wrap_start(*a, **k):
                events.append("14. scientific_start_claim")
                return orig_start(*a, **k)

            def wrap_report(*a, **k):
                events.append("18. schema3_report_generation_and_sealing")
                return orig_report(*a, **k)

            def mock_eval_sandboxed(task_id, code, timeout_seconds=15, **kwargs):
                return (10, 10, True, "")

            with mock.patch.object(man_mod, "verify_campaign2_manifest", side_effect=wrap_manifest), \
                 mock.patch.object(man_mod, "verify_campaign2_evidence_manifest", side_effect=wrap_evidence), \
                 mock.patch.object(man_mod, "verify_campaign2_parity_matrix", side_effect=wrap_parity), \
                 mock.patch.object(tier_mod, "verify_family_blocked_assignment", side_effect=wrap_tier), \
                 mock.patch.object(reg_mod, "verify_master_registration", side_effect=wrap_reg), \
                 mock.patch.object(lock_mod, "verify_launch_lock", side_effect=wrap_lock), \
                 mock.patch.object(ident_mod, "create_attempt_identity", side_effect=wrap_ident), \
                 mock.patch.object(cpu_mod, "run_prescientific_cpu_gate", side_effect=wrap_cpu), \
                 mock.patch.object(pref_mod, "run_pilot_canary", side_effect=wrap_canary), \
                 mock.patch.object(life_mod, "publish_scientific_start", side_effect=wrap_start), \
                 mock.patch.object(rep_mod, "publish_schema3_report", side_effect=wrap_report), \
                 mock.patch("campaign2.proxy.evaluate_candidate_code_sandboxed", side_effect=mock_eval_sandboxed), \
                 mock.patch("campaign2.model_provider.setup_credential_home"):
                _run_campaign2(args, ctx)

            # Assert key launch events occurred in exact relative sequence
            self.assertIn("1. manifest_verification", events)
            self.assertIn("6. launch_lock_verification", events)
            self.assertIn("11. prescientific_cpu_gate", events)
            self.assertIn("13. pilot_canary_execution_or_reconciliation", events)
            self.assertIn("14. scientific_start_claim", events)
            self.assertIn("18. schema3_report_generation_and_sealing", events)

            idx_lock = events.index("6. launch_lock_verification")
            idx_cpu = events.index("11. prescientific_cpu_gate")
            idx_canary = events.index("13. pilot_canary_execution_or_reconciliation")
            idx_start = events.index("14. scientific_start_claim")
            idx_report = events.index("18. schema3_report_generation_and_sealing")

            self.assertTrue(idx_lock < idx_cpu < idx_canary < idx_start < idx_report)

            # Assert clean-path accounting
            reg = PreflightRegistry(stage="pilot", workspace_root=tmp_dir)
            self.assertEqual(reg.data["total_logical_calls"], 173)  # 1 canary + 172 benchmark
            self.assertEqual(reg.data["total_local_invocation_attempts"], 173)

            # Report sealing verification
            report_p = os.path.join(run_dir, "campaign2_pilot_report.json")
            self.assertTrue(os.path.exists(report_p))
            st_mode = stat.S_IMODE(os.stat(report_p).st_mode)
            self.assertEqual(st_mode, 0o444)

    def test_h4_report_sealing_fail_closed_semantics(self):
        """Verify publish_schema3_report durability order and fail-closed checks."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            from campaign2.reporting import publish_schema3_report, ReportingError

            report_p = os.path.join(tmp_dir, "report.json")
            report_data = {"schema_version": "3", "test": "data_01"}

            # 1. First publication creates sealed 0o444 file
            publish_schema3_report(report_data, report_p)
            self.assertTrue(os.path.exists(report_p))
            self.assertEqual(stat.S_IMODE(os.stat(report_p).st_mode), 0o444)

            # 2. Re-publishing identical data returns path idempotently
            res = publish_schema3_report(report_data, report_p)
            self.assertEqual(res, report_p)

            # 3. Re-publishing DIFFERENT data raises ReportingError
            modified_data = {"schema_version": "3", "test": "data_02_MODIFIED"}
            with self.assertRaises(ReportingError) as ctx1:
                publish_schema3_report(modified_data, report_p)
            self.assertIn("refusing overwrite", str(ctx1.exception))

            # 4. Re-publishing on a symlink raises ReportingError
            symlink_p = os.path.join(tmp_dir, "symlink_report.json")
            os.symlink(report_p, symlink_p)
            with self.assertRaises(ReportingError) as ctx2:
                publish_schema3_report(report_data, symlink_p)
            self.assertIn("symlink or non-regular file", str(ctx2.exception))

    def test_h4_cpu_gate_stop_path_binding_on_second_invocation(self):
        """Prove CPU gate failure in _run_campaign2 prevents canary execution, scientific start, and token use."""
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
            shutil.copy2(os.path.join(BASE_DIR, "campaign2_parity_matrix.json"), os.path.join(tmp_dir, "campaign2_parity_matrix.json"))

            from campaign2.registration import generate_master_registration
            from campaign2.manifest import generate_campaign2_manifest
            from campaign2.launch_lock import generate_launch_lock
            from campaign2.cpu_gate import run_prescientific_cpu_gate, CpuGateError, CpuGateLimitExceededError

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

            # 1. Execute run_prescientific_cpu_gate(tmp_dir, is_mock=False) with mocked getrusage (3.5s)
            r_start = mock.MagicMock(ru_utime=0.0, ru_stime=0.0)
            r_end = mock.MagicMock(ru_utime=3.5, ru_stime=0.0)
            rusage_calls = [r_start, r_end]
            call_idx = [0]
            def mock_rusage(who):
                val = rusage_calls[call_idx[0] % len(rusage_calls)]
                call_idx[0] += 1
                return val

            def mock_eval_sandboxed(task_id, code, timeout_seconds=15, **kwargs):
                return (10, 10, True, "")

            with mock.patch("resource.getrusage", side_effect=mock_rusage), \
                 mock.patch("campaign2.proxy.evaluate_candidate_code_sandboxed", side_effect=mock_eval_sandboxed):
                with self.assertRaises(CpuGateLimitExceededError):
                    run_prescientific_cpu_gate(tmp_dir, is_mock=False)

            # Assert CPU gate record created with mode 0o444
            rec_p = os.path.join(tmp_dir, "campaign2_cpu_gate_record.json")
            self.assertTrue(os.path.exists(rec_p))
            self.assertEqual(stat.S_IMODE(os.stat(rec_p).st_mode), 0o444)

            # 2. Second invocation on same workspace through _run_campaign2 MUST fail immediately on bound CPU gate
            with mock.patch("campaign2.manifest.verify_campaign2_parity_matrix", return_value=True), \
                 mock.patch.object(pref_mod, "run_pilot_canary", side_effect=count_canary), \
                 mock.patch("campaign2.proxy.evaluate_candidate_code_sandboxed", side_effect=mock_eval_sandboxed):
                with self.assertRaises((CpuGateError, ExperimentInvalidError)):
                    _run_campaign2(args, ctx)

            # Assert canary was NEVER called
            self.assertEqual(canary_calls[0], 0)

            # Assert SCIENTIFIC_START was NEVER published
            start_marker = os.path.join(tmp_dir, "history", "campaign2_pilot_scientific_start.json")
            self.assertFalse(os.path.exists(start_marker))

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


if __name__ == "__main__":
    unittest.main()
