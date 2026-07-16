from __future__ import annotations

from datetime import datetime

from deal_finder.adapters.base import Listing
from deal_finder.ai.client import AiUnavailable
from deal_finder.categories.car import CarCategory
from deal_finder.config import Settings
from deal_finder.matching import dedup_cross_marketplace, filter_rejection_reason, passes_filters
from deal_finder.models import Watch

CAT = CarCategory()


class StubAiClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def chat(self, messages, **kwargs):
        self.calls += 1
        return self.responses.pop(0)


class RaisingAiClient:
    def chat(self, messages, **kwargs):
        raise AiUnavailable("server down")


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


def test_non_negotiables_skipped_when_blank():
    w = _watch(filters={"non_negotiables": "   "})
    q = CAT.build_query(w)
    client = StubAiClient([])
    assert passes_filters(_listing(), q, CAT, w)  # no settings/ai_client at all
    assert filter_rejection_reason(_listing(), q, CAT, w, settings=Settings(ai_enabled=True), ai_client=client) is None
    assert client.calls == 0  # blank requirement text -> never even calls the model


def test_non_negotiables_skipped_without_settings():
    """Existing callers that don't pass settings/ai_client (e.g. other tests, or a
    context with no known settings) must not be affected by a non_negotiables filter."""
    w = _watch(filters={"non_negotiables": "must be green"})
    q = CAT.build_query(w)
    assert passes_filters(_listing(), q, CAT, w)


def test_non_negotiables_rejects_on_ai_fail():
    w = _watch(filters={"non_negotiables": "must be green"})
    q = CAT.build_query(w)
    client = StubAiClient(["FAIL: the car is red, not green"])
    reason = filter_rejection_reason(
        _listing(), q, CAT, w, settings=Settings(ai_enabled=True), ai_client=client
    )
    assert reason is not None
    assert "red, not green" in reason
    assert client.calls == 1


def test_non_negotiables_passes_on_ai_pass():
    w = _watch(filters={"non_negotiables": "must be green"})
    q = CAT.build_query(w)
    client = StubAiClient(["PASS"])
    assert filter_rejection_reason(
        _listing(), q, CAT, w, settings=Settings(ai_enabled=True), ai_client=client
    ) is None


def test_non_negotiables_fails_open_when_ai_unavailable():
    """An AI hiccup on this specific check must never silently hide a real match."""
    w = _watch(filters={"non_negotiables": "must be green"})
    q = CAT.build_query(w)
    assert filter_rejection_reason(
        _listing(), q, CAT, w, settings=Settings(ai_enabled=True), ai_client=RaisingAiClient()
    ) is None


def test_non_negotiables_never_called_when_a_cheap_filter_already_failed():
    """The AI check runs LAST -- a listing already rejected by price/keyword/etc. must
    never pay for an AI call at all."""
    w = _watch(filters={"price_max": 1000, "non_negotiables": "must be green"})
    q = CAT.build_query(w)
    client = StubAiClient([])
    reason = filter_rejection_reason(
        _listing(price=38900), q, CAT, w, settings=Settings(ai_enabled=True), ai_client=client
    )
    assert reason is not None and "price" in reason
    assert client.calls == 0


def test_cross_marketplace_dedup():
    a = _listing(marketplace="tutti", external_id="1", title="Tesla Model S", price=40000)
    b = _listing(marketplace="ricardo", external_id="9", title="Tesla Model S", price=40000)
    c = _listing(marketplace="demo", external_id="3", title="Tesla Model S Plaid", price=80000)
    out = dedup_cross_marketplace([a, b, c])
    assert len(out) == 2  # a and b collapse, c stays
