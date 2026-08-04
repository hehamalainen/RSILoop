"""
Campaign 2 Attempt Lifecycle & Ledger Module.

P0-2 & P0-7 — LIFECYCLE, RETRY, & LEDGER ENFORCEMENT:
1. SCIENTIFIC_START published at runtime with read-only permission (0o444).
2. Append-only JSONL ledger (.jsonl) flushed line-by-line with os.fsync.
3. Fail-closed fault classification requiring explicit bytes_sent == 0 evidence for retryability.
4. Intent-before-completion ordering verification.
"""

import os
import json
import stat
import time
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCIENTIFIC_START_REL = "campaign2_scientific_start.json"


class AttemptLifecycleError(RuntimeError):
    """Raised when attempt lifecycle state is invalid or unrecoverable."""
    pass


class AttemptInvalidatedError(AttemptLifecycleError):
    """Raised when the attempt is invalidated and cannot proceed."""
    pass


# ---- Fault Classification ----

class CallFault:
    """Classified fault from a model call."""
    __slots__ = ('fault_type', 'retryable', 'detail', 'exception_type', 'bytes_sent', 'bytes_received', 'timestamp')

    def __init__(
        self,
        fault_type: str,
        retryable: bool,
        detail: str,
        exception_type: str,
        bytes_sent: int = -1,
        bytes_received: int = -1,
    ):
        self.fault_type = fault_type
        self.retryable = retryable
        self.detail = detail
        self.exception_type = exception_type
        self.bytes_sent = bytes_sent
        self.bytes_received = bytes_received
        self.timestamp = time.time()

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}


def classify_call_fault(
    exception: Exception,
    bytes_sent: int = -1,
    bytes_received: int = -1,
) -> CallFault:
    """
    Classify a model call fault with strict evidence requirements.

    P0-7 APPROVED RETRY TAXONOMY:
    - Connection failure BEFORE request bytes cross boundary (bytes_sent == 0) -> RETRYABLE.
    - If bytes_sent > 0 or bytes_sent is unconfirmed (-1) -> NON-RETRYABLE (fail-closed).
    - Generic TimeoutError, TimeoutExpired, CalledProcessError, JSONDecodeError -> NON-RETRYABLE.
    """
    exc_type = type(exception).__name__

    # Network connection failure: retryable ONLY IF bytes_sent == 0
    if isinstance(exception, (ConnectionRefusedError, ConnectionError, OSError)):
        if "DNS" in str(exception).upper() or "REFUSED" in str(exception).upper() or "RESET" in str(exception).upper():
            if bytes_sent == 0:
                return CallFault("connection_pre_request", True, str(exception), exc_type, bytes_sent, bytes_received)
            else:
                return CallFault("connection_post_request", False,
                                 f"Connection error after {bytes_sent} bytes sent: {exception}",
                                 exc_type, bytes_sent, bytes_received)

    # Subprocess / Timeout / Execution errors — non-retryable (fail-closed)
    import subprocess
    if isinstance(exception, subprocess.TimeoutExpired):
        return CallFault("subprocess_timeout", False, f"Timeout after {exception.timeout}s", exc_type, bytes_sent, bytes_received)
    if isinstance(exception, subprocess.CalledProcessError):
        return CallFault("subprocess_error", False, f"CLI exit code {exception.returncode}", exc_type, bytes_sent, bytes_received)
    if isinstance(exception, TimeoutError):
        return CallFault("timeout", False, str(exception), exc_type, bytes_sent, bytes_received)
    if isinstance(exception, (json.JSONDecodeError, ValueError, KeyError)):
        return CallFault("parse_error", False, str(exception), exc_type, bytes_sent, bytes_received)

    from campaign2.proxy import InfrastructureError
    if isinstance(exception, InfrastructureError):
        return CallFault("infrastructure", False, str(exception), exc_type, bytes_sent, bytes_received)

    # Unknown - fail-closed
    return CallFault("unknown", False, str(exception), exc_type, bytes_sent, bytes_received)


# ---- Append-Only JSONL Attempt Ledger ----

class AttemptLedger:
    """
    Append-only JSONL call ledger (.jsonl) with line-by-line fsync durability.
    Enforces that every completion event MUST be preceded by a matching intent event.
    """

    def __init__(self, ledger_path: str):
        self.ledger_path = ledger_path
        if not ledger_path.endswith(".jsonl"):
            self.ledger_path = ledger_path + ".jsonl"
        self._seen_intents = set()
        self._completed_calls = set()

        # If file exists, load existing index
        if os.path.exists(self.ledger_path):
            self._load_existing()

    def _load_existing(self):
        with open(self.ledger_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                if entry.get("event") == "intent":
                    self._seen_intents.add(entry["call_id"])
                elif entry.get("event") == "completion":
                    self._completed_calls.add(entry["call_id"])

    def record_intent(
        self, call_id: str, call_type: str, arm: str, phase: int,
        task_id: str = "", metadata: dict = None
    ):
        """Record intent to make a model call BEFORE the call."""
        entry = {
            "event": "intent",
            "call_id": call_id,
            "call_type": call_type,
            "arm": arm,
            "phase": phase,
            "task_id": task_id,
            "timestamp_iso": datetime.now(timezone.utc).isoformat(),
        }
        if metadata:
            entry["metadata"] = metadata

        self._append_entry(entry)
        self._seen_intents.add(call_id)

    def record_completion(
        self, call_id: str, success: bool, tokens: int = 0,
        fault: Optional[CallFault] = None
    ):
        """Record completion of a model call AFTER the call."""
        if call_id not in self._seen_intents:
            raise AttemptLifecycleError(
                f"Ledger completion recorded for call_id '{call_id}' without prior intent record"
            )

        entry = {
            "event": "completion",
            "call_id": call_id,
            "success": success,
            "tokens": tokens,
            "timestamp_iso": datetime.now(timezone.utc).isoformat(),
        }
        if fault:
            entry["fault"] = fault.to_dict()

        self._append_entry(entry)
        self._completed_calls.add(call_id)

    def _append_entry(self, entry: dict):
        """Append single JSON line to ledger file with os.fsync."""
        line = json.dumps(entry, sort_keys=True) + "\n"
        with open(self.ledger_path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())

    def verify_intent_before_completion(self) -> bool:
        """Verify line-by-line that every completion has a prior intent."""
        seen = set()
        if not os.path.exists(self.ledger_path):
            return True
        with open(self.ledger_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entry = json.loads(line)
                if entry.get("event") == "intent":
                    seen.add(entry["call_id"])
                elif entry.get("event") == "completion":
                    if entry["call_id"] not in seen:
                        return False
        return True

    def get_entries(self) -> List[dict]:
        """Read all ledger entries from disk."""
        if not os.path.exists(self.ledger_path):
            return []
        entries = []
        with open(self.ledger_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    entries.append(json.loads(line))
        return entries


# ---- Scientific Start Marker ----

def publish_scientific_start(stage: str, workspace_root: str = BASE_DIR) -> str:
    """
    Publish SCIENTIFIC_START marker at runtime with mode 0o444.
    Returns path to the marker file.
    """
    marker_path = os.path.join(workspace_root, SCIENTIFIC_START_REL)

    if os.path.exists(marker_path):
        raise AttemptLifecycleError(
            f"SCIENTIFIC_START marker already exists at {marker_path}. "
            "Use check_and_resume() to handle existing markers."
        )

    marker_data = {
        "schema_version": "3",
        "stage": stage,
        "status": "published",
        "timestamp_iso": datetime.now(timezone.utc).isoformat(),
    }

    content = json.dumps(marker_data, indent=2, sort_keys=True) + "\n"
    with open(marker_path, "w", encoding="utf-8") as f:
        f.write(content)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(marker_path, 0o444)

    dir_fd = os.open(workspace_root, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)

    return marker_path


def check_and_resume(
    stage: str,
    checkpoint_path: str,
    workspace_root: str = BASE_DIR,
) -> Optional[Dict[str, Any]]:
    """
    Check for existing SCIENTIFIC_START marker and handle resume/invalidation.

    Returns:
    - None if no marker exists (fresh start)
    - checkpoint data dict if marker exists AND valid checkpoint exists
    Raises:
    - AttemptInvalidatedError if marker exists but checkpoint is corrupt or missing
    """
    marker_path = os.path.join(workspace_root, SCIENTIFIC_START_REL)

    if not os.path.exists(marker_path):
        return None

    if not os.path.exists(checkpoint_path):
        raise AttemptInvalidatedError(
            f"SCIENTIFIC_START exists at {marker_path} but checkpoint "
            f"is missing at {checkpoint_path}. Attempt is invalidated."
        )

    try:
        with open(checkpoint_path, "r", encoding="utf-8") as f:
            checkpoint_data = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        raise AttemptInvalidatedError(
            f"SCIENTIFIC_START exists at {marker_path} but checkpoint "
            f"at {checkpoint_path} is corrupt: {e}. Attempt is invalidated."
        )

    ckpt_stage = checkpoint_data.get("stage", "")
    if ckpt_stage != stage:
        raise AttemptInvalidatedError(
            f"Checkpoint stage '{ckpt_stage}' does not match requested stage '{stage}'. "
            "Attempt is invalidated."
        )

    return checkpoint_data
