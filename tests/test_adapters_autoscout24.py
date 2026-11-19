"""AutoScout24.ch adapter tests — no real network.

The adapter calls the `autoscout24-scraper` package, which itself talks to the site's
public JSON API (api.autoscout24.ch). We monkeypatch that package's functions (the same
seam the adapter imports) rather than mocking HTTP, and pin the field-mapping against a
real captured payload (tests/fixtures/autoscout24_listings.json), mirroring the
fixture-test pattern used across the other adapters.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deal_finder.adapters import autoscout24 as as24
from deal_finder.adapters.autoscout24 import AutoScout24Adapter, listing_from_api_item
from deal_finder.adapters.base import AdapterError, MarketplaceQuery
from deal_finder.config import Settings

_FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "autoscout24_listings.json").read_text()
)


def _query(**params) -> MarketplaceQuery:
    return MarketplaceQuery(category="car", terms=["Tesla", "Model S"], params=params)


# --- pure field mapping (real captured data) --------------------------------


def test_listing_from_api_item_real_fixture():
    listings = [listing_from_api_item(item) for item in _FIXTURE]
    assert all(li is not None for li in listings)
    li = listings[0]
    assert li.marketplace == "autoscout24"
    assert li.external_id.isdigit()
    assert li.url.startswith("https://www.autoscout24.ch/de/d/")
    assert "TESLA" in li.title and "MODEL S" in li.title
    assert li.price and li.price > 0 and li.currency == "CHF"
    assert li.attributes["year"] >= 1980
    assert li.attributes["mileage_km"] > 0
    assert li.description  # real listing had a description
    assert li.image_urls and li.image_urls[0].startswith("https://listing-images.autoscout24.ch/")


def test_listing_from_api_item_keeps_who_publishes_the_ad():
    li = listing_from_api_item(_FIXTURE[0])  # real payload: a private seller in Lindau
    assert (li.attributes["seller_name"], li.attributes["seller_type"]) == ("Claudio", "private")
    assert li.location == "8312 Lindau"


def test_listing_from_api_item_infers_dual_motor_from_version_name():
    """Regression: some AutoScout24 listings have `driveType: null` even though the
    Tesla trim name itself ("90 D", "P100D", ...) unambiguously means dual-motor AWD --
    must not fall back to "not stated" when the naming already answers the question."""
    item = {
        "id": 1, "make": {"name": "TESLA"}, "model": {"name": "MODEL S"},
        "versionFullName": "Model S 90 D",
        "driveType": None,
    }
    li = listing_from_api_item(item)
    assert "dual motor" in li.attributes["drive_type"]


def test_listing_from_api_item_propagates_seats_power_and_registration():
    item = {
        "id": 1, "make": {"name": "TESLA"}, "model": {"name": "MODEL S"},
        "versionFullName": "Model S 90 D",
        "seats": 5,
        "horsePower": 525,
        "kiloWatts": 386,
        "firstRegistrationYear": 2016,
        "firstRegistrationDate": "2016-10-01",
    }
    li = listing_from_api_item(item)
    assert li.attributes["seats"] == 5
    assert li.attributes["horsepower"] == 525
    assert li.attributes["power_kw"] == 386
    assert li.attributes["year"] == 2016
    assert li.attributes["registration_month"] == "October"


def test_listing_from_api_item_teaser_kept_alongside_description():
    """Regression: the teaser used to be silently dropped whenever a real `description`
    was also present, so facts stated ONLY in the teaser (e.g. "no free supercharging")
    never reached the AI's Q&A context -- it must now always end up in `attributes`."""
    item = {
        "id": 20281401,
        "make": {"name": "TESLA"}, "model": {"name": "MODEL S"},
        "versionFullName": "Model S 90 D",
        "description": "Fahrzeug ist in einem sehr gepflegten Zustand.",
        "teaser": "Kein free supercharging",
        "hadAccident": False,
        "driveType": "all",
    }
    li = listing_from_api_item(item)
    assert li.description == "Fahrzeug ist in einem sehr gepflegten Zustand."
    assert li.attributes["teaser"] == "Kein free supercharging"
    assert li.attributes["had_accident"] == "no"
    assert li.attributes["drive_type"] == "all-wheel drive (dual motor)"
    assert "Kein free supercharging" in li.as_key_value_text


def test_listing_from_api_item_handles_missing_fields():
    assert listing_from_api_item({}) is None  # no id -> skip
    assert listing_from_api_item({"id": 1}) is None  # no make/model/version -> no title
    li = listing_from_api_item({"id": 1, "make": {"name": "Tesla"}, "model": {"name": "Model S"}})
    assert li is not None and li.price is None and li.attributes == {}


# --- search() orchestration (monkeypatched package functions) --------------


def test_search_requires_make_and_model():
    with pytest.raises(AdapterError, match="Make and Model"):
        list(AutoScout24Adapter().search(_query()))


def test_search_happy_path(monkeypatch):
    monkeypatch.setattr(as24, "resolve_make_key", lambda s, m: ("tesla", "TESLA"))
    monkeypatch.setattr(as24, "resolve_model_key", lambda s, mk, md: ("model-s", "MODEL S"))
    captured_search_kwargs = {}

    def fake_search_listings(session, make_key, model_key, **kwargs):
        captured_search_kwargs.update(kwargs)
        return [{"id": i, "createdDate": f"2026-06-{20 - i:02d}T00:00:00Z"} for i in range(3)]

    def fake_visit_all(session, candidates, **kwargs):
        return _FIXTURE  # pretend detail-fetch returned our real fixture items

    monkeypatch.setattr(as24, "search_listings", fake_search_listings)
    monkeypatch.setattr(as24, "visit_all_listings", fake_visit_all)

    q = _query(make="Tesla", model="Model S", year_min=2015, mileage_max=200000)
    q.price_min, q.price_max = 5000, 90000
    listings = list(AutoScout24Adapter().search(q))

    assert len(listings) == len(_FIXTURE)
    assert captured_search_kwargs["price_from"] == 5000 and captured_search_kwargs["price_to"] == 90000
    assert captured_search_kwargs["year_from"] == 2015
    assert captured_search_kwargs["mileage_to"] == 200000


def test_search_caps_detail_fetch_to_newest(monkeypatch):
    monkeypatch.setattr(as24, "resolve_make_key", lambda s, m: ("tesla", "TESLA"))
    monkeypatch.setattr(as24, "resolve_model_key", lambda s, mk, md: ("model-s", "MODEL S"))
    monkeypatch.setattr(
        as24, "search_listings",
        lambda s, mk, md, **kw: [{"id": i, "createdDate": f"2026-06-{i:02d}T00:00:00Z"} for i in range(1, 40)],
    )
    seen_ids = []

    def fake_visit_all(session, candidates, **kwargs):
        seen_ids.extend(c["id"] for c in candidates)
        return []

    monkeypatch.setattr(as24, "visit_all_listings", fake_visit_all)

    monkeypatch.setattr(as24, "get_settings", lambda: Settings(browser_max_items_per_run=5))
    list(AutoScout24Adapter().search(_query(make="Tesla", model="Model S")))

    assert len(seen_ids) == 5
    assert seen_ids == [39, 38, 37, 36, 35]  # newest (highest createdDate) first


def test_search_unknown_make_raises_adapter_error(monkeypatch):
    def boom(session, make):
        raise ValueError(f"Could not find a make matching {make!r}")

    monkeypatch.setattr(as24, "resolve_make_key", boom)
    with pytest.raises(AdapterError, match="AutoScout24"):
        list(AutoScout24Adapter().search(_query(make="Nope", model="X")))


def test_search_network_error_raises_adapter_error(monkeypatch):
    def boom(session, make):
        raise ConnectionError("no route to host")

    monkeypatch.setattr(as24, "resolve_make_key", boom)
    with pytest.raises(AdapterError, match="request failed"):
        list(AutoScout24Adapter().search(_query(make="Tesla", model="Model S")))


def test_search_detail_failure_degrades_to_summary(monkeypatch):
    monkeypatch.setattr(as24, "resolve_make_key", lambda s, m: ("tesla", "TESLA"))
    monkeypatch.setattr(as24, "resolve_model_key", lambda s, mk, md: ("model-s", "MODEL S"))
    monkeypatch.setattr(
        as24, "search_listings",
        lambda s, mk, md, **kw: [
            {"id": 1, "make": {"name": "Tesla"}, "model": {"name": "Model S"}, "price": 20000, "createdDate": ""}
        ],
    )

    def boom(session, candidates, **kwargs):
        raise ConnectionError("timeout")

    monkeypatch.setattr(as24, "visit_all_listings", boom)
    listings = list(AutoScout24Adapter().search(_query(make="Tesla", model="Model S")))
    assert len(listings) == 1 and listings[0].price == 20000.0  # summary fields, not fatal


def test_health_check(monkeypatch):
    monkeypatch.setattr(as24, "resolve_make_key", lambda s, m: ("tesla", "TESLA"))
    monkeypatch.setattr(as24, "resolve_model_key", lambda s, mk, md: ("model-s", "MODEL S"))
    monkeypatch.setattr(as24, "search_listings", lambda s, mk, md, **kw: [])
    assert AutoScout24Adapter().health_check() is True

    def boom(session, make):
        raise ConnectionError("down")

    monkeypatch.setattr(as24, "resolve_make_key", boom)
    assert AutoScout24Adapter().health_check() is False


# --- an ad an aggregator (AutoUncle) links to ------------------------------------


@pytest.mark.parametrize("url, expected", [
    ("https://www.autoscout24.ch/de/d/tesla-model-s-20919059", "20919059"),  # as AutoUncle gives it
    ("https://www.autoscout24.ch/de/d/20919059", "20919059"),  # as this adapter's listings have it
    ("https://autoscout24.ch/fr/d/tesla-model-s-90-d-20919059?source=x", "20919059"),
    ("https://www.autoscout24.ch/de/s/mo-model-s/mk-tesla", None),  # a search, not an ad
    ("https://www.autolina.ch/auto/tesla-model-s/5191221", None),
    ("", None),
])
def test_listing_id_from_url(url, expected):
    assert AutoScout24Adapter().listing_id_from_url(url) == expected


def test_fetch_listing_maps_the_ads_detail_record(monkeypatch):
    item = dict(_FIXTURE[0])
    item.pop("seller", None)  # the detail endpoint has a seller id only
    asked = []
    monkeypatch.setattr(as24, "fetch_detail", lambda session, listing_id: asked.append(listing_id) or dict(item))
    li = AutoScout24Adapter().fetch_listing(f"https://www.autoscout24.ch/de/d/tesla-model-s-{item['id']}")
    assert asked == [item["id"]]
    assert li.marketplace == "autoscout24" and li.external_id == str(item["id"])
    assert li.url == f"https://www.autoscout24.ch/de/d/{item['id']}" and li.location is None


def test_fetch_listing_request_failure_raises_adapter_error(monkeypatch):
    def boom(session, listing_id):
        raise ConnectionError("down")

    monkeypatch.setattr(as24, "fetch_detail", boom)
    with pytest.raises(AdapterError, match="request failed"):
        AutoScout24Adapter().fetch_listing("https://www.autoscout24.ch/de/d/1")
