"""
candidate_runner.py — Untrusted candidate execution environment.

This process runs under candidate.sb sandbox and is supervised by evaluator.py (the host parent).
It receives framed JSON commands from stdin, executes them against the candidate
solution module, and returns framed JSON responses via stdout.

CRITICAL: stdout is the control channel. The candidate must not write to stdout.
"""
import os
import sys

# --- 0. Redirect candidate-visible stdout to /dev/null immediately ---
_control_fd = os.dup(1)  # Save the control channel fd
try:
    _devnull_fd = os.open('/dev/null', os.O_WRONLY)
    os.dup2(_devnull_fd, 1)  # Replace fd 1 with /dev/null
    os.close(_devnull_fd)
except Exception as e:
    os.write(_control_fd, f'FATAL: could not redirect stdout: {e}\n'.encode())
    sys.exit(1)

# Rebind Python's sys.stdout to /dev/null too
devnull_out = open(os.devnull, 'w')
sys.stdout = devnull_out

import json
import resource
import traceback
import uuid
import math
import types

# --- 1. Set irreversible resource limits ---
def _set_limits() -> None:
    limits = {
        resource.RLIMIT_CPU:    (30, 30),
        resource.RLIMIT_FSIZE:  (1 * 1024 * 1024, 1 * 1024 * 1024),
        resource.RLIMIT_NOFILE: (256, 256),
        resource.RLIMIT_CORE:   (0, 0),
    }
    for lim, values in limits.items():
        try:
            resource.setrlimit(lim, values)
        except Exception as e:
            os.write(_control_fd, f"FATAL: limit {lim} failed: {e}\n".encode())
            sys.exit(1)

_set_limits()

# --- 2. Control channel I/O using the saved fd ---
_control_write = os.fdopen(_control_fd, 'wb', buffering=0)

def send_response(data: dict) -> None:
    """Write a length-prefixed JSON frame to the control channel with 2MB bounds."""
    try:
        payload = json.dumps(data)
        encoded = payload.encode('utf-8')
        if len(encoded) > 2 * 1024 * 1024:
            err_data = {
                'status': 'error',
                'message': 'Response payload exceeds 2 MB limit',
                'uuid': data.get('uuid')
            }
            payload = json.dumps(err_data)
            encoded = payload.encode('utf-8')

        _control_write.write(f'{len(encoded)}\n'.encode('utf-8'))
        _control_write.write(encoded)
        _control_write.write(b'\n')
    except Exception as e:
        try:
            fallback = json.dumps({'status': 'error', 'message': f'Internal serialization error: {e}'}).encode('utf-8')
            _control_write.write(f'{len(fallback)}\n'.encode('utf-8') + fallback + b'\n')
        except Exception:
            pass

def read_request() -> dict | None:
    """Read a length-prefixed JSON frame from stdin with 2MB bounds."""
    length_line = sys.stdin.buffer.readline(32)
    if not length_line:
        return None  # EOF — clean shutdown
    if not length_line.endswith(b'\n'):
        raise ValueError('Malformed header line or exceeds 32 bytes limit')
    try:
        length = int(length_line.strip())
    except ValueError:
        raise ValueError(f'Malformed length line: {length_line!r}')
    if length <= 0 or length > 2 * 1024 * 1024:  # 2 MB cap
        raise ValueError(f'Invalid frame length: {length}')
    data = sys.stdin.buffer.read(length)
    if len(data) != length:
        raise ValueError(f'Truncated payload: expected {length}, got {len(data)}')
    delim = sys.stdin.buffer.read(1)
    if delim != b'\n':
        raise ValueError(f'Malformed trailing delimiter: expected b\'\\n\', got {delim!r}')
    return json.loads(data.decode('utf-8'))

# --- 3. Handshake Verification ---
_session_id = None
try:
    first_req = read_request()
    if first_req is None:
        sys.exit(1)
    if first_req.get('action') != 'handshake':
        os.write(_control_fd, b'FATAL: Expected handshake action first\n')
        sys.exit(1)
    _session_id = first_req.get('session_id')
    if not _session_id:
        os.write(_control_fd, b'FATAL: Missing session_id in handshake\n')
        sys.exit(1)
    # Send ready response
    req_uuid = first_req.get('uuid')
    send_response({'status': 'ready', 'uuid': req_uuid})
except Exception as e:
    try:
        os.write(_control_fd, f'FATAL: Handshake failed: {e}\n'.encode())
    except Exception:
        pass
    sys.exit(1)

# --- 4. Load candidate solution ---
_solution = None
_import_error = None

sys.path.insert(0, os.getcwd())
try:
    import solution as _solution
except Exception as e:
    _import_error = f'{type(e).__name__}: {e}'

# --- 5. Bounded Serialization Codec ---
_objects = {}
_next_obj_id = 1
_exported_class_types = set()

def register_object(val) -> int:
    for existing_id, obj in _objects.items():
        if obj is val:
            return existing_id
    if len(_objects) >= 1000:
        raise RuntimeError("Runner object store limit of 1000 exceeded")
    global _next_obj_id
    obj_id = _next_obj_id
    _next_obj_id += 1
    _objects[obj_id] = val
    return obj_id

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
            raise ValueError("Cumulative decoded payload size exceeds 2 MB")
        if utf8_size > 1 * 1024 * 1024:
            raise ValueError("String size exceeds 1 MB limit")
        return {"type": "primitive", "value": val}

    if isinstance(val, bytes):
        if len(val) > 1 * 1024 * 1024:
            raise ValueError("Bytes size exceeds 1 MB limit")
        stats["string_bytes"] += len(val)
        if stats["string_bytes"] > 2 * 1024 * 1024:
            raise ValueError("Cumulative decoded payload size exceeds 2 MB")
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

    # Check if object is an instance of an exported candidate class
    cls = type(val)
    if cls in _exported_class_types:
        obj_id = register_object(val)
        return {
            "type": "object_ref",
            "module": cls.__module__,
            "qualname": cls.__qualname__,
            "obj_id": obj_id
        }

    raise TypeError(f"Unserializable return type: {cls.__name__}")

def from_envelope(envelope, depth=0, stats=None):
    if stats is None:
        stats = {"nodes": 0, "string_bytes": 0}
    stats["nodes"] += 1
    if stats["nodes"] > 1000:
        raise ValueError("Payload exceeds maximum node limit of 1000")
    if depth > 16:
        raise ValueError("Payload depth exceeds maximum limit of 16")

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
        elif isinstance(val, bool):
            pass
        elif isinstance(val, int):
            if len(str(val)) > 100:
                raise ValueError("Integer length exceeds 100 digits")
        elif isinstance(val, str):
            utf8_size = len(val.encode('utf-8'))
            stats["string_bytes"] += utf8_size
            if stats["string_bytes"] > 2 * 1024 * 1024:
                raise ValueError("Cumulative string/bytes size exceeds 2 MB")
            if utf8_size > 1 * 1024 * 1024:
                raise ValueError("String size exceeds 1 MB limit")
        elif val is not None:
            raise ValueError(f"Invalid primitive value type: {type(val)}")
        return val

    if t == "bytes":
        if set(envelope.keys()) != {"type", "hex"}:
            raise ValueError("Invalid envelope fields for bytes")
        hex_val = envelope["hex"]
        if not isinstance(hex_val, str) or len(hex_val) > 2 * 1024 * 1024:
            raise ValueError("Invalid bytes hex length")
        val = bytes.fromhex(hex_val)
        if len(val) > 1 * 1024 * 1024:
            raise ValueError("Bytes size exceeds 1 MB limit")
        stats["string_bytes"] += len(val)
        if stats["string_bytes"] > 2 * 1024 * 1024:
            raise ValueError("Cumulative string/bytes size exceeds 2 MB")
        return val

    if t == "tuple":
        if set(envelope.keys()) != {"type", "value"}:
            raise ValueError("Invalid envelope fields for tuple")
        items = envelope["value"]
        if not isinstance(items, list):
            raise ValueError("Tuple envelope value must be a list of envelopes")
        return tuple(from_envelope(x, depth + 1, stats) for x in items)

    if t == "list":
        if set(envelope.keys()) != {"type", "value"}:
            raise ValueError("Invalid envelope fields for list")
        items = envelope["value"]
        if not isinstance(items, list):
            raise ValueError("List envelope value must be a list of envelopes")
        return [from_envelope(x, depth + 1, stats) for x in items]

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
            res[from_envelope(pair[0], depth + 1, stats)] = from_envelope(pair[1], depth + 1, stats)
        return res

    if t == "not_implemented":
        if set(envelope.keys()) != {"type"}:
            raise ValueError("Invalid envelope fields for not_implemented")
        return NotImplemented

    if t == "object_ref":
        if set(envelope.keys()) != {"type", "module", "qualname", "obj_id"}:
            raise ValueError("Invalid envelope fields for object_ref")
        obj_id = envelope["obj_id"]
        if isinstance(obj_id, bool) or not isinstance(obj_id, int) or obj_id <= 0:
            raise ValueError("Invalid object ID")
        if obj_id not in _objects:
            raise ValueError(f"Unknown object ID: {obj_id}")
        obj = _objects[obj_id]
        if type(obj).__module__ != envelope["module"] or type(obj).__qualname__ != envelope["qualname"]:
            raise ValueError("Object class identity mismatch")
        return obj

    raise ValueError(f"Unknown envelope type: {t}")

# --- 6. Main request loop ---
def _handle(req: dict) -> None:
    action = req.get('action')
    req_uuid = req.get('uuid')
    if not req_uuid:
        send_response({'status': 'error', 'message': 'Missing UUID', 'uuid': None})
        return

    try:
        if action == 'check_import':
            if _solution is not None:
                send_response({'status': 'success', 'uuid': req_uuid})
            else:
                send_response({'status': 'error', 'message': _import_error, 'uuid': req_uuid})

        elif action == 'get_exports':
            if _solution is None:
                send_response({'status': 'error', 'message': 'Module failed to import', 'uuid': req_uuid})
                return

            # Pass 1: Register all exported class types
            for name in dir(_solution):
                if name.startswith('__'):
                    continue
                attr = getattr(_solution, name)
                if isinstance(attr, type) and getattr(attr, '__module__', None) == _solution.__name__:
                    _exported_class_types.add(attr)

            # Pass 2: Build exports metadata and envelope variables
            exports = []
            for name in dir(_solution):
                if name.startswith('__'):
                    continue
                attr = getattr(_solution, name)
                if isinstance(attr, type):
                    if getattr(attr, '__module__', None) != _solution.__name__:
                        continue
                    is_exc = issubclass(attr, BaseException)
                    mro_data = [
                        {'qualname': cls.__qualname__, 'module': cls.__module__}
                        for cls in attr.__mro__
                    ]
                    bases_data = [
                        {'qualname': b.__qualname__, 'module': b.__module__}
                        for b in attr.__bases__
                        if b is not object
                    ]

                    allowed_magics = [
                        "__add__", "__radd__", "__sub__", "__rsub__", "__mul__", "__rmul__",
                        "__truediv__", "__rtruediv__", "__eq__", "__len__", "__str__", "__repr__"
                    ]
                    methods_implemented = []
                    for cls in attr.__mro__:
                        if cls in (object, BaseException, Exception):
                            continue
                        for mname in allowed_magics:
                            if mname in cls.__dict__ and mname not in methods_implemented:
                                methods_implemented.append(mname)

                    exports.append({
                        'name': name,
                        'qualname': attr.__qualname__,
                        'module': attr.__module__,
                        'type': 'class',
                        'is_exception': is_exc,
                        'bases': bases_data,
                        'mro': mro_data,
                        'magic_methods': methods_implemented
                    })
                elif callable(attr):
                    if getattr(attr, '__module__', None) != _solution.__name__:
                        continue
                    exports.append({'name': name, 'type': 'function'})
                else:
                    try:
                        enveloped = to_envelope(attr)
                        exports.append({'name': name, 'type': 'variable', 'value': enveloped})
                    except Exception:
                        pass
            send_response({'status': 'success', 'exports': exports, 'uuid': req_uuid})

        elif action == 'call_func':
            func_name = req.get('func_name')
            args = [from_envelope(x) for x in req.get('args', [])]
            kwargs = {k: from_envelope(v) for k, v in req.get('kwargs', {}).items()}
            if _solution is None:
                send_response({'status': 'error', 'message': 'Module failed to import', 'uuid': req_uuid})
                return
            func = getattr(_solution, func_name, None)
            if func is None:
                send_response({'status': 'error', 'message': f'Function {func_name} not found', 'uuid': req_uuid})
                return
            result = func(*args, **kwargs)
            send_response({'status': 'success', 'result': to_envelope(result), 'uuid': req_uuid})

        elif action == 'init_class':
            class_name = req.get('class_name')
            args = [from_envelope(x) for x in req.get('args', [])]
            kwargs = {k: from_envelope(v) for k, v in req.get('kwargs', {}).items()}
            if _solution is None:
                send_response({'status': 'error', 'message': 'Module failed to import', 'uuid': req_uuid})
                return
            cls = getattr(_solution, class_name, None)
            if cls is None:
                send_response({'status': 'error', 'message': f'Class {class_name} not found', 'uuid': req_uuid})
                return
            obj = cls(*args, **kwargs)
            obj_id = register_object(obj)
            send_response({'status': 'success', 'obj_id': obj_id, 'uuid': req_uuid})

        elif action == 'call_method':
            obj_id = req.get('obj_id')
            method_name = req.get('method_name')
            args = [from_envelope(x) for x in req.get('args', [])]
            kwargs = {k: from_envelope(v) for k, v in req.get('kwargs', {}).items()}
            obj = _objects.get(obj_id)
            if obj is None:
                send_response({'status': 'error', 'message': 'Object not found', 'uuid': req_uuid})
                return
            method = getattr(obj, method_name, None)
            if method is None:
                send_response({'status': 'error', 'message': f'Method {method_name} not found', 'uuid': req_uuid})
                return
            result = method(*args, **kwargs)

            # Check type of returned value from specific special methods
            if method_name == "__len__":
                if not isinstance(result, int) or result < 0:
                    raise TypeError(f"__len__ must return non-negative int, got {result}")
            elif method_name in ("__str__", "__repr__"):
                if not isinstance(result, str):
                    raise TypeError(f"{method_name} must return str, got {type(result)}")

            send_response({'status': 'success', 'result': to_envelope(result), 'uuid': req_uuid})

        elif action == 'get_attr':
            obj_id = req.get('obj_id')
            name = req.get('name')
            obj = _objects.get(obj_id)
            if obj is None:
                send_response({'status': 'error', 'message': 'Object not found', 'uuid': req_uuid})
                return

            if name.startswith('_'):
                send_response({'status': 'error', 'message': 'PermissionError: Private attribute access denied', 'uuid': req_uuid})
                return

            try:
                attr = getattr(obj, name)
            except AttributeError as ae:
                send_response({'status': 'error', 'message': f'AttributeError: {ae}', 'uuid': req_uuid})
                return

            if callable(attr):
                if isinstance(attr, (types.MethodType, types.FunctionType, types.BuiltinMethodType, types.BuiltinFunctionType)):
                    send_response({
                        'status': 'success',
                        'result': {'type': 'callable_ref', 'obj_id': obj_id, 'name': name},
                        'uuid': req_uuid
                    })
                else:
                    send_response({
                        'status': 'error',
                        'message': 'TypeError: Accessing arbitrary callable objects is restricted',
                        'uuid': req_uuid
                    })
            else:
                send_response({
                    'status': 'success',
                    'result': to_envelope(attr),
                    'uuid': req_uuid
                })

        elif action == 'shutdown':
            send_response({'status': 'shutdown_ok', 'uuid': req_uuid})
            _control_write.flush()
            _control_write.close()
            sys.stdin.close()
            sys.exit(0)

        else:
            send_response({'status': 'error', 'message': f'Unknown action: {action}', 'uuid': req_uuid})

    except SystemExit:
        raise
    except Exception as e:
        tb_str = traceback.format_exc()
        send_response({
            'status': 'exception',
            'type': type(e).__name__,
            'qualname': type(e).__qualname__,
            'module': type(e).__module__,
            'message': str(e),
            'traceback': tb_str,
            'uuid': req_uuid,
        })

def main():
    while True:
        try:
            req = read_request()
        except Exception as e:
            send_response({'status': 'error', 'message': f'Read error: {e}', 'uuid': None})
            sys.exit(1)
        if req is None:
            break  # EOF
        if req.get('session_id') != _session_id:
            send_response({'status': 'error', 'message': 'Invalid session ID', 'uuid': req.get('uuid')})
            continue
        _handle(req)

if __name__ == '__main__':
    main()
