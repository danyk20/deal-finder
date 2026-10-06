"""Server-rendered web UI (Jinja2 + HTMX). Mounted at / by the app."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, select
from starlette.concurrency import run_in_threadpool

from ..config import EDITABLE_KEYS
from ..db import get_session, load_setting_overrides, runtime_settings
from ..languages import SUPPORTED_LANGUAGES
from ..models import AppSetting, NotificationLog, SeenListing, Watch
from ..progress import get_status
from ..registry import get_category, list_adapters, list_categories
from ..scheduler import next_run_time
from ..site_categories import category_adapters, effective_id, form_choices, stale_keys
from ..util import localtime
from .. import service

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
templates.env.filters["localtime"] = localtime  # every timestamp on the pages, in local time


def _get_watch_or_404(session: Session, watch_id: int) -> Watch:
    watch = session.get(Watch, watch_id)
    if watch is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="watch not found")
    return watch


def _parse_watch_form(form) -> dict:
    """Turn the flat add/edit form into Watch fields (sp_* -> search_params, f_* -> filters)."""
    search_params, filters, site_choices = {}, {}, {}
    for key in form:
        if key.startswith("sp_"):
            search_params[key[3:]] = form.get(key)
        elif key.startswith("f_"):
            filters[key[2:]] = form.get(key)
        elif key.startswith("sitecat_"):
            site_choices[key[8:]] = form.get(key)
    questions = [q.strip() for q in form.get("questions", "").splitlines() if q.strip()]
    data = {
        "name": form.get("name", "").strip() or "Untitled watch",
        "category": form.get("category", "car"),
        "schedule_kind": form.get("schedule_kind", "interval"),
        "schedule_value": form.get("schedule_value", "1d").strip(),
        "marketplaces": form.getlist("marketplaces"),
        "search_params": search_params,
        "filters": filters,
        "notify_email": form.get("notify_email", "").strip(),
        "notify_channel": form.get("notify_channel", "telegram"),
        "telegram_chat_id": form.get("telegram_chat_id", "").strip(),
        "questions": questions,
    }
    if site_choices:
        data["site_category_choices"] = site_choices
    return data


@router.get("/", response_class=HTMLResponse)
def index(request: Request, session: Session = Depends(get_session)):
    watches = session.exec(select(Watch).order_by(Watch.id)).all()
    rows = [
        {"w": w, "next_run": next_run_time(w.id) if w.active else None, "search_text": _search_text(w)}
        for w in watches
    ]
    health = runtime_settings(session)
    return templates.TemplateResponse(
        request,
        "watches.html",
        {"request": request, "rows": rows, "smtp_configured": bool(health.smtp_host)},
    )


def _search_text(watch: Watch) -> str:
    category = get_category(watch.category)
    return category.search_text(watch) if category else ""


@router.get("/watches/new", response_class=HTMLResponse)
def new_watch(request: Request, watch_type: str | None = None, session: Session = Depends(get_session)):
    return _render_form(request, session, watch=None, watch_type=watch_type)


@router.get("/watches/{watch_id}/edit", response_class=HTMLResponse)
def edit_watch(
    watch_id: int, request: Request, watch_type: str | None = None, session: Session = Depends(get_session)
):
    return _render_form(request, session, watch=_get_watch_or_404(session, watch_id), watch_type=watch_type)


def _render_form(request: Request, session: Session, watch: Watch | None, watch_type: str | None = None):
    """``watch_type`` (?watch_type=general) renders the form for another watch type than
    the watch's own -- the form's type picker reloads with it; saving switches the type."""
    settings = runtime_settings(session)
    current = get_category(watch.category) if watch else None
    category = get_category(watch_type or "") or current or get_category("car")
    adapters = [a for a in list_adapters() if category.key in a.supported_categories]
    if watch is None:
        selected_mkt = [a.key for a in adapters if a.enabled_by_default]
        questions = category.default_questions
        sp_values: dict = {}
        default_email = settings.default_notify_email
        default_chat_id = settings.telegram_default_chat_id
    else:
        offered = {a.key for a in adapters}
        selected_mkt = [k for k in watch.marketplaces if k in offered]
        questions = watch.questions
        sp_values = dict(watch.search_params or {})
        if current is not None and category.key != current.key:
            # Switching type: carry over what translates. Untouched default questions
            # follow the type; a car's make + model become the general search text.
            if list(watch.questions or []) == list(current.default_questions):
                questions = category.default_questions
            if not sp_values.get("query"):
                sp_values["query"] = current.search_text(watch)
        default_email = watch.notify_email
        default_chat_id = watch.telegram_chat_id or settings.telegram_default_chat_id
    questions_text = "\n".join(questions)
    return templates.TemplateResponse(
        request,
        "watch_form.html",
        {
            "request": request,
            "watch": watch,
            "category": category,
            "categories": list_categories(),
            "sp_values": sp_values,
            "adapters": adapters,
            "selected_mkt": selected_mkt,
            "questions_text": questions_text,
            "default_email": default_email,
            "default_chat_id": default_chat_id,
            "site_category_choices": form_choices(watch, adapters),
        },
    )


@router.post("/watches")
async def create_watch_form(request: Request, session: Session = Depends(get_session)):
    data = _parse_watch_form(await request.form())
    watch = service.create_watch(session, data)
    return RedirectResponse(f"/watches/{watch.id}", status_code=303)


@router.post("/watches/{watch_id}")
async def update_watch_form(
    watch_id: int, request: Request, session: Session = Depends(get_session)
):
    watch = _get_watch_or_404(session, watch_id)
    data = _parse_watch_form(await request.form())
    service.update_watch(session, watch, data)
    return RedirectResponse(f"/watches/{watch_id}", status_code=303)


@router.post("/watches/{watch_id}/start")
def start_watch_form(watch_id: int, session: Session = Depends(get_session)):
    service.start_watch(session, _get_watch_or_404(session, watch_id))
    return RedirectResponse(f"/watches/{watch_id}", status_code=303)


@router.post("/watches/{watch_id}/stop")
def stop_watch_form(watch_id: int, session: Session = Depends(get_session)):
    service.stop_watch(session, _get_watch_or_404(session, watch_id))
    return RedirectResponse(f"/watches/{watch_id}", status_code=303)


@router.post("/watches/{watch_id}/delete")
def delete_watch_form(watch_id: int, session: Session = Depends(get_session)):
    service.delete_watch(session, _get_watch_or_404(session, watch_id))
    return RedirectResponse("/", status_code=303)


@router.get("/watches/{watch_id}", response_class=HTMLResponse)
def watch_detail(watch_id: int, request: Request, session: Session = Depends(get_session)):
    return _render_detail(request, session, watch_id, run_result=None)


@router.get("/watches/{watch_id}/run-status")
def run_status(watch_id: int) -> dict:
    """Polled by the watch detail page while a 'Run now' request is in flight, to show
    a live status message (see progress.py) alongside the indeterminate progress bar."""
    return {"status": get_status(watch_id)}


@router.post("/watches/{watch_id}/run-now", response_class=HTMLResponse)
async def run_now_form(
    watch_id: int, request: Request, session: Session = Depends(get_session)
):
    watch = _get_watch_or_404(session, watch_id)
    form = await request.form()
    dry_run = form.get("dry_run") == "on"
    # Form field name kept as "send_email" for compatibility; means "notify" (any channel).
    notify = form.get("send_email") == "on" and not dry_run  # dry run never notifies
    # Run the blocking pipeline off the event loop.
    result = await run_in_threadpool(
        service.run_now, session, watch, notify=notify, test_mode=True, dry_run=dry_run
    )
    return _render_detail(request, session, watch_id, run_result=result)


def _site_categories_view(watch: Watch) -> dict | None:
    """What each selected marketplace that supports categories searches; None when none
    of them does."""
    adapters = category_adapters(watch)
    if not adapters:
        return None
    stale = stale_keys(watch, adapters)
    if stale == {a.key for a in adapters}:
        return {"pending": True, "rows": []}
    sites = (watch.site_categories or {}).get("sites") or {}
    rows = []
    for a in adapters:
        if a.key in stale:
            rows.append({"label": a.label, "text": "not picked yet", "by": "all categories until then"})
            continue
        site = sites.get(a.key) or {}
        chosen = effective_id(site)
        path = next((c["path"] for c in site.get("candidates") or [] if c["id"] == chosen), site.get("path"))
        text = path if chosen is not None else "all categories"
        rows.append({"label": a.label, "text": text, "by": "your choice" if site.get("user_set") else "AI"})
    return {"pending": False, "rows": rows}


def _render_detail(request: Request, session: Session, watch_id: int, run_result):
    watch = _get_watch_or_404(session, watch_id)
    matches = session.exec(
        select(SeenListing)
        .where(SeenListing.watch_id == watch_id)
        .order_by(SeenListing.first_seen_at.desc())
        .limit(50)
    ).all()
    logs = session.exec(
        select(NotificationLog)
        .where(NotificationLog.watch_id == watch_id)
        .order_by(NotificationLog.sent_at.desc())
        .limit(20)
    ).all()
    return templates.TemplateResponse(
        request,
        "watch_detail.html",
        {
            "request": request,
            "w": watch,
            "category": get_category(watch.category),
            "search_text": _search_text(watch),
            "matches": matches,
            "logs": logs,
            "next_run": next_run_time(watch_id) if watch.active else None,
            "run_result": run_result,
            "site_categories": _site_categories_view(watch),
        },
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, session: Session = Depends(get_session)):
    settings = runtime_settings(session)
    overrides = load_setting_overrides(session)
    values = {k: getattr(settings, k) for k in EDITABLE_KEYS}
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "request": request,
            "values": values,
            "overrides": overrides,
            "editable_keys": EDITABLE_KEYS,
            "supported_languages": SUPPORTED_LANGUAGES,
        },
    )


_BOOL_SETTINGS = {
    "smtp_starttls",
    "ai_enabled",
    "seed_mode",
    "adapter_tutti_enabled",
    "adapter_ricardo_enabled",
    "adapter_autoscout24_enabled",
    "adapter_autolina_enabled",
    "adapter_autouncle_enabled",
    "adapter_facebook_enabled",
}
_MASKED_SETTINGS = {"smtp_password", "facebook_password", "telegram_bot_token"}


@router.post("/settings")
async def settings_save(request: Request, session: Session = Depends(get_session)):
    form = await request.form()
    for key in EDITABLE_KEYS:
        if key in _BOOL_SETTINGS:
            # Checkbox: present => true, absent => false (always persist).
            value = "true" if key in form else "false"
        else:
            if key not in form:
                continue
            value = form.get(key, "")
            # Skip the masked password placeholder so we don't overwrite with stars.
            if key in _MASKED_SETTINGS and value == "********":
                continue
        row = session.get(AppSetting, key)
        if row is None:
            session.add(AppSetting(key=key, value=str(value)))
        else:
            row.value = str(value)
            session.add(row)
    session.commit()
    return RedirectResponse("/settings", status_code=303)
