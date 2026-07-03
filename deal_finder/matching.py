"""Generic matching/filter engine applied to listings returned by adapters.

Adapters do server-side filtering where they can, but it is uneven across marketplaces,
so we always re-apply the watch's constraints here as the source of truth.
"""

from __future__ import annotations

from .adapters.base import Listing, MarketplaceQuery
from .categories.base import BaseCategory
from .models import Watch


def passes_filters(
    listing: Listing, query: MarketplaceQuery, category: BaseCategory, watch: Watch
) -> bool:
    # Price (skip listings only when a price is known AND out of range).
    if listing.price is not None:
        if query.price_min is not None and listing.price < query.price_min:
            return False
        if query.price_max is not None and listing.price > query.price_max:
            return False

    text = listing.searchable_text

    # All search terms must appear (guards loose/offline adapters returning everything).
    for term in query.terms:
        if term and term.lower() not in text:
            return False

    # Required keywords.
    for kw in query.keywords_include:
        if kw.lower() not in text:
            return False

    # Excluded keywords.
    for kw in query.keywords_exclude:
        if kw.lower() in text:
            return False

    # Location (lenient substring match against location field or full text).
    if query.location:
        loc = (listing.location or "").lower()
        needle = query.location.lower()
        if needle not in loc and needle not in text:
            return False

    return category.post_match(listing, watch)


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
