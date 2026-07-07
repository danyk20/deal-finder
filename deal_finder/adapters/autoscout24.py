"""AutoScout24.ch adapter — uses the `autoscout24-scraper` PyPI package
(https://pypi.org/project/autoscout24-scraper/), which calls the site's own public,
UNAUTHENTICATED JSON API directly (``api.autoscout24.ch``). There is no Cloudflare/Akamai
challenge on that API host, so — unlike tutti/Ricardo/AutoScout24's own HTML frontend —
this adapter needs no browser, no stealth, and no anti-bot bypass of any kind. It's a
plain HTTP JSON adapter, exactly like the pattern used elsewhere for simple REST APIs.

Two-phase fetch (mirrors the library's own ``scrape()``, but capped between phases so a
broad watch doesn't trigger hundreds of detail requests every run):
  1. ``search_listings`` — cheap, paginated, summary fields only.
  2. ``visit_all_listings`` — one GET per listing, for the full description/images used by
     translation + AI Q&A. Capped to the newest ``browser_max_items_per_run`` candidates;
     the matching engine's dedup means any candidates dropped this run are simply picked
     up (or already seen) on a later scan.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime
from typing import Any

from autoscout24_scraper import make_session, resolve_make_key, resolve_model_key, search_listings, visit_all_listings

from ..config import Settings, get_settings
from .base import AdapterError, BaseAdapter, Listing, MarketplaceQuery

log = logging.getLogger("deal_finder.adapters.autoscout24")

# Image "key" values from the API need this CDN host prefixed to become a real URL.
AS24_IMG_BASE = "https://listing-images.autoscout24.ch/"


def _name(v: Any) -> str:
    return (v.get("name") or "") if isinstance(v, dict) else (v or "")


def _image_urls(images: Any) -> list[str]:
    urls: list[str] = []
    for img in images or []:
        key = img.get("key") if isinstance(img, dict) else img
        if isinstance(key, str) and key:
            urls.append(key if key.startswith("http") else AS24_IMG_BASE + key.lstrip("/"))
    return urls


def _posted_at(raw: Any) -> datetime | None:
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def listing_from_api_item(item: dict) -> Listing | None:
    """Map one autoscout24-scraper item (search-summary or merged-detail shape) to a
    Listing. Pure + fixture-testable without any network access."""
    ext_id = item.get("id")
    if ext_id is None:
        return None

    make_name, model_name = _name(item.get("make")), _name(item.get("model"))
    version = (item.get("versionFullName") or "").strip()
    # versionFullName from the API sometimes already repeats the model name (e.g.
    # model="MODEL S", versionFullName="Model S 100 D") -> avoid "MODEL S Model S 100 D".
    if model_name and version.lower().startswith(model_name.lower()):
        model_name = ""
    title = " ".join(p for p in (make_name, model_name, version) if p).strip()
    if not title:
        return None

    seller = item.get("seller") if isinstance(item.get("seller"), dict) else {}
    location = " ".join(str(x) for x in (seller.get("zipCode"), seller.get("city")) if x) or None

    attrs: dict[str, int] = {}
    year = item.get("firstRegistrationYear")
    if isinstance(year, int):
        attrs["year"] = year
    mileage = item.get("mileage")
    if isinstance(mileage, (int, float)):
        attrs["mileage_km"] = int(mileage)

    price = item.get("price")
    return Listing(
        marketplace="autoscout24",
        external_id=str(ext_id),
        url=item.get("url") or f"https://www.autoscout24.ch/de/d/{ext_id}",
        title=title,
        description=(item.get("description") or item.get("teaser") or "").strip(),
        language=None,
        price=float(price) if isinstance(price, (int, float)) else None,
        currency="CHF",
        location=location,
        posted_at=_posted_at(item.get("createdDate")),
        attributes=attrs,
        image_urls=_image_urls(item.get("images")),
    )


class AutoScout24Adapter(BaseAdapter):
    key = "autoscout24"
    label = "AutoScout24.ch"
    supported_categories = {"car"}
    enabled_by_default = True
    status_note = "public JSON API (api.autoscout24.ch) via the autoscout24-scraper package — no browser needed"

    def search(self, query: MarketplaceQuery, settings: Settings | None = None) -> Iterable[Listing]:
        settings = settings or get_settings()
        p = query.params or {}
        make, model = (p.get("make") or "").strip(), (p.get("model") or "").strip()
        if not make or not model:
            raise AdapterError("AutoScout24.ch requires both Make and Model to be set on the watch")

        session = make_session()
        try:
            make_key, _ = resolve_make_key(session, make)
            model_key, _ = resolve_model_key(session, make_key, model)
            candidates = search_listings(
                session,
                make_key,
                model_key,
                verbose=False,
                price_from=int(query.price_min) if query.price_min is not None else None,
                price_to=int(query.price_max) if query.price_max is not None else None,
                mileage_to=int(p["mileage_max"]) if p.get("mileage_max") else None,
                year_from=int(p["year_min"]) if p.get("year_min") else None,
                year_to=int(p["year_max"]) if p.get("year_max") else None,
            )
        except ValueError as exc:  # unknown make/model
            raise AdapterError(f"AutoScout24.ch: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 - requests.RequestException / HTTPError etc.
            raise AdapterError(f"AutoScout24.ch request failed: {exc}") from exc

        # Newest first, then cap the (slower, one-request-per-listing) detail phase.
        candidates.sort(key=lambda c: c.get("createdDate") or "", reverse=True)
        capped = candidates[: settings.browser_max_items_per_run]

        try:
            detailed = visit_all_listings(session, capped, verbose=False)
        except Exception as exc:  # noqa: BLE001 - degrade to summary fields rather than fail the run
            log.warning("autoscout24: detail fetch failed (%s); using summary fields only", exc)
            detailed = capped

        return [li for item in detailed if (li := listing_from_api_item(item)) is not None]

    def health_check(self) -> bool:
        try:
            session = make_session()
            make_key, _ = resolve_make_key(session, "tesla")
            model_key, _ = resolve_model_key(session, make_key, "model-s")
            search_listings(session, make_key, model_key, verbose=False)
            return True
        except Exception:  # noqa: BLE001
            return False
