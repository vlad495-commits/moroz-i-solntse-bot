"""Глобальный тумблер «Бот вкл/выкл».

Флаг хранится в Redis ключе `bot:paused` (значение "1" = пауза, отсутствие = работает).
LLM-контейнер проверяет этот флаг перед каждой LLM-итерацией.
"""

import logging
import os
from pathlib import Path

import redis.asyncio as aioredis
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from auth import get_current_user

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
    try:
        client = await _redis_client()
        paused = bool(await client.get(BOT_PAUSE_KEY))
        await client.aclose()
    except Exception as e:
        logger.exception("Не удалось проверить bot:paused")
        error = str(e)
    return templates.TemplateResponse(
        request, "bot_control.html",
        {"user": user, "paused": paused, "error": error},
    )


@router.post("/toggle")
async def bot_control_toggle(request: Request):
    get_current_user(request)
    try:
        client = await _redis_client()
        if await client.get(BOT_PAUSE_KEY):
            await client.delete(BOT_PAUSE_KEY)
        else:
            await client.set(BOT_PAUSE_KEY, "1")
        await client.aclose()
    except Exception:
        logger.exception("Не удалось переключить тумблер")
    return RedirectResponse(url="/bot-control/", status_code=302)
