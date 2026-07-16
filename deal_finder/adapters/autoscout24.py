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

import calendar
import logging
import re
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


_DRIVE_TYPE_LABELS = {
    "all": "all-wheel drive (dual motor)",
    "front": "front-wheel drive",
    "rear": "rear-wheel drive",
}

# Tesla's own naming convention: a trim ending in "D" ("90D", "P100D", "90 D", ...) is
# always dual-motor AWD; used as a fallback when the API's own `driveType` field is
# null/missing, so a version name that plainly says "D" doesn't get lost.
_DUAL_MOTOR_VERSION_RE = re.compile(r"\b\d{2,3}\s*D\b", re.IGNORECASE)


def _yes_no(v: Any) -> str | None:
    return None if v is None else ("yes" if v else "no")


def _registration_month_name(date_str: Any) -> str | None:
    if not isinstance(date_str, str):
        return None
    try:
        d = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
    except ValueError:
        return None
    return calendar.month_name[d.month]


def _build_attributes(item: dict, version: str = "") -> dict[str, Any]:
    """Flatten every AI-Q&A-relevant structured field from the API's raw item into
    ``Listing.attributes``. The API exposes far more than year/mileage (accident
    history, drive type, supercharging, warranty, ...) and free-text questions often ask
    about exactly these facts -- if a field isn't mapped here, the AI can never answer
    from it, even when the raw payload states it explicitly (e.g. ``teaser``, which used
    to be dropped outright whenever a real ``description`` was also present)."""
    attrs: dict[str, Any] = {}

    year = item.get("firstRegistrationYear")
    if isinstance(year, int):
        attrs["year"] = year
    reg_month = _registration_month_name(item.get("firstRegistrationDate"))
    if reg_month:
        attrs["registration_month"] = reg_month
    mileage = item.get("mileage")
    if isinstance(mileage, (int, float)):
        attrs["mileage_km"] = int(mileage)
    if item.get("fuelType"):
        attrs["fuel"] = item["fuelType"]
    if item.get("driveType"):
        attrs["drive_type"] = _DRIVE_TYPE_LABELS.get(item["driveType"], item["driveType"])
    elif _DUAL_MOTOR_VERSION_RE.search(version or ""):
        attrs["drive_type"] = "all-wheel drive (dual motor) -- inferred from 'D' in the model designation"
    if item.get("transmissionType"):
        attrs["transmission"] = item["transmissionType"]
    if item.get("conditionType"):
        attrs["condition"] = item["conditionType"]
    had_accident = _yes_no(item.get("hadAccident"))
    if had_accident is not None:
        attrs["had_accident"] = had_accident
    if item.get("doors"):
        attrs["doors"] = item["doors"]
    if item.get("seats"):
        attrs["seats"] = item["seats"]
    if item.get("bodyColor"):
        attrs["body_color"] = item["bodyColor"]
    if item.get("interiorColor"):
        attrs["interior_color"] = item["interiorColor"]
    if item.get("horsePower"):
        attrs["horsepower"] = item["horsePower"]
    if item.get("kiloWatts"):
        attrs["power_kw"] = item["kiloWatts"]
    if item.get("batteryCapacity"):
        attrs["battery_capacity_kwh"] = item["batteryCapacity"]
    if item.get("range"):
        attrs["range_km"] = item["range"]
    if item.get("chargingPlugType"):
        attrs["charging_plug_type"] = item["chargingPlugType"]
    if item.get("fastChargingPlugType"):
        attrs["fast_charging_plug_type"] = item["fastChargingPlugType"]
    warranty = item.get("warranty") if isinstance(item.get("warranty"), dict) else {}
    if warranty.get("type") and warranty["type"] != "none":
        attrs["warranty"] = warranty["type"]
    inspected = _yes_no(item.get("inspected"))
    if inspected is not None:
        attrs["inspected_mfk"] = inspected
    has_new_tires = _yes_no(item.get("hasNewTires"))
    if has_new_tires is not None:
        attrs["has_new_tires"] = has_new_tires
    has_extra_tires = _yes_no(item.get("hasAdditionalSetOfTires"))
    if has_extra_tires is not None:
        attrs["has_additional_tire_set"] = has_extra_tires
    co2 = item.get("co2Emission")
    if co2 is not None:
        attrs["co2_emission_g_km"] = co2
    consumption = item.get("consumption") if isinstance(item.get("consumption"), dict) else {}
    if consumption.get("combined") is not None:
        attrs["consumption_combined_l_100km"] = consumption["combined"]
    # AutoScout24's short marketing blurb -- kept separate from `description` (rather
    # than only used as a fallback when there's no description) so facts stated ONLY in
    # the teaser (e.g. "Kein free supercharging") always reach the AI's context.
    teaser = (item.get("teaser") or "").strip()
    if teaser:
        attrs["teaser"] = teaser

    return attrs


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

    attrs = _build_attributes(item, version=version)

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
