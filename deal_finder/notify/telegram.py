"""Render and send match notifications via a Telegram bot.

Mirrors notify/email.py's shape (a Match dataclass + render + send), but renders and
sends PER LISTING rather than one document for the whole batch: Telegram messages are
short chat-style messages, not a rich multi-section HTML document, so one match = one
message (optionally with a photo) fits the medium far better than trying to cram a
whole run's matches into a single message.
"""

from __future__ import annotations

import html as _html
import re
from dataclasses import dataclass

import httpx

from ..adapters.base import Listing
from ..ai import Enrichment
from ..config import Settings

TELEGRAM_API = "https://api.telegram.org"

# Telegram's own limits: sendPhoto captions cap at 1024 chars, sendMessage text at 4096.
_CAPTION_LIMIT = 1024
_MESSAGE_LIMIT = 4096


@dataclass
class TelegramMatch:
    listing: Listing
    enrichment: Enrichment
    questions: list[str]


class TelegramNotConfigured(Exception):
    """Bot token or chat ID missing."""


class TelegramApiError(Exception):
    """Telegram's Bot API returned a non-ok response."""


def _esc(text: str) -> str:
    """Escape for Telegram's HTML parse mode: only <, >, & need escaping (unlike the far
    more fragile MarkdownV2 mode, which requires escaping over a dozen punctuation
    characters that routinely appear in listing titles/descriptions)."""
    return _html.escape(text or "", quote=False)


_DANGLING_ENTITY_RE = re.compile(r"&[a-zA-Z]{0,5}$")


def _safe_truncate(escaped_text: str, limit: int) -> str:
    """Cut already-HTML-escaped text at `limit` chars without leaving a severed entity
    (e.g. slicing "...&amp;" mid-way into "...&am") dangling in the output."""
    cut = escaped_text[:limit]
    return _DANGLING_ENTITY_RE.sub("", cut)


def _facts_line(listing: Listing) -> str:
    bits = []
    if listing.price is not None:
        bits.append(f"{listing.price:,.0f} {listing.currency}".replace(",", "'"))
    year = listing.attributes.get("year")
    if year:
        bits.append(str(year))
    km = listing.attributes.get("mileage_km")
    if km:
        bits.append(f"{km:,.0f} km".replace(",", "'"))
    if listing.location:
        bits.append(listing.location)
    return " · ".join(bits)


def _header(listing: Listing) -> str:
    """Title + facts + link -- the part that must NEVER be truncated, regardless of how
    the caption-length budget below plays out."""
    lines = [f"<b>{_esc(listing.title)}</b>"]
    facts = _facts_line(listing)
    if facts:
        lines.append(_esc(facts))
    lines.append(f'<a href="{_esc(listing.url)}">Open listing</a>')
    return "\n".join(lines)


def _body(match: TelegramMatch) -> str:
    """The optional, enrichment-derived part: translated description + AI answers."""
    parts = []
    desc = (match.enrichment.translated_description or match.listing.description or "").strip()
    if desc:
        parts.append(_esc(desc))
    if match.questions:
        qa_lines = [f"• <b>{_esc(q)}</b>: {_esc(match.enrichment.answers.get(q, 'not stated'))}" for q in match.questions]
        parts.append("\n".join(qa_lines))
    if match.enrichment.note:
        parts.append(f"<i>{_esc(match.enrichment.note)}</i>")
    return "\n\n".join(parts)


def render_telegram_message(match: TelegramMatch) -> tuple[str, str | None]:
    """One match -> (html_text, photo_url_or_None).

    The header (title/price/year/km/location/link) is always included in full. The body
    (translated description + AI Q&A) is appended in full when sending as a plain message
    (4096-char budget); render_caption() below applies a much tighter budget for the
    sendPhoto path, never letting the body crowd out the header there.
    """
    header = _header(match.listing)
    body = _body(match)
    text = f"{header}\n\n{body}" if body else header
    photo = match.listing.image_urls[0] if match.listing.image_urls else None
    return _safe_truncate(text, _MESSAGE_LIMIT), photo


def render_caption(match: TelegramMatch) -> str:
    """Header-first caption for sendPhoto, truncated to Telegram's 1024-char cap. The
    header (facts + link) is never cut; only the body is trimmed or dropped to fit."""
    header = _header(match.listing)
    body = _body(match)
    if not body:
        return header[:_CAPTION_LIMIT]
    budget = _CAPTION_LIMIT - len(header) - len("\n\n")
    if budget <= 20:  # no meaningful room left for any body text
        return header[:_CAPTION_LIMIT]
    if len(body) > budget:
        body = _safe_truncate(body, budget - 1).rstrip() + "…"
    return f"{header}\n\n{body}"[:_CAPTION_LIMIT]


def _post(url: str, payload: dict) -> dict:
    try:
        resp = httpx.post(url, json=payload, timeout=30)
    except httpx.HTTPError as exc:
        raise TelegramApiError(f"request failed: {exc}") from exc
    try:
        data = resp.json()
    except ValueError as exc:
        raise TelegramApiError(f"non-JSON response (HTTP {resp.status_code})") from exc
    if not data.get("ok"):
        raise TelegramApiError(data.get("description") or f"HTTP {resp.status_code}")
    return data


def _require_configured(settings: Settings, chat_id: str) -> None:
    if not settings.telegram_bot_token:
        raise TelegramNotConfigured("Telegram bot token is not configured (set DF_TELEGRAM_BOT_TOKEN or Settings).")
    if not chat_id:
        raise TelegramNotConfigured("No Telegram chat ID set for this watch.")


def send_telegram(settings: Settings, chat_id: str, text: str, photo_url: str | None = None) -> None:
    """Send one message as-is: sendPhoto with `text` as the caption (truncated to
    Telegram's 1024-char cap) if `photo_url` is given, else sendMessage with `text` (up to
    4096 chars). A simple, no-fallback primitive -- see send_telegram_match() for the
    smarter photo-then-full-text fallback the pipeline actually uses for real listings.
    Raises TelegramNotConfigured / TelegramApiError.
    """
    _require_configured(settings, chat_id)
    base = f"{TELEGRAM_API}/bot{settings.telegram_bot_token}"
    if photo_url:
        _post(
            f"{base}/sendPhoto",
            {"chat_id": chat_id, "photo": photo_url, "caption": _safe_truncate(text, _CAPTION_LIMIT), "parse_mode": "HTML"},
        )
    else:
        _post(
            f"{base}/sendMessage",
            {"chat_id": chat_id, "text": _safe_truncate(text, _MESSAGE_LIMIT), "parse_mode": "HTML", "disable_web_page_preview": False},
        )


def send_telegram_match(settings: Settings, chat_id: str, match: TelegramMatch) -> None:
    """Render one listing's notification and send it -- the pipeline's entry point.

    Tries sendPhoto with the header-first, budget-aware caption (render_caption()) first
    when the listing has an image. If that call fails for ANY reason (most commonly:
    Telegram couldn't fetch the marketplace's image URL), falls back to sendMessage with
    the FULL text (render_telegram_message()'s output, not just the truncated caption) --
    so a broken image never costs the user the rest of the listing's information.
    """
    _require_configured(settings, chat_id)
    text, photo_url = render_telegram_message(match)
    base = f"{TELEGRAM_API}/bot{settings.telegram_bot_token}"

    if photo_url:
        try:
            _post(
                f"{base}/sendPhoto",
                {"chat_id": chat_id, "photo": photo_url, "caption": render_caption(match), "parse_mode": "HTML"},
            )
            return
        except TelegramApiError:
            pass  # fall through to the full text message below

    _post(
        f"{base}/sendMessage",
        {"chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False},
    )
