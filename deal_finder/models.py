"""Database models (SQLModel / SQLite).

Datetimes are stored as naive UTC (SQLite drops tz info anyway); use :func:`utcnow`
everywhere so comparisons stay consistent.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlmodel import JSON, Column, Field, SQLModel, UniqueConstraint


def utcnow() -> datetime:
    """Timezone-naive UTC now (consistent with what SQLite stores)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Watch(SQLModel, table=True):
    """A saved search: what to look for, where, how often, and where to email matches."""

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str
    category: str = "car"
    active: bool = False

    # Scheduling: kind="interval" -> value like "15m"/"2h"/"1d"/"1w";
    #             kind="cron"     -> value is a 5-field crontab like "0 8 * * *".
    schedule_kind: str = "interval"
    schedule_value: str = "1d"

    # Which marketplace adapters to run (list of adapter keys, e.g. ["demo", "tutti"]).
    marketplaces: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    # Category-specific search params (e.g. {"make": "Tesla", "model": "Model S"}).
    search_params: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    # Generic + category filters (price_min/max, year_min/max, mileage_max, location,
    # radius_km, keywords_include[], keywords_exclude[]).
    filters: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))

    notify_email: str = ""
    # Which channel to notify through: "email" or "telegram" (default).
    notify_channel: str = "telegram"
    # Chat ID to message via the Telegram bot (see Settings -> Telegram). Falls back to
    # settings.telegram_default_chat_id when creating a new watch, same pattern as notify_email.
    telegram_chat_id: str = ""
    # Predefined questions answered by the local AI from each listing's text.
    questions: list[str] = Field(default_factory=list, sa_column=Column(JSON))

    # True once the first (seeding) run has marked pre-existing listings as seen.
    seed_done: bool = False

    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    last_run_at: Optional[datetime] = None
    last_run_status: Optional[str] = None


class SeenListing(SQLModel, table=True):
    """Records every listing we've already processed for a watch (the dedup key)."""

    __table_args__ = (
        UniqueConstraint("watch_id", "marketplace", "external_id", name="uq_seen_listing"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    watch_id: int = Field(foreign_key="watch.id", index=True)
    marketplace: str
    external_id: str
    content_hash: str = ""
    url: str = ""
    title: str = ""
    price: Optional[float] = None
    notified: bool = False
    first_seen_at: datetime = Field(default_factory=utcnow)
    notified_at: Optional[datetime] = None


class NotificationLog(SQLModel, table=True):
    """One row per notification send attempt (success or failure), for any channel."""

    id: Optional[int] = Field(default=None, primary_key=True)
    watch_id: int = Field(foreign_key="watch.id", index=True)
    # Recipient address: an email address or a Telegram chat ID, depending on `channel`.
    # Field name kept as `email_to` (not renamed) to avoid a RENAME COLUMN migration.
    email_to: str = ""
    channel: str = "email"  # "email" | "telegram"
    subject: str = ""
    num_matches: int = 0
    success: bool = False
    error: Optional[str] = None
    sent_at: datetime = Field(default_factory=utcnow)


class AppSetting(SQLModel, table=True):
    """Key-value store for UI-editable settings that override env defaults."""

    key: str = Field(primary_key=True)
    value: str = ""
