"""
Campaign 2 Attempt Lifecycle Module.

Manages the SCIENTIFIC_START marker and attempt lifecycle states:
1. SCIENTIFIC_START is published at runtime, NOT committed in source.
2. If SCIENTIFIC_START exists AND a valid durability checkpoint exists -> resume.
3. If SCIENTIFIC_START exists but checkpoint is corrupt/missing -> INVALIDATE.
4. Call fault classification from actual exception types, not caller labels.
"""

import os
import json
import time
import hashlib
from datetime import datetime, timezone
from typing import Optional, Dict, Any

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class AttemptLifecycleError(RuntimeError):
    """Raised when attempt lifecycle state is invalid or unrecoverable."""
    pass


class AttemptInvalidatedError(AttemptLifecycleError):
    """Raised when the attempt is invalidated and cannot proceed."""
    pass


# ---- Fault Classification ----

class CallFault:
    """Classified fault from a model call."""
    __slots__ = ('fault_type', 'retryable', 'detail', 'exception_type', 'timestamp')

    def __init__(self, fault_type: str, retryable: bool, detail: str,
                 exception_type: str):
        self.fault_type = fault_type
        self.retryable = retryable
        self.detail = detail
        self.exception_type = exception_type
        self.timestamp = time.time()

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}


def classify_call_fault(exception: Exception) -> CallFault:
    """
    Classify a model call fault from the actual exception type.
    Returns a CallFault with retryability determined by observable evidence.
    """
    exc_type = type(exception).__name__

    # Network/connection errors are retryable
    if isinstance(exception, ConnectionRefusedError):
        return CallFault("connection_refused", True, str(exception), exc_type)
    if isinstance(exception, ConnectionError):
        return CallFault("network", True, str(exception), exc_type)
    if isinstance(exception, TimeoutError):
        return CallFault("timeout", True, str(exception), exc_type)
    if isinstance(exception, OSError) and "DNS" in str(exception).upper():
        return CallFault("dns_resolution", True, str(exception), exc_type)

    # Subprocess errors may be retryable (transient) or not (code bug)
    import subprocess
    if isinstance(exception, subprocess.TimeoutExpired):
        return CallFault("subprocess_timeout", True, str(exception), exc_type)
    if isinstance(exception, subprocess.CalledProcessError):
        # Non-zero exit from model CLI - may be transient
        return CallFault("subprocess_error", True,
                         f"rc={exception.returncode}", exc_type)

    # JSON/parsing errors are NOT retryable
    if isinstance(exception, (json.JSONDecodeError, ValueError, KeyError)):
        return CallFault("parse_error", False, str(exception), exc_type)

    # Infrastructure errors are NOT retryable
    from campaign2.proxy import InfrastructureError
    if isinstance(exception, InfrastructureError):
        return CallFault("infrastructure", False, str(exception), exc_type)

    # Unknown - default to not retryable
    return CallFault("unknown", False, str(exception), exc_type)


# ---- Attempt Ledger ----

class AttemptLedger:
    """
    Records all call intents and completions for audit trail.
    Every model call MUST have a matching intent() BEFORE the call
    and a completion() AFTER the call (success or failure).
    """

    def __init__(self, ledger_path: str):
        self.ledger_path = ledger_path
        self.entries: list = []

    def record_intent(self, call_id: str, call_type: str, arm: str,
                      phase: int, task_id: str = "", metadata: dict = None):
        """Record intent to make a model call BEFORE the call."""
        entry = {
            "event": "intent",
            "call_id": call_id,
            "call_type": call_type,
            "arm": arm,
            "phase": phase,
            "task_id": task_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if metadata:
            entry["metadata"] = metadata
        self.entries.append(entry)
        self._flush()

    def record_completion(self, call_id: str, success: bool,
                          tokens: int = 0, fault: Optional[CallFault] = None):
        """Record completion of a model call AFTER the call."""
        entry = {
            "event": "completion",
            "call_id": call_id,
            "success": success,
            "tokens": tokens,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        if fault:
            entry["fault"] = fault.to_dict()
        self.entries.append(entry)
        self._flush()

    def _flush(self):
        """Flush ledger to disk."""
        with open(self.ledger_path, "w", encoding="utf-8") as f:
            json.dump(self.entries, f, indent=2)
            f.flush()
            os.fsync(f.fileno())

    def verify_intent_before_completion(self) -> bool:
        """Verify that every completion has a matching prior intent."""
        seen_intents = set()
        for entry in self.entries:
            if entry["event"] == "intent":
                seen_intents.add(entry["call_id"])
            elif entry["event"] == "completion":
                if entry["call_id"] not in seen_intents:
                    return False
        return True


# ---- Scientific Start Marker ----

SCIENTIFIC_START_REL = "campaign2_scientific_start.json"


def publish_scientific_start(stage: str, workspace_root: str = BASE_DIR) -> str:
    """
    Publish SCIENTIFIC_START marker at runtime.
    This is a runtime artifact, NOT committed in source.
    Returns the path to the published marker.
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

    with open(marker_path, "w", encoding="utf-8") as f:
        json.dump(marker_data, f, indent=2)
        f.flush()
        os.fsync(f.fileno())

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
    - None if no marker exists (fresh start, caller should publish_scientific_start)
    - checkpoint data dict if marker exists AND valid checkpoint exists (resume)
    Raises:
    - AttemptInvalidatedError if marker exists but checkpoint is corrupt/missing
    """
    marker_path = os.path.join(workspace_root, SCIENTIFIC_START_REL)

    if not os.path.exists(marker_path):
        return None

    # Marker exists — check checkpoint
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

    # Validate checkpoint stage matches
    ckpt_stage = checkpoint_data.get("stage", "")
    if ckpt_stage != stage:
        raise AttemptInvalidatedError(
            f"Checkpoint stage '{ckpt_stage}' does not match requested stage '{stage}'. "
            "Attempt is invalidated."
        )

    return checkpoint_data
