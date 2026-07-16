"""Category contract: describes WHAT is being searched.

A category declares the form fields (which drive the web UI automatically), the default
AI questions, how to turn a Watch into a normalized MarketplaceQuery, and any
category-specific match refinement (e.g. year/mileage for cars).

Add a new category (house, phone, ...) by subclassing :class:`BaseCategory` and
registering it in :mod:`deal_finder.registry`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..adapters.base import Listing, MarketplaceQuery
    from ..models import Watch


@dataclass
class FieldDef:
    """One form field. The web UI renders these for the chosen category."""

    name: str
    label: str
    kind: str = "text"  # text | number | textarea
    placeholder: str = ""
    help: str = ""
    options: list[str] = field(default_factory=list)
    default: str = ""  # prefilled value for a brand-new watch


class BaseCategory:
    key: str = ""
    label: str = ""
    search_param_fields: list[FieldDef] = []  # identity of the item (make, model, ...)
    filter_fields: list[FieldDef] = []  # constraints (price, year, location, ...)
    default_questions: list[str] = []

    def build_query(self, watch: "Watch") -> "MarketplaceQuery":  # pragma: no cover - interface
        raise NotImplementedError

    def post_match(self, listing: "Listing", watch: "Watch") -> bool:
        """Category-specific filtering on top of the generic matching engine."""
        return self.post_match_reason(listing, watch) is None

    def post_match_reason(self, listing: "Listing", watch: "Watch") -> str | None:
        """Like :meth:`post_match`, but return None on pass or a human-readable reason
        for the rejection instead of a bare bool."""
        return None
