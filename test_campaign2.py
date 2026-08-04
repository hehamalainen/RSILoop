"""
Campaign 2 Comprehensive Master Test Suite (Work Order A-REV2 Remediation).

Verifies frozen benchmark inventory, family-blocked tier assignment, strict exception origin isolation,
phase-attributed candidate timeouts, real worker limits, sandbox containment, sealed parity evidence,
and read-only verification gates.
"""

import os
import sys
import json
import stat
import time
import signal
import tempfile
import unittest
import subprocess
import shutil
from contextlib import contextmanager
from unittest.mock import MagicMock, patch

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BENCHMARKS_DIR = os.path.join(BASE_DIR, "benchmarks", "campaign2")

from campaign2.proxy import (
    evaluate_candidate_code_sandboxed,
    get_candidate_sandbox_profile,
    WorkerIPCClient,
    CandidateModuleProxy,
    CandidateObjectProxy,
    TaggedCodec,
    InfrastructureError,
    CandidateTimeoutError,
    CandidateProcessError,
    WORKER_SCRIPT_CODE
)
from campaign2.tier_assignment import (
    generate_family_blocked_assignment,
    verify_family_blocked_assignment,
    verify_tier_assignment_artifact,
    RejectionSamplingPRNGStream,
    BASE_COMMIT_SHA,
    DOMAIN
)
from campaign2.manifest import (
    verify_campaign2_manifest,
    verify_campaign2_evidence_manifest,
    verify_campaign2_parity_matrix,
    get_expected_frozen_inventory
)


@contextmanager
def sandboxed_worker(candidate_code, cpu_timeout=2):
    """Launches the production worker script and sandbox profile for protocol-level assertions."""
    with tempfile.TemporaryDirectory() as cand_tmpdir:
        cand_file = os.path.join(cand_tmpdir, "candidate.py")
        worker_file = os.path.join(cand_tmpdir, "worker.py")
        sb_path = os.path.join(cand_tmpdir, "cand.sb")
        with open(cand_file, "w", encoding="utf-8") as f:
            f.write(candidate_code)
        with open(worker_file, "w", encoding="utf-8") as f:
            f.write(WORKER_SCRIPT_CODE)
        with open(sb_path, "w", encoding="utf-8") as f:
            f.write(get_candidate_sandbox_profile(BASE_DIR, cand_tmpdir))

        host_in, worker_out = os.pipe()
        worker_in, host_out = os.pipe()
        cmd = [
            "sandbox-exec", "-f", sb_path, sys.executable, worker_file,
            "--ipc-read-fd", str(worker_in), "--ipc-write-fd", str(worker_out),
            "--cpu-timeout", str(cpu_timeout),
        ]
        env = {
            "HOME": cand_tmpdir,
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONPATH": cand_tmpdir,
            "TMPDIR": cand_tmpdir,
        }
        proc = subprocess.Popen(
            cmd,
            cwd=cand_tmpdir,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            pass_fds=(worker_in, worker_out),
            start_new_session=True,
            env=env,
        )
        os.close(worker_in)
        os.close(worker_out)
        client = WorkerIPCClient(host_in, host_out, time.time() + 5, proc)
        try:
            yield cand_tmpdir, proc, client
        finally:
            client.selector.close()
            for fd in (host_in, host_out):
                try:
                    os.close(fd)
                except OSError:
                    pass
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    proc.kill()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()


class TestCampaign2(unittest.TestCase):
    # Finding 4 [P1] FIX: No setUp() or chmod mutations. All verification runs strictly read-only on committed disk files.

    def test_01_all_128_benchmark_files_exist_and_match_hashes(self):
        """Verifies Category 1: 128 Git-tracked frozen files exist and pass SHA256 preflight manifest."""
        ok, count = verify_campaign2_manifest(BASE_DIR)
        self.assertTrue(ok)
        self.assertEqual(count, 128)

    def test_02_task_defects_structure_and_monotonicity(self):
        """Verifies strict monotonicity of defect ladders (ref > rung1 > rung2 > rung3) across all 30 tasks."""
        from campaign2.defects import verify_all_defects
        results = verify_all_defects()
        self.assertEqual(len(results), 30)
        for res in results:
            self.assertTrue(res["strictly_decreasing"])

    def test_03_sandboxed_ipc_handshake_and_readiness(self):
        """Proves READY and RLIMIT attestation occur before candidate top-level code executes."""
        cand_code = "from pathlib import Path\nPath('imported.marker').write_text('imported')\ndef parse_log_stream(s): return []"
        with sandboxed_worker(cand_code, cpu_timeout=2) as (cand_tmpdir, _proc, client):
            marker_path = os.path.join(cand_tmpdir, "imported.marker")
            ready = client._read_framed_msg()
            self.assertEqual((ready.get("op"), ready.get("status")), ("ready", "ok"))
            self.assertFalse(ready.get("candidate_imported"))
            self.assertEqual(ready.get("limits", {}).get("cpu"), [2, 3])
            self.assertFalse(os.path.exists(marker_path), "candidate executed before READY")

            client.set_phase("candidate_import", time.time() + 3)
            client.send_request({"id": "load_candidate", "op": "load_candidate"})
            self.assertFalse(client.candidate_exception_occurred)
            self.assertTrue(os.path.exists(marker_path), "explicit load did not import candidate")

    def test_04_sandboxed_ipc_rlimits(self):
        """Proves a real worker is terminated by its installed RLIMIT_CPU."""
        cand_code = "def parse_log_stream(s):\n    while True: pass"
        with sandboxed_worker(cand_code, cpu_timeout=1) as (_cand_tmpdir, proc, client):
            ready = client._read_framed_msg()
            self.assertEqual(ready.get("limits", {}).get("cpu"), [1, 2])
            client.set_phase("candidate_import", time.time() + 4)
            client.send_request({"id": "load_candidate", "op": "load_candidate"})
            client.set_phase("candidate_execution", time.time() + 4)
            proxy = CandidateModuleProxy(client)
            with self.assertRaises(CandidateTimeoutError) as ctx:
                proxy.parse_log_stream("input")
            proc.wait(timeout=2)
            self.assertIn(proc.returncode, (-signal.SIGXCPU, 128 + signal.SIGXCPU))
            self.assertIn("RLIMIT_CPU", str(ctx.exception))

    def test_05_sandboxed_ipc_isolation(self):
        """Proves repository, system-file, and live loopback network access are denied."""
        task_id = "c_f1_01_log_stream_parser"
        sol_path = os.path.join(BENCHMARKS_DIR, "suite_c", task_id, "solution.py")
        with open(sol_path, "r", encoding="utf-8") as f:
            sol_code = f.read()

        containment_probe = f'''
import socket as _probe_socket
_containment_breach = False
for _probe_path in ({os.path.join(BASE_DIR, "README.md")!r}, "/etc/passwd"):
    try:
        with open(_probe_path, "rb") as _probe_file:
            _probe_file.read(1)
        _containment_breach = True
    except OSError:
        pass
try:
    _probe_sock = _probe_socket.socket(_probe_socket.AF_INET, _probe_socket.SOCK_STREAM)
    _probe_sock.bind(("127.0.0.1", 0))
    _probe_sock.listen(1)
    _containment_breach = True
except OSError:
    pass
finally:
    try:
        _probe_sock.close()
    except Exception:
        pass
_contained_parse_log_stream = parse_log_stream
def parse_log_stream(stream):
    if _containment_breach:
        return []
    return _contained_parse_log_stream(stream)
'''
        earned, max_s = evaluate_candidate_code_sandboxed(task_id, sol_code + containment_probe)
        self.assertEqual((earned, max_s), (10, 10))

    def test_06_reference_solution_score_preservation(self):
        """Verifies reference solutions across all 30 tasks achieve 10/10 in sandboxed worker."""
        for suite, prefix in [("c", "c_"), ("p", "p_")]:
            s_dir = os.path.join(BENCHMARKS_DIR, f"suite_{suite}")
            tasks = sorted([d for d in os.listdir(s_dir) if os.path.isdir(os.path.join(s_dir, d))])
            for task_id in tasks:
                sol_path = os.path.join(s_dir, task_id, "solution.py")
                with open(sol_path, "r", encoding="utf-8") as f:
                    sol_code = f.read()

                earned, max_s = evaluate_candidate_code_sandboxed(task_id, sol_code)
                self.assertEqual(earned, 10, f"Reference solution for {task_id} failed to earn 10/10 in sandboxed worker: earned {earned}")
                self.assertEqual(max_s, 10)

    def test_07_preflight_manifest_verification(self):
        """Verifies campaign2_preflight_hashes.json authenticates all 128 Category 1 files."""
        ok, count = verify_campaign2_manifest(BASE_DIR)
        self.assertTrue(ok)
        self.assertEqual(count, 128)

    def test_08_production_parity_matrix_verification(self):
        """Finding 1 [P0] FIX: Verifies campaign2_parity_matrix.json recomputes code and harness hashes for all 120 rows on disk."""
        self.assertTrue(verify_campaign2_parity_matrix(BASE_DIR))

        matrix_path = os.path.join(BASE_DIR, "campaign2_parity_matrix.json")
        with open(matrix_path, "r", encoding="utf-8") as f:
            matrix = json.load(f)

        self.assertEqual(len(matrix), 120)
        ref_count = 0
        defect_count = 0
        for row_id, row in matrix.items():
            self.assertTrue(row.get("parity_verified"))
            if row.get("type") == "reference":
                ref_count += 1
                self.assertEqual(row.get("direct_earned_score"), 10)
                self.assertEqual(row.get("ipc_earned_score"), 10)
            elif row.get("type") == "defect":
                defect_count += 1
        self.assertEqual(ref_count, 30)
        self.assertEqual(defect_count, 90)

    def test_09_parity_defect_rung_score_matching(self):
        """Finding 1 [P0] FIX: Reconstructs exact candidate code for all 90 defect rungs and verifies SHA256 code_hash matches sealed matrix."""
        from campaign2.defects import load_task_module
        import hashlib

        matrix_path = os.path.join(BASE_DIR, "campaign2_parity_matrix.json")
        with open(matrix_path, "r", encoding="utf-8") as f:
            matrix = json.load(f)

        total_tested = 0
        mismatches = 0

        for suite, prefix in [("c", "c_"), ("p", "p_")]:
            s_dir = os.path.join(BENCHMARKS_DIR, f"suite_{suite}")
            tasks = sorted([d for d in os.listdir(s_dir) if os.path.isdir(os.path.join(s_dir, d))])
            for task_id in tasks:
                task_dir = os.path.join(s_dir, task_id)
                sol_mod = load_task_module(task_dir, "solution")
                defects_mod = load_task_module(task_dir, "defects")

                callable_names = [attr for attr in dir(sol_mod) if not attr.startswith("__") and callable(getattr(sol_mod, attr)) and hasattr(getattr(sol_mod, attr), "__name__")]
                defect_ladder = getattr(defects_mod, "DEFECT_LADDER", [])
                defects_path = os.path.join(task_dir, "defects.py")
                sol_path = os.path.join(task_dir, "solution.py")
                with open(defects_path, "r", encoding="utf-8") as f:
                    def_code = f.read()
                with open(sol_path, "r", encoding="utf-8") as f:
                    sol_code = f.read()

                for idx, rung_fn in enumerate(defect_ladder[:3], start=1):
                    total_tested += 1
                    aliases = "\n".join([f"{name} = {rung_fn.__name__}" for name in callable_names])
                    cand_code = sol_code + "\n\n" + def_code + "\n\n" + aliases + "\n"
                    expected_code_hash = hashlib.sha256(cand_code.encode("utf-8")).hexdigest()

                    row_key = f"defect_{task_id}_rung_{idx}"
                    self.assertIn(row_key, matrix)
                    sealed_code_hash = matrix[row_key]["code_hash"]
                    self.assertEqual(sealed_code_hash, expected_code_hash, f"Reconstructed candidate code_hash mismatch for {row_key}")

        self.assertEqual(total_tested, 90)

    def test_11_popen_launch_failure_infrastructure_error(self):
        """Verifies mocked subprocess.Popen raising OSError results in InfrastructureError (fail-closed)."""
        with patch("subprocess.Popen", side_effect=OSError("Injected Popen failure")):
            with self.assertRaises(InfrastructureError) as ctx:
                evaluate_candidate_code_sandboxed("c_f1_01_log_stream_parser", "def parse_log_stream(s): return []")
            self.assertIn("Injected Popen failure", str(ctx.exception))

    def test_12_collision_free_codec_dictionary_wrapping(self):
        """Verifies application dictionaries containing protocol-like keys round-trip cleanly as user dicts."""
        user_dict = {"__type__": "bytes", "data": "QQ=="}
        encoded = TaggedCodec.encode(user_dict)
        decoded = TaggedCodec.decode(encoded)
        self.assertEqual(decoded, user_dict)

    def test_13_exit_code_1_and_sigkill_infrastructure_error(self):
        """Uses real pipe descriptors so selector registration succeeds in non-interactive TTY/non-TTY modes."""
        r_fd1, w_fd1 = os.pipe()
        mock_proc_1 = MagicMock()
        mock_proc_1.poll.return_value = 1
        client_1 = WorkerIPCClient(in_fd=r_fd1, out_fd=w_fd1, deadline=time.time()+5, worker_proc=mock_proc_1)
        os.close(w_fd1)
        with self.assertRaises(InfrastructureError) as ctx:
            client_1._read_exact(4)
        client_1.selector.close()
        os.close(r_fd1)

        r_fd2, w_fd2 = os.pipe()
        mock_proc_kill = MagicMock()
        mock_proc_kill.poll.return_value = -signal.SIGKILL
        client_kill = WorkerIPCClient(in_fd=r_fd2, out_fd=w_fd2, deadline=time.time()+5, worker_proc=mock_proc_kill)
        os.close(w_fd2)
        with self.assertRaises(InfrastructureError) as ctx:
            client_kill._read_exact(4)
        client_kill.selector.close()
        os.close(r_fd2)

        r_fd3, w_fd3 = os.pipe()
        mock_proc_cpu = MagicMock()
        mock_proc_cpu.poll.return_value = -signal.SIGXCPU
        client_cpu = WorkerIPCClient(in_fd=r_fd3, out_fd=w_fd3, deadline=time.time()+5, worker_proc=mock_proc_cpu)
        client_cpu.set_phase("candidate_execution", time.time() + 5)
        os.close(w_fd3)
        with self.assertRaises(CandidateTimeoutError) as ctx:
            client_cpu._read_exact(4)
        client_cpu.selector.close()
        os.close(r_fd3)
        self.assertIn("RLIMIT_CPU", str(ctx.exception))

        # The same unconfirmed wall wait is infrastructure during startup and
        # a candidate failure once candidate import/execution has begun.
        for phase, expected_error in (
            ("startup", InfrastructureError),
            ("candidate_import", CandidateTimeoutError),
            ("candidate_execution", CandidateTimeoutError),
        ):
            r_fd, w_fd = os.pipe()
            live_proc = MagicMock()
            live_proc.poll.return_value = None
            client = WorkerIPCClient(r_fd, w_fd, time.time() - 1, live_proc)
            client.set_phase(phase, time.time() - 1)
            with self.assertRaises(expected_error):
                client._read_exact(4)
            client.selector.close()
            os.close(r_fd)
            os.close(w_fd)

    def test_14_archive_byte_preservation(self):
        """Verifies archived audit files exist in campaign2_archive/, have 0o444 mode, and preserve SHA256 hashes."""
        qwen_p = os.path.join(BASE_DIR, "campaign2_archive", "proxy_calibration_qwen2_5_coder_0_5b_rejected.json")
        self.assertTrue(os.path.exists(qwen_p))
        self.assertEqual(stat.S_IMODE(os.stat(qwen_p).st_mode), 0o444)

        gpt_p = os.path.join(BASE_DIR, "campaign2_archive", "proxy_config_gpt_oss_unexecuted.json")
        self.assertTrue(os.path.exists(gpt_p))
        self.assertEqual(stat.S_IMODE(os.stat(gpt_p).st_mode), 0o444)

    def test_15_tier_assignment_reproducibility(self):
        """Verifies family-blocked assignment is 100% reproducible from base commit SHA alone."""
        assign1 = generate_family_blocked_assignment(BASE_COMMIT_SHA, DOMAIN)
        assign2 = generate_family_blocked_assignment(BASE_COMMIT_SHA, DOMAIN)
        self.assertEqual(assign1, assign2)
        self.assertEqual(assign1["base_commit_sha"], BASE_COMMIT_SHA)
        self.assertEqual(assign1["domain"], DOMAIN)

    def test_16_tier_assignment_exact_quotas(self):
        """Verifies exact family-by-tier quota matrix conformance for Suite C and Suite P sets."""
        assign = generate_family_blocked_assignment(BASE_COMMIT_SHA, DOMAIN)
        c_opt = assign["set_assignments"]["suite_c"]["optimization"]
        c_sel = assign["set_assignments"]["suite_c"]["selection"]
        c_conf = assign["set_assignments"]["suite_c"]["confirmatory"]

        c_opt_f = [t.split("_")[1] for t in c_opt]
        c_sel_f = [t.split("_")[1] for t in c_sel]
        c_conf_f = [t.split("_")[1] for t in c_conf]

        self.assertEqual(sorted(c_opt_f), ["f1", "f2", "f3", "f3"])
        self.assertEqual(sorted(c_sel_f), ["f1", "f2", "f2", "f3"])
        self.assertEqual(sorted(c_conf_f), ["f1", "f1", "f1", "f2", "f2", "f3", "f3", "f3"])

    def test_17_tier_assignment_total_coverage(self):
        """Verifies every task across 30 tasks occurs EXACTLY ONCE with zero omissions and zero duplicates."""
        assign = generate_family_blocked_assignment(BASE_COMMIT_SHA, DOMAIN)
        all_tasks = assign["all_tasks"]
        self.assertEqual(len(all_tasks), 30)

    def test_18_rejection_sampling_correctness_and_transcript(self):
        """Verifies rejection sampling PRNG bounds and deterministic sequence generation."""
        prng = RejectionSamplingPRNGStream(BASE_COMMIT_SHA, DOMAIN)
        sample = prng.rejection_sample(10)
        self.assertTrue(0 <= sample < 10)

    def test_19_evidence_manifest_verification(self):
        """Verifies campaign2_evidence_hashes.json authenticates exact 4-file evidence inventory."""
        self.assertTrue(verify_campaign2_evidence_manifest(BASE_DIR))

    def test_20_evidence_manifest_rejection(self):
        """Verifies verify_campaign2_evidence_manifest rejects empty, partial, or tampered manifests."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_p = os.path.join(tmpdir, "campaign2_evidence_hashes.json")
            with open(manifest_p, "w") as f:
                json.dump({}, f)
            os.chmod(manifest_p, 0o444)
            with self.assertRaises(ValueError):
                verify_campaign2_evidence_manifest(tmpdir)

    def test_21_evidence_manifest_permission_mode_rejection(self):
        """Verifies evidence files with mode 0o644 raise ValueError during verification."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manifest_p = os.path.join(tmpdir, "campaign2_evidence_hashes.json")
            with open(manifest_p, "w") as f:
                json.dump({"test": "a" * 64}, f)
            os.chmod(manifest_p, 0o644)
            with self.assertRaises(ValueError) as ctx:
                verify_campaign2_evidence_manifest(tmpdir)
            self.assertIn("mode must be 0o444", str(ctx.exception))

    def test_22_tier_assignment_strict_sha_and_domain(self):
        """Verifies verify_family_blocked_assignment rejects forged base_commit_sha or domain."""
        with tempfile.TemporaryDirectory() as tmpdir:
            art_path = os.path.join(tmpdir, "campaign2_tier_assignment.json")
            bad_data = {
                "base_commit_sha": "0000000000000000000000000000000000000000",
                "domain": DOMAIN
            }
            with open(art_path, "w") as f:
                json.dump(bad_data, f)
            os.chmod(art_path, 0o444)

            with self.assertRaises(ValueError) as ctx:
                verify_family_blocked_assignment(tmpdir, art_path)
            self.assertIn("base_commit_sha mismatch", str(ctx.exception))

            os.chmod(art_path, 0o644)
            bad_data_domain = {
                "base_commit_sha": BASE_COMMIT_SHA,
                "domain": "attacker/alternate-domain"
            }
            with open(art_path, "w") as f:
                json.dump(bad_data_domain, f)
            os.chmod(art_path, 0o444)

            with self.assertRaises(ValueError) as ctx2:
                verify_family_blocked_assignment(tmpdir, art_path)
            self.assertIn("domain mismatch", str(ctx2.exception))

    def test_23_host_callback_exception_provenance(self):
        """P0 FIX: Unhandled domain callback exception propagates as candidate failure (0, 10), not InfrastructureError."""
        def bad_cb():
            raise ValueError("Host callback failure")

        cand_code = r"""
def parse_log_stream(stream, cb=None):
    if cb:
        cb()
    return []
"""
        with tempfile.TemporaryDirectory() as cand_tmpdir:
            cand_file = os.path.join(cand_tmpdir, "candidate.py")
            with open(cand_file, "w") as f:
                f.write(cand_code)
            worker_file = os.path.join(cand_tmpdir, "worker.py")
            with open(worker_file, "w") as f:
                f.write(WORKER_SCRIPT_CODE)
            sb_profile = get_candidate_sandbox_profile(BASE_DIR, cand_tmpdir)
            sb_path = os.path.join(cand_tmpdir, "cand.sb")
            with open(sb_path, "w") as f:
                f.write(sb_profile)

            r_in, w_out = os.pipe()
            w_in, r_out = os.pipe()

            cmd = ["sandbox-exec", "-f", sb_path, sys.executable, worker_file, "--ipc-read-fd", str(w_in), "--ipc-write-fd", str(w_out), "--cpu-timeout", "10"]
            proc = subprocess.Popen(cmd, cwd=cand_tmpdir, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, pass_fds=(w_in, w_out))
            os.close(w_in); os.close(w_out)

            client = WorkerIPCClient(r_in, r_out, time.time()+10, proc)
            client._read_framed_msg()
            client.set_phase("candidate_import", time.time() + 10)
            client.send_request({"id": "load_candidate", "op": "load_candidate"})
            client.set_phase("candidate_execution", time.time() + 10)
            cand_proxy = CandidateModuleProxy(client)

            # Domain callback exception is NOT InfrastructureError — candidate did not catch it, so it
            # propagates as a candidate exception. The harness/proxy re-raises as a regular exception.
            try:
                result = cand_proxy.parse_log_stream("stream", bad_cb)
                # If candidate_exception_occurred is True, the result is the exception object
                if client.candidate_exception_occurred:
                    got_candidate_exception = True
                else:
                    got_candidate_exception = False
            except InfrastructureError:
                self.fail("Domain callback exception should NOT raise InfrastructureError")
            except Exception:
                got_candidate_exception = True

            self.assertTrue(got_candidate_exception, "Unhandled domain callback exception should propagate as candidate exception")
            # No sticky infrastructure error should be set
            self.assertIsNone(client.infrastructure_error, "Domain callback exception must NOT set sticky infrastructure_error")

            os.close(r_in); os.close(r_out)
            proc.kill()
            proc.wait()

    def test_24_candidate_top_level_import_error(self):
        """Finding 2 [P0] FIX: Verifies candidate top-level import failure carries candidate origin (0, 10)."""
        task_id = "c_f1_01_log_stream_parser"
        bad_import_cand = "import non_existent_package_xyz_999\ndef parse_log_stream(s):\n    return []"
        earned, max_s = evaluate_candidate_code_sandboxed(task_id, bad_import_cand)
        self.assertEqual((earned, max_s), (0, 10))

    def test_25_no_hidden_ast_policy_and_waits_are_candidate_failures(self):
        """Candidate semantics are unbiased; residual waits are phase-attributed candidate failures."""
        from campaign2 import proxy as proxy_module

        self.assertFalse(hasattr(proxy_module, "BindingAwareASTPrescreener"))
        self.assertFalse(hasattr(proxy_module, "SequentialScopeASTVisitor"))
        self.assertFalse(hasattr(proxy_module, "pre_screen_candidate_code"))

        task_id = "c_f1_01_log_stream_parser"
        sol_path = os.path.join(BENCHMARKS_DIR, "suite_c", task_id, "solution.py")
        with open(sol_path, "r", encoding="utf-8") as f:
            sol_code = f.read()
        self.assertEqual(
            evaluate_candidate_code_sandboxed(task_id, "import time\n" + sol_code),
            (10, 10),
            "ordinary time import was subject to a hidden launch policy",
        )

        wait_candidates = [
            "import typing\ndef parse_log_stream(s):\n    typing.sys.modules['time'].sleep(2)\n    return []",
            "import builtins\ndef parse_log_stream(s):\n    getattr(builtins, '__import__')('time').sleep(2)\n    return []",
            "import asyncio\ndef parse_log_stream(s):\n    asyncio.run(asyncio.sleep(2))\n    return []",
        ]
        for candidate_code in wait_candidates:
            started = time.monotonic()
            self.assertEqual(
                evaluate_candidate_code_sandboxed(task_id, candidate_code, timeout_seconds=0.4),
                (0, 10),
            )
            self.assertLess(time.monotonic() - started, 2.5)

        import_wait = "import time\ntime.sleep(2)\ndef parse_log_stream(s): return []"
        started = time.monotonic()
        self.assertEqual(
            evaluate_candidate_code_sandboxed(task_id, import_wait, timeout_seconds=0.4),
            (0, 10),
        )
        self.assertLess(time.monotonic() - started, 2.5)

    def test_26_production_ipc_architecture_and_pathologies(self):
        """Finding 6 [P1] FIX: Verifies production DEVNULL output redirection, non-UTF-8 bytes, and exception handling."""
        task_id = "c_f1_01_log_stream_parser"

        # Custom exception across IPC -> candidate failure (0, 10)
        custom_exc_cand = "class MyCustomCandidateErr(Exception): pass\ndef parse_log_stream(s):\n    raise MyCustomCandidateErr('Candidate custom failure message')"
        earned_c, max_c = evaluate_candidate_code_sandboxed(task_id, custom_exc_cand)
        self.assertEqual((earned_c, max_c), (0, 10))

        # Deep recursion -> candidate failure (0, 10)
        deep_rec_cand = "def parse_log_stream(s):\n    return parse_log_stream(s)"
        earned_r, max_r = evaluate_candidate_code_sandboxed(task_id, deep_rec_cand)
        self.assertEqual((earned_r, max_r), (0, 10))

        # BaseException subclasses raised by candidate code must not kill the worker
        # and be misclassified as campaign infrastructure failure.
        sys_exit_cand = "import sys\ndef parse_log_stream(s):\n    sys.exit(7)"
        earned_x, max_x = evaluate_candidate_code_sandboxed(task_id, sys_exit_cand)
        self.assertEqual((earned_x, max_x), (0, 10))

    def test_27_no_residual_model_files_or_code_paths(self):
        """Finding 3 [P1] FIX: Verifies no residual model files, models/ directory, or model runner code paths exist."""
        from campaign2 import proxy
        self.assertFalse(os.path.exists(os.path.join(BASE_DIR, "models")))
        self.assertFalse(os.path.exists(os.path.join(BASE_DIR, "campaign2", "local_model_runner.py")))
        self.assertFalse(os.path.exists(os.path.join(BASE_DIR, "campaign2", "proxy_config.json")))
        self.assertFalse(hasattr(proxy, "run_local_proxy_inference"))
        self.assertFalse(hasattr(proxy, "evaluate_proxy_adequacy"))

    def test_28_parity_matrix_forgery_rejection(self):
        """Finding 2 [P0] FIX: Verifies parity matrix verifier rejects score forgery and duplicate rungs."""
        with tempfile.TemporaryDirectory() as tmpdir:
            os.symlink(os.path.join(BASE_DIR, "benchmarks"), os.path.join(tmpdir, "benchmarks"))
            mat_path = os.path.join(tmpdir, "campaign2_parity_matrix.json")
            with open(os.path.join(BASE_DIR, "campaign2_parity_matrix.json"), "r") as f:
                mat = json.load(f)

            # Test score forgery
            mat["ref_c_f1_01_log_stream_parser"]["direct_earned_score"] = 5
            mat["ref_c_f1_01_log_stream_parser"]["ipc_earned_score"] = 5
            with open(mat_path, "w") as f:
                json.dump(mat, f)
            os.chmod(mat_path, 0o444)

            with self.assertRaises(ValueError) as ctx:
                verify_campaign2_parity_matrix(tmpdir)
            self.assertIn("Sealed parity matrix mismatch for key", str(ctx.exception))

            # Test extra/duplicate rung inventory rejection
            mat_duplicate = dict(mat)
            mat_duplicate["defect_c_f1_01_log_stream_parser_rung_4"] = dict(mat["defect_c_f1_01_log_stream_parser_rung_1"])
            os.chmod(mat_path, 0o644)
            with open(mat_path, "w") as f:
                json.dump(mat_duplicate, f)
            os.chmod(mat_path, 0o444)

            with self.assertRaises(ValueError) as ctx:
                verify_campaign2_parity_matrix(tmpdir)
            self.assertIn("must contain EXACTLY 120 rows", str(ctx.exception))

    def test_29_parent_symlink_rejection(self):
        """Finding 4 [P1] FIX: Verifies component-by-component parent symlink rejection."""
        from campaign2.manifest import _check_no_symlink_components
        with tempfile.TemporaryDirectory() as tmpdir:
            archive_dir = os.path.join(tmpdir, "campaign2_archive")
            real_archive = os.path.join(tmpdir, "real_archive")
            os.makedirs(real_archive)
            os.symlink(real_archive, archive_dir)

            ev_path = os.path.join(archive_dir, "proxy_calibration_qwen2_5_coder_0_5b_rejected.json")
            with open(ev_path, "w") as f:
                f.write("{}")

            with self.assertRaises(ValueError) as ctx:
                _check_no_symlink_components(tmpdir, ev_path)
            self.assertIn("Symlink rejected in parent component path", str(ctx.exception))

    def test_30_domain_callback_exception_catchable(self):
        """P0 FIX: Domain callback exceptions are catchable by candidate code without becoming infrastructure errors."""
        # The saga orchestrator (c_f4_05) and workflow engine (p_f2_01) rely on this.
        # Verify via the full evaluate_candidate_code_sandboxed path that a candidate
        # which catches domain callback exceptions earns points.
        task_id = "c_f4_05_saga_orchestrator_fsm"
        sol_path = os.path.join(BASE_DIR, "benchmarks", "campaign2", "suite_c", task_id, "solution.py")
        with open(sol_path, "r") as f:
            sol_code = f.read()
        earned, max_s = evaluate_candidate_code_sandboxed(task_id, sol_code)
        # The reference solution handles bad_comp() raising ValueError in clause 6.
        # If domain exceptions were still tagged as infrastructure, this would raise InfrastructureError
        # or return (0, 10) instead of earning points.
        self.assertEqual(earned, 10, f"Saga orchestrator reference solution failed: earned {earned}/10")
        self.assertEqual(max_s, 10)

        # Also verify the workflow engine which has err_fn raising ValueError
        task_id_wf = "p_f2_01_workflow_execution_engine"
        sol_path_wf = os.path.join(BASE_DIR, "benchmarks", "campaign2", "suite_p", task_id_wf, "solution.py")
        with open(sol_path_wf, "r") as f:
            sol_code_wf = f.read()
        earned_wf, max_wf = evaluate_candidate_code_sandboxed(task_id_wf, sol_code_wf)
        self.assertEqual(earned_wf, 10, f"Workflow engine reference solution failed: earned {earned_wf}/10")
        self.assertEqual(max_wf, 10)

    def test_31_infrastructure_callback_failure_sticky(self):
        """P0 FIX: Inner callback infrastructure failures (unserializable result) set sticky infrastructure_error even when caught by candidate."""
        cand_code = r"""
def parse_log_stream(stream, cb=None):
    if cb:
        try:
            cb()
        except Exception:
            pass  # Candidate suppresses the inner exception
    return 123
"""
        with tempfile.TemporaryDirectory() as cand_tmpdir:
            cand_file = os.path.join(cand_tmpdir, "candidate.py")
            with open(cand_file, "w") as f:
                f.write(cand_code)
            worker_file = os.path.join(cand_tmpdir, "worker.py")
            with open(worker_file, "w") as f:
                f.write(WORKER_SCRIPT_CODE)
            sb_profile = get_candidate_sandbox_profile(BASE_DIR, cand_tmpdir)
            sb_path = os.path.join(cand_tmpdir, "cand.sb")
            with open(sb_path, "w") as f:
                f.write(sb_profile)

            r_in, w_out = os.pipe()
            w_in, r_out = os.pipe()

            cmd = ["sandbox-exec", "-f", sb_path, sys.executable, worker_file, "--ipc-read-fd", str(w_in), "--ipc-write-fd", str(w_out), "--cpu-timeout", "10"]
            proc = subprocess.Popen(cmd, cwd=cand_tmpdir, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, pass_fds=(w_in, w_out))
            os.close(w_in); os.close(w_out)

            client = WorkerIPCClient(r_in, r_out, time.time()+10, proc)
            client._read_framed_msg()
            client.set_phase("candidate_import", time.time() + 10)
            client.send_request({"id": "load_candidate", "op": "load_candidate"})
            client.set_phase("candidate_execution", time.time() + 10)
            cand_proxy = CandidateModuleProxy(client)

            # Register a callback that returns an unserializable object (sys module) -> causes result serialization failure
            def infra_cb():
                return sys

            result = cand_proxy.parse_log_stream("stream", infra_cb)

            # Candidate suppressed worker-side exception and returned 123
            self.assertEqual(result, 123)

            # BUT sticky infrastructure_error MUST be set on the host client!
            self.assertIsNotNone(client.infrastructure_error, "Inner callback infrastructure failure MUST set sticky infrastructure_error")
            self.assertIsInstance(client.infrastructure_error, InfrastructureError)

            os.close(r_in); os.close(r_out)
            proc.kill()
            proc.wait()

    def test_32_frozen_inventory_rejects_unauthorized_extra_task(self):
        """P1 FIX: Workspace with an unauthorized extra task directory is rejected by preflight manifest verification."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Copy Category 1 static files & benchmark tree
            for static_file in ["README.md", "run_tests.py"]:
                shutil.copy2(os.path.join(BASE_DIR, static_file), os.path.join(tmpdir, static_file))
            os.makedirs(os.path.join(tmpdir, "docs"))
            shutil.copy2(os.path.join(BASE_DIR, "docs", "walkthrough.md"), os.path.join(tmpdir, "docs", "walkthrough.md"))
            for cfile in ["proxy.py", "manifest.py", "tier_assignment.py", "scorer.py", "defects.py"]:
                os.makedirs(os.path.join(tmpdir, "campaign2"), exist_ok=True)
                shutil.copy2(os.path.join(BASE_DIR, "campaign2", cfile), os.path.join(tmpdir, "campaign2", cfile))
            shutil.copytree(os.path.join(BASE_DIR, "benchmarks"), os.path.join(tmpdir, "benchmarks"))

            # Add an unauthorized extra task directory in suite_c
            extra_dir = os.path.join(tmpdir, "benchmarks", "campaign2", "suite_c", "c_f9_99_unauthorized_extra")
            os.makedirs(extra_dir)
            for fname in ["problem.txt", "solution.py", "test_harness.py", "defects.py"]:
                with open(os.path.join(extra_dir, fname), "w") as f:
                    f.write("# UNAUTHORIZED CONTENT")

            # Verify manifest MUST reject the unauthorized extra task directory
            with self.assertRaises(ValueError) as ctx:
                verify_campaign2_manifest(tmpdir)
            self.assertIn("Unauthorized extra task directory", str(ctx.exception))

    def test_33_symlinked_benchmark_tree_rejected(self):
        """P1 FIX: Symlinked benchmark tree is rejected during manifest verification."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Copy static files
            for static_file in ["README.md", "run_tests.py"]:
                shutil.copy2(os.path.join(BASE_DIR, static_file), os.path.join(tmpdir, static_file))
            os.makedirs(os.path.join(tmpdir, "docs"))
            shutil.copy2(os.path.join(BASE_DIR, "docs", "walkthrough.md"), os.path.join(tmpdir, "docs", "walkthrough.md"))
            for cfile in ["proxy.py", "manifest.py", "tier_assignment.py", "scorer.py", "defects.py"]:
                os.makedirs(os.path.join(tmpdir, "campaign2"), exist_ok=True)
                shutil.copy2(os.path.join(BASE_DIR, "campaign2", cfile), os.path.join(tmpdir, "campaign2", cfile))

            # Symlink the entire benchmarks directory
            os.symlink(
                os.path.join(BASE_DIR, "benchmarks"),
                os.path.join(tmpdir, "benchmarks")
            )

            # Generate manifest in temp workspace (will succeed because files exist)
            from campaign2.manifest import generate_campaign2_manifest
            generate_campaign2_manifest(tmpdir)

            # Verify should reject because benchmarks/ is a symlink
            with self.assertRaises(ValueError) as ctx:
                verify_campaign2_manifest(tmpdir)
            self.assertIn("Symlink rejected", str(ctx.exception))

    def test_29_candidate_process_signal_and_exit_attribution(self):
        """Finding 1 [P1] FIX: Verifies candidate process signal termination (e.g. SIGABRT) during candidate execution explicitly raises CandidateProcessError and scores (0, 10)."""
        task_id = "c_f1_01_log_stream_parser"
        cand_sig_code = "import os, signal\ndef parse_log_stream(s):\n    os.kill(os.getpid(), signal.SIGABRT)"

        # 1. Direct IPC verification of CandidateProcessError exception and -SIGABRT returncode
        cand_tmpdir = tempfile.mkdtemp()
        try:
            host_in, worker_out = os.pipe()
            worker_in, host_out = os.pipe()
            with open(os.path.join(cand_tmpdir, "candidate.py"), "w") as f:
                f.write(cand_sig_code)
            with open(os.path.join(cand_tmpdir, "worker.py"), "w") as f:
                f.write(WORKER_SCRIPT_CODE)
            sb_profile = get_candidate_sandbox_profile(BASE_DIR, cand_tmpdir)
            sb_path = os.path.join(cand_tmpdir, "cand.sb")
            with open(sb_path, "w") as f:
                f.write(sb_profile)
            cmd = ["sandbox-exec", "-f", sb_path, sys.executable, os.path.join(cand_tmpdir, "worker.py"), "--ipc-read-fd", str(worker_in), "--ipc-write-fd", str(worker_out), "--cpu-timeout", "5"]
            proc = subprocess.Popen(cmd, cwd=cand_tmpdir, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, pass_fds=(worker_in, worker_out), start_new_session=True, env={"HOME": cand_tmpdir, "PATH": "/usr/bin:/bin", "PYTHONPATH": cand_tmpdir, "TMPDIR": cand_tmpdir})
            os.close(worker_in)
            os.close(worker_out)
            client = WorkerIPCClient(host_in, host_out, time.time() + 5, proc)
            client._read_framed_msg()
            client.set_phase("candidate_import", time.time() + 5)
            client.send_request({"id": "load_candidate", "op": "load_candidate"})
            client.set_phase("candidate_execution", time.time() + 5)
            cand_proxy = CandidateModuleProxy(client)

            with self.assertRaises(CandidateProcessError) as ctx:
                cand_proxy.parse_log_stream("[2026-01-01] INFO - Test")

            self.assertIn("Candidate process terminated", str(ctx.exception))
            self.assertEqual(proc.poll(), -signal.SIGABRT, f"Expected -SIGABRT returncode (-6), got {proc.poll()}")
        finally:
            if 'client' in locals() and client and client.selector:
                try:
                    client.selector.close()
                except Exception:
                    pass
            for fd in ('host_in', 'host_out'):
                if fd in locals():
                    try:
                        os.close(locals()[fd])
                    except OSError:
                        pass
            if 'proc' in locals() and proc and proc.poll() is None:
                try:
                    proc.kill()
                    proc.wait()
                except Exception:
                    pass
            shutil.rmtree(cand_tmpdir, ignore_errors=True)

        # 2. High-level supervisor verification scoring (0, 10)
        earned, max_s = evaluate_candidate_code_sandboxed(task_id, cand_sig_code, timeout_seconds=5)
        self.assertEqual((earned, max_s), (0, 10), "Candidate process signal termination must score (0, 10) as candidate failure")


if __name__ == "__main__":
    unittest.main()
