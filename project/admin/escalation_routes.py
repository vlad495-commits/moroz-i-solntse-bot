from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
from audit_repository import request_ip_address, request_user_agent
from auth import get_current_user
from moroz.escalation.service import EscalationNotOpen, EscalationService
from paths import admin_url
from rbac import require_role, validate_csrf


router = APIRouter(prefix="/escalations", tags=["escalations"])
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")
_ALLOWED_ROLES = {"owner", "manager"}
_MAX_TEXT_LENGTH = 4000


def _service() -> EscalationService:
    if database._pool is None:
        raise RuntimeError("database is not initialized")
    return EscalationService(database._pool)


async def list_open_escalations_data() -> list[dict[str, object]]:
    return await _service().list_open()


def _bounded(value: str, field: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > _MAX_TEXT_LENGTH:
        raise HTTPException(status_code=422, detail=f"invalid_{field}")
    return normalized


def _actor_id(user) -> int:
    if user.id is None:
        raise HTTPException(status_code=403, detail="durable_actor_required")
    return int(user.id)


@router.get("/", response_class=HTMLResponse)
async def escalation_list(request: Request):
    user = await get_current_user(request)
    require_role(user, _ALLOWED_ROLES)
    return templates.TemplateResponse(
        request,
        "escalations.html",
        {
            "user": user,
            "escalations": await list_open_escalations_data(),
            "csrf_token": user.csrf_token,
        },
    )


@router.post("/{escalation_id}/reply")
async def reply_escalation(
    request: Request,
    escalation_id: UUID,
    text: str = Form(""),
    csrf_token: str = Form(""),
):
    user = await get_current_user(request)
    require_role(user, _ALLOWED_ROLES)
    validate_csrf(user, csrf_token)
    actor_id = _actor_id(user)
    text = _bounded(text, "reply")
    try:
        await _service().reply(
            escalation_id,
            text=text,
            actor_id=actor_id,
            ip_address=request_ip_address(request),
            user_agent=request_user_agent(request),
        )
    except EscalationNotOpen:
        raise HTTPException(status_code=409, detail="escalation_not_open") from None
    return RedirectResponse(
        url=admin_url(request, "/escalations/"), status_code=302
    )


@router.post("/{escalation_id}/resolve")
async def resolve_escalation(
    request: Request,
    escalation_id: UUID,
    reason: str = Form(""),
    csrf_token: str = Form(""),
):
    user = await get_current_user(request)
    require_role(user, _ALLOWED_ROLES)
    validate_csrf(user, csrf_token)
    actor_id = _actor_id(user)
    reason = _bounded(reason, "resolution_reason")
    try:
        await _service().resolve(
            escalation_id,
            reason=reason,
            actor_id=actor_id,
            ip_address=request_ip_address(request),
            user_agent=request_user_agent(request),
        )
    except EscalationNotOpen:
        raise HTTPException(status_code=409, detail="escalation_not_open") from None
    return RedirectResponse(
        url=admin_url(request, "/escalations/"), status_code=302
    )
