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


def test_listing_from_api_node_maps_year_and_mileage_from_text():
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


def test_listing_from_api_node_no_year_or_mileage_in_text():
    """Regression (reported issue): ricardo-scraper doesn't expose the site's own
    structured "Fahrzeug-Klassierung" panel (year/mileage/transmission/etc, confirmed
    live to live under __NEXT_DATA__'s article.attributes, which the package's
    _extract_extra_fields() doesn't read) -- deal_finder falls back to regex over
    title+description, which finds nothing when neither mentions year/mileage in text,
    even though the real ad page shows both. Documents the current (known-limited)
    behavior rather than silently producing wrong data."""
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
