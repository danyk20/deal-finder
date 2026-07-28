"""Ricardo.ch adapter tests — no real network.

The adapter calls the `ricardo-scraper` package, which drives its own bundled Camoufox
browser. We monkeypatch the package's `scrape` function (the same seam the adapter
imports) rather than driving a real browser.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from deal_finder.adapters import ricardo
from deal_finder.adapters.base import AdapterError, MarketplaceQuery
from deal_finder.adapters.ricardo import RicardoAdapter, listing_from_api_node


def _query(**params) -> MarketplaceQuery:
    return MarketplaceQuery(category="car", terms=["Tesla", "Model X"], params=params)


def _result(listings):
    return SimpleNamespace(listings=listings)


# --- pure field mapping ------------------------------------------------------


def test_listing_from_api_node_uses_structured_attributes():
    """Regression (originally reported issue, fixed in ricardo-scraper >=0.2.1): real
    car listings used to show up with no year/mileage at all, since ricardo-scraper
    didn't expose the site's own "Fahrzeug-Klassierung" characteristics panel and the
    text-regex fallback found nothing in a pure feature-bullet description. Confirmed
    live against the exact reported listing (tesla-model-x-100d-1324931008): the
    structured `attributes` dict is now the primary, reliable source."""
    node = {
        "id": "1324931008",
        "title": "TESLA Model X 100D",
        "description": "Neupreis130000.611PS, 4x4 Allrad, Sommer und Winterrader 20,"
        "Anhanger-Kupplung, Falcon Flugelturen, sehr grosser Stauraum.",  # no year/km in text
        "price": 32000,
        "brand": "Tesla",
        "model": "Model X",
        "color": "Weiss",
        "attributes": {
            "auto_first_registration_year": "2018",
            "vehicle_classification": "Standard",
            "color": "Weiss",
            "auto_gear_type": "Automat",
            "auto_mileage": "93'500 km",
            "car_brand": "Tesla",
            "car_model": "Model X",
            "car_fuel_type": "Elektrisch",
        },
    }
    li = listing_from_api_node(node)
    assert li.attributes["year"] == 2018
    assert li.attributes["mileage_km"] == 93500
    assert li.attributes["transmission"] == "Automat"
    assert li.attributes["fuel"] == "Elektrisch"
    assert li.attributes["classification"] == "Standard"
    assert li.attributes["color"] == "Weiss"


def test_listing_from_api_node_skips_redundant_attribute_keys():
    """car_brand/car_model/color inside `attributes` duplicate the listing's own
    dedicated brand/model/color fields -- must not also appear (renamed or verbatim)
    in Listing.attributes a second time."""
    node = {
        "id": "1",
        "title": "Tesla Model X",
        "brand": "Tesla",
        "model": "Model X",
        "color": "Weiss",
        "attributes": {"car_brand": "Tesla", "car_model": "Model X", "color": "Weiss"},
    }
    li = listing_from_api_node(node)
    assert "car_brand" not in li.attributes and "car_model" not in li.attributes
    assert li.attributes["color"] == "Weiss"  # from the top-level field, not duplicated


def test_listing_from_api_node_falls_back_to_text_regex_without_attributes():
    """When `attributes` is absent/empty (e.g. a listing category ricardo-scraper
    doesn't have a schema for yet), year/mileage must still fall back to the
    regex-over-text safety net rather than disappearing entirely."""
    node = {
        "id": "1",
        "title": "TESLA Model X 100D Baujahr 2018",
        "description": "Sehr gepflegt, 93500 km gelaufen.",
        "price": 32000,
        "location_zip": "8952",
        "location_city": "Schlieren",
        "images": ["https://x/1.jpg"],
    }
    li = listing_from_api_node(node)
    assert li.marketplace == "ricardo"
    assert li.external_id == "1"
    assert li.attributes["year"] == 2018
    assert li.attributes["mileage_km"] == 93500
    assert li.location == "8952, Schlieren"


def test_listing_from_api_node_no_year_or_mileage_anywhere():
    """No structured attributes and no year/mileage mentioned in text either -- must
    degrade cleanly (no attribute set) rather than raising or guessing."""
    node = {
        "id": "1",
        "title": "TESLA Model X 100D",
        "description": "Neupreis130000.611PS, 4x4 Allrad, Sommer und Winterrader 20,"
        "Anhanger-Kupplung, Falcon Flugelturen, sehr grosser Stauraum.",
        "price": 32000,
    }
    li = listing_from_api_node(node)
    assert "year" not in li.attributes
    assert "mileage_km" not in li.attributes


def test_listing_from_api_node_handles_missing_fields():
    assert listing_from_api_node({}) is None  # no id/title -> skip
    assert listing_from_api_node({"id": "1"}) is None  # no title -> skip
    li = listing_from_api_node({"id": "1", "title": "Tesla Model X"})
    assert li is not None and li.price is None and li.attributes == {}
    assert li.image_urls == []


# --- search() orchestration ---------------------------------------------------


def test_search_requires_text():
    empty_query = MarketplaceQuery(category="car", terms=[])
    with pytest.raises(AdapterError, match="no search text"):
        list(RicardoAdapter().search(empty_query))


def test_search_filters_to_cars_category(monkeypatch):
    """Regression (reported issue): Ricardo is a general marketplace, not car-specific
    -- a free-text search for "Tesla Model X" also surfaces non-car listings (wheel/rim
    sets, chargers) that merely mention the model, e.g. a real listing categorized under
    "fahrzeugzubehoer" (vehicle accessories) whose "5000 km" was the *wheels'* own wear,
    not a car's mileage. ricardo-scraper already supports filtering by category
    breadcrumb -- must be passed "autos" so only genuine cars come back."""
    captured_kwargs = {}

    def fake_scrape(text, **kwargs):
        captured_kwargs["text"] = text
        captured_kwargs.update(kwargs)
        return _result([])

    monkeypatch.setattr(ricardo, "scrape", fake_scrape)
    list(RicardoAdapter().search(_query(make="Tesla", model="Model X")))

    assert captured_kwargs["category"] == "autos"
    assert captured_kwargs["detail"] is True


def test_search_happy_path(monkeypatch):
    nodes = [{"id": "1", "title": "Tesla Model X 100D", "price": 32000}]

    def fake_scrape(text, **kwargs):
        return _result(nodes)

    monkeypatch.setattr(ricardo, "scrape", fake_scrape)
    listings = list(RicardoAdapter().search(_query()))
    assert len(listings) == 1
    assert listings[0].external_id == "1"


def test_search_bad_price_range_raises_adapter_error(monkeypatch):
    def boom(text, **kwargs):
        raise ValueError("price_from (90000) cannot be greater than price_to (5000)")

    monkeypatch.setattr(ricardo, "scrape", boom)
    with pytest.raises(AdapterError, match="Ricardo.ch"):
        list(RicardoAdapter().search(_query()))


def test_search_network_error_raises_adapter_error(monkeypatch):
    def boom(text, **kwargs):
        raise ConnectionError("no route to host")

    monkeypatch.setattr(ricardo, "scrape", boom)
    with pytest.raises(AdapterError, match="request failed"):
        list(RicardoAdapter().search(_query()))


def test_health_check(monkeypatch):
    monkeypatch.setattr(ricardo, "scrape", lambda text, **kw: _result([]))
    assert RicardoAdapter().health_check() is True

    def boom(text, **kwargs):
        raise ConnectionError("down")

    monkeypatch.setattr(ricardo, "scrape", boom)
    assert RicardoAdapter().health_check() is False
