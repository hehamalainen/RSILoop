"""
Campaign 2 Prompt Envelope Protection Module.

P0-6 — PROMPT-ENVELOPE PROTECTION:
- Enforces immutable contract and delimiter protection.
- Computes component SHA-256 hashes (immutable_contract_hash, delimiter_hash).
- Validates that meta-optimizer mutation outputs contain ONLY the mutable strategy body.
- Rejects any output that contains contract bytes, delimiter text, or envelope escape attempts.
"""

import hashlib
import re
from typing import Any, Optional, Tuple


ALLOWED_FAILURE_CATEGORIES = frozenset({
    "CandidateImportError",
    "CandidateProcessError",
    "CandidateException",
    "EvaluatorFailure",
})

ALLOWED_CANDIDATE_EXCEPTION_TYPES = frozenset({
    "ArithmeticError",
    "AssertionError",
    "AttributeError",
    "BufferError",
    "EOFError",
    "Exception",
    "FloatingPointError",
    "ImportError",
    "IndexError",
    "KeyError",
    "LookupError",
    "MemoryError",
    "ModuleNotFoundError",
    "NameError",
    "NotImplementedError",
    "OSError",
    "OverflowError",
    "PermissionError",
    "RecursionError",
    "RuntimeError",
    "StopIteration",
    "SyntaxError",
    "SystemError",
    "TimeoutError",
    "TypeError",
    "UnboundLocalError",
    "UnicodeDecodeError",
    "UnicodeEncodeError",
    "UnicodeError",
    "ValueError",
    "ZeroDivisionError",
    "CustomCandidateException",
    "UnclassifiedError",
})

ALLOWED_PROCESS_ERROR_REASONS = frozenset({
    "handshake_failed",
    "terminated_during_candidate_execution",
    "shutdown_integrity_failed",
    "worker_crash",
    "import_failed",
})

_FLOOR_HEADER_RE = re.compile(r"^Floor failure \(score (\d+)/(\d+) < (\d+)/(\d+)\):$")
_ROUND_HEADER_RE = re.compile(r"^Round (\d+) feedback \(aggregate (\d+\.\d{4})\):$")
_ORCH_TASK_FEEDBACK_LINE_RE = re.compile(
    r"^([A-Za-z0-9_]{1,64}): score (\d+)/(\d+); errors: (.*)$"
)
_TASK_FEEDBACK_LINE_RE = re.compile(r"^Task ([A-Za-z0-9_]{1,64}): (\d+)/(\d+)(?: — (.*))?$")


def classify_candidate_exception_type(exc_or_name: Any) -> str:
    """
    Maps an exception object, class, or string name to a finite closed-vocabulary
    builtin exception identifier. Never includes exception message text.
    """
    if isinstance(exc_or_name, str):
        candidate_name = exc_or_name.strip().split(":", 1)[0].strip()
    elif isinstance(exc_or_name, type) and issubclass(exc_or_name, BaseException):
        candidate_name = exc_or_name.__name__
    elif isinstance(exc_or_name, BaseException):
        candidate_name = type(exc_or_name).__name__
    else:
        candidate_name = ""

    if candidate_name in ALLOWED_CANDIDATE_EXCEPTION_TYPES:
        return candidate_name
    return "CustomCandidateException"


def canonicalize_failure_trace_for_optimizer(raw_trace: Any) -> str:
    """
    Enforces a strict closed-vocabulary boundary on evaluator error traces before
    they can enter optimizer feedback. Free-text exception messages, stack frames,
    and unrecognized markers are unconditionally stripped.
    """
    if not isinstance(raw_trace, str):
        return ""
    stripped = raw_trace.strip()
    if not stripped:
        return ""

    parts = stripped.split(":")
    first = parts[0].strip()
    second = ""
    if len(parts) > 1 and parts[1].strip():
        second = parts[1].strip().split()[0].rstrip(":,;.")

    if first in ALLOWED_FAILURE_CATEGORIES:
        if first == "CandidateProcessError":
            reason = (
                second
                if second in ALLOWED_PROCESS_ERROR_REASONS
                else "terminated_during_candidate_execution"
            )
            return f"CandidateProcessError: {reason}"
        exc_label = (
            second
            if second in ALLOWED_CANDIDATE_EXCEPTION_TYPES
            else "CustomCandidateException"
        )
        return f"{first}: {exc_label}"

    if first in ALLOWED_CANDIDATE_EXCEPTION_TYPES:
        return f"{first}: EvaluatorFailure"

    return "EvaluatorFailure: UnclassifiedError"


def sanitize_failure_context_for_optimizer(failure_context: Any) -> str:
    """
    Canonicalizes a multi-line optimizer failure_context so that every line contains
    only the registered control reformulation text, validated task identifiers,
    integer scores, and finite closed-vocabulary failure classifications.
    """
    if not isinstance(failure_context, str):
        return ""
    if failure_context.strip() == CONTROL_REFORMULATION_TEXT.strip():
        return CONTROL_REFORMULATION_TEXT

    raw_lines = [line.strip() for line in failure_context.splitlines() if line.strip()]
    if not raw_lines:
        return ""

    sanitized_lines = []
    for line in raw_lines:
        round_match = _ROUND_HEADER_RE.fullmatch(line)
        if round_match:
            rnd_str, agg_str = round_match.groups()
            sanitized_lines.append(f"Round {int(rnd_str)} feedback (aggregate {agg_str}):")
            continue

        orch_match = _ORCH_TASK_FEEDBACK_LINE_RE.fullmatch(line)
        if orch_match:
            task_id, earned_s, max_s, errors_part = orch_match.groups()
            if errors_part.strip() == "no evaluator trace supplied":
                canon_errors = "no evaluator trace supplied"
            else:
                parts = [
                    canonicalize_failure_trace_for_optimizer(p)
                    for p in errors_part.split("|")
                ]
                parts = [p for p in parts if p]
                canon_errors = " | ".join(parts) if parts else "no evaluator trace supplied"
            sanitized_lines.append(
                f"{task_id}: score {int(earned_s)}/{int(max_s)}; errors: {canon_errors}"
            )
            continue

        floor_match = _FLOOR_HEADER_RE.fullmatch(line)
        if floor_match:
            s_num, s_den, f_num, f_den = (int(g) for g in floor_match.groups())
            sanitized_lines.append(
                f"Floor failure (score {s_num}/{s_den} < {f_num}/{f_den}):"
            )
            continue

        task_match = _TASK_FEEDBACK_LINE_RE.fullmatch(line)
        if task_match:
            task_id, earned_s, max_s, raw_trace = task_match.groups()
            canon_trace = canonicalize_failure_trace_for_optimizer(raw_trace or "")
            suffix = f" — {canon_trace}" if canon_trace else ""
            sanitized_lines.append(f"Task {task_id}: {int(earned_s)}/{int(max_s)}{suffix}")
            continue

        canon_line = canonicalize_failure_trace_for_optimizer(line)
        if canon_line:
            sanitized_lines.append(canon_line)

    return "\n".join(sanitized_lines)


IMMUTABLE_CONTRACT = (
    "You are an expert Python coder. Your job is to solve the programming task described in the input.\n"
    "You must output ONLY valid Python code inside a markdown block starting with ```python and ending with ```.\n"
    "Do not write explanations, markdown text, or any formatting outside the code block.\n"
    "Write the code to implement the requested functions or classes exactly as specified.\n"
    "If any exceptions are requested, define them as standard Exception subclasses.\n"
    "Write clean, correct, and self-contained code."
)

DELIMITER = "\n\n---STRATEGY---\n\n"

CONTROL_REFORMULATION_TEXT = (
    "Rewrite the strategy section of this prompt using different wording.\n"
    "Do not change the meaning or intent. Keep it concise."
)

IMMUTABLE_CONTRACT_HASH = hashlib.sha256(IMMUTABLE_CONTRACT.encode("utf-8")).hexdigest()
DELIMITER_HASH = hashlib.sha256(DELIMITER.encode("utf-8")).hexdigest()
CONTROL_REFORMULATION_HASH = hashlib.sha256(CONTROL_REFORMULATION_TEXT.encode("utf-8")).hexdigest()


class PromptEnvelopeError(ValueError):
    """Raised when prompt envelope construction or mutation output validation fails."""
    pass


class PromptEnvelope:
    """
    Constructs effective prompts and validates meta-optimizer mutation outputs.
    Consumes verified master registration data to bind prompt components and hashes.
    """

    def __init__(
        self,
        master_registration_data: Optional[dict] = None,
        immutable_contract: str = IMMUTABLE_CONTRACT,
        delimiter: str = DELIMITER,
        control_reformulation_text: str = CONTROL_REFORMULATION_TEXT,
    ):
        if master_registration_data and "prompt_envelope" in master_registration_data:
            pe_data = master_registration_data["prompt_envelope"]
            self.immutable_contract = pe_data["immutable_contract"]
            self.delimiter = pe_data["delimiter"]
            self.control_reformulation_text = pe_data["control_reformulation_text"]
            self.immutable_contract_hash = pe_data["immutable_contract_hash"]
            self.delimiter_hash = pe_data["delimiter_hash"]
            self.control_reformulation_hash = pe_data["control_reformulation_hash"]
        else:
            self.immutable_contract = immutable_contract
            self.delimiter = delimiter
            self.control_reformulation_text = control_reformulation_text
            self.immutable_contract_hash = hashlib.sha256(self.immutable_contract.encode("utf-8")).hexdigest()
            self.delimiter_hash = hashlib.sha256(self.delimiter.encode("utf-8")).hexdigest()
            self.control_reformulation_hash = hashlib.sha256(self.control_reformulation_text.encode("utf-8")).hexdigest()

        if self.immutable_contract_hash != IMMUTABLE_CONTRACT_HASH:
            raise PromptEnvelopeError("Immutable contract does not match registered constant hash")
        if self.delimiter_hash != DELIMITER_HASH:
            raise PromptEnvelopeError("Delimiter does not match registered constant hash")
        if self.control_reformulation_hash != CONTROL_REFORMULATION_HASH:
            raise PromptEnvelopeError("Control reformulation text does not match registered constant hash")

    def build_effective_prompt(self, mutable_body: str) -> Tuple[str, str]:
        """
        Build the full effective prompt from contract + delimiter + mutable_body.
        Returns (effective_prompt, prompt_hash).
        """
        if not isinstance(mutable_body, str):
            raise PromptEnvelopeError(f"mutable_body must be str, got {type(mutable_body)}")

        effective_prompt = self.immutable_contract + self.delimiter + mutable_body
        prompt_hash = hashlib.sha256(effective_prompt.encode("utf-8")).hexdigest()
        return effective_prompt, prompt_hash

    def validate_mutation_output(self, raw_output: str) -> str:
        """
        Validate and sanitize meta-optimizer output.

        Enforces:
        1. Output is non-empty string.
        2. Output does NOT contain the immutable contract text.
        3. Output does NOT contain the delimiter string.
        4. Output does NOT contain forbidden markdown code block wrappers (```python or ```).
        5. Output is trimmed of leading/trailing whitespace.

        Returns clean mutable strategy body string.
        """
        if not isinstance(raw_output, str):
            raise PromptEnvelopeError(f"Mutation output must be str, got {type(raw_output)}")

        cleaned = raw_output.strip()
        if not cleaned:
            raise PromptEnvelopeError("Mutation output cannot be empty")

        # Envelope protection checks
        if "You are an expert Python coder" in cleaned or "markdown block starting with ```python" in cleaned:
            raise PromptEnvelopeError("Mutation output attempted to duplicate or alter immutable contract")

        if self.delimiter in cleaned or "---STRATEGY---" in cleaned:
            raise PromptEnvelopeError("Mutation output attempted envelope escape via delimiter injection")

        # Formatting checks
        if cleaned.startswith("```"):
            raise PromptEnvelopeError("Mutation output contains markdown code block wrapper; must be raw text")

        return cleaned
