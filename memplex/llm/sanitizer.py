"""LLM input sanitization for safe prompt construction."""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Optional


class LLMPromptSanitizer:
    """Sanitize and structure LLM inputs to prevent injection and token overflow.

    All public methods are static so the sanitizer can be used without
    instantiation.
    """

    MAX_INPUT_LENGTH: int = 10000

    @staticmethod
    def sanitize(text: str, max_length: int = 10000) -> str:
        """Sanitize raw text for LLM consumption.

        Steps:
        1. NFKC unicode normalization (eliminates visually-similar homoglyphs).
        2. Zero-width character removal (U+200B, U+200C, U+200D, U+FEFF).
        3. Length truncation to prevent token overflow.
        """
        text = unicodedata.normalize("NFKC", text)
        # Remove zero-width characters
        text = re.sub("[\u200b\u200c\u200d\ufeff]", "", text)
        if len(text) > max_length:
            text = text[:max_length] + "...(truncated)"
        return text

    @staticmethod
    def build_structured_prompt(
        instruction: str,
        user_input: str,
        output_schema: dict | None = None,
        max_length: int = 10000,
    ) -> str:
        """Build a structured JSON prompt for safe LLM interaction.

        The user input is embedded as a JSON value so ``json.dumps``
        automatically escapes special characters, eliminating separator
        escape and newline injection risks.

        Parameters
        ----------
        instruction:
            The system-level instruction for the LLM.
        user_input:
            Untrusted user content to be safely embedded.
        output_schema:
            Optional JSON schema describing the expected output format.
        max_length:
            Maximum character length for the sanitized user input.
        """
        safe_input = LLMPromptSanitizer.sanitize(user_input, max_length)
        payload: dict = {
            "instruction": instruction,
            "user_input": safe_input,
        }
        if output_schema is not None:
            payload["output_format"] = output_schema
        return json.dumps(payload, ensure_ascii=False)
