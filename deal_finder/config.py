"""Application configuration.

Defaults live here and can be overridden by environment variables (prefix ``DF_``)
or a ``.env`` file. A subset of keys is also editable at runtime through the web UI;
those overrides are stored in the database and merged on top of these defaults by
:func:`effective_settings`.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Stable, absolute default DB location (co-located with browser profiles) so watches and
# saved credentials persist regardless of the process working directory. A relative path
# would resolve against CWD and differ between `pipenv run` and launchd, losing data.
_DEFAULT_DB_PATH = Path.home() / ".deal_finder" / "deal_finder.db"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="DF_", env_file=".env", extra="ignore")

    # --- Database ---
    database_url: str = f"sqlite:///{_DEFAULT_DB_PATH}"

    # --- Email (SMTP) ---
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    smtp_starttls: bool = True

    # --- Local AI (Ollama, OpenAI-compatible API) ---
    ai_enabled: bool = True
    ollama_base_url: str = "http://localhost:11434/v1"
    ollama_model: str = "gemma4:12b"
    ollama_timeout: float = 120.0
    ai_translate_to: str = "English"  # target language for the description translation step

    # --- Telegram (optional alternative to email; per-watch choice) ---
    telegram_bot_token: str = ""          # from @BotFather
    telegram_default_chat_id: str = ""    # prefills new watches, like default_notify_email

    # --- Behaviour ---
    default_notify_email: str = ""
    max_results_per_run: int = 50
    seed_mode: bool = True
    http_user_agent: str = "DealFinder/0.1 (personal monitoring)"
    request_timeout: float = 20.0

    # --- Scraping limits (shared by every adapter) ---
    # Kept the "browser_" prefix for backward compatibility with existing stored DB
    # overrides, even though no adapter drives its own browser session anymore -- each
    # wraps a dedicated PyPI package (tutti-scraper, ricardo-scraper, autoscout24-scraper,
    # facebook-marketplace-scraper) that manages its own access internally.
    browser_max_items_per_run: int = 15

    # --- Per-adapter enable (which marketplaces a run may touch) ---
    adapter_tutti_enabled: bool = True        # plain public GraphQL API; no browser involved
    adapter_ricardo_enabled: bool = True
    adapter_autoscout24_enabled: bool = True  # plain public JSON API; no browser involved
    adapter_autolina_enabled: bool = True     # plain HTML parsing; no browser involved
    adapter_facebook_enabled: bool = True      # user chose on-by-default (ToS/ban risk!)

    # --- Facebook credentials (optional auto-login fallback) ---
    # Preferred is a one-time MANUAL login via `python -m deal_finder.browser.fb_login`,
    # which persists the session cookie so no password is stored. These are only used as a
    # fallback auto-login and carry security + ban-risk tradeoffs.
    facebook_email: str = ""
    facebook_password: str = ""


# Keys the user may override live from the Settings page (stored in the DB).
EDITABLE_KEYS: tuple[str, ...] = (
    "smtp_host",
    "smtp_port",
    "smtp_user",
    "smtp_password",
    "smtp_from",
    "smtp_starttls",
    "ai_enabled",
    "ollama_base_url",
    "ollama_model",
    "ollama_timeout",
    "ai_translate_to",
    "telegram_bot_token",
    "telegram_default_chat_id",
    "default_notify_email",
    "seed_mode",
    "max_results_per_run",
    "browser_max_items_per_run",
    # adapters
    "adapter_tutti_enabled",
    "adapter_ricardo_enabled",
    "adapter_autoscout24_enabled",
    "adapter_autolina_enabled",
    "adapter_facebook_enabled",
    # facebook credentials
    "facebook_email",
    "facebook_password",
)


@lru_cache
def get_settings() -> Settings:
    """Environment/.env-based settings (cached)."""
    return Settings()


def effective_settings(db_overrides: dict[str, str] | None = None) -> Settings:
    """Return settings with database overrides layered on top of the env defaults.

    ``db_overrides`` maps editable keys to their stored string values. Empty strings
    are ignored so a blank override falls back to the env default. Explicit kwargs
    passed to ``Settings(...)`` take priority over env/.env in pydantic-settings.
    """
    base = get_settings()
    if not db_overrides:
        return base
    data = base.model_dump()
    for key, value in db_overrides.items():
        if key in EDITABLE_KEYS and value not in (None, ""):
            data[key] = value  # pydantic coerces str -> bool/int/float on construction
    return Settings(**data)
