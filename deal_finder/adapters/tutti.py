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

Every listing's ``properties`` list already carries structured car facts from tutti's own
AutoScout integration (brand, model, body type, doors, color, fuel type, transmission,
horsepower) -- confirmed against a real fixture. This is all fetched by ``tutti-scraper``
already; the gap (fixed here) was entirely on this adapter's side: only the year/mileage
properties were ever read into ``Listing.attributes``, the other six were computed into a
dict and then silently dropped. ``_build_attributes()`` now surfaces all of them, plus a
generic catch-all for any property id not in the known-fields rename table, so a future
tutti property doesn't get silently dropped again either.
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

# deal_finder category -> tutti categoryID. Confirmed live against a real fixture
# (tests/fixtures/tutti_listings.json): every listing's own primaryCategory.categoryID
# comes back "cars", matching this filter.
_TUTTI_CATEGORY = {"car": "cars"}

# Structured car property IDs tutti exposes (from its AutoScout integration). Reading these
# is far more reliable than regex — e.g. it avoids mistaking an EV's "Reichweite 350 km"
# range for the odometer. Confirmed live: every real listing carries all of these.
_PROP_YEAR_IDS = ("cars_carAutoScoutRegistrationYear",)
_PROP_MILEAGE_IDS = ("cars_carAutoScoutMileage",)
_PROP_ALREADY_HANDLED = set(_PROP_YEAR_IDS) | set(_PROP_MILEAGE_IDS)

# Other structured property IDs confirmed present on every real listing (same fixture)
# but never read into Listing.attributes until now -- renamed to the naming convention
# used across every other adapter's attributes.
_PROP_ATTRIBUTE_RENAMES: dict[str, str] = {
    "cars_carAutoScoutBrand": "brand",
    "cars_carAutoScoutModel": "model",
    "cars_carAutoScoutBodyType": "body_type",
    "cars_carAutoScoutDoors": "doors",
    "cars_carAutoScoutColor": "color",
    "cars_carAutoScoutFuelType": "fuel",
    "cars_carAutoScoutTransmissionType": "transmission",
    "cars_carAutoScoutHorsepower": "horsepower",
}
_PROP_INT_IDS = {"cars_carAutoScoutDoors", "cars_carAutoScoutHorsepower"}


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


def _build_attributes(props: dict[str, str], title: str, body: str) -> dict[str, object]:
    """Prefer tutti's own structured `properties` (from its AutoScout integration) over
    regex guesses. Known car-relevant property IDs are renamed to the naming convention
    used across every other adapter's `attributes`; any other property id not in that
    table (a future tutti addition, or a category-specific one this adapter doesn't know
    about yet) still reaches the AI verbatim under its own raw key, rather than being
    silently dropped -- the exact bug this fixes: six of these ids (brand, model, body
    type, doors, color, transmission, fuel, horsepower) were already present on every
    real listing's `properties` but were never read into `attributes` at all."""
    attrs: dict[str, object] = {}

    # Fall back to regex over title/body only (never the property text, which can
    # contain e.g. an EV's "Reichweite 350 km" range and get mistaken for mileage).
    year = _prop_int(props, _PROP_YEAR_IDS) or ex.parse_year(title, body)
    if year is not None and 1980 <= year <= 2035:
        attrs["year"] = year

    mileage = _prop_int(props, _PROP_MILEAGE_IDS)
    if mileage is None:
        mileage = ex.parse_int_km(title, body)
    if mileage is not None:
        attrs["mileage_km"] = mileage

    for prop_id, value in props.items():
        if prop_id in _PROP_ALREADY_HANDLED or not value:
            continue
        attr_name = _PROP_ATTRIBUTE_RENAMES.get(prop_id, prop_id)
        if prop_id in _PROP_INT_IDS:
            digits = re.sub(r"[^\d]", "", str(value))
            attrs[attr_name] = int(digits) if digits else value
        else:
            attrs[attr_name] = value

    return attrs


def listing_from_api_node(node: dict) -> Listing | None:
    """Map one tutti-scraper node (search-summary or full-detail shape) to a Listing.
    Pure + fixture-testable without any network access."""
    ext = node.get("listingID")
    title = (node.get("title") or "").strip()
    if not ext or not title:
        return None

    body = (node.get("body") or "").strip()
    props = _props_by_id(node)
    attrs = _build_attributes(props, title, body)

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
