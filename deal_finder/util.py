"""Small coercion helpers used when reading user-entered (string) filter values."""

from __future__ import annotations

from typing import Any


def to_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace("'", "").replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def to_int(value: Any) -> int | None:
    f = to_float(value)
    return int(f) if f is not None else None


def csv_list(value: Any) -> list[str]:
    """Accept a list already, or a comma/newline-separated string -> list of trimmed strings."""
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip()]
    parts = str(value).replace("\n", ",").split(",")
    return [p.strip() for p in parts if p.strip()]
