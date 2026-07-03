"""Tests the shared BrowserAdapter flow with a FakeBrowserSession — no real browser.

Verifies the human 'one listing at a time' behaviour: listings are opened sequentially,
human_pause() is called between them, the per-run cap is honoured, and a bot-wall on the
search page raises AdapterError (isolated by the pipeline)."""

from __future__ import annotations

import pytest

from deal_finder.adapters.base import MarketplaceQuery
from deal_finder.adapters.tutti import TuttiBrowserAdapter
from deal_finder.browser.detect import check_blocked
from deal_finder.browser.errors import BotWallError
from deal_finder.browser.page import PageView
from deal_finder.config import Settings

# tutti detail URLs are /de/vi/{canton}/fahrzeuge/autos/{slug}/{id} (verified live).
SEARCH_HTML = (
    "<html><body>"
    '<a href="/de/vi/zurich/fahrzeuge/autos/tesla-model-s-a/111">a</a>'
    '<a href="/de/vi/bern/fahrzeuge/autos/tesla-model-s-b/222">b</a>'
    '<a href="/de/vi/aargau/fahrzeuge/autos/tesla-model-s-c/333">c</a>'
    '<a href="/de/vi/zug/spielzeuge/modellbau/toy-car/999">toy (excluded)</a>'
    "</body></html>"
)


def _detail_html(title):
    return f"""<html><head><script type="application/ld+json">
    {{"@type":"Car","name":"{title}","description":"Tesla Model S, 2019, 60000 km",
      "offers":{{"price":49000,"priceCurrency":"CHF"}},"vehicleModelDate":"2019"}}
    </script></head><body>ok</body></html>"""


class FakeBrowserSession:
    def __init__(self, search_html=SEARCH_HTML, blocked=False):
        self._search_html = search_html
        self._blocked = blocked
        self.opened: list[str] = []
        self.pauses = 0

    def goto(self, url, *, scroll=True):
        view = PageView(url=url, html=self._search_html, status=403 if self._blocked else 200)
        check_blocked(view, "fake")  # mirrors the real session
        return view

    def open_detail(self, url):
        self.opened.append(url)
        return PageView(url=url, html=_detail_html(f"Tesla Model S ({url[-3:]})"), status=200)

    def human_pause(self):
        self.pauses += 1


def _query():
    return MarketplaceQuery(category="car", terms=["Tesla", "Model S"])


def test_one_at_a_time_flow():
    fake = FakeBrowserSession()
    settings = Settings(browser_max_items_per_run=15, browser_search_pages=1)
    listings = list(TuttiBrowserAdapter().search(_query(), browser=fake, settings=settings))
    assert len(listings) == 3  # the toy link is excluded by the /autos/ filter
    assert [li.external_id for li in listings] == ["111", "222", "333"]
    assert fake.opened == [
        "https://www.tutti.ch/de/vi/zurich/fahrzeuge/autos/tesla-model-s-a/111",
        "https://www.tutti.ch/de/vi/bern/fahrzeuge/autos/tesla-model-s-b/222",
        "https://www.tutti.ch/de/vi/aargau/fahrzeuge/autos/tesla-model-s-c/333",
    ]  # sequential, one at a time
    assert fake.pauses == 3  # a human pause before each listing
    li = listings[0]
    assert li.marketplace == "tutti"
    assert li.price == 49000.0
    assert li.attributes["year"] == 2019
    assert li.attributes["mileage_km"] == 60000


def test_per_run_cap():
    fake = FakeBrowserSession()
    settings = Settings(browser_max_items_per_run=1, browser_search_pages=1)
    listings = list(TuttiBrowserAdapter().search(_query(), browser=fake, settings=settings))
    assert len(listings) == 1
    assert len(fake.opened) == 1


def test_blocked_search_raises():
    fake = FakeBrowserSession(blocked=True)
    settings = Settings(browser_search_pages=1)
    with pytest.raises(BotWallError):
        list(TuttiBrowserAdapter().search(_query(), browser=fake, settings=settings))


def test_no_browser_raises_browser_unavailable():
    from deal_finder.browser.errors import BrowserUnavailable

    with pytest.raises(BrowserUnavailable):
        list(TuttiBrowserAdapter().search(_query(), browser=None, settings=Settings()))
