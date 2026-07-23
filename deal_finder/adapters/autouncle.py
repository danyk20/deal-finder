"""AutoUncle.ch adapter — uses the `autouncle-scraper` PyPI package
(https://pypi.org/project/autouncle-scraper/), which parses schema.org JSON-LD (for
unfiltered searches and every detail page) plus a filtered-search RSC/GraphQL path — no
login, no browser. AutoUncle explicitly marks its listing data CC BY 4.0.

``detail=True`` is not optional here, unlike the AutoScout24/Autolina adapters: as soon
as ANY price/mileage/year filter is set (true of almost every real watch), AutoUncle's
own filtered-search path returns bare listing ids with no summary fields at all -- only
a detail-page visit fills those in. ``max_results=settings.browser_max_items_per_run``
bounds that detail-fetch phase (verified against 0.3.0's actual source: the candidate
list is sliced *before* ``visit_all_listings`` runs, not after -- 0.1.0 had no cap at
all, and 0.2.0's cap only trimmed the *returned* set while still detail-fetching every
match, since recency ("firstSeenAt") is itself a detail-only field).

Residual limitation, confirmed live and accepted rather than silently hidden: the
*search* (id-collection) phase always pages through the entire match count first,
regardless of ``max_results`` -- only the detail-fetch phase is bounded. A very broad,
loosely-filtered watch can still mean a slow-ish scan (tens of seconds), just not the
many-minutes risk that existed pre-0.3.0. Also, ``max_results`` keeps "the first N
AutoUncle's own search returns" -- not confirmed newest-first (unlike Autolina's
``carId``-descending guarantee), since AutoUncle exposes no cheap recency signal outside
of `firstSeenAt`.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable
from typing import Any

from autouncle_scraper import (
    DEFAULT_DOMAIN,
    build_car_search_input,
    count_cars,
    fetch_search_form_config,
    flatten_listing,
    get_domain_config,
    make_session,
    resolve_make_key,
    resolve_model_key,
    scrape,
)

from ..config import Settings, get_settings
from .base import AdapterError, BaseAdapter, Listing, MarketplaceQuery

log = logging.getLogger("deal_finder.adapters.autouncle")

# autouncle-scraper's resolve_model_key() only matches a query that is a substring of a
# listed model name -- unlike its sibling packages (autoscout24-scraper, autolina-scraper),
# it has no fallback for the reverse case, a trim/variant query like "Model S90" that is a
# *superstring* of a listed base model ("Model S"). Both sibling packages already special-
# case exactly this ("Model S90D" for "MODEL S" is autoscout24-scraper's own example). Until
# autouncle-scraper gets the same fallback, this adapter does it client-side: on an "unknown
# model" error, parse the "Available: ..." list it raises and retry once with the single
# listed model name that the query contains (or that contains the query) -- only when that
# match is unambiguous, never guessing between multiple plausible candidates.
_AVAILABLE_MODELS_RE = re.compile(r"Available:\s*(.+)$")


def _find_unambiguous_model_match(model_query: str, error_message: str) -> str | None:
    match = _AVAILABLE_MODELS_RE.search(error_message)
    if not match:
        return None
    available = [name.strip() for name in match.group(1).split(",") if name.strip()]
    q = model_query.strip().lower()
    candidates = [name for name in available if name.lower() in q or q in name.lower()]
    return candidates[0] if len(candidates) == 1 else None


# Priority fields renamed to the naming convention used by the other adapters'
# `attributes`. Everything else `flatten_listing()` produces (equipment_*,
# otherProperties, priceHistory, ...) passes through under its own key via the
# catch-all below, so nothing AutoUncle adds is ever silently dropped.
_ATTRIBUTE_RENAMES: dict[str, str] = {
    "mileageKm": "mileage_km",
    "fuelType": "fuel",
    "bodyType": "body_type",
    "enginePowerPs": "horsepower",
    "enginePowerKw": "power_kw",
    "engineDisplacementL": "engine_displacement_l",
    "co2GKm": "co2_emission_g_km",
    "fuelConsumptionL100km": "fuel_consumption_l_100km",
    "numberOfDoors": "doors",
    "priceRatingLabel": "price_rating",
    "savingsVsMarketChf": "savings_vs_market_chf",
    "daysOnMarket": "days_on_market",
    "sourcePlatform": "source_platform",
    "dealerName": "dealer_name",
    "firstSeenAt": "first_seen_at",
    "lastUpdatedAt": "last_updated_at",
    "priceHistory": "price_history",
}

# Already surfaced as dedicated Listing fields, or metadata with no AI-Q&A value --
# excluded from the generic catch-all pass below.
_SKIP_KEYS = {
    "id", "name", "description", "make", "model", "url",
    "price", "priceCurrency", "imageUrl", "imageCaption", "imageUrls",
    "itemCondition", "availability",
    "addressCountry", "addressLocality", "addressRegion", "postalCode",
    "datasetLicense", "datasetIsAccessibleForFree",
}


def _build_attributes(item: dict) -> dict[str, Any]:
    """Flatten every AI-Q&A-relevant field into ``Listing.attributes``, reusing the
    package's own ``flatten_listing()`` (nested dicts -> parent_child columns, lists ->
    semicolon-joined -- no need to hand-roll that convention here)."""
    attrs: dict[str, Any] = {}
    for key, value in flatten_listing(item).items():
        if key in _SKIP_KEYS or value in (None, ""):
            continue
        attrs[_ATTRIBUTE_RENAMES.get(key, key)] = value
    return attrs


def listing_from_api_item(item: dict) -> Listing | None:
    """Map one autouncle-scraper listing dict to a Listing. Pure + fixture-testable
    without any network access."""
    ext_id = item.get("id")
    if not ext_id:
        return None

    make = (item.get("make") or "").strip()
    model = (item.get("model") or "").strip()
    fallback_title = " ".join(p for p in (make, model) if p).strip()
    title = (item.get("name") or "").strip() or fallback_title
    if not title:
        return None

    location = " ".join(p for p in (item.get("postalCode"), item.get("addressLocality")) if p) or None

    price = item.get("price")
    images = item.get("imageUrls") or ([item["imageUrl"]] if item.get("imageUrl") else [])
    return Listing(
        marketplace="autouncle",
        external_id=str(ext_id),
        url=item.get("url") or "",
        title=title,
        description=(item.get("description") or "").strip(),
        price=float(price) if isinstance(price, (int, float)) else None,
        currency=item.get("priceCurrency") or "CHF",
        location=location,
        attributes=_build_attributes(item),
        image_urls=list(images),
    )


class AutoUncleAdapter(BaseAdapter):
    key = "autouncle"
    label = "AutoUncle.ch"
    supported_categories = {"car"}
    enabled_by_default = True
    status_note = "schema.org JSON-LD + GraphQL (autouncle.ch) via the autouncle-scraper package — no browser needed"

    def search(self, query: MarketplaceQuery, settings: Settings | None = None) -> Iterable[Listing]:
        settings = settings or get_settings()
        p = query.params or {}
        make, model = (p.get("make") or "").strip(), (p.get("model") or "").strip()
        if not make or not model:
            raise AdapterError("AutoUncle.ch requires both Make and Model to be set on the watch")

        scrape_kwargs = dict(
            detail=True,
            price_from=int(query.price_min) if query.price_min is not None else None,
            price_to=int(query.price_max) if query.price_max is not None else None,
            mileage_to=int(p["mileage_max"]) if p.get("mileage_max") else None,
            year_from=int(p["year_min"]) if p.get("year_min") else None,
            year_to=int(p["year_max"]) if p.get("year_max") else None,
            max_results=settings.browser_max_items_per_run,
            delay=0.4,
            verbose=False,
        )

        try:
            result = self._scrape_with_model_fallback(make, model, scrape_kwargs)
        except ValueError as exc:  # bad filters / unknown make-model
            raise AdapterError(f"AutoUncle.ch: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - requests exceptions (incl. stray HTTPError), etc.
            raise AdapterError(f"AutoUncle.ch request failed: {exc}") from exc

        return [li for item in result.listings if (li := listing_from_api_item(item)) is not None]

    @staticmethod
    def _scrape_with_model_fallback(make: str, model: str, scrape_kwargs: dict[str, Any]):
        try:
            return scrape(make, model, **scrape_kwargs)
        except ValueError as exc:
            corrected = _find_unambiguous_model_match(model, str(exc))
            if corrected is None or corrected.lower() == model.lower():
                raise
            log.warning(
                "AutoUncle.ch: no exact match for model %r; retrying with closest listed model %r",
                model,
                corrected,
            )
            return scrape(make, corrected, **scrape_kwargs)

    def health_check(self) -> bool:
        try:
            session = make_session()
            config = fetch_search_form_config(DEFAULT_DOMAIN, session=session)
            make_key = resolve_make_key("Tesla", config)
            model_key = resolve_model_key(make_key, "Model 3", config)
            car_search_input = build_car_search_input(make_key, model_key)
            count_cars(car_search_input, domain_cfg=get_domain_config(DEFAULT_DOMAIN), session=session)
            return True
        except Exception:  # noqa: BLE001
            return False
