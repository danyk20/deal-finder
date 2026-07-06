"""JSON API. Mounted under /api by the app."""

from __future__ import annotations

import dataclasses

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from ..config import EDITABLE_KEYS
from ..db import get_session, load_setting_overrides, runtime_settings
from ..models import AppSetting, NotificationLog, SeenListing, Watch
from ..registry import list_adapters, list_categories
from ..scheduler import build_trigger, next_run_time
from ..schemas import SettingsUpdate, WatchCreate, WatchRead, WatchUpdate
from .. import service

router = APIRouter(prefix="/api", tags=["api"])

_MASKED_KEYS = {"smtp_password", "facebook_password", "telegram_bot_token"}


def _get_watch(session: Session, watch_id: int) -> Watch:
    watch = session.get(Watch, watch_id)
    if watch is None:
        raise HTTPException(status_code=404, detail="watch not found")
    return watch


def watch_to_read(watch: Watch) -> WatchRead:
    data = watch.model_dump()
    data["next_run_at"] = next_run_time(watch.id) if watch.active else None
    return WatchRead(**data)


# --- Metadata ---------------------------------------------------------------


@router.get("/health")
def health(session: Session = Depends(get_session)) -> dict:
    settings = runtime_settings(session)
    db_ok = True
    try:
        session.exec(select(Watch).limit(1)).all()
    except Exception:  # noqa: BLE001
        db_ok = False
    from ..scheduler import get_scheduler
    from ..ai import OllamaClient

    ai_ok = False
    if settings.ai_enabled:
        ai_ok = OllamaClient(settings.ollama_base_url, settings.ollama_model).available()
    return {
        "db": db_ok,
        "scheduler_running": get_scheduler().running,
        "ai_enabled": settings.ai_enabled,
        "ai_reachable": ai_ok,
        "ollama_model": settings.ollama_model,
        "smtp_configured": bool(settings.smtp_host),
    }


@router.get("/categories")
def categories() -> list[dict]:
    out = []
    for cat in list_categories():
        out.append(
            {
                "key": cat.key,
                "label": cat.label,
                "search_param_fields": [dataclasses.asdict(f) for f in cat.search_param_fields],
                "filter_fields": [dataclasses.asdict(f) for f in cat.filter_fields],
                "default_questions": cat.default_questions,
            }
        )
    return out


@router.get("/marketplaces")
def marketplaces() -> list[dict]:
    return [
        {
            "key": a.key,
            "label": a.label,
            "supported_categories": sorted(a.supported_categories),
            "enabled_by_default": a.enabled_by_default,
            "status_note": a.status_note,
        }
        for a in list_adapters()
    ]


# --- Watches ----------------------------------------------------------------


@router.get("/watches", response_model=list[WatchRead])
def list_watches(session: Session = Depends(get_session)) -> list[WatchRead]:
    watches = session.exec(select(Watch).order_by(Watch.id)).all()
    return [watch_to_read(w) for w in watches]


@router.post("/watches", response_model=WatchRead, status_code=201)
def create_watch(payload: WatchCreate, session: Session = Depends(get_session)) -> WatchRead:
    _validate_schedule(payload.schedule_kind, payload.schedule_value)
    watch = service.create_watch(session, payload.model_dump())
    return watch_to_read(watch)


@router.get("/watches/{watch_id}", response_model=WatchRead)
def get_watch(watch_id: int, session: Session = Depends(get_session)) -> WatchRead:
    return watch_to_read(_get_watch(session, watch_id))


@router.patch("/watches/{watch_id}", response_model=WatchRead)
def update_watch(
    watch_id: int, payload: WatchUpdate, session: Session = Depends(get_session)
) -> WatchRead:
    watch = _get_watch(session, watch_id)
    data = payload.model_dump(exclude_unset=True)
    kind = data.get("schedule_kind", watch.schedule_kind)
    value = data.get("schedule_value", watch.schedule_value)
    if "schedule_kind" in data or "schedule_value" in data:
        _validate_schedule(kind, value)
    watch = service.update_watch(session, watch, data)
    return watch_to_read(watch)


@router.delete("/watches/{watch_id}", status_code=204)
def delete_watch(watch_id: int, session: Session = Depends(get_session)) -> None:
    service.delete_watch(session, _get_watch(session, watch_id))


@router.post("/watches/{watch_id}/start", response_model=WatchRead)
def start_watch(watch_id: int, session: Session = Depends(get_session)) -> WatchRead:
    watch = _get_watch(session, watch_id)
    _validate_schedule(watch.schedule_kind, watch.schedule_value)
    return watch_to_read(service.start_watch(session, watch))


@router.post("/watches/{watch_id}/stop", response_model=WatchRead)
def stop_watch(watch_id: int, session: Session = Depends(get_session)) -> WatchRead:
    return watch_to_read(service.stop_watch(session, _get_watch(session, watch_id)))


@router.post("/watches/{watch_id}/run-now")
def run_now(
    watch_id: int,
    send_email: bool = False,  # query param name kept for compatibility; means "notify"
    dry_run: bool = False,
    session: Session = Depends(get_session),
) -> dict:
    watch = _get_watch(session, watch_id)
    result = service.run_now(
        session, watch, notify=send_email and not dry_run, test_mode=True, dry_run=dry_run
    )
    return dataclasses.asdict(result)


@router.get("/watches/{watch_id}/matches")
def watch_matches(watch_id: int, session: Session = Depends(get_session)) -> list[dict]:
    _get_watch(session, watch_id)
    rows = session.exec(
        select(SeenListing)
        .where(SeenListing.watch_id == watch_id)
        .order_by(SeenListing.first_seen_at.desc())
        .limit(200)
    ).all()
    return [r.model_dump() for r in rows]


@router.get("/watches/{watch_id}/logs")
def watch_logs(watch_id: int, session: Session = Depends(get_session)) -> list[dict]:
    _get_watch(session, watch_id)
    rows = session.exec(
        select(NotificationLog)
        .where(NotificationLog.watch_id == watch_id)
        .order_by(NotificationLog.sent_at.desc())
        .limit(100)
    ).all()
    return [r.model_dump() for r in rows]


# --- Settings ---------------------------------------------------------------


@router.get("/settings")
def get_settings_api(session: Session = Depends(get_session)) -> dict:
    overrides = load_setting_overrides(session)
    settings = runtime_settings(session)
    data = {k: getattr(settings, k) for k in EDITABLE_KEYS}
    for k in _MASKED_KEYS:
        if data.get(k):
            data[k] = "********"
    return {"effective": data, "overrides_set": sorted(overrides.keys())}


@router.patch("/settings")
def update_settings_api(
    payload: SettingsUpdate, session: Session = Depends(get_session)
) -> dict:
    for key, value in payload.values.items():
        if key not in EDITABLE_KEYS:
            raise HTTPException(status_code=400, detail=f"'{key}' is not an editable setting")
        row = session.get(AppSetting, key)
        if row is None:
            session.add(AppSetting(key=key, value=str(value)))
        else:
            row.value = str(value)
            session.add(row)
    session.commit()
    return get_settings_api(session)


def _validate_schedule(kind: str, value: str) -> None:
    try:
        build_trigger(kind, value)
    except (ValueError, Exception) as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"invalid schedule: {exc}") from exc
