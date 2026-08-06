"""Admin authentication: DB users plus a local bootstrap fallback."""

import os
import secrets
from dataclasses import dataclass

from fastapi import Request
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

import user_repository
from security import new_csrf_token, verify_password, verify_totp

SESSION_SECRET = os.getenv("ADMIN_SESSION_SECRET", "change-me-min-32-chars-please")
SESSION_COOKIE_NAME = "admin_session"
SESSION_MAX_AGE = int(os.getenv("ADMIN_SESSION_TTL_SEC", str(24 * 60 * 60)))
_DEFAULT_USERNAME = "admin"
_DEFAULT_PASSWORD = "admin"
_DEFAULT_SESSION_SECRET = "change-me-min-32-chars-please"

_serializer = URLSafeTimedSerializer(SESSION_SECRET, salt="admin-session")


class _LoginRequired(Exception):
    """Кидаем когда нет валидной сессии — обработчик редиректит на /login."""


@dataclass(frozen=True, slots=True)
class AuthenticatedUser:
    id: int | None
    username: str
    role: str
    csrf_token: str | None = None
    session_id: str | None = None

    def __str__(self) -> str:
        return self.username


def authenticate(username: str, password: str) -> bool:
    """Legacy bootstrap credential check."""
    expected_username = os.getenv("ADMIN_USERNAME", "")
    expected_password = os.getenv("ADMIN_PASSWORD", "")
    if (
        not expected_username
        or not expected_password
        or expected_username == _DEFAULT_USERNAME
        or expected_password == _DEFAULT_PASSWORD
        or SESSION_SECRET == _DEFAULT_SESSION_SECRET
        or len(SESSION_SECRET) < 32
    ):
        return False
    return username == expected_username and password == expected_password


async def authenticate_admin(
    username: str,
    password: str,
    totp_code: str,
) -> AuthenticatedUser | None:
    user = await user_repository.get_user_by_username(username)
    if user:
        if not user["enabled"]:
            return None
        if not verify_password(user["password_hash"], password):
            return None
        if not verify_totp(user["totp_secret"], totp_code):
            return None
        session_id = secrets.token_urlsafe(32)
        csrf_token = await user_repository.create_session(
            int(user["id"]),
            session_id,
            SESSION_MAX_AGE,
        )
        return AuthenticatedUser(
            id=int(user["id"]),
            username=user["username"],
            role=user["role"],
            csrf_token=csrf_token,
            session_id=session_id,
        )

    if await user_repository.count_admin_users() == 0 and authenticate(username, password):
        return AuthenticatedUser(
            id=None,
            username=username,
            role="owner",
            csrf_token=new_csrf_token(),
            session_id=None,
        )
    return None


def create_session_token(user: AuthenticatedUser | str) -> str:
    """Create a signed session token."""
    if isinstance(user, str):
        user = AuthenticatedUser(
            id=None,
            username=user,
            role="owner",
            csrf_token=None,
            session_id=None,
        )
    return _serializer.dumps(
        {
            "id": user.id,
            "u": user.username,
            "r": user.role,
            "csrf": user.csrf_token,
            "sid": user.session_id,
        }
    )


def verify_session_token(token: str) -> AuthenticatedUser | None:
    """Verify signed session token and return its user payload."""
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE)
        username = data.get("u")
        if not username:
            return None
        return AuthenticatedUser(
            id=data.get("id"),
            username=username,
            role=data.get("r") or "admin",
            csrf_token=data.get("csrf"),
            session_id=data.get("sid"),
        )
    except (BadSignature, SignatureExpired):
        return None


async def get_current_user(request: Request) -> AuthenticatedUser:
    """FastAPI dependency: return current user or redirect to login."""
    token = request.cookies.get(SESSION_COOKIE_NAME)
    if not token:
        raise _LoginRequired()
    user = verify_session_token(token)
    if not user:
        raise _LoginRequired()
    if not user.session_id:
        if (
            user.id is None
            and await user_repository.count_admin_users() == 0
            and authenticate(user.username, os.getenv("ADMIN_PASSWORD", ""))
        ):
            return user
        raise _LoginRequired()
    session = await user_repository.get_active_session(user.session_id)
    if not session:
        raise _LoginRequired()
    if user.id is not None and int(session["user_id"]) != int(user.id):
        raise _LoginRequired()
    if session["username"] != user.username:
        raise _LoginRequired()
    return AuthenticatedUser(
        id=int(session["user_id"]),
        username=session["username"],
        role=session["role"],
        csrf_token=session["csrf_token"],
        session_id=session["session_id"],
    )
