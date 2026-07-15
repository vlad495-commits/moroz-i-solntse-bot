"""Просмотр логов бота в админке.

LLM-контейнер пишет логи через RotatingFileHandler в /app/logs/bot.log.
Папка монтируется в admin как ./logs:/app/logs:ro — admin читает.

Эндпоинты:
- GET /logs/                    → страница с фильтрами + tail
- GET /logs/tail?level=&search= → JSON-фрагмент (используется HTMX-полингом или fetch)
"""

import logging
import os
from collections import deque
from pathlib import Path
from typing import Iterable

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from auth import get_current_user

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/logs", tags=["logs"])

_BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=_BASE_DIR / "templates")

LOG_FILE = Path(os.getenv("LOG_FILE", "/app/logs/bot.log"))
TAIL_LINES = int(os.getenv("LOGS_TAIL_LINES", "300"))

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def _read_tail(path: Path, lines: int) -> list[str]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            return list(deque(f, maxlen=lines))
    except OSError as error:
        logger.error("admin_log_read_failed error_type=%s", type(error).__name__)
        return []


def _filter_lines(
    lines: Iterable[str],
    level: str | None,
    search: str | None,
) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    level_norm = (level or "").upper()
    needle = (search or "").lower()
    for raw in lines:
        line = raw.rstrip("\n")
        # Уровень определяем по подстроке "[LEVEL]"
        line_level = ""
        for lvl in LEVELS:
            if f"[{lvl}]" in line:
                line_level = lvl
                break

        if level_norm and level_norm != "ALL" and line_level != level_norm:
            continue
        if needle and needle not in line.lower():
            continue
        out.append({"text": line, "level": line_level or "INFO"})
    return out


@router.get("/", response_class=HTMLResponse)
async def logs_page(
    request: Request,
    level: str = "ALL",
    search: str = "",
):
    user = get_current_user(request)
    raw = _read_tail(LOG_FILE, TAIL_LINES)
    rows = _filter_lines(raw, level, search)
    return templates.TemplateResponse(
        request, "logs.html",
        {
            "user": user,
            "rows": rows,
            "level": level,
            "search": search,
            "log_file": str(LOG_FILE),
            "log_exists": LOG_FILE.exists(),
            "levels": ("ALL",) + LEVELS,
        },
    )


@router.get("/tail")
async def logs_tail(level: str = "ALL", search: str = "", lines: int = 300):
    raw = _read_tail(LOG_FILE, min(max(lines, 10), 2000))
    rows = _filter_lines(raw, level, search)
    return JSONResponse({"rows": rows, "log_exists": LOG_FILE.exists()})
