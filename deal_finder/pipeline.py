"""The core scan pipeline: run a single watch once.

``run_watch`` is synchronous (adapters, DB, SMTP are all blocking). Async callers
(the scheduler and API) invoke it via ``asyncio.to_thread`` so nothing blocks the loop.

Modes (controlled by flags):
  * Scheduled scan:  send_email=True,  ignore_seen=False  -> seeds on first run, then
    emails genuinely-new listings and records them as seen.
  * Preview search:  send_email=False, ignore_seen=True   -> returns matches, no email,
    no DB writes (the UI "test search" button).
  * Test email:      send_email=True,  ignore_seen=True    -> emails matches treating all
    as new, no DB writes (verify email formatting/SMTP).
  * Dry run:         dry_run=True                          -> opens every match in a new
    local browser tab instead of emailing. No AI enrichment, no DB writes, ever (dry_run
    always behaves as a pure preview regardless of ignore_seen/send_email).
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, select

from .adapters.base import AdapterError, Listing
from .ai import Enrichment, OllamaClient, enrich_listing
from .config import Settings
from .db import runtime_settings
from .matching import dedup_cross_marketplace, passes_filters
from .models import NotificationLog, SeenListing, Watch, utcnow
# Aliased so the `send_email: bool` parameter of run_watch can't shadow the function.
from .notify import EmailMatch, open_listings, render_email
from .notify import send_email as send_match_email
from .registry import get_adapter, get_category

log = logging.getLogger("deal_finder.pipeline")


def listing_to_dict(li: Listing) -> dict[str, Any]:
    return {
        "marketplace": li.marketplace,
        "external_id": li.external_id,
        "url": li.url,
        "title": li.title,
        "price": li.price,
        "currency": li.currency,
        "location": li.location,
        "year": li.attributes.get("year"),
        "mileage_km": li.attributes.get("mileage_km"),
        "posted_at": li.posted_at.isoformat() if li.posted_at else None,
    }


@dataclass
class RunResult:
    watch_id: int | None
    found: int = 0
    matched: int = 0
    new: int = 0
    notified: int = 0
    seeded: bool = False
    emailed: bool = False
    dry_run: bool = False
    opened: int = 0
    adapter_status: dict[str, str] = field(default_factory=dict)
    matches_preview: list[dict] = field(default_factory=list)
    error: str | None = None


def _adapter_enabled(key: str, settings: Settings) -> bool:
    """Per-adapter global enable flag (a watch may select an adapter that's globally off)."""
    return {
        "ricardo": settings.adapter_ricardo_enabled,
        "autoscout24": settings.adapter_autoscout24_enabled,
        "facebook": settings.adapter_facebook_enabled,
    }.get(key, True)


@contextlib.contextmanager
def _browser_session(settings: Settings):
    """Yield a live BrowserSession, or None if Playwright is missing / launch fails.
    Never raises on launch failure, so httpx/demo adapters still run."""
    try:
        from ..browser import BrowserConfig, BrowserSession, is_available
    except Exception:  # noqa: BLE001
        yield None
        return
    if not is_available():
        yield None
        return
    try:
        session = BrowserSession(BrowserConfig.from_settings(settings))
    except Exception:  # noqa: BLE001
        yield None
        return
    entered = False
    try:
        session.__enter__()
        entered = True
        yield session
    except Exception as exc:  # noqa: BLE001 - launch failure -> browser adapters degrade
        log.warning("browser session unavailable: %s", exc)
        if not entered:
            yield None
    finally:
        if entered:
            try:
                session.__exit__(None, None, None)
            except Exception:  # noqa: BLE001
                pass


def _collect_listings(watch: Watch, query, category, result: RunResult, settings: Settings) -> list[Listing]:
    # Resolve which selected adapters actually run (known, supports category, enabled).
    plan: list[tuple[str, object]] = []
    for key in watch.marketplaces or []:
        adapter = get_adapter(key)
        if adapter is None:
            result.adapter_status[key] = "unknown adapter"
        elif watch.category not in adapter.supported_categories:
            result.adapter_status[key] = f"does not support category '{watch.category}'"
        elif not _adapter_enabled(key, settings):
            result.adapter_status[key] = "disabled in settings"
        else:
            plan.append((key, adapter))

    def _needs_browser(a) -> bool:
        fn = getattr(a, "needs_browser", None)
        return fn(settings) if callable(fn) else getattr(a, "requires_browser", False)

    needs_browser = any(_needs_browser(a) for _, a in plan)
    listings: list[Listing] = []

    with _browser_session(settings) if needs_browser else contextlib.nullcontext(None) as browser:
        for key, adapter in plan:
            try:
                if getattr(adapter, "requires_browser", False):
                    found = list(adapter.search(query, browser=browser, settings=settings))
                else:
                    found = list(adapter.search(query))
                listings.extend(found)
                result.adapter_status[key] = f"ok ({len(found)})"
            except AdapterError as exc:
                result.adapter_status[key] = f"error: {exc}"
                log.warning("adapter %s failed for watch %s: %s", key, watch.id, exc)
            except Exception as exc:  # noqa: BLE001 - never let one adapter abort the run
                result.adapter_status[key] = f"error: {exc!r}"
                log.exception("adapter %s crashed for watch %s", key, watch.id)
    return listings


def run_watch(
    session: Session,
    watch: Watch,
    *,
    settings: Settings | None = None,
    send_email: bool = True,
    ignore_seen: bool = False,
    dry_run: bool = False,
    ai_client: OllamaClient | None = None,
) -> RunResult:
    settings = settings or runtime_settings(session)
    result = RunResult(watch_id=watch.id)
    # dry_run is always a pure preview: never write to the DB, regardless of ignore_seen.
    record = (not ignore_seen) and not dry_run

    category = get_category(watch.category)
    if category is None:
        result.error = f"unknown category '{watch.category}'"
        return result

    query = category.build_query(watch)
    listings = _collect_listings(watch, query, category, result, settings)
    result.found = len(listings)

    matched = [li for li in listings if passes_filters(li, query, category, watch)]
    matched = dedup_cross_marketplace(matched)
    result.matched = len(matched)
    result.matches_preview = [listing_to_dict(li) for li in matched[:50]]

    # Which are new (not previously seen for this watch)?
    if ignore_seen:
        new = matched
    else:
        seen_keys = {
            (row.marketplace, row.external_id)
            for row in session.exec(
                select(SeenListing).where(SeenListing.watch_id == watch.id)
            ).all()
        }
        new = [li for li in matched if (li.marketplace, li.external_id) not in seen_keys]
    result.new = len(new)

    # Seeding run: record existing matches as seen, do not email.
    is_seed = record and settings.seed_mode and not watch.seed_done
    if is_seed:
        for li in matched:
            _record_seen(session, watch, li, notified=False)
        watch.seed_done = True
        result.seeded = True
        _finish(session, watch, "seeded", record)
        return result

    if record and not watch.seed_done:
        watch.seed_done = True  # seed mode off, but mark seeded so future runs are normal

    new = new[: settings.max_results_per_run]
    if not new:
        _finish(session, watch, "ok (no new matches)", record)
        return result

    if dry_run:
        # No AI enrichment, no email, no DB writes -- just pop each match open for a look.
        opened = open_listings([li.url for li in new])
        result.dry_run = True
        result.opened = opened
        _finish(session, watch, f"dry run: opened {opened} tab(s)", record)
        return result

    if not send_email:
        result.new = len(new)
        _finish(session, watch, "preview", record=False)
        return result

    # Enrich + email.
    email_matches = [
        EmailMatch(
            listing=li,
            enrichment=_safe_enrich(settings, li, watch.questions, ai_client),
            questions=watch.questions or [],
        )
        for li in new
    ]
    subject, html = render_email(watch, email_matches)
    try:
        send_match_email(settings, watch.notify_email, subject, html)
        result.emailed = True
        result.notified = len(email_matches)
        if record:
            for li in new:
                _record_seen(session, watch, li, notified=True)
            _log_notification(session, watch, subject, len(email_matches), True, None)
        _finish(session, watch, f"emailed {len(email_matches)}", record)
    except Exception as exc:  # noqa: BLE001 - EmailNotConfigured, SMTP/OS errors, etc.
        result.error = f"email failed: {exc}"
        log.warning("email failed for watch %s: %s", watch.id, exc)
        if record:
            # Do NOT mark as seen -> retried on the next run.
            _log_notification(session, watch, subject, len(email_matches), False, str(exc))
        _finish(session, watch, result.error, record)
    return result


def _safe_enrich(settings, listing, questions, ai_client) -> Enrichment:
    try:
        return enrich_listing(settings, listing, questions or [], client=ai_client)
    except Exception as exc:  # noqa: BLE001 - enrichment must never block email
        log.warning("enrichment failed for %s: %s", listing.external_id, exc)
        return Enrichment(
            answers={q: "not stated" for q in (questions or [])},
            note=f"enrichment error: {exc}",
        )


def _record_seen(session: Session, watch: Watch, li: Listing, *, notified: bool) -> None:
    row = SeenListing(
        watch_id=watch.id,
        marketplace=li.marketplace,
        external_id=li.external_id,
        content_hash=li.content_hash(),
        url=li.url,
        title=li.title,
        price=li.price,
        notified=notified,
        notified_at=utcnow() if notified else None,
    )
    session.add(row)


def _log_notification(session, watch, subject, n, success, error) -> None:
    session.add(
        NotificationLog(
            watch_id=watch.id,
            email_to=watch.notify_email,
            subject=subject,
            num_matches=n,
            success=success,
            error=error,
        )
    )


def _finish(session: Session, watch: Watch, status: str, record: bool) -> None:
    if record:
        watch.last_run_at = utcnow()
        watch.last_run_status = status
        session.add(watch)
        session.commit()
