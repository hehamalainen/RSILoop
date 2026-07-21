"""
test_rsi_loop.py — Tests for the RSI orchestrator layer and main execution path.
"""
import os
import sys
import unittest
import json
import tempfile
import shutil
import hashlib
import subprocess
import gc
import warnings
import stat
from unittest import mock

# Treat warnings as errors
warnings.simplefilter('error', ResourceWarning)

if getattr(sys.unraisablehook, "__name__", "") == "unraisable_hook":
    main_mod = sys.modules.get('__main__')
    unraisable_exceptions = getattr(main_mod, 'unraisable_exceptions', [])
else:
    unraisable_exceptions = []
    def custom_unraisable_hook(unraisable):
        unraisable_exceptions.append(unraisable)
    sys.unraisablehook = custom_unraisable_hook

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

from rsi_loop import (
    parse_codex_jsonl_output,
    extract_code,
    TOKEN_CEILING,
    BEST_MANIFEST_PATH,
    BENCHMARKS_DIR,
    VALIDATION_DIR,
    ExperimentInvalidError,
    RunContext,
    is_contained,
    verify_preflight_hashes,
    verify_experiment_lock,
    normalize_and_truncate_stream,
    get_stderr_summary,
    main,
    init_operator_report,
    save_operator_report,
    log_phase_transition,
    publish_terminal_manifest_and_report,
    run_smoke_and_canary,
    validate_manifest_pointer_and_target,
    _cleanup_canary_dir,
    sync_context_to_report,
    recover_pending_runs,
    update_best_manifest_pointer,
    _main_exec,
)

class TestRsiLoopOrchestrator(unittest.TestCase):
    def setUp(self):
        unraisable_exceptions.clear()
        self.tmp_dir = tempfile.mkdtemp()
        self.patchers = []
        p = mock.patch("rsi_loop.BEST_MANIFEST_PATH", os.path.realpath(os.path.join(self.tmp_dir, "best_manifest.json")))
        p.start()
        self.patchers.append(p)

    def tearDown(self):
        for patcher in self.patchers:
            patcher.stop()
        gc.collect()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        if unraisable_exceptions:
            errors = [f"{type(x.exc_value).__name__}: {x.exc_value}" for x in unraisable_exceptions]
            unraisable_exceptions.clear()
            self.fail(f"ResourceWarning or unraisable exception detected: {errors}")

    def _assert_terminal_metadata(self, manifest):
        for key in ("sw_vers", "python_version", "codex_cli_version"):
            self.assertIn(key, manifest)
            self.assertIsInstance(manifest[key], str)
            self.assertTrue(manifest[key])
        self.assertEqual(manifest.get("model_resolved"), "unavailable")
        self.assertIn("model_name", manifest)
        self.assertEqual(manifest.get("model_requested"), manifest["model_name"])

    # ── Test 21: standard parse ───────────────────────────────────────────────

    def test_parse_jsonl_standard(self):
        lines = [
            '{"type":"thread.started","thread_id":"abc"}',
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"print(\'hello\')"}}',
            '{"type":"turn.completed","usage":{"input_tokens":100,"output_tokens":5}}',
        ]
        code, usage = parse_codex_jsonl_output(lines)
        self.assertEqual(code, "print('hello')")
        self.assertEqual(usage.get("output_tokens"), 5)

    # ── Test 22: multiple agent messages → last selected ─────────────────────

    def test_parse_jsonl_multiple_agent_messages(self):
        lines = [
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"first"}}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"second"}}',
            '{"type":"turn.completed","usage":{"input_tokens":100,"output_tokens":10}}',
        ]
        code, usage = parse_codex_jsonl_output(lines)
        self.assertEqual(code, "second")
        self.assertEqual(usage.get("output_tokens"), 10)

    # ── Test 23: missing turn.completed ──────────────────────────────────────

    def test_parse_jsonl_missing_turn_completed(self):
        lines = [
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"hello"}}',
        ]
        with self.assertRaises(ExperimentInvalidError) as ctx:
            parse_codex_jsonl_output(lines)
        self.assertIn("turn.completed", str(ctx.exception))

    # ── Test 24: duplicate turn.completed rejected ────────────────────────────

    def test_parse_jsonl_duplicate_turn_completed(self):
        lines = [
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"hello"}}',
            '{"type":"turn.completed","usage":{"output_tokens":5}}',
            '{"type":"turn.completed","usage":{"output_tokens":3}}',
        ]
        with self.assertRaises(ExperimentInvalidError) as ctx:
            parse_codex_jsonl_output(lines)
        self.assertIn("multiple turn.completed", str(ctx.exception))

    # ── Test 25: malformed JSON raises immediately ────────────────────────────

    def test_parse_jsonl_malformed_event(self):
        lines = [
            '{"type":"turn.started"}',
            'this is not json {{{',
        ]
        with self.assertRaises(ExperimentInvalidError):
            parse_codex_jsonl_output(lines)

    # ── Test 26: extract_code fenced ─────────────────────────────────────────

    def test_extract_code_fenced(self):
        raw = "Here is the code:\n```python\ndef greet():\n    return 'hello'\n```\nDone."
        code = extract_code(raw)
        self.assertEqual(code, "def greet():\n    return 'hello'")

    # ── Test 27: extract_code plain text ─────────────────────────────────────

    def test_extract_code_plain_text(self):
        raw = "Write solutions that are concise and correct."
        code = extract_code(raw)
        self.assertEqual(code, raw)

    # ── Test 28: interrupted atomic promotion ─────────────────────────────────

    def test_interrupted_atomic_promotion(self):
        run_dir = tempfile.mkdtemp()
        test_best = os.path.join(run_dir, "best_manifest.json")

        from rsi_loop import _write_once_fsynced_manifest
        data1 = {"run_id": "run_1", "score": 0.66, "schema_version": "2"}
        _write_once_fsynced_manifest(test_best, data1)

        with open(test_best) as f:
            loaded = json.load(f)
        self.assertEqual(loaded["run_id"], "run_1")

        tmp_path = test_best + ".tmp"
        with open(tmp_path, "w") as f:
            f.write('{"score": 1.0')

        with open(test_best) as f:
            loaded2 = json.load(f)
        self.assertEqual(loaded2["run_id"], "run_1")
        self.assertEqual(loaded2["score"], 0.66)

        shutil.rmtree(run_dir)

    # ── Test 29: mock Codex accounting ceiling breach ────────────────────────

    def test_mock_codex_accounting_ceiling(self):
        from rsi_loop import call_codex_sandboxed

        fake_stdout = "\n".join([
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"code"}}',
            '{"type":"turn.completed","model":"gpt-5.6-sol","usage":{"input_tokens":100,"output_tokens":49999}}',
        ])

        ctx = RunContext()
        ctx.consumed_tokens = 2
        ctx.codex_model = "codex-mini-latest"
        ctx.model_resolved = "unavailable"

        with mock.patch("rsi_loop._build_codex_run_scoped_home", return_value=tempfile.mkdtemp()), \
             mock.patch("rsi_loop._build_generator_sb", return_value="/dev/null"), \
             mock.patch("rsi_loop._cleanup_codex_run_scoped_home"), \
             mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=0,
                stdout=fake_stdout,
                stderr="",
            )
            with self.assertRaises(ExperimentInvalidError) as ctx_err:
                call_codex_sandboxed(
                    "test prompt", "system", "/tmp", ctx
                )
            self.assertIn("ceiling breached", str(ctx_err.exception).lower())
            self.assertEqual(ctx.consumed_tokens, 50001)

    # ── Test 29b: mock Codex invalid token values ─────────────────────────────

    def test_mock_codex_invalid_token_values(self):
        from rsi_loop import call_codex_sandboxed

        # Test cases for invalid token values
        invalid_tokens = [
            "45",         # String
            -10,          # Negative
            12.5,         # Float
            True,         # Boolean
        ]

        for val in invalid_tokens:
            fake_stdout = "\n".join([
                '{"type":"turn.started"}',
                '{"type":"item.completed","item":{"type":"agent_message","text":"code"}}',
                f'{{"type":"turn.completed","model":"gpt-5.6-sol","usage":{{"input_tokens":100,"output_tokens":{json.dumps(val)}}}}}',
            ])

            ctx = RunContext()
            ctx.consumed_tokens = 0
            ctx.codex_model = "codex-mini-latest"
            ctx.model_resolved = "unavailable"

            with mock.patch("rsi_loop._build_codex_run_scoped_home", return_value=tempfile.mkdtemp()), \
                 mock.patch("rsi_loop._build_generator_sb", return_value="/dev/null"), \
                 mock.patch("rsi_loop._cleanup_codex_run_scoped_home"), \
                 mock.patch("subprocess.run") as mock_run:
                mock_run.return_value = mock.Mock(
                    returncode=0,
                    stdout=fake_stdout,
                    stderr="",
                )
                with self.assertRaises(ExperimentInvalidError) as ctx_err:
                    call_codex_sandboxed(
                        "test prompt", "system", "/tmp", ctx
                    )
                self.assertTrue(ctx.unknown_token_state)
                self.assertEqual(ctx.consumed_tokens, 0)

    # ── Test 30: generator credential isolation denials ──────────────────────

    def test_generator_credential_isolation_denials(self):
        sb_template_path = os.path.join(BASE_DIR, "generator.sb.template")
        real_temp_home = tempfile.mkdtemp()
        try:
            from rsi_loop import _build_generator_sb
            sb_path = _build_generator_sb(real_temp_home, real_temp_home)

            repo_file = os.path.join(BASE_DIR, "rsi_loop.py")
            cmd = ["sandbox-exec", "-f", sb_path, "cat", repo_file]
            res = subprocess.run(cmd, capture_output=True, timeout=5)
            self.assertNotEqual(res.returncode, 0, "Generator should deny reading repository files")
        finally:
            shutil.rmtree(real_temp_home)

    # ── Test 31: Run states & invalid-run pointer isolation ───────────────────

    def test_run_states_and_pointer_isolation(self):
        from rsi_loop import update_best_manifest_pointer, load_and_verify_best_manifest

        run_dir = tempfile.mkdtemp()
        try:
            history_dir = os.path.join(run_dir, "history")
            os.makedirs(history_dir)
            target_dir = os.path.join(history_dir, "run_123")
            os.makedirs(target_dir)

            prompt_path = os.path.join(target_dir, "inner_agent_best.txt")
            with open(prompt_path, "w") as f:
                f.write("content")
            os.chmod(prompt_path, 0o444)

            run_manifest = os.path.join(target_dir, "run_manifest.json")
            data = {
                "schema_version": "2",
                "status": "completed_promoted",
                "selected_prompt_path": "inner_agent_best.txt",
                "selected_prompt_hash": hashlib.sha256(b"content").hexdigest()
            }

            with mock.patch("rsi_loop.BEST_MANIFEST_PATH", os.path.join(run_dir, "best_manifest.json")), \
                 mock.patch("rsi_loop.BASE_DIR", run_dir), \
                 mock.patch("rsi_loop.HISTORY_DIR", history_dir):
                with open(run_manifest, "w") as f:
                    json.dump(data, f)
                report_path = os.path.join(target_dir, "operator_report.json")
                with open(report_path, "w") as f:
                    json.dump({
                        "schema_version": "2",
                        "status": "completed_promoted",
                        "pointer_commit_status": "committed",
                        "manifest_sha256": hashlib.sha256(json.dumps(data).encode('utf-8')).hexdigest()
                    }, f)
                os.chmod(report_path, 0o444)
                update_best_manifest_pointer(run_manifest)

                loaded = load_and_verify_best_manifest()
                self.assertIsNotNone(loaded)
                self.assertEqual(loaded["status"], "completed_promoted")
        finally:
            shutil.rmtree(run_dir)

    # ── Test 32: Hash verification failure checks (Missing/Extra/Traversal/Symlink) ──

    def test_preflight_hash_verification_failures(self):
        from rsi_loop import verify_preflight_hashes

        with mock.patch("rsi_loop.PREFLIGHT_HASHES_PATH", "/nonexistent_preflight.json"):
            with self.assertRaises(ExperimentInvalidError):
                verify_preflight_hashes()

        bad_hashes = {"bad/file.txt": "dummy_sha"}
        hashes_file = os.path.join(self.tmp_dir, "preflight_hashes.json")
        with open(hashes_file, "w") as f:
            json.dump(bad_hashes, f)

        with mock.patch("rsi_loop.PREFLIGHT_HASHES_PATH", hashes_file):
            with self.assertRaises(ExperimentInvalidError) as ctx:
                verify_preflight_hashes()
            self.assertIn("Preflight keys mismatch", str(ctx.exception))

        traversal_hashes = {"../bad/file.txt": "dummy_sha"}
        with open(hashes_file, "w") as f:
            json.dump(traversal_hashes, f)
        with mock.patch("rsi_loop.PREFLIGHT_HASHES_PATH", hashes_file), \
             mock.patch("rsi_loop.preflight_hashes_global", return_value=traversal_hashes), \
             mock.patch("rsi_loop.EXPECTED_PREFLIGHT_KEYS", {"../bad/file.txt"}):
            with self.assertRaises(ExperimentInvalidError) as ctx:
                verify_preflight_hashes()
            self.assertIn("Path traversal detected", str(ctx.exception))

        symlink_dir = os.path.join(self.tmp_dir, "sym_dir")
        os.makedirs(symlink_dir, exist_ok=True)
        target_file = os.path.join(symlink_dir, "target.txt")
        with open(target_file, "w") as f:
            f.write("hello")

        link_file = os.path.join(self.tmp_dir, "link.txt")
        os.symlink(target_file, link_file)

        symlink_hashes = {"candidate.sb.template": "dummy"}
        with open(hashes_file, "w") as f:
            json.dump(symlink_hashes, f)
        with mock.patch("rsi_loop.BASE_DIR", self.tmp_dir), \
             mock.patch("rsi_loop.PREFLIGHT_HASHES_PATH", hashes_file), \
             mock.patch("rsi_loop.preflight_hashes_global", return_value={"candidate.sb.template": "dummy"}), \
             mock.patch("rsi_loop.EXPECTED_PREFLIGHT_KEYS", {"candidate.sb.template"}):
            shutil.copy2(target_file, os.path.join(self.tmp_dir, "candidate.sb.template"))
            os.remove(os.path.join(self.tmp_dir, "candidate.sb.template"))
            os.symlink(target_file, os.path.join(self.tmp_dir, "candidate.sb.template"))

            with self.assertRaises(ExperimentInvalidError) as ctx:
                verify_preflight_hashes()
            self.assertIn("Symlink detected", str(ctx.exception))

    # ── Test 33: Git lock verification integration test ────────────────────────

    def test_git_lock_integration(self):
        git_dir = os.path.join(self.tmp_dir, "git_repo")
        os.makedirs(git_dir)

        subprocess.run(["git", "init"], cwd=git_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=git_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=git_dir, check=True)

        dummy_file = os.path.join(git_dir, "file.txt")
        with open(dummy_file, "w") as f:
            f.write("initial commit")
        subprocess.run(["git", "add", "file.txt"], cwd=git_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "commit1"], cwd=git_dir, check=True, capture_output=True)

        gitignore_path = os.path.join(git_dir, ".gitignore")
        with open(gitignore_path, "w") as f:
            f.write("experiment.lock\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=git_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add gitignore"], cwd=git_dir, check=True, capture_output=True)

        res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=git_dir, capture_output=True, text=True, check=True)
        commit_sha = res.stdout.strip()

        lock_path = os.path.join(git_dir, "experiment.lock")
        lock_data = {
            "approved_commit_sha": commit_sha,
            "remote_url": "https://github.com/hehamalainen/RSILoop.git",
            "model": "gpt-5.6-sol"
        }
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)

        original_run = subprocess.run
        def mock_git_run(args, **kwargs):
            cmd_str = " ".join(args)
            if "remote get-url" in cmd_str:
                return mock.Mock(stdout="https://github.com/hehamalainen/RSILoop.git\n", stderr="", returncode=0)
            elif "ls-remote" in cmd_str:
                return mock.Mock(stdout=commit_sha + "\trefs/heads/main\n", stderr="", returncode=0)
            return original_run(args, **kwargs)

        ctx = RunContext()
        ctx.operator_report = {}
        init_operator_report(ctx)
        with mock.patch("rsi_loop.BASE_DIR", git_dir), \
             mock.patch("rsi_loop.subprocess.run", side_effect=mock_git_run):
            sha = verify_experiment_lock(ctx)
            self.assertEqual(sha, commit_sha)
            self.assertEqual(ctx.approved_commit_sha, commit_sha)

        with open(dummy_file, "w") as f:
            f.write("dirty change")
        with mock.patch("rsi_loop.BASE_DIR", git_dir):
            with self.assertRaises(ExperimentInvalidError):
                verify_experiment_lock(ctx)

    # ── Test 34: E2E main() states check (no_headroom, completed_no_improvement, completed_promoted, invalid) ──

    def test_main_orchestration_states(self):
        def fake_verify_lock(ctx):
            ctx.approved_commit_sha = "approved_sha"
            ctx.operator_report["lock_contents"] = {
                "approved_commit_sha": "approved_sha",
                "remote_url": "https://github.com/hehamalainen/RSILoop.git",
                "model": "gpt-5.6-sol",
                "allow_no_holdout": True,
            }
            ctx.lock_file_hash = "mock_lock_hash"
            return "approved_sha"
        mock_verify_lock = mock.patch("rsi_loop.verify_experiment_lock", side_effect=fake_verify_lock)
        mock_verify_hashes = mock.patch("rsi_loop.verify_preflight_hashes")
        def fake_smoke(ctx):
            ctx.codex_version = "codex-cli 0.144.5"
            ctx.codex_cli_version = ctx.codex_version
            ctx.model_resolved = "unavailable"
            return ctx.codex_version, 50

        mock_smoke = mock.patch("rsi_loop.run_smoke_and_canary", side_effect=fake_smoke)
        mock_list_opt = mock.patch("rsi_loop._list_optimization_benchmarks", return_value=["task1"])
        mock_list_val = mock.patch("rsi_loop._list_validation_benchmarks", return_value=["val1"])

        local_patchers = [mock_verify_lock, mock_verify_hashes, mock_smoke, mock_list_opt, mock_list_val]
        for p in local_patchers:
            p.start()
        self.patchers.extend(local_patchers)

        eval_success = {"success": True, "log": "Ran 5 tests\nOK"}
        eval_fail = {"success": False, "log": "Ran 5 tests\nFAIL"}

        real_base_dir = os.path.dirname(os.path.abspath(__file__))
        shutil.copy(os.path.join(real_base_dir, "candidate.sb.template"), self.tmp_dir)
        shutil.copy(os.path.join(real_base_dir, "generator.sb.template"), self.tmp_dir)

        with mock.patch("rsi_loop.call_codex_sandboxed", return_value=("mock_code", 10)), \
             mock.patch("rsi_loop.run_evaluation_on_candidate", return_value=eval_success), \
             mock.patch("rsi_loop.HISTORY_DIR", self.tmp_dir), \
             mock.patch("rsi_loop.BASE_DIR", os.path.realpath(self.tmp_dir)), \
             mock.patch("rsi_loop.BEST_MANIFEST_PATH", os.path.join(self.tmp_dir, "best_manifest.json")), \
             mock.patch("rsi_loop.BASELINE_PROMPT_PATH", os.path.join(self.tmp_dir, "baseline.txt")), \
             mock.patch.dict(os.environ, {"ALLOW_NO_HOLDOUT": "1"}), \
             self.assertRaises(SystemExit) as sy_exit:
            os.makedirs(os.path.join(self.tmp_dir, "prompt_templates"), exist_ok=True)
            with open(os.path.join(self.tmp_dir, "baseline.txt"), "w") as f:
                f.write("baseline prompt")
            main()
        self.assertEqual(sy_exit.exception.code, 0)

        runs = [d for d in os.listdir(self.tmp_dir) if d.startswith("run_")]
        self.assertEqual(len(runs), 1)
        manifest_path = os.path.join(self.tmp_dir, runs[0], "run_manifest.json")
        with open(manifest_path) as f:
            manifest_data = json.load(f)
        self._assert_terminal_metadata(manifest_data)
        self.assertEqual(manifest_data["status"], "no_headroom")

        best_manifest_path = os.path.join(self.tmp_dir, "best_manifest.json")
        self.assertTrue(os.path.exists(best_manifest_path))
        with open(best_manifest_path) as f:
            pointer = json.load(f)
        self.assertEqual(pointer["pointer_schema_version"], "2")
        self.assertEqual(pointer["run_manifest_path"], os.path.relpath(manifest_path, self.tmp_dir))
        with open(manifest_path, "rb") as f:
            expected_hash = hashlib.sha256(f.read()).hexdigest()
        self.assertEqual(pointer["run_manifest_sha256"], expected_hash)

        shutil.rmtree(self.tmp_dir)
        self.tmp_dir = tempfile.mkdtemp()
        shutil.copy(os.path.join(real_base_dir, "candidate.sb.template"), self.tmp_dir)
        shutil.copy(os.path.join(real_base_dir, "generator.sb.template"), self.tmp_dir)
        os.makedirs(os.path.join(self.tmp_dir, "prompt_templates"), exist_ok=True)
        with open(os.path.join(self.tmp_dir, "baseline.txt"), "w") as f:
            f.write("baseline prompt")

        with mock.patch("rsi_loop.call_codex_sandboxed", return_value=("mock_code", 10)), \
             mock.patch("rsi_loop.run_evaluation_on_candidate", return_value=eval_fail), \
             mock.patch("rsi_loop.evaluate_prompt_on_tasks", return_value=(0.5, {}, 10)), \
             mock.patch("rsi_loop.HISTORY_DIR", self.tmp_dir), \
             mock.patch("rsi_loop.BASE_DIR", os.path.realpath(self.tmp_dir)), \
             mock.patch("rsi_loop.BEST_MANIFEST_PATH", os.path.join(self.tmp_dir, "best_manifest.json")), \
             mock.patch("rsi_loop.BASELINE_PROMPT_PATH", os.path.join(self.tmp_dir, "baseline.txt")), \
             mock.patch.dict(os.environ, {"ALLOW_NO_HOLDOUT": "1"}), \
             self.assertRaises(SystemExit) as sy_exit:
            main()
        self.assertEqual(sy_exit.exception.code, 0)
        runs = [d for d in os.listdir(self.tmp_dir) if d.startswith("run_")]
        self.assertEqual(len(runs), 1)
        manifest_path = os.path.join(self.tmp_dir, runs[0], "run_manifest.json")
        with open(manifest_path) as f:
            manifest_data = json.load(f)
        self._assert_terminal_metadata(manifest_data)
        self.assertEqual(manifest_data["status"], "completed_no_improvement")

        shutil.rmtree(self.tmp_dir)
        self.tmp_dir = tempfile.mkdtemp()
        shutil.copy(os.path.join(real_base_dir, "candidate.sb.template"), self.tmp_dir)
        shutil.copy(os.path.join(real_base_dir, "generator.sb.template"), self.tmp_dir)
        os.makedirs(os.path.join(self.tmp_dir, "prompt_templates"), exist_ok=True)
        with open(os.path.join(self.tmp_dir, "baseline.txt"), "w") as f:
            f.write("baseline prompt")

        with mock.patch("rsi_loop.call_codex_sandboxed", side_effect=RuntimeError("API failure")), \
             mock.patch("rsi_loop.HISTORY_DIR", self.tmp_dir), \
             mock.patch("rsi_loop.BASE_DIR", os.path.realpath(self.tmp_dir)), \
             mock.patch("rsi_loop.BEST_MANIFEST_PATH", os.path.join(self.tmp_dir, "best_manifest.json")), \
             mock.patch("rsi_loop.BASELINE_PROMPT_PATH", os.path.join(self.tmp_dir, "baseline.txt")), \
             mock.patch.dict(os.environ, {"ALLOW_NO_HOLDOUT": "1"}), \
             self.assertRaises(SystemExit) as sy_exit:
            main()
        self.assertEqual(sy_exit.exception.code, 1)
        runs = [d for d in os.listdir(self.tmp_dir) if d.startswith("run_")]
        self.assertEqual(len(runs), 1)
        invalid_manifest_path = os.path.join(self.tmp_dir, runs[0], "invalid_manifest.json")
        with open(invalid_manifest_path) as f:
            invalid_data = json.load(f)
        self._assert_terminal_metadata(invalid_data)
        self.assertEqual(invalid_data["status"], "invalid")
        self.assertIn("API failure", invalid_data["error_message"])

        # Test case 4: completed_promoted (with holdout)
        shutil.rmtree(self.tmp_dir)
        self.tmp_dir = tempfile.mkdtemp()
        shutil.copy(os.path.join(real_base_dir, "candidate.sb.template"), self.tmp_dir)
        shutil.copy(os.path.join(real_base_dir, "generator.sb.template"), self.tmp_dir)
        os.makedirs(os.path.join(self.tmp_dir, "prompt_templates"), exist_ok=True)
        with open(os.path.join(self.tmp_dir, "baseline.txt"), "w") as f:
            f.write("baseline prompt")

        # Set up a fake private holdout directory
        private_dir = os.path.join(self.tmp_dir, "private", "benchmarks")
        regex_dir = os.path.join(private_dir, "simple_regex_matcher")
        os.makedirs(regex_dir, exist_ok=True)

        problem_file = os.path.join(regex_dir, "problem.txt")
        with open(problem_file, "w") as f:
            f.write("regex problem")
        harness_file = os.path.join(regex_dir, "test_holdout.py")
        with open(harness_file, "w") as f:
            f.write("regex harness")

        prob_hash = hashlib.sha256(b"regex problem").hexdigest()
        harn_hash = hashlib.sha256(b"regex harness").hexdigest()

        # Calculate holdout_hashes.json
        dummy_hash = hashlib.sha256(b"dummy").hexdigest()
        holdout_hashes = {
            "simple_regex_matcher/problem.txt": prob_hash,
            "simple_regex_matcher/test_harness.py": dummy_hash,
            "simple_regex_matcher/test_holdout.py": harn_hash
        }
        # Create a dummy test_harness.py so expected keys check passes
        with open(os.path.join(regex_dir, "test_harness.py"), "w") as f:
            f.write("dummy")

        with open(os.path.join(private_dir, "holdout_hashes.json"), "w") as f:
            json.dump(holdout_hashes, f)

        # Mock prompt evaluations to trigger promotion gate
        def fake_eval_prompt(prompt, run_dir, subdir, task_dir, tasks, tokens, ctx):
            results = {t: {"success": True, "error": None} for t in tasks}
            if "baseline" in subdir:
                # Baseline scores
                if "validation" in subdir:
                    return 0.4, results, 10
                else:
                    if tasks:
                        results[tasks[0]] = {"success": False, "error": "failed"}
                    return 0.5, results, 10
            else:
                # Mutated / candidate scores (improvement)
                if "validation" in subdir:
                    return 0.7, results, 10
                else:
                    return 0.8, results, 10

        def fake_run_eval(candidate_dir, harness_path):
            if "huffman_coding" in harness_path:
                return {"success": False, "log": "Ran 5 tests\nFAILED (failures=1)"}
            else:
                return {"success": True, "log": "Holdout passed"}

        with mock.patch("rsi_loop.evaluate_prompt_on_tasks", side_effect=fake_eval_prompt), \
             mock.patch("rsi_loop.call_codex_sandboxed", return_value=("mutated solution code", 15)), \
             mock.patch("rsi_loop.mutate_prompt", return_value=("better prompt", 25)), \
             mock.patch("rsi_loop.run_evaluation_on_candidate", side_effect=fake_run_eval), \
             mock.patch("rsi_loop._list_optimization_benchmarks", return_value=["circular_buffer"]), \
             mock.patch("rsi_loop._list_validation_benchmarks", return_value=["json_parser"]), \
             mock.patch("rsi_loop.BASE_DIR", os.path.realpath(self.tmp_dir)), \
             mock.patch("rsi_loop.PROMPTS_DIR", os.path.realpath(os.path.join(self.tmp_dir, "prompt_templates"))), \
             mock.patch("rsi_loop.PRIVATE_DIR", os.path.realpath(private_dir)), \
             mock.patch("rsi_loop.HISTORY_DIR", os.path.realpath(self.tmp_dir)), \
             mock.patch("rsi_loop.BEST_MANIFEST_PATH", os.path.realpath(os.path.join(self.tmp_dir, "best_manifest.json"))), \
             mock.patch("rsi_loop.BASELINE_PROMPT_PATH", os.path.realpath(os.path.join(self.tmp_dir, "baseline.txt"))), \
             self.assertRaises(SystemExit) as sy_exit:
            main()
        self.assertEqual(sy_exit.exception.code, 0)

        # Check run manifest and best manifest pointer
        runs = [d for d in os.listdir(self.tmp_dir) if d.startswith("run_")]
        self.assertEqual(len(runs), 1)

        run_manifest_path = os.path.join(self.tmp_dir, runs[0], "run_manifest.json")
        with open(run_manifest_path) as f:
            run_manifest = json.load(f)
        self._assert_terminal_metadata(run_manifest)
        self.assertEqual(run_manifest["status"], "completed_promoted")
        self.assertTrue(run_manifest["promotion_gate_passed"])

        best_manifest_path = os.path.join(self.tmp_dir, "best_manifest.json")
        with open(best_manifest_path) as f:
            pointer = json.load(f)
        self.assertEqual(pointer["pointer_schema_version"], "2")

        # Resolve target run_manifest path relative to the temporary base directory
        target_path = os.path.join(self.tmp_dir, pointer["run_manifest_path"])
        with open(target_path) as f:
            best_manifest = json.load(f)
        self.assertEqual(best_manifest["status"], "completed_promoted")

        # Verify prompt persistence
        meta_opt_path = os.path.join(self.tmp_dir, runs[0], "inner_agent_best.txt")
        with open(meta_opt_path) as f:
            meta_opt_prompt = f.read()
        self.assertEqual(meta_opt_prompt, "better prompt")

    # ── Test 35: Layout & symlink containment automation ─────────────────────

    def test_layout_and_symlink_containment(self):
        base = os.path.realpath(self.tmp_dir)
        sub = os.path.realpath(os.path.join(base, "history", "run_123"))
        os.makedirs(sub, exist_ok=True)
        self.assertTrue(is_contained(sub, base))

        outside = os.path.realpath(os.path.join(base, ".."))
        self.assertFalse(is_contained(outside, base))

        escaped_link = os.path.join(base, "history", "link_out")
        os.symlink(outside, escaped_link)
        self.assertFalse(is_contained(escaped_link, base))

    # ── Test 36: Codex binary path detection ──────────────────────────────────

    def test_codex_bin_path_detection(self):
        from rsi_loop import _get_codex_bin_path

        # Test fallback order
        with mock.patch("os.path.exists", side_effect=lambda p: p == "/usr/local/bin/codex"):
            self.assertEqual(_get_codex_bin_path(), "/usr/local/bin/codex")

        with mock.patch("os.path.exists", side_effect=lambda p: p == "/opt/homebrew/bin/codex"):
            self.assertEqual(_get_codex_bin_path(), "/opt/homebrew/bin/codex")

        with mock.patch("os.path.exists", return_value=False):
            self.assertEqual(_get_codex_bin_path(), "/opt/homebrew/bin/codex")

    # ── Test 37: Legacy best manifest startup archival and failure ──

    def test_legacy_best_manifest_startup_archival_and_failure(self):
        from rsi_loop import verify_existing_best_manifest_startup, load_and_verify_best_manifest, ExperimentInvalidError, _main_exec, RunContext

        test_dir = os.path.join(self.tmp_dir, "legacy_manifest_test")
        os.makedirs(test_dir)
        best_manifest_path = os.path.join(test_dir, "best_manifest.json")
        history_dir = os.path.join(test_dir, "history")
        legacy_dir = os.path.join(history_dir, "legacy")
        os.makedirs(legacy_dir)

        # 1. Test status "invalid" is rejected
        target_dir = os.path.join(history_dir, "run_123")
        os.makedirs(target_dir)
        run_manifest_path = os.path.join(target_dir, "run_manifest.json")
        with open(run_manifest_path, "w") as f:
            json.dump({"schema_version": "2", "status": "invalid"}, f)

        # Calculate expected hash of run manifest
        import hashlib
        with open(run_manifest_path, "rb") as f:
            manifest_hash = hashlib.sha256(f.read()).hexdigest()

        pointer_data = {
            "pointer_schema_version": "2",
            "run_manifest_path": "history/run_123/run_manifest.json",
            "run_manifest_sha256": manifest_hash
        }
        with open(best_manifest_path, "w") as f:
            json.dump(pointer_data, f)

        with mock.patch("rsi_loop.BEST_MANIFEST_PATH", best_manifest_path), \
             mock.patch("rsi_loop.BASE_DIR", test_dir), \
             mock.patch("rsi_loop.HISTORY_DIR", history_dir):

            # Loading an invalid manifest should raise/abort
            with self.assertRaises(ExperimentInvalidError) as ctx:
                verify_existing_best_manifest_startup()
            self.assertIn("Invalid target manifest status", str(ctx.exception))

            # The manifest pointer is recoverably archived to history/legacy/
            self.assertFalse(os.path.exists(best_manifest_path))
            files = os.listdir(legacy_dir)
            self.assertEqual(len(files), 1)
            self.assertTrue(files[0].startswith("best_manifest.json.archive_"))
            self.assertTrue(files[0].endswith(".backup"))

        # 2. Test schema_version mismatch in target manifest is rejected
        # Recreate a pointer pointing to a manifest with invalid schema_version (missing or wrong)
        with open(run_manifest_path, "w") as f:
            json.dump({"schema_version": "1", "status": "completed_promoted"}, f)

        with open(run_manifest_path, "rb") as f:
            manifest_hash = hashlib.sha256(f.read()).hexdigest()

        pointer_data["run_manifest_sha256"] = manifest_hash
        with open(best_manifest_path, "w") as f:
            json.dump(pointer_data, f)

        # Re-create empty legacy directory to reset count
        shutil.rmtree(legacy_dir)
        os.makedirs(legacy_dir)

        with mock.patch("rsi_loop.BEST_MANIFEST_PATH", best_manifest_path), \
             mock.patch("rsi_loop.BASE_DIR", test_dir), \
             mock.patch("rsi_loop.HISTORY_DIR", history_dir):

            with self.assertRaises(ExperimentInvalidError) as ctx:
                verify_existing_best_manifest_startup()
            self.assertIn("Invalid target manifest schema version", str(ctx.exception))

            # The manifest pointer is recoverably archived to history/legacy/
            self.assertFalse(os.path.exists(best_manifest_path))
            files = os.listdir(legacy_dir)
            self.assertEqual(len(files), 1)

        # 3. Test collision safety (multiple archival calls produce separate files)
        # Recreate a legacy file
        legacy_data = {"status": "success", "some_data": 123}
        with open(best_manifest_path, "w") as f:
            json.dump(legacy_data, f)

        with mock.patch("rsi_loop.BEST_MANIFEST_PATH", best_manifest_path), \
             mock.patch("rsi_loop.BASE_DIR", test_dir), \
             mock.patch("rsi_loop.HISTORY_DIR", history_dir):

            with self.assertRaises(ExperimentInvalidError) as ctx:
                verify_existing_best_manifest_startup()
            self.assertIn("was invalid/legacy", str(ctx.exception))

            # Now we should have 2 archived files under history/legacy
            self.assertFalse(os.path.exists(best_manifest_path))
            files = os.listdir(legacy_dir)
            self.assertEqual(len(files), 2)
            self.assertNotEqual(files[0], files[1])

        # 4. Test symlink rejection: if history/legacy is a symlink, archival fails closed (file NOT moved)
        # Recreate a legacy best_manifest.json
        with open(best_manifest_path, "w") as f:
            json.dump(legacy_data, f)

        # Remove the legacy directory and replace with a symlink to an external directory
        shutil.rmtree(legacy_dir)
        external_dir = os.path.join(self.tmp_dir, "external_dir")
        os.makedirs(external_dir)
        os.symlink(external_dir, legacy_dir)

        with mock.patch("rsi_loop.BEST_MANIFEST_PATH", best_manifest_path), \
             mock.patch("rsi_loop.BASE_DIR", test_dir), \
             mock.patch("rsi_loop.HISTORY_DIR", history_dir):

            # Archiving must fail because of the symlink
            with self.assertRaises(ExperimentInvalidError) as ctx:
                verify_existing_best_manifest_startup()
            self.assertIn("Symlink detected in archive path component", str(ctx.exception))

            # Crucially, best_manifest.json must NOT have been moved/archived (fail closed)
            self.assertTrue(os.path.exists(best_manifest_path))
            # External dir must be empty
            self.assertEqual(len(os.listdir(external_dir)), 0)

        # 5. Test order of lock verification: if lock check fails, no mutation/archival happens
        # Remove symlink and recreate legacy dir
        os.remove(legacy_dir)
        os.makedirs(legacy_dir)
        with open(best_manifest_path, "w") as f:
            json.dump(legacy_data, f)

        ctx = RunContext()
        ctx.allow_no_holdout = False

        # Mock verify_experiment_lock to fail
        with mock.patch("rsi_loop.BEST_MANIFEST_PATH", best_manifest_path), \
             mock.patch("rsi_loop.BASE_DIR", test_dir), \
             mock.patch("rsi_loop.HISTORY_DIR", history_dir), \
             mock.patch("rsi_loop.verify_experiment_lock", side_effect=ExperimentInvalidError("Lock check failed")):

            with self.assertRaises(ExperimentInvalidError) as ctx_err:
                _main_exec(ctx)
            # The exception raised must be from the lock verification failure, not archival
            self.assertEqual(str(ctx_err.exception), "Lock check failed")
            # And the legacy manifest file MUST NOT have been archived/moved yet!
            self.assertTrue(os.path.exists(best_manifest_path))

    # ── Test 38: Lock bound holdout mode verification and disagreement ──

    def test_lock_bound_holdout_agreement(self):
        from rsi_loop import verify_experiment_lock, RunContext, ExperimentInvalidError

        test_dir = os.path.join(self.tmp_dir, "lock_holdout_test")
        os.makedirs(test_dir)

        # Initialize mock git repository
        subprocess.run(["git", "init"], cwd=test_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=test_dir, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=test_dir, check=True)

        dummy_file = os.path.join(test_dir, "file.txt")
        with open(dummy_file, "w") as f:
            f.write("initial commit")
        subprocess.run(["git", "add", "file.txt"], cwd=test_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "commit1"], cwd=test_dir, check=True, capture_output=True)

        gitignore_path = os.path.join(test_dir, ".gitignore")
        with open(gitignore_path, "w") as f:
            f.write("experiment.lock\n")
        subprocess.run(["git", "add", ".gitignore"], cwd=test_dir, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "add gitignore"], cwd=test_dir, check=True, capture_output=True)

        res = subprocess.run(["git", "rev-parse", "HEAD"], cwd=test_dir, capture_output=True, text=True, check=True)
        commit_sha = res.stdout.strip()

        original_run = subprocess.run
        def mock_git_run(args, **kwargs):
            cmd_str = " ".join(args)
            if "remote get-url" in cmd_str:
                return mock.Mock(stdout="https://github.com/hehamalainen/RSILoop.git\n", stderr="", returncode=0)
            elif "ls-remote" in cmd_str:
                return mock.Mock(stdout=commit_sha + "\trefs/heads/main\n", stderr="", returncode=0)
            return original_run(args, **kwargs)

        lock_path = os.path.join(test_dir, "experiment.lock")

        # Case 1: Environment ALLOW_NO_HOLDOUT is 1, but lock file allow_no_holdout is False (or missing) -> Disagreement -> Fails!
        lock_data = {
            "approved_commit_sha": commit_sha,
            "remote_url": "https://github.com/hehamalainen/RSILoop.git",
            "model": "gpt-5.6-sol"
        }
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)

        ctx = RunContext()
        ctx.operator_report = {}
        init_operator_report(ctx)
        ctx.allow_no_holdout = True  # Simulated ALLOW_NO_HOLDOUT=1
        with mock.patch("rsi_loop.BASE_DIR", test_dir), \
             mock.patch("rsi_loop.subprocess.run", side_effect=mock_git_run), \
             mock.patch.dict(os.environ, {"ALLOW_NO_HOLDOUT": "1"}):
            with self.assertRaises(ExperimentInvalidError) as r_ctx:
                verify_experiment_lock(ctx)
            self.assertIn("disagrees with experiment.lock", str(r_ctx.exception))

        # Case 2: Environment ALLOW_NO_HOLDOUT is 0/empty, but lock file allow_no_holdout is True -> Disagreement -> Fails!
        lock_data = {
            "approved_commit_sha": commit_sha,
            "remote_url": "https://github.com/hehamalainen/RSILoop.git",
            "allow_no_holdout": True,
            "model": "gpt-5.6-sol",
        }
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)

        ctx = RunContext()
        ctx.operator_report = {}
        init_operator_report(ctx)
        ctx.allow_no_holdout = False  # Simulated ALLOW_NO_HOLDOUT=0
        with mock.patch("rsi_loop.BASE_DIR", test_dir), \
             mock.patch("rsi_loop.subprocess.run", side_effect=mock_git_run), \
             mock.patch.dict(os.environ, {"ALLOW_NO_HOLDOUT": "0"}):
            with self.assertRaises(ExperimentInvalidError) as r_ctx:
                verify_experiment_lock(ctx)
            self.assertIn("disagrees with experiment.lock", str(r_ctx.exception))

        # Case 3: Both agree to allow no holdout (env="1", lock=True) -> Success!
        ctx = RunContext()
        ctx.operator_report = {}
        init_operator_report(ctx)
        ctx.allow_no_holdout = True
        with mock.patch("rsi_loop.BASE_DIR", test_dir), \
             mock.patch("rsi_loop.subprocess.run", side_effect=mock_git_run), \
             mock.patch.dict(os.environ, {"ALLOW_NO_HOLDOUT": "1"}):
            sha = verify_experiment_lock(ctx)
            self.assertEqual(sha, commit_sha)
            self.assertTrue(ctx.allow_no_holdout)

        # Case 4: Both agree to require holdout (env omitted, lock=False) -> Success!
        lock_data = {
            "approved_commit_sha": commit_sha,
            "remote_url": "https://github.com/hehamalainen/RSILoop.git",
            "allow_no_holdout": False,
            "model": "gpt-5.6-sol",
        }
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)

        ctx = RunContext()
        ctx.operator_report = {}
        init_operator_report(ctx)
        ctx.allow_no_holdout = False
        with mock.patch("rsi_loop.BASE_DIR", test_dir), \
             mock.patch("rsi_loop.subprocess.run", side_effect=mock_git_run), \
             mock.patch.dict(os.environ, {}):
            sha = verify_experiment_lock(ctx)
            self.assertEqual(sha, commit_sha)
            self.assertFalse(ctx.allow_no_holdout)

    # ── Test 39: Holdout validation modes (missing holdout checks) ──

    def test_holdout_validation_modes(self):
        from rsi_loop import _main_exec, RunContext, ExperimentInvalidError

        test_dir = os.path.join(self.tmp_dir, "holdout_validation_test")
        os.makedirs(test_dir)

        # Mock preflight hashes to be empty for simplicity
        preflight_json = os.path.join(test_dir, "preflight_hashes.json")
        with open(preflight_json, "w") as f:
            json.dump({}, f)

        # Case 1: allow_no_holdout is False, private holdout directory missing -> Invalid run!
        ctx = RunContext()
        ctx.allow_no_holdout = False

        with mock.patch("rsi_loop.BASE_DIR", test_dir), \
             mock.patch("rsi_loop.verify_experiment_lock", return_value="mock_sha"), \
             mock.patch("rsi_loop.BEST_MANIFEST_PATH", os.path.join(test_dir, "best_manifest.json")), \
             mock.patch("rsi_loop.PREFLIGHT_HASHES_PATH", preflight_json), \
             mock.patch("rsi_loop.EXPECTED_PREFLIGHT_KEYS", set()), \
             mock.patch("rsi_loop.PRIVATE_DIR", os.path.join(test_dir, "rsi_loop_private")), \
             mock.patch("rsi_loop.BASELINE_PROMPT_PATH", os.path.join(test_dir, "baseline.txt")):

            with open(os.path.join(test_dir, "baseline.txt"), "w") as f:
                f.write("baseline")

            with self.assertRaises(ExperimentInvalidError) as r_ctx:
                _main_exec(ctx)
            self.assertIn("Private holdout directory is missing", str(r_ctx.exception))

        # Case 2: allow_no_holdout is True, private holdout directory missing -> Proceeds past preflight (stops at paid call/smoke check)
        ctx = RunContext()
        ctx.allow_no_holdout = True

        with mock.patch("rsi_loop.BASE_DIR", test_dir), \
             mock.patch("rsi_loop.verify_experiment_lock", return_value="mock_sha"), \
             mock.patch("rsi_loop.BEST_MANIFEST_PATH", os.path.join(test_dir, "best_manifest.json")), \
             mock.patch("rsi_loop.PREFLIGHT_HASHES_PATH", preflight_json), \
             mock.patch("rsi_loop.EXPECTED_PREFLIGHT_KEYS", set()), \
             mock.patch("rsi_loop.PRIVATE_DIR", os.path.join(test_dir, "rsi_loop_private")), \
             mock.patch("rsi_loop.BASELINE_PROMPT_PATH", os.path.join(test_dir, "baseline.txt")), \
             mock.patch("rsi_loop.run_smoke_and_canary", side_effect=RuntimeError("Stopped before paid execution")):

            with open(os.path.join(test_dir, "baseline.txt"), "w") as f:
                f.write("baseline")

            with self.assertRaises(RuntimeError) as r_ctx:
                _main_exec(ctx)
            # Confirms we passed holdout checks and hit the mock smoke check!
            self.assertEqual(str(r_ctx.exception), "Stopped before paid execution")

    # ── Approved defect-fix regressions ──────────────────────────────────────

    def test_lock_model_validation(self):
        cases = [
            ({}, False),
            ({"model": None}, False),
            ({"model": 7}, False),
            ({"model": ""}, False),
            ({"model": "   "}, False),
            ({"model": " gpt-5.6-sol "}, False),
            ({"model": "gpt 5.6"}, False),
            ({"model": "../gpt"}, False),
            ({"model": "resolve-at-canary"}, False),
            ({"model": "gpt-5.6-sol"}, True),
        ]
        approved_sha = "a" * 40

        def fake_git_run(command, **kwargs):
            cmd_str = " ".join(command)
            if "remote get-url" in cmd_str:
                return mock.Mock(stdout="https://github.com/hehamalainen/RSILoop.git\n", stderr="", returncode=0)
            elif "ls-remote" in cmd_str:
                return mock.Mock(stdout=approved_sha + "\trefs/heads/main\n", stderr="", returncode=0)
            elif command[:2] == ["git", "status"]:
                return mock.Mock(stdout="", stderr="", returncode=0)
            return mock.Mock(stdout=approved_sha + "\n", stderr="", returncode=0)

        for index, (model_data, valid) in enumerate(cases):
            with self.subTest(model=model_data.get("model", "missing")):
                case_dir = os.path.join(self.tmp_dir, f"model_case_{index}")
                os.mkdir(case_dir)
                lock_data = {
                    "approved_commit_sha": approved_sha,
                    "remote_url": "https://github.com/hehamalainen/RSILoop.git",
                    "allow_no_holdout": False,
                    **model_data,
                }
                with open(os.path.join(case_dir, "experiment.lock"), "w") as handle:
                    json.dump(lock_data, handle)
                ctx = RunContext()
                ctx.operator_report = {}
                init_operator_report(ctx)
                with mock.patch("rsi_loop.BASE_DIR", case_dir), \
                     mock.patch("rsi_loop.subprocess.run", side_effect=fake_git_run), \
                     mock.patch.dict(os.environ, {"ALLOW_NO_HOLDOUT": "0"}):
                    if valid:
                        self.assertEqual(verify_experiment_lock(ctx), approved_sha)
                        self.assertEqual(ctx.codex_model, model_data["model"])
                        self.assertEqual(ctx.lock_model, model_data["model"])
                    else:
                        with self.assertRaises(ExperimentInvalidError):
                            verify_experiment_lock(ctx)

    def test_codex_commands_propagate_model_flag(self):
        from rsi_loop import _build_codex_command, call_codex_sandboxed

        ctx = RunContext()
        ctx.run_id = "model_flag_test"
        ctx.codex_model = "gpt-5.6-sol"
        ctx.lock_model = "gpt-5.6-sol"
        canary_command = _build_codex_command("/exact/generator.sb", ctx.codex_model, "/candidate/dir")
        self.assertEqual(canary_command.count("--model"), 1)
        idx = canary_command.index("--model")
        self.assertEqual(canary_command[idx + 1], "gpt-5.6-sol")

        fake_stdout = "\n".join([
            '{"type":"item.completed","item":{"type":"agent_message","text":"code"}}',
            '{"type":"turn.completed","usage":{"output_tokens":1}}',
        ])
        captured_commands = []

        def fake_run(command, **kwargs):
            captured_commands.append(command)
            return mock.Mock(returncode=0, stdout=fake_stdout, stderr="")

        with mock.patch(
            "rsi_loop._build_codex_run_scoped_home",
            return_value=tempfile.mkdtemp(),
        ), mock.patch(
            "rsi_loop._build_generator_sb",
            return_value="/exact/generator.sb",
        ), mock.patch(
            "rsi_loop._cleanup_codex_run_scoped_home",
        ), mock.patch(
            "rsi_loop.subprocess.run",
            side_effect=fake_run,
        ):
            call_codex_sandboxed("prompt", "system", self.tmp_dir, ctx)

        self.assertEqual(len(captured_commands), 1)
        command = captured_commands[0]
        self.assertEqual(command.count("--model"), 1)
        idx2 = command.index("--model")
        self.assertEqual(command[idx2 + 1], "gpt-5.6-sol")

    def test_subprocess_stdout_stderr_truncation(self):
        from rsi_loop import _run_codex_canary

        for stream in (None, "plain", b"plain", b"bad-utf8-\xff"):
            with self.subTest(stream=stream):
                value = normalize_and_truncate_stream(stream, max_bytes=32)
                self.assertIsInstance(value, str)
                self.assertLessEqual(len(value.encode("utf-8")), 32)

        truncated = normalize_and_truncate_stream("é" * 100000)
        self.assertTrue(truncated.endswith("\n[TRUNCATED]"))
        self.assertLessEqual(len(truncated.encode("utf-8")), 65536)

        scenarios = [
            subprocess.TimeoutExpired(
                ["codex"], 1, output=b"o" * 70000, stderr=b"e" * 70000
            ),
            subprocess.CalledProcessError(
                1, ["codex"], output=b"out", stderr=b"err"
            ),
            OSError("launch failed"),
        ]
        for error in scenarios:
            with self.subTest(error=type(error).__name__):
                ctx = RunContext()
                ctx.codex_model = "gpt-5.6-sol"
                with mock.patch("rsi_loop.subprocess.run", side_effect=error):
                    with self.assertRaises(ExperimentInvalidError) as raised:
                        _run_codex_canary("/exact/generator.sb", {}, ctx, self.tmp_dir)
                self.assertTrue(ctx.unknown_token_state)
                self.assertLessEqual(
                    len(raised.exception.stdout.encode("utf-8")), 65536
                )
                self.assertLessEqual(
                    len(raised.exception.stderr.encode("utf-8")), 65536
                )

        ctx = RunContext()
        ctx.codex_model = "gpt-5.6-sol"
        result = mock.Mock(returncode=2, stdout=b"partial", stderr=b"fatal")
        with mock.patch("rsi_loop.subprocess.run", return_value=result):
            with self.assertRaises(ExperimentInvalidError) as raised:
                _run_codex_canary("/exact/generator.sb", {}, ctx, self.tmp_dir)
        self.assertEqual(raised.exception.stdout, "partial")
        self.assertEqual(raised.exception.stderr, "fatal")

        long_stderr_cases = [
            (
                "WARNING " + ("w" * 70000),
                "WARNING " + ("w" * 70000),
            ),
            (
                "WARNING " + ("w" * 70000) + "\nfatal final line",
                "fatal final line",
            ),
        ]
        for raw_stderr, expected_summary in long_stderr_cases:
            with self.subTest(expected_summary=expected_summary[:30]):
                ctx = RunContext()
                ctx.codex_model = "gpt-5.6-sol"
                result = mock.Mock(returncode=2, stdout="", stderr=raw_stderr)
                with mock.patch("rsi_loop.subprocess.run", return_value=result):
                    with self.assertRaises(ExperimentInvalidError) as raised:
                        _run_codex_canary("/exact/generator.sb", {}, ctx, self.tmp_dir)
                self.assertTrue(raised.exception.stderr.endswith("\n[TRUNCATED]"))
                self.assertLessEqual(
                    len(raised.exception.stderr.encode("utf-8")), 65536
                )
                self.assertEqual(
                    raised.exception.stderr_summary,
                    expected_summary,
                )

    def test_stderr_summary_recovery(self):
        self.assertEqual(get_stderr_summary(None), "stderr was empty")
        self.assertEqual(get_stderr_summary("\n\n"), "stderr was empty")
        self.assertEqual(
            get_stderr_summary("  WARNING first\nWARNING second\n"),
            "WARNING second",
        )
        self.assertEqual(
            get_stderr_summary("fatal one\nWARNING later\nfatal final\n"),
            "fatal final",
        )
        self.assertEqual(
            get_stderr_summary("fatal one\nWARNING later\n"),
            "fatal one",
        )

    def test_credential_isolation_outside_readable_roots(self):
        from rsi_loop import (
            _build_codex_run_scoped_home,
            _cleanup_codex_run_scoped_home,
        )

        base_dir = os.path.join(self.tmp_dir, "repo")
        os.mkdir(base_dir)
        auth_source = os.path.join(self.tmp_dir, "source_auth.json")
        auth_fd = os.open(auth_source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(auth_fd, b'{"auth_mode":"apikey","OPENAI_API_KEY":"dummy"}')
        os.close(auth_fd)

        ctx = RunContext()
        with mock.patch("rsi_loop.BASE_DIR", base_dir):
            home = _build_codex_run_scoped_home(ctx, "credential_test", auth_source)
            self.assertEqual(ctx.created_codex_home, home)
            self.assertFalse(is_contained(home, base_dir))
            self.assertFalse(
                is_contained(home, os.path.join(self.tmp_dir, "rsi_loop_private"))
            )
            self.assertEqual(stat.S_IMODE(os.lstat(home).st_mode), 0o700)
            self.assertEqual(
                stat.S_IMODE(os.lstat(os.path.join(home, ".codex")).st_mode),
                0o700,
            )
            self.assertEqual(
                stat.S_IMODE(os.lstat(os.path.join(home, "tmp")).st_mode),
                0o700,
            )
            destination = os.path.join(home, ".codex", "auth.json")
            self.assertFalse(os.path.islink(destination))
            self.assertEqual(stat.S_IMODE(os.lstat(destination).st_mode), 0o600)
            _cleanup_codex_run_scoped_home(ctx, home)
            self.assertIsNone(ctx.created_codex_home)
            self.assertFalse(os.path.lexists(home))

            with self.assertRaises(ExperimentInvalidError):
                _build_codex_run_scoped_home(ctx, "../escape", auth_source)

            preexisting = os.path.join(self.tmp_dir, "codex_home_preexisting")
            os.mkdir(preexisting, 0o700)
            with self.assertRaises(ExperimentInvalidError):
                _build_codex_run_scoped_home(ctx, "preexisting", auth_source)
            self.assertTrue(os.path.isdir(preexisting))

            source_link = os.path.join(self.tmp_dir, "source_link.json")
            os.symlink(auth_source, source_link)
            with self.assertRaises(ExperimentInvalidError):
                _build_codex_run_scoped_home(ctx, "source_symlink", source_link)
            self.assertFalse(
                os.path.lexists(os.path.join(self.tmp_dir, "codex_home_source_symlink"))
            )
            self.assertIsNone(ctx.created_codex_home)

            from rsi_loop import _assert_components_without_symlinks
            call_count = 0

            def fail_second_component_check(path, parent):
                nonlocal call_count
                call_count += 1
                if call_count == 2:
                    raise ExperimentInvalidError("injected post-check failure")
                return _assert_components_without_symlinks(path, parent)

            with mock.patch(
                "rsi_loop._assert_components_without_symlinks",
                side_effect=fail_second_component_check,
            ):
                with self.assertRaises(ExperimentInvalidError):
                    _build_codex_run_scoped_home(ctx, "postcheck_failure", auth_source)
            self.assertFalse(
                os.path.lexists(os.path.join(self.tmp_dir, "codex_home_postcheck_failure"))
            )
            self.assertIsNone(ctx.created_codex_home)

    def test_bootstrap_warning_regression(self):
        from rsi_loop import _codex_environment, _run_codex_canary

        home = os.path.join(self.tmp_dir, "codex_home_bootstrap")
        env = _codex_environment(home)
        self.assertEqual(env["HOME"], home)
        self.assertEqual(env["TMPDIR"], os.path.join(home, "tmp"))
        self.assertNotEqual(env["HOME"], env["TMPDIR"])

        warning = (
            "WARNING: proceeding, even though we could not create PATH aliases: "
            "Refusing to create helper binaries under temporary dir"
        )
        ctx = RunContext()
        ctx.codex_model = "gpt-5.6-sol"
        ctx.lock_model = "gpt-5.6-sol"
        with mock.patch(
            "rsi_loop.subprocess.run",
            return_value=mock.Mock(returncode=1, stdout="", stderr=warning),
        ):
            with self.assertRaises(ExperimentInvalidError) as raised:
                _run_codex_canary("/exact/generator.sb", env, ctx, self.tmp_dir)
        self.assertIn("Refusing to create helper binaries", raised.exception.stderr)

    def test_host_boundary_cli_bootstrap(self):
        import rsi_loop
        from rsi_loop import (
            _build_codex_command,
            _build_codex_run_scoped_home,
            _build_generator_sb,
            _cleanup_codex_run_scoped_home,
            _codex_environment,
        )

        self.assertEqual(sys.platform, "darwin")
        self.assertTrue(os.path.exists("/usr/bin/sandbox-exec"))
        dummy_source = os.path.join(self.tmp_dir, "dummy_auth.json")
        dummy_fd = os.open(
            dummy_source,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        os.write(
            dummy_fd,
            json.dumps({
                "auth_mode": "apikey",
                "OPENAI_API_KEY": "sk-dummy-invalid-for-boundary-test",
            }).encode("utf-8"),
        )
        os.fsync(dummy_fd)
        os.close(dummy_fd)

        ctx = RunContext()
        ctx.run_id = f"host_boundary_{os.getpid()}_{id(self)}"
        ctx.codex_model = "gpt-5.6-sol"
        ctx.lock_model = "gpt-5.6-sol"
        home = _build_codex_run_scoped_home(ctx, ctx.run_id, dummy_source)
        try:
            sb_path = _build_generator_sb(home, rsi_loop.BASE_DIR)
            self.assertEqual(sb_path, os.path.join(home, "generator.sb"))
            command = _build_codex_command(sb_path, ctx.codex_model, rsi_loop.BASE_DIR)
            self.assertEqual(command.count("--model"), 1)
            idx = command.index("--model")
            self.assertEqual(command[idx + 1], "gpt-5.6-sol")
            result = subprocess.run(
                command,
                input="Reply with exactly one word: ready",
                capture_output=True,
                text=True,
                timeout=60,
                env=_codex_environment(home),
                cwd=rsi_loop.BASE_DIR,
            )
            self.assertIn(result.returncode, (1, 71))
            forbidden = [
                "Refusing to create helper binaries",
                "sandbox_apply",
                "Operation not permitted",
                "Permission denied",
                "No such file or directory",
                "configuration error",
                "failed to spawn",
            ]
            combined = result.stdout + "\n" + result.stderr
            for text_value in forbidden:
                self.assertNotIn(text_value, combined)
            events = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
            self.assertTrue(events)
            terminal = events[-1]
            self.assertEqual(terminal.get("type"), "turn.failed")
            self.assertIsInstance(terminal.get("error"), dict)
            message = terminal["error"].get("message")
            self.assertIsInstance(message, str)
            for exact_classification in (
                "unexpected status 401 Unauthorized",
                "auth error: 401",
                "auth error code: invalid_api_key",
            ):
                self.assertIn(exact_classification, message)
            sys.stdout.write(
                "[host-boundary-evidence] profile=generator.sb rc=1 "
                "terminal=turn.failed auth_error_code=invalid_api_key\n"
            )
        finally:
            _cleanup_codex_run_scoped_home(ctx, home)
        self.assertIsNone(ctx.created_codex_home)
        self.assertFalse(os.path.lexists(home))

    def test_cleanup_lifecycles(self):
        import rsi_loop
        from rsi_loop import call_codex_sandboxed

        base_dir = os.path.join(self.tmp_dir, "cleanup_repo")
        os.mkdir(base_dir)
        shutil.copy(
            os.path.join(BASE_DIR, "generator.sb.template"),
            os.path.join(base_dir, "generator.sb.template"),
        )
        auth_source = os.path.join(self.tmp_dir, "cleanup_auth.json")
        auth_fd = os.open(auth_source, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(auth_fd, b'{"auth_mode":"apikey","OPENAI_API_KEY":"dummy"}')
        os.close(auth_fd)
        success_stdout = "\n".join([
            '{"type":"item.completed","item":{"type":"agent_message","text":"code"}}',
            '{"type":"turn.completed","model":"gpt-5.6-sol","usage":{"output_tokens":1}}',
        ])
        scenarios = [
            mock.Mock(returncode=0, stdout=success_stdout, stderr=""),
            mock.Mock(returncode=1, stdout="partial", stderr="failed"),
            subprocess.TimeoutExpired(["codex"], 1, output="partial", stderr="timed out"),
        ]

        real_expanduser = os.path.expanduser
        for index, outcome in enumerate(scenarios):
            with self.subTest(index=index):
                ctx = RunContext()
                ctx.run_id = f"cleanup_{index}"
                ctx.codex_model = "gpt-5.6-sol"
                ctx.lock_model = "gpt-5.6-sol"
                ctx.model_resolved = "unavailable"

                def expanduser(path):
                    if path == "~/.codex/auth.json":
                        return auth_source
                    return real_expanduser(path)

                run_patch = (
                    mock.patch("rsi_loop.subprocess.run", side_effect=outcome)
                    if isinstance(outcome, BaseException)
                    else mock.patch("rsi_loop.subprocess.run", return_value=outcome)
                )
                with mock.patch("rsi_loop.BASE_DIR", base_dir), \
                     mock.patch("rsi_loop.os.path.expanduser", side_effect=expanduser), \
                     run_patch:
                    if index == 0:
                        self.assertEqual(
                            call_codex_sandboxed("prompt", "system", base_dir, ctx),
                            ("code", 1),
                        )
                    else:
                        with self.assertRaises(ExperimentInvalidError):
                            call_codex_sandboxed("prompt", "system", base_dir, ctx)
                expected_home = os.path.join(self.tmp_dir, f"codex_home_cleanup_{index}")
                self.assertIsNone(ctx.created_codex_home)
                self.assertFalse(os.path.lexists(expected_home))

    def test_canary_model_resolution_and_manifest_persistence(self):
        from rsi_loop import (
            _model_manifest_fields,
            _run_codex_canary,
        )

        # Before lock validation: all model manifest fields must be "unavailable"
        pre_ctx = RunContext()
        self.assertEqual(
            _model_manifest_fields(pre_ctx),
            {
                "model_requested": "unavailable",
                "model_resolved": "unavailable",
                "model_name": "unavailable",
            },
        )

        canary_stdout = "\n".join([
            '{"type":"thread.started","thread_id":"test"}',
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"ready"}}',
            '{"type":"turn.completed","usage":{"output_tokens":1}}',
        ])
        ctx = RunContext()
        ctx.codex_model = "gpt-5.6-sol"
        ctx.lock_model = "gpt-5.6-sol"
        with mock.patch(
            "rsi_loop.subprocess.run",
            return_value=mock.Mock(returncode=0, stdout=canary_stdout, stderr=""),
        ):
            self.assertEqual(_run_codex_canary("/exact/generator.sb", {}, ctx, self.tmp_dir), 1)
        self.assertEqual(ctx.model_resolved, "unavailable")
        self.assertEqual(
            _model_manifest_fields(ctx),
            {
                "model_requested": "gpt-5.6-sol",
                "model_resolved": "unavailable",
                "model_name": "gpt-5.6-sol",
            },
        )

    def test_model_drift_not_enforced_and_resolved_unavailable(self):
        from rsi_loop import call_codex_sandboxed, _model_manifest_fields

        ctx = RunContext()
        ctx.run_id = "drift_test"
        ctx.codex_model = "gpt-5.6-sol"
        ctx.lock_model = "gpt-5.6-sol"
        ctx.model_resolved = "unavailable"
        drift_stdout = "\n".join([
            '{"type":"item.completed","item":{"type":"agent_message","text":"code"}}',
            '{"type":"turn.completed","usage":{"output_tokens":1}}',
        ])
        captured_commands = []

        def fake_run(command, **kwargs):
            captured_commands.append(command)
            return mock.Mock(returncode=0, stdout=drift_stdout, stderr="")

        with mock.patch(
            "rsi_loop._build_codex_run_scoped_home",
            return_value=tempfile.mkdtemp(),
        ), mock.patch(
            "rsi_loop._build_generator_sb",
            return_value="/exact/generator.sb",
        ), mock.patch(
            "rsi_loop._cleanup_codex_run_scoped_home",
        ), mock.patch(
            "rsi_loop.subprocess.run",
            side_effect=fake_run,
        ):
            code, tokens = call_codex_sandboxed("prompt", "system", self.tmp_dir, ctx)
        self.assertEqual(code, "code")
        self.assertEqual(tokens, 1)
        self.assertEqual(ctx.model_resolved, "unavailable")
        self.assertEqual(
            _model_manifest_fields(ctx),
            {
                "model_requested": "gpt-5.6-sol",
                "model_resolved": "unavailable",
                "model_name": "gpt-5.6-sol",
            },
        )

    def test_call_codex_sandboxed_cwd(self):
        import rsi_loop
        from rsi_loop import call_codex_sandboxed
        ctx = RunContext()
        ctx.codex_model = "gpt-5.6-sol"
        ctx.lock_model = "gpt-5.6-sol"
        ctx.model_resolved = "unavailable"
        drift_stdout = "\n".join([
            '{"type":"item.completed","item":{"type":"agent_message","text":"code"}}',
            '{"type":"turn.completed","usage":{"output_tokens":1}}',
        ])
        captured_kwargs = []

        def fake_run(command, **kwargs):
            captured_kwargs.append(kwargs)
            return mock.Mock(returncode=0, stdout=drift_stdout, stderr="")

        candidate_dir = os.path.join(self.tmp_dir, "my_candidate_dir")
        os.makedirs(candidate_dir, exist_ok=True)
        real_candidate_dir = os.path.realpath(candidate_dir)

        with mock.patch(
            "rsi_loop._build_codex_run_scoped_home",
            return_value=tempfile.mkdtemp(),
        ), mock.patch(
            "rsi_loop._build_generator_sb",
            return_value="/exact/generator.sb",
        ), mock.patch(
            "rsi_loop._cleanup_codex_run_scoped_home",
        ), mock.patch(
            "rsi_loop.subprocess.run",
            side_effect=fake_run,
        ):
            call_codex_sandboxed("prompt", "system", candidate_dir, ctx)

        self.assertEqual(len(captured_kwargs), 1)
        self.assertEqual(captured_kwargs[0].get("cwd"), real_candidate_dir)

    def test_smoke_version_gate(self):
        from rsi_loop import _run_codex_version_smoke_test

        for stdout in ("", "   \n", "not-a-version", "codex-cli 0.144"):
            with self.subTest(stdout=stdout):
                with mock.patch(
                    "rsi_loop.subprocess.run",
                    return_value=mock.Mock(returncode=0, stdout=stdout, stderr=""),
                ):
                    with self.assertRaises(ExperimentInvalidError):
                        _run_codex_version_smoke_test("/exact/generator.sb", {}, self.tmp_dir)

        with mock.patch(
            "rsi_loop.subprocess.run",
            return_value=mock.Mock(
                returncode=0,
                stdout="codex-cli 0.144.5\n",
                stderr="",
            ),
        ):
            self.assertEqual(
                _run_codex_version_smoke_test("/exact/generator.sb", {}, self.tmp_dir),
                "codex-cli 0.144.5",
            )

    def test_cleanup_failure_invalid_manifest_diagnostic(self):
        import rsi_loop
        from rsi_loop import call_codex_sandboxed

        unresolved_home = os.path.join(self.tmp_dir, "codex_home_unresolved")
        captured_errors = []

        def fake_home_builder(ctx, run_id):
            ctx.created_codex_home = unresolved_home
            return unresolved_home

        def execute_with_cleanup_failure(ctx):
            try:
                call_codex_sandboxed("prompt", "system", self.tmp_dir, ctx)
            except ExperimentInvalidError as error:
                captured_errors.append(error)
                raise

        def capture_environment(ctx):
            ctx.sw_vers = "ProductName: macOS"
            ctx.python_version = "3.test"
            ctx.codex_cli_version = "codex-cli 0.144.5"

        with mock.patch("rsi_loop.HISTORY_DIR", self.tmp_dir), \
             mock.patch("rsi_loop._capture_environment_metadata", side_effect=capture_environment), \
             mock.patch("rsi_loop._main_exec", side_effect=execute_with_cleanup_failure), \
             mock.patch("rsi_loop._build_codex_run_scoped_home", side_effect=fake_home_builder), \
             mock.patch(
                 "rsi_loop._build_generator_sb",
                 side_effect=ExperimentInvalidError(
                     "primary generator failure",
                     stderr="WARNING primary diagnostic",
                 ),
             ), \
             mock.patch(
                 "rsi_loop._remove_owned_codex_home",
                 side_effect=OSError("injected cleanup failure"),
             ), \
             self.assertRaises(SystemExit) as exit_ctx:
            main()
        self.assertEqual(exit_ctx.exception.code, 1)
        self.assertEqual(len(captured_errors), 1)
        combined_error = captured_errors[0]
        self.assertIn("primary generator failure", str(combined_error))
        self.assertIn("injected cleanup failure", str(combined_error))
        self.assertIsInstance(combined_error.__cause__, ExperimentInvalidError)

        run_dirs = [
            name for name in os.listdir(self.tmp_dir) if name.startswith("run_")
        ]
        self.assertEqual(len(run_dirs), 1)
        manifest_path = os.path.join(
            self.tmp_dir,
            run_dirs[0],
            "invalid_manifest.json",
        )
        with open(manifest_path) as handle:
            manifest = json.load(handle)
        self.assertEqual(manifest["unresolved_codex_home"], unresolved_home)
        self.assertIn("injected cleanup failure", manifest["cleanup_error"])
        self.assertEqual(
            manifest["stderr_summary"],
            "WARNING primary diagnostic",
        )
        self.assertEqual(
            manifest["error_stderr"],
            "WARNING primary diagnostic",
        )

    def test_cleanup_failure_without_primary_raises(self):
        from rsi_loop import _cleanup_codex_run_scoped_home

        unresolved_home = os.path.join(self.tmp_dir, "codex_home_direct_failure")
        ctx = RunContext()
        ctx.created_codex_home = unresolved_home
        with mock.patch(
            "rsi_loop._remove_owned_codex_home",
            side_effect=OSError("direct cleanup failure"),
        ):
            with self.assertRaises(ExperimentInvalidError) as raised:
                _cleanup_codex_run_scoped_home(ctx, unresolved_home)
        self.assertEqual(raised.exception.unresolved_codex_home, unresolved_home)
        self.assertIn("direct cleanup failure", raised.exception.cleanup_error)
        self.assertEqual(ctx.created_codex_home, unresolved_home)
        self.assertEqual(ctx.unresolved_codex_home, unresolved_home)

    def test_environment_metadata_terminal_manifests(self):
        from rsi_loop import _terminal_environment_metadata

        ctx = RunContext()
        ctx.sw_vers = "ProductName: macOS"
        ctx.python_version = "3.test"
        ctx.codex_cli_version = "codex-cli test"
        metadata = _terminal_environment_metadata(ctx)
        self.assertEqual(
            set(metadata),
            {"sw_vers", "python_version", "codex_cli_version"},
        )
        for value in metadata.values():
            self.assertIsInstance(value, str)
            self.assertTrue(value)

        ctx.codex_cli_version = "unavailable"
        ctx.codex_version = None
        self.assertEqual(
            _terminal_environment_metadata(ctx)["codex_cli_version"],
            "unavailable",
        )

    def test_canary_dir_preexistence_rejection(self):
        ctx = RunContext()
        ctx.run_dir = os.path.join(self.tmp_dir, "run_dir")
        os.makedirs(ctx.run_dir, exist_ok=True)
        os.makedirs(os.path.join(ctx.run_dir, "canary_candidate"), exist_ok=True)
        with mock.patch("rsi_loop._build_codex_run_scoped_home", return_value=os.path.join(self.tmp_dir, "temp_home")), \
             mock.patch("rsi_loop.HISTORY_DIR", self.tmp_dir):
            with self.assertRaises(ExperimentInvalidError) as e:
                run_smoke_and_canary(ctx)
        self.assertIn("Canary candidate directory already exists", str(e.exception))

    def test_canary_dir_symlink_rejection(self):
        from rsi_loop import _validate_canary_dir
        expected_parent = os.path.join(self.tmp_dir, "expected_parent")
        os.makedirs(expected_parent, exist_ok=True)
        target_dir = os.path.join(self.tmp_dir, "target_dir")
        os.makedirs(target_dir, exist_ok=True)
        symlink_path = os.path.join(expected_parent, "canary_candidate")
        os.symlink(target_dir, symlink_path)
        with mock.patch("rsi_loop.HISTORY_DIR", self.tmp_dir):
            with self.assertRaises(ExperimentInvalidError) as e:
                _validate_canary_dir(symlink_path, expected_parent, os.path.join(self.tmp_dir, "temp_home"))
        self.assertIn("is a symlink", str(e.exception))

    def test_canary_dir_containment_rejection(self):
        from rsi_loop import _validate_canary_dir
        expected_parent = os.path.join(self.tmp_dir, "expected_parent")
        os.makedirs(expected_parent, exist_ok=True)
        canary_dir = os.path.join(self.tmp_dir, "canary_candidate")
        os.makedirs(canary_dir, exist_ok=True)
        os.chmod(canary_dir, 0o700)
        with mock.patch("rsi_loop.HISTORY_DIR", self.tmp_dir):
            with self.assertRaises(ExperimentInvalidError) as e:
                _validate_canary_dir(canary_dir, expected_parent, os.path.join(self.tmp_dir, "temp_home"))
        self.assertTrue("containment" in str(e.exception).lower() or "parent" in str(e.exception).lower())

    def test_canary_dir_mode_rejection(self):
        from rsi_loop import _validate_canary_dir
        expected_parent = os.path.join(self.tmp_dir, "expected_parent")
        os.makedirs(expected_parent, exist_ok=True)
        canary_dir = os.path.join(expected_parent, "canary_candidate")
        os.mkdir(canary_dir, 0o777)
        with mock.patch("rsi_loop.HISTORY_DIR", self.tmp_dir):
            with self.assertRaises(ExperimentInvalidError) as e:
                _validate_canary_dir(canary_dir, expected_parent, os.path.join(self.tmp_dir, "temp_home"))
        self.assertIn("is not 0o700", str(e.exception))

    def test_canary_dir_owner_rejection(self):
        from rsi_loop import _validate_canary_dir
        expected_parent = os.path.join(self.tmp_dir, "expected_parent")
        os.makedirs(expected_parent, exist_ok=True)
        canary_dir = os.path.join(expected_parent, "canary_candidate")
        os.mkdir(canary_dir, 0o700)
        original_lstat = os.lstat
        def fake_lstat(path):
            res = original_lstat(path)
            if os.path.abspath(path) == os.path.abspath(canary_dir):
                class FakeStat:
                    def __init__(self, orig):
                        self.st_uid = orig.st_uid + 1
                        self.st_mode = orig.st_mode
                return FakeStat(res)
            return res
        with mock.patch("os.lstat", side_effect=fake_lstat), \
             mock.patch("rsi_loop.HISTORY_DIR", self.tmp_dir):
            with self.assertRaises(ExperimentInvalidError) as e:
                _validate_canary_dir(canary_dir, expected_parent, os.path.join(self.tmp_dir, "temp_home"))
            self.assertIn("does not match current UID", str(e.exception))

    def test_canary_dir_successful_cleanup(self):
        ctx = RunContext()
        ctx.run_dir = os.path.realpath(os.path.join(self.tmp_dir, "run_dir"))
        os.makedirs(ctx.run_dir, exist_ok=True)
        ctx.codex_model = "gpt-5.6-sol"
        ctx.lock_model = "gpt-5.6-sol"
        ctx.operator_report = {}
        init_operator_report(ctx)
        os.makedirs(os.path.join(self.tmp_dir, "temp_home"), exist_ok=True)
        def fake_run(command, **kwargs):
            if "--version" in command:
                return mock.Mock(returncode=0, stdout="codex-cli 0.144.5\n", stderr="")
            else:
                drift_stdout = '{"type":"turn.completed","usage":{"output_tokens":1}}'
                return mock.Mock(returncode=0, stdout=drift_stdout, stderr="")
        with mock.patch("rsi_loop.subprocess.run", side_effect=fake_run), \
             mock.patch("rsi_loop._build_codex_run_scoped_home", return_value=os.path.join(self.tmp_dir, "temp_home")), \
             mock.patch("rsi_loop.HISTORY_DIR", self.tmp_dir), \
             mock.patch("rsi_loop._remove_owned_codex_home"):
            run_smoke_and_canary(ctx)
        self.assertFalse(os.path.exists(os.path.join(ctx.run_dir, "canary_candidate")))

    def test_canary_dir_failed_cleanup(self):
        ctx = RunContext()
        ctx.run_dir = os.path.realpath(os.path.join(self.tmp_dir, "run_dir"))
        os.makedirs(ctx.run_dir, exist_ok=True)
        ctx.codex_model = "gpt-5.6-sol"
        ctx.lock_model = "gpt-5.6-sol"
        ctx.operator_report = {}
        init_operator_report(ctx)
        os.makedirs(os.path.join(self.tmp_dir, "temp_home"), exist_ok=True)
        def fake_run(command, **kwargs):
            if "--version" in command:
                return mock.Mock(returncode=0, stdout="codex-cli 0.144.5\n", stderr="")
            else:
                drift_stdout = '{"type":"turn.completed","usage":{"output_tokens":1}}'
                return mock.Mock(returncode=0, stdout=drift_stdout, stderr="")
        with mock.patch("rsi_loop.subprocess.run", side_effect=fake_run), \
             mock.patch("rsi_loop._build_codex_run_scoped_home", return_value=os.path.join(self.tmp_dir, "temp_home")), \
             mock.patch("rsi_loop.HISTORY_DIR", self.tmp_dir), \
             mock.patch("rsi_loop._remove_owned_codex_home"), \
             mock.patch("shutil.rmtree", side_effect=OSError("rmtree failure")):
            with self.assertRaises(ExperimentInvalidError) as e:
                run_smoke_and_canary(ctx)
            self.assertIn("Canary cleanup failed", str(e.exception))

    def test_canary_dir_dual_cleanup_failure(self):
        ctx = RunContext()
        ctx.run_dir = os.path.realpath(os.path.join(self.tmp_dir, "run_dir"))
        os.makedirs(ctx.run_dir, exist_ok=True)
        ctx.codex_model = "gpt-5.6-sol"
        ctx.lock_model = "gpt-5.6-sol"
        ctx.operator_report = {}
        init_operator_report(ctx)
        os.makedirs(os.path.join(self.tmp_dir, "temp_home"), exist_ok=True)
        def fake_run(command, **kwargs):
            if "--version" in command:
                return mock.Mock(returncode=0, stdout="codex-cli 0.144.5\n", stderr="")
            else:
                drift_stdout = '{"type":"turn.completed","usage":{"output_tokens":1}}'
                return mock.Mock(returncode=0, stdout=drift_stdout, stderr="")
        with mock.patch("rsi_loop.subprocess.run", side_effect=fake_run), \
             mock.patch("rsi_loop._build_codex_run_scoped_home", return_value=os.path.join(self.tmp_dir, "temp_home")), \
             mock.patch("rsi_loop.HISTORY_DIR", self.tmp_dir), \
             mock.patch("rsi_loop._remove_owned_codex_home", side_effect=OSError("home delete failure")), \
             mock.patch("shutil.rmtree", side_effect=OSError("canary delete failure")):
            with self.assertRaises(ExperimentInvalidError) as e:
                run_smoke_and_canary(ctx)
            self.assertIn("Canary cleanup failed", str(e.exception))
            self.assertIn("Home cleanup failed", str(e.exception))

    def test_canary_profile_and_command_parity(self):
        ctx = RunContext()
        ctx.run_dir = os.path.realpath(os.path.join(self.tmp_dir, "run_dir"))
        os.makedirs(ctx.run_dir, exist_ok=True)
        ctx.codex_model = "gpt-5.6-sol"
        ctx.lock_model = "gpt-5.6-sol"
        ctx.operator_report = {}
        init_operator_report(ctx)
        os.makedirs(os.path.join(self.tmp_dir, "temp_home"), exist_ok=True)
        captured_commands = []
        captured_cwds = []
        captured_profiles = []
        def fake_run(command, **kwargs):
            captured_commands.append(command)
            captured_cwds.append(kwargs.get("cwd"))
            if "--version" in command:
                return mock.Mock(returncode=0, stdout="codex-cli 0.144.5\n", stderr="")
            else:
                drift_stdout = '{"type":"turn.completed","usage":{"output_tokens":1}}'
                return mock.Mock(returncode=0, stdout=drift_stdout, stderr="")
        def fake_profile_builder(real_temp_home, real_candidate_input_dir):
            captured_profiles.append(real_candidate_input_dir)
            return "/dummy/path.sb"
        with mock.patch("rsi_loop.subprocess.run", side_effect=fake_run), \
             mock.patch("rsi_loop._build_codex_run_scoped_home", return_value=os.path.join(self.tmp_dir, "temp_home")), \
             mock.patch("rsi_loop._build_generator_sb", side_effect=fake_profile_builder), \
             mock.patch("rsi_loop.HISTORY_DIR", self.tmp_dir), \
             mock.patch("rsi_loop._remove_owned_codex_home"):
            run_smoke_and_canary(ctx)
        canary_dir = os.path.realpath(os.path.join(ctx.run_dir, "canary_candidate"))
        self.assertEqual(captured_profiles[0], canary_dir)
        self.assertEqual(captured_cwds[0], canary_dir)
        self.assertEqual(captured_cwds[1], canary_dir)
        self.assertIn("-C", captured_commands[1])
        idx = captured_commands[1].index("-C")
        self.assertEqual(captured_commands[1][idx + 1], canary_dir)

    def test_strict_remote_verification_match_proceeds(self):
        ctx = RunContext()
        ctx.operator_report = {}
        init_operator_report(ctx)
        lock_data = {
            "approved_commit_sha": "061deeaf7c844a09cc52507d575e75cef25b2d1a",
            "remote_url": "https://github.com/hehamalainen/RSILoop.git",
            "model": "gpt-5.6-sol",
            "allow_no_holdout": False
        }
        lock_path = os.path.join(self.tmp_dir, "experiment.lock")
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)
        def fake_run(args, **kwargs):
            cmd_str = " ".join(args)
            if "git remote get-url" in cmd_str:
                return mock.Mock(returncode=0, stdout="https://github.com/hehamalainen/RSILoop.git\n", stderr="")
            elif "git ls-remote" in cmd_str:
                return mock.Mock(returncode=0, stdout="061deeaf7c844a09cc52507d575e75cef25b2d1a\trefs/heads/main\n", stderr="")
            elif "git rev-parse" in cmd_str:
                return mock.Mock(returncode=0, stdout="061deeaf7c844a09cc52507d575e75cef25b2d1a\n", stderr="")
            elif "git status" in cmd_str:
                return mock.Mock(returncode=0, stdout="", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("rsi_loop.BASE_DIR", self.tmp_dir), \
             mock.patch("rsi_loop.subprocess.run", side_effect=fake_run):
            sha = verify_experiment_lock(ctx)
        self.assertEqual(sha, "061deeaf7c844a09cc52507d575e75cef25b2d1a")

    def test_strict_remote_verification_rewritten_origin(self):
        ctx = RunContext()
        ctx.operator_report = {}
        init_operator_report(ctx)
        lock_data = {
            "approved_commit_sha": "061deeaf7c844a09cc52507d575e75cef25b2d1a",
            "remote_url": "https://github.com/hehamalainen/RSILoop.git",
            "model": "gpt-5.6-sol"
        }
        lock_path = os.path.join(self.tmp_dir, "experiment.lock")
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)
        def fake_run(args, **kwargs):
            cmd_str = " ".join(args)
            if "git remote get-url" in cmd_str:
                return mock.Mock(returncode=0, stdout="https://some-other-repo.git\n", stderr="")
            elif "git status" in cmd_str:
                return mock.Mock(returncode=0, stdout="", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("rsi_loop.BASE_DIR", self.tmp_dir), \
             mock.patch("rsi_loop.subprocess.run", side_effect=fake_run):
            with self.assertRaises(ExperimentInvalidError) as e:
                verify_experiment_lock(ctx)
        self.assertIn("Git origin URL mismatch", str(e.exception))

    def test_strict_remote_verification_missing_remote_url(self):
        ctx = RunContext()
        ctx.operator_report = {}
        init_operator_report(ctx)
        lock_data = {
            "approved_commit_sha": "061deeaf7c844a09cc52507d575e75cef25b2d1a",
            "model": "gpt-5.6-sol"
        }
        lock_path = os.path.join(self.tmp_dir, "experiment.lock")
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)
        def fake_run(args, **kwargs):
            return mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("rsi_loop.BASE_DIR", self.tmp_dir), \
             mock.patch("rsi_loop.subprocess.run", side_effect=fake_run):
            with self.assertRaises(ExperimentInvalidError) as e:
                verify_experiment_lock(ctx)
        self.assertIn("remote_url missing from experiment.lock", str(e.exception))

    def test_strict_remote_verification_malformed_output(self):
        ctx = RunContext()
        ctx.operator_report = {}
        init_operator_report(ctx)
        lock_data = {
            "approved_commit_sha": "061deeaf7c844a09cc52507d575e75cef25b2d1a",
            "remote_url": "https://github.com/hehamalainen/RSILoop.git",
            "model": "gpt-5.6-sol"
        }
        lock_path = os.path.join(self.tmp_dir, "experiment.lock")
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)
        def fake_run(args, **kwargs):
            cmd_str = " ".join(args)
            if "git remote get-url" in cmd_str:
                return mock.Mock(returncode=0, stdout="https://github.com/hehamalainen/RSILoop.git\n", stderr="")
            elif "git ls-remote" in cmd_str:
                return mock.Mock(returncode=0, stdout="not-a-sha-tab-refs/heads/main\n", stderr="")
            elif "git status" in cmd_str:
                return mock.Mock(returncode=0, stdout="", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("rsi_loop.BASE_DIR", self.tmp_dir), \
             mock.patch("rsi_loop.subprocess.run", side_effect=fake_run):
            with self.assertRaises(ExperimentInvalidError) as e:
                verify_experiment_lock(ctx)
        self.assertIn("git ls-remote output format mismatch", str(e.exception))

    def test_strict_remote_verification_multiple_lines(self):
        ctx = RunContext()
        ctx.operator_report = {}
        init_operator_report(ctx)
        lock_data = {
            "approved_commit_sha": "061deeaf7c844a09cc52507d575e75cef25b2d1a",
            "remote_url": "https://github.com/hehamalainen/RSILoop.git",
            "model": "gpt-5.6-sol"
        }
        lock_path = os.path.join(self.tmp_dir, "experiment.lock")
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)
        def fake_run(args, **kwargs):
            cmd_str = " ".join(args)
            if "git remote get-url" in cmd_str:
                return mock.Mock(returncode=0, stdout="https://github.com/hehamalainen/RSILoop.git\n", stderr="")
            elif "git ls-remote" in cmd_str:
                return mock.Mock(returncode=0, stdout="061deeaf7c844a09cc52507d575e75cef25b2d1a\trefs/heads/main\n061deeaf7c844a09cc52507d575e75cef25b2d1a\trefs/heads/main\n", stderr="")
            elif "git status" in cmd_str:
                return mock.Mock(returncode=0, stdout="", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("rsi_loop.BASE_DIR", self.tmp_dir), \
             mock.patch("rsi_loop.subprocess.run", side_effect=fake_run):
            with self.assertRaises(ExperimentInvalidError) as e:
                verify_experiment_lock(ctx)
        self.assertIn("git ls-remote returned invalid number of lines", str(e.exception))

    def test_strict_remote_verification_wrong_ref(self):
        ctx = RunContext()
        ctx.operator_report = {}
        init_operator_report(ctx)
        lock_data = {
            "approved_commit_sha": "061deeaf7c844a09cc52507d575e75cef25b2d1a",
            "remote_url": "https://github.com/hehamalainen/RSILoop.git",
            "model": "gpt-5.6-sol"
        }
        lock_path = os.path.join(self.tmp_dir, "experiment.lock")
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)
        def fake_run(args, **kwargs):
            cmd_str = " ".join(args)
            if "git remote get-url" in cmd_str:
                return mock.Mock(returncode=0, stdout="https://github.com/hehamalainen/RSILoop.git\n", stderr="")
            elif "git ls-remote" in cmd_str:
                return mock.Mock(returncode=0, stdout="061deeaf7c844a09cc52507d575e75cef25b2d1a\trefs/heads/not-main\n", stderr="")
            elif "git status" in cmd_str:
                return mock.Mock(returncode=0, stdout="", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("rsi_loop.BASE_DIR", self.tmp_dir), \
             mock.patch("rsi_loop.subprocess.run", side_effect=fake_run):
            with self.assertRaises(ExperimentInvalidError) as e:
                verify_experiment_lock(ctx)
        self.assertIn("git ls-remote output format mismatch", str(e.exception))

    def test_strict_remote_verification_timeout(self):
        ctx = RunContext()
        ctx.operator_report = {}
        init_operator_report(ctx)
        lock_data = {
            "approved_commit_sha": "061deeaf7c844a09cc52507d575e75cef25b2d1a",
            "remote_url": "https://github.com/hehamalainen/RSILoop.git",
            "model": "gpt-5.6-sol"
        }
        lock_path = os.path.join(self.tmp_dir, "experiment.lock")
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)
        def fake_run(args, **kwargs):
            cmd_str = " ".join(args)
            if "git remote get-url" in cmd_str:
                return mock.Mock(returncode=0, stdout="https://github.com/hehamalainen/RSILoop.git\n", stderr="")
            elif "git ls-remote" in cmd_str:
                raise subprocess.TimeoutExpired(args, 15)
            elif "git status" in cmd_str:
                return mock.Mock(returncode=0, stdout="", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("rsi_loop.BASE_DIR", self.tmp_dir), \
             mock.patch("rsi_loop.subprocess.run", side_effect=fake_run):
            with self.assertRaises(ExperimentInvalidError) as e:
                verify_experiment_lock(ctx)
        self.assertIn("git ls-remote", str(e.exception))

    def test_strict_remote_verification_authentication_failure(self):
        ctx = RunContext()
        ctx.operator_report = {}
        init_operator_report(ctx)
        lock_data = {
            "approved_commit_sha": "061deeaf7c844a09cc52507d575e75cef25b2d1a",
            "remote_url": "https://github.com/hehamalainen/RSILoop.git",
            "model": "gpt-5.6-sol"
        }
        lock_path = os.path.join(self.tmp_dir, "experiment.lock")
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)
        def fake_run(args, **kwargs):
            cmd_str = " ".join(args)
            if "git remote get-url" in cmd_str:
                return mock.Mock(returncode=0, stdout="https://github.com/hehamalainen/RSILoop.git\n", stderr="")
            elif "git ls-remote" in cmd_str:
                return mock.Mock(returncode=128, stdout="", stderr="fatal: Authentication failed")
            elif "git status" in cmd_str:
                return mock.Mock(returncode=0, stdout="", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("rsi_loop.BASE_DIR", self.tmp_dir), \
             mock.patch("rsi_loop.subprocess.run", side_effect=fake_run):
            with self.assertRaises(ExperimentInvalidError) as e:
                verify_experiment_lock(ctx)
        self.assertIn("git ls-remote", str(e.exception))

    def test_strict_remote_verification_sha_mismatch(self):
        ctx = RunContext()
        ctx.operator_report = {}
        init_operator_report(ctx)
        lock_data = {
            "approved_commit_sha": "061deeaf7c844a09cc52507d575e75cef25b2d1a",
            "remote_url": "https://github.com/hehamalainen/RSILoop.git",
            "model": "gpt-5.6-sol"
        }
        lock_path = os.path.join(self.tmp_dir, "experiment.lock")
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)
        def fake_run(args, **kwargs):
            cmd_str = " ".join(args)
            if "git remote get-url" in cmd_str:
                return mock.Mock(returncode=0, stdout="https://github.com/hehamalainen/RSILoop.git\n", stderr="")
            elif "git ls-remote" in cmd_str:
                return mock.Mock(returncode=0, stdout="beefeeaf7c844a09cc52507d575e75cef25b2d1a\trefs/heads/main\n", stderr="")
            elif "git rev-parse" in cmd_str:
                return mock.Mock(returncode=0, stdout="061deeaf7c844a09cc52507d575e75cef25b2d1a\n", stderr="")
            elif "git status" in cmd_str:
                return mock.Mock(returncode=0, stdout="", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("rsi_loop.BASE_DIR", self.tmp_dir), \
             mock.patch("rsi_loop.subprocess.run", side_effect=fake_run):
            with self.assertRaises(ExperimentInvalidError) as e:
                verify_experiment_lock(ctx)
        self.assertIn("Remote main SHA mismatch", str(e.exception))

    def test_strict_remote_verification_no_canary_called(self):
        ctx = RunContext()
        ctx.run_dir = os.path.join(self.tmp_dir, "run_dir")
        os.makedirs(ctx.run_dir, exist_ok=True)
        ctx.operator_report = {}
        init_operator_report(ctx)
        lock_data = {
            "approved_commit_sha": "061deeaf7c844a09cc52507d575e75cef25b2d1a",
            "remote_url": "https://github.com/hehamalainen/RSILoop.git",
            "model": "gpt-5.6-sol"
        }
        lock_path = os.path.join(self.tmp_dir, "experiment.lock")
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)
        def fake_run(args, **kwargs):
            cmd_str = " ".join(args)
            if "git remote get-url" in cmd_str:
                return mock.Mock(returncode=0, stdout="https://github.com/hehamalainen/RSILoop.git\n", stderr="")
            elif "git ls-remote" in cmd_str:
                return mock.Mock(returncode=1, stdout="", stderr="error")
            elif "git status" in cmd_str:
                return mock.Mock(returncode=0, stdout="", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")
        canary_called = False
        def fake_canary(*args, **kwargs):
            nonlocal canary_called
            canary_called = True
            return "version", 0
        with mock.patch("rsi_loop.BASE_DIR", self.tmp_dir), \
             mock.patch("rsi_loop.subprocess.run", side_effect=fake_run), \
             mock.patch("rsi_loop.run_smoke_and_canary", side_effect=fake_canary):
            with self.assertRaises(SystemExit):
                main()
        self.assertFalse(canary_called)

    def test_operator_report_early_invalid_emission(self):
        # No local context needed, main() creates its own.
        lock_data = {
            "approved_commit_sha": "061deeaf7c844a09cc52507d575e75cef25b2d1a",
            "remote_url": "https://github.com/hehamalainen/RSILoop.git",
            "model": "gpt-5.6-sol"
        }
        lock_path = os.path.join(self.tmp_dir, "experiment.lock")
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)
        def fake_run(args, **kwargs):
            cmd_str = " ".join(args)
            if "git remote get-url" in cmd_str:
                return mock.Mock(returncode=0, stdout="https://github.com/hehamalainen/RSILoop.git\n", stderr="")
            elif "git ls-remote" in cmd_str:
                raise RuntimeError("auth error")
            elif "git status" in cmd_str:
                return mock.Mock(returncode=0, stdout="", stderr="")
            return mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("rsi_loop.BASE_DIR", self.tmp_dir), \
             mock.patch("rsi_loop.subprocess.run", side_effect=fake_run), \
             mock.patch("rsi_loop.HISTORY_DIR", self.tmp_dir):
            with self.assertRaises(SystemExit):
                main()
        runs = [d for d in os.listdir(self.tmp_dir) if d.startswith("run_")]
        self.assertEqual(len(runs), 1)
        report_path = os.path.join(self.tmp_dir, runs[0], "operator_report.json")
        self.assertTrue(os.path.exists(report_path))
        with open(report_path) as rf:
            report = json.load(rf)
        self.assertEqual(report["status"], "invalid")
        self.assertIn("auth error", report["invalidation_reason"])
        self.assertEqual(report["phase_summaries"]["phase_0_lock_and_git"], "failed")

    def test_operator_report_cumulative_records(self):
        ctx = RunContext()
        ctx.run_dir = os.path.join(self.tmp_dir, "run_dir")
        os.makedirs(ctx.run_dir, exist_ok=True)
        ctx.operator_report = {}
        init_operator_report(ctx)
        log_phase_transition(ctx, "phase_0_lock_and_git", "running")
        log_phase_transition(ctx, "phase_0_lock_and_git", "completed")
        log_phase_transition(ctx, "smoke_and_canary", "running")
        self.assertEqual(len(ctx.operator_report["phase_records"]), 3)
        self.assertEqual(ctx.operator_report["phase_records"][0]["phase"], "phase_0_lock_and_git")
        self.assertEqual(ctx.operator_report["phase_records"][0]["new_state"], "running")
        self.assertEqual(ctx.operator_report["phase_records"][1]["new_state"], "completed")
        self.assertEqual(ctx.operator_report["phase_records"][2]["phase"], "smoke_and_canary")
        self.assertEqual(ctx.operator_report["phase_records"][2]["new_state"], "running")

    def test_operator_report_atomic_replacement(self):
        ctx = RunContext()
        ctx.run_dir = os.path.join(self.tmp_dir, "run_dir")
        os.makedirs(ctx.run_dir, exist_ok=True)
        ctx.operator_report = {"test": 123}
        save_operator_report(ctx)
        report_path = os.path.join(ctx.run_dir, "operator_report.json")
        self.assertTrue(os.path.exists(report_path))
        with open(report_path) as rf:
            self.assertEqual(json.load(rf)["test"], 123)

    def test_operator_report_explicit_states(self):
        ctx = RunContext()
        ctx.run_dir = os.path.join(self.tmp_dir, "run_dir")
        os.makedirs(ctx.run_dir, exist_ok=True)
        ctx.operator_report = {}
        init_operator_report(ctx)
        self.assertEqual(ctx.operator_report["phase_summaries"]["validation_loop"], "not_run")
        self.assertEqual(ctx.operator_report["local_head_sha"], "unknown")

    def test_operator_report_manifest_equality(self):
        ctx = RunContext()
        ctx.run_dir = os.path.join(self.tmp_dir, "run_dir")
        os.makedirs(ctx.run_dir, exist_ok=True)
        ctx.operator_report = {}
        init_operator_report(ctx)
        ctx.lock_file_hash = "mock_hash"
        ctx.operator_report["run_id"] = "run_123"
        ctx.operator_report["lock_contents"]["approved_commit_sha"] = "sha_abc"
        manifest_data = {
            "status": "completed_promoted",
            "run_id": "run_123",
            "approved_commit_sha": "sha_abc",
            "lock_file_hash": "mock_hash"
        }
        manifest_path = os.path.join(ctx.run_dir, "run_manifest.json")
        with mock.patch("rsi_loop.update_best_manifest_pointer"):
            publish_terminal_manifest_and_report(ctx, manifest_path, manifest_data)
        with open(manifest_path) as mf:
            m = json.load(mf)
        with open(os.path.join(ctx.run_dir, "operator_report.json")) as rf:
            r = json.load(rf)
        self.assertEqual(m["status"], r["status"])
        self.assertEqual(m["run_id"], r["run_id"])
        self.assertEqual(m["approved_commit_sha"], r["lock_contents"]["approved_commit_sha"])

    def test_operator_report_manifest_hash(self):
        ctx = RunContext()
        ctx.run_dir = os.path.join(self.tmp_dir, "run_dir")
        os.makedirs(ctx.run_dir, exist_ok=True)
        ctx.operator_report = {}
        init_operator_report(ctx)
        ctx.lock_file_hash = "mock_hash"
        ctx.operator_report["run_id"] = "run_123"
        ctx.operator_report["lock_contents"]["approved_commit_sha"] = "sha_abc"
        manifest_data = {
            "status": "completed_promoted",
            "run_id": "run_123",
            "approved_commit_sha": "sha_abc",
            "lock_file_hash": "mock_hash"
        }
        manifest_path = os.path.join(ctx.run_dir, "run_manifest.json")
        with mock.patch("rsi_loop.update_best_manifest_pointer"):
            publish_terminal_manifest_and_report(ctx, manifest_path, manifest_data)
        with open(os.path.join(ctx.run_dir, "operator_report.json")) as rf:
            r = json.load(rf)
        with open(manifest_path, "rb") as mf:
            manifest_hash = hashlib.sha256(mf.read()).hexdigest()
        self.assertEqual(r["manifest_sha256"], manifest_hash)

    def test_operator_report_sensitive_data_exclusion(self):
        ctx = RunContext()
        ctx.run_dir = os.path.join(self.tmp_dir, "run_dir")
        os.makedirs(ctx.run_dir, exist_ok=True)
        ctx.operator_report = {}
        init_operator_report(ctx)
        serialized = json.dumps(ctx.operator_report)
        self.assertNotIn("prompt_templates", serialized)
        self.assertNotIn("solution_code", serialized)

    def test_operator_report_terminal_sealing(self):
        ctx = RunContext()
        ctx.run_dir = os.path.join(self.tmp_dir, "run_dir")
        os.makedirs(ctx.run_dir, exist_ok=True)
        ctx.operator_report = {}
        init_operator_report(ctx)
        ctx.lock_file_hash = "mock_hash"
        ctx.operator_report["run_id"] = "run_123"
        ctx.operator_report["lock_contents"]["approved_commit_sha"] = "sha_abc"
        manifest_data = {
            "status": "completed_promoted",
            "run_id": "run_123",
            "approved_commit_sha": "sha_abc",
            "lock_file_hash": "mock_hash"
        }
        manifest_path = os.path.join(ctx.run_dir, "run_manifest.json")
        with mock.patch("rsi_loop.update_best_manifest_pointer"):
            publish_terminal_manifest_and_report(ctx, manifest_path, manifest_data)
        self.assertEqual(os.stat(manifest_path).st_mode & 0o777, 0o444)
        self.assertEqual(os.stat(os.path.join(ctx.run_dir, "operator_report.json")).st_mode & 0o777, 0o444)

    def test_operator_report_write_failure(self):
        ctx = RunContext()
        ctx.run_dir = os.path.join(self.tmp_dir, "run_dir")
        os.makedirs(ctx.run_dir, exist_ok=True)
        ctx.operator_report = {}
        init_operator_report(ctx)
        with mock.patch("os.open", side_effect=OSError("disk full")):
            with self.assertRaises(ExperimentInvalidError) as e:
                save_operator_report(ctx)
        self.assertIn("Failed to open temporary report file", str(e.exception))

    def test_operator_report_incomplete_pair_rejection(self):
        ctx = RunContext()
        ctx.run_dir = os.path.join(self.tmp_dir, "run_dir")
        os.makedirs(ctx.run_dir, exist_ok=True)
        pointer_data = {
            "pointer_schema_version": "2",
            "run_manifest_path": "run_dir/run_manifest.json",
            "run_manifest_sha256": "05dc7d7b73e719b07198fa845d47b0499baea8f21635e3f5ce8684cfda3770d0"
        }
        with open(os.path.join(ctx.run_dir, "run_manifest.json"), "w") as f:
            json.dump({"schema_version": "2", "status": "completed_promoted"}, f)
        with mock.patch("rsi_loop.BASE_DIR", self.tmp_dir), \
             mock.patch("rsi_loop.HISTORY_DIR", self.tmp_dir):
            with self.assertRaises(RuntimeError) as e:
                validate_manifest_pointer_and_target(pointer_data)
        self.assertIn("Target operator report missing", str(e.exception))

    def test_strict_remote_verification_whitespace_and_blank_lines(self):
        ctx = RunContext()
        ctx.approved_commit_sha = "a" * 40
        ctx.lock_file_hash = "mock_hash"
        ctx.lock_remote_url = "https://github.com/hehamalainen/RSILoop.git"
        ctx.lock_model = "gpt-5.6-sol"
        ctx.allow_no_holdout = False

        lock_data = {
            "approved_commit_sha": ctx.approved_commit_sha,
            "remote_url": ctx.lock_remote_url,
            "model": ctx.lock_model,
            "allow_no_holdout": ctx.allow_no_holdout
        }
        with open(os.path.join(self.tmp_dir, "experiment.lock"), "w") as f:
            json.dump(lock_data, f)

        # Test case 1: Multiple lines
        with mock.patch("rsi_loop.BASE_DIR", self.tmp_dir):
            with mock.patch("rsi_loop.subprocess.run") as mock_run:
                mock_run.side_effect = [
                    mock.Mock(returncode=0, stdout=""),  # git status
                    mock.Mock(returncode=0, stdout=f"{ctx.lock_remote_url}\n"),  # git remote get-url
                    mock.Mock(returncode=0, stdout=f"{ctx.approved_commit_sha}\trefs/heads/main\n\n"),  # git ls-remote (blank line at end)
                ]
                with self.assertRaises(ExperimentInvalidError) as e:
                    verify_experiment_lock(ctx)
                self.assertIn("git ls-remote returned invalid number of lines", str(e.exception))

        # Test case 2: Space separation instead of tab
        with mock.patch("rsi_loop.BASE_DIR", self.tmp_dir):
            with mock.patch("rsi_loop.subprocess.run") as mock_run:
                mock_run.side_effect = [
                    mock.Mock(returncode=0, stdout=""),  # git status
                    mock.Mock(returncode=0, stdout=f"{ctx.lock_remote_url}\n"),  # git remote get-url
                    mock.Mock(returncode=0, stdout=f"{ctx.approved_commit_sha} refs/heads/main\n"),  # space
                ]
                with self.assertRaises(ExperimentInvalidError) as e:
                    verify_experiment_lock(ctx)
                self.assertIn("git ls-remote output format mismatch", str(e.exception))

        # Test case 3: Trailing space on the SHA line
        with mock.patch("rsi_loop.BASE_DIR", self.tmp_dir):
            with mock.patch("rsi_loop.subprocess.run") as mock_run:
                mock_run.side_effect = [
                    mock.Mock(returncode=0, stdout=""),  # git status
                    mock.Mock(returncode=0, stdout=f"{ctx.lock_remote_url}\n"),  # git remote get-url
                    mock.Mock(returncode=0, stdout=f"{ctx.approved_commit_sha}\trefs/heads/main \n"),  # trailing space
                ]
                with self.assertRaises(ExperimentInvalidError) as e:
                    verify_experiment_lock(ctx)
                self.assertIn("git ls-remote output format mismatch", str(e.exception))

        # Test case 4: Valid trailing newline (Unix)
        with mock.patch("rsi_loop.BASE_DIR", self.tmp_dir):
            with mock.patch("rsi_loop.subprocess.run") as mock_run:
                mock_run.side_effect = [
                    mock.Mock(returncode=0, stdout=""),  # git status
                    mock.Mock(returncode=0, stdout=f"{ctx.lock_remote_url}\n"),  # git remote get-url
                    mock.Mock(returncode=0, stdout=f"{ctx.approved_commit_sha}\trefs/heads/main\n"),  # valid
                    mock.Mock(returncode=0, stdout=f"{ctx.approved_commit_sha}\n"),  # git rev-parse HEAD
                ]
                self.assertEqual(verify_experiment_lock(ctx), ctx.approved_commit_sha)

        # Test case 5: Valid trailing CRLF (Windows)
        with mock.patch("rsi_loop.BASE_DIR", self.tmp_dir):
            with mock.patch("rsi_loop.subprocess.run") as mock_run:
                mock_run.side_effect = [
                    mock.Mock(returncode=0, stdout=""),  # git status
                    mock.Mock(returncode=0, stdout=f"{ctx.lock_remote_url}\n"),  # git remote get-url
                    mock.Mock(returncode=0, stdout=f"{ctx.approved_commit_sha}\trefs/heads/main\r\n"),  # valid CRLF
                    mock.Mock(returncode=0, stdout=f"{ctx.approved_commit_sha}\n"),  # git rev-parse HEAD
                ]
                self.assertEqual(verify_experiment_lock(ctx), ctx.approved_commit_sha)

    def test_operator_report_sensitive_data_exclusion_under_nesting(self):
        ctx = RunContext()
        ctx.trial_results = [{
            "trial": "trial_1",
            "baseline_score": 0.5,
            "opt_score": 0.8,
            "delta": 0.3,
            "candidate_prompt": "SECRET_PROMPT",
            "candidate_prompt_hash": "hash_abc",
            "baseline_run": {
                "benchmarks": {"task": {"success": True, "error": "SECRET_ERROR"}},
                "tokens_consumed": 100
            },
            "mutations": [{
                "mutation_index": 1,
                "prompt": "SECRET_PROMPT_2",
                "prompt_hash": "hash_def",
                "optimizer_tokens_consumed": 50,
                "evaluation_run": {
                    "benchmarks": {"task": {"success": False, "error": "SECRET_ERROR_2"}},
                    "tokens_consumed": 20
                }
            }]
        }]
        ctx.preflight_huffman_data = {
            "huffman_preflight_score": 1.0,
            "huffman_preflight_success": True,
            "huffman_log": "SECRET_LOG",
            "solution_hash": "hash_huff",
            "tokens_consumed": 30
        }
        sync_context_to_report(ctx)
        serialized = json.dumps(ctx.operator_report)
        self.assertNotIn("SECRET_PROMPT", serialized)
        self.assertNotIn("SECRET_PROMPT_2", serialized)
        self.assertNotIn("SECRET_ERROR", serialized)
        self.assertNotIn("SECRET_ERROR_2", serialized)
        self.assertNotIn("SECRET_LOG", serialized)
        self.assertEqual(ctx.operator_report["preflight_huffman"]["tests_run"], 5)
        self.assertEqual(ctx.operator_report["trials"][0]["delta"], 0.3)

    def test_sync_context_to_report_inside_publication(self):
        ctx = RunContext()
        ctx.run_id = "run_123"
        ctx.run_dir = os.path.join(self.tmp_dir, "run_dir")
        os.makedirs(ctx.run_dir, exist_ok=True)
        ctx.operator_report = {}
        init_operator_report(ctx)
        ctx.lock_file_hash = "mock_hash"
        ctx.approved_commit_sha = "sha_abc"
        manifest_data = {
            "status": "completed_promoted",
            "run_id": "run_123",
            "approved_commit_sha": "sha_abc",
            "lock_file_hash": "mock_hash"
        }
        manifest_path = os.path.join(ctx.run_dir, "run_manifest.json")
        with mock.patch("rsi_loop.update_best_manifest_pointer"):
            publish_terminal_manifest_and_report(ctx, manifest_path, manifest_data)

        self.assertEqual(ctx.operator_report["lock_contents"]["approved_commit_sha"], "sha_abc")
        manifest_bytes = json.dumps(manifest_data, indent=2).encode('utf-8')
        expected_sha = hashlib.sha256(manifest_bytes).hexdigest()
        self.assertEqual(ctx.operator_report["manifest_sha256"], expected_sha)

    def test_partial_terminal_publication_blocks_competing_invalid_manifest(self):
        ctx = RunContext()
        ctx.run_id = "run_123"
        ctx.run_dir = os.path.join(self.tmp_dir, "run_dir")
        os.makedirs(ctx.run_dir, exist_ok=True)
        ctx.operator_report = {}
        init_operator_report(ctx)
        ctx.lock_file_hash = "mock_hash"
        ctx.approved_commit_sha = "sha_abc"
        manifest_data = {
            "status": "completed_promoted",
            "run_id": "run_123",
            "approved_commit_sha": "sha_abc",
            "lock_file_hash": "mock_hash"
        }
        manifest_path = os.path.join(ctx.run_dir, "run_manifest.json")

        original_replace = os.replace
        report_path = os.path.join(ctx.run_dir, "operator_report.json")
        def mock_replace(src, dst):
            if dst == report_path:
                raise OSError("injected report replace failure")
            return original_replace(src, dst)

        with mock.patch("rsi_loop.os.replace", side_effect=mock_replace):
            with self.assertRaises(ExperimentInvalidError):
                publish_terminal_manifest_and_report(ctx, manifest_path, manifest_data)

        self.assertEqual(ctx.terminal_state, "manifest_published")

        with self.assertRaises(ExperimentInvalidError) as e:
            publish_terminal_manifest_and_report(ctx, manifest_path, manifest_data)
        self.assertIn("competing write blocked", str(e.exception))

    def test_main_no_headroom_pairing(self):
        run_id = "test_run_no_headroom"
        run_dir = os.path.join(self.tmp_dir, "history", run_id)

        lock_data = {
            "approved_commit_sha": "a" * 40,
            "remote_url": "https://github.com/hehamalainen/RSILoop.git",
            "model": "gpt-5.6-sol",
            "allow_no_holdout": False
        }
        lock_path = os.path.join(self.tmp_dir, "experiment.lock")
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)

        prompt_dir = os.path.join(self.tmp_dir, "prompt_templates")
        os.makedirs(prompt_dir, exist_ok=True)
        with open(os.path.join(prompt_dir, "inner_agent_baseline.txt"), "w") as f:
            f.write("baseline prompt")

        hashes_data = {
            "prompt_templates/inner_agent_baseline.txt": hashlib.sha256(b"baseline prompt").hexdigest(),
            "prompt_templates/meta_optimizer.txt": "dummy",
            "benchmarks/circular_buffer/problem.txt": "dummy",
            "benchmarks/circular_buffer/test_harness.py": "dummy",
            "benchmarks/json_parser/problem.txt": "dummy",
            "benchmarks/json_parser/test_harness.py": "dummy",
            "benchmarks/run_length_encoding/problem.txt": "dummy",
            "benchmarks/run_length_encoding/test_harness.py": "dummy",
            "benchmarks/huffman_coding/problem.txt": "dummy",
            "benchmarks/huffman_coding/test_harness.py": "dummy",
            "benchmarks/validation/string_calculator/problem.txt": "dummy",
            "benchmarks/validation/string_calculator/test_harness.py": "dummy",
            "benchmarks/validation/matrix_operations/problem.txt": "dummy",
            "benchmarks/validation/matrix_operations/test_harness.py": "dummy",
            "candidate.sb.template": "dummy",
            "generator.sb.template": "dummy"
        }
        shutil.copy(os.path.join(BASE_DIR, "candidate.sb.template"), os.path.join(self.tmp_dir, "candidate.sb.template"))
        shutil.copy(os.path.join(BASE_DIR, "generator.sb.template"), os.path.join(self.tmp_dir, "generator.sb.template"))
        with open(os.path.join(self.tmp_dir, "preflight_hashes.json"), "w") as f:
            json.dump(hashes_data, f)

        with mock.patch("rsi_loop.BASE_DIR", self.tmp_dir), \
             mock.patch("rsi_loop.HISTORY_DIR", os.path.join(self.tmp_dir, "history")), \
             mock.patch("rsi_loop.PROMPTS_DIR", os.path.join(self.tmp_dir, "prompts")), \
             mock.patch("rsi_loop.verify_preflight_hashes"), \
             mock.patch("rsi_loop._capture_environment_metadata"), \
             mock.patch("rsi_loop.run_smoke_and_canary", return_value=("codex-1.0", 50)), \
             mock.patch("rsi_loop.call_codex_sandboxed", return_value=("def solution(): pass", 100)), \
             mock.patch("rsi_loop.run_evaluation_on_candidate", return_value={"success": True, "log": "Ran 5 tests"}):

            with mock.patch("rsi_loop.subprocess.run") as mock_sub:
                mock_sub.side_effect = [
                    mock.Mock(returncode=0, stdout=""),  # git status
                    mock.Mock(returncode=0, stdout=f"{lock_data['remote_url']}\n"),  # git remote get-url
                    mock.Mock(returncode=0, stdout=f"{lock_data['approved_commit_sha']}\trefs/heads/main\n"),  # git ls-remote
                    mock.Mock(returncode=0, stdout=f"{lock_data['approved_commit_sha']}\n"),  # git rev-parse HEAD
                ]
                with mock.patch("rsi_loop.update_best_manifest_pointer"), \
                     mock.patch("rsi_loop.uuid.uuid4") as mock_uuid:
                    mock_uuid.return_value = mock.Mock(hex=run_id.split("_")[-1])
                    with mock.patch("rsi_loop.time.time", return_value=12345678):
                        expected_run_id = f"run_12345678_{run_id.split('_')[-1]}"
                        expected_run_dir = os.path.join(self.tmp_dir, "history", expected_run_id)
                        with self.assertRaises(SystemExit) as se:
                            main()
                        self.assertEqual(se.exception.code, 0)

                        manifest_path = os.path.join(expected_run_dir, "run_manifest.json")
                        report_path = os.path.join(expected_run_dir, "operator_report.json")
                        self.assertTrue(os.path.exists(manifest_path))
                        self.assertTrue(os.path.exists(report_path))

                        self.assertEqual(os.stat(manifest_path).st_mode & 0o777, 0o444)
                        self.assertEqual(os.stat(report_path).st_mode & 0o777, 0o444)

                        with open(manifest_path, "rb") as mf:
                            m_hash = hashlib.sha256(mf.read()).hexdigest()
                        with open(report_path) as rf:
                            r_data = json.load(rf)
                        self.assertEqual(r_data["manifest_sha256"], m_hash)
                        self.assertEqual(r_data["status"], "no_headroom")

    def test_main_completed_no_improvement_pairing(self):
        run_id = "test_run_noimprov"
        lock_data = {
            "approved_commit_sha": "a" * 40,
            "remote_url": "https://github.com/hehamalainen/RSILoop.git",
            "model": "gpt-5.6-sol",
            "allow_no_holdout": False
        }
        lock_path = os.path.join(self.tmp_dir, "experiment.lock")
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)

        prompt_dir = os.path.join(self.tmp_dir, "prompt_templates")
        os.makedirs(prompt_dir, exist_ok=True)
        with open(os.path.join(prompt_dir, "inner_agent_baseline.txt"), "w") as f:
            f.write("baseline prompt")

        hashes_data = {
            "prompt_templates/inner_agent_baseline.txt": hashlib.sha256(b"baseline prompt").hexdigest(),
            "prompt_templates/meta_optimizer.txt": "dummy",
            "benchmarks/circular_buffer/problem.txt": "dummy",
            "benchmarks/circular_buffer/test_harness.py": "dummy",
            "benchmarks/json_parser/problem.txt": "dummy",
            "benchmarks/json_parser/test_harness.py": "dummy",
            "benchmarks/run_length_encoding/problem.txt": "dummy",
            "benchmarks/run_length_encoding/test_harness.py": "dummy",
            "benchmarks/huffman_coding/problem.txt": "dummy",
            "benchmarks/huffman_coding/test_harness.py": "dummy",
            "benchmarks/validation/string_calculator/problem.txt": "dummy",
            "benchmarks/validation/string_calculator/test_harness.py": "dummy",
            "benchmarks/validation/matrix_operations/problem.txt": "dummy",
            "benchmarks/validation/matrix_operations/test_harness.py": "dummy",
            "candidate.sb.template": "dummy",
            "generator.sb.template": "dummy"
        }
        shutil.copy(os.path.join(BASE_DIR, "candidate.sb.template"), os.path.join(self.tmp_dir, "candidate.sb.template"))
        shutil.copy(os.path.join(BASE_DIR, "generator.sb.template"), os.path.join(self.tmp_dir, "generator.sb.template"))
        with open(os.path.join(self.tmp_dir, "preflight_hashes.json"), "w") as f:
            json.dump(hashes_data, f)

        with mock.patch("rsi_loop.BASE_DIR", self.tmp_dir), \
             mock.patch("rsi_loop.HISTORY_DIR", os.path.join(self.tmp_dir, "history")), \
             mock.patch("rsi_loop.PROMPTS_DIR", os.path.join(self.tmp_dir, "prompts")), \
             mock.patch("rsi_loop.verify_preflight_hashes"), \
             mock.patch("rsi_loop._capture_environment_metadata"), \
             mock.patch("rsi_loop.run_smoke_and_canary", return_value=("codex-1.0", 50)), \
             mock.patch("rsi_loop.call_codex_sandboxed", return_value=("def solution(): pass", 100)), \
             mock.patch("rsi_loop.run_evaluation_on_candidate", return_value={"success": False, "log": "Ran 5 tests"}), \
             mock.patch("rsi_loop._list_optimization_benchmarks", return_value=["circular_buffer"]), \
             mock.patch("rsi_loop._list_validation_benchmarks", return_value=["validation_task"]), \
             mock.patch("rsi_loop.evaluate_prompt_on_tasks", return_value=(0.5, {}, 100)):

            with mock.patch("rsi_loop.subprocess.run") as mock_sub:
                run_calls = []
                mocks = [
                    mock.Mock(returncode=0, stdout=""),  # git status
                    mock.Mock(returncode=0, stdout=f"{lock_data['remote_url']}\n"),  # git remote get-url
                    mock.Mock(returncode=0, stdout=f"{lock_data['approved_commit_sha']}\trefs/heads/main\n"),  # git ls-remote
                    mock.Mock(returncode=0, stdout=f"{lock_data['approved_commit_sha']}\n"),  # git rev-parse HEAD
                ]
                def run_side_effect(*args, **kwargs):
                    cmd = args[0]
                    run_calls.append(cmd)
                    if len(run_calls) <= len(mocks):
                        return mocks[len(run_calls) - 1]
                    return mock.Mock(returncode=0, stdout="")
                mock_sub.side_effect = run_side_effect
                with mock.patch("rsi_loop.update_best_manifest_pointer"), \
                     mock.patch("rsi_loop.uuid.uuid4") as mock_uuid:
                    mock_uuid.return_value = mock.Mock(hex=run_id.split("_")[-1])
                    with mock.patch("rsi_loop.time.time", return_value=12345678):
                        expected_run_id = f"run_12345678_{run_id.split('_')[-1]}"
                        expected_run_dir = os.path.join(self.tmp_dir, "history", expected_run_id)
                        with self.assertRaises(SystemExit) as se:
                            main()
                        try:
                            self.assertEqual(se.exception.code, 0)
                        except AssertionError:
                            self.fail(f"SystemExit code was not 0. Subprocess calls: {run_calls}")

                        manifest_path = os.path.join(expected_run_dir, "run_manifest.json")
                        report_path = os.path.join(expected_run_dir, "operator_report.json")
                        self.assertTrue(os.path.exists(manifest_path))
                        self.assertTrue(os.path.exists(report_path))

                        with open(manifest_path, "rb") as mf:
                            m_hash = hashlib.sha256(mf.read()).hexdigest()
                        with open(report_path) as rf:
                            r_data = json.load(rf)
                        self.assertEqual(r_data["manifest_sha256"], m_hash)
                        self.assertEqual(r_data["status"], "completed_no_improvement")

    def test_main_completed_promoted_pairing(self):
        run_id = "test_run_promoted"
        lock_data = {
            "approved_commit_sha": "a" * 40,
            "remote_url": "https://github.com/hehamalainen/RSILoop.git",
            "model": "gpt-5.6-sol",
            "allow_no_holdout": False
        }
        lock_path = os.path.join(self.tmp_dir, "experiment.lock")
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)

        prompt_dir = os.path.join(self.tmp_dir, "prompt_templates")
        os.makedirs(prompt_dir, exist_ok=True)
        os.makedirs(os.path.join(self.tmp_dir, "prompts"), exist_ok=True)
        with open(os.path.join(prompt_dir, "inner_agent_baseline.txt"), "w") as f:
            f.write("baseline prompt")

        hashes_data = {
            "prompt_templates/inner_agent_baseline.txt": hashlib.sha256(b"baseline prompt").hexdigest(),
            "prompt_templates/meta_optimizer.txt": "dummy",
            "benchmarks/circular_buffer/problem.txt": "dummy",
            "benchmarks/circular_buffer/test_harness.py": "dummy",
            "benchmarks/json_parser/problem.txt": "dummy",
            "benchmarks/json_parser/test_harness.py": "dummy",
            "benchmarks/run_length_encoding/problem.txt": "dummy",
            "benchmarks/run_length_encoding/test_harness.py": "dummy",
            "benchmarks/huffman_coding/problem.txt": "dummy",
            "benchmarks/huffman_coding/test_harness.py": "dummy",
            "benchmarks/validation/string_calculator/problem.txt": "dummy",
            "benchmarks/validation/string_calculator/test_harness.py": "dummy",
            "benchmarks/validation/matrix_operations/problem.txt": "dummy",
            "benchmarks/validation/matrix_operations/test_harness.py": "dummy",
            "candidate.sb.template": "dummy",
            "generator.sb.template": "dummy"
        }
        shutil.copy(os.path.join(BASE_DIR, "candidate.sb.template"), os.path.join(self.tmp_dir, "candidate.sb.template"))
        shutil.copy(os.path.join(BASE_DIR, "generator.sb.template"), os.path.join(self.tmp_dir, "generator.sb.template"))
        with open(os.path.join(self.tmp_dir, "preflight_hashes.json"), "w") as f:
            json.dump(hashes_data, f)

        with mock.patch("rsi_loop.BASE_DIR", os.path.realpath(self.tmp_dir)), \
             mock.patch("rsi_loop.HISTORY_DIR", os.path.realpath(os.path.join(self.tmp_dir, "history"))), \
             mock.patch("rsi_loop.PROMPTS_DIR", os.path.realpath(os.path.join(self.tmp_dir, "prompts"))), \
             mock.patch("rsi_loop.BEST_MANIFEST_PATH", os.path.realpath(os.path.join(self.tmp_dir, "best_manifest.json"))), \
             mock.patch("rsi_loop.verify_preflight_hashes"), \
             mock.patch("rsi_loop._capture_environment_metadata"), \
             mock.patch("rsi_loop.run_smoke_and_canary", return_value=("codex-1.0", 50)), \
             mock.patch("rsi_loop.call_codex_sandboxed", return_value=("def solution(): pass", 100)), \
             mock.patch("rsi_loop.run_evaluation_on_candidate", return_value={"success": False, "log": "Ran 5 tests"}), \
             mock.patch("rsi_loop._list_optimization_benchmarks", return_value=["circular_buffer"]), \
             mock.patch("rsi_loop._list_validation_benchmarks", return_value=["validation_task"]), \
             mock.patch("rsi_loop.mutate_prompt", return_value=("better prompt", 200)), \
             mock.patch("rsi_loop.evaluate_prompt_on_tasks") as mock_eval:

            mock_eval.side_effect = [
                (0.5, {"circular_buffer": {"success": False, "error": "err"}}, 100), # trial 1 baseline
                (0.8, {}, 100), # trial 1 mut 1
                (0.5, {"circular_buffer": {"success": False, "error": "err"}}, 100), # trial 2 baseline
                (0.8, {}, 100), # trial 2 mut 1
                (0.5, {"circular_buffer": {"success": False, "error": "err"}}, 100), # trial 3 baseline
                (0.8, {}, 100), # trial 3 mut 1
                (0.6, {}, 100), # validation baseline
                (0.9, {}, 100), # validation candidate 1
                (0.9, {}, 100), # validation candidate 2
                (0.9, {}, 100), # validation candidate 3
            ]

            with mock.patch("rsi_loop.subprocess.run") as mock_sub:
                mock_sub.side_effect = [
                    mock.Mock(returncode=0, stdout=""),  # git status
                    mock.Mock(returncode=0, stdout=f"{lock_data['remote_url']}\n"),  # git remote get-url
                    mock.Mock(returncode=0, stdout=f"{lock_data['approved_commit_sha']}\trefs/heads/main\n"),  # git ls-remote
                    mock.Mock(returncode=0, stdout=f"{lock_data['approved_commit_sha']}\n"),  # git rev-parse HEAD
                ]
                with mock.patch("rsi_loop.update_best_manifest_pointer"), \
                     mock.patch("rsi_loop.uuid.uuid4") as mock_uuid:
                    mock_uuid.return_value = mock.Mock(hex=run_id.split("_")[-1])
                    with mock.patch("rsi_loop.time.time", return_value=12345678):
                        expected_run_id = f"run_12345678_{run_id.split('_')[-1]}"
                        expected_run_dir = os.path.join(self.tmp_dir, "history", expected_run_id)
                        with self.assertRaises(SystemExit) as se:
                            main()
                        self.assertEqual(se.exception.code, 0)

                        manifest_path = os.path.join(expected_run_dir, "run_manifest.json")
                        report_path = os.path.join(expected_run_dir, "operator_report.json")
                        self.assertTrue(os.path.exists(manifest_path))
                        self.assertTrue(os.path.exists(report_path))

                        with open(manifest_path, "rb") as mf:
                            m_hash = hashlib.sha256(mf.read()).hexdigest()
                        with open(report_path) as rf:
                            r_data = json.load(rf)
                        self.assertEqual(r_data["manifest_sha256"], m_hash)
                        self.assertEqual(r_data["status"], "completed_promoted")

                        run_best_prompt_path = os.path.join(expected_run_dir, "inner_agent_best.txt")
                        self.assertTrue(os.path.exists(run_best_prompt_path))
                        with open(run_best_prompt_path) as pf:
                            self.assertEqual(pf.read(), "better prompt")

    def test_main_invalid_pairing(self):
        run_id = "test_run_invalid"
        lock_data = {
            "approved_commit_sha": "a" * 40,
            "remote_url": "https://github.com/hehamalainen/RSILoop.git",
            "model": "gpt-5.6-sol",
            "allow_no_holdout": True
        }
        lock_path = os.path.join(self.tmp_dir, "experiment.lock")
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)

        with mock.patch("rsi_loop.BASE_DIR", self.tmp_dir), \
             mock.patch("rsi_loop.HISTORY_DIR", os.path.join(self.tmp_dir, "history")), \
             mock.patch("rsi_loop.PROMPTS_DIR", os.path.join(self.tmp_dir, "prompts")), \
             mock.patch("rsi_loop.verify_preflight_hashes"), \
             mock.patch("rsi_loop._capture_environment_metadata"), \
             mock.patch("rsi_loop.update_best_manifest_pointer"), \
             mock.patch("rsi_loop.uuid.uuid4") as mock_uuid:

            mock_uuid.return_value = mock.Mock(hex=run_id.split("_")[-1])
            with mock.patch("rsi_loop.time.time", return_value=12345678):
                with mock.patch("rsi_loop.subprocess.run") as mock_sub:
                    mock_sub.side_effect = [
                        mock.Mock(returncode=0, stdout=" M rsi_loop.py\n"),  # git status dirty!
                    ]
                    expected_run_id = f"run_12345678_{run_id.split('_')[-1]}"
                    expected_run_dir = os.path.join(self.tmp_dir, "history", expected_run_id)
                    with self.assertRaises(SystemExit) as se:
                        main()
                    self.assertEqual(se.exception.code, 1)

                    manifest_path = os.path.join(expected_run_dir, "invalid_manifest.json")
                    report_path = os.path.join(expected_run_dir, "operator_report.json")
                    self.assertTrue(os.path.exists(manifest_path))
                    self.assertTrue(os.path.exists(report_path))

                    with open(manifest_path, "rb") as mf:
                        m_hash = hashlib.sha256(mf.read()).hexdigest()
                    with open(report_path) as rf:
                        r_data = json.load(rf)
                    self.assertEqual(r_data["manifest_sha256"], m_hash)
                    self.assertEqual(r_data["status"], "invalid")
                    self.assertIn("Git worktree is not clean", r_data["invalidation_reason"])

    def test_competing_invalid_manifest_sentinel_blocked(self):
        ctx = RunContext()
        ctx.run_dir = os.path.join(self.tmp_dir, "run_dir")
        os.makedirs(ctx.run_dir, exist_ok=True)
        ctx.lock_file_hash = "mock_hash"
        ctx.approved_commit_sha = "sha_abc"
        ctx.terminal_state = "staging"

        invalid_manifest_path = os.path.join(ctx.run_dir, "invalid_manifest.json")
        with self.assertRaises(ExperimentInvalidError) as e:
            publish_terminal_manifest_and_report(ctx, invalid_manifest_path, {})
        self.assertIn("competing write blocked", str(e.exception))

    def test_filter_benchmarks_non_dict_rejection(self):
        from rsi_loop import _filter_benchmarks
        with self.assertRaises(ExperimentInvalidError) as e:
            _filter_benchmarks({"task": "not a dict"})
        self.assertIn("Unexpected benchmark data shape", str(e.exception))

    def test_pre_existing_staging_files_rejected(self):
        ctx = RunContext()
        ctx.run_id = "run_123"
        ctx.run_dir = os.path.join(self.tmp_dir, "run_dir")
        os.makedirs(ctx.run_dir, exist_ok=True)
        ctx.lock_file_hash = "mock_hash"
        ctx.approved_commit_sha = "sha_abc"
        ctx.terminal_state = "not_started"
        init_operator_report(ctx)

        manifest_path = os.path.join(ctx.run_dir, "run_manifest.json")
        with open(manifest_path + ".tmp", "w") as f:
            f.write("preexisting")

        with self.assertRaises(ExperimentInvalidError) as e:
            publish_terminal_manifest_and_report(ctx, manifest_path, {"status": "completed_no_improvement", "run_id": "run_123", "approved_commit_sha": "sha_abc", "lock_file_hash": "mock_hash"})
        self.assertIn("Staging manifest file already exists", str(e.exception))

    def test_parent_dir_fsync_failure(self):
        ctx = RunContext()
        ctx.run_id = "run_123"
        ctx.run_dir = os.path.join(self.tmp_dir, "run_dir")
        os.makedirs(ctx.run_dir, exist_ok=True)
        ctx.lock_file_hash = "mock_hash"
        ctx.approved_commit_sha = "sha_abc"
        ctx.terminal_state = "not_started"
        init_operator_report(ctx)

        manifest_path = os.path.join(ctx.run_dir, "run_manifest.json")
        def fake_fsync(fd):
            raise OSError("injected fsync failure")

        with mock.patch("rsi_loop.os.fsync", fake_fsync):
            with self.assertRaises(ExperimentInvalidError) as e:
                publish_terminal_manifest_and_report(ctx, manifest_path, {"status": "completed_no_improvement", "run_id": "run_123", "approved_commit_sha": "sha_abc", "lock_file_hash": "mock_hash"})
            self.assertIn("injected fsync failure", str(e.exception))

    def test_promotion_failure_fallbacks(self):
        run_id = "test_run_promoted_fail"
        lock_data = {
            "approved_commit_sha": "a" * 40,
            "remote_url": "https://github.com/hehamalainen/RSILoop.git",
            "model": "gpt-5.6-sol",
            "allow_no_holdout": True
        }
        lock_path = os.path.join(self.tmp_dir, "experiment.lock")
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)

        prompt_dir = os.path.join(self.tmp_dir, "prompt_templates")
        os.makedirs(prompt_dir, exist_ok=True)
        os.makedirs(os.path.join(self.tmp_dir, "prompts"), exist_ok=True)
        with open(os.path.join(prompt_dir, "inner_agent_baseline.txt"), "w") as f:
            f.write("baseline prompt")

        hashes_data = {
            "prompt_templates/inner_agent_baseline.txt": hashlib.sha256(b"baseline prompt").hexdigest(),
            "prompt_templates/meta_optimizer.txt": "dummy",
            "benchmarks/circular_buffer/problem.txt": "dummy",
            "benchmarks/circular_buffer/test_harness.py": "dummy",
            "benchmarks/json_parser/problem.txt": "dummy",
            "benchmarks/json_parser/test_harness.py": "dummy",
            "benchmarks/run_length_encoding/problem.txt": "dummy",
            "benchmarks/run_length_encoding/test_harness.py": "dummy",
            "benchmarks/huffman_coding/problem.txt": "dummy",
            "benchmarks/huffman_coding/test_harness.py": "dummy",
            "benchmarks/validation/string_calculator/problem.txt": "dummy",
            "benchmarks/validation/string_calculator/test_harness.py": "dummy",
            "benchmarks/validation/matrix_operations/problem.txt": "dummy",
            "benchmarks/validation/matrix_operations/test_harness.py": "dummy",
            "candidate.sb.template": "dummy",
            "generator.sb.template": "dummy"
        }
        shutil.copy(os.path.join(BASE_DIR, "candidate.sb.template"), os.path.join(self.tmp_dir, "candidate.sb.template"))
        shutil.copy(os.path.join(BASE_DIR, "generator.sb.template"), os.path.join(self.tmp_dir, "generator.sb.template"))
        with open(os.path.join(self.tmp_dir, "preflight_hashes.json"), "w") as f:
            json.dump(hashes_data, f)

        def fake_verify_lock(ctx):
            ctx.approved_commit_sha = "a" * 40
            ctx.lock_file_hash = "mock_hash"
            return "a" * 40

        with mock.patch("rsi_loop.BASE_DIR", os.path.realpath(self.tmp_dir)), \
             mock.patch("rsi_loop.HISTORY_DIR", os.path.realpath(os.path.join(self.tmp_dir, "history"))), \
             mock.patch("rsi_loop.PROMPTS_DIR", os.path.realpath(os.path.join(self.tmp_dir, "prompts"))), \
             mock.patch("rsi_loop.BEST_MANIFEST_PATH", os.path.realpath(os.path.join(self.tmp_dir, "best_manifest.json"))), \
             mock.patch("rsi_loop.verify_preflight_hashes"), \
             mock.patch("rsi_loop.verify_experiment_lock", fake_verify_lock), \
             mock.patch("rsi_loop._capture_environment_metadata"), \
             mock.patch("rsi_loop.run_smoke_and_canary", return_value=("codex-1.0", 50)), \
             mock.patch("rsi_loop.call_codex_sandboxed", return_value=("def solution(): pass", 100)), \
             mock.patch("rsi_loop.run_evaluation_on_candidate", return_value={"success": False, "log": "Ran 5 tests"}), \
             mock.patch("rsi_loop._list_optimization_benchmarks", return_value=["circular_buffer"]), \
             mock.patch("rsi_loop._list_validation_benchmarks", return_value=["validation_task"]), \
             mock.patch("rsi_loop.mutate_prompt", return_value=("better prompt", 200)), \
             mock.patch("rsi_loop.evaluate_prompt_on_tasks") as mock_eval:

            mock_eval.side_effect = [
                (0.5, {"circular_buffer": {"success": False, "error": "err"}}, 100), # trial 1 baseline
                (0.8, {}, 100), # trial 1 mut 1
                (0.5, {"circular_buffer": {"success": False, "error": "err"}}, 100), # trial 2 baseline
                (0.8, {}, 100), # trial 2 mut 1
                (0.5, {"circular_buffer": {"success": False, "error": "err"}}, 100), # trial 3 baseline
                (0.8, {}, 100), # trial 3 mut 1
                (0.6, {}, 100), # validation baseline
                (0.9, {}, 100), # validation candidate 1
                (0.9, {}, 100), # validation candidate 2
                (0.9, {}, 100), # validation candidate 3
            ]

            with mock.patch("rsi_loop.subprocess.run") as mock_sub:
                mock_sub.side_effect = [
                    mock.Mock(returncode=0, stdout=""),  # git status
                    mock.Mock(returncode=0, stdout=f"{lock_data['remote_url']}\n"),  # git remote get-url
                    mock.Mock(returncode=0, stdout=f"{lock_data['approved_commit_sha']}\trefs/heads/main\n"),  # git ls-remote
                    mock.Mock(returncode=0, stdout=f"{lock_data['approved_commit_sha']}\n"),  # git rev-parse HEAD
                ]
                with mock.patch("rsi_loop.uuid.uuid4") as mock_uuid:
                    mock_uuid.return_value = mock.Mock(hex=run_id.split("_")[-1])
                    with mock.patch("rsi_loop.time.time", return_value=12345678):
                        expected_run_id = f"run_12345678_{run_id.split('_')[-1]}"
                        expected_run_dir = os.path.join(self.tmp_dir, "history", expected_run_id)

                        original_replace = os.replace
                        def fake_replace(src, dst):
                            if dst.endswith("run_manifest.json"):
                                raise OSError("injected manifest publication failure")
                            original_replace(src, dst)

                        with mock.patch("rsi_loop.os.replace", fake_replace):
                            with self.assertRaises(SystemExit) as se:
                                main()
                            self.assertEqual(se.exception.code, 1)

                        manifest_path = os.path.join(expected_run_dir, "run_manifest.json")
                        self.assertFalse(os.path.exists(manifest_path))

                        invalid_path = os.path.join(expected_run_dir, "invalid_manifest.json")
                        report_path = os.path.join(expected_run_dir, "operator_report.json")
                        self.assertTrue(os.path.exists(invalid_path))
                        self.assertTrue(os.path.exists(report_path))

                        with open(report_path) as rf:
                            r_data = json.load(rf)
                        self.assertEqual(r_data["status"], "invalid")
                        self.assertIn("injected manifest publication failure", r_data["invalidation_reason"])

    def test_preflight_failure_after_phase0_transition(self):
        run_id = "test_run_phase0_fail"
        lock_data = {
            "approved_commit_sha": "a" * 40,
            "remote_url": "https://github.com/hehamalainen/RSILoop.git",
            "model": "gpt-5.6-sol",
            "allow_no_holdout": False
        }
        lock_path = os.path.join(self.tmp_dir, "experiment.lock")
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)

        with mock.patch("rsi_loop.BASE_DIR", os.path.realpath(self.tmp_dir)), \
             mock.patch("rsi_loop.HISTORY_DIR", os.path.realpath(os.path.join(self.tmp_dir, "history"))), \
             mock.patch("rsi_loop.PRIVATE_DIR", os.path.realpath(os.path.join(self.tmp_dir, "private_holdout_missing"))), \
             mock.patch("rsi_loop.BEST_MANIFEST_PATH", os.path.realpath(os.path.join(self.tmp_dir, "best_manifest.json"))), \
             mock.patch("rsi_loop.verify_preflight_hashes"), \
             mock.patch("rsi_loop._capture_environment_metadata"), \
             mock.patch("rsi_loop.subprocess.run") as mock_sub:

            mock_sub.side_effect = [
                mock.Mock(returncode=0, stdout=""),  # git status
                mock.Mock(returncode=0, stdout=f"{lock_data['remote_url']}\n"),  # git remote get-url
                mock.Mock(returncode=0, stdout=f"{lock_data['approved_commit_sha']}\trefs/heads/main\n"),  # git ls-remote
                mock.Mock(returncode=0, stdout=f"{lock_data['approved_commit_sha']}\n"),  # git rev-parse HEAD
            ]
            with mock.patch("rsi_loop.uuid.uuid4") as mock_uuid:
                mock_uuid.return_value = mock.Mock(hex=run_id.split("_")[-1])
                with mock.patch("rsi_loop.time.time", return_value=12345678):
                    expected_run_id = f"run_12345678_{run_id.split('_')[-1]}"
                    expected_run_dir = os.path.join(self.tmp_dir, "history", expected_run_id)

                    with self.assertRaises(SystemExit) as se:
                        main()
                    self.assertEqual(se.exception.code, 1)

                    invalid_path = os.path.join(expected_run_dir, "invalid_manifest.json")
                    report_path = os.path.join(expected_run_dir, "operator_report.json")
                    self.assertTrue(os.path.exists(invalid_path))
                    self.assertTrue(os.path.exists(report_path))

                    with open(report_path) as rf:
                        r_data = json.load(rf)
                    self.assertEqual(r_data["status"], "invalid")
                    self.assertIn("Private holdout directory is missing", r_data["invalidation_reason"])
                    self.assertEqual(r_data["phase_summaries"]["phase_0_lock_and_git"], "failed")

    def test_promotion_failure_does_not_contaminate(self):
        run_id = "test_run_contaminate_check"
        lock_data = {
            "approved_commit_sha": "a" * 40,
            "remote_url": "https://github.com/hehamalainen/RSILoop.git",
            "model": "gpt-5.6-sol",
            "allow_no_holdout": True
        }
        lock_path = os.path.join(self.tmp_dir, "experiment.lock")
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)

        # Set up prompts dir and preexisting best prompt in history/run_prev
        history_dir = os.path.join(self.tmp_dir, "history")
        prev_run_dir = os.path.join(history_dir, "run_prev")
        os.makedirs(prev_run_dir, exist_ok=True)
        prev_prompt_path = os.path.join(prev_run_dir, "inner_agent_best.txt")
        with open(prev_prompt_path, "w") as f:
            f.write("previous best prompt content")
        os.chmod(prev_prompt_path, 0o444)

        # Set up a valid previous manifest
        prev_manifest = {
            "schema_version": "2",
            "run_id": "run_prev",
            "status": "completed_promoted",
            "approved_commit_sha": "a" * 40,
            "lock_file_hash": "mock_hash",
            "model_name": "gpt-5.6-sol",
            "model_requested": "gpt-5.6-sol",
            "model_resolved": "gpt-5.6-sol",
            "selected_prompt_path": "inner_agent_best.txt",
            "selected_prompt_hash": hashlib.sha256(b"previous best prompt content").hexdigest(),
            "timestamp": "2026-07-20T00:00:00Z"
        }
        prev_manifest_path = os.path.join(prev_run_dir, "run_manifest.json")
        with open(prev_manifest_path, "w") as f:
            json.dump(prev_manifest, f, indent=2)
        os.chmod(prev_manifest_path, 0o444)

        prev_report = {
            "schema_version": "2",
            "status": "completed_promoted",
            "pointer_commit_status": "committed",
            "manifest_sha256": hashlib.sha256(json.dumps(prev_manifest, indent=2).encode('utf-8')).hexdigest()
        }
        with open(os.path.join(prev_run_dir, "operator_report.json"), "w") as f:
            json.dump(prev_report, f, indent=2)
        os.chmod(os.path.join(prev_run_dir, "operator_report.json"), 0o444)

        # Set up valid pointer pointing to the previous manifest
        best_manifest_path = os.path.realpath(os.path.join(self.tmp_dir, "best_manifest.json"))
        pointer_data = {
            "pointer_schema_version": "2",
            "run_manifest_path": "history/run_prev/run_manifest.json",
            "run_manifest_sha256": hashlib.sha256(json.dumps(prev_manifest, indent=2).encode('utf-8')).hexdigest()
        }
        with open(best_manifest_path, "w") as f:
            json.dump(pointer_data, f, indent=2)

        with open(best_manifest_path, "rb") as f:
            original_pointer_bytes = f.read()

        prompt_dir = os.path.join(self.tmp_dir, "prompt_templates")
        os.makedirs(prompt_dir, exist_ok=True)
        with open(os.path.join(prompt_dir, "inner_agent_baseline.txt"), "w") as f:
            f.write("baseline prompt")

        hashes_data = {
            "prompt_templates/inner_agent_baseline.txt": hashlib.sha256(b"baseline prompt").hexdigest(),
            "prompt_templates/meta_optimizer.txt": "dummy",
            "benchmarks/circular_buffer/problem.txt": "dummy",
            "benchmarks/circular_buffer/test_harness.py": "dummy",
            "benchmarks/json_parser/problem.txt": "dummy",
            "benchmarks/json_parser/test_harness.py": "dummy",
            "benchmarks/run_length_encoding/problem.txt": "dummy",
            "benchmarks/run_length_encoding/test_harness.py": "dummy",
            "benchmarks/huffman_coding/problem.txt": "dummy",
            "benchmarks/huffman_coding/test_harness.py": "dummy",
            "benchmarks/validation/string_calculator/problem.txt": "dummy",
            "benchmarks/validation/string_calculator/test_harness.py": "dummy",
            "benchmarks/validation/matrix_operations/problem.txt": "dummy",
            "benchmarks/validation/matrix_operations/test_harness.py": "dummy",
            "candidate.sb.template": "dummy",
            "generator.sb.template": "dummy"
        }
        shutil.copy(os.path.join(BASE_DIR, "candidate.sb.template"), os.path.join(self.tmp_dir, "candidate.sb.template"))
        shutil.copy(os.path.join(BASE_DIR, "generator.sb.template"), os.path.join(self.tmp_dir, "generator.sb.template"))
        with open(os.path.join(self.tmp_dir, "preflight_hashes.json"), "w") as f:
            json.dump(hashes_data, f)

        def fake_verify_lock(ctx):
            ctx.approved_commit_sha = "a" * 40
            ctx.lock_file_hash = "mock_hash"
            return "a" * 40

        with mock.patch("rsi_loop.BASE_DIR", os.path.realpath(self.tmp_dir)), \
             mock.patch("rsi_loop.HISTORY_DIR", os.path.realpath(history_dir)), \
             mock.patch("rsi_loop.PROMPTS_DIR", os.path.realpath(os.path.join(self.tmp_dir, "prompts"))), \
             mock.patch("rsi_loop.BEST_MANIFEST_PATH", best_manifest_path), \
             mock.patch("rsi_loop.verify_preflight_hashes"), \
             mock.patch("rsi_loop.verify_experiment_lock", fake_verify_lock), \
             mock.patch("rsi_loop._capture_environment_metadata"), \
             mock.patch("rsi_loop.run_smoke_and_canary", return_value=("codex-1.0", 50)), \
             mock.patch("rsi_loop.call_codex_sandboxed", return_value=("def solution(): pass", 100)), \
             mock.patch("rsi_loop.run_evaluation_on_candidate", return_value={"success": False, "log": "Ran 5 tests"}), \
             mock.patch("rsi_loop._list_optimization_benchmarks", return_value=["circular_buffer"]), \
             mock.patch("rsi_loop._list_validation_benchmarks", return_value=["validation_task"]), \
             mock.patch("rsi_loop.mutate_prompt", return_value=("better prompt", 200)), \
             mock.patch("rsi_loop.evaluate_prompt_on_tasks") as mock_eval:

            mock_eval.side_effect = [
                (0.5, {"circular_buffer": {"success": False, "error": "err"}}, 100), # trial 1 baseline
                (0.8, {}, 100), # trial 1 mut 1
                (0.5, {"circular_buffer": {"success": False, "error": "err"}}, 100), # trial 2 baseline
                (0.8, {}, 100), # trial 2 mut 1
                (0.5, {"circular_buffer": {"success": False, "error": "err"}}, 100), # trial 3 baseline
                (0.8, {}, 100), # trial 3 mut 1
                (0.6, {}, 100), # validation baseline
                (0.9, {}, 100), # validation candidate 1
                (0.9, {}, 100), # validation candidate 2
                (0.9, {}, 100), # validation candidate 3
            ]

            with mock.patch("rsi_loop.subprocess.run") as mock_sub:
                mock_sub.side_effect = [
                    mock.Mock(returncode=0, stdout=""),  # git status
                    mock.Mock(returncode=0, stdout=f"{lock_data['remote_url']}\n"),  # git remote get-url
                    mock.Mock(returncode=0, stdout=f"{lock_data['approved_commit_sha']}\trefs/heads/main\n"),  # git ls-remote
                    mock.Mock(returncode=0, stdout=f"{lock_data['approved_commit_sha']}\n"),  # git rev-parse HEAD
                ]
                with mock.patch("rsi_loop.uuid.uuid4") as mock_uuid:
                    mock_uuid.return_value = mock.Mock(hex=run_id.split("_")[-1])
                    with mock.patch("rsi_loop.time.time", return_value=12345678):
                        expected_run_id = f"run_12345678_{run_id.split('_')[-1]}"
                        expected_run_dir = os.path.join(history_dir, expected_run_id)

                        original_replace = os.replace
                        def fake_replace(src, dst):
                            if dst == best_manifest_path:
                                raise OSError("injected pointer failure")
                            original_replace(src, dst)

                        with mock.patch("rsi_loop.os.replace", fake_replace):
                            with self.assertRaises(SystemExit) as se:
                                main()
                            self.assertEqual(se.exception.code, 1)

                        # Check that rollback deleted the false completed_promoted artifacts
                        new_manifest_path = os.path.join(expected_run_dir, "run_manifest.json")
                        new_report_path = os.path.join(expected_run_dir, "operator_report.json")
                        new_prompt_path = os.path.join(expected_run_dir, "inner_agent_best.txt")
                        self.assertFalse(os.path.exists(new_manifest_path))
                        self.assertFalse(os.path.exists(new_prompt_path))

                        # And check that the invalid outcomes were correctly written
                        invalid_path = os.path.join(expected_run_dir, "invalid_manifest.json")
                        self.assertTrue(os.path.exists(invalid_path))
                        self.assertTrue(os.path.exists(new_report_path))

                        with open(new_report_path) as rf:
                            r_data = json.load(rf)
                        self.assertEqual(r_data["status"], "invalid")
                        self.assertIn("injected pointer failure", r_data["invalidation_reason"])

                        # Crucially, the previous best prompt remains byte-for-byte unchanged
                        with open(prev_prompt_path) as pf:
                            self.assertEqual(pf.read(), "previous best prompt content")

                        # Crucially, the pointer best_manifest.json remains byte-for-byte unchanged
                        with open(best_manifest_path, "rb") as f:
                            self.assertEqual(f.read(), original_pointer_bytes)

    def test_promotion_post_commit_indeterminate_failure(self):
        run_id = "test_run_post_commit_fail"
        lock_data = {
            "approved_commit_sha": "a" * 40,
            "remote_url": "https://github.com/hehamalainen/RSILoop.git",
            "model": "gpt-5.6-sol",
            "allow_no_holdout": True
        }
        lock_path = os.path.join(self.tmp_dir, "experiment.lock")
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)


        # Set up prompts dir and preexisting best prompt in history/run_prev
        history_dir = os.path.join(self.tmp_dir, "history")
        prev_run_dir = os.path.join(history_dir, "run_prev")
        os.makedirs(prev_run_dir, exist_ok=True)
        prev_prompt_path = os.path.join(prev_run_dir, "inner_agent_best.txt")
        with open(prev_prompt_path, "w") as f:
            f.write("previous best prompt content")
        os.chmod(prev_prompt_path, 0o444)

        # Set up a valid previous manifest
        prev_manifest = {
            "schema_version": "2",
            "run_id": "run_prev",
            "status": "completed_promoted",
            "approved_commit_sha": "a" * 40,
            "lock_file_hash": "mock_hash",
            "model_name": "gpt-5.6-sol",
            "model_requested": "gpt-5.6-sol",
            "model_resolved": "gpt-5.6-sol",
            "selected_prompt_path": "inner_agent_best.txt",
            "selected_prompt_hash": hashlib.sha256(b"previous best prompt content").hexdigest(),
            "timestamp": "2026-07-20T00:00:00Z"
        }
        prev_manifest_path = os.path.join(prev_run_dir, "run_manifest.json")
        with open(prev_manifest_path, "w") as f:
            json.dump(prev_manifest, f, indent=2)
        os.chmod(prev_manifest_path, 0o444)

        prev_report = {
            "schema_version": "2",
            "status": "completed_promoted",
            "pointer_commit_status": "committed",
            "manifest_sha256": hashlib.sha256(json.dumps(prev_manifest, indent=2).encode('utf-8')).hexdigest()
        }
        with open(os.path.join(prev_run_dir, "operator_report.json"), "w") as f:
            json.dump(prev_report, f, indent=2)
        os.chmod(os.path.join(prev_run_dir, "operator_report.json"), 0o444)

        # Set up valid pointer pointing to the previous manifest
        best_manifest_path = os.path.realpath(os.path.join(self.tmp_dir, "best_manifest.json"))
        pointer_data = {
            "pointer_schema_version": "2",
            "run_manifest_path": "history/run_prev/run_manifest.json",
            "run_manifest_sha256": hashlib.sha256(json.dumps(prev_manifest, indent=2).encode('utf-8')).hexdigest()
        }
        with open(best_manifest_path, "w") as f:
            json.dump(pointer_data, f, indent=2)

        prompt_dir = os.path.join(self.tmp_dir, "prompt_templates")
        os.makedirs(prompt_dir, exist_ok=True)
        with open(os.path.join(prompt_dir, "inner_agent_baseline.txt"), "w") as f:
            f.write("baseline prompt")

        hashes_data = {
            "prompt_templates/inner_agent_baseline.txt": hashlib.sha256(b"baseline prompt").hexdigest(),
            "prompt_templates/meta_optimizer.txt": "dummy",
            "benchmarks/circular_buffer/problem.txt": "dummy",
            "benchmarks/circular_buffer/test_harness.py": "dummy",
            "benchmarks/json_parser/problem.txt": "dummy",
            "benchmarks/json_parser/test_harness.py": "dummy",
            "benchmarks/run_length_encoding/problem.txt": "dummy",
            "benchmarks/run_length_encoding/test_harness.py": "dummy",
            "benchmarks/huffman_coding/problem.txt": "dummy",
            "benchmarks/huffman_coding/test_harness.py": "dummy",
            "benchmarks/validation/string_calculator/problem.txt": "dummy",
            "benchmarks/validation/string_calculator/test_harness.py": "dummy",
            "benchmarks/validation/matrix_operations/problem.txt": "dummy",
            "benchmarks/validation/matrix_operations/test_harness.py": "dummy",
            "candidate.sb.template": "dummy",
            "generator.sb.template": "dummy"
        }
        shutil.copy(os.path.join(BASE_DIR, "candidate.sb.template"), os.path.join(self.tmp_dir, "candidate.sb.template"))
        shutil.copy(os.path.join(BASE_DIR, "generator.sb.template"), os.path.join(self.tmp_dir, "generator.sb.template"))
        with open(os.path.join(self.tmp_dir, "preflight_hashes.json"), "w") as f:
            json.dump(hashes_data, f)

        def fake_verify_lock(ctx):
            ctx.approved_commit_sha = "a" * 40
            ctx.lock_file_hash = "mock_hash"
            return "a" * 40

        with mock.patch("rsi_loop.BASE_DIR", os.path.realpath(self.tmp_dir)), \
             mock.patch("rsi_loop.HISTORY_DIR", os.path.realpath(history_dir)), \
             mock.patch("rsi_loop.PROMPTS_DIR", os.path.realpath(os.path.join(self.tmp_dir, "prompts"))), \
             mock.patch("rsi_loop.BEST_MANIFEST_PATH", best_manifest_path), \
             mock.patch("rsi_loop.verify_preflight_hashes"), \
             mock.patch("rsi_loop.verify_experiment_lock", fake_verify_lock), \
             mock.patch("rsi_loop._capture_environment_metadata"), \
             mock.patch("rsi_loop.run_smoke_and_canary", return_value=("codex-1.0", 50)), \
             mock.patch("rsi_loop.call_codex_sandboxed", return_value=("def solution(): pass", 100)), \
             mock.patch("rsi_loop.run_evaluation_on_candidate", return_value={"success": False, "log": "Ran 5 tests"}), \
             mock.patch("rsi_loop._list_optimization_benchmarks", return_value=["circular_buffer"]), \
             mock.patch("rsi_loop._list_validation_benchmarks", return_value=["validation_task"]), \
             mock.patch("rsi_loop.mutate_prompt", return_value=("better prompt", 200)), \
             mock.patch("rsi_loop.evaluate_prompt_on_tasks") as mock_eval:

            mock_eval.side_effect = [
                (0.5, {"circular_buffer": {"success": False, "error": "err"}}, 100), # trial 1 baseline
                (0.8, {}, 100), # trial 1 mut 1
                (0.5, {"circular_buffer": {"success": False, "error": "err"}}, 100), # trial 2 baseline
                (0.8, {}, 100), # trial 2 mut 1
                (0.5, {"circular_buffer": {"success": False, "error": "err"}}, 100), # trial 3 baseline
                (0.8, {}, 100), # trial 3 mut 1
                (0.6, {}, 100), # validation baseline
                (0.9, {}, 100), # validation candidate 1
                (0.9, {}, 100), # validation candidate 2
                (0.9, {}, 100), # validation candidate 3
            ]

            with mock.patch("rsi_loop.subprocess.run") as mock_sub:
                mock_sub.side_effect = [
                    mock.Mock(returncode=0, stdout=""),  # git status
                    mock.Mock(returncode=0, stdout=f"{lock_data['remote_url']}\n"),  # git remote get-url
                    mock.Mock(returncode=0, stdout=f"{lock_data['approved_commit_sha']}\trefs/heads/main\n"),  # git ls-remote
                    mock.Mock(returncode=0, stdout=f"{lock_data['approved_commit_sha']}\n"),  # git rev-parse HEAD
                ]
                with mock.patch("rsi_loop.uuid.uuid4") as mock_uuid:
                    mock_uuid.return_value = mock.Mock(hex=run_id.split("_")[-1])
                    with mock.patch("rsi_loop.time.time", return_value=12345678):
                        expected_run_id = f"run_12345678_{run_id.split('_')[-1]}"
                        expected_run_dir = os.path.join(history_dir, expected_run_id)

                        original_fsync = os.fsync
                        original_open = os.open
                        opened_fds = {}

                        def fake_open(path, flags, *args, **kwargs):
                            fd = original_open(path, flags, *args, **kwargs)
                            opened_fds[fd] = os.path.realpath(path)
                            return fd

                        def fake_fsync(fd):
                            path = opened_fds.get(fd)
                            if path == os.path.realpath(os.path.dirname(best_manifest_path)):
                                raise OSError("injected directory fsync failure")
                            original_fsync(fd)

                        with mock.patch("rsi_loop.os.open", fake_open), \
                             mock.patch("rsi_loop.os.fsync", fake_fsync):
                            with self.assertRaises(SystemExit) as se:
                                main()
                            self.assertEqual(se.exception.code, 1)

                        # Indeterminate: pointer HAS mutated to target the new manifest
                        with open(best_manifest_path) as f:
                            ptr = json.load(f)
                        self.assertEqual(ptr["run_manifest_path"], f"history/{expected_run_id}/run_manifest.json")

                        # New run manifest/report/prompt MUST exist and not be rolled back
                        new_manifest_path = os.path.join(expected_run_dir, "run_manifest.json")
                        new_report_path = os.path.join(expected_run_dir, "operator_report.json")
                        new_prompt_path = os.path.join(expected_run_dir, "inner_agent_best.txt")
                        self.assertTrue(os.path.exists(new_manifest_path))
                        self.assertTrue(os.path.exists(new_report_path))
                        self.assertTrue(os.path.exists(new_prompt_path))

                        with open(new_report_path) as rf:
                            r_data = json.load(rf)
                        self.assertEqual(r_data.get("pointer_commit_status"), "indeterminate")
                        self.assertIn("injected directory fsync failure", r_data.get("pointer_commit_error", ""))

                        # No invalid manifest exists
                        invalid_path = os.path.join(expected_run_dir, "invalid_manifest.json")
                        self.assertFalse(os.path.exists(invalid_path))

                        # Verify that the startup validator validates containment, symlink rejection, type, mode, and hash of the prompt.
                        # If the prompt is missing:
                        os.chmod(new_prompt_path, 0o600)
                        os.remove(new_prompt_path)
                        with self.assertRaises(RuntimeError) as e_miss:
                            validate_manifest_pointer_and_target(ptr)
                        self.assertIn("Selected prompt file missing", str(e_miss.exception))

                        # If the prompt is a symlink:
                        os.symlink(prev_prompt_path, new_prompt_path)
                        with self.assertRaises(RuntimeError) as e_sym:
                            validate_manifest_pointer_and_target(ptr)
                        self.assertIn("Symlink detected in selected prompt path", str(e_sym.exception))
                        os.remove(new_prompt_path)

                        # Restore prompt with tampered hash:
                        with open(new_prompt_path, "w") as f:
                            f.write("tampered content")
                        os.chmod(new_prompt_path, 0o444)
                        with self.assertRaises(RuntimeError) as e_hash:
                            validate_manifest_pointer_and_target(ptr)
                        self.assertIn("Selected prompt hash mismatch", str(e_hash.exception))

    def test_validate_manifest_pointer_missing_prompt_fields(self):
        # Create pointer data pointing to a manifest that lacks prompt fields
        history_dir = os.path.join(self.tmp_dir, "history")
        run_dir = os.path.join(history_dir, "run_missing_fields")
        os.makedirs(run_dir, exist_ok=True)

        manifest = {
            "schema_version": "2",
            "run_id": "run_missing_fields",
            "status": "completed_promoted",
            "approved_commit_sha": "a" * 40,
            "lock_file_hash": "mock_hash"
        }
        manifest_path = os.path.join(run_dir, "run_manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)

        report = {
            "schema_version": "2",
            "status": "completed_promoted",
            "pointer_commit_status": "committed",
            "manifest_sha256": hashlib.sha256(json.dumps(manifest, indent=2).encode('utf-8')).hexdigest()
        }
        report_path = os.path.join(run_dir, "operator_report.json")
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)
        os.chmod(report_path, 0o444)

        pointer_data = {
            "pointer_schema_version": "2",
            "run_manifest_path": "history/run_missing_fields/run_manifest.json",
            "run_manifest_sha256": hashlib.sha256(json.dumps(manifest, indent=2).encode('utf-8')).hexdigest()
        }
        with mock.patch("rsi_loop.HISTORY_DIR", history_dir), \
             mock.patch("rsi_loop.BASE_DIR", self.tmp_dir):
            with self.assertRaises(RuntimeError) as e:
                validate_manifest_pointer_and_target(pointer_data)
            self.assertIn("selected_prompt_path or selected_prompt_hash missing", str(e.exception))

    def test_validate_manifest_pointer_parent_symlink_rejection(self):
        # Create a symlinked directory component in prompt path and verify rejection
        history_dir = os.path.join(self.tmp_dir, "history")
        run_dir = os.path.join(history_dir, "run_parent_symlink")
        os.makedirs(run_dir, exist_ok=True)

        # Create alias -> . symlink inside run_dir
        os.symlink(".", os.path.join(run_dir, "alias"))

        # Create best prompt
        prompt_path = os.path.join(run_dir, "inner_agent_best.txt")
        with open(prompt_path, "w") as f:
            f.write("content")
        os.chmod(prompt_path, 0o444)

        manifest = {
            "schema_version": "2",
            "run_id": "run_parent_symlink",
            "status": "completed_promoted",
            "approved_commit_sha": "a" * 40,
            "lock_file_hash": "mock_hash",
            "selected_prompt_path": "alias/inner_agent_best.txt",
            "selected_prompt_hash": hashlib.sha256(b"content").hexdigest()
        }
        manifest_path = os.path.join(run_dir, "run_manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(manifest, f, indent=2)

        report = {
            "schema_version": "2",
            "status": "completed_promoted",
            "pointer_commit_status": "committed",
            "manifest_sha256": hashlib.sha256(json.dumps(manifest, indent=2).encode('utf-8')).hexdigest()
        }
        report_path = os.path.join(run_dir, "operator_report.json")
        with open(report_path, "w") as f:
            json.dump(report, f, indent=2)
        os.chmod(report_path, 0o444)

        pointer_data = {
            "pointer_schema_version": "2",
            "run_manifest_path": "history/run_parent_symlink/run_manifest.json",
            "run_manifest_sha256": hashlib.sha256(json.dumps(manifest, indent=2).encode('utf-8')).hexdigest()
        }

        with mock.patch("rsi_loop.HISTORY_DIR", history_dir), \
             mock.patch("rsi_loop.BASE_DIR", self.tmp_dir):
            with self.assertRaises(RuntimeError) as e:
                validate_manifest_pointer_and_target(pointer_data)
            self.assertIn("Symlink detected in selected prompt path", str(e.exception))

        # Cleanup the symlink
        os.remove(os.path.join(run_dir, "alias"))

    def test_startup_recovery_pending_runs(self):
        # Setup pending run in history
        history_dir = os.path.join(self.tmp_dir, "history")
        run_dir = os.path.join(history_dir, "run_pending_crashed")
        os.makedirs(run_dir, exist_ok=True)

        manifest_path = os.path.join(run_dir, "run_manifest.json")
        report_path = os.path.join(run_dir, "operator_report.json")
        prompt_path = os.path.join(run_dir, "inner_agent_best.txt")

        with open(prompt_path, "w") as f:
            f.write("prompt content")
        os.chmod(prompt_path, 0o444)

        manifest_data = {
            "schema_version": "2",
            "run_id": "run_pending_crashed",
            "status": "completed_promoted",
            "approved_commit_sha": "a" * 40,
            "lock_file_hash": "mock",
            "selected_prompt_path": "inner_agent_best.txt",
            "selected_prompt_hash": hashlib.sha256(b"prompt content").hexdigest()
        }
        with open(manifest_path, "w") as f:
            json.dump(manifest_data, f, indent=2)
        os.chmod(manifest_path, 0o444)

        report_data = {
            "schema_version": "2",
            "run_id": "run_pending_crashed",
            "status": "completed_promoted",
            "pointer_commit_status": "pending",
            "lock_contents": {"approved_commit_sha": "a" * 40},
            "lock_file_hash": "mock",
            "tokens": {"cumulative_consumed": 0},
            "manifest_sha256": hashlib.sha256(json.dumps(manifest_data, indent=2).encode('utf-8')).hexdigest()
        }
        with open(report_path, "w") as f:
            json.dump(report_data, f, indent=2)
        os.chmod(report_path, 0o444)

        # Invoke recover_pending_runs
        with mock.patch("rsi_loop.HISTORY_DIR", history_dir):
            recover_pending_runs()

        # Verify it was rolled back to invalid
        self.assertFalse(os.path.exists(prompt_path))
        self.assertFalse(os.path.exists(manifest_path))
        self.assertTrue(os.path.exists(os.path.join(run_dir, "invalid_manifest.json")))
        self.assertTrue(os.path.exists(report_path))

        with open(os.path.join(run_dir, "invalid_manifest.json")) as f:
            inv_m = json.load(f)
        self.assertEqual(inv_m["status"], "invalid")
        self.assertIn("Interrupted/crashed", inv_m["invalidation_reason"])

        with open(report_path) as f:
            inv_r = json.load(f)
        self.assertEqual(inv_r["status"], "invalid")

    def test_promotion_rollback_deletion_failure(self):
        history_dir = os.path.join(self.tmp_dir, "history")
        prev_run_dir = os.path.join(history_dir, "run_prev")
        os.makedirs(prev_run_dir, exist_ok=True)
        prev_prompt_path = os.path.join(prev_run_dir, "inner_agent_best.txt")
        with open(prev_prompt_path, "w") as f:
            f.write("previous best prompt content")
        os.chmod(prev_prompt_path, 0o444)

        prev_manifest = {
            "schema_version": "2",
            "run_id": "run_prev",
            "status": "completed_promoted",
            "approved_commit_sha": "a" * 40,
            "lock_file_hash": "mock_hash",
            "model_name": "gpt-5.6-sol",
            "model_requested": "gpt-5.6-sol",
            "model_resolved": "gpt-5.6-sol",
            "selected_prompt_path": "inner_agent_best.txt",
            "selected_prompt_hash": hashlib.sha256(b"previous best prompt content").hexdigest(),
            "timestamp": "2026-07-20T00:00:00Z"
        }
        prev_manifest_path = os.path.join(prev_run_dir, "run_manifest.json")
        with open(prev_manifest_path, "w") as f:
            json.dump(prev_manifest, f, indent=2)
        os.chmod(prev_manifest_path, 0o444)

        prev_report = {
            "schema_version": "2",
            "status": "completed_promoted",
            "pointer_commit_status": "committed",
            "manifest_sha256": hashlib.sha256(json.dumps(prev_manifest, indent=2).encode('utf-8')).hexdigest()
        }
        with open(os.path.join(prev_run_dir, "operator_report.json"), "w") as f:
            json.dump(prev_report, f, indent=2)
        os.chmod(os.path.join(prev_run_dir, "operator_report.json"), 0o444)

        best_manifest_path = os.path.realpath(os.path.join(self.tmp_dir, "best_manifest.json"))
        pointer_data = {
            "pointer_schema_version": "2",
            "run_manifest_path": "history/run_prev/run_manifest.json",
            "run_manifest_sha256": hashlib.sha256(json.dumps(prev_manifest, indent=2).encode('utf-8')).hexdigest()
        }
        with open(best_manifest_path, "w") as f:
            json.dump(pointer_data, f, indent=2)

        prompt_dir = os.path.join(self.tmp_dir, "prompt_templates")
        os.makedirs(prompt_dir, exist_ok=True)
        with open(os.path.join(prompt_dir, "inner_agent_baseline.txt"), "w") as f:
            f.write("baseline prompt")

        hashes_data = {
            "prompt_templates/inner_agent_baseline.txt": hashlib.sha256(b"baseline prompt").hexdigest(),
            "prompt_templates/meta_optimizer.txt": "dummy",
            "benchmarks/circular_buffer/problem.txt": "dummy",
            "benchmarks/circular_buffer/test_harness.py": "dummy",
            "benchmarks/json_parser/problem.txt": "dummy",
            "benchmarks/json_parser/test_harness.py": "dummy",
            "benchmarks/run_length_encoding/problem.txt": "dummy",
            "benchmarks/run_length_encoding/test_harness.py": "dummy",
            "benchmarks/huffman_coding/problem.txt": "dummy",
            "benchmarks/huffman_coding/test_harness.py": "dummy",
            "benchmarks/validation/string_calculator/problem.txt": "dummy",
            "benchmarks/validation/string_calculator/test_harness.py": "dummy",
            "benchmarks/validation/matrix_operations/problem.txt": "dummy",
            "benchmarks/validation/matrix_operations/test_harness.py": "dummy",
            "candidate.sb.template": "dummy",
            "generator.sb.template": "dummy"
        }
        shutil.copy(os.path.join(BASE_DIR, "candidate.sb.template"), os.path.join(self.tmp_dir, "candidate.sb.template"))
        shutil.copy(os.path.join(BASE_DIR, "generator.sb.template"), os.path.join(self.tmp_dir, "generator.sb.template"))
        with open(os.path.join(self.tmp_dir, "preflight_hashes.json"), "w") as f:
            json.dump(hashes_data, f)

        def fake_verify_lock(ctx):
            ctx.approved_commit_sha = "a" * 40
            ctx.lock_file_hash = "mock_hash"
            return "a" * 40

        # Run E2E setup, mock pointer rename failure and mock os.remove on run_manifest.json to raise OSError
        with mock.patch("rsi_loop.BASE_DIR", os.path.realpath(self.tmp_dir)), \
             mock.patch("rsi_loop.HISTORY_DIR", os.path.realpath(history_dir)), \
             mock.patch("rsi_loop.PROMPTS_DIR", os.path.realpath(os.path.join(self.tmp_dir, "prompts"))), \
             mock.patch("rsi_loop.BEST_MANIFEST_PATH", best_manifest_path), \
             mock.patch("rsi_loop.verify_preflight_hashes"), \
             mock.patch("rsi_loop.verify_experiment_lock", fake_verify_lock), \
             mock.patch("rsi_loop._capture_environment_metadata"), \
             mock.patch("rsi_loop.run_smoke_and_canary", return_value=("codex-1.0", 50)), \
             mock.patch("rsi_loop.call_codex_sandboxed", return_value=("def solution(): pass", 100)), \
             mock.patch("rsi_loop.run_evaluation_on_candidate", return_value={"success": False, "log": "Ran 5 tests"}), \
             mock.patch("rsi_loop._list_optimization_benchmarks", return_value=["circular_buffer"]), \
             mock.patch("rsi_loop._list_validation_benchmarks", return_value=["validation_task"]), \
             mock.patch("rsi_loop.mutate_prompt", return_value=("better prompt", 200)), \
             mock.patch("rsi_loop.evaluate_prompt_on_tasks") as mock_eval:

            mock_eval.side_effect = [
                (0.5, {"circular_buffer": {"success": False, "error": "err"}}, 100), # trial 1 baseline
                (0.8, {}, 100), # trial 1 mut 1
                (0.5, {"circular_buffer": {"success": False, "error": "err"}}, 100), # trial 2 baseline
                (0.8, {}, 100), # trial 2 mut 1
                (0.5, {"circular_buffer": {"success": False, "error": "err"}}, 100), # trial 3 baseline
                (0.8, {}, 100), # trial 3 mut 1
                (0.6, {}, 100), # validation baseline
                (0.9, {}, 100), # validation candidate 1
                (0.9, {}, 100), # validation candidate 2
                (0.9, {}, 100), # validation candidate 3
            ]

            with mock.patch("rsi_loop.subprocess.run") as mock_sub:
                mock_sub.side_effect = [
                    mock.Mock(returncode=0, stdout=""),  # git status
                    mock.Mock(returncode=0, stdout=f"{pointer_data['run_manifest_path']}\n"),  # git remote get-url
                    mock.Mock(returncode=0, stdout="abc\trefs/heads/main\n"),  # git ls-remote
                    mock.Mock(returncode=0, stdout="abc\n"),  # git rev-parse HEAD
                ]
                with mock.patch("rsi_loop.uuid.uuid4") as mock_uuid:
                    mock_uuid.return_value = mock.Mock(hex="fail_del")
                    with mock.patch("rsi_loop.time.time", return_value=12345678):
                        expected_run_id = "run_12345678_fail_del"
                        expected_run_dir = os.path.join(history_dir, expected_run_id)

                        original_replace = os.replace
                        def fake_replace(src, dst):
                            if dst == best_manifest_path:
                                raise OSError("injected pointer failure")
                            original_replace(src, dst)

                        original_remove = os.remove
                        def fake_remove(path):
                            if path.endswith("run_manifest.json"):
                                raise OSError("injected remove failure")
                            original_remove(path)

                        with mock.patch("rsi_loop.os.replace", fake_replace), \
                             mock.patch("rsi_loop.os.remove", fake_remove):
                            with self.assertRaises(SystemExit) as se:
                                main()
                            self.assertEqual(se.exception.code, 1)

                        # Since rollback deletion failed, terminal_state remains "pair_verified"
                        # and no invalid manifest should be written.
                        invalid_path = os.path.join(expected_run_dir, "invalid_manifest.json")
                        self.assertFalse(os.path.exists(invalid_path))
                        # The run_manifest.json was NOT deleted due to the error
                        self.assertTrue(os.path.exists(os.path.join(expected_run_dir, "run_manifest.json")))

    def test_update_operator_report_commit_status_indeterminate_marker_persistence(self):
        # Set up a dummy operator_report.json
        run_dir = os.path.join(self.tmp_dir, "test_indeterminate_marker")
        os.makedirs(run_dir, exist_ok=True)
        report_path = os.path.join(run_dir, "operator_report.json")

        ctx = RunContext()
        ctx.run_dir = run_dir
        ctx.operator_report = {
            "status": "completed_promoted",
            "pointer_commit_status": "pending"
        }

        # Write initial report
        with open(report_path, "w") as f:
            json.dump(ctx.operator_report, f)

        # Update status to indeterminate
        from rsi_loop import _update_operator_report_commit_status
        _update_operator_report_commit_status(ctx, "indeterminate", "injected error detail")

        # Verify the report file has the correct values
        with open(report_path, "r") as f:
            data = json.load(f)

        self.assertEqual(data.get("pointer_commit_status"), "indeterminate")
        self.assertEqual(data.get("pointer_commit_error"), "injected error detail")

        # Verify file mode is 0o444
        st = os.stat(report_path)
        self.assertEqual(st.st_mode & 0o777, 0o444)

    def test_recovery_preserves_committed_run_on_report_update_failure(self):
        history_dir = os.path.join(self.tmp_dir, "history")
        run_dir = os.path.join(history_dir, "run_committed_ok")
        os.makedirs(run_dir, exist_ok=True)

        prompt_path = os.path.join(run_dir, "inner_agent_best.txt")
        with open(prompt_path, "w") as f:
            f.write("content")
        os.chmod(prompt_path, 0o444)

        manifest_data = {
            "schema_version": "2",
            "run_id": "run_committed_ok",
            "status": "completed_promoted",
            "selected_prompt_path": "inner_agent_best.txt",
            "selected_prompt_hash": hashlib.sha256(b"content").hexdigest(),
            "approved_commit_sha": "a" * 40,
            "lock_file_hash": "mock_hash"
        }
        manifest_path = os.path.join(run_dir, "run_manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(manifest_data, f, indent=2)
        os.chmod(manifest_path, 0o444)

        report_data = {
            "schema_version": "2",
            "run_id": "run_committed_ok",
            "status": "completed_promoted",
            "manifest_sha256": hashlib.sha256(json.dumps(manifest_data, indent=2).encode('utf-8')).hexdigest(),
            "pointer_commit_status": "pending",
            "lock_contents": {
                "approved_commit_sha": "a" * 40
            },
            "lock_file_hash": "mock_hash",
            "tokens": {
                "cumulative_consumed": 0
            }
        }
        report_path = os.path.join(run_dir, "operator_report.json")
        with open(report_path, "w") as f:
            json.dump(report_data, f, indent=2)
        os.chmod(report_path, 0o444)

        best_manifest_path = os.path.join(self.tmp_dir, "best_manifest.json")
        pointer_data = {
            "pointer_schema_version": "2",
            "run_manifest_path": "history/run_committed_ok/run_manifest.json",
            "run_manifest_sha256": report_data["manifest_sha256"]
        }
        with open(best_manifest_path, "w") as f:
            json.dump(pointer_data, f, indent=2)

        with mock.patch("rsi_loop.HISTORY_DIR", history_dir), \
             mock.patch("rsi_loop.BASE_DIR", self.tmp_dir), \
             mock.patch("rsi_loop.BEST_MANIFEST_PATH", best_manifest_path):
            recover_pending_runs()

        # Report must be updated to committed
        with open(report_path, "r") as f:
            updated_report = json.load(f)
        self.assertEqual(updated_report.get("pointer_commit_status"), "committed")
        # Manifest and prompt preserved
        self.assertTrue(os.path.exists(manifest_path))
        self.assertTrue(os.path.exists(prompt_path))

    def test_recovery_corrupted_pointer_aborts(self):
        history_dir = os.path.join(self.tmp_dir, "history")
        run_dir = os.path.join(history_dir, "run_corrupt_abort")
        os.makedirs(run_dir, exist_ok=True)

        report_data = {"status": "completed_promoted", "pointer_commit_status": "pending"}
        report_path = os.path.join(run_dir, "operator_report.json")
        with open(report_path, "w") as f:
            json.dump(report_data, f)

        best_manifest_path = os.path.join(self.tmp_dir, "best_manifest.json")
        with open(best_manifest_path, "w") as f:
            f.write("corrupted non-json content")

        with mock.patch("rsi_loop.HISTORY_DIR", history_dir), \
             mock.patch("rsi_loop.BASE_DIR", self.tmp_dir), \
             mock.patch("rsi_loop.BEST_MANIFEST_PATH", best_manifest_path):
            with self.assertRaises(ExperimentInvalidError) as e:
                recover_pending_runs()
            self.assertIn("corrupted, malformed, or unsafe", str(e.exception))

    def test_recovery_pending_with_matching_pointer_but_invalid_prompt_aborts(self):
        history_dir = os.path.join(self.tmp_dir, "history")
        run_dir = os.path.join(history_dir, "run_invalid_prompt")
        os.makedirs(run_dir, exist_ok=True)

        # Mismatch hash in manifest
        manifest_data = {
            "schema_version": "2",
            "run_id": "run_invalid_prompt",
            "status": "completed_promoted",
            "selected_prompt_path": "inner_agent_best.txt",
            "selected_prompt_hash": "wrong_hash",
            "approved_commit_sha": "a" * 40,
            "lock_file_hash": "mock_hash"
        }
        manifest_path = os.path.join(run_dir, "run_manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(manifest_data, f, indent=2)
        os.chmod(manifest_path, 0o444)

        prompt_path = os.path.join(run_dir, "inner_agent_best.txt")
        with open(prompt_path, "w") as f:
            f.write("content")
        os.chmod(prompt_path, 0o444)

        report_data = {
            "schema_version": "2",
            "run_id": "run_invalid_prompt",
            "status": "completed_promoted",
            "manifest_sha256": hashlib.sha256(json.dumps(manifest_data, indent=2).encode('utf-8')).hexdigest(),
            "pointer_commit_status": "pending",
            "lock_contents": {
                "approved_commit_sha": "a" * 40
            },
            "lock_file_hash": "mock_hash",
            "tokens": {
                "cumulative_consumed": 0
            }
        }
        report_path = os.path.join(run_dir, "operator_report.json")
        with open(report_path, "w") as f:
            json.dump(report_data, f, indent=2)
        os.chmod(report_path, 0o444)

        best_manifest_path = os.path.join(self.tmp_dir, "best_manifest.json")
        pointer_data = {
            "pointer_schema_version": "2",
            "run_manifest_path": "history/run_invalid_prompt/run_manifest.json",
            "run_manifest_sha256": report_data["manifest_sha256"]
        }
        with open(best_manifest_path, "w") as f:
            json.dump(pointer_data, f, indent=2)

        with mock.patch("rsi_loop.HISTORY_DIR", history_dir), \
             mock.patch("rsi_loop.BASE_DIR", self.tmp_dir), \
             mock.patch("rsi_loop.BEST_MANIFEST_PATH", best_manifest_path):
            with self.assertRaises(ExperimentInvalidError) as e:
                recover_pending_runs()
            self.assertIn("Selected prompt hash mismatch", str(e.exception))

    def test_recovery_non_promoted_not_applicable_survives(self):
        history_dir = os.path.join(self.tmp_dir, "history")
        run_dir = os.path.join(history_dir, "run_not_app")
        os.makedirs(run_dir, exist_ok=True)

        manifest_path = os.path.join(run_dir, "run_manifest.json")
        with open(manifest_path, "w") as f:
            json.dump({"status": "completed_no_improvement"}, f)

        report_path = os.path.join(run_dir, "operator_report.json")
        report_data = {
            "status": "completed_no_improvement",
            "pointer_commit_status": "not_applicable"
        }
        with open(report_path, "w") as f:
            json.dump(report_data, f)

        with mock.patch("rsi_loop.HISTORY_DIR", history_dir), \
             mock.patch("rsi_loop.BASE_DIR", self.tmp_dir):
            recover_pending_runs()

        # Should be ignored and survive intact
        with open(report_path, "r") as f:
            data = json.load(f)
        self.assertEqual(data.get("pointer_commit_status"), "not_applicable")

    def test_recovery_idempotence(self):
        history_dir = os.path.join(self.tmp_dir, "history")
        run_dir = os.path.join(history_dir, "run_idempotent")
        os.makedirs(run_dir, exist_ok=True)

        prompt_path = os.path.join(run_dir, "inner_agent_best.txt")
        with open(prompt_path, "w") as f:
            f.write("content")
        os.chmod(prompt_path, 0o444)

        manifest_path = os.path.join(run_dir, "run_manifest.json")
        manifest_data = {
            "schema_version": "2",
            "run_id": "run_idempotent",
            "status": "completed_promoted",
            "approved_commit_sha": "a" * 40,
            "lock_file_hash": "mock_hash",
            "selected_prompt_path": "inner_agent_best.txt",
            "selected_prompt_hash": hashlib.sha256(b"content").hexdigest()
        }
        with open(manifest_path, "w") as f:
            json.dump(manifest_data, f, indent=2)
        os.chmod(manifest_path, 0o444)

        report_path = os.path.join(run_dir, "operator_report.json")
        report_data = {
            "schema_version": "2",
            "run_id": "run_idempotent",
            "status": "completed_promoted",
            "pointer_commit_status": "pending",
            "lock_contents": {
                "approved_commit_sha": "a" * 40
            },
            "lock_file_hash": "mock_hash",
            "tokens": {
                "cumulative_consumed": 0
            },
            "manifest_sha256": hashlib.sha256(json.dumps(manifest_data, indent=2).encode('utf-8')).hexdigest()
        }
        with open(report_path, "w") as f:
            json.dump(report_data, f, indent=2)
        os.chmod(report_path, 0o444)

        best_manifest_path = os.path.join(self.tmp_dir, "best_manifest.json")
        if os.path.exists(best_manifest_path):
            os.remove(best_manifest_path)

        with mock.patch("rsi_loop.HISTORY_DIR", history_dir), \
             mock.patch("rsi_loop.BASE_DIR", self.tmp_dir), \
             mock.patch("rsi_loop.BEST_MANIFEST_PATH", best_manifest_path):

            # First recovery run: invalidates and cleans up
            recover_pending_runs()

            # Verify invalidated
            self.assertFalse(os.path.exists(prompt_path))
            self.assertFalse(os.path.exists(manifest_path))
            self.assertTrue(os.path.exists(os.path.join(run_dir, "invalid_manifest.json")))

            # Second recovery run: must be idempotent
            recover_pending_runs()
            self.assertTrue(os.path.exists(os.path.join(run_dir, "invalid_manifest.json")))

    def test_recovery_deletion_failure_preserves_evidence(self):
        history_dir = os.path.join(self.tmp_dir, "history")
        run_dir = os.path.join(history_dir, "run_del_fail")
        os.makedirs(run_dir, exist_ok=True)

        prompt_path = os.path.join(run_dir, "inner_agent_best.txt")
        with open(prompt_path, "w") as f:
            f.write("prompt content")
        os.chmod(prompt_path, 0o444)

        manifest_data = {
            "schema_version": "2",
            "run_id": "run_del_fail",
            "status": "completed_promoted",
            "approved_commit_sha": "a" * 40,
            "lock_file_hash": "mock",
            "selected_prompt_path": "inner_agent_best.txt",
            "selected_prompt_hash": hashlib.sha256(b"prompt content").hexdigest()
        }
        manifest_path = os.path.join(run_dir, "run_manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(manifest_data, f, indent=2)
        os.chmod(manifest_path, 0o444)

        report_path = os.path.join(run_dir, "operator_report.json")
        report_data = {
            "schema_version": "2",
            "run_id": "run_del_fail",
            "status": "completed_promoted",
            "pointer_commit_status": "pending",
            "lock_contents": {"approved_commit_sha": "a" * 40},
            "lock_file_hash": "mock",
            "tokens": {"cumulative_consumed": 0},
            "manifest_sha256": hashlib.sha256(json.dumps(manifest_data, indent=2).encode('utf-8')).hexdigest()
        }
        with open(report_path, "w") as f:
            json.dump(report_data, f, indent=2)
        os.chmod(report_path, 0o444)

        original_remove = os.remove
        def fake_remove(path):
            if path.endswith("inner_agent_best.txt") or path.endswith("run_manifest.json"):
                raise OSError("injected remove failure")
            original_remove(path)

        with mock.patch("rsi_loop.HISTORY_DIR", history_dir), \
             mock.patch("rsi_loop.BASE_DIR", self.tmp_dir), \
             mock.patch("rsi_loop.os.remove", fake_remove):
            with self.assertRaises(ExperimentInvalidError) as e:
                recover_pending_runs()
            self.assertIn("Failed to delete false promotion artifact", str(e.exception))
            # Manifest is preserved due to deletion failure
            self.assertTrue(os.path.exists(manifest_path))

    def test_recovery_symlink_aborts(self):
        history_dir = os.path.join(self.tmp_dir, "history")
        os.makedirs(history_dir, exist_ok=True)
        run_dir = os.path.join(history_dir, "run_symlink")

        # Create external directory
        external_dir = os.path.join(self.tmp_dir, "external_run")
        os.makedirs(external_dir, exist_ok=True)
        # Create symlink run_symlink -> external_run
        os.symlink(external_dir, run_dir)

        with mock.patch("rsi_loop.HISTORY_DIR", history_dir), \
             mock.patch("rsi_loop.BASE_DIR", self.tmp_dir):
            with self.assertRaises(ExperimentInvalidError) as e:
                recover_pending_runs()
            self.assertIn("Symlink detected", str(e.exception))

        os.remove(run_dir)

    def test_recovery_not_invoked_on_authorization_failure(self):
        ctx = RunContext()
        ctx.allow_no_holdout = True

        with mock.patch("rsi_loop.verify_experiment_lock", side_effect=ExperimentInvalidError("lock failure")), \
             mock.patch("rsi_loop.recover_pending_runs") as mock_rec:
            with self.assertRaises(ExperimentInvalidError):
                _main_exec(ctx)
            # Recovery must NOT be called on auth failure
            mock_rec.assert_not_called()

    def test_recovery_legacy_dir_skipped(self):
        history_dir = os.path.join(self.tmp_dir, "history")
        legacy_dir = os.path.join(history_dir, "legacy")
        os.makedirs(legacy_dir, exist_ok=True)

        with mock.patch("rsi_loop.HISTORY_DIR", history_dir), \
             mock.patch("rsi_loop.BASE_DIR", self.tmp_dir), \
             mock.patch("rsi_loop.os.path.isdir", return_value=True):
            # Recovery should complete without attempting to process the legacy directory
            recover_pending_runs()

    def test_recovery_multiple_pending_runs_deterministic(self):
        history_dir = os.path.join(self.tmp_dir, "history")

        # Run 1: matching pointer
        run1_dir = os.path.join(history_dir, "run_matching")
        os.makedirs(run1_dir, exist_ok=True)
        manifest1_path = os.path.join(run1_dir, "run_manifest.json")
        with open(manifest1_path, "w") as f:
            json.dump({"schema_version": "2", "run_id": "run_matching", "status": "completed_promoted", "approved_commit_sha": "a" * 40, "lock_file_hash": "mock", "selected_prompt_path": "inner_agent_best.txt", "selected_prompt_hash": hashlib.sha256(b"p").hexdigest()}, f, indent=2)
        os.chmod(manifest1_path, 0o444)
        prompt1_path = os.path.join(run1_dir, "inner_agent_best.txt")
        with open(prompt1_path, "w") as f:
            f.write("p")
        os.chmod(prompt1_path, 0o444)
        report1_path = os.path.join(run1_dir, "operator_report.json")
        with open(report1_path, "w") as f:
            json.dump({"schema_version": "2", "run_id": "run_matching", "status": "completed_promoted", "manifest_sha256": hashlib.sha256(json.dumps({"schema_version": "2", "run_id": "run_matching", "status": "completed_promoted", "approved_commit_sha": "a" * 40, "lock_file_hash": "mock", "selected_prompt_path": "inner_agent_best.txt", "selected_prompt_hash": hashlib.sha256(b"p").hexdigest()}, indent=2).encode('utf-8')).hexdigest(), "pointer_commit_status": "pending", "lock_contents": {"approved_commit_sha": "a" * 40}, "lock_file_hash": "mock", "tokens": {"cumulative_consumed": 0}}, f, indent=2)
        os.chmod(report1_path, 0o444)

        # Run 2: other non-matching pointer
        run2_dir = os.path.join(history_dir, "run_other")
        os.makedirs(run2_dir, exist_ok=True)
        prompt2_path = os.path.join(run2_dir, "inner_agent_best.txt")
        with open(prompt2_path, "w") as f:
            f.write("other prompt")
        os.chmod(prompt2_path, 0o444)

        manifest2_data = {
            "schema_version": "2",
            "run_id": "run_other",
            "status": "completed_promoted",
            "approved_commit_sha": "a" * 40,
            "lock_file_hash": "mock",
            "selected_prompt_path": "inner_agent_best.txt",
            "selected_prompt_hash": hashlib.sha256(b"other prompt").hexdigest()
        }
        manifest2_path = os.path.join(run2_dir, "run_manifest.json")
        with open(manifest2_path, "w") as f:
            json.dump(manifest2_data, f, indent=2)
        os.chmod(manifest2_path, 0o444)

        report2_path = os.path.join(run2_dir, "operator_report.json")
        report2_data = {
            "schema_version": "2",
            "run_id": "run_other",
            "status": "completed_promoted",
            "pointer_commit_status": "pending",
            "lock_contents": {"approved_commit_sha": "a" * 40},
            "lock_file_hash": "mock",
            "tokens": {"cumulative_consumed": 0},
            "manifest_sha256": hashlib.sha256(json.dumps(manifest2_data, indent=2).encode('utf-8')).hexdigest()
        }
        with open(report2_path, "w") as f:
            json.dump(report2_data, f, indent=2)
        os.chmod(report2_path, 0o444)

        best_manifest_path = os.path.join(self.tmp_dir, "best_manifest.json")
        pointer_data = {
            "pointer_schema_version": "2",
            "run_manifest_path": "history/run_matching/run_manifest.json",
            "run_manifest_sha256": hashlib.sha256(json.dumps({"schema_version": "2", "run_id": "run_matching", "status": "completed_promoted", "approved_commit_sha": "a" * 40, "lock_file_hash": "mock", "selected_prompt_path": "inner_agent_best.txt", "selected_prompt_hash": hashlib.sha256(b"p").hexdigest()}, indent=2).encode('utf-8')).hexdigest()
        }
        with open(best_manifest_path, "w") as f:
            json.dump(pointer_data, f, indent=2)

        with mock.patch("rsi_loop.HISTORY_DIR", history_dir), \
             mock.patch("rsi_loop.BASE_DIR", self.tmp_dir), \
             mock.patch("rsi_loop.BEST_MANIFEST_PATH", best_manifest_path):
            recover_pending_runs()

        # Run 1 must reconcile to committed
        with open(report1_path, "r") as f:
            r1 = json.load(f)
        self.assertEqual(r1.get("pointer_commit_status"), "committed")

        # Run 2 must be invalidated
        self.assertTrue(os.path.exists(os.path.join(run2_dir, "invalid_manifest.json")))

    def test_recovered_invalid_schema_conformance(self):
        history_dir = os.path.join(self.tmp_dir, "history")
        run_dir = os.path.join(history_dir, "run_schema")
        os.makedirs(run_dir, exist_ok=True)

        prompt_path = os.path.join(run_dir, "inner_agent_best.txt")
        with open(prompt_path, "w") as f:
            f.write("prompt content")
        os.chmod(prompt_path, 0o444)

        manifest_data = {
            "schema_version": "2",
            "run_id": "run_schema",
            "status": "completed_promoted",
            "approved_commit_sha": "a" * 40,
            "lock_file_hash": "mock",
            "selected_prompt_path": "inner_agent_best.txt",
            "selected_prompt_hash": hashlib.sha256(b"prompt content").hexdigest()
        }
        manifest_path = os.path.join(run_dir, "run_manifest.json")
        with open(manifest_path, "w") as f:
            json.dump(manifest_data, f, indent=2)
        os.chmod(manifest_path, 0o444)

        report_data = {
            "schema_version": "2",
            "run_id": "run_schema",
            "status": "completed_promoted",
            "pointer_commit_status": "pending",
            "lock_contents": {"approved_commit_sha": "a" * 40},
            "lock_file_hash": "mock",
            "tokens": {"cumulative_consumed": 0},
            "manifest_sha256": hashlib.sha256(json.dumps(manifest_data, indent=2).encode('utf-8')).hexdigest()
        }
        report_path = os.path.join(run_dir, "operator_report.json")
        with open(report_path, "w") as f:
            json.dump(report_data, f, indent=2)
        os.chmod(report_path, 0o444)

        with mock.patch("rsi_loop.HISTORY_DIR", history_dir), \
             mock.patch("rsi_loop.BASE_DIR", self.tmp_dir):
            recover_pending_runs()

        invalid_manifest_path = os.path.join(run_dir, "invalid_manifest.json")
        self.assertTrue(os.path.exists(invalid_manifest_path))

        with open(invalid_manifest_path, "r") as f:
            m = json.load(f)
        self.assertEqual(m.get("schema_version"), "2")
        self.assertEqual(m.get("status"), "invalid")
        self.assertEqual(m.get("approved_commit_sha"), "a" * 40)

        with open(report_path, "r") as f:
            r = json.load(f)
        self.assertEqual(r.get("status"), "invalid")
        self.assertEqual(r.get("pointer_commit_status"), "not_applicable")
        self.assertEqual(r.get("manifest_sha256"), hashlib.sha256(json.dumps(m, indent=2).encode('utf-8')).hexdigest())

        # Verify modes are 0o444
        self.assertEqual(os.stat(invalid_manifest_path).st_mode & 0o777, 0o444)
        self.assertEqual(os.stat(report_path).st_mode & 0o777, 0o444)

    def test_promotion_post_commit_failure_preserves_target(self):
        # We simulate the post-commit status update failure in completed_promoted path
        ctx = RunContext()
        ctx.run_id = "run_post_commit_fail"
        ctx.run_dir = os.path.join(self.tmp_dir, "history", ctx.run_id)
        os.makedirs(ctx.run_dir, exist_ok=True)
        ctx.approved_commit_sha = "a" * 40
        ctx.lock_file_hash = "mock"
        ctx.operator_report = {
            "schema_version": "2",
            "run_id": ctx.run_id,
            "status": "staging",
            "lock_contents": {"approved_commit_sha": ctx.approved_commit_sha},
            "lock_file_hash": ctx.lock_file_hash,
            "tokens": {"cumulative_consumed": 0}
        }

        run_manifest_path = os.path.join(ctx.run_dir, "run_manifest.json")
        best_manifest_path = os.path.join(self.tmp_dir, "best_manifest.json")

        # Mock _update_operator_report_commit_status to fail
        def fake_update(c, status, error_msg=None):
            raise OSError("injected post-commit failure")

        from rsi_loop import publish_terminal_manifest_and_report, update_best_manifest_pointer

        promoted_manifest = {
            "schema_version": "2",
            "run_id": ctx.run_id,
            "status": "completed_promoted",
            "approved_commit_sha": ctx.approved_commit_sha,
            "lock_file_hash": ctx.lock_file_hash
        }

        run_best_prompt_path = os.path.join(ctx.run_dir, "inner_agent_best.txt")
        with open(run_best_prompt_path, "w") as f:
            f.write("best prompt")

        # Write initial report so publish_terminal_manifest_and_report can read/write it
        report_path = os.path.join(ctx.run_dir, "operator_report.json")
        with open(report_path, "w") as f:
            json.dump(ctx.operator_report, f)

        # Call the promotion sequence with mock patches
        with mock.patch("rsi_loop.BEST_MANIFEST_PATH", best_manifest_path), \
             mock.patch("rsi_loop._update_operator_report_commit_status", side_effect=fake_update):

            publish_terminal_manifest_and_report(ctx, run_manifest_path, promoted_manifest, pointer_commit_status="pending")

            # Replicate the split try-catch execution:
            try:
                update_best_manifest_pointer(run_manifest_path)
            except Exception as e:
                # Should not fail pre-commit
                self.fail(f"Pre-commit failed unexpectedly: {e}")

            with self.assertRaises(OSError) as oe:
                try:
                    from rsi_loop import _update_operator_report_commit_status
                    _update_operator_report_commit_status(ctx, "committed")
                except Exception as e:
                    sys.stderr.write(f"WARNING: Post-commit report status update failed: {e}\n")
                    raise
            self.assertIn("injected post-commit failure", str(oe.exception))

            # Verify pointer committed successfully
            self.assertTrue(os.path.exists(best_manifest_path))
            # Verify run manifest and best prompt were NOT deleted
            self.assertTrue(os.path.exists(run_manifest_path))
            self.assertTrue(os.path.exists(run_best_prompt_path))

    def test_report_symlink_rejected_unresolved(self):
        ctx = RunContext()
        ctx.run_dir = os.path.join(self.tmp_dir, "run_sym_report")
        os.makedirs(ctx.run_dir, exist_ok=True)

        # Create external file
        external_file = os.path.join(self.tmp_dir, "external_report.json")
        with open(external_file, "w") as f:
            json.dump({"status": "original"}, f)

        # Create report symlink pointing to external_file
        report_path = os.path.join(ctx.run_dir, "operator_report.json")
        os.symlink(external_file, report_path)

        from rsi_loop import _update_operator_report_commit_status
        with self.assertRaises(ExperimentInvalidError) as e:
            _update_operator_report_commit_status(ctx, "committed")
        self.assertIn("Symlink detected at operator report path", str(e.exception))

        # Verify external file has NOT been modified
        with open(external_file, "r") as f:
            data = json.load(f)
        self.assertEqual(data.get("status"), "original")

        os.remove(report_path)

    def test_malformed_invalid_marker_aborts(self):
        history_dir = os.path.join(self.tmp_dir, "history")
        run_dir = os.path.join(history_dir, "run_malformed_marker")
        os.makedirs(run_dir, exist_ok=True)

        # Setup run files
        manifest_path = os.path.join(run_dir, "run_manifest.json")
        with open(manifest_path, "w") as f:
            f.write("content")
        prompt_path = os.path.join(run_dir, "inner_agent_best.txt")
        with open(prompt_path, "w") as f:
            f.write("prompt")

        # Create empty invalid manifest (malformed)
        invalid_path = os.path.join(run_dir, "invalid_manifest.json")
        with open(invalid_path, "w") as f:
            json.dump({}, f)
        os.chmod(invalid_path, 0o444)

        with mock.patch("rsi_loop.HISTORY_DIR", history_dir), \
             mock.patch("rsi_loop.BASE_DIR", self.tmp_dir):
            with self.assertRaises(ExperimentInvalidError) as e:
                recover_pending_runs()
            self.assertIn("Untrusted or malformed invalid marker", str(e.exception))

            # Verify no files were deleted
            self.assertTrue(os.path.exists(manifest_path))
            self.assertTrue(os.path.exists(prompt_path))

    def test_wrong_pointer_schema_aborts(self):
        history_dir = os.path.join(self.tmp_dir, "history")
        run_dir = os.path.join(history_dir, "run_pending_ok")
        os.makedirs(run_dir, exist_ok=True)

        report_path = os.path.join(run_dir, "operator_report.json")
        with open(report_path, "w") as f:
            json.dump({"schema_version": "2", "status": "completed_promoted", "pointer_commit_status": "pending"}, f)

        # Write wrong pointer schema
        best_manifest_path = os.path.join(self.tmp_dir, "best_manifest.json")
        with open(best_manifest_path, "w") as f:
            json.dump({"pointer_schema_version": "1"}, f)

        with mock.patch("rsi_loop.HISTORY_DIR", history_dir), \
             mock.patch("rsi_loop.BASE_DIR", self.tmp_dir), \
             mock.patch("rsi_loop.BEST_MANIFEST_PATH", best_manifest_path):
            with self.assertRaises(ExperimentInvalidError) as e:
                recover_pending_runs()
            self.assertIn("schema version mismatch", str(e.exception))
            # Verify no modifications occurred
            with open(report_path, "r") as f:
                rep = json.load(f)
            self.assertEqual(rep.get("pointer_commit_status"), "pending")

    def test_any_pending_ambiguity_aborts_all(self):
        history_dir = os.path.join(self.tmp_dir, "history")

        # Run A: valid uncommitted pending
        run_a_dir = os.path.join(history_dir, "run_a")
        os.makedirs(run_a_dir, exist_ok=True)
        manifest_a = os.path.join(run_a_dir, "run_manifest.json")
        with open(manifest_a, "w") as f:
            json.dump({"schema_version": "2", "run_id": "run_a", "status": "completed_promoted", "approved_commit_sha": "a"*40, "lock_file_hash": "mock"}, f)
        report_a = os.path.join(run_a_dir, "operator_report.json")
        with open(report_a, "w") as f:
            json.dump({"schema_version": "2", "status": "completed_promoted", "pointer_commit_status": "pending", "lock_contents": {"approved_commit_sha": "a"*40}, "lock_file_hash": "mock", "tokens": {"cumulative_consumed": 0}}, f)

        # Run B: pending but has a symlinked manifest (malformed/ambiguous)
        run_b_dir = os.path.join(history_dir, "run_b")
        os.makedirs(run_b_dir, exist_ok=True)
        manifest_b = os.path.join(run_b_dir, "run_manifest.json")
        os.symlink(manifest_a, manifest_b)
        report_b = os.path.join(run_b_dir, "operator_report.json")
        with open(report_b, "w") as f:
            json.dump({"schema_version": "2", "status": "completed_promoted", "pointer_commit_status": "pending", "lock_contents": {"approved_commit_sha": "a"*40}, "lock_file_hash": "mock", "tokens": {"cumulative_consumed": 0}}, f)

        with mock.patch("rsi_loop.HISTORY_DIR", history_dir), \
             mock.patch("rsi_loop.BASE_DIR", self.tmp_dir):
            with self.assertRaises(ExperimentInvalidError) as e:
                recover_pending_runs()
            self.assertIn("Symlink detected", str(e.exception))

            # Verify Run A was NOT mutated (not invalidated, invalid_manifest.json not written)
            self.assertFalse(os.path.exists(os.path.join(run_a_dir, "invalid_manifest.json")))
            with open(report_a, "r") as f:
                rep_a_data = json.load(f)
            self.assertEqual(rep_a_data.get("pointer_commit_status"), "pending")

        os.remove(manifest_b)

    def test_recovery_corrupted_authoritative_target_aborts_without_mutation(self):
        history_dir = os.path.join(self.tmp_dir, "history")
        os.makedirs(history_dir, exist_ok=True)

        # Valid authoritative pointer
        best_manifest_path = os.path.join(self.tmp_dir, "best_manifest.json")
        pointer_data = {
            "pointer_schema_version": "2",
            "run_manifest_path": "history/run_missing/run_manifest.json",
            "run_manifest_sha256": "a" * 64
        }
        with open(best_manifest_path, "w") as f:
            json.dump(pointer_data, f, indent=2)

        # A valid pending run in history
        run_pending_dir = os.path.join(history_dir, "run_pending_ok")
        os.makedirs(run_pending_dir, exist_ok=True)

        manifest_path = os.path.join(run_pending_dir, "run_manifest.json")
        with open(manifest_path, "w") as f:
            json.dump({
                "schema_version": "2",
                "run_id": "run_pending_ok",
                "status": "completed_promoted",
                "approved_commit_sha": "a" * 40,
                "lock_file_hash": "mock",
                "selected_prompt_path": "inner_agent_best.txt",
                "selected_prompt_hash": hashlib.sha256(b"prompt").hexdigest()
            }, f, indent=2)
        os.chmod(manifest_path, 0o444)

        prompt_path = os.path.join(run_pending_dir, "inner_agent_best.txt")
        with open(prompt_path, "w") as f:
            f.write("prompt")
        os.chmod(prompt_path, 0o444)

        with open(manifest_path, "rb") as mf:
            m_sha = hashlib.sha256(mf.read()).hexdigest()

        report_path = os.path.join(run_pending_dir, "operator_report.json")
        with open(report_path, "w") as f:
            json.dump({
                "schema_version": "2",
                "run_id": "run_pending_ok",
                "status": "completed_promoted",
                "pointer_commit_status": "pending",
                "lock_contents": {"approved_commit_sha": "a" * 40},
                "lock_file_hash": "mock",
                "tokens": {"cumulative_consumed": 0},
                "manifest_sha256": m_sha
            }, f, indent=2)
        os.chmod(report_path, 0o444)

        with mock.patch("rsi_loop.HISTORY_DIR", history_dir), \
             mock.patch("rsi_loop.BASE_DIR", self.tmp_dir), \
             mock.patch("rsi_loop.BEST_MANIFEST_PATH", best_manifest_path):
            with self.assertRaises(ExperimentInvalidError) as e:
                recover_pending_runs()
            self.assertIn("Authoritative pointer targets a corrupted, malformed, or missing run manifest", str(e.exception))

            # Verify the pending run was NOT mutated/deleted
            self.assertTrue(os.path.exists(manifest_path))
            self.assertTrue(os.path.exists(prompt_path))

    def test_recovery_unauthenticated_pending_report_aborts(self):
        history_dir = os.path.join(self.tmp_dir, "history")
        os.makedirs(history_dir, exist_ok=True)

        best_manifest_path = os.path.join(self.tmp_dir, "best_manifest.json")
        if os.path.exists(best_manifest_path):
            os.remove(best_manifest_path)

        run_pending_dir = os.path.join(history_dir, "run_forged")
        os.makedirs(run_pending_dir, exist_ok=True)

        # Forge report containing only status = pending, but schema version is "1.0" or wrong mode
        report_path = os.path.join(run_pending_dir, "operator_report.json")
        with open(report_path, "w") as f:
            json.dump({
                "schema_version": "1.0",
                "pointer_commit_status": "pending"
            }, f, indent=2)
        # Mode not 0o444 (e.g. 0o600 or 0o644)
        os.chmod(report_path, 0o600)

        manifest_path = os.path.join(run_pending_dir, "run_manifest.json")
        with open(manifest_path, "w") as f:
            json.dump({"schema_version": "2", "run_id": "run_forged"}, f)

        with mock.patch("rsi_loop.HISTORY_DIR", history_dir), \
             mock.patch("rsi_loop.BASE_DIR", self.tmp_dir), \
             mock.patch("rsi_loop.BEST_MANIFEST_PATH", best_manifest_path):
            with self.assertRaises(ExperimentInvalidError) as e:
                recover_pending_runs()
            self.assertIn("Operator report file mode is not 0o444", str(e.exception))

            # Verify no files were deleted
            self.assertTrue(os.path.exists(manifest_path))

    def test_report_sealed_before_replacement(self):
        ctx = RunContext()
        ctx.run_dir = os.path.join(self.tmp_dir, "run_seal_test")
        os.makedirs(ctx.run_dir, exist_ok=True)

        report_path = os.path.join(ctx.run_dir, "operator_report.json")
        with open(report_path, "w") as f:
            json.dump({"schema_version": "2", "run_id": "run_seal_test", "status": "staging"}, f)
        os.chmod(report_path, 0o444)

        ctx.operator_report = {"schema_version": "2", "run_id": "run_seal_test", "status": "staging"}

        # We hook into os.replace to check the mode of the temporary file before it is replaced.
        # It must be 0o444.
        original_replace = os.replace
        verified_mode = [False]
        def fake_replace(src, dst):
            if src.endswith("operator_report.json.tmp"):
                st = os.stat(src)
                if (st.st_mode & 0o777) == 0o444:
                    verified_mode[0] = True
            original_replace(src, dst)

        with mock.patch("rsi_loop.os.replace", fake_replace):
            from rsi_loop import _update_operator_report_commit_status
            _update_operator_report_commit_status(ctx, "committed")

        self.assertTrue(verified_mode[0])
        # Also check the final on-disk mode
        self.assertEqual(os.stat(report_path).st_mode & 0o777, 0o444)

    def test_dangling_symlink_authoritative_pointer_rejected(self):
        best_manifest_path = os.path.join(self.tmp_dir, "best_manifest.json")
        if os.path.exists(best_manifest_path) or os.path.lexists(best_manifest_path):
            os.remove(best_manifest_path)

        # Create a dangling symlink pointing to non_existent.json
        os.symlink("non_existent.json", best_manifest_path)

        from rsi_loop import _read_authoritative_pointer
        with mock.patch("rsi_loop.BEST_MANIFEST_PATH", best_manifest_path):
            with self.assertRaises(ExperimentInvalidError) as e:
                _read_authoritative_pointer()
            self.assertIn("Symlink detected at pointer path", str(e.exception))

        os.remove(best_manifest_path)

    def test_production_no_headroom_post_commit_failure(self):
        run_id = "test_run_no_headroom_fail"
        run_dir = os.path.join(self.tmp_dir, "history", run_id)
        os.makedirs(run_dir, exist_ok=True)

        lock_data = {
            "approved_commit_sha": "a" * 40,
            "remote_url": "https://github.com/hehamalainen/RSILoop.git",
            "model": "gpt-5.6-sol",
            "allow_no_holdout": False
        }
        lock_path = os.path.join(self.tmp_dir, "experiment.lock")
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)

        prompt_dir = os.path.join(self.tmp_dir, "prompt_templates")
        os.makedirs(prompt_dir, exist_ok=True)
        with open(os.path.join(prompt_dir, "inner_agent_baseline.txt"), "w") as f:
            f.write("baseline prompt")

        hashes_data = {
            "prompt_templates/inner_agent_baseline.txt": hashlib.sha256(b"baseline prompt").hexdigest(),
            "prompt_templates/meta_optimizer.txt": "dummy",
            "benchmarks/circular_buffer/problem.txt": "dummy",
            "benchmarks/circular_buffer/test_harness.py": "dummy",
            "benchmarks/json_parser/problem.txt": "dummy",
            "benchmarks/json_parser/test_harness.py": "dummy",
            "benchmarks/run_length_encoding/problem.txt": "dummy",
            "benchmarks/run_length_encoding/test_harness.py": "dummy",
            "benchmarks/huffman_coding/problem.txt": "dummy",
            "benchmarks/huffman_coding/test_harness.py": "dummy",
            "benchmarks/validation/string_calculator/problem.txt": "dummy",
            "benchmarks/validation/string_calculator/test_harness.py": "dummy",
            "benchmarks/validation/matrix_operations/problem.txt": "dummy",
            "benchmarks/validation/matrix_operations/test_harness.py": "dummy",
            "candidate.sb.template": "dummy",
            "generator.sb.template": "dummy"
        }
        shutil.copy(os.path.join(BASE_DIR, "candidate.sb.template"), os.path.join(self.tmp_dir, "candidate.sb.template"))
        shutil.copy(os.path.join(BASE_DIR, "generator.sb.template"), os.path.join(self.tmp_dir, "generator.sb.template"))
        with open(os.path.join(self.tmp_dir, "preflight_hashes.json"), "w") as f:
            json.dump(hashes_data, f)

        best_manifest_path = os.path.join(self.tmp_dir, "best_manifest.json")
        if os.path.exists(best_manifest_path):
            os.remove(best_manifest_path)

        def fake_update_status(ctx, status, error_msg=None):
            raise OSError("injected status update failure")

        with mock.patch("rsi_loop.BASE_DIR", self.tmp_dir), \
             mock.patch("rsi_loop.HISTORY_DIR", os.path.join(self.tmp_dir, "history")), \
             mock.patch("rsi_loop.PROMPTS_DIR", os.path.join(self.tmp_dir, "prompts")), \
             mock.patch("rsi_loop.BEST_MANIFEST_PATH", best_manifest_path), \
             mock.patch("rsi_loop.verify_preflight_hashes"), \
             mock.patch("rsi_loop._capture_environment_metadata"), \
             mock.patch("rsi_loop.run_smoke_and_canary", return_value=("codex-1.0", 50)), \
             mock.patch("rsi_loop.call_codex_sandboxed", return_value=("def solution(): pass", 100)), \
             mock.patch("rsi_loop.run_evaluation_on_candidate", return_value={"success": True, "log": "Ran 5 tests"}), \
             mock.patch("rsi_loop._update_operator_report_commit_status", fake_update_status):

            with mock.patch("rsi_loop.subprocess.run") as mock_sub:
                mock_sub.side_effect = [
                    mock.Mock(returncode=0, stdout=""),  # git status
                    mock.Mock(returncode=0, stdout=f"{lock_data['remote_url']}\n"),  # git remote get-url
                    mock.Mock(returncode=0, stdout=f"{lock_data['approved_commit_sha']}\trefs/heads/main\n"),  # git ls-remote
                    mock.Mock(returncode=0, stdout=f"{lock_data['approved_commit_sha']}\n"),  # git rev-parse HEAD
                ]
                with mock.patch("rsi_loop.uuid.uuid4") as mock_uuid:
                    mock_uuid.return_value = mock.Mock(hex=run_id.split("_")[-1])
                    with mock.patch("rsi_loop.time.time", return_value=12345678):
                        expected_run_id = f"run_12345678_{run_id.split('_')[-1]}"
                        expected_run_dir = os.path.join(self.tmp_dir, "history", expected_run_id)

                        with self.assertRaises(SystemExit) as se:
                            main()
                        # Should exit 1 due to the injected post-commit status failure
                        self.assertEqual(se.exception.code, 1)

                        # Validate pointer replacement succeeded
                        self.assertTrue(os.path.exists(best_manifest_path))
                        # Validate new run manifest/report/prompt files were NOT deleted
                        self.assertTrue(os.path.exists(os.path.join(expected_run_dir, "run_manifest.json")))
                        self.assertTrue(os.path.exists(os.path.join(expected_run_dir, "inner_agent_best.txt")))

    def test_production_completed_no_improvement_post_commit_failure(self):
        run_id = "test_run_no_imp_fail"
        run_dir = os.path.join(self.tmp_dir, "history", run_id)
        os.makedirs(run_dir, exist_ok=True)

        lock_data = {
            "approved_commit_sha": "a" * 40,
            "remote_url": "https://github.com/hehamalainen/RSILoop.git",
            "model": "gpt-5.6-sol",
            "allow_no_holdout": False
        }
        lock_path = os.path.join(self.tmp_dir, "experiment.lock")
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)

        prompt_dir = os.path.join(self.tmp_dir, "prompt_templates")
        os.makedirs(prompt_dir, exist_ok=True)
        with open(os.path.join(prompt_dir, "inner_agent_baseline.txt"), "w") as f:
            f.write("baseline prompt")

        hashes_data = {
            "prompt_templates/inner_agent_baseline.txt": hashlib.sha256(b"baseline prompt").hexdigest(),
            "prompt_templates/meta_optimizer.txt": "dummy",
            "benchmarks/circular_buffer/problem.txt": "dummy",
            "benchmarks/circular_buffer/test_harness.py": "dummy",
            "benchmarks/json_parser/problem.txt": "dummy",
            "benchmarks/json_parser/test_harness.py": "dummy",
            "benchmarks/run_length_encoding/problem.txt": "dummy",
            "benchmarks/run_length_encoding/test_harness.py": "dummy",
            "benchmarks/huffman_coding/problem.txt": "dummy",
            "benchmarks/huffman_coding/test_harness.py": "dummy",
            "benchmarks/validation/string_calculator/problem.txt": "dummy",
            "benchmarks/validation/string_calculator/test_harness.py": "dummy",
            "benchmarks/validation/matrix_operations/problem.txt": "dummy",
            "benchmarks/validation/matrix_operations/test_harness.py": "dummy",
            "candidate.sb.template": "dummy",
            "generator.sb.template": "dummy"
        }
        shutil.copy(os.path.join(BASE_DIR, "candidate.sb.template"), os.path.join(self.tmp_dir, "candidate.sb.template"))
        shutil.copy(os.path.join(BASE_DIR, "generator.sb.template"), os.path.join(self.tmp_dir, "generator.sb.template"))
        with open(os.path.join(self.tmp_dir, "preflight_hashes.json"), "w") as f:
            json.dump(hashes_data, f)

        best_manifest_path = os.path.join(self.tmp_dir, "best_manifest.json")
        if os.path.exists(best_manifest_path):
            os.remove(best_manifest_path)

        def fake_update_status(ctx, status, error_msg=None):
            raise OSError("injected status update failure")

        with mock.patch("rsi_loop.BASE_DIR", self.tmp_dir), \
             mock.patch("rsi_loop.HISTORY_DIR", os.path.join(self.tmp_dir, "history")), \
             mock.patch("rsi_loop.PROMPTS_DIR", os.path.join(self.tmp_dir, "prompts")), \
             mock.patch("rsi_loop.BEST_MANIFEST_PATH", best_manifest_path), \
             mock.patch("rsi_loop.verify_preflight_hashes"), \
             mock.patch("rsi_loop._capture_environment_metadata"), \
             mock.patch("rsi_loop.run_smoke_and_canary", return_value=("codex-1.0", 50)), \
             mock.patch("rsi_loop.call_codex_sandboxed", return_value=("def solution(): pass", 100)), \
             mock.patch("rsi_loop.run_evaluation_on_candidate", return_value={"success": False, "log": "Ran 5 tests"}), \
             mock.patch("rsi_loop._list_optimization_benchmarks", return_value=["circular_buffer"]), \
             mock.patch("rsi_loop._list_validation_benchmarks", return_value=["validation_task"]), \
             mock.patch("rsi_loop.mutate_prompt", return_value=("better prompt", 200)), \
             mock.patch("rsi_loop.evaluate_prompt_on_tasks") as mock_eval, \
             mock.patch("rsi_loop._update_operator_report_commit_status", fake_update_status):

            mock_eval.side_effect = [
                (0.5, {"circular_buffer": {"success": False, "error": "err"}}, 100), # trial 1 baseline
                (0.6, {}, 100), # trial 1 mut 1
                (0.5, {"circular_buffer": {"success": False, "error": "err"}}, 100), # trial 2 baseline
                (0.6, {}, 100), # trial 2 mut 1
                (0.5, {"circular_buffer": {"success": False, "error": "err"}}, 100), # trial 3 baseline
                (0.6, {}, 100), # trial 3 mut 1
                (0.6, {}, 100), # validation baseline
                (0.6, {}, 100), # validation candidate 1
                (0.6, {}, 100), # validation candidate 2
                (0.6, {}, 100), # validation candidate 3
            ]

            with mock.patch("rsi_loop.subprocess.run") as mock_sub:
                mock_sub.side_effect = [
                    mock.Mock(returncode=0, stdout=""),  # git status
                    mock.Mock(returncode=0, stdout=f"{lock_data['remote_url']}\n"),  # git remote get-url
                    mock.Mock(returncode=0, stdout=f"{lock_data['approved_commit_sha']}\trefs/heads/main\n"),  # git ls-remote
                    mock.Mock(returncode=0, stdout=f"{lock_data['approved_commit_sha']}\n"),  # git rev-parse HEAD
                ]
                with mock.patch("rsi_loop.uuid.uuid4") as mock_uuid:
                    mock_uuid.return_value = mock.Mock(hex=run_id.split("_")[-1])
                    with mock.patch("rsi_loop.time.time", return_value=12345678):
                        expected_run_id = f"run_12345678_{run_id.split('_')[-1]}"
                        expected_run_dir = os.path.join(self.tmp_dir, "history", expected_run_id)

                        with self.assertRaises(SystemExit) as se:
                            main()
                        # Should exit 1 due to the injected post-commit status failure
                        self.assertEqual(se.exception.code, 1)

                        # Validate pointer replacement succeeded
                        self.assertTrue(os.path.exists(best_manifest_path))
                        # Validate new run manifest/report/prompt files were NOT deleted
                        self.assertTrue(os.path.exists(os.path.join(expected_run_dir, "run_manifest.json")))
                        self.assertTrue(os.path.exists(os.path.join(expected_run_dir, "inner_agent_best.txt")))

    def test_production_completed_promoted_post_commit_failure(self):
        run_id = "test_run_prom_fail"
        run_dir = os.path.join(self.tmp_dir, "history", run_id)
        os.makedirs(run_dir, exist_ok=True)

        lock_data = {
            "approved_commit_sha": "a" * 40,
            "remote_url": "https://github.com/hehamalainen/RSILoop.git",
            "model": "gpt-5.6-sol",
            "allow_no_holdout": False
        }
        lock_path = os.path.join(self.tmp_dir, "experiment.lock")
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)

        prompt_dir = os.path.join(self.tmp_dir, "prompt_templates")
        os.makedirs(prompt_dir, exist_ok=True)
        with open(os.path.join(prompt_dir, "inner_agent_baseline.txt"), "w") as f:
            f.write("baseline prompt")

        hashes_data = {
            "prompt_templates/inner_agent_baseline.txt": hashlib.sha256(b"baseline prompt").hexdigest(),
            "prompt_templates/meta_optimizer.txt": "dummy",
            "benchmarks/circular_buffer/problem.txt": "dummy",
            "benchmarks/circular_buffer/test_harness.py": "dummy",
            "benchmarks/json_parser/problem.txt": "dummy",
            "benchmarks/json_parser/test_harness.py": "dummy",
            "benchmarks/run_length_encoding/problem.txt": "dummy",
            "benchmarks/run_length_encoding/test_harness.py": "dummy",
            "benchmarks/huffman_coding/problem.txt": "dummy",
            "benchmarks/huffman_coding/test_harness.py": "dummy",
            "benchmarks/validation/string_calculator/problem.txt": "dummy",
            "benchmarks/validation/string_calculator/test_harness.py": "dummy",
            "benchmarks/validation/matrix_operations/problem.txt": "dummy",
            "benchmarks/validation/matrix_operations/test_harness.py": "dummy",
            "candidate.sb.template": "dummy",
            "generator.sb.template": "dummy"
        }
        shutil.copy(os.path.join(BASE_DIR, "candidate.sb.template"), os.path.join(self.tmp_dir, "candidate.sb.template"))
        shutil.copy(os.path.join(BASE_DIR, "generator.sb.template"), os.path.join(self.tmp_dir, "generator.sb.template"))
        with open(os.path.join(self.tmp_dir, "preflight_hashes.json"), "w") as f:
            json.dump(hashes_data, f)

        best_manifest_path = os.path.join(self.tmp_dir, "best_manifest.json")
        if os.path.exists(best_manifest_path):
            os.remove(best_manifest_path)

        def fake_update_status(ctx, status, error_msg=None):
            raise OSError("injected status update failure")

        with mock.patch("rsi_loop.BASE_DIR", self.tmp_dir), \
             mock.patch("rsi_loop.HISTORY_DIR", os.path.join(self.tmp_dir, "history")), \
             mock.patch("rsi_loop.PROMPTS_DIR", os.path.join(self.tmp_dir, "prompts")), \
             mock.patch("rsi_loop.BEST_MANIFEST_PATH", best_manifest_path), \
             mock.patch("rsi_loop.verify_preflight_hashes"), \
             mock.patch("rsi_loop._capture_environment_metadata"), \
             mock.patch("rsi_loop.run_smoke_and_canary", return_value=("codex-1.0", 50)), \
             mock.patch("rsi_loop.call_codex_sandboxed", return_value=("def solution(): pass", 100)), \
             mock.patch("rsi_loop.run_evaluation_on_candidate", return_value={"success": True, "log": "Ran 5 tests"}), \
             mock.patch("rsi_loop._list_optimization_benchmarks", return_value=["circular_buffer"]), \
             mock.patch("rsi_loop._list_validation_benchmarks", return_value=["validation_task"]), \
             mock.patch("rsi_loop.mutate_prompt", return_value=("better prompt", 200)), \
             mock.patch("rsi_loop.evaluate_prompt_on_tasks") as mock_eval, \
             mock.patch("rsi_loop._update_operator_report_commit_status", fake_update_status):

            mock_eval.side_effect = [
                (0.5, {"circular_buffer": {"success": False, "error": "err"}}, 100), # trial 1 baseline
                (0.8, {}, 100), # trial 1 mut 1
                (0.5, {"circular_buffer": {"success": False, "error": "err"}}, 100), # trial 2 baseline
                (0.8, {}, 100), # trial 2 mut 1
                (0.5, {"circular_buffer": {"success": False, "error": "err"}}, 100), # trial 3 baseline
                (0.8, {}, 100), # trial 3 mut 1
                (0.6, {}, 100), # validation baseline
                (0.9, {}, 100), # validation candidate 1
                (0.9, {}, 100), # validation candidate 2
                (0.9, {}, 100), # validation candidate 3
            ]

            with mock.patch("rsi_loop.subprocess.run") as mock_sub:
                mock_sub.side_effect = [
                    mock.Mock(returncode=0, stdout=""),  # git status
                    mock.Mock(returncode=0, stdout=f"{lock_data['remote_url']}\n"),  # git remote get-url
                    mock.Mock(returncode=0, stdout=f"{lock_data['approved_commit_sha']}\trefs/heads/main\n"),  # git ls-remote
                    mock.Mock(returncode=0, stdout=f"{lock_data['approved_commit_sha']}\n"),  # git rev-parse HEAD
                ]
                with mock.patch("rsi_loop.uuid.uuid4") as mock_uuid:
                    mock_uuid.return_value = mock.Mock(hex=run_id.split("_")[-1])
                    with mock.patch("rsi_loop.time.time", return_value=12345678):
                        expected_run_id = f"run_12345678_{run_id.split('_')[-1]}"
                        expected_run_dir = os.path.join(self.tmp_dir, "history", expected_run_id)

                        with self.assertRaises(SystemExit) as se:
                            main()
                        # Should exit 1 due to the injected post-commit status failure
                        self.assertEqual(se.exception.code, 1)

                        # Validate pointer replacement succeeded
                        self.assertTrue(os.path.exists(best_manifest_path))
                        # Validate new run manifest/report/prompt files were NOT deleted
                        self.assertTrue(os.path.exists(os.path.join(expected_run_dir, "run_manifest.json")))
                        self.assertTrue(os.path.exists(os.path.join(expected_run_dir, "inner_agent_best.txt")))

    def test_recovery_dangling_operator_report_symlink_aborts_without_mutation(self):
        history_dir = os.path.join(self.tmp_dir, "history")
        os.makedirs(history_dir, exist_ok=True)

        # Run A: valid authenticated pending run
        run_a_dir = os.path.join(history_dir, "run_a")
        os.makedirs(run_a_dir, exist_ok=True)

        prompt_a_path = os.path.join(run_a_dir, "inner_agent_best.txt")
        prompt_a_bytes = b"valid prompt A"
        with open(prompt_a_path, "wb") as f:
            f.write(prompt_a_bytes)
        os.chmod(prompt_a_path, 0o444)

        manifest_a_data = {
            "schema_version": "2",
            "run_id": "run_a",
            "status": "completed_promoted",
            "approved_commit_sha": "a" * 40,
            "lock_file_hash": "mock_hash",
            "selected_prompt_path": "inner_agent_best.txt",
            "selected_prompt_hash": hashlib.sha256(prompt_a_bytes).hexdigest()
        }
        manifest_a_path = os.path.join(run_a_dir, "run_manifest.json")
        manifest_a_bytes = json.dumps(manifest_a_data, indent=2).encode('utf-8')
        with open(manifest_a_path, "wb") as f:
            f.write(manifest_a_bytes)
        os.chmod(manifest_a_path, 0o444)

        report_a_data = {
            "schema_version": "2",
            "run_id": "run_a",
            "status": "completed_promoted",
            "pointer_commit_status": "pending",
            "lock_contents": {"approved_commit_sha": "a" * 40},
            "lock_file_hash": "mock_hash",
            "tokens": {"cumulative_consumed": 0},
            "manifest_sha256": hashlib.sha256(manifest_a_bytes).hexdigest()
        }
        report_a_path = os.path.join(run_a_dir, "operator_report.json")
        report_a_bytes = json.dumps(report_a_data, indent=2).encode('utf-8')
        with open(report_a_path, "wb") as f:
            f.write(report_a_bytes)
        os.chmod(report_a_path, 0o444)

        # Run B: run with dangling operator_report.json symlink
        run_b_dir = os.path.join(history_dir, "run_b")
        os.makedirs(run_b_dir, exist_ok=True)
        dangling_report_symlink = os.path.join(run_b_dir, "operator_report.json")
        non_existent_target = os.path.join(self.tmp_dir, "does_not_exist.json")
        os.symlink(non_existent_target, dangling_report_symlink)

        best_manifest_path = os.path.join(self.tmp_dir, "best_manifest.json")
        if os.path.exists(best_manifest_path):
            os.remove(best_manifest_path)

        with mock.patch("rsi_loop.HISTORY_DIR", history_dir), \
             mock.patch("rsi_loop.BASE_DIR", self.tmp_dir), \
             mock.patch("rsi_loop.BEST_MANIFEST_PATH", best_manifest_path):
            with self.assertRaises(ExperimentInvalidError) as e:
                recover_pending_runs()
            self.assertIn("Symlink detected", str(e.exception))

            # No invalid_manifest.json published in run_a or run_b
            self.assertFalse(os.path.exists(os.path.join(run_a_dir, "invalid_manifest.json")))
            self.assertFalse(os.path.exists(os.path.join(run_b_dir, "invalid_manifest.json")))

            # Run A files remain byte-for-byte unchanged
            with open(prompt_a_path, "rb") as f:
                self.assertEqual(f.read(), prompt_a_bytes)
            with open(manifest_a_path, "rb") as f:
                self.assertEqual(f.read(), manifest_a_bytes)
            with open(report_a_path, "rb") as f:
                self.assertEqual(f.read(), report_a_bytes)

    def test_recovery_dangling_invalid_manifest_symlink_aborts_without_mutation(self):
        history_dir = os.path.join(self.tmp_dir, "history")
        os.makedirs(history_dir, exist_ok=True)

        # Run A: valid authenticated pending run
        run_a_dir = os.path.join(history_dir, "run_a")
        os.makedirs(run_a_dir, exist_ok=True)

        prompt_a_path = os.path.join(run_a_dir, "inner_agent_best.txt")
        prompt_a_bytes = b"valid prompt A"
        with open(prompt_a_path, "wb") as f:
            f.write(prompt_a_bytes)
        os.chmod(prompt_a_path, 0o444)

        manifest_a_data = {
            "schema_version": "2",
            "run_id": "run_a",
            "status": "completed_promoted",
            "approved_commit_sha": "a" * 40,
            "lock_file_hash": "mock_hash",
            "selected_prompt_path": "inner_agent_best.txt",
            "selected_prompt_hash": hashlib.sha256(prompt_a_bytes).hexdigest()
        }
        manifest_a_path = os.path.join(run_a_dir, "run_manifest.json")
        manifest_a_bytes = json.dumps(manifest_a_data, indent=2).encode('utf-8')
        with open(manifest_a_path, "wb") as f:
            f.write(manifest_a_bytes)
        os.chmod(manifest_a_path, 0o444)

        report_a_data = {
            "schema_version": "2",
            "run_id": "run_a",
            "status": "completed_promoted",
            "pointer_commit_status": "pending",
            "lock_contents": {"approved_commit_sha": "a" * 40},
            "lock_file_hash": "mock_hash",
            "tokens": {"cumulative_consumed": 0},
            "manifest_sha256": hashlib.sha256(manifest_a_bytes).hexdigest()
        }
        report_a_path = os.path.join(run_a_dir, "operator_report.json")
        report_a_bytes = json.dumps(report_a_data, indent=2).encode('utf-8')
        with open(report_a_path, "wb") as f:
            f.write(report_a_bytes)
        os.chmod(report_a_path, 0o444)

        # Run B: run with dangling invalid_manifest.json symlink
        run_b_dir = os.path.join(history_dir, "run_b")
        os.makedirs(run_b_dir, exist_ok=True)
        dangling_invalid_symlink = os.path.join(run_b_dir, "invalid_manifest.json")
        non_existent_target = os.path.join(self.tmp_dir, "does_not_exist.json")
        os.symlink(non_existent_target, dangling_invalid_symlink)

        best_manifest_path = os.path.join(self.tmp_dir, "best_manifest.json")
        if os.path.exists(best_manifest_path):
            os.remove(best_manifest_path)

        with mock.patch("rsi_loop.HISTORY_DIR", history_dir), \
             mock.patch("rsi_loop.BASE_DIR", self.tmp_dir), \
             mock.patch("rsi_loop.BEST_MANIFEST_PATH", best_manifest_path):
            with self.assertRaises(ExperimentInvalidError) as e:
                recover_pending_runs()
            self.assertIn("Symlink detected", str(e.exception))

            # No invalid_manifest.json published in run_a or run_b
            self.assertFalse(os.path.exists(os.path.join(run_a_dir, "invalid_manifest.json")))

            # Run A files remain byte-for-byte unchanged
            with open(prompt_a_path, "rb") as f:
                self.assertEqual(f.read(), prompt_a_bytes)
            with open(manifest_a_path, "rb") as f:
                self.assertEqual(f.read(), manifest_a_bytes)
            with open(report_a_path, "rb") as f:
                self.assertEqual(f.read(), report_a_bytes)

    def test_production_pending_recovery_pointer_committed_outcome(self):
        # 1. Create real RunContext and run init_operator_report
        ctx = RunContext()
        ctx.run_id = "run_prod_pending_committed"
        ctx.run_dir = os.path.join(self.tmp_dir, "history", ctx.run_id)
        os.makedirs(ctx.run_dir, exist_ok=True)
        ctx.lock_file_hash = "mock_lock_hash"

        init_operator_report(ctx)
        ctx.operator_report["lock_contents"]["approved_commit_sha"] = "a" * 40
        self.assertEqual(ctx.operator_report["schema_version"], "2")

        # 2. Setup prompt and manifest
        prompt_path = os.path.join(ctx.run_dir, "inner_agent_best.txt")
        prompt_bytes = b"production prompt content"
        with open(prompt_path, "wb") as f:
            f.write(prompt_bytes)
        os.chmod(prompt_path, 0o444)

        manifest_data = {
            "schema_version": "2",
            "run_id": ctx.run_id,
            "status": "completed_promoted",
            "approved_commit_sha": "a" * 40,
            "lock_file_hash": ctx.lock_file_hash,
            "selected_prompt_path": "inner_agent_best.txt",
            "selected_prompt_hash": hashlib.sha256(prompt_bytes).hexdigest()
        }
        manifest_path = os.path.join(ctx.run_dir, "run_manifest.json")

        # 3. Publish using production publish_terminal_manifest_and_report
        with mock.patch("rsi_loop.BASE_DIR", self.tmp_dir):
            publish_terminal_manifest_and_report(ctx, manifest_path, manifest_data, pointer_commit_status="pending")

        report_path = os.path.join(ctx.run_dir, "operator_report.json")
        with open(report_path, "r") as f:
            rep = json.load(f)
        self.assertEqual(rep["schema_version"], "2")
        self.assertEqual(rep["pointer_commit_status"], "pending")

        # 4. Update pointer to point to manifest_path (pointer committed, but report update crashed)
        best_manifest_path = os.path.join(self.tmp_dir, "best_manifest.json")
        history_dir = os.path.join(self.tmp_dir, "history")
        with mock.patch("rsi_loop.BASE_DIR", self.tmp_dir), \
             mock.patch("rsi_loop.BEST_MANIFEST_PATH", best_manifest_path):
            update_best_manifest_pointer(manifest_path)

            # 5. Invoke recovery
            with mock.patch("rsi_loop.HISTORY_DIR", history_dir):
                recover_pending_runs()

            # 6. Verify report updated to committed, manifest & prompt preserved
            with open(report_path, "r") as f:
                rec_rep = json.load(f)
            self.assertEqual(rec_rep["pointer_commit_status"], "committed")
            self.assertEqual(rec_rep["schema_version"], "2")
            self.assertTrue(os.path.exists(manifest_path))
            self.assertTrue(os.path.exists(prompt_path))
            self.assertFalse(os.path.exists(os.path.join(ctx.run_dir, "invalid_manifest.json")))

    def test_production_pending_recovery_pointer_not_committed_outcome(self):
        # 1. Create real RunContext and run init_operator_report
        ctx = RunContext()
        ctx.run_id = "run_prod_pending_uncommitted"
        ctx.run_dir = os.path.join(self.tmp_dir, "history", ctx.run_id)
        os.makedirs(ctx.run_dir, exist_ok=True)
        ctx.lock_file_hash = "mock_lock_hash"

        init_operator_report(ctx)
        ctx.operator_report["lock_contents"]["approved_commit_sha"] = "a" * 40
        self.assertEqual(ctx.operator_report["schema_version"], "2")

        # 2. Setup prompt and manifest
        prompt_path = os.path.join(ctx.run_dir, "inner_agent_best.txt")
        prompt_bytes = b"production prompt content"
        with open(prompt_path, "wb") as f:
            f.write(prompt_bytes)
        os.chmod(prompt_path, 0o444)

        manifest_data = {
            "schema_version": "2",
            "run_id": ctx.run_id,
            "status": "completed_promoted",
            "approved_commit_sha": "a" * 40,
            "lock_file_hash": ctx.lock_file_hash,
            "selected_prompt_path": "inner_agent_best.txt",
            "selected_prompt_hash": hashlib.sha256(prompt_bytes).hexdigest()
        }
        manifest_path = os.path.join(ctx.run_dir, "run_manifest.json")

        # 3. Publish using production publish_terminal_manifest_and_report
        with mock.patch("rsi_loop.BASE_DIR", self.tmp_dir):
            publish_terminal_manifest_and_report(ctx, manifest_path, manifest_data, pointer_commit_status="pending")

        report_path = os.path.join(ctx.run_dir, "operator_report.json")

        # 4. Do NOT commit pointer (crash before pointer commit). Ensure pointer is absent or points elsewhere
        best_manifest_path = os.path.join(self.tmp_dir, "best_manifest.json")
        if os.path.exists(best_manifest_path):
            os.remove(best_manifest_path)

        history_dir = os.path.join(self.tmp_dir, "history")
        with mock.patch("rsi_loop.HISTORY_DIR", history_dir), \
             mock.patch("rsi_loop.BASE_DIR", self.tmp_dir), \
             mock.patch("rsi_loop.BEST_MANIFEST_PATH", best_manifest_path):

            # 5. Invoke recovery
            recover_pending_runs()

            # 6. Verify run was invalidated and uncommitted files deleted
            invalid_path = os.path.join(ctx.run_dir, "invalid_manifest.json")
            self.assertTrue(os.path.exists(invalid_path))
            with open(invalid_path, "r") as f:
                inv_m = json.load(f)
            self.assertEqual(inv_m["status"], "invalid")
            self.assertEqual(inv_m["schema_version"], "2")

            with open(report_path, "r") as f:
                rec_rep = json.load(f)
            self.assertEqual(rec_rep["status"], "invalid")
            self.assertEqual(rec_rep["pointer_commit_status"], "not_applicable")
            self.assertEqual(rec_rep["schema_version"], "2")

            self.assertFalse(os.path.exists(manifest_path))
            self.assertFalse(os.path.exists(prompt_path))

if __name__ == "__main__":
    unittest.main()
