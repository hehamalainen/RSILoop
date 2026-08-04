"""
Campaign 2 Experiment Orchestrator.

Executes the full per-trial experiment loop:
  Phase 1 — Pre-mutation baseline on optimization+selection (16 calls, Suite C)
  Phase 2 — 3 mutation rounds × (2 mutation + 2×opt_sel×k evaluation) (102 calls, Suite C)
  Phase 3 — Post-selection microblock trios on confirmatory+holdout (84 calls, Suite C)
  Total per trial: 202 calls (Suite C), 86 calls (Suite P)

Integrates: model_provider, scorer, registration, tier_assignment,
attempt_lifecycle, durability, reporting, ladder, exploratory.
"""

import os
import sys
import json
import time
import hashlib
from fractions import Fraction
from typing import Dict, Any, List, Tuple, Optional
from datetime import datetime, timezone

from campaign2.model_provider import ModelProvider, MockTaskEvaluator
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

# Prompt contract
IMMUTABLE_CONTRACT = (
    "You are an expert Python coder. Your job is to solve the programming task described in the input.\n"
    "You must output ONLY valid Python code inside a markdown block starting with ```python and ending with ```.\n"
    "Do not write explanations, markdown text, or any formatting outside the code block.\n"
    "Write the code to implement the requested functions or classes exactly as specified.\n"
    "If any exceptions are requested, define them as standard Exception subclasses.\n"
    "Write clean, correct, and self-contained code."
)

DELIMITER = "\n\n---STRATEGY---\n\n"

INITIAL_MUTABLE_BODY = "Follow good software engineering practices. Handle edge cases carefully."

CONTROL_REFORMULATION_TEXT = (
    "Rewrite the strategy section of this prompt using different wording.\n"
    "Do not change the meaning or intent. Keep it concise."
)


class OrchestratorError(RuntimeError):
    """Raised when the orchestrator detects an invariant violation."""
    pass


class CallArithmeticError(OrchestratorError):
    """Raised when per-trial call count does not match the expected arithmetic."""
    pass


class TrioAdjacencyError(OrchestratorError):
    """Raised when a microblock trio is interrupted by an unrelated call."""
    pass


class CallRecord:
    """Record of a single model or evaluation call."""
    __slots__ = ('call_id', 'call_type', 'arm', 'phase', 'task_id',
                 'replicate', 'trio_slot', 'trio_position', 'tokens',
                 'earned', 'max_score', 'timestamp', 'round_index')

    def __init__(self, call_id: str, call_type: str, arm: str, phase: int,
                 task_id: str = "", replicate: int = -1, trio_slot: str = "",
                 trio_position: int = -1, tokens: int = 0, earned: int = -1,
                 max_score: int = -1, round_index: int = -1):
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

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__}


class TrialResult:
    """Complete result of a single trial."""

    def __init__(self, trial_index: int, trial_id: str):
        self.trial_index = trial_index
        self.trial_id = trial_id
        self.call_records: List[CallRecord] = []
        self.phase1_scores: Dict[str, List[Tuple[int, int]]] = {}  # task -> [(earned, max)]
        self.phase2_arm_scores: Dict[str, Dict[str, List[Tuple[int, int]]]] = {
            "optimizer": {},
            "control": {},
        }
        self.phase2_winner: str = ""  # "optimizer" or "control"
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
    Full Campaign 2 experiment engine.

    Executes the complete per-trial loop with:
    - Pluggable model provider (MockModelProvider for testing, SolModelProvider for production)
    - Pluggable task evaluator (MockTaskEvaluator for testing, sandboxed proxy for production)
    - Integrated lifecycle, durability, scoring, reporting, and exploratory tracking
    """

    def __init__(
        self,
        model_provider: ModelProvider,
        task_evaluator: Any,
        tier_assignment: Dict[str, Any],
        registration: Dict[str, Any],
        suite: str = "suite_c",
        workspace_root: str = BASE_DIR,
    ):
        self.model = model_provider
        self.evaluator = task_evaluator

        if suite not in ("suite_c", "suite_p"):
            raise OrchestratorError(f"Unknown suite: {suite}")

        self.suite = suite
        self.params = SUITE_C_PARAMS if suite == "suite_c" else SUITE_P_PARAMS
        self.workspace_root = workspace_root

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

        # State
        self.trial_results: List[TrialResult] = []
        self.call_counter = 0
        self.exploratory = ExploratoryTracker()

        # Validate expected call arithmetic
        k = self.params["k"]
        num_opt_sel = len(self.opt_sel_tasks)
        num_conf_holdout = len(self.conf_holdout_tasks)
        mr = self.params["mutation_rounds"]

        expected_p1 = num_opt_sel * k
        expected_p2 = mr * (2 + num_opt_sel * 2 * k)
        expected_p3 = num_conf_holdout * k * 3

        if expected_p1 != self.params["expected_phase1_calls"]:
            raise OrchestratorError(
                f"Phase 1 call arithmetic mismatch: computed {expected_p1}, "
                f"expected {self.params['expected_phase1_calls']}"
            )
        if expected_p2 != self.params["expected_phase2_calls"]:
            raise OrchestratorError(
                f"Phase 2 call arithmetic mismatch: computed {expected_p2}, "
                f"expected {self.params['expected_phase2_calls']}"
            )
        if expected_p3 != self.params["expected_phase3_calls"]:
            raise OrchestratorError(
                f"Phase 3 call arithmetic mismatch: computed {expected_p3}, "
                f"expected {self.params['expected_phase3_calls']}"
            )

    def _next_call_id(self, prefix: str) -> str:
        self.call_counter += 1
        return f"{prefix}_{self.call_counter:04d}"

    def _make_effective_prompt(self, mutable_body: str) -> str:
        """Compose the full effective prompt from immutable contract + delimiter + mutable body."""
        return IMMUTABLE_CONTRACT + DELIMITER + mutable_body

    def _generate_and_evaluate(
        self, task_id: str, effective_prompt: str, arm: str, phase: int,
        replicate: int, trio_slot: str = "", trio_position: int = -1,
        round_index: int = -1,
    ) -> Tuple[int, int, int]:
        """Generate candidate code and evaluate it. Returns (earned, max, tokens)."""
        call_id = self._next_call_id(f"gen_{arm}_{task_id}_r{replicate}")
        code, tokens = self.model.generate_candidate(task_id, effective_prompt, call_id)
        earned, max_s = self.evaluator.evaluate(task_id, code)

        self.exploratory.record_generation(tokens, earned > 0)

        record = CallRecord(
            call_id=call_id, call_type="generate", arm=arm, phase=phase,
            task_id=task_id, replicate=replicate, trio_slot=trio_slot,
            trio_position=trio_position, tokens=tokens, earned=earned,
            max_score=max_s, round_index=round_index,
        )
        return earned, max_s, tokens, record

    def _evaluate_prompt_on_tasks(
        self, tasks: List[str], mutable_body: str, arm: str, phase: int,
        round_index: int = -1,
    ) -> Tuple[Dict[str, List[Tuple[int, int]]], List[CallRecord], int]:
        """
        Evaluate a prompt on all tasks with k replicates.
        Returns (task_scores, call_records, total_tokens).
        """
        effective_prompt = self._make_effective_prompt(mutable_body)
        k = self.params["k"]
        scores: Dict[str, List[Tuple[int, int]]] = {}
        records: List[CallRecord] = []
        total_tokens = 0

        for task_id in tasks:
            task_pairs = []
            for rep in range(k):
                earned, max_s, tokens, record = self._generate_and_evaluate(
                    task_id, effective_prompt, arm, phase, rep,
                    round_index=round_index,
                )
                task_pairs.append((earned, max_s))
                records.append(record)
                total_tokens += tokens
            scores[task_id] = task_pairs

        return scores, records, total_tokens

    def _compute_aggregate_score(self, task_scores: Dict[str, List[Tuple[int, int]]]) -> Fraction:
        """Compute equal-weighted aggregate across tasks, mean over k within each task."""
        task_means = []
        for task_id in sorted(task_scores.keys()):
            pairs = task_scores[task_id]
            mean = task_mean_over_k(pairs)
            task_means.append(mean)
        return aggregate_tasks_equal_weight(task_means)

    def _collect_failure_traces(
        self, task_scores: Dict[str, List[Tuple[int, int]]], mutable_body: str
    ) -> str:
        """Collect failure context for optimizer mutation. Tasks with any replicate scoring < max."""
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
        """Execute a complete trial with Phase 1, 2, and 3."""
        trial_id = f"trial_{trial_index + 1}"
        result = TrialResult(trial_index, trial_id)
        result.started_at = time.time()

        k = self.params["k"]
        mr = self.params["mutation_rounds"]
        arm_sign = self.arm_vectors[trial_index]  # +1 or -1

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

        for round_idx in range(mr):
            # Optimizer mutation: receives failure traces
            failure_ctx = self._collect_failure_traces(phase1_scores if round_idx == 0 else opt_scores, opt_body)
            opt_mut_call_id = self._next_call_id(f"mut_opt_r{round_idx}")
            new_opt_body, opt_mut_tokens = self.model.mutate_prompt(opt_body, failure_ctx, opt_mut_call_id)
            opt_mut_record = CallRecord(
                call_id=opt_mut_call_id, call_type="mutate", arm="optimizer",
                phase=2, round_index=round_idx, tokens=opt_mut_tokens,
            )
            result.call_records.append(opt_mut_record)
            result.total_tokens += opt_mut_tokens
            phase2_call_count += 1

            # Control mutation: receives NO failures, only generic reformulation text
            ctrl_mut_call_id = self._next_call_id(f"mut_ctrl_r{round_idx}")
            new_ctrl_body, ctrl_mut_tokens = self.model.mutate_prompt(ctrl_body, CONTROL_REFORMULATION_TEXT, ctrl_mut_call_id)
            ctrl_mut_record = CallRecord(
                call_id=ctrl_mut_call_id, call_type="mutate", arm="control",
                phase=2, round_index=round_idx, tokens=ctrl_mut_tokens,
            )
            result.call_records.append(ctrl_mut_record)
            result.total_tokens += ctrl_mut_tokens
            phase2_call_count += 1

            # Evaluate optimizer mutant on opt+sel
            opt_scores, opt_records, opt_tokens = self._evaluate_prompt_on_tasks(
                self.opt_sel_tasks, new_opt_body, "optimizer", phase=2,
                round_index=round_idx,
            )
            result.call_records.extend(opt_records)
            result.total_tokens += opt_tokens
            phase2_call_count += len(opt_records)

            opt_score = self._compute_aggregate_score(opt_scores)
            self.exploratory.record_mutation_outcome(opt_score > best_opt_score)
            if opt_score > best_opt_score:
                best_opt_score = opt_score
                best_opt_body = new_opt_body
                best_opt_depth = round_idx + 1
            opt_body = new_opt_body  # Always continue from latest mutation

            # Evaluate control mutant on opt+sel
            ctrl_scores, ctrl_records, ctrl_tokens = self._evaluate_prompt_on_tasks(
                self.opt_sel_tasks, new_ctrl_body, "control", phase=2,
                round_index=round_idx,
            )
            result.call_records.extend(ctrl_records)
            result.total_tokens += ctrl_tokens
            phase2_call_count += len(ctrl_records)

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

        # Winner selection: arm with higher aggregate on opt+sel
        if best_opt_score >= best_ctrl_score:
            result.phase2_winner = "optimizer"
        else:
            result.phase2_winner = "control"

        result.optimizer_mutable_body = best_opt_body
        result.control_mutable_body = best_ctrl_body
        self.exploratory.record_best_depth(best_opt_depth)

        # Store Phase 2 arm scores (last round scores for reporting)
        result.phase2_arm_scores["optimizer"] = opt_scores
        result.phase2_arm_scores["control"] = ctrl_scores

        # ---- PHASE 3: Post-selection microblock trios on conf+holdout ----
        # Map arm_sign to slot assignments
        # +1: optimizer = slot A, control = slot B
        # -1: optimizer = slot B, control = slot A
        winner_body = best_opt_body if result.phase2_winner == "optimizer" else best_ctrl_body
        loser_body = best_ctrl_body if result.phase2_winner == "optimizer" else best_opt_body

        slot_bodies = {
            "baseline": baseline_body,
            "optimizer_winner": best_opt_body,
            "control_winner": best_ctrl_body,
        }

        phase3_call_count = 0

        for task_idx, task_id in enumerate(self.conf_holdout_tasks):
            for rep in range(k):
                # Determine trio ordering
                ordering_idx = get_trio_ordering_index(self.trio_seed, task_idx, rep)
                ordering = self.trio_orderings[ordering_idx]

                trio_records = []
                for position, slot_name in enumerate(ordering):
                    body = slot_bodies[slot_name]
                    effective_prompt = self._make_effective_prompt(body)
                    call_id = self._next_call_id(f"trio_{slot_name}_{task_id}_r{rep}")
                    code, tokens = self.model.generate_candidate(task_id, effective_prompt, call_id)
                    earned, max_s = self.evaluator.evaluate(task_id, code)

                    self.exploratory.record_generation(tokens, earned > 0)

                    record = CallRecord(
                        call_id=call_id, call_type="generate", arm=slot_name,
                        phase=3, task_id=task_id, replicate=rep,
                        trio_slot=slot_name, trio_position=position,
                        tokens=tokens, earned=earned, max_score=max_s,
                    )
                    trio_records.append(record)
                    result.total_tokens += tokens

                    if task_id not in result.phase3_trio_scores[slot_name]:
                        result.phase3_trio_scores[slot_name][task_id] = []
                    result.phase3_trio_scores[slot_name][task_id].append((earned, max_s))

                # Verify trio adjacency: all 3 records must be sequential
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

        # Verify total call count
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
        """Execute all trials for the suite."""
        num_trials = self.params["num_trials"]
        results = []

        for trial_idx in range(num_trials):
            result = self.run_trial(trial_idx)
            results.append(result)

        return results

    def compute_outcome(self) -> Dict[str, Any]:
        """
        Compute the experiment outcome from all trial results.
        Returns a Schema 3 outcome dict with:
        - per-trial scores for each slot (baseline, optimizer_winner, control_winner)
        - aggregate scores per slot
        - delta (optimizer_winner - control_winner)
        - exploratory metrics
        """
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
            "baseline_aggregate": str(baseline_mean),
            "optimizer_winner_aggregate": str(opt_mean),
            "control_winner_aggregate": str(ctrl_mean),
            "delta_opt_minus_ctrl": str(delta),
            "delta_positive": delta > 0,
            "per_trial": [
                {
                    "trial_id": r.trial_id,
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
        """
        Verify that all Phase 3 microblock trios are adjacent in the call log.
        No unrelated call should appear between trio members.
        """
        for result in self.trial_results:
            phase3_records = [r for r in result.call_records if r.phase == 3]

            # Group by (task_id, replicate) and verify adjacency
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
