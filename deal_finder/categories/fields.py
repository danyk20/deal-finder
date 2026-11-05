"""Form fields shared by every watch type (car, general item, ...), so they stay identical
across types: same names (a watch keeps its values when switched to another type), same
labels/help, same mapping onto the MarketplaceQuery."""

from __future__ import annotations

from typing import Any

from ..ai.dealbreakers import Requirement
from ..util import csv_list, to_bool, to_float, to_int
from .base import FieldDef

ITEM_CATEGORY = FieldDef(
    "item_category",
    "Category",
    placeholder="e.g. car, desktop computer, road bike",
    help=(
        "Optional, in your own words. After saving, the AI picks the best-matching "
        "category on tutti.ch and Ricardo from this and every other field here -- "
        "also when this is left empty. Other marketplaces ignore it."
    ),
    needs_site_categories=True,
)

PRICE_FIELDS = [
    FieldDef("price_min", "Min price (CHF)", kind="number"),
    FieldDef("price_max", "Max price (CHF)", kind="number"),
]

LOCATION_FIELDS = [
    FieldDef("location", "Location / canton", placeholder="Zürich"),
    FieldDef("radius_km", "Radius (km)", kind="number"),
]

KEYWORD_AND_AI_FIELDS = [
    FieldDef(
        "keywords_include",
        "Must contain (comma-separated)",
        help="All of these words must appear in the listing.",
    ),
    FieldDef(
        "keywords_exclude",
        "Must NOT contain (comma-separated)",
        help="Listing is skipped if any of these words appear.",
    ),
    # The label is part of what the AI's marketplace-category pick is based on (see
    # site_categories.describe_watch): changing it redoes every watch's pick.
    FieldDef(
        "non_negotiables",
        "Non-negotiables (checked by AI)",
        kind="requirements",
        default="Item is currently working.",
        placeholder="e.g. no visible rust or accident damage",
        help=(
            "One must-have per box. The AI checks each on its own against each listing's "
            "full data and description, and filters out a listing as soon as one clearly "
            "fails; details a listing doesn't mention are never held against it. With "
            "\"Use photos\", a box the text doesn't settle is then checked against the "
            "listing's photos, 3 per AI call (~10-40 s each), until one call settles it -- "
            "e.g. the colour, visible damage, or specs printed on the box; a listing "
            "rejected that way says which photos showed it. Leave every box empty to "
            "disable. Costs about one AI call per box for each listing that already "
            "passed every other filter."
        ),
    ),
]


def non_negotiables(filters: dict[str, Any], name: str = "non_negotiables") -> list[Requirement]:
    """A watch's non-negotiables, one per box: (text as typed, use photos). Before they had
    a box each they were one text, a line each, with one "use photos" switch for all of
    them (``<name>_photos``, on unless turned off) -- still read that way until re-saved."""
    value = filters.get(name)
    if isinstance(value, list):
        rows = [r for r in value if isinstance(r, dict)]
        return [Requirement(str(r.get("text") or ""), to_bool(r.get("photos"))) for r in rows
                if str(r.get("text") or "").strip()]
    use_photos = to_bool(filters.get(f"{name}_photos"))
    return [Requirement(line, use_photos) for line in str(value or "").splitlines() if line.strip()]


def common_query_kwargs(filters: dict[str, Any]) -> dict[str, Any]:
    """The MarketplaceQuery arguments every watch type fills the same way."""
    return {
        "price_min": to_float(filters.get("price_min")),
        "price_max": to_float(filters.get("price_max")),
        "location": (filters.get("location") or None),
        "radius_km": to_int(filters.get("radius_km")),
        "keywords_include": csv_list(filters.get("keywords_include")),
        "keywords_exclude": csv_list(filters.get("keywords_exclude")),
    }
