"""Pydantic schemas for the JSON API."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel


class WatchCreate(BaseModel):
    name: str
    category: str = "car"
    schedule_kind: str = "interval"  # "interval" | "cron"
    schedule_value: str = "1d"
    marketplaces: list[str] = []
    search_params: dict[str, Any] = {}
    filters: dict[str, Any] = {}
    notify_email: str = ""
    questions: list[str] = []
    active: bool = False


class WatchUpdate(BaseModel):
    name: Optional[str] = None
    category: Optional[str] = None
    schedule_kind: Optional[str] = None
    schedule_value: Optional[str] = None
    marketplaces: Optional[list[str]] = None
    search_params: Optional[dict[str, Any]] = None
    filters: Optional[dict[str, Any]] = None
    notify_email: Optional[str] = None
    questions: Optional[list[str]] = None
    active: Optional[bool] = None


class WatchRead(BaseModel):
    id: int
    name: str
    category: str
    active: bool
    schedule_kind: str
    schedule_value: str
    marketplaces: list[str]
    search_params: dict[str, Any]
    filters: dict[str, Any]
    notify_email: str
    questions: list[str]
    seed_done: bool
    created_at: datetime
    updated_at: datetime
    last_run_at: Optional[datetime] = None
    last_run_status: Optional[str] = None
    next_run_at: Optional[datetime] = None


class SettingsUpdate(BaseModel):
    """Any subset of editable settings keys (string values)."""

    values: dict[str, str]
