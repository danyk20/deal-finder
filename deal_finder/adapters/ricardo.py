"""Ricardo.ch browser adapter. Free-text search is used (robust vs. brittle brand IDs).
Constants marked VERIFY LIVE were low-confidence in recon and must be confirmed against
a real page during implementation."""

from __future__ import annotations

import re
from urllib.parse import quote

from ._browser_car import CarBrowserAdapter

RICARDO_BASE = "https://www.ricardo.ch"
# Path-based search: spaces MUST be %20 (quote), not "+". VERIFIED live 2026.
RICARDO_SEARCH = RICARDO_BASE + "/de/s/{q}"
# Detail URL form: /de/a/{slug}-{id}/  -> trailing numeric id. VERIFIED live.
RICARDO_ID_RE = re.compile(r"/a/[^/?#]*?-(\d+)(?:[/?#]|$)")


class RicardoBrowserAdapter(CarBrowserAdapter):
    key = "ricardo"
    label = "Ricardo.ch"
    base_url = RICARDO_BASE
    id_regex = RICARDO_ID_RE
    profile_name = "ricardo"
    enabled_by_default = True
    status_note = "real browser (headful); auctions + buy-now; one listing at a time"

    def build_search_urls(self, query, settings):
        q = quote(query.text or " ".join(query.terms), safe="")
        pages = max(1, settings.browser_search_pages)
        return [RICARDO_SEARCH.format(q=q) + (f"?page={p}" if p > 1 else "") for p in range(1, pages + 1)]
