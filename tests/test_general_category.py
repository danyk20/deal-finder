"""The 'general' watch type (anything that isn't a car) next to 'car'."""

from __future__ import annotations

from deal_finder import pipeline
from deal_finder.adapters.base import Listing
from deal_finder.categories.general import GeneralCategory
from deal_finder.config import Settings
from deal_finder.matching import passes_filters
from deal_finder.models import Watch
from deal_finder.notify.email import render_email
from deal_finder.registry import adapters_for_category, get_category

CAR_ONLY = {"autoscout24", "autouncle", "autolina"}


def _general(**filters) -> Watch:
    return Watch(name="Mac Mini", category="general", search_params={"query": "Mac Mini M2"}, filters=filters)


def test_registered_next_to_car():
    assert get_category("general").label == "General item"
    assert get_category("car").label == "Car"


def test_only_marketplaces_that_sell_more_than_cars():
    assert {a.key for a in adapters_for_category("general")} == {"tutti", "ricardo", "facebook"}
    assert CAR_ONLY <= {a.key for a in adapters_for_category("car")}


def test_fields_have_search_text_instead_of_make_model_year_mileage():
    cat = GeneralCategory()
    names = {f.name for f in cat.search_param_fields + cat.filter_fields}
    assert "query" in names and "item_category" in names and "price_max" in names
    assert not names & {"make", "model", "year_min", "year_max", "mileage_max"}


def test_build_query_searches_the_text_and_requires_every_word():
    q = GeneralCategory().build_query(_general(price_max="800", keywords_exclude="Intel"))
    assert q.text == "Mac Mini M2" and q.terms == ["Mac", "Mini", "M2"]
    assert q.price_max == 800 and q.keywords_exclude == ["Intel"]
    w = _general()

    def listing(title):
        return Listing(marketplace="tutti", external_id="1", url="u", title=title, price=500)

    assert passes_filters(listing("Apple Mac mini (M2, 2023)"), q, GeneralCategory(), w)
    assert not passes_filters(listing("Apple Mac mini M1"), q, GeneralCategory(), w)


def test_no_car_year_or_mileage_filtering():
    w = _general(year_min="2016")  # leftover from a car watch switched to general
    li = Listing(marketplace="tutti", external_id="1", url="u", title="Mac Mini M2",
                 attributes={"year": 2014, "mileage_km": 999999})
    assert passes_filters(li, GeneralCategory().build_query(w), GeneralCategory(), w)


def test_run_skips_car_only_marketplaces(session, monkeypatch):
    w = _general()
    w.marketplaces = ["autoscout24", "tutti"]
    session.add(w)
    session.commit()
    searched = []
    monkeypatch.setattr(pipeline, "_search_isolated", lambda a, q, s: searched.append(a.key) or [])
    res = pipeline.run_watch(session, w, settings=Settings(ai_enabled=False), notify=False, ignore_seen=True)
    assert searched == ["tutti"]
    assert res.adapter_status["autoscout24"] == "doesn't support General item watches"


def test_email_names_the_search_text_and_type():
    subject, html = render_email(_general(), [])
    assert subject == "Deal Finder: 0 new Mac Mini M2 matches"
    assert "General item" in html


# --- web form -------------------------------------------------------------------------


def _offered_marketplaces(page: str) -> set[str]:
    import re

    return set(re.findall(r'name="marketplaces" value="([a-z0-9]+)"', page))


def test_new_form_defaults_to_car_and_offers_a_type_picker(client):
    page = client.get("/watches/new").text
    assert 'id="watch-type"' in page and 'value="general"' in page
    assert 'name="sp_make"' in page and 'name="f_year_min"' in page and 'name="f_mileage_max"' in page
    assert CAR_ONLY <= _offered_marketplaces(page)


def test_new_general_form_has_search_text_and_no_car_only_marketplaces(client):
    page = client.get("/watches/new?watch_type=general").text
    assert 'name="sp_query"' in page and 'name="sp_item_category"' in page
    for car_field in ("sp_make", "sp_model", "f_year_min", "f_year_max", "f_mileage_max"):
        assert f'name="{car_field}"' not in page
    assert _offered_marketplaces(page) == {"tutti", "ricardo", "facebook"}
    assert "Is the item in perfect working condition?" in page  # general default questions


def test_switching_an_existing_car_watch_to_general(client):
    from deal_finder.registry import get_category

    car_questions = get_category("car").default_questions
    wid = client.post("/api/watches", json={
        "name": "Mac Mini", "category": "car", "marketplaces": ["tutti", "autoscout24", "facebook"],
        "search_params": {"make": "Mac", "model": "Mini"}, "filters": {"price_max": "800", "year_min": "2016"},
        "questions": car_questions,
    }).json()["id"]
    page = client.get(f"/watches/{wid}/edit?watch_type=general").text
    assert 'name="sp_query"' in page and 'value="Mac Mini"' in page  # make + model carried over
    assert 'value="800"' in page  # shared fields keep their values
    assert "Is the item in perfect working condition?" in page  # untouched defaults follow the type
    assert _offered_marketplaces(page) == {"tutti", "ricardo", "facebook"}

    form = {"name": "Mac Mini", "category": "general", "sp_query": "Mac Mini M2", "f_price_max": "800",
            "marketplaces": ["tutti", "facebook"], "questions": "Is it working?"}
    assert client.post(f"/watches/{wid}", data=form, follow_redirects=False).status_code == 303
    w = client.get(f"/api/watches/{wid}").json()
    assert w["category"] == "general" and w["marketplaces"] == ["tutti", "facebook"]
    assert w["search_params"]["query"] == "Mac Mini M2" and "year_min" not in w["filters"]
    assert "General item: Mac Mini M2" in client.get(f"/watches/{wid}").text
    assert "Mac Mini M2" in client.get("/").text
