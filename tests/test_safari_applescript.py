"""Tests SafariAppleScriptSession and the engine dispatch, with subprocess/osascript
fully mocked out -- no real macOS Safari involved, so this runs in CI too."""

from __future__ import annotations

import pytest

from deal_finder.browser import safari_applescript as sa
from deal_finder.browser.errors import BotWallError, BrowserUnavailable
from deal_finder.browser.session import BrowserConfig, BrowserSession


def test_escape_as_handles_quotes_and_backslashes():
    assert sa._escape_as('He said "hi"') == 'He said \\"hi\\"'
    assert sa._escape_as("back\\slash") == "back\\\\slash"


def test_new_dispatches_to_safari_session_for_safari_engine():
    cfg = BrowserConfig(engine="safari")
    session = BrowserSession(cfg)
    assert isinstance(session, sa.SafariAppleScriptSession)
    assert session.config is cfg


def test_new_stays_browser_session_for_other_engines():
    for engine in ("webkit", "chromium"):
        session = BrowserSession(BrowserConfig(engine=engine))
        assert type(session) is BrowserSession


class _FakeCalls:
    """Records (script) calls and returns canned responses in order, or a fixed page."""

    def __init__(self, html="<html>ok</html>", url="https://example.com/"):
        self.calls: list[str] = []
        self.html = html
        self.url = url

    def __call__(self, script: str) -> str:
        self.calls.append(script)
        if "do JavaScript" in script and "outerHTML" in script:
            return self.html
        if "do JavaScript" in script and "readyState" in script:
            return "complete"
        if "do JavaScript" in script and "outerHTML.length" in script:
            return str(len(self.html))
        if "do JavaScript" in script:
            return "true"
        if "return URL of document" in script:
            return self.url
        return ""


def _session(monkeypatch, calls) -> sa.SafariAppleScriptSession:
    monkeypatch.setattr(sa, "_run_applescript", calls)
    monkeypatch.setattr(sa.human, "random_delay", lambda *a, **k: None)
    return sa.SafariAppleScriptSession(BrowserConfig(engine="safari", min_delay=0, max_delay=0))


def test_goto_returns_page_view_with_current_url_and_html(monkeypatch):
    calls = _FakeCalls(html="<html><body>hi</body></html>", url="https://www.ricardo.ch/de/s/x")
    session = _session(monkeypatch, calls)
    view = session.goto("https://www.ricardo.ch/de/s/x")
    assert view.url == "https://www.ricardo.ch/de/s/x"
    assert "hi" in view.html
    assert view.captures == []


def test_goto_raises_bot_wall_on_challenge_content(monkeypatch):
    calls = _FakeCalls(html="<html><title>Just a moment...</title></html>")
    session = _session(monkeypatch, calls)
    with pytest.raises(BotWallError):
        session.goto("https://www.ricardo.ch/de/s/x")


def test_open_detail_does_not_scroll(monkeypatch):
    calls = _FakeCalls()
    session = _session(monkeypatch, calls)
    session.open_detail("https://www.ricardo.ch/de/a/1/")
    assert not any("scrollTo" in c for c in calls.calls)


def test_run_js_missing_apple_events_permission_raises_browser_unavailable(monkeypatch):
    def boom(script):
        if "do JavaScript" in script:
            raise RuntimeError("You must enable 'Allow JavaScript from Apple Events' ...")
        return ""

    monkeypatch.setattr(sa, "_run_applescript", boom)
    with pytest.raises(BrowserUnavailable):
        sa._run_js("1+1")


def test_enter_exit_uses_session_lock(monkeypatch):
    calls = _FakeCalls()
    monkeypatch.setattr(sa, "_run_applescript", calls)
    cfg = BrowserConfig(engine="safari")
    session = sa.SafariAppleScriptSession(cfg)
    with session as s:
        assert s is session
        assert sa._SESSION_LOCK.locked()
    assert not sa._SESSION_LOCK.locked()
