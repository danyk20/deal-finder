"""The 'general' watch type: anything that isn't a car (a Mac Mini, a road bike, a sofa,
...). One free-text search instead of make/model, and no car-only year/mileage filters.
Only offered on marketplaces that sell more than cars (tutti, Ricardo, Facebook)."""

from __future__ import annotations

import dataclasses

from ..adapters.base import MarketplaceQuery
from ..models import Watch
from .base import BaseCategory, FieldDef
from .fields import ITEM_CATEGORY, KEYWORD_AND_AI_FIELDS, LOCATION_FIELDS, PRICE_FIELDS, common_query_kwargs


def _with_item_placeholder(field: FieldDef) -> FieldDef:
    # The shared non-negotiables example is about cars ("engine currently starts").
    if field.name == "non_negotiables":
        return dataclasses.replace(
            field, placeholder="One per line, e.g.\nat least 32 GB RAM\nApple M1 or newer\nno visible damage\noriginal box"
        )
    return field


class GeneralCategory(BaseCategory):
    key = "general"
    label = "General item"

    search_param_fields = [
        FieldDef(
            "query",
            "Search text",
            placeholder="Mac Mini M2",
            help="What you'd type into the marketplace's search box. Every word must appear in a listing.",
        ),
        ITEM_CATEGORY,
    ]

    filter_fields = [
        *PRICE_FIELDS,
        *LOCATION_FIELDS,
        *(_with_item_placeholder(f) for f in KEYWORD_AND_AI_FIELDS),
    ]

    default_questions = [
        "Is the item in perfect working condition?",
        "What are the known issues, defects, or damage?",
        "How old is it, and why is it being sold?",
        "What accessories or original packaging are included?",
        "When and where can it be picked up, or can it be shipped?",
    ]

    def build_query(self, watch: Watch) -> MarketplaceQuery:
        sp = watch.search_params or {}
        text = str(sp.get("query") or "").strip()
        return MarketplaceQuery(
            category=self.key,
            # One term per word: the matcher requires every term in the listing, so word
            # order/separators don't matter ("Mac mini (M2, 2023)" still matches "Mac Mini
            # M2"), while the marketplaces get the text as typed (query.text).
            terms=text.split(),
            **common_query_kwargs(watch.filters or {}),
            params={"query": text},
        )
