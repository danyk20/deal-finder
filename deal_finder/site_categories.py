"""Per-marketplace category for a watch, picked by the AI from each scraper's own category
list (see BaseAdapter.category_tree and ai/category.py).

The AI gets everything the watch says about the item (name, make/model, the optional
"Category" field in the user's own words, keywords, non-negotiables, ...) and picks ONE of
the categories the scraper offers, per marketplace -- and keeps its runner-ups (top 5 in
all), which the watch's edit form offers so the user can switch to another one (or to all
categories). Marketplaces whose scraper has no category list are skipped and search as
before.

Per marketplace, ``Watch.site_categories["sites"][key]`` holds::

    {"basis": <fingerprint of what this pick was made from>,
     "id": <AI pick or None>, "path": "A > B",
     "candidates": [{"id": ..., "path": ...}, ...],   # top 5, best first
     "user_set": True, "chosen": <id or None>}        # only after the user overrode it

A user's choice survives a re-pick (after the watch's fields changed) as long as it's still
among the new candidates, or was "all categories"; otherwise the new AI pick applies.

The AI only runs for a marketplace when its fingerprint changed -- i.e. when something the
AI is shown (the watch's item fields) or that marketplace's category list changed, or the
marketplace has no pick yet. Saving the form unchanged, or changing only things the AI
doesn't see (schedule, questions, notification, location, ...), never re-runs it; ticking
another marketplace only picks for that one; unticking one keeps its pick, so re-ticking
it later costs nothing either. Picks happen right after the watch is saved (in a
background thread, so saving stays instant) and, if that hasn't happened yet, at the start
of the next run. When the AI is disabled, unreachable or answers garbage, nothing is cached
for that marketplace and it searches all categories -- the AI never blocks a run -- and the
next save/run simply tries again. "No category fits" IS cached (as None = all categories).
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Callable

from sqlmodel import Session, select

from .adapters.base import BaseAdapter, SiteCategory
from .ai.category import rank_site_categories
from .ai.client import AiUnavailable, OllamaClient
from .config import Settings
from .db import runtime_settings, session_scope
from .models import Watch
from .registry import get_adapter, get_category

log = logging.getLogger("deal_finder.site_categories")

# Watch fields that say nothing about WHAT the item is -- left out of the AI's description.
_NOT_ABOUT_THE_ITEM = {"location", "radius_km"}
# Part of every fingerprint: bump it when the stored pick's shape or the way it's picked
# changes, so picks stored by older code are redone (2: added the ranked "candidates";
# 3: one fingerprint per marketplace instead of one for all of them).
_FORMAT = "3"


def category_adapters(watch: Watch) -> list[BaseAdapter]:
    """The watch's selected marketplaces whose scraper offers a category list."""
    adapters = [get_adapter(key) for key in watch.marketplaces or []]
    return [a for a in adapters if a is not None and a.category_tree()]


def describe_watch(watch: Watch) -> str:
    """Everything the watch says about the searched item, as ``label: value`` lines."""
    lines = [f"Watch name: {watch.name}"]
    category = get_category(watch.category)
    sp, filters = watch.search_params or {}, watch.filters or {}
    if category is not None:
        fields = [(f, sp) for f in category.search_param_fields] + [(f, filters) for f in category.filter_fields]
        for f, values in fields:
            value = str(values.get(f.name) or "").strip()
            if value and f.name not in _NOT_ABOUT_THE_ITEM:
                lines.append(f"{f.label}: {value}")
    else:
        for key, value in {**sp, **filters}.items():
            if str(value or "").strip() and key not in _NOT_ABOUT_THE_ITEM:
                lines.append(f"{key}: {value}")
    return "\n".join(lines)


def _tree_hash(h, adapter: BaseAdapter) -> None:
    h.update(f"\0{adapter.key}\0".encode())
    for n in adapter.category_tree():
        h.update(f"{n.id}|{n.parent_id}|{n.selectable}\n".encode())


def _site_basis(item_text: str, adapter: BaseAdapter) -> str:
    """Fingerprint of everything one marketplace's pick depends on: the item description
    and that marketplace's category list (a scraper update with new categories re-picks)."""
    h = hashlib.sha256(f"{_FORMAT}\0{item_text}".encode())
    _tree_hash(h, adapter)
    return h.hexdigest()[:16]


def _legacy_basis(item_text: str, adapters: list[BaseAdapter]) -> str:
    """Format 2's single fingerprint over all marketplaces together -- only used to keep
    picks stored that way (no per-site "basis") current instead of redoing them."""
    h = hashlib.sha256(f"2\0{item_text}".encode())
    for a in sorted(adapters, key=lambda a: a.key):
        _tree_hash(h, a)
    return h.hexdigest()[:16]


def stale_keys(watch: Watch, adapters: list[BaseAdapter] | None = None, item_text: str | None = None) -> set[str]:
    """Selected marketplaces (with categories) whose pick is missing or out of date."""
    adapters = category_adapters(watch) if adapters is None else adapters
    if not adapters:
        return set()
    item_text = describe_watch(watch) if item_text is None else item_text
    cached = watch.site_categories or {}
    sites = cached.get("sites") or {}
    legacy_current = "basis" in cached and cached["basis"] == _legacy_basis(item_text, adapters)
    stale = set()
    for a in adapters:
        site = sites.get(a.key)
        if site is None:
            stale.add(a.key)
        elif "basis" in site:
            if site["basis"] != _site_basis(item_text, a):
                stale.add(a.key)
        elif not legacy_current:
            stale.add(a.key)
    return stale


def _path(node: SiteCategory, tree: list[SiteCategory]) -> str:
    by_id = {n.id: n for n in tree}
    names, cur = [], node
    while cur is not None and len(names) < 10:
        names.append(cur.name)
        cur = by_id.get(cur.parent_id) if cur.parent_id else None
    return " > ".join(reversed(names))


def effective_id(site: dict) -> str | None:
    """The category a marketplace actually searches: the user's choice, else the AI's."""
    return site.get("chosen") if site.get("user_set") else site.get("id")


def is_current(watch: Watch) -> bool:
    """True when every selected marketplace's pick matches the watch's current fields."""
    return not stale_keys(watch)


def resolve(
    session: Session,
    watch: Watch,
    settings: Settings,
    *,
    ai_client: OllamaClient | None = None,
    persist: bool = True,
    on_status: Callable[[str], None] | None = None,
) -> dict[str, str | None]:
    """Adapter key -> category id to search (None = all categories), for every selected
    marketplace that supports categories. Asks the AI only for marketplaces whose pick is
    missing or out of date (and caches the answer if ``persist``); a marketplace that still
    has no current pick afterwards (AI off/down) is left out = searches all categories."""
    adapters = category_adapters(watch)
    if not adapters:
        return {}
    item_text = describe_watch(watch)
    stale = stale_keys(watch, adapters, item_text)
    sites = {key: dict(site) for key, site in ((watch.site_categories or {}).get("sites") or {}).items()}
    changed = False
    for a in adapters:  # stamp still-current format-2 picks with their own fingerprint
        if a.key not in stale and "basis" not in sites[a.key]:
            sites[a.key]["basis"] = _site_basis(item_text, a)
            changed = True

    if stale and settings.ai_enabled:
        client = ai_client or OllamaClient(settings.ollama_base_url, settings.ollama_model, settings.ollama_timeout)
        for adapter in (a for a in adapters if a.key in stale):
            if on_status:
                on_status(f"Choosing the {adapter.label} category…")
            tree = adapter.category_tree()
            try:
                ranked = rank_site_categories(client, item_text, adapter.label, tree)
            except AiUnavailable as exc:
                log.warning(
                    "watch %s: couldn't pick a %s category (%s); searching all categories", watch.id, adapter.key, exc
                )
                break
            best = ranked[0] if ranked else None
            site = {
                "basis": _site_basis(item_text, adapter),
                "id": best.id if best else None,
                "path": _path(best, tree) if best else None,
                "candidates": [{"id": n.id, "path": _path(n, tree)} for n in ranked if n is not None],
            }
            prev = sites.get(adapter.key) or {}
            if prev.get("user_set") and (
                prev.get("chosen") is None or prev["chosen"] in {c["id"] for c in site["candidates"]}
            ):
                site.update(user_set=True, chosen=prev["chosen"])
            sites[adapter.key] = site
            stale.discard(adapter.key)
            changed = True

    if persist and changed:
        watch.site_categories = {"sites": sites}  # new dict: JSON column change detection
        session.add(watch)
        session.commit()
    return {a.key: effective_id(sites[a.key]) for a in adapters if a.key not in stale}


def apply_user_choices(watch: Watch, choices: dict[str, str]) -> None:
    """Apply the edit form's per-marketplace category choice (adapter key -> category id,
    "" = all categories). Picking the AI's own pick again clears the override. Values
    that aren't among the offered candidates are ignored. Doesn't commit."""
    cached = watch.site_categories or {}
    sites = {key: dict(site) for key, site in (cached.get("sites") or {}).items()}
    changed = False
    for key, value in choices.items():
        site = sites.get(key)
        if site is None:
            continue
        chosen = value or None
        if chosen is not None and chosen not in {c["id"] for c in site.get("candidates") or []}:
            continue
        if chosen == site.get("id"):
            new = {k: v for k, v in site.items() if k not in ("user_set", "chosen")}
        else:
            new = {**site, "user_set": True, "chosen": chosen}
        if new != site:
            sites[key] = new
            changed = True
    if changed:
        watch.site_categories = {**cached, "sites": sites}  # new dict: JSON column change detection


def form_choices(watch: Watch | None, adapters: list[BaseAdapter]) -> list[dict]:
    """The watch form's per-marketplace category dropdowns, one for each of ``adapters``
    (the marketplaces the form offers) that supports categories -- also unticked ones, so
    the form can show/hide them as marketplaces are ticked. Each offers the AI's top
    candidates plus "all categories", with the currently searched one selected;
    ``pending`` = no current AI pick to choose from yet (e.g. a new watch, or a
    marketplace that only gets ticked now)."""
    stale = stale_keys(watch) if watch is not None else set()
    sites = ((watch.site_categories if watch else None) or {}).get("sites") or {}
    selected_keys = set(watch.marketplaces or []) if watch else set()
    out = []
    for adapter in (a for a in adapters if a.category_tree()):
        site = sites.get(adapter.key)
        if site is None or adapter.key in stale or adapter.key not in selected_keys:
            out.append({"key": adapter.key, "label": adapter.label, "pending": True, "options": []})
            continue
        options = [
            {"value": c["id"], "label": c["path"] + ("  (AI pick)" if c["id"] == site.get("id") else "")}
            for c in site.get("candidates") or []
        ]
        options.append({"value": "", "label": "All categories" + ("  (AI pick)" if site.get("id") is None else "")})
        selected = effective_id(site) or ""
        out.append({"key": adapter.key, "label": adapter.label, "pending": False, "options": options,
                    "selected": selected})
    return out


def resolve_in_background(session: Session, watch: Watch) -> None:
    """After a watch is saved: (re)pick its categories in a background thread when they're
    out of date, so the save itself returns immediately. No-op when nothing needs picking
    or the AI is disabled."""
    if is_current(watch) or not runtime_settings(session).ai_enabled:
        return
    watch_id = watch.id

    def _run() -> None:
        try:
            with session_scope() as s:
                w = s.get(Watch, watch_id)
                if w is not None:
                    resolve(s, w, runtime_settings(s))
        except Exception:  # noqa: BLE001 - a background helper must never crash the app
            log.exception("watch %s: background category pick failed", watch_id)

    threading.Thread(target=_run, name=f"site-categories-{watch_id}", daemon=True).start()


def resolve_stale_in_background() -> None:
    """On app start: pick categories, one watch after another in a single background
    thread, for every watch whose pick is out of date -- e.g. watches created before this
    feature existed, or a scraper update that changed a category list. No-op when the AI is
    disabled or every pick is current."""
    with session_scope() as s:
        if not runtime_settings(s).ai_enabled:
            return
        stale = [w.id for w in s.exec(select(Watch)).all() if not is_current(w)]
    if not stale:
        return

    def _run() -> None:
        for watch_id in stale:
            try:
                with session_scope() as s:
                    w = s.get(Watch, watch_id)
                    if w is not None and not is_current(w):
                        resolve(s, w, runtime_settings(s))
            except Exception:  # noqa: BLE001 - a background helper must never crash the app
                log.exception("watch %s: startup category pick failed", watch_id)

    threading.Thread(target=_run, name="site-categories-startup", daemon=True).start()
