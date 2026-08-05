"""
Campaign 2 Model Provider Interface & Mock Implementation.

Provides a pluggable model provider abstraction for Campaign 2 orchestration.
- ModelProvider ABC: defines generate_candidate() and mutate_prompt().
- MockModelProvider: returns deterministic partial-scoring code and mutations for testing.
- SolModelProvider: stub for gpt-5.6-sol via Codex CLI (Work Order C, raises NotImplementedError).
"""

import hashlib
from abc import ABC, abstractmethod
from typing import Tuple


class ModelProvider(ABC):
    """Abstract base class for Campaign 2 model providers."""

    @abstractmethod
    def generate_candidate(self, task_id: str, effective_prompt: str,
                           call_id: str) -> Tuple[str, int]:
        """
        Generate candidate Python code for a task.
        Args:
            task_id: benchmark task identifier
            effective_prompt: the full effective prompt (immutable contract + delimiter + mutable body)
            call_id: unique correlation identifier for this call
        Returns:
            (candidate_code: str, tokens_used: int)
        """

    @abstractmethod
    def mutate_prompt(self, mutable_body: str, failure_context: str,
                      call_id: str) -> Tuple[str, int]:
        """
        Mutate the mutable strategy body of the prompt.
        Args:
            mutable_body: current mutable strategy body text
            failure_context: failure traces for guided mutation (empty string for control arm)
            call_id: unique correlation identifier for this call
        Returns:
            (new_mutable_body: str, tokens_used: int)
        """


class MockModelProvider(ModelProvider):
    """
    Deterministic mock model provider for testing the orchestration vertical slice.
    Returns predictable candidate code and mutations based on input hashes.
    Mock candidates return partial scores when evaluated by mock evaluators.
    """

    def __init__(self, base_score_numerator: int = 5, max_score: int = 10):
        self.is_mock = True
        self.base_score_numerator = base_score_numerator
        self.max_score = max_score
        self.call_log = []  # Records all calls for test verification

    def generate_candidate(self, task_id: str, effective_prompt: str,
                           call_id: str) -> Tuple[str, int]:
        prompt_hash = hashlib.sha256(effective_prompt.encode('utf-8')).hexdigest()[:16]
        code = (
            f"# Mock candidate for {task_id}\n"
            f"# prompt_hash={prompt_hash}\n"
            f"# call_id={call_id}\n"
            f"def solve():\n"
            f"    return '{task_id}_{prompt_hash[:8]}'\n"
        )
        tokens = 100
        self.call_log.append({
            'type': 'generate_candidate',
            'task_id': task_id,
            'call_id': call_id,
            'prompt_hash': prompt_hash,
            'tokens': tokens
        })
        return code, tokens

    def mutate_prompt(self, mutable_body: str, failure_context: str,
                      call_id: str) -> Tuple[str, int]:
        body_hash = hashlib.sha256(mutable_body.encode('utf-8')).hexdigest()[:16]
        has_failures = len(failure_context) > 0
        new_body = (
            f"# Mutation {call_id} (guided={has_failures})\n"
            f"# parent_hash={body_hash}\n"
            f"{mutable_body}\n"
        )
        tokens = 200
        self.call_log.append({
            'type': 'mutate_prompt',
            'call_id': call_id,
            'guided': has_failures,
            'body_hash': body_hash,
            'tokens': tokens
        })
        return new_body, tokens


class MockTaskEvaluator:
    """
    Deterministic mock task evaluator for testing.
    Returns scores based on task_id and code hash to produce partial scores.
    Avoids requiring real sandbox execution during orchestration tests.
    """

    def __init__(self, default_earned: int = 5, default_max: int = 10):
        self.default_earned = default_earned
        self.default_max = default_max
        self.eval_log = []
        self._score_overrides = {}  # (task_id, code_hash_prefix) -> (earned, max)

    def set_score(self, task_id: str, earned: int, max_score: int) -> None:
        """Override score for a specific task_id (all code variants)."""
        self._score_overrides[task_id] = (earned, max_score)

    def evaluate(self, task_id: str, candidate_code: str) -> Tuple[int, int, bool, str]:
        code_hash = hashlib.sha256(candidate_code.encode('utf-8')).hexdigest()[:16]
        if task_id in self._score_overrides:
            earned, max_s = self._score_overrides[task_id]
        else:
            earned, max_s = self.default_earned, self.default_max
        is_valid = True
        error_trace = ""
        self.eval_log.append({
            'task_id': task_id,
            'code_hash': code_hash,
            'earned': earned,
            'max': max_s,
            'is_valid': is_valid,
            'error_trace': error_trace,
        })
        return earned, max_s, is_valid, error_trace


class SolModelProvider(ModelProvider):
    """
    Production model provider for gpt-5.6-sol via Codex CLI.
    NOT IMPLEMENTED in Work Order B — requires Work Order C.
    """

    def __init__(self):
        raise NotImplementedError(
            "SolModelProvider requires Work Order C implementation. "
            "Use MockModelProvider for testing."
        )

    def generate_candidate(self, task_id: str, effective_prompt: str,
                           call_id: str) -> Tuple[str, int]:
        raise NotImplementedError(
            "SolModelProvider requires Work Order C implementation. "
            "Use MockModelProvider for testing."
        )

    def mutate_prompt(self, mutable_body: str, failure_context: str,
                      call_id: str) -> Tuple[str, int]:
        raise NotImplementedError(
            "SolModelProvider requires Work Order C implementation. "
            "Use MockModelProvider for testing."
        )
