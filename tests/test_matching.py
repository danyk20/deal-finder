from __future__ import annotations

from datetime import datetime

from deal_finder.adapters.base import Listing
from deal_finder.categories.car import CarCategory
from deal_finder.matching import dedup_cross_marketplace, passes_filters
from deal_finder.models import Watch

CAT = CarCategory()


def _listing(**kw):
    base = dict(
        marketplace="demo",
        external_id="x",
        url="http://x",
        title="Tesla Model S 75D",
        description="Top Zustand, Zürich",
        price=38900,
        location="Zürich",
        attributes={"year": 2017, "mileage_km": 95000},
    )
    base.update(kw)
    return Listing(**base)


def _watch(search=None, filters=None):
    return Watch(
        name="t",
        category="car",
        search_params=search or {"make": "Tesla", "model": "Model S"},
        filters=filters or {},
    )


def test_query_terms_must_match():
    w = _watch()
    q = CAT.build_query(w)
    assert q.terms == ["Tesla", "Model S"]
    # Model 3 listing should not pass (no "model s" in text).
    li = _listing(title="Tesla Model 3", description="Standard Range")
    assert not passes_filters(li, q, CAT, w)
    assert passes_filters(_listing(), q, CAT, w)


def test_price_bounds():
    w = _watch(filters={"price_max": 30000})
    q = CAT.build_query(w)
    assert not passes_filters(_listing(price=38900), q, CAT, w)
    assert passes_filters(_listing(price=25000), q, CAT, w)
    # Unknown price is not filtered out.
    assert passes_filters(_listing(price=None), q, CAT, w)


def test_year_and_mileage():
    w = _watch(filters={"year_min": 2016, "mileage_max": 100000})
    q = CAT.build_query(w)
    assert not passes_filters(_listing(attributes={"year": 2014}), q, CAT, w)
    assert not passes_filters(_listing(attributes={"year": 2018, "mileage_km": 150000}), q, CAT, w)
    assert passes_filters(_listing(attributes={"year": 2018, "mileage_km": 50000}), q, CAT, w)


def test_keyword_include_exclude():
    w = _watch(filters={"keywords_include": "Allrad", "keywords_exclude": "Unfall"})
    q = CAT.build_query(w)
    assert q.keywords_include == ["Allrad"] and q.keywords_exclude == ["Unfall"]
    assert passes_filters(_listing(description="Allrad, top"), q, CAT, w)
    assert not passes_filters(_listing(description="Frontantrieb"), q, CAT, w)  # missing include
    assert not passes_filters(_listing(description="Allrad aber Unfall"), q, CAT, w)  # has exclude


def test_location_filter():
    w = _watch(filters={"location": "Genève"})
    q = CAT.build_query(w)
    assert not passes_filters(_listing(location="Zürich", description="x"), q, CAT, w)
    assert passes_filters(_listing(location="Genève", description="x"), q, CAT, w)


def test_cross_marketplace_dedup():
    a = _listing(marketplace="tutti", external_id="1", title="Tesla Model S", price=40000)
    b = _listing(marketplace="ricardo", external_id="9", title="Tesla Model S", price=40000)
    c = _listing(marketplace="demo", external_id="3", title="Tesla Model S Plaid", price=80000)
    out = dedup_cross_marketplace([a, b, c])
    assert len(out) == 2  # a and b collapse, c stays
