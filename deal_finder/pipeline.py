"""The core scan pipeline: run a single watch once.

``run_watch`` is synchronous (adapters, DB, SMTP/Telegram are all blocking). Async callers
(the scheduler and API) invoke it via ``asyncio.to_thread`` so nothing blocks the loop.

Modes (controlled by flags):
  * Scheduled scan:  notify=True,  ignore_seen=False  -> seeds on first run, then notifies
    (via the watch's chosen channel) genuinely-new listings and records them as seen.
  * Preview search:  notify=False, ignore_seen=True   -> returns matches, no notification,
    no DB writes (the UI "test search" button).
  * Test send:       notify=True,  ignore_seen=True    -> notifies matches treating all as
    new, no DB writes (verify email/Telegram formatting and delivery).
  * Dry run:         dry_run=True                     -> opens every match in a new local
    browser tab instead of notifying, REGARDLESS of the watch's channel. No AI enrichment,
    no DB writes, ever (dry_run always behaves as a pure preview regardless of
    ignore_seen/notify).

``notify`` was named ``send_email`` before Telegram support was added; the query
param/form field at the HTTP layer keeps that name for compatibility (see web/api.py,
web/routes.py) and is simply mapped to ``notify=`` when calling into this module.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from sqlmodel import Session, select

from . import progress
from .adapters.base import AdapterError, Listing
from .ai import Enrichment, OllamaClient, enrich_listing
from .config import Settings
from .db import runtime_settings
from .matching import dedup_cross_marketplace, passes_filters
from .models import NotificationLog, SeenListing, Watch, utcnow
from .notify import EmailMatch, TelegramMatch, open_listings, render_email
from .notify import send_email as send_match_email
from .notify import send_telegram_match
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
    emailed: bool = False  # kept name for compatibility; means "notified via any channel"
    channel: str | None = None  # "email" | "telegram", set once a send is attempted
    dry_run: bool = False
    opened: int = 0
    adapter_status: dict[str, str] = field(default_factory=dict)
    matches_preview: list[dict] = field(default_factory=list)
    error: str | None = None


def _adapter_enabled(key: str, settings: Settings) -> bool:
    """Per-adapter global enable flag (a watch may select an adapter that's globally off)."""
    return {
        "tutti": settings.adapter_tutti_enabled,
        "ricardo": settings.adapter_ricardo_enabled,
        "autoscout24": settings.adapter_autoscout24_enabled,
        "facebook": settings.adapter_facebook_enabled,
    }.get(key, True)


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

    listings: list[Listing] = []
    for key, adapter in plan:
        progress.set_status(watch.id, f"Searching {getattr(adapter, 'label', key)}…")
        try:
            found = list(adapter.search(query))
            listings.extend(found)
            result.adapter_status[key] = f"ok ({len(found)})"
        except AdapterError as exc:
            # A partial-run error (e.g. a bot-wall hit partway through) may carry
            # whatever listings the adapter already fetched successfully before failing
            # (see AdapterError.partial_listings) -- keep those rather than discarding a
            # run's worth of successful work over one later failure.
            partial = getattr(exc, "partial_listings", None)
            if partial:
                listings.extend(partial)
                result.adapter_status[key] = f"partial ({len(partial)}): {exc}"
            else:
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
    notify: bool = True,
    ignore_seen: bool = False,
    dry_run: bool = False,
    ai_client: OllamaClient | None = None,
) -> RunResult:
    """Thin wrapper around _run_watch that guarantees the live status shown to the web UI
    (see progress.py) is cleared once the run ends, however it ends."""
    try:
        return _run_watch(
            session, watch, settings=settings, notify=notify,
            ignore_seen=ignore_seen, dry_run=dry_run, ai_client=ai_client,
        )
    finally:
        progress.clear_status(watch.id)


def _run_watch(
    session: Session,
    watch: Watch,
    *,
    settings: Settings | None = None,
    notify: bool = True,
    ignore_seen: bool = False,
    dry_run: bool = False,
    ai_client: OllamaClient | None = None,
) -> RunResult:
    settings = settings or runtime_settings(session)
    result = RunResult(watch_id=watch.id)
    # dry_run is always a pure preview: never write to the DB, regardless of ignore_seen.
    record = (not ignore_seen) and not dry_run

    progress.set_status(watch.id, "Starting run…")
    category = get_category(watch.category)
    if category is None:
        result.error = f"unknown category '{watch.category}'"
        return result

    query = category.build_query(watch)
    listings = _collect_listings(watch, query, category, result, settings)
    result.found = len(listings)

    progress.set_status(watch.id, f"Found {result.found} listing(s); filtering & deduping…")
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
        progress.set_status(watch.id, f"First run: recording {len(matched)} existing listing(s) as seen…")
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
        progress.set_status(watch.id, "No new matches.")
        _finish(session, watch, "ok (no new matches)", record)
        return result

    if dry_run:
        # No AI enrichment, no email, no DB writes -- just pop each match open for a look.
        progress.set_status(watch.id, f"Dry run: opening {len(new)} listing(s) in your browser…")
        opened = open_listings([li.url for li in new])
        result.dry_run = True
        result.opened = opened
        _finish(session, watch, f"dry run: opened {opened} tab(s)", record)
        return result

    if not notify:
        result.new = len(new)
        progress.set_status(watch.id, f"Preview: found {len(new)} new match(es).")
        _finish(session, watch, "preview", record=False)
        return result

    channel = watch.notify_channel or "email"
    result.channel = channel
    if channel == "telegram":
        _send_via_telegram(session, watch, new, settings, ai_client, result, record)
    else:
        _send_via_email(session, watch, new, settings, ai_client, result, record)
    return result


def _send_via_email(session, watch, new, settings, ai_client, result, record) -> None:
    """One HTML email covering the whole batch -- all-or-nothing per run, same as before
    Telegram support existed."""
    progress.set_status(watch.id, f"Enriching {len(new)} listing(s) with AI…")
    email_matches = [
        EmailMatch(
            listing=li,
            enrichment=_safe_enrich(settings, li, watch.questions, ai_client),
            questions=watch.questions or [],
        )
        for li in new
    ]
    subject, html = render_email(watch, email_matches)
    progress.set_status(watch.id, "Sending email…")
    try:
        send_match_email(settings, watch.notify_email, subject, html)
        result.emailed = True
        result.notified = len(email_matches)
        if record:
            for li in new:
                _record_seen(session, watch, li, notified=True)
            _log_notification(session, watch, subject, len(email_matches), True, None, channel="email")
        _finish(session, watch, f"emailed {len(email_matches)}", record)
    except Exception as exc:  # noqa: BLE001 - EmailNotConfigured, SMTP/OS errors, etc.
        result.error = f"email failed: {exc}"
        log.warning("email failed for watch %s: %s", watch.id, exc)
        if record:
            # Do NOT mark as seen -> retried on the next run.
            _log_notification(session, watch, subject, len(email_matches), False, str(exc), channel="email")
        _finish(session, watch, result.error, record)


def _send_via_telegram(session, watch, new, settings, ai_client, result, record) -> None:
    """One Telegram message PER LISTING (unlike email's single batch document). Each
    listing is marked seen as soon as its own send succeeds, so a mid-batch failure never
    causes already-delivered listings to be re-sent on retry. Stops at the first failure
    (almost always systemic -- bad token/chat id, rate limit -- not per-listing; the one
    per-listing failure mode, an unfetchable photo, is already absorbed inside
    send_telegram_match's own photo->text fallback) and leaves that listing plus every
    remaining one unseen, to be retried on the next scheduled run."""
    sent = 0
    error: str | None = None
    for idx, li in enumerate(new, start=1):
        progress.set_status(watch.id, f"Enriching & sending listing {idx}/{len(new)} via Telegram…")
        match = TelegramMatch(
            listing=li,
            enrichment=_safe_enrich(settings, li, watch.questions, ai_client),
            questions=watch.questions or [],
        )
        try:
            send_telegram_match(settings, watch.telegram_chat_id, match)
        except Exception as exc:  # noqa: BLE001 - TelegramNotConfigured, TelegramApiError, etc.
            error = str(exc)
            log.warning("telegram send failed for watch %s: %s", watch.id, exc)
            break
        sent += 1
        if record:
            _record_seen(session, watch, li, notified=True)

    result.emailed = sent > 0
    result.notified = sent
    if error:
        result.error = f"telegram failed: {error}"
    if record:
        _log_notification(
            session, watch, f"Telegram: {sent} match(es)", sent, error is None, error,
            channel="telegram", recipient=watch.telegram_chat_id,
        )
    status = f"sent via telegram ({sent})" if error is None else result.error
    _finish(session, watch, status, record)


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


def _log_notification(
    session, watch, subject, n, success, error, *, channel: str = "email", recipient: str | None = None
) -> None:
    session.add(
        NotificationLog(
            watch_id=watch.id,
            # `email_to` holds the recipient regardless of channel (an email address or a
            # Telegram chat ID) -- kept unrenamed to avoid a RENAME COLUMN migration.
            email_to=recipient if recipient is not None else watch.notify_email,
            channel=channel,
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
