"""AI-checked non-negotiable requirements: a free-text filter judged by the model
against every known field of a listing AND its photos (colour, visible condition,
damage, ...), not just the text -- e.g. "must be green" or "engine currently starts and
runs, no rust" can be judged even when the description never mentions it explicitly.
"""

from __future__ import annotations

import base64

import httpx

from ..adapters.base import Listing
from .client import AiUnavailable, OllamaClient

_SYSTEM = (
    "You are screening ONE second-hand marketplace listing against a buyer's "
    "non-negotiable requirements. Use the listing's structured data and description, "
    "AND any attached photos (colour, visible condition, damage, etc.) if given. Only "
    "judge the listing to FAIL when it clearly contradicts a stated requirement, or a "
    "requirement plainly cannot be met given the stated facts (e.g. requirement is "
    "'green' but the data says the colour is red). If the listing simply doesn't "
    "mention something, and the photos don't show or contradict it either, give the "
    "listing the benefit of the doubt and do not fail it for that alone. Respond with "
    "EXACTLY one line: 'PASS' if the listing satisfies the requirements, or "
    "'FAIL: <short reason>' if it clearly does not."
)

# Bounds the vision payload/cost per listing -- a car listing rarely needs more than a
# couple of photos to judge colour/visible condition.
_MAX_IMAGES = 3

# Ollama's OpenAI-compatible endpoint rejects remote image_url values outright
# ("image URLs are not currently supported, please use base64 encoded data instead") --
# every image has to be fetched and inlined as a base64 data URI ourselves first.
_MAX_IMAGE_BYTES = 8 * 1024 * 1024  # skip anything unexpectedly huge rather than hang


def _image_data_uri(url: str) -> str | None:
    """Fetch one listing photo and return it as a base64 data: URI, or None if it
    can't be fetched/is too large -- a single broken image link must never abort the
    whole non-negotiables check."""
    try:
        resp = httpx.get(url, timeout=15.0, follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError:
        return None
    if len(resp.content) > _MAX_IMAGE_BYTES:
        return None
    content_type = resp.headers.get("content-type", "image/jpeg").split(";")[0].strip()
    if not content_type.startswith("image/"):
        return None
    encoded = base64.b64encode(resp.content).decode("ascii")
    return f"data:{content_type};base64,{encoded}"


def check_non_negotiables(
    client: OllamaClient, listing: Listing, requirements: str
) -> tuple[bool, str | None]:
    """Return (passes, reason). ``reason`` is only set when ``passes`` is False.

    Fails OPEN: if the requirements text is blank, or the AI call itself fails/errors
    (model down, doesn't support vision, timeout, ...), this returns ``(True, None)`` --
    an AI hiccup on this specific check must never silently hide a real match, matching
    this app's "AI never blocks" principle everywhere else.
    """
    requirements = (requirements or "").strip()
    if not requirements:
        return True, None

    content: list[dict] = [
        {
            "type": "text",
            "text": (
                f"LISTING DATA:\n{listing.as_key_value_text}\n\n"
                f"BUYER'S NON-NEGOTIABLE REQUIREMENTS:\n{requirements}"
            ),
        }
    ]
    for url in listing.image_urls[:_MAX_IMAGES]:
        data_uri = _image_data_uri(url)
        if data_uri:
            content.append({"type": "image_url", "image_url": {"url": data_uri}})

    try:
        raw = client.chat(
            [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": content}],
            temperature=0.0,
        )
    except AiUnavailable:
        return True, None

    raw = raw.strip()
    if raw.upper().startswith("FAIL"):
        reason = raw.split(":", 1)[1].strip() if ":" in raw else "does not meet the stated requirements"
        return False, reason
    return True, None
