"""
Campaign 2 Sandboxed Local Model Proxy Calibration Harness (A4 & A5).

CONTAINMENT & EVALUATION ARCHITECTURE:
- Host Supervisor: Runs outside candidate workspace. Imports and executes test_harness.py in host memory.
- Sandboxed Candidate Worker: Evaluates ONLY candidate.py inside sandbox-exec container over dedicated IPC pipes (FDs 3 & 4).
  Candidate stdout/stderr are redirected to /dev/null, preventing IPC spoofing.
  Has ZERO access to test_harness.py, solution.py, repository files, home directory, .codex, or network.
  Worker enforces resource limits (RLIMITs fail-closed) and executes a READY handshake before importing candidate code.
- Collision-Free Tagged Codec: Encodes values in explicit `{"t": ...}` tagged envelopes, preventing collisions with application dicts.
- Timeout Attribution: ONLY confirmed worker RLIMIT_CPU termination (-SIGXCPU or 152) is classified as candidate timeout.
  Exit code 1, SIGKILL, 137, broken pipe, or unexplained worker exit raises InfrastructureError (fail-closed).
- Generator: Executes local_model_runner.py under sandbox-exec deny-default profile with explicit model permissions.
"""

import os
import sys
import json
import time
import base64
import struct
import shutil
import hashlib
import tempfile
import subprocess
import importlib.util
import signal
import selectors
from typing import Dict, Any, List, Tuple

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "campaign2", "proxy_config.json")
BENCHMARKS_DIR = os.path.join(BASE_DIR, "benchmarks", "campaign2")
MAX_FRAME_SIZE = 1 * 1024 * 1024  # 1 MB max framed IPC payload limit


class InfrastructureError(RuntimeError):
    """Raised when an infrastructure, sandbox, serialization, or harness protocol error occurs. Invalidates calibration."""
    pass


class CandidateTimeoutError(Exception):
    """Raised when candidate code execution times out due to CPU limits."""
    pass


class CandidateObjectProxy:
    """Proxy object representing a sandboxed candidate object instance in host memory."""
    def __init__(self, obj_id: str, client: Any):
        super().__setattr__("_obj_id", obj_id)
        super().__setattr__("_client", client)

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def proxy_method(*args, **kwargs):
            enc_args = [TaggedCodec.encode(a, self._client.register_callback, None, self._client.register_host_object) for a in args]
            enc_kwargs = {k: TaggedCodec.encode(v, self._client.register_callback, None, self._client.register_host_object) for k, v in kwargs.items()}
            self._client.counter += 1
            req_id = f"req_{self._client.counter}"
            req = {
                "op": "call_method",
                "id": req_id,
                "obj_id": self._obj_id,
                "method_name": name,
                "args": enc_args,
                "kwargs": enc_kwargs
            }
            return self._client.send_request(req)

        self._client.counter += 1
        req_id = f"req_{self._client.counter}"
        req_attr = {
            "op": "get_attr",
            "id": req_id,
            "obj_id": self._obj_id,
            "attr_name": name
        }
        res = self._client.send_request(req_attr)
        if isinstance(res, dict) and res.get("__status__") == "error":
            return proxy_method
        return res

    def __setattr__(self, name: str, value: Any):
        if name.startswith("_"):
            super().__setattr__(name, value)
        else:
            self._client.counter += 1
            req_id = f"req_{self._client.counter}"
            enc_val = TaggedCodec.encode(value, self._client.register_callback, None, self._client.register_host_object)
            req = {
                "op": "set_attr",
                "id": req_id,
                "obj_id": self._obj_id,
                "attr_name": name,
                "value": enc_val
            }
            self._client.send_request(req)


class TaggedCodec:
    """Collision-free tagged serializer for primitives, bytes, tuples, dicts, callbacks, host objects, candidate objects, and exceptions."""
    @classmethod
    def encode(cls, obj: Any, register_cb_fn=None, register_obj_fn=None, register_host_obj_fn=None) -> Any:
        if obj is None or isinstance(obj, (int, float, str, bool)):
            return {"t": "prim", "v": obj}
        elif isinstance(obj, bytes):
            return {"t": "bytes", "v": base64.b64encode(obj).decode("ascii")}
        elif isinstance(obj, tuple):
            return {"t": "tuple", "v": [cls.encode(x, register_cb_fn, register_obj_fn, register_host_obj_fn) for x in obj]}
        elif isinstance(obj, list):
            return {"t": "list", "v": [cls.encode(x, register_cb_fn, register_obj_fn, register_host_obj_fn) for x in obj]}
        elif isinstance(obj, CandidateObjectProxy):
            return {"t": "candidate_object", "id": obj._obj_id}
        elif isinstance(obj, dict):
            if register_host_obj_fn:
                if any(isinstance(v, (dict, list)) for v in obj.values()):
                    pairs = [[cls.encode(k, register_cb_fn, register_obj_fn, register_host_obj_fn), cls.encode(v, register_cb_fn, register_obj_fn, register_host_obj_fn)] for k, v in obj.items()]
                    return {"t": "dict", "pairs": pairs}
                h_id = register_host_obj_fn(obj)
                return {"t": "host_object", "id": h_id}
            pairs = [[cls.encode(k, register_cb_fn, register_obj_fn, register_host_obj_fn), cls.encode(v, register_cb_fn, register_obj_fn, register_host_obj_fn)] for k, v in obj.items()]
            return {"t": "dict", "pairs": pairs}
        elif isinstance(obj, BaseException):
            return {
                "t": "exception",
                "module": type(obj).__module__,
                "qualname": type(obj).__qualname__,
                "message": str(obj)
            }
        elif callable(obj):
            if register_cb_fn:
                cb_id = register_cb_fn(obj)
                return {"t": "host_callback", "id": cb_id}
            raise TypeError(f"Callable cannot be encoded without callback registrar: {obj}")
        else:
            if register_obj_fn:
                obj_id = register_obj_fn(obj)
                return {"t": "candidate_object", "id": obj_id}
            if register_host_obj_fn:
                h_id = register_host_obj_fn(obj)
                return {"t": "host_object", "id": h_id}
            raise TypeError(f"Unsupported type for TaggedCodec: {type(obj).__name__}")

    @classmethod
    def decode(cls, env: Any, make_cb_fn=None, make_obj_proxy_fn=None, get_worker_obj_fn=None, make_host_obj_proxy_fn=None, get_host_obj_fn=None) -> Any:
        if not isinstance(env, dict) or "t" not in env:
            if env is None or isinstance(env, (int, float, str, bool)):
                return env
            if isinstance(env, list):
                return [cls.decode(x, make_cb_fn, make_obj_proxy_fn, get_worker_obj_fn, make_host_obj_proxy_fn, get_host_obj_fn) for x in env]
            if isinstance(env, dict):
                return {cls.decode(k, make_cb_fn, make_obj_proxy_fn, get_worker_obj_fn, make_host_obj_proxy_fn, get_host_obj_fn): cls.decode(v, make_cb_fn, make_obj_proxy_fn, get_worker_obj_fn, make_host_obj_proxy_fn, get_host_obj_fn) for k, v in env.items()}
            return env

        t = env.get("t")
        if t == "prim":
            return env["v"]
        elif t == "bytes":
            return base64.b64decode(env["v"].encode("ascii"))
        elif t == "tuple":
            return tuple(cls.decode(x, make_cb_fn, make_obj_proxy_fn, get_worker_obj_fn, make_host_obj_proxy_fn, get_host_obj_fn) for x in env["v"])
        elif t == "list":
            return [cls.decode(x, make_cb_fn, make_obj_proxy_fn, get_worker_obj_fn, make_host_obj_proxy_fn, get_host_obj_fn) for x in env["v"]]
        elif t == "dict":
            return {cls.decode(k, make_cb_fn, make_obj_proxy_fn, get_worker_obj_fn, make_host_obj_proxy_fn, get_host_obj_fn): cls.decode(v, make_cb_fn, make_obj_proxy_fn, get_worker_obj_fn, make_host_obj_proxy_fn, get_host_obj_fn) for k, v in env["pairs"]}
        elif t == "host_callback":
            if make_cb_fn:
                return make_cb_fn(env["id"])
            raise ValueError("Host callback received without handler factory")
        elif t == "host_object":
            if get_host_obj_fn:
                return get_host_obj_fn(env["id"])
            if make_host_obj_proxy_fn:
                return make_host_obj_proxy_fn(env["id"])
            raise ValueError("host_object received without handler")
        elif t == "candidate_object":
            if get_worker_obj_fn:
                return get_worker_obj_fn(env["id"])
            if make_obj_proxy_fn:
                return make_obj_proxy_fn(env["id"])
            raise ValueError("candidate_object received without handler")
        elif t == "exception":
            exc_module = env.get("module", "builtins")
            exc_qualname = env.get("qualname", "RuntimeError")
            msg = env.get("message", "")
            try:
                mod = sys.modules.get(exc_module) or __import__(exc_module, fromlist=[exc_qualname])
                exc_cls = getattr(mod, exc_qualname, None)
                if not (isinstance(exc_cls, type) and issubclass(exc_cls, BaseException)):
                    exc_cls = getattr(sys.modules["builtins"], exc_qualname, RuntimeError)
            except Exception:
                exc_cls = getattr(sys.modules["builtins"], exc_qualname, RuntimeError)
            if not (isinstance(exc_cls, type) and issubclass(exc_cls, BaseException)):
                exc_cls = RuntimeError
            return exc_cls(msg)
        else:
            raise ValueError(f"Unknown tagged type: {t}")


def compute_combined_native_library_hash() -> str:
    """Computes a single sorted combined SHA256 hash across all native shared libraries in llama_cpp/lib."""
    try:
        import llama_cpp
        llama_pkg_dir = os.path.dirname(llama_cpp.__file__)
        lib_dir = os.path.join(llama_pkg_dir, "lib")
        if not os.path.exists(lib_dir):
            lib_dir = llama_pkg_dir

        so_files = []
        for root, _, files in os.walk(lib_dir):
            for f in sorted(files):
                if f.endswith((".so", ".dylib")):
                    so_files.append(os.path.join(root, f))

        so_files = sorted(so_files)
        if not so_files:
            return ""

        sha = hashlib.sha256()
        for fpath in so_files:
            with open(fpath, "rb") as f:
                sha.update(f.read())
        return sha.hexdigest()
    except Exception as e:
        raise InfrastructureError(f"Failed to compute combined native library hash: {e}")


def load_proxy_config() -> Dict[str, Any]:
    if not os.path.exists(CONFIG_PATH):
        raise InfrastructureError(f"Missing proxy config: {CONFIG_PATH}")
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def verify_proxy_config_provenance() -> bool:
    """Dynamically verifies Python SHA256, llama_cpp version, combined native lib hash, n_ctx, max_tokens, stop sequences."""
    cfg = load_proxy_config()

    # 1. Python executable SHA256
    py_executable = sys.executable
    if not os.path.exists(py_executable):
        raise InfrastructureError(f"Configured Python executable missing: {py_executable}")
    with open(py_executable, "rb") as f:
        py_hash = hashlib.sha256(f.read()).hexdigest()
    if py_hash != cfg.get("python_executable_sha256"):
        raise InfrastructureError(
            f"Python executable SHA256 mismatch: expected {cfg.get('python_executable_sha256')}, got {py_hash}"
        )

    # 2. llama_cpp version
    try:
        import llama_cpp
        actual_ver = getattr(llama_cpp, "__version__", "")
    except Exception as e:
        raise InfrastructureError(f"Failed to import llama_cpp: {e}")
    if actual_ver != cfg.get("llama_cpp_version"):
        raise InfrastructureError(
            f"llama_cpp version mismatch: expected {cfg.get('llama_cpp_version')}, got {actual_ver}"
        )

    # 3. Native library hash
    native_hash = compute_combined_native_library_hash()
    if native_hash != cfg.get("native_lib_sha256"):
        raise InfrastructureError(
            f"Native library combined SHA256 mismatch: expected {cfg.get('native_lib_sha256')}, got {native_hash}"
        )

    # 4. Generation parameters
    req_fields = ["temperature", "top_p", "n_ctx", "max_tokens", "stop"]
    for field in req_fields:
        if field not in cfg:
            raise InfrastructureError(f"Missing required proxy config generation parameter: {field}")

    return True


def get_generator_sandbox_profile(repo_dir: str, gen_tmpdir: str, model_path: str, spec_path: str = None, runner_path: str = None) -> str:
    """Returns macOS sandbox-exec deny-default profile for model generator process."""
    home_dir = os.path.expanduser("~")
    real_home = os.path.realpath(home_dir)
    real_repo = os.path.realpath(repo_dir)
    spec_allow = f'(allow file-read* (literal "{spec_path}"))' if spec_path else ""
    runner_allow = f'(allow file-read* (literal "{runner_path}"))' if runner_path else ""
    return f"""(version 1)
(deny default)
(allow process-exec)
(allow process-fork)
(allow sysctl-read)
(allow mach-lookup)
(allow iokit-open)
(allow ipc-posix-shm*)
(allow file-map-executable)
(allow file-read*)
(deny file-read* (subpath "{repo_dir}"))
(deny file-read* (subpath "{real_repo}"))
(deny file-read* (subpath "{home_dir}/.codex"))
(deny file-read* (subpath "{real_home}/.codex"))
(deny file-read* (subpath "{home_dir}/.ssh"))
(deny file-read* (subpath "{real_home}/.ssh"))
(deny file-read* (subpath "{home_dir}/.aws"))
(deny file-read* (subpath "{real_home}/.aws"))
(deny file-read* (subpath "{home_dir}/.gemini"))
(deny file-read* (subpath "{real_home}/.gemini"))
(allow file-read* (subpath "{gen_tmpdir}"))
(allow file-read* (literal "{model_path}"))
{spec_allow}
{runner_allow}
(deny network*)
(deny file-write*)
(allow file-write* (subpath "{gen_tmpdir}"))
(allow file-write* (subpath "/private/tmp"))
(allow file-write* (subpath "/tmp"))
(allow file-write* (literal "/dev/null"))
(allow file-write* (literal "/dev/zero"))
"""


def get_candidate_sandbox_profile(repo_dir: str, cand_tmpdir: str) -> str:
    """Returns macOS sandbox-exec deny-default profile for candidate worker process."""
    home_dir = os.path.expanduser("~")
    real_home = os.path.realpath(home_dir)
    real_repo = os.path.realpath(repo_dir)
    return f"""(version 1)
(deny default)
(allow process-exec)
(allow sysctl-read)
(allow mach-lookup)
(allow ipc-posix-shm*)
(allow file-map-executable)
(allow file-read*)
(deny file-read* (subpath "{repo_dir}"))
(deny file-read* (subpath "{real_repo}"))
(deny file-read* (subpath "{home_dir}/.codex"))
(deny file-read* (subpath "{real_home}/.codex"))
(deny file-read* (subpath "{home_dir}/.ssh"))
(deny file-read* (subpath "{real_home}/.ssh"))
(deny file-read* (subpath "{home_dir}/.aws"))
(deny file-read* (subpath "{real_home}/.aws"))
(deny file-read* (subpath "{home_dir}/.gemini"))
(deny file-read* (subpath "{real_home}/.gemini"))
(deny file-read* (subpath "{home_dir}/.bash_history"))
(deny file-read* (subpath "{home_dir}/.zsh_history"))
(allow file-read* (subpath "{cand_tmpdir}"))
(deny process-fork)
(deny network*)
(deny file-write*)
(allow file-write* (subpath "{cand_tmpdir}"))
(allow file-write* (subpath "/private/tmp"))
(allow file-write* (subpath "/tmp"))
(allow file-write* (literal "/dev/null"))
(allow file-write* (literal "/dev/zero"))
"""


def _find_task_dir(task_id: str) -> str:
    for suite in ["suite_c", "suite_p"]:
        p = os.path.join(BENCHMARKS_DIR, suite, task_id)
        if os.path.exists(p):
            return p
    raise InfrastructureError(f"Task directory not found for: {task_id}")


def extract_code_from_output(raw_output: str) -> str:
    """Extracts candidate python code from ```python ... ``` fences."""
    if "```python" in raw_output:
        start = raw_output.find("```python") + len("```python")
        end = raw_output.find("```", start)
        if end != -1:
            return raw_output[start:end].strip()
    return raw_output.strip()


class WorkerIPCClient:
    """Client for candidate IPC worker process over dedicated OS pipe FDs with binary length-prefixed framing."""
    def __init__(self, in_fd: int, out_fd: int, deadline: float, worker_proc: subprocess.Popen = None):
        self.in_fd = in_fd
        self.out_fd = out_fd
        self.deadline = deadline
        self.worker_proc = worker_proc
        self.counter = 0
        self.host_callbacks = {}
        self.host_objects = {}
        self.infrastructure_error = None
        self.candidate_timeout = False
        self.candidate_exception_occurred = False
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.in_fd, selectors.EVENT_READ)

    def make_obj_proxy(self, obj_id: str):
        return CandidateObjectProxy(obj_id, self)

    def register_callback(self, cb_callable) -> str:
        self.counter += 1
        cb_id = f"cb_{self.counter}"
        self.host_callbacks[cb_id] = cb_callable
        return cb_id

    def register_host_object(self, obj: Any) -> str:
        self.counter += 1
        h_id = f"hobj_{self.counter}"
        self.host_objects[h_id] = obj
        return h_id

    def make_host_callback_proxy(self, cb_id: str):
        def cb_proxy(*args, **kwargs):
            return self.send_request({
                "op": "call_host_callback_from_worker",
                "id": f"cb_call_{self.counter+1}",
                "callback_id": cb_id,
                "args": [TaggedCodec.encode(a, self.register_callback, None, self.register_host_object) for a in args],
                "kwargs": {k: TaggedCodec.encode(v, self.register_callback, None, self.register_host_object) for k, v in kwargs.items()}
            })
        return cb_proxy

    def _read_exact(self, num_bytes: int) -> bytearray:
        buf = bytearray()
        while len(buf) < num_bytes:
            rem = self.deadline - time.time()
            if rem <= 0:
                if self.worker_proc:
                    poll_res = self.worker_proc.poll()
                    if poll_res in (-signal.SIGXCPU, 152):
                        self.candidate_timeout = True
                        raise CandidateTimeoutError("Worker terminated due to CPU limit (SIGXCPU)")
                err = InfrastructureError("Unconfirmed wall-clock timeout waiting for worker IPC frame")
                self.infrastructure_error = err
                raise err

            events = self.selector.select(timeout=min(rem, 0.5))
            if not events:
                if self.worker_proc and self.worker_proc.poll() is not None:
                    retcode = self.worker_proc.poll()
                    if retcode in (-signal.SIGXCPU, 152):
                        self.candidate_timeout = True
                        raise CandidateTimeoutError("Worker process terminated by CPU limit (SIGXCPU)")
                    err = InfrastructureError(f"IPC pipe closed unexpectedly by worker process (exit status {retcode})")
                    self.infrastructure_error = err
                    raise err
                continue

            chunk = os.read(self.in_fd, num_bytes - len(buf))
            if not chunk:
                if self.worker_proc and self.worker_proc.poll() is not None:
                    retcode = self.worker_proc.poll()
                    if retcode in (-signal.SIGXCPU, 152):
                        self.candidate_timeout = True
                        raise CandidateTimeoutError("Worker process terminated by CPU limit (SIGXCPU)")
                err = InfrastructureError("IPC pipe closed unexpectedly by worker process")
                self.infrastructure_error = err
                raise err
            buf.extend(chunk)
        return buf

    def _read_framed_msg(self) -> dict:
        header_bytes = self._read_exact(4)
        frame_len = struct.unpack(">I", header_bytes)[0]

        if frame_len > MAX_FRAME_SIZE:
            err = InfrastructureError(f"Oversized IPC frame header ({frame_len} bytes > max {MAX_FRAME_SIZE} bytes)")
            self.infrastructure_error = err
            raise err

        payload_bytes = self._read_exact(frame_len)
        try:
            return json.loads(payload_bytes.decode("utf-8"))
        except Exception as e:
            err = InfrastructureError(f"Malformed JSON payload in IPC frame: {e}")
            self.infrastructure_error = err
            raise err

    def _write_framed_msg(self, msg: dict):
        try:
            data = json.dumps(msg).encode("utf-8")
            if len(data) > MAX_FRAME_SIZE:
                raise ValueError(f"Oversized IPC request payload ({len(data)} bytes)")
            header = struct.pack(">I", len(data))
            os.write(self.out_fd, header + data)
        except Exception as e:
            if self.worker_proc and self.worker_proc.poll() is not None:
                retcode = self.worker_proc.poll()
                if retcode in (-signal.SIGXCPU, 152):
                    self.candidate_timeout = True
                    raise CandidateTimeoutError("Worker process terminated by CPU limit (SIGXCPU)") from e
            err = InfrastructureError(f"Failed to write IPC frame to worker: {e}")
            self.infrastructure_error = err
            raise err from e

    def send_request(self, req: dict) -> Any:
        req_id = req.get("id")
        self._write_framed_msg(req)

        while True:
            resp = self._read_framed_msg()

            if resp.get("op") == "host_callback":
                cb_id = resp["callback_id"]
                cb = self.host_callbacks.get(cb_id)
                cb_req_id = resp["id"]
                cb_args = TaggedCodec.decode(resp.get("args", []), self.make_host_callback_proxy, self.make_obj_proxy, None, None, lambda h_id: self.host_objects[h_id])
                cb_kwargs = TaggedCodec.decode(resp.get("kwargs", {}), self.make_host_callback_proxy, self.make_obj_proxy, None, None, lambda h_id: self.host_objects[h_id])
                try:
                    res = cb(*cb_args, **cb_kwargs)
                    cb_resp = {"id": cb_req_id, "status": "ok", "result": TaggedCodec.encode(res, self.register_callback, None, self.register_host_object)}
                except Exception as e:
                    cb_resp = {"id": cb_req_id, "status": "exception", "result": TaggedCodec.encode(e, self.register_callback, None, self.register_host_object)}
                self._write_framed_msg(cb_resp)
                continue
            elif resp.get("op") == "call_host_obj":
                h_id = resp["obj_id"]
                h_obj = self.host_objects.get(h_id)
                h_req_id = resp["id"]
                method_name = resp["method"]
                h_args = TaggedCodec.decode(resp.get("args", []), self.make_host_callback_proxy, self.make_obj_proxy, None, None, lambda h_id: self.host_objects[h_id])
                h_kwargs = TaggedCodec.decode(resp.get("kwargs", {}), self.make_host_callback_proxy, self.make_obj_proxy, None, None, lambda h_id: self.host_objects[h_id])
                try:
                    if method_name == "__getitem__":
                        res = h_obj[h_args[0]]
                    elif method_name == "__setitem__":
                        h_obj[h_args[0]] = h_args[1]
                        res = None
                    elif method_name == "__delitem__":
                        del h_obj[h_args[0]]
                        res = None
                    elif method_name == "__contains__":
                        res = h_args[0] in h_obj
                    elif method_name == "__len__":
                        res = len(h_obj)
                    elif method_name == "keys":
                        res = list(h_obj.keys()) if hasattr(h_obj, "keys") else list(h_obj)
                    elif method_name == "values":
                        res = list(h_obj.values())
                    elif method_name == "items":
                        res = list(h_obj.items())
                    else:
                        target_fn = getattr(h_obj, method_name)
                        res = target_fn(*h_args, **h_kwargs)
                    h_resp = {"id": h_req_id, "status": "ok", "result": TaggedCodec.encode(res, self.register_callback, None, self.register_host_object)}
                except Exception as e:
                    h_resp = {"id": h_req_id, "status": "exception", "result": TaggedCodec.encode(e, self.register_callback, None, self.register_host_object)}
                self._write_framed_msg(h_resp)
                continue

            if resp.get("id") != req_id:
                err = InfrastructureError(f"IPC protocol frame ID mismatch: expected {req_id}, got {resp.get('id')}")
                self.infrastructure_error = err
                raise err

            if resp.get("status") == "ok":
                return TaggedCodec.decode(resp.get("result"), self.make_host_callback_proxy, self.make_obj_proxy, None, None, lambda h_id: self.host_objects[h_id])
            elif resp.get("status") in ("no_func", "no_class"):
                return {"__status__": resp.get("status")}
            elif resp.get("status") == "exception":
                self.candidate_exception_occurred = True
                exc_obj = TaggedCodec.decode(resp.get("result"), self.make_host_callback_proxy, self.make_obj_proxy, None, None, lambda h_id: self.host_objects[h_id])
                if isinstance(exc_obj, BaseException):
                    raise exc_obj
                raise RuntimeError(str(exc_obj))
            else:
                err = InfrastructureError(f"IPC worker return status error: {resp.get('error')}")
                self.infrastructure_error = err
                raise err


class CandidateModuleProxy:
    """Proxy representing sandboxed candidate module in host memory."""
    def __init__(self, client: WorkerIPCClient):
        self._client = client

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def proxy_callable(*args, **kwargs):
            enc_args = [TaggedCodec.encode(a, self._client.register_callback, None, self._client.register_host_object) for a in args]
            enc_kwargs = {k: TaggedCodec.encode(v, self._client.register_callback, None, self._client.register_host_object) for k, v in kwargs.items()}

            self._client.counter += 1
            req_id = f"req_{self._client.counter}"
            req = {
                "op": "call_func",
                "id": req_id,
                "func_name": name,
                "args": enc_args,
                "kwargs": enc_kwargs
            }
            res = self._client.send_request(req)
            if isinstance(res, dict) and res.get("__status__") == "no_func":
                self._client.counter += 1
                req_obj_id = f"req_{self._client.counter}"
                req_obj = {
                    "op": "create_obj",
                    "id": req_obj_id,
                    "class_name": name,
                    "args": enc_args,
                    "kwargs": enc_kwargs
                }
                res_obj = self._client.send_request(req_obj)
                if isinstance(res_obj, dict) and "obj_id" in res_obj:
                    return CandidateObjectProxy(res_obj["obj_id"], self._client)
                else:
                    def missing_symbol(*args, **kwargs):
                        raise AttributeError(f"Candidate symbol '{name}' not found")
                    return missing_symbol
            elif isinstance(res, dict) and res.get("__status__") == "no_class":
                def missing_symbol(*args, **kwargs):
                    raise AttributeError(f"Candidate symbol '{name}' not found")
                return missing_symbol
            return res

        return proxy_callable



WORKER_SCRIPT_CODE = r"""
import sys, os, json, argparse, resource, struct, base64, signal

parser = argparse.ArgumentParser()
parser.add_argument("--ipc-read-fd", type=int, required=True)
parser.add_argument("--ipc-write-fd", type=int, required=True)
parser.add_argument("--cpu-timeout", type=int, default=10)
args = parser.parse_args()

ipc_in_fd = args.ipc_read_fd
ipc_out_fd = args.ipc_write_fd
cpu_limit = max(1, args.cpu_timeout)

MAX_FRAME_SIZE = 1 * 1024 * 1024

def _write_raw_frame(msg):
    data = json.dumps(msg).encode("utf-8")
    header = struct.pack(">I", len(data))
    os.write(ipc_out_fd, header + data)

# 1. Install RLIMITs fail-closed matching cpu_timeout
rlimit_failures = []
for res_type, limits in [
    (resource.RLIMIT_CPU, (cpu_limit, cpu_limit)),
    (resource.RLIMIT_NOFILE, (64, 64)),
    (resource.RLIMIT_FSIZE, (10 * 1024 * 1024, 10 * 1024 * 1024))
]:
    try:
        resource.setrlimit(res_type, limits)
    except Exception as e:
        rlimit_failures.append(f"{res_type}: {e}")

if rlimit_failures:
    _write_raw_frame({"op": "ready", "status": "rlimit_failed", "error": "; ".join(rlimit_failures)})
    sys.exit(1)

# 2. Redirect candidate stdout/stderr to /dev/null
sys.stdout = sys.stderr = open(os.devnull, "w")

# 3. Complete READY handshake BEFORE candidate import
_write_raw_frame({"op": "ready", "status": "ok"})

class HostObjectProxy(dict):
    def __init__(self, obj_id, ipc):
        super().__setattr__("_obj_id", obj_id)
        super().__setattr__("_ipc", ipc)
    def __getitem__(self, key):
        return self._ipc.call_host_obj(self._obj_id, "__getitem__", [key], {})
    def __setitem__(self, key, val):
        return self._ipc.call_host_obj(self._obj_id, "__setitem__", [key, val], {})
    def __delitem__(self, key):
        return self._ipc.call_host_obj(self._obj_id, "__delitem__", [key], {})
    def __contains__(self, key):
        return self._ipc.call_host_obj(self._obj_id, "__contains__", [key], {})
    def __len__(self):
        return self._ipc.call_host_obj(self._obj_id, "__len__", [], {})
    def __iter__(self):
        keys = self._ipc.call_host_obj(self._obj_id, "keys", [], {})
        return iter(keys)
    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        def proxy_method(*args, **kwargs):
            return self._ipc.call_host_obj(self._obj_id, name, list(args), dict(kwargs))
        return proxy_method
    def get(self, key, default=None):
        return self._ipc.call_host_obj(self._obj_id, "get", [key, default], {})
    def keys(self):
        return self._ipc.call_host_obj(self._obj_id, "keys", [], {})
    def values(self):
        return self._ipc.call_host_obj(self._obj_id, "values", [], {})
    def items(self):
        return self._ipc.call_host_obj(self._obj_id, "items", [], {})
    def pop(self, key, default=None):
        return self._ipc.call_host_obj(self._obj_id, "pop", [key, default], {})
    def clear(self):
        return self._ipc.call_host_obj(self._obj_id, "clear", [], {})

class TaggedCodec:
    @classmethod
    def encode(cls, obj, register_cb_fn=None, register_obj_fn=None, register_host_obj_fn=None):
        if obj is None or isinstance(obj, (int, float, str, bool)):
            return {"t": "prim", "v": obj}
        elif isinstance(obj, bytes):
            return {"t": "bytes", "v": base64.b64encode(obj).decode("ascii")}
        elif isinstance(obj, tuple):
            return {"t": "tuple", "v": [cls.encode(x, register_cb_fn, register_obj_fn, register_host_obj_fn) for x in obj]}
        elif isinstance(obj, list):
            return {"t": "list", "v": [cls.encode(x, register_cb_fn, register_obj_fn, register_host_obj_fn) for x in obj]}
        elif isinstance(obj, HostObjectProxy):
            return {"t": "host_object", "id": obj._obj_id}
        elif isinstance(obj, dict):
            pairs = [[cls.encode(k, register_cb_fn, register_obj_fn, register_host_obj_fn), cls.encode(v, register_cb_fn, register_obj_fn, register_host_obj_fn)] for k, v in obj.items()]
            return {"t": "dict", "pairs": pairs}
        elif isinstance(obj, BaseException):
            return {
                "t": "exception",
                "module": type(obj).__module__,
                "qualname": type(obj).__qualname__,
                "message": str(obj)
            }
        elif callable(obj):
            if register_cb_fn:
                cb_id = register_cb_fn(obj)
                return {"t": "host_callback", "id": cb_id}
            raise TypeError(f"Callable cannot be encoded without callback registrar: {obj}")
        else:
            if register_obj_fn:
                obj_id = register_obj_fn(obj)
                return {"t": "candidate_object", "id": obj_id}
            raise TypeError(f"Unsupported type for TaggedCodec: {type(obj).__name__}")

    @classmethod
    def decode(cls, env, make_cb_fn=None, make_obj_proxy_fn=None, get_worker_obj_fn=None, make_host_obj_proxy_fn=None, get_host_obj_fn=None):
        if not isinstance(env, dict) or "t" not in env:
            if env is None or isinstance(env, (int, float, str, bool)):
                return env
            if isinstance(env, list):
                return [cls.decode(x, make_cb_fn, make_obj_proxy_fn, get_worker_obj_fn, make_host_obj_proxy_fn, get_host_obj_fn) for x in env]
            if isinstance(env, dict):
                return {cls.decode(k, make_cb_fn, make_obj_proxy_fn, get_worker_obj_fn, make_host_obj_proxy_fn, get_host_obj_fn): cls.decode(v, make_cb_fn, make_obj_proxy_fn, get_worker_obj_fn, make_host_obj_proxy_fn, get_host_obj_fn) for k, v in env.items()}
            return env

        t = env.get("t")
        if t == "prim":
            return env["v"]
        elif t == "bytes":
            return base64.b64decode(env["v"].encode("ascii"))
        elif t == "tuple":
            return tuple(cls.decode(x, make_cb_fn, make_obj_proxy_fn, get_worker_obj_fn, make_host_obj_proxy_fn, get_host_obj_fn) for x in env["v"])
        elif t == "list":
            return [cls.decode(x, make_cb_fn, make_obj_proxy_fn, get_worker_obj_fn, make_host_obj_proxy_fn, get_host_obj_fn) for x in env["v"]]
        elif t == "dict":
            return {cls.decode(k, make_cb_fn, make_obj_proxy_fn, get_worker_obj_fn, make_host_obj_proxy_fn, get_host_obj_fn): cls.decode(v, make_cb_fn, make_obj_proxy_fn, get_worker_obj_fn, make_host_obj_proxy_fn, get_host_obj_fn) for k, v in env["pairs"]}
        elif t == "host_callback":
            if make_cb_fn:
                return make_cb_fn(env["id"])
            raise ValueError("Host callback received without handler factory")
        elif t == "host_object":
            if get_host_obj_fn:
                return get_host_obj_fn(env["id"])
            if make_host_obj_proxy_fn:
                return make_host_obj_proxy_fn(env["id"])
            raise ValueError("host_object received without handler")
        elif t == "candidate_object":
            if get_worker_obj_fn:
                return get_worker_obj_fn(env["id"])
            if make_obj_proxy_fn:
                return make_obj_proxy_fn(env["id"])
            raise ValueError("candidate_object received without handler")
        elif t == "exception":
            exc_module = env.get("module", "builtins")
            exc_qualname = env.get("qualname", "RuntimeError")
            msg = env.get("message", "")
            try:
                mod = sys.modules.get(exc_module) or __import__(exc_module, fromlist=[exc_qualname])
                exc_cls = getattr(mod, exc_qualname, None)
                if not (isinstance(exc_cls, type) and issubclass(exc_cls, BaseException)):
                    exc_cls = getattr(sys.modules["builtins"], exc_qualname, RuntimeError)
            except Exception:
                exc_cls = getattr(sys.modules["builtins"], exc_qualname, RuntimeError)
            if not (isinstance(exc_cls, type) and issubclass(exc_cls, BaseException)):
                exc_cls = RuntimeError
            return exc_cls(msg)
        else:
            raise ValueError(f"Unknown tagged type: {t}")

candidate = None
import_exception = None
try:
    import candidate
except Exception as e:
    import_exception = e

class HostCallback:
    def __init__(self, callback_id, ipc):
        self.callback_id = callback_id
        self.ipc = ipc
    def __call__(self, *args, **kwargs):
        return self.ipc.call_host(self.callback_id, args, kwargs)

class WorkerIPC:
    def __init__(self):
        self.objects = {}
        self.obj_counter = 0
        self.req_counter = 0

    def make_cb(self, cb_id):
        return HostCallback(cb_id, self)

    def make_host_obj_proxy(self, host_obj_id):
        return HostObjectProxy(host_obj_id, self)

    def get_obj(self, obj_id):
        return self.objects[obj_id]

    def register_obj(self, obj):
        self.obj_counter += 1
        obj_id = f"obj_{self.obj_counter}"
        self.objects[obj_id] = obj
        return obj_id

    def _read_exact(self, num_bytes):
        buf = bytearray()
        while len(buf) < num_bytes:
            chunk = os.read(ipc_in_fd, num_bytes - len(buf))
            if not chunk:
                raise RuntimeError("IPC pipe closed by host")
            buf.extend(chunk)
        return buf

    def read_msg(self):
        header_bytes = self._read_exact(4)
        frame_len = struct.unpack(">I", header_bytes)[0]
        if frame_len > MAX_FRAME_SIZE:
            raise ValueError(f"Oversized IPC frame ({frame_len} bytes)")
        payload_bytes = self._read_exact(frame_len)
        return json.loads(payload_bytes.decode("utf-8"))

    def write_msg(self, msg):
        _write_raw_frame(msg)

    def call_host(self, callback_id, args, kwargs):
        self.req_counter += 1
        req_id = f"cb_{self.req_counter}"
        enc_args = TaggedCodec.encode(args, lambda cb: None, self.register_obj, None)
        enc_kwargs = TaggedCodec.encode(kwargs, lambda cb: None, self.register_obj, None)
        self.write_msg({"op": "host_callback", "id": req_id, "callback_id": callback_id, "args": enc_args, "kwargs": enc_kwargs})
        resp = self.read_msg()
        if resp.get("status") == "ok":
            return TaggedCodec.decode(resp.get("result"), self.make_cb, None, self.get_obj, self.make_host_obj_proxy, None)
        elif resp.get("status") == "exception":
            exc_obj = TaggedCodec.decode(resp.get("result"), self.make_cb, None, self.get_obj, self.make_host_obj_proxy, None)
            if isinstance(exc_obj, BaseException):
                raise exc_obj
            raise RuntimeError(str(exc_obj))
        raise RuntimeError(resp.get("error", "Host callback failed"))

    def call_host_obj(self, obj_id, method_name, args, kwargs):
        self.req_counter += 1
        req_id = f"hobj_{self.req_counter}"
        enc_args = TaggedCodec.encode(args, lambda cb: None, self.register_obj, None)
        enc_kwargs = TaggedCodec.encode(kwargs, lambda cb: None, self.register_obj, None)
        self.write_msg({"op": "call_host_obj", "id": req_id, "obj_id": obj_id, "method": method_name, "args": enc_args, "kwargs": enc_kwargs})
        resp = self.read_msg()
        if resp.get("status") == "ok":
            return TaggedCodec.decode(resp.get("result"), self.make_cb, None, self.get_obj, self.make_host_obj_proxy, None)
        elif resp.get("status") == "exception":
            exc_obj = TaggedCodec.decode(resp.get("result"), self.make_cb, None, self.get_obj, self.make_host_obj_proxy, None)
            if isinstance(exc_obj, BaseException):
                raise exc_obj
            raise RuntimeError(str(exc_obj))
        raise RuntimeError(resp.get("error", "Host object call failed"))

    def run(self):
        while True:
            try:
                req = self.read_msg()
            except RuntimeError:
                break
            except Exception as e:
                break

            op = req.get("op")
            req_id = req.get("id")

            if import_exception is not None:
                enc_exc = TaggedCodec.encode(import_exception, lambda cb: None, self.register_obj, None)
                self.write_msg({"id": req_id, "status": "exception", "result": enc_exc})
                continue

            try:
                if op == "call_func":
                    if not candidate or not hasattr(candidate, req["func_name"]):
                        self.write_msg({"id": req_id, "status": "no_func"})
                        continue
                    fn = getattr(candidate, req["func_name"])
                    args = TaggedCodec.decode(req.get("args", []), self.make_cb, None, self.get_obj, self.make_host_obj_proxy, None)
                    kwargs = TaggedCodec.decode(req.get("kwargs", {}), self.make_cb, None, self.get_obj, self.make_host_obj_proxy, None)
                    res = fn(*args, **kwargs)
                    enc_res = TaggedCodec.encode(res, lambda cb: None, self.register_obj, None)
                    self.write_msg({"id": req_id, "status": "ok", "result": enc_res})
                elif op == "create_obj":
                    if not candidate or not hasattr(candidate, req["class_name"]):
                        self.write_msg({"id": req_id, "status": "no_class"})
                        continue
                    cls = getattr(candidate, req["class_name"])
                    args = TaggedCodec.decode(req.get("args", []), self.make_cb, None, self.get_obj, self.make_host_obj_proxy, None)
                    kwargs = TaggedCodec.decode(req.get("kwargs", {}), self.make_cb, None, self.get_obj, self.make_host_obj_proxy, None)
                    obj_id = self.register_obj(cls(*args, **kwargs))
                    self.write_msg({"id": req_id, "status": "ok", "result": {"obj_id": obj_id}})
                elif op == "call_method":
                    obj = self.objects[req["obj_id"]]
                    fn = getattr(obj, req["method_name"])
                    args = TaggedCodec.decode(req.get("args", []), self.make_cb, None, self.get_obj, self.make_host_obj_proxy, None)
                    kwargs = TaggedCodec.decode(req.get("kwargs", {}), self.make_cb, None, self.get_obj, self.make_host_obj_proxy, None)
                    res = fn(*args, **kwargs)
                    enc_res = TaggedCodec.encode(res, lambda cb: None, self.register_obj, None)
                    self.write_msg({"id": req_id, "status": "ok", "result": enc_res})
                elif op == "set_attr":
                    obj = self.objects[req["obj_id"]]
                    val = TaggedCodec.decode(req["value"], self.make_cb, None, self.get_obj, self.make_host_obj_proxy, None)
                    setattr(obj, req["attr_name"], val)
                    self.write_msg({"id": req_id, "status": "ok", "result": None})
                elif op == "get_attr":
                    obj = self.objects[req["obj_id"]]
                    if hasattr(obj, req["attr_name"]) and not callable(getattr(obj, req["attr_name"])):
                        res = getattr(obj, req["attr_name"])
                        enc_res = TaggedCodec.encode(res, lambda cb: None, self.register_obj, None)
                        self.write_msg({"id": req_id, "status": "ok", "result": enc_res})
                    else:
                        self.write_msg({"id": req_id, "status": "ok", "result": {"__status__": "error"}})
            except Exception as e:
                enc_exc = TaggedCodec.encode(e, lambda cb: None, self.register_obj, None)
                self.write_msg({"id": req_id, "status": "exception", "result": enc_exc})


if __name__ == "__main__":
    WorkerIPC().run()
"""


def evaluate_candidate_code_sandboxed(task_id: str, candidate_code: str, timeout_seconds: int = 15) -> Tuple[int, int]:
    """
    Host Supervisor Candidate Evaluator.
    Imports test_harness.py in host memory outside candidate workspace.
    Sandboxed Candidate Worker imports ONLY candidate.py inside sandbox-exec container over dedicated IPC pipes.
    Completes worker READY handshake before importing candidate code.
    Distinguishes confirmed RLIMIT_CPU worker termination from unconfirmed host wall-clock timeout.
    Candidate syntax/runtime exception/timeout -> (0, 10).
    IPC / Popen / Launch / Readiness / Infrastructure error -> InfrastructureError (invalidates calibration).
    """
    task_dir = _find_task_dir(task_id)
    harness_path = os.path.join(task_dir, "test_harness.py")
    if not os.path.exists(harness_path):
        raise InfrastructureError(f"Missing test harness at: {harness_path}")

    mod_key = f"harness_sup_{task_id}"
    try:
        spec = importlib.util.spec_from_file_location(mod_key, harness_path)
        harness_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(harness_mod)
        harness_mod._sticky_infrastructure_error = None
    except Exception as e:
        raise InfrastructureError(f"Failed to load test harness for {task_id}: {e}")

    cand_tmpdir = tempfile.mkdtemp(prefix="rsi_cand_")
    proc = None

    host_in, worker_out = os.pipe()
    worker_in, host_out = os.pipe()

    cpu_limit = max(1, timeout_seconds - 1) if timeout_seconds <= 3 else timeout_seconds
    deadline = time.time() + timeout_seconds
    ipc_client = None
    try:
        cand_file = os.path.join(cand_tmpdir, "candidate.py")
        with open(cand_file, "w", encoding="utf-8") as f:
            f.write(candidate_code)

        worker_file = os.path.join(cand_tmpdir, "worker.py")
        with open(worker_file, "w", encoding="utf-8") as f:
            f.write(WORKER_SCRIPT_CODE)

        sb_profile = get_candidate_sandbox_profile(BASE_DIR, cand_tmpdir)
        sb_path = os.path.join(cand_tmpdir, "cand.sb")
        with open(sb_path, "w", encoding="utf-8") as f:
            f.write(sb_profile)

        cmd = [
            "sandbox-exec", "-f", sb_path,
            sys.executable, worker_file,
            "--ipc-read-fd", str(worker_in),
            "--ipc-write-fd", str(worker_out),
            "--cpu-timeout", str(cpu_limit)
        ]

        env = {
            "PYTHONPATH": cand_tmpdir,
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": cand_tmpdir,
            "TMPDIR": cand_tmpdir
        }

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cand_tmpdir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                pass_fds=(worker_in, worker_out),
                start_new_session=True,
                env=env
            )
        except Exception as popen_err:
            raise InfrastructureError(f"Worker Popen launch failure: {popen_err}") from popen_err

        os.close(worker_in)
        os.close(worker_out)

        ipc_client = WorkerIPCClient(host_in, host_out, deadline, proc)

        ready_frame = ipc_client._read_framed_msg()
        if ready_frame.get("op") != "ready" or ready_frame.get("status") != "ok":
            err_detail = ready_frame.get("error", "Worker readiness handshake failed")
            raise InfrastructureError(f"Worker startup readiness failed: {err_detail}")

        cand_proxy = CandidateModuleProxy(ipc_client)

        run_eval = getattr(harness_mod, "run_evaluation", None)
        if not run_eval:
            raise InfrastructureError(f"Test harness missing run_evaluation for {task_id}")

        earned, max_s = run_eval(cand_proxy)

        if ipc_client.infrastructure_error:
            raise ipc_client.infrastructure_error
        if getattr(harness_mod, "_sticky_infrastructure_error", None):
            raise harness_mod._sticky_infrastructure_error

        return int(earned), int(max_s)
    except CandidateTimeoutError:
        return 0, 10
    except InfrastructureError:
        raise
    except Exception as e:
        if ipc_client and ipc_client.candidate_timeout:
            return 0, 10
        if ipc_client and ipc_client.infrastructure_error:
            raise ipc_client.infrastructure_error
        if getattr(harness_mod, "_sticky_infrastructure_error", None):
            raise harness_mod._sticky_infrastructure_error
        if ipc_client and ipc_client.candidate_exception_occurred:
            return 0, 10
        raise InfrastructureError(f"Host evaluation infrastructure failure: {type(e).__name__}: {e}") from e
    finally:
        try: os.close(host_in)
        except Exception: pass
        try: os.close(host_out)
        except Exception: pass
        if proc:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    pass
                try:
                    proc.wait(timeout=2)
                except Exception:
                    pass
        shutil.rmtree(cand_tmpdir, ignore_errors=True)


def run_local_proxy_inference(task_id: str, seed: int) -> Tuple[int, int, str, str]:
    """
    Executes GGUF LLM inference under macOS sandbox-exec containment profile.
    Captures raw output, extracts candidate code, and evaluates candidate in sandboxed IPC worker.
    Returns (earned_score, max_score, raw_output_sha256, extracted_code_sha256).
    """
    verify_proxy_config_provenance()
    cfg = load_proxy_config()

    # 1. Verify model artifact file and SHA256 before launch
    model_path = os.path.join(BASE_DIR, cfg["model_artifact_path"])
    if not os.path.exists(model_path):
        raise InfrastructureError(f"Missing model artifact file: {model_path}")
    if os.path.islink(model_path):
        raise InfrastructureError(f"Model artifact must not be a symlink: {model_path}")

    sha = hashlib.sha256()
    with open(model_path, "rb") as f:
        while chunk := f.read(8192 * 1024):
            sha.update(chunk)
    if sha.hexdigest() != cfg["model_artifact_sha256"]:
        raise InfrastructureError(
            f"Model artifact SHA256 mismatch for {cfg['model_artifact_path']}: "
            f"expected {cfg['model_artifact_sha256']}, got {sha.hexdigest()}"
        )

    # 2. Verify runtime binary file and SHA256 before launch
    runtime_bin_path = os.path.join(BASE_DIR, cfg["runtime_binary"])
    if not os.path.exists(runtime_bin_path):
        raise InfrastructureError(f"Missing runtime binary: {runtime_bin_path}")

    with open(runtime_bin_path, "rb") as f:
        actual_bin_hash = hashlib.sha256(f.read()).hexdigest()
    if actual_bin_hash != cfg["runtime_binary_sha256"]:
        raise InfrastructureError(
            f"Runtime binary SHA256 mismatch for {cfg['runtime_binary']}: "
            f"expected {cfg['runtime_binary_sha256']}, got {actual_bin_hash}"
        )

    # 3. Create isolated generator spec directory containing runner copy & problem.txt
    task_dir = _find_task_dir(task_id)
    spec_file = os.path.join(task_dir, "problem.txt")
    if not os.path.exists(spec_file):
        raise InfrastructureError(f"Missing problem.txt for task {task_id}")

    gen_tmpdir = tempfile.mkdtemp(prefix="rsi_gen_")
    try:
        tmp_spec_file = os.path.join(gen_tmpdir, "problem.txt")
        shutil.copy(spec_file, tmp_spec_file)

        tmp_runner_file = os.path.join(gen_tmpdir, "local_model_runner.py")
        shutil.copy(runtime_bin_path, tmp_runner_file)

        sb_profile = get_generator_sandbox_profile(BASE_DIR, gen_tmpdir, model_path, tmp_spec_file, tmp_runner_file)
        sb_path = os.path.join(gen_tmpdir, "gen.sb")
        with open(sb_path, "w", encoding="utf-8") as f:
            f.write(sb_profile)

        stops = cfg.get("stop", ["<|im_end|>"])
        stop_arg = stops[0] if isinstance(stops, list) and stops else "<|im_end|>"

        cmd = [
            "sandbox-exec", "-f", sb_path,
            sys.executable,
            tmp_runner_file,
            "--task-id", task_id,
            "--seed", str(seed),
            "--model-path", model_path,
            "--spec-path", tmp_spec_file,
            "--temperature", str(cfg["temperature"]),
            "--top-p", str(cfg["top_p"]),
            "--n-ctx", str(cfg["n_ctx"]),
            "--max-tokens", str(cfg["max_tokens"]),
            "--stop", stop_arg
        ]

        proc = subprocess.run(
            cmd,
            cwd=gen_tmpdir,
            capture_output=True,
            text=True,
            timeout=cfg.get("timeout_seconds", 120)
        )

        if proc.returncode != 0:
            raise InfrastructureError(f"Sandboxed GGUF generator failed with exit code {proc.returncode}: {proc.stderr}")

        raw_output = proc.stdout
        if not raw_output.strip():
            raise InfrastructureError("Sandboxed GGUF generator produced empty output")

        raw_output_hash = hashlib.sha256(raw_output.encode("utf-8")).hexdigest()

        candidate_code = extract_code_from_output(raw_output)
        extracted_code_hash = hashlib.sha256(candidate_code.encode("utf-8")).hexdigest()

        earned, max_score = evaluate_candidate_code_sandboxed(task_id, candidate_code)
        return earned, max_score, raw_output_hash, extracted_code_hash
    except subprocess.TimeoutExpired:
        raise InfrastructureError(f"GGUF generator timed out after {cfg.get('timeout_seconds', 120)} seconds")
    finally:
        shutil.rmtree(gen_tmpdir, ignore_errors=True)


def verify_proxy_reproducibility() -> bool:
    """
    Executes proxy model subprocess inference runs twice with identical seeds.
    Asserts byte-identical raw outputs, extracted code hashes, and evaluation scores.
    Raises InfrastructureError fail-closed if there is any mismatch.
    """
    cfg = load_proxy_config()
    test_task = "c_f1_01_log_stream_parser"
    test_seed = cfg["seeds"][0]

    run1 = run_local_proxy_inference(test_task, test_seed)
    run2 = run_local_proxy_inference(test_task, test_seed)

    if run1 != run2:
        raise InfrastructureError(f"Proxy reproducibility check failed: run1={run1} != run2={run2}")

    return True


def generate_and_persist_proxy_calibration_artifacts() -> Dict[str, Any]:
    """
    Generates and persists campaign2_proxy_calibration.json containing complete pre-registered
    proxy score distribution across ALL 30 tasks, deterministic tier summaries, and reproducibility evidence.
    """
    verify_proxy_config_provenance()
    reproducible = verify_proxy_reproducibility()
    cfg = load_proxy_config()
    seeds = cfg.get("seeds", [1729])
    seed = seeds[0]

    all_task_ids = [
        "c_f1_01_log_stream_parser", "c_f1_02_json_tokenizer_recovery", "c_f1_03_csv_record_recovery",
        "c_f1_04_http_chunked_decoder", "c_f1_05_protobuf_varint_decoder", "c_f2_01_dag_task_scheduler",
        "c_f2_02_resource_limit_planner", "c_f2_03_build_dependency_graph", "c_f2_04_pipeline_stage_scheduler",
        "c_f2_05_package_version_resolver", "c_f3_01_lru_ttl_cache", "c_f3_02_lfu_serialized_cache",
        "c_f3_03_write_through_cache", "c_f3_04_segmented_2tier_cache", "c_f3_05_two_queue_cache",
        "c_f3_06_tiered_storage_store", "c_f4_01_raft_leader_election", "c_f4_02_tcp_state_machine",
        "c_f4_03_oauth2_flow_simulator", "c_f4_04_websocket_frame_fsm", "c_f4_05_saga_orchestrator_fsm",
        "c_f4_06_dtls_handshake_fsm", "p_f1_01_ndjson_stream_parser", "p_f1_02_syslog_framing_decoder",
        "p_f2_01_workflow_execution_engine", "p_f2_02_k8s_pod_scheduler", "p_f3_01_arc_adaptive_cache",
        "p_f3_02_ring_buffer_cache", "p_f4_01_bgp_peer_fsm", "p_f4_02_sip_call_flow_fsm"
    ]

    score_records = []
    tier_counts = {"Tier-1 (Optimal)": 0, "Tier-2 (Partial)": 0, "Tier-3 (Zero)": 0}

    for task_id in all_task_ids:
        earned, max_s, raw_hash, code_hash = run_local_proxy_inference(task_id, seed)
        if earned == 10:
            tier = "Tier-1 (Optimal)"
        elif earned > 0:
            tier = "Tier-2 (Partial)"
        else:
            tier = "Tier-3 (Zero)"

        tier_counts[tier] += 1
        score_records.append({
            "task_id": task_id,
            "seed": seed,
            "earned": earned,
            "max_score": max_s,
            "raw_output_sha256": raw_hash,
            "extracted_code_sha256": code_hash,
            "derived_tier": tier
        })

    calibration_data = {
        "provenance_verified": True,
        "reproducibility_verified": reproducible,
        "total_tasks_evaluated": len(score_records),
        "tier_summary": tier_counts,
        "score_records": score_records
    }

    artifact_path = os.path.join(BASE_DIR, "campaign2_proxy_calibration.json")
    if os.path.exists(artifact_path):
        os.chmod(artifact_path, 0o644)

    with open(artifact_path, "w", encoding="utf-8") as f:
        json.dump(calibration_data, f, indent=2)

    os.chmod(artifact_path, 0o444)
    return calibration_data
