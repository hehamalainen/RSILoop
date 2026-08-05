"""Crash-recoverable exact-head durability for Campaign 2.

The protocol is deliberately fail closed.  A sealed commit intent contains the
exact bytes of the next checkpoint, so recovery can either prove that the old
head is still installed and roll forward, or prove that the new head is already
installed and finish anchoring it.  No trial with a residual pre-trial intent is
ever silently sampled again.
"""

import base64
import hashlib
import json
import os
import stat
from typing import Optional

GENESIS_HEAD_HASH = "0" * 64


class DurabilityError(RuntimeError):
    """Raised when durable state is missing, ambiguous, rolled back, or forked."""


class CheckpointManager:
    def __init__(self, checkpoint_path: str, stage: str = "pilot"):
        self.checkpoint_path = checkpoint_path
        self.stage = stage
        self.intent_path = checkpoint_path + ".intent"
        self.commit_intent_path = checkpoint_path + ".commit_intent"
        self.anchor_path = checkpoint_path + ".state_anchor"
        self._open_or_recover()

    @staticmethod
    def _canonical_bytes(data: dict) -> bytes:
        return (json.dumps(data, indent=2, sort_keys=True) + "\n").encode("utf-8")

    def _fault_hook(self, boundary: str) -> None:
        """Test-only injection point; production instances leave it as a no-op."""

    def _write_sealed_bytes(self, target_path: str, content_bytes: bytes) -> None:
        """Atomically publish bytes, with the frozen fchmod-before-fsync ordering."""
        dir_name = os.path.dirname(target_path) or "."
        tmp_path = target_path + ".tmp"
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
                    raise DurabilityError(f"Short write while sealing {target_path}")
                view = view[written:]
            os.fchmod(fd, 0o444)
            os.fsync(fd)
        finally:
            os.close(fd)

        os.replace(tmp_path, target_path)
        dir_fd = os.open(dir_name, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    def _write_sealed_file(self, target_path: str, data: dict) -> None:
        self._write_sealed_bytes(target_path, self._canonical_bytes(data))

    def _read_sealed_bytes(self, path: str) -> bytes:
        if not os.path.lexists(path):
            raise DurabilityError(f"Required sealed file missing: {path}")
        if os.path.islink(path) or not os.path.isfile(path):
            raise DurabilityError(f"Sealed state must be a regular non-symlink file: {path}")
        mode = stat.S_IMODE(os.stat(path).st_mode)
        if mode != 0o444:
            raise DurabilityError(f"Sealed state mode must be 0o444 for {path}, got {oct(mode)}")
        with open(path, "rb") as handle:
            return handle.read()

    def _read_sealed_json(self, path: str) -> dict:
        try:
            data = json.loads(self._read_sealed_bytes(path).decode("utf-8"))
        except DurabilityError:
            raise
        except Exception as exc:
            raise DurabilityError(f"Invalid sealed JSON in {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise DurabilityError(f"Sealed JSON must be an object: {path}")
        return data

    def _genesis(self) -> dict:
        return {
            "stage": self.stage,
            "version": 1,
            "head_index": -1,
            "previous_head_hash": GENESIS_HEAD_HASH,
            "head_hash": GENESIS_HEAD_HASH,
            "completed_trials": [],
            "last_intent": None,
        }

    def _open_or_recover(self) -> None:
        paths = (self.checkpoint_path, self.anchor_path, self.intent_path, self.commit_intent_path)
        present = {path: os.path.lexists(path) for path in paths}
        if not any(present.values()):
            self._write_sealed_file(self.checkpoint_path, self._genesis())
            self._update_state_anchor(-1, GENESIS_HEAD_HASH)
            return
        if not present[self.checkpoint_path]:
            raise DurabilityError("Checkpoint is missing while durable attempt state remains; refusing genesis re-anchor")
        if present[self.commit_intent_path]:
            self._recover_commit_intent()
        elif present[self.intent_path]:
            intent = self._read_sealed_json(self.intent_path)
            raise DurabilityError(
                f"Ambiguous pre-trial intent for trial {intent.get('trial_index')}; attempt must be invalidated, not resampled"
            )
        if not os.path.exists(self.anchor_path):
            raise DurabilityError("Checkpoint state anchor is missing; refusing implicit re-anchor")
        self.read_checkpoint()

    def _update_state_anchor(self, head_index: int, head_hash: str) -> None:
        anchor_data = {
            "highest_committed_index": head_index,
            "highest_committed_hash": head_hash,
        }
        self._write_sealed_file(self.anchor_path, anchor_data)

    def _remove_durable_files(self, paths) -> None:
        removed = False
        for path in paths:
            if os.path.lexists(path):
                if os.path.islink(path) or not os.path.isfile(path):
                    raise DurabilityError(f"Refusing to remove non-regular durable state: {path}")
                os.chmod(path, 0o600)
                os.unlink(path)
                removed = True
        if removed:
            dir_fd = os.open(os.path.dirname(self.checkpoint_path) or ".", os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

    def _recover_commit_intent(self) -> None:
        intent = self._read_sealed_json(self.commit_intent_path)
        required = {
            "trial_index", "previous_head_hash", "trial_record_hash",
            "expected_new_head_hash", "expected_checkpoint_sha256",
            "expected_checkpoint_size", "expected_checkpoint_bytes_b64",
        }
        missing = sorted(required - set(intent))
        if missing:
            raise DurabilityError(f"Commit intent missing recovery fields: {missing}")
        try:
            expected_bytes = base64.b64decode(intent["expected_checkpoint_bytes_b64"], validate=True)
            expected_data = json.loads(expected_bytes.decode("utf-8"))
        except Exception as exc:
            raise DurabilityError(f"Commit intent contains invalid expected checkpoint bytes: {exc}") from exc
        if expected_bytes != self._canonical_bytes(expected_data):
            raise DurabilityError("Commit intent expected checkpoint is not canonical")
        expected_digest = hashlib.sha256(expected_bytes).hexdigest()
        if len(expected_bytes) != intent["expected_checkpoint_size"]:
            raise DurabilityError("Commit intent expected checkpoint byte length mismatch")
        if expected_digest != intent["expected_checkpoint_sha256"]:
            raise DurabilityError("Commit intent expected checkpoint byte hash mismatch")
        if expected_data.get("head_index") != intent["trial_index"]:
            raise DurabilityError("Commit intent trial index does not match expected checkpoint")
        if expected_data.get("previous_head_hash") != intent["previous_head_hash"]:
            raise DurabilityError("Commit intent previous head does not match expected checkpoint")
        if expected_data.get("head_hash") != intent["expected_new_head_hash"]:
            raise DurabilityError("Commit intent new head does not match expected checkpoint")
        if expected_data.get("trial_record_hash") != intent["trial_record_hash"]:
            raise DurabilityError("Commit intent trial record does not match expected checkpoint")
        self.verify_checkpoint_head_chain(expected_data)

        current_bytes = self._read_sealed_bytes(self.checkpoint_path)
        try:
            current = json.loads(current_bytes.decode("utf-8"))
        except Exception as exc:
            raise DurabilityError(f"Invalid checkpoint during recovery: {exc}") from exc
        current_is_old = (
            current.get("head_index") == intent["trial_index"] - 1
            and current.get("head_hash") == intent["previous_head_hash"]
        )
        current_is_new = hashlib.sha256(current_bytes).hexdigest() == expected_digest
        if current_is_old:
            raise DurabilityError(
                f"Interrupted commit intent for trial {intent['trial_index']} with previous head hash '{intent['previous_head_hash']}': "
                f"attempt invalidated under frozen B5 rule"
            )
        elif not current_is_new:
            raise DurabilityError("Commit recovery found neither the proven previous head nor exact expected new bytes")

        self._write_sealed_bytes(self.checkpoint_path, current_bytes)

        if os.path.exists(self.anchor_path):
            anchor = self._read_sealed_json(self.anchor_path)
            anchor_state = (anchor.get("highest_committed_index"), anchor.get("highest_committed_hash"))
            allowed = {
                (intent["trial_index"] - 1, intent["previous_head_hash"]),
                (intent["trial_index"], intent["expected_new_head_hash"]),
            }
            if anchor_state not in allowed:
                raise DurabilityError(f"Commit recovery anchor is not on the proven transition: {anchor_state}")
        self._update_state_anchor(intent["trial_index"], intent["expected_new_head_hash"])
        self._remove_durable_files((self.intent_path, self.commit_intent_path))

    def seal_pre_trial_intent(self, trial_index: int, metadata: Optional[dict] = None) -> str:
        current = self.read_checkpoint()
        if trial_index != current["head_index"] + 1:
            raise DurabilityError(f"Pre-trial intent {trial_index} is not next after head {current['head_index']}")
        if os.path.lexists(self.intent_path) or os.path.lexists(self.commit_intent_path):
            raise DurabilityError("Residual trial intent exists; refusing to overwrite it")
        self._write_sealed_file(self.intent_path, {
            "trial_index": trial_index,
            "previous_head_hash": current["head_hash"],
            "stage": self.stage,
            "status": "intent_sealed",
            "metadata": metadata or {},
        })
        self._fault_hook("after_pre_trial_intent")
        return self.intent_path

    def commit_checkpoint_head(self, trial_index: int, trial_result_dict: dict) -> str:
        current = self.read_checkpoint()
        if trial_index != current["head_index"] + 1:
            raise DurabilityError(f"Cannot commit trial {trial_index}; current head is {current['head_index']}")
        pre_intent = self._read_sealed_json(self.intent_path)
        if (pre_intent.get("trial_index"), pre_intent.get("previous_head_hash")) != (trial_index, current["head_hash"]):
            raise DurabilityError("Pre-trial intent does not authorize this checkpoint transition")

        trial_bytes = json.dumps(trial_result_dict, sort_keys=True).encode("utf-8")
        trial_hash = hashlib.sha256(trial_bytes).hexdigest()
        new_head = hashlib.sha256(f"{current['head_hash']}|{trial_index}|{trial_hash}".encode("utf-8")).hexdigest()
        new_data = {
            "stage": self.stage,
            "version": 1,
            "head_index": trial_index,
            "previous_head_hash": current["head_hash"],
            "trial_record_hash": trial_hash,
            "head_hash": new_head,
            "completed_trials": list(current.get("completed_trials", [])) + [trial_result_dict],
            "last_intent": None,
        }
        expected_bytes = self._canonical_bytes(new_data)
        commit_intent = {
            "trial_index": trial_index,
            "previous_head_hash": current["head_hash"],
            "trial_record_hash": trial_hash,
            "expected_new_head_hash": new_head,
            "expected_checkpoint_sha256": hashlib.sha256(expected_bytes).hexdigest(),
            "expected_checkpoint_size": len(expected_bytes),
            "expected_checkpoint_bytes_b64": base64.b64encode(expected_bytes).decode("ascii"),
            "status": "commit_intent_sealed",
        }
        self._write_sealed_file(self.commit_intent_path, commit_intent)
        self._fault_hook("after_commit_intent")
        self._write_sealed_bytes(self.checkpoint_path, expected_bytes)
        self._fault_hook("after_checkpoint_replace")
        self._update_state_anchor(trial_index, new_head)
        self._fault_hook("after_anchor_replace")
        self._remove_durable_files((self.intent_path, self.commit_intent_path))
        self._fault_hook("after_intent_cleanup")
        return new_head

    def read_checkpoint(self) -> dict:
        data = self._read_sealed_json(self.checkpoint_path)
        if data.get("stage") != self.stage:
            raise DurabilityError(f"Checkpoint stage mismatch: expected {self.stage}, got {data.get('stage')}")
        anchor = self._read_sealed_json(self.anchor_path)
        actual = (data.get("head_index"), data.get("head_hash"))
        anchored = (anchor.get("highest_committed_index"), anchor.get("highest_committed_hash"))
        if actual != anchored:
            raise DurabilityError(f"Checkpoint/anchor exact-head mismatch: checkpoint={actual}, anchor={anchored}")
        self.verify_checkpoint_head_chain(data)
        return data

    def verify_checkpoint_head_chain(self, data: Optional[dict] = None) -> bool:
        if data is None:
            data = self._read_sealed_json(self.checkpoint_path)
        head_index = data.get("head_index")
        completed = data.get("completed_trials")
        if not isinstance(head_index, int) or not isinstance(completed, list):
            raise DurabilityError("Checkpoint has invalid head_index or completed_trials")
        if head_index == -1:
            if completed or data.get("head_hash") != GENESIS_HEAD_HASH:
                raise DurabilityError("Invalid genesis checkpoint")
            return True
        if len(completed) != head_index + 1:
            raise DurabilityError("Checkpoint completed trial count does not match head index")
        head = GENESIS_HEAD_HASH
        prior_head = GENESIS_HEAD_HASH
        final_trial_hash = None
        for index, trial in enumerate(completed):
            trial_hash = hashlib.sha256(json.dumps(trial, sort_keys=True).encode("utf-8")).hexdigest()
            prior_head = head
            final_trial_hash = trial_hash
            head = hashlib.sha256(f"{head}|{index}|{trial_hash}".encode("utf-8")).hexdigest()
        if head != data.get("head_hash"):
            raise DurabilityError("Checkpoint hash-chain verification failed")
        if data.get("previous_head_hash") != prior_head:
            raise DurabilityError("Checkpoint previous_head_hash does not match its chain")
        if data.get("trial_record_hash") != final_trial_hash:
            raise DurabilityError("Checkpoint trial_record_hash does not match its final trial")
        return True

    def get_resume_trial_index(self) -> int:
        if os.path.exists(self.intent_path) or os.path.exists(self.commit_intent_path):
            raise DurabilityError("Residual intent reached scheduling; recovery/admission did not complete")
        return self.read_checkpoint()["head_index"] + 1

    def get_completed_results(self) -> list:
        return self.read_checkpoint().get("completed_trials", [])
