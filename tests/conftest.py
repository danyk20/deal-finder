from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _no_ambient_secrets(monkeypatch):
    """Keep tests hermetic: a real ./.env must never leak secrets into Settings() or
    cause a test to hit a live API. Empty env vars override .env values."""
    for key in ("DF_SMTP_PASSWORD", "DF_FACEBOOK_PASSWORD", "DF_FACEBOOK_EMAIL"):
        monkeypatch.setenv(key, "")
    from deal_finder import config

    config.get_settings.cache_clear()
    yield
    config.get_settings.cache_clear()


def _reset(monkeypatch, tmp_path, db_name: str):
    monkeypatch.setenv("DF_DATABASE_URL", f"sqlite:///{tmp_path / db_name}")
    monkeypatch.setenv("DF_AI_ENABLED", "false")
    monkeypatch.setenv("DF_SEED_MODE", "false")
    import deal_finder.config as config
    import deal_finder.db as db
    import deal_finder.scheduler as scheduler

    config.get_settings.cache_clear()
    db._engine = None
    scheduler._scheduler = None
    return config, db, scheduler


@pytest.fixture
def session(tmp_path, monkeypatch):
    config, db, _ = _reset(monkeypatch, tmp_path, "test.db")
    db.init_db()
    with db.session_scope() as s:
        yield s
    config.get_settings.cache_clear()
    db._engine = None


@pytest.fixture
def client(tmp_path, monkeypatch):
    config, db, scheduler = _reset(monkeypatch, tmp_path, "api.db")
    from fastapi.testclient import TestClient

    from deal_finder.main import app

    with TestClient(app) as c:
        yield c
    config.get_settings.cache_clear()
    db._engine = None
    scheduler._scheduler = None
