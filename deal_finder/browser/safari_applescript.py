"""Drive your actual, real Safari.app via AppleScript -- no browser-automation protocol
involved at all.

Confirmed by direct testing to be dramatically more reliable against Ricardo's Cloudflare
challenge than any Playwright/Selenium-based approach (Chromium, Playwright's WebKit, and
even real Safari driven via safaridriver/WebDriver all got intermittently blocked; 5/5
AppleScript-driven search+detail cycles passed cleanly). The reason: the W3C WebDriver
spec REQUIRES every implementation -- Selenium, Playwright, anything -- to set
``navigator.webdriver = true`` while a session is attached. AppleScript's ``do JavaScript``
uses Safari's decades-old Apple Events scripting support, a completely different mechanism
that never attaches any automation protocol, so that tell never fires.

The window is positioned off every display (never shown to you) rather than minimized or
hidden: minimizing changes the Page Visibility API (document.hidden/visibilityState),
itself a plausible bot signal and untested territory, whereas an off-screen-but-not-
minimized window has the exact same document.hidden state a normal backgrounded tab
already has (confirmed empirically: document.hidden was already true just from Safari
not being the frontmost app, and 5/5 search+detail cycles against Ricardo still passed
with the window off-screen) -- so this doesn't introduce a new signal, it reuses one
that's already a completely ordinary state for a real user's browser.

Every session creates its OWN dedicated Safari window (tracked by window id) and only
ever operates on that id -- never on "window 1"/"document 1", which are ambiguous and
would silently act on whatever window the user actually has focused. This matters
because it's the exact thing that used to go wrong: an earlier version checked "if no
document exists, make one" and otherwise reused whatever document/window was already
open, meaning a user's own real Safari window got navigated away and moved off-screen.
See _create_dedicated_window()'s docstring for how a genuinely separate window is
guaranteed even when the user's "prefer tabs" setting would otherwise fold a new
document into an existing window. The dedicated window is also the only thing this
module ever closes (on session exit) -- never the whole Safari application, and never
any window it didn't create itself.

Trade-offs vs. the Playwright-based BrowserSession:
  * Requires the user to run `defaults write com.apple.Safari
    AllowJavaScriptFromAppleEvents -bool true` once (a real, if narrow, security-relevant
    setting -- it lets any AppleScript-capable process run JS in your open Safari pages).
    Not enabled by default; deal_finder never sets it for you.
  * Uses your REAL Safari application, not an isolated automation profile -- there is no
    separate "profile" to keep clean, and a scan briefly opens its own Safari window
    (off-screen, closed again when the scan ends). Your other Safari windows/tabs are
    never touched.
  * No network-response capture (PageView.captures is always empty) -- fine for Ricardo,
    which extracts everything from the rendered HTML, but a hard limitation vs Playwright.
  * macOS only, obviously.
"""

from __future__ import annotations

import logging
import subprocess
import time

from . import human
from .detect import check_blocked
from .errors import BrowserUnavailable
from .page import PageView
from .session import _SESSION_LOCK

log = logging.getLogger("deal_finder.browser.safari_applescript")

_JS_TIMEOUT_S = 30
_READY_TIMEOUT_S = 20.0
_READY_SETTLE_S = 1.5


def _escape_as(text: str) -> str:
    """Escape a string for embedding inside a double-quoted AppleScript literal."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _run_applescript(script: str) -> str:
    try:
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, timeout=_JS_TIMEOUT_S
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"osascript timed out: {exc}") from exc
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "osascript failed")
    return result.stdout.rstrip("\n")


# Off-screen, not minimized: minimizing (or literally hiding the app) changes the Page
# Visibility API (document.hidden/visibilityState), which is itself a plausible bot
# signal and untested territory. A window positioned off every display keeps the exact
# same document.hidden state a real backgrounded tab has (confirmed by direct testing:
# document.hidden was already true just from Safari not being the frontmost app, and
# 5/5 search+detail cycles against Ricardo still passed with the window off-screen) --
# so this doesn't introduce a new signal, it just uses one that's already normal.
_OFFSCREEN_BOUNDS = "-3000, -3000, -1560, -2100"


def _window_ids() -> set[int]:
    raw = _run_applescript('tell application "Safari" to return id of every window')
    return {int(x) for x in raw.split(", ") if x.strip()}


def _create_dedicated_window() -> int:
    """Create a brand-new Safari window for this session and return its window id --
    NEVER the user's own, pre-existing window. Everything else in this module operates
    on that specific id from then on (`window id N`), never on `window 1`/`document 1`,
    which are ambiguous once other windows exist and would silently operate on whatever
    the user's own frontmost window happens to be.

    Whether `make new document` opens a standalone window or a new tab inside whatever
    window is currently frontmost depends on the user's system-wide "prefer tabs"
    setting (Desktop & Dock settings) -- outside deal_finder's control. Verified by
    diffing window ids before/after; if no new window appeared, the new tab is moved out
    into one of its own via Safari's own `move` command, so this is correct either way.
    """
    before = _window_ids()
    _run_applescript('tell application "Safari" to make new document')
    new_ids = _window_ids() - before
    if new_ids:
        return next(iter(new_ids))

    before2 = _window_ids()
    _run_applescript(
        'tell application "Safari"\n'
        "    move (current tab of window 1) to (make new window)\n"
        "end tell"
    )
    new_ids2 = _window_ids() - before2
    if not new_ids2:
        raise BrowserUnavailable("could not create a dedicated Safari window")
    return next(iter(new_ids2))


def _set_offscreen(window_id: int) -> None:
    _run_applescript(f'tell application "Safari" to set bounds of window id {window_id} to {{{_OFFSCREEN_BOUNDS}}}')


def _navigate(window_id: int, url: str) -> None:
    escaped = _escape_as(url)
    _run_applescript(
        f'tell application "Safari" to set URL of current tab of window id {window_id} to "{escaped}"'
    )


def _run_js(window_id: int, js: str) -> str:
    escaped = _escape_as(js)
    try:
        return _run_applescript(
            f'tell application "Safari" to do JavaScript "{escaped}" in current tab of window id {window_id}'
        )
    except RuntimeError as exc:
        if "must enable" in str(exc).lower() or "AllowJavaScriptFromAppleEvents" in str(exc):
            raise BrowserUnavailable(
                "Safari engine needs: defaults write com.apple.Safari "
                "AllowJavaScriptFromAppleEvents -bool true (then relaunch Safari)"
            ) from exc
        raise


def _current_url(window_id: int) -> str:
    return _run_applescript(f'tell application "Safari" to return URL of current tab of window id {window_id}')


def _close_window(window_id: int) -> None:
    # Plain `close` on a window whose loaded page has a beforeunload handler (common on
    # marketplace sites, confirmed live on Ricardo's detail pages) triggers a native
    # "Leave Page?" confirmation dialog -- `close` itself returns success immediately
    # regardless, but the window silently stays open because nothing dismisses that
    # dialog. `saving no` bypasses it. Confirmed by direct testing: plain `close`
    # reported success but left a real Ricardo-detail-page window open; `saving no`
    # closed it cleanly.
    _run_applescript(f'tell application "Safari" to close window id {window_id} saving no')


def _wait_for_ready(window_id: int, timeout: float = _READY_TIMEOUT_S, settle: float = _READY_SETTLE_S) -> None:
    """Best-effort replacement for Playwright's networkidle wait: poll document.readyState,
    then wait for the rendered HTML's length to stop changing (SPA hydration settling)."""
    deadline = time.monotonic() + timeout
    last_len = -1
    stable_since: float | None = None
    while time.monotonic() < deadline:
        try:
            ready = _run_js(window_id, "document.readyState")
            length = int(_run_js(window_id, "document.documentElement.outerHTML.length"))
        except Exception:  # noqa: BLE001 - transient during navigation; keep polling
            time.sleep(0.4)
            continue
        if ready == "complete":
            if length == last_len and length > 0:
                if stable_since is None:
                    stable_since = time.monotonic()
                elif time.monotonic() - stable_since >= settle:
                    return
            else:
                stable_since = None
            last_len = length
        time.sleep(0.4)


def is_available() -> bool:
    """True if Safari is installed and scriptable. Doesn't check
    AllowJavaScriptFromAppleEvents (that's checked lazily on first real use, with a clear
    error message pointing at the fix)."""
    try:
        _run_applescript('tell application "System Events" to return exists application process "Safari"')
        return True
    except Exception:  # noqa: BLE001
        try:
            subprocess.run(["osascript", "-e", "1"], capture_output=True, timeout=5)
            return True
        except Exception:  # noqa: BLE001
            return False


class SafariAppleScriptSession:
    """Real Safari, driven via AppleScript. Implements the same SessionLike surface as
    BrowserSession (goto/open_detail/human_pause/type_search) so adapters can't tell the
    difference -- see BrowserSession.__new__ for how BrowserConfig(engine="safari")
    dispatches here."""

    def __init__(self, config) -> None:
        self.config = config
        self._locked = False
        self._window_id: int | None = None
        self.backend = "safari"
        self.channel_used = "safari (AppleScript, no automation protocol)"

    def __enter__(self) -> "SafariAppleScriptSession":
        _SESSION_LOCK.acquire()
        self._locked = True
        try:
            # Always a brand-new, dedicated window (never the user's own, pre-existing
            # one -- see _create_dedicated_window's docstring for how that's guaranteed).
            self._window_id = _create_dedicated_window()
            _set_offscreen(self._window_id)
        except Exception as exc:  # noqa: BLE001
            self._window_id = None
            self._release()
            raise BrowserUnavailable(f"could not control Safari via AppleScript: {exc}") from exc
        return self

    def __exit__(self, *exc) -> None:
        # Safe to close now: this is a window we created ourselves and tracked by id,
        # never the user's own -- unlike operating on "window 1"/"document 1", which
        # would be ambiguous and risk touching whatever window the user actually has
        # open (that's what a real, since-fixed bug used to do).
        if self._window_id is not None:
            try:
                _close_window(self._window_id)
            except Exception:  # noqa: BLE001
                pass
        self._release()

    def _release(self) -> None:
        if self._locked:
            self._locked = False
            _SESSION_LOCK.release()

    def goto(self, url: str, *, scroll: bool = True) -> PageView:
        wid = self._window_id
        _navigate(wid, url)
        _wait_for_ready(wid)
        if scroll:
            try:
                _run_js(wid, "window.scrollTo(0, Math.floor(document.body.scrollHeight/3))")
            except Exception:  # noqa: BLE001 - cosmetic
                pass
        html = _run_js(wid, "document.documentElement.outerHTML")
        current_url = _current_url(wid)
        # No network-response capture is possible via AppleScript (see module docstring);
        # no adapter currently relies on it. Real HTTP status also isn't observable this
        # way, so detection relies entirely on check_blocked's HTML-content markers,
        # which is what actually caught Ricardo's challenge page in practice.
        view = PageView(url=current_url, html=html, status=None, captures=[])
        check_blocked(view, url)
        return view

    def open_detail(self, url: str) -> PageView:
        return self.goto(url, scroll=False)

    def human_pause(self) -> None:
        human.random_delay(self.config.min_delay, self.config.max_delay)

    def type_search(self, landing_url: str, input_selectors: tuple[str, ...], text: str) -> PageView:
        """Not exercised by any current adapter (Ricardo uses direct search URLs), but
        implemented for SessionLike-completeness. Uses synthetic JS events since
        AppleScript can't send native keystrokes without System Events automation."""
        wid = self._window_id
        self.goto(landing_url, scroll=False)
        escaped_text = _escape_as(text)
        for sel in input_selectors:
            escaped_sel = _escape_as(sel)
            js = (
                "(function(){"
                f'var el=document.querySelector("{escaped_sel}");'
                "if(!el)return false;"
                "el.focus();"
                f'el.value="{escaped_text}";'
                "el.dispatchEvent(new Event('input',{bubbles:true}));"
                "var f=el.form;"
                "if(f){f.requestSubmit?f.requestSubmit():f.submit();}"
                "return true;"
                "})()"
            )
            try:
                if _run_js(wid, js) == "true":
                    break
            except Exception:  # noqa: BLE001
                continue
        _wait_for_ready(wid)
        html = _run_js(wid, "document.documentElement.outerHTML")
        current_url = _current_url(wid)
        view = PageView(url=current_url, html=html, status=None, captures=[])
        check_blocked(view, current_url)
        return view
