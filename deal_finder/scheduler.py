"""Per-watch scheduling via APScheduler.

The source of truth for schedules is the ``Watch`` table (active flag + schedule_kind/
value). On startup we rebuild jobs from the DB, so schedules survive restarts without
pickling job state. Each active watch maps to one job ``watch:<id>``.
"""

from __future__ import annotations

import asyncio
import logging
import re

from apscheduler.jobstores.base import JobLookupError
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlmodel import select

from .db import session_scope
from .models import Watch
from .pipeline import run_watch

log = logging.getLogger("deal_finder.scheduler")

_scheduler: AsyncIOScheduler | None = None
_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
_MIN_INTERVAL_SECONDS = 60  # be polite to marketplaces


def get_scheduler() -> AsyncIOScheduler:
    global _scheduler
    if _scheduler is None:
        _scheduler = AsyncIOScheduler(timezone="UTC")
    return _scheduler


def parse_interval(value: str) -> int:
    """'15m' -> 900, '2h' -> 7200, '1d' -> 86400, '1w' -> 604800. Bare number = minutes."""
    value = (value or "").strip().lower()
    m = re.fullmatch(r"(\d+)\s*([smhdw]?)", value)
    if not m:
        raise ValueError(f"invalid interval '{value}' (use e.g. 15m, 2h, 1d, 1w)")
    amount = int(m.group(1))
    unit = m.group(2) or "m"
    seconds = amount * _UNITS[unit]
    if seconds <= 0:
        raise ValueError("interval must be positive")
    return max(seconds, _MIN_INTERVAL_SECONDS)


def build_trigger(kind: str, value: str):
    if kind == "cron":
        return CronTrigger.from_crontab(value, timezone="UTC")
    return IntervalTrigger(seconds=parse_interval(value))


def job_id(watch_id: int) -> str:
    return f"watch:{watch_id}"


def schedule_watch(watch: Watch) -> None:
    """Add or replace the job for a watch. Raises ValueError on a bad schedule."""
    trigger = build_trigger(watch.schedule_kind, watch.schedule_value)
    get_scheduler().add_job(
        _run_watch_job,
        trigger=trigger,
        args=[watch.id],
        id=job_id(watch.id),
        replace_existing=True,
        max_instances=1,
        coalesce=True,
        misfire_grace_time=3600,  # still run a job missed while the machine slept
    )
    log.info("scheduled watch %s (%s=%s)", watch.id, watch.schedule_kind, watch.schedule_value)


def unschedule_watch(watch_id: int) -> None:
    try:
        get_scheduler().remove_job(job_id(watch_id))
        log.info("unscheduled watch %s", watch_id)
    except JobLookupError:
        pass


def next_run_time(watch_id: int):
    job = get_scheduler().get_job(job_id(watch_id))
    return job.next_run_time if job else None


async def _run_watch_job(watch_id: int) -> None:
    # Offload the blocking pipeline to a thread so the event loop stays responsive.
    await asyncio.to_thread(_run_watch_sync, watch_id)


def _run_watch_sync(watch_id: int) -> None:
    with session_scope() as session:
        watch = session.get(Watch, watch_id)
        if watch is None or not watch.active:
            return
        try:
            run_watch(session, watch)
        except Exception:  # noqa: BLE001 - log and keep the scheduler alive
            log.exception("scheduled run failed for watch %s", watch_id)


def start_scheduler() -> None:
    scheduler = get_scheduler()
    if not scheduler.running:
        scheduler.start()
    with session_scope() as session:
        active = session.exec(select(Watch).where(Watch.active == True)).all()  # noqa: E712
    for watch in active:
        try:
            schedule_watch(watch)
        except ValueError as exc:
            log.warning("skipping watch %s with bad schedule: %s", watch.id, exc)


def shutdown_scheduler() -> None:
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
