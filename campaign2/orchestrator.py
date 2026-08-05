"""
Campaign 2 Orchestrator Module.

Manages two-arm randomized strategy search across optimization, selection,
confirmatory, and structural holdout task tiers.
Integrates durability checkpointing, attempt ledger, prompt envelope protection,
exact rational outcome arithmetic, equal-task weighted winner selection,
and observable retries.
"""

import os
import time
import json
import re
from typing import Dict, Any, List, Tuple, Optional
from fractions import Fraction

from campaign2.model_provider import ModelProvider
from campaign2.envelope import PromptEnvelope, PromptEnvelopeError
from campaign2.attempt_lifecycle import AttemptLedger, classify_call_fault, CallFault
from campaign2.durability import CheckpointManager
from campaign2.registration import get_trio_ordering_index

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class OrchestratorError(RuntimeError):
    """Base exception for orchestrator execution failures."""
    pass


class CallArithmeticError(OrchestratorError):
    """Raised when call count arithmetic fails."""
    pass


class TrioAdjacencyError(OrchestratorError):
    """Raised when microblock trio adjacency requirements are violated."""
    pass


SUITE_C_PARAMS = {
    "num_trials": 10,
    "mutations_per_arm": 3,
    "expected_phase1_calls": 16,
    "expected_phase2_calls": 102,
    "expected_phase3_calls": 84,
    "expected_total_calls": 202,
}

SUITE_P_PARAMS = {
    "num_trials": 2,
    "mutations_per_arm": 3,
    "expected_phase1_calls": 8,
    "expected_phase2_calls": 54,
    "expected_phase3_calls": 24,
    "expected_total_calls": 86,
}


class CallRecord:
    """Record of a single model call and its evaluation outcome."""
    __slots__ = (
        'call_id', 'call_type', 'arm', 'phase', 'task_id',
        'replicate', 'trio_slot', 'trio_position', 'tokens',
        'earned', 'max_score', 'round_index', 'prompt_hash', 'error_trace',
        'was_improving', 'is_valid',
    )

    def __init__(
        self, call_id: str, call_type: str, arm: str, phase: int,
        task_id: str = "", replicate: int = -1, trio_slot: str = "",
        trio_position: int = -1, tokens: int = 0, earned: int = -1,
        max_score: int = -1, round_index: int = -1, prompt_hash: str = "",
        error_trace: str = "", was_improving: bool = False, is_valid: bool = True,
    ):
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
        self.round_index = round_index
        self.prompt_hash = prompt_hash
        self.error_trace = error_trace
        self.was_improving = was_improving
        self.is_valid = is_valid

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}


class TrialResult:
    """Complete result of a single trial."""

    def __init__(self, trial_index: int, trial_id: str, arm_sign: int):
        self.trial_index = trial_index
        self.trial_id = trial_id
        self.arm_sign = arm_sign
        self.call_records: List[CallRecord] = []
        self.phase1_scores: Dict[str, List[Tuple[int, int]]] = {}
        self.phase2_arm_scores: Dict[str, Dict[str, List[Tuple[int, int]]]] = {
            "slot_a": {},
            "slot_b": {},
        }
        self.phase2_winner: str = ""
        self.phase3_trio_scores: Dict[str, Dict[str, List[Tuple[int, int]]]] = {
            "baseline": {},
            "slot_a_winner": {},
            "slot_b_winner": {},
        }
        self.slot_a_mutable_body: str = ""
        self.slot_b_mutable_body: str = ""
        self.optimizer_mutable_body: str = ""
        self.control_mutable_body: str = ""
        self.best_opt_depth: int = 0
        self.total_tokens: int = 0
        self.started_at: float = 0.0
        self.finished_at: float = 0.0

    def model_call_count(self) -> int:
        return len([r for r in self.call_records if r.call_type in ("generate", "mutate")])

    def to_dict(self) -> dict:
        return {
            "trial_index": self.trial_index,
            "trial_id": self.trial_id,
            "arm_sign": self.arm_sign,
            "call_records": [r.to_dict() for r in self.call_records],
            "phase1_scores": {k: [list(p) for p in v] for k, v in self.phase1_scores.items()},
            "phase2_arm_scores": {
                arm: {k: [list(p) for p in v] for k, v in tasks.items()}
                for arm, tasks in self.phase2_arm_scores.items()
            },
            "phase2_winner": self.phase2_winner,
            "phase3_trio_scores": {
                slot: {k: [list(p) for p in v] for k, v in tasks.items()}
                for slot, tasks in self.phase3_trio_scores.items()
            },
            "slot_a_mutable_body": self.slot_a_mutable_body,
            "slot_b_mutable_body": self.slot_b_mutable_body,
            "optimizer_mutable_body": self.optimizer_mutable_body,
            "control_mutable_body": self.control_mutable_body,
            "best_opt_depth": self.best_opt_depth,
            "total_tokens": self.total_tokens,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TrialResult":
        res = cls(data["trial_index"], data["trial_id"], data["arm_sign"])
        res.phase2_winner = data.get("phase2_winner", "")
        res.slot_a_mutable_body = data.get("slot_a_mutable_body", "")
        res.slot_b_mutable_body = data.get("slot_b_mutable_body", "")
        res.optimizer_mutable_body = data.get("optimizer_mutable_body", "")
        res.control_mutable_body = data.get("control_mutable_body", "")
        res.best_opt_depth = data.get("best_opt_depth", 0)
        res.total_tokens = data.get("total_tokens", 0)
        res.started_at = data.get("started_at", 0.0)
        res.finished_at = data.get("finished_at", 0.0)

        res.phase1_scores = {
            k: [tuple(p) for p in v]
            for k, v in data.get("phase1_scores", {}).items()
        }
        res.phase2_arm_scores = {
            arm: {k: [tuple(p) for p in v] for k, v in tasks.items()}
            for arm, tasks in data.get("phase2_arm_scores", {}).items()
        }
        res.phase3_trio_scores = {
            slot: {k: [tuple(p) for p in v] for k, v in tasks.items()}
            for slot, tasks in data.get("phase3_trio_scores", {}).items()
        }
        for r_dict in data.get("call_records", []):
            rec = CallRecord(
                call_id=r_dict.get("call_id", ""),
                call_type=r_dict.get("call_type", ""),
                arm=r_dict.get("arm", ""),
                phase=r_dict.get("phase", 0),
                task_id=r_dict.get("task_id", ""),
                replicate=r_dict.get("replicate", -1),
                trio_slot=r_dict.get("trio_slot", ""),
                trio_position=r_dict.get("trio_position", -1),
                tokens=r_dict.get("tokens", 0),
                earned=r_dict.get("earned", -1),
                max_score=r_dict.get("max_score", -1),
                round_index=r_dict.get("round_index", -1),
                prompt_hash=r_dict.get("prompt_hash", ""),
                error_trace=r_dict.get("error_trace", ""),
                was_improving=r_dict.get("was_improving", False),
                is_valid=r_dict.get("is_valid", True),
            )
            res.call_records.append(rec)
        return res


class ExploratoryTracker:
    """Tracks the 4 pre-registered exploratory secondary metrics."""

    def __init__(self):
        self.optimizer_mutations_total = 0
        self.score_improving_mutations = 0
        self.total_output_tokens = 0
        self.valid_candidates = 0
        self.total_candidates = 0
        self.best_depth_per_trial: List[int] = []

    def record_optimizer_mutation(self, improved: bool, tokens: int = 0):
        self.optimizer_mutations_total += 1
        if improved:
            self.score_improving_mutations += 1
        if tokens > 0:
            self.total_output_tokens += tokens

    def record_control_mutation(self, tokens: int = 0):
        if tokens > 0:
            self.total_output_tokens += tokens

    def record_generation(self, tokens: int, valid: bool):
        if tokens > 0:
            self.total_output_tokens += tokens
        self.total_candidates += 1
        if valid:
            self.valid_candidates += 1

    def record_best_depth(self, depth: int):
        self.best_depth_per_trial.append(depth)

    def summary(self) -> dict:
        return {
            "score_improving_mutations_per_call": (
                f"{self.score_improving_mutations}/{self.optimizer_mutations_total}"
                if self.optimizer_mutations_total > 0 else "0/0"
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
    arm assignment randomization, and prompt envelope protection.
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
        self.envelope = PromptEnvelope(master_registration_data=registration)

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
        if self.ledger and hasattr(self.ledger, "_max_call_counter") and self.ledger._max_call_counter > 0:
            self.call_counter = self.ledger._max_call_counter
        else:
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
        call_id = self._next_call_id(f"gen_{arm}_{task_id}_r{replicate}")
        effective_prompt, prompt_hash = self.envelope.build_effective_prompt(mutable_body)

        max_attempts = 3
        code, tokens = "", 0
        for attempt in range(1, max_attempts + 1):
            if self.ledger:
                self.ledger.record_intent(call_id, "generate", arm, phase, task_id, {"prompt_hash": prompt_hash, "attempt": attempt})
            try:
                code, tokens = self.model.generate_candidate(task_id, effective_prompt, call_id)
                if self.ledger:
                    self.ledger.record_completion(call_id, True, tokens)
                break
            except Exception as e:
                bytes_sent = getattr(e, "bytes_sent", None)
                bytes_received = getattr(e, "bytes_received", None)
                fault = classify_call_fault(e, bytes_sent=bytes_sent, bytes_received=bytes_received)
                if self.ledger:
                    self.ledger.record_completion(call_id, False, 0, fault=fault)
                if fault.retryable and attempt < max_attempts:
                    time.sleep(0.1 * (2 ** (attempt - 1)))
                    continue
                raise OrchestratorError(f"Generation call {call_id} failed on attempt {attempt}: {e}") from e

        evaluation = self.evaluator.evaluate(task_id, code)
        if isinstance(evaluation, tuple) and len(evaluation) == 4:
            earned, max_s, is_valid, error_trace = evaluation
        elif isinstance(evaluation, tuple) and len(evaluation) == 2:
            earned, max_s = evaluation
            is_valid = True
            error_trace = ""
        elif isinstance(evaluation, tuple) and len(evaluation) == 3:
            earned, max_s, is_valid = evaluation
            error_trace = ""
        else:
            raise OrchestratorError(
                "Task evaluator must return (earned: int, max_s: int, is_valid: bool, error_trace: str)"
            )

        if not isinstance(is_valid, bool):
            raise OrchestratorError(f"is_valid field must be a boolean, got {type(is_valid)}")

        earned = int(earned)
        max_s = int(max_s)
        error_trace = str(error_trace)

        self.exploratory.record_generation(tokens, is_valid)

        record = CallRecord(
            call_id=call_id, call_type="generate", arm=arm, phase=phase,
            task_id=task_id, replicate=replicate, trio_slot=trio_slot,
            trio_position=trio_position, tokens=tokens, earned=earned,
            max_score=max_s, round_index=round_index, prompt_hash=prompt_hash,
            error_trace=error_trace, is_valid=is_valid,
        )
        return earned, max_s, tokens, record

    def _mutate_prompt_with_envelope(
        self, mutable_body: str, failure_context: str, arm: str, phase: int,
        round_index: int,
    ) -> Tuple[str, int, CallRecord]:
        call_id = self._next_call_id(f"mut_{arm}_r{round_index}")

        max_attempts = 3
        new_body, tokens = "", 0
        for attempt in range(1, max_attempts + 1):
            if self.ledger:
                self.ledger.record_intent(call_id, "mutate", arm, phase, metadata={"round_index": round_index, "attempt": attempt})
            try:
                new_body, tokens = self.model.mutate_prompt(mutable_body, failure_context, call_id)
                new_body = self.envelope.validate_mutation_output(new_body)
                if self.ledger:
                    self.ledger.record_completion(call_id, True, tokens)
                break
            except Exception as e:
                bytes_sent = getattr(e, "bytes_sent", None)
                bytes_received = getattr(e, "bytes_received", None)
                fault = classify_call_fault(e, bytes_sent=bytes_sent, bytes_received=bytes_received)
                if self.ledger:
                    self.ledger.record_completion(call_id, False, 0, fault=fault)
                if fault.retryable and attempt < max_attempts:
                    time.sleep(0.1 * (2 ** (attempt - 1)))
                    continue
                raise OrchestratorError(f"Mutation call {call_id} failed on attempt {attempt}: {e}") from e

        record = CallRecord(
            call_id=call_id, call_type="mutate", arm=arm, phase=phase,
            tokens=tokens, round_index=round_index,
        )
        return new_body, tokens, record

    def run_trial(self, trial_index: int) -> TrialResult:
        """Execute a single trial with equal-task weighted winner selection and counterbalanced arm scheduling."""
        arm_sign = self.arm_vectors[trial_index]
        trial_id = f"trial_{trial_index + 1}"
        result = TrialResult(trial_index, trial_id, arm_sign)
        result.started_at = time.time()

        baseline_body = "# Initial default strategy body\n"
        k = 2

        # ---- PHASE 1: Baseline evaluation on opt/sel tasks ----
        phase1_call_count = 0
        for task_id in self.opt_sel_tasks:
            result.phase1_scores[task_id] = []
            for rep in range(k):
                earned, max_s, tokens, record = self._generate_and_evaluate(
                    task_id, baseline_body, arm="baseline", phase=1, replicate=rep,
                )
                result.phase1_scores[task_id].append((earned, max_s))
                result.call_records.append(record)
                result.total_tokens += tokens
                phase1_call_count += 1

        if phase1_call_count != self.params["expected_phase1_calls"]:
            raise CallArithmeticError(
                f"Trial {trial_id} Phase 1: expected {self.params['expected_phase1_calls']} "
                f"calls, got {phase1_call_count}"
            )

        # ---- EQUAL-TASK WEIGHTED BASELINE FRACTION COMPUTATION ----
        # Calculate task mean for each task, then take unweighted average across tasks
        p1_task_means = [
            sum(Fraction(e, m) for e, m in result.phase1_scores[t]) / Fraction(k)
            for t in self.opt_sel_tasks
        ]
        initial_baseline_frac = sum(p1_task_means) / Fraction(len(p1_task_means))

        # ---- PHASE 2: Strategy Search (Counterbalanced Arm Scheduling) ----
        # Arm execution order: slot_a runs 1st, slot_b runs 2nd.
        # Under arm_sign == +1: slot_a = Optimizer, slot_b = Control (Optimizer 1st)
        # Under arm_sign == -1: slot_a = Control, slot_b = Optimizer (Control 1st)
        slots = ["slot_a", "slot_b"]

        phase2_call_count = 0
        best_opt_body = baseline_body
        best_ctrl_body = baseline_body

        best_opt_frac = initial_baseline_frac
        best_ctrl_frac = initial_baseline_frac
        best_opt_depth = 0

        slot_bodies = {"slot_a": baseline_body, "slot_b": baseline_body}
        slot_scores = {
            "slot_a": dict(result.phase1_scores),
            "slot_b": dict(result.phase1_scores),
        }

        for slot in slots:
            is_optimizer_arm = (slot == "slot_a" and arm_sign == 1) or (slot == "slot_b" and arm_sign == -1)
            current_body = baseline_body
            current_best_frac = initial_baseline_frac
            last_round_scores = dict(result.phase1_scores)
            last_round_errors = {
                task_id: [
                    rec.error_trace for rec in result.call_records
                    if rec.phase == 1 and rec.task_id == task_id and rec.error_trace
                ]
                for task_id in self.opt_sel_tasks
            }

            for rnd in range(1, self.params["mutations_per_arm"] + 1):
                if is_optimizer_arm:
                    failed = []
                    for task_id, pairs in last_round_scores.items():
                        earned_total = sum(e for e, _m in pairs)
                        max_total = sum(m for _e, m in pairs)
                        if earned_total < max_total:
                            traces = last_round_errors.get(task_id, [])
                            trace_text = " | ".join(traces) if traces else "no evaluator trace supplied"
                            failed.append(
                                f"{task_id}: score {earned_total}/{max_total}; errors: {trace_text}"
                            )
                    fail_ctx = (
                        f"Round {rnd-1} feedback (aggregate {current_best_frac:.4f}):\n"
                        + "\n".join(failed)
                    )
                else:
                    # Control arm receives registered CONTROL_REFORMULATION_TEXT
                    fail_ctx = self.envelope.control_reformulation_text

                new_body, mut_tokens, mut_record = self._mutate_prompt_with_envelope(
                    current_body, fail_ctx, arm=slot, phase=2, round_index=rnd,
                )
                result.call_records.append(mut_record)
                result.total_tokens += mut_tokens
                phase2_call_count += 1

                rnd_scores = {}
                rnd_errors = {}
                for task_id in self.opt_sel_tasks:
                    rnd_scores[task_id] = []
                    rnd_errors[task_id] = []
                    for rep in range(k):
                        earned, max_s, tokens, record = self._generate_and_evaluate(
                            task_id, new_body, arm=slot, phase=2, replicate=rep, round_index=rnd,
                        )
                        rnd_scores[task_id].append((earned, max_s))
                        result.call_records.append(record)
                        if record.error_trace:
                            rnd_errors[task_id].append(record.error_trace)
                        result.total_tokens += tokens
                        phase2_call_count += 1

                last_round_scores = rnd_scores
                last_round_errors = rnd_errors

                # EQUAL-TASK WEIGHTED ROUND EVALUATION
                rnd_task_means = [
                    sum(Fraction(e, m) for e, m in rnd_scores[t]) / Fraction(k)
                    for t in self.opt_sel_tasks
                ]
                rnd_frac = sum(rnd_task_means) / Fraction(len(rnd_task_means))

                if is_optimizer_arm:
                    improved = rnd_frac > current_best_frac
                    mut_record.was_improving = improved
                    self.exploratory.record_optimizer_mutation(improved, mut_tokens)
                    if improved:
                        current_best_frac = rnd_frac
                        current_body = new_body
                        best_opt_body = new_body
                        best_opt_frac = rnd_frac
                        best_opt_depth = rnd
                        slot_scores[slot] = rnd_scores
                else:
                    mut_record.was_improving = False
                    self.exploratory.record_control_mutation(mut_tokens)
                    if rnd_frac > current_best_frac:
                        current_best_frac = rnd_frac
                        current_body = new_body
                        best_ctrl_body = new_body
                        best_ctrl_frac = rnd_frac
                        slot_scores[slot] = rnd_scores

                slot_bodies[slot] = current_body

        if phase2_call_count != self.params["expected_phase2_calls"]:
            raise CallArithmeticError(
                f"Trial {trial_id} Phase 2: expected {self.params['expected_phase2_calls']} "
                f"calls, got {phase2_call_count}"
            )

        if best_opt_frac >= best_ctrl_frac:
            result.phase2_winner = "slot_a" if arm_sign == 1 else "slot_b"
        else:
            result.phase2_winner = "slot_b" if arm_sign == 1 else "slot_a"

        result.slot_a_mutable_body = slot_bodies["slot_a"]
        result.slot_b_mutable_body = slot_bodies["slot_b"]

        if arm_sign == 1:
            result.optimizer_mutable_body = slot_bodies["slot_a"]
            result.control_mutable_body = slot_bodies["slot_b"]
        else:
            result.optimizer_mutable_body = slot_bodies["slot_b"]
            result.control_mutable_body = slot_bodies["slot_a"]

        self.exploratory.record_best_depth(best_opt_depth)
        result.best_opt_depth = best_opt_depth
        result.phase2_arm_scores["slot_a"] = slot_scores["slot_a"]
        result.phase2_arm_scores["slot_b"] = slot_scores["slot_b"]

        # ---- PHASE 3: Post-selection microblock trios on conf+holdout ----
        trio_bodies = {
            "baseline": baseline_body,
            "slot_a_winner": slot_bodies["slot_a"],
            "slot_b_winner": slot_bodies["slot_b"],
        }

        phase3_call_count = 0

        for task_idx, task_id in enumerate(self.conf_holdout_tasks):
            for rep in range(k):
                ordering_idx = get_trio_ordering_index(self.trio_seed, task_idx, rep)
                ordering = self.trio_orderings[ordering_idx]

                trio_records = []
                for position, slot_name in enumerate(ordering):
                    body = trio_bodies[slot_name]
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
        Execute trials with exact-head durability resumption support.
        Reconstructs exploratory metrics state from loaded trial results.
        """
        start_trial_index = 0
        if self.checkpoint_mgr:
            start_trial_index = self.checkpoint_mgr.get_resume_trial_index()
            completed = self.checkpoint_mgr.get_completed_results()
            for res_dict in completed:
                if isinstance(res_dict, dict) and "trial_index" in res_dict:
                    t_res = TrialResult.from_dict(res_dict)
                    if not any(r.trial_index == t_res.trial_index for r in self.trial_results):
                        self.trial_results.append(t_res)

                        self.exploratory.record_best_depth(t_res.best_opt_depth)
                        opt_slot = "slot_a" if t_res.arm_sign == 1 else "slot_b"
                        for r in t_res.call_records:
                            if r.call_type == "generate":
                                self.exploratory.record_generation(r.tokens, getattr(r, "is_valid", True))
                            elif r.call_type == "mutate":
                                if r.arm == opt_slot:
                                    was_imp = getattr(r, "was_improving", False)
                                    self.exploratory.record_optimizer_mutation(was_imp, r.tokens)
                                else:
                                    self.exploratory.record_control_mutation(r.tokens)

        num_trials = self.params["num_trials"]
        results = list(self.trial_results)

        for trial_idx in range(start_trial_index, num_trials):
            if self.checkpoint_mgr:
                self.checkpoint_mgr.seal_pre_trial_intent(trial_idx, {"arm_sign": self.arm_vectors[trial_idx]})

            result = self.run_trial(trial_idx)
            results.append(result)

            if self.checkpoint_mgr:
                self.checkpoint_mgr.commit_checkpoint_head(trial_idx, result.to_dict())

        return results

    def verify_trio_adjacency(self) -> bool:
        """Verify that microblock trios in Phase 3 are strictly adjacent in call sequence."""
        for result in self.trial_results:
            p3_calls = [r for r in result.call_records if r.phase == 3]
            if len(p3_calls) % 3 != 0:
                raise TrioAdjacencyError(f"Trial {result.trial_id} Phase 3 call count {len(p3_calls)} is not a multiple of 3")
            for i in range(0, len(p3_calls), 3):
                trio = p3_calls[i:i+3]
                slots = {r.trio_slot for r in trio}
                if len(slots) != 3:
                    raise TrioAdjacencyError(f"Trio at index {i} in trial {result.trial_id} does not contain 3 distinct slots: {slots}")
        return True

    def compute_outcome(self) -> Dict[str, Any]:
        """Compute Schema 3 experiment outcome with equal-task weighted exact rational arithmetic."""
        if not self.trial_results:
            raise OrchestratorError("No trials have been executed")

        opt_sel_baseline_fracs = []
        conf_baseline_fracs = []
        conf_holdout_baseline_fracs = []

        opt_sel_opt_fracs = []
        opt_sel_ctrl_fracs = []

        conf_holdout_opt_fracs = []
        conf_holdout_ctrl_fracs = []

        all_16_opt_sel_fracs = []
        conf_task_set = set(self.conf_tasks)
        family_samples = {
            f"F{index}": {"task_ids": set(), "optimizer_samples": [], "control_samples": []}
            for index in range(1, 5)
        }

        def family_of(task_id: str) -> str:
            match = re.match(r"^[cp]_f([1-4])_", task_id)
            if not match:
                raise OrchestratorError(f"Task id does not encode a structural family: {task_id}")
            return f"F{match.group(1)}"

        for result in self.trial_results:
            arm_sign = result.arm_sign

            # Phase 1: Opt/Sel Baseline
            for task_id, pairs in result.phase1_scores.items():
                for e, m in pairs:
                    frac = Fraction(e, m)
                    opt_sel_baseline_fracs.append(frac)
                    all_16_opt_sel_fracs.append(frac)

            # Phase 2: Opt/Sel Arm Scores
            for slot_name in ("slot_a", "slot_b"):
                is_opt = (slot_name == "slot_a" and arm_sign == 1) or (slot_name == "slot_b" and arm_sign == -1)
                target_list = opt_sel_opt_fracs if is_opt else opt_sel_ctrl_fracs
                slot_scores = result.phase2_arm_scores.get(slot_name, {})
                for task_id, pairs in slot_scores.items():
                    for e, m in pairs:
                        frac = Fraction(e, m)
                        target_list.append(frac)
                        family = family_of(task_id)
                        family_samples[family]["task_ids"].add(task_id)
                        role = "optimizer_samples" if is_opt else "control_samples"
                        family_samples[family][role].append(frac)

            # Phase 3: Confirmatory & Holdout Trio Scores
            for slot_name in ("baseline", "slot_a_winner", "slot_b_winner"):
                slot_scores = result.phase3_trio_scores.get(slot_name, {})
                for task_id, pairs in slot_scores.items():
                    for e, m in pairs:
                        frac = Fraction(e, m)
                        if slot_name == "baseline":
                            conf_holdout_baseline_fracs.append(frac)
                            if task_id in conf_task_set:
                                conf_baseline_fracs.append(frac)
                        else:
                            is_opt = (slot_name == "slot_a_winner" and arm_sign == 1) or (slot_name == "slot_b_winner" and arm_sign == -1)
                            target_list = conf_holdout_opt_fracs if is_opt else conf_holdout_ctrl_fracs
                            target_list.append(frac)
                            family = family_of(task_id)
                            family_samples[family]["task_ids"].add(task_id)
                            role = "optimizer_samples" if is_opt else "control_samples"
                            family_samples[family][role].append(frac)

        def mean_fraction(fracs: List[Fraction]) -> Fraction:
            if not fracs:
                return Fraction(0)
            return sum(fracs) / Fraction(len(fracs))

        opt_sel_baseline_mean = mean_fraction(opt_sel_baseline_fracs)
        conf_baseline_mean = mean_fraction(conf_baseline_fracs)
        conf_holdout_baseline_mean = mean_fraction(conf_holdout_baseline_fracs)
        total_baseline_mean = mean_fraction(opt_sel_baseline_fracs + conf_holdout_baseline_fracs)

        opt_sel_opt_mean = mean_fraction(opt_sel_opt_fracs)
        opt_sel_ctrl_mean = mean_fraction(opt_sel_ctrl_fracs)

        conf_holdout_opt_mean = mean_fraction(conf_holdout_opt_fracs)
        conf_holdout_ctrl_mean = mean_fraction(conf_holdout_ctrl_fracs)

        delta_opt_minus_ctrl = conf_holdout_opt_mean - conf_holdout_ctrl_mean
        all_16_opt_sel_perfect = all(f == Fraction(1) for f in all_16_opt_sel_fracs) if all_16_opt_sel_fracs else False

        tier_tasks = {
            "optimization": self.opt_tasks,
            "selection": self.sel_tasks,
            "confirmatory": self.conf_tasks,
            "structural_holdout": self.holdout_tasks,
        }

        def serialize_samples(pairs):
            return [str(Fraction(e, m)) for e, m in pairs]

        per_trial_outcomes = []
        for r in self.trial_results:
            opt_slot = "slot_a" if r.arm_sign == 1 else "slot_b"
            ctrl_slot = "slot_b" if r.arm_sign == 1 else "slot_a"
            opt_trio = f"{opt_slot}_winner"
            ctrl_trio = f"{ctrl_slot}_winner"
            tier_evidence = {}
            for tier, tasks in tier_tasks.items():
                task_evidence = {}
                task_deltas = []
                for task_id in tasks:
                    if tier in ("optimization", "selection"):
                        baseline_pairs = r.phase1_scores.get(task_id, [])
                        opt_pairs = r.phase2_arm_scores.get(opt_slot, {}).get(task_id, [])
                        ctrl_pairs = r.phase2_arm_scores.get(ctrl_slot, {}).get(task_id, [])
                    else:
                        baseline_pairs = r.phase3_trio_scores.get("baseline", {}).get(task_id, [])
                        opt_pairs = r.phase3_trio_scores.get(opt_trio, {}).get(task_id, [])
                        ctrl_pairs = r.phase3_trio_scores.get(ctrl_trio, {}).get(task_id, [])
                    opt_mean = mean_fraction([Fraction(e, m) for e, m in opt_pairs])
                    ctrl_mean = mean_fraction([Fraction(e, m) for e, m in ctrl_pairs])
                    delta = opt_mean - ctrl_mean
                    task_deltas.append(delta)
                    task_evidence[task_id] = {
                        "family": family_of(task_id),
                        "baseline_k_samples": serialize_samples(baseline_pairs),
                        "optimizer_k_samples": serialize_samples(opt_pairs),
                        "control_k_samples": serialize_samples(ctrl_pairs),
                        "optimizer_mean": str(opt_mean),
                        "control_mean": str(ctrl_mean),
                        "delta_opt_minus_ctrl": str(delta),
                    }
                tier_evidence[tier] = {
                    "tasks": task_evidence,
                    "delta_opt_minus_ctrl": str(mean_fraction(task_deltas)),
                }

            control_records = []
            for record in r.call_records:
                is_phase2_control = record.phase == 2 and record.arm == ctrl_slot
                is_phase3_control = record.phase == 3 and record.trio_slot == ctrl_trio
                if is_phase2_control or is_phase3_control:
                    control_records.append(record.to_dict())
            per_trial_outcomes.append({
                "trial_index": r.trial_index,
                "trial_id": r.trial_id,
                "arm_sign": r.arm_sign,
                "optimizer_slot": opt_slot,
                "control_slot": ctrl_slot,
                "phase2_winner": r.phase2_winner,
                "total_tokens": r.total_tokens,
                "call_count": r.model_call_count(),
                "tiers": tier_evidence,
                "control_records": control_records,
            })

        family_scores = {}
        for family, samples in family_samples.items():
            family_scores[family] = {
                "task_ids": sorted(samples["task_ids"]),
                "optimizer_samples": [str(value) for value in samples["optimizer_samples"]],
                "control_samples": [str(value) for value in samples["control_samples"]],
            }

        return {
            "suite": self.suite,
            "num_trials": len(self.trial_results),
            "opt_sel_baseline_aggregate": str(opt_sel_baseline_mean),
            "conf_baseline_aggregate": str(conf_baseline_mean),
            "conf_holdout_baseline_aggregate": str(conf_holdout_baseline_mean),
            "baseline_aggregate": str(total_baseline_mean),
            "opt_sel_opt_aggregate": str(opt_sel_opt_mean),
            "opt_sel_ctrl_aggregate": str(opt_sel_ctrl_mean),
            "optimizer_winner_aggregate": str(conf_holdout_opt_mean),
            "control_winner_aggregate": str(conf_holdout_ctrl_mean),
            "delta_opt_minus_ctrl": str(delta_opt_minus_ctrl),
            "delta_positive": delta_opt_minus_ctrl > 0,
            "all_16_opt_sel_perfect": all_16_opt_sel_perfect,
            "raw_fractions": {
                "opt_sel_baseline": opt_sel_baseline_mean,
                "conf_baseline": conf_baseline_mean,
                "opt_sel_opt": opt_sel_opt_mean,
                "opt_sel_ctrl": opt_sel_ctrl_mean,
                "conf_opt": conf_holdout_opt_mean,
                "conf_ctrl": conf_holdout_ctrl_mean,
                "delta": delta_opt_minus_ctrl,
            },
            "per_trial": per_trial_outcomes,
            "family_scores": family_scores,
            "exploratory": self.exploratory.summary(),
        }
