"""Answer predefined questions about a listing using ONLY its known data (no hallucination).

Asks one question per model call (rather than batching all questions into a single JSON
response) so callers can report fine-grained "answering question X/Y" progress, and so a
single question's slower/larger answer doesn't inflate every other question's latency.
"""

from __future__ import annotations

from collections.abc import Callable

from .client import OllamaClient

_SYSTEM = (
    "You answer a single question about a second-hand marketplace listing. Use ONLY the "
    "information in the provided listing data (structured fields like price/year/mileage/ "
    "fuel/location as well as the free-text description) -- the answer may live in a "
    "field rather than the description. If the listing does not contain the answer, "
    "respond exactly with 'not stated'. Do NOT guess or invent details. Keep your answer "
    "to one or two short sentences. Output ONLY the answer -- no preamble, no restating "
    "the question."
)


def answer_questions(
    client: OllamaClient,
    listing_text: str,
    questions: list[str],
    *,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> dict[str, str]:
    """Return {question: answer}. Best-effort; missing answers default to 'not stated'.

    ``listing_text`` should be every known field of the listing (see
    ``Listing.as_key_value_text``), not just the free-text description -- an answer may
    only live in a structured field (e.g. mileage, fuel type).

    ``on_progress(index, total, question)``, if given, is called just before each
    question is sent to the model (1-indexed).
    """
    questions = [q for q in (questions or []) if q.strip()]
    if not questions or not (listing_text or "").strip():
        return {q: "not stated" for q in questions}

    total = len(questions)
    answers: dict[str, str] = {}
    for i, question in enumerate(questions, start=1):
        if on_progress:
            on_progress(i, total, question)
        user = f"LISTING DATA:\n{listing_text}\n\nQUESTION: {question}"
        raw = client.chat(
            [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
            temperature=0.0,
        )
        answers[question] = raw.strip() or "not stated"
    return answers
