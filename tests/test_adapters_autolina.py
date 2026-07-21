"""Autolina.ch adapter tests — no real network.

The adapter calls the `autolina-scraper` package, which itself parses autolina.ch's own
server-rendered HTML. We monkeypatch the package's `scrape` function (the same seam the
adapter imports) rather than mocking HTTP, and pin the field-mapping against a real
captured payload (tests/fixtures/autolina_listings.json — six real Tesla Model S
listings), mirroring the fixture-test pattern used across the other adapters.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from deal_finder.adapters import autolina
from deal_finder.adapters.autolina import AutolinaAdapter, listing_from_api_item
from deal_finder.adapters.base import AdapterError, MarketplaceQuery

_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "autolina_listings.json").read_text())


def _query(**params) -> MarketplaceQuery:
    return MarketplaceQuery(category="car", terms=["Tesla", "Model S"], params=params)


def _result(listings):
    return SimpleNamespace(listings=listings)


# --- pure field mapping (real captured data) --------------------------------


def test_listing_from_api_item_real_fixture():
    listings = [listing_from_api_item(item) for item in _FIXTURE]
    assert all(li is not None for li in listings)
    li = listings[0]
    assert li.marketplace == "autolina"
    assert li.external_id == "4776599"
    assert li.url == "https://www.autolina.ch/auto/tesla-model-s/4776599"
    assert "TESLA" in li.title and "Model S" in li.title
    assert li.price == 11999.0 and li.currency == "CHF"
    assert li.location == "4142 Münchenstein / BL"
    assert li.description == ""  # autolina.ch listings have no free-text description
    assert li.attributes["year"] == 2015
    assert li.attributes["mileage_km"] == 154000
    assert li.attributes["horsepower"] == 388
    assert li.attributes["fuel"] == "Elektro"
    assert li.attributes["transmission"] == "Automatik, 1 Gänge"
    assert li.attributes["condition"] == "Occasion / Gebraucht"
    assert li.attributes["color"] == "Rot"
    assert li.attributes["drivetrain"] == "Hinterradantrieb"
    assert li.attributes["standard_equipment"].startswith("Airbag: Airbag Fahrer und Beifahrer")
    assert li.attributes["dealer_name"] == "AUTO WBO AG"
    assert li.attributes["dealer_phone"] == "062 516 81 06"
    assert li.image_urls and li.image_urls[0].startswith("https://www.autolina.ch/auto-bild/")
    assert len(li.image_urls) == 7


def test_listing_from_api_item_maps_vin_and_optional_fields():
    """Regression: only some listings carry a VIN/energy-efficiency/warranty row —
    when present they must reach `attributes` since that's the only place autolina.ch's
    facts live (there's no description to fall back on)."""
    item = next(i for i in _FIXTURE if i.get("fahrgestell_nr"))
    li = listing_from_api_item(item)
    assert li.attributes["vin"] == "5YJSA7H21FF100168"
    assert li.attributes["energy_efficiency_class"] == "A"
    assert li.attributes["warranty"]
    assert li.attributes["optional_equipment"]


def test_listing_from_api_item_unknown_fields_reach_ai_via_catchall():
    """Regression: a spec row not in the explicit rename table (e.g. a new field
    autolina.ch adds later) must still reach the AI instead of being silently dropped."""
    item = {"carId": 1, "make": "TESLA", "modelType": "Model S", "someNewSpecRow": "value"}
    li = listing_from_api_item(item)
    assert li.attributes["someNewSpecRow"] == "value"


def test_listing_from_api_item_handles_missing_fields():
    assert listing_from_api_item({}) is None  # no carId -> skip
    assert listing_from_api_item({"carId": 1}) is None  # no make/modelType -> no title
    li = listing_from_api_item({"carId": 1, "make": "Tesla", "modelType": "Model S"})
    assert li is not None and li.price is None and li.attributes == {}
    assert li.image_urls == []


def test_listing_from_api_item_falls_back_to_summary_image_url():
    """detail=False summary rows only have a single `imageUrl`, no `images` list."""
    item = {"carId": 1, "make": "Tesla", "modelType": "Model S", "imageUrl": "https://x/1.jpg"}
    li = listing_from_api_item(item)
    assert li.image_urls == ["https://x/1.jpg"]


# --- search() orchestration (monkeypatched package function) ---------------


def test_search_requires_make_and_model():
    with pytest.raises(AdapterError, match="Make and Model"):
        list(AutolinaAdapter().search(_query()))


def test_search_happy_path(monkeypatch):
    captured_kwargs = {}

    def fake_scrape(make, model, **kwargs):
        captured_kwargs["make"], captured_kwargs["model"] = make, model
        captured_kwargs.update(kwargs)
        return _result(_FIXTURE)

    monkeypatch.setattr(autolina, "scrape", fake_scrape)

    q = _query(make="Tesla", model="Model S", year_min=2015, mileage_max=200000)
    q.price_min, q.price_max = 5000, 90000
    listings = list(AutolinaAdapter().search(q))

    assert len(listings) == len(_FIXTURE)
    assert captured_kwargs["make"] == "Tesla" and captured_kwargs["model"] == "Model S"
    assert captured_kwargs["price_from"] == 5000 and captured_kwargs["price_to"] == 90000
    assert captured_kwargs["year_from"] == 2015
    assert captured_kwargs["mileage_to"] == 200000
    assert captured_kwargs["detail"] is True


def test_search_unknown_make_raises_adapter_error(monkeypatch):
    def boom(make, model, **kwargs):
        raise ValueError(f"could not resolve make {make!r}")

    monkeypatch.setattr(autolina, "scrape", boom)
    with pytest.raises(AdapterError, match="Autolina.ch"):
        list(AutolinaAdapter().search(_query(make="Nope", model="X")))


def test_search_network_error_raises_adapter_error(monkeypatch):
    def boom(make, model, **kwargs):
        raise ConnectionError("no route to host")

    monkeypatch.setattr(autolina, "scrape", boom)
    with pytest.raises(AdapterError, match="request failed"):
        list(AutolinaAdapter().search(_query(make="Tesla", model="Model S")))


def test_health_check(monkeypatch):
    monkeypatch.setattr(autolina, "scrape", lambda make, model, **kw: _result([]))
    assert AutolinaAdapter().health_check() is True

    def boom(make, model, **kwargs):
        raise ConnectionError("down")

    monkeypatch.setattr(autolina, "scrape", boom)
    assert AutolinaAdapter().health_check() is False
