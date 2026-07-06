from __future__ import annotations

from sqlmodel import select

from deal_finder import pipeline
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


def test_adapter_error_is_isolated(session, monkeypatch):
    # A non-browser adapter that always fails, injected into the registry.
    from deal_finder import registry
    from deal_finder.adapters.base import AdapterError, BaseAdapter

    class BoomAdapter(BaseAdapter):
        key = "boom"
        label = "Boom"
        supported_categories = {"car"}

        def search(self, query):
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
    """A browser adapter that hits a bot-wall partway through should still contribute
    whatever it fetched before the wall, via BotWallError.partial_listings, instead of
    losing that run's work entirely -- see pipeline.py::_collect_listings."""
    from deal_finder import registry
    from deal_finder.adapters.base import BaseAdapter, Listing
    from deal_finder.browser.errors import BotWallError

    partial = [
        Listing(marketplace="wally", external_id="1", url="https://x/1", title="Tesla A", price=40000),
        Listing(marketplace="wally", external_id="2", url="https://x/2", title="Tesla B", price=41000),
    ]

    class WalledAdapter(BaseAdapter):
        key = "wally"
        label = "Wally"
        supported_categories = {"car"}

        def search(self, query):
            raise BotWallError("wally: HTTP 403 (bot-wall / rate-limited)", partial_listings=partial)

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


def test_browser_session_imports_browser_layer(monkeypatch):
    """Regression: _browser_session must import deal_finder.browser with a SINGLE dot.
    A '..browser' typo raised 'attempted relative import beyond top-level package', which
    was swallowed into a None session so every browser adapter (tutti/Ricardo) failed with
    an opaque 'no browser session available'. Mocks avoid launching a real browser."""
    import deal_finder.browser as browser_mod

    class _FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(browser_mod, "is_available", lambda: True)
    monkeypatch.setattr(browser_mod, "BrowserSession", lambda cfg: _FakeSession())

    with pipeline._browser_session(Settings()) as (browser, error):
        assert error is None, f"browser layer should import cleanly, got: {error}"
        assert browser is not None


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
