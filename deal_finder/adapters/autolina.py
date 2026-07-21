"""Autolina.ch adapter — uses the `autolina-scraper` PyPI package
(https://pypi.org/project/autolina-scraper/), which is deliberately signature-compatible
with autoscout24-scraper but parses the site's own server-rendered HTML directly rather
than calling a JSON API. Plain ``requests``, no browser, no anti-bot bypass.

Single ``scrape()`` call (unlike autoscout24's two-phase search/detail split), but capped
the same way: ``max_results=settings.browser_max_items_per_run`` bounds how many listings
get the expensive per-listing detail-page visit when ``detail=True`` (the default, used
here for the richest field set). Note this caps *cost*, not "top N by some criterion" --
autolina.ch's search doesn't return listings in a guaranteed price/date order the way
autoscout24's API does, so (unlike that adapter) there's no pre-detail sort to bias
towards the newest listings first.

Most listings carry no free-text seller description (autolina.ch's own UI is spec-first),
but private-seller listings often do -- both are mapped into ``Listing.title``/``description``
when present, falling back to a make+trim title and empty description otherwise.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from typing import Any

from autolina_scraper import scrape

from ..config import Settings, get_settings
from .base import AdapterError, BaseAdapter, Listing, MarketplaceQuery

log = logging.getLogger("deal_finder.adapters.autolina")

# Detail-page fields renamed to clearer, stable attribute keys. autolina.ch's spec rows
# are scraped generically (label text -> slugified column, see autolina_scraper.htmlparse)
# so the raw keys are German UI labels; the ones below are common across virtually every
# listing regardless of fuel type/body style.
_ATTRIBUTE_RENAMES: dict[str, str] = {
    "getriebeart": "transmission",
    "treibstoff": "fuel",
    "fahrzeugzustand": "condition",
    "farbe_aussen_innen": "color",
    "antrieb": "drivetrain",
    "aufbau": "body_type",
    "fahrgestell_nr": "vin",
    "erstzulassung": "first_registration",
    "letzte_pruefung_mfk": "last_inspection_mfk",
    "garantie": "warranty",
    "neupreis": "new_price_chf",
    "tueren_sitze_doors": "doors",
    "tueren_sitze_seats": "seats",
    "stromverbrauch": "energy_consumption",
    "verbrauch": "fuel_consumption",
    "energieeffizienz": "energy_efficiency_class",
    "co2_emission": "co2_emission_g_km",
    "euro_norm": "euro_norm",
    "typengenehmigung": "type_approval",
    "wagen_nr": "dealer_stock_number",
}

# Already surfaced as dedicated Listing fields, redundant string duplicates of a
# numeric field already mapped (kilometer/preis/leistung_hubraum), or internal
# bookkeeping with no AI-Q&A value -- excluded from both the explicit renames above
# and the generic catch-all pass below.
_SKIP_KEYS = {
    "carId", "slug", "url", "make", "modelType", "price", "previousPrice", "preis",
    "constructionYear", "mileage", "kilometer", "powerOutput", "leistung_hubraum",
    "isNew", "isPremium", "imageUrl", "images", "location", "dealer",
    "serienmaessige_ausstattung", "optionale_ausstattung",
    "hitCount", "lastUpdatedDateLabel", "adTitle", "beschreibung",
}

_EQUIPMENT_ATTRIBUTE_NAMES = {
    "serienmaessige_ausstattung": "standard_equipment",
    "optionale_ausstattung": "optional_equipment",
}


def _build_attributes(item: dict) -> dict[str, Any]:
    """Flatten every AI-Q&A-relevant field from the raw listing dict into
    ``Listing.attributes``. Known common fields get clear, stable names; anything else
    the site adds later still reaches the AI via the generic catch-all pass, mirroring
    the scraper package's own "a new spec row becomes a new column automatically"
    design instead of being silently dropped."""
    attrs: dict[str, Any] = {}

    year = item.get("constructionYear")
    if isinstance(year, int):
        attrs["year"] = year
    mileage = item.get("mileage")
    if isinstance(mileage, (int, float)):
        attrs["mileage_km"] = int(mileage)
    power = item.get("powerOutput")
    if isinstance(power, (int, float)):
        attrs["horsepower"] = int(power)

    for raw_key, attr_name in _ATTRIBUTE_RENAMES.items():
        value = item.get(raw_key)
        if value not in (None, ""):
            attrs[attr_name] = value

    for raw_key, attr_name in _EQUIPMENT_ATTRIBUTE_NAMES.items():
        items = item.get(raw_key)
        if items:
            attrs[attr_name] = "; ".join(items)

    dealer = item.get("dealer") if isinstance(item.get("dealer"), dict) else {}
    if dealer.get("name"):
        attrs["dealer_name"] = dealer["name"]
    if dealer.get("phone"):
        attrs["dealer_phone"] = dealer["phone"]
    if dealer.get("address"):
        attrs["dealer_address"] = dealer["address"]

    consumed = _SKIP_KEYS | set(_ATTRIBUTE_RENAMES) | set(_EQUIPMENT_ATTRIBUTE_NAMES)
    for key, value in item.items():
        if key in consumed or value in (None, ""):
            continue
        if isinstance(value, (str, int, float, bool)):
            attrs[key] = value

    return attrs


def listing_from_api_item(item: dict) -> Listing | None:
    """Map one autolina-scraper listing dict to a Listing. Pure + fixture-testable
    without any network access."""
    ext_id = item.get("carId")
    if ext_id is None:
        return None

    make = (item.get("make") or "").strip()
    model_type = (item.get("modelType") or "").strip()
    fallback_title = " ".join(p for p in (make, model_type) if p).strip()
    title = (item.get("adTitle") or "").strip() or fallback_title
    if not title:
        return None

    price = item.get("price")
    return Listing(
        marketplace="autolina",
        external_id=str(ext_id),
        url=item.get("url") or "",
        title=title,
        description=(item.get("beschreibung") or "").strip(),
        price=float(price) if isinstance(price, (int, float)) else None,
        currency="CHF",
        location=(item.get("location") or None),
        attributes=_build_attributes(item),
        image_urls=list(item.get("images") or ([item["imageUrl"]] if item.get("imageUrl") else [])),
    )


class AutolinaAdapter(BaseAdapter):
    key = "autolina"
    label = "Autolina.ch"
    supported_categories = {"car"}
    enabled_by_default = True
    status_note = "public HTML parsing (autolina.ch) via the autolina-scraper package — no browser needed"

    def search(self, query: MarketplaceQuery, settings: Settings | None = None) -> Iterable[Listing]:
        settings = settings or get_settings()
        p = query.params or {}
        make, model = (p.get("make") or "").strip(), (p.get("model") or "").strip()
        if not make or not model:
            raise AdapterError("Autolina.ch requires both Make and Model to be set on the watch")

        try:
            result = scrape(
                make,
                model,
                detail=True,
                price_from=int(query.price_min) if query.price_min is not None else None,
                price_to=int(query.price_max) if query.price_max is not None else None,
                mileage_to=int(p["mileage_max"]) if p.get("mileage_max") else None,
                year_from=int(p["year_min"]) if p.get("year_min") else None,
                year_to=int(p["year_max"]) if p.get("year_max") else None,
                max_results=settings.browser_max_items_per_run,
                delay=1.0,
                verbose=False,
            )
        except ValueError as exc:  # bad filters / unknown make-model
            raise AdapterError(f"Autolina.ch: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - requests exceptions, ChallengeDetectedError, etc.
            raise AdapterError(f"Autolina.ch request failed: {exc}") from exc

        return [li for item in result.listings if (li := listing_from_api_item(item)) is not None]

    def health_check(self) -> bool:
        try:
            scrape("Tesla", "Model S", detail=False, max_results=1, verbose=False)
            return True
        except Exception:  # noqa: BLE001
            return False
