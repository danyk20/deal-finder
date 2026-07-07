"""Text normalization helpers (price/mileage/year) shared by adapters that parse
unstructured listing text as a fallback when their underlying package doesn't expose a
structured field for it. Pure functions — fixture-testable."""

from __future__ import annotations

import re
from typing import Any

_NUM_RE = re.compile(r"\d[\d'’  .,]*\d|\d")


def _digits_only(s: str) -> int | None:
    m = _NUM_RE.search(s)
    if not m:
        return None
    cleaned = re.sub(r"[^\d]", "", m.group(0))  # drop apostrophes/dots/commas/spaces
    return int(cleaned) if cleaned else None


def parse_price(raw: Any) -> tuple[float | None, str]:
    """Return (amount, currency). Handles numbers, {'amount': n}, and strings like
    "CHF 38'900.-", "Fr. 27500", "€ 45.900"."""
    if raw is None:
        return None, "CHF"
    if isinstance(raw, dict):
        raw = raw.get("amount", raw.get("value", raw.get("amountInCents")))
        if isinstance(raw, (int, float)) and raw and raw % 100 == 0 and raw > 1_000_000:
            raw = raw / 100  # cents heuristic
    if isinstance(raw, (int, float)):
        return float(raw), "CHF"
    s = str(raw)
    currency = "EUR" if ("€" in s or "EUR" in s.upper()) else "CHF"
    n = _digits_only(s)
    return (float(n) if n is not None else None), currency


def parse_int_km(*texts: str) -> int | None:
    for t in texts:
        if not t:
            continue
        m = re.search(r"([\d'’  .]+)\s*km", t, re.IGNORECASE)
        if m:
            digits = re.sub(r"[^\d]", "", m.group(1))
            if digits:
                return int(digits)
    return None


def parse_year(*texts: str) -> int | None:
    """First plausible model/registration year (1980..2035) found across the texts."""
    for t in texts:
        if not t:
            continue
        for m in re.finditer(r"\b(19[89]\d|20[0-3]\d)\b", t):
            year = int(m.group(1))
            if 1980 <= year <= 2035:
                return year
    return None
