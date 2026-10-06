"""Facebook adapter tests — no real network, no Facebook login.

The adapter lazily imports `fb_scraper` (an optional extra — the [facebook] extra in
pyproject.toml) inside search(), so these tests monkeypatch the third-party package's
own objects (fb_scraper.browser.FacebookSession, fb_scraper.scraper.search_all_listings /
visit_all_listings) rather than names on our module. Skipped entirely if the extra
isn't installed.
"""

from __future__ import annotations

import sys

import pytest

pytest.importorskip("fb_scraper")

from deal_finder.adapters.base import AdapterError, MarketplaceQuery  # noqa: E402
from deal_finder.adapters.facebook import FacebookAdapter, listing_from_api_item  # noqa: E402
from deal_finder.config import Settings  # noqa: E402

# --- pure field mapping ------------------------------------------------

REAL_ITEM = {
    "listing_id": "1234567890",
    "title": "Tesla Model S 85D, Baujahr 2016",
    "price": "16.900 CHF",
    "location": "Zürich, ZH",
    "url": "https://www.facebook.com/marketplace/item/1234567890/",
    "image_url": "https://scontent.example/thumb.jpg",
    "is_local": True,
    "country": "ch",
    "condition": "Gebraucht - Gut",
    "description": "Sehr gepflegt, 150000 km, keine Unfälle.",
    "posted_at": "vor 3 Tagen",
    "images": ["https://scontent.example/a.jpg", "https://scontent.example/b.jpg"],
}


def test_listing_from_api_item_full():
    li = listing_from_api_item(REAL_ITEM)
    assert li is not None
    assert li.marketplace == "facebook"
    assert li.external_id == "1234567890"
    assert li.url == REAL_ITEM["url"]
    assert li.title == REAL_ITEM["title"]
    assert li.price == 16900.0 and li.currency == "CHF"
    assert li.location == "Zürich, ZH"
    assert li.attributes["year"] == 2016
    assert li.attributes["mileage_km"] == 150000
    assert li.image_urls == REAL_ITEM["images"]  # prefers the full gallery over the thumbnail
    assert li.posted_at is None  # only a relative date string is available; not parsed


def test_listing_from_api_item_no_listing_id():
    assert listing_from_api_item({"title": "x"}) is None


def test_listing_from_api_item_missing_title_falls_back():
    item = {**REAL_ITEM, "title": "", "description": ""}
    li = listing_from_api_item(item)
    assert li is not None and li.title == "Facebook Marketplace listing"


def test_listing_from_api_item_falls_back_to_thumbnail():
    item = dict(REAL_ITEM)
    del item["images"]
    li = listing_from_api_item(item)
    assert li.image_urls == [REAL_ITEM["image_url"]]


# --- search() orchestration (monkeypatched third-party package) --------


@pytest.fixture(autouse=True)
def _fresh_facebook_state(monkeypatch):
    """The adapter remembers city ids and the account's radius across searches; every
    test starts with an empty memory and an account whose radius the page doesn't show."""
    import deal_finder.adapters.facebook as fb

    monkeypatch.setattr(fb, "_city_ids", {})
    monkeypatch.setattr(fb, "_account_radius", None)
    account["radius"] = None


def _query(**params) -> MarketplaceQuery:
    return MarketplaceQuery(category="car", terms=["Tesla", "Model S"], params=params)


# The fake Facebook account: its saved search radius, shown in the search page's data.
account: dict = {"radius": None}


class _FakePage:
    def close(self):
        pass

    def content(self):
        return f'{{"filter_radius_km":{account["radius"]}}}' if account["radius"] else "<html></html>"


class _FakeContext:
    def __init__(self):
        self.pages_created = 0

    def new_page(self):
        self.pages_created += 1
        return _FakePage()


class _FakeSession:
    """Stand-in for fb_scraper.browser.FacebookSession."""

    instances: list["_FakeSession"] = []

    def __init__(self, headless=True, email=None, password=None):
        self.headless, self.email, self.password = headless, email, password
        _FakeSession.instances.append(self)

    def __enter__(self):
        return _FakeContext()

    def __exit__(self, *exc):
        return False


def _install_fake(monkeypatch, *, search_result=None, visit_result=None, search_raises=None):
    import fb_scraper.browser as fb_browser
    import fb_scraper.scraper as fb_scraper_mod

    _FakeSession.instances.clear()
    monkeypatch.setattr(fb_browser, "FacebookSession", _FakeSession)

    def fake_search_listings(page, query, **kwargs):
        if search_raises:
            raise search_raises
        return search_result if search_result is not None else []

    def fake_visit_all_listings(page, listings, **kwargs):
        return visit_result if visit_result is not None else listings

    monkeypatch.setattr(fb_scraper_mod, "search_all_listings", fake_search_listings)
    monkeypatch.setattr(fb_scraper_mod, "visit_all_listings", fake_visit_all_listings)
    # Never let a test reach the real account-radius dialog.
    radius_calls.clear()

    def fake_set_radius(page, radius_km, query, country="ch", **kw):
        radius_calls.append((radius_km, kw.get("location")))
        account["radius"] = fb_scraper_mod.supported_radius_km(radius_km)

    monkeypatch.setattr(fb_scraper_mod, "set_account_search_radius", fake_set_radius)
    return fb_scraper_mod


radius_calls: list = []


def test_search_requires_text():
    with pytest.raises(AdapterError, match="no search text"):
        list(FacebookAdapter().search(MarketplaceQuery(category="car")))


def test_search_happy_path(monkeypatch):
    _install_fake(monkeypatch, search_result=[dict(REAL_ITEM)], visit_result=[REAL_ITEM])
    monkeypatch.setattr(
        "deal_finder.adapters.facebook.get_settings", lambda: Settings(browser_max_items_per_run=15)
    )
    listings = list(FacebookAdapter().search(_query()))
    assert len(listings) == 1 and listings[0].external_id == "1234567890"
    assert _FakeSession.instances[0].email is None  # no creds configured by default


def test_search_filters_non_local(monkeypatch):
    local = dict(REAL_ITEM, listing_id="1", is_local=True)
    foreign = dict(REAL_ITEM, listing_id="2", is_local=False)
    seen: dict = {}

    def fake_visit_all(page, listings, **kwargs):
        seen["items"] = listings
        return listings

    import fb_scraper.browser as fb_browser
    import fb_scraper.scraper as fb_scraper_mod

    monkeypatch.setattr(fb_browser, "FacebookSession", _FakeSession)
    monkeypatch.setattr(fb_scraper_mod, "search_all_listings", lambda page, q, **k: [local, foreign])
    monkeypatch.setattr(fb_scraper_mod, "visit_all_listings", fake_visit_all)

    list(FacebookAdapter().search(_query()))
    assert [c["listing_id"] for c in seen["items"]] == ["1"]


def test_search_caps_detail_fetch(monkeypatch):
    candidates = [dict(REAL_ITEM, listing_id=str(i), is_local=True) for i in range(30)]
    seen: dict = {}

    def fake_visit_all(page, listings, **kwargs):
        seen["n"] = len(listings)
        return listings

    import fb_scraper.browser as fb_browser
    import fb_scraper.scraper as fb_scraper_mod

    monkeypatch.setattr(fb_browser, "FacebookSession", _FakeSession)
    monkeypatch.setattr(fb_scraper_mod, "search_all_listings", lambda page, q, **k: candidates)
    monkeypatch.setattr(fb_scraper_mod, "visit_all_listings", fake_visit_all)
    monkeypatch.setattr(
        "deal_finder.adapters.facebook.get_settings", lambda: Settings(browser_max_items_per_run=5)
    )

    list(FacebookAdapter().search(_query()))
    assert seen["n"] == 5


def test_search_passes_credentials(monkeypatch):
    _install_fake(monkeypatch, search_result=[], visit_result=[])
    monkeypatch.setattr(
        "deal_finder.adapters.facebook.get_settings",
        lambda: Settings(facebook_email="me@example.com", facebook_password="hunter2"),
    )
    list(FacebookAdapter().search(_query()))
    assert _FakeSession.instances[0].email == "me@example.com"
    assert _FakeSession.instances[0].password == "hunter2"


def test_search_prefers_passed_settings_over_get_settings(monkeypatch):
    """The pipeline resolves effective settings (env + DB-stored Settings-page overrides,
    see config.effective_settings/runtime_settings) once per run and passes them into
    search() -- see adapters/base.py's search() docstring. A bare get_settings() call
    only sees env/.env values, so it would never see credentials the user only saved via
    the web UI. Guards against regressing back to that (the original bug: Facebook
    credentials configured in Settings were silently ignored, always logging in
    anonymously)."""
    _install_fake(monkeypatch, search_result=[], visit_result=[])
    # get_settings() deliberately has no credentials and a different item cap, so any use
    # of it instead of the passed-in settings would be caught by the assertions below.
    monkeypatch.setattr(
        "deal_finder.adapters.facebook.get_settings",
        lambda: Settings(browser_max_items_per_run=99),
    )
    passed_in = Settings(
        facebook_email="ui@example.com", facebook_password="s3cret", browser_max_items_per_run=3
    )
    list(FacebookAdapter().search(_query(), passed_in))
    assert _FakeSession.instances[0].email == "ui@example.com"
    assert _FakeSession.instances[0].password == "s3cret"


def test_login_required_raises_adapter_error(monkeypatch):
    import fb_scraper.scraper as fb_scraper_mod

    _install_fake(monkeypatch, search_raises=fb_scraper_mod.LoginRequiredError("redirected to /login"))
    with pytest.raises(AdapterError, match="fb_login"):
        list(FacebookAdapter().search(_query()))


def test_consent_required_raises_adapter_error(monkeypatch):
    import fb_scraper.scraper as fb_scraper_mod

    _install_fake(monkeypatch, search_raises=fb_scraper_mod.MarketplaceConsentRequiredError("consent"))
    with pytest.raises(AdapterError):
        list(FacebookAdapter().search(_query()))


def test_package_not_installed_raises_adapter_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "fb_scraper", None)
    monkeypatch.setitem(sys.modules, "fb_scraper.browser", None)
    with pytest.raises(AdapterError, match="isn't installed"):
        list(FacebookAdapter().search(_query()))


def test_health_check(monkeypatch):
    _install_fake(monkeypatch, search_result=[], visit_result=[])
    assert FacebookAdapter().health_check() is True

    _install_fake(monkeypatch, search_raises=RuntimeError("boom"))
    assert FacebookAdapter().health_check() is False


def test_search_never_sends_year_or_mileage(monkeypatch):
    """fb-scraper 0.4.0 dropped year/mileage search filters: Facebook silently drops every
    listing without structured vehicle data. They're enforced after the detail fetch."""
    fb_scraper_mod = _install_fake(monkeypatch)
    seen: dict = {}

    def fake_search_all(page, q, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(fb_scraper_mod, "search_all_listings", fake_search_all)
    q = MarketplaceQuery(
        category="car", terms=["Tesla", "Model X"], price_min=2222, price_max=16500,
        params={"year_min": 2015, "year_max": 2017, "mileage_max": 150000},
    )
    list(FacebookAdapter().search(q, Settings()))
    assert seen["min_price"] == 2222 and seen["max_price"] == 16500
    assert not {"min_year", "max_year", "min_mileage", "max_mileage"} & seen.keys()


def _capture_search(monkeypatch, fb_scraper_mod) -> dict:
    seen: dict = {}

    def fake_search_all(page, q, **kwargs):
        seen.update(kwargs)
        return []

    monkeypatch.setattr(fb_scraper_mod, "search_all_listings", fake_search_all)
    return seen


def test_search_uses_watch_city(monkeypatch):
    """The watch's city is looked up (whitespace-trimmed) and its Facebook location id
    goes into the search; the radius is never sent."""
    fb_scraper_mod = _install_fake(monkeypatch)
    looked_up: list = []

    def fake_lookup_city(page, city, country="ch"):
        looked_up.append((city, country))
        return "106015269434234", "Zürich, Switzerland"

    monkeypatch.setattr(fb_scraper_mod, "lookup_city", fake_lookup_city)
    seen = _capture_search(monkeypatch, fb_scraper_mod)
    q = MarketplaceQuery(category="car", terms=["Mac", "Mini"], location="Zurich ", radius_km=30)
    list(FacebookAdapter().search(q, Settings()))
    assert looked_up == [("Zurich", "ch")]
    assert seen["location"] == "106015269434234"
    assert "radius_km" not in seen and "radius" not in seen  # radius is an account setting, not a search arg


def test_search_without_city_uses_default_anchor(monkeypatch):
    fb_scraper_mod = _install_fake(monkeypatch)
    monkeypatch.setattr(fb_scraper_mod, "lookup_city", lambda *a, **k: pytest.fail("no city to look up"))
    seen = _capture_search(monkeypatch, fb_scraper_mod)
    list(FacebookAdapter().search(_query(), Settings()))
    assert seen["location"] is None


def test_search_numeric_location_id_used_as_is(monkeypatch):
    fb_scraper_mod = _install_fake(monkeypatch)
    monkeypatch.setattr(fb_scraper_mod, "lookup_city", lambda *a, **k: pytest.fail("id needs no lookup"))
    seen = _capture_search(monkeypatch, fb_scraper_mod)
    q = MarketplaceQuery(category="car", terms=["Mac", "Mini"], location="110868505604715")
    list(FacebookAdapter().search(q, Settings()))
    assert seen["location"] == "110868505604715"


def test_unknown_city_raises_adapter_error(monkeypatch):
    fb_scraper_mod = _install_fake(monkeypatch)

    def fake_lookup_city(page, city, country="ch"):
        raise fb_scraper_mod.CityNotFoundError("no suggestion inside 'ch'")

    monkeypatch.setattr(fb_scraper_mod, "lookup_city", fake_lookup_city)
    q = MarketplaceQuery(category="car", terms=["Mac", "Mini"], location="Atlantis")
    with pytest.raises(AdapterError, match="couldn't find the location 'Atlantis'"):
        list(FacebookAdapter().search(q, Settings()))


def test_watch_radius_sets_account_radius_around_city(monkeypatch):
    fb_scraper_mod = _install_fake(monkeypatch)
    monkeypatch.setattr(fb_scraper_mod, "lookup_city", lambda page, city, country="ch": ("103767472995143", "Schlieren"))
    q = MarketplaceQuery(category="car", terms=["Mac", "Mini"], location="Zurich", radius_km=30)
    list(FacebookAdapter().search(q, Settings()))
    assert radius_calls == [(30, "103767472995143")]  # passed as-is; the scraper rounds it


@pytest.mark.parametrize("radius_km", [0, -5])
def test_non_positive_radius_leaves_account_alone(monkeypatch, radius_km):
    _install_fake(monkeypatch)
    q = MarketplaceQuery(category="car", terms=["Mac", "Mini"], radius_km=radius_km)
    list(FacebookAdapter().search(q, Settings()))
    assert radius_calls == []


def test_no_watch_radius_leaves_account_alone(monkeypatch):
    _install_fake(monkeypatch)
    list(FacebookAdapter().search(_query(), Settings()))
    assert radius_calls == []


def test_radius_change_failure_still_searches(monkeypatch):
    fb_scraper_mod = _install_fake(monkeypatch)

    def fail(*a, **k):
        raise fb_scraper_mod.SearchRadiusError("dialog didn't offer 40 km")

    monkeypatch.setattr(fb_scraper_mod, "set_account_search_radius", fail)
    seen = _capture_search(monkeypatch, fb_scraper_mod)
    q = MarketplaceQuery(category="car", terms=["Mac", "Mini"], radius_km=30)
    assert list(FacebookAdapter().search(q, Settings())) == []
    assert "min_price" in seen  # the search still ran


def _count_searches(monkeypatch, fb_scraper_mod) -> list:
    searches: list = []
    monkeypatch.setattr(fb_scraper_mod, "search_all_listings", lambda page, q, **k: searches.append(k) or [])
    return searches


def test_city_is_looked_up_once(monkeypatch):
    """The lookup types the city into Facebook's location dialog; a city's id never
    changes, so later searches (any spelling case) reuse it."""
    fb_scraper_mod = _install_fake(monkeypatch)
    looked_up: list = []

    def fake_lookup_city(page, city, country="ch"):
        looked_up.append(city)
        return "106015269434234", "Zürich, Switzerland"

    monkeypatch.setattr(fb_scraper_mod, "lookup_city", fake_lookup_city)
    searches = _count_searches(monkeypatch, fb_scraper_mod)
    for city in ("Zurich", "zurich "):
        list(FacebookAdapter().search(MarketplaceQuery(category="car", terms=["Mac"], location=city), Settings()))
    assert looked_up == ["Zurich"]
    assert [s["location"] for s in searches] == ["106015269434234", "106015269434234"]


def test_unchanged_radius_is_not_set_again(monkeypatch):
    fb_scraper_mod = _install_fake(monkeypatch)
    searches = _count_searches(monkeypatch, fb_scraper_mod)
    q = MarketplaceQuery(category="car", terms=["Mac", "Mini"], radius_km=30)
    list(FacebookAdapter().search(q, Settings()))
    list(FacebookAdapter().search(q, Settings()))
    assert radius_calls == [(30, None)]  # the second search found the account still at 40 km
    assert len(searches) == 2


def test_radius_already_on_the_account_is_remembered(monkeypatch):
    """The first search with a radius sets it (the scraper itself returns early when the
    account already has it); the radius read back from the search page is what later
    searches compare against."""
    fb_scraper_mod = _install_fake(monkeypatch)
    _count_searches(monkeypatch, fb_scraper_mod)
    account["radius"] = 40
    q = MarketplaceQuery(category="car", terms=["Mac", "Mini"], radius_km=40)
    for _ in range(3):
        list(FacebookAdapter().search(q, Settings()))
    assert radius_calls == [(40, None)]


def test_radius_changed_elsewhere_is_set_back_in_the_same_run(monkeypatch):
    fb_scraper_mod = _install_fake(monkeypatch)
    searches = _count_searches(monkeypatch, fb_scraper_mod)
    q = MarketplaceQuery(category="car", terms=["Mac", "Mini"], radius_km=30)
    list(FacebookAdapter().search(q, Settings()))
    account["radius"] = 250  # the user changed it in their own browser
    list(FacebookAdapter().search(q, Settings()))
    assert radius_calls == [(30, None), (30, None)]
    assert len(searches) == 3  # searched again with the radius set back
    assert account["radius"] == 40


def test_failed_radius_change_is_not_retried_in_the_same_run(monkeypatch):
    fb_scraper_mod = _install_fake(monkeypatch)
    attempts: list = []

    def fail(*a, **k):
        attempts.append(1)
        raise fb_scraper_mod.SearchRadiusError("dialog didn't offer 40 km")

    monkeypatch.setattr(fb_scraper_mod, "set_account_search_radius", fail)
    searches = _count_searches(monkeypatch, fb_scraper_mod)
    account["radius"] = 250
    list(FacebookAdapter().search(MarketplaceQuery(category="car", terms=["Mac"], radius_km=30), Settings()))
    assert len(attempts) == 1 and len(searches) == 1
