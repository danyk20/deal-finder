"""Shared base for browser-driven car adapters.

Subclasses set `key`, `label`, `base_url`, `id_regex`, and `build_search_urls`. The
default card-collection and detail-extraction are robust (URL-pattern card links +
JSON-LD/OpenGraph/text detail extraction), so a subclass is usually just a few lines.
All site-specific values are marked ``# VERIFY LIVE`` where the recon confidence was low.
"""

from __future__ import annotations

import re

from ..browser import BrowserAdapter, CardRef, PageView
from ..browser import extract as ex
from ..config import Settings
from .base import Listing, MarketplaceQuery


class CarBrowserAdapter(BrowserAdapter):
    supported_categories = {"car"}
    base_url: str = ""
    id_regex: re.Pattern = re.compile(r"(\d+)")

    def build_search_urls(self, query: MarketplaceQuery, settings: Settings) -> list[str]:
        raise NotImplementedError

    def extract_cards(self, view: PageView, query: MarketplaceQuery) -> list[CardRef]:
        return [
            CardRef(url=url, external_id=ext)
            for ext, url in ex.card_links(view.html, self.id_regex, self.base_url)
        ]

    def external_id_from_url(self, url: str) -> str | None:
        m = self.id_regex.search(url)
        return m.group(1) if m else None

    def extract_detail(self, view: PageView, card: CardRef, query: MarketplaceQuery) -> Listing | None:
        f = ex.car_listing_fields(view.html)
        title = f["title"] or card.partial.get("title", "")
        if not title:
            return None
        attrs = {k: v for k, v in (("year", f["year"]), ("mileage_km", f["mileage_km"])) if v is not None}
        return Listing(
            marketplace=self.key,
            external_id=card.external_id,
            url=(view.url or card.url),
            title=title,
            description=f["description"],
            language=None,
            price=f["price"],
            currency=f["currency"] or "CHF",
            location=f["location"] or card.partial.get("location"),
            attributes=attrs,
            image_urls=f["image_urls"],
        )
