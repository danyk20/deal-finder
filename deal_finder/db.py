"""Database engine, session helpers, and settings overlay access."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine, select

from .config import Settings, effective_settings, get_settings
from .models import AppSetting

_engine = None


def get_engine():
    global _engine
    if _engine is None:
        url = get_settings().database_url
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        # Ensure the parent directory exists for file-based SQLite (it won't be created).
        if url.startswith("sqlite") and ":memory:" not in url:
            path = url.split("sqlite:///", 1)[-1]
            if path:
                Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
        _engine = create_engine(url, connect_args=connect_args)
    return _engine


# Lightweight, idempotent schema migrations: (table, column, DDL type+default).
# create_all() only creates *missing tables*, never adds columns to tables that already
# exist -- so a column added here in code needs an explicit ALTER TABLE for anyone who
# already has a database from before that column existed. No migration framework (e.g.
# Alembic) is in place yet; this simple list is a pragmatic fit for the project's current
# size. Add one entry per new column; defaults must match the model field's default so
# existing rows behave exactly as they did before the column existed.
_MIGRATIONS: list[tuple[str, str, str]] = [
    ("watch", "notify_channel", "TEXT NOT NULL DEFAULT 'email'"),
    ("watch", "telegram_chat_id", "TEXT NOT NULL DEFAULT ''"),
    ("notificationlog", "channel", "TEXT NOT NULL DEFAULT 'email'"),
]


def _migrate_schema(engine) -> None:
    """Add any missing columns from _MIGRATIONS. Safe to call on every startup: a fresh
    database already has every column via create_all(), so this is a no-op there."""
    with engine.begin() as conn:
        for table, column, ddl in _MIGRATIONS:
            existing = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if column not in existing:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def init_db() -> None:
    """Create tables if they don't exist, then apply any pending column migrations."""
    from . import models  # noqa: F401  (ensure model classes are registered)

    engine = get_engine()
    SQLModel.metadata.create_all(engine)
    _migrate_schema(engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Context-managed session that commits on success and rolls back on error."""
    session = Session(get_engine())
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def get_session() -> Iterator[Session]:
    """FastAPI dependency: yields a session (no auto-commit; callers commit explicitly)."""
    with Session(get_engine()) as session:
        yield session


def load_setting_overrides(session: Session) -> dict[str, str]:
    return {row.key: row.value for row in session.exec(select(AppSetting)).all()}


def runtime_settings(session: Session) -> Settings:
    """Effective settings = env defaults overlaid with DB overrides."""
    return effective_settings(load_setting_overrides(session))
