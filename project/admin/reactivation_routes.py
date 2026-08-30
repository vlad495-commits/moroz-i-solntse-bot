"""Owner-only admin routes for the internal reactivation queue."""

from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
import reactivation_database as rdb
from audit_repository import record_audit, request_ip_address, request_user_agent
from auth import get_current_user
from paths import admin_url
from rbac import require_role, validate_csrf


router = APIRouter(prefix="/reactivation", tags=["reactivation"])
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")


@router.get("/", response_class=HTMLResponse)
async def reactivation_page(request: Request):
    user = await get_current_user(request)
    require_role(user, {"owner"})
    data = await rdb.get_page_data(database.get_database())
    return templates.TemplateResponse(
        request,
        "reactivation.html",
        {"user": user, "csrf_token": user.csrf_token, "data": data},
    )


@router.post("/settings")
async def reactivation_settings(
    request: Request,
    after_visit_days: int = Form(...),
    sleeping_days: int = Form(...),
    discount_percent: int = Form(...),
    monthly_message_limit: int = Form(...),
    ignore_limit: int = Form(...),
    base_offer: str = Form(""),
    llm_instruction: str = Form(""),
    csrf_token: str = Form(""),
):
    user = await get_current_user(request)
    require_role(user, {"owner"})
    validate_csrf(user, csrf_token)
    _validate_settings(
        after_visit_days=after_visit_days,
        sleeping_days=sleeping_days,
        discount_percent=discount_percent,
        monthly_message_limit=monthly_message_limit,
        ignore_limit=ignore_limit,
        base_offer=base_offer,
        llm_instruction=llm_instruction,
    )
    target = database.get_database()
    before = await rdb.get_settings(target)
    after = await rdb.save_settings(
        target,
        after_visit_days=after_visit_days,
        sleeping_days=sleeping_days,
        discount_percent=discount_percent,
        monthly_message_limit=monthly_message_limit,
        ignore_limit=ignore_limit,
        base_offer=base_offer,
        llm_instruction=llm_instruction,
    )
    await _audit(
        request,
        user,
        action="reactivation.settings_saved",
        object_type="reactivation_settings",
        object_id="1",
        before=_audit_snapshot(before),
        after=_audit_snapshot(after),
    )
    return RedirectResponse(
        url=admin_url(request, "/reactivation/?saved=1"), status_code=302
    )


@router.post("/consent")
async def reactivation_consent(
    request: Request,
    channel: str = Form("telegram"),
    user_id: str = Form(...),
    consent_version: str = Form(...),
    action: str = Form(...),
    csrf_token: str = Form(""),
):
    user = await get_current_user(request)
    require_role(user, {"owner"})
    validate_csrf(user, csrf_token)
    if channel != "telegram" or action not in {"grant", "revoke"}:
        raise HTTPException(status_code=422, detail="invalid consent action")
    if not user_id.strip() or not consent_version.strip():
        raise HTTPException(status_code=422, detail="consent fields required")
    if len(user_id) > 128 or len(consent_version) > 128:
        raise HTTPException(status_code=422, detail="consent fields too long")
    result = await rdb.set_marketing_consent(
        database.get_database(),
        channel=channel,
        user_id=user_id.strip(),
        consent_version=consent_version.strip(),
        active=action == "grant",
    )
    await _audit(
        request,
        user,
        action=f"reactivation.consent_{action}",
        object_type="marketing_consent",
        object_id=f"{channel}:{user_id.strip()}",
        before=None,
        after={
            "active": result["active"],
            "consent_version": result["consent_version"],
        },
    )
    status = "granted" if action == "grant" else "revoked"
    return RedirectResponse(
        url=admin_url(request, f"/reactivation/?consent={status}"),
        status_code=302,
    )


@router.post("/campaigns")
async def reactivation_campaign_create(
    request: Request,
    segment: str = Form(...),
    action: str = Form("draft"),
    csrf_token: str = Form(""),
):
    user = await get_current_user(request)
    require_role(user, {"owner"})
    validate_csrf(user, csrf_token)
    if segment not in rdb.SEGMENTS or action != "draft":
        raise HTTPException(status_code=422, detail="invalid campaign")
    campaign_id = await rdb.create_campaign(
        database.get_database(),
        segment=segment,
        created_by=user.id,
    )
    await _audit(
        request,
        user,
        action="reactivation.campaign_draft",
        object_type="reactivation_campaign",
        object_id=str(campaign_id),
        before=None,
        after={
            "segment": segment,
            "status": "draft",
        },
    )
    return RedirectResponse(
        url=admin_url(request, "/reactivation/?campaign=draft"),
        status_code=302,
    )


@router.post("/campaigns/{campaign_id}/queue")
async def reactivation_campaign_queue(
    request: Request,
    campaign_id: UUID,
    csrf_token: str = Form(""),
):
    user = await get_current_user(request)
    require_role(user, {"owner"})
    validate_csrf(user, csrf_token)
    result = await rdb.queue_campaign(
        database.get_database(), campaign_id=campaign_id
    )
    await _audit(
        request,
        user,
        action="reactivation.campaign_queued",
        object_type="reactivation_campaign",
        object_id=str(campaign_id),
        before={"status": "draft"},
        after={
            "status": "queued",
            "recipient_count": result["recipient_count"],
        },
    )
    return RedirectResponse(
        url=admin_url(request, "/reactivation/?campaign=queued"),
        status_code=302,
    )


def _validate_settings(**values) -> None:
    bounds = {
        "after_visit_days": (0, 3650),
        "sleeping_days": (1, 3650),
        "discount_percent": (0, 100),
        "monthly_message_limit": (0, 100),
        "ignore_limit": (0, 100),
    }
    for name, (minimum, maximum) in bounds.items():
        if not minimum <= values[name] <= maximum:
            raise HTTPException(status_code=422, detail=f"invalid {name}")
    if len(values["base_offer"]) > 4000 or len(values["llm_instruction"]) > 4000:
        raise HTTPException(status_code=422, detail="reactivation text too long")


async def _audit(request, user, **values) -> None:
    await record_audit(
        actor_id=user.id,
        ip_address=request_ip_address(request),
        user_agent=request_user_agent(request),
        **values,
    )


def _audit_snapshot(values: dict) -> dict:
    return {
        key: value.isoformat() if hasattr(value, "isoformat") else value
        for key, value in values.items()
    }
