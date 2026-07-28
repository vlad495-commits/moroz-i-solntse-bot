"""Minimal admin role and CSRF checks."""

from __future__ import annotations

import hmac

from fastapi import HTTPException

from auth import AuthenticatedUser


def require_role(user: AuthenticatedUser, allowed: set[str]) -> None:
    if user.role not in allowed:
        raise HTTPException(status_code=403, detail="forbidden")


def validate_csrf(user: AuthenticatedUser, csrf_token: str) -> None:
    expected = user.csrf_token or ""
    actual = csrf_token or ""
    if not expected or not hmac.compare_digest(expected, actual):
        raise HTTPException(status_code=403, detail="bad_csrf")
