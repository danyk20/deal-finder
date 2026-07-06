"""tutti.ch adapter tests — no real network.

The adapter calls the `tutti-scraper` package (plain requests -> tutti's GraphQL API).
Tests monkeypatch that package's `scrape` (the same seam the adapter imports) and pin the
field mapping against a real captured node (tests/fixtures/tutti_listings.json), mirroring
the AutoScout24/Facebook adapter tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deal_finder.adapters import tutti as tutti_mod
from deal_finder.adapters.base import AdapterError, MarketplaceQuery
from deal_finder.adapters.tutti import TuttiAdapter, listing_from_api_node
from deal_finder.config import Settings

_NODES = json.loads((Path(__file__).parent / "fixtures" / "tutti_listings.json").read_text())


def _query(**params) -> MarketplaceQuery:
    return MarketplaceQuery(category="car", terms=["Tesla", "Model S"], params=params)


# --- pure field mapping (real captured nodes) -----------------------------


def test_listing_from_api_node_real_fixture():
    listings = [listing_from_api_node(n) for n in _NODES]
    assert all(li is not None for li in listings)
    li = listings[0]
    assert li.marketplace == "tutti"
    assert li.external_id.isdigit()
    assert li.url.startswith("https://www.tutti.ch/")
    assert "Tesla Model S" in li.title
    assert li.price and li.price > 0 and li.currency == "CHF"
    assert 1980 <= li.attributes["year"] <= 2035
    # Structured mileage (odometer), not the EV range — must be a realistic used-car figure.
    assert li.attributes["mileage_km"] > 10000
    assert li.location and li.image_urls
    assert li.language  # de/fr/it -> AI translation will run


def test_listing_from_api_node_prefers_structured_mileage():
    # A node whose body advertises a 350 km EV range must NOT report 350 km mileage.
    node = {
        "listingID": "999",
        "title": "Tesla Model S",
        "body": "Reichweite 350 km, Topzustand",
        "properties": [{"listingPropertyID": "cars_carAutoScoutMileage", "label": "Kilometerstand", "text": "128000"}],
    }
    li = listing_from_api_node(node)
    assert li.attributes["mileage_km"] == 128000


def test_listing_from_api_node_skips_without_id_or_title():
    assert listing_from_api_node({"title": "x"}) is None
    assert listing_from_api_node({"listingID": "1"}) is None


# --- search() orchestration (monkeypatched package) ------------------------


class _FakeResult:
    def __init__(self, listings):
        self.listings = listings


def test_search_requires_text():
    with pytest.raises(AdapterError, match="no search text"):
        list(TuttiAdapter().search(MarketplaceQuery(category="car")))


def test_search_happy_path(monkeypatch):
    captured = {}

    def fake_scrape(text, **kwargs):
        captured["text"] = text
        captured.update(kwargs)
        return _FakeResult(list(_NODES))

    monkeypatch.setattr(tutti_mod, "scrape", fake_scrape)
    monkeypatch.setattr(tutti_mod, "get_settings", lambda: Settings(browser_max_items_per_run=15))

    q = _query(make="Tesla", model="Model S")
    q.price_min, q.price_max = 5000, 40000
    listings = list(TuttiAdapter().search(q))

    assert len(listings) == len(_NODES)
    assert captured["text"] == "Tesla Model S"
    assert captured["category"] == "cars"          # pinned to the Autos category
    assert captured["detail"] is True
    assert captured["max_results"] == 15
    assert captured["price_from"] == 5000 and captured["price_to"] == 40000


def test_search_bad_filters_raise_adapter_error(monkeypatch):
    def fake_scrape(text, **kwargs):
        raise ValueError("price_from (9) must be <= price_to (1)")

    monkeypatch.setattr(tutti_mod, "scrape", fake_scrape)
    with pytest.raises(AdapterError, match="tutti.ch"):
        list(TuttiAdapter().search(_query(make="Tesla", model="Model S")))


def test_search_network_error_raises_adapter_error(monkeypatch):
    def fake_scrape(text, **kwargs):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(tutti_mod, "scrape", fake_scrape)
    with pytest.raises(AdapterError, match="request failed"):
        list(TuttiAdapter().search(_query(make="Tesla", model="Model S")))
