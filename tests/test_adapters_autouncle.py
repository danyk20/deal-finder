"""AutoUncle.ch adapter tests — no real network.

The adapter calls the `autouncle-scraper` package, which itself parses schema.org
JSON-LD (unfiltered search + every detail page) plus a filtered-search RSC/GraphQL path.
We monkeypatch the package's `scrape`/`count_cars` functions (the same seam the adapter
imports) rather than mocking HTTP, and pin the field-mapping against a real captured
payload (tests/fixtures/autouncle_listings.json — eight real VW Golf VIII listings,
captured with a narrow price band + max_results=8 so the fixture stayed fast to build),
mirroring the fixture-test pattern used across the other adapters.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from deal_finder.adapters import autouncle
from deal_finder.adapters.autouncle import AutoUncleAdapter, _find_unambiguous_model_match, listing_from_api_item
from deal_finder.adapters.base import AdapterError, MarketplaceQuery
from deal_finder.config import Settings

_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "autouncle_listings.json").read_text())


def _query(**params) -> MarketplaceQuery:
    return MarketplaceQuery(category="car", terms=["VW", "Golf VIII"], params=params)


def _result(listings):
    return SimpleNamespace(listings=listings)


# --- pure field mapping (real captured data) --------------------------------


def test_listing_from_api_item_real_fixture():
    listings = [listing_from_api_item(item) for item in _FIXTURE]
    assert all(li is not None for li in listings)
    li = listings[0]
    assert li.marketplace == "autouncle"
    assert li.external_id == "7025401"
    assert li.url == "https://www.autouncle.ch/de-ch/d/7025401"
    assert li.title == "Gebraucht 2022 VW Golf VIII 110 PS"
    assert li.description.startswith("Gebraucht VW Golf VIII")
    assert li.price == 12999.0 and li.currency == "CHF"
    assert li.location == "5703 Seon"
    assert li.attributes["year"] == 2022
    assert li.attributes["mileage_km"] == 123000
    assert li.attributes["horsepower"] == 110
    assert li.attributes["fuel"] == "Elektrisch/Benzin"
    assert li.attributes["price_rating"] == "Superpreis"
    assert li.attributes["savings_vs_market_chf"] == 2501
    assert li.attributes["source_platform"] == "autoscout24-ch"
    assert li.image_urls and li.image_urls[0].startswith("https://images.autouncle.com/")


def test_listing_from_api_item_equipment_and_price_history_reach_attributes():
    """Regression: equipment (dict) and priceHistory (list of records) are AutoUncle-only
    fields with no dedicated Listing column -- they must reach `attributes` via the
    flatten_listing()-based catch-all, not get silently dropped."""
    li = listing_from_api_item(_FIXTURE[0])
    equipment_keys = [k for k in li.attributes if k.startswith("equipment_")]
    assert equipment_keys, "expected at least one flattened equipment_* attribute"
    assert "price_history" in li.attributes
    assert "=" in li.attributes["price_history"]


def test_listing_from_api_item_dedicated_fields_excluded_from_attributes():
    """address components / price / description / name are already dedicated Listing
    fields -- they must not also leak into attributes (would just duplicate them)."""
    li = listing_from_api_item(_FIXTURE[0])
    for leaked in ("addressLocality", "addressRegion", "postalCode", "price", "name", "description"):
        assert leaked not in li.attributes


def test_listing_from_api_item_unknown_fields_reach_ai_via_catchall():
    """A field not in the explicit rename table (e.g. something AutoUncle adds later)
    must still reach the AI instead of being silently dropped."""
    item = {"id": "1", "make": "VW", "model": "Golf VIII", "someNewField": "value"}
    li = listing_from_api_item(item)
    assert li.attributes["someNewField"] == "value"


def test_listing_from_api_item_falls_back_to_make_model_title():
    """Regression: not every listing has a populated `name` -- must fall back cleanly to
    "<make> <model>" rather than producing an empty/None title."""
    item = {"id": "1", "make": "VW", "model": "Golf VIII", "name": None}
    li = listing_from_api_item(item)
    assert li.title == "VW Golf VIII"
    assert li.description == ""


def test_listing_from_api_item_handles_missing_fields():
    assert listing_from_api_item({}) is None  # no id -> skip
    assert listing_from_api_item({"id": "1"}) is None  # no make/model/name -> no title
    li = listing_from_api_item({"id": "1", "make": "VW", "model": "Golf VIII"})
    assert li is not None and li.price is None and li.location is None
    assert li.image_urls == []


def test_listing_from_api_item_falls_back_to_summary_image_url():
    """Unfiltered detail=False summary records only have a single `imageUrl`, no
    `imageUrls` gallery list."""
    item = {"id": "1", "make": "VW", "model": "Golf VIII", "imageUrl": "https://x/1.jpg"}
    li = listing_from_api_item(item)
    assert li.image_urls == ["https://x/1.jpg"]


# --- search() orchestration (monkeypatched package function) ---------------


def test_search_requires_make_and_model():
    with pytest.raises(AdapterError, match="Make and Model"):
        list(AutoUncleAdapter().search(_query()))


def test_search_happy_path(monkeypatch):
    captured_kwargs = {}

    def fake_scrape(make, model, **kwargs):
        captured_kwargs["make"], captured_kwargs["model"] = make, model
        captured_kwargs.update(kwargs)
        return _result(_FIXTURE)

    monkeypatch.setattr(autouncle, "scrape", fake_scrape)

    q = _query(make="VW", model="Golf VIII", year_min=2018, mileage_max=150000)
    q.price_min, q.price_max = 5000, 20000
    listings = list(AutoUncleAdapter().search(q))

    assert len(listings) == len(_FIXTURE)
    assert captured_kwargs["make"] == "VW" and captured_kwargs["model"] == "Golf VIII"
    assert captured_kwargs["price_from"] == 5000 and captured_kwargs["price_to"] == 20000
    assert captured_kwargs["year_from"] == 2018
    assert captured_kwargs["mileage_to"] == 150000
    assert captured_kwargs["detail"] is True


def test_search_passes_browser_max_items_per_run_as_max_results(monkeypatch):
    """Regression: autouncle-scraper's max_results (>=0.3.0) actually bounds the
    detail-fetch request cost -- must be wired to the same setting the other adapters
    use to bound their own detail-fetch cost."""
    captured_kwargs = {}

    def fake_scrape(make, model, **kwargs):
        captured_kwargs.update(kwargs)
        return _result(_FIXTURE[:5])

    monkeypatch.setattr(autouncle, "scrape", fake_scrape)
    settings = Settings(browser_max_items_per_run=5)
    listings = list(AutoUncleAdapter().search(_query(make="VW", model="Golf VIII"), settings=settings))

    assert captured_kwargs["max_results"] == 5
    assert len(listings) == 5


def test_find_unambiguous_model_match_trim_variant():
    """Regression: real reported error -- 'Model S90' isn't a listed model itself (it's
    a trim/variant of 'Model S'), but the match must be unambiguous to auto-correct."""
    msg = (
        "Could not find a model matching 'Model S90' for brand 'Tesla'. "
        "Available: Cybertruck, Model 3, Model S, Model X, Model Y, Roadster"
    )
    assert _find_unambiguous_model_match("Model S90", msg) == "Model S"


def test_find_unambiguous_model_match_ambiguous_returns_none():
    msg = "Could not find a model matching 'Model' for brand 'Tesla'. Available: Model S, Model X"
    assert _find_unambiguous_model_match("Model", msg) is None


def test_find_unambiguous_model_match_no_available_list_returns_none():
    assert _find_unambiguous_model_match("Model S90", "Could not find a make matching 'Teslaa'") is None


def test_search_retries_with_corrected_model_on_unambiguous_match(monkeypatch):
    """Regression: the exact reported failure -- a trim/variant model query ('Model S90')
    should transparently retry with the single unambiguous listed model ('Model S')
    rather than failing the whole watch run."""
    calls = []

    def fake_scrape(make, model, **kwargs):
        calls.append(model)
        if model == "Model S90":
            raise ValueError(
                "Could not find a model matching 'Model S90' for brand 'Tesla'. "
                "Available: Cybertruck, Model 3, Model S, Model X, Model Y, Roadster"
            )
        return _result(_FIXTURE[:2])

    monkeypatch.setattr(autouncle, "scrape", fake_scrape)
    listings = list(AutoUncleAdapter().search(_query(make="Tesla", model="Model S90")))

    assert calls == ["Model S90", "Model S"]
    assert len(listings) == 2


def test_search_does_not_retry_on_ambiguous_model_error(monkeypatch):
    calls = []

    def fake_scrape(make, model, **kwargs):
        calls.append(model)
        raise ValueError(
            f"Could not find a model matching {model!r} for brand 'Tesla'. Available: Model S, Model X"
        )

    monkeypatch.setattr(autouncle, "scrape", fake_scrape)
    with pytest.raises(AdapterError, match="AutoUncle.ch"):
        list(AutoUncleAdapter().search(_query(make="Tesla", model="Model")))

    assert calls == ["Model"]  # no retry attempted -- ambiguous, refuses to guess


def test_search_unknown_make_raises_adapter_error(monkeypatch):
    def boom(make, model, **kwargs):
        raise ValueError(f"could not resolve make {make!r}")

    monkeypatch.setattr(autouncle, "scrape", boom)
    with pytest.raises(AdapterError, match="AutoUncle.ch"):
        list(AutoUncleAdapter().search(_query(make="Nope", model="X")))


def test_search_network_error_raises_adapter_error(monkeypatch):
    def boom(make, model, **kwargs):
        raise ConnectionError("no route to host")

    monkeypatch.setattr(autouncle, "scrape", boom)
    with pytest.raises(AdapterError, match="request failed"):
        list(AutoUncleAdapter().search(_query(make="VW", model="Golf VIII")))


def test_health_check(monkeypatch):
    monkeypatch.setattr(autouncle, "resolve_make_key", lambda make, config: "Tesla")
    monkeypatch.setattr(autouncle, "resolve_model_key", lambda make_key, model, config: "Model 3")
    monkeypatch.setattr(autouncle, "fetch_search_form_config", lambda domain, session: {})
    monkeypatch.setattr(autouncle, "count_cars", lambda car_search_input, *, domain_cfg, session: 42)
    assert AutoUncleAdapter().health_check() is True

    def boom(make, config):
        raise ConnectionError("down")

    monkeypatch.setattr(autouncle, "resolve_make_key", boom)
    assert AutoUncleAdapter().health_check() is False
