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
)

class TestRsiLoopOrchestrator(unittest.TestCase):
    def setUp(self):
        unraisable_exceptions.clear()
        self.tmp_dir = tempfile.mkdtemp()
        self.patchers = []

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

            run_manifest = os.path.join(target_dir, "run_manifest.json")
            data = {"schema_version": "2", "status": "completed_promoted"}

            with mock.patch("rsi_loop.BEST_MANIFEST_PATH", os.path.join(run_dir, "best_manifest.json")), \
                 mock.patch("rsi_loop.BASE_DIR", run_dir), \
                 mock.patch("rsi_loop.HISTORY_DIR", history_dir):
                with open(run_manifest, "w") as f:
                    json.dump(data, f)
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
        lock_data = {"approved_commit_sha": commit_sha, "model": "gpt-5.6-sol"}
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)

        ctx = RunContext()
        with mock.patch("rsi_loop.BASE_DIR", git_dir):
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
        mock_verify_lock = mock.patch("rsi_loop.verify_experiment_lock", return_value="approved_sha")
        mock_verify_hashes = mock.patch("rsi_loop.verify_preflight_hashes")
        def fake_smoke(ctx):
            ctx.codex_version = "codex-cli 0.144.5"
            ctx.codex_cli_version = ctx.codex_version
            ctx.model_resolved = "unavailable"
            return ctx.codex_version, 50

        mock_smoke = mock.patch("rsi_loop.run_smoke_and_canary", side_effect=fake_smoke)
        mock_list_opt = mock.patch("rsi_loop._list_optimization_benchmarks", return_value=["task1"])
        mock_list_val = mock.patch("rsi_loop._list_validation_benchmarks", return_value=["val1"])

        self.patchers.extend([mock_verify_lock, mock_verify_hashes, mock_smoke, mock_list_opt, mock_list_val])
        for p in self.patchers:
            p.start()

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
        meta_opt_path = os.path.join(self.tmp_dir, "prompt_templates", "inner_agent_best.txt")
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

        lock_path = os.path.join(test_dir, "experiment.lock")

        # Case 1: Environment ALLOW_NO_HOLDOUT is 1, but lock file allow_no_holdout is False (or missing) -> Disagreement -> Fails!
        lock_data = {"approved_commit_sha": commit_sha, "model": "gpt-5.6-sol"}
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)

        ctx = RunContext()
        ctx.allow_no_holdout = True  # Simulated ALLOW_NO_HOLDOUT=1
        with mock.patch("rsi_loop.BASE_DIR", test_dir), \
             mock.patch.dict(os.environ, {"ALLOW_NO_HOLDOUT": "1"}):
            with self.assertRaises(ExperimentInvalidError) as r_ctx:
                verify_experiment_lock(ctx)
            self.assertIn("disagrees with experiment.lock", str(r_ctx.exception))

        # Case 2: Environment ALLOW_NO_HOLDOUT is 0/empty, but lock file allow_no_holdout is True -> Disagreement -> Fails!
        lock_data = {
            "approved_commit_sha": commit_sha,
            "allow_no_holdout": True,
            "model": "gpt-5.6-sol",
        }
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)

        ctx = RunContext()
        ctx.allow_no_holdout = False  # Simulated ALLOW_NO_HOLDOUT=0
        with mock.patch("rsi_loop.BASE_DIR", test_dir), \
             mock.patch.dict(os.environ, {"ALLOW_NO_HOLDOUT": "0"}):
            with self.assertRaises(ExperimentInvalidError) as r_ctx:
                verify_experiment_lock(ctx)
            self.assertIn("disagrees with experiment.lock", str(r_ctx.exception))

        # Case 3: Both agree to allow no holdout (env="1", lock=True) -> Success!
        ctx = RunContext()
        ctx.allow_no_holdout = True
        with mock.patch("rsi_loop.BASE_DIR", test_dir), \
             mock.patch.dict(os.environ, {"ALLOW_NO_HOLDOUT": "1"}):
            sha = verify_experiment_lock(ctx)
            self.assertEqual(sha, commit_sha)
            self.assertTrue(ctx.allow_no_holdout)

        # Case 4: Both agree to require holdout (env omitted, lock=False) -> Success!
        lock_data = {
            "approved_commit_sha": commit_sha,
            "allow_no_holdout": False,
            "model": "gpt-5.6-sol",
        }
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)

        ctx = RunContext()
        ctx.allow_no_holdout = False
        with mock.patch("rsi_loop.BASE_DIR", test_dir), \
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
            if command[:2] == ["git", "status"]:
                return mock.Mock(stdout="", stderr="", returncode=0)
            return mock.Mock(stdout=approved_sha + "\n", stderr="", returncode=0)

        for index, (model_data, valid) in enumerate(cases):
            with self.subTest(model=model_data.get("model", "missing")):
                case_dir = os.path.join(self.tmp_dir, f"model_case_{index}")
                os.mkdir(case_dir)
                lock_data = {
                    "approved_commit_sha": approved_sha,
                    "allow_no_holdout": False,
                    **model_data,
                }
                with open(os.path.join(case_dir, "experiment.lock"), "w") as handle:
                    json.dump(lock_data, handle)
                ctx = RunContext()
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
        from rsi_loop import _build_canary_command, call_codex_sandboxed

        ctx = RunContext()
        ctx.run_id = "model_flag_test"
        ctx.codex_model = "gpt-5.6-sol"
        ctx.lock_model = "gpt-5.6-sol"
        canary_command = _build_canary_command("/exact/generator.sb", ctx)
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
                        _run_codex_canary("/exact/generator.sb", {}, ctx)
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
                _run_codex_canary("/exact/generator.sb", {}, ctx)
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
                        _run_codex_canary("/exact/generator.sb", {}, ctx)
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
                _run_codex_canary("/exact/generator.sb", env, ctx)
        self.assertIn("Refusing to create helper binaries", raised.exception.stderr)

    def test_host_boundary_cli_bootstrap(self):
        import rsi_loop
        from rsi_loop import (
            _build_canary_command,
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
            command = _build_canary_command(sb_path, ctx)
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
            self.assertEqual(result.returncode, 1)
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
            self.assertEqual(_run_codex_canary("/exact/generator.sb", {}, ctx), 1)
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
                        _run_codex_version_smoke_test("/exact/generator.sb", {})

        with mock.patch(
            "rsi_loop.subprocess.run",
            return_value=mock.Mock(
                returncode=0,
                stdout="codex-cli 0.144.5\n",
                stderr="",
            ),
        ):
            self.assertEqual(
                _run_codex_version_smoke_test("/exact/generator.sb", {}),
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

if __name__ == "__main__":
    unittest.main()
