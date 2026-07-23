"""Central registries for categories and marketplace adapters.

To add a category or marketplace, import it here and add an instance to the list.
Everything else in the app discovers them through these functions.

tutti, Ricardo, AutoScout24, Autolina, AutoUncle and Facebook are all plain adapters from
the pipeline's point of view: each wraps a dedicated PyPI package that manages its own
access internally (tutti and AutoScout24 call genuinely public JSON/GraphQL APIs; Autolina
parses the site's own server-rendered HTML directly; AutoUncle parses schema.org JSON-LD
plus a filtered-search RSC/GraphQL path; Ricardo drives its own bundled
anti-fingerprinting Camoufox browser to get past Cloudflare; Facebook drives its own
separate Playwright browser + login flow). None of them need deal_finder's own shared
browser session. The offline Demo adapter is also plain HTTP (canned data).
"""

from __future__ import annotations

from .adapters.autolina import AutolinaAdapter
from .adapters.autouncle import AutoUncleAdapter
from .adapters.autoscout24 import AutoScout24Adapter
from .adapters.base import BaseAdapter
from .adapters.demo import DemoAdapter
from .adapters.facebook import FacebookAdapter
from .adapters.ricardo import RicardoAdapter
from .adapters.tutti import TuttiAdapter
from .categories.base import BaseCategory
from .categories.car import CarCategory

CATEGORIES: dict[str, BaseCategory] = {c.key: c for c in (CarCategory(),)}

ADAPTERS: dict[str, BaseAdapter] = {
    a.key: a
    for a in (
        TuttiAdapter(),
        RicardoAdapter(),
        AutoScout24Adapter(),
        AutolinaAdapter(),
        AutoUncleAdapter(),
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
