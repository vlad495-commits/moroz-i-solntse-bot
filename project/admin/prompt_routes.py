"""Редактор системного промпта в админке: CRUD версий + rollback + hot-reload.

Поток:
- GET  /prompt/                    → редактор (текущая версия) + список версий
- POST /prompt/save                → сохранить новую версию + публикация в Redis
- GET  /prompt/versions/{id}       → просмотр конкретной версии
- POST /prompt/rollback/{id}       → откатиться на версию (создаёт новую запись с её content)

После каждой записи: пишем prompts/system.md → публикуем в канал prompt:reload.
LLM-контейнер подписан на этот канал и перечитывает файл.
"""

import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
from uuid import uuid4

import redis.asyncio as aioredis
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

import prompt_database as pdb
from auth import get_current_user
from audit_repository import record_audit, request_ip_address, request_user_agent
from paths import admin_url
from rbac import require_role, validate_csrf

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/prompt", tags=["prompt"])

_BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=_BASE_DIR / "templates")

# Путь монтируется через volume в docker-compose: ./llm/prompts:/app/prompts:rw
PROMPT_FILE = Path(os.getenv("PROMPT_FILE_PATH", "/app/prompts/system.md"))
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")
PROMPT_RELOAD_CHANNEL = "prompt:reload"
PROMPT_RELOAD_ACK_PREFIX = "prompt:reload:ack:"
PROMPT_RELOAD_ACK_POLLS = 20
PROMPT_RELOAD_ACK_INTERVAL_SECONDS = 0.1
PROMPT_RELOAD_APPLIED = "applied"
PROMPT_RELOAD_REJECTED = "rejected"
PROMPT_RELOAD_UNCONFIRMED = "unconfirmed"
PROMPT_UPDATE_LOCK = asyncio.Lock()


async def _publish_reload(version_id: int, content: str) -> str:
    """Publish a version-bound reload and wait for the worker ACK."""
    client = None
    published = False
    try:
        client = aioredis.from_url(REDIS_URL, decode_responses=True)
        request_id = uuid4().hex
        ack_key = f"{PROMPT_RELOAD_ACK_PREFIX}{request_id}"
        payload = json.dumps(
            {
                "version_id": version_id,
                "request_id": request_id,
                "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            },
            separators=(",", ":"),
        )
        subscribers = await client.publish(
            PROMPT_RELOAD_CHANNEL, payload
        )
        published = bool(subscribers)
        if not published:
            logger.error("prompt_reload_publish_failed error_type=NoSubscribers")
            return PROMPT_RELOAD_REJECTED
        for _ in range(PROMPT_RELOAD_ACK_POLLS):
            acknowledgement = await client.get(ack_key)
            if acknowledgement is not None:
                if acknowledgement != "applied":
                    logger.error(
                        "prompt_reload_apply_failed error_type=Rejected"
                    )
                return (
                    PROMPT_RELOAD_APPLIED
                    if acknowledgement == "applied"
                    else PROMPT_RELOAD_REJECTED
                )
            await asyncio.sleep(PROMPT_RELOAD_ACK_INTERVAL_SECONDS)
        logger.error("prompt_reload_apply_failed error_type=AckTimeout")
        return PROMPT_RELOAD_UNCONFIRMED
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
    return PROMPT_RELOAD_UNCONFIRMED


async def _discard_unactivated_version(version_id: int) -> None:
    try:
        await pdb.delete_version(version_id)
    except Exception as error:
        logger.error(
            "prompt_version_cleanup_failed error_type=%s",
            type(error).__name__,
        )


def _read_prompt_snapshot() -> str | None:
    if not PROMPT_FILE.exists():
        return None
    return PROMPT_FILE.read_text(encoding="utf-8")


def _restore_prompt_if_current(
    expected_content: str,
    previous_content: str | None,
) -> bool:
    try:
        if not PROMPT_FILE.exists():
            if previous_content is None:
                return True
            return _write_prompt(previous_content)
        if PROMPT_FILE.read_text(encoding="utf-8") != expected_content:
            return True
        if previous_content is None:
            PROMPT_FILE.unlink()
            return True
        return _write_prompt(previous_content)
    except OSError as error:
        logger.error(
            "prompt_restore_failed error_type=%s",
            type(error).__name__,
        )
        return False


def _read_current_prompt() -> str:
    if not PROMPT_FILE.exists():
        return ""
    try:
        return PROMPT_FILE.read_text(encoding="utf-8")
    except OSError as error:
        logger.error("prompt_read_failed error_type=%s", type(error).__name__)
        return ""


def _write_prompt(content: str) -> bool:
    temporary_path: Path | None = None
    try:
        PROMPT_FILE.parent.mkdir(parents=True, exist_ok=True)
        descriptor, name = tempfile.mkstemp(
            dir=PROMPT_FILE.parent,
            prefix=f".{PROMPT_FILE.name}.",
            suffix=".tmp",
        )
        temporary_path = Path(name)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, PROMPT_FILE)
        temporary_path = None
        return True
    except OSError as error:
        logger.error("prompt_write_failed error_type=%s", type(error).__name__)
        return False
    finally:
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError as error:
                logger.error(
                    "prompt_temp_cleanup_failed error_type=%s",
                    type(error).__name__,
                )


@router.get("/", response_class=HTMLResponse)
async def prompt_editor(request: Request, saved: str = "", error: str = ""):
    user = await get_current_user(request)
    require_role(user, {"owner"})
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
    csrf_token: str = Form(""),
):
    user = await get_current_user(request)
    validate_csrf(user, csrf_token)
    require_role(user, {"owner"})
    content = content.replace("\r\n", "\n").rstrip() + "\n"

    async with PROMPT_UPDATE_LOCK:
        try:
            previous_content = _read_prompt_snapshot()
        except OSError as error:
            logger.error(
                "prompt_read_failed error_type=%s",
                type(error).__name__,
            )
            return RedirectResponse(
                url=admin_url(request, "/prompt/?error=read_failed"),
                status_code=302,
            )
        try:
            version_id = await pdb.create_version(
                content=content,
                author=user.username[:64],
                comment=comment.strip() or None,
            )
        except Exception as error:
            logger.error(
                "prompt_db_save_failed error_type=%s",
                type(error).__name__,
            )
            return RedirectResponse(
                url=admin_url(request, "/prompt/?error=db_failed"),
                status_code=302,
            )

        if not _write_prompt(content):
            await _discard_unactivated_version(version_id)
            return RedirectResponse(
                url=admin_url(request, "/prompt/?error=write_failed"),
                status_code=302,
            )

        reload_status = await _publish_reload(version_id, content)
        if reload_status == PROMPT_RELOAD_REJECTED:
            restored = _restore_prompt_if_current(content, previous_content)
            await _discard_unactivated_version(version_id)
            return RedirectResponse(
                url=admin_url(
                    request,
                    "/prompt/?error="
                    + ("reload_rejected" if restored else "restore_failed"),
                ),
                status_code=302,
            )

    await record_audit(
        actor_id=user.id,
        action="prompt.save",
        object_type="prompt_version",
        object_id=str(version_id),
        before=None,
        after={"comment": comment.strip() or None},
        ip_address=request_ip_address(request),
        user_agent=request_user_agent(request),
    )
    return RedirectResponse(
        url=admin_url(
            request,
            f"/prompt/?saved={version_id}"
            + (
                ""
                if reload_status == PROMPT_RELOAD_APPLIED
                else "&error=reload_failed"
            ),
        ),
        status_code=302,
    )


@router.get("/versions/{version_id}", response_class=HTMLResponse)
async def prompt_version_view(request: Request, version_id: int):
    user = await get_current_user(request)
    require_role(user, {"owner"})
    version = await pdb.get_version(version_id)
    if not version:
        return RedirectResponse(url=admin_url(request, "/prompt/"), status_code=302)
    return templates.TemplateResponse(
        request, "prompt_version.html",
        {"user": user, "version": version},
    )


@router.post("/rollback/{version_id}")
async def prompt_rollback(
    request: Request,
    version_id: int,
    csrf_token: str = Form(""),
):
    user = await get_current_user(request)
    validate_csrf(user, csrf_token)
    require_role(user, {"owner"})
    version = await pdb.get_version(version_id)
    if not version:
        return RedirectResponse(
            url=admin_url(request, "/prompt/?error=version_not_found"), status_code=302
        )

    content = version["content"]
    async with PROMPT_UPDATE_LOCK:
        try:
            previous_content = _read_prompt_snapshot()
        except OSError as error:
            logger.error(
                "prompt_read_failed error_type=%s",
                type(error).__name__,
            )
            return RedirectResponse(
                url=admin_url(request, "/prompt/?error=read_failed"),
                status_code=302,
            )
        try:
            new_id = await pdb.create_version(
                content=content,
                author=user.username[:64],
                comment=f"Откат на версию #{version_id}",
            )
        except Exception as error:
            logger.error(
                "prompt_db_rollback_failed error_type=%s", type(error).__name__
            )
            return RedirectResponse(
                url=admin_url(request, "/prompt/?error=db_failed"), status_code=302
            )

        if not _write_prompt(content):
            await _discard_unactivated_version(new_id)
            return RedirectResponse(
                url=admin_url(request, "/prompt/?error=write_failed"),
                status_code=302,
            )

        reload_status = await _publish_reload(new_id, content)
        if reload_status == PROMPT_RELOAD_REJECTED:
            restored = _restore_prompt_if_current(content, previous_content)
            await _discard_unactivated_version(new_id)
            return RedirectResponse(
                url=admin_url(
                    request,
                    "/prompt/?error="
                    + ("reload_rejected" if restored else "restore_failed"),
                ),
                status_code=302,
            )

    await record_audit(
        actor_id=user.id,
        action="prompt.rollback",
        object_type="prompt_version",
        object_id=str(version_id),
        before=None,
        after={"new_version_id": new_id},
        ip_address=request_ip_address(request),
        user_agent=request_user_agent(request),
    )
    return RedirectResponse(
        url=admin_url(
            request,
            f"/prompt/?saved={new_id}"
            + (
                ""
                if reload_status == PROMPT_RELOAD_APPLIED
                else "&error=reload_failed"
            ),
        ),
        status_code=302,
    )
