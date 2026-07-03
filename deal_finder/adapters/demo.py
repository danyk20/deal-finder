"""Offline demo adapter with canned listings.

It returns a fixed set of (multilingual) car listings regardless of the query, so the
full pipeline — matching, dedup, AI translation/Q&A, email, scheduling, UI — can be
exercised end-to-end with no network or marketplace access. Great for development,
tests, and the first run-through. Disabled-by-default in real watches is fine; it's
handy to leave it selectable.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime

from .base import BaseAdapter, Listing, MarketplaceQuery

_DEMO_LISTINGS = [
    Listing(
        marketplace="demo",
        external_id="demo-1",
        url="https://example.com/demo/tesla-model-s-75d",
        title="Tesla Model S 75D Allrad — top gepflegt",
        description=(
            "Verkaufe meinen Tesla Model S 75D, Erstzulassung 2017, 95'000 km. "
            "Fahrzeug in sehr gutem Zustand, scheckheftgepflegt, neue Winterreifen. "
            "Kleiner Steinschlag in der Frontscheibe. Abholung in Zürich ab nächster Woche möglich."
        ),
        language="de",
        price=38900,
        currency="CHF",
        location="Zürich",
        posted_at=datetime(2026, 6, 23, 9, 30),
        attributes={"year": 2017, "mileage_km": 95000, "fuel": "electric", "drive": "AWD"},
        image_urls=["https://example.com/img/ms75d.jpg"],
    ),
    Listing(
        marketplace="demo",
        external_id="demo-2",
        url="https://example.com/demo/tesla-model-s-p85",
        title="Tesla Model S P85 — Liebhaberfahrzeug",
        description=(
            "Model S P85 aus 2014, 120'000 km. Erste Generation, frisch ab MFK. "
            "Batterie noch gut. Einige Gebrauchsspuren im Innenraum. Standort Bern."
        ),
        language="de",
        price=27500,
        currency="CHF",
        location="Bern",
        posted_at=datetime(2026, 6, 22, 14, 0),
        attributes={"year": 2014, "mileage_km": 120000, "fuel": "electric"},
        image_urls=[],
    ),
    Listing(
        marketplace="demo",
        external_id="demo-3",
        url="https://example.com/demo/tesla-model-s-long-range",
        title="Tesla Model S Long Range 2020 — état impeccable",
        description=(
            "Tesla Model S Long Range, mise en circulation 2020, 45'000 km. "
            "Véhicule en parfait état, jamais accidenté, carnet de service complet. "
            "Disponible à Genève, enlèvement possible le week-end."
        ),
        language="fr",
        price=56000,
        currency="CHF",
        location="Genève",
        posted_at=datetime(2026, 6, 24, 8, 15),
        attributes={"year": 2020, "mileage_km": 45000, "fuel": "electric"},
        image_urls=["https://example.com/img/mslr.jpg"],
    ),
    Listing(
        marketplace="demo",
        external_id="demo-4",
        url="https://example.com/demo/tesla-model-3",
        title="Tesla Model 3 Standard Range Plus",
        description="Tesla Model 3, 2021, 60'000 km. Different model — should be excluded.",
        language="en",
        price=31000,
        currency="CHF",
        location="Luzern",
        posted_at=datetime(2026, 6, 21, 11, 0),
        attributes={"year": 2021, "mileage_km": 60000, "fuel": "electric"},
        image_urls=[],
    ),
    Listing(
        marketplace="demo",
        external_id="demo-5",
        url="https://example.com/demo/tesla-model-s-plaid",
        title="Tesla Model S Plaid 2022 — like new",
        description=(
            "Tesla Model S Plaid, 2022, only 15'000 km. Perfect condition, no issues, "
            "full warranty remaining. Pickup in Zug, flexible timing."
        ),
        language="en",
        price=89000,
        currency="CHF",
        location="Zug",
        posted_at=datetime(2026, 6, 24, 7, 0),
        attributes={"year": 2022, "mileage_km": 15000, "fuel": "electric"},
        image_urls=["https://example.com/img/plaid.jpg"],
    ),
]


class DemoAdapter(BaseAdapter):
    key = "demo"
    label = "Demo (offline sample data)"
    supported_categories = {"car"}
    enabled_by_default = False
    status_note = "offline sample data for testing"
    internal_only = True  # dev/test only — hidden from the web UI and public API

    def search(self, query: MarketplaceQuery) -> Iterable[Listing]:
        # Returns everything; the matching engine applies the watch's filters.
        return list(_DEMO_LISTINGS)

    def health_check(self) -> bool:
        return True
