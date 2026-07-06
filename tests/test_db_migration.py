"""Regression test: _migrate_schema() must upgrade a pre-existing (old-schema) SQLite
database in place without touching existing rows, since real users already have a
database from before notify_channel/telegram_chat_id/channel existed."""

from __future__ import annotations

from sqlalchemy import create_engine

from deal_finder.db import _migrate_schema


def _old_schema_engine(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE watch (
                id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
                active BOOLEAN NOT NULL, notify_email TEXT NOT NULL
            )
            """
        )
        conn.exec_driver_sql(
            """
            CREATE TABLE notificationlog (
                id INTEGER PRIMARY KEY, watch_id INTEGER NOT NULL, email_to TEXT NOT NULL,
                subject TEXT NOT NULL, success BOOLEAN NOT NULL
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO watch (id, name, category, active, notify_email) "
            "VALUES (1, 'old watch', 'car', 1, 'me@x.com')"
        )
        conn.exec_driver_sql(
            "INSERT INTO notificationlog (id, watch_id, email_to, subject, success) "
            "VALUES (1, 1, 'me@x.com', 'old subject', 1)"
        )
    return engine


def _columns(engine, table):
    with engine.connect() as conn:
        return {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}


def test_migration_adds_missing_columns_with_backward_compatible_defaults(tmp_path):
    engine = _old_schema_engine(tmp_path)
    assert "notify_channel" not in _columns(engine, "watch")
    assert "channel" not in _columns(engine, "notificationlog")

    _migrate_schema(engine)

    assert {"notify_channel", "telegram_chat_id"} <= _columns(engine, "watch")
    assert "channel" in _columns(engine, "notificationlog")

    with engine.connect() as conn:
        watch_row = conn.exec_driver_sql(
            "SELECT name, notify_email, notify_channel, telegram_chat_id FROM watch WHERE id=1"
        ).fetchone()
        log_row = conn.exec_driver_sql(
            "SELECT subject, email_to, channel FROM notificationlog WHERE id=1"
        ).fetchone()

    # Existing data is untouched; new columns default to values matching pre-feature behavior.
    assert watch_row == ("old watch", "me@x.com", "email", "")
    assert log_row == ("old subject", "me@x.com", "email")


def test_migration_is_idempotent(tmp_path):
    engine = _old_schema_engine(tmp_path)
    _migrate_schema(engine)
    _migrate_schema(engine)  # must not raise ("duplicate column name" etc.)
    assert {"notify_channel", "telegram_chat_id"} <= _columns(engine, "watch")


def test_migration_is_a_noop_on_a_fresh_database(tmp_path, monkeypatch):
    """init_db() on a brand-new DB already creates every column via create_all(); the
    migration must find nothing to do and must not error."""
    import deal_finder.config as config_module
    import deal_finder.db as db_module

    monkeypatch.setenv("DF_DATABASE_URL", f"sqlite:///{tmp_path / 'fresh.db'}")
    config_module.get_settings.cache_clear()
    db_module._engine = None
    try:
        db_module.init_db()
        engine = db_module.get_engine()
        assert {"notify_channel", "telegram_chat_id"} <= _columns(engine, "watch")
        assert "channel" in _columns(engine, "notificationlog")
    finally:
        config_module.get_settings.cache_clear()
        db_module._engine = None
