"""BrowserAdapter — template-method base for browser-driven marketplace adapters.

Encodes the shared human flow ONCE: build search URL(s) -> open search page ->
collect card links -> open each listing ONE AT A TIME with a random delay between ->
map to Listing. Subclasses supply only the site-specific pieces (URL builder, card
extraction, detail extraction, id parsing). Extraction hooks are pure functions over a
PageView, so they're unit-testable from fixtures without a browser.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field

from ..adapters.base import BaseAdapter, Listing, MarketplaceQuery
from ..config import Settings, get_settings
from .errors import BotWallError, BrowserUnavailable
from .page import PageView
from .session import SessionLike

log = logging.getLogger("deal_finder.browser.adapter")


@dataclass
class CardRef:
    """A search-result card: its detail URL plus any fields cheaply available up-front."""

    url: str
    external_id: str = ""
    partial: dict = field(default_factory=dict)


class BrowserAdapter(BaseAdapter):
    requires_browser = True
    profile_name = "default"      # persistent-profile subdir; sites needing login isolate theirs
    detail_needed = True          # open each listing's detail page (vs. cards being enough)
    site_login_required = False   # Facebook overrides

    def needs_browser(self, settings: Settings) -> bool:
        """Whether this adapter needs a real browser for THIS run. A subclass that can
        fetch another way in some configurations can override this so the pipeline skips
        launching Chrome when it isn't needed."""
        return self.requires_browser

    # --- subclass hooks ---
    def build_search_urls(self, query: MarketplaceQuery, settings: Settings) -> list[str]:
        raise NotImplementedError

    def extract_cards(self, view: PageView, query: MarketplaceQuery) -> list[CardRef]:
        raise NotImplementedError

    def extract_detail(self, view: PageView, card: CardRef, query: MarketplaceQuery) -> Listing | None:
        raise NotImplementedError

    def external_id_from_url(self, url: str) -> str | None:
        return None

    def before_search(self, session: SessionLike, settings: Settings) -> None:
        """Hook for pre-flight checks (e.g. Facebook login). May raise AdapterError."""

    def iter_search_views(self, browser: SessionLike, query: MarketplaceQuery,
                          settings: Settings) -> Iterable[PageView]:
        """Yield the search-results page(s). Default: navigate each build_search_urls URL.
        Adapters can override to type into the site's search box instead (more human)."""
        for url in self.build_search_urls(query, settings):
            yield browser.goto(url)

    # --- shared driver ---
    def search(self, query: MarketplaceQuery, *, browser: SessionLike | None = None,
               settings: Settings | None = None) -> Iterable[Listing]:
        if browser is None:
            raise BrowserUnavailable(f"{self.label}: no browser session available")
        settings = settings or get_settings()
        cap = settings.browser_max_items_per_run

        self.before_search(browser, settings)

        listings: list[Listing] = []
        seen: set[str] = set()
        for view in self.iter_search_views(browser, query, settings):
            for card in self.extract_cards(view, query):
                ext = card.external_id or self.external_id_from_url(card.url) or ""
                if not ext or ext in seen:
                    continue
                seen.add(ext)
                card.external_id = ext
                if len(listings) >= cap:
                    log.info("%s: hit per-run cap of %d listings", self.key, cap)
                    return listings
                if not self.detail_needed:
                    li = self.extract_detail(view, card, query)
                    if li:
                        listings.append(li)
                    continue
                browser.human_pause()  # act like a human between opening listings
                try:
                    dview = browser.open_detail(card.url)
                    li = self.extract_detail(dview, card, query)
                    if li:
                        listings.append(li)
                except BotWallError as exc:
                    # Stop trying more listings this run, but keep whatever we already
                    # fetched successfully -- a wall partway through (confirmed to happen
                    # intermittently, not just on the very first request) shouldn't erase
                    # already-good results.
                    exc.partial_listings = list(listings)
                    raise
                except Exception as exc:  # noqa: BLE001 - one bad listing shouldn't kill the run
                    log.warning("%s: detail failed for %s: %s", self.key, card.url, exc)
        return listings

    def health_check(self) -> bool:
        from .session import BrowserConfig, BrowserSession

        try:
            cfg = BrowserConfig.from_settings(get_settings(), profile=self.profile_name)
            with BrowserSession(cfg) as browser:
                self.search(MarketplaceQuery(category="car", terms=["Tesla"]), browser=browser)
            return True
        except Exception:  # noqa: BLE001
            return False
