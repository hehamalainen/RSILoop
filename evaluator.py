"""
evaluator.py — Host parent supervisor for RSI evaluation harness.

Architecture (simplified to avoid macOS nested sandbox-exec restriction):
  evaluator.py (this file, host, not sandboxed)
    └── candidate_runner.py  (untrusted solution, under candidate.sb via sandbox-exec)

The host spawns candidate_runner.py directly under candidate.sb. All IPC
validation (UUID correlation, duplicate detection, frame validation) is
performed here in the trusted host process. There is no intermediate worker
process — the two-level sandbox-exec nesting is prohibited by macOS.

Security properties maintained:
- candidate_runner.py is isolated under candidate.sb (deny-default macOS sandbox)
- Host generates all UUIDs; runner must echo them back
- Drain threads are non-daemon; surviving threads invalidate evaluation
- EOF-based shutdown: runner closes _control_write, host sees EOF
- (module, qualname) exception class identity with MRO validation
- Exception registry cleared per evaluation
"""
import sys
import os
import json
import subprocess
import tempfile
import signal
import selectors
import threading
import types
import uuid
import unittest
import shutil

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Exception registry ────────────────────────────────────────────────────────

exception_class_registry: dict = {}


def _builtin_bases(module: str, qualname: str) -> type | None:
    BUILTINS = {
        ("builtins", "Exception"): Exception,
        ("builtins", "BaseException"): BaseException,
        ("builtins", "object"): object,
        ("builtins", "ValueError"): ValueError,
        ("builtins", "TypeError"): TypeError,
        ("builtins", "IndexError"): IndexError,
        ("builtins", "KeyError"): KeyError,
        ("builtins", "AttributeError"): AttributeError,
        ("builtins", "RuntimeError"): RuntimeError,
        ("builtins", "StopIteration"): StopIteration,
        ("builtins", "OSError"): OSError,
        ("builtins", "IOError"): IOError,
        ("builtins", "PermissionError"): PermissionError,
    }
    return BUILTINS.get((module, qualname))


def _get_or_create_exception_class(qualname: str, module: str, all_exports: list) -> type:
    key = (module, qualname)
    if key in exception_class_registry:
        return exception_class_registry[key]
    builtin = _builtin_bases(module, qualname)
    if builtin is not None:
        return builtin

    spec = None
    for export in all_exports:
        if export.get("qualname") == qualname and export.get("module") == module:
            spec = export
            break
    if spec is None:
        raise RuntimeError(
            f"Unknown non-builtin class in MRO: ({module!r}, {qualname!r}) — fail closed"
        )

    bases = []
    for base_info in spec.get("bases", []):
        b_qualname = base_info["qualname"]
        b_module = base_info["module"]
        b_builtin = _builtin_bases(b_module, b_qualname)
        if b_builtin is not None:
            if b_builtin is not object:
                bases.append(b_builtin)
        else:
            bases.append(_get_or_create_exception_class(b_qualname, b_module, all_exports))
    if not bases:
        bases = [Exception]

    exc_cls = type(qualname, tuple(bases), {"__module__": module})
    exception_class_registry[key] = exc_cls

    # Validate reconstructed MRO
    reported_mro = spec.get("mro", [])
    reconstructed_mro = exc_cls.__mro__
    for i, reported_entry in enumerate(reported_mro):
        r_qualname = reported_entry["qualname"]
        r_module = reported_entry["module"]
        if i < len(reconstructed_mro):
            actual_cls = reconstructed_mro[i]
            if actual_cls.__qualname__ != r_qualname or actual_cls.__module__ != r_module:
                raise RuntimeError(
                    f"MRO mismatch at position {i} for {qualname!r}: "
                    f"expected ({r_module!r}, {r_qualname!r}), "
                    f"got ({actual_cls.__module__!r}, {actual_cls.__qualname__!r})"
                )

    return exc_cls


def _handle_response(resp: dict, all_exports: list):
    status = resp.get("status")
    if status == "success":
        return resp.get("result")
    elif status == "exception":
        exc_qualname = resp.get("qualname", resp.get("type", "RuntimeError"))
        exc_module = resp.get("module", "builtins")
        msg = resp.get("message", "")
        tb = resp.get("traceback")
        if tb:
            sys.stderr.write(f"\n--- Runner Traceback ---\n{tb}\n------------------------\n")
            sys.stderr.flush()

        key = (exc_module, exc_qualname)
        if key in exception_class_registry:
            raise exception_class_registry[key](msg)
        builtin = _builtin_bases(exc_module, exc_qualname)
        if builtin is not None and builtin is not object:
            raise builtin(msg)
        try:
            exc_cls = _get_or_create_exception_class(exc_qualname, exc_module, all_exports)
            raise exc_cls(msg)
        except RuntimeError as reconstruct_err:
            raise RuntimeError(
                f"Sandbox exception (unresolvable type {exc_qualname!r}): {msg}"
            ) from reconstruct_err
    else:
        raise RuntimeError(f"Sandbox error: {resp.get('message', 'unknown')}")


# ── Drain thread ──────────────────────────────────────────────────────────────

_MAX_DRAIN_BYTES = 10 * 1024


class _PipeDrainer(threading.Thread):
    def __init__(self, fd: int, label: str, stop_event: threading.Event):
        super().__init__(name=f"drain-{label}", daemon=False)
        self._fd = fd
        self._label = label
        self._stop = stop_event
        self._buf = bytearray()
        self._error: Exception | None = None

    @property
    def diagnostics(self) -> bytes:
        return bytes(self._buf)

    def run(self) -> None:
        sel = selectors.DefaultSelector()
        try:
            sel.register(self._fd, selectors.EVENT_READ)
            while not self._stop.is_set():
                events = sel.select(timeout=0.1)
                if not events:
                    continue
                try:
                    chunk = os.read(self._fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                if len(self._buf) < _MAX_DRAIN_BYTES:
                    room = _MAX_DRAIN_BYTES - len(self._buf)
                    self._buf.extend(chunk[:room])
        except Exception as e:
            self._error = e
        finally:
            sel.close()
            try:
                os.close(self._fd)
            except OSError:
                pass


# ── RunnerProxy ───────────────────────────────────────────────────────────────

class RunnerProxy:
    """
    Manages candidate_runner.py under candidate.sb directly from the host.
    No intermediate sandbox_worker subprocess — macOS prohibits nested sandbox-exec.
    """

    def __init__(self, candidate_dir: str, temp_dir: str):
        self._candidate_dir = os.path.realpath(candidate_dir)
        self._temp_dir = os.path.realpath(temp_dir)
        self._proc: subprocess.Popen | None = None
        self._drain_stop = threading.Event()
        self._stderr_drainer: _PipeDrainer | None = None
        self._sent_uuids: set = set()
        self._all_exports: list = []
        self._shutdown_clean: bool | None = None

    def _build_candidate_sb(self) -> str:
        template_path = os.path.join(BASE_DIR, "candidate.sb.template")
        if not os.path.exists(template_path):
            raise RuntimeError(f"Sandbox template missing: {template_path}")
        with open(template_path) as f:
            tmpl = f.read()
        sb_content = tmpl.format(
            candidate_dir=self._candidate_dir,
            temp_dir=self._temp_dir,
        )
        sb_path = os.path.join(self._temp_dir, "candidate.sb")
        with open(sb_path, "w") as f:
            f.write(sb_content)
        return sb_path

    def start(self) -> None:
        candidate_sb_path = self._build_candidate_sb()

        if not os.path.exists("/usr/bin/sandbox-exec"):
            raise RuntimeError("sandbox-exec not available on this system")

        venv_python = os.path.join(BASE_DIR, ".venv", "bin", "python")
        runner_path = os.path.join(BASE_DIR, "candidate_runner.py")

        allowed_env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
            "LANG": "en_US.UTF-8",
            "PYTHONIOENCODING": "utf-8",
        }

        cmd = [
            "/usr/bin/sandbox-exec", "-f", candidate_sb_path,
            venv_python, "-u", runner_path,
        ]

        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,  # binary; framing handles encoding
            env=allowed_env,
            cwd=self._candidate_dir,
            close_fds=True,
            start_new_session=True,
        )

        # Drain stderr (candidate diagnostics)
        stderr_fd = os.dup(self._proc.stderr.fileno())
        self._proc.stderr.close()
        self._stderr_drainer = _PipeDrainer(stderr_fd, "runner-stderr", self._drain_stop)
        self._stderr_drainer.start()

    def _write_frame(self, data: dict) -> None:
        payload = json.dumps(data).encode()
        header = f"{len(payload)}\n".encode()
        self._proc.stdin.write(header + payload + b"\n")
        self._proc.stdin.flush()

    def _read_frame(self) -> dict | None:
        length_line = self._proc.stdout.readline()
        if not length_line:
            return None
        try:
            length = int(length_line.strip())
        except ValueError:
            raise ValueError(f"Malformed length from runner: {length_line!r}")
        if length > 10 * 1024 * 1024:
            raise ValueError("Oversized runner response")
        data = self._proc.stdout.read(length)
        if len(data) != length:
            raise ValueError(f"Truncated runner response: expected {length}, got {len(data)}")
        self._proc.stdout.readline()
        return json.loads(data)

    def send_request(self, req: dict) -> dict:
        if self._proc is None or self._proc.poll() is not None:
            raise RuntimeError("Runner process is not running")
        req_uuid = str(uuid.uuid4())
        if req_uuid in self._sent_uuids:
            raise RuntimeError("UUID collision")
        self._sent_uuids.add(req_uuid)
        req["uuid"] = req_uuid

        self._write_frame(req)
        resp = self._read_frame()
        if resp is None:
            stderr_data = self._stderr_drainer.diagnostics.decode(errors="replace") if self._stderr_drainer else ""
            raise RuntimeError(
                f"Runner exited unexpectedly. Stderr:\n{stderr_data[:2048]}"
            )

        resp_uuid = resp.get("uuid")
        if resp_uuid != req_uuid:
            raise RuntimeError(
                f"UUID mismatch from runner: sent {req_uuid!r}, got {resp_uuid!r}"
            )
        return resp

    def shutdown(self) -> bool:
        if self._proc is None or self._proc.poll() is not None:
            return True
        req_uuid = str(uuid.uuid4())
        try:
            self._write_frame({"action": "shutdown", "uuid": req_uuid})
            resp = self._read_frame()
            if resp is None:
                self._shutdown_clean = False
                return False
            if resp.get("status") == "shutdown_ok" and resp.get("uuid") == req_uuid:
                # Read to EOF — any trailing frame invalidates
                extra = self._read_frame()
                if extra is not None:
                    self._shutdown_clean = False
                    return False
                self._shutdown_clean = True
                return True
            else:
                self._shutdown_clean = False
                return False
        except Exception:
            self._shutdown_clean = False
            return False

    def close(self) -> None:
        self._drain_stop.set()
        if self._proc is not None and self._proc.poll() is None:
            try:
                import signal as _sig
                pgid = os.getpgid(self._proc.pid)
                os.killpg(pgid, _sig.SIGKILL)
            except Exception:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            try:
                self._proc.stdin.close()
            except Exception:
                pass
            try:
                self._proc.stdout.close()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                pass

        if self._stderr_drainer is not None:
            self._stderr_drainer.join(timeout=2.0)

    def assert_no_threads(self) -> None:
        if self._stderr_drainer is not None and self._stderr_drainer.is_alive():
            raise RuntimeError(
                f"Drain thread {self._stderr_drainer.name!r} still alive after shutdown — evaluation invalidated"
            )

    # ── High-level commands ──────────────────────────────────────────────────

    def check_import(self):
        res = self.send_request({"action": "check_import"})
        return _handle_response(res, self._all_exports)

    def get_exports(self) -> list:
        res = self.send_request({"action": "get_exports"})
        if res.get("status") == "success":
            self._all_exports = res.get("exports", [])
            return self._all_exports
        raise RuntimeError(f"Failed to get exports: {res.get('message')}")

    def init_class(self, class_name: str, args: list, kwargs: dict | None = None) -> int:
        res = self.send_request({
            "action": "init_class",
            "class_name": class_name,
            "args": args,
            "kwargs": kwargs or {},
        })
        if res.get("status") == "success":
            return res.get("obj_id")
        return _handle_response(res, self._all_exports)

    def call_method(self, obj_id: int, method_name: str, args: list, kwargs: dict | None = None):
        res = self.send_request({
            "action": "call_method",
            "obj_id": obj_id,
            "method_name": method_name,
            "args": args,
            "kwargs": kwargs or {},
        })
        return _handle_response(res, self._all_exports)

    def call_func(self, func_name: str, args: list, kwargs: dict | None = None):
        res = self.send_request({
            "action": "call_func",
            "func_name": func_name,
            "args": args,
            "kwargs": kwargs or {},
        })
        return _handle_response(res, self._all_exports)


# ── Alias for backward compatibility (tests reference WorkerProxy) ────────────
WorkerProxy = RunnerProxy

# ── Global proxy for test harness ─────────────────────────────────────────────

global_runner: RunnerProxy | None = None


def make_class_proxy(class_name: str):
    class DynamicClassProxy:
        def __init__(self, *args, **kwargs):
            self._class_name = class_name
            self.obj_id = global_runner.init_class(self._class_name, list(args), kwargs)

        def __getattr__(self, method_name: str):
            def _method_proxy(*args, **kwargs):
                return global_runner.call_method(self.obj_id, method_name, list(args), kwargs)
            return _method_proxy

    DynamicClassProxy.__name__ = class_name
    DynamicClassProxy.__qualname__ = class_name
    return DynamicClassProxy


# ── Main evaluation entry point ───────────────────────────────────────────────

def run_evaluation(candidate_dir: str, test_harness_path: str, timeout: float = 30.0) -> dict:
    global global_runner, exception_class_registry

    if not os.path.exists("/usr/bin/sandbox-exec"):
        return {"success": False, "error": "sandbox-exec not available"}

    exception_class_registry.clear()

    temp_dir = tempfile.mkdtemp(prefix="rsi_run_")
    os.chmod(temp_dir, 0o700)

    runner = RunnerProxy(candidate_dir, temp_dir)

    result_override = None
    try:
        try:
            runner.start()
        except Exception as e:
            return {"success": False, "error": f"Runner startup failed: {e}"}

        global_runner = runner

        try:
            runner.check_import()
        except Exception as e:
            return {"success": False, "error": f"Import failed: {e}"}

        try:
            exports = runner.get_exports()
        except Exception as e:
            return {"success": False, "error": f"Export failed: {e}"}

        mock_solution = types.ModuleType("solution")

        for item in exports:
            name = item["name"]
            itype = item["type"]
            if itype == "function":
                def _make_func_proxy(fname):
                    return lambda *args, **kwargs: global_runner.call_func(fname, list(args), kwargs)
                setattr(mock_solution, name, _make_func_proxy(name))
            elif itype == "class":
                if item.get("is_exception", False):
                    exc_cls = _get_or_create_exception_class(
                        item["qualname"], item["module"], exports
                    )
                    setattr(mock_solution, name, exc_cls)
                else:
                    setattr(mock_solution, name, make_class_proxy(name))
            elif itype == "variable":
                setattr(mock_solution, name, item["value"])

        sys.modules["solution"] = mock_solution

        import importlib.util, io
        spec = importlib.util.spec_from_file_location("test_module", test_harness_path)
        test_module = importlib.util.module_from_spec(spec)

        def _timeout_handler(signum, frame):
            raise TimeoutError("Test execution exceeded wall-clock timeout")

        signal.signal(signal.SIGALRM, _timeout_handler)
        signal.alarm(int(timeout))
        try:
            spec.loader.exec_module(test_module)
            suite = unittest.defaultTestLoader.loadTestsFromModule(test_module)
            stream = io.StringIO()
            run_res = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
            signal.alarm(0)
            success = run_res.wasSuccessful()
            eval_result = {"success": success, "error": None if success else stream.getvalue()}
        except TimeoutError as te:
            eval_result = {"success": False, "error": str(te)}
        finally:
            signal.alarm(0)

    except Exception as e:
        eval_result = {"success": False, "error": f"Evaluation failure: {e}"}
    finally:
        shutdown_clean = runner.shutdown()
        runner.close()

        thread_err = None
        try:
            runner.assert_no_threads()
        except RuntimeError as te:
            thread_err = te

        global_runner = None
        if "solution" in sys.modules:
            del sys.modules["solution"]

        try:
            shutil.rmtree(temp_dir)
        except Exception as ce:
            sys.stderr.write(f"WARNING: temp dir cleanup failed: {ce}\n")

        if thread_err is not None:
            result_override = {"success": False, "error": str(thread_err)}

    return result_override if result_override is not None else eval_result


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python evaluator.py <candidate_dir> [test_harness_path]")
        sys.exit(1)
    candidate_dir = sys.argv[1]
    test_harness_path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(candidate_dir, "test_harness.py")
    result = run_evaluation(candidate_dir, test_harness_path)
    print(f"Success: {result['success']}")
    if result.get("error"):
        print(f"Error:\n{result['error']}")
    sys.exit(0 if result["success"] else 1)
