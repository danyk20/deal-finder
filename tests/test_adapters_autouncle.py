"""AutoUncle.ch adapter tests — no real network.

The adapter calls the `autouncle-scraper` package's lower-level search/detail building
blocks directly (not its `scrape()` convenience wrapper — see the adapter's module
docstring for why: `scrape(detail=True)` discards search-summary-only fields like
`modelVariant`, since the detail page has no equivalent). We monkeypatch those
functions (the same seam the adapter imports) rather than mocking HTTP, and pin the
field-mapping against a real captured payload (tests/fixtures/autouncle_listings.json —
six real Tesla Model S listings, captured through the adapter's own merge logic with a
narrow price band + max_results=6 so the fixture stayed fast to build), mirroring the
fixture-test pattern used across the other adapters.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from deal_finder.adapters import autouncle
from deal_finder.adapters.autouncle import AutoUncleAdapter, _find_unambiguous_model_match, listing_from_api_item
from deal_finder.adapters.base import AdapterError, MarketplaceQuery
from deal_finder.config import Settings

_FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "autouncle_listings.json").read_text())


def _query(**params) -> MarketplaceQuery:
    return MarketplaceQuery(category="car", terms=["Tesla", "Model S"], params=params)


# --- pure field mapping (real captured data) --------------------------------


def test_listing_from_api_item_real_fixture():
    listings = [listing_from_api_item(item) for item in _FIXTURE]
    assert all(li is not None for li in listings)
    li = next(li for li, item in zip(listings, _FIXTURE) if item["id"] == "6910126")
    assert li.marketplace == "autouncle"
    assert li.external_id == "6910126"
    assert li.url == "https://www.autouncle.ch/de-ch/d/6910126"
    assert li.title == (
        "Tesla Model S P90D (Free Supercharging) 2017 — Gebraucht 2017 Tesla Model S 772 PS "
        "(Automatikgetriebe, Elektro)"
    )
    assert li.price == 24000.0 and li.currency == "CHF"
    assert li.location == "7546 Ardez"
    assert li.attributes["year"] == 2017
    assert li.attributes["mileage_km"] == 125250
    assert li.attributes["price_rating"] == "Guter Preis"
    assert li.attributes["source_platform"] == "autoscout24-ch"
    assert li.image_urls and li.image_urls[0].startswith("https://images.autouncle.com/")


def test_listing_from_api_item_model_variant_becomes_part_of_title():
    """Regression (reported issue): autouncle-scraper 0.4.0 added `modelVariant` --
    Tesla's actual battery/trim code (e.g. "P90D (Free Supercharging)", "100 D") --
    exactly the piece previously missing that made a trim-specific watch ("Model S90")
    fail to match a genuinely matching listing. It must be part of the searchable
    vehicle identity, not just decorative."""
    item = next(i for i in _FIXTURE if i.get("modelVariant"))
    li = listing_from_api_item(item)
    assert item["modelVariant"] in li.title
    assert item["modelVariant"].lower() in li.searchable_text


def test_listing_from_api_item_new_0_4_0_fields_reach_attributes():
    """priceChangePercent/estimatedMarketPriceChf/sourcePath are new in 0.4.0 -- must
    reach `attributes` (renamed to the snake_case convention) rather than being dropped."""
    item = next(i for i in _FIXTURE if i.get("sourcePath"))
    li = listing_from_api_item(item)
    assert li.attributes["price_change_percent"] == item["priceChangePercent"]
    assert li.attributes["estimated_market_price_chf"] == item["estimatedMarketPriceChf"]
    assert li.attributes["source_path"] == item["sourcePath"]


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
    """address components / price / description / name / modelVariant are already
    dedicated Listing fields -- they must not also leak into attributes (would just
    duplicate them)."""
    li = listing_from_api_item(_FIXTURE[0])
    for leaked in ("addressLocality", "addressRegion", "postalCode", "price", "name", "description", "modelVariant"):
        assert leaked not in li.attributes


def test_listing_from_api_item_unknown_fields_reach_ai_via_catchall():
    """A field not in the explicit rename table (e.g. something AutoUncle adds later)
    must still reach the AI instead of being silently dropped."""
    item = {"id": "1", "make": "VW", "model": "Golf VIII", "someNewField": "value"}
    li = listing_from_api_item(item)
    assert li.attributes["someNewField"] == "value"


def test_listing_from_api_item_enriches_title_with_split_spec_fields():
    """Regression (reported issue): before `modelVariant` existed, AutoUncle had no
    field anywhere for the battery/trim code -- surface every other spec fact AutoUncle
    splits across separate fields (kW, transmission, fuel) directly in the title too, so
    a listing without a `modelVariant` is still judgable at a glance."""
    item = {
        "id": "1",
        "make": "Tesla",
        "model": "Model S",
        "year": 2015,
        "name": "Gebraucht 2015 Tesla Model S Performance 772 PS",
        "enginePowerKw": 568,
        "transmission": "Automatikgetriebe",
        "fuelType": "Elektro",
        "bodyType": "Kleinwagen",  # deliberately excluded from title -- confirmed unreliable
    }
    li = listing_from_api_item(item)
    assert li.title == "Tesla Model S 2015 — Gebraucht 2015 Tesla Model S Performance 772 PS (568 kW, Automatikgetriebe, Elektro)"
    assert "Kleinwagen" not in li.title


def test_listing_from_api_item_title_leads_with_vehicle_identity_when_name_lacks_it():
    """Regression: mirrors the Autolina fix -- if `name` were ever missing the make/model
    (defensive; not observed live for AutoUncle, but must not silently lose identity if it
    happens), the structured vehicle_title must lead instead."""
    item = {"id": "1", "make": "Tesla", "model": "Model S", "year": 2015, "name": "Top gepflegtes Fahrzeug"}
    li = listing_from_api_item(item)
    assert li.title.startswith("Tesla Model S 2015")
    assert "Top gepflegtes Fahrzeug" in li.title


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


# --- _fetch_listings() merge logic (monkeypatched low-level functions) -----


def _stub_resolution(monkeypatch, *, filtered_input_extra=None):
    monkeypatch.setattr(autouncle, "make_session", lambda: object())
    monkeypatch.setattr(autouncle, "fetch_search_form_config", lambda domain, session: {})
    monkeypatch.setattr(autouncle, "resolve_make_key", lambda make, config: "Tesla")
    monkeypatch.setattr(autouncle, "resolve_model_key", lambda make_key, model, config: "Model S")
    monkeypatch.setattr(autouncle, "get_domain_config", lambda domain: object())

    def fake_build_car_search_input(make_key, model_key, **kwargs):
        base = {"brand": make_key, "carModel": model_key, "brandsModels": []}
        if filtered_input_extra:
            base.update(filtered_input_extra)
        return base

    monkeypatch.setattr(autouncle, "build_car_search_input", fake_build_car_search_input)


def test_fetch_listings_merge_preserves_summary_only_fields(monkeypatch):
    """The actual fix: modelVariant (and other search-summary-only fields) has no
    equivalent key on the detail page at all -- the merge must keep it rather than
    lose it the way autouncle-scraper's own scrape(detail=True) does."""
    _stub_resolution(monkeypatch, filtered_input_extra={"maxPrice": 90000})
    monkeypatch.setattr(
        autouncle,
        "search_listings_filtered",
        lambda *a, **kw: [{"id": "1", "modelVariant": "P90D (Free Supercharging)", "price": 20000}],
    )
    monkeypatch.setattr(
        autouncle,
        "visit_all_listings",
        lambda ids, **kw: [{"id": "1", "price": 20500, "transmission": "Automatikgetriebe"}],
    )

    listings = autouncle._fetch_listings(
        "Tesla", "Model S", price_from=None, price_to=90000, mileage_to=None,
        year_from=None, year_to=None, max_results=None, delay=0, verbose=False,
    )

    assert len(listings) == 1
    merged = listings[0]
    assert merged["modelVariant"] == "P90D (Free Supercharging)"  # summary-only, survived
    assert merged["price"] == 20500  # detail's value wins over summary's for a shared key
    assert merged["transmission"] == "Automatikgetriebe"  # detail-only field, present


def test_fetch_listings_detail_none_does_not_erase_summary_value(monkeypatch):
    """A detail record that sets a shared key to None (field genuinely absent on that
    detail page) must not blank out a good value the summary phase already had."""
    _stub_resolution(monkeypatch, filtered_input_extra={"maxPrice": 90000})
    monkeypatch.setattr(
        autouncle, "search_listings_filtered", lambda *a, **kw: [{"id": "1", "bodyType": "Limousine"}]
    )
    monkeypatch.setattr(autouncle, "visit_all_listings", lambda ids, **kw: [{"id": "1", "bodyType": None}])

    listings = autouncle._fetch_listings(
        "Tesla", "Model S", price_from=None, price_to=90000, mileage_to=None,
        year_from=None, year_to=None, max_results=None, delay=0, verbose=False,
    )
    assert listings[0]["bodyType"] == "Limousine"


def test_fetch_listings_caps_before_detail_fetch(monkeypatch):
    """max_results must slice the candidate list before visit_all_listings runs, not
    after -- that's what actually bounds the expensive per-listing request cost."""
    _stub_resolution(monkeypatch, filtered_input_extra={"maxPrice": 90000})
    monkeypatch.setattr(
        autouncle,
        "search_listings_filtered",
        lambda *a, **kw: [{"id": str(i)} for i in range(10)],
    )
    seen_ids = []

    def fake_visit_all(ids, **kw):
        seen_ids.extend(ids)
        return [{"id": i} for i in ids]

    monkeypatch.setattr(autouncle, "visit_all_listings", fake_visit_all)

    listings = autouncle._fetch_listings(
        "Tesla", "Model S", price_from=None, price_to=90000, mileage_to=None,
        year_from=None, year_to=None, max_results=3, delay=0, verbose=False,
    )
    assert seen_ids == ["0", "1", "2"]
    assert len(listings) == 3


def test_fetch_listings_uses_unfiltered_path_without_filters(monkeypatch):
    _stub_resolution(monkeypatch)  # base car_search_input only (no filter keys)
    called = {}

    def fake_unfiltered(*a, **kw):
        called["unfiltered"] = True
        return []

    def fake_filtered(*a, **kw):
        called["filtered"] = True
        return []

    monkeypatch.setattr(autouncle, "search_listings", fake_unfiltered)
    monkeypatch.setattr(autouncle, "search_listings_filtered", fake_filtered)
    monkeypatch.setattr(autouncle, "visit_all_listings", lambda ids, **kw: [])

    autouncle._fetch_listings(
        "Tesla", "Model S", price_from=None, price_to=None, mileage_to=None,
        year_from=None, year_to=None, max_results=None, delay=0, verbose=False,
    )
    assert called == {"unfiltered": True}


def test_fetch_listings_rejects_inverted_price_range():
    with pytest.raises(ValueError, match="price_from"):
        autouncle._fetch_listings(
            "Tesla", "Model S", price_from=90000, price_to=5000, mileage_to=None,
            year_from=None, year_to=None, max_results=None, delay=0, verbose=False,
        )


# --- search() orchestration (monkeypatched at the _fetch_listings seam) -----


def test_search_requires_make_and_model():
    with pytest.raises(AdapterError, match="Make and Model"):
        list(AutoUncleAdapter().search(_query()))


def test_search_happy_path(monkeypatch):
    captured_kwargs = {}

    def fake_fetch(make, model, **kwargs):
        captured_kwargs["make"], captured_kwargs["model"] = make, model
        captured_kwargs.update(kwargs)
        return _FIXTURE

    monkeypatch.setattr(autouncle, "_fetch_listings", fake_fetch)

    q = _query(make="Tesla", model="Model S", year_min=2018, mileage_max=150000)
    q.price_min, q.price_max = 5000, 90000
    listings = list(AutoUncleAdapter().search(q))

    assert len(listings) == len(_FIXTURE)
    assert captured_kwargs["make"] == "Tesla" and captured_kwargs["model"] == "Model S"
    assert captured_kwargs["price_from"] == 5000 and captured_kwargs["price_to"] == 90000
    assert captured_kwargs["year_from"] == 2018
    assert captured_kwargs["mileage_to"] == 150000


def test_search_passes_browser_max_items_per_run_as_max_results(monkeypatch):
    captured_kwargs = {}

    def fake_fetch(make, model, **kwargs):
        captured_kwargs.update(kwargs)
        return _FIXTURE[:3]

    monkeypatch.setattr(autouncle, "_fetch_listings", fake_fetch)
    settings = Settings(browser_max_items_per_run=5)
    listings = list(AutoUncleAdapter().search(_query(make="Tesla", model="Model S"), settings=settings))

    assert captured_kwargs["max_results"] == 5
    assert len(listings) == 3


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

    def fake_fetch(make, model, **kwargs):
        calls.append(model)
        if model == "Model S90":
            raise ValueError(
                "Could not find a model matching 'Model S90' for brand 'Tesla'. "
                "Available: Cybertruck, Model 3, Model S, Model X, Model Y, Roadster"
            )
        return _FIXTURE[:2]

    monkeypatch.setattr(autouncle, "_fetch_listings", fake_fetch)
    listings = list(AutoUncleAdapter().search(_query(make="Tesla", model="Model S90")))

    assert calls == ["Model S90", "Model S"]
    assert len(listings) == 2


def test_search_does_not_retry_on_ambiguous_model_error(monkeypatch):
    calls = []

    def fake_fetch(make, model, **kwargs):
        calls.append(model)
        raise ValueError(
            f"Could not find a model matching {model!r} for brand 'Tesla'. Available: Model S, Model X"
        )

    monkeypatch.setattr(autouncle, "_fetch_listings", fake_fetch)
    with pytest.raises(AdapterError, match="AutoUncle.ch"):
        list(AutoUncleAdapter().search(_query(make="Tesla", model="Model")))

    assert calls == ["Model"]  # no retry attempted -- ambiguous, refuses to guess


def test_search_unknown_make_raises_adapter_error(monkeypatch):
    def boom(make, model, **kwargs):
        raise ValueError(f"could not resolve make {make!r}")

    monkeypatch.setattr(autouncle, "_fetch_listings", boom)
    with pytest.raises(AdapterError, match="AutoUncle.ch"):
        list(AutoUncleAdapter().search(_query(make="Nope", model="X")))


def test_search_network_error_raises_adapter_error(monkeypatch):
    def boom(make, model, **kwargs):
        raise ConnectionError("no route to host")

    monkeypatch.setattr(autouncle, "_fetch_listings", boom)
    with pytest.raises(AdapterError, match="request failed"):
        list(AutoUncleAdapter().search(_query(make="Tesla", model="Model S")))


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
