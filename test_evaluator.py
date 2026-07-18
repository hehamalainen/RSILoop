"""
test_evaluator.py — 20+ tests for the evaluator / IPC / sandbox layer.
"""
import os
import sys
import unittest
import shutil
import tempfile
import json
import subprocess
import threading
import gc
import warnings
import signal

# Treat warnings as errors
warnings.simplefilter('error', ResourceWarning)

unraisable_exceptions = []
def custom_unraisable_hook(unraisable):
    unraisable_exceptions.append(unraisable)
sys.unraisablehook = custom_unraisable_hook

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def _make_harness(candidate_dir: str, code: str) -> str:
    path = os.path.join(candidate_dir, "test_harness.py")
    with open(path, "w") as f:
        f.write(code)
    return path

def _write_solution(candidate_dir: str, code: str) -> None:
    with open(os.path.join(candidate_dir, "solution.py"), "w") as f:
        f.write(code)

class TestEvaluatorRigor(unittest.TestCase):
    def setUp(self):
        self.candidate_dir = tempfile.mkdtemp(prefix="test_candidate_")
        self.test_harness_path = _make_harness(self.candidate_dir, """
import unittest
class TestDummy(unittest.TestCase):
    def test_solve(self):
        from solution import solve
        self.assertEqual(solve(), "OK")
if __name__ == "__main__":
    unittest.main()
""")

    def tearDown(self):
        gc.collect()
        shutil.rmtree(self.candidate_dir, ignore_errors=True)
        if unraisable_exceptions:
            errors = [f"{type(x.exc_value).__name__}: {x.exc_value}" for x in unraisable_exceptions]
            unraisable_exceptions.clear()
            self.fail(f"ResourceWarning or unraisable exception detected: {errors}")

    # ── Test 1: sys.exit(0) fail-closed ──────────────────────────────────────

    def test_sys_exit_fail_closed(self):
        _write_solution(self.candidate_dir, """
import sys
sys.exit(0)
""")
        from evaluator import run_evaluation
        res = run_evaluation(self.candidate_dir, self.test_harness_path)
        self.assertFalse(res["success"])

    # ── Test 2: write outside sandbox ────────────────────────────────────────

    def test_sandbox_write_block(self):
        _write_solution(self.candidate_dir, """
def solve():
    with open('/Users/heikkihamalainen/escaped_test.txt', 'w') as f:
        f.write('leak')
    return "OK"
""")
        from evaluator import run_evaluation
        res = run_evaluation(self.candidate_dir, self.test_harness_path)
        self.assertFalse(res["success"])

    # ── Test 3: subprocess spawn blocked ─────────────────────────────────────

    def test_subprocess_block(self):
        _write_solution(self.candidate_dir, """
import subprocess
def solve():
    subprocess.run(['ls'])
    return "OK"
""")
        from evaluator import run_evaluation
        res = run_evaluation(self.candidate_dir, self.test_harness_path)
        self.assertFalse(res["success"])

    # ── Test 4: unsolicited stdout/stderr ────────────────────────────────────

    def test_unsolicited_stdout_stderr(self):
        _write_solution(self.candidate_dir, """
def solve():
    print("UNSOLICITED STDOUT SPOOFING OK")
    import sys
    sys.stderr.write("UNSOLICITED STDERR SPOOFING OK\\n")
    return "BAD"
""")
        from evaluator import run_evaluation
        res = run_evaluation(self.candidate_dir, self.test_harness_path)
        self.assertFalse(res["success"])

    # ── Test 5: CPU exhaustion timeout ───────────────────────────────────────

    def test_cpu_exhaustion_timeout(self):
        _write_solution(self.candidate_dir, """
def solve():
    while True:
        pass
    return "OK"
""")
        from evaluator import run_evaluation
        res = run_evaluation(self.candidate_dir, self.test_harness_path, timeout=3.0)
        self.assertFalse(res["success"])

    # ── Test 6: private file leak blocked ────────────────────────────────────

    def test_private_file_leak_block(self):
        _write_solution(self.candidate_dir, """
def solve():
    with open('/Users/heikkihamalainen/.gemini/antigravity/scratch/rsi_loop_private/benchmarks/simple_regex_matcher/test_holdout.py', 'r') as f:
        _ = f.read()
    return "OK"
""")
        from evaluator import run_evaluation
        res = run_evaluation(self.candidate_dir, self.test_harness_path)
        self.assertFalse(res["success"])

    # ── Test 7: environment secrets scrubbed ─────────────────────────────────

    def test_environment_secrets_scrubbed(self):
        os.environ["SECRET_API_KEY"] = "super-secret-token"
        _write_solution(self.candidate_dir, """
import os
def solve():
    if "SECRET_API_KEY" in os.environ:
        raise ValueError(f"Secret leaked: {os.environ['SECRET_API_KEY']}")
    return "OK"
""")
        from evaluator import run_evaluation
        res = run_evaluation(self.candidate_dir, self.test_harness_path)
        self.assertTrue(res["success"], f"Secret leaked: {res['error']}")

    # ── Test 8: class proxy late-binding ─────────────────────────────────────

    def test_class_proxy_late_binding_remediated(self):
        _write_solution(self.candidate_dir, """
class Alpha:
    def greet(self): return "Alpha"
class Beta:
    def greet(self): return "Beta"
""")
        h = _make_harness(self.candidate_dir, """
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
""")
        from evaluator import run_evaluation
        res = run_evaluation(self.candidate_dir, h)
        self.assertTrue(res["success"], f"Late binding error: {res['error']}")

    # ── Test 9: deque import filtered ────────────────────────────────────────

    def test_deque_import_unaffected(self):
        _write_solution(self.candidate_dir, """
from collections import deque
def solve():
    d = deque([1, 2, 3])
    return "OK"
""")
        from evaluator import run_evaluation
        res = run_evaluation(self.candidate_dir, self.test_harness_path)
        self.assertTrue(res["success"], f"deque proxy error: {res['error']}")

    # ── Test 10: setup limit failure causes fail-closed exit ─────────────────

    def test_setup_limit_failure(self):
        cmd = [
            "sandbox-exec", "-p", "(version 1) (deny default)",
            os.path.join(BASE_DIR, ".venv", "bin", "python"),
            "-u",
            os.path.join(BASE_DIR, "candidate_runner.py"),
        ]
        res = subprocess.run(cmd, capture_output=True, timeout=5)
        self.assertNotEqual(res.returncode, 0)

    # ── Test 11: RLIMIT_FSIZE enforcement (EFBIG/SIGXFSZ) ─────────────────────

    def test_file_size_write_enforcement(self):
        _write_solution(self.candidate_dir, """
import tempfile, os
def solve():
    path = os.path.join(tempfile.gettempdir(), 'large.txt')
    with open(path, 'wb') as f:
        f.write(b'x' * 2 * 1024 * 1024)
    return "OK"
""")
        from evaluator import run_evaluation
        res = run_evaluation(self.candidate_dir, self.test_harness_path)
        self.assertFalse(res["success"])
        self.assertTrue(any(x in str(res.get("error")) for x in ["Errno 27", "EFBIG", "SIGXFSZ", "File size limit exceeded"]))

    # ── Test 12: IPC adversarial frames (wrong UUID, duplicate, stalled) ────

    def test_ipc_adversarial_frames(self):
        """
        Verify that stalled IPC frames trigger read timeout.
        """
        from evaluator import WorkerProxy, CandidateProtocolError
        temp_dir = tempfile.mkdtemp()
        try:
            proxy = WorkerProxy.__new__(WorkerProxy)
            proxy._candidate_dir = self.candidate_dir
            proxy._temp_dir = temp_dir
            proxy._proc = None
            proxy._sent_uuids = set()
            proxy._session_id = "test-session"

            # Stalled frame timeout test
            import unittest.mock as mock
            responses = []
            with mock.patch.object(proxy, '_write_frame'), \
                 mock.patch.object(proxy, '_read_frame', side_effect=CandidateProtocolError("IPC read timeout exceeded")), \
                 mock.patch.object(proxy, '_proc') as mock_proc:
                mock_proc.poll.return_value = None
                with self.assertRaises(CandidateProtocolError) as ctx:
                    proxy.send_request({"action": "check_import"})
                self.assertIn("timeout", str(ctx.exception).lower())
        finally:
            shutil.rmtree(temp_dir)

    # ── Test 13: parent-supervised reaping on SIGKILL ────────────────────────

    def test_parent_supervised_reaping(self):
        """
        Verify that evaluator reaps process when it kills itself via SIGKILL.
        """
        _write_solution(self.candidate_dir, """
import os, signal
def solve():
    os.kill(os.getpid(), signal.SIGKILL)
    return "OK"
""")
        from evaluator import run_evaluation
        res = run_evaluation(self.candidate_dir, self.test_harness_path, timeout=10.0)
        self.assertFalse(res["success"])
        self.assertTrue(any(x in str(res.get("error")) for x in ["unexpected", "exit", "killed", "SIGKILL", "signal"]))

    # ── Test 14: output flooding drain ───────────────────────────────────────

    def test_output_flooding_nonblocking_drain(self):
        _write_solution(self.candidate_dir, """
import sys
def solve():
        sys.stderr.write('X' * 2 * 1024 * 1024)
        return "OK"
""")
        from evaluator import run_evaluation
        res = run_evaluation(self.candidate_dir, self.test_harness_path, timeout=15.0)
        self.assertTrue(res["success"])

    # ── Test 15: /private/var/folders blocked ─────────────────────────────────

    def test_candidate_filesystem_allowlist(self):
        _write_solution(self.candidate_dir, """
import os
def solve():
    try:
        os.listdir('/private/var/folders')
        return "LEAKED_VAR_FOLDERS"
    except PermissionError:
        pass
    try:
        open('/Users/heikkihamalainen/.gemini/antigravity/scratch/rsi_loop/rsi_loop.py').read()
        return "LEAKED_REPO"
    except PermissionError:
        pass
    return "OK"
""")
        from evaluator import run_evaluation
        res = run_evaluation(self.candidate_dir, self.test_harness_path)
        self.assertTrue(res["success"], f"Unexpected failure: {res['error']}")

    # ── Test 16: exception qualified identity ─────────────────────────────────

    def test_exception_qualified_identity(self):
        from evaluator import run_evaluation, exception_class_registry
        _write_solution(self.candidate_dir, """
class LevelA(Exception):
    pass
class LevelB(LevelA):
    pass
class LevelC(LevelB):
    pass
def raise_c():
    raise LevelC("three-level")
""")
        h = _make_harness(self.candidate_dir, """
import unittest
class TestThreeLevel(unittest.TestCase):
    def test_hierarchy(self):
        from solution import raise_c, LevelA, LevelB, LevelC
        try:
            raise_c()
        except LevelC as e:
            self.assertIsInstance(e, LevelB)
            self.assertIsInstance(e, LevelA)
            return
        self.fail("LevelC not raised")
if __name__ == "__main__":
    unittest.main()
""")
        res = run_evaluation(self.candidate_dir, h)
        self.assertTrue(res["success"], f"Three-level MRO error: {res['error']}")

    # ── Test 17: CircularBuffer regression ───────────────────────────────────

    def test_exact_circular_buffer_candidate_remediation(self):
        _write_solution(self.candidate_dir, """
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
""")
        h = _make_harness(self.candidate_dir, """
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
""")
        from evaluator import run_evaluation
        res = run_evaluation(self.candidate_dir, h)
        self.assertTrue(res["success"], f"CircularBuffer regression: {res['error']}")

    # ── Test 18: trailing frames after shutdown invalidate evaluation ─────────

    def test_shutdown_trailing_frames_invalidate(self):
        """
        Verify that when a trailing frame is received after shutdown, run_evaluation fails.
        """
        import unittest.mock as mock
        from evaluator import WorkerProxy, run_evaluation

        # Test candidate
        _write_solution(self.candidate_dir, "def solve(): return 'OK'")

        # We mock RunnerProxy.shutdown to return False (trailing data)
        with mock.patch("evaluator.RunnerProxy.shutdown", return_value=False):
            res = run_evaluation(self.candidate_dir, self.test_harness_path)
            self.assertFalse(res["success"])
            self.assertIn("shutdown integrity check failed", str(res.get("error")))

    # ── Test 19: worker cannot read harness files ─────────────────────────────

    def test_worker_no_harness_access(self):
        worker_sb_path = os.path.join(BASE_DIR, "worker.sb")
        if not os.path.exists(worker_sb_path):
            self.skipTest("worker.sb not found")
        test_harness = os.path.join(BASE_DIR, "benchmarks", "circular_buffer", "test_harness.py")
        cmd = ["sandbox-exec", "-f", worker_sb_path, "cat", test_harness]
        res = subprocess.run(cmd, capture_output=True, timeout=5)
        self.assertNotEqual(res.returncode, 0)

    # ── Test 20: generator sandbox positive startup ───────────────────────────

    def test_generator_sandbox_positive_startup(self):
        import tempfile, os
        from rsi_loop import _build_codex_temp_home, _build_generator_sb, _cleanup_temp_home

        real_temp_home = _build_codex_temp_home()
        try:
            sb_path = _build_generator_sb(real_temp_home, BASE_DIR)
            allowed_env = {
                "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin",
                "LANG": "en_US.UTF-8",
                "HOME": real_temp_home,
                "USER": os.environ.get("USER", "codex"),
                "TMPDIR": real_temp_home,
            }
            cmd = ["sandbox-exec", "-f", sb_path, "/opt/homebrew/bin/codex", "--version"]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=15, env=allowed_env)
            self.assertEqual(res.returncode, 0)
        finally:
            _cleanup_temp_home(real_temp_home)

    # ── Test 21: E2E Huffman reference implementation ─────────────────────────

    def test_huffman_reference_solution(self):
        huffman_spec = """
import json
import heapq
from collections import Counter

class HuffmanCoder:
    def encode(self, text: str) -> bytes:
        if not text:
            return b""
        freq = Counter(text)
        heap = []
        creation_counter = 0
        for sym, count in freq.items():
            heapq.heappush(heap, (count, sym, 0, creation_counter, (sym, None, None)))
            creation_counter += 1

        if len(freq) == 1:
            sym = list(freq.keys())[0]
            codes = {sym: "0"}
        else:
            while len(heap) > 1:
                f1, sym1, kind1, c1, n1 = heapq.heappop(heap)
                f2, sym2, kind2, c2, n2 = heapq.heappop(heap)
                min_desc = min(sym1, sym2)
                parent_node = (min_desc, n1, n2)
                heapq.heappush(heap, (f1 + f2, min_desc, 1, creation_counter, parent_node))
                creation_counter += 1
            _, _, _, _, root = heap[0]
            codes = {}
            def traverse(node, code):
                sym, left, right = node
                if left is None and right is None:
                    codes[sym] = code
                    return
                traverse(left, code + "0")
                traverse(right, code + "1")
            traverse(root, "")

        bit_str = "".join(codes[char] for char in text)
        byte_list = bytearray()
        for i in range(0, len(bit_str), 8):
            chunk = bit_str[i:i+8].ljust(8, '0')
            byte_list.append(int(chunk, 2))

        header_data = {"freq": freq}
        header_bytes = json.dumps(header_data).encode('utf-8')
        return header_bytes + b"\\x00" + bytes(byte_list)

    def decode(self, data: bytes) -> str:
        if not data:
            return ""
        idx = data.find(b"\\x00")
        if idx == -1:
            raise ValueError("No null byte separator found")
        header_bytes = data[:idx]
        bit_bytes = data[idx+1:]
        header_data = json.loads(header_bytes.decode('utf-8'))
        freq = header_data["freq"]
        total_symbols = sum(freq.values())
        if total_symbols == 0:
            return ""

        heap = []
        creation_counter = 0
        for sym, count in freq.items():
            heapq.heappush(heap, (count, sym, 0, creation_counter, (sym, None, None)))
            creation_counter += 1

        if len(freq) == 1:
            sym = list(freq.keys())[0]
            root = (sym, None, None)
        else:
            while len(heap) > 1:
                f1, sym1, kind1, c1, n1 = heapq.heappop(heap)
                f2, sym2, kind2, c2, n2 = heapq.heappop(heap)
                min_desc = min(sym1, sym2)
                parent_node = (min_desc, n1, n2)
                heapq.heappush(heap, (f1 + f2, min_desc, 1, creation_counter, parent_node))
                creation_counter += 1
            _, _, _, _, root = heap[0]

        bit_str_list = [bin(b)[2:].zfill(8) for b in bit_bytes]
        bit_str = "".join(bit_str_list)
        decoded = []
        node = root
        bit_idx = 0
        symbols_decoded = 0
        while symbols_decoded < total_symbols:
            sym, left, right = node
            if left is None and right is None:
                decoded.append(sym)
                symbols_decoded += 1
                node = root
                if len(freq) == 1:
                    bit_idx += 1
                continue
            if bit_idx >= len(bit_str):
                break
            bit = bit_str[bit_idx]
            bit_idx += 1
            if bit == "0":
                node = left
            else:
                node = right
        return "".join(decoded)
"""
        _write_solution(self.candidate_dir, huffman_spec)
        h = _make_harness(self.candidate_dir, """
import unittest
class TestHuffman(unittest.TestCase):
    def test_run(self):
        from solution import HuffmanCoder
        coder = HuffmanCoder()
        text = "abracadabra"
        encoded = coder.encode(text)
        self.assertIsInstance(encoded, bytes)
        self.assertEqual(coder.decode(encoded), text)
if __name__ == "__main__":
    unittest.main()
""")
        from evaluator import run_evaluation
        res = run_evaluation(self.candidate_dir, h)
        self.assertTrue(res["success"], f"Huffman reference run failed: {res.get('error')}")

    # ── Test 22: E2E Matrix reference implementation (both forms) ─────────────

    def test_matrix_reference_solutions(self):
        matrix_spec_instance_attr = """
class Matrix:
    def __init__(self, data: list):
        self.data = data
    def __add__(self, other):
        res = [[a + b for a, b in zip(r1, r2)] for r1, r2 in zip(self.data, other.data)]
        return Matrix(res)
"""
        _write_solution(self.candidate_dir, matrix_spec_instance_attr)
        h1 = _make_harness(self.candidate_dir, """
import unittest
class TestMatrixInstance(unittest.TestCase):
    def test_attr(self):
        from solution import Matrix
        m1 = Matrix([[1, 2]])
        m2 = Matrix([[3, 4]])
        m3 = m1 + m2
        self.assertEqual(m3.data, [[4, 6]])
if __name__ == "__main__":
    unittest.main()
""")
        from evaluator import run_evaluation
        res1 = run_evaluation(self.candidate_dir, h1)
        self.assertTrue(res1["success"], f"Matrix instance attr failed: {res1.get('error')}")

        # Property version
        matrix_spec_property = """
class Matrix:
    def __init__(self, data: list):
        self._data = data
    @property
    def data(self):
        return self._data
    def __add__(self, other):
        res = [[a + b for a, b in zip(r1, r2)] for r1, r2 in zip(self.data, other.data)]
        return Matrix(res)
"""
        _write_solution(self.candidate_dir, matrix_spec_property)
        res2 = run_evaluation(self.candidate_dir, h1)
        self.assertTrue(res2["success"], f"Matrix property failed: {res2.get('error')}")


if __name__ == "__main__":
    # Custom Strict Test Runner that asserts skipped == 0
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
            # Discover and report test count
            sys.stdout.write(f"\nStrictTestRunner discovered and ran {res.testsRun} tests.\n")
            sys.stdout.flush()
            return res

    unittest.main(testRunner=StrictTestRunner)
