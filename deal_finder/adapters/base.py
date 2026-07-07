"""Core adapter contract: the normalized query in, normalized listings out.

Add a new marketplace by subclassing :class:`BaseAdapter`, implementing ``search``,
and registering it in :mod:`deal_finder.registry`. Nothing else in the app needs to
change — the pipeline, matching engine, and web UI all work in terms of these types.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..config import Settings


@dataclass
class MarketplaceQuery:
    """Normalized search intent, produced by a Category from a Watch."""

    category: str
    terms: list[str] = field(default_factory=list)  # e.g. ["Tesla", "Model S"]
    price_min: float | None = None
    price_max: float | None = None
    location: str | None = None
    radius_km: int | None = None
    keywords_include: list[str] = field(default_factory=list)
    keywords_exclude: list[str] = field(default_factory=list)
    params: dict[str, Any] = field(default_factory=dict)  # raw category params

    @property
    def text(self) -> str:
        return " ".join(self.terms).strip()


@dataclass
class Listing:
    """A normalized marketplace result. All adapters map their raw data into this."""

    marketplace: str
    external_id: str
    url: str
    title: str
    description: str = ""
    language: str | None = None  # ISO code if known ("de", "fr", "it", "en")
    price: float | None = None
    currency: str = "CHF"
    location: str | None = None
    posted_at: datetime | None = None
    attributes: dict[str, Any] = field(default_factory=dict)  # year, mileage_km, fuel, ...
    image_urls: list[str] = field(default_factory=list)

    def content_hash(self) -> str:
        """Stable short hash of the fields that matter for "did this listing change?"."""
        basis = f"{self.title}|{self.price}|{self.description}".encode("utf-8", "ignore")
        return hashlib.sha256(basis).hexdigest()[:16]

    @property
    def searchable_text(self) -> str:
        return f"{self.title}\n{self.description}".lower()


class AdapterError(Exception):
    """Recoverable adapter failure (network error, bot challenge, parse problem).

    The pipeline catches this per-adapter so one marketplace failing never aborts a run.

    ``partial_listings`` lets an adapter that fails partway through a multi-item fetch
    (e.g. a bot-wall hit after some listings' details were already retrieved) attach
    whatever it already collected, so the pipeline keeps those results instead of
    discarding a run's worth of successful work over one later failure -- see
    pipeline.py's _collect_listings.
    """

    def __init__(self, message: str, *, partial_listings: list | None = None):
        super().__init__(message)
        self.partial_listings = partial_listings or []


class BaseAdapter:
    key: str = ""
    label: str = ""
    supported_categories: set[str] = set()
    enabled_by_default: bool = True
    # Short human note shown in the UI, e.g. "experimental" or "needs browser".
    status_note: str = ""
    # True for adapters that exist for testing/development only (e.g. offline sample
    # data) and should stay registered (a watch that already uses one keeps working,
    # and tests can still reach it via get_adapter) but never be offered to end users
    # in the web UI or the public /api/marketplaces listing.
    internal_only: bool = False

    def search(
        self, query: MarketplaceQuery, settings: Settings | None = None
    ) -> Iterable[Listing]:  # pragma: no cover - interface
        """``settings``, when given, is the caller's already-resolved effective settings
        (env defaults + DB overrides — see config.effective_settings) for this run. Adapter
        instances are shared singletons across concurrent watch runs (see registry.py), so
        this must be taken as a plain argument rather than stored on ``self``. Adapters
        that don't need any settings can ignore it; adapters that do should prefer it over
        calling config.get_settings() themselves, which only sees env/.env values and
        misses anything the user configured via the web UI's Settings page."""
        raise NotImplementedError

    def health_check(self) -> bool:
        """Quick self-test that the endpoint/parser still works. Override per adapter."""
        return True
