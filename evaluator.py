"""
evaluator.py — Host parent supervisor for RSI evaluation harness.
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
import math

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

class HarnessInfrastructureError(Exception):
    """Failure of trusted machinery (e.g. sandbox, canary, smoke test, preflight)."""
    pass

class CandidateExecutionError(Exception):
    """Standard evaluation failures (e.g. unit test failures, solutions exceptions)."""
    pass

class CandidateProtocolError(Exception):
    """Protocol failures after handshake (e.g. runner crash, signal termination, timeout, invalid JSON)."""
    pass

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
        raise CandidateProtocolError(
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

    reported_mro = spec.get("mro", [])
    reconstructed_mro = exc_cls.__mro__
    for i, reported_entry in enumerate(reported_mro):
        r_qualname = reported_entry["qualname"]
        r_module = reported_entry["module"]
        if i < len(reconstructed_mro):
            actual_cls = reconstructed_mro[i]
            if actual_cls.__qualname__ != r_qualname or actual_cls.__module__ != r_module:
                raise CandidateProtocolError(
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
        except Exception as reconstruct_err:
            if isinstance(reconstruct_err, (CandidateProtocolError, CandidateExecutionError)):
                raise
            raise CandidateExecutionError(
                f"Sandbox exception (unresolvable type {exc_qualname!r}): {msg}"
            ) from reconstruct_err
    else:
        raise CandidateProtocolError(f"Sandbox error: {resp.get('message', 'unknown')}")

# ── Safe Bounded Codec ───────────────────────────────────────────────────────

class _DirectObjRef:
    def __init__(self, obj_id: int):
        self.obj_id = obj_id

def to_envelope(val, depth=0, stats=None) -> dict:
    if stats is None:
        stats = {"nodes": 0, "string_bytes": 0}
    stats["nodes"] += 1
    if stats["nodes"] > 1000:
        raise ValueError("Payload exceeds maximum node limit of 1000")
    if depth > 16:
        raise ValueError("Payload depth exceeds maximum limit of 16")

    if val is None or isinstance(val, bool):
        return {"type": "primitive", "value": val}

    if isinstance(val, int):
        if len(str(val)) > 100:
            raise ValueError("Integer length exceeds 100 digits")
        return {"type": "primitive", "value": val}

    if isinstance(val, float):
        if math.isnan(val) or math.isinf(val):
            raise ValueError("NaN and Infinity are not allowed")
        return {"type": "primitive", "value": val}

    if isinstance(val, str):
        utf8_size = len(val.encode('utf-8'))
        stats["string_bytes"] += utf8_size
        if stats["string_bytes"] > 2 * 1024 * 1024:
            raise ValueError("Cumulative string/bytes size exceeds 2 MB")
        if utf8_size > 1 * 1024 * 1024:
            raise ValueError("String size exceeds 1 MB limit")
        return {"type": "primitive", "value": val}

    if isinstance(val, bytes):
        if len(val) > 1 * 1024 * 1024:
            raise ValueError("Bytes size exceeds 1 MB limit")
        stats["string_bytes"] += len(val)
        if stats["string_bytes"] > 2 * 1024 * 1024:
            raise ValueError("Cumulative string/bytes size exceeds 2 MB")
        return {"type": "bytes", "hex": val.hex()}

    if isinstance(val, list):
        return {"type": "list", "value": [to_envelope(x, depth + 1, stats) for x in val]}

    if isinstance(val, tuple):
        return {"type": "tuple", "value": [to_envelope(x, depth + 1, stats) for x in val]}

    if isinstance(val, dict):
        items = []
        for k, v in val.items():
            items.append([to_envelope(k, depth + 1, stats), to_envelope(v, depth + 1, stats)])
        return {"type": "dict", "value": items}

    if val is NotImplemented:
        return {"type": "not_implemented"}

    if hasattr(val, "obj_id"):
        return {
            "type": "object_ref",
            "module": getattr(val, "_module", "solution"),
            "qualname": getattr(val, "_qualname", val.__class__.__name__),
            "obj_id": val.obj_id
        }

    raise TypeError(f"Unserializable argument type: {type(val)}")

def from_envelope(envelope):
    if not isinstance(envelope, dict):
        raise ValueError("Invalid envelope: not a dictionary")
    t = envelope.get("type")
    if t is None:
        raise ValueError("Invalid envelope: missing type tag")

    if t == "primitive":
        if set(envelope.keys()) != {"type", "value"}:
            raise ValueError("Invalid envelope fields for primitive")
        val = envelope["value"]
        if isinstance(val, float):
            if math.isnan(val) or math.isinf(val):
                raise ValueError("NaN and Infinity are not allowed")
        elif isinstance(val, int):
            if len(str(val)) > 100:
                raise ValueError("Integer length exceeds 100 digits")
        elif isinstance(val, str):
            if len(val.encode('utf-8')) > 1 * 1024 * 1024:
                raise ValueError("String size exceeds 1 MB limit")
        elif val is not None and not isinstance(val, bool):
            raise ValueError(f"Invalid primitive value type: {type(val)}")
        return val

    if t == "bytes":
        if set(envelope.keys()) != {"type", "hex"}:
            raise ValueError("Invalid envelope fields for bytes")
        hex_val = envelope["hex"]
        if not isinstance(hex_val, str) or len(hex_val) > 2 * 1024 * 1024:
            raise ValueError("Invalid bytes hex length")
        return bytes.fromhex(hex_val)

    if t == "tuple":
        if set(envelope.keys()) != {"type", "value"}:
            raise ValueError("Invalid envelope fields for tuple")
        items = envelope["value"]
        if not isinstance(items, list):
            raise ValueError("Tuple envelope value must be a list of envelopes")
        return tuple(from_envelope(x) for x in items)

    if t == "list":
        if set(envelope.keys()) != {"type", "value"}:
            raise ValueError("Invalid envelope fields for list")
        items = envelope["value"]
        if not isinstance(items, list):
            raise ValueError("List envelope value must be a list of envelopes")
        return [from_envelope(x) for x in items]

    if t == "dict":
        if set(envelope.keys()) != {"type", "value"}:
            raise ValueError("Invalid envelope fields for dict")
        items = envelope["value"]
        if not isinstance(items, list):
            raise ValueError("Dict envelope value must be a list of envelope pairs")
        res = {}
        for pair in items:
            if not isinstance(pair, list) or len(pair) != 2:
                raise ValueError("Dict envelope items must be key-value envelope pairs")
            res[from_envelope(pair[0])] = from_envelope(pair[1])
        return res

    if t == "not_implemented":
        if set(envelope.keys()) != {"type"}:
            raise ValueError("Invalid envelope fields for not_implemented")
        return NotImplemented

    if t == "object_ref":
        if set(envelope.keys()) != {"type", "module", "qualname", "obj_id"}:
            raise ValueError("Invalid envelope fields for object_ref")
        cls_name = envelope["qualname"]
        obj_id = envelope["obj_id"]
        magic_methods = []
        if global_runner:
            for item in global_runner._all_exports:
                if item.get("qualname") == cls_name and item.get("module") == envelope["module"]:
                    magic_methods = item.get("magic_methods", [])
                    break
        proxy_cls = make_class_proxy(cls_name, magic_methods)
        proxy = proxy_cls(_DirectObjRef(obj_id))
        proxy._module = envelope["module"]
        proxy._qualname = cls_name
        return proxy

    if t == "callable_ref":
        if set(envelope.keys()) != {"type", "obj_id", "name"}:
            raise ValueError("Invalid envelope fields for callable_ref")
        return envelope

    raise ValueError(f"Unknown envelope type: {t}")

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
        self._session_id = str(uuid.uuid4())

    def _build_candidate_sb(self) -> str:
        template_path = os.path.join(BASE_DIR, "candidate.sb.template")
        if not os.path.exists(template_path):
            raise HarnessInfrastructureError(f"Sandbox template missing: {template_path}")
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
            raise HarnessInfrastructureError("sandbox-exec not available on this system")

        venv_python = os.path.join(BASE_DIR, ".venv", "bin", "python")
        runner_path = os.path.join(BASE_DIR, "candidate_runner.py")

        allowed_env = {
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
            "LANG": "en_US.UTF-8",
            "PYTHONIOENCODING": "utf-8",
            "TMPDIR": self._temp_dir,
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
            text=False,
            env=allowed_env,
            cwd=self._candidate_dir,
            close_fds=True,
            start_new_session=True,
        )

        stderr_fd = os.dup(self._proc.stderr.fileno())
        self._proc.stderr.close()
        self._stderr_drainer = _PipeDrainer(stderr_fd, "runner-stderr", self._drain_stop)
        self._stderr_drainer.start()

        # Perform Handshake
        handshake_uuid = str(uuid.uuid4())
        handshake_payload = {
            "action": "handshake",
            "session_id": self._session_id,
            "uuid": handshake_uuid
        }
        self._write_frame(handshake_payload)
        resp = self._read_frame(timeout=10.0)
        if resp is None or resp.get("status") != "ready" or resp.get("uuid") != handshake_uuid:
            raise HarnessInfrastructureError("Handshake failed: runner not ready or protocol error")

    def _write_frame(self, data: dict) -> None:
        payload = json.dumps(data).encode('utf-8')
        if len(payload) > 2 * 1024 * 1024:
            raise ValueError("Request frame size exceeds 2 MB")
        header = f"{len(payload)}\n".encode('utf-8')
        self._proc.stdin.write(header + payload + b"\n")
        self._proc.stdin.flush()

    def _read_bytes_with_deadline(self, n: int, deadline: float) -> bytes:
        import time
        buf = bytearray()
        fd = self._proc.stdout.fileno()
        os.set_blocking(fd, False)
        sel = selectors.DefaultSelector()
        sel.register(fd, selectors.EVENT_READ)
        try:
            while len(buf) < n:
                rem = deadline - time.time()
                if rem <= 0:
                    raise TimeoutError("IPC read timeout exceeded")
                events = sel.select(timeout=max(0.1, rem))
                if not events:
                    continue
                try:
                    chunk = os.read(fd, n - len(buf))
                except OSError:
                    break
                if not chunk:
                    break  # EOF
                buf.extend(chunk)
            if len(buf) < n:
                raise ValueError(f"Truncated read: expected {n} bytes, got {len(buf)}")
            return bytes(buf)
        finally:
            sel.close()
            os.set_blocking(fd, True)

    def _read_line_with_deadline(self, deadline: float) -> str:
        import time
        buf = bytearray()
        fd = self._proc.stdout.fileno()
        os.set_blocking(fd, False)
        sel = selectors.DefaultSelector()
        sel.register(fd, selectors.EVENT_READ)
        try:
            while True:
                rem = deadline - time.time()
                if rem <= 0:
                    raise TimeoutError("IPC read timeout exceeded")
                events = sel.select(timeout=max(0.1, rem))
                if not events:
                    continue
                try:
                    chunk = os.read(fd, 1)
                except OSError:
                    break
                if not chunk:
                    break  # EOF
                buf.extend(chunk)
                if chunk == b"\n":
                    break
            return buf.decode('utf-8', errors='replace')
        finally:
            sel.close()
            os.set_blocking(fd, True)

    def _read_frame(self, timeout: float = 10.0) -> dict | None:
        import time
        deadline = time.time() + timeout
        try:
            length_line = self._read_line_with_deadline(deadline)
        except (TimeoutError, ValueError) as e:
            raise CandidateProtocolError(f"IPC read timeout/failure: {e}")
        if not length_line:
            return None
        try:
            length = int(length_line.strip())
        except ValueError:
            raise CandidateProtocolError(f"Malformed length from runner: {length_line!r}")
        if length > 2 * 1024 * 1024:
            raise CandidateProtocolError("Oversized runner response (>2MB)")
        try:
            data = self._read_bytes_with_deadline(length, deadline)
        except (TimeoutError, ValueError) as e:
            raise CandidateProtocolError(f"IPC payload read timeout/failure: {e}")
        try:
            self._read_bytes_with_deadline(1, deadline)  # trailing newline
        except Exception:
            pass
        return json.loads(data.decode('utf-8'))

    def send_request(self, req: dict) -> dict:
        if self._proc is None or self._proc.poll() is not None:
            raise CandidateProtocolError("Runner process is not running")
        req_uuid = str(uuid.uuid4())
        if req_uuid in self._sent_uuids:
            raise HarnessInfrastructureError("UUID collision")
        self._sent_uuids.add(req_uuid)
        req["uuid"] = req_uuid
        req["session_id"] = self._session_id

        try:
            self._write_frame(req)
            resp = self._read_frame()
        except Exception as e:
            self._shutdown_clean = False
            self.close()
            if isinstance(e, (CandidateProtocolError, HarnessInfrastructureError)):
                raise
            raise CandidateProtocolError(f"Request failed: {e}") from e

        if resp is None:
            stderr_data = self._stderr_drainer.diagnostics.decode(errors="replace") if self._stderr_drainer else ""
            raise CandidateProtocolError(
                f"Runner exited unexpectedly. Stderr:\n{stderr_data[:2048]}"
            )

        resp_uuid = resp.get("uuid")
        if resp_uuid != req_uuid:
            raise CandidateProtocolError(
                f"UUID mismatch from runner: sent {req_uuid!r}, got {resp_uuid!r}"
            )
        return resp

    def shutdown(self) -> bool:
        if self._proc is None or self._proc.poll() is not None:
            sys.stderr.write("DEBUG: shutdown: process already terminated\n")
            return True
        req_uuid = str(uuid.uuid4())
        try:
            self._write_frame({"action": "shutdown", "uuid": req_uuid, "session_id": self._session_id})
            resp = self._read_frame()
            sys.stderr.write(f"DEBUG: shutdown response: {resp}\n")
            sys.stderr.flush()
            if resp is None:
                self._shutdown_clean = False
                sys.stderr.write("DEBUG: shutdown: resp is None\n")
                return False
            if resp.get("status") == "shutdown_ok" and resp.get("uuid") == req_uuid:
                # EOF read validation
                import time
                deadline = time.time() + 2.0
                try:
                    extra = self._read_frame(timeout=2.0)
                    sys.stderr.write(f"DEBUG: shutdown extra check: {extra}\n")
                    if extra is not None:
                        self._shutdown_clean = False
                        return False
                except Exception as ex:
                    sys.stderr.write(f"DEBUG: shutdown extra exception: {ex}\n")

                os.set_blocking(self._proc.stdout.fileno(), False)
                try:
                    trailing = os.read(self._proc.stdout.fileno(), 4096)
                    sys.stderr.write(f"DEBUG: shutdown trailing check: {trailing!r}\n")
                    if trailing:
                        self._shutdown_clean = False
                        return False
                except OSError as oe:
                    sys.stderr.write(f"DEBUG: shutdown trailing OSError: {oe}\n")
                finally:
                    os.set_blocking(self._proc.stdout.fileno(), True)

                self._shutdown_clean = True
                return True
            else:
                sys.stderr.write(f"DEBUG: shutdown status/uuid mismatch: status={resp.get('status')}, uuid={resp.get('uuid')}\n")
                self._shutdown_clean = False
                return False
        except Exception as e:
            sys.stderr.write(f"DEBUG: shutdown exception: {e}\n")
            sys.stderr.flush()
            self._shutdown_clean = False
            return False

    def close(self) -> None:
        if hasattr(self, "_drain_stop"):
            self._drain_stop.set()
        if self._proc is not None:
            if self._proc.poll() is None:
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

        if getattr(self, "_stderr_drainer", None) is not None:
            self._stderr_drainer.join(timeout=2.0)

    def assert_no_threads(self) -> None:
        drainer = getattr(self, "_stderr_drainer", None)
        if drainer is not None and drainer.is_alive():
            raise RuntimeError(
                f"Drain thread {drainer.name!r} still alive after shutdown — evaluation invalidated"
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
        raise CandidateProtocolError(f"Failed to get exports: {res.get('message')}")

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
        val = _handle_response(res, self._all_exports)
        return from_envelope(val)

    def call_func(self, func_name: str, args: list, kwargs: dict | None = None):
        res = self.send_request({
            "action": "call_func",
            "func_name": func_name,
            "args": [to_envelope(x) for x in args],
            "kwargs": {k: to_envelope(v) for k, v in (kwargs or {}).items()},
        })
        val = _handle_response(res, self._all_exports)
        return from_envelope(val)

    def get_attr(self, obj_id: int, name: str):
        res = self.send_request({
            "action": "get_attr",
            "obj_id": obj_id,
            "name": name,
        })
        val = _handle_response(res, self._all_exports)
        return from_envelope(val)

WorkerProxy = RunnerProxy
global_runner: RunnerProxy | None = None

def make_class_proxy(class_name: str, magic_methods: list):
    class DynamicClassProxy:
        def __init__(self, *args, **kwargs):
            if len(args) == 1 and isinstance(args[0], _DirectObjRef):
                self.obj_id = args[0].obj_id
            else:
                serialized_args = [to_envelope(x) for x in args]
                serialized_kwargs = {k: to_envelope(v) for k, v in kwargs.items()}
                self.obj_id = global_runner.init_class(class_name, serialized_args, serialized_kwargs)

        def __getattr__(self, name: str):
            if name.startswith('_'):
                raise AttributeError(name)
            res = global_runner.get_attr(self.obj_id, name)
            if isinstance(res, dict) and res.get("type") == "callable_ref":
                def _method_proxy(*args, **kwargs):
                    serialized_args = [to_envelope(x) for x in args]
                    serialized_kwargs = {k: to_envelope(v) for k, v in kwargs.items()}
                    return global_runner.call_method(self.obj_id, name, serialized_args, serialized_kwargs)
                return _method_proxy
            return res

    def make_magic(magic_name):
        if magic_name in ("__str__", "__repr__"):
            def _magic(self):
                res = global_runner.call_method(self.obj_id, magic_name, [], None)
                if not isinstance(res, str):
                    raise TypeError(f"{magic_name} must return str, got {type(res)}")
                return res
            return _magic
        elif magic_name == "__len__":
            def _magic(self):
                res = global_runner.call_method(self.obj_id, magic_name, [], None)
                if not isinstance(res, int) or res < 0:
                    raise TypeError(f"__len__ must return non-negative int, got {res}")
                return res
            return _magic
        else:
            def _magic(self, other):
                res = global_runner.call_method(self.obj_id, magic_name, [to_envelope(other)], None)
                if isinstance(res, dict) and res.get("type") == "not_implemented":
                    return NotImplemented
                return res
            return _magic

    ALLOWED_MAGIC_METHODS = {
        "__add__", "__radd__", "__sub__", "__rsub__", "__mul__", "__rmul__",
        "__truediv__", "__rtruediv__", "__eq__", "__len__", "__str__", "__repr__"
    }
    for m in magic_methods:
        if m in ALLOWED_MAGIC_METHODS:
            setattr(DynamicClassProxy, m, make_magic(m))

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
    eval_result = {"success": False, "error": "Unknown evaluation error"}
    handshake_ok = False
    result_override = None

    try:
        try:
            runner.start()
            handshake_ok = True
        except Exception as e:
            if isinstance(e, HarnessInfrastructureError):
                raise
            raise HarnessInfrastructureError(f"Runner startup or handshake failed: {e}") from e

        global_runner = runner

        try:
            try:
                runner.check_import()
            except Exception as e:
                eval_result = {"success": False, "error": f"Import failed: {e}"}
                return eval_result

            try:
                exports = runner.get_exports()
            except Exception as e:
                eval_result = {"success": False, "error": f"Export failed: {e}"}
                return eval_result

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
                        magic_methods = item.get("magic_methods", [])
                        setattr(mock_solution, name, make_class_proxy(name, magic_methods))
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

                # Custom Test Runner with strict skipped check
                class CustomTextTestRunner(unittest.TextTestRunner):
                    def run(self, test):
                        result = super().run(test)
                        if result.skipped:
                            raise RuntimeError(f"Skipped tests are not allowed, got {len(result.skipped)}")
                        return result

                run_res = CustomTextTestRunner(stream=stream, verbosity=2).run(suite)
                signal.alarm(0)
                success = run_res.wasSuccessful()
                eval_result = {"success": success, "error": None if success else stream.getvalue(), "log": stream.getvalue()}
            except TimeoutError as te:
                eval_result = {"success": False, "error": str(te)}
            except Exception as e:
                eval_result = {"success": False, "error": f"Test runner execution error: {e}"}
            finally:
                signal.alarm(0)
        except Exception as e:
            if isinstance(e, (CandidateProtocolError, CandidateExecutionError)):
                eval_result = {"success": False, "error": str(e)}
            else:
                eval_result = {"success": False, "error": f"Evaluation path error: {e}"}

    except HarnessInfrastructureError:
        raise
    except Exception as e:
        raise HarnessInfrastructureError(f"Evaluator internal exception: {e}") from e
    finally:
        shutdown_clean = True
        if handshake_ok:
            try:
                shutdown_clean = runner.shutdown()
            except Exception:
                shutdown_clean = False

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

        if handshake_ok:
            if thread_err is not None:
                raise HarnessInfrastructureError(f"Stranded thread detected: {thread_err}")
            if not shutdown_clean:
                result_override = {"success": False, "error": "Runner shutdown integrity check failed (trailing data or unclean EOF)"}

    return result_override if result_override is not None else eval_result

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python evaluator.py <candidate_dir> [test_harness_path]")
        sys.exit(1)
    candidate_dir = sys.argv[1]
    test_harness_path = sys.argv[2] if len(sys.argv) > 2 else os.path.join(candidate_dir, "test_harness.py")
    try:
        result = run_evaluation(candidate_dir, test_harness_path)
        print(f"Success: {result['success']}")
        if result.get("error"):
            print(f"Error:\n{result['error']}")
        sys.exit(0 if result["success"] else 1)
    except Exception as e:
        print(f"Infrastructure Failure: {e}")
        sys.exit(2)
