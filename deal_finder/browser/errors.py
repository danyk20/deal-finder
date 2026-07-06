"""Browser-layer errors. Both subclass AdapterError so the pipeline's per-adapter
try/except already isolates them — a blocked/failed browser adapter never aborts a run."""

from __future__ import annotations

from ..adapters.base import AdapterError


class BrowserUnavailable(AdapterError):
    """Playwright isn't installed, or no browser could be launched."""


class BotWallError(AdapterError):
    """A bot-wall / CAPTCHA / forced-login page was detected.

    ``partial_listings`` lets a caller that hit this wall partway through a run (e.g.
    after successfully fetching some listings' details, then getting blocked on a later
    one) attach whatever it already collected, so the pipeline can keep those results
    instead of discarding them just because the run didn't finish cleanly.
    """

    def __init__(self, message: str, *, partial_listings: list | None = None):
        super().__init__(message)
        self.partial_listings = partial_listings or []


class LoginRequiredError(AdapterError):
    """A site needs an authenticated session that isn't present (e.g. Facebook)."""
