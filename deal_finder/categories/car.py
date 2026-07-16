"""The 'car' category — the v1 focus (e.g. Tesla Model S)."""

from __future__ import annotations

from ..adapters.base import Listing, MarketplaceQuery
from ..models import Watch
from ..util import csv_list, to_float, to_int
from .base import BaseCategory, FieldDef


class CarCategory(BaseCategory):
    key = "car"
    label = "Car"

    search_param_fields = [
        FieldDef("make", "Make", placeholder="Tesla"),
        FieldDef("model", "Model", placeholder="Model S"),
    ]

    filter_fields = [
        FieldDef("price_min", "Min price (CHF)", kind="number"),
        FieldDef("price_max", "Max price (CHF)", kind="number"),
        FieldDef("year_min", "Min year", kind="number", placeholder="2016"),
        FieldDef("year_max", "Max year", kind="number"),
        FieldDef("mileage_max", "Max mileage (km)", kind="number"),
        FieldDef("location", "Location / canton", placeholder="Zürich"),
        FieldDef("radius_km", "Radius (km)", kind="number"),
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
            "Non-negotiables (checked by AI, incl. photos)",
            kind="textarea",
            default="Item is currently working.",
            placeholder="e.g. must be green, no visible rust or accident damage, engine currently starts and runs",
            help=(
                "Free-text must-haves. The AI checks each listing's full data, description, "
                "AND photos against this text, and filters out anything that clearly fails "
                "it -- things the description never mentions (like colour) are still judged "
                "from photos when available. A listing is only rejected when it clearly "
                "contradicts a requirement; ambiguous/unmentioned details are not held "
                "against it. Leave blank to disable. Costs one extra AI call per candidate "
                "listing that already passed every other filter."
            ),
        ),
    ]

    default_questions = [
        "Is the car in perfect condition?",
        "What are the known issues, defects, or damage?",
        "Has it ever been in an accident?",
        "When and where can it be picked up?",
        "What is the service and maintenance history?",
    ]

    def build_query(self, watch: Watch) -> MarketplaceQuery:
        sp = watch.search_params or {}
        f = watch.filters or {}
        terms = [t for t in (sp.get("make"), sp.get("model")) if t]
        return MarketplaceQuery(
            category=self.key,
            terms=terms,
            price_min=to_float(f.get("price_min")),
            price_max=to_float(f.get("price_max")),
            location=(f.get("location") or None),
            radius_km=to_int(f.get("radius_km")),
            keywords_include=csv_list(f.get("keywords_include")),
            keywords_exclude=csv_list(f.get("keywords_exclude")),
            params={
                "make": sp.get("make"),
                "model": sp.get("model"),
                "year_min": to_int(f.get("year_min")),
                "year_max": to_int(f.get("year_max")),
                "mileage_max": to_int(f.get("mileage_max")),
            },
        )

    def post_match_reason(self, listing: Listing, watch: Watch) -> str | None:
        f = watch.filters or {}
        year = listing.attributes.get("year")
        if year is not None:
            ymin, ymax = to_int(f.get("year_min")), to_int(f.get("year_max"))
            if ymin is not None and year < ymin:
                return f"year {year} is below the minimum {ymin}"
            if ymax is not None and year > ymax:
                return f"year {year} is above the maximum {ymax}"
        mileage = listing.attributes.get("mileage_km")
        mmax = to_int(f.get("mileage_max"))
        if mmax is not None and mileage is not None and mileage > mmax:
            return f"mileage {mileage} km is above the maximum {mmax} km"
        return None
