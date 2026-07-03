from __future__ import annotations

import json
from pathlib import Path

import httpx
import respx

from deal_finder.adapters.base import AdapterError, MarketplaceQuery
from deal_finder.adapters.tutti import GRAPHQL_ENDPOINT, TuttiAdapter, parse_tutti_response

FIXTURE = json.loads((Path(__file__).parent / "fixtures" / "tutti_search.json").read_text())


def test_parse_handles_both_price_shapes():
    listings = parse_tutti_response(FIXTURE)
    assert len(listings) == 2
    a, b = listings
    assert a.marketplace == "tutti"
    assert a.external_id == "111"
    assert a.price == 38900.0
    assert a.attributes["year"] == 2017
    assert a.url == "https://www.tutti.ch/vi/111"
    assert b.price == 27500.0  # nested {"amount": ...}
    assert b.posted_at is not None


def test_parse_tolerates_empty():
    assert parse_tutti_response({}) == []
    assert parse_tutti_response({"data": {"search": {"items": []}}}) == []


@respx.mock
def test_search_maps_response():
    respx.post(GRAPHQL_ENDPOINT).mock(return_value=httpx.Response(200, json=FIXTURE))
    out = list(TuttiAdapter().search(MarketplaceQuery(category="car", terms=["Tesla", "Model S"])))
    assert {li.external_id for li in out} == {"111", "112"}


@respx.mock
def test_blocked_raises_adapter_error():
    respx.post(GRAPHQL_ENDPOINT).mock(return_value=httpx.Response(403, text="Forbidden"))
    adapter = TuttiAdapter()
    try:
        list(adapter.search(MarketplaceQuery(category="car", terms=["Tesla"])))
        assert False, "expected AdapterError"
    except AdapterError as exc:
        assert "403" in str(exc)
    assert adapter.health_check() is False
