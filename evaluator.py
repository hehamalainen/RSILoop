import sys
import os
import json
import subprocess
import tempfile
import signal
import types
import unittest

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Dynamic exception registry to dynamically instantiate exact exception class hierarchies
exception_class_registry = {}

def get_or_create_exception_class(class_name, mro_list):
    if class_name in exception_class_registry:
        return exception_class_registry[class_name]
        
    # Reconstruct dynamic inheritance hierarchy without duplicates/redundancies
    bases = []
    for base_name in mro_list[1:]:
        if base_name in ("BaseException", "Exception", "object"):
            continue
        else:
            # Recursively build/get exception classes
            bases.append(get_or_create_exception_class(base_name, [base_name, "Exception"]))
            
    if not bases:
        bases = [Exception]
        
    exc_cls = type(class_name, tuple(bases), {})
    exception_class_registry[class_name] = exc_cls
    return exc_cls

def handle_response(resp):
    if resp.get("status") == "success":
        return resp.get("result")
    elif resp.get("status") == "exception":
        exc_type = resp.get("type")
        msg = resp.get("message", "")
        tb = resp.get("traceback")
        if tb:
            sys.stderr.write(f"\n--- Sandbox Worker Traceback ---\n{tb}\n---------------------------------\n")
            sys.stderr.flush()
            
        if exc_type in exception_class_registry:
            raise exception_class_registry[exc_type](msg)
        elif exc_type == "ValueError":
            raise ValueError(msg)
        elif exc_type == "TypeError":
            raise TypeError(msg)
        elif exc_type == "IndexError":
            raise IndexError(msg)
        elif exc_type == "KeyError":
            raise KeyError(msg)
        elif exc_type == "AttributeError":
            raise AttributeError(msg)
        else:
            raise get_or_create_exception_class(exc_type, [exc_type, "Exception"])(msg)
    else:
        raise RuntimeError(f"Sandbox Error: {resp.get('message', 'Unknown error')}")

class WorkerProxy:
    def __init__(self, candidate_dir, temp_dir):
        self.proc = None
        self.candidate_dir = os.path.abspath(candidate_dir)
        self.temp_dir = os.path.abspath(temp_dir)
        self.start_worker()

    def start_worker(self):
        template_path = os.path.join(BASE_DIR, "candidate.sb.template")
        if not os.path.exists(template_path):
            raise RuntimeError(f"Sandbox template missing at {template_path}")
            
        with open(template_path, "r") as f:
            template = f.read()
        
        sb_content = template.format(
            candidate_dir=self.candidate_dir,
            temp_dir=self.temp_dir
        )
        
        sb_path = os.path.join(self.temp_dir, "candidate.sb")
        with open(sb_path, "w") as f:
            f.write(sb_content)

        allowed_env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "LANG": "en_US.UTF-8"
        }

        venv_python = os.path.join(BASE_DIR, ".venv", "bin", "python")
        cmd = [
            "sandbox-exec", "-f", sb_path,
            venv_python, "-u", os.path.join(BASE_DIR, "sandbox_worker.py")
        ]
        
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=allowed_env,
                cwd=self.candidate_dir,
                start_new_session=True
            )
        except Exception as e:
            raise RuntimeError(f"Sandbox startup failed: {e}")

    def send_request(self, req):
        if self.proc is None or self.proc.poll() is not None:
            raise RuntimeError("Worker process is not running")
        
        data = json.dumps(req)
        try:
            self.proc.stdin.write(f"{len(data)}\n{data}\n")
            self.proc.stdin.flush()
        except Exception as e:
            raise RuntimeError(f"Failed to write to worker: {e}")

        try:
            length_line = self.proc.stdout.readline()
            if not length_line:
                self.proc.poll()
                stderr_data = self.proc.stderr.read(1024)
                raise RuntimeError(f"Worker exited early. Stderr:\n{stderr_data}")
            length = int(length_line.strip())
            if length > 10 * 1024 * 1024:
                raise ValueError("Oversized worker response")
            resp_data = self.proc.stdout.read(length)
            if len(resp_data) != length:
                raise ValueError("Truncated worker response")
            self.proc.stdout.readline()
            return json.loads(resp_data)
        except Exception as e:
            self.close()
            raise RuntimeError(f"Worker communication failure: {e}")

    def init_class(self, class_name, args, kwargs=None):
        res = self.send_request({"action": "init_class", "class_name": class_name, "args": args, "kwargs": kwargs or {}})
        if res.get("status") == "success":
            return res.get("obj_id")
        else:
            return handle_response(res)

    def call_method(self, obj_id, method_name, args, kwargs=None):
        res = self.send_request({"action": "call_method", "obj_id": obj_id, "method_name": method_name, "args": args, "kwargs": kwargs or {}})
        return handle_response(res)

    def call_func(self, func_name, args, kwargs=None):
        res = self.send_request({"action": "call_func", "func_name": func_name, "args": args, "kwargs": kwargs or {}})
        return handle_response(res)

    def check_import(self):
        res = self.send_request({"action": "check_import"})
        return handle_response(res)

    def get_exports(self):
        res = self.send_request({"action": "get_exports"})
        if res.get("status") == "success":
            return res.get("exports", [])
        else:
            raise RuntimeError(f"Failed to get exports: {res.get('message')}")

    def close(self):
        if self.proc is not None:
            try:
                pgid = os.getpgid(self.proc.pid)
                os.killpg(pgid, signal.SIGKILL)
            except Exception:
                try:
                    self.proc.kill()
                except Exception:
                    pass
            try:
                self.proc.stdin.close()
            except Exception:
                pass
            try:
                self.proc.stdout.close()
            except Exception:
                pass
            try:
                self.proc.stderr.close()
            except Exception:
                pass
            try:
                self.proc.wait(timeout=1.0)
            except Exception:
                pass
            self.proc = None

# Global worker reference for test harness mocks
global_worker = None

# Dynamic Class Proxy Factory to cleanly bind class name parameter
def make_class_proxy(class_name):
    class DynamicClassProxy:
        def __init__(self, *args, **kwargs):
            self._class_name = class_name
            self.obj_id = global_worker.init_class(self._class_name, list(args), kwargs)
            
        def __getattr__(self, method_name):
            def make_method_proxy(*args, **kwargs):
                return global_worker.call_method(self.obj_id, method_name, list(args), kwargs)
            return make_method_proxy
    return DynamicClassProxy

def run_evaluation(candidate_dir, test_harness_path, timeout=3.0):
    global global_worker
    
    if not os.path.exists("/usr/bin/sandbox-exec"):
        return {"success": False, "error": "sandbox-exec not available on system"}

    temp_dir = tempfile.mkdtemp(prefix="rsi_run_", dir="/tmp")
    os.chmod(temp_dir, 0o700)

    try:
        global_worker = WorkerProxy(candidate_dir, temp_dir)
        global_worker.check_import()
        exports_res = global_worker.get_exports()
        
        mock_solution = types.ModuleType("solution")

        for item in exports_res:
            name = item["name"]
            itype = item["type"]
            
            if itype == "function":
                def make_func_proxy(fname):
                    return lambda *args, **kwargs: global_worker.call_func(fname, list(args), kwargs)
                setattr(mock_solution, name, make_func_proxy(name))
                
            elif itype == "class":
                is_exception = item.get("is_exception", False)
                mro_list = item.get("mro", [])
                
                if is_exception:
                    exc_cls = get_or_create_exception_class(name, mro_list)
                    setattr(mock_solution, name, exc_cls)
                else:
                    setattr(mock_solution, name, make_class_proxy(name))
                
            elif itype == "variable":
                setattr(mock_solution, name, item["value"])

        sys.modules["solution"] = mock_solution

        import importlib.util
        spec = importlib.util.spec_from_file_location("test_module", test_harness_path)
        test_module = importlib.util.module_from_spec(spec)
        
        def run_tests():
            spec.loader.exec_module(test_module)
            suite = unittest.defaultTestLoader.loadTestsFromModule(test_module)
            
            import io
            stream = io.StringIO()
            runner = unittest.TextTestRunner(stream=stream, verbosity=2)
            res = runner.run(suite)
            return res, stream.getvalue()

        def timeout_handler(signum, frame):
            raise TimeoutError("Test execution exceeded wall-clock timeout limit")
            
        signal.signal(signal.SIGALRM, timeout_handler)
        signal.alarm(int(timeout))
        
        try:
            run_res, log_output = run_tests()
            signal.alarm(0)
            
            success = run_res.wasSuccessful()
            err_msg = None if success else log_output
            return {"success": success, "error": err_msg}
        except TimeoutError as te:
            return {"success": False, "error": str(te)}
        finally:
            signal.alarm(0)

    except Exception as e:
        return {"success": False, "error": f"Evaluation Failure: {str(e)}"}
    finally:
        if global_worker is not None:
            global_worker.close()
            global_worker = None
        if "solution" in sys.modules:
            del sys.modules["solution"]
        try:
            import shutil
            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass

if __name__ == "__main__":
    if len(sys.argv) < 3:
        if len(sys.argv) == 2:
            candidate_dir = sys.argv[1]
            test_harness_path = os.path.join(candidate_dir, "test_harness.py")
        else:
            print("Usage: python evaluator.py <candidate_dir> <test_harness_path>")
            sys.exit(1)
    else:
        candidate_dir = sys.argv[1]
        test_harness_path = sys.argv[2]
        
    result = run_evaluation(candidate_dir, test_harness_path)
    print(f"Success: {result['success']}")
    if result['error']:
        print(f"Error:\n{result['error']}")
    sys.exit(0 if result['success'] else 1)
