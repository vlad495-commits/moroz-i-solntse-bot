"""Авторизация админки: проверка логина/пароля + session cookies с TTL."""

import os

from fastapi import Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin")
SESSION_SECRET = os.getenv("ADMIN_SESSION_SECRET", "change-me-min-32-chars-please")
SESSION_COOKIE_NAME = "admin_session"
SESSION_MAX_AGE = int(os.getenv("ADMIN_SESSION_TTL_SEC", str(24 * 60 * 60)))

_serializer = URLSafeTimedSerializer(SESSION_SECRET, salt="admin-session")


class _LoginRequired(Exception):
    """Кидаем когда нет валидной сессии — обработчик редиректит на /login."""


def authenticate(username: str, password: str) -> bool:
    """Проверить логин/пароль."""
    return username == ADMIN_USERNAME and password == ADMIN_PASSWORD


def create_session_token(username: str) -> str:
    """Создать подписанный токен сессии."""
    return _serializer.dumps({"u": username})


def verify_session_token(token: str) -> str | None:
    """Проверить токен (подпись + срок). Возвращает username или None."""
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE)
        return data.get("u")
    except (BadSignature, SignatureExpired):
        return None


def get_current_user(request: Request) -> str:
    """FastAPI dependency: вытащить текущего юзера из cookie или редирект на login."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise _LoginRequired()
    username = verify_session_token(token)
    if not username:
        raise _LoginRequired()
    return username
