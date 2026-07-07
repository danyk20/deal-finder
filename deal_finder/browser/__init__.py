"""Shared, marketplace-agnostic scraping helpers reused by multiple adapters.

Deal Finder no longer drives its own browser session: every marketplace adapter (tutti,
Ricardo, AutoScout24, Facebook) now wraps a dedicated PyPI package that manages its own
access internally. This package only holds pure, dependency-free utilities those
adapters still share -- e.g. extract.py's parse_price/parse_year/parse_int_km regex
fallbacks for unstructured listing text.
"""

from __future__ import annotations
