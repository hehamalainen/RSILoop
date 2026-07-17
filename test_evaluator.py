import os
import sys
import unittest
import shutil
import tempfile
import json
import subprocess
from evaluator import run_evaluation

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

class TestEvaluatorRigor(unittest.TestCase):
    def setUp(self):
        # Create a unique temp workspace for candidate testing
        self.candidate_dir = tempfile.mkdtemp(prefix="test_candidate_")
        self.test_harness_path = os.path.join(self.candidate_dir, "test_harness.py")
        
        # Write a basic test harness
        harness_code = """
import unittest
class TestDummy(unittest.TestCase):
    def test_solve(self):
        try:
            from solution import solve
        except ImportError as e:
            self.fail(f"Import error: {e}")
        self.assertEqual(solve(), "OK")
if __name__ == "__main__":
    unittest.main()
"""
        with open(self.test_harness_path, "w") as f:
            f.write(harness_code)

    def tearDown(self):
        shutil.rmtree(self.candidate_dir, ignore_errors=True)

    def write_solution(self, code):
        solution_path = os.path.join(self.candidate_dir, "solution.py")
        with open(solution_path, "w") as f:
            f.write(code)

    def test_sys_exit_fail_closed(self):
        # Solvers calling sys.exit(0) to bypass unittest
        self.write_solution("""
import sys
sys.exit(0)
""")
        res = run_evaluation(self.candidate_dir, self.test_harness_path)
        self.assertFalse(res["success"])
        self.assertIn("Worker exited early", str(res["error"]))

    def test_sandbox_write_block(self):
        # Solver trying to write outside permitted directories
        self.write_solution("""
def solve():
    with open('/Users/heikkihamalainen/escaped.txt', 'w') as f:
        f.write('leak')
    return "OK"
""")
        res = run_evaluation(self.candidate_dir, self.test_harness_path)
        self.assertFalse(res["success"])
        self.assertIn("PermissionError", str(res["error"]))

    def test_subprocess_block(self):
        # Solver trying to execute shell command
        self.write_solution("""
import subprocess
def solve():
    subprocess.run(['ls'])
    return "OK"
""")
        res = run_evaluation(self.candidate_dir, self.test_harness_path)
        self.assertFalse(res["success"])
        self.assertIn("PermissionError", str(res["error"]))

    def test_unsolicited_stdout_stderr(self):
        # Solver printing random text to stdout/stderr (should not corrupt protocol or result in pass)
        self.write_solution("""
def solve():
    print("UNSOLICITED STDOUT SPOOFING OK")
    import sys
    sys.stderr.write("UNSOLICITED STDERR SPOOFING OK\\n")
    return "BAD"
""")
        res = run_evaluation(self.candidate_dir, self.test_harness_path)
        self.assertFalse(res["success"])

    def test_cpu_exhaustion_timeout(self):
        # Solver entering infinite loop
        self.write_solution("""
def solve():
    while True:
        pass
    return "OK"
""")
        res = run_evaluation(self.candidate_dir, self.test_harness_path, timeout=1.0)
        self.assertFalse(res["success"])

    def test_private_file_leak_block(self):
        # Solver attempting to read private tests
        self.write_solution("""
def solve():
    with open('/Users/heikkihamalainen/.gemini/antigravity/scratch/rsi_loop_private/benchmarks/circular_buffer/test_holdout.py', 'r') as f:
        data = f.read()
    return "OK"
""")
        res = run_evaluation(self.candidate_dir, self.test_harness_path)
        self.assertFalse(res["success"])
        self.assertIn("PermissionError", str(res["error"]))

    def test_environment_secrets_scrubbed(self):
        # Solver attempting to read environment keys
        os.environ["SECRET_API_KEY"] = "super-secret-token"
        self.write_solution("""
import os
def solve():
    if "SECRET_API_KEY" in os.environ:
        raise ValueError(f"Secret leaked: {os.environ['SECRET_API_KEY']}")
    return "OK"
""")
        res = run_evaluation(self.candidate_dir, self.test_harness_path)
        # Should pass because SECRET_API_KEY is scrubbed inside the worker
        self.assertTrue(res["success"], f"Error was: {res['error']}")

    # --- P0/P1 REGRESSION TESTS ---

    def test_class_proxy_late_binding_remediated(self):
        # Verify multiple classes are proxied dynamically without late-bound collision
        self.write_solution("""
class Alpha:
    def greet(self):
        return "Alpha"
class Beta:
    def greet(self):
        return "Beta"
""")
        harness_code = """
import unittest
class TestCollisions(unittest.TestCase):
    def test_solve(self):
        from solution import Alpha, Beta
        a = Alpha()
        b = Beta()
        self.assertEqual(a.greet(), "Alpha")
        self.assertEqual(b.greet(), "Beta")
if __name__ == "__main__":
    unittest.main()
"""
        h_path = os.path.join(self.candidate_dir, "test_harness_collision.py")
        with open(h_path, "w") as f:
            f.write(harness_code)
            
        res = run_evaluation(self.candidate_dir, h_path)
        self.assertTrue(res["success"], f"Late binding collision error: {res['error']}")

    def test_deque_import_unaffected(self):
        # Verify imported classes like deque are filtered out and do not get dynamic proxy registration
        self.write_solution("""
from collections import deque
def solve():
    d = deque([1, 2, 3])
    return "OK"
""")
        res = run_evaluation(self.candidate_dir, self.test_harness_path)
        self.assertTrue(res["success"], f"deque import proxy error: {res['error']}")

    def test_custom_exception_mro_reconstruction(self):
        # Verify dynamic exception registry handles MRO subclass hierarchies correctly
        self.write_solution("""
class BaseCustomException(Exception):
    pass
class DerivedCustomException(BaseCustomException):
    pass
def solve():
    raise DerivedCustomException("Failure")
""")
        harness_code = """
import unittest
class TestCustomException(unittest.TestCase):
    def test_solve(self):
        from solution import solve, BaseCustomException, DerivedCustomException
        try:
            solve()
        except DerivedCustomException as e:
            self.assertTrue(isinstance(e, BaseCustomException))
            self.assertEqual(str(e), "Failure")
            return
        self.fail("DerivedCustomException was not raised")
if __name__ == "__main__":
    unittest.main()
"""
        h_path = os.path.join(self.candidate_dir, "test_harness_custom_exc.py")
        with open(h_path, "w") as f:
            f.write(harness_code)
            
        res = run_evaluation(self.candidate_dir, h_path)
        self.assertTrue(res["success"], f"MRO exception reconstruction error: {res['error']}")

    def test_exact_circular_buffer_candidate_remediation(self):
        # Test circular buffer candidate solution passing cleanly through dynamically built proxies
        self.write_solution("""
from collections import deque
class BufferFullException(Exception):
    pass
class BufferEmptyException(Exception):
    pass
class CircularBuffer:
    def __init__(self, capacity: int):
        self.capacity = capacity
        self._buffer = deque(maxlen=capacity)
    def write(self, item):
        if len(self._buffer) >= self.capacity:
            raise BufferFullException()
        self._buffer.append(item)
    def read(self):
        if not self._buffer:
            raise BufferEmptyException()
        return self._buffer.popleft()
    def overwrite(self, item):
        if self.capacity == 0:
            return
        self._buffer.append(item)
""")
        harness_code = """
import unittest
class TestCB(unittest.TestCase):
    def test_basic(self):
        from solution import CircularBuffer, BufferFullException, BufferEmptyException
        buf = CircularBuffer(3)
        buf.write(1)
        buf.write(2)
        self.assertEqual(buf.read(), 1)
        
    def test_exceptions(self):
        from solution import CircularBuffer, BufferFullException, BufferEmptyException
        buf = CircularBuffer(1)
        buf.write(1)
        self.assertRaises(BufferFullException, buf.write, 2)
if __name__ == "__main__":
    unittest.main()
"""
        h_path = os.path.join(self.candidate_dir, "test_harness_cb.py")
        with open(h_path, "w") as f:
            f.write(harness_code)
            
        res = run_evaluation(self.candidate_dir, h_path)
        self.assertTrue(res["success"], f"CircularBuffer candidate evaluation error: {res['error']}")

    def test_generator_isolation_remediated(self):
        # Verify generator sandbox profile blocks unauthorized reads to host files
        sb_template_path = os.path.join(BASE_DIR, "generator.sb.template")
        with open(sb_template_path, "r") as f:
            sb_profile = f.read().format(candidate_dir=self.candidate_dir, temp_dir="/tmp")
            
        sb_profile_path = os.path.join(self.candidate_dir, "generator_test.sb")
        with open(sb_profile_path, "w") as f:
            f.write(sb_profile)
            
        cmd = [
            "sandbox-exec", "-f", sb_profile_path,
            "cat", "/Users/heikkihamalainen/.gemini/antigravity/scratch/rsi_loop_private/benchmarks/state_machine/test_holdout.py"
        ]
        res = subprocess.run(cmd, capture_output=True)
        # Sandbox must block read access returning non-zero exit code
        self.assertNotEqual(res.returncode, 0)
        self.assertIn("Operation not permitted", res.stderr.decode())

    def test_fail_closed_on_setup_limit_failure(self):
        # Verify worker immediately exits on setup limit failure
        allowed_env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8"
        }
        cmd = [
            "sandbox-exec", "-p", "(version 1) (deny default)",
            os.path.join(BASE_DIR, ".venv", "bin", "python"), "-u", os.path.join(BASE_DIR, "sandbox_worker.py")
        ]
        res = subprocess.run(cmd, capture_output=True)
        self.assertNotEqual(res.returncode, 0)

    # --- BEHAVIORAL RESOURCE ENFORCEMENT CHECKS ---

    # Note: Memory limit (RLIMIT_AS) is not supported under macOS setrlimit.
    # Virtual memory limits must be enforced via sandboxing or external supervision.

    def test_file_size_write_enforcement(self):
        # Solver attempting to write large file exceeding 1MB limit
        self.write_solution("""
def solve():
    with open('large.txt', 'wb') as f:
        f.write(b'x' * 2 * 1024 * 1024)
    return "OK"
""")
        res = run_evaluation(self.candidate_dir, self.test_harness_path)
        self.assertFalse(res["success"])

if __name__ == "__main__":
    unittest.main()
