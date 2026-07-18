"""
candidate_runner.py — Untrusted candidate execution environment.

This process runs under candidate.sb sandbox and is supervised by evaluator.py (the host parent).
It receives framed JSON commands from sandbox_worker.py via stdin, executes them against the
candidate solution module, and returns framed JSON responses via stdout.

CRITICAL: stdout is the control channel. The candidate must not write to stdout.
"""
import os
import sys

# --- 0. Redirect candidate-visible stdout to /dev/null immediately ---
# The real stdout file descriptor (fd 1) is the control channel to the worker.
# We give the candidate process a /dev/null stdout so print() calls go nowhere.
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

# --- 1. Control channel I/O using the saved fd ---
_control_write = os.fdopen(_control_fd, 'w', buffering=1)

def send_response(data: dict) -> None:
    """Write a length-prefixed JSON frame to the control channel."""
    payload = json.dumps(data)
    _control_write.write(f'{len(payload)}\n{payload}\n')
    _control_write.flush()

def read_request() -> dict | None:
    """Read a length-prefixed JSON frame from stdin."""
    length_line = sys.stdin.readline()
    if not length_line:
        return None  # EOF — clean shutdown
    try:
        length = int(length_line.strip())
    except ValueError:
        raise ValueError(f'Malformed length line: {length_line!r}')
    if length > 1 * 1024 * 1024:  # 1 MB cap
        raise ValueError(f'Oversized payload: {length}')
    data = sys.stdin.read(length)
    if len(data) != length:
        raise ValueError(f'Truncated payload: expected {length}, got {len(data)}')
    sys.stdin.readline()  # consume trailing newline
    return json.loads(data)

# --- 2. Set irreversible resource limits BEFORE importing solution ---
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
            send_response({'status': 'error', 'message': f'FATAL: limit {lim} failed: {e}'})
            sys.exit(1)

_set_limits()

# --- 3. Load candidate solution ---
_solution = None
_import_error = None

sys.path.insert(0, os.getcwd())
try:
    import solution as _solution
except Exception as e:
    _import_error = f'{type(e).__name__}: {e}'

# --- 4. Object store for instance handles ---
_objects: dict = {}
_next_obj_id: int = 1

# --- 5. Main request loop ---
def _handle(req: dict) -> None:
    global _next_obj_id
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
            exports = []
            for name in dir(_solution):
                if name.startswith('__'):
                    continue
                attr = getattr(_solution, name)
                if getattr(attr, '__module__', None) != _solution.__name__:
                    continue
                if isinstance(attr, type):
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
                    exports.append({
                        'name': name,
                        'qualname': attr.__qualname__,
                        'module': attr.__module__,
                        'type': 'class',
                        'is_exception': is_exc,
                        'bases': bases_data,
                        'mro': mro_data,
                    })
                elif callable(attr):
                    exports.append({'name': name, 'type': 'function'})
                else:
                    try:
                        json.dumps(attr)
                        exports.append({'name': name, 'type': 'variable', 'value': attr})
                    except TypeError:
                        pass
            send_response({'status': 'success', 'exports': exports, 'uuid': req_uuid})

        elif action == 'call_func':
            func_name = req.get('func_name')
            args = req.get('args', [])
            kwargs = req.get('kwargs', {})
            if _solution is None:
                send_response({'status': 'error', 'message': 'Module failed to import', 'uuid': req_uuid})
                return
            func = getattr(_solution, func_name, None)
            if func is None:
                send_response({'status': 'error', 'message': f'Function {func_name} not found', 'uuid': req_uuid})
                return
            result = func(*args, **kwargs)
            send_response({'status': 'success', 'result': result, 'uuid': req_uuid})

        elif action == 'init_class':
            class_name = req.get('class_name')
            args = req.get('args', [])
            kwargs = req.get('kwargs', {})
            if _solution is None:
                send_response({'status': 'error', 'message': 'Module failed to import', 'uuid': req_uuid})
                return
            cls = getattr(_solution, class_name, None)
            if cls is None:
                send_response({'status': 'error', 'message': f'Class {class_name} not found', 'uuid': req_uuid})
                return
            obj = cls(*args, **kwargs)
            obj_id = _next_obj_id
            _next_obj_id += 1
            _objects[obj_id] = obj
            send_response({'status': 'success', 'obj_id': obj_id, 'uuid': req_uuid})

        elif action == 'call_method':
            obj_id = req.get('obj_id')
            method_name = req.get('method_name')
            args = req.get('args', [])
            kwargs = req.get('kwargs', {})
            obj = _objects.get(obj_id)
            if obj is None:
                send_response({'status': 'error', 'message': 'Object not found', 'uuid': req_uuid})
                return
            method = getattr(obj, method_name, None)
            if method is None:
                send_response({'status': 'error', 'message': f'Method {method_name} not found', 'uuid': req_uuid})
                return
            result = method(*args, **kwargs)
            send_response({'status': 'success', 'result': result, 'uuid': req_uuid})

        elif action == 'shutdown':
            # Write confirmation, close the control channel write end, exit cleanly.
            send_response({'status': 'shutdown_ok', 'uuid': req_uuid})
            _control_write.flush()
            _control_write.close()
            # Close stdin to signal EOF upstream
            sys.stdin.close()
            sys.exit(0)

        else:
            send_response({'status': 'error', 'message': f'Unknown action: {action}', 'uuid': req_uuid})

    except SystemExit:
        raise  # Let sys.exit(0) from shutdown propagate
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
        _handle(req)

if __name__ == '__main__':
    main()
