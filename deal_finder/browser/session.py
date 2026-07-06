"""BrowserSession — a real, headful Chrome driven via Playwright's SYNC API.

Sync API is correct here: run_watch executes in a worker thread with no asyncio loop
(scheduler uses asyncio.to_thread; the API uses run_in_threadpool). A persistent context
(on-disk profile) is the key to bot-bypass: cookies/localStorage — including Akamai's
_abck and a manual Facebook login — persist across runs.

A module-level lock serializes sessions process-wide, because two watches can run in
parallel worker threads and would otherwise collide on the same Chrome profile dir.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from ..adapters.base import AdapterError
from ..config import Settings
from . import human
from .detect import check_blocked
from .errors import BrowserUnavailable
from .page import PageView
from .stealth import DEFAULT_USER_AGENT, STEALTH_JS

log = logging.getLogger("deal_finder.browser")

_SESSION_LOCK = threading.Lock()


@runtime_checkable
class SessionLike(Protocol):
    """The narrow surface adapters use — implemented by BrowserSession and (in tests)
    FakeBrowserSession, so adapter/pipeline logic is testable without a real browser."""

    def goto(self, url: str, *, scroll: bool = True) -> PageView: ...
    def open_detail(self, url: str) -> PageView: ...
    def human_pause(self) -> None: ...
    def type_search(self, landing_url: str, input_selectors: tuple[str, ...], text: str) -> PageView: ...


@dataclass
class BrowserConfig:
    headless: bool = False
    channel: str | None = "chrome"
    user_data_dir: Path = Path("~/.deal_finder/profiles/default").expanduser()
    locale: str = "de-CH"
    timezone_id: str = "Europe/Zurich"
    user_agent: str = DEFAULT_USER_AGENT
    min_delay: float = 2.5
    max_delay: float = 6.0
    nav_timeout_ms: int = 45_000
    proxy: dict | None = None
    use_patchright: bool = True

    @classmethod
    def from_settings(cls, settings: Settings, *, profile: str = "default") -> "BrowserConfig":
        base = Path(settings.browser_profile_dir).expanduser()
        proxy = {"server": settings.browser_proxy_url} if settings.browser_proxy_url else None
        return cls(
            headless=settings.browser_headless,
            channel=(settings.browser_channel or None),
            user_data_dir=base / profile,
            user_agent=(settings.browser_user_agent or DEFAULT_USER_AGENT),
            min_delay=settings.browser_min_delay,
            max_delay=settings.browser_max_delay,
            nav_timeout_ms=int(settings.browser_nav_timeout * 1000),
            proxy=proxy,
            use_patchright=settings.browser_use_patchright,
        )


def _sync_playwright_factory(use_patchright: bool):
    """Return (sync_playwright, backend_name). Prefer patchright (patched Playwright that
    hides automation/CDP signals so Cloudflare/Turnstile usually doesn't challenge);
    fall back to stock Playwright."""
    if use_patchright:
        try:
            from patchright.sync_api import sync_playwright  # type: ignore

            return sync_playwright, "patchright"
        except Exception:  # noqa: BLE001 - not installed / import issue
            pass
    from playwright.sync_api import sync_playwright

    return sync_playwright, "playwright"


def is_available() -> bool:
    """True if a browser backend (patchright or Playwright) is importable (cheap; no launch)."""
    for mod in ("patchright.sync_api", "playwright.sync_api"):
        try:
            __import__(mod)
            return True
        except Exception:  # noqa: BLE001
            continue
    return False


class BrowserSession:
    def __init__(self, config: BrowserConfig):
        self.config = config
        self._pw = None
        self._ctx = None
        self._page = None
        self._captures: list[tuple[str, object]] = []
        self._locked = False
        self.backend: str | None = None       # "patchright" | "playwright", set once launched
        self.channel_used: str | None = None  # e.g. "chrome" or "bundled chromium"

    # -- lifecycle --
    def __enter__(self) -> "BrowserSession":
        _SESSION_LOCK.acquire()
        self._locked = True
        try:
            self._start()
        except Exception:
            self._release()
            raise
        return self

    def __exit__(self, *exc) -> None:
        try:
            if self._ctx is not None:
                self._ctx.close()
            if self._pw is not None:
                self._pw.stop()
        except Exception:  # noqa: BLE001
            log.warning("error closing browser session", exc_info=True)
        finally:
            self._ctx = self._pw = self._page = None
            self._release()

    def _release(self) -> None:
        if self._locked:
            self._locked = False
            _SESSION_LOCK.release()

    def _start(self) -> None:
        try:
            sync_playwright, backend = _sync_playwright_factory(self.config.use_patchright)
        except ImportError as exc:
            raise BrowserUnavailable(
                "no browser backend; run: pipenv install && pipenv run browsers"
            ) from exc

        self.backend = backend
        log.info("browser backend: %s", backend)
        self.config.user_data_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self._ctx = self._launch(self.config.channel)
        self.channel_used = self.config.channel if self._ctx is not None else None
        if self._ctx is None:
            self._ctx = self._launch(None)
            self.channel_used = "bundled chromium" if self._ctx is not None else None
        if self._ctx is None:
            self._pw.stop()
            self._pw = None
            raise BrowserUnavailable("could not launch Chrome or bundled Chromium")

        self._ctx.add_init_script(STEALTH_JS)
        self._ctx.set_default_navigation_timeout(self.config.nav_timeout_ms)
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
        self._page.on("response", self._on_response)

    def _launch(self, channel):
        kwargs = dict(
            user_data_dir=str(self.config.user_data_dir),
            headless=self.config.headless,
            locale=self.config.locale,
            timezone_id=self.config.timezone_id,
            user_agent=self.config.user_agent,
            viewport={"width": 1440, "height": 900},
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--enable-automation"],
            # Playwright/patchright default chromium_sandbox to False, which always adds
            # --no-sandbox -- itself a strong, highly visible automation fingerprint (and
            # the reason a real Chrome launch still showed that warning banner). A real
            # user's Chrome always runs sandboxed, so this must be explicitly re-enabled.
            chromium_sandbox=True,
        )
        if channel:
            kwargs["channel"] = channel
        if self.config.proxy:
            kwargs["proxy"] = self.config.proxy
        try:
            return self._pw.chromium.launch_persistent_context(**kwargs)
        except Exception as exc:  # noqa: BLE001
            log.warning("browser launch failed (channel=%s): %s", channel, exc)
            return None

    # -- response capture --
    def _on_response(self, response) -> None:
        try:
            ct = (response.headers or {}).get("content-type", "")
            if "application/json" in ct:
                self._captures.append((response.url, response.json()))
        except Exception:  # noqa: BLE001 - body may be unavailable/streamed; ignore
            pass

    # -- navigation --
    @property
    def playwright_page(self):
        """The underlying Playwright Page (for advanced flows like Facebook login)."""
        return self._page

    def _safe_content(self) -> str:
        """page.content() but tolerant of App Router pages still streaming/navigating."""
        for _ in range(5):
            try:
                return self._page.content()
            except Exception:  # noqa: BLE001 - "page is navigating and changing the content"
                try:
                    self._page.wait_for_timeout(700)
                except Exception:  # noqa: BLE001
                    break
        try:
            return self._page.content()
        except Exception:  # noqa: BLE001 - last resort: rendered DOM without a clean snapshot
            return self._page.evaluate("() => document.documentElement.outerHTML") or ""

    def goto(self, url: str, *, scroll: bool = True) -> PageView:
        self._captures = []
        resp = self._page.goto(url, wait_until="domcontentloaded")
        try:
            self._page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:  # noqa: BLE001 - networkidle is best-effort
            pass
        if scroll:
            human.human_scroll(self._page)
        view = PageView(
            url=self._page.url,
            html=self._safe_content(),
            status=(resp.status if resp else None),
            captures=list(self._captures),
        )
        check_blocked(view, url)
        return view

    def open_detail(self, url: str) -> PageView:
        return self.goto(url, scroll=False)

    def type_search(self, landing_url: str, input_selectors: tuple[str, ...], text: str) -> PageView:
        """Land on ``landing_url`` like a person, find the search box, type the query
        LETTER BY LETTER with human delays, press Enter, and return the results page.
        More human than hitting a query URL — helps get past Cloudflare/Turnstile."""
        self._captures = []
        self._page.goto(landing_url, wait_until="domcontentloaded")
        try:
            self._page.wait_for_load_state("networkidle", timeout=8_000)
        except Exception:  # noqa: BLE001
            pass
        human.dismiss_cookie_banner(self._page)

        box = None
        for sel in input_selectors:
            try:
                loc = self._page.locator(sel).first
                if loc.count() and loc.is_visible():
                    box = loc
                    break
            except Exception:  # noqa: BLE001
                continue
        if box is None:
            raise AdapterError(f"search input not found on {landing_url} (selectors: {input_selectors})")

        human.human_type(self._page, box, text)  # types char-by-char with per-key delays
        human.random_delay(0.4, 1.1)
        self._page.keyboard.press("Enter")
        try:
            self._page.wait_for_load_state("networkidle", timeout=12_000)
        except Exception:  # noqa: BLE001
            pass
        human.human_scroll(self._page)
        view = PageView(url=self._page.url, html=self._safe_content(), status=200,
                        captures=list(self._captures))
        check_blocked(view, self._page.url)
        return view

    def human_pause(self) -> None:
        human.random_delay(self.config.min_delay, self.config.max_delay)
