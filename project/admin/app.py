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

from fastapi import FastAPI, Form, HTTPException, Query, Request  # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse  # noqa: E402
from fastapi.staticfiles import StaticFiles  # noqa: E402
from fastapi.templating import Jinja2Templates  # noqa: E402

import database  # noqa: E402
import user_repository  # noqa: E402
from audit_repository import (  # noqa: E402
    record_audit,
    request_ip_address,
    request_user_agent,
)
from llm_status import get_llm_status  # noqa: E402
from auth import (  # noqa: E402
    _LoginRequired,
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE,
    authenticate_admin,
    create_session_token,
    get_current_user,
    verify_session_token,
)
from pricing import calculate_cost, summarize_usage_groups  # noqa: E402
from prompt_routes import router as prompt_router  # noqa: E402
from eval_routes import cancel_eval_tasks, router as eval_router  # noqa: E402
from bot_control_routes import router as bot_control_router  # noqa: E402
from logs_routes import router as logs_router  # noqa: E402
from metrics_routes import router as metrics_router  # noqa: E402
from customer_data_routes import router as customer_data_router  # noqa: E402
from escalation_routes import router as escalation_router  # noqa: E402
from booking_routes import router as booking_router  # noqa: E402
from reactivation_routes import router as reactivation_router  # noqa: E402
from statistics_routes import router as statistics_router  # noqa: E402
from paths import admin_url  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)
ADMIN_COOKIE_SECURE = os.getenv("ADMIN_COOKIE_SECURE", "").lower() in {"1", "true", "yes"}
ADMIN_ROOT_PATH = os.getenv("ADMIN_ROOT_PATH", "").rstrip("/")

_BASE_DIR = Path(__file__).resolve().parent


@asynccontextmanager
async def lifespan(app: FastAPI):
    await database.init_db()
    logger.info("Админ-панель готова")
    try:
        yield
    finally:
        await cancel_eval_tasks()
        await database.close_db()


app = FastAPI(
    title="Moroz i Solntse Bot — Admin",
    docs_url=None,
    redoc_url=None,
    lifespan=lifespan,
    root_path=ADMIN_ROOT_PATH,
)

app.mount("/static", StaticFiles(directory=_BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=_BASE_DIR / "templates")

app.include_router(prompt_router)
app.include_router(eval_router)
app.include_router(bot_control_router)
app.include_router(logs_router)
app.include_router(metrics_router)
app.include_router(customer_data_router)
app.include_router(escalation_router)
app.include_router(booking_router)
app.include_router(reactivation_router)
app.include_router(statistics_router)


# Jinja2 фильтры для форматирования
def _fmt_money(value: float) -> str:
    return f"${value:.4f}"


def _fmt_int(value: int) -> str:
    return f"{int(value):,}".replace(",", " ")


templates.env.filters["money"] = _fmt_money
templates.env.filters["int"] = _fmt_int


@app.exception_handler(_LoginRequired)
async def _login_required_handler(request: Request, exc: _LoginRequired):
    return RedirectResponse(url=admin_url(request, "/login"), status_code=302)


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
    totp_code: str = Form(""),
):
    user = await authenticate_admin(username, password, totp_code)
    if not user:
        return RedirectResponse(
            url=admin_url(request, "/login?error=invalid"), status_code=302
        )
    token = create_session_token(user)
    response = RedirectResponse(url=admin_url(request, "/"), status_code=302)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        samesite="lax",
        secure=ADMIN_COOKIE_SECURE or request.url.scheme == "https",
    )
    return response


@app.get("/api/llm-status")
async def llm_status_api(request: Request):
    """JSON статус LLM-провайдеров для polling из навбара."""
    await get_current_user(request)  # требует авторизации
    return JSONResponse(await get_llm_status())


@app.get("/logout")
async def logout(request: Request):
    token = request.cookies.get(SESSION_COOKIE_NAME)
    user = verify_session_token(token) if token else None
    if user and user.session_id:
        await user_repository.delete_session(user.session_id)
    response = RedirectResponse(url=admin_url(request, "/login"), status_code=302)
    response.delete_cookie(SESSION_COOKIE_NAME)
    return response


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    user = await get_current_user(request)

    chats = await database.get_chats_list(limit=100)
    total = await database.get_chats_total()
    summary = await database.get_global_stats()
    summary_cost, _ = calculate_cost(
        summary.get("prompt_tokens", 0),
        summary.get("completion_tokens", 0),
        summary.get("cached_tokens", 0),
        os.getenv("LLM_MODEL", "gpt-4.1-mini"),
    )
    summary["cost_usd"] = summary_cost
    llm_models = {
        "main": os.getenv("LLM_MODEL", "не настроена"),
        "reserve": os.getenv("RESERVE_MODEL") or "не настроена",
    }

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
        {
            "user": user,
            "chats": chats,
            "total": total,
            "summary": summary,
            "llm_models": llm_models,
        },
    )


@app.get("/chats/{chat_id}", response_class=HTMLResponse)
async def chat_detail(
    request: Request,
    chat_id: int,
    events_cursor: str | None = Query(None, max_length=2048),
):
    user = await get_current_user(request)
    detail = await database.get_chat_detail(chat_id)
    if not detail:
        return RedirectResponse(url=admin_url(request, "/"), status_code=302)
    try:
        events = await database.get_customer_events(
            chat_id,
            limit=50,
            cursor=events_cursor,
        )
    except ValueError as error:
        raise HTTPException(status_code=422, detail="invalid events cursor") from error
    await record_audit(
        actor_id=user.id,
        action="chat.view",
        object_type="chat",
        object_id=str(chat_id),
        before=None,
        after=None,
        ip_address=request_ip_address(request),
        user_agent=request_user_agent(request),
    )

    stats = detail["stats"]
    cost, savings = calculate_cost(
        stats.get("prompt_tokens", 0),
        stats.get("completion_tokens", 0),
        stats.get("cached_tokens", 0),
        stats.get("last_model"),
    )
    stats["cost_usd"] = cost
    stats["savings_usd"] = savings
    for message in detail["messages"]:
        if message["role"] == "user" and message["llm_usage_state"] == "used":
            message["llm_usage"] = summarize_usage_groups(message["usage_groups"])

    return templates.TemplateResponse(
        request,
        "chat_detail.html",
        {"user": user, "chat": detail, "events": events},
    )
