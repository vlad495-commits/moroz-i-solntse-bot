"""Алерты администратору в Telegram через прямой HTTP-запрос к Bot API."""

import logging
import time

import httpx

from config import TELEGRAM_BOT_TOKEN, ADMIN_TG_CHAT_ID, ALERT_RATE_LIMIT_SEC

logger = logging.getLogger(__name__)

_last_alert_time: dict[str, float] = {}
_http_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _http_client
    if _http_client is None:
        _http_client = httpx.AsyncClient(timeout=10.0)
    return _http_client


async def close_alerts() -> None:
    global _http_client
    if _http_client:
        await _http_client.aclose()
        _http_client = None


async def send_admin_alert(
    error_type: str,
    details: str = "",
    severity: str = "CRITICAL",
    chat_id: int | None = None,
    username: str | None = None,
    user_id: int | None = None,
) -> None:
    """Отправить алерт администратору. Rate-limited по типу ошибки."""
    if not ADMIN_TG_CHAT_ID or not TELEGRAM_BOT_TOKEN:
        return

    now = time.time()
    rate_key = f"{severity}:{error_type}"
    last_time = _last_alert_time.get(rate_key, 0)
    if now - last_time < ALERT_RATE_LIMIT_SEC:
        logger.debug("Алерт %s пропущен (rate limit)", rate_key)
        return
    _last_alert_time[rate_key] = now

    icon = "\U0001f534" if severity == "CRITICAL" else "\U0001f7e1"
    parts = [f"{icon} [{severity}] {error_type}"]

    if details:
        truncated = details[:500] + ("..." if len(details) > 500 else "")
        parts.append(f"Детали: {truncated}")

    if username:
        parts.append(f"Пользователь: @{username}")
    elif user_id:
        parts.append(f"User ID: {user_id}")

    if chat_id:
        parts.append(f"Чат: {chat_id}")

    text = "\n".join(parts)

    try:
        client = _get_client()
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        await client.post(url, json={"chat_id": int(ADMIN_TG_CHAT_ID), "text": text})
    except Exception:
        logger.exception("Не удалось отправить алерт администратору")
