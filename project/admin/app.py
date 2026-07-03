"""FastAPI админка: список диалогов, детали диалога, общая статистика, инциденты."""

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv

# Подгружаем корневой .env
_ROOT_ENV = Path(__file__).resolve().parent.parent.parent / ".env"
if _ROOT_ENV.exists():
    load_dotenv(_ROOT_ENV)

from fastapi import FastAPI, Form, Request  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402

import database  # noqa: E402
from llm_status import get_llm_status  # noqa: E402
from auth import (  # noqa: E402
    _LoginRequired,
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE,
    authenticate,
    create_session_token,
    get_current_user,
)
from pricing import calculate_cost  # noqa: E402
from prompt_routes import router as prompt_router  # noqa: E402
from eval_routes import router as eval_router  # noqa: E402
from bot_control_routes import router as bot_control_router  # noqa: E402
from logs_routes import router as logs_router  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    await database.init_db()
    logger.info("Админ-панель готова")
    yield
    await database.close_db()


app = FastAPI(title="Moroz i Solntse Bot — Admin", docs_url=None, redoc_url=None, lifespan=lifespan)

app.mount("/static", StaticFiles(directory=_BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=_BASE_DIR / "templates")

app.include_router(prompt_router)
app.include_router(eval_router)
app.include_router(bot_control_router)
app.include_router(logs_router)


# Jinja2 фильтры для форматирования
def _fmt_money(value: float) -> str:
    return f"${value:.4f}"


def _fmt_int(value: int) -> str:
    return f"{int(value):,}".replace(",", " ")


templates.env.filters["money"] = _fmt_money
templates.env.filters["int"] = _fmt_int


@app.exception_handler(_LoginRequired)
async def _login_required_handler(request: Request, exc: _LoginRequired):
    return RedirectResponse(url="/login", status_code=302)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    return templates.TemplateResponse(
        request, "login.html", {"error": error}
    )


@app.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    if not authenticate(username, password):
        return RedirectResponse(url="/login?error=invalid", status_code=302)
    token = create_session_token(username)
    response = RedirectResponse(url="/", status_code=302)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
    )
    return response


@app.get("/api/llm-status")
async def llm_status_api(request: Request):
    """JSON статус LLM-провайдеров для polling из навбара."""
    get_current_user(request)  # требует авторизации
    return JSONResponse(await get_llm_status())


@app.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    user = get_current_user(request)

    chats = await database.get_chats_list(limit=100)
    total = await database.get_chats_total()

    # Расчёт стоимости для каждого чата
    for c in chats:
        cost, savings = calculate_cost(
            c["prompt_tokens"], c["completion_tokens"],
            c["cached_tokens"], c.get("last_model"),
        )
        c["cost_usd"] = cost
        c["savings_usd"] = savings

    return templates.TemplateResponse(
        request,
        "chats_list.html",
        {"user": user, "chats": chats, "total": total},
    )


@app.get("/chats/{chat_id}", response_class=HTMLResponse)
async def chat_detail(request: Request, chat_id: int):
    user = get_current_user(request)
    detail = await database.get_chat_detail(chat_id)
    if not detail:
        return RedirectResponse(url="/", status_code=302)

    stats = detail["stats"]
    cost, savings = calculate_cost(
        stats.get("prompt_tokens", 0),
        stats.get("completion_tokens", 0),
        stats.get("cached_tokens", 0),
        stats.get("last_model"),
    )
    stats["cost_usd"] = cost
    stats["savings_usd"] = savings

    return templates.TemplateResponse(
        request,
        "chat_detail.html",
        {"user": user, "chat": detail},
    )


@app.get("/stats", response_class=HTMLResponse)
async def stats_page(request: Request):
    user = get_current_user(request)
    stats = await database.get_global_stats()
    incidents = await database.get_recent_incidents(limit=20)

    cost, savings = calculate_cost(
        stats.get("prompt_tokens", 0),
        stats.get("completion_tokens", 0),
        stats.get("cached_tokens", 0),
        os.getenv("LLM_MODEL", "gpt-4.1-mini"),
    )
    stats["cost_usd"] = cost
    stats["savings_usd"] = savings

    return templates.TemplateResponse(
        request,
        "stats.html",
        {"user": user, "stats": stats, "incidents": incidents},
    )
