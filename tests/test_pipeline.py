from __future__ import annotations

import threading

import pytest
from sqlmodel import select

from deal_finder import pipeline
from deal_finder.adapters.base import MarketplaceQuery
from deal_finder.config import Settings
from deal_finder.models import SeenListing, Watch


def _mk_watch(session, marketplaces=("demo",), seed_done=False):
    w = Watch(
        name="Tesla MS", category="car", marketplaces=list(marketplaces),
        search_params={"make": "Tesla", "model": "Model S"},
        filters={"price_max": 60000, "year_min": 2016},
        notify_email="me@example.com", notify_channel="email",
        questions=["Condition?"], seed_done=seed_done,
    )
    session.add(w)
    session.commit()
    session.refresh(w)
    return w


def test_seed_run_records_without_email(session, monkeypatch):
    sent = []
    monkeypatch.setattr(pipeline, "send_match_email", lambda *a, **k: sent.append(a))
    w = _mk_watch(session)
    s = Settings(seed_mode=True, ai_enabled=False, smtp_host="smtp.test")
    res = pipeline.run_watch(session, w, settings=s, notify=True, ignore_seen=False)
    assert res.seeded is True and res.notified == 0
    assert sent == []
    rows = session.exec(select(SeenListing).where(SeenListing.watch_id == w.id)).all()
    assert len(rows) == res.matched > 0
    assert w.seed_done is True


def test_normal_run_emails_then_dedups(session, monkeypatch):
    sent = []
    monkeypatch.setattr(pipeline, "send_match_email", lambda settings, to, subj, html: sent.append((to, subj)))
    w = _mk_watch(session)
    s = Settings(seed_mode=False, ai_enabled=False, smtp_host="smtp.test", smtp_from="x@y.z")
    res = pipeline.run_watch(session, w, settings=s)
    assert res.emailed is True and res.notified > 0
    assert len(sent) == 1
    # Second run finds nothing new -> no further email.
    res2 = pipeline.run_watch(session, w, settings=s)
    assert res2.new == 0 and res2.emailed is False
    assert len(sent) == 1


def test_email_failure_keeps_listing_unseen(session, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("smtp exploded")

    monkeypatch.setattr(pipeline, "send_match_email", boom)
    w = _mk_watch(session)
    s = Settings(seed_mode=False, ai_enabled=False, smtp_host="smtp.test")
    res = pipeline.run_watch(session, w, settings=s)
    assert res.emailed is False and res.error and "smtp exploded" in res.error
    # Nothing recorded as seen -> will be retried next run.
    rows = session.exec(select(SeenListing).where(SeenListing.watch_id == w.id)).all()
    assert rows == []


def test_search_isolated_runs_off_the_caller_thread():
    """Each adapter.search() call must run on its own throwaway OS thread -- see
    pipeline._search_isolated's docstring. A leaked/corrupted Playwright asyncio
    thread-local from one adapter's browser session must not survive to the next
    adapter call in the same watch run; running inline on the caller's thread (or
    reusing one shared background thread across calls) would let it."""

    class ThreadRecordingAdapter:
        def search(self, query, settings=None):
            return [threading.get_ident()]

    caller_thread = threading.get_ident()
    adapter = ThreadRecordingAdapter()
    query = MarketplaceQuery(category="car")

    first = pipeline._search_isolated(adapter, query, None)
    second = pipeline._search_isolated(adapter, query, None)

    assert first[0] != caller_thread
    assert second[0] != caller_thread


def test_search_isolated_propagates_adapter_exceptions():
    from deal_finder.adapters.base import AdapterError

    class BoomAdapter:
        def search(self, query, settings=None):
            raise AdapterError("kaboom")

    try:
        pipeline._search_isolated(BoomAdapter(), MarketplaceQuery(category="car"), None)
        assert False, "expected AdapterError"
    except AdapterError as exc:
        assert str(exc) == "kaboom"


def test_adapter_error_is_isolated(session, monkeypatch):
    # A non-browser adapter that always fails, injected into the registry.
    from deal_finder import registry
    from deal_finder.adapters.base import AdapterError, BaseAdapter

    class BoomAdapter(BaseAdapter):
        key = "boom"
        label = "Boom"
        supported_categories = {"car"}

        def search(self, query, settings=None):
            raise AdapterError("kaboom")

    monkeypatch.setitem(registry.ADAPTERS, "boom", BoomAdapter())
    monkeypatch.setattr(pipeline, "send_match_email", lambda *a, **k: None)
    w = _mk_watch(session, marketplaces=("demo", "boom"))
    s = Settings(seed_mode=False, ai_enabled=False, smtp_host="smtp.test")
    res = pipeline.run_watch(session, w, settings=s)
    assert res.adapter_status["demo"].startswith("ok")
    assert res.adapter_status["boom"].startswith("error")
    assert res.matched > 0  # demo still produced matches despite boom failing


def test_adapter_bot_wall_keeps_partial_listings(session, monkeypatch):
    """An adapter that fails partway through a multi-item fetch should still contribute
    whatever it fetched before failing, via AdapterError.partial_listings, instead of
    losing that run's work entirely -- see pipeline.py::_Run._searched."""
    from deal_finder import registry
    from deal_finder.adapters.base import AdapterError, BaseAdapter, Listing

    partial = [
        Listing(marketplace="wally", external_id="1", url="https://x/1", title="Tesla A", price=40000),
        Listing(marketplace="wally", external_id="2", url="https://x/2", title="Tesla B", price=41000),
    ]

    class WalledAdapter(BaseAdapter):
        key = "wally"
        label = "Wally"
        supported_categories = {"car"}

        def search(self, query, settings=None):
            raise AdapterError("wally: HTTP 403 (bot-wall / rate-limited)", partial_listings=partial)

    monkeypatch.setitem(registry.ADAPTERS, "wally", WalledAdapter())
    monkeypatch.setattr(pipeline, "send_match_email", lambda *a, **k: None)
    w = _mk_watch(session, marketplaces=("wally",))
    s = Settings(seed_mode=False, ai_enabled=False, smtp_host="smtp.test")
    res = pipeline.run_watch(session, w, settings=s)
    assert res.adapter_status["wally"].startswith("partial (2)")
    assert res.found == 2


def test_preview_writes_nothing(session):
    w = _mk_watch(session, seed_done=True)
    s = Settings(seed_mode=False, ai_enabled=False)
    res = pipeline.run_watch(session, w, settings=s, notify=False, ignore_seen=True)
    assert res.matched > 0 and res.matches_preview
    rows = session.exec(select(SeenListing).where(SeenListing.watch_id == w.id)).all()
    assert rows == []


def test_top_level_modules_use_single_dot_imports():
    """Guard the whole class of bug: modules directly under deal_finder/ must not use
    '..' relative imports (that reaches beyond the top-level package and always throws)."""
    import pathlib
    import re

    pkg_dir = pathlib.Path(pipeline.__file__).parent
    offenders = [
        f.name for f in pkg_dir.glob("*.py")
        if re.search(r"^\s*from \.\.", f.read_text(encoding="utf-8"), re.MULTILINE)
    ]
    assert offenders == [], f"top-level modules must use single-dot imports, found '..' in: {offenders}"


def test_dry_run_opens_tabs_instead_of_emailing(session, monkeypatch):
    opened_urls = []
    monkeypatch.setattr(pipeline, "open_listings", lambda urls, **k: opened_urls.extend(urls) or len(urls))
    sent = []
    monkeypatch.setattr(pipeline, "send_match_email", lambda *a, **k: sent.append(a))

    w = _mk_watch(session, seed_done=True)
    s = Settings(seed_mode=False, ai_enabled=False, smtp_host="smtp.test")
    res = pipeline.run_watch(session, w, settings=s, dry_run=True, ignore_seen=True)

    assert res.dry_run is True
    assert res.opened == len(opened_urls) > 0
    assert sent == []  # never emails
    rows = session.exec(select(SeenListing).where(SeenListing.watch_id == w.id)).all()
    assert rows == []  # no DB side effects


def test_dry_run_never_writes_even_with_ignore_seen_false(session, monkeypatch):
    """dry_run is a hard guarantee of no side effects, independent of ignore_seen."""
    monkeypatch.setattr(pipeline, "open_listings", lambda urls, **k: len(urls))
    w = _mk_watch(session)  # seed_done=False
    s = Settings(seed_mode=True, ai_enabled=False)
    res = pipeline.run_watch(session, w, settings=s, dry_run=True, ignore_seen=False)
    assert res.dry_run is True and res.opened > 0
    assert res.seeded is False  # seeding never triggers under dry_run
    rows = session.exec(select(SeenListing).where(SeenListing.watch_id == w.id)).all()
    assert rows == []
    session.refresh(w)
    assert w.seed_done is False  # untouched


def _mk_telegram_watch(session, marketplaces=("demo",), seed_done=False):
    w = Watch(
        name="Tesla MS", category="car", marketplaces=list(marketplaces),
        search_params={"make": "Tesla", "model": "Model S"},
        filters={"price_max": 60000, "year_min": 2016},
        notify_channel="telegram", telegram_chat_id="12345",
        questions=["Condition?"], seed_done=seed_done,
    )
    session.add(w)
    session.commit()
    session.refresh(w)
    return w


def test_normal_run_sends_telegram_then_dedups(session, monkeypatch):
    sent = []
    monkeypatch.setattr(pipeline, "send_telegram_match", lambda settings, chat_id, match: sent.append((chat_id, match)))
    w = _mk_telegram_watch(session)
    s = Settings(seed_mode=False, ai_enabled=False, telegram_bot_token="TOKEN")
    res = pipeline.run_watch(session, w, settings=s)
    assert res.channel == "telegram"
    assert res.emailed is True and res.notified > 0
    assert len(sent) == res.notified
    assert all(chat_id == "12345" for chat_id, _ in sent)
    # Second run finds nothing new -> no further sends.
    res2 = pipeline.run_watch(session, w, settings=s)
    assert res2.new == 0 and res2.emailed is False
    assert len(sent) == res.notified


def test_telegram_failure_keeps_listing_unseen(session, monkeypatch):
    calls = {"n": 0}

    def flaky(settings, chat_id, match):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("bad chat id")

    monkeypatch.setattr(pipeline, "send_telegram_match", flaky)
    w = _mk_telegram_watch(session)
    s = Settings(seed_mode=False, ai_enabled=False, telegram_bot_token="TOKEN")
    res = pipeline.run_watch(session, w, settings=s)
    assert res.error and "bad chat id" in res.error
    assert res.notified == 1  # only the first listing succeeded before the failure
    rows = session.exec(select(SeenListing).where(SeenListing.watch_id == w.id)).all()
    assert len(rows) == 1  # only the successfully-sent listing was recorded as seen


def test_dry_run_ignores_channel(session, monkeypatch):
    opened = []
    monkeypatch.setattr(pipeline, "open_listings", lambda urls, **k: opened.extend(urls) or len(urls))
    sent = []
    monkeypatch.setattr(pipeline, "send_telegram_match", lambda *a, **k: sent.append(a))
    w = _mk_telegram_watch(session, seed_done=True)
    s = Settings(seed_mode=False, ai_enabled=False, telegram_bot_token="TOKEN")
    res = pipeline.run_watch(session, w, settings=s, dry_run=True, ignore_seen=True)
    assert res.dry_run is True
    assert len(opened) > 0
    assert sent == []


def test_dry_run_takes_precedence_over_notify(session, monkeypatch):
    opened = []
    monkeypatch.setattr(pipeline, "open_listings", lambda urls, **k: opened.extend(urls) or len(urls))
    sent = []
    monkeypatch.setattr(pipeline, "send_match_email", lambda *a, **k: sent.append(a))
    w = _mk_watch(session, seed_done=True)
    s = Settings(seed_mode=False, ai_enabled=False, smtp_host="smtp.test")
    res = pipeline.run_watch(session, w, settings=s, notify=True, dry_run=True, ignore_seen=True)
    assert res.dry_run is True and res.emailed is False
    assert sent == [] and len(opened) > 0


def test_run_result_records_when_the_run_started(session):
    from deal_finder.models import utcnow

    w = _mk_watch(session)
    before = utcnow()
    res = pipeline.run_watch(session, w, settings=Settings(ai_enabled=False), notify=False, ignore_seen=True)
    assert before <= res.started_at <= utcnow()


# --- concurrency: marketplaces searched at once, one shared pool of AI work ---


def _tesla(marketplace, i, **kw):
    from deal_finder.adapters.base import Listing

    return Listing(
        marketplace=marketplace, external_id=str(i), url=f"https://{marketplace}/{i}",
        title=f"Tesla Model S {marketplace} {i}", price=30000 + i, attributes={"year": 2018}, **kw,
    )


def _adapter(key, search):
    from deal_finder.adapters.base import BaseAdapter

    cls = type(f"{key}Adapter", (BaseAdapter,), {
        "key": key, "label": key, "supported_categories": {"car"},
        "search": lambda self, query, settings=None: search(),
    })
    return cls()


def _install(monkeypatch, **searches):
    from deal_finder import registry

    for key, search in searches.items():
        monkeypatch.setitem(registry.ADAPTERS, key, _adapter(key, search))


class _PassingAi:
    """Answers every non-negotiables check with PASS; ``on_chat`` runs first."""

    def __init__(self, on_chat=lambda: None):
        self.on_chat = on_chat

    def chat(self, messages, **kwargs):
        self.on_chat()
        return "PASS"


def _with_non_negotiables(session, w):
    w.filters = {**w.filters, "non_negotiables": "must have free supercharging"}
    session.add(w)
    session.commit()
    return w


def test_marketplaces_are_searched_at_the_same_time(session, monkeypatch):
    both_searching = threading.Barrier(2, timeout=5)  # breaks if the searches run one by one

    def search(name):
        def run():
            both_searching.wait()
            return [_tesla(name, 1)]
        return run

    _install(monkeypatch, one=search("one"), two=search("two"))
    w = _mk_watch(session, marketplaces=("one", "two"))
    res = pipeline.run_watch(session, w, settings=Settings(ai_enabled=False), notify=False, ignore_seen=True)
    assert res.adapter_status == {"one": "ok (1)", "two": "ok (1)"}
    assert res.matched == 2


def test_ai_checks_listings_while_slower_marketplaces_still_search(session, monkeypatch):
    """The shared pool: the fast marketplace's listing is checked by the AI before the slow
    marketplace finishes. Results still come out in the watch's marketplace order."""
    ai_checked = threading.Event()

    def slow():
        assert ai_checked.wait(timeout=5), "the AI never started while this marketplace was searching"
        return [_tesla("slow", 1)]

    _install(monkeypatch, slow=slow, fast=lambda: [_tesla("fast", 1)])
    w = _with_non_negotiables(session, _mk_watch(session, marketplaces=("slow", "fast")))
    res = pipeline.run_watch(
        session, w, settings=Settings(ai_enabled=True), notify=False, ignore_seen=True,
        ai_client=_PassingAi(on_chat=ai_checked.set),
    )
    assert res.adapter_status == {"slow": "ok (1)", "fast": "ok (1)"}
    assert [m["marketplace"] for m in res.matches_preview] == ["slow", "fast"]


def test_ai_requests_run_in_parallel(session, monkeypatch):
    three_at_once = threading.Barrier(3, timeout=5)

    def chat():
        try:
            three_at_once.wait()
        except threading.BrokenBarrierError:
            raise AssertionError("the AI checks ran one by one")

    _install(monkeypatch, one=lambda: [_tesla("one", i) for i in range(3)])
    w = _with_non_negotiables(session, _mk_watch(session, marketplaces=("one",)))
    res = pipeline.run_watch(
        session, w, settings=Settings(ai_enabled=True, ai_parallel_requests=3), notify=False,
        ignore_seen=True, ai_client=_PassingAi(on_chat=chat),
    )
    assert res.matched == 3 and res.rejected_preview == []


def test_non_negotiables_failures_are_rejected(session, monkeypatch):
    class Ai:
        def chat(self, messages, **kwargs):
            return "FAIL: no free supercharging" if "one 1" in messages[1]["content"] else "PASS"

    _install(monkeypatch, one=lambda: [_tesla("one", 0), _tesla("one", 1)])
    w = _with_non_negotiables(session, _mk_watch(session, marketplaces=("one",)))
    res = pipeline.run_watch(session, w, settings=Settings(ai_enabled=True), notify=False, ignore_seen=True, ai_client=Ai())
    assert [m["external_id"] for m in res.matches_preview] == ["0"]
    assert [(r["external_id"], r["reason"]) for r in res.rejected_preview] == [
        ("1", "doesn't meet non-negotiables: no free supercharging")
    ]


@pytest.mark.parametrize("use_photos, rejected", [
    (True, [("1", "doesn't meet non-negotiables: from photo 1: no supercharger card visible")]),
    (False, []),
])
def test_non_negotiables_use_photos_only_when_the_requirement_says_so(session, monkeypatch, use_photos, rejected):
    class Ai:
        def chat(self, messages, **kwargs):
            if isinstance(messages[1]["content"], str):
                return "UNKNOWN: free supercharging"
            return "FAIL: no supercharger card visible"

    class Photo:
        content, headers = b"jpeg", {"content-type": "image/jpeg"}

        def raise_for_status(self):
            pass

    monkeypatch.setattr("deal_finder.ai.dealbreakers.httpx.get", lambda url, **kw: Photo())
    _install(monkeypatch, one=lambda: [_tesla("one", 1, image_urls=["https://one/1.jpg"])])
    w = _mk_watch(session, marketplaces=("one",))
    w.filters = {**w.filters, "non_negotiables": [{"text": "must have free supercharging", "photos": use_photos}]}
    session.add(w)
    session.commit()
    res = pipeline.run_watch(session, w, settings=Settings(ai_enabled=True), notify=False, ignore_seen=True, ai_client=Ai())
    assert [(r["external_id"], r["reason"]) for r in res.rejected_preview] == rejected
    assert res.matched == (0 if rejected else 1)


def test_telegram_sends_matches_while_slower_marketplaces_still_search(session, monkeypatch):
    first_sent = threading.Event()
    sent = []

    def send(settings, chat_id, match):
        sent.append(match.listing.marketplace)
        first_sent.set()

    def slow():
        assert first_sent.wait(timeout=5), "nothing was sent while this marketplace was searching"
        return [_tesla("slow", 1)]

    monkeypatch.setattr(pipeline, "send_telegram_match", send)
    _install(monkeypatch, slow=slow, fast=lambda: [_tesla("fast", 1)])
    w = _mk_telegram_watch(session, marketplaces=("slow", "fast"))
    res = pipeline.run_watch(session, w, settings=Settings(seed_mode=False, ai_enabled=False, telegram_bot_token="T"))
    assert sent == ["fast", "slow"]
    assert res.notified == 2 and res.error is None
    rows = session.exec(select(SeenListing).where(SeenListing.watch_id == w.id)).all()
    assert len(rows) == 2


def test_same_item_on_two_marketplaces_matches_once(session, monkeypatch):
    from deal_finder.adapters.base import Listing

    def copy(marketplace):
        return lambda: [Listing(marketplace=marketplace, external_id="9", url=f"https://{marketplace}/9",
                                title="Tesla Model S 90D", price=30000, attributes={"year": 2018})]

    _install(monkeypatch, one=copy("one"), two=copy("two"))
    w = _mk_watch(session, marketplaces=("one", "two"))
    res = pipeline.run_watch(session, w, settings=Settings(ai_enabled=False), notify=False, ignore_seen=True)
    assert res.found == 2 and res.matched == 1


def test_email_matches_are_enriched_during_the_run_and_sent_in_order(session, monkeypatch):
    sent = []
    monkeypatch.setattr(pipeline, "send_match_email", lambda settings, to, subject, html: sent.append(html))
    _install(monkeypatch, one=lambda: [_tesla("one", 1)], two=lambda: [_tesla("two", 1)])
    w = _mk_watch(session, marketplaces=("one", "two"))
    res = pipeline.run_watch(session, w, settings=Settings(seed_mode=False, ai_enabled=False, smtp_host="smtp.test"))
    assert res.emailed is True and res.notified == 2
    assert sent[0].index("Tesla Model S one 1") < sent[0].index("Tesla Model S two 1")


# --- an aggregator's listing (AutoUncle) of an ad on another portal -----------------


_AD = "https://portal.test/ad/7"


def _aggregator_and_portal(monkeypatch, *, aggregator, portal=lambda: [], fetch=None):
    """An aggregator ("agg") whose listings link to ads on a portal. The portal takes the
    AutoScout24 slot, so its "enabled in Settings" switch is adapter_autoscout24_enabled.
    Returns the list of ad links the portal was asked to fetch."""
    import re

    from deal_finder import registry
    from deal_finder.adapters.base import BaseAdapter

    fetched = []

    class Portal(BaseAdapter):
        key, label, supported_categories = "autoscout24", "Portal.ch", {"car"}

        def search(self, query, settings=None):
            return portal()

        def listing_id_from_url(self, url):
            m = re.match(r"https://portal\.test/ad/(\d+)$", url or "")
            return m.group(1) if m else None

        def fetch_listing(self, url, settings=None):
            fetched.append(url)
            if fetch is not None:
                return fetch(url)
            return _tesla("autoscout24", int(self.listing_id_from_url(url)), description="the portal's own text")

    class Aggregator(BaseAdapter):
        key, label, supported_categories = "agg", "Agg.ch", {"car"}

        def search(self, query, settings=None):
            return aggregator()

    monkeypatch.setitem(registry.ADAPTERS, "autoscout24", Portal())
    monkeypatch.setitem(registry.ADAPTERS, "agg", Aggregator())
    return fetched


def _via_aggregator(i=1, original_url=_AD):
    return _tesla("agg", i, original_url=original_url, location="8259 Kaltenbach")


def _rejected(res):
    return [(r["marketplace"], r["external_id"], r["reason"]) for r in res.rejected_preview]


def test_aggregator_listing_is_fetched_from_its_portal(session, monkeypatch):
    """Even when the watch doesn't search that portal itself."""
    fetched = _aggregator_and_portal(monkeypatch, aggregator=lambda: [_via_aggregator()])
    w = _mk_watch(session, marketplaces=("agg",))
    res = pipeline.run_watch(session, w, settings=Settings(ai_enabled=False), notify=False, ignore_seen=True)
    assert fetched == [_AD]
    [m] = res.matches_preview
    assert (m["marketplace"], m["external_id"], m["via"]) == ("autoscout24", "7", "Agg.ch")
    assert m["location"] == "8259 Kaltenbach"  # the portal's copy had none


def test_aggregator_listing_the_portal_found_this_run_is_skipped_unfetched(session, monkeypatch):
    portal_taken = threading.Event()
    real_filter = pipeline.filter_rejection_reason

    def filter_spy(li, *args, **kwargs):
        if li.marketplace == "autoscout24":
            portal_taken.set()
        return real_filter(li, *args, **kwargs)

    def aggregator():
        assert portal_taken.wait(timeout=5), "the portal's listing was never taken"
        return [_via_aggregator()]

    monkeypatch.setattr(pipeline, "filter_rejection_reason", filter_spy)
    fetched = _aggregator_and_portal(monkeypatch, aggregator=aggregator, portal=lambda: [_tesla("autoscout24", 7)])
    w = _mk_watch(session, marketplaces=("autoscout24", "agg"))
    res = pipeline.run_watch(session, w, settings=Settings(ai_enabled=False), notify=False, ignore_seen=True)
    assert fetched == []
    assert [(m["marketplace"], m["external_id"], m["via"]) for m in res.matches_preview] == [("autoscout24", "7", None)]
    assert _rejected(res) == [("agg", "1", "already found on Portal.ch in this run")]


def test_portal_finding_an_ad_already_fetched_via_the_aggregator_keeps_it_once(session, monkeypatch):
    fetched_once = threading.Event()

    def fetch(url):
        fetched_once.set()
        return _tesla("autoscout24", 7)

    def portal():
        assert fetched_once.wait(timeout=5)
        return [_tesla("autoscout24", 7)]

    _aggregator_and_portal(monkeypatch, aggregator=lambda: [_via_aggregator()], portal=portal, fetch=fetch)
    w = _mk_watch(session, marketplaces=("autoscout24", "agg"))
    res = pipeline.run_watch(session, w, settings=Settings(ai_enabled=False), notify=False, ignore_seen=True)
    assert len(res.matches_preview) == 1 and res.matches_preview[0]["external_id"] == "7"
    [(marketplace, external_id, reason)] = _rejected(res)
    assert (marketplace, external_id) == ("autoscout24", "7") and reason.startswith("already found on Portal.ch")


@pytest.mark.parametrize("seen_as", [
    ("autoscout24", "7", "https://portal.test/ad/7"),  # found by the portal's own search before
    ("agg", "9", _AD),  # kept with the aggregator's data before, linking to the ad
])
def test_aggregator_listing_already_seen_on_the_portal_is_skipped_unfetched(session, monkeypatch, seen_as):
    fetched = _aggregator_and_portal(monkeypatch, aggregator=lambda: [_via_aggregator()])
    w = _mk_watch(session, marketplaces=("agg",), seed_done=True)
    marketplace, external_id, url = seen_as
    session.add(SeenListing(watch_id=w.id, marketplace=marketplace, external_id=external_id, url=url, notified=True))
    session.commit()
    res = pipeline.run_watch(session, w, settings=Settings(ai_enabled=False), notify=False)
    assert fetched == [] and res.matches_preview == []
    assert _rejected(res) == [("agg", "1", "already seen on Portal.ch")]


@pytest.mark.parametrize("case", ["no adapter for the portal", "fetch fails", "portal off in Settings"])
def test_aggregator_listing_keeps_its_own_data_linking_to_the_original_ad(session, monkeypatch, case):
    from deal_finder.adapters.base import AdapterError

    original_url = "https://elsewhere.test/ad/9" if case == "no adapter for the portal" else _AD

    def fetch(url):
        raise AdapterError("Portal.ch request failed: down")

    _aggregator_and_portal(monkeypatch, aggregator=lambda: [_via_aggregator(original_url=original_url)],
                           fetch=fetch if case == "fetch fails" else None)
    settings = Settings(ai_enabled=False, adapter_autoscout24_enabled=case != "portal off in Settings")
    w = _mk_watch(session, marketplaces=("agg",))
    res = pipeline.run_watch(session, w, settings=settings, notify=False, ignore_seen=True)
    [m] = res.matches_preview
    assert (m["marketplace"], m["external_id"], m["url"], m["via"]) == ("agg", "1", original_url, None)
