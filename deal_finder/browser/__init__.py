"""Shared browser-automation layer (Playwright sync API, headful persistent Chrome).

Marketplace-agnostic: nothing site-specific lives here. Playwright is an optional
dependency ([browser] extra); everything imports lazily so the core app and offline
tests run without it.
"""

from __future__ import annotations

from .adapter import BrowserAdapter, CardRef
from .errors import BotWallError, BrowserUnavailable, LoginRequiredError
from .page import PageView
from .session import BrowserConfig, BrowserSession, SessionLike, is_available

__all__ = [
    "BrowserAdapter",
    "CardRef",
    "BrowserConfig",
    "BrowserSession",
    "SessionLike",
    "PageView",
    "is_available",
    "BotWallError",
    "BrowserUnavailable",
    "LoginRequiredError",
]
