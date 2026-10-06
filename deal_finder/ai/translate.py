"""Translate listing text to the user's configured target language via the local model."""

from __future__ import annotations

from .client import OllamaClient

# ISO listing-language code -> the English name we'd expect a user to type as their
# target language, so we can skip the model call when source and target already match.
_LANGUAGE_NAMES = {
    "en": "english",
    "de": "german",
    "fr": "french",
    "it": "italian",
}


def translate_text(
    client: OllamaClient,
    text: str,
    source_language: str | None = None,
    target_language: str = "English",
) -> str:
    """Return ``text`` translated into ``target_language`` (a free-text language name,
    e.g. "English", "German"). Skips the call when empty, or when ``source_language``
    is already known to be the target language."""
    text = (text or "").strip()
    if not text:
        return text
    target = (target_language or "English").strip()
    if source_language and _LANGUAGE_NAMES.get(source_language.lower()) == target.lower():
        return text
    system = (
        f"You are a translation engine. Translate the user's text into natural {target}. "
        f"If it is already in {target}, return it unchanged. Output ONLY the "
        "translation, with no preamble, notes, or quotation marks."
    )
    # No reasoning: on a thinking model (gemma4) a translation with it on ran past 4 minutes
    # (live, a ~250-word tutti description) -- and past the timeout, which drops the
    # translation and every answer -- vs ~25s without, for the same translation.
    result = client.chat(
        [
            {"role": "system", "content": system},
            {"role": "user", "content": text},
        ],
        temperature=0.1,
        reasoning_effort="none",
    )
    return result.strip()
