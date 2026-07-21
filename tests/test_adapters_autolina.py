"""Autolina.ch adapter tests — no real network.

The adapter calls the `autolina-scraper` package, which itself parses autolina.ch's own
server-rendered HTML. We monkeypatch the package's `scrape` function (the same seam the
adapter imports) rather than mocking HTTP, and pin the field-mapping against a real
captured payload (tests/fixtures/autolina_listings.json — ten real Tesla Model S
listings, captured with ``max_results=10`` against a 16-listing search so it also
demonstrates the site's true ``total_elements`` vs. the capped ``listings`` count),
mirroring the fixture-test pattern used across the other adapters.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from deal_finder.adapters import autolina
from deal_finder.adapters.autolina import AutolinaAdapter, listing_from_api_item
from deal_finder.adapters.base import AdapterError, MarketplaceQuery
from deal_finder.config import Settings

_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "autolina_listings.json").read_text())


def _query(**params) -> MarketplaceQuery:
    return MarketplaceQuery(category="car", terms=["Tesla", "Model S"], params=params)


def _result(listings, total_elements=None):
    return SimpleNamespace(listings=listings, total_elements=total_elements or len(listings))


# --- pure field mapping (real captured data) --------------------------------


def test_listing_from_api_item_real_fixture():
    listings = [listing_from_api_item(item) for item in _FIXTURE]
    assert all(li is not None for li in listings)
    li = listings[0]
    assert li.marketplace == "autolina"
    assert li.external_id == "5027330"
    assert li.url == "https://www.autolina.ch/auto/tesla-model-s/5027330"
    assert li.price == 14880.0 and li.currency == "CHF"
    assert li.location == "5502 Hunzenschwil / AG"
    assert li.attributes["year"] == 2017
    assert li.attributes["mileage_km"] == 219000
    assert li.attributes["horsepower"] == 388
    assert li.attributes["fuel"] == "Elektro"
    assert li.attributes["standard_equipment"].startswith("Airbag: Airbag Beifahrer deaktivierbar")
    assert li.attributes["dealer_name"] == "Auto Blitz AG"
    assert li.image_urls and li.image_urls[0].startswith("https://www.autolina.ch/auto-bild/")


def test_listing_from_api_item_uses_ad_title_and_description_when_present():
    """Regression: autolina-scraper >=0.2.0 added the seller's own ad headline
    (`adTitle`) and free-text description (`beschreibung`) -- both mostly seen on
    private-seller listings. When present they must be preferred over the generic
    make+trim title and the empty description."""
    item = next(i for i in _FIXTURE if i.get("adTitle") and i.get("beschreibung"))
    li = listing_from_api_item(item)
    assert li.title == item["adTitle"]
    assert li.description == item["beschreibung"]


def test_listing_from_api_item_falls_back_without_ad_title_or_description():
    """Regression: dealer listings often have neither field -- must still fall back
    cleanly to the make+trim title and an empty description, not None/crash."""
    item = next(i for i in _FIXTURE if not i.get("adTitle") and not i.get("beschreibung"))
    li = listing_from_api_item(item)
    assert li.title  # falls back to "<make> <modelType>"
    assert item.get("make", "") in li.title
    assert li.description == ""


def test_listing_from_api_item_maps_vin_and_optional_fields():
    """Only some listings carry a VIN/energy-efficiency/warranty row -- when present
    they must reach `attributes` since structured data is autolina.ch's primary source
    of facts for the AI."""
    item = next((i for i in _FIXTURE if i.get("fahrgestell_nr")), None)
    if item is None:
        pytest.skip("no VIN present in this fixture snapshot")
    li = listing_from_api_item(item)
    assert li.attributes["vin"] == item["fahrgestell_nr"]


def test_listing_from_api_item_unknown_fields_reach_ai_via_catchall():
    """Regression: a spec row not in the explicit rename table (e.g. a new field
    autolina.ch adds later) must still reach the AI instead of being silently dropped."""
    item = {"carId": 1, "make": "TESLA", "modelType": "Model S", "someNewSpecRow": "value"}
    li = listing_from_api_item(item)
    assert li.attributes["someNewSpecRow"] == "value"


def test_listing_from_api_item_adtitle_beschreibung_excluded_from_attributes():
    """adTitle/beschreibung become dedicated Listing.title/description -- they must not
    also leak into attributes via the generic catch-all pass (would duplicate them)."""
    item = {"carId": 1, "make": "TESLA", "modelType": "Model S", "adTitle": "Ad", "beschreibung": "Text"}
    li = listing_from_api_item(item)
    assert "adTitle" not in li.attributes and "beschreibung" not in li.attributes


def test_listing_from_api_item_handles_missing_fields():
    assert listing_from_api_item({}) is None  # no carId -> skip
    assert listing_from_api_item({"carId": 1}) is None  # no make/modelType/adTitle -> no title
    li = listing_from_api_item({"carId": 1, "make": "Tesla", "modelType": "Model S"})
    assert li is not None and li.price is None and li.attributes == {}
    assert li.image_urls == [] and li.description == ""


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


def test_search_passes_browser_max_items_per_run_as_max_results(monkeypatch):
    """Regression: autolina-scraper's `max_results` caps the number of listings that get
    an expensive per-listing detail-page visit -- must be wired to the same setting the
    other adapters use to bound their own detail-fetch cost. Since 0.3.0 the package
    itself also sorts candidates newest-first (by carId descending) before applying this
    cap, so it means "the newest N" rather than an arbitrary site-returned order."""
    captured_kwargs = {}

    def fake_scrape(make, model, **kwargs):
        captured_kwargs.update(kwargs)
        return _result(_FIXTURE[:5], total_elements=100)  # site has far more than we take

    monkeypatch.setattr(autolina, "scrape", fake_scrape)
    settings = Settings(browser_max_items_per_run=5)
    listings = list(AutolinaAdapter().search(_query(make="Tesla", model="Model S"), settings=settings))

    assert captured_kwargs["max_results"] == 5
    assert len(listings) == 5  # capped, even though the site reports far more (100)


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
