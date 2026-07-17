import os
import sys

# 1. Scrub environment variables before any other imports
allowed_env = {
    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
    "LANG": "en_US.UTF-8",
    "PYTHONIOENCODING": "utf-8"
}
os.environ.clear()
os.environ.update(allowed_env)

import json
import resource
import importlib

# 2. Set irreversible resource limits
def set_limits():
    limits = {
        resource.RLIMIT_CPU: (2, 2),          # 2s CPU limit
        resource.RLIMIT_FSIZE: (1024*1024, 1024*1024),      # 1MB write limit
        resource.RLIMIT_NOFILE: (64, 64),     # 64 open descriptors
        resource.RLIMIT_CORE: (0, 0),         # Disable core dumps
    }
    for lim, values in limits.items():
        try:
            resource.setrlimit(lim, values)
        except Exception as e:
            # Mandatory limit failure -> fail closed immediately
            sys.stderr.write(f"FATAL: Failed to set limit {lim}: {e}\n")
            sys.stderr.flush()
            sys.exit(1)

set_limits()

# 3. Save original stdout and redirect standard output to stderr to prevent spoofing
control_stdout = sys.stdout
sys.stdout = sys.stderr

def send_response(response):
    try:
        data = json.dumps(response)
        control_stdout.write(f"{len(data)}\n{data}\n")
        control_stdout.flush()
    except Exception as e:
        sys.exit(1)

def read_request():
    try:
        length_line = sys.stdin.readline()
        if not length_line:
            return None
        length = int(length_line.strip())
        if length > 10 * 1024 * 1024:  # 10MB cap
            raise ValueError("Oversized payload")
        data = sys.stdin.read(length)
        if len(data) != length:
            raise ValueError("Payload truncation")
        # Consume the trailing newline
        sys.stdin.readline()
        return json.loads(data)
    except Exception as e:
        send_response({"status": "error", "message": f"Malformed request: {str(e)}"})
        return None

# Load candidate solution
solution = None
import_error = None

sys.path.insert(0, os.getcwd())

try:
    import solution
except Exception as e:
    import_error = f"{type(e).__name__}: {str(e)}"

# Simple local object store
objects = {}
next_obj_id = 1

def main():
    global next_obj_id
    while True:
        req = read_request()
        if req is None:
            break
            
        action = req.get("action")
        try:
            if action == "check_import":
                if solution is not None:
                    send_response({"status": "success"})
                else:
                    send_response({"status": "error", "message": import_error})
                    
            elif action == "get_exports":
                if solution is None:
                    send_response({"status": "error", "message": "Module failed to import"})
                else:
                    exports = []
                    for name in dir(solution):
                        if name.startswith("__"):
                            continue
                        attr = getattr(solution, name)
                        # Filter to only keep elements defined strictly inside the candidate solution
                        if getattr(attr, "__module__", None) != solution.__name__:
                            continue
                        if isinstance(attr, type):
                            is_exception = issubclass(attr, BaseException)
                            mro_names = [cls.__name__ for cls in attr.__mro__]
                            exports.append({
                                "name": name,
                                "type": "class",
                                "is_exception": is_exception,
                                "mro": mro_names
                            })
                        elif callable(attr):
                            exports.append({"name": name, "type": "function"})
                        else:
                            try:
                                json.dumps(attr)
                                exports.append({"name": name, "type": "variable", "value": attr})
                            except TypeError:
                                pass
                    send_response({"status": "success", "exports": exports})

            elif action == "call_func":
                func_name = req.get("func_name")
                args = req.get("args", [])
                kwargs = req.get("kwargs", {})
                if solution is None:
                    send_response({"status": "error", "message": "Module failed to import"})
                else:
                    func = getattr(solution, func_name, None)
                    if func is None:
                        send_response({"status": "error", "message": f"Function {func_name} not found"})
                    else:
                        res = func(*args, **kwargs)
                        send_response({"status": "success", "result": res})
                        
            elif action == "init_class":
                class_name = req.get("class_name")
                args = req.get("args", [])
                kwargs = req.get("kwargs", {})
                if solution is None:
                    send_response({"status": "error", "message": "Module failed to import"})
                else:
                    cls = getattr(solution, class_name, None)
                    if cls is None:
                        send_response({"status": "error", "message": f"Class {class_name} not found"})
                    else:
                        obj = cls(*args, **kwargs)
                        obj_id = next_obj_id
                        next_obj_id += 1
                        objects[obj_id] = obj
                        send_response({"status": "success", "obj_id": obj_id})
                        
            elif action == "call_method":
                obj_id = req.get("obj_id")
                method_name = req.get("method_name")
                args = req.get("args", [])
                kwargs = req.get("kwargs", {})
                obj = objects.get(obj_id)
                if obj is None:
                    send_response({"status": "error", "message": "Object not found"})
                else:
                    method = getattr(obj, method_name, None)
                    if method is None:
                        send_response({"status": "error", "message": f"Method {method_name} not found"})
                    else:
                        res = method(*args, **kwargs)
                        send_response({"status": "success", "result": res})
                        
            else:
                send_response({"status": "error", "message": f"Unknown action: {action}"})
                
        except Exception as e:
            import traceback
            tb_str = traceback.format_exc()
            send_response({
                "status": "exception",
                "type": type(e).__name__,
                "message": str(e),
                "traceback": tb_str
            })

if __name__ == "__main__":
    main()
