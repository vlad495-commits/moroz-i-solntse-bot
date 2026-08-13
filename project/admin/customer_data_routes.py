"""Owner-only route for irreversible local customer-data deletion."""

import logging
import os

import redis.asyncio as aioredis
from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

import database
from audit_repository import request_ip_address, request_user_agent
from auth import get_current_user
from customer_data_deletion import CustomerDataDeletionError, delete_customer_data
from paths import admin_url
from rbac import require_role, validate_csrf


logger = logging.getLogger(__name__)
router = APIRouter(prefix="/customer-data", tags=["customer-data"])
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")


async def _redis_client():
    return aioredis.from_url(REDIS_URL, decode_responses=True)


@router.post("/delete")
async def customer_data_delete(
    request: Request,
    chat_id: int = Form(...),
    confirmation: str = Form(""),
    csrf_token: str = Form(""),
):
    user = await get_current_user(request)
    validate_csrf(user, csrf_token)
    require_role(user, {"owner"})
    if confirmation != "УДАЛИТЬ":
        raise HTTPException(status_code=400, detail="bad_confirmation")
    if database._pool is None:
        return RedirectResponse(
            url=admin_url(request, "/?delete_error=unavailable"),
            status_code=302,
        )

    client = None
    try:
        client = await _redis_client()
        result = await delete_customer_data(
            pool=database._pool,
            redis_client=client,
            chat_id=chat_id,
            actor_id=user.id,
            ip_address=request_ip_address(request),
            user_agent=request_user_agent(request),
        )
        return RedirectResponse(
            url=admin_url(request, f"/?deleted={result.status}"),
            status_code=302,
        )
    except CustomerDataDeletionError as error:
        logger.error(
            "customer_data_delete_failed error_type=%s",
            type(error).__name__,
        )
        return RedirectResponse(
            url=admin_url(request, "/?delete_error=unavailable"),
            status_code=302,
        )
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception as error:
                logger.error(
                    "customer_data_redis_close_failed error_type=%s",
                    type(error).__name__,
                )
