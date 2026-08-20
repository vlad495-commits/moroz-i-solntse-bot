"""Staff queue for resolving human handoffs."""

from pathlib import Path
from uuid import UUID, uuid4

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
async def escalation_queue(request: Request, reply: str = ""):
    user = await get_current_user(request)
    require_role(user, STAFF_ROLES)
    escalations = await database.get_open_escalations(limit=100)
    for escalation in escalations:
        escalation["reply_token"] = uuid4()
    return templates.TemplateResponse(
        request,
        "escalations.html",
        {
            "user": user,
            "escalations": escalations,
            "csrf_token": user.csrf_token or "",
            "reply": reply,
        },
    )


@router.post("/{escalation_id}/reply")
async def escalation_reply(
    request: Request,
    escalation_id: UUID,
    csrf_token: str = Form(""),
    reply_token: UUID = Form(...),
    reply_text: str = Form(""),
):
    user = await get_current_user(request)
    validate_csrf(user, csrf_token)
    require_role(user, STAFF_ROLES)
    text = reply_text.strip()
    if not text or len(text) > 4096:
        raise HTTPException(status_code=422, detail="invalid reply text")
    result, _ = await database.enqueue_escalation_reply(
        escalation_id,
        reply_token=reply_token,
        text=text,
        actor_id=user.id,
        ip_address=request_ip_address(request),
        user_agent=request_user_agent(request),
    )
    if result == "not_found":
        raise HTTPException(status_code=404, detail="escalation not found")
    if result == "inactive":
        raise HTTPException(status_code=409, detail="escalation is not active")
    return RedirectResponse(
        url=admin_url(request, f"/escalations/?reply={result}"),
        status_code=302,
    )
