"""AutoUncle.ch adapter — uses the `autouncle-scraper` PyPI package
(https://pypi.org/project/autouncle-scraper/), which parses schema.org JSON-LD (for
unfiltered searches) plus a filtered-search RSC/GraphQL path — no login, no browser.
AutoUncle explicitly marks its listing data CC BY 4.0.

``max_results=settings.browser_max_items_per_run`` bounds the detail-fetch phase
(verified against 0.3.0's actual source: the candidate list is sliced *before* the
detail visit runs, not after -- 0.1.0 had no cap at all, and 0.2.0's cap only trimmed
the *returned* set while still detail-fetching every match).

Residual limitation, confirmed live and accepted rather than silently hidden: the
*search* (id-collection) phase always pages through the entire match count first,
regardless of ``max_results`` -- only the detail-fetch phase is bounded. A very broad,
loosely-filtered watch can still mean a slow-ish scan (tens of seconds), just not the
many-minutes risk that existed pre-0.3.0. Also, ``max_results`` keeps "the first N
AutoUncle's own search returns" -- not confirmed newest-first (unlike Autolina's
``carId``-descending guarantee), since AutoUncle exposes no cheap recency signal.

This adapter calls the search/detail building blocks directly rather than the
`scrape()` convenience wrapper, specifically to work around a gap confirmed live in
0.4.0: that version added a search-summary-only "modelVariant" field (Tesla's actual
battery/trim code, e.g. "P90D (Free Supercharging)", "100 D") -- exactly the piece
previously missing that made trim-specific watches fail to match genuinely matching
listings. But `scrape(detail=True)` (which every real watch needs -- see below) fully
*replaces* each listing with a fresh per-id detail-page fetch instead of merging, and
`modelVariant` has no equivalent field on the detail page at all, so it silently
vanishes the moment `detail=True` is used. Calling `search_listings`/
`search_listings_filtered` and `visit_all_listings` ourselves and merging (detail
values win when both have one; search-summary-only fields like `modelVariant`,
`priceChangePercent`, `estimatedMarketPriceChf`, `sourcePath` survive since detail
never sets them) keeps everything. ``detail`` is still effectively mandatory: as soon
as ANY price/mileage/year filter is set (true of almost every real watch), the search
summary alone has no fuel type/transmission/engine power/CO2 figures -- only a detail
visit fills those in.
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
    search_listings,
    search_listings_filtered,
    visit_all_listings,
)

from ..config import Settings, get_settings
from .base import AdapterError, BaseAdapter, Listing, MarketplaceQuery

log = logging.getLogger("deal_finder.adapters.autouncle")

# CarSearchInput keys that only identify make/model (mirrors autouncle-scraper's own
# private `_CAR_SEARCH_INPUT_PATH_KEYS` -- kept as a local literal rather than importing
# a leading-underscore name). Any other key present means the search needs the
# filtered/RSC path instead of plain unfiltered JSON-LD.
_CAR_SEARCH_INPUT_IDENTITY_KEYS = frozenset({"brand", "carModel", "brandsModels"})

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
    "sourcePath": "source_path",
    "dealerName": "dealer_name",
    "firstSeenAt": "first_seen_at",
    "lastUpdatedAt": "last_updated_at",
    "priceHistory": "price_history",
    "priceChangePercent": "price_change_percent",
    "estimatedMarketPriceChf": "estimated_market_price_chf",
}

# Already surfaced as dedicated Listing fields, or metadata with no AI-Q&A value --
# excluded from the generic catch-all pass below. `modelVariant` becomes part of
# Listing.title (see listing_from_api_item) rather than a separate attribute, same
# treatment as Autolina's `adTitle`.
_SKIP_KEYS = {
    "id", "name", "description", "make", "model", "modelVariant", "url",
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
    year = item.get("year")
    model_variant = (item.get("modelVariant") or "").strip()
    # `modelVariant` (added in autouncle-scraper 0.4.0) is the actual battery/trim code
    # (e.g. "P90D (Free Supercharging)", "100 D") -- exactly the piece previously
    # missing that made a trim-specific watch ("Model S90") fail to match a genuinely
    # matching listing. It's part of the vehicle identity, so it belongs in
    # vehicle_title itself, not bolted on afterward.
    vehicle_title = " ".join(str(p) for p in (make, model, model_variant, year) if p)

    name = (item.get("name") or "").strip()
    # `name` is AutoUncle's own auto-generated headline (e.g. "Gebraucht 2015 Tesla
    # Model S Performance 772 PS") and usually already carries the make/model -- but it
    # does NOT reliably carry `modelVariant`'s exact battery/trim code (it says
    # "Performance", a rough tier word, not "P90D"), so `name` can never be trusted
    # alone (same principle as the Autolina fix, one level stricter): always lead with
    # the structured vehicle_title -- which does carry modelVariant -- and append `name`
    # afterward for its own extra context, when it's not just a duplicate.
    base_title = " — ".join(p for p in (vehicle_title, name if name and name != vehicle_title else None) if p)

    # `modelVariant` isn't always present (older/thinner listings, or a card this
    # release's parser couldn't confidently read), and even when present it doesn't
    # cover every possible watch phrasing -- so still surface every other spec fact
    # AutoUncle splits across separate fields (power in kW alongside name's PS,
    # transmission, fuel type) directly in the title, so a listing is judgable at a
    # glance from the filtered-out list instead of only via a click-through.
    # (bodyType is deliberately excluded -- confirmed unreliable, e.g. a real Model S
    # sedan reported as "Kleinwagen"/subcompact.)
    power_kw = item.get("enginePowerKw")
    spec_bits = [f"{power_kw} kW" if power_kw else None, item.get("transmission"), item.get("fuelType")]
    spec_suffix = ", ".join(b for b in spec_bits if b)
    title = f"{base_title} ({spec_suffix})" if base_title and spec_suffix else base_title
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


def _fetch_listings(
    make: str,
    model: str,
    *,
    price_from: int | None,
    price_to: int | None,
    mileage_to: int | None,
    year_from: int | None,
    year_to: int | None,
    max_results: int | None,
    delay: float,
    verbose: bool,
) -> list[dict]:
    """Search + detail-fetch, merging so search-summary-only fields (``modelVariant``,
    price rating/change, source platform, ...) survive the detail visit instead of being
    silently dropped by it -- see the module docstring for why this can't just be
    ``scrape(detail=True)``."""
    for lo, hi, name in ((price_from, price_to, "price"), (year_from, year_to, "year")):
        if lo is not None and hi is not None and lo > hi:
            raise ValueError(f"{name}_from ({lo}) cannot be greater than {name}_to ({hi})")

    session = make_session()
    config = fetch_search_form_config(DEFAULT_DOMAIN, session=session)
    make_key = resolve_make_key(make, config)
    model_key = resolve_model_key(make_key, model, config)

    car_search_input = build_car_search_input(
        make_key,
        model_key,
        price_from=price_from,
        price_to=price_to,
        mileage_to=mileage_to,
        year_from=year_from,
        year_to=year_to,
    )
    domain_cfg = get_domain_config(DEFAULT_DOMAIN)
    filtered = any(k not in _CAR_SEARCH_INPUT_IDENTITY_KEYS for k in car_search_input)

    if filtered:
        summaries = search_listings_filtered(
            make_key, model_key, domain_cfg, car_search_input, session=session, delay=delay, verbose=verbose
        )
    else:
        summaries = search_listings(make_key, model_key, domain_cfg, session=session, delay=delay, verbose=verbose)

    if max_results is not None and len(summaries) > max_results:
        if verbose:
            log.info(
                "AutoUncle.ch: opening only the first %d of %d matching listings (max_results=%d)",
                max_results,
                len(summaries),
                max_results,
            )
        summaries = summaries[:max_results]

    by_id = {item["id"]: item for item in summaries if item.get("id")}
    details = visit_all_listings(list(by_id), domain_cfg=domain_cfg, session=session, delay=delay, verbose=verbose)

    merged = []
    for detail in details:
        summary = by_id.get(detail.get("id"), {})
        # Detail values win wherever both phases set a field; a search-summary-only
        # field (modelVariant, priceChangePercent, ...) has no key at all in `detail`,
        # so it survives untouched.
        merged.append({**summary, **{k: v for k, v in detail.items() if v is not None}})
    return merged


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

        fetch_kwargs = dict(
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
            listings = self._fetch_with_model_fallback(make, model, fetch_kwargs)
        except ValueError as exc:  # bad filters / unknown make-model
            raise AdapterError(f"AutoUncle.ch: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - requests exceptions (incl. stray HTTPError), etc.
            raise AdapterError(f"AutoUncle.ch request failed: {exc}") from exc

        return [li for item in listings if (li := listing_from_api_item(item)) is not None]

    @staticmethod
    def _fetch_with_model_fallback(make: str, model: str, fetch_kwargs: dict[str, Any]) -> list[dict]:
        try:
            return _fetch_listings(make, model, **fetch_kwargs)
        except ValueError as exc:
            corrected = _find_unambiguous_model_match(model, str(exc))
            if corrected is None or corrected.lower() == model.lower():
                raise
            log.warning(
                "AutoUncle.ch: no exact match for model %r; retrying with closest listed model %r",
                model,
                corrected,
            )
            return _fetch_listings(make, corrected, **fetch_kwargs)

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
