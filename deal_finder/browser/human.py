"""Human-like pacing helpers. All synchronous (time.sleep) — the pipeline runs off the
event loop in a worker thread. Sleeps route through ``_sleep`` so tests can patch it."""

from __future__ import annotations

import random
import time

# Patchable seam: tests monkeypatch deal_finder.browser.human._sleep to a no-op.
_sleep = time.sleep


def random_delay(min_s: float, max_s: float) -> None:
    lo, hi = sorted((max(0.0, min_s), max(0.0, max_s)))
    _sleep(random.uniform(lo, hi))


def short_pause() -> None:
    random_delay(0.3, 1.1)


def dwell() -> None:
    """A 'reading the page' pause."""
    random_delay(1.0, 3.0)


def human_scroll(page, *, steps: int | None = None) -> None:
    """Wheel-scroll down in a few small randomized increments to trigger lazy loading
    and look human. ``page`` is a Playwright Page; failures are swallowed (best-effort)."""
    n = steps if steps is not None else random.randint(3, 6)
    try:
        for _ in range(n):
            page.mouse.wheel(0, random.randint(500, 1100))
            random_delay(0.4, 1.2)
    except Exception:  # noqa: BLE001 - scrolling is cosmetic; never fail a scan on it
        pass


def human_click(page, locator) -> None:
    """Move toward the element in a couple of steps, small dwell, then click."""
    try:
        box = locator.bounding_box()
        if box:
            page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=random.randint(4, 9))
            short_pause()
        locator.click(timeout=10_000)
    except Exception:
        locator.click(timeout=10_000)


def human_type(page, locator, text: str) -> None:
    """Type char-by-char with per-keystroke delays."""
    locator.click()
    short_pause()
    for ch in text:
        locator.type(ch, delay=random.uniform(60, 180))


# Consent-banner buttons, privacy-preserving options FIRST (decline non-essential),
# then accept as a fallback so the banner is at least dismissed. de/fr/it/en.
_CONSENT_SELECTORS = (
    "#onetrust-reject-all-handler",
    'button:has-text("Nur notwendige")',
    'button:has-text("Ablehnen")',
    'button:has-text("Alle ablehnen")',
    'button:has-text("Refuser")',
    'button:has-text("Reject all")',
    'button:has-text("Necessary only")',
    "#onetrust-accept-btn-handler",
    'button:has-text("Akzeptieren")',
    'button:has-text("Alle akzeptieren")',
    'button:has-text("Accept all")',
    'button[aria-label*="akzeptieren" i]',
)


def dismiss_cookie_banner(page) -> bool:
    """Best-effort click a cookie-consent button, preferring 'reject/necessary only'.
    Returns True if something was clicked. Never raises."""
    for sel in _CONSENT_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible():
                loc.click(timeout=2500)
                random_delay(0.5, 1.2)
                return True
        except Exception:  # noqa: BLE001 - banners vary; keep trying / move on
            continue
    return False
