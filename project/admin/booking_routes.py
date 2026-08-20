"""Read-only staff views for the local booking projection."""

import logging
from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

import database
from audit_repository import request_ip_address, request_user_agent
from auth import get_current_user
from booking_views import decode_booking_cursor, validate_booking_filters
from bookings_database import get_booking_detail, list_bookings
from rbac import require_role


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bookings", tags=["bookings"])
templates = Jinja2Templates(directory=Path(__file__).resolve().parent / "templates")
STAFF_ROLES = {"owner", "admin"}


@router.get("/", response_class=HTMLResponse)
async def booking_list(
    request: Request,
    view: str = "upcoming",
    status: str | None = None,
    source: str = "all",
    reconciliation: str = "all",
    cursor: str | None = None,
):
    user = await get_current_user(request)
    require_role(user, STAFF_ROLES)
    status = status or None
    try:
        validate_booking_filters(view, status, source, reconciliation)
        decode_booking_cursor(cursor)
        page = await list_bookings(
            database.get_database(),
            view=view,
            status=status,
            source=source,
            reconciliation=reconciliation,
            cursor=cursor,
        )
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
            "view": view,
            "status": status,
            "source": source,
            "reconciliation": reconciliation,
        },
    )


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
