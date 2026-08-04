"""
Campaign 2 Experiment Orchestrator Module.

P0-3, P0-4, P0-6 — EXPERIMENT ORCHESTRATION ENGINE:
- Complete per-trial execution loop with exact call arithmetic (202 for Suite C, 86 for Suite P).
- Active arm assignment vector (+1/-1) wiring altering scheduling order and slot labeling.
- Integrated AttemptLedger (intent-before-completion recording per model call).
- Integrated CheckpointManager (trial-boundary intent sealing and completed head commits).
- Resumption support starting at checkpoint resume_trial_index.
- PromptEnvelope validation on all effective prompts and mutation outputs.
- Trio adjacency verification across all Phase 3 microblock trios.
"""

import os
import sys
import json
import time
import hashlib
from fractions import Fraction
from typing import Dict, Any, List, Tuple, Optional
from datetime import datetime, timezone

from campaign2.model_provider import ModelProvider
from campaign2.envelope import PromptEnvelope, PromptEnvelopeError
from campaign2.scorer import (
    task_score_fraction,
    task_mean_over_k,
    aggregate_tasks_equal_weight,
    ScorerValidationError,
)
from campaign2.registration import (
    verify_master_registration,
    FROZEN_TRIO_ORDERINGS,
    get_trio_ordering_index,
)
from campaign2.tier_assignment import (
    EXPECTED_SUITE_C_TASK_IDS,
    EXPECTED_SUITE_P_TASK_IDS,
)
from campaign2.attempt_lifecycle import AttemptLedger, CallFault
from campaign2.durability import CheckpointManager

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Suite parameters
SUITE_C_PARAMS = {
    "name": "suite_c",
    "num_trials": 10,
    "k": 2,
    "mutation_rounds": 3,
    "expected_phase1_calls": 16,
    "expected_phase2_calls": 102,
    "expected_phase3_calls": 84,
    "expected_total_calls": 202,
}

SUITE_P_PARAMS = {
    "name": "suite_p",
    "num_trials": 2,
    "k": 2,
    "mutation_rounds": 3,
    "expected_phase1_calls": 8,
    "expected_phase2_calls": 54,
    "expected_phase3_calls": 24,
    "expected_total_calls": 86,
}

INITIAL_MUTABLE_BODY = "Follow good software engineering practices. Handle edge cases carefully."

CONTROL_REFORMULATION_TEXT = (
    "Rewrite the strategy section of this prompt using different wording.\n"
    "Do not change the meaning or intent. Keep it concise."
)


class OrchestratorError(RuntimeError):
    """Raised when the orchestrator detects an invariant violation."""
    pass


class CallArithmeticError(OrchestratorError):
    """Raised when per-trial call count does not match expected arithmetic."""
    pass


class TrioAdjacencyError(OrchestratorError):
    """Raised when a microblock trio is interrupted by an unrelated call."""
    pass


class CallRecord:
    """Record of a single model or evaluation call."""
    __slots__ = ('call_id', 'call_type', 'arm', 'phase', 'task_id',
                 'replicate', 'trio_slot', 'trio_position', 'tokens',
                 'earned', 'max_score', 'timestamp', 'round_index', 'prompt_hash')

    def __init__(self, call_id: str, call_type: str, arm: str, phase: int,
                 task_id: str = "", replicate: int = -1, trio_slot: str = "",
                 trio_position: int = -1, tokens: int = 0, earned: int = -1,
                 max_score: int = -1, round_index: int = -1, prompt_hash: str = ""):
        self.call_id = call_id
        self.call_type = call_type
        self.arm = arm
        self.phase = phase
        self.task_id = task_id
        self.replicate = replicate
        self.trio_slot = trio_slot
        self.trio_position = trio_position
        self.tokens = tokens
        self.earned = earned
        self.max_score = max_score
        self.timestamp = time.time()
        self.round_index = round_index
        self.prompt_hash = prompt_hash

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}


class TrialResult:
    """Complete result of a single trial."""

    def __init__(self, trial_index: int, trial_id: str, arm_sign: int):
        self.trial_index = trial_index
        self.trial_id = trial_id
        self.arm_sign = arm_sign  # +1 or -1
        self.call_records: List[CallRecord] = []
        self.phase1_scores: Dict[str, List[Tuple[int, int]]] = {}
        self.phase2_arm_scores: Dict[str, Dict[str, List[Tuple[int, int]]]] = {
            "optimizer": {},
            "control": {},
        }
        self.phase2_winner: str = ""
        self.phase3_trio_scores: Dict[str, Dict[str, List[Tuple[int, int]]]] = {
            "baseline": {},
            "optimizer_winner": {},
            "control_winner": {},
        }
        self.optimizer_mutable_body: str = ""
        self.control_mutable_body: str = ""
        self.total_tokens: int = 0
        self.started_at: float = 0.0
        self.finished_at: float = 0.0

    def model_call_count(self) -> int:
        return len([r for r in self.call_records if r.call_type in ("generate", "mutate")])


class ExploratoryTracker:
    """Tracks the 4 pre-registered exploratory secondary metrics."""

    def __init__(self):
        self.mutations_total = 0
        self.score_improving_mutations = 0
        self.total_output_tokens = 0
        self.valid_candidates = 0
        self.total_candidates = 0
        self.best_depth_per_trial: List[int] = []

    def record_mutation_outcome(self, improved: bool):
        self.mutations_total += 1
        if improved:
            self.score_improving_mutations += 1

    def record_generation(self, tokens: int, valid: bool):
        self.total_output_tokens += tokens
        self.total_candidates += 1
        if valid:
            self.valid_candidates += 1

    def record_best_depth(self, depth: int):
        self.best_depth_per_trial.append(depth)

    def summary(self) -> dict:
        return {
            "score_improving_mutations_per_call": (
                f"{self.score_improving_mutations}/{self.mutations_total}"
                if self.mutations_total > 0 else "0/0"
            ),
            "improvement_per_1000_tokens": (
                f"{self.score_improving_mutations / (self.total_output_tokens / 1000):.4f}"
                if self.total_output_tokens > 0 else "N/A"
            ),
            "valid_candidate_rate": (
                f"{self.valid_candidates}/{self.total_candidates}"
                if self.total_candidates > 0 else "0/0"
            ),
            "best_mutation_depths": self.best_depth_per_trial,
        }


class Campaign2Orchestrator:
    """
    Full Campaign 2 experiment engine with integrated ledger, durability,
    arm assignment randomization, and prompt envelope validation.
    """

    def __init__(
        self,
        model_provider: ModelProvider,
        task_evaluator: Any,
        tier_assignment: Dict[str, Any],
        registration: Dict[str, Any],
        suite: str = "suite_c",
        ledger: Optional[AttemptLedger] = None,
        checkpoint_mgr: Optional[CheckpointManager] = None,
        workspace_root: str = BASE_DIR,
    ):
        self.model = model_provider
        self.evaluator = task_evaluator
        self.ledger = ledger
        self.checkpoint_mgr = checkpoint_mgr

        if suite not in ("suite_c", "suite_p"):
            raise OrchestratorError(f"Unknown suite: {suite}")

        self.suite = suite
        self.params = SUITE_C_PARAMS if suite == "suite_c" else SUITE_P_PARAMS
        self.workspace_root = workspace_root
        self.envelope = PromptEnvelope()

        # Load tier assignments
        assignments = tier_assignment["set_assignments"]
        suite_key = suite
        self.opt_tasks = sorted(assignments[suite_key]["optimization"])
        self.sel_tasks = sorted(assignments[suite_key]["selection"])
        self.conf_tasks = sorted(assignments[suite_key]["confirmatory"])
        all_holdouts = assignments["structural_holdout"]
        if suite == "suite_c":
            self.holdout_tasks = sorted([t for t in all_holdouts if t.startswith("c_f4_")])
        else:
            self.holdout_tasks = sorted([t for t in all_holdouts if t.startswith("p_f4_")])

        self.opt_sel_tasks = sorted(self.opt_tasks + self.sel_tasks)
        self.conf_holdout_tasks = sorted(self.conf_tasks + self.holdout_tasks)

        # Load registration parameters
        self.arm_vectors = registration["arm_assignment_vectors"]
        self.trio_seed = registration["trio_ordering_master_seed"]
        self.trio_orderings = [tuple(t) for t in registration["frozen_trio_orderings"]]

        self.trial_results: List[TrialResult] = []
        self.call_counter = 0
        self.exploratory = ExploratoryTracker()

    def _next_call_id(self, prefix: str) -> str:
        self.call_counter += 1
        return f"{prefix}_{self.call_counter:04d}"

    def _generate_and_evaluate(
        self, task_id: str, mutable_body: str, arm: str, phase: int,
        replicate: int, trio_slot: str = "", trio_position: int = -1,
        round_index: int = -1,
    ) -> Tuple[int, int, int, CallRecord]:
        """Generate candidate code and evaluate it with ledger & envelope protection."""
        call_id = self._next_call_id(f"gen_{arm}_{task_id}_r{replicate}")
        effective_prompt, prompt_hash = self.envelope.build_effective_prompt(mutable_body)

        # 1. Record Intent BEFORE model call
        if self.ledger:
            self.ledger.record_intent(call_id, "generate", arm, phase, task_id, {"prompt_hash": prompt_hash})

        # 2. Make Model Call
        try:
            code, tokens = self.model.generate_candidate(task_id, effective_prompt, call_id)
        except Exception as e:
            if self.ledger:
                self.ledger.record_completion(call_id, False, 0, fault=CallFault("generation_error", False, str(e), type(e).__name__))
            raise

        # 3. Evaluate Candidate Code
        earned, max_s = self.evaluator.evaluate(task_id, code)

        # 4. Record Completion AFTER model call
        if self.ledger:
            self.ledger.record_completion(call_id, True, tokens)

        self.exploratory.record_generation(tokens, earned > 0)

        record = CallRecord(
            call_id=call_id, call_type="generate", arm=arm, phase=phase,
            task_id=task_id, replicate=replicate, trio_slot=trio_slot,
            trio_position=trio_position, tokens=tokens, earned=earned,
            max_score=max_s, round_index=round_index, prompt_hash=prompt_hash,
        )
        return earned, max_s, tokens, record

    def _mutate_prompt_with_envelope(
        self, mutable_body: str, failure_context: str, arm: str, phase: int,
        round_index: int,
    ) -> Tuple[str, int, CallRecord]:
        """Mutate prompt body with envelope validation & ledger tracking."""
        call_id = self._next_call_id(f"mut_{arm}_r{round_index}")

        # 1. Record Intent BEFORE model call
        if self.ledger:
            self.ledger.record_intent(call_id, "mutate", arm, phase, metadata={"round_index": round_index})

        # 2. Make Model Call
        try:
            raw_new_body, tokens = self.model.mutate_prompt(mutable_body, failure_context, call_id)
        except Exception as e:
            if self.ledger:
                self.ledger.record_completion(call_id, False, 0, fault=CallFault("mutation_error", False, str(e), type(e).__name__))
            raise

        # 3. Validate Envelope Integrity
        clean_new_body = self.envelope.validate_mutation_output(raw_new_body)

        # 4. Record Completion AFTER model call
        if self.ledger:
            self.ledger.record_completion(call_id, True, tokens)

        record = CallRecord(
            call_id=call_id, call_type="mutate", arm=arm, phase=phase,
            round_index=round_index, tokens=tokens,
        )
        return clean_new_body, tokens, record

    def _evaluate_prompt_on_tasks(
        self, tasks: List[str], mutable_body: str, arm: str, phase: int,
        round_index: int = -1,
    ) -> Tuple[Dict[str, List[Tuple[int, int]]], List[CallRecord], int]:
        """Evaluate a prompt across tasks with k replicates."""
        k = self.params["k"]
        scores: Dict[str, List[Tuple[int, int]]] = {}
        records: List[CallRecord] = []
        total_tokens = 0

        for task_id in tasks:
            task_pairs = []
            for rep in range(k):
                earned, max_s, tokens, record = self._generate_and_evaluate(
                    task_id, mutable_body, arm, phase, rep,
                    round_index=round_index,
                )
                task_pairs.append((earned, max_s))
                records.append(record)
                total_tokens += tokens
            scores[task_id] = task_pairs

        return scores, records, total_tokens

    def _compute_aggregate_score(self, task_scores: Dict[str, List[Tuple[int, int]]]) -> Fraction:
        """Compute equal-weighted aggregate score across tasks."""
        task_means = []
        for task_id in sorted(task_scores.keys()):
            pairs = task_scores[task_id]
            mean = task_mean_over_k(pairs)
            task_means.append(mean)
        return aggregate_tasks_equal_weight(task_means)

    def _collect_failure_traces(
        self, task_scores: Dict[str, List[Tuple[int, int]]], mutable_body: str
    ) -> str:
        """Collect failure context for optimizer arm mutation."""
        failures = []
        for task_id in sorted(task_scores.keys()):
            for rep_idx, (earned, max_s) in enumerate(task_scores[task_id]):
                if earned < max_s:
                    failures.append(
                        f"--- Task: {task_id}, Replicate: {rep_idx} ---\n"
                        f"Score: {earned}/{max_s}\n"
                    )
        if not failures:
            return ""
        return "Failed Tasks:\n" + "\n".join(failures)

    def run_trial(self, trial_index: int) -> TrialResult:
        """Execute a complete trial using registered arm_sign (+1/-1)."""
        trial_id = f"trial_{trial_index + 1}"
        arm_sign = self.arm_vectors[trial_index]  # +1 or -1
        result = TrialResult(trial_index, trial_id, arm_sign)
        result.started_at = time.time()

        k = self.params["k"]
        mr = self.params["mutation_rounds"]

        # ---- PHASE 1: Pre-mutation baseline on opt+sel ----
        baseline_body = INITIAL_MUTABLE_BODY
        phase1_scores, phase1_records, phase1_tokens = self._evaluate_prompt_on_tasks(
            self.opt_sel_tasks, baseline_body, "baseline", phase=1,
        )
        result.phase1_scores = phase1_scores
        result.call_records.extend(phase1_records)
        result.total_tokens += phase1_tokens

        phase1_call_count = len(phase1_records)
        if phase1_call_count != self.params["expected_phase1_calls"]:
            raise CallArithmeticError(
                f"Trial {trial_id} Phase 1: expected {self.params['expected_phase1_calls']} "
                f"calls, got {phase1_call_count}"
            )

        # ---- PHASE 2: 3 mutation rounds + selection ----
        opt_body = baseline_body
        ctrl_body = baseline_body
        best_opt_score = self._compute_aggregate_score(phase1_scores)
        best_ctrl_score = best_opt_score
        best_opt_body = opt_body
        best_ctrl_body = ctrl_body
        best_opt_depth = 0
        best_ctrl_depth = 0
        phase2_call_count = 0

        # P0-4: Active arm assignment vector wiring
        # When arm_sign == +1: Arm A = Optimizer, Arm B = Control
        # When arm_sign == -1: Arm A = Control, Arm B = Optimizer
        for round_idx in range(mr):
            if arm_sign == +1:
                # 1. Mutate Optimizer (Arm A)
                failure_ctx = self._collect_failure_traces(phase1_scores if round_idx == 0 else opt_scores, opt_body)
                new_opt_body, opt_mut_tokens, opt_mut_rec = self._mutate_prompt_with_envelope(
                    opt_body, failure_ctx, "optimizer", phase=2, round_index=round_idx
                )
                result.call_records.append(opt_mut_rec)
                result.total_tokens += opt_mut_tokens
                phase2_call_count += 1

                # 2. Mutate Control (Arm B)
                new_ctrl_body, ctrl_mut_tokens, ctrl_mut_rec = self._mutate_prompt_with_envelope(
                    ctrl_body, CONTROL_REFORMULATION_TEXT, "control", phase=2, round_index=round_idx
                )
                result.call_records.append(ctrl_mut_rec)
                result.total_tokens += ctrl_mut_tokens
                phase2_call_count += 1

                # 3. Evaluate Optimizer (Arm A)
                opt_scores, opt_recs, opt_tokens = self._evaluate_prompt_on_tasks(
                    self.opt_sel_tasks, new_opt_body, "optimizer", phase=2, round_index=round_idx
                )
                result.call_records.extend(opt_recs)
                result.total_tokens += opt_tokens
                phase2_call_count += len(opt_recs)

                # 4. Evaluate Control (Arm B)
                ctrl_scores, ctrl_recs, ctrl_tokens = self._evaluate_prompt_on_tasks(
                    self.opt_sel_tasks, new_ctrl_body, "control", phase=2, round_index=round_idx
                )
                result.call_records.extend(ctrl_recs)
                result.total_tokens += ctrl_tokens
                phase2_call_count += len(ctrl_recs)
            else:
                # arm_sign == -1: Swap execution order (Arm A = Control, Arm B = Optimizer)
                # 1. Mutate Control (Arm A)
                new_ctrl_body, ctrl_mut_tokens, ctrl_mut_rec = self._mutate_prompt_with_envelope(
                    ctrl_body, CONTROL_REFORMULATION_TEXT, "control", phase=2, round_index=round_idx
                )
                result.call_records.append(ctrl_mut_rec)
                result.total_tokens += ctrl_mut_tokens
                phase2_call_count += 1

                # 2. Mutate Optimizer (Arm B)
                failure_ctx = self._collect_failure_traces(phase1_scores if round_idx == 0 else opt_scores, opt_body)
                new_opt_body, opt_mut_tokens, opt_mut_rec = self._mutate_prompt_with_envelope(
                    opt_body, failure_ctx, "optimizer", phase=2, round_index=round_idx
                )
                result.call_records.append(opt_mut_rec)
                result.total_tokens += opt_mut_tokens
                phase2_call_count += 1

                # 3. Evaluate Control (Arm A)
                ctrl_scores, ctrl_recs, ctrl_tokens = self._evaluate_prompt_on_tasks(
                    self.opt_sel_tasks, new_ctrl_body, "control", phase=2, round_index=round_idx
                )
                result.call_records.extend(ctrl_recs)
                result.total_tokens += ctrl_tokens
                phase2_call_count += len(ctrl_recs)

                # 4. Evaluate Optimizer (Arm B)
                opt_scores, opt_recs, opt_tokens = self._evaluate_prompt_on_tasks(
                    self.opt_sel_tasks, new_opt_body, "optimizer", phase=2, round_index=round_idx
                )
                result.call_records.extend(opt_recs)
                result.total_tokens += opt_tokens
                phase2_call_count += len(opt_recs)

            # Score tracking for both arms
            opt_score = self._compute_aggregate_score(opt_scores)
            self.exploratory.record_mutation_outcome(opt_score > best_opt_score)
            if opt_score > best_opt_score:
                best_opt_score = opt_score
                best_opt_body = new_opt_body
                best_opt_depth = round_idx + 1
            opt_body = new_opt_body

            ctrl_score = self._compute_aggregate_score(ctrl_scores)
            self.exploratory.record_mutation_outcome(ctrl_score > best_ctrl_score)
            if ctrl_score > best_ctrl_score:
                best_ctrl_score = ctrl_score
                best_ctrl_body = new_ctrl_body
                best_ctrl_depth = round_idx + 1
            ctrl_body = new_ctrl_body

        if phase2_call_count != self.params["expected_phase2_calls"]:
            raise CallArithmeticError(
                f"Trial {trial_id} Phase 2: expected {self.params['expected_phase2_calls']} "
                f"calls, got {phase2_call_count}"
            )

        if best_opt_score >= best_ctrl_score:
            result.phase2_winner = "optimizer"
        else:
            result.phase2_winner = "control"

        result.optimizer_mutable_body = best_opt_body
        result.control_mutable_body = best_ctrl_body
        self.exploratory.record_best_depth(best_opt_depth)

        result.phase2_arm_scores["optimizer"] = opt_scores
        result.phase2_arm_scores["control"] = ctrl_scores

        # ---- PHASE 3: Post-selection microblock trios on conf+holdout ----
        slot_bodies = {
            "baseline": baseline_body,
            "optimizer_winner": best_opt_body,
            "control_winner": best_ctrl_body,
        }

        phase3_call_count = 0

        for task_idx, task_id in enumerate(self.conf_holdout_tasks):
            for rep in range(k):
                ordering_idx = get_trio_ordering_index(self.trio_seed, task_idx, rep)
                ordering = self.trio_orderings[ordering_idx]

                trio_records = []
                for position, slot_name in enumerate(ordering):
                    body = slot_bodies[slot_name]
                    earned, max_s, tokens, record = self._generate_and_evaluate(
                        task_id, body, slot_name, phase=3, replicate=rep,
                        trio_slot=slot_name, trio_position=position,
                    )
                    trio_records.append(record)
                    result.total_tokens += tokens

                    if task_id not in result.phase3_trio_scores[slot_name]:
                        result.phase3_trio_scores[slot_name][task_id] = []
                    result.phase3_trio_scores[slot_name][task_id].append((earned, max_s))

                result.call_records.extend(trio_records)
                phase3_call_count += len(trio_records)

                if len(trio_records) != 3:
                    raise TrioAdjacencyError(
                        f"Trio for {task_id} rep {rep} has {len(trio_records)} records, expected 3"
                    )

        if phase3_call_count != self.params["expected_phase3_calls"]:
            raise CallArithmeticError(
                f"Trial {trial_id} Phase 3: expected {self.params['expected_phase3_calls']} "
                f"calls, got {phase3_call_count}"
            )

        total = phase1_call_count + phase2_call_count + phase3_call_count
        if total != self.params["expected_total_calls"]:
            raise CallArithmeticError(
                f"Trial {trial_id} total: expected {self.params['expected_total_calls']} "
                f"calls, got {total}"
            )

        result.finished_at = time.time()
        self.trial_results.append(result)
        return result

    def run_all_trials(self) -> List[TrialResult]:
        """
        Execute trials with resumption support via CheckpointManager.
        Resumes from start_trial_index if an existing checkpoint is loaded.
        """
        start_trial_index = 0
        if self.checkpoint_mgr:
            start_trial_index = self.checkpoint_mgr.get_resume_trial_index()

        num_trials = self.params["num_trials"]
        results = []

        for trial_idx in range(start_trial_index, num_trials):
            # 1. Seal intent BEFORE trial execution
            if self.checkpoint_mgr:
                self.checkpoint_mgr.seal_checkpoint_intent(trial_idx, {"arm_sign": self.arm_vectors[trial_idx]})

            # 2. Run Trial
            result = self.run_trial(trial_idx)
            results.append(result)

            # 3. Commit completed head AFTER trial execution
            if self.checkpoint_mgr:
                summary = {
                    "trial_id": result.trial_id,
                    "arm_sign": result.arm_sign,
                    "winner": result.phase2_winner,
                    "tokens": result.total_tokens,
                }
                self.checkpoint_mgr.commit_checkpoint_head(trial_idx, summary)

        return results

    def compute_outcome(self) -> Dict[str, Any]:
        """Compute Schema 3 experiment outcome."""
        if not self.trial_results:
            raise OrchestratorError("No trials have been executed")

        slot_trial_aggregates = {
            "baseline": [],
            "optimizer_winner": [],
            "control_winner": [],
        }

        for result in self.trial_results:
            for slot_name in ("baseline", "optimizer_winner", "control_winner"):
                slot_scores = result.phase3_trio_scores[slot_name]
                if slot_scores:
                    agg = self._compute_aggregate_score(slot_scores)
                    slot_trial_aggregates[slot_name].append(agg)

        def mean_fraction(fracs: List[Fraction]) -> Fraction:
            if not fracs:
                return Fraction(0)
            return sum(fracs) / Fraction(len(fracs))

        baseline_mean = mean_fraction(slot_trial_aggregates["baseline"])
        opt_mean = mean_fraction(slot_trial_aggregates["optimizer_winner"])
        ctrl_mean = mean_fraction(slot_trial_aggregates["control_winner"])
        delta = opt_mean - ctrl_mean

        return {
            "schema_version": "3",
            "suite": self.suite,
            "num_trials": self.params["num_trials"],
            "arm_assignment_vectors": self.arm_vectors,
            "immutable_contract_hash": self.envelope.immutable_contract_hash,
            "delimiter_hash": self.envelope.delimiter_hash,
            "baseline_aggregate": str(baseline_mean),
            "optimizer_winner_aggregate": str(opt_mean),
            "control_winner_aggregate": str(ctrl_mean),
            "delta_opt_minus_ctrl": str(delta),
            "delta_positive": delta > 0,
            "per_trial": [
                {
                    "trial_id": r.trial_id,
                    "arm_sign": r.arm_sign,
                    "winner": r.phase2_winner,
                    "total_tokens": r.total_tokens,
                    "model_calls": r.model_call_count(),
                    "baseline_scores": {
                        t: list(pairs)
                        for t, pairs in r.phase3_trio_scores["baseline"].items()
                    },
                    "optimizer_scores": {
                        t: list(pairs)
                        for t, pairs in r.phase3_trio_scores["optimizer_winner"].items()
                    },
                    "control_scores": {
                        t: list(pairs)
                        for t, pairs in r.phase3_trio_scores["control_winner"].items()
                    },
                }
                for r in self.trial_results
            ],
            "exploratory": self.exploratory.summary(),
        }

    def verify_trio_adjacency(self) -> bool:
        """Verify that all Phase 3 microblock trios are adjacent in the call log."""
        for result in self.trial_results:
            phase3_records = [r for r in result.call_records if r.phase == 3]

            i = 0
            while i < len(phase3_records):
                if i + 2 >= len(phase3_records):
                    raise TrioAdjacencyError(
                        f"Incomplete trio at end of Phase 3 records in {result.trial_id}"
                    )
                trio = phase3_records[i:i+3]
                task_ids = {r.task_id for r in trio}
                replicates = {r.replicate for r in trio}

                if len(task_ids) != 1:
                    raise TrioAdjacencyError(
                        f"Trio task_id mismatch in {result.trial_id}: {task_ids}"
                    )
                if len(replicates) != 1:
                    raise TrioAdjacencyError(
                        f"Trio replicate mismatch in {result.trial_id}: {replicates}"
                    )

                positions = [r.trio_position for r in trio]
                if positions != [0, 1, 2]:
                    raise TrioAdjacencyError(
                        f"Trio positions not sequential in {result.trial_id}: {positions}"
                    )

                slots = {r.trio_slot for r in trio}
                expected_slots = {"baseline", "optimizer_winner", "control_winner"}
                if slots != expected_slots:
                    raise TrioAdjacencyError(
                        f"Trio slots incomplete in {result.trial_id}: {slots}"
                    )

                i += 3

        return True
