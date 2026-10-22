"""
Campaign 2 Host Supervisor & Sandboxed Candidate Worker IPC Module.

ARCHITECTURE & CONTAINMENT:
- Supervisor process (host, trusted) imports test_harness.py in host memory.
- Worker process (untrusted) runs candidate code inside a macOS sandbox-exec jail container.
- Communication uses framed JSON RPC over a dedicated pipe (pass_fds).
- Worker process redirects stdout and stderr to subprocess.DEVNULL to prevent output flood deadlocks.
- Worker installs irreversible CPU, file-size, file-descriptor, and core-dump RLIMITs before readiness.
- Startup, candidate import, and candidate execution are explicit protocol phases. Candidate code is imported only after readiness.
- Protocol framing separates candidate-originated exceptions (score 0/10) from host infrastructure failures (raises InfrastructureError).
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
import resource
from typing import Dict, Any, List, Tuple

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BENCHMARKS_DIR = os.path.join(BASE_DIR, "benchmarks", "campaign2")
MAX_FRAME_SIZE = 1 * 1024 * 1024  # 1 MB max framed IPC payload limit
WORKER_STARTUP_TIMEOUT_SECONDS = 5


class InfrastructureError(RuntimeError):
    """Raised when an infrastructure, sandbox, serialization, or harness protocol error occurs. Invalidates calibration."""
    pass


class CandidateTimeoutError(Exception):
    """Raised when candidate import or execution exceeds its CPU or wall-clock budget."""
    pass


class CandidateProcessError(Exception):
    """Raised when worker process exits unexpectedly (process signal termination or non-zero exit code) during candidate import or execution phase."""
    pass


class CandidateObjectProxy:
    """Proxy object representing a sandboxed candidate object instance in host memory."""
    def __init__(self, obj_id: str, client: Any):
        super().__setattr__("_obj_id", obj_id)
        super().__setattr__("_client", client)

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        self._client.counter += 1
        req_id = f"g_{self._client.counter}"
        req = {
            "id": req_id,
            "op": "get_attr",
            "obj_id": self._obj_id,
            "attr_name": name
        }
        res = self._client.send_request(req)
        if self._client.last_status == "exception":
            if isinstance(res, BaseException):
                raise res
            raise AttributeError(name)

        if isinstance(res, dict) and res.get("is_callable"):
            def proxy_method(*args, **kwargs):
                enc_args = TaggedCodec.encode(list(args), self._client.register_callback, None, self._client.register_host_object)
                enc_kwargs = TaggedCodec.encode(kwargs, self._client.register_callback, None, None)
                self._client.counter += 1
                m_req_id = f"m_{self._client.counter}"
                m_req = {
                    "id": m_req_id,
                    "op": "call_method",
                    "obj_id": self._obj_id,
                    "method_name": name,
                    "args": enc_args,
                    "kwargs": enc_kwargs
                }
                m_res = self._client.send_request(m_req)
                if self._client.last_status == "exception":
                    if isinstance(m_res, BaseException):
                        raise m_res
                    raise RuntimeError(str(m_res))
                return m_res
            return proxy_method

        return TaggedCodec.decode(res.get("value"), self._client.make_host_callback_proxy, self._client.make_obj_proxy, None, None, lambda h_id: self._client.host_objects[h_id]) if isinstance(res, dict) and "value" in res else res

    def __getitem__(self, key: Any):
        return getattr(self, "__getitem__")(key)

    def __setitem__(self, key: Any, value: Any):
        return getattr(self, "__setitem__")(key, value)

    def __len__(self):
        return getattr(self, "__len__")()

    def __contains__(self, item: Any):
        return getattr(self, "__contains__")(item)

    def __setattr__(self, name: str, value: Any):
        if name.startswith("_"):
            super().__setattr__(name, value)
            return

        enc_val = TaggedCodec.encode(value, self._client.register_callback, None, self._client.register_host_object)
        self._client.counter += 1
        req_id = f"s_{self._client.counter}"
        req = {
            "id": req_id,
            "op": "set_attr",
            "obj_id": self._obj_id,
            "attr_name": name,
            "value": enc_val
        }
        res = self._client.send_request(req)
        if self._client.last_status == "exception":
            if isinstance(res, BaseException):
                raise res
            raise RuntimeError(str(res))


class CandidateModuleProxy:
    """Proxy object representing a sandboxed candidate module in host memory."""
    def __init__(self, client: Any):
        self._client = client

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)

        def proxy_target(*args, **kwargs):
            enc_args = TaggedCodec.encode(list(args), self._client.register_callback, None, self._client.register_host_object)
            enc_kwargs = TaggedCodec.encode(kwargs, self._client.register_callback, None, None)
            self._client.counter += 1
            req_id = f"f_{self._client.counter}"
            req_func = {
                "id": req_id,
                "op": "call_func",
                "func_name": name,
                "args": enc_args,
                "kwargs": enc_kwargs
            }
            res_func = self._client.send_request(req_func)

            if self._client.last_status == "no_func":
                self._client.counter += 1
                req_obj_id = f"c_{self._client.counter}"
                req_cls = {
                    "id": req_obj_id,
                    "op": "create_obj",
                    "class_name": name,
                    "args": enc_args,
                    "kwargs": enc_kwargs
                }
                res_cls = self._client.send_request(req_cls)
                if self._client.last_status == "exception":
                    if isinstance(res_cls, BaseException):
                        raise res_cls
                    raise RuntimeError(str(res_cls))
                if self._client.last_status == "no_class":
                    raise AttributeError(f"Candidate module has no attribute or function '{name}'")
                obj_id = res_cls["obj_id"]
                proxy_obj = CandidateObjectProxy(obj_id, self._client)
                self._client.object_cache[obj_id] = proxy_obj
                return proxy_obj

            if self._client.last_status == "exception":
                if isinstance(res_func, BaseException):
                    raise res_func
                raise RuntimeError(str(res_func))

            return res_func

        return proxy_target


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
                h_id = register_host_obj_fn(obj)
                return {"t": "host_object", "id": h_id}
            pairs = [[cls.encode(k, register_cb_fn, register_obj_fn, register_host_obj_fn), cls.encode(v, register_cb_fn, register_obj_fn, register_host_obj_fn)] for k, v in obj.items()]
            return {"t": "dict", "pairs": pairs}
        elif type(obj).__name__ in ("dict_keys", "dict_values"):
            return {"t": "list", "v": [cls.encode(x, register_cb_fn, register_obj_fn, register_host_obj_fn) for x in obj]}
        elif type(obj).__name__ == "dict_items":
            return {"t": "list", "v": [cls.encode(tuple(x), register_cb_fn, register_obj_fn, register_host_obj_fn) for x in obj]}
        elif isinstance(obj, BaseException):
            exc_mod = type(obj).__module__
            exc_qual = type(obj).__qualname__
            if exc_mod != "builtins" or exc_qual not in ("ValueError", "RuntimeError", "TypeError", "AttributeError", "KeyError", "IndexError", "SyntaxError", "NameError", "ZeroDivisionError", "OverflowError", "MemoryError", "RecursionError", "AssertionError", "ImportError", "FileNotFoundError", "PermissionError", "OSError", "Exception"):
                exc_mod = "builtins"
                exc_qual = "RuntimeError"
            return {
                "t": "exception",
                "module": exc_mod,
                "qualname": exc_qual,
                "message": str(obj)
            }
        elif callable(obj):
            if register_cb_fn:
                cb_id = register_cb_fn(obj)
                return {"t": "host_callback", "id": cb_id}
            raise TypeError(f"Callable cannot be encoded without callback registrar: {obj}")
        else:
            if register_host_obj_fn:
                h_id = register_host_obj_fn(obj)
                return {"t": "host_object", "id": h_id}
            raise TypeError(f"Object of type {type(obj)} cannot be encoded without host_object registrar")

    @classmethod
    def decode(cls, env: Any, make_cb_fn=None, make_obj_proxy_fn=None, get_worker_obj_fn=None, make_host_obj_proxy_fn=None, get_host_obj_fn=None) -> Any:
        if not isinstance(env, dict) or "t" not in env:
            return env
        t = env["t"]
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


class HostObjectProxy:
    """Proxy object representing a host object inside worker process memory."""
    def __init__(self, obj_id: str, ipc: Any):
        self._obj_id = obj_id
        self._ipc = ipc

    def __getattr__(self, name: str):
        def proxy_method(*args, **kwargs):
            return self._ipc.call_host_obj(self._obj_id, name, args, kwargs)
        return proxy_method

    def __getitem__(self, key: Any):
        return self._ipc.call_host_obj(self._obj_id, "__getitem__", [key], {})

    def __setitem__(self, key: Any, value: Any):
        return self._ipc.call_host_obj(self._obj_id, "__setitem__", [key, value], {})

    def __len__(self):
        return self._ipc.call_host_obj(self._obj_id, "__len__", [], {})

    def __contains__(self, item: Any):
        return self._ipc.call_host_obj(self._obj_id, "__contains__", [item], {})

    def __iter__(self):
        keys = self._ipc.call_host_obj(self._obj_id, "keys", [], {})
        return iter(keys)


WORKER_SCRIPT_CODE = r"""import sys
import os
import json
import struct
import base64
import importlib
import resource

ipc_read_fd = None
ipc_write_fd = None
cpu_timeout = 10

for i, arg in enumerate(sys.argv):
    if arg == "--ipc-read-fd" and i + 1 < len(sys.argv):
        ipc_read_fd = int(sys.argv[i + 1])
    elif arg == "--ipc-write-fd" and i + 1 < len(sys.argv):
        ipc_write_fd = int(sys.argv[i + 1])
    elif arg == "--cpu-timeout" and i + 1 < len(sys.argv):
        cpu_timeout = int(sys.argv[i + 1])

if ipc_read_fd is None or ipc_write_fd is None:
    sys.exit(1)

MAX_FRAME_SIZE = 1 * 1024 * 1024

def _write_raw_frame(data_dict):
    payload = json.dumps(data_dict).encode("utf-8")
    header = struct.pack(">I", len(payload))
    os.write(ipc_write_fd, header + payload)

cpu_soft_limit = max(1, cpu_timeout)
applied_limits = {
    "cpu": [cpu_soft_limit, cpu_soft_limit + 1],
    "nofile": [64, 64],
    "fsize": [1024 * 1024, 1024 * 1024],
    "core": [0, 0],
}

rlimit_failures = []
for resource_name, resource_id in (
    ("cpu", resource.RLIMIT_CPU),
    ("nofile", resource.RLIMIT_NOFILE),
    ("fsize", resource.RLIMIT_FSIZE),
    ("core", resource.RLIMIT_CORE),
):
    try:
        resource.setrlimit(resource_id, tuple(applied_limits[resource_name]))
    except BaseException as exc:
        rlimit_failures.append(f"{resource_name}: {type(exc).__name__}: {exc}")

if rlimit_failures:
    _write_raw_frame({
        "op": "ready",
        "status": "rlimit_failed",
        "phase": "startup",
        "error": "; ".join(rlimit_failures),
    })
    sys.exit(1)

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
            exc_mod = type(obj).__module__
            exc_qual = type(obj).__qualname__
            if exc_mod != "builtins" or exc_qual not in ("ValueError", "RuntimeError", "TypeError", "AttributeError", "KeyError", "IndexError", "SyntaxError", "NameError", "ZeroDivisionError", "OverflowError", "MemoryError", "RecursionError", "AssertionError", "ImportError", "FileNotFoundError", "PermissionError", "OSError", "Exception"):
                exc_mod = "builtins"
                exc_qual = "RuntimeError"
            return {
                "t": "exception",
                "module": exc_mod,
                "qualname": exc_qual,
                "message": str(obj)
            }
        elif callable(obj):
            if register_obj_fn:
                c_id = register_obj_fn(obj)
                return {"t": "candidate_object", "id": c_id}
            raise TypeError(f"Callable object cannot be encoded: {obj}")
        else:
            if register_obj_fn:
                c_id = register_obj_fn(obj)
                return {"t": "candidate_object", "id": c_id}
            raise TypeError(f"Worker object cannot be encoded: {obj}")

    @classmethod
    def decode(cls, env, make_cb_fn=None, make_obj_proxy_fn=None, get_worker_obj_fn=None, make_host_obj_proxy_fn=None, get_host_obj_fn=None):
        if not isinstance(env, dict) or "t" not in env:
            return env
        t = env["t"]
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
            if make_host_obj_proxy_fn:
                return make_host_obj_proxy_fn(env["id"])
            raise ValueError("host_object received without handler")
        elif t == "candidate_object":
            if get_worker_obj_fn:
                return get_worker_obj_fn(env["id"])
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

class HostCallback:
    def __init__(self, callback_id, ipc):
        self.callback_id = callback_id
        self.ipc = ipc
    def __call__(self, *args, **kwargs):
        return self.ipc.call_host(self.callback_id, args, kwargs)

class HostObjectProxy:
    def __init__(self, obj_id, ipc):
        self._obj_id = obj_id
        self._ipc = ipc
    def __getattr__(self, name):
        def proxy_method(*args, **kwargs):
            return self._ipc.call_host_obj(self._obj_id, name, args, kwargs)
        return proxy_method
    def __getitem__(self, key):
        return self._ipc.call_host_obj(self._obj_id, "__getitem__", [key], {})
    def __setitem__(self, key, value):
        return self._ipc.call_host_obj(self._obj_id, "__setitem__", [key, value], {})
    def __len__(self):
        return self._ipc.call_host_obj(self._obj_id, "__len__", [], {})
    def __contains__(self, item):
        return self._ipc.call_host_obj(self._obj_id, "__contains__", [item], {})
    def __iter__(self):
        keys = self._ipc.call_host_obj(self._obj_id, "keys", [], {})
        return iter(keys)

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
            chunk = os.read(ipc_read_fd, num_bytes - len(buf))
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
                if resp.get("origin") == "infrastructure":
                    setattr(exc_obj, "_is_host_infrastructure", True)
                raise exc_obj
            exc_err = RuntimeError(str(exc_obj))
            if resp.get("origin") == "infrastructure":
                setattr(exc_err, "_is_host_infrastructure", True)
            raise exc_err
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
                if resp.get("origin") == "infrastructure":
                    setattr(exc_obj, "_is_host_infrastructure", True)
                raise exc_obj
            exc_err = RuntimeError(str(exc_obj))
            if resp.get("origin") == "infrastructure":
                setattr(exc_err, "_is_host_infrastructure", True)
            raise exc_err
        raise RuntimeError(resp.get("error", "Host object call failed"))

    def run(self):
        self.write_msg({
            "op": "ready",
            "status": "ok",
            "phase": "startup",
            "candidate_imported": False,
            "limits": applied_limits,
        })

        while True:
            try:
                req = self.read_msg()
            except RuntimeError:
                break
            except Exception:
                break

            op = req.get("op")
            req_id = req.get("id")

            if op == "load_candidate":
                if candidate is not None or import_exception is not None:
                    self.write_msg({"id": req_id, "status": "error", "error": "Candidate load requested more than once"})
                    continue
                try:
                    globals()["candidate"] = importlib.import_module("candidate")
                    self.write_msg({"id": req_id, "status": "ok", "result": {"phase": "candidate_import"}})
                except BaseException as e:
                    globals()["import_exception"] = e
                    enc_exc = TaggedCodec.encode(e, lambda cb: None, self.register_obj, None)
                    self.write_msg({"id": req_id, "status": "exception", "origin": "candidate", "result": enc_exc})
                continue

            if import_exception is not None:
                enc_exc = TaggedCodec.encode(import_exception, lambda cb: None, self.register_obj, None)
                self.write_msg({"id": req_id, "status": "exception", "origin": "candidate", "result": enc_exc})
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
                elif op == "get_attr":
                    obj = self.objects[req["obj_id"]]
                    attr_val = getattr(obj, req["attr_name"])
                    if callable(attr_val) and not isinstance(attr_val, (dict, list, tuple, set, str, bytes)):
                        self.write_msg({"id": req_id, "status": "ok", "result": {"is_callable": True}})
                    else:
                        enc_val = TaggedCodec.encode(attr_val, lambda cb: None, self.register_obj, None)
                        self.write_msg({"id": req_id, "status": "ok", "result": {"is_callable": False, "value": enc_val}})
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
                    self.write_msg({"id": req_id, "status": "ok", "result": True})
                else:
                    self.write_msg({"id": req_id, "status": "error", "error": f"Unknown op '{op}'"})
            except BaseException as e:
                is_infra = getattr(e, "_is_host_infrastructure", False)
                origin = "infrastructure" if is_infra else "candidate"
                enc_exc = TaggedCodec.encode(e, lambda cb: None, self.register_obj, None)
                self.write_msg({"id": req_id, "status": "exception", "origin": origin, "result": enc_exc})

if __name__ == "__main__":
    w = WorkerIPC()
    w.run()
"""


class WorkerIPCClient:
    """Host supervisor side of Worker IPC channel."""
    def __init__(self, in_fd: int, out_fd: int, deadline: float, worker_proc: subprocess.Popen = None):
        self.in_fd = in_fd
        self.out_fd = out_fd
        self.deadline = deadline
        self.worker_proc = worker_proc
        self.host_callbacks = {}
        self.cb_counter = 0
        self.host_objects = {}
        self.hobj_counter = 0
        self.object_cache = {}
        self.counter = 0
        self.last_status = None
        self.candidate_exception_occurred = False
        self.last_exception_message = ""
        self.candidate_timeout = False
        self.candidate_process_error = None
        self.infrastructure_error = None
        self.phase = "startup"
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.in_fd, selectors.EVENT_READ)

    def set_phase(self, phase: str, deadline: float) -> None:
        if phase not in ("startup", "candidate_import", "candidate_execution"):
            raise ValueError(f"Unknown worker phase: {phase}")
        self.phase = phase
        self.deadline = deadline

    def _is_candidate_phase(self) -> bool:
        return self.phase in ("candidate_import", "candidate_execution")

    def _raise_timeout(self) -> None:
        if self._is_candidate_phase():
            self.candidate_timeout = True
            raise CandidateTimeoutError(f"Candidate wall-clock timeout during {self.phase}")
        err = InfrastructureError(f"Worker wall-clock timeout during {self.phase}")
        self.infrastructure_error = err
        raise err

    def _raise_worker_exit(self, retcode: int, cause: BaseException = None) -> None:
        if self._is_candidate_phase():
            exc = CandidateProcessError(f"Candidate process terminated by signal/exit ({retcode}) during {self.phase}")
            self.candidate_process_error = exc
            if cause is not None:
                raise exc from cause
            raise exc
        err = InfrastructureError(f"Worker process exited during {self.phase} with return code {retcode}")
        self.infrastructure_error = err
        if cause is not None:
            raise err from cause
        raise err

    def register_callback(self, cb: callable) -> str:
        self.cb_counter += 1
        cb_id = f"cb_{self.cb_counter}"
        self.host_callbacks[cb_id] = cb
        return cb_id

    def register_host_object(self, obj: Any) -> str:
        self.hobj_counter += 1
        h_id = f"hobj_{self.hobj_counter}"
        self.host_objects[h_id] = obj
        return h_id

    def make_host_callback_proxy(self, cb_id: str):
        def host_cb_proxy(*args, **kwargs):
            return self.host_callbacks[cb_id](*args, **kwargs)
        return host_cb_proxy

    def make_obj_proxy(self, obj_id: str):
        if obj_id in self.object_cache:
            return self.object_cache[obj_id]
        proxy = CandidateObjectProxy(obj_id, self)
        self.object_cache[obj_id] = proxy
        return proxy

    def _read_exact(self, num_bytes: int) -> bytes:
        buf = bytearray()
        while len(buf) < num_bytes:
            remaining = self.deadline - time.time()
            if remaining <= 0:
                if self.worker_proc and self.worker_proc.poll() is not None:
                    self._raise_worker_exit(self.worker_proc.poll())
                self._raise_timeout()

            events = self.selector.select(timeout=min(0.1, remaining))
            if not events:
                if self.worker_proc and self.worker_proc.poll() is not None:
                    self._raise_worker_exit(self.worker_proc.poll())
                continue

            try:
                chunk = os.read(self.in_fd, num_bytes - len(buf))
            except OSError as e:
                retcode = self.worker_proc.poll() if self.worker_proc else None
                if retcode is None and self.worker_proc:
                    try:
                        retcode = self.worker_proc.wait(timeout=1.0)
                    except Exception:
                        pass
                if retcode is not None:
                    self._raise_worker_exit(retcode, e)
                err = InfrastructureError(f"IPC pipe closed unexpectedly: {e}")
                self.infrastructure_error = err
                raise err from e

            if not chunk:
                retcode = self.worker_proc.poll() if self.worker_proc else None
                if retcode is None and self.worker_proc:
                    try:
                        retcode = self.worker_proc.wait(timeout=1.0)
                    except Exception:
                        pass
                if retcode is not None:
                    self._raise_worker_exit(retcode)
                err = InfrastructureError("Worker IPC pipe reached EOF unexpectedly")
                self.infrastructure_error = err
                raise err
            buf.extend(chunk)
        return bytes(buf)

    def _read_framed_msg(self) -> dict:
        header_bytes = self._read_exact(4)
        frame_len = struct.unpack(">I", header_bytes)[0]
        if frame_len > MAX_FRAME_SIZE:
            err = InfrastructureError(f"Worker sent oversized IPC frame ({frame_len} bytes)")
            self.infrastructure_error = err
            raise err
        payload_bytes = self._read_exact(frame_len)
        try:
            return json.loads(payload_bytes.decode("utf-8"))
        except Exception as e:
            err = InfrastructureError(f"Worker sent invalid JSON frame payload: {e}")
            self.infrastructure_error = err
            raise err from e

    def _write_framed_msg(self, msg_dict: dict):
        try:
            data = json.dumps(msg_dict).encode("utf-8")
            if len(data) > MAX_FRAME_SIZE:
                raise InfrastructureError(f"Outbound IPC frame exceeds maximum size limit ({len(data)} bytes)")
            header = struct.pack(">I", len(data))
            os.write(self.out_fd, header + data)
        except Exception as e:
            if self.worker_proc and self.worker_proc.poll() is not None:
                self._raise_worker_exit(self.worker_proc.poll(), e)
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

                # Infrastructure: callback lookup failure
                if cb is None:
                    err = InfrastructureError(f"Unknown callback ID: {cb_id}")
                    self.infrastructure_error = err
                    cb_resp = {
                        "id": cb_req_id, "status": "exception", "origin": "infrastructure",
                        "result": TaggedCodec.encode(KeyError(f"Unknown callback ID: {cb_id}"), self.register_callback, None, self.register_host_object)
                    }
                    self._write_framed_msg(cb_resp)
                    continue

                try:
                    cb_args = TaggedCodec.decode(resp.get("args", []), self.make_host_callback_proxy, self.make_obj_proxy, None, None, lambda h_id: self.host_objects[h_id])
                    cb_kwargs = TaggedCodec.decode(resp.get("kwargs", {}), self.make_host_callback_proxy, self.make_obj_proxy, None, None, lambda h_id: self.host_objects[h_id])
                except Exception as e:
                    # Infrastructure: argument deserialization failure
                    err = InfrastructureError(f"Host callback argument deserialization failure: {e}")
                    self.infrastructure_error = err
                    cb_resp = {
                        "id": cb_req_id, "status": "exception", "origin": "infrastructure",
                        "result": TaggedCodec.encode(e, self.register_callback, None, self.register_host_object)
                    }
                    self._write_framed_msg(cb_resp)
                    continue

                try:
                    res = cb(*cb_args, **cb_kwargs)
                except Exception as e:
                    # Domain: callback function itself raised — expected harness behavior, candidate may catch
                    cb_resp = {
                        "id": cb_req_id, "status": "exception", "origin": "domain",
                        "result": TaggedCodec.encode(e, self.register_callback, None, self.register_host_object)
                    }
                    self._write_framed_msg(cb_resp)
                    continue

                try:
                    cb_resp = {"id": cb_req_id, "status": "ok", "result": TaggedCodec.encode(res, self.register_callback, None, None)}
                except Exception as e:
                    # Infrastructure: result serialization failure
                    err = InfrastructureError(f"Host callback result serialization failure: {e}")
                    self.infrastructure_error = err
                    cb_resp = {
                        "id": cb_req_id, "status": "exception", "origin": "infrastructure",
                        "result": TaggedCodec.encode(e, self.register_callback, None, self.register_host_object)
                    }
                self._write_framed_msg(cb_resp)
                continue
            elif resp.get("op") == "call_host_obj":
                h_id = resp["obj_id"]
                h_obj = self.host_objects.get(h_id)
                h_req_id = resp["id"]
                method_name = resp["method"]

                # Infrastructure: host object lookup failure
                if h_obj is None:
                    err = InfrastructureError(f"Unknown host object ID: {h_id}")
                    self.infrastructure_error = err
                    h_resp = {
                        "id": h_req_id, "status": "exception", "origin": "infrastructure",
                        "result": TaggedCodec.encode(KeyError(f"Unknown host object ID: {h_id}"), self.register_callback, None, self.register_host_object)
                    }
                    self._write_framed_msg(h_resp)
                    continue

                try:
                    h_args = TaggedCodec.decode(resp.get("args", []), self.make_host_callback_proxy, self.make_obj_proxy, None, None, lambda h_id: self.host_objects[h_id])
                    h_kwargs = TaggedCodec.decode(resp.get("kwargs", {}), self.make_host_callback_proxy, self.make_obj_proxy, None, None, lambda h_id: self.host_objects[h_id])
                except Exception as e:
                    # Infrastructure: argument deserialization failure
                    err = InfrastructureError(f"Host object method argument deserialization failure: {e}")
                    self.infrastructure_error = err
                    h_resp = {
                        "id": h_req_id, "status": "exception", "origin": "infrastructure",
                        "result": TaggedCodec.encode(e, self.register_callback, None, self.register_host_object)
                    }
                    self._write_framed_msg(h_resp)
                    continue

                try:
                    if method_name == "__getitem__":
                        res = h_obj[h_args[0]]
                    elif method_name == "__setitem__":
                        h_obj[h_args[0]] = h_args[1]
                        res = None
                    elif method_name == "__len__":
                        res = len(h_obj)
                    elif method_name == "__contains__":
                        res = h_args[0] in h_obj
                    else:
                        m = getattr(h_obj, method_name)
                        res = m(*h_args, **h_kwargs)
                except Exception as e:
                    # Domain: host object operation raised — expected behavior (e.g. KeyError on dict), candidate may catch
                    h_resp = {
                        "id": h_req_id, "status": "exception", "origin": "domain",
                        "result": TaggedCodec.encode(e, self.register_callback, None, self.register_host_object)
                    }
                    self._write_framed_msg(h_resp)
                    continue

                try:
                    h_resp = {"id": h_req_id, "status": "ok", "result": TaggedCodec.encode(res, self.register_callback, None, self.register_host_object)}
                except Exception as e:
                    # Infrastructure: result serialization failure
                    err = InfrastructureError(f"Host object method result serialization failure: {e}")
                    self.infrastructure_error = err
                    h_resp = {
                        "id": h_req_id, "status": "exception", "origin": "infrastructure",
                        "result": TaggedCodec.encode(e, self.register_callback, None, self.register_host_object)
                    }
                self._write_framed_msg(h_resp)
                continue

            # Standard response matching req_id
            if resp.get("id") != req_id:
                err = InfrastructureError(f"Mismatched IPC response ID: expected {req_id}, got {resp.get('id')}")
                self.infrastructure_error = err
                raise err

            self.last_status = resp.get("status")
            if self.last_status == "ok":
                return TaggedCodec.decode(resp.get("result"), self.make_host_callback_proxy, self.make_obj_proxy, None, None, lambda h_id: self.host_objects[h_id])
            elif self.last_status == "exception":
                # P0 FIX: Distinguish between infrastructure origin and candidate/domain origin
                if resp.get("origin") == "infrastructure":
                    err_infra = InfrastructureError(f"Infrastructure failure inside candidate execution across IPC: {resp.get('result')}")
                    self.infrastructure_error = err_infra  # STICKY: persists even if caught downstream
                    raise err_infra

                self.candidate_exception_occurred = True
                res_obj = TaggedCodec.decode(resp.get("result"), self.make_host_callback_proxy, self.make_obj_proxy, None, None, lambda h_id: self.host_objects[h_id])
                if not self.last_exception_message:
                    self.last_exception_message = f"{type(res_obj).__name__}: {res_obj}" if res_obj else ""
                return res_obj
            elif self.last_status in ("no_func", "no_class"):
                return None
            else:
                err = InfrastructureError(f"Worker returned error status '{self.last_status}': {resp.get('error')}")
                self.infrastructure_error = err
                raise err


def get_candidate_sandbox_profile(workspace_root: str, cand_tmpdir: str) -> str:
    """Generates macOS sandbox-exec containment profile for candidate worker execution."""
    del workspace_root  # The repository is deliberately absent from the candidate read allowlist.
    real_cand = os.path.realpath(cand_tmpdir)
    real_py = os.path.realpath(sys.executable)
    py_prefix = os.path.realpath(sys.prefix)
    py_base_prefix = os.path.realpath(sys.base_prefix)

    profile = f"""(version 1)
(deny default)
(deny process-fork)

(allow process-exec
    (literal "{real_py}")
    (literal "{sys.executable}")
    (subpath "{py_prefix}")
    (subpath "{py_base_prefix}")
    (subpath "/usr/bin")
    (subpath "/usr/local/bin")
    (subpath "/opt/homebrew")
)

(allow sysctl-read)
(allow mach-lookup)
(allow file-read-metadata)

(allow file-read*
    (literal "/")
    (subpath "{real_cand}")
    (subpath "{py_prefix}")
    (subpath "{py_base_prefix}")
    (subpath "/usr")
    (subpath "/System")
    (subpath "/Library")
    (subpath "/opt/homebrew")
    (subpath "/private/var/db")
    (subpath "/private/var/select")
    (literal "/dev/null")
    (literal "/dev/urandom")
    (literal "/dev/random")
    (literal "/dev/zero")
)

(allow file-write*
    (subpath "{real_cand}")
    (literal "/dev/null")
)

(deny network-outbound)
(deny network-inbound)
"""
    return profile


def _find_task_dir(task_id: str) -> str:
    suite_sub = "suite_c" if task_id.startswith("c_") else "suite_p"
    task_dir = os.path.join(BENCHMARKS_DIR, suite_sub, task_id)
    if os.path.exists(task_dir):
        return task_dir
    raise InfrastructureError(f"Task directory not found for task_id '{task_id}' at {task_dir}")


def evaluate_candidate_code_sandboxed(task_id: str, candidate_code: str, timeout_seconds: int = 15) -> Tuple[int, int, bool, str]:
    """
    Host Supervisor Candidate Evaluator.
    Imports test_harness.py in host memory outside candidate workspace.
    Executes candidate code inside sandbox-exec container over dedicated IPC pipe.
    Worker startup/readiness failures invalidate the campaign. Candidate import/execution
    CPU or wall-clock exhaustion scores the candidate 0/10.
    """
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be a positive number")

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

    cpu_limit = max(1, int(timeout_seconds))
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

        worker_env = {
            "HOME": cand_tmpdir,
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "PYTHONPATH": cand_tmpdir,
            "TMPDIR": cand_tmpdir,
        }

        # Production Architecture: Redirect stdout and stderr to DEVNULL to avoid memory accumulation & deadlocks
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=cand_tmpdir,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                pass_fds=(worker_in, worker_out),
                start_new_session=True,
                env=worker_env,
            )
        except Exception as e:
            raise InfrastructureError(f"Failed to launch sandboxed worker process: {e}") from e
        os.close(worker_in)
        os.close(worker_out)

        startup_deadline = time.time() + WORKER_STARTUP_TIMEOUT_SECONDS
        ipc_client = WorkerIPCClient(host_in, host_out, startup_deadline, proc)

        ready_frame = ipc_client._read_framed_msg()
        if ready_frame.get("op") != "ready" or ready_frame.get("status") != "ok":
            err_detail = ready_frame.get("error", "Worker readiness handshake failed")
            raise InfrastructureError(f"Worker startup readiness failed: {err_detail}")
        expected_limits = {
            "cpu": [cpu_limit, cpu_limit + 1],
            "nofile": [64, 64],
            "fsize": [1024 * 1024, 1024 * 1024],
            "core": [0, 0],
        }
        if ready_frame.get("candidate_imported") is not False or ready_frame.get("limits") != expected_limits:
            raise InfrastructureError("Worker readiness did not attest pre-import RLIMIT installation")

        candidate_deadline = time.time() + timeout_seconds
        ipc_client.set_phase("candidate_import", candidate_deadline)
        ipc_client.send_request({"id": "load_candidate", "op": "load_candidate"})
        if ipc_client.candidate_exception_occurred:
            return 0, 10, False, f"CandidateImportError: {ipc_client.last_exception_message}"
        ipc_client.set_phase("candidate_execution", candidate_deadline)

        cand_proxy = CandidateModuleProxy(ipc_client)

        run_eval = getattr(harness_mod, "run_evaluation", None)
        if not run_eval:
            raise InfrastructureError(f"Test harness missing run_evaluation for {task_id}")

        eval_res = run_eval(cand_proxy)
        if type(eval_res) is not tuple or len(eval_res) != 2:
            raise InfrastructureError(
                f"Test harness run_evaluation for {task_id} must return (earned: int, max_s: int), got {eval_res}"
            )
        earned, max_s = eval_res
        if type(earned) is not int or type(max_s) is not int:
            raise InfrastructureError(
                f"Test harness run_evaluation for {task_id} must return exact int scores, got earned={type(earned)}, max_s={type(max_s)}"
            )
        if max_s <= 0 or earned < 0 or earned > max_s:
            raise InfrastructureError(
                f"Test harness run_evaluation for {task_id} score out of range: earned={earned}, max_s={max_s}"
            )

        if getattr(ipc_client, "candidate_process_error", None):
            return 0, 10, False, f"CandidateProcessError: {ipc_client.candidate_process_error}"

        if ipc_client.candidate_timeout:
            raise CandidateTimeoutError(
                f"Candidate wall-clock timeout during {ipc_client.phase}; "
                "host harness handling cannot convert it into a scored outcome"
            )
        if ipc_client.infrastructure_error:
            raise ipc_client.infrastructure_error
        if getattr(harness_mod, "_sticky_infrastructure_error", None):
            raise harness_mod._sticky_infrastructure_error

        err_trace = f"CandidateException: {ipc_client.last_exception_message}" if ipc_client.candidate_exception_occurred else ""
        return earned, max_s, True, err_trace
    except CandidateTimeoutError:
        # A live worker that stops producing protocol output is not attributable
        # to a scored candidate outcome. Registered Limitation 10 makes this a
        # campaign-fatal ambiguity.
        raise
    except CandidateProcessError as cpe:
        return 0, 10, False, f"CandidateProcessError: {cpe}"
    except Exception as e:
        if ipc_client and ipc_client.infrastructure_error:
            raise ipc_client.infrastructure_error
        if isinstance(e, InfrastructureError):
            raise
        # Candidate execution syntax / runtime error -> return candidate failure (0, 10)
        return 0, 10, False, f"{type(e).__name__}: {e}"
    finally:
        if ipc_client:
            ipc_client.selector.close()
        for fd in (host_in, host_out, worker_in, worker_out):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        if proc:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except OSError:
                    proc.kill()
                try:
                    proc.wait(timeout=2)
                except Exception:
                    pass
        shutil.rmtree(cand_tmpdir, ignore_errors=True)
