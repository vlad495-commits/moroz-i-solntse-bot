"""Staff queue for resolving human handoffs."""

from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
from audit_repository import request_ip_address, request_user_agent
from auth import get_current_user
from paths import admin_url
from rbac import require_role, validate_csrf


router = APIRouter(prefix="/escalations", tags=["escalations"])
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")
STAFF_ROLES = {"owner", "admin"}


@router.get("/", response_class=HTMLResponse)
async def escalation_queue(request: Request, resolved: str = ""):
    user = await get_current_user(request)
    require_role(user, STAFF_ROLES)
    escalations = await database.get_open_escalations(limit=100)
    return templates.TemplateResponse(
        request,
        "escalations.html",
        {
            "user": user,
            "escalations": escalations,
            "csrf_token": user.csrf_token or "",
            "resolved": resolved,
        },
    )


@router.post("/{escalation_id}/resolve")
async def escalation_resolve(
    request: Request,
    escalation_id: UUID,
    csrf_token: str = Form(""),
):
    user = await get_current_user(request)
    validate_csrf(user, csrf_token)
    require_role(user, STAFF_ROLES)
    result = await database.resolve_escalation(
        escalation_id,
        actor_id=user.id,
        ip_address=request_ip_address(request),
        user_agent=request_user_agent(request),
    )
    if result == "not_found":
        raise HTTPException(status_code=404, detail="escalation not found")
    return RedirectResponse(
        url=admin_url(request, f"/escalations/?resolved={result}"),
        status_code=302,
    )
