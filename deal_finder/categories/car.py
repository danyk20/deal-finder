"""The 'car' watch type (e.g. Tesla Model S): make/model, plus year/mileage filters."""

from __future__ import annotations

from ..adapters.base import Listing, MarketplaceQuery
from ..models import Watch
from ..util import to_int
from .base import BaseCategory, FieldDef
from .fields import ITEM_CATEGORY, KEYWORD_AND_AI_FIELDS, LOCATION_FIELDS, PRICE_FIELDS, common_query_kwargs


class CarCategory(BaseCategory):
    key = "car"
    label = "Car"

    search_param_fields = [
        FieldDef("make", "Make", placeholder="Tesla"),
        FieldDef("model", "Model", placeholder="Model S"),
        ITEM_CATEGORY,
    ]

    filter_fields = [
        *PRICE_FIELDS,
        FieldDef("year_min", "Min year", kind="number", placeholder="2016"),
        FieldDef("year_max", "Max year", kind="number"),
        FieldDef("mileage_max", "Max mileage (km)", kind="number"),
        *LOCATION_FIELDS,
        *KEYWORD_AND_AI_FIELDS,
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
            **common_query_kwargs(f),
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
