"""Ricardo.ch adapter — uses the `ricardo-scraper` PyPI package
(https://pypi.org/project/ricardo-scraper/), which drives its own bundled Camoufox
browser (a Firefox build patched against the CDP-level fingerprints Cloudflare's
challenge platform uses to detect plain automation) to get past Cloudflare's Managed
Challenge on ricardo.ch.

Self-contained: no shared deal_finder browser session, persistent profile, or manual
"solve the challenge once" step needed for this adapter anymore -- the package handles
its own browser lifecycle internally, one call to scrape() at a time.

``category="autos"`` matters more than it looks: Ricardo is a general marketplace, not
car-specific, so a free-text search for a make/model also surfaces non-car listings that
merely mention it -- confirmed live, a real "Tesla Model X" search returned a wheel/rim
set (Ricardo category "fahrzeugzubehoer", vehicle accessories) and a charger, alongside
actual cars. The rim listing's own "5000 km" was the *wheels'* wear, not a car's mileage
-- if it hadn't been filtered out by category, the naive year/mileage regex below would
have attributed that number to a "car" that doesn't exist. `ricardo-scraper` already
supports this filter (matched against each listing's JSON-LD category breadcrumbs);
requires `detail=True` (already the case here).

Known limitation, confirmed live rather than assumed: `ricardo-scraper` doesn't expose
year/mileage/transmission/fuel/color even for genuine cars, so this adapter falls back
to a regex over title+description (`browser/extract.py`'s `parse_year`/`parse_int_km`,
shared with other adapters' own unstructured-text fallback) -- which finds nothing for a
listing whose description is pure feature-bullet text with no year/mileage mentioned in
words. The real data exists: ricardo.ch's own "Fahrzeug-Klassierung" characteristics
panel (year, mileage, transmission, color, fuel, ...) lives in
`__NEXT_DATA__.props.pageProps.article.attributes` -- a clean `{label, key, values}`
list the package's own `extract_next_data()` already fetches as part of every detail
visit, but its `_extract_extra_fields()` never reads. Until that's exposed upstream (or
this adapter reimplements the detail-visit loop itself instead of using the package's
`visit_all_listings()`, which currently discards it), year/mileage stay regex-derived
and can be missing or wrong for listings whose description doesn't restate them in text.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from ricardo_scraper import scrape

from ..browser import extract as ex  # shared parse_year / parse_int_km
from ..config import Settings, get_settings
from .base import AdapterError, BaseAdapter, Listing, MarketplaceQuery

log = logging.getLogger("deal_finder.adapters.ricardo")


def listing_from_api_node(node: dict) -> Listing | None:
    """Map one ricardo-scraper listing record to a Listing. Pure + fixture-testable
    without any network access."""
    ext = node.get("id")
    title = (node.get("title") or "").strip()
    if not ext or not title:
        return None

    description = (node.get("description") or "").strip()
    # ricardo-scraper doesn't expose structured year/mileage fields (only tutti's API
    # does) -- fall back to regex over the title+description, same as deal_finder's own
    # extract.py fallback used for every other adapter's unstructured text.
    text_blob = f"{title}\n{description}"
    attrs: dict[str, int] = {}
    year = ex.parse_year(text_blob)
    if year is not None:
        attrs["year"] = year
    mileage = ex.parse_int_km(text_blob)
    if mileage is not None:
        attrs["mileage_km"] = mileage

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
    supported_categories = {"car"}
    enabled_by_default = True
    status_note = "ricardo-scraper package (Camoufox browser, bypasses Cloudflare) — no shared browser session needed"

    def search(self, query: MarketplaceQuery, settings: Settings | None = None) -> Iterable[Listing]:
        text = (query.text or " ".join(query.terms)).strip()
        if not text:
            raise AdapterError("Ricardo.ch: no search text (make/model) set on the watch")

        settings = settings or get_settings()
        try:
            result = scrape(
                text,
                locale="de",
                detail=True,
                category="autos",
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
