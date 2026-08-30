from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

from fastapi import APIRouter, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
from audit_repository import record_audit, request_ip_address, request_user_agent
from auth import get_current_user
from paths import admin_url
from rbac import require_role, validate_csrf
from stats_calculations import (
    MOSCOW,
    UsageRow,
    calculate_known_usage_cost,
    calculate_operator_estimate,
    parse_statistics_period,
)


router = APIRouter(prefix="/stats", tags=["statistics"])
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")


def _selected_period(start: date | None, end: date | None):
    today = datetime.now(MOSCOW).date()
    if start is None and end is None:
        start, end = today.replace(day=1), today
    elif start is None or end is None:
        raise HTTPException(status_code=422, detail="invalid statistics period")
    try:
        return parse_statistics_period(start, end)
    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail="invalid statistics period",
        ) from error


@router.get("", response_class=HTMLResponse)
async def statistics_page(
    request: Request,
    start: date | None = Query(None),
    end: date | None = Query(None),
):
    user = await get_current_user(request)
    require_role(user, {"owner"})
    period = _selected_period(start, end)
    snapshot = await database.get_statistics_snapshot(period)
    settings = await database.get_statistics_settings()
    usage = [UsageRow(**row) for row in snapshot.pop("usage_rows")]
    cost = calculate_known_usage_cost(usage)
    estimate = calculate_operator_estimate(
        snapshot["automated_dialogues"],
        settings["minutes_per_dialogue"],
        settings["hourly_rate_rub"],
    )
    return templates.TemplateResponse(
        request,
        "stats.html",
        {
            "user": user,
            "period": period,
            "stats": snapshot,
            "settings": settings,
            "cost": cost,
            "estimate": estimate,
            "csrf_token": user.csrf_token,
        },
    )


@router.post("/settings")
async def statistics_settings_save(
    request: Request,
    csrf_token: str = Form(""),
    minutes_per_dialogue: Decimal = Form(...),
    hourly_rate_rub: Decimal = Form(...),
):
    user = await get_current_user(request)
    validate_csrf(user, csrf_token)
    require_role(user, {"owner"})
    try:
        settings = await database.save_statistics_settings(
            minutes_per_dialogue,
            hourly_rate_rub,
        )
    except ValueError as error:
        raise HTTPException(
            status_code=422,
            detail="invalid statistics settings",
        ) from error
    await record_audit(
        actor_id=user.id,
        action="statistics.settings_updated",
        object_type="statistics_settings",
        object_id="singleton",
        before=None,
        after={key: str(value) for key, value in settings.items()},
        ip_address=request_ip_address(request),
        user_agent=request_user_agent(request),
    )
    return RedirectResponse(url=admin_url(request, "/stats"), status_code=303)
