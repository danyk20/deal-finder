"""Central registries for categories and marketplace adapters.

To add a category or marketplace, import it here and add an instance to the list.
Everything else in the app discovers them through these functions.

Ricardo is browser-driven (real headful Chrome, one listing at a time) via deal_finder's
shared browser/ infra, to bypass bot detection. tutti, AutoScout24 and Facebook are plain
adapters from the pipeline's point of view: each wraps a dedicated PyPI package that
manages its own access internally (tutti and AutoScout24 call genuinely public JSON/GraphQL
APIs; Facebook drives its own separate Playwright browser + login flow). The offline Demo
adapter is also plain HTTP (canned data).
"""

from __future__ import annotations

from .adapters.autoscout24 import AutoScout24Adapter
from .adapters.base import BaseAdapter
from .adapters.demo import DemoAdapter
from .adapters.facebook import FacebookAdapter
from .adapters.ricardo import RicardoBrowserAdapter
from .adapters.tutti import TuttiAdapter
from .categories.base import BaseCategory
from .categories.car import CarCategory

CATEGORIES: dict[str, BaseCategory] = {c.key: c for c in (CarCategory(),)}

ADAPTERS: dict[str, BaseAdapter] = {
    a.key: a
    for a in (
        TuttiAdapter(),
        RicardoBrowserAdapter(),
        AutoScout24Adapter(),
        FacebookAdapter(),
        DemoAdapter(),
    )
}


def get_category(key: str) -> BaseCategory | None:
    return CATEGORIES.get(key)


def get_adapter(key: str) -> BaseAdapter | None:
    return ADAPTERS.get(key)


def adapters_for_category(category_key: str, *, include_internal: bool = False) -> list[BaseAdapter]:
    return [
        a
        for a in ADAPTERS.values()
        if category_key in a.supported_categories and (include_internal or not a.internal_only)
    ]


def list_categories() -> list[BaseCategory]:
    return list(CATEGORIES.values())


def list_adapters(*, include_internal: bool = False) -> list[BaseAdapter]:
    """All registered adapters. Excludes internal/dev-only ones (e.g. Demo) by default —
    pass include_internal=True for test/dev tooling that needs the full set. Adapters
    stay resolvable via get_adapter() regardless, so a watch already using one keeps
    working even after it's hidden from the UI/API."""
    return [a for a in ADAPTERS.values() if include_internal or not a.internal_only]
