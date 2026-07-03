"""tutti.ch adapter (best-effort, Swiss classifieds).

tutti exposes a private GraphQL API that its web frontend uses. There is no official
public API, and the site rejects naive requests (HTTP 403/redirect) without a
browser-like session. This adapter therefore:

  * builds a GraphQL search request against the documented endpoint,
  * sends browser-like headers,
  * parses the response with :func:`parse_tutti_response` — the SINGLE place to adjust
    if tutti changes its response shape,
  * raises :class:`AdapterError` (caught by the pipeline) when blocked or unparseable,
    so a tutti failure never aborts a scan.

The parser is unit-tested against ``tests/fixtures/tutti_search.json`` so its behaviour
is pinned regardless of network access. If the live schema differs from that fixture
when you run it from your own network, update the fixture + parser together.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from typing import Any

import httpx

from ..config import get_settings
from .base import AdapterError, BaseAdapter, Listing, MarketplaceQuery

GRAPHQL_ENDPOINT = "https://api.tutti.ch/v10/graphql"

# Minimal GraphQL search query. Field names mirror the documented frontend query;
# adjust here (and in the parser/fixture) if tutti changes its schema.
_SEARCH_QUERY = """
query SearchListings($query: String!, $constraints: ListingSearchConstraints) {
  search: searchListingsByQuery(query: $query, constraints: $constraints) {
    items {
      id
      title
      body
      price
      currency
      url
      location
      timestamp
      images
      attributes
    }
  }
}
"""


def _num(value: Any) -> float | None:
    if isinstance(value, dict):  # tutti sometimes wraps price as {"amount": n}
        value = value.get("amount")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def parse_tutti_response(payload: dict[str, Any]) -> list[Listing]:
    """Map a tutti GraphQL response into normalized :class:`Listing` objects.

    Expected shape (see fixture)::

        {"data": {"search": {"items": [ {id,title,body,price,currency,url,
                                         location,timestamp,images,attributes}, ... ]}}}
    """
    items = (((payload or {}).get("data") or {}).get("search") or {}).get("items") or []
    listings: list[Listing] = []
    for it in items:
        ext_id = str(it.get("id", "")).strip()
        if not ext_id:
            continue
        ts = it.get("timestamp")
        posted_at = None
        if isinstance(ts, (int, float)):
            posted_at = datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)
        attrs = it.get("attributes") or {}
        listings.append(
            Listing(
                marketplace="tutti",
                external_id=ext_id,
                url=it.get("url") or f"https://www.tutti.ch/vi/{ext_id}",
                title=it.get("title") or "",
                description=it.get("body") or "",
                language=None,  # detected later by the AI translation step
                price=_num(it.get("price")),
                currency=it.get("currency") or "CHF",
                location=it.get("location"),
                posted_at=posted_at,
                attributes=attrs if isinstance(attrs, dict) else {},
                image_urls=list(it.get("images") or []),
            )
        )
    return listings


class TuttiAdapter(BaseAdapter):
    key = "tutti"
    label = "tutti.ch"
    supported_categories = {"car"}
    enabled_by_default = True
    status_note = "best-effort; may require running from your own network/browser session"

    def _post(self, query: MarketplaceQuery) -> dict[str, Any]:
        settings = get_settings()
        variables: dict[str, Any] = {"query": query.text or " ".join(query.terms)}
        constraints: dict[str, Any] = {}
        if query.price_min is not None:
            constraints["priceMin"] = int(query.price_min)
        if query.price_max is not None:
            constraints["priceMax"] = int(query.price_max)
        if query.location:
            constraints["location"] = query.location
        if constraints:
            variables["constraints"] = constraints

        headers = {
            "User-Agent": settings.http_user_agent,
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Origin": "https://www.tutti.ch",
            "Referer": "https://www.tutti.ch/",
        }
        try:
            resp = httpx.post(
                GRAPHQL_ENDPOINT,
                json={"query": _SEARCH_QUERY, "variables": variables},
                headers=headers,
                timeout=settings.request_timeout,
            )
        except httpx.HTTPError as exc:
            raise AdapterError(f"tutti request failed: {exc}") from exc
        if resp.status_code != 200:
            raise AdapterError(
                f"tutti returned HTTP {resp.status_code} — the endpoint likely needs a "
                "browser session/cookies; run from your own network or add a Playwright "
                "fallback. See the adapter docstring."
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise AdapterError(f"tutti returned non-JSON response: {exc}") from exc

    def search(self, query: MarketplaceQuery) -> Iterable[Listing]:
        payload = self._post(query)
        if payload.get("errors"):
            raise AdapterError(f"tutti GraphQL errors: {payload['errors']}")
        return parse_tutti_response(payload)

    def health_check(self) -> bool:
        try:
            self._post(MarketplaceQuery(category="car", terms=["Tesla"]))
            return True
        except AdapterError:
            return False


# ---------------------------------------------------------------------------
# Browser-driven adapter (the default): drives a real Chrome like a human,
# opening the search page then each listing one at a time. Reuses the shared
# CarBrowserAdapter extraction. Constants below are VERIFY LIVE.
# ---------------------------------------------------------------------------

import re as _re  # noqa: E402
from urllib.parse import quote_plus  # noqa: E402

from ._browser_car import CarBrowserAdapter  # noqa: E402

TUTTI_BASE = "https://www.tutti.ch"
TUTTI_SEARCH = TUTTI_BASE + "/de/q/autos?query={q}&sorting=newest"  # VERIFIED (redirects to a search-token URL; results render server-side)
# Detail URLs look like /de/vi/{canton}/fahrzeuge/autos/{slug}/{id}. Requiring the
# "/autos/" segment restricts to cars (excludes toys, accessories) and the trailing
# numeric group is the stable listing id. VERIFIED live 2026.
TUTTI_ID_RE = _re.compile(r"/vi/[^?#]*/autos/[^?#]*?/(\d+)(?:[/?#]|$)")


class TuttiBrowserAdapter(CarBrowserAdapter):
    key = "tutti"
    label = "tutti.ch"
    base_url = TUTTI_BASE
    id_regex = TUTTI_ID_RE
    profile_name = "tutti"
    enabled_by_default = True
    status_note = "real browser (headful); one listing at a time"

    def build_search_urls(self, query, settings):
        q = quote_plus(query.text or " ".join(query.terms))
        pages = max(1, settings.browser_search_pages)
        return [TUTTI_SEARCH.format(q=q) + (f"&page={p}" if p > 1 else "") for p in range(1, pages + 1)]
