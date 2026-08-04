"""
Campaign 2 Durability Module.

Provides trial-boundary checkpointing for crash recovery.
The orchestrator calls seal_checkpoint_intent() before each trial
and commit_checkpoint_head() after each trial completes.
"""

import os
import json
import time
import hashlib
from datetime import datetime, timezone
from typing import Dict, Any, Optional, List

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class DurabilityError(RuntimeError):
    """Raised when checkpoint operations fail."""
    pass


class CheckpointManager:
    """
    Manages trial-boundary checkpoints for crash recovery.

    Called by the orchestrator at trial boundaries:
    - seal_checkpoint_intent() BEFORE starting a trial
    - commit_checkpoint_head() AFTER a trial completes successfully
    """

    def __init__(self, checkpoint_path: str, stage: str):
        self.checkpoint_path = checkpoint_path
        self.stage = stage
        self.pending_intent: Optional[dict] = None

    def seal_checkpoint_intent(self, trial_index: int, metadata: dict = None):
        """
        Record intent to start a trial. Written before trial execution begins.
        If the process crashes during the trial, this intent record identifies
        the last incomplete trial.
        """
        intent = {
            "stage": self.stage,
            "event": "trial_intent",
            "trial_index": trial_index,
            "timestamp_iso": datetime.now(timezone.utc).isoformat(),
        }
        if metadata:
            intent["metadata"] = metadata
        self.pending_intent = intent

        # Write intent to checkpoint file
        data = self._read_or_create()
        data["last_intent"] = intent
        self._write(data)

    def commit_checkpoint_head(
        self,
        trial_index: int,
        trial_result_summary: dict,
    ):
        """
        Commit completed trial result to checkpoint.
        Called after a trial completes successfully.
        """
        data = self._read_or_create()

        completion = {
            "stage": self.stage,
            "event": "trial_completed",
            "trial_index": trial_index,
            "timestamp_iso": datetime.now(timezone.utc).isoformat(),
            "result_summary": trial_result_summary,
        }

        if "completed_trials" not in data:
            data["completed_trials"] = []
        data["completed_trials"].append(completion)
        data["last_completed_trial_index"] = trial_index
        data["last_intent"] = None
        self.pending_intent = None

        self._write(data)

    def get_resume_trial_index(self) -> int:
        """
        Determine which trial to resume from.
        Returns the index of the next trial to execute (0-indexed).
        Returns 0 if no checkpoint exists.
        """
        data = self._read_or_create()
        last_completed = data.get("last_completed_trial_index")
        if last_completed is not None:
            return last_completed + 1
        return 0

    def get_completed_results(self) -> List[dict]:
        """Return the list of completed trial result summaries from checkpoint."""
        data = self._read_or_create()
        return [c["result_summary"] for c in data.get("completed_trials", [])]

    def _read_or_create(self) -> dict:
        """Read checkpoint file or create empty structure."""
        if os.path.exists(self.checkpoint_path):
            try:
                with open(self.checkpoint_path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError) as e:
                raise DurabilityError(f"Corrupt checkpoint at {self.checkpoint_path}: {e}")
        return {
            "schema_version": "3",
            "stage": self.stage,
            "created_iso": datetime.now(timezone.utc).isoformat(),
            "completed_trials": [],
            "last_completed_trial_index": None,
            "last_intent": None,
        }

    def _write(self, data: dict):
        """Write checkpoint data with fsync for durability."""
        with open(self.checkpoint_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        dir_fd = os.open(os.path.dirname(self.checkpoint_path), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
