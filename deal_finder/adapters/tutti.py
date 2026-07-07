"""tutti.ch adapter — uses the `tutti-scraper` PyPI package
(https://pypi.org/project/tutti-scraper/), which talks directly to tutti.ch's own public
GraphQL API (``tutti.ch/api/v10/graphql``) with plain ``requests``. No browser, no
Cloudflare/anti-bot bypass, no persistent profile needed — it's a plain HTTP adapter,
exactly like the AutoScout24 one.

Two-phase fetch (mirrors the package's own ``scrape()``): a paginated search, then one
detail request per listing for the full body/images/attributes used by translation + AI
Q&A. Capped to the newest ``browser_max_items_per_run`` results so a broad watch doesn't
fire hundreds of detail requests every run; the search is pinned to tutti's ``cars``
category so toy/accessory listings that merely mention the model don't leak in.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from datetime import datetime, timezone

from tutti_scraper import scrape

from ..browser import extract as ex  # shared parse_price / parse_year / parse_int_km
from ..config import Settings, get_settings
from .base import AdapterError, BaseAdapter, Listing, MarketplaceQuery

log = logging.getLogger("deal_finder.adapters.tutti")

# deal_finder category -> tutti categoryID (VERIFY LIVE via ScrapeResult.suggested_categories).
_TUTTI_CATEGORY = {"car": "cars"}

# Structured car property IDs tutti exposes (from its AutoScout integration). Reading these
# is far more reliable than regex — e.g. it avoids mistaking an EV's "Reichweite 350 km"
# range for the odometer. VERIFY LIVE against ScrapeResult.listings[].properties.
_PROP_YEAR_IDS = ("cars_carAutoScoutRegistrationYear",)
_PROP_MILEAGE_IDS = ("cars_carAutoScoutMileage",)


def _num_price(node: dict) -> float | None:
    seo = node.get("seoInformation") or {}
    n = seo.get("numericPrice")
    if isinstance(n, (int, float)):
        return float(n)
    price, _ = ex.parse_price(node.get("formattedPrice"))  # e.g. "15'000.-"
    return price


def _location(node: dict) -> str | None:
    pc = node.get("postcodeInformation") or {}
    loc = " ".join(str(p) for p in (pc.get("postcode"), pc.get("locationName")) if p).strip()
    return loc or (pc.get("canton") or {}).get("name") or None


def _images(node: dict) -> list[str]:
    out: list[str] = []
    for img in node.get("images") or []:
        if isinstance(img, dict):
            src = (img.get("rendition") or {}).get("src")
            if src:
                out.append(src)
    if not out:
        thumb = (node.get("thumbnail") or {}).get("normalRendition") or {}
        if thumb.get("src"):
            out.append(thumb["src"])
    return out[:8]


def _posted_at(node: dict) -> datetime | None:
    ts = node.get("timestamp")
    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts).replace(tzinfo=None)
        except ValueError:
            return None
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
    return None


def _props_by_id(node: dict) -> dict[str, str]:
    return {
        p.get("listingPropertyID"): p.get("text")
        for p in (node.get("properties") or [])
        if isinstance(p, dict) and p.get("listingPropertyID")
    }


def _prop_int(props: dict[str, str], ids: tuple[str, ...]) -> int | None:
    for i in ids:
        digits = re.sub(r"[^\d]", "", str(props.get(i) or ""))
        if digits:
            return int(digits)
    return None


def listing_from_api_node(node: dict) -> Listing | None:
    """Map one tutti-scraper node (search-summary or full-detail shape) to a Listing.
    Pure + fixture-testable without any network access."""
    ext = node.get("listingID")
    title = (node.get("title") or "").strip()
    if not ext or not title:
        return None

    body = (node.get("body") or "").strip()
    props = _props_by_id(node)
    attrs: dict[str, int] = {}
    # Prefer tutti's structured fields; fall back to regex over title/body only (never the
    # property text, which can contain the EV range in km and get mistaken for mileage).
    year = _prop_int(props, _PROP_YEAR_IDS) or ex.parse_year(title, body)
    if year is not None and 1980 <= year <= 2035:
        attrs["year"] = year
    mileage = _prop_int(props, _PROP_MILEAGE_IDS)
    if mileage is None:
        mileage = ex.parse_int_km(title, body)
    if mileage is not None:
        attrs["mileage_km"] = mileage

    return Listing(
        marketplace="tutti",
        external_id=str(ext),
        url=node.get("url") or f"https://www.tutti.ch/de/vi/{ext}",
        title=title,
        description=body,
        language=node.get("language"),  # de/fr/it -> AI translates to English
        price=_num_price(node),
        currency="CHF",
        location=_location(node),
        posted_at=_posted_at(node),
        attributes=attrs,
        image_urls=_images(node),
    )


class TuttiAdapter(BaseAdapter):
    key = "tutti"
    label = "tutti.ch"
    supported_categories = {"car"}
    enabled_by_default = True
    status_note = "public GraphQL API (tutti.ch) via the tutti-scraper package — no browser needed"

    def search(self, query: MarketplaceQuery, settings: Settings | None = None) -> Iterable[Listing]:
        text = (query.text or " ".join(query.terms)).strip()
        if not text:
            raise AdapterError("tutti.ch: no search text (make/model) set on the watch")

        settings = settings or get_settings()
        try:
            result = scrape(
                text,
                lang="de",
                category=_TUTTI_CATEGORY.get(query.category),  # None -> all categories
                detail=True,
                max_results=settings.browser_max_items_per_run,
                price_from=int(query.price_min) if query.price_min is not None else None,
                price_to=int(query.price_max) if query.price_max is not None else None,
                delay=0.6,
                verbose=False,
            )
        except ValueError as exc:  # bad filters (e.g. price_from > price_to)
            raise AdapterError(f"tutti.ch: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - network/TuttiError etc.
            raise AdapterError(f"tutti.ch request failed: {exc}") from exc

        return [li for node in result.listings if (li := listing_from_api_node(node)) is not None]

    def health_check(self) -> bool:
        try:
            scrape("Tesla", category="cars", detail=False, max_results=1, verbose=False)
            return True
        except Exception:  # noqa: BLE001
            return False
