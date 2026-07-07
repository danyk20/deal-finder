"""Facebook Marketplace adapter — uses the `facebook-marketplace-scraper` PyPI package
(https://pypi.org/project/facebook-marketplace-scraper/), which drives its own dedicated
Playwright/Chromium browser against facebook.com directly (Facebook has no public JSON
API and blocks plain HTTP requests at the TLS/fingerprint level - confirmed by the
package's own investigation). It manages its own persistent browser profile and login
flow internally, so this adapter does NOT use deal_finder's shared `browser/` session
infra the way tutti/Ricardo do - it's a plain (non-browser-requiring, from the pipeline's
point of view) adapter, exactly like the AutoScout24 one.

⚠️  Automating Facebook violates its Terms of Service and can get an account temporarily
locked or permanently BANNED. Enabled by default per the user's earlier explicit choice,
but use a dedicated/secondary account.

Login is effectively required (anonymous Marketplace search now hard-redirects to
/login - confirmed live). Two ways, same as before:
  * Preferred: a ONE-TIME MANUAL login via `python -m deal_finder.browser.fb_login`
    (now a thin wrapper around this package's own FacebookSession) - no password stored.
  * Fallback: Facebook email/password in Settings, used to auto-fill the login form.
    Less safe (may trigger 2FA/checkpoints); the package raises a clear error rather
    than hanging if that happens.

Session storage caveat: the package keeps its persistent Chrome profile (and therefore
the logged-in session) inside its own installed package directory
(`<venv>/lib/.../site-packages/browser_profile/`), not inside deal_finder's own
`~/.deal_finder/profiles/`. That means a `pipenv sync` / dependency reinstall wipes the
login and `fb_login` needs to be run again - this is a limitation of the package as
published, not something this adapter works around.

Optional dependency: install with `pipenv install --categories facebook` (or
`pip install facebook-marketplace-scraper`); imported lazily so the rest of the app
works without it, degrading to a clear AdapterError if it's missing.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

from ..config import Settings, get_settings
from .base import AdapterError, BaseAdapter, Listing, MarketplaceQuery

log = logging.getLogger("deal_finder.adapters.facebook")


def _images(item: dict) -> list[str]:
    images = item.get("images")
    if images:
        return list(images)
    thumb = item.get("image_url")
    return [thumb] if thumb else []


def listing_from_api_item(item: dict) -> Listing | None:
    """Map one facebook-marketplace-scraper item (search-tile or merged-detail shape)
    to a Listing. Pure + fixture-testable without any network access."""
    from ..browser import extract as ex  # local: only needed for the parse_* helpers

    ext_id = item.get("listing_id")
    if not ext_id:
        return None

    # Many real listings have no free-text title at all (confirmed by the package's own
    # docs) - fall back to a generic label rather than dropping a genuine match.
    title = (item.get("title") or "").strip() or "Facebook Marketplace listing"
    description = (item.get("description") or "").strip()

    price, currency = ex.parse_price(item.get("price"))

    # Facebook has no structured year/mileage fields (unlike AutoScout24's JSON) - best
    # effort regex extraction from the free text, same technique used for Ricardo.
    attrs: dict[str, int] = {}
    year = ex.parse_year(title, description)
    if year is not None:
        attrs["year"] = year
    mileage = ex.parse_int_km(title, description)
    if mileage is not None:
        attrs["mileage_km"] = mileage

    return Listing(
        marketplace="facebook",
        external_id=str(ext_id),
        url=item.get("url") or f"https://www.facebook.com/marketplace/item/{ext_id}/",
        title=title,
        description=description,
        language=None,
        price=price,
        currency=currency,
        location=item.get("location"),
        posted_at=None,  # only a relative string ("vor 3 Tagen") is available; not parsed
        attributes=attrs,
        image_urls=_images(item),
    )


class FacebookAdapter(BaseAdapter):
    key = "facebook"
    label = "Facebook Marketplace"
    supported_categories = {"car"}
    enabled_by_default = True  # user's explicit choice; ToS/ban risk — see module docstring
    status_note = "⚠ automated FB use risks account bans; needs a one-time login"

    def search(self, query: MarketplaceQuery, settings: Settings | None = None) -> Iterable[Listing]:
        text = (query.text or " ".join(query.terms)).strip()
        if not text:
            raise AdapterError("Facebook Marketplace: no search text (make/model) set on the watch")

        try:
            from fb_scraper.browser import FacebookSession, LoginFailedError
            from fb_scraper.scraper import (
                LoginRequiredError,
                MarketplaceConsentRequiredError,
                search_listings,
                visit_all_listings,
            )
        except ImportError as exc:
            raise AdapterError(
                "Facebook Marketplace: 'facebook-marketplace-scraper' isn't installed; "
                "run: pipenv install --categories facebook (or pip install facebook-marketplace-scraper)"
            ) from exc

        # ``settings`` is the pipeline's already-resolved effective settings (env + the
        # web UI's DB-stored overrides — see adapters/base.py's search() docstring).
        # Falling back to get_settings() (env/.env only) keeps direct callers (tests,
        # health_check() below) working without needing to pass one in, but it will
        # never see credentials saved only via the Settings page.
        settings = settings or get_settings()
        p = query.params or {}

        try:
            with FacebookSession(
                headless=True,
                email=settings.facebook_email or None,
                password=settings.facebook_password or None,
            ) as context:
                page = context.new_page()
                try:
                    candidates = search_listings(
                        page,
                        text,
                        country="ch",
                        min_price=int(query.price_min) if query.price_min is not None else None,
                        max_price=int(query.price_max) if query.price_max is not None else None,
                        max_mileage=int(p["mileage_max"]) if p.get("mileage_max") else None,
                        min_year=int(p["year_min"]) if p.get("year_min") else None,
                        max_year=int(p["year_max"]) if p.get("year_max") else None,
                        verbose=False,
                    )
                    # Facebook's radius search can spill just over the border.
                    candidates = [c for c in candidates if c.get("is_local", True)]
                    # Cap the (slower, one-request-per-listing) detail phase.
                    capped = candidates[: settings.browser_max_items_per_run]
                    detailed = visit_all_listings(page, capped, verbose=False)
                finally:
                    page.close()
        except (LoginRequiredError, MarketplaceConsentRequiredError, LoginFailedError) as exc:
            raise AdapterError(
                f"Facebook Marketplace: {exc} Run `python -m deal_finder.browser.fb_login` to log in once."
            ) from exc
        except Exception as exc:  # noqa: BLE001 - Playwright/network errors etc.
            raise AdapterError(f"Facebook Marketplace request failed: {exc}") from exc

        return [li for item in detailed if (li := listing_from_api_item(item)) is not None]

    def health_check(self) -> bool:
        try:
            list(self.search(MarketplaceQuery(category="car", terms=["Tesla"])))
            return True
        except AdapterError:
            return False
