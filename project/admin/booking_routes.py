"""Read-only staff views for the local booking projection."""

import logging
from datetime import timedelta
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import database
from audit_repository import request_ip_address, request_user_agent
from auth import get_current_user
from booking_views import (
    CALENDAR_END_HOUR,
    CALENDAR_START_HOUR,
    MOSCOW,
    calendar_layout,
    validate_booking_status_action,
    validate_manual_booking,
    week_bounds,
)
from bookings_database import (
    enqueue_admin_booking_command,
    get_booking_detail,
    list_booking_service_options,
    list_calendar_bookings,
)
from moroz.booking.admin_commands import (
    ADMIN_BOOKING_CREATE_KIND,
    ADMIN_BOOKING_STATUS_KIND,
)
from paths import admin_url
from rbac import require_role, validate_csrf


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bookings", tags=["bookings"])
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")
STAFF_ROLES = {"owner", "admin"}


@router.get("/", response_class=HTMLResponse)
async def booking_list(
    request: Request,
    week: str | None = None,
):
    user = await get_current_user(request)
    require_role(user, STAFF_ROLES)
    try:
        week_start, week_end = week_bounds(week)
        page = await list_calendar_bookings(
            database.get_database(),
            week_start=week_start,
            week_end=week_end,
        )
        service_options = await list_booking_service_options(database.get_database())
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception as error:
        logger.error("booking_list_failed error_type=%s", type(error).__name__)
        raise HTTPException(status_code=503, detail="bookings unavailable") from error
    return templates.TemplateResponse(
        request,
        "bookings.html",
        {
            "user": user,
            "page": page,
            "days": calendar_layout(
                page["items"], week_start.astimezone(MOSCOW).date()
            ),
            "week_start": week_start.astimezone(MOSCOW).date(),
            "week_end": (week_end.astimezone(MOSCOW).date() - timedelta(days=1)),
            "previous_week": (
                week_start.astimezone(MOSCOW).date() - timedelta(days=7)
            ).isoformat(),
            "next_week": (
                week_start.astimezone(MOSCOW).date() + timedelta(days=7)
            ).isoformat(),
            "calendar_hours": range(CALENDAR_START_HOUR, CALENDAR_END_HOUR + 1),
            "calendar_start_hour": CALENDAR_START_HOUR,
            "calendar_height": (CALENDAR_END_HOUR - CALENDAR_START_HOUR) * 60,
            "service_options": service_options,
            "notice": request.query_params.get("notice"),
        },
    )


@router.post("/manual")
async def create_manual_booking(
    request: Request,
    customer_name: str = Form(""),
    customer_phone: str = Form(""),
    service_staff: str = Form(""),
    starts_at: str = Form(""),
    consent: str = Form(""),
    comment: str = Form(""),
    csrf_token: str = Form(""),
):
    user = await get_current_user(request)
    validate_csrf(user, csrf_token)
    require_role(user, STAFF_ROLES)
    try:
        payload = validate_manual_booking(
            customer_name=customer_name,
            customer_phone=customer_phone,
            service_staff=service_staff,
            starts_at=starts_at,
            consent=consent,
            comment=comment,
        )
        options = await list_booking_service_options(database.get_database())
        if not any(
            option["service_id"] == payload["service_id"]
            and option["staff_id"] == payload["staff_id"]
            for option in options
        ):
            raise ValueError("manual booking")
        await enqueue_admin_booking_command(
            database.get_database(),
            kind=ADMIN_BOOKING_CREATE_KIND,
            payload=payload,
            actor_id=user.id,
            ip_address=request_ip_address(request),
            user_agent=request_user_agent(request),
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception as error:
        logger.error("manual_booking_failed error_type=%s", type(error).__name__)
        raise HTTPException(status_code=503, detail="bookings unavailable") from error
    selected_week = starts_at[:10] if len(starts_at) >= 10 else ""
    return RedirectResponse(
        url=admin_url(request, f"/bookings/?week={selected_week}&notice=queued"),
        status_code=303,
    )


@router.post("/external/{external_id}/status")
async def update_booking_status(
    request: Request,
    external_id: str,
    status: str = Form(""),
    week: str = Form(""),
    csrf_token: str = Form(""),
):
    user = await get_current_user(request)
    validate_csrf(user, csrf_token)
    require_role(user, STAFF_ROLES)
    try:
        external_id, status = validate_booking_status_action(external_id, status)
        await enqueue_admin_booking_command(
            database.get_database(),
            kind=ADMIN_BOOKING_STATUS_KIND,
            payload={"external_id": external_id, "status": status},
            actor_id=user.id,
            ip_address=request_ip_address(request),
            user_agent=request_user_agent(request),
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception as error:
        logger.error("booking_status_failed error_type=%s", type(error).__name__)
        raise HTTPException(status_code=503, detail="bookings unavailable") from error
    suffix = f"?week={week}&notice=queued" if week else "?notice=queued"
    return RedirectResponse(url=admin_url(request, f"/bookings/{suffix}"), status_code=303)


@router.get("/{booking_id}", response_class=HTMLResponse)
async def booking_detail(request: Request, booking_id: UUID):
    user = await get_current_user(request)
    require_role(user, STAFF_ROLES)
    try:
        booking = await get_booking_detail(
            database.get_database(),
            booking_id,
            actor_id=user.id,
            ip_address=request_ip_address(request),
            user_agent=request_user_agent(request),
        )
    except Exception as error:
        logger.error("booking_detail_failed error_type=%s", type(error).__name__)
        raise HTTPException(status_code=503, detail="bookings unavailable") from error
    if booking is None:
        raise HTTPException(status_code=404, detail="booking not found")
    return templates.TemplateResponse(
        request, "booking_detail.html", {"user": user, "booking": booking}
    )
