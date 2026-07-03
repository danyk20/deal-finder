"""Bot-wall / CAPTCHA / forced-login detection over a PageView (pure, testable)."""

from __future__ import annotations

import re

from .errors import BotWallError
from .page import PageView

# Case-insensitive body/title markers that indicate a challenge or block page.
_BLOCK_MARKERS = (
    "access denied",
    "unusual traffic",
    "are you a robot",
    "verify you are human",
    "just a moment",  # Cloudflare interstitial
    "cf-chl",  # Cloudflare challenge
    "captcha-delivery.com",  # DataDome
    "px-captcha",  # PerimeterX
    "/_incapsula_",  # Imperva
    "reference #",  # Akamai reference error
    "_sec/cpt",  # Akamai Bot Manager interactive challenge iframe path
    "nur einen moment",  # Cloudflare interstitial, German locale (confirmed live on autoscout24.ch)
    "bitte bestätigen sie",
    "请完成安全验证",
)

# NOTE: do NOT add _abck / ak_bmsc / bm_sz as markers — those Akamai cookies/scripts
# appear on every legitimate page load and would produce permanent false-positive walls.

_CAPTCHA_RE = re.compile(
    r"<iframe[^>]+(recaptcha|hcaptcha|turnstile|captcha-delivery)", re.IGNORECASE
)
_LOGIN_REDIRECT_RE = re.compile(r"/(login|checkpoint|challenge|signin)\b", re.IGNORECASE)


def check_blocked(page: PageView, marketplace: str) -> None:
    """Raise BotWallError if the page looks like a wall. No-op otherwise."""
    if page.status in (403, 429, 503):
        raise BotWallError(f"{marketplace}: HTTP {page.status} (bot-wall / rate-limited)")

    low = (page.html or "").lower()
    for marker in _BLOCK_MARKERS:
        if marker in low:
            raise BotWallError(f"{marketplace}: challenge page detected ('{marker}')")

    if _CAPTCHA_RE.search(page.html or ""):
        raise BotWallError(f"{marketplace}: CAPTCHA challenge detected")

    if _LOGIN_REDIRECT_RE.search(page.url or ""):
        raise BotWallError(f"{marketplace}: redirected to a login/challenge page ({page.url})")
