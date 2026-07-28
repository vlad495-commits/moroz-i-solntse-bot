"""Глобальный тумблер «Бот вкл/выкл».

Флаг хранится в Redis ключе `bot:paused` (значение "1" = пауза, отсутствие = работает).
LLM-контейнер проверяет этот флаг перед каждой LLM-итерацией.
"""

import logging
import os
from pathlib import Path

import redis.asyncio as aioredis
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from auth import get_current_user
from audit_repository import record_audit, request_ip_address, request_user_agent
from rbac import validate_csrf

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/bot-control", tags=["bot-control"])

_BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=_BASE_DIR / "templates")

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
BOT_PAUSE_KEY = "bot:paused"


async def _redis_client():
    return aioredis.from_url(REDIS_URL, decode_responses=True)


@router.get("/", response_class=HTMLResponse)
async def bot_control_page(request: Request):
    user = get_current_user(request)
    paused = False
    error = ""
    client = None
    try:
        client = await _redis_client()
        paused = bool(await client.get(BOT_PAUSE_KEY))
    except Exception as redis_error:
        logger.error(
            "bot_control_read_failed error_type=%s",
            type(redis_error).__name__,
        )
        error = "Сервис временно недоступен"
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception as close_error:
                logger.error(
                    "bot_control_close_failed error_type=%s",
                    type(close_error).__name__,
                )
    return templates.TemplateResponse(
        request, "bot_control.html",
        {"user": user, "paused": paused, "error": error, "csrf_token": user.csrf_token},
    )


@router.post("/toggle")
async def bot_control_toggle(request: Request, csrf_token: str = Form("")):
    user = get_current_user(request)
    validate_csrf(user, csrf_token)
    client = None
    before = None
    after = None
    try:
        client = await _redis_client()
        if await client.get(BOT_PAUSE_KEY):
            before = {"paused": True}
            await client.delete(BOT_PAUSE_KEY)
            after = {"paused": False}
        else:
            before = {"paused": False}
            await client.set(BOT_PAUSE_KEY, "1")
            after = {"paused": True}
        await record_audit(
            actor_id=user.id,
            action="bot.toggle",
            object_type="bot_control",
            object_id=None,
            before=before,
            after=after,
            ip_address=request_ip_address(request),
            user_agent=request_user_agent(request),
        )
    except Exception as redis_error:
        logger.error(
            "bot_control_toggle_failed error_type=%s",
            type(redis_error).__name__,
        )
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception as close_error:
                logger.error(
                    "bot_control_close_failed error_type=%s",
                    type(close_error).__name__,
                )
    return RedirectResponse(url="/bot-control/", status_code=302)
