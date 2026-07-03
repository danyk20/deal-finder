"""Dry-run notification: open matched listings in the local browser instead of emailing.

Uses the stdlib ``webbrowser`` module, which opens tabs in the default browser on the
machine running the app (this is a personal, locally-run tool, so that's the user's own
machine). No dependency on the internal Playwright/patchright automation layer at all.
"""

from __future__ import annotations

import logging
import time
import webbrowser

log = logging.getLogger("deal_finder.notify.browser_open")


def open_listings(urls: list[str], *, delay: float = 0.4) -> int:
    """Open each URL in a new browser tab, one at a time, with a short pause between
    opens so the OS/browser can keep up. Best-effort: one bad URL doesn't stop the rest.
    Returns the number of tabs successfully requested to open."""
    opened = 0
    for i, url in enumerate(urls):
        try:
            webbrowser.open_new_tab(url)
            opened += 1
        except Exception as exc:  # noqa: BLE001 - never fail a dry run over one bad tab
            log.warning("could not open a browser tab for %s: %s", url, exc)
        if i < len(urls) - 1:
            time.sleep(delay)
    return opened
