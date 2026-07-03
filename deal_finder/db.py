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


def init_db() -> None:
    """Create tables if they don't exist. Import models for side effects first."""
    from . import models  # noqa: F401  (ensure model classes are registered)

    SQLModel.metadata.create_all(get_engine())


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
