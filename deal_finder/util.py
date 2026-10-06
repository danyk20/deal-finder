"""Small helpers: coercing user-entered (string) filter values, and showing timestamps."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def localtime(dt: datetime | None, fmt: str = "%Y-%m-%d %H:%M") -> str:
    """A timestamp in the machine's local time zone, labelled (e.g. "2026-10-06 14:03
    CEST"). Naive datetimes are UTC, as stored everywhere (see models.utcnow); aware ones
    (the scheduler's next run times) are converted from their own zone. None -> "—"."""
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone()
    return f"{local.strftime(fmt)} {local.tzname()}"


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
