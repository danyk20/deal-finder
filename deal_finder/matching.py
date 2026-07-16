"""Generic matching/filter engine applied to listings returned by adapters.

Adapters do server-side filtering where they can, but it is uneven across marketplaces,
so we always re-apply the watch's constraints here as the source of truth.
"""

from __future__ import annotations

import re

from .adapters.base import Listing, MarketplaceQuery
from .ai.client import OllamaClient
from .ai.dealbreakers import check_non_negotiables
from .categories.base import BaseCategory
from .config import Settings
from .models import Watch


def _squash(s: str) -> str:
    """Drop whitespace so e.g. 'Model S90' matches listing text with different spacing
    ('Model S 90', 'ModelS90', ...)."""
    return re.sub(r"\s+", "", s)


def _contains(needle: str, haystack: str) -> bool:
    """Substring check that also matches when the two sides only differ in spacing."""
    return needle in haystack or _squash(needle) in _squash(haystack)


def filter_rejection_reason(
    listing: Listing,
    query: MarketplaceQuery,
    category: BaseCategory,
    watch: Watch,
    *,
    settings: Settings | None = None,
    ai_client: OllamaClient | None = None,
) -> str | None:
    """Return None if the listing passes every filter, else a human-readable reason for
    the first one it fails (checked in the same order as ``passes_filters``).

    The AI-checked "non-negotiables" filter (see ``category.filter_fields``) runs LAST,
    after every free (non-AI) check, so a listing already ruled out by a cheap filter
    never pays for an AI call. ``settings``/``ai_client`` are optional -- omitting them
    (e.g. existing callers/tests that don't care about the AI filter) simply skips it."""
    # Price (skip listings only when a price is known AND out of range).
    if listing.price is not None:
        if query.price_min is not None and listing.price < query.price_min:
            return f"price {listing.price:.0f} is below the minimum {query.price_min:.0f}"
        if query.price_max is not None and listing.price > query.price_max:
            return f"price {listing.price:.0f} is above the maximum {query.price_max:.0f}"

    text = listing.searchable_text

    # All search terms must appear (guards loose/offline adapters returning everything).
    # Spacing-insensitive: "Model S90" also matches listing text spelling it "Model S 90".
    for term in query.terms:
        if term and not _contains(term.lower(), text):
            return f"search term '{term}' not found in the listing's title/description"

    # Required keywords.
    for kw in query.keywords_include:
        if not _contains(kw.lower(), text):
            return f"required keyword '{kw}' not found in the listing's title/description"

    # Excluded keywords.
    for kw in query.keywords_exclude:
        if _contains(kw.lower(), text):
            return f"excluded keyword '{kw}' found in the listing's title/description"

    # Location (lenient substring match against location field or full text).
    if query.location:
        loc = (listing.location or "").lower()
        needle = query.location.lower()
        if not _contains(needle, loc) and not _contains(needle, text):
            return f"location '{query.location}' not found in the listing's location/text"

    category_reason = category.post_match_reason(listing, watch)
    if category_reason is not None:
        return category_reason

    non_negotiables = (watch.filters or {}).get("non_negotiables", "").strip()
    if non_negotiables and settings is not None and settings.ai_enabled:
        client = ai_client or OllamaClient(
            settings.ollama_base_url, settings.ollama_model, settings.ollama_timeout
        )
        ok, reason = check_non_negotiables(client, listing, non_negotiables)
        if not ok:
            return f"doesn't meet non-negotiables: {reason}"

    return None


def passes_filters(
    listing: Listing, query: MarketplaceQuery, category: BaseCategory, watch: Watch
) -> bool:
    return filter_rejection_reason(listing, query, category, watch) is None


def dedup_cross_marketplace(listings: list[Listing]) -> list[Listing]:
    """Drop near-duplicates (same item listed on multiple marketplaces).

    Heuristic key: lowercased title + rounded price. Keeps the first occurrence.
    """
    seen: set[tuple[str, int | None]] = set()
    out: list[Listing] = []
    for li in listings:
        key = (li.title.strip().lower(), int(li.price) if li.price is not None else None)
        if key in seen:
            continue
        seen.add(key)
        out.append(li)
    return out
