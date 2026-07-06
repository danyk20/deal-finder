"""Tests SafariAppleScriptSession and the engine dispatch, with subprocess/osascript
fully mocked out -- no real macOS Safari involved, so this runs in CI too.

Extra emphasis on window isolation: an earlier version of this module reused whatever
Safari document/window was already open instead of creating its own, which meant a
user's own real Safari window got navigated away and moved off-screen. These tests
guard against that regressing."""

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


class _FakeSafari:
    """Simulates just enough of Safari's AppleScript surface to test window isolation:
    pre-existing windows (the user's own, by id), window creation (either a genuinely
    new window, mimicking normal behavior, or -- when `folds_into_existing` is set --
    folding into the current front window the way "prefer tabs: Always" would, which
    the module must detect and correct for), navigation, JS, and closing."""

    def __init__(self, existing_window_ids=(111, 222), folds_into_existing=False,
                 html="<html>ok</html>", url="https://example.com/"):
        self.windows = set(existing_window_ids)
        self._next_id = max(self.windows, default=0) + 1000
        self.folds_into_existing = folds_into_existing
        self.html = html
        self.url = url
        self.calls: list[str] = []
        self.closed: list[int] = []
        self.navigated_windows: list[int] = []

    def __call__(self, script: str) -> str:
        self.calls.append(script)
        if script == 'tell application "Safari" to return id of every window':
            return ", ".join(str(w) for w in sorted(self.windows))
        if "make new document" in script:
            if not self.folds_into_existing:
                new_id = self._next_id
                self._next_id += 1
                self.windows.add(new_id)
            return ""
        if "move (current tab of window 1) to (make new window)" in script:
            new_id = self._next_id
            self._next_id += 1
            self.windows.add(new_id)
            return ""
        if script.startswith('tell application "Safari" to close window id '):
            wid = int(script.split("close window id ")[1].split(" ")[0])
            if wid not in self.windows:
                raise RuntimeError(f"can't get window id {wid}")
            self.windows.discard(wid)
            self.closed.append(wid)
            return ""
        if "set bounds of window id" in script:
            return ""
        if "set URL of current tab of window id" in script:
            wid = int(script.split("window id ")[1].split(" ")[0])
            self.navigated_windows.append(wid)
            return ""
        if "do JavaScript" in script and "outerHTML.length" in script:
            return str(len(self.html))
        if "do JavaScript" in script and "readyState" in script:
            return "complete"
        if "do JavaScript" in script and "outerHTML" in script:
            return self.html
        if "do JavaScript" in script:
            return "true"
        if "return URL of current tab of window id" in script:
            return self.url
        return ""


def _session(monkeypatch, fake) -> sa.SafariAppleScriptSession:
    monkeypatch.setattr(sa, "_run_applescript", fake)
    monkeypatch.setattr(sa.human, "random_delay", lambda *a, **k: None)
    return sa.SafariAppleScriptSession(BrowserConfig(engine="safari", min_delay=0, max_delay=0))


def test_enter_creates_a_new_window_not_an_existing_one(monkeypatch):
    fake = _FakeSafari(existing_window_ids=(111, 222))
    session = _session(monkeypatch, fake)
    with session:
        assert session._window_id not in (111, 222)
        assert session._window_id in fake.windows


def test_enter_moves_folded_tab_into_its_own_window_when_prefer_tabs_is_on(monkeypatch):
    """When the user's "prefer tabs" setting folds a new document into the current
    window instead of opening a standalone one, the module must detect that (no new
    window id appeared) and explicitly move the tab out into a fresh window."""
    fake = _FakeSafari(existing_window_ids=(111,), folds_into_existing=True)
    session = _session(monkeypatch, fake)
    with session:
        assert session._window_id not in (111,)
        assert any("move (current tab of window 1) to (make new window)" in c for c in fake.calls)


def test_exit_closes_only_its_own_window(monkeypatch):
    fake = _FakeSafari(existing_window_ids=(111, 222))
    session = _session(monkeypatch, fake)
    with session as s:
        my_id = s._window_id
    assert fake.closed == [my_id]
    assert fake.windows == {111, 222}  # the user's own windows are untouched


def test_navigation_targets_only_the_dedicated_window(monkeypatch):
    fake = _FakeSafari(existing_window_ids=(111, 222))
    session = _session(monkeypatch, fake)
    with session as s:
        s.goto("https://www.ricardo.ch/de/s/x")
        assert fake.navigated_windows == [s._window_id]
        assert 111 not in fake.navigated_windows
        assert 222 not in fake.navigated_windows


def test_goto_returns_page_view_with_current_url_and_html(monkeypatch):
    fake = _FakeSafari(html="<html><body>hi</body></html>", url="https://www.ricardo.ch/de/s/x")
    session = _session(monkeypatch, fake)
    with session as s:
        view = s.goto("https://www.ricardo.ch/de/s/x")
    assert view.url == "https://www.ricardo.ch/de/s/x"
    assert "hi" in view.html
    assert view.captures == []


def test_goto_raises_bot_wall_on_challenge_content(monkeypatch):
    fake = _FakeSafari(html="<html><title>Just a moment...</title></html>")
    session = _session(monkeypatch, fake)
    with session as s:
        with pytest.raises(BotWallError):
            s.goto("https://www.ricardo.ch/de/s/x")


def test_open_detail_does_not_scroll(monkeypatch):
    fake = _FakeSafari()
    session = _session(monkeypatch, fake)
    with session as s:
        s.open_detail("https://www.ricardo.ch/de/a/1/")
    assert not any("scrollTo" in c for c in fake.calls)


def test_run_js_missing_apple_events_permission_raises_browser_unavailable(monkeypatch):
    def boom(script):
        if "do JavaScript" in script:
            raise RuntimeError("You must enable 'Allow JavaScript from Apple Events' ...")
        return ""

    monkeypatch.setattr(sa, "_run_applescript", boom)
    with pytest.raises(BrowserUnavailable):
        sa._run_js(999, "1+1")


def test_enter_exit_uses_session_lock(monkeypatch):
    fake = _FakeSafari()
    session = _session(monkeypatch, fake)
    with session as s:
        assert s is session
        assert sa._SESSION_LOCK.locked()
    assert not sa._SESSION_LOCK.locked()
