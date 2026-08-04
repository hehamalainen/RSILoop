"""
Campaign 2 Prompt Envelope Protection Module.

P0-6 — PROMPT-ENVELOPE PROTECTION:
- Enforces immutable contract and delimiter protection.
- Computes component SHA-256 hashes (immutable_contract_hash, delimiter_hash).
- Validates that meta-optimizer mutation outputs contain ONLY the mutable strategy body.
- Rejects any output that contains contract bytes, delimiter text, or envelope escape attempts.
"""

import hashlib
from typing import Tuple


IMMUTABLE_CONTRACT = (
    "You are an expert Python coder. Your job is to solve the programming task described in the input.\n"
    "You must output ONLY valid Python code inside a markdown block starting with ```python and ending with ```.\n"
    "Do not write explanations, markdown text, or any formatting outside the code block.\n"
    "Write the code to implement the requested functions or classes exactly as specified.\n"
    "If any exceptions are requested, define them as standard Exception subclasses.\n"
    "Write clean, correct, and self-contained code."
)

DELIMITER = "\n\n---STRATEGY---\n\n"

IMMUTABLE_CONTRACT_HASH = hashlib.sha256(IMMUTABLE_CONTRACT.encode("utf-8")).hexdigest()
DELIMITER_HASH = hashlib.sha256(DELIMITER.encode("utf-8")).hexdigest()


class PromptEnvelopeError(ValueError):
    """Raised when prompt envelope construction or mutation output validation fails."""
    pass


class PromptEnvelope:
    """
    Constructs effective prompts and validates meta-optimizer mutation outputs.
    """

    def __init__(
        self,
        immutable_contract: str = IMMUTABLE_CONTRACT,
        delimiter: str = DELIMITER,
    ):
        self.immutable_contract = immutable_contract
        self.delimiter = delimiter
        self.immutable_contract_hash = hashlib.sha256(immutable_contract.encode("utf-8")).hexdigest()
        self.delimiter_hash = hashlib.sha256(delimiter.encode("utf-8")).hexdigest()

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
