"""
Campaign 2 Prompt Envelope Protection Module.

P0-6 — PROMPT-ENVELOPE PROTECTION:
- Enforces immutable contract and delimiter protection.
- Computes component SHA-256 hashes (immutable_contract_hash, delimiter_hash).
- Validates that meta-optimizer mutation outputs contain ONLY the mutable strategy body.
- Rejects any output that contains contract bytes, delimiter text, or envelope escape attempts.
"""

import hashlib
from typing import Optional, Tuple


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
