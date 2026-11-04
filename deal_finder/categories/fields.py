"""Form fields shared by every watch type (car, general item, ...), so they stay identical
across types: same names (a watch keeps its values when switched to another type), same
labels/help, same mapping onto the MarketplaceQuery."""

from __future__ import annotations

from typing import Any

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
    FieldDef(
        "non_negotiables",
        "Non-negotiables (checked by AI)",
        kind="textarea",
        default="Item is currently working.",
        placeholder="One per line, e.g.\nmust be green\nno visible rust or accident damage\nengine currently starts and runs",
        help=(
            "Free-text must-haves, ONE PER LINE. The AI checks each line on its own "
            "against each listing's full data and description, and filters out a listing "
            "as soon as one line clearly fails. A listing is only rejected when it clearly "
            "contradicts a requirement; ambiguous/unmentioned details are not held against "
            "it. Leave blank to disable. Costs about one AI call per line for each listing "
            "that already passed every other filter."
        ),
    ),
    FieldDef(
        "non_negotiables_photos",
        "Use photos",
        kind="checkbox",
        default="true",
        help=(
            "The text always comes first. A line the text doesn't settle is then checked "
            "against the listing's photos, 3 per AI call (~10-20 s each), until one call "
            "settles it -- e.g. the colour, visible damage, or specs printed on the box. "
            "A listing rejected this way says which photos showed it. Off: a line the text "
            "doesn't settle is given the benefit of the doubt."
        ),
    ),
]


def non_negotiables_use_photos(filters: dict[str, Any]) -> bool:
    """The watch's "use photos" switch for its non-negotiables -- on unless turned off."""
    return to_bool(filters.get("non_negotiables_photos"))


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
