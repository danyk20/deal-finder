"""Ricardo.ch adapter — uses the `ricardo-scraper` PyPI package
(https://pypi.org/project/ricardo-scraper/), which drives its own bundled Camoufox
browser (a Firefox build patched against the CDP-level fingerprints Cloudflare's
challenge platform uses to detect plain automation) to get past Cloudflare's Managed
Challenge on ricardo.ch.

Self-contained: no shared deal_finder browser session, persistent profile, or manual
"solve the challenge once" step needed for this adapter anymore -- the package handles
its own browser lifecycle internally, one call to scrape() at a time.

The category filter matters more than it looks: Ricardo is a general marketplace, not
car-specific, so a free-text search for a make/model also surfaces non-car listings that
merely mention it -- confirmed live, a real "Tesla Model X" search returned a wheel/rim
set (Ricardo category "fahrzeugzubehoer", vehicle accessories) and a charger, alongside
actual cars. The rim listing's own "5000 km" was the *wheels'* wear, not a car's mileage
-- if it hadn't been filtered out by category, the naive year/mileage regex below would
have attributed that number to a "car" that doesn't exist. `ricardo-scraper` already
supports this filter (matched against each listing's JSON-LD category breadcrumbs);
requires `detail=True` (already the case here). The category is the one the AI picked for
the watch from the scraper's category list (see category_tree() and site_categories.py);
with a scraper that doesn't ship that list yet, searches stay pinned to "autos".

`ricardo-scraper` >=0.2.1 fixed the year/mileage gap: ricardo.ch's own "Fahrzeug-Klassierung"
characteristics panel (year, mileage, transmission, color, fuel, ...) lives in
`__NEXT_DATA__.props.pageProps.article.attributes` -- a clean `{label, key, values}`
list the package's `extract_next_data()` already fetches on every detail visit, but its
`_extract_extra_fields()` used to never read. Confirmed live (0.2.1): each listing dict
now carries a flattened `attributes` dict (e.g. ``{"auto_first_registration_year":
"2018", "auto_mileage": "93'500 km", "auto_gear_type": "Automat", "car_fuel_type":
"Elektrisch", "vehicle_classification": "Standard", ...}``), plus top-level `color`/
`model` fields the JSON-LD always had but weren't being extracted either. This is now
the primary source for year/mileage/transmission/fuel/color/classification --
structured, not guessed from free text. The regex-over-title+description fallback
(`browser/extract.py`'s `parse_year`/`parse_int_km`) stays as a safety net for whatever
`attributes` doesn't cover for a given listing, rather than being removed outright.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

import ricardo_scraper
from ricardo_scraper import scrape

from ..browser import extract as ex  # shared parse_year / parse_int_km fallback
from ..config import Settings, get_settings
from .base import AdapterError, BaseAdapter, Listing, MarketplaceQuery, SiteCategory

log = logging.getLogger("deal_finder.adapters.ricardo")

# ricardo-scraper's own structured `attributes` keys (car-category specific, confirmed
# live) renamed to the naming convention used by the other adapters' `attributes`.
# car_brand/car_model/color are skipped -- already surfaced via the listing's own
# dedicated `brand`/`model`/`color` fields (see below), no need to duplicate them here.
_ATTRIBUTE_RENAMES: dict[str, str] = {
    "auto_gear_type": "transmission",
    "car_fuel_type": "fuel",
    "vehicle_classification": "classification",
}
_ATTRIBUTE_SKIP_KEYS = {"car_brand", "car_model", "color", "auto_first_registration_year", "auto_mileage"}


def _build_attributes(node: dict, text_blob: str) -> dict[str, Any]:
    """Structured `attributes` (added in ricardo-scraper 0.2.1) is the primary source;
    the regex-over-text fallback only fills in year/mileage when that structured data
    doesn't have them, rather than being dropped now that a better source exists."""
    attrs: dict[str, Any] = {}
    raw_attrs = node.get("attributes") or {}

    year_str = raw_attrs.get("auto_first_registration_year")
    year = int(year_str) if isinstance(year_str, str) and year_str.isdigit() else ex.parse_year(text_blob)
    if year is not None:
        attrs["year"] = year

    mileage = ex.parse_int_km(raw_attrs.get("auto_mileage") or "") or ex.parse_int_km(text_blob)
    if mileage is not None:
        attrs["mileage_km"] = mileage

    for key, value in raw_attrs.items():
        if key in _ATTRIBUTE_SKIP_KEYS or not value:
            continue
        attrs[_ATTRIBUTE_RENAMES.get(key, key)] = value

    color = node.get("color")
    if color:
        attrs["color"] = color

    return attrs


def listing_from_api_node(node: dict) -> Listing | None:
    """Map one ricardo-scraper listing record to a Listing. Pure + fixture-testable
    without any network access."""
    ext = node.get("id")
    title = (node.get("title") or "").strip()
    if not ext or not title:
        return None

    description = (node.get("description") or "").strip()
    text_blob = f"{title}\n{description}"
    attrs = _build_attributes(node, text_blob)

    location = ", ".join(p for p in (node.get("location_zip"), node.get("location_city")) if p) or None

    return Listing(
        marketplace="ricardo",
        external_id=str(ext),
        url=node.get("url") or "",
        title=title,
        description=description,
        price=node.get("price"),
        currency=node.get("currency") or "CHF",
        location=location,
        attributes=attrs,
        image_urls=(node.get("images") or [])[:8],
    )


class RicardoAdapter(BaseAdapter):
    key = "ricardo"
    label = "Ricardo.ch"
    supported_categories = {"car", "general"}  # sells everything, not just cars
    enabled_by_default = True
    status_note = "ricardo-scraper package (Camoufox browser, bypasses Cloudflare) — no shared browser session needed"

    def search(self, query: MarketplaceQuery, settings: Settings | None = None) -> Iterable[Listing]:
        text = (query.text or " ".join(query.terms)).strip()
        if not text:
            raise AdapterError("Ricardo.ch: no search text set on the watch")

        settings = settings or get_settings()
        if _CATEGORIES:
            category = query.site_categories.get(self.key)  # AI-picked; None -> all categories
        else:
            category = "autos"  # scraper without a category list: keep the old car-only pin
        try:
            result = scrape(
                text,
                locale="de",
                detail=True,
                category=category,
                max_results=settings.browser_max_items_per_run,
                price_from=query.price_min,
                price_to=query.price_max,
                delay=1.5,
                verbose=False,
                headless=True,
            )
        except ValueError as exc:  # bad filters (e.g. price_from > price_to)
            raise AdapterError(f"Ricardo.ch: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - network/browser-launch errors, etc.
            raise AdapterError(f"Ricardo.ch request failed: {exc}") from exc

        return [li for node in result.listings if (li := listing_from_api_node(node)) is not None]

    def health_check(self) -> bool:
        try:
            scrape("Tesla", detail=False, max_results=1, verbose=False)
            return True
        except Exception:  # noqa: BLE001
            return False

    def category_tree(self) -> list[SiteCategory]:
        return [
            SiteCategory(str(c["id"]), c["name"], str(c["parent_id"]) if c.get("parent_id") is not None else None)
            for c in _CATEGORIES
        ]


# Ricardo's full category list (id/name/parent_id/depth/path, ~1700 entries), once
# ricardo-scraper ships it importable as ``ricardo_scraper.CATEGORIES``. Up to 0.2.2 it only
# exists in the repo's docs/categories.json, which the wheel doesn't include -- then this is
# empty, no AI category pick happens for Ricardo, and searches stay pinned to "autos".
_CATEGORIES: list[dict[str, Any]] = list(getattr(ricardo_scraper, "CATEGORIES", None) or [])
