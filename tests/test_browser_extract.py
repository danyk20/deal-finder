from __future__ import annotations

import re

import pytest

from deal_finder.browser import extract as ex
from deal_finder.browser.detect import check_blocked
from deal_finder.browser.errors import BotWallError
from deal_finder.browser.page import PageView

DETAIL_HTML = """
<html><head>
<script type="application/ld+json">
{"@type":"Car","name":"Tesla Model S 75D","description":"Sehr gepflegt, 95'000 km, Erstzulassung 2017.",
 "offers":{"price":"38900","priceCurrency":"CHF"},"image":["https://x/a.jpg","https://x/b.jpg"],
 "mileageFromOdometer":{"value":95000},"vehicleModelDate":"2017"}
</script>
<meta property="og:title" content="Tesla Model S 75D"></head><body>ok</body></html>
"""

SEARCH_HTML = """
<html><body>
<a href="/vi/111">one</a>
<a href="/vi/111?ref=x">dup</a>
<a href="/vi/222/">two</a>
<a href="/de/a/tesla-model-s-98765/">ricardo</a>
</body></html>
"""


def test_parse_price_variants():
    assert ex.parse_price("CHF 38'900.-") == (38900.0, "CHF")
    assert ex.parse_price("Fr. 27500") == (27500.0, "CHF")
    assert ex.parse_price({"amount": 45000}) == (45000.0, "CHF")
    assert ex.parse_price("€ 45.900")[0] == 45900.0
    assert ex.parse_price("€ 45.900")[1] == "EUR"
    assert ex.parse_price(None) == (None, "CHF")


def test_parse_km_and_year():
    assert ex.parse_int_km("Occasion, 95'000 km, top") == 95000
    assert ex.parse_int_km("120 000 km") == 120000
    assert ex.parse_int_km("no mileage here") is None
    assert ex.parse_year("Erstzulassung 2017, gepflegt") == 2017
    assert ex.parse_year("kein Jahr") is None


def test_car_listing_fields_from_jsonld():
    f = ex.car_listing_fields(DETAIL_HTML)
    assert f["title"] == "Tesla Model S 75D"
    assert f["price"] == 38900.0
    assert f["year"] == 2017
    assert f["mileage_km"] == 95000
    assert f["image_urls"] == ["https://x/a.jpg", "https://x/b.jpg"]


GRAPH_WRAPPED_DETAIL_HTML = """
<html><head>
<script id="pdp-json-ld" type="application/ld+json">
{"@context":"https://schema.org","@graph":[
  {"@type":"WebPage","name":"irrelevant wrapper node"},
  {"@type":"Product","name":"Tesla Model S90D","description":"Free SuC",
   "offers":{"@type":"Offer","price":"1","priceCurrency":"CHF"},
   "image":["https://x/a.jpg"],"vehicleModelDate":"2016"}
]}
</script></head><body>ok</body></html>
"""


def test_find_json_ld_unpacks_graph_wrapper():
    """Ricardo's current detail pages (and other schema.org sites) bundle multiple
    entities under one @graph-wrapped script tag rather than separate top-level nodes."""
    nodes = ex.find_json_ld(GRAPH_WRAPPED_DETAIL_HTML)
    types = [n.get("@type") for n in nodes]
    assert "Product" in types
    assert "WebPage" in types


def test_car_listing_fields_from_graph_wrapped_jsonld():
    f = ex.car_listing_fields(GRAPH_WRAPPED_DETAIL_HTML)
    assert f["title"] == "Tesla Model S90D"
    assert f["price"] == 1.0
    assert f["year"] == 2016


def test_card_links_dedup_and_id():
    tutti = ex.card_links(SEARCH_HTML, re.compile(r"/vi/(\d+)"), "https://www.tutti.ch")
    assert dict(tutti) == {
        "111": "https://www.tutti.ch/vi/111",
        "222": "https://www.tutti.ch/vi/222/",  # site's own URL form preserved
    }
    ric = ex.card_links(SEARCH_HTML, re.compile(r"/a/[^/?#]*?-(\d+)(?:[/?#]|$)"), "https://www.ricardo.ch")
    assert dict(ric) == {"98765": "https://www.ricardo.ch/de/a/tesla-model-s-98765/"}


def test_check_blocked():
    check_blocked(PageView(url="https://x", html="<html>fine</html>", status=200), "x")  # no raise
    with pytest.raises(BotWallError):
        check_blocked(PageView(url="https://x", html="ok", status=403), "x")
    with pytest.raises(BotWallError):
        check_blocked(PageView(url="https://x", html="<h1>Just a moment...</h1>", status=200), "cf")
    with pytest.raises(BotWallError):
        check_blocked(PageView(url="https://site/login", html="ok", status=200), "fb")


def test_empty_html_is_safe():
    assert ex.car_listing_fields("") == {
        "title": "", "description": "", "price": None, "currency": "CHF",
        "image_urls": [], "year": None, "mileage_km": None, "location": None,
    }
    assert ex.card_links("", re.compile(r"/vi/(\d+)"), "https://x") == []
