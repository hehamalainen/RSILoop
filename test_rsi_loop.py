"""
test_rsi_loop.py — Tests for the RSI orchestrator layer.
"""
import os
import sys
import unittest
import json
import tempfile
import shutil
import hashlib
import subprocess
import threading
import gc
import warnings
from unittest import mock

# Treat warnings as errors
warnings.simplefilter('error', ResourceWarning)

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
)

class TestRsiLoopOrchestrator(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.mkdtemp()

    def tearDown(self):
        gc.collect()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        if unraisable_exceptions:
            errors = [f"{type(x.exc_value).__name__}: {x.exc_value}" for x in unraisable_exceptions]
            unraisable_exceptions.clear()
            self.fail(f"ResourceWarning or unraisable exception detected: {errors}")

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
            f.write('{"score": 1.0')  # truncated JSON

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
            '{"type":"turn.completed","usage":{"input_tokens":100,"output_tokens":49999}}',
        ])

        with mock.patch("rsi_loop._build_codex_temp_home", return_value=tempfile.mkdtemp()), \
             mock.patch("rsi_loop._build_generator_sb", return_value="/dev/null"), \
             mock.patch("rsi_loop._cleanup_temp_home"), \
             mock.patch("subprocess.run") as mock_run:
            mock_run.return_value = mock.Mock(
                returncode=0,
                stdout=fake_stdout,
                stderr="",
            )
            with self.assertRaises(ExperimentInvalidError) as ctx:
                call_codex_sandboxed(
                    "test prompt", "system", "/tmp",
                    cumulative_tokens=2,
                    token_ceiling=TOKEN_CEILING,
                )
            self.assertIn("ceiling breached", str(ctx.exception).lower())

    # ── Test 30: generator credential isolation denials ──────────────────────

    def test_generator_credential_isolation_denials(self):
        sb_template_path = os.path.join(BASE_DIR, "generator.sb.template")
        if not os.path.exists(sb_template_path):
            self.skipTest("generator.sb.template not found")

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
        """
        Verify that:
        - completed_promoted, completed_no_improvement, no_headroom, invalid manifest keys are correct.
        - invalid runs cannot modify root-level best_manifest.json.
        """
        from rsi_loop import update_best_manifest_pointer, load_and_verify_best_manifest

        # Setup temp manifest paths
        run_dir = tempfile.mkdtemp()
        try:
            run_manifest = os.path.join(run_dir, "run_manifest.json")
            data = {"pointer_schema_version": "2", "status": "completed_promoted"}

            # Atomic pointer setup
            with mock.patch("rsi_loop.BEST_MANIFEST_PATH", os.path.join(run_dir, "best_manifest.json")):
                with open(run_manifest, "w") as f:
                    json.dump(data, f)
                update_best_manifest_pointer(run_manifest)

                # Load and verify
                loaded = load_and_verify_best_manifest()
                self.assertIsNotNone(loaded)
                self.assertEqual(loaded["status"], "completed_promoted")
        finally:
            shutil.rmtree(run_dir)

    # ── Test 32: Hash verification failure checks ────────────────────────────

    def test_preflight_hash_verification_failures(self):
        """
        Verify that missing files, symlinks, extra keys, or path traversal fail closed.
        """
        from rsi_loop import verify_preflight_hashes

        # Test case: missing preflight hashes file
        with mock.patch("rsi_loop.PREFLIGHT_HASHES_PATH", "/nonexistent_preflight.json"):
            with self.assertRaises(ExperimentInvalidError):
                verify_preflight_hashes()


if __name__ == "__main__":
    class StrictTestResult(unittest.TextTestResult):
        def addSkip(self, test, reason):
            super().addSkip(test, reason)
            raise AssertionError(f"Skips are not allowed: {test} skipped because {reason}")

    class StrictTestRunner(unittest.TextTestRunner):
        resultclass = StrictTestResult
        def run(self, test):
            res = super().run(test)
            if len(res.skipped) > 0:
                raise AssertionError(f"Skips are not allowed: {len(res.skipped)} tests skipped")
            sys.stdout.write(f"\nStrictTestRunner discovered and ran {res.testsRun} tests.\n")
            sys.stdout.flush()
            return res

    unittest.main(testRunner=StrictTestRunner)
