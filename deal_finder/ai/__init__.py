"""AI enrichment orchestration: translate + answer questions, with graceful fallback.

If AI is disabled or the local model server is unreachable, enrichment returns an empty
result with a note — the pipeline still sends the email using the original text. AI is
never allowed to block a notification.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from ..adapters.base import Listing
from ..config import Settings
from .client import AiUnavailable, OllamaClient
from .questions import answer_questions
from .translate import translate_text

__all__ = ["Enrichment", "enrich_listing", "OllamaClient", "AiUnavailable"]


@dataclass
class Enrichment:
    translated_description: str | None = None
    answers: dict[str, str] = field(default_factory=dict)
    ai_used: bool = False
    note: str = ""


def enrich_listing(
    settings: Settings,
    listing: Listing,
    questions: list[str],
    *,
    client: OllamaClient | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> Enrichment:
    """``on_progress(message)``, if given, is called with a short human-readable
    description of the enrichment step currently in flight (translating, or answering
    a specific question) -- surfaced by the pipeline as the live "Running watch…" status."""
    if not settings.ai_enabled:
        return Enrichment(note="AI disabled in settings")
    client = client or OllamaClient(
        settings.ollama_base_url, settings.ollama_model, settings.ollama_timeout
    )
    try:
        if on_progress:
            on_progress(f"translating description to {settings.ai_translate_to}")
        translated = translate_text(
            client, listing.description, listing.language, settings.ai_translate_to
        )

        def _question_progress(index: int, total: int, question: str) -> None:
            if on_progress:
                on_progress(f"answering question {index}/{total}: “{question}”")

        # Q&A gets every scraped field (price, year, mileage, fuel, location, ...), not
        # just the free-text description -- an answer may only live in a structured
        # field (e.g. "what's the mileage?" when the description never mentions it).
        answers = answer_questions(
            client, listing.as_key_value_text, questions, on_progress=_question_progress
        )
        return Enrichment(translated_description=translated, answers=answers, ai_used=True)
    except AiUnavailable as exc:
        return Enrichment(
            answers={q: "not stated" for q in (questions or [])},
            note=f"local AI unavailable ({exc}); email sent with original text",
        )
