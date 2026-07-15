"""Редактор системного промпта в админке: CRUD версий + rollback + hot-reload.

Поток:
- GET  /prompt/                    → редактор (текущая версия) + список версий
- POST /prompt/save                → сохранить новую версию + публикация в Redis
- GET  /prompt/versions/{id}       → просмотр конкретной версии
- POST /prompt/rollback/{id}       → откатиться на версию (создаёт новую запись с её content)

После каждой записи: пишем prompts/system.md → публикуем в канал prompt:reload.
LLM-контейнер подписан на этот канал и перечитывает файл.
"""

import logging
import os
from pathlib import Path

import redis.asyncio as aioredis
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import prompt_database as pdb
from auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/prompt", tags=["prompt"])

_BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=_BASE_DIR / "templates")

# Путь монтируется через volume в docker-compose: ./llm/prompts:/app/prompts:rw
PROMPT_FILE = Path(os.getenv("PROMPT_FILE_PATH", "/app/prompts/system.md"))
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
PROMPT_RELOAD_CHANNEL = "prompt:reload"


async def _publish_reload(version_id: int) -> None:
    """Опубликовать в Redis канал, чтобы LLM перечитал промпт."""
    client = None
    try:
        client = aioredis.from_url(REDIS_URL, decode_responses=True)
        await client.publish(PROMPT_RELOAD_CHANNEL, f"version:{version_id}")
    except Exception as error:
        logger.error(
            "prompt_reload_publish_failed error_type=%s", type(error).__name__
        )
    finally:
        if client is not None:
            try:
                await client.aclose()
            except Exception as error:
                logger.error(
                    "prompt_reload_redis_close_failed error_type=%s",
                    type(error).__name__,
                )


def _read_current_prompt() -> str:
    if not PROMPT_FILE.exists():
        return ""
    try:
        return PROMPT_FILE.read_text(encoding="utf-8")
    except OSError as error:
        logger.error("prompt_read_failed error_type=%s", type(error).__name__)
        return ""


def _write_prompt(content: str) -> bool:
    try:
        PROMPT_FILE.parent.mkdir(parents=True, exist_ok=True)
        PROMPT_FILE.write_text(content, encoding="utf-8")
        return True
    except OSError as error:
        logger.error("prompt_write_failed error_type=%s", type(error).__name__)
        return False


@router.get("/", response_class=HTMLResponse)
async def prompt_editor(request: Request, saved: str = "", error: str = ""):
    user = get_current_user(request)
    current = _read_current_prompt()
    versions = await pdb.list_versions(limit=50)
    return templates.TemplateResponse(
        request, "prompt_edit.html",
        {
            "user": user,
            "current_content": current,
            "versions": versions,
            "saved": saved,
            "error": error,
            "file_path": str(PROMPT_FILE),
        },
    )


@router.post("/save")
async def prompt_save(
    request: Request,
    content: str = Form(...),
    comment: str = Form(""),
):
    user = get_current_user(request)
    content = content.replace("\r\n", "\n").rstrip() + "\n"

    if not _write_prompt(content):
        return RedirectResponse(url="/prompt/?error=write_failed", status_code=302)

    try:
        version_id = await pdb.create_version(
            content=content, author=user, comment=comment.strip() or None,
        )
    except Exception as error:
        logger.error("prompt_db_save_failed error_type=%s", type(error).__name__)
        return RedirectResponse(url="/prompt/?error=db_failed", status_code=302)

    await _publish_reload(version_id)
    return RedirectResponse(url=f"/prompt/?saved={version_id}", status_code=302)


@router.get("/versions/{version_id}", response_class=HTMLResponse)
async def prompt_version_view(request: Request, version_id: int):
    user = get_current_user(request)
    version = await pdb.get_version(version_id)
    if not version:
        return RedirectResponse(url="/prompt/", status_code=302)
    return templates.TemplateResponse(
        request, "prompt_version.html",
        {"user": user, "version": version},
    )


@router.post("/rollback/{version_id}")
async def prompt_rollback(request: Request, version_id: int):
    user = get_current_user(request)
    version = await pdb.get_version(version_id)
    if not version:
        return RedirectResponse(url="/prompt/?error=version_not_found", status_code=302)

    content = version["content"]
    if not _write_prompt(content):
        return RedirectResponse(url="/prompt/?error=write_failed", status_code=302)

    try:
        new_id = await pdb.create_version(
            content=content,
            author=user,
            comment=f"Откат на версию #{version_id}",
        )
    except Exception as error:
        logger.error(
            "prompt_db_rollback_failed error_type=%s", type(error).__name__
        )
        return RedirectResponse(url="/prompt/?error=db_failed", status_code=302)

    await _publish_reload(new_id)
    return RedirectResponse(url=f"/prompt/?saved={new_id}", status_code=302)
