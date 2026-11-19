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

Every marketplace is searched at the same time, and the AI works through the listings
while the slower marketplaces are still scraping -- see ``_Run``.
"""

from __future__ import annotations

import logging
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sqlmodel import Session, select

from . import progress, site_categories
from .adapters.base import AdapterError, Listing
from .ai import Enrichment, OllamaClient, enrich_listing
from .categories.fields import non_negotiables
from .config import Settings
from .db import runtime_settings
from .matching import dedup_key, filter_rejection_reason, non_negotiables_rejection_reason
from .models import NotificationLog, SeenListing, Watch, utcnow
from .notify import EmailMatch, TelegramMatch, open_listings, render_email
from .notify import send_email as send_match_email
from .notify import send_telegram_match
from .registry import adapter_for_url, get_adapter, get_category
from .same_car import CarFacts, car_facts, compare, same_photos

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
        "via": li.via,
    }


def _label(marketplace: str) -> str:
    adapter = get_adapter(marketplace)
    return adapter.label if adapter else marketplace


def _found_by(li: Listing) -> str:
    """Who found a listing, for "already found on ..." reasons: "AutoScout24.ch", or
    "AutoScout24.ch via AutoUncle.ch" for one fetched from an aggregator's link."""
    label = _label(li.marketplace)
    return f"{label} via {li.via}" if li.via else label


def _agreeing(facts: list[str]) -> str:
    """"same year, mileage, price, postcode, first registration, colour" -- the reason
    then ends in " and photos"."""
    return "same " + ", ".join(["year", "mileage", "price", *facts])


@dataclass
class RunResult:
    watch_id: int | None
    started_at: datetime | None = None  # naive UTC (see models.utcnow), set as the run begins
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
    rejected_preview: list[dict] = field(default_factory=list)
    error: str | None = None


def _adapter_enabled(key: str, settings: Settings) -> bool:
    """Per-adapter global enable flag (a watch may select an adapter that's globally off)."""
    return {
        "tutti": settings.adapter_tutti_enabled,
        "ricardo": settings.adapter_ricardo_enabled,
        "autoscout24": settings.adapter_autoscout24_enabled,
        "autolina": settings.adapter_autolina_enabled,
        "autouncle": settings.adapter_autouncle_enabled,
        "facebook": settings.adapter_facebook_enabled,
    }.get(key, True)


def _search_isolated(adapter, query, settings: Settings) -> list[Listing]:
    """Run one adapter's search() in a fresh, throwaway OS thread.

    Ricardo and Facebook each drive their own Playwright sync-API session (Camoufox /
    Chromium respectively). Playwright's sync API tracks "is an event loop running on
    this thread?" per-thread; if one adapter's browser session doesn't shut down
    cleanly (observed after Ricardo hits a Cloudflare-challenge page-load timeout), that
    can leave the *calling* thread's asyncio state looking like a loop is still running,
    and the next adapter's own sync_playwright() call on that same thread then fails
    with "Playwright Sync API inside the asyncio loop" -- even though nothing here
    actually uses asyncio. The run searches every marketplace at once from a thread pool
    whose threads could in principle be reused, so one adapter's cleanup bug could
    corrupt whichever adapter ran on that thread next. A dedicated ThreadPoolExecutor per
    call guarantees each adapter gets a brand new thread (and therefore clean asyncio
    thread-local state), joined and discarded once its search ends.
    """
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: list(adapter.search(query, settings))).result()


def _plan(watch: Watch, result: RunResult, settings: Settings) -> list[tuple[str, object]]:
    """The watch's selected adapters that actually run (known, support the watch's
    category, enabled); the others get their reason in ``result.adapter_status``."""
    plan: list[tuple[str, object]] = []
    for key in watch.marketplaces or []:
        adapter = get_adapter(key)
        if adapter is None:
            result.adapter_status[key] = "unknown adapter"
        elif watch.category not in adapter.supported_categories:
            label = getattr(get_category(watch.category), "label", watch.category)
            result.adapter_status[key] = f"doesn't support {label} watches"
        elif not _adapter_enabled(key, settings):
            result.adapter_status[key] = "disabled in settings"
        else:
            plan.append((key, adapter))
    return plan


@dataclass
class _Found:
    """A listing plus where it came from -- (position of its marketplace in the watch,
    position in that marketplace's results) -- so results can be put back in a stable
    order however the concurrent searches and AI requests happened to finish."""

    order: tuple[int, int]
    listing: Listing


def _in_order(found: list[_Found]) -> list[Listing]:
    return [f.listing for f in sorted(found, key=lambda f: f.order)]


class _Run:
    """The concurrent part of a run. Every marketplace is searched at once, and each one's
    listings go into one shared pool of AI work as soon as that marketplace finishes -- so
    the AI checks tutti's listings while Ricardo and Facebook are still scraping. The pool
    sends ``settings.ai_parallel_requests`` requests at once (default 1: one by one) --
    non-negotiables checks and, for each new match about to be notified, translation +
    Q&A. A Telegram match is sent the moment it's ready; email matches wait
    for the one batch email at the end.

    Only the calling thread touches the Watch (and nothing here touches the DB session);
    worker threads get plain values and hand results back through futures.

    A listing found on several marketplaces is still kept once, but now as whichever copy
    passed its checks first rather than the one from the marketplace listed first.

    An aggregator's listing (AutoUncle) of an ad on another portal is skipped when that ad
    was already seen on this watch or another marketplace found it this run; else that
    portal's own adapter fetches it (from a small pool of its own, so it doesn't wait
    behind the AI -- that pool also compares photos, see below), and it's checked and shown as
    that portal's listing -- see _from_aggregator.

    A dealer's ad of a used car that another of their ads (on another portal) stands for
    already is skipped too, but only when that's certain -- same facts and same photos:
    see _possible_copy_of and same_car.py.
    """

    def __init__(self, watch, query, category, settings, ai_client, result, *, seen_keys, seen_cars=(), deliver):
        self.watch = watch
        self.watch_id = watch.id
        self.query = query
        self.category = category
        self.settings = settings
        self.ai_client = ai_client
        self.result = result
        self.seen_keys = seen_keys
        # (CarFacts, label of the marketplace it was seen on) of every used car seen before.
        self.seen_cars: list[tuple[CarFacts, str]] = list(seen_cars)
        # Translate/answer (and on Telegram, send) new matches during the run -- off for
        # seeding, dry runs and previews, which never notify.
        self.deliver = deliver
        self.telegram = (watch.notify_channel or "email") == "telegram"
        self.chat_id = watch.telegram_chat_id
        self.questions = list(watch.questions or [])
        self.requirements = non_negotiables(watch.filters or {}) if settings.ai_enabled else []

        self.matched: list[_Found] = []
        self.rejected: list[tuple[_Found, str]] = []
        self.new: list[_Found] = []
        self.delivering: list[_Found] = []  # new matches being enriched/sent, at most max_results_per_run
        self.enriched: list[tuple[_Found, Enrichment]] = []  # email: ready for the batch email
        self.sent: list[_Found] = []  # Telegram: delivered
        self.send_error: str | None = None
        self._match_keys: set = set()
        # (marketplace, external_id) of every listing taken this run -> who found it
        # ("AutoScout24.ch", "AutoScout24.ch via AutoUncle.ch"), for "already found" reasons.
        self._found_keys: dict[tuple[str, str], str] = {}
        # (CarFacts, who found it) of every used car taken this run that passed the free checks.
        self._found_cars: list[tuple[CarFacts, str]] = []

        self._pending: dict[Future, tuple] = {}  # future -> (handler, *handler args)
        self._ai: ThreadPoolExecutor | None = None
        # Fetches aggregators' original ads, and compares photos of possible copies of a car.
        self._downloads: ThreadPoolExecutor | None = None
        self._fingerprints: dict[str, int | None] = {}  # photo link -> same_car.fingerprint
        # Live status, also updated from the worker threads.
        self._lock = threading.Lock()
        self._searching: dict[str, str] = {}
        self._checks_queued = 0
        self._checks_done = 0
        self._activity = ""

    def run(self, plan: list[tuple[str, object]]) -> None:
        searches = ThreadPoolExecutor(max_workers=max(1, len(plan)), thread_name_prefix=f"search-{self.watch_id}")
        self._ai = ThreadPoolExecutor(
            max_workers=max(1, self.settings.ai_parallel_requests), thread_name_prefix=f"ai-{self.watch_id}"
        )
        self._downloads = ThreadPoolExecutor(max_workers=2, thread_name_prefix=f"downloads-{self.watch_id}")
        try:
            for pos, (key, adapter) in enumerate(plan):
                with self._lock:
                    self._searching[key] = getattr(adapter, "label", key)
                future = searches.submit(_search_isolated, adapter, self.query, self.settings)
                self._pending[future] = (self._searched, pos, key)
            self._publish()
            while self._pending:
                done, _ = wait(self._pending, return_when=FIRST_COMPLETED)
                for future in done:
                    handler, *args = self._pending.pop(future)
                    if not future.cancelled():
                        handler(future, *args)
                self._publish()
        finally:
            # Everything is done by now, unless a handler raised: then don't wait for the rest.
            searches.shutdown(wait=False, cancel_futures=True)
            self._ai.shutdown(wait=False, cancel_futures=True)
            self._downloads.shutdown(wait=False, cancel_futures=True)

    # --- handlers (calling thread) ---

    def _searched(self, future: Future, pos: int, key: str) -> None:
        with self._lock:
            self._searching.pop(key, None)
        try:
            found = future.result()
            self.result.adapter_status[key] = f"ok ({len(found)})"
        except AdapterError as exc:
            # A partial-run error (e.g. a bot-wall hit partway through) may carry
            # whatever listings the adapter already fetched successfully before failing
            # (see AdapterError.partial_listings) -- keep those rather than discarding a
            # run's worth of successful work over one later failure.
            found = list(getattr(exc, "partial_listings", None) or [])
            if found:
                self.result.adapter_status[key] = f"partial ({len(found)}): {exc}"
            else:
                self.result.adapter_status[key] = f"error: {exc}"
            log.warning("adapter %s failed for watch %s: %s", key, self.watch_id, exc)
        except Exception as exc:  # noqa: BLE001 - never let one adapter abort the run
            found = []
            self.result.adapter_status[key] = f"error: {exc!r}"
            log.exception("adapter %s crashed for watch %s", key, self.watch_id)
        self.result.found += len(found)

        for i, li in enumerate(found):
            item = _Found((pos, i), li)
            if li.original_url:
                self._from_aggregator(item)
            else:
                self._take(item)

    def _take(self, item: _Found) -> None:
        """A listing for this run, unless another copy of the same ad was taken already, or
        it's certainly another ad of the same car (see _possible_copy_of). The free checks
        right away (no settings -> no AI); only the AI check is queued."""
        li = item.listing
        key = (li.marketplace, li.external_id)
        if key in self._found_keys:
            self.rejected.append((item, f"already found on {self._found_keys[key]} in this run"))
            return
        self._found_keys[key] = _found_by(li)
        reason = filter_rejection_reason(li, self.query, self.category, self.watch)
        if reason is not None:
            self.rejected.append((item, reason))
            return
        car = car_facts(li)
        candidates = self._possible_copy_of(li, car) if car is not None else []
        if candidates:
            future = self._downloads.submit(self._copy_by_photos, car, candidates)
            self._pending[future] = (self._compared_photos, item, car)
        else:
            self._passed(item, car)

    def _passed(self, item: _Found, car: CarFacts | None) -> None:
        """Through the free checks, and no copy of a car already taken or seen: on to the AI."""
        if car is not None:
            self._found_cars.append((car, _found_by(item.listing)))
        if self.requirements:
            self._checks_queued += 1
            self._submit(self._check, item, then=self._checked)
        else:
            self._matched(item)

    def _possible_copy_of(self, li: Listing, car: CarFacts) -> list[tuple[CarFacts, str]]:
        """Used cars this one may be a copy of -- a dealer's ad of it on another portal, with
        a different ID, link and title -- by their facts (same_car.compare): one taken this
        run, or (unless this very ad was seen before) one seen on an earlier run. Each as
        (its facts, the reason to skip this ad, minus "and photos" -- if they match too).
        Only checked once a listing passes the free checks, so a copy those reject (its
        title lacks the trim, say) doesn't hide one they'd pass; and before the AI check,
        which would be wasted on a copy."""
        candidates = []
        for other, who in self._found_cars:
            same, facts = compare(car, other)
            if same:
                candidates.append((other, f"same car already found on {who} in this run ({_agreeing(facts)}"))
        if (li.marketplace, li.external_id) not in self.seen_keys:
            for other, label in self.seen_cars:
                same, facts = compare(car, other)
                if same:
                    candidates.append((other, f"same car already seen on {label} ({_agreeing(facts)}"))
        return candidates

    def _copy_by_photos(self, car: CarFacts, candidates: list[tuple[CarFacts, str]]) -> str | None:
        """(Worker thread.) The reason to skip this ad, if its photos are the same as one
        candidate's too -- else None: never skipped unless that's certain."""
        for other, reason in candidates:
            if same_photos(car, other, self._fingerprints):
                return f"{reason} and photos)"
        return None

    def _compared_photos(self, future: Future, item: _Found, car: CarFacts) -> None:
        try:
            reason = future.result()
        except Exception:  # noqa: BLE001 - can't tell: keep it
            log.exception("watch %s: comparing photos of %s failed", self.watch_id, item.listing.url)
            reason = None
        if reason is None:
            self._passed(item, car)
        else:
            self.rejected.append((item, reason))

    def _from_aggregator(self, item: _Found) -> None:
        """An aggregator's listing (AutoUncle) of an ad on another portal: skipped if that
        ad was already seen on this watch or found by another marketplace this run; else
        fetched by the portal's own adapter, if it's enabled in Settings -- even when the
        watch doesn't search that marketplace itself. Without one (or if it can't fetch
        single ads), the aggregator's own data is used, linking to the original ad."""
        li = item.listing
        portal = adapter_for_url(li.original_url)
        if portal is not None:
            adapter, ad_id = portal
            key = (adapter.key, ad_id)
            if key in self.seen_keys:
                self.rejected.append((item, f"already seen on {adapter.label}"))
                return
            if key in self._found_keys:
                self.rejected.append((item, f"already found on {self._found_keys[key]} in this run"))
                return
            if _adapter_enabled(adapter.key, self.settings):
                future = self._downloads.submit(adapter.fetch_listing, li.original_url, self.settings)
                self._pending[future] = (self._fetched_original, item, adapter)
                return
        self._take(_Found(item.order, get_adapter(li.marketplace).with_original(li, None)))

    def _fetched_original(self, future: Future, item: _Found, adapter) -> None:
        """The portal's own copy of an aggregator's listing arrived. A failed fetch keeps
        the aggregator's data. If the portal's own search found the ad meanwhile, _take
        drops this copy."""
        li = item.listing
        try:
            original = future.result()
        except Exception as exc:  # noqa: BLE001 - AdapterError or anything else: keep the aggregator's data
            log.warning("watch %s: couldn't fetch %s from %s (%s); using %s's data",
                        self.watch_id, li.original_url, adapter.label, exc, li.marketplace)
            original = None
        self._take(_Found(item.order, get_adapter(li.marketplace).with_original(li, original)))

    def _checked(self, future: Future, item: _Found) -> None:
        self._checks_done += 1
        reason = future.result()
        if reason is None:
            self._matched(item)
        else:
            self.rejected.append((item, reason))

    def _matched(self, item: _Found) -> None:
        key = dedup_key(item.listing)
        if key in self._match_keys:
            return  # the same item from another marketplace already matched
        self._match_keys.add(key)
        self.matched.append(item)
        li = item.listing
        if (li.marketplace, li.external_id) in self.seen_keys:
            return
        self.new.append(item)
        if self.deliver and self.send_error is None and len(self.delivering) < self.settings.max_results_per_run:
            self.delivering.append(item)
            self._submit(self._enrich, item, then=self._enriched)

    def _enriched(self, future: Future, item: _Found) -> None:
        enrichment = future.result()
        if not self.telegram:
            self.enriched.append((item, enrichment))
            return
        if self.send_error is not None:
            return
        match = TelegramMatch(listing=item.listing, enrichment=enrichment, questions=self.questions)
        try:
            send_telegram_match(self.settings, self.chat_id, match)
        except Exception as exc:  # noqa: BLE001 - TelegramNotConfigured, TelegramApiError, etc.
            # Almost always systemic (bad token/chat id, rate limit): stop sending, and
            # leave this match and every later one unseen for the next run to retry.
            self.send_error = str(exc)
            log.warning("telegram send failed for watch %s: %s", self.watch_id, exc)
            for pending, (handler, *_) in self._pending.items():
                if handler == self._enriched:
                    pending.cancel()
            return
        self.sent.append(item)

    # --- AI work (worker threads) ---

    def _submit(self, job, item: _Found, *, then) -> None:
        self._pending[self._ai.submit(job, item.listing)] = (then, item)

    def _check(self, li: Listing) -> str | None:
        self._set_activity(f"{li.title}: checking non-negotiables")
        return non_negotiables_rejection_reason(li, self.requirements, settings=self.settings, ai_client=self.ai_client)

    def _enrich(self, li: Listing) -> Enrichment:
        return _safe_enrich(
            self.settings, li, self.questions, self.ai_client,
            on_progress=lambda message: self._set_activity(f"{li.title}: {message}"),
        )

    # --- live status (shown by the web UI's "Running watch…" overlay) ---

    def _set_activity(self, message: str) -> None:
        with self._lock:
            self._activity = message
        self._publish()

    def _publish(self) -> None:
        with self._lock:
            parts = []
            if self._searching:
                parts.append(f"Searching {', '.join(self._searching.values())}…")
            if self._checks_queued:
                parts.append(f"checked {self._checks_done}/{self._checks_queued} against non-negotiables")
            if self.delivering:
                if self.telegram:
                    parts.append(f"sent {len(self.sent)}/{len(self.delivering)} via Telegram")
                else:
                    parts.append(f"prepared {len(self.enriched)}/{len(self.delivering)} for the email")
            if self._activity:
                parts.append(self._activity)
            text = " · ".join(parts)
        if text:
            progress.set_status(self.watch_id, text[0].upper() + text[1:])


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
    started_at = utcnow()  # first thing, so it marks when the run began
    settings = settings or runtime_settings(session)
    result = RunResult(watch_id=watch.id, started_at=started_at)
    # dry_run is always a pure preview: never write to the DB, regardless of ignore_seen.
    record = (not ignore_seen) and not dry_run

    progress.set_status(watch.id, "Starting run…")
    category = get_category(watch.category)
    if category is None:
        result.error = f"unknown category '{watch.category}'"
        return result

    query = category.build_query(watch)
    # Marketplaces whose scraper offers a category list search only the category the AI
    # picked for this watch (normally already picked in the background when the watch was
    # saved; picked now if not). None/missing = all categories. Only cached on real runs,
    # same as every other DB write here.
    query.site_categories = site_categories.resolve(
        session, watch, settings, ai_client=ai_client, persist=record,
        on_status=lambda msg: progress.set_status(watch.id, msg),
    )

    # Which listings were already seen (notified, or recorded by seeding) for this watch?
    seen_keys: set[tuple[str, str]] = set()
    seen_cars: list[tuple[CarFacts, str]] = []
    if not ignore_seen:
        for row in session.exec(select(SeenListing).where(SeenListing.watch_id == watch.id)).all():
            seen_keys.add((row.marketplace, row.external_id))
            # An aggregator's listing kept with its own data links to the original ad: it
            # also counts as that ad seen on its portal, should that portal find it later.
            if (portal := adapter_for_url(row.url)) is not None:
                seen_keys.add((portal[0].key, portal[1]))
            if (car := CarFacts.from_json(row.car_key or "")) is not None:
                seen_cars.append((car, _label(row.marketplace)))
    is_seed = record and settings.seed_mode and not watch.seed_done

    run = _Run(
        watch, query, category, settings, ai_client, result,
        seen_keys=seen_keys, seen_cars=seen_cars, deliver=notify and not dry_run and not is_seed,
    )
    run.run(_plan(watch, result, settings))

    matched = _in_order(run.matched)
    result.matched = len(matched)
    result.matches_preview = [listing_to_dict(li) for li in matched[:50]]
    result.rejected_preview = [
        {**listing_to_dict(f.listing), "reason": reason}
        for f, reason in sorted(run.rejected, key=lambda r: r[0].order)[:50]
    ]
    new = _in_order(run.new)
    result.new = len(new)

    # Seeding run: record existing matches as seen, do not email.
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
        _finish_telegram(session, watch, run, result, record)
    else:
        _send_email(session, watch, run, settings, result, record)
    return result


def _send_email(session, watch, run: _Run, settings, result, record) -> None:
    """One HTML email covering the whole batch -- all-or-nothing per run, same as before
    Telegram support existed. Its matches were translated/answered during the run."""
    email_matches = [
        EmailMatch(listing=f.listing, enrichment=enrichment, questions=watch.questions or [])
        for f, enrichment in sorted(run.enriched, key=lambda e: e[0].order)
    ]
    subject, html = render_email(watch, email_matches)
    progress.set_status(watch.id, "Sending email…")
    try:
        send_match_email(settings, watch.notify_email, subject, html)
        result.emailed = True
        result.notified = len(email_matches)
        if record:
            for m in email_matches:
                _record_seen(session, watch, m.listing, notified=True)
            _log_notification(session, watch, subject, len(email_matches), True, None, channel="email")
        _finish(session, watch, f"emailed {len(email_matches)}", record)
    except Exception as exc:  # noqa: BLE001 - EmailNotConfigured, SMTP/OS errors, etc.
        result.error = f"email failed: {exc}"
        log.warning("email failed for watch %s: %s", watch.id, exc)
        if record:
            # Do NOT mark as seen -> retried on the next run.
            _log_notification(session, watch, subject, len(email_matches), False, str(exc), channel="email")
        _finish(session, watch, result.error, record)


def _finish_telegram(session, watch, run: _Run, result, record) -> None:
    """Telegram sends one message PER LISTING (unlike email's single batch document), each
    as soon as it's ready during the run (see _Run._enriched). Each delivered listing is
    marked seen, so a mid-batch failure never causes already-delivered listings to be
    re-sent on retry. Sending stops at the first failure (almost always systemic -- bad
    token/chat id, rate limit -- not per-listing; the one per-listing failure mode, an
    unfetchable photo, is already absorbed inside send_telegram_match's own photo->text
    fallback) and leaves that listing plus every remaining one unseen, to be retried on
    the next scheduled run."""
    sent = len(run.sent)
    error = run.send_error
    if record:
        for f in run.sent:
            _record_seen(session, watch, f.listing, notified=True)
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


def _safe_enrich(settings, listing, questions, ai_client, *, on_progress=None) -> Enrichment:
    try:
        return enrich_listing(
            settings, listing, questions or [], client=ai_client, on_progress=on_progress
        )
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
        car_key=car.to_json() if (car := car_facts(li)) is not None else "",
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
