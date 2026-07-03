"""Browser-layer errors. Both subclass AdapterError so the pipeline's per-adapter
try/except already isolates them — a blocked/failed browser adapter never aborts a run."""

from __future__ import annotations

from ..adapters.base import AdapterError


class BrowserUnavailable(AdapterError):
    """Playwright isn't installed, or no browser could be launched."""


class BotWallError(AdapterError):
    """A bot-wall / CAPTCHA / forced-login page was detected."""


class LoginRequiredError(AdapterError):
    """A site needs an authenticated session that isn't present (e.g. Facebook)."""
