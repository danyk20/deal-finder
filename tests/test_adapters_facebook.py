"""Facebook adapter tests — no real network, no Facebook login.

The adapter lazily imports `fb_scraper` (an optional extra — the [facebook] extra in
pyproject.toml) inside search(), so these tests monkeypatch the third-party package's
own objects (fb_scraper.browser.FacebookSession, fb_scraper.scraper.search_listings /
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


def _query(**params) -> MarketplaceQuery:
    return MarketplaceQuery(category="car", terms=["Tesla", "Model S"], params=params)


class _FakePage:
    def close(self):
        pass


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

    monkeypatch.setattr(fb_scraper_mod, "search_listings", fake_search_listings)
    monkeypatch.setattr(fb_scraper_mod, "visit_all_listings", fake_visit_all_listings)
    return fb_scraper_mod


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
    monkeypatch.setattr(fb_scraper_mod, "search_listings", lambda page, q, **k: [local, foreign])
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
    monkeypatch.setattr(fb_scraper_mod, "search_listings", lambda page, q, **k: candidates)
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
