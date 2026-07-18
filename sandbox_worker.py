"""
sandbox_worker.py — Trusted IPC mediator between evaluator.py (host) and candidate_runner.py.

Responsibilities:
- Receives framed JSON commands from evaluator.py via stdin.
- Forwards commands (with correlation UUID) to candidate_runner.py via its stdin pipe.
- Reads responses from candidate_runner.py, validates UUID, forwards to evaluator.py via stdout.
- Implements EOF-based shutdown: after forwarding shutdown command, reads runner's stdout to
  EOF. Any trailing frames after the shutdown_ok response invalidate the evaluation.
- Never imports or executes solution code.

This process runs under worker.sb (deny-default profile). No network, no fork, no harness reads.
"""
import os
import sys

# --- Scrub environment immediately ---
_allowed_env = {
    "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
    "LANG": "en_US.UTF-8",
    "PYTHONIOENCODING": "utf-8",
}
os.environ.clear()
os.environ.update(_allowed_env)

import json
import subprocess
import signal

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Framing helpers ──────────────────────────────────────────────────────────

def _write_frame(fp, data: dict) -> None:
    payload = json.dumps(data)
    fp.write(f"{len(payload)}\n{payload}\n")
    fp.flush()

def _read_frame(fp) -> dict | None:
    """Read one length-prefixed JSON frame. Returns None on clean EOF."""
    length_line = fp.readline()
    if not length_line:
        return None  # EOF
    try:
        length = int(length_line.strip())
    except ValueError:
        raise ValueError(f"Malformed length line from runner: {length_line!r}")
    if length > 1 * 1024 * 1024:
        raise ValueError(f"Oversized frame from runner: {length}")
    data = fp.read(length)
    if len(data) != length:
        raise ValueError(f"Truncated frame: expected {length}, got {len(data)}")
    fp.readline()  # consume trailing newline
    return json.loads(data)

# ── Host I/O ─────────────────────────────────────────────────────────────────

def host_send(data: dict) -> None:
    _write_frame(sys.stdout, data)

def host_recv() -> dict | None:
    length_line = sys.stdin.readline()
    if not length_line:
        return None
    try:
        length = int(length_line.strip())
    except ValueError:
        raise ValueError(f"Malformed length from host: {length_line!r}")
    if length > 10 * 1024 * 1024:
        raise ValueError("Oversized payload from host")
    data = sys.stdin.read(length)
    if len(data) != length:
        raise ValueError("Truncated payload from host")
    sys.stdin.readline()
    return json.loads(data)

# ── Runner process ────────────────────────────────────────────────────────────

_runner_proc = None

def _start_runner(candidate_dir: str, sb_path: str) -> None:
    """Spawn candidate_runner.py under candidate.sb sandbox."""
    global _runner_proc
    venv_python = os.path.join(BASE_DIR, ".venv", "bin", "python")
    runner_path = os.path.join(BASE_DIR, "candidate_runner.py")

    allowed_env = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "LANG": "en_US.UTF-8",
        "PYTHONIOENCODING": "utf-8",
    }

    cmd = [
        "sandbox-exec", "-f", sb_path,
        venv_python, "-u", runner_path,
    ]
    _runner_proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=allowed_env,
        cwd=candidate_dir,
        close_fds=True,
        start_new_session=True,
    )

def _forward(req: dict) -> dict:
    """Forward one request to runner, validate UUID, return response."""
    if _runner_proc is None or _runner_proc.poll() is not None:
        raise RuntimeError("Runner process is not running")
    req_uuid = req.get("uuid")
    if not req_uuid:
        raise ValueError("Request missing UUID")

    _write_frame(_runner_proc.stdin, req)

    resp = _read_frame(_runner_proc.stdout)
    if resp is None:
        raise RuntimeError("Runner closed stdout unexpectedly")

    resp_uuid = resp.get("uuid")
    if resp_uuid != req_uuid:
        raise RuntimeError(
            f"UUID mismatch: sent {req_uuid!r}, got {resp_uuid!r}"
        )
    return resp

def _shutdown_runner() -> bool:
    """
    Send shutdown to runner, read its stdout to EOF.
    Returns True if clean (no trailing frames), False if trailing bytes seen.
    """
    import uuid as _uuid_mod
    req_uuid = str(_uuid_mod.uuid4())
    shutdown_req = {"action": "shutdown", "uuid": req_uuid}

    if _runner_proc is None or _runner_proc.poll() is not None:
        return True  # Already dead — treat as clean

    # Send shutdown command
    _write_frame(_runner_proc.stdin, shutdown_req)
    _runner_proc.stdin.flush()

    # Read until EOF, tracking whether we saw the confirmation and any trailing data
    confirmation_seen = False
    trailing_frames = 0

    while True:
        frame = _read_frame(_runner_proc.stdout)
        if frame is None:
            break  # Clean EOF
        if not confirmation_seen:
            # Validate UUID on confirmation
            if frame.get("uuid") != req_uuid:
                trailing_frames += 1
            elif frame.get("status") == "shutdown_ok":
                confirmation_seen = True
            else:
                trailing_frames += 1
        else:
            # Any frame after confirmation is a trailing frame — invalidate
            trailing_frames += 1

    # Reap runner
    try:
        _runner_proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        _runner_proc.kill()
        _runner_proc.wait(timeout=1.0)

    return confirmation_seen and trailing_frames == 0

def _kill_runner() -> None:
    if _runner_proc is not None and _runner_proc.poll() is None:
        try:
            _runner_proc.kill()
            _runner_proc.wait(timeout=1.0)
        except Exception:
            pass

# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    # The evaluator passes candidate_dir and sb_path as the first two lines of stdin
    # before the framed command stream begins. We read them as plain lines.
    candidate_dir_line = sys.stdin.readline().strip()
    sb_path_line = sys.stdin.readline().strip()
    if not candidate_dir_line or not sb_path_line:
        host_send({"status": "error", "message": "Missing startup parameters"})
        sys.exit(1)

    try:
        _start_runner(candidate_dir_line, sb_path_line)
    except Exception as e:
        host_send({"status": "error", "message": f"Runner startup failed: {e}"})
        sys.exit(1)

    # Signal host that worker+runner are ready
    host_send({"status": "ready"})

    seen_uuids: set = set()

    while True:
        try:
            req = host_recv()
        except Exception as e:
            host_send({"status": "error", "message": f"Host read error: {e}"})
            _kill_runner()
            sys.exit(1)

        if req is None:
            # Host closed stdin — kill runner and exit
            _kill_runner()
            break

        action = req.get("action")
        req_uuid = req.get("uuid")

        # Duplicate UUID detection
        if req_uuid in seen_uuids:
            host_send({
                "status": "error",
                "message": f"Duplicate UUID detected: {req_uuid}",
                "uuid": req_uuid,
            })
            _kill_runner()
            sys.exit(1)
        if req_uuid:
            seen_uuids.add(req_uuid)

        if action == "shutdown":
            clean = _shutdown_runner()
            if clean:
                host_send({"status": "shutdown_ok", "uuid": req_uuid})
            else:
                host_send({
                    "status": "error",
                    "message": "Trailing frames detected after runner shutdown confirmation — evaluation invalidated",
                    "uuid": req_uuid,
                })
            break

        # Forward to runner
        try:
            resp = _forward(req)
        except Exception as e:
            host_send({"status": "error", "message": f"Runner forwarding error: {e}", "uuid": req_uuid})
            _kill_runner()
            sys.exit(1)

        # Forward runner response to host
        host_send(resp)

if __name__ == "__main__":
    main()
