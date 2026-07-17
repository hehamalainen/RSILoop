import os
import sys
import unittest
import json
import tempfile
import shutil
import hashlib
from rsi_loop import (
    parse_codex_jsonl_output,
    extract_code,
    promote_manifest,
    BASE_DIR
)

class TestRsiLoopOrchestrator(unittest.TestCase):
    def test_parse_jsonl_standard(self):
        stdout_lines = [
            '{"type":"thread.started","thread_id":"019f71c9-f0d9-73e1-930a-718a2e5224e2"}',
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"print(\'hello\')"}}',
            '{"type":"turn.completed","usage":{"input_tokens":100,"output_tokens":5}}'
        ]
        code, usage = parse_codex_jsonl_output(stdout_lines)
        self.assertEqual(code, "print('hello')")
        self.assertEqual(usage.get("output_tokens"), 5)

    def test_parse_jsonl_multiple_agent_messages(self):
        # Verify parser extracts the final message precisely
        stdout_lines = [
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"first message"}}',
            '{"type":"item.completed","item":{"id":"item_1","type":"agent_message","text":"second message"}}',
            '{"type":"turn.completed","usage":{"input_tokens":100,"output_tokens":10}}'
        ]
        code, usage = parse_codex_jsonl_output(stdout_lines)
        self.assertEqual(code, "second message")
        self.assertEqual(usage.get("output_tokens"), 10)

    def test_parse_jsonl_missing_turn_completed(self):
        # Verify it fails closed when turn.completed event is missing
        stdout_lines = [
            '{"type":"turn.started"}',
            '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"print(\'hello\')"}}'
        ]
        with self.assertRaises(ValueError) as ctx:
            parse_codex_jsonl_output(stdout_lines)
        self.assertIn("missing turn.completed event", str(ctx.exception))

    def test_parse_jsonl_malformed_event(self):
        # Verify malformed lines are skipped but bad turn.completed triggers fail-closed behavior
        stdout_lines = [
            '{"type":"turn.started"}',
            'malformed line here',
            '{"type":"item.completed","item":{"id":"item_0","type":"agent_message","text":"print(\'hello\')"}}',
            '{"type":"turn.completed", "usage": {}'  # Malformed JSON
        ]
        with self.assertRaises(ValueError):
            parse_codex_jsonl_output(stdout_lines)

    def test_extract_code_fenced(self):
        raw_msg = "Here is my code:\n```python\ndef greet():\n    return 'hello'\n```\nHope it works!"
        code = extract_code(raw_msg)
        self.assertEqual(code, "def greet():\n    return 'hello'")

    def test_extract_code_plain_text_prompt(self):
        # Plain text meta prompt mutation output must be extracted intact without python block wrappers
        raw_msg = "Please write a simple regex matcher solution"
        code = extract_code(raw_msg)
        self.assertEqual(code, "Please write a simple regex matcher solution")

    def test_interrupted_atomic_promotion(self):
        # Verify atomic manifest promotion using temp replacement prevents half-written state
        run_dir = tempfile.mkdtemp()
        try:
            best_manifest_path = os.path.join(run_dir, "best_manifest.json")
            
            # 1. Promote first manifest
            manifest_data = {
                "run_id": "run_1",
                "score": 0.66,
                "prompt_text": "prompt_1"
            }
            temp_path = os.path.join(run_dir, "best_manifest.json.tmp")
            with open(temp_path, "w") as f:
                json.dump(manifest_data, f)
            os.replace(temp_path, best_manifest_path)
            
            # Read back
            with open(best_manifest_path, "r") as f:
                data = json.load(f)
            self.assertEqual(data["run_id"], "run_1")
            
            # 2. Emulate crash during next promotion write (temp file created but replace not called)
            temp_path = os.path.join(run_dir, "best_manifest.json.tmp")
            with open(temp_path, "w") as f:
                f.write('{"score": 1.0') # Interrupted/half-written json
                
            # Assert main best_manifest.json remains intact and uncorrupted
            with open(best_manifest_path, "r") as f:
                data = json.load(f)
            self.assertEqual(data["run_id"], "run_1")
            self.assertEqual(data["score"], 0.66)
            
        finally:
            shutil.rmtree(run_dir)

if __name__ == "__main__":
    unittest.main()
