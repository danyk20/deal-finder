"""Timestamps are stored as naive UTC but shown in the machine's local time zone."""

from __future__ import annotations

import time
from datetime import datetime, timezone

import pytest

from deal_finder.scheduler import build_trigger
from deal_finder.util import localtime


@pytest.fixture
def zurich(monkeypatch):
    monkeypatch.setenv("TZ", "Europe/Zurich")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def test_naive_utc_is_shown_in_local_time_with_zone(zurich):
    assert localtime(datetime(2026, 10, 6, 12, 0, 5)) == "2026-10-06 14:00 CEST"  # summer: UTC+2
    assert localtime(datetime(2026, 12, 6, 12, 0, 5), "%Y-%m-%d %H:%M:%S") == "2026-12-06 13:00:05 CET"


def test_aware_times_are_converted_from_their_own_zone(zurich):
    assert localtime(datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)) == "2026-10-06 14:00 CEST"


def test_missing_time():
    assert localtime(None) == "—"


def test_cron_means_local_time(zurich):
    trigger = build_trigger("cron", "0 8 * * *")
    nxt = trigger.get_next_fire_time(None, datetime(2026, 10, 6, 5, 0, tzinfo=timezone.utc))
    assert nxt.astimezone(timezone.utc) == datetime(2026, 10, 6, 6, 0, tzinfo=timezone.utc)  # 08:00 CEST
