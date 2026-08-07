"""
Campaign 2 Attempt Lifecycle & Audit Ledger Module.

Manages call fault classification, retryability assessment, append-only audit ledger,
and stage-global scientific start claim markers.
"""

import os
import json
import stat
import time
import hashlib
from typing import Dict, Any, List, Optional, Tuple

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LEDGER_SCHEMA_VERSION = "1"


class AttemptLifecycleError(RuntimeError):
    """Base exception for attempt lifecycle errors."""
    pass


class AttemptInvalidatedError(AttemptLifecycleError):
    """Raised when an attempt is invalidated due to pre-commit, transport, or one-shot claim failure."""
    pass


def publish_scientific_start(stage: str, attempt_id: str, workspace_root: str = BASE_DIR) -> str:
    """
    Publish stage-global scientific start claim marker file with descriptor fchmod, fsync, and parent dir fsync.
    Stored at history/campaign2_{stage}_scientific_start.json.
    Raises AttemptInvalidatedError if another attempt has already claimed scientific start for this stage.
    """
    history_dir = os.path.join(workspace_root, "history")
    if not os.path.exists(history_dir):
        os.makedirs(history_dir, exist_ok=True)

    stage_start_p = os.path.join(history_dir, f"campaign2_{stage}_scientific_start.json")

    if os.path.exists(stage_start_p):
        verify_stage_scientific_start(stage, attempt_id, workspace_root)
        try:
            dir_fd = os.open(history_dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception as exc:
            raise AttemptInvalidatedError(
                f"Failed to fsync parent history directory {history_dir} during pre-existing scientific start marker recovery: {exc}"
            ) from exc
        return stage_start_p

    data = {
        "stage": stage,
        "attempt_id": attempt_id,
        "timestamp": time.time(),
        "status": "started"
    }
    content_bytes = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")

    tmp_p = f"{stage_start_p}_{os.getpid()}.tmp"
    fd = os.open(tmp_p, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(fd, content_bytes)
        os.fchmod(fd, 0o444)
        os.fsync(fd)
    finally:
        os.close(fd)

    try:
        os.link(tmp_p, stage_start_p)
    except FileExistsError:
        pass
    except Exception as exc:
        raise AttemptInvalidatedError(f"Stage scientific start claim link failed: {exc}") from exc
    finally:
        if os.path.lexists(tmp_p):
            try:
                os.unlink(tmp_p)
            except OSError:
                pass

    try:
        dir_fd = os.open(history_dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception as exc:
        raise AttemptInvalidatedError(
            f"Failed to fsync parent history directory {history_dir} after scientific start marker publication: {exc}"
        ) from exc

    verify_stage_scientific_start(stage, attempt_id, workspace_root)
    return stage_start_p


def verify_stage_scientific_start(stage: str, attempt_id: str, workspace_root: str = BASE_DIR) -> bool:
    """Verify existing stage-global scientific start claim marker on resume."""
    stage_start_p = os.path.join(workspace_root, "history", f"campaign2_{stage}_scientific_start.json")
    if not os.path.exists(stage_start_p):
        raise AttemptInvalidatedError(f"Missing stage-global scientific start claim marker for stage '{stage}'")
    if os.path.islink(stage_start_p) or not os.path.isfile(stage_start_p):
        raise AttemptInvalidatedError(f"Scientific start marker must be a regular non-symlink file: '{stage_start_p}'")
    st_mode = stat.S_IMODE(os.stat(stage_start_p).st_mode)
    if st_mode != 0o444:
        raise AttemptInvalidatedError(f"Scientific start marker mode must be 0o444, got {oct(st_mode)}")
    try:
        with open(stage_start_p, "rb") as f:
            content_bytes = f.read()
        data = json.loads(content_bytes.decode("utf-8"))
    except Exception as exc:
        raise AttemptInvalidatedError(f"Corrupted scientific start marker: {exc}") from exc

    if data.get("stage") != stage:
        raise AttemptInvalidatedError(f"Scientific start marker stage mismatch: '{data.get('stage')}' vs '{stage}'")
    if data.get("status") != "started":
        raise AttemptInvalidatedError(f"Scientific start marker status invalid: '{data.get('status')}'")
    if data.get("attempt_id") != attempt_id:
        raise AttemptInvalidatedError(
            f"Stage scientific start claim attempt_id mismatch: marker has '{data.get('attempt_id')}', "
            f"resuming attempt has '{attempt_id}'"
        )

    expected_bytes = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if content_bytes != expected_bytes:
        raise AttemptInvalidatedError(f"Scientific start marker is not canonically formatted JSON bytes: '{stage_start_p}'")

    return True


def check_and_resume(stage: str, ckpt_path: str, workspace_root: str = BASE_DIR) -> Optional[dict]:
    """Check if stage-global scientific start file and checkpoint exist for resumption."""
    stage_start_p = os.path.join(workspace_root, "history", f"campaign2_{stage}_scientific_start.json")
    if os.path.exists(stage_start_p) and os.path.exists(ckpt_path):
        with open(ckpt_path, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


class CallFault:
    """Classifies a call failure for retryability and audit logging."""
    __slots__ = ('fault_type', 'retryable', 'detail', 'bytes_sent', 'bytes_received')

    def __init__(self, fault_type: str, retryable: bool, detail: str, bytes_sent: int = -1, bytes_received: int = -1):
        self.fault_type = fault_type
        self.retryable = retryable
        self.detail = detail
        self.bytes_sent = bytes_sent
        self.bytes_received = bytes_received

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}


def classify_call_fault(exc: Exception, bytes_sent: int = -1, bytes_received: int = -1) -> CallFault:
    """
    Classify a call fault for retryability using complete pre-request transport evidence.

    POLICY:
    1. Unknown evidence (bytes_received == -1): NON-RETRYABLE (fail-closed).
    2. HTTP 429 / 503 Rate Limit or Service Unavailable:
       - Retryable ONLY IF bytes_received == 0 (explicitly verified zero response-body bytes).
       - If bytes_received > 0 (body bytes received) or -1 (unknown), NON-RETRYABLE (fail-closed).
    3. Socket / DNS / TLS / Subprocess Spawn Errors:
       - Retryable ONLY IF bytes_sent == 0 AND bytes_received == 0.
       - If bytes_sent > 0 or unconfirmed (-1), NON-RETRYABLE (fail-closed).
    """
    msg = str(exc).lower()
    exc_type = type(exc).__name__.lower()

    # 1. HTTP 429 Rate Limit / HTTP 503 Service Unavailable
    if "429" in msg or "rate limit" in msg or "too many requests" in msg:
        if bytes_received == 0:
            return CallFault("rate_limit_pre_request", True, f"HTTP 429 Rate Limit (zero body): {exc}", bytes_sent, 0)
        return CallFault("rate_limit_with_body", False, f"HTTP 429 Rate Limit with response body ({bytes_received} bytes): {exc}", bytes_sent, bytes_received)

    if "503" in msg or "service unavailable" in msg:
        if bytes_received == 0:
            return CallFault("service_unavailable_pre_request", True, f"HTTP 503 Service Unavailable (zero body): {exc}", bytes_sent, 0)
        return CallFault("service_unavailable_with_body", False, f"HTTP 503 Service Unavailable with response body ({bytes_received} bytes): {exc}", bytes_sent, bytes_received)

    # 2. Pre-request Socket / Connection Refused / DNS / TLS / Subprocess Spawn
    if bytes_sent == 0 and bytes_received == 0:
        if "connection refused" in msg or "socket" in exc_type or "connect" in exc_type:
            return CallFault("connection_pre_request", True, f"Pre-request socket error: {exc}", 0, 0)
        if "dns" in msg or "gai_error" in msg or "name resolution" in msg or "ssl" in msg or "tls" in msg:
            return CallFault("dns_tls_pre_request", True, f"Pre-request DNS/TLS error: {exc}", 0, 0)
        if "oserror" in exc_type or "filenotfounderror" in exc_type or "permissionerror" in exc_type or "spawn" in msg:
            return CallFault("spawn_failure", True, f"Pre-exec subprocess spawn failure: {exc}", 0, 0)

    # Default fallback: non-retryable (fail-closed)
    return CallFault(
        "unconfirmed_transport_failure",
        False,
        f"Unconfirmed transport failure (bytes_sent={bytes_sent}, bytes_received={bytes_received}): {exc}",
        bytes_sent,
        bytes_received
    )


def publish_sealed_invalidation(run_dir: str, stage: str, attempt_id: str, reason: str) -> str:
    """
    Publish an independently sealed invalidation marker file when ledger is corrupted or invalid.
    Prevents mutating an unparseable ledger file while ensuring permanent trial invalidation.
    Uses the frozen durability sequence with atomic no-clobber hard link publication (os.link):
    write unique tmp_path, fchmod(0o444), fsync, os.link to destination, parent dir fsync.
    On FileExistsError, authenticates destination marker without replacing it.
    """
    path = os.path.join(run_dir, f"campaign2_{stage}_invalidated.json")

    if os.path.lexists(path):
        if not is_sealed_invalidated(run_dir, stage, attempt_id):
            raise AttemptLifecycleError(
                f"Sealed invalidation marker {path} already exists and is invalid/mismatched; cannot overwrite."
            )
        try:
            dir_fd = os.open(run_dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except Exception as exc:
            raise AttemptLifecycleError(
                f"Failed to fsync parent directory {run_dir} during pre-existing invalidation marker recovery: {exc}"
            ) from exc
        return path

    data = {
        "record_type": "terminal_classification",
        "stage": stage,
        "attempt_id": attempt_id,
        "classification": "invalidated",
        "reason": reason,
        "timestamp": time.time(),
    }
    content_bytes = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")

    tmp_path = os.path.join(run_dir, f".inval_{stage}_{attempt_id}_{os.getpid()}_{time.time_ns()}.tmp")
    if os.path.lexists(tmp_path):
        if not os.path.islink(tmp_path):
            os.chmod(tmp_path, 0o600)
        os.unlink(tmp_path)

    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(content_bytes)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise AttemptLifecycleError(f"Short write while sealing invalidation marker {path}")
            view = view[written:]
        os.fchmod(fd, 0o444)
        os.fsync(fd)
    finally:
        os.close(fd)

    try:
        os.link(tmp_path, path)
    except FileExistsError:
        if not is_sealed_invalidated(run_dir, stage, attempt_id):
            raise AttemptLifecycleError(
                f"Sealed invalidation marker {path} already exists and is invalid/mismatched; cannot overwrite."
            )
    finally:
        if os.path.lexists(tmp_path):
            try:
                os.unlink(tmp_path)
            except Exception:
                pass

    try:
        dir_fd = os.open(run_dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except Exception as exc:
        raise AttemptLifecycleError(f"Failed to fsync parent directory {run_dir} after marker publication: {exc}") from exc

    return path


def is_sealed_invalidated(run_dir: str, stage: str, attempt_id: str) -> bool:
    """
    Check and strictly authenticate an independently sealed invalidation marker in run_dir.
    attempt_id is MANDATORY.
    Outcomes:
    - absent (file does not exist) -> return False
    - present and 100% valid for stage, attempt_id, permissions, and canonical formatting -> return True
    - present but malformed, noncanonical, mismatched, or incorrectly sealed -> raise AttemptLifecycleError
    """
    if not attempt_id or not isinstance(attempt_id, str):
        raise ValueError("is_sealed_invalidated requires non-empty attempt_id string")

    path = os.path.join(run_dir, f"campaign2_{stage}_invalidated.json")
    if not os.path.lexists(path):
        return False

    if os.path.islink(path):
        raise AttemptLifecycleError(f"Sealed invalidation marker path exists as a symlink: {path}")

    real_run_dir = os.path.realpath(run_dir)
    real_path = os.path.realpath(path)
    if os.path.dirname(real_path) != real_run_dir:
        raise AttemptLifecycleError(f"Sealed invalidation marker path traverses outside run_dir: {path}")

    st = os.stat(path)
    if not stat.S_ISREG(st.st_mode):
        raise AttemptLifecycleError(f"Sealed invalidation marker is not a regular file: {path}")

    mode = stat.S_IMODE(st.st_mode)
    if mode != 0o444:
        raise AttemptLifecycleError(f"Sealed invalidation marker permission mode must be 0o444, got {oct(mode)} for {path}")

    try:
        with open(path, "rb") as f:
            raw_bytes = f.read()
        data = json.loads(raw_bytes.decode("utf-8"))
    except Exception as exc:
        raise AttemptLifecycleError(f"Sealed invalidation marker {path} contains invalid JSON: {exc}") from exc

    if not isinstance(data, dict):
        raise AttemptLifecycleError(f"Sealed invalidation marker {path} JSON must be a dictionary")

    if data.get("record_type") != "terminal_classification":
        raise AttemptLifecycleError(f"Sealed invalidation marker {path} record_type mismatch: got {data.get('record_type')}")
    if data.get("stage") != stage:
        raise AttemptLifecycleError(f"Sealed invalidation marker {path} stage mismatch: expected {stage}, got {data.get('stage')}")
    if data.get("classification") != "invalidated":
        raise AttemptLifecycleError(f"Sealed invalidation marker {path} classification mismatch: got {data.get('classification')}")
    if data.get("attempt_id") != attempt_id:
        raise AttemptLifecycleError(f"Sealed invalidation marker {path} attempt_id mismatch: expected {attempt_id}, got {data.get('attempt_id')}")

    expected_bytes = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if raw_bytes != expected_bytes:
        raise AttemptLifecycleError(f"Sealed invalidation marker {path} contains noncanonical JSON formatting")

    return True


class AttemptLedger:
    """Append-only audit ledger tracking attempt lifecycle, call intents, and completions."""

    def __init__(self, ledger_path: str):
        self.ledger_path = ledger_path
        self._max_call_counter = 0
        self.cumulative_calls = 0
        self.cumulative_tokens = 0
        self.unknown_usage_count = 0
        self._load_ledger()

    def _load_ledger(self):
        if not os.path.exists(self.ledger_path):
            return
        with open(self.ledger_path, "r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    entry = json.loads(line)
                    if not isinstance(entry, dict):
                        raise ValueError("Ledger line JSON is not an object/dict")
                except Exception as e:
                    raise AttemptLifecycleError(f"Ledger file is corrupted (invalid JSON line): {self.ledger_path}") from e

                rec_type = entry.get("record_type")
                if rec_type == "completion":
                    self.cumulative_calls += 1
                    toks = entry.get("tokens", 0)
                    if toks > 0:
                        self.cumulative_tokens += toks
                    if "unknown_usage_count" in entry:
                        self.unknown_usage_count = entry["unknown_usage_count"]
                    elif toks == 0:
                        fault_dict = entry.get("fault") or {}
                        bs = fault_dict.get("bytes_sent", -1)
                        br = fault_dict.get("bytes_received", -1)
                        is_pre_req = (bs == 0 and br == 0)
                        if not is_pre_req:
                            self.unknown_usage_count += 1
                elif rec_type in ("terminal_classification", "attempt_start"):
                    if "unknown_usage_count" in entry:
                        self.unknown_usage_count = entry["unknown_usage_count"]
                call_id = entry.get("call_id", "")
                if "_" in call_id:
                    parts = call_id.split("_")
                    if parts[-1].isdigit():
                        num = int(parts[-1])
                        if num > self._max_call_counter:
                            self._max_call_counter = num

    def record_attempt_start(self, attempt_id: str, stage: str, metadata: Optional[dict] = None):
        entry = {
            "record_type": "attempt_start",
            "attempt_id": attempt_id,
            "stage": stage,
            "timestamp": time.time(),
            "metadata": metadata or {},
        }
        self._append(entry)

    def record_terminal_classification(self, attempt_id: str, classification: str, reason: str, metadata: Optional[dict] = None):
        entry = {
            "record_type": "terminal_classification",
            "attempt_id": attempt_id,
            "classification": classification,
            "reason": reason,
            "cumulative_calls": self.cumulative_calls,
            "cumulative_tokens": self.cumulative_tokens,
            "unknown_usage_count": self.unknown_usage_count,
            "timestamp": time.time(),
            "metadata": metadata or {},
        }
        self._append(entry)

    def record_intent(self, call_id: str, call_type: str, arm: str, phase: int, task_id: str = "", metadata: Optional[dict] = None):
        entry = {
            "record_type": "intent",
            "call_id": call_id,
            "call_type": call_type,
            "arm": arm,
            "phase": phase,
            "task_id": task_id,
            "timestamp": time.time(),
            "metadata": metadata or {},
        }
        self._append(entry)

    def record_completion(self, call_id: str, success: bool, tokens: int = 0, fault: Optional[CallFault] = None, metadata: Optional[dict] = None):
        self.cumulative_calls += 1
        is_pre_request = fault and fault.bytes_sent == 0 and fault.bytes_received == 0
        if tokens > 0:
            self.cumulative_tokens += tokens
        elif not is_pre_request:
            self.unknown_usage_count += 1

        entry = {
            "record_type": "completion",
            "call_id": call_id,
            "success": success,
            "tokens": tokens,
            "fault": fault.to_dict() if fault else None,
            "cumulative_calls": self.cumulative_calls,
            "cumulative_tokens": self.cumulative_tokens,
            "unknown_usage_count": self.unknown_usage_count,
            "timestamp": time.time(),
            "metadata": metadata or {},
        }
        self._append(entry)

    def _append(self, entry: dict):
        line = json.dumps(entry) + "\n"
        first_write = not os.path.exists(self.ledger_path)
        with open(self.ledger_path, "a", encoding="utf-8") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        if first_write:
            dir_fd = os.open(os.path.dirname(self.ledger_path), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

    def _append_record(self, entry: dict):
        self._append(entry)

    def get_entries(self) -> List[dict]:
        if not os.path.exists(self.ledger_path):
            return []
        entries = []
        with open(self.ledger_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    entries.append(json.loads(line))
        return entries

    def verify_intent_before_completion(self) -> bool:
        intents = set()
        for entry in self.get_entries():
            if entry.get("record_type") == "intent":
                intents.add(entry["call_id"])
            elif entry.get("record_type") == "completion":
                if entry["call_id"] not in intents:
                    raise AttemptLifecycleError(f"Completion recorded without intent for call_id: {entry['call_id']}")
        return True

    def verify_all_intents_completed(self) -> bool:
        intents = {}
        completions = {}
        for entry in self.get_entries():
            cid = entry.get("call_id")
            if entry.get("record_type") == "intent":
                intents[cid] = intents.get(cid, 0) + 1
            elif entry.get("record_type") == "completion":
                completions[cid] = completions.get(cid, 0) + 1

        all_keys = set(intents.keys()) | set(completions.keys())
        for cid in all_keys:
            i_count = intents.get(cid, 0)
            c_count = completions.get(cid, 0)
            if c_count != i_count:
                raise AttemptLifecycleError(
                    f"Intent/completion multiplicity mismatch for call_id '{cid}': "
                    f"intents={i_count}, completions={c_count}"
                )
        return True

    def verify_three_record_protocol(self) -> bool:
        entries = self.get_entries()
        intents = {}
        started = {}
        completes = {}
        call_ordinals = {}

        for idx, entry in enumerate(entries):
            r_type = entry.get("record_type")
            cid = entry.get("logical_call_id")
            ord_num = entry.get("invocation_ordinal")

            if r_type in ("invocation_intent", "process_started", "invocation_completion"):
                if not cid or not isinstance(ord_num, int) or ord_num <= 0:
                    raise AttemptLifecycleError(f"Invalid call_id or ordinal in record {r_type}: cid={cid}, ord={ord_num}")
                key = (cid, ord_num)

            if r_type == "invocation_intent":
                if key in intents:
                    raise AttemptLifecycleError(f"Duplicate invocation_intent for key {key}")

                expected_ord = len(call_ordinals.get(cid, [])) + 1
                if ord_num != expected_ord:
                    raise AttemptLifecycleError(f"Non-contiguous or non-monotonic invocation_ordinal for call_id '{cid}': expected {expected_ord}, got {ord_num}")
                call_ordinals.setdefault(cid, []).append(ord_num)

                rec_bytes = (json.dumps(entry, sort_keys=True) + "\n").encode("utf-8")
                rec_hash = hashlib.sha256(rec_bytes).hexdigest()
                intents[key] = (entry, rec_hash, idx)

            elif r_type == "process_started":
                if key in started:
                    raise AttemptLifecycleError(f"Duplicate process_started for key {key}")
                if key in completes:
                    raise AttemptLifecycleError(f"process_started recorded after invocation_completion for key {key}")
                if key not in intents:
                    raise AttemptLifecycleError(f"process_started recorded before invocation_intent for key {key}")
                intent_rec, intent_hash, intent_idx = intents[key]
                if idx < intent_idx:
                    raise AttemptLifecycleError(f"process_started index {idx} precedes invocation_intent index {intent_idx} for key {key}")
                if entry.get("intent_record_hash") != intent_hash:
                    raise AttemptLifecycleError(f"process_started intent_record_hash mismatch for key {key}")
                rec_bytes = (json.dumps(entry, sort_keys=True) + "\n").encode("utf-8")
                rec_hash = hashlib.sha256(rec_bytes).hexdigest()
                started[key] = (entry, rec_hash, idx)

            elif r_type == "invocation_completion":
                if key in completes:
                    raise AttemptLifecycleError(f"Duplicate invocation_completion for key {key}")
                if key not in intents:
                    raise AttemptLifecycleError(f"invocation_completion recorded before invocation_intent for key {key}")
                intent_rec, intent_hash, intent_idx = intents[key]
                if idx < intent_idx:
                    raise AttemptLifecycleError(f"invocation_completion index {idx} precedes invocation_intent index {intent_idx} for key {key}")
                if entry.get("intent_record_hash") != intent_hash:
                    raise AttemptLifecycleError(f"invocation_completion intent_record_hash mismatch for key {key}")

                if key in started:
                    st_rec, st_hash, st_idx = started[key]
                    if idx < st_idx:
                        raise AttemptLifecycleError(f"invocation_completion index {idx} precedes process_started index {st_idx} for key {key}")
                    if entry.get("started_record_hash") != st_hash:
                        raise AttemptLifecycleError(f"invocation_completion started_record_hash mismatch for key {key}")
                elif entry.get("parse_state") != "spawn_failure":
                    raise AttemptLifecycleError(f"invocation_completion without process_started must be spawn_failure for key {key}")

                completes[key] = (entry, None, idx)

        for key in intents:
            if key not in completes:
                raise AttemptLifecycleError(f"Uncompleted invocation_intent for key {key}")

        return True

    def verify_resume_admission(self) -> bool:
        for entry in self.get_entries():
            if entry.get("record_type") == "terminal_classification":
                raise AttemptLifecycleError(
                    f"Attempt is already terminal (classified as '{entry.get('classification')}' - "
                    f"'{entry.get('reason')}') and cannot be resumed"
                )

        # Credential residue scan across scratch directories (P1-4)
        import glob
        for scratch in glob.glob("/tmp/rsi_codex_scratch_*"):
            auth_p = os.path.join(scratch, ".codex", "auth.json")
            if os.path.exists(auth_p):
                raise AttemptLifecycleError(f"Credential residue found during resume admission scan: {auth_p}")

        self.verify_all_intents_completed()
        return self.verify_three_record_protocol()
