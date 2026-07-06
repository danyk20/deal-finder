"""Watch lifecycle operations shared by the JSON API and the web UI.

Keeps the database and the scheduler consistent: starting a watch schedules its job,
stopping/deleting removes it, editing an active watch reschedules it.
"""

from __future__ import annotations

from typing import Any

from sqlmodel import Session, select

from .models import NotificationLog, SeenListing, Watch, utcnow
from .pipeline import RunResult, run_watch
from .registry import get_category
from .scheduler import schedule_watch, unschedule_watch

_SCHEDULE_FIELDS = {"schedule_kind", "schedule_value"}


def _apply_questions_default(watch: Watch) -> None:
    if not watch.questions:
        category = get_category(watch.category)
        if category is not None:
            watch.questions = list(category.default_questions)


def create_watch(session: Session, data: dict[str, Any]) -> Watch:
    watch = Watch(**data)
    _apply_questions_default(watch)
    session.add(watch)
    session.commit()
    session.refresh(watch)
    if watch.active:
        schedule_watch(watch)
    return watch


def update_watch(session: Session, watch: Watch, data: dict[str, Any]) -> Watch:
    for key, value in data.items():
        if value is not None:
            setattr(watch, key, value)
    watch.updated_at = utcnow()
    session.add(watch)
    session.commit()
    session.refresh(watch)
    # Keep the scheduler in sync with the new state.
    if watch.active:
        schedule_watch(watch)  # add or replace (picks up any schedule change)
    else:
        unschedule_watch(watch.id)
    return watch


def start_watch(session: Session, watch: Watch) -> Watch:
    watch.active = True
    watch.updated_at = utcnow()
    session.add(watch)
    session.commit()
    session.refresh(watch)
    schedule_watch(watch)
    return watch


def stop_watch(session: Session, watch: Watch) -> Watch:
    watch.active = False
    watch.updated_at = utcnow()
    session.add(watch)
    session.commit()
    session.refresh(watch)
    unschedule_watch(watch.id)
    return watch


def delete_watch(session: Session, watch: Watch) -> None:
    unschedule_watch(watch.id)
    for row in session.exec(select(SeenListing).where(SeenListing.watch_id == watch.id)).all():
        session.delete(row)
    for row in session.exec(
        select(NotificationLog).where(NotificationLog.watch_id == watch.id)
    ).all():
        session.delete(row)
    session.delete(watch)
    session.commit()


def run_now(
    session: Session,
    watch: Watch,
    *,
    notify: bool = False,
    test_mode: bool = True,
    dry_run: bool = False,
) -> RunResult:
    """Manual run. Default = preview (no notification, no DB writes).

    test_mode=True treats all matches as new and writes nothing (idempotent testing).
    dry_run=True opens every match in a local browser tab instead of notifying (and, like
    test_mode, never writes to the DB) -- see pipeline.run_watch for details.
    """
    return run_watch(session, watch, notify=notify, ignore_seen=test_mode, dry_run=dry_run)
