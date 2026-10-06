"""AI-checked non-negotiable requirements: a free-text filter judged by the model
against every known field of a listing and, only where the text doesn't settle it, its
photos (colour, visible condition, damage, ...) -- e.g. "must be green" or "engine
currently starts and runs, no rust" can be judged even when the description never
mentions it explicitly.

Text first, photos only when needed: the model first gets just the listing's data and
description and answers PASS, FAIL or UNKNOWN (the text doesn't say). Only on UNKNOWN are
photos looked at, a few per call, stopping at the first clear answer. Sending all photos
at once made the check fail both ways (live, gemma4:12b, a "Mac Mini M2 mit 8 GB RAM"
against "more than 32 GB of RAM"): with reasoning on it ran past the 120s timeout -- and a
timeout lets the listing through -- and with reasoning off the photos drowned out the
"8 GB" in the text and it answered PASS. Text-only got all of them right in ~5s.

An UNKNOWN also says whether a photo could settle it at all: "free supercharging", a
service history or how the engine runs never show in a photo, and looking anyway cost up
to 10 photo calls (~16s each, live) per listing, all answering UNKNOWN.

Each line of the requirements is one requirement, judged on its own, and the first FAIL
ends the check. Judged together, the model mixed them up: "must be green and have free
supercharging" against a listing saying only "green" came back "FAIL: no free
supercharging" (live, gemma4:12b, every time) -- a real match silently dropped.
"""

from __future__ import annotations

import base64
import logging
import re
from concurrent.futures import Future, ThreadPoolExecutor

import httpx

from ..adapters.base import Listing
from .client import AiUnavailable, OllamaClient

log = logging.getLogger("deal_finder.ai.dealbreakers")

# "several parts": a line can still hold more than one thing ("green and free
# supercharging"); without it the unmentioned part was a FAIL.
_RULES = (
    "You are screening ONE second-hand marketplace listing against ONE of a buyer's "
    "non-negotiable requirements. Answer FAIL when the listing clearly contradicts the "
    "requirement, or the requirement plainly cannot be met given the stated facts (e.g. "
    "requirement 'more than 32 GB of RAM' but the listing says 8 GB; requirement 'green' "
    "but the colour is red). If the requirement has several parts, judge each part on its "
    "own: a part the facts don't mention is never a FAIL, even when the other parts are "
    "mentioned or met."
)

# "never a FAIL": without it the model answered "FAIL: Not stated if RAM is more than
# 32 GB" for a listing silent on RAM. The "| PHOTOS:" part (live, gemma4:12b): NO for free
# supercharging, how the engine runs and when the seller joined; YES for colour and rust.
_TEXT_SYSTEM = _RULES + (
    " You only get the listing's text. Something the text doesn't mention is never a "
    "FAIL -- that's UNKNOWN. Respond with EXACTLY one line: 'FAIL: <short reason>' if "
    "the text clearly contradicts the requirement; 'PASS' if the text shows the "
    "requirement is met; otherwise 'UNKNOWN: <what the text doesn't say> | PHOTOS: YES' "
    "if a photo of the item itself could show what's missing (e.g. its colour, visible "
    "damage or rust, what's included), or 'UNKNOWN: <what the text doesn't say> | PHOTOS: NO' "
    "if no photo of the item could show it (e.g. an included service or perk, its history, "
    "how it runs, facts about the seller)."
)
_PHOTOS_RE = re.compile(r"\|?\s*PHOTOS?\s*:?\s*(YES|NO)\b\.?", re.IGNORECASE)

# Same "never a FAIL" rule: without it the photo step answered "FAIL: amount of RAM not
# specified" for photos that just didn't show the RAM.
_PHOTO_SYSTEM = _RULES + (
    " The listing's text didn't settle the requirement; you now also get some of its "
    "photos. Something neither the text nor these photos show is never a FAIL -- that's "
    "UNKNOWN. Respond with EXACTLY one line: 'FAIL: <short reason>' if the text or these "
    "photos clearly show the requirement is NOT met; 'PASS' if they show it IS met; "
    "'UNKNOWN' otherwise."
)

# Bounds the photo payload per listing -- high enough to cover a typical listing's full
# gallery (damage/rust often only shows in the later close-up photos). Only downloaded,
# a batch at a time, when the text alone can't decide.
_MAX_IMAGES = 30
# Photos per model call: live, 3 still left the text facts intact; 5 at once made the
# model ignore a plainly stated "8 GB RAM" and answer PASS.
_PHOTOS_PER_CALL = 3

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


def _verdict(raw: str) -> tuple[str, str]:
    """("PASS" | "FAIL" | "UNKNOWN", detail) from the model's answer, tolerating markdown
    ("**FAIL**: ...") and a lead-in sentence. Raises AiUnavailable if there's no verdict
    in it at all -- an unreadable answer must not count as a PASS."""
    text = re.sub(r"[*_`#>]", "", raw or "").strip()
    m = re.match(r"(PASS|FAIL|UNKNOWN)\b\s*:?\s*(.*)", text, re.IGNORECASE | re.DOTALL) or re.search(
        r"\b(FAIL|UNKNOWN|PASS)\b\s*:?\s*(.*)", text, re.IGNORECASE | re.DOTALL
    )
    if not m:
        raise AiUnavailable(f"no PASS/FAIL/UNKNOWN in the model's answer: {raw!r}")
    return m.group(1).upper(), m.group(2).strip().splitlines()[0].strip() if m.group(2).strip() else ""


def _photos_could_help(raw: str) -> bool:
    """Whether the text step's UNKNOWN said a photo could settle it. No answer either way
    means look, as before this question existed."""
    m = _PHOTOS_RE.search(raw or "")
    return m is None or m.group(1).upper() == "YES"


def _chat(client: OllamaClient, system: str, content) -> str:
    # No reasoning: on a thinking model it's what pushed photo checks past the timeout,
    # and the text-only verdicts were just as right without it (and ~4x faster).
    return client.chat(
        [{"role": "system", "content": system}, {"role": "user", "content": content}],
        temperature=0.0,
        reasoning_effort="none",
    )


def _ask(client: OllamaClient, system: str, content) -> tuple[str, str]:
    return _verdict(_chat(client, system, content))


def requirement_lines(requirements: str) -> list[str]:
    """The separate requirements in a watch's non-negotiables text: one per line."""
    return [line.strip() for line in (requirements or "").splitlines() if line.strip()]


def check_non_negotiables(
    client: OllamaClient, listing: Listing, requirements: str
) -> tuple[bool, str | None]:
    """Return (passes, reason). ``reason`` is only set when ``passes`` is False.

    Every line of ``requirements`` is checked on its own, in order, and the first FAIL
    rejects the listing without checking the rest. All of them are judged from the text
    first; photos are only looked at afterwards, for the ones the text left open -- so a
    FAIL any line's text shows never waits behind photo calls for another line.

    Fails OPEN: if the requirements text is blank, or the AI call itself fails/errors
    (model down, timeout, unreadable answer, ...), this returns ``(True, None)`` -- an AI
    hiccup on this specific check must never silently hide a real match, matching this
    app's "AI never blocks" principle everywhere else. (Logged, so it isn't silent.)
    A requirement neither the text nor the photos settle gets the benefit of the doubt.
    """
    lines = requirement_lines(requirements)
    if not lines:
        return True, None

    def facts(requirement: str) -> str:
        return f"LISTING DATA:\n{listing.as_key_value_text}\n\nBUYER'S NON-NEGOTIABLE REQUIREMENT:\n{requirement}"

    # Photos download a batch at a time, in parallel, and the next batch downloads while
    # the model looks at the current one. Nothing is downloaded before the text step: it
    # settles most listings on its own. Downloaded photos are reused for the next line.
    urls = listing.image_urls[:_MAX_IMAGES]
    batches = [urls[start : start + _PHOTOS_PER_CALL] for start in range(0, len(urls), _PHOTOS_PER_CALL)]
    downloads = ThreadPoolExecutor(max_workers=_PHOTOS_PER_CALL, thread_name_prefix="photos")
    fetched: dict[int, list[Future]] = {}

    def photos(i: int) -> list[str]:
        for j in (i, i + 1):  # this batch, and the next one in the background
            if j < len(batches) and j not in fetched:
                fetched[j] = [downloads.submit(_image_data_uri, url) for url in batches[j]]
        return [uri for f in fetched[i] if (uri := f.result())]

    try:
        open_lines: list[tuple[str, str]] = []  # (requirement, what the text doesn't say)
        for requirement in lines:
            raw = _chat(client, _TEXT_SYSTEM, facts(requirement))
            kind, detail = _verdict(raw)
            if kind == "FAIL":
                return False, detail or f"doesn't meet: {requirement}"
            if kind == "UNKNOWN" and _photos_could_help(raw):
                open_lines.append((requirement, _PHOTOS_RE.sub("", detail).strip(" |")))

        # The text left these open: look at the photos, a few at a time, until one batch
        # settles it.
        for requirement, detail in open_lines:
            unsettled = f"\n\nNOT SETTLED BY THE TEXT: {detail}" if detail else ""
            for i in range(len(batches)):
                images = photos(i)
                if not images:
                    continue
                content = [{"type": "text", "text": facts(requirement) + unsettled}] + [
                    {"type": "image_url", "image_url": {"url": uri}} for uri in images
                ]
                kind, detail_from_photos = _ask(client, _PHOTO_SYSTEM, content)
                if kind == "FAIL":
                    return False, detail_from_photos or f"doesn't meet: {requirement}"
                if kind == "PASS":
                    break
    except AiUnavailable as exc:
        log.warning("non-negotiables check skipped for %s (%s): %s", listing.url, listing.title, exc)
        return True, None
    finally:
        # Don't wait for a next batch nobody will look at.
        downloads.shutdown(wait=False, cancel_futures=True)

    return True, None
