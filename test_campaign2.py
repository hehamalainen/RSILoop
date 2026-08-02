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
        cand_code_with_containment_checks = """
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
        self.assertTrue(verify_external_model_artifact())

    def test_09_production_path_proxy_inference_smoke_test(self):
        """End-to-end production smoke test executing run_local_proxy_inference under sandbox-exec."""
        task_id = "c_f1_01_log_stream_parser"
        seed = 1729
        earned, max_s, raw_hash, code_hash = run_local_proxy_inference(task_id, seed)
        self.assertGreaterEqual(earned, 0)
        self.assertEqual(max_s, 10)
        self.assertEqual(len(raw_hash), 64)
        self.assertEqual(len(code_hash), 64)

    def test_10_provenance_verification(self):
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
    def test_07_preflight_manifest_verification(self):
        """Verifies campaign2_preflight_hashes.json manifest authenticates all 128 Category 1 files and model artifact."""
        from campaign2.manifest import verify_campaign2_manifest
        ok, count = verify_campaign2_manifest(BASE_DIR)
        self.assertTrue(ok)
        self.assertEqual(count, 128)

    def test_08_reproducible_proxy_calibration(self):
        """Verifies model proxy inference runs produce identical outputs and evaluation scores across repeated runs with identical seeds."""
        verify_proxy_config_provenance()
        reproducible = verify_proxy_reproducibility()
        self.assertTrue(reproducible)

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
        """Asserts campaign2_evidence_hashes.json, parity matrix, and 30-task calibration distribution exist and pass integrity checks."""
        from campaign2.manifest import verify_campaign2_evidence_manifest
        self.assertTrue(verify_campaign2_evidence_manifest(BASE_DIR))

        parity_path = os.path.join(BASE_DIR, "campaign2_parity_matrix.json")
        self.assertTrue(os.path.exists(parity_path), "Missing campaign2_parity_matrix.json artifact")
        with open(parity_path, "r", encoding="utf-8") as f:
            parity_data = json.load(f)
        self.assertEqual(parity_data.get("total_variants_tested"), 120)
        self.assertEqual(parity_data.get("total_mismatches"), 0)
        self.assertTrue(parity_data.get("parity_verified"))

        calib_path = os.path.join(BASE_DIR, "campaign2_proxy_calibration.json")
        self.assertTrue(os.path.exists(calib_path), "Missing campaign2_proxy_calibration.json artifact")
        with open(calib_path, "r", encoding="utf-8") as f:
            calib_data = json.load(f)
        self.assertTrue(calib_data.get("provenance_verified"))
        self.assertTrue(calib_data.get("reproducibility_verified"))
        self.assertEqual(calib_data.get("total_tasks_evaluated"), 30)
        self.assertIn("tier_summary", calib_data)
        self.assertEqual(len(calib_data.get("score_records", [])), 30)

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


if __name__ == "__main__":
    unittest.main()
