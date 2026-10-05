"""AI-picked per-marketplace category (ai/category.py + site_categories.py + pipeline hook)."""

from __future__ import annotations

import pytest

from deal_finder import pipeline, site_categories
from deal_finder.adapters.base import BaseAdapter, SiteCategory
from deal_finder.ai.category import rank_site_categories
from deal_finder.ai.client import AiUnavailable
from deal_finder.config import Settings
from deal_finder.models import Watch

# A tutti-like tree: groups aren't selectable, only their sub-categories.
TREE = [
    SiteCategory("vehicles", "vehicles", selectable=False),
    SiteCategory("cars", "cars", parent_id="vehicles"),
    SiteCategory("computersAccessories", "computers accessories", selectable=False),
    SiteCategory("computers", "computers", parent_id="computersAccessories"),
    SiteCategory("tablets", "tablets", parent_id="computersAccessories"),
]


class StubAiClient:
    """Answers in order; once out of answers it behaves like an unreachable model."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.prompts: list[str] = []

    def chat(self, messages, **kwargs):
        self.prompts.append(messages[-1]["content"])
        if not self.responses:
            raise AiUnavailable("no more answers")
        return self.responses.pop(0)


class RaisingAiClient:
    def chat(self, messages, **kwargs):
        raise AiUnavailable("server down")


class _CatAdapter(BaseAdapter):
    key = "tutti"
    label = "tutti.ch"
    supported_categories = {"car"}

    def category_tree(self):
        return TREE


class _CatAdapter2(_CatAdapter):
    key = "ricardo"
    label = "Ricardo.ch"


class _PlainAdapter(BaseAdapter):
    key = "facebook"
    label = "Facebook"
    supported_categories = {"car"}


@pytest.fixture
def adapters(monkeypatch):
    registry = {"tutti": _CatAdapter(), "ricardo": _CatAdapter2(), "facebook": _PlainAdapter()}
    monkeypatch.setattr(site_categories, "get_adapter", registry.get)
    return registry


def _watch(session=None, **kw) -> Watch:
    w = Watch(
        name="Apple Mac Mini", category="car", marketplaces=kw.pop("marketplaces", ["tutti", "facebook"]),
        search_params=kw.pop("search_params", {"make": "Mac", "model": "Mini", "item_category": "desktop computer"}),
        filters=kw.pop("filters", {"price_max": "800", "location": "Zurich", "radius_km": "30"}),
        **kw,
    )
    if session is not None:
        session.add(w)
        session.commit()
        session.refresh(w)
    return w


def _without_basis(sites):
    return {k: {f: v for f, v in site.items() if f != "basis"} for k, site in sites.items()}


def _ids(nodes):
    return [n.id if n is not None else None for n in nodes]


# --- rank_site_categories -------------------------------------------------------------


def test_ranks_best_first_down_the_tree():
    # top level: computers accessories, then vehicles; inside each: ranked sub-categories
    client = StubAiClient(["2, 1", "1, 2", "1"])
    ranked = rank_site_categories(client, "Make: Mac", "tutti.ch", TREE)
    assert _ids(ranked) == ["computers", "tablets", "cars"]
    assert "1. vehicles (cars)" in client.prompts[0]
    assert "2. computers accessories (computers, tablets)" in client.prompts[0]
    assert "CURRENT CATEGORY: computers accessories" in client.prompts[1]
    assert "CURRENT CATEGORY: vehicles" in client.prompts[2]


def test_stops_asking_once_enough_candidates():
    client = StubAiClient(["2, 1", "1, 2"])
    assert _ids(rank_site_categories(client, "x", "tutti.ch", TREE, limit=2)) == ["computers", "tablets"]
    assert len(client.prompts) == 2  # vehicles never asked about


def test_none_fits_ranked_first_means_all_categories_as_pick():
    ranked = rank_site_categories(StubAiClient(["0, 2", "1"]), "x", "tutti.ch", TREE)
    assert _ids(ranked) == [None, "computers"]


def test_none_fits_inside_a_non_selectable_group_yields_nothing():
    assert rank_site_categories(StubAiClient(["2", "0"]), "x", "tutti.ch", TREE) == []


def test_can_stay_at_a_selectable_parent():
    tree = [SiteCategory("1", "Computer"), SiteCategory("2", "Notebooks", parent_id="1")]
    client = StubAiClient(["1", "0, 1"])
    assert _ids(rank_site_categories(client, "x", "Ricardo", tree)) == ["1", "2"]
    assert "Stay at 'Computer'" in client.prompts[1]


def test_failure_while_looking_for_runner_ups_keeps_the_pick():
    # top level ranks two branches; the second branch's question fails
    assert _ids(rank_site_categories(StubAiClient(["2, 1", "1"]), "x", "tutti.ch", TREE)) == ["computers"]


def test_answer_parsing_dedupes_drops_out_of_range_and_caps_per_level():
    client = StubAiClient(["Options 2, 2, 9, 1.", "2, 1", "1"])
    assert _ids(rank_site_categories(client, "x", "tutti.ch", TREE)) == ["tablets", "computers", "cars"]


def test_asks_without_reasoning():
    seen = {}

    class Client(StubAiClient):
        def chat(self, messages, **kwargs):
            seen.update(kwargs)
            return super().chat(messages, **kwargs)

    rank_site_categories(Client(["0"]), "x", "tutti.ch", TREE)
    assert seen["reasoning_effort"] == "none" and "json_mode" not in seen


@pytest.mark.parametrize("answer", ["7", "no idea", "-1", ""])
def test_unusable_first_answer_is_an_ai_failure(answer):
    with pytest.raises(AiUnavailable):
        rank_site_categories(StubAiClient([answer]), "x", "tutti.ch", TREE)


# --- describe_watch / resolve ---------------------------------------------------------


def test_description_has_every_item_field_but_not_location():
    text = site_categories.describe_watch(_watch())
    assert "Watch name: Apple Mac Mini" in text
    assert "Make: Mac" in text and "Model: Mini" in text
    assert "Category: desktop computer" in text
    assert "Max price (CHF): 800" in text
    assert "Zurich" not in text and "Radius" not in text


def _pick(session, w, answers=("2, 1", "1, 2", "1")):
    return site_categories.resolve(session, w, Settings(ai_enabled=True), ai_client=StubAiClient(list(answers)))


def test_resolve_picks_keeps_top_candidates_caches_and_reuses(session, adapters):
    w = _watch(session)
    assert _pick(session, w) == {"tutti": "computers"}
    assert _without_basis(w.site_categories["sites"]) == {"tutti": {
        "id": "computers", "path": "computers accessories > computers",
        "candidates": [
            {"id": "computers", "path": "computers accessories > computers"},
            {"id": "tablets", "path": "computers accessories > tablets"},
            {"id": "cars", "path": "vehicles > cars"},
        ],
    }}
    assert site_categories.is_current(w)
    # Cached: no further AI call (the stub has no answers and would fail).
    assert site_categories.resolve(session, w, Settings(ai_enabled=True), ai_client=StubAiClient([])) == {
        "tutti": "computers"
    }


def test_changed_fields_trigger_a_new_pick(session, adapters):
    w = _watch(session)
    _pick(session, w)
    w.search_params = {"make": "Tesla", "model": "Model 3", "item_category": "car"}
    assert not site_categories.is_current(w)
    assert _pick(session, w, ["1", "1"]) == {"tutti": "cars"}


def test_no_fitting_category_is_cached_as_all_categories(session, adapters):
    w = _watch(session)
    assert _pick(session, w, ["0"]) == {"tutti": None}
    assert _without_basis(w.site_categories["sites"])["tutti"] == {"id": None, "path": None, "candidates": []}
    assert site_categories.is_current(w)


# --- the user's choice from the edit form ---------------------------------------------


def test_user_can_switch_to_a_runner_up_or_all_categories(session, adapters):
    w = _watch(session)
    _pick(session, w)
    site_categories.apply_user_choices(w, {"tutti": "cars"})
    assert site_categories.resolve(session, w, Settings(ai_enabled=False)) == {"tutti": "cars"}
    site_categories.apply_user_choices(w, {"tutti": ""})
    assert site_categories.resolve(session, w, Settings(ai_enabled=False)) == {"tutti": None}
    site_categories.apply_user_choices(w, {"tutti": "computers"})  # the AI's pick again
    assert "user_set" not in w.site_categories["sites"]["tutti"]


def test_choices_outside_the_candidates_are_ignored(session, adapters):
    w = _watch(session)
    _pick(session, w)
    site_categories.apply_user_choices(w, {"tutti": "toys", "unknown": "x"})
    assert "user_set" not in w.site_categories["sites"]["tutti"]


def test_user_choice_survives_a_repick_while_still_a_candidate(session, adapters):
    w = _watch(session)
    _pick(session, w)
    site_categories.apply_user_choices(w, {"tutti": "tablets"})
    w.search_params = {**w.search_params, "item_category": "small computer"}
    assert _pick(session, w) == {"tutti": "tablets"}  # still among the new top 5
    w.search_params = {**w.search_params, "item_category": "car"}
    assert _pick(session, w, ["1", "1"]) == {"tutti": "cars"}  # tablets no longer offered -> AI pick


def test_form_offers_candidates_plus_all_categories(session, adapters):
    offered = [adapters["tutti"], adapters["facebook"]]  # has categories / doesn't
    w = _watch(session)
    pending = [{"key": "tutti", "label": "tutti.ch", "pending": True, "options": []}]
    assert site_categories.form_choices(w, offered) == pending
    _pick(session, w)
    site_categories.apply_user_choices(w, {"tutti": "tablets"})
    (choice,) = site_categories.form_choices(w, offered)
    assert [o["value"] for o in choice["options"]] == ["computers", "tablets", "cars", ""]
    assert choice["options"][0]["label"].endswith("(AI pick)")
    assert choice["selected"] == "tablets"


@pytest.mark.parametrize(
    "settings, client",
    [(Settings(ai_enabled=False), StubAiClient([])), (Settings(ai_enabled=True), RaisingAiClient())],
)
def test_ai_off_or_down_searches_all_categories_and_caches_nothing(session, adapters, settings, client):
    w = _watch(session)
    assert site_categories.resolve(session, w, settings, ai_client=client) == {}
    assert w.site_categories == {}  # retried on the next save/run


def test_persist_false_doesnt_write(session, adapters):
    w = _watch(session)
    assert site_categories.resolve(
        session, w, Settings(ai_enabled=True), ai_client=StubAiClient(["2", "1"]), persist=False
    ) == {"tutti": "computers"}
    assert w.site_categories == {}


def test_marketplaces_without_category_list_are_skipped(session, adapters):
    w = _watch(session, marketplaces=["facebook"])
    assert site_categories.resolve(session, w, Settings(ai_enabled=True), ai_client=StubAiClient([])) == {}
    assert site_categories.is_current(w)  # nothing to pick


# --- background pick after save -------------------------------------------------------


class _RecordingThread:
    started: list = []

    def __init__(self, target, name, daemon):
        self.name = name

    def start(self):
        _RecordingThread.started.append(self.name)


@pytest.fixture
def threads(monkeypatch):
    _RecordingThread.started = []
    monkeypatch.setattr(site_categories.threading, "Thread", _RecordingThread)
    return _RecordingThread.started


def test_background_pick_starts_when_stale_and_ai_enabled(session, adapters, threads, monkeypatch):
    monkeypatch.setattr(site_categories, "runtime_settings", lambda s: Settings(ai_enabled=True))
    w = _watch(session)
    site_categories.resolve_in_background(session, w)
    assert threads == [f"site-categories-{w.id}"]


def test_background_pick_skipped_when_ai_disabled(session, adapters, threads, monkeypatch):
    monkeypatch.setattr(site_categories, "runtime_settings", lambda s: Settings(ai_enabled=False))
    site_categories.resolve_in_background(session, _watch(session))
    assert threads == []


def test_background_pick_skipped_when_current(session, adapters, threads, monkeypatch):
    monkeypatch.setattr(site_categories, "runtime_settings", lambda s: Settings(ai_enabled=True))
    w = _watch(session)
    site_categories.resolve(session, w, Settings(ai_enabled=True), ai_client=StubAiClient(["2", "1"]))
    site_categories.resolve_in_background(session, w)
    assert threads == []


# --- pipeline hook --------------------------------------------------------------------


@pytest.mark.parametrize("ignore_seen, persist", [(True, False), (False, True)])
def test_run_passes_picked_categories_to_adapters(session, monkeypatch, ignore_seen, persist):
    calls, seen_queries = [], []

    def fake_resolve(session_, watch, settings, *, ai_client=None, persist=True, on_status=None):
        calls.append(persist)
        return {"tutti": "computers"}

    monkeypatch.setattr(pipeline.site_categories, "resolve", fake_resolve)
    monkeypatch.setattr(pipeline, "_search_isolated", lambda a, q, s: seen_queries.append(q) or [])
    w = _watch(session, marketplaces=["demo"], seed_done=True)
    pipeline.run_watch(session, w, settings=Settings(ai_enabled=False, seed_mode=False), notify=False,
                       ignore_seen=ignore_seen)
    assert calls == [persist]  # previews never write to the DB
    assert seen_queries and seen_queries[0].site_categories == {"tutti": "computers"}


def test_startup_sweep_picks_only_stale_watches_in_one_thread(session, adapters, threads, monkeypatch):
    monkeypatch.setattr(site_categories, "runtime_settings", lambda s: Settings(ai_enabled=True))
    current = _watch(session)
    site_categories.resolve(session, current, Settings(ai_enabled=True), ai_client=StubAiClient(["2", "1"]))
    _watch(session, marketplaces=["facebook"])  # nothing to pick
    site_categories.resolve_stale_in_background()
    assert threads == []  # everything current -> no thread

    _watch(session)  # stale
    site_categories.resolve_stale_in_background()
    assert threads == ["site-categories-startup"]


def test_startup_sweep_skipped_when_ai_disabled(session, adapters, threads):
    _watch(session)  # stale, but conftest disables the AI
    site_categories.resolve_stale_in_background()
    assert threads == []


def test_picks_stored_by_older_code_are_redone(session, adapters, monkeypatch):
    w = _watch(session)
    _pick(session, w)
    assert site_categories.is_current(w)
    monkeypatch.setattr(site_categories, "_FORMAT", "1")  # as if stored by an older version
    assert not site_categories.is_current(w)


def test_form_has_a_pending_dropdown_for_unticked_or_new(session, adapters):
    """The form shows/hides dropdowns as marketplaces get ticked, so every offered
    marketplace with categories gets one -- pending until the AI picks after saving."""
    offered = [adapters["tutti"], adapters["facebook"]]
    pending = [{"key": "tutti", "label": "tutti.ch", "pending": True, "options": []}]
    assert site_categories.form_choices(None, offered) == pending  # new watch
    w = _watch(session, marketplaces=["facebook"])
    assert site_categories.form_choices(w, offered) == pending  # tutti not ticked (yet)



# --- only re-pick what changed --------------------------------------------------------


def test_ticking_another_marketplace_only_picks_for_that_one(session, adapters):
    w = _watch(session)
    _pick(session, w)
    w.marketplaces = ["tutti", "ricardo", "facebook"]
    client = StubAiClient(["2, 1", "1, 2", "1"])  # enough for exactly one marketplace
    assert site_categories.resolve(session, w, Settings(ai_enabled=True), ai_client=client) == {
        "tutti": "computers", "ricardo": "computers"
    }
    assert all("CURRENT CATEGORY" in p or "MARKETPLACE: Ricardo.ch" in p for p in client.prompts)
    assert len(client.prompts) == 3 and "MARKETPLACE: Ricardo.ch" in client.prompts[0]


def test_unticking_and_reticking_keeps_the_pick(session, adapters):
    w = _watch(session)
    _pick(session, w)
    w.marketplaces = ["facebook"]
    assert site_categories.resolve(session, w, Settings(ai_enabled=True), ai_client=StubAiClient([])) == {}
    w.marketplaces = ["tutti", "facebook"]
    assert site_categories.is_current(w)
    assert site_categories.resolve(session, w, Settings(ai_enabled=True), ai_client=StubAiClient([])) == {
        "tutti": "computers"
    }


def test_fields_the_ai_doesnt_see_dont_make_it_stale(session, adapters):
    w = _watch(session)
    _pick(session, w)
    w.schedule_value, w.questions, w.notify_channel = "2h", ["Anything else?"], "email"
    w.filters = {**w.filters, "location": "Bern", "radius_km": "80"}
    assert site_categories.is_current(w)


def test_ai_down_for_one_marketplace_keeps_the_others(session, adapters):
    w = _watch(session, marketplaces=["tutti", "ricardo"])
    client = StubAiClient(["2, 1", "1, 2", "1"])  # tutti gets picked, then the AI is "down"
    assert site_categories.resolve(session, w, Settings(ai_enabled=True), ai_client=client) == {
        "tutti": "computers"
    }
    assert site_categories.stale_keys(w) == {"ricardo"}  # retried next time; tutti isn't


def test_format_2_picks_stay_current_without_asking_the_ai(session, adapters):
    w = _watch(session)
    text = site_categories.describe_watch(w)
    legacy = site_categories._legacy_basis(text, site_categories.category_adapters(w))
    w.site_categories = {"basis": legacy, "sites": {"tutti": {"id": "tablets", "path": "x > tablets", "candidates": []}}}
    assert site_categories.is_current(w)
    assert site_categories.resolve(session, w, Settings(ai_enabled=True), ai_client=StubAiClient([])) == {
        "tutti": "tablets"
    }
    assert w.site_categories["sites"]["tutti"]["basis"] == site_categories._site_basis(text, adapters["tutti"])
    w.search_params = {**w.search_params, "model": "Studio"}
    assert not site_categories.is_current(w)  # a real change still re-picks
