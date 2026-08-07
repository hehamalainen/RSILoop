"""
Campaign 2 Stage-Global Preflight Registry & Canary Lifecycle Module (C1-4, C1-5).

Manages atomic, durable stage-global preflight registry (campaign2_pilot_preflight_registry.json)
binding registration hash, launch lock hash, canary ordinals, attempt IDs, cumulative tokens,
and cumulative active time.
Enforces canary execution before SCIENTIFIC_START, canary retry containment,
and stage-global limits (175 total logical calls max, 525 local attempts max, 600k tokens, 8h active hours).
"""

import os
import json
import stat
import time
import hashlib
from typing import Dict, Any, List, Optional

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_PILOT_TOKENS = 600000
MAX_PILOT_ACTIVE_SECONDS = 28800.0  # 8 hours
MAX_LOGICAL_CALLS = 175
MAX_LOCAL_INVOCATION_ATTEMPTS = 525


CANARY_NEUTRAL_PROMPT = "Return the exact string: CANARY_OK"
CANARY_EXPECTED_RESPONSE = "CANARY_OK"


class PreflightRegistryError(RuntimeError):
    """Base exception for preflight registry failures."""
    pass


class CanaryFailureError(PreflightRegistryError):
    """Raised when pilot canary fails or violates containment rules."""
    pass


class StageLimitsExceededError(PreflightRegistryError):
    """Raised when stage-global tokens, active time, or call counts exceed limits."""
    pass


class PreflightRegistry:
    """Stage-global preflight registry for binding preflight hashes, canary state, and cumulative metrics."""

    def __init__(self, stage: str = "pilot", workspace_root: str = BASE_DIR):
        self.stage = stage
        self.workspace_root = workspace_root
        self.history_dir = os.path.join(workspace_root, "history")
        if not os.path.exists(self.history_dir):
            os.makedirs(self.history_dir, exist_ok=True)
        self.registry_path = os.path.join(self.history_dir, f"campaign2_{stage}_preflight_registry.json")
        self.data = self._load_or_init()

    def _load_or_init(self) -> Dict[str, Any]:
        reg_file_p = os.path.join(self.workspace_root, "campaign2_master_registration.json")
        reg_hash = ""
        if os.path.exists(reg_file_p):
            with open(reg_file_p, "rb") as f:
                reg_hash = hashlib.sha256(f.read()).hexdigest()

        lock_file_p = os.path.join(self.workspace_root, "campaign2_launch_lock.json")
        lock_hash = ""
        if os.path.exists(lock_file_p):
            with open(lock_file_p, "rb") as f:
                lock_hash = hashlib.sha256(f.read()).hexdigest()

        if os.path.exists(self.registry_path):
            if os.path.islink(self.registry_path) or not os.path.isfile(self.registry_path):
                raise PreflightRegistryError(f"Preflight registry must be a regular non-symlink file: {self.registry_path}")
            st_mode = stat.S_IMODE(os.stat(self.registry_path).st_mode)
            if st_mode != 0o444:
                raise PreflightRegistryError(f"Preflight registry mode must be 0o444, got {oct(st_mode)}")
            try:
                with open(self.registry_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as exc:
                raise PreflightRegistryError(f"Corrupted preflight registry: {exc}") from exc

            stored_reg_hash = data.get("registration_hash", "")
            stored_lock_hash = data.get("launch_lock_hash", "")

            # If disk hash is present, registry MUST match disk hash
            if reg_hash and stored_reg_hash != reg_hash:
                raise PreflightRegistryError(
                    f"Preflight registry registration_hash mismatch: registry has '{stored_reg_hash}', disk has '{reg_hash}'"
                )
            if lock_hash and stored_lock_hash != lock_hash:
                raise PreflightRegistryError(
                    f"Preflight registry launch_lock_hash mismatch: registry has '{stored_lock_hash}', disk has '{lock_hash}'"
                )

            # If registry stored a non-empty hash, disk file MUST still exist and match
            if stored_reg_hash and not reg_hash:
                raise PreflightRegistryError("Preflight registry bound registration_hash, but master registration file is missing on disk")
            if stored_lock_hash and not lock_hash:
                raise PreflightRegistryError("Preflight registry bound launch_lock_hash, but launch lock file is missing on disk")
            return data

        initial_data = {
            "record_type": "preflight_registry",
            "stage": self.stage,
            "registration_hash": reg_hash,
            "launch_lock_hash": lock_hash,
            "canary_passed": False,
            "accepted_attempt_id": "",
            "canary_ordinals": [],
            "attempt_ids": [],
            "cumulative_tokens": 0,
            "cumulative_active_time": 0.0,
            "total_logical_calls": 0,
            "total_local_invocation_attempts": 0,
            "seen_logical_call_ids": [],
            "backend_request_count": "unknown",
        }
        self._save_data(initial_data)
        return initial_data

    def _save_data(self, data: Dict[str, Any]) -> None:
        content_bytes = (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")
        tmp_p = f"{self.registry_path}_{os.getpid()}.tmp"

        if os.path.exists(self.registry_path):
            os.chmod(self.registry_path, 0o644)

        with open(tmp_p, "wb") as f:
            f.write(content_bytes)
            f.flush()
            os.fsync(f.fileno())

        os.chmod(tmp_p, 0o444)
        os.replace(tmp_p, self.registry_path)

        dir_fd = os.open(self.history_dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

        self.data = data

    def _reload(self) -> None:
        """Reload latest registry state from disk with strict authentication (P0-4)."""
        if not os.path.exists(self.registry_path):
            if hasattr(self, "data") and self.data:
                # File existed when object was initialized, but was deleted/removed on disk!
                raise PreflightRegistryError(f"Preflight registry file missing on disk: {self.registry_path}")
            return

        if os.path.islink(self.registry_path):
            raise PreflightRegistryError(f"Registry file must not be a symlink: {self.registry_path}")
        if not os.path.isfile(self.registry_path):
            raise PreflightRegistryError(f"Registry file must be a regular file: {self.registry_path}")

        # Enforce permission mode 0o444
        st_mode = stat.S_IMODE(os.stat(self.registry_path).st_mode)
        if st_mode != 0o444:
            raise PreflightRegistryError(
                f"Preflight registry permission mode must be 0o444, got {oct(st_mode)} for {self.registry_path}"
            )

        try:
            with open(self.registry_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as e:
            raise PreflightRegistryError(f"Failed to read/parse preflight registry file {self.registry_path}: {e}")

        for req_field in ("record_type", "stage", "registration_hash", "launch_lock_hash"):
            if req_field not in data:
                raise PreflightRegistryError(f"Corrupted preflight registry missing field '{req_field}': {self.registry_path}")

        if data.get("record_type") != "preflight_registry":
            raise PreflightRegistryError(f"Invalid preflight registry record_type: '{data.get('record_type')}'")

        if data.get("stage") != self.stage:
            raise PreflightRegistryError(f"Preflight registry stage mismatch: expected '{self.stage}', got '{data.get('stage')}'")

        # Verify registration and launch lock hashes match workspace files (P0-4, bidirectional)
        reg_file = os.path.join(self.workspace_root, "campaign2_master_registration.json")
        reg_exists = os.path.exists(reg_file)
        stored_reg_hash = data.get("registration_hash", "")

        if reg_exists or stored_reg_hash:
            if not reg_exists or os.path.islink(reg_file) or not os.path.isfile(reg_file):
                raise PreflightRegistryError(
                    f"Bound master registration file missing or invalid: {reg_file}"
                )
            if not stored_reg_hash:
                raise PreflightRegistryError(
                    f"Preflight registry missing stored registration_hash for existing bound file: {reg_file}"
                )
            with open(reg_file, "rb") as f:
                curr_reg_hash = hashlib.sha256(f.read()).hexdigest()
            if stored_reg_hash != curr_reg_hash:
                raise PreflightRegistryError(
                    f"Preflight registry registration_hash mismatch: stored '{stored_reg_hash}' vs current '{curr_reg_hash}'"
                )

        lock_file = os.path.join(self.workspace_root, "campaign2_launch_lock.json")
        lock_exists = os.path.exists(lock_file)
        stored_lock_hash = data.get("launch_lock_hash", "")

        if lock_exists or stored_lock_hash:
            if not lock_exists or os.path.islink(lock_file) or not os.path.isfile(lock_file):
                raise PreflightRegistryError(
                    f"Bound launch lock file missing or invalid: {lock_file}"
                )
            if not stored_lock_hash:
                raise PreflightRegistryError(
                    f"Preflight registry missing stored launch_lock_hash for existing bound file: {lock_file}"
                )
            with open(lock_file, "rb") as f:
                curr_lock_hash = hashlib.sha256(f.read()).hexdigest()
            if stored_lock_hash != curr_lock_hash:
                raise PreflightRegistryError(
                    f"Preflight registry launch_lock_hash mismatch: stored '{stored_lock_hash}' vs current '{curr_lock_hash}'"
                )

        self.data = data

    def set_stage_blocked(self, attempt_id: str, reason: str) -> None:
        """Durably block stage from future preflight canary execution (P0-3)."""
        self._reload()
        self.data["stage_blocked"] = True
        self.data["block_reason"] = reason
        self.data["blocked_attempt_id"] = attempt_id
        self._save_data(self.data)

    def bind_attempt(self, attempt_id: str) -> None:
        """Bind attempt ID to preflight registry."""
        self._reload()
        attempts = self.data.get("attempt_ids", [])
        if attempt_id not in attempts:
            attempts.append(attempt_id)
            self.data["attempt_ids"] = attempts
            self._save_data(self.data)

    def record_canary_attempt(self, attempt_id: str, status: str, tokens: int, active_time: float, usage_known: bool, call_id: str = "") -> None:
        """Record canary attempt outcome durably and idempotently by call_id (P0-3, P1-1)."""
        self._reload()
        attempts_log = self.data.get("canary_attempts", [])

        # Idempotently update or append attempt entry by call_id
        existing = False
        if call_id:
            for item in attempts_log:
                if item.get("call_id") == call_id:
                    item["status"] = status
                    item["tokens"] = tokens
                    item["active_time"] = active_time
                    item["usage_known"] = usage_known
                    item["timestamp"] = time.time()
                    existing = True
                    break

        if not existing:
            attempts_log.append({
                "attempt_id": attempt_id,
                "call_id": call_id,
                "status": status,
                "tokens": tokens,
                "active_time": active_time,
                "usage_known": usage_known,
                "timestamp": time.time(),
            })

        self.data["canary_attempts"] = attempts_log
        if not usage_known or status in ("invalidated", "unknown_usage", "failed_execution"):
            self.data["stage_blocked"] = True
            self.data["block_reason"] = f"Canary attempt '{call_id}' failed with status '{status}' (usage_known={usage_known})"

        self._save_data(self.data)

    def record_canary_passed(self, attempt_id: str, canary_log: List[dict], tokens_used: int, active_time: float) -> None:
        """Record canary pass and bind attempt ID."""
        self._reload()
        self.data["canary_passed"] = True
        self.data["accepted_attempt_id"] = attempt_id
        self.data["canary_ordinals"] = canary_log
        attempts = self.data.get("attempt_ids", [])
        if attempt_id not in attempts:
            attempts.append(attempt_id)
            self.data["attempt_ids"] = attempts
        self._save_data(self.data)

    def record_usage(
        self,
        tokens_used: int,
        active_time: float,
        logical_calls: int = 1,
        local_attempts: int = 1,
        logical_call_id: str = "",
        invocation_ordinal: int = 1,
        completion_hash: str = "",
    ) -> None:
        """Accumulate active time, tokens, and call counts across trials and retries while enforcing stage limits (idempotent per completion hash, P0-1)."""
        self._reload()
        seen = set(self.data.get("seen_logical_call_ids", []))
        seen_keys = set(self.data.get("seen_invocation_keys", []))

        key = f"{logical_call_id}_{invocation_ordinal}_{completion_hash}" if (logical_call_id and completion_hash) else ""

        if key and key in seen_keys:
            # Already applied this completion's usage idempotently
            return

        inc_calls = 0
        if logical_call_id:
            if logical_call_id not in seen:
                seen.add(logical_call_id)
                inc_calls = 1
        else:
            inc_calls = logical_calls

        if key:
            seen_keys.add(key)

        self.data["seen_logical_call_ids"] = sorted(list(seen))
        self.data["seen_invocation_keys"] = sorted(list(seen_keys))

        new_tokens = self.data.get("cumulative_tokens", 0) + tokens_used
        new_active = self.data.get("cumulative_active_time", 0.0) + active_time
        new_calls = self.data.get("total_logical_calls", 0) + inc_calls
        new_attempts = self.data.get("total_local_invocation_attempts", 0) + local_attempts

        self.data["cumulative_tokens"] = new_tokens
        self.data["cumulative_active_time"] = new_active
        self.data["total_logical_calls"] = new_calls
        self.data["total_local_invocation_attempts"] = new_attempts

        # Durably record overshooting usage BEFORE checking limit thresholds
        self._save_data(self.data)

        # Authenticate master registration limits (P0-3)
        max_tokens = MAX_PILOT_TOKENS
        max_active = MAX_PILOT_ACTIVE_SECONDS
        max_calls = MAX_LOGICAL_CALLS
        max_attempts = MAX_LOCAL_INVOCATION_ATTEMPTS

        reg_file_p = os.path.join(self.workspace_root, "campaign2_master_registration.json")
        if os.path.exists(reg_file_p):
            from campaign2.registration import verify_master_registration
            reg_data = verify_master_registration(self.workspace_root)
            exec_lims = reg_data.get("execution_limits", {})
            call_arith = reg_data.get("call_arithmetic", {})

            if "max_output_tokens" in exec_lims:
                max_tokens = exec_lims["max_output_tokens"]
            if "max_active_seconds" in exec_lims:
                max_active = float(exec_lims["max_active_seconds"])
            if "pilot_max_logical_calls" in call_arith:
                max_calls = call_arith["pilot_max_logical_calls"]
            if "max_local_invocation_attempts" in call_arith:
                max_attempts = call_arith["max_local_invocation_attempts"]

        if new_tokens > max_tokens:
            raise StageLimitsExceededError(
                f"Pilot token limit exceeded: {new_tokens} > {max_tokens}"
            )
        if new_active > max_active:
            raise StageLimitsExceededError(
                f"Pilot active time limit exceeded: {new_active:.2f} s > {max_active} s"
            )
        if new_calls > max_calls:
            raise StageLimitsExceededError(
                f"Pilot total logical calls limit exceeded: {new_calls} > {max_calls}"
            )
        if new_attempts > max_attempts:
            raise StageLimitsExceededError(
                f"Pilot total local invocation attempts limit exceeded: {new_attempts} > {max_attempts}"
            )


def run_pilot_canary(
    stage: str,
    attempt_id: str,
    model_provider: Any,
    workspace_root: str = BASE_DIR,
    ledger: Optional[Any] = None,
) -> Dict[str, Any]:
    """
    Executes or reconciles preflight pilot canary call (0o444 sealed, fsynced).
    """
    from campaign2.attempt_lifecycle import publish_scientific_start, AttemptLedger

    registry = PreflightRegistry(stage=stage, workspace_root=workspace_root)

    # Check if stage is permanently blocked due to prior ambiguous/unknown usage (P0-3)
    attempts_log = registry.data.get("canary_attempts", [])
    if registry.data.get("stage_blocked", False) or any(not a.get("usage_known", True) or a.get("status") in ("invalidated", "unknown_usage", "failed_execution") for a in attempts_log):
        reason = registry.data.get("block_reason", "prior unknown-usage or boundary failure")
        raise CanaryFailureError(f"Stage '{stage}' is permanently BLOCKED due to unrecoverable preflight failure: {reason}")

    # If canary already passed for this accepted attempt, reconcile SCIENTIFIC_START marker if missing
    if registry.data.get("canary_passed", False):
        accepted_id = registry.data.get("accepted_attempt_id", "")
        if accepted_id and accepted_id != attempt_id:
            raise CanaryFailureError(
                f"Preflight registry bound accepted attempt '{accepted_id}', cannot run with '{attempt_id}'"
            )
        start_marker_p = os.path.join(registry.history_dir, f"campaign2_{stage}_scientific_start.json")
        if not os.path.exists(start_marker_p):
            publish_scientific_start(stage=stage, attempt_id=attempt_id, workspace_root=workspace_root)
        return registry.data

    # Check AttemptLedger to reconcile completed canary call if provider completion succeeded before registry update
    candidate_ledger_paths = []
    if ledger and hasattr(ledger, "ledger_path") and ledger.ledger_path:
        candidate_ledger_paths = [ledger.ledger_path]
    else:
        candidate_ledger_paths = [
            os.path.join(workspace_root, "history", attempt_id, f"campaign2_{stage}_ledger.jsonl"),
            os.path.join(workspace_root, "history", attempt_id, "ledger.jsonl"),
            os.path.join(workspace_root, attempt_id, f"campaign2_{stage}_ledger.jsonl"),
            os.path.join(workspace_root, attempt_id, "ledger.jsonl"),
            os.path.join(registry.history_dir, f"campaign2_{attempt_id}_attempt_ledger.jsonl"),
            os.path.join(workspace_root, "ledger.jsonl"),
        ]

    expected_canary_prompt_hash = hashlib.sha256(CANARY_NEUTRAL_PROMPT.encode("utf-8")).hexdigest()
    expected_canary_text_hash = hashlib.sha256(CANARY_EXPECTED_RESPONSE.encode("utf-8")).hexdigest()

    for ledger_p in candidate_ledger_paths:
        if os.path.exists(ledger_p) and os.path.isfile(ledger_p) and os.path.getsize(ledger_p) > 0:
            ledger_obj = AttemptLedger(ledger_p)
            ledger_obj.verify_three_record_protocol()
            entries = ledger_obj.get_entries()

            intents = [
                e for e in entries
                if e.get("record_type") == "invocation_intent"
                and str(e.get("logical_call_id", "")).startswith(f"canary_{stage}_{attempt_id}")
            ]

            # Group intents by logical_call_id (P0-2)
            intents_by_call = {}
            for e in intents:
                cid = str(e.get("logical_call_id", ""))
                intents_by_call.setdefault(cid, []).append(e)

            for cid in sorted(intents_by_call.keys()):
                call_intents = sorted(intents_by_call[cid], key=lambda x: x.get("invocation_ordinal", 1))

                # 1. Account EVERY local invocation attempt idempotently in registry (P0-1, P0-2)
                call_completes = []
                for intent_rec in call_intents:
                    inv_ordinal = intent_rec.get("invocation_ordinal", 1)
                    intent_bytes = (json.dumps(intent_rec, sort_keys=True) + "\n").encode("utf-8")
                    intent_hash = hashlib.sha256(intent_bytes).hexdigest()

                    if intent_rec.get("prompt_hash") != expected_canary_prompt_hash:
                        raise CanaryFailureError(f"Ledger canary intent '{cid}' prompt_hash mismatch during reconciliation")

                    matching = [
                        e for e in entries
                        if e.get("record_type") == "invocation_completion"
                        and e.get("logical_call_id") == cid
                        and e.get("intent_record_hash") == intent_hash
                    ]
                    if matching:
                        comp = matching[-1]
                        call_completes.append((intent_rec, comp))
                        comp_bytes = (json.dumps(comp, sort_keys=True) + "\n").encode("utf-8")
                        comp_hash = hashlib.sha256(comp_bytes).hexdigest()
                        tokens = comp.get("tokens", 0)
                        duration = comp.get("duration", 0.0)
                        registry.record_usage(tokens, duration, 1, 1, cid, inv_ordinal, comp_hash)

                if call_completes:
                    # Authenticate preceding completions against shared retry predicate before considering terminal result (P0-1)
                    from campaign2.attempt_lifecycle import is_permitted_local_retry
                    for prec_intent, prec_comp in call_completes[:-1]:
                        ord_num = prec_intent.get("invocation_ordinal", 1)
                        has_started = any(
                            e.get("record_type") == "process_started"
                            and e.get("logical_call_id") == cid
                            and e.get("invocation_ordinal") == ord_num
                            for e in entries
                        )
                        if not is_permitted_local_retry(prec_comp, has_started):
                            reason = (
                                f"Preflight canary call '{cid}' contains unauthorized intermediate retry attempt "
                                f"(ordinal {ord_num}, classification='{prec_comp.get('classification')}', parse_state='{prec_comp.get('parse_state')}', usage_known={prec_comp.get('usage_known')})"
                            )
                            registry.record_canary_attempt(
                                attempt_id, "invalidated", prec_comp.get("tokens", 0), prec_comp.get("duration", 0.0), False, call_id=cid
                            )
                            registry.set_stage_blocked(attempt_id, reason)
                            raise CanaryFailureError(f"{reason}. Pilot BLOCKED.")

                    # 2. Evaluate outcome based on TERMINAL (final) completion of this logical call sequence (P0-2)
                    last_intent, last_comp = call_completes[-1]
                    last_ordinal = last_intent.get("invocation_ordinal", 1)
                    last_tokens = last_comp.get("tokens", 0)
                    last_duration = last_comp.get("duration", 0.0)

                    is_valid_pass = (
                        last_comp.get("classification") == "valid"
                        and last_comp.get("return_code") == 0
                        and last_comp.get("text_hash") == expected_canary_text_hash
                        and type(last_tokens) is not bool and isinstance(last_tokens, int) and last_tokens >= 0
                        and type(last_duration) is not bool and isinstance(last_duration, (int, float)) and last_duration >= 0.0
                    )

                    is_clean_content_failure = (
                        last_comp.get("classification") == "valid"
                        and last_comp.get("return_code") == 0
                    )

                    if is_valid_pass:
                        canary_record = {
                            "call_id": cid,
                            "status": "passed",
                            "tokens": last_tokens,
                            "duration": last_duration,
                            "attempts": last_ordinal,
                            "usage_known": True,
                        }
                        registry.record_canary_attempt(attempt_id, "passed", last_tokens, last_duration, True, call_id=cid)
                        registry.record_canary_passed(attempt_id, [canary_record], last_tokens, last_duration)
                        publish_scientific_start(stage=stage, attempt_id=attempt_id, workspace_root=workspace_root)
                        return registry.data
                    elif is_clean_content_failure:
                        registry.record_canary_attempt(attempt_id, "failed_content", last_tokens, last_duration, True, call_id=cid)
                    else:
                        # Ambiguous / unknown usage / boundary failure on terminal completion (P0-2, P0-3)
                        reason = f"Preflight canary completion '{cid}' failed with non-retryable ambiguous classification '{last_comp.get('classification')}' (return_code={last_comp.get('return_code')})"
                        registry.record_canary_attempt(attempt_id, "invalidated", last_tokens, last_duration, False, call_id=cid)
                        registry.set_stage_blocked(attempt_id, reason)
                        raise CanaryFailureError(f"{reason}. Pilot BLOCKED.")

    # Reload latest registry data after reconciliation phase
    registry._reload()

    # Check max canary attempts cap (3 attempts max)
    attempts_log = registry.data.get("canary_attempts", [])
    if len(attempts_log) >= 3:
        raise CanaryFailureError(
            f"Preflight canary has exhausted maximum allowed 3 attempts (count={len(attempts_log)}). Pilot BLOCKED."
        )

    canary_call_id = f"canary_{stage}_{attempt_id}_{len(attempts_log) + 1:02d}"

    t0 = time.monotonic()
    canary_log = []
    try:
        if hasattr(model_provider, "generate_candidate"):
            code, tokens = model_provider.generate_candidate(
                task_id="canary",
                effective_prompt=CANARY_NEUTRAL_PROMPT,
                call_id=canary_call_id,
            )
        else:
            code, tokens = "CANARY_OK", 10

        t1 = time.monotonic()
        active_sec = t1 - t0

        if code != CANARY_EXPECTED_RESPONSE:  # Exact string match, NO .strip() or rstrip() (P1-3)
            registry.record_canary_attempt(attempt_id, "failed_content", tokens, active_sec, True, call_id=canary_call_id)
            raise CanaryFailureError(
                f"Canary response content mismatch for attempt '{attempt_id}': expected '{CANARY_EXPECTED_RESPONSE}', got '{code}'"
            )

        canary_record = {
            "call_id": canary_call_id,
            "status": "passed",
            "tokens": tokens,
            "duration": active_sec,
            "attempts": 1,
            "usage_known": True,
        }
        canary_log.append(canary_record)
        registry.record_canary_attempt(attempt_id, "passed", tokens, active_sec, True, call_id=canary_call_id)
        registry.record_canary_passed(attempt_id, canary_log, tokens, active_sec)

        # Publish SCIENTIFIC_START immediately after canary passes
        publish_scientific_start(stage=stage, attempt_id=attempt_id, workspace_root=workspace_root)
        return registry.data

    except CanaryFailureError:
        raise
    except Exception as exc:
        registry.record_canary_attempt(attempt_id, "failed_execution", 0, 0.0, False, call_id=canary_call_id)
        registry.set_stage_blocked(attempt_id, f"Canary execution failed: {exc}")
        raise CanaryFailureError(f"Preflight canary execution failed for attempt '{attempt_id}': {exc}") from exc
