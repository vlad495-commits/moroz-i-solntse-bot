"""Owner-only controls for the reactivation marketing program."""

from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
import reactivation_database as rdb
from audit_repository import record_audit, request_ip_address, request_user_agent
from auth import get_current_user
from moroz.reactivation.policy import (
    DEFAULT_MAIN_TEXT,
    DEFAULT_REMINDER_TEXT,
    ProgramPolicy,
)
from paths import admin_url
from rbac import require_role, validate_csrf


router = APIRouter(prefix="/marketing", tags=["marketing"])
legacy_router = APIRouter(prefix="/reactivation", tags=["marketing-legacy"])
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")


@legacy_router.get("/")
async def legacy_reactivation(request: Request) -> RedirectResponse:
    suffix = f"?{request.url.query}" if request.url.query else ""
    return RedirectResponse(f"/marketing/{suffix}", status_code=307)


@router.get("/", response_class=HTMLResponse)
async def marketing_page(
    request: Request,
    consent_id: UUID | None = Query(None),
    page: int = Query(1, ge=1),
):
    user = await _owner(request)
    data = await rdb.get_marketing_page_data(
        database.get_database(),
        actor_id=user.id,
        consent_id=consent_id,
        page=page,
    )
    return templates.TemplateResponse(
        request,
        "reactivation.html",
        {"user": user, "csrf_token": user.csrf_token, "data": data},
    )


@router.post("/versions")
async def marketing_version_create(
    request: Request,
    inactivity_days: int = Form(90),
    reminder_after_days: str = Form("5"),
    cooldown_days: int = Form(90),
    main_text: str = Form(DEFAULT_MAIN_TEXT),
    reminder_text: str = Form(DEFAULT_REMINDER_TEXT),
    csrf_token: str = Form(""),
):
    user = await _owner_write(request, csrf_token)
    try:
        reminder = (
            None
            if reminder_after_days in {"", "off"}
            else int(reminder_after_days)
        )
        version_id = await rdb.create_draft(
            database.get_database(),
            policy=ProgramPolicy(
                inactivity_days=inactivity_days,
                reminder_after_days=reminder,
                cooldown_days=cooldown_days,
                main_text=main_text.strip(),
                reminder_text=reminder_text.strip(),
            ),
            actor_id=user.id,
        )
    except (TypeError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return _redirect(request, f"/?version={version_id}")


@router.post("/versions/{version_id}/preview")
async def marketing_version_preview(
    request: Request,
    version_id: UUID,
    csrf_token: str = Form(""),
):
    user = await _owner_write(request, csrf_token)
    await rdb.preview_version(
        database.get_database(), version_id, actor_id=user.id
    )
    return _redirect(request, "/?preview=ready")


@router.post("/versions/{version_id}/test")
async def marketing_version_test(
    request: Request,
    version_id: UUID,
    csrf_token: str = Form(""),
):
    user = await _owner_write(request, csrf_token)
    outbound_id = await rdb.queue_test_send(
        database.get_database(), version_id, actor_id=user.id
    )
    status = "queued" if outbound_id is not None else "not_configured"
    return _redirect(request, f"/?test={status}")


@router.post("/versions/{version_id}/activate")
async def marketing_version_activate(
    request: Request,
    version_id: UUID,
    confirmation: str = Form(""),
    csrf_token: str = Form(""),
):
    user = await _owner_write(request, csrf_token)
    if confirmation != "АКТИВИРОВАТЬ":
        raise HTTPException(status_code=400, detail="invalid activation confirmation")
    await rdb.activate_version(
        database.get_database(), version_id, actor_id=user.id
    )
    return _redirect(request, "/?activated=1")


@router.post("/legal")
async def marketing_legal_approve(
    request: Request,
    reference: str = Form(""),
    csrf_token: str = Form(""),
):
    user = await _owner_write(request, csrf_token)
    try:
        await rdb.approve_legal(
            database.get_database(), actor_id=user.id, reference=reference
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return _redirect(request, "/?legal=approved")


@router.post("/mode")
async def marketing_mode_set(
    request: Request,
    mode: str = Form(""),
    confirmation: str = Form(""),
    csrf_token: str = Form(""),
):
    user = await _owner_write(request, csrf_token)
    if mode == "active" and confirmation != "АКТИВИРОВАТЬ":
        raise HTTPException(status_code=400, detail="invalid activation confirmation")
    try:
        await rdb.set_mode(database.get_database(), mode, actor_id=user.id)
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    return _redirect(request, f"/?mode={mode}")


@router.post("/consents/{consent_id}/revoke")
async def marketing_consent_revoke(
    request: Request,
    consent_id: UUID,
    csrf_token: str = Form(""),
):
    user = await _owner_write(request, csrf_token)
    try:
        result = await rdb.revoke_marketing_consent_by_id(
            database.get_database(), consent_id=consent_id
        )
    except ValueError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    await _audit(
        request,
        user,
        action="reactivation.consent_revoked",
        object_type="marketing_consent",
        object_id=str(consent_id),
        before=None,
        after={
            "active": result["active"],
            "consent_version": result["consent_version"],
        },
    )
    return _redirect(request, "/?consent=revoked")


async def _owner(request):
    user = await get_current_user(request)
    require_role(user, {"owner"})
    return user


async def _owner_write(request, csrf_token: str):
    user = await _owner(request)
    validate_csrf(user, csrf_token)
    return user


def _redirect(request, suffix: str):
    return RedirectResponse(admin_url(request, f"/marketing{suffix}"), status_code=302)


async def _audit(request, user, **values) -> None:
    await record_audit(
        actor_id=user.id,
        ip_address=request_ip_address(request),
        user_agent=request_user_agent(request),
        **values,
    )
