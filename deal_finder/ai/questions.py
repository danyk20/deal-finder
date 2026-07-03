"""Answer predefined questions about a listing using ONLY its text (no hallucination).

The model is asked to answer strictly from the provided description and to say
"not stated" when the listing doesn't contain the information. Output is parsed
robustly (the model may wrap JSON in prose or code fences).
"""

from __future__ import annotations

import json
import re

from .client import OllamaClient

_SYSTEM = (
    "You answer questions about a second-hand marketplace listing. Use ONLY the "
    "information in the provided listing text. If the listing does not contain the "
    "answer, respond exactly with 'not stated'. Do NOT guess or invent details. "
    "Keep each answer to one or two short sentences. Respond with a single JSON object "
    'whose keys are the question numbers as strings (e.g. "1", "2") and whose values '
    "are your answers."
)


def _extract_json_object(text: str) -> dict:
    """Pull the first JSON object out of model output, tolerating fences/prose."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
        text = re.sub(r"\n?```$", "", text).strip()
    try:
        return json.loads(text)
    except ValueError:
        pass
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            return json.loads(text[start : end + 1])
        except ValueError:
            pass
    raise ValueError("no JSON object found in model output")


def answer_questions(
    client: OllamaClient, description: str, questions: list[str]
) -> dict[str, str]:
    """Return {question: answer}. Best-effort; missing answers default to 'not stated'."""
    questions = [q for q in (questions or []) if q.strip()]
    if not questions or not (description or "").strip():
        return {q: "not stated" for q in questions}

    numbered = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, start=1))
    user = (
        f"LISTING TEXT:\n{description}\n\n"
        f"QUESTIONS:\n{numbered}\n\n"
        'Answer as JSON, e.g. {"1": "...", "2": "not stated"}.'
    )
    raw = client.chat(
        [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": user}],
        temperature=0.0,
        json_mode=True,
    )
    try:
        parsed = _extract_json_object(raw)
    except ValueError:
        return {q: "(AI answer could not be parsed)" for q in questions}

    answers: dict[str, str] = {}
    for i, q in enumerate(questions, start=1):
        val = parsed.get(str(i), parsed.get(q, "not stated"))
        answers[q] = str(val).strip() if val is not None else "not stated"
    return answers
