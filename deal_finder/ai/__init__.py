"""AI enrichment orchestration: translate + answer questions, with graceful fallback.

If AI is disabled or the local model server is unreachable, enrichment returns an empty
result with a note — the pipeline still sends the email using the original text. AI is
never allowed to block a notification.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..adapters.base import Listing
from ..config import Settings
from .client import AiUnavailable, OllamaClient
from .questions import answer_questions
from .translate import translate_to_english

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
) -> Enrichment:
    if not settings.ai_enabled:
        return Enrichment(note="AI disabled in settings")
    client = client or OllamaClient(
        settings.ollama_base_url, settings.ollama_model, settings.ollama_timeout
    )
    try:
        translated = translate_to_english(client, listing.description, listing.language)
        answers = answer_questions(client, listing.description, questions)
        return Enrichment(translated_description=translated, answers=answers, ai_used=True)
    except AiUnavailable as exc:
        return Enrichment(
            answers={q: "not stated" for q in (questions or [])},
            note=f"local AI unavailable ({exc}); email sent with original text",
        )
