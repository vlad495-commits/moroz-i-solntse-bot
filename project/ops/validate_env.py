"""Fail-closed production environment validation."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping


REQUIRED = (
    "PUBLIC_DOMAIN",
    "PUBLIC_BASE_URL",
    "POSTGRES_USER",
    "POSTGRES_PASSWORD",
    "POSTGRES_DB",
    "REDIS_PASSWORD",
    "REDIS_URL",
    "RABBITMQ_USER",
    "RABBITMQ_PASSWORD",
    "RABBITMQ_URL",
    "TELEGRAM_BOT_TOKEN",
    "TELEGRAM_WEBHOOK_SECRET",
    "ADMIN_USERNAME",
    "ADMIN_PASSWORD",
    "ADMIN_SESSION_SECRET",
    "YCLIENTS_PARTNER_TOKEN",
    "YCLIENTS_USER_TOKEN",
    "YCLIENTS_COMPANY_ID",
    "YCLIENTS_BASE_URL",
)

DEFAULT_SESSION_SECRETS = {
    "change-me-in-production",
    "change-me-min-32-chars-please",
}


def validate(env: Mapping[str, str]) -> list[str]:
    errors: list[str] = []
    for name in REQUIRED:
        if not (env.get(name) or "").strip():
            errors.append(f"{name} is required")

    public_base_url = (env.get("PUBLIC_BASE_URL") or "").strip()
    if public_base_url and not public_base_url.startswith("https://"):
        errors.append("PUBLIC_BASE_URL must start with https://")

    if (env.get("ADMIN_USERNAME") or "").strip() == "admin":
        errors.append("ADMIN_USERNAME must not be the default admin user")
    if (env.get("ADMIN_PASSWORD") or "").strip() == "admin":
        errors.append("ADMIN_PASSWORD must not be the default admin password")

    session_secret = (env.get("ADMIN_SESSION_SECRET") or "").strip()
    if session_secret in DEFAULT_SESSION_SECRETS:
        errors.append("ADMIN_SESSION_SECRET must not use a default value")
    if session_secret and len(session_secret) < 32:
        errors.append("ADMIN_SESSION_SECRET must be at least 32 characters")

    return errors


def main() -> int:
    errors = validate(os.environ)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    print("production env validation passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
