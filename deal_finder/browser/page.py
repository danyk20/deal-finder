"""PageView: a snapshot of a loaded page decoupled from Playwright.

Adapters and the extraction/detection helpers operate on PageView (plain data), so they
are unit-testable from fixtures without a real browser. The real session builds a
PageView after a navigation settles; the FakeBrowserSession in tests builds one directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class PageView:
    url: str
    html: str = ""
    status: int | None = None
    # Captured network responses that returned JSON: list of (url, parsed_body).
    captures: list[tuple[str, Any]] = field(default_factory=list)

    def json_for(self, url_substr: str) -> list[Any]:
        """Parsed JSON bodies from captured responses whose URL contains ``url_substr``."""
        return [body for (u, body) in self.captures if url_substr in u]
