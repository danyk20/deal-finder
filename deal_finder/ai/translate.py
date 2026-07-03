"""Translate listing text to English via the local model."""

from __future__ import annotations

from .client import OllamaClient

_SYSTEM = (
    "You are a translation engine. Translate the user's text into natural English. "
    "If it is already English, return it unchanged. Output ONLY the translation, with "
    "no preamble, notes, or quotation marks."
)


def translate_to_english(client: OllamaClient, text: str, source_language: str | None = None) -> str:
    """Return ``text`` in English. Skips the call when already English or empty."""
    text = (text or "").strip()
    if not text:
        return text
    if source_language == "en":
        return text
    result = client.chat(
        [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": text},
        ],
        temperature=0.1,
    )
    return result.strip()
