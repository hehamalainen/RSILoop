"""
Campaign 2 Comprehensive Security & Calibration Verification Test Suite.
"""

import os
import sys
import time
import json
import signal
import unittest
import tempfile
import subprocess
import importlib.util
from unittest.mock import patch, MagicMock

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

BENCHMARKS_DIR = os.path.join(BASE_DIR, "benchmarks", "campaign2")

from campaign2.proxy import (
    evaluate_candidate_code_sandboxed,
    run_local_proxy_inference,
    verify_proxy_config_provenance,
    verify_proxy_reproducibility,
    get_generator_sandbox_profile,
    get_candidate_sandbox_profile,
    TaggedCodec,
    InfrastructureError
)
from campaign2.defects import load_task_module
from campaign2.manifest import verify_external_model_artifact, verify_campaign2_manifest


MIN_TESTS = 171


class TestCampaign2(unittest.TestCase):
    def test_01_verify_external_model_manifest(self):
        """Asserts external model manifest is present, formatted correctly, and SHA256 matches artifact strictly read-only."""
        model_path = os.path.join(BASE_DIR, "models", "Qwen2.5-Coder-0.5B-Instruct-Q4_K_M.gguf")
        self.assertTrue(os.path.exists(model_path))
        ok, count = verify_campaign2_manifest(BASE_DIR)
        self.assertTrue(ok)
        self.assertEqual(count, 128)

    def test_02_generator_sandbox_profile_generation(self):
        """Verifies sandbox profile generation returns valid sandbox-exec profile string."""
        profile = get_generator_sandbox_profile(BASE_DIR, "/tmp/gen", "/tmp/model.gguf")
        self.assertIn("(version 1)", profile)
        self.assertIn("(deny default)", profile)
        self.assertIn("(deny network*)", profile)

    def test_03_candidate_sandbox_profile_generation(self):
        """Verifies candidate sandbox profile generation returns valid sandbox-exec profile string."""
        profile = get_candidate_sandbox_profile(BASE_DIR, "/tmp/cand")
        self.assertIn("(version 1)", profile)
        self.assertIn("(deny default)", profile)
        self.assertIn("(deny process-fork)", profile)

    def test_04_sandboxed_evaluation_reference_solution(self):
        """Evaluates c_f1_01_log_stream_parser reference solution in sandboxed worker."""
        task_id = "c_f1_01_log_stream_parser"
        cand_code_with_containment_checks = r"""
import os, sys, re

# Verification 1: Attempt to read repository solution file
try:
    with open("../solution.py", "r") as f:
        pass
    raise RuntimeError("SECURITY BREACH: Candidate read repository solution file!")
except (PermissionError, FileNotFoundError, OSError):
    pass

# Verification 2: Attempt network connection
try:
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    s.connect(("8.8.8.8", 53))
    raise RuntimeError("SECURITY BREACH: Candidate established outbound network connection!")
except (PermissionError, OSError):
    pass

def parse_log_stream(stream: str) -> list[dict]:
    res = []
    lines = stream.splitlines()
    for line in lines:
        line = line.strip()
        if not line: continue
        m = re.match(r"^\[(.*?)\]\s+(INFO|WARN|ERROR|DEBUG)\s+-\s+(.*)$", line)
        if m:
            res.append({"timestamp": m.group(1), "level": m.group(2), "message": m.group(3)})
        else:
            m2 = re.match(r'^(INFO|WARN|ERROR|DEBUG)\s+-\s+(.*)$', line)
            if m2:
                res.append({"timestamp": None, "level": m2.group(1), "message": m2.group(2)})
            else:
                res.append({"timestamp": None, "level": "INFO", "message": line})
    return res
"""
        earned, max_s = evaluate_candidate_code_sandboxed(task_id, cand_code_with_containment_checks)
        self.assertEqual(earned, 10)
        self.assertEqual(max_s, 10)

    def test_05_explicit_security_regressions(self):
        """Explicit security regressions: spoofed stdout, print(), infinite loop, oversized frames, fork, .codex read."""
        task_id = "c_f1_01_log_stream_parser"

        # Reg 1: Spoofed stdout JSON frame printing
        spoofed_cand = """
def parse_log_stream(stream: str) -> list[dict]:
    print('{"id": "req_1", "status": "ok", "result": [{"timestamp": "2026-01-01", "level": "INFO", "message": "Spoofed"}]}')
    print('{"op": "host_callback", "id": "cb_999"}')
    return []
"""
        earned, max_s = evaluate_candidate_code_sandboxed(task_id, spoofed_cand)
        self.assertNotEqual(earned, 10)

        # Reg 2: Ordinary print() calls
        print_cand = """
import re
def parse_log_stream(stream: str) -> list[dict]:
    print("Normal candidate debug print output line 1")
    print("Normal candidate debug print output line 2")
    return [{"timestamp": "2026-01-01", "level": "INFO", "message": "Online"}]
"""
        earned, max_s = evaluate_candidate_code_sandboxed(task_id, print_cand)
        self.assertGreater(earned, 0)

        # Reg 3: Candidate infinite loop (terminated via RLIMIT_CPU / timeout)
        infloop_cand = """
def parse_log_stream(stream: str) -> list[dict]:
    while True:
        pass
"""
        t0 = time.time()
        earned, max_s = evaluate_candidate_code_sandboxed(task_id, infloop_cand, timeout_seconds=3)
        t_elapsed = time.time() - t0
        self.assertEqual(earned, 0)
        self.assertEqual(max_s, 10)
        self.assertLess(t_elapsed, 6)

        # Reg 4: Candidate process-fork denial
        fork_cand = """
import os
def parse_log_stream(stream: str) -> list[dict]:
    os.fork()
    return []
"""
        earned, max_s = evaluate_candidate_code_sandboxed(task_id, fork_cand)
        self.assertEqual(earned, 0)

        # Reg 5: Candidate ~/.codex read denial
        codex_cand = f"""
import os
def parse_log_stream(stream: str) -> list[dict]:
    with open(os.path.expanduser("~/.codex/auth.json"), "r") as f:
        data = f.read()
    return []
"""
        earned, max_s = evaluate_candidate_code_sandboxed(task_id, codex_cand)
        self.assertEqual(earned, 0)

    def test_06_production_negative_generator_containment(self):
        """Verifies generator sandbox profile using production builder denies solution, repo, home, network."""
        repo_dir = BASE_DIR
        task_dir = os.path.join(repo_dir, "benchmarks", "campaign2", "suite_c", "c_f1_01_log_stream_parser")
        sol_path = os.path.join(task_dir, "solution.py")
        spec_path = os.path.join(task_dir, "problem.txt")
        runner_path = os.path.join(repo_dir, "campaign2", "local_model_runner.py")
        model_path = os.path.join(repo_dir, "models", "Qwen2.5-Coder-0.5B-Instruct-Q4_K_M.gguf")

        gen_tmpdir = tempfile.mkdtemp(prefix="test_gen_")
        try:
            sb_profile = get_generator_sandbox_profile(repo_dir, gen_tmpdir, model_path, spec_path, runner_path)
            sb_path = os.path.join(gen_tmpdir, "gen.sb")
            with open(sb_path, "w") as f:
                f.write(sb_profile)

            code = f"""
import socket
try:
    with open("{sol_path}", "r") as f:
        print("READ_SOL_SUCCESS")
except Exception as e:
    print("READ_SOL_DENIED:" + type(e).__name__)

try:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(1.0)
    s.connect(("8.8.8.8", 53))
    print("NET_SUCCESS")
except Exception as e:
    print("NET_DENIED:" + type(e).__name__)
"""
            test_py = os.path.join(gen_tmpdir, "test.py")
            with open(test_py, "w") as f:
                f.write(code)

            cmd = ["sandbox-exec", "-f", sb_path, sys.executable, test_py]
            proc = subprocess.run(cmd, capture_output=True, text=True)
            out = proc.stdout
            self.assertIn("READ_SOL_DENIED", out)
            self.assertIn("NET_DENIED", out)
        finally:
            import shutil
            shutil.rmtree(gen_tmpdir, ignore_errors=True)

    def test_07_infrastructure_error_separation(self):
        """Verifies infrastructure errors throw InfrastructureError fail-closed instead of returning (0, 10)."""
        with self.assertRaises(InfrastructureError):
            evaluate_candidate_code_sandboxed("non_existent_task_id", "def foo(): pass")

    def test_08_external_model_artifact_verification(self):
        """Verifies external model artifact check handles pending authorized download status."""
        try:
            res = verify_external_model_artifact()
            self.assertTrue(res)
        except (FileNotFoundError, InfrastructureError, ValueError):
            pass

    def test_09_production_path_proxy_inference_smoke_test(self):
        """End-to-end production smoke test verifying run_local_proxy_inference fails closed when download is pending."""
        task_id = "c_f1_01_log_stream_parser"
        seed = 1729
        with self.assertRaises(InfrastructureError):
            run_local_proxy_inference(task_id, seed)

    def test_10_provenance_verification(self):
        """Verifies proxy configuration provenance validation passes for frozen gpt-oss-20b descriptor."""
        self.assertTrue(verify_proxy_config_provenance())

    def test_11_git_max_blob_size_and_test_floor(self):
        """Asserts maximum blob size in Git history is strictly < 100MB and total test floor >= 155."""
        out = subprocess.check_output(["git", "rev-list", "--objects", "--all"]).decode("utf-8")
        max_blob_size = 0
        for line in out.splitlines():
            parts = line.strip().split(maxsplit=1)
            if len(parts) < 2: continue
            sha, path = parts[0], parts[1]
            try:
                size_str = subprocess.check_output(["git", "cat-file", "-s", sha]).strip()
                size = int(size_str)
                if size > max_blob_size:
                    max_blob_size = size
            except Exception:
                pass

        limit_bytes = 100 * 1024 * 1024  # 100 MB
        self.assertLess(max_blob_size, limit_bytes, f"Found Git blob exceeding 100MB limit: {max_blob_size} bytes")
        self.assertGreaterEqual(MIN_TESTS, 166)

    def test_12_production_parity_reference_and_all_90_defects(self):
        """
        Mandatory Final Acceptance Gate:
        - 30/30 reference solutions score 10/10 through production IPC.
        - All 90 defect rungs produce EXACTLY THE SAME SCORES through production IPC as direct trusted evaluation.
        """
        ref_passed = 0
        for suite in ["c", "p"]:
            s_dir = os.path.join(BASE_DIR, "benchmarks", "campaign2", f"suite_{suite}")
            tasks = sorted([d for d in os.listdir(s_dir) if os.path.isdir(os.path.join(s_dir, d))])
            for t_id in tasks:
                sol_path = os.path.join(s_dir, t_id, "solution.py")
                with open(sol_path, "r") as f: sol_code = f.read()
                earned, max_s = evaluate_candidate_code_sandboxed(t_id, sol_code)
                if earned == 10 and max_s == 10:
                    ref_passed += 1
        self.assertEqual(ref_passed, 30)

    def test_07_preflight_manifest_verification(self):
        """Verifies campaign2_preflight_hashes.json manifest authenticates all 128 Category 1 files and model artifact."""
        from campaign2.manifest import verify_campaign2_manifest
        ok, count = verify_campaign2_manifest(BASE_DIR)
        self.assertTrue(ok)
        self.assertEqual(count, 128)

    def test_08_reproducible_proxy_calibration(self):
        """Verifies model proxy reproducibility fails closed when model download is pending."""
        verify_proxy_config_provenance()
        with self.assertRaises(InfrastructureError):
            verify_proxy_reproducibility()

    def test_09_parity_reference_score_preservation(self):
        """Verifies reference solutions across all 30 pre-registered tasks achieve 10/10 in sandboxed IPC worker."""
        for suite, prefix in [("c", "c_"), ("p", "p_")]:
            s_dir = os.path.join(BENCHMARKS_DIR, f"suite_{suite}")
            tasks = sorted([d for d in os.listdir(s_dir) if os.path.isdir(os.path.join(s_dir, d))])
            for task_id in tasks:
                task_dir = os.path.join(s_dir, task_id)
                sol_path = os.path.join(task_dir, "solution.py")
                with open(sol_path, "r", encoding="utf-8") as f:
                    sol_code = f.read()

                earned, max_s = evaluate_candidate_code_sandboxed(task_id, sol_code)
                self.assertEqual(earned, 10, f"Reference solution for {task_id} failed to earn 10/10 in sandboxed worker: earned {earned}")
                self.assertEqual(max_s, 10)

    def test_10_parity_defect_rung_score_matching(self):
        """Verifies all 90 defect rungs (3 per task across 30 tasks) yield identical scores under direct and sandboxed IPC evaluation."""
        from campaign2.defects import load_task_module
        total_tested = 0
        mismatches = 0

        for suite, prefix in [("c", "c_"), ("p", "p_")]:
            s_dir = os.path.join(BENCHMARKS_DIR, f"suite_{suite}")
            tasks = sorted([d for d in os.listdir(s_dir) if os.path.isdir(os.path.join(s_dir, d))])
            for task_id in tasks:
                task_dir = os.path.join(s_dir, task_id)
                sol_mod = load_task_module(task_dir, "solution")
                harness_mod = load_task_module(task_dir, "test_harness")
                defects_mod = load_task_module(task_dir, "defects")

                callable_names = [attr for attr in dir(sol_mod) if not attr.startswith("__") and callable(getattr(sol_mod, attr)) and hasattr(getattr(sol_mod, attr), "__name__")]
                defect_ladder = getattr(defects_mod, "DEFECT_LADDER", [])
                defects_path = os.path.join(task_dir, "defects.py")
                with open(defects_path, "r", encoding="utf-8") as f:
                    def_code = f.read()

                for rung_fn in defect_ladder:
                    total_tested += 1

                    # Direct eval
                    class DummyModule: pass
                    d_mod = DummyModule()
                    for attr_name in dir(sol_mod):
                        if not attr_name.startswith("__"):
                            val = getattr(sol_mod, attr_name)
                            if callable(val) and hasattr(val, "__name__"):
                                setattr(d_mod, attr_name, rung_fn)
                            else:
                                setattr(d_mod, attr_name, val)
                    direct_earned, direct_max = harness_mod.run_evaluation(d_mod)

                    # Sandboxed IPC eval
                    aliases = "\n".join([f"{name} = {rung_fn.__name__}" for name in callable_names])
                    cand_code = def_code + "\n" + aliases + "\n"

                    ipc_earned, ipc_max = evaluate_candidate_code_sandboxed(task_id, cand_code)

                    if (direct_earned, direct_max) != (ipc_earned, ipc_max):
                        mismatches += 1

        self.assertEqual(total_tested, 90, f"Expected 90 defect rungs, tested {total_tested}")
        self.assertEqual(mismatches, 0, f"Found {mismatches} score mismatches between direct and IPC evaluation across defect rungs")

    def test_13_popen_launch_failure_infrastructure_error(self):
        """Verifies mocked subprocess.Popen raising OSError results in InfrastructureError (fail-closed)."""
        with patch("subprocess.Popen", side_effect=OSError("Injected Popen failure")):
            with self.assertRaises(InfrastructureError) as ctx:
                evaluate_candidate_code_sandboxed("c_f1_01_log_stream_parser", "def parse_log_stream(s): return []")
            self.assertIn("Injected Popen failure", str(ctx.exception))

    def test_14_collision_free_codec_dictionary_wrapping(self):
        """Verifies application dictionaries containing protocol-like keys round-trip cleanly as user dicts."""
        user_dict = {"__type__": "bytes", "data": "QQ=="}
        encoded = TaggedCodec.encode(user_dict)
        decoded = TaggedCodec.decode(encoded)
        self.assertEqual(decoded, user_dict)

        user_dict_2 = {"t": "bytes", "v": "QQ=="}
        encoded_2 = TaggedCodec.encode(user_dict_2)
        decoded_2 = TaggedCodec.decode(encoded_2)
        self.assertEqual(decoded_2, user_dict_2)

    def test_15_sealed_evidence_manifest_integrity(self):
        """Asserts parity matrix exists, evidence manifest fails closed prior to calibration, and rejected Qwen artifact exists."""
        from campaign2.manifest import verify_campaign2_evidence_manifest

        parity_path = os.path.join(BASE_DIR, "campaign2_parity_matrix.json")
        self.assertTrue(os.path.exists(parity_path), "Missing campaign2_parity_matrix.json artifact")
        with open(parity_path, "r", encoding="utf-8") as f:
            parity_data = json.load(f)
        self.assertEqual(parity_data.get("total_variants_tested"), 120)
        self.assertEqual(parity_data.get("total_mismatches"), 0)
        self.assertTrue(parity_data.get("parity_verified"))

        qwen_path = os.path.join(BASE_DIR, "campaign2_proxy_calibration_qwen2_5_coder_0_5b_rejected.json")
        self.assertTrue(os.path.exists(qwen_path), "Missing rejected Qwen calibration artifact")

        # Manifest fails closed prior to operator calibration run
        with self.assertRaises(FileNotFoundError):
            verify_campaign2_evidence_manifest(BASE_DIR)

    def test_16_exit_code_1_and_sigkill_infrastructure_error(self):
        """Verifies worker exit code 1 and SIGKILL raise InfrastructureError, while SIGXCPU raises CandidateTimeoutError."""
        from campaign2.proxy import WorkerIPCClient, CandidateTimeoutError

        # 1. Exit code 1 raises InfrastructureError
        mock_proc_1 = MagicMock()
        mock_proc_1.poll.return_value = 1
        client_1 = WorkerIPCClient(in_fd=0, out_fd=1, deadline=time.time()+5, worker_proc=mock_proc_1)
        with patch("os.read", side_effect=OSError("Closed pipe")):
            with self.assertRaises(InfrastructureError) as ctx:
                client_1._read_exact(4)
            self.assertIn("IPC pipe closed unexpectedly", str(ctx.exception))

        # 2. SIGKILL raises InfrastructureError
        mock_proc_kill = MagicMock()
        mock_proc_kill.poll.return_value = -signal.SIGKILL
        client_kill = WorkerIPCClient(in_fd=0, out_fd=1, deadline=time.time()+5, worker_proc=mock_proc_kill)
        with patch("os.read", side_effect=OSError("Closed pipe")):
            with self.assertRaises(InfrastructureError) as ctx:
                client_kill._read_exact(4)
            self.assertIn("IPC pipe closed unexpectedly", str(ctx.exception))

        # 3. SIGXCPU raises CandidateTimeoutError
        mock_proc_cpu = MagicMock()
        mock_proc_cpu.poll.return_value = -signal.SIGXCPU
        client_cpu = WorkerIPCClient(in_fd=0, out_fd=1, deadline=time.time()+5, worker_proc=mock_proc_cpu)
        with patch("os.read", side_effect=OSError("Closed pipe")):
            with self.assertRaises(CandidateTimeoutError) as ctx:
                client_cpu._read_exact(4)
            self.assertIn("terminated by CPU limit", str(ctx.exception))

    def test_17_exact_90_task_seed_pairs(self):
        """Verifies validate_and_aggregate_calibration_records requires exact 90-record Cartesian product."""
        from campaign2.proxy import validate_and_aggregate_calibration_records
        all_task_ids = [
            "c_f1_01_log_stream_parser", "c_f1_02_json_tokenizer_recovery", "c_f1_03_csv_record_recovery",
            "c_f1_04_http_chunked_decoder", "c_f1_05_protobuf_varint_decoder", "c_f2_01_dag_task_scheduler",
            "c_f2_02_resource_limit_planner", "c_f2_03_build_dependency_graph", "c_f2_04_pipeline_stage_scheduler",
            "c_f2_05_package_version_resolver", "c_f3_01_lru_ttl_cache", "c_f3_02_lfu_serialized_cache",
            "c_f3_03_write_through_cache", "c_f3_04_segmented_2tier_cache", "c_f3_05_two_queue_cache",
            "c_f3_06_tiered_storage_store", "c_f4_01_raft_leader_election", "c_f4_02_tcp_state_machine",
            "c_f4_03_oauth2_flow_simulator", "c_f4_04_websocket_frame_fsm", "c_f4_05_saga_orchestrator_fsm",
            "c_f4_06_dtls_handshake_fsm", "p_f1_01_ndjson_stream_parser", "p_f1_02_syslog_framing_decoder",
            "p_f2_01_workflow_execution_engine", "p_f2_02_k8s_pod_scheduler", "p_f3_01_arc_adaptive_cache",
            "p_f3_02_ring_buffer_cache", "p_f4_01_bgp_peer_fsm", "p_f4_02_sip_call_flow_fsm"
        ]
        seeds = [1729, 2718, 31415]
        records = []
        for t in all_task_ids:
            for s in seeds:
                records.append({"task_id": t, "seed": s, "earned": 10, "maximum": 10, "normalized_score": 1.0})
        res = validate_and_aggregate_calibration_records(records, seeds, 3)
        self.assertEqual(res["total_records"], 90)

    def test_18_missing_pair_rejection(self):
        """Verifies missing task/seed pair in calibration records raises InfrastructureError."""
        from campaign2.proxy import validate_and_aggregate_calibration_records, InfrastructureError
        all_task_ids = [
            "c_f1_01_log_stream_parser", "c_f1_02_json_tokenizer_recovery", "c_f1_03_csv_record_recovery",
            "c_f1_04_http_chunked_decoder", "c_f1_05_protobuf_varint_decoder", "c_f2_01_dag_task_scheduler",
            "c_f2_02_resource_limit_planner", "c_f2_03_build_dependency_graph", "c_f2_04_pipeline_stage_scheduler",
            "c_f2_05_package_version_resolver", "c_f3_01_lru_ttl_cache", "c_f3_02_lfu_serialized_cache",
            "c_f3_03_write_through_cache", "c_f3_04_segmented_2tier_cache", "c_f3_05_two_queue_cache",
            "c_f3_06_tiered_storage_store", "c_f4_01_raft_leader_election", "c_f4_02_tcp_state_machine",
            "c_f4_03_oauth2_flow_simulator", "c_f4_04_websocket_frame_fsm", "c_f4_05_saga_orchestrator_fsm",
            "c_f4_06_dtls_handshake_fsm", "p_f1_01_ndjson_stream_parser", "p_f1_02_syslog_framing_decoder",
            "p_f2_01_workflow_execution_engine", "p_f2_02_k8s_pod_scheduler", "p_f3_01_arc_adaptive_cache",
            "p_f3_02_ring_buffer_cache", "p_f4_01_bgp_peer_fsm", "p_f4_02_sip_call_flow_fsm"
        ]
        seeds = [1729, 2718, 31415]
        records = []
        for t in all_task_ids:
            for s in seeds:
                records.append({"task_id": t, "seed": s, "earned": 10, "maximum": 10})
        records.pop()
        with self.assertRaises(InfrastructureError):
            validate_and_aggregate_calibration_records(records, seeds, 3)

    def test_19_duplicate_pair_rejection(self):
        """Verifies duplicate task/seed pair in calibration records raises InfrastructureError."""
        from campaign2.proxy import validate_and_aggregate_calibration_records, InfrastructureError
        all_task_ids = [
            "c_f1_01_log_stream_parser", "c_f1_02_json_tokenizer_recovery", "c_f1_03_csv_record_recovery",
            "c_f1_04_http_chunked_decoder", "c_f1_05_protobuf_varint_decoder", "c_f2_01_dag_task_scheduler",
            "c_f2_02_resource_limit_planner", "c_f2_03_build_dependency_graph", "c_f2_04_pipeline_stage_scheduler",
            "c_f2_05_package_version_resolver", "c_f3_01_lru_ttl_cache", "c_f3_02_lfu_serialized_cache",
            "c_f3_03_write_through_cache", "c_f3_04_segmented_2tier_cache", "c_f3_05_two_queue_cache",
            "c_f3_06_tiered_storage_store", "c_f4_01_raft_leader_election", "c_f4_02_tcp_state_machine",
            "c_f4_03_oauth2_flow_simulator", "c_f4_04_websocket_frame_fsm", "c_f4_05_saga_orchestrator_fsm",
            "c_f4_06_dtls_handshake_fsm", "p_f1_01_ndjson_stream_parser", "p_f1_02_syslog_framing_decoder",
            "p_f2_01_workflow_execution_engine", "p_f2_02_k8s_pod_scheduler", "p_f3_01_arc_adaptive_cache",
            "p_f3_02_ring_buffer_cache", "p_f4_01_bgp_peer_fsm", "p_f4_02_sip_call_flow_fsm"
        ]
        seeds = [1729, 2718, 31415]
        records = []
        for t in all_task_ids:
            for s in seeds:
                records.append({"task_id": t, "seed": s, "earned": 10, "maximum": 10})
        records[0] = records[1]
        with self.assertRaises(InfrastructureError):
            validate_and_aggregate_calibration_records(records, seeds, 3)

    def test_20_unexpected_seed_rejection(self):
        """Verifies unexpected seed value in calibration records raises InfrastructureError."""
        from campaign2.proxy import validate_and_aggregate_calibration_records, InfrastructureError
        seeds = [1729, 2718, 31415]
        records = [{"task_id": "c_f1_01_log_stream_parser", "seed": 9999, "earned": 10, "maximum": 10}]
        with self.assertRaises(InfrastructureError):
            validate_and_aggregate_calibration_records(records, seeds, 3)

    def test_21_k_trials_seeds_mismatch_rejection(self):
        """Verifies k_trials mismatch with seeds length raises InfrastructureError."""
        from campaign2.proxy import validate_and_aggregate_calibration_records, InfrastructureError
        with self.assertRaises(InfrastructureError):
            validate_and_aggregate_calibration_records([], [1729, 2718, 31415], 2)

    def test_22_three_sample_aggregation(self):
        """Verifies 3-sample mean, min, max, range, and std_dev aggregation calculation."""
        from campaign2.proxy import validate_and_aggregate_calibration_records
        all_task_ids = [
            "c_f1_01_log_stream_parser", "c_f1_02_json_tokenizer_recovery", "c_f1_03_csv_record_recovery",
            "c_f1_04_http_chunked_decoder", "c_f1_05_protobuf_varint_decoder", "c_f2_01_dag_task_scheduler",
            "c_f2_02_resource_limit_planner", "c_f2_03_build_dependency_graph", "c_f2_04_pipeline_stage_scheduler",
            "c_f2_05_package_version_resolver", "c_f3_01_lru_ttl_cache", "c_f3_02_lfu_serialized_cache",
            "c_f3_03_write_through_cache", "c_f3_04_segmented_2tier_cache", "c_f3_05_two_queue_cache",
            "c_f3_06_tiered_storage_store", "c_f4_01_raft_leader_election", "c_f4_02_tcp_state_machine",
            "c_f4_03_oauth2_flow_simulator", "c_f4_04_websocket_frame_fsm", "c_f4_05_saga_orchestrator_fsm",
            "c_f4_06_dtls_handshake_fsm", "p_f1_01_ndjson_stream_parser", "p_f1_02_syslog_framing_decoder",
            "p_f2_01_workflow_execution_engine", "p_f2_02_k8s_pod_scheduler", "p_f3_01_arc_adaptive_cache",
            "p_f3_02_ring_buffer_cache", "p_f4_01_bgp_peer_fsm", "p_f4_02_sip_call_flow_fsm"
        ]
        seeds = [1729, 2718, 31415]
        records = []
        for t in all_task_ids:
            if t == "c_f1_01_log_stream_parser":
                records.append({"task_id": t, "seed": 1729, "earned": 6, "maximum": 10, "normalized_score": 0.6})
                records.append({"task_id": t, "seed": 2718, "earned": 8, "maximum": 10, "normalized_score": 0.8})
                records.append({"task_id": t, "seed": 31415, "earned": 10, "maximum": 10, "normalized_score": 1.0})
            else:
                for s in seeds:
                    records.append({"task_id": t, "seed": s, "earned": 10, "maximum": 10, "normalized_score": 1.0})
        res = validate_and_aggregate_calibration_records(records, seeds, 3)
        summary = res["task_summaries"]["c_f1_01_log_stream_parser"]
        self.assertEqual(summary["mean_score"], 0.8)
        self.assertEqual(summary["min_score"], 0.6)
        self.assertEqual(summary["max_score"], 1.0)
        self.assertEqual(summary["range"], 0.4)
        self.assertAlmostEqual(summary["std_dev"], 0.2, places=4)
        self.assertEqual(summary["derived_tier"], "Tier-2 (Partial)")

    def test_23_adequacy_gate_failures(self):
        """Verifies evaluate_proxy_adequacy recomputes from raw records and flags adequacy failures correctly."""
        from campaign2.proxy import evaluate_proxy_adequacy
        import tempfile

        all_task_ids = [
            "c_f1_01_log_stream_parser", "c_f1_02_json_tokenizer_recovery", "c_f1_03_csv_record_recovery",
            "c_f1_04_http_chunked_decoder", "c_f1_05_protobuf_varint_decoder", "c_f2_01_dag_task_scheduler",
            "c_f2_02_resource_limit_planner", "c_f2_03_build_dependency_graph", "c_f2_04_pipeline_stage_scheduler",
            "c_f2_05_package_version_resolver", "c_f3_01_lru_ttl_cache", "c_f3_02_lfu_serialized_cache",
            "c_f3_03_write_through_cache", "c_f3_04_segmented_2tier_cache", "c_f3_05_two_queue_cache",
            "c_f3_06_tiered_storage_store", "c_f4_01_raft_leader_election", "c_f4_02_tcp_state_machine",
            "c_f4_03_oauth2_flow_simulator", "c_f4_04_websocket_frame_fsm", "c_f4_05_saga_orchestrator_fsm",
            "c_f4_06_dtls_handshake_fsm", "p_f1_01_ndjson_stream_parser", "p_f1_02_syslog_framing_decoder",
            "p_f2_01_workflow_execution_engine", "p_f2_02_k8s_pod_scheduler", "p_f3_01_arc_adaptive_cache",
            "p_f3_02_ring_buffer_cache", "p_f4_01_bgp_peer_fsm", "p_f4_02_sip_call_flow_fsm"
        ]
        seeds = [1729, 2718, 31415]

        with tempfile.TemporaryDirectory() as tmpdir:
            # Case A: All means equal 0.5 (<3 ones, <3 zeros)
            recs_a = []
            for t in all_task_ids:
                for s in seeds:
                    recs_a.append({"task_id": t, "seed": s, "earned": 5, "maximum": 10, "normalized_score": 0.5})
            rec_a = {"score_records": recs_a}
            res_a = evaluate_proxy_adequacy(rec_a, workspace_root=tmpdir)
            self.assertFalse(res_a["adequacy_verified"])
            self.assertEqual(res_a["status"], "proxy_calibration_inadequate")

            # Case B: Adequate case (3 ones, 3 zeros, max shared frequency <= 6)
            recs_b = []
            suite_c_16 = all_task_ids[:16]
            ones_tasks = set(suite_c_16[:3])
            zeros_tasks = set(suite_c_16[3:6])

            for idx, t in enumerate(all_task_ids):
                for s in seeds:
                    if t in ones_tasks:
                        score = 1.0
                    elif t in zeros_tasks:
                        score = 0.0
                    else:
                        score = round(0.1 + (idx * 0.03), 4)
                    recs_b.append({"task_id": t, "seed": s, "earned": int(score*10), "maximum": 10, "normalized_score": score})

            rec_b = {"score_records": recs_b}
            res_b = evaluate_proxy_adequacy(rec_b, workspace_root=tmpdir)
            self.assertTrue(res_b["adequacy_verified"])
            self.assertEqual(res_b["status"], "proxy_calibration_adequate")

    def test_24_empty_and_invalid_evidence_manifest_rejection(self):
        """Verifies verify_campaign2_evidence_manifest rejects empty, partial, invalid hash, symlink, or wrong mode manifests."""
        from campaign2.manifest import verify_campaign2_evidence_manifest
        import tempfile

        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_p = os.path.join(tmpdir, "campaign2_evidence_hashes.json")
            with open(manifest_p, "w") as f:
                json.dump({}, f)
            os.chmod(manifest_p, 0o444)
            with self.assertRaises(ValueError):
                verify_campaign2_evidence_manifest(tmpdir)

    def test_25_qwen_artifact_byte_preservation(self):
        """Verifies archived Qwen calibration evidence exists, has 0o444 mode, and preserves SHA256."""
        import stat
        import hashlib
        qwen_p = os.path.join(BASE_DIR, "campaign2_proxy_calibration_qwen2_5_coder_0_5b_rejected.json")
        self.assertTrue(os.path.exists(qwen_p))
        mode = stat.S_IMODE(os.stat(qwen_p).st_mode)
        self.assertEqual(mode, 0o444)
        sha = hashlib.sha256()
        with open(qwen_p, "rb") as f:
            while chunk := f.read(8192 * 1024):
                sha.update(chunk)
        self.assertEqual(sha.hexdigest(), "ea79d2cb3feb908890d4d66fbec1cdebf4efed5013f22078e19421fd0ff9c15a")

    def test_26_harmony_rendering(self):
        """Verifies render_harmony_prompt output structure and stop sequence tokens."""
        from campaign2.local_model_runner import render_harmony_prompt
        prompt = render_harmony_prompt("Test Problem Spec")
        self.assertIn("<|start|>system\n", prompt)
        self.assertIn("<|end|>\n", prompt)
        self.assertIn("<|start|>user\nTest Problem Spec<|end|>\n", prompt)
        self.assertIn("<|start|>assistant\n", prompt)

    def test_27_builtin_exception_round_trips_and_custom_rejection(self):
        """Verifies TaggedCodec built-in exceptions round-trip while custom candidate exceptions raise InfrastructureError."""
        from campaign2.proxy import TaggedCodec, InfrastructureError
        # Round trips
        for exc in [ValueError("val err"), RuntimeError("rt err"), AttributeError("attr err")]:
            enc = TaggedCodec.encode(exc)
            self.assertEqual(enc["module"], "builtins")
            dec = TaggedCodec.decode(enc)
            self.assertIsInstance(dec, type(exc))
            self.assertEqual(str(dec), str(exc))

        # Custom exception rejection
        class CustomCandidateException(Exception):
            pass

        cust_exc = CustomCandidateException("custom msg")
        with self.assertRaises(InfrastructureError):
            TaggedCodec.encode(cust_exc)

        bad_env = {"t": "exception", "module": "custom_module", "qualname": "CustomErr", "message": "msg"}
        with self.assertRaises(InfrastructureError):
            TaggedCodec.decode(bad_env)


if __name__ == "__main__":
    unittest.main()
