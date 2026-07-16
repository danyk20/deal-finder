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
from .. import service

router = APIRouter(include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def _get_watch_or_404(session: Session, watch_id: int) -> Watch:
    watch = session.get(Watch, watch_id)
    if watch is None:
        from fastapi import HTTPException

        raise HTTPException(status_code=404, detail="watch not found")
    return watch


def _parse_watch_form(form) -> dict:
    """Turn the flat add/edit form into Watch fields (sp_* -> search_params, f_* -> filters)."""
    search_params, filters = {}, {}
    for key in form:
        if key.startswith("sp_"):
            search_params[key[3:]] = form.get(key)
        elif key.startswith("f_"):
            filters[key[2:]] = form.get(key)
    questions = [q.strip() for q in form.get("questions", "").splitlines() if q.strip()]
    return {
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


@router.get("/", response_class=HTMLResponse)
def index(request: Request, session: Session = Depends(get_session)):
    watches = session.exec(select(Watch).order_by(Watch.id)).all()
    rows = [{"w": w, "next_run": next_run_time(w.id) if w.active else None} for w in watches]
    health = runtime_settings(session)
    return templates.TemplateResponse(
        request,
        "watches.html",
        {"request": request, "rows": rows, "smtp_configured": bool(health.smtp_host)},
    )


@router.get("/watches/new", response_class=HTMLResponse)
def new_watch(request: Request, session: Session = Depends(get_session)):
    return _render_form(request, session, watch=None)


@router.get("/watches/{watch_id}/edit", response_class=HTMLResponse)
def edit_watch(watch_id: int, request: Request, session: Session = Depends(get_session)):
    return _render_form(request, session, watch=_get_watch_or_404(session, watch_id))


def _render_form(request: Request, session: Session, watch: Watch | None):
    settings = runtime_settings(session)
    category = get_category("car")
    adapters = [a for a in list_adapters() if "car" in a.supported_categories]
    if watch is None:
        selected_mkt = [a.key for a in adapters if a.enabled_by_default]
        questions_text = "\n".join(category.default_questions)
        default_email = settings.default_notify_email
        default_chat_id = settings.telegram_default_chat_id
    else:
        selected_mkt = watch.marketplaces
        questions_text = "\n".join(watch.questions)
        default_email = watch.notify_email
        default_chat_id = watch.telegram_chat_id or settings.telegram_default_chat_id
    return templates.TemplateResponse(
        request,
        "watch_form.html",
        {
            "request": request,
            "watch": watch,
            "category": category,
            "categories": list_categories(),
            "adapters": adapters,
            "selected_mkt": selected_mkt,
            "questions_text": questions_text,
            "default_email": default_email,
            "default_chat_id": default_chat_id,
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
            "matches": matches,
            "logs": logs,
            "next_run": next_run_time(watch_id) if watch.active else None,
            "run_result": run_result,
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
